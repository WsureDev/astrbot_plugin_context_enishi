from __future__ import annotations

import ast
import copy
import os
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from astrbot_plugin_context_enishi.capabilities.artifacts import ArtifactStore
from astrbot_plugin_context_enishi.capabilities.catalog import tool_entries
from astrbot_plugin_context_enishi.capabilities.service import CapabilityService
from astrbot_plugin_context_enishi.capabilities.state import StateStore

from .conftest import Event


@dataclass
class Tool:
    name: str
    description: str = "Full description"
    parameters: dict = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    handler: object = None
    handler_module_path: str = "data.plugins.example.main"
    active: bool = True


class ToolSet:
    def __init__(self, tools=None):
        self.tools = list(tools or [])

    def get_tool(self, name):
        return next((tool for tool in self.tools if tool.name == name), None)

    def openai_schema(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": copy.deepcopy(tool.parameters),
                },
            }
            for tool in self.tools
        ]


@pytest.fixture
async def capabilities(tmp_path):
    context = SimpleNamespace(
        get_config=lambda **kw: {},
        get_registered_star=lambda name: None,
    )
    config = {"capabilities": {"enabled": True}}
    service = CapabilityService(context, config)
    service.store = StateStore(tmp_path)
    service.artifacts = ArtifactStore(tmp_path)
    service.tool_class, service.tool_set_class = Tool, ToolSet
    service.message_class = lambda **kw: kw
    service.dirty = False
    yield service
    await service.close()


def capability_request(tools=None, *, cid="one", prompt="do the task", skills=None):
    request = SimpleNamespace(
        func_tool=ToolSet(tools),
        system_prompt="persona\n[Other plugin policy] keep it",
        conversation=SimpleNamespace(cid=cid),
        prompt=prompt,
    )
    if skills is not None:
        request._enishi_skill_entries = skills
    return request


async def run_for(service, tools=None, **kw):
    return await service.prepare(Event(), capability_request(tools, **kw))


def publish(service, tools):
    service.entries = tool_entries(ToolSet(tools), service.plugin_map())
    service.discover(service.entries)


@pytest.fixture
def native_tools(monkeypatch):
    source = os.environ.get("ASTRBOT_SOURCE")
    if not source:
        pytest.skip("set ASTRBOT_SOURCE for native tool class contracts")
    import jsonschema
    import mcp
    import pydantic
    from pydantic.dataclasses import dataclass as native_dataclass

    module = ModuleType("enishi_native_tools_contract")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    namespace = module.__dict__
    namespace.update(
        copy=copy,
        jsonschema=jsonschema,
        mcp=mcp,
        Any=typing.Any,
        Generic=typing.Generic,
        Callable=typing.Callable,
        Awaitable=typing.Awaitable,
        AsyncGenerator=typing.AsyncGenerator,
        TContext=typing.TypeVar("TContext"),
        Field=pydantic.Field,
        model_validator=pydantic.model_validator,
        dataclass=native_dataclass,
        ParametersType=dict[str, typing.Any],
        ToolExecResult=str,
        MessageEventResult=object,
        deprecated=lambda **kw: lambda method: method,
    )
    root = Path(source)
    tree = ast.parse((root / "astrbot/core/agent/tool.py").read_text())
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name in {"ToolSchema", "FunctionTool", "ToolSet"}
    ]
    source_module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            *nodes,
        ],
        type_ignores=[],
    )
    exec(
        compile(
            ast.fix_missing_locations(source_module), "<native AstrBot tools>", "exec"
        ),
        namespace,
    )
    tree = ast.parse((root / "astrbot/core/provider/func_tool_manager.py").read_text())
    guard = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "_PermissionGuardedTool"
    )
    source_module.body = [source_module.body[0], guard]
    exec(
        compile(
            ast.fix_missing_locations(source_module),
            "<native permission wrapper>",
            "exec",
        ),
        namespace,
    )
    return module
