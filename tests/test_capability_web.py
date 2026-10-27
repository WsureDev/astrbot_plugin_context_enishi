from __future__ import annotations

import ast
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

from astrbot_plugin_context_enishi.capabilities.web import CapabilityAPI

from .capability_support import Tool, publish
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


@pytest.mark.parametrize("endpoint", ["settings", "group", "tool"])
async def test_save_with_native_plugin_request(native_web, capabilities, endpoint):
    publish(capabilities, [Tool("lookup")])
    entry = next(iter(capabilities.entries.values()))
    capabilities.config = type("Config", (dict,), {"save_config": Mock()})(
        capabilities.config
    )
    refresh = Mock()
    capabilities.request_refresh = refresh
    api = CapabilityAPI(capabilities.context, capabilities)
    bodies = {
        "settings": {"enabled": False, "new_group_mode": "primary", "idle_rounds": 7},
        "group": {"group": entry.group, "mode": "primary", "name": "常用工具"},
        "tool": {"id": entry.id, "mode": "primary"},
    }
    response = await call_endpoint(
        native_web, api, endpoint, json.dumps(bodies[endpoint]).encode()
    )
    assert response.status_code == 200
    assert json.loads(response.body) == {"ok": True}
    if endpoint == "settings":
        assert capabilities.policy.enabled is False
        assert capabilities.policy.new_group_mode == "primary"
        assert capabilities.policy.idle_rounds == 7
        capabilities.config.save_config.assert_called_once_with()
        refresh.assert_called_once_with()
    elif endpoint == "group":
        assert capabilities.store.groups[entry.group]["mode"] == "primary"
        assert capabilities.store.groups[entry.group]["name"] == "常用工具"
    else:
        assert capabilities.store.tools[entry.id]["mode"] == "primary"


@pytest.mark.parametrize("endpoint", ["settings", "group", "tool"])
@pytest.mark.parametrize("body", [b"not json", b"[]", b"null"])
async def test_invalid_request_returns_400(native_web, capabilities, endpoint, body):
    api = CapabilityAPI(capabilities.context, capabilities)
    before = capabilities.store.snapshot()
    response = await call_endpoint(native_web, api, endpoint, body)
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "请求必须是 JSON 对象"}
    assert capabilities.store.snapshot() == before
