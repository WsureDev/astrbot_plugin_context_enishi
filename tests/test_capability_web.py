from __future__ import annotations

import ast
import asyncio
import contextvars
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Generic, TypeVar, overload
from unittest.mock import Mock

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from astrbot_plugin_context_enishi.capabilities.catalog import tool_entries
from astrbot_plugin_context_enishi.capabilities.web import CapabilityAPI

from .capability_support import Tool, ToolSet, publish
from .capability_support import capabilities as capabilities


@pytest.fixture
def native_web(monkeypatch):
    source = os.environ.get("ASTRBOT_SOURCE")
    if not source:
        pytest.skip("set ASTRBOT_SOURCE for native plugin Web request contracts")
    path = Path(source) / "astrbot/api/web.py"
    tree = ast.parse(path.read_text())
    names = {
        "PluginMultiDict",
        "PluginRequest",
        "PluginRequestProxy",
        "bind_request_context",
    }
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names
    ]
    module = ModuleType("astrbot.api.web")
    namespace = module.__dict__
    namespace.update(
        contextmanager=contextmanager,
        _request_var=contextvars.ContextVar("enishi_test_web_request"),
        Generic=Generic,
        ValueT=TypeVar("ValueT"),
        overload=overload,
    )
    compiled = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            *nodes,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(compiled), str(path), "exec"), namespace)
    module.request = module.PluginRequestProxy()
    # The endpoint payloads are already JSON values; only the response transport
    # is supplied here. Request parsing and context binding use native source.
    module.json_response = JSONResponse
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


async def call_endpoint(native_web, api, endpoint, body):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("test", 80),
            "path": "/capabilities/" + endpoint,
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )
    with native_web.bind_request_context(native_web.PluginRequest(request)):
        return await getattr(api, endpoint)()


async def test_batch_save_with_native_plugin_request(native_web, capabilities):
    publish(capabilities, [Tool("lookup")])
    entry = next(iter(capabilities.entries.values()))
    capabilities.config = type("Config", (dict,), {"save_config": Mock()})(
        capabilities.config
    )
    refresh = Mock()
    capabilities.request_refresh = refresh
    api = CapabilityAPI(capabilities.context, capabilities)
    body = {
        "revision": capabilities.preferences.revision(),
        "settings": {"enabled": False, "new_group_mode": "primary", "idle_rounds": 7},
        "groups": [{"id": entry.group, "mode": "primary", "name": "常用工具"}],
        "tools": [{"id": entry.id, "mode": "folded"}],
    }
    response = await call_endpoint(native_web, api, "commit", json.dumps(body).encode())
    assert response.status_code == 200
    saved = json.loads(response.body)
    assert saved["ok"]
    assert saved["state"]["policy_revision"] != body["revision"]
    assert capabilities.policy.enabled is False
    assert capabilities.policy.new_group_mode == "primary"
    assert capabilities.policy.idle_rounds == 7
    capabilities.config.save_config.assert_called_once_with()
    refresh.assert_called_once_with()
    assert capabilities.store.groups[entry.group]["mode"] == "primary"
    assert capabilities.store.groups[entry.group]["name"] == "常用工具"
    assert capabilities.store.tools[entry.id]["mode"] == "folded"


@pytest.mark.parametrize("endpoint", ["commit", "detail"])
@pytest.mark.parametrize("body", [b"not json", b"[]", b"null"])
async def test_invalid_request_returns_400(native_web, capabilities, endpoint, body):
    api = CapabilityAPI(capabilities.context, capabilities)
    before = capabilities.store.snapshot()
    response = await call_endpoint(native_web, api, endpoint, body)
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "请求必须是 JSON 对象"}
    assert capabilities.store.snapshot() == before


async def test_batch_validation_and_revision_prevent_partial_edits(
    native_web, capabilities
):
    publish(capabilities, [Tool("lookup")])
    entry = next(iter(capabilities.entries.values()))
    api = CapabilityAPI(capabilities.context, capabilities)
    before = capabilities.store.snapshot()
    body = {
        "revision": capabilities.preferences.revision(),
        "groups": [{"id": entry.group, "mode": "primary"}],
        "tools": [{"id": "missing/tool", "mode": "primary"}],
    }
    response = await call_endpoint(native_web, api, "commit", json.dumps(body).encode())
    assert response.status_code == 400
    assert capabilities.store.snapshot() == before
    body["tools"] = []
    body["revision"] = "stale"
    response = await call_endpoint(native_web, api, "commit", json.dumps(body).encode())
    assert response.status_code == 409
    assert capabilities.store.snapshot() == before


async def test_failed_config_write_rolls_back_batch(native_web, capabilities):
    publish(capabilities, [Tool("lookup")])
    await capabilities.persist()
    entry = next(iter(capabilities.entries.values()))
    before = capabilities.store.path.read_text()
    config = type(
        "Config",
        (dict,),
        {"save_config": Mock(side_effect=[OSError("disk full"), None])},
    )(capabilities.config)
    capabilities.config = config
    api = CapabilityAPI(capabilities.context, capabilities)
    body = {
        "revision": capabilities.preferences.revision(),
        "groups": [{"id": entry.group, "mode": "primary"}],
        "settings": {"enabled": False},
    }
    response = await call_endpoint(native_web, api, "commit", json.dumps(body).encode())
    assert response.status_code == 500
    assert capabilities.policy.enabled is True
    assert capabilities.store.groups[entry.group]["mode"] == "folded"
    assert capabilities.store.path.read_text() == before


async def test_detail_returns_original_schema(native_web, capabilities):
    tool = Tool(
        "lookup",
        parameters={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    )
    publish(capabilities, [tool])
    entry = next(iter(capabilities.entries.values()))
    response = await call_endpoint(
        native_web,
        CapabilityAPI(capabilities.context, capabilities),
        "detail",
        json.dumps({"id": entry.id}).encode(),
    )
    assert response.status_code == 200
    assert json.loads(response.body)["parameters"] == tool.parameters


async def test_interrupted_save_finishes_and_preserves_newly_discovered_groups(
    capabilities,
):
    publish(capabilities, [Tool("lookup")])
    await capabilities.persist()
    await asyncio.gather(*list(capabilities.tasks))
    entry = next(iter(capabilities.entries.values()))
    started, release = asyncio.Event(), asyncio.Event()
    original_io = capabilities.io

    async def blocked_io(method, *args):
        if not started.is_set():
            started.set()
            await release.wait()
        return await original_io(method, *args)

    capabilities.io = blocked_io
    task = asyncio.create_task(
        capabilities.preferences.commit(
            {
                "revision": capabilities.preferences.revision(),
                "groups": [{"id": entry.group, "mode": "primary"}],
            }
        )
    )
    await started.wait()
    discovered = tool_entries(
        ToolSet([Tool("new", handler_module_path="data.plugins.new.main")])
    )
    capabilities.entries.update(discovered)
    capabilities.discover(discovered)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await capabilities.persist()
    saved = json.loads(capabilities.store.path.read_text())
    assert saved["groups"][entry.group]["mode"] == "primary"
    assert saved["groups"]["plugin:new"]["mode"] == "folded"
