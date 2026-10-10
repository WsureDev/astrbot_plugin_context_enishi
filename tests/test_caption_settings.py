from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_context_enishi.channels.media import MAX_CAPTION_BYTES, ChannelError
from astrbot_plugin_context_enishi.settings import Settings

from .conftest import Event


def test_ui_is_channel_first_with_native_model_selectors():
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    assert next(iter(schema)) == "channel_settings"
    assert schema["channel_settings"]["type"] == "template_list"
    assert set(schema["channel_settings"]["templates"]) == {"qq", "telegram"}
    assert not {
        "default_mode",
        "channels",
        "max_cache_mb",
        "max_download_mb",
        "cache_ttl_hours",
    }.intersection(schema)
    for template in schema["channel_settings"]["templates"].values():
        items = template["items"]["context"]["items"]
        assert next(iter(items)) == "image_mode"
        assert items["image_mode"]["labels"] == [
            "关闭",
            "仅本轮消息及引用允许图片内容",
            "始终使用图片 ID 引用图片",
        ]
        assert items["caption_provider_id"]["_special"] == "select_provider"
        assert items["caption_fallback_provider_ids"]["_special"] == "select_providers"
        assert items["caption_cache_ttl_hours"]["default"] == 0
    for row in schema["channel_settings"]["default"]:
        assert (
            Settings({"channel_settings": [row]})
            .for_event(
                Event(
                    platform="aiocqhttp"
                    if row["__template_key"] == "qq"
                    else "telegram"
                )
            )
            .mode
            == "disabled"
        )


def test_channels_have_independent_modes_and_model_chains():
    settings = Settings(
        {
            "channel_settings": [
                {
                    "__template_key": "qq",
                    "image_mode": "current_turn",
                    "caption_provider_id": "qq-primary",
                    "caption_fallback_provider_ids": ["qq-backup"],
                },
                {
                    "__template_key": "telegram",
                    "image_mode": "reference_only",
                    "caption_provider_id": "tg-primary",
                    "caption_fallback_provider_ids": ["tg-backup1", "tg-backup2"],
                },
            ]
        }
    )
    qq = settings.for_event(Event(platform="aiocqhttp"))
    tg = settings.for_event(Event())
    assert qq.mode == "current_turn" and tg.mode == "reference_only"
    assert qq.caption_fallback_provider_ids == ("qq-backup",)
    assert tg.caption_fallback_provider_ids == ("tg-backup1", "tg-backup2")


async def test_primary_failure_uses_ordered_fallback_and_caches_success(service):
    service.config["channel_settings"][0]["caption_fallback_provider_ids"] = [
        "backup1",
        "backup2",
    ]
    service.context.llm_generate.side_effect = [
        RuntimeError("network error"),
        SimpleNamespace(completion_text=" "),
        SimpleNamespace(completion_text="备用模型转述"),
    ]
    result = await service.policy.describe_image(
        service.event, "native_current", "内容"
    )
    assert result["provider_id"] == "backup2" and not result["cached"]
    assert [
        call.kwargs["chat_provider_id"]
        for call in service.context.llm_generate.call_args_list
    ] == ["vision", "backup1", "backup2"]
    result = await service.policy.describe_image(
        service.event, "native_current", "内容"
    )
    assert result["cached"] and result["provider_id"] == "backup2"
    assert service.context.llm_generate.await_count == 3
    assert service.event.client.get_file.await_count == 2


async def test_text_only_provider_is_skipped_without_image_call(service):
    service.config["channel_settings"][0]["caption_fallback_provider_ids"] = [
        "visual-backup"
    ]
    service.context.get_provider_by_id = lambda value: SimpleNamespace(
        provider_config={
            "modalities": ["text"] if value == "vision" else ["image", "text"]
        }
    )
    result = await service.policy.describe_image(
        service.event, "native_current", "内容"
    )
    assert result["provider_id"] == "visual-backup"
    assert service.context.llm_generate.await_count == 1


async def test_provider_lookup_failure_still_uses_fallback(service):
    service.config["channel_settings"][0]["caption_fallback_provider_ids"] = ["backup"]

    def getter(value):
        if value == "vision":
            raise RuntimeError("unavailable")
        return SimpleNamespace(provider_config={"modalities": ["image"]})

    service.context.get_provider_by_id = getter
    result = await service.policy.describe_image(
        service.event, "native_current", "内容"
    )
    assert result["provider_id"] == "backup"
    assert service.context.llm_generate.await_count == 1


