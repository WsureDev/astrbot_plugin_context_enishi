from __future__ import annotations

import asyncio
import base64

import pytest

from astrbot_plugin_channel_media_tools.native import ImageRef
from astrbot_plugin_channel_media_tools.platforms import ChannelError

from .conftest import image_count, picture, request, runner


async def test_a_current_and_unseen_quote_only_have_native_ids(service):
    req = request(["current.png", "reply.png"])
    await service.policy.prepare_request(service.event, req)
    assert req.image_urls == []
    refs = req.extra_user_content_parts[-1]["text"]
    assert "native_current" in refs and "native_unseen_reply" in refs
    assert service.cache.db.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    service.event.client.get_file.assert_not_called()


async def test_native_reply_is_retrieved_without_prior_reception(service):
    source = (await service.channels.current_images(service.event))[1]
    path, metadata = await service.channels.fetch_image(service.event, source.ref.uri)
    assert path.is_file() and metadata["width"] == 7
    service.event.client.get_file.assert_awaited_once_with(
        file_id="native_unseen_reply"
    )


async def test_descriptions_are_cached_per_content_question_provider(service):
    first = await service.policy.describe_image(
        service.event, "native_unseen_reply", "看到了什么？"
    )
    second = await service.policy.describe_image(
        service.event, "native_unseen_reply", "看到了什么？"
    )
    assert not first["cached"] and second["cached"]
    await service.policy.describe_image(
        service.event, "native_unseen_reply", "文字是什么？"
    )
    service.config["caption_provider_id"] = "another-vision"
    await service.policy.describe_image(
        service.event, "native_unseen_reply", "文字是什么？"
    )
    assert service.context.llm_generate.await_count == 3
    assert service.event.client.get_file.await_count == 1
    assert service.context.llm_generate.call_args.kwargs["contexts"] == []


async def test_concurrent_image_and_caption_requests_do_not_duplicate(service):
    results = await asyncio.gather(
        *(
            service.policy.describe_image(service.event, "native_unseen_reply", "内容")
            for _ in range(5)
        )
    )
    assert service.context.llm_generate.await_count == 1
    assert service.event.client.get_file.await_count == 1
    assert sum(r["cached"] for r in results) == 4


async def test_a_removes_existing_and_tool_pixels_before_model(service, image_bytes):
    url = "data:image/png;base64," + base64.b64encode(image_bytes).decode()
    run = runner(
        service.event,
        request(),
        [
            {"role": "user", "content": [picture(url)]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "[Image from tool 'read_file', path='test.png']",
                    },
                    picture(url),
                ],
            },
        ],
    )
    await service.policy.before_step(run)
    assert image_count(run.run_context.messages) == 0
    assert "蓝色背景" in str(run.run_context.messages)
    assert "base64" not in str(run.run_context.messages)


async def test_b_keeps_only_current_and_quote_then_saves_no_pixels(service):
    service.config["default_mode"] = "B"
    req = request(["current", "quote"])
    await service.policy.prepare_request(service.event, req)
    run = runner(
        service.event,
        req,
        [
            {"role": "user", "content": [picture("old")]},
            {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
            {"role": "user", "content": [picture("current"), picture("quote")]},
        ],
    )
    await service.policy.before_step(run)
    assert image_count(run.run_context.messages) == 2
    assert "old'" not in str(run.run_context.messages)
    await service.policy.finish(run)
    assert image_count(run.run_context.messages) == 0
    assert "native_current" in str(run.run_context.messages)
    assert "native_unseen_reply" in str(run.run_context.messages)


async def test_b_old_duplicate_of_current_picture_is_removed(service):
    service.config["default_mode"] = "B"
    run = runner(
        service.event,
        request(["same"]),
        [
            {"role": "user", "content": [picture("same")]},
            {"role": "user", "content": [picture("same")]},
        ],
    )
    await service.policy.before_step(run)
    assert image_count(run.run_context.messages) == 1


async def test_b_blocks_new_tool_images_in_subsequent_steps(service):
    service.config["default_mode"] = "B"
    run = runner(
        service.event,
        request(["current"]),
        [{"role": "user", "content": [picture("current")]}],
    )
    await service.policy.before_step(run)
    run.run_context.messages.append(
        {"role": "user", "content": [picture("tool-image")]}
    )
    await service.policy.before_step(run)
    assert image_count(run.run_context.messages) == 1
    assert "tool-image" not in str(run.run_context.messages)


async def test_off_has_no_image_changes_or_native_api_calls(service):
    service.config["default_mode"] = "off"
    req = request(["original"])
    run = runner(
        service.event, req, [{"role": "user", "content": [picture("original")]}]
    )
    await service.policy.prepare_request(service.event, req)
    await service.policy.before_step(run)
    await service.policy.finish(run)
    assert req.image_urls == ["original"]
    assert image_count(run.run_context.messages) == 1
    assert not req.extra_user_content_parts


async def test_missing_caption_model_does_not_fall_back_to_main_pixels(service):
    service.config["caption_provider_id"] = ""
    with pytest.raises(ChannelError, match="caption_provider_id"):
        await service.policy.describe_image(service.event, "native_current", "内容")
    service.context.llm_generate.assert_not_called()


async def test_final_payload_guard_filters_direct_follow_up_images(service):
    run = runner(service.event, request(), [])
    payload = service.policy.sanitize_payload(
        run, [{"role": "user", "content": [picture("unseen-followup")]}]
    )
    assert image_count(payload) == 0


async def test_files_are_cached_but_ids_work_after_cache_loss(service):
    ref = ImageRef("telegram", "bot", "-100#2", "image", "native_unseen_reply")
    first, _ = await service.channels.fetch_image(service.event, ref.uri)
    first.unlink()
    second, _ = await service.channels.fetch_image(service.event, ref.uri)
    assert second.is_file() and service.event.client.get_file.await_count == 2


async def test_b_does_not_admit_background_images_without_current_sources(service):
    service.config["default_mode"] = "B"
    service.event.message_obj.raw_message = "background result"
    run = runner(
        service.event,
        request(["generated"]),
        [{"role": "user", "content": [picture("generated")]}],
    )
    await service.policy.before_step(run)
    assert image_count(run.run_context.messages) == 0


async def test_b_drops_additional_provider_image_attachment_path(service):
    service.config["default_mode"] = "B"
    req = request(["current"])
    req.extra_user_content_parts = [picture("foreign-extra")]
    await service.policy.prepare_request(service.event, req)
    assert all(p["type"] != "image_url" for p in req.extra_user_content_parts)
