from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import mcp.types
import pytest

from .capability_support import capabilities as capabilities


def spec(name):
    return mcp.types.Tool(name=name, inputSchema={"type": "object", "properties": {}})


async def test_paginated_mcp_inventory_is_published_as_one_complete_list(capabilities):
    class Session:
        list_tools = AsyncMock(
            return_value=mcp.types.ListToolsResult(tools=[spec("second")])
        )

    class Client:
        def __init__(self):
            self.tools = []
            self.session = Session()

        async def list_tools_and_save(self):
            response = mcp.types.ListToolsResult(
                tools=[spec("first")], nextCursor="next"
            )
            self.tools = response.tools
            return response

    client = Client()
    capabilities.context.get_llm_tool_manager = lambda: SimpleNamespace(
        mcp_client_dict={"server": client}
    )
    capabilities.request_refresh = lambda: None
    observer = capabilities.observer
    observer.reconcile = AsyncMock()
    observer.install(type("Manager", (), {}), client_class=Client)
    result = await client.list_tools_and_save()
    assert [tool.name for tool in client.tools] == ["first", "second"]
    assert result.nextCursor is None and len(result.tools) == 2
    observer.reconcile.assert_awaited_once_with(client)
    client.session.list_tools.assert_awaited_once_with(cursor="next")


@pytest.mark.parametrize("failure", ["network", "cursor_loop"])
async def test_failed_pagination_does_not_remove_previously_registered_tools(
    capabilities, failure
):
    class Session:
        async def list_tools(self, cursor=None):
            if failure == "network":
                raise RuntimeError("offline")
            return mcp.types.ListToolsResult(tools=[spec("second")], nextCursor="next")

    class Client:
        def __init__(self):
            self.tools = [spec("previous")]
            self.session = Session()

        async def list_tools_and_save(self):
            response = mcp.types.ListToolsResult(
                tools=[spec("first")], nextCursor="next"
            )
            self.tools = response.tools
            return response

    client = Client()
    capabilities.context.get_llm_tool_manager = lambda: SimpleNamespace(
        mcp_client_dict={"server": client}
    )
    capabilities.request_refresh = lambda: None
    observer = capabilities.observer
    observer.reconcile = AsyncMock()
    observer.install(type("Manager", (), {}), client_class=Client)
    await client.list_tools_and_save()
    assert [tool.name for tool in client.tools] == ["previous"]
    observer.reconcile.assert_not_awaited()
    assert capabilities.errors


async def test_unload_cancels_owned_inventory_requests_without_closing_mcp_client(
    capabilities,
):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class Client:
        async def list_tools_and_save(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    client = Client()
    observer = capabilities.observer
    observer.schedule(client)
    await started.wait()
    await observer.close()
    assert cancelled.is_set() and not observer.refreshing
