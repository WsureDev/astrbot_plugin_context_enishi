from __future__ import annotations

import ast
import copy
import json
import os
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import mcp.types

from astrbot_plugin_context_enishi.capabilities.catalog import tool_entries
from astrbot_plugin_context_enishi.capabilities.state import StateStore

from .capability_support import Tool, ToolSet, run_for
from .capability_support import capabilities as capabilities
from .capability_support import native_tools as native_tools
from .conftest import Event


async def test_real_toolset_and_permission_guard_are_preserved(
    capabilities, native_tools
):
    handler = AsyncMock(return_value="original handler")
    raw = native_tools.FunctionTool(
        name="original",
        description="whole description",
        parameters={
            "type": "object",
            "properties": {"choice": {"type": "string", "enum": ["a", "b"]}},
            "required": ["choice"],
        },
        handler=handler,
    )
    manager = SimpleNamespace(_check_tool_permission=AsyncMock(return_value=None))
    guarded = native_tools._PermissionGuardedTool(raw, manager)
    capabilities.tool_class = native_tools.FunctionTool
    capabilities.tool_set_class = native_tools.ToolSet
    run = await run_for(capabilities, [guarded])
    await run.load(["original"])
    selected = run.injected.get_tool("original")
    assert selected.handler is None and selected._wrapped is raw
    assert run.injected.openai_schema()[-1]["function"]["parameters"] == raw.parameters
    context = SimpleNamespace(context=SimpleNamespace(event=Event()))
    assert await selected.call(context, choice="a") == "original handler"
    manager._check_tool_permission.return_value = "permission denied"
    assert await selected.call(context, choice="a") == "permission denied"
    assert handler.await_count == 1


async def test_mcp_reconnect_republishes_native_tools_and_preserves_disabled_state(
    capabilities, native_tools, monkeypatch
):
    root = Path(os.environ["ASTRBOT_SOURCE"])
    tree = ast.parse((root / "astrbot/core/agent/mcp_client.py").read_text())
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        and node.name in {"MCPTool", "_normalize_mcp_input_schema"}
    ]
    namespace = {**native_tools.__dict__, "re": re, "copy": copy}
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            *nodes,
        ],
        type_ignores=[],
    )
    exec(
        compile(ast.fix_missing_locations(module), "<native MCPTool>", "exec"),
        namespace,
    )
    MCPTool = namespace["MCPTool"]
    native_module = ModuleType("astrbot.core.agent.mcp_client")
    native_module.MCPTool = MCPTool
    core = ModuleType("astrbot.core")
    core.sp = SimpleNamespace(get_async=AsyncMock(return_value=["disabled"]))
    monkeypatch.setitem(sys.modules, "astrbot.core", core)
    monkeypatch.setitem(sys.modules, native_module.__name__, native_module)

    def spec(name):
        return mcp.types.Tool(
            name=name,
            inputSchema={"type": "object", "properties": {"q": {"type": "string"}}},
        )

    client = SimpleNamespace(tools=[spec("new.name"), spec("disabled")])
    old = MCPTool(mcp_tool=spec("removed"), mcp_client=client, mcp_server_name="server")
    disabled = MCPTool(
        mcp_tool=spec("disabled"), mcp_client=client, mcp_server_name="server"
    )
    disabled.active = False
    manager = SimpleNamespace(
        func_list=[old, disabled],
        builtin_func_list={},
        mcp_client_dict={"server": client},
    )
    manager.get_full_tool_set = lambda: ToolSet(manager.func_list)
    capabilities.context.get_llm_tool_manager = lambda: manager
    await capabilities.observer.reconcile(client)
    assert [tool.name for tool in manager.func_list] == ["new_name", "disabled"]
    assert manager.func_list[0].mcp_tool.name == "new.name"
    assert manager.func_list[1].active is False
    assert manager.func_list[0].mcp_client is client
    entries = tool_entries(ToolSet(manager.func_list))
    capabilities.discover(entries)
    assert capabilities.store.groups["mcp:server"]["mode"] == "folded"


async def test_unobserved_list_mutation_is_detected_at_request_boundary(
    capabilities, monkeypatch
):
    class SkillManager:
        def list_skills(self, **kwargs):
            return []

    module = ModuleType("astrbot.core.skills.skill_manager")
    module.SkillManager = SkillManager
    monkeypatch.setitem(sys.modules, module.__name__, module)
    manager = SimpleNamespace(func_list=[Tool("one")], builtin_func_list={})
    manager.get_full_tool_set = lambda: ToolSet(manager.func_list)
    capabilities.context.get_llm_tool_manager = lambda: manager
    await capabilities.refresh()
    revision = capabilities.revision
    manager.func_list.append(Tool("added_without_method"))
    await capabilities.refresh_if_dirty()
    assert capabilities.revision != revision
    assert "plugin:example/added_without_method" in capabilities.entries
    capabilities.store.update("plugin:example", "primary")
    await capabilities.persist()
    state = StateStore(capabilities.store.path.parent)
    assert state.groups["plugin:example"]["mode"] == "primary"
    assert (
        json.loads(capabilities.store.path.read_text())["groups"]["plugin:example"][
            "mode"
        ]
        == "primary"
    )
