from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_context_enishi.capabilities.catalog import skill_entries
from astrbot_plugin_context_enishi.capabilities.skill_source import read_skill

from .capability_support import Tool, run_for
from .capability_support import capabilities as capabilities


async def test_changed_skill_source_replaces_active_body_without_an_extra_model_step(
    capabilities, tmp_path
):
    path = tmp_path / "SKILL.md"
    path.write_text("old instructions")
    skill = SimpleNamespace(
        name="documents", path=str(path), description="docs", source_type="local"
    )
    first = await run_for(
        capabilities, skills=skill_entries([skill], local_metadata=True)
    )
    await first.load(["documents"])
    first.finish(True)
    path.write_text("new complete instructions with a different size")
    second = await run_for(
        capabilities, skills=skill_entries([skill], local_metadata=True)
    )
    messages = []
    await second.before_step(messages, capabilities.message_class)
    assert len(messages) == 1
    text = messages[0]["content"][0]["text"]
    assert "new complete instructions" in text and "old instructions" not in text


@pytest.mark.parametrize(
    ("booter", "source_path", "read_path"),
    [
        ("shipyard_neo", "/workspace/skills/docs/SKILL.md", "skills/docs/SKILL.md"),
        ("shipyard_neo", "skills/docs/SKILL.md", "skills/docs/SKILL.md"),
        (
            "shipyard",
            "/workspace/skills/docs/SKILL.md",
            "/workspace/skills/docs/SKILL.md",
        ),
        ("cua", "/opt/skills/docs/SKILL.md", "/opt/skills/docs/SKILL.md"),
    ],
)
async def test_sandbox_skill_is_read_through_native_runtime_not_host_files(
    capabilities, monkeypatch, booter, source_path, read_path
):
    full = "complete sandbox document\n" * 100000 + "TAIL"
    reader = AsyncMock(return_value={"success": True, "content": full})
    get_booter = AsyncMock(
        return_value=SimpleNamespace(fs=SimpleNamespace(read_file=reader))
    )
    module = ModuleType("astrbot.core.computer.computer_client")
    module.get_booter = get_booter
    monkeypatch.setitem(sys.modules, module.__name__, module)
    capabilities.context.get_config = lambda **kw: {
        "provider_settings": {
            "computer_use_runtime": "sandbox",
            "sandbox": {"booter": booter},
        }
    }
    skill = SimpleNamespace(name="docs", path=source_path, source_type="sandbox_only")
    run = await run_for(capabilities, skills=skill_entries([skill]))
    result = await read_skill(capabilities.context, run.event, skill)
    assert result == full
    reader.assert_awaited_once_with(
        read_path, encoding="utf-8", offset=None, limit=None
    )
    assert skill.path == source_path


