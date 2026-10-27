from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_context_enishi.capabilities.catalog import skill_entries
from astrbot_plugin_context_enishi.capabilities.skills import read_skill

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


async def test_sandbox_skill_is_read_through_native_runtime_not_host_files(
    capabilities, monkeypatch
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
        "provider_settings": {"computer_use_runtime": "sandbox"}
    }
    skill = SimpleNamespace(
        name="docs", path="/workspace/skills/docs/SKILL.md", source_type="sandbox_only"
    )
    run = await run_for(capabilities, skills=skill_entries([skill]))
    result = await read_skill(capabilities.context, run.event, skill)
    assert result == full
    reader.assert_awaited_once_with(
        skill.path, encoding="utf-8", offset=None, limit=None
    )


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


def test_workspace_skill_identity_includes_workspace_root():
    one = SimpleNamespace(
        name="same", path="/workspace-one/skills/same/SKILL.md", source_type="workspace"
    )
    two = SimpleNamespace(
        name="same", path="/workspace-two/skills/same/SKILL.md", source_type="workspace"
    )
    assert set(skill_entries([one])).isdisjoint(skill_entries([two]))