async def test_empty_primary_uses_framework_image_caption_model(service):
    service.config["channel_settings"][0]["caption_provider_id"] = ""
    service.context.get_config = lambda **kwargs: {
        "provider_settings": {"default_image_caption_provider_id": "framework-vision"}
    }
    result = await service.policy.describe_image(
        service.event, "native_current", "内容"
    )
    assert result["provider_id"] == "framework-vision"


async def test_all_models_failed_does_not_cache_error(service):
    service.config["channel_settings"][0]["caption_fallback_provider_ids"] = ["backup"]
    service.context.llm_generate.side_effect = RuntimeError("private backend error")
    with pytest.raises(ChannelError, match="备用模型均失败"):
        await service.policy.describe_image(service.event, "native_current", "内容")
    assert service.cache.db.execute("SELECT count(*) FROM captions").fetchone()[0] == 0
    assert service.context.llm_generate.await_count == 2


async def test_caption_cache_does_not_expire_by_default_and_saves_no_image_file(
    service, monkeypatch
):
    await service.policy.describe_image(service.event, "native_current", "内容")
    monkeypatch.setattr(
        "astrbot_plugin_context_enishi.context.storage.time.time", lambda: 9_999_999_999
    )
    result = await service.policy.describe_image(
        service.event, "native_current", "内容"
    )
    assert result["cached"] and service.context.llm_generate.await_count == 1
    assert not list(service.tmp.rglob("*.png"))
    assert not hasattr(service.cache, "file") and not hasattr(
        service.cache, "save_file"
    )


async def test_explicit_caption_expiration_works(service, monkeypatch):
    service.config["channel_settings"][0]["caption_cache_ttl_hours"] = 1
    await service.policy.describe_image(service.event, "native_current", "内容")
    monkeypatch.setattr(
        "astrbot_plugin_context_enishi.context.storage.time.time", lambda: 9_999_999_999
    )
    result = await service.policy.describe_image(
        service.event, "native_current", "内容"
    )
    assert not result["cached"] and service.context.llm_generate.await_count == 2


async def test_source_expired_is_not_resurrected_from_cached_description(service):
    await service.policy.describe_image(service.event, "native_current", "内容")
    service.event.client.get_file.side_effect = RuntimeError("expired")
    with pytest.raises(ChannelError, match="get_file failed"):
        await service.policy.describe_image(service.event, "native_current", "内容")
    assert service.context.llm_generate.await_count == 1


async def test_cached_transfer_file_is_not_reused_if_source_expired(service):
    path, _ = await service.channels.fetch_image(service.event, "native_current")
    assert path.is_file()
    service.event.client.get_file.side_effect = RuntimeError("expired")
    with pytest.raises(ChannelError, match="get_file failed"):
        await service.channels.fetch_image(service.event, "native_current")


@pytest.mark.parametrize("size", [MAX_CAPTION_BYTES, MAX_CAPTION_BYTES + 1])
async def test_large_source_is_rejected_before_download_or_model_call(service, size):
    service.media.file_size = size
    with pytest.raises(ChannelError, match="16 MB"):
        await service.policy.describe_image(service.event, "native_current", "内容")
    service.media.download_as_bytearray.assert_not_called()
    service.context.llm_generate.assert_not_called()


async def test_large_bytes_are_rejected_before_model_fallback(service):
    with pytest.raises(ChannelError, match="16 MB"):
        await service.policy.describe_bytes(
            service.event, bytes(MAX_CAPTION_BYTES), "内容"
        )
    service.context.llm_generate.assert_not_called()


async def test_finished_turn_deletes_transfer_files(service):
    from .conftest import request, runner

    path, _ = await service.channels.fetch_image(service.event, "native_current")
    assert path in service.event.temporary_files
    await service.policy.finish(runner(service.event, request(), []))
    assert not path.exists()


def test_history_implementation_and_limits_are_unchanged():
    root = Path(__file__).parents[1]
    previous = subprocess.run(
        ["git", "show", "ab68296:platforms.py"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    def history(code):
        klass = next(
            n
            for n in ast.parse(code).body
            if isinstance(n, ast.ClassDef)
            and any(
                isinstance(method, ast.AsyncFunctionDef)
                and method.name == "user_history"
                for method in n.body
            )
        )
        return ast.dump(
            next(
                n
                for n in klass.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "user_history"
            ),
            include_attributes=False,
        )

    assert history(previous) == history((root / "awareness/sources.py").read_text())
    old_schema = json.loads(
        subprocess.run(
            ["git", "show", "ab68296:_conf_schema.json"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    new_schema = json.loads((root / "_conf_schema.json").read_text())
    for key in ("max_history_results", "max_history_pages"):
        assert new_schema[key] == old_schema[key]