async def test_neo_skill_load_with_id_or_name_expands_full_text_and_preserves_path(
    capabilities, monkeypatch
):
    text = "完整 Skill 指令\n" * 1000 + "END"

    async def read_file(path, **kwargs):
        if path.startswith("/"):
            raise ValueError("path must be a relative path")
        assert path == "skills/krea2-prompting-main/SKILL.md"
        assert kwargs == {"encoding": "utf-8", "offset": None, "limit": None}
        return {"success": True, "content": text}

    module = ModuleType("astrbot.core.computer.computer_client")
    module.get_booter = AsyncMock(
        return_value=SimpleNamespace(fs=SimpleNamespace(read_file=read_file))
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    capabilities.context.get_config = lambda **kw: {
        "provider_settings": {"computer_use_runtime": "sandbox"}
    }
    skill = SimpleNamespace(
        name="krea2-prompting-main",
        path="/workspace/skills/krea2-prompting-main/SKILL.md",
        source_type="both",
    )
    run = await run_for(capabilities, skills=skill_entries([skill]))
    entry_id = "skill:both:both/krea2-prompting-main"
    for target in (entry_id, skill.name):
        result = await run.load([target])
        assert result[0]["id"] == entry_id
        assert result[0]["path"] == skill.path
    messages = []
    await run.before_step(messages, capabilities.message_class)
    assert text in messages[0]["content"][0]["text"]
    assert skill.path in messages[0]["content"][0]["text"]


async def test_remote_skill_read_failure_is_returned_to_model_without_partial_loading(
    capabilities, monkeypatch
):
    import json

    class RemoteReadError(Exception):
        pass

    reader = AsyncMock(side_effect=RemoteReadError("unavailable"))
    module = ModuleType("astrbot.core.computer.computer_client")
    module.get_booter = AsyncMock(
        return_value=SimpleNamespace(fs=SimpleNamespace(read_file=reader))
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    capabilities.context.get_config = lambda **kw: {
        "provider_settings": {"computer_use_runtime": "sandbox"}
    }
    skill = SimpleNamespace(
        name="docs", path="/workspace/skills/docs/SKILL.md", source_type="both"
    )
    run = await run_for(capabilities, skills=skill_entries([skill]))
    tool = run.injected.get_tool("enishi_load")
    result = json.loads(await tool.handler(run.event, [skill.name]))
    assert "RemoteReadError: unavailable" in result["error"]
    assert "更换同一 Skill 的 ID 或名称不会改变读取路径" in result["error"]
    assert not run.session.leases and not run.resources.expanded
    assert "Skill read" in capabilities.errors[-1]
    reader.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await tool.handler(run.event, [skill.name])


@pytest.mark.parametrize(
    "path", ["/workspace-other/docs/SKILL.md", "/workspace/../docs/SKILL.md"]
)
async def test_neo_path_outside_workspace_is_not_rewritten_to_another_file(
    capabilities, monkeypatch, path
):
    module = ModuleType("astrbot.core.computer.computer_client")
    module.get_booter = AsyncMock()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    capabilities.context.get_config = lambda **kw: {
        "provider_settings": {"computer_use_runtime": "sandbox"}
    }
    skill = SimpleNamespace(name="docs", path=path, source_type="sandbox_only")
    run = await run_for(capabilities, skills=skill_entries([skill]))
    with pytest.raises(ValueError, match="Neo Skill 路径"):
        await run.load([skill.name])
    module.get_booter.assert_not_called()


async def test_native_file_reads_use_skill_group_lifecycle_when_path_is_registered(
    capabilities, tmp_path
):
    path = tmp_path / "SKILL.md"
    path.write_text("instructions")
    skill = SimpleNamespace(
        name="documents", path=str(path), description="docs", source_type="local"
    )
    run = await run_for(
        capabilities, [Tool("astrbot_file_read_tool")], skills=skill_entries([skill])
    )
    await run.load(["astrbot_file_read_tool"])
    run.calls_started(
        SimpleNamespace(
            tools_call_name=["astrbot_file_read_tool"],
            tools_call_ids=["file"],
            tools_call_args=[{"path": str(path)}],
        )
    )
    run.tool_started("astrbot_file_read_tool")
    message = {
        "role": "tool",
        "tool_call_id": "file",
        "content": "native file read result",
    }
    await run.before_step([message], capabilities.message_class)
    ref = run.session.artifacts["file"]
    owner = run.session.resource_owners[ref]
    assert owner == next(iter(skill_entries([skill])))
    assert run.session.leases[owner].last == 1


async def test_neo_relative_skill_read_refreshes_original_skill_lease(capabilities):
    capabilities.context.get_config = lambda **kw: {
        "provider_settings": {"computer_use_runtime": "sandbox"}
    }
    skill = SimpleNamespace(
        name="documents",
        path="/workspace/skills/documents/SKILL.md",
        source_type="both",
    )
    run = await run_for(
        capabilities, [Tool("astrbot_file_read_tool")], skills=skill_entries([skill])
    )
    await run.load(["astrbot_file_read_tool"])
    run.calls_started(
        SimpleNamespace(
            tools_call_name=["astrbot_file_read_tool"],
            tools_call_ids=["relative-file"],
            tools_call_args=[{"path": "skills/documents/SKILL.md"}],
        )
    )
    run.tool_started("astrbot_file_read_tool")
    owner = next(iter(skill_entries([skill])))
    assert run.call_skills["relative-file"] == owner
    assert owner in run.used and run.session.active(
        owner, capabilities.policy.idle_rounds
    )


def test_workspace_skill_identity_includes_workspace_root():
    one = SimpleNamespace(
        name="same", path="/workspace-one/skills/same/SKILL.md", source_type="workspace"
    )
    two = SimpleNamespace(
        name="same", path="/workspace-two/skills/same/SKILL.md", source_type="workspace"
    )
    assert set(skill_entries([one])).isdisjoint(skill_entries([two]))
