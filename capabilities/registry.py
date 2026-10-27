from __future__ import annotations

import asyncio
import inspect
import weakref
from contextvars import ContextVar

from .catalog import digest, unwrap
from .patches import Patches

MCP_OWNER = ContextVar("enishi_mcp_owner", default=None)


class RegistryObserver:
    """Observe native mutations and bridge MCP list changes without blocking its reader."""

    def __init__(self, service):
        self.service = service
        self.patches = Patches()
        self.sessions = weakref.WeakKeyDictionary()
        self.refreshing = set()
        self.pending_refresh = set()
        self.poll_task = None
        self.pull_tasks = set()
        self.notifications_supported = False
        self.activation_epoch = 0
        self.activation_pending = 0
        self.activation_settled = asyncio.Event()
        self.activation_settled.set()

    def install(
        self, manager_class=None, context_class=None, client_class=None, sdk_class=None
    ):
        if manager_class is None:
            from astrbot.core.agent.mcp_client import MCPClient
            from astrbot.core.provider.func_tool_manager import FunctionToolManager
            from astrbot.core.star.context import Context
            from mcp import ClientSession

            manager_class, context_class = FunctionToolManager, Context
            client_class, sdk_class = MCPClient, ClientSession
        for target, names in (
            (
                manager_class,
                (
                    "add_func",
                    "remove_func",
                    "activate_llm_tool",
                    "deactivate_llm_tool",
                    "activate_llm_tool_async",
                    "deactivate_llm_tool_async",
                    "_start_mcp_server",
                    "_terminate_mcp_client",
                ),
            ),
            (context_class, ("add_llm_tools",)),
        ):
            if target is not None:
                for name in names:
                    factory = self._mutation
                    if name == "_start_mcp_server":
                        factory = self._starting
                    self.patches.add(target, name, factory)
        if client_class is not None:
            self.patches.add(client_class, "_do_connect", self._connected)
            self.patches.add(client_class, "list_tools_and_save", self._listed)
        if sdk_class is not None:
            self.notifications_supported = self.patches.add(
                sdk_class, "_received_notification", self._notification
            )
        self.bind_sessions()
        self.poll_task = asyncio.create_task(self._poll(), name="enishi-catalog-poll")

    def _mutation(self, original):
        toggles = original.__name__ in {
            "activate_llm_tool",
            "deactivate_llm_tool",
            "activate_llm_tool_async",
            "deactivate_llm_tool_async",
        }
        if inspect.iscoroutinefunction(original):

            async def wrapped(*args, **kwargs):
                if toggles:
                    self._toggle_started()
                try:
                    return await original(*args, **kwargs)
                finally:
                    if toggles:
                        self._toggle_finished()
                    self.service.request_refresh()
        else:

            def wrapped(*args, **kwargs):
                if toggles:
                    self._toggle_started()
                try:
                    return original(*args, **kwargs)
                finally:
                    if toggles:
                        self._toggle_finished()
                    self.service.request_refresh()

        return wrapped

    def _toggle_started(self):
        self.activation_epoch += 1
        self.activation_pending += 1
        self.activation_settled.clear()

    def _toggle_finished(self):
        self.activation_epoch += 1
        self.activation_pending -= 1
        if not self.activation_pending:
            self.activation_settled.set()

    def _starting(self, original):
        async def wrapped(manager, *args, **kwargs):
            token = MCP_OWNER.set(manager)
            try:
                return await original(manager, *args, **kwargs)
            finally:
                MCP_OWNER.reset(token)
                self.service.request_refresh()

        return wrapped

    def clients(self):
        manager = self.service.manager()
        if manager is None:
            return {}
        return dict(getattr(manager, "mcp_client_dict", {}) or {})

    def bind_sessions(self):
        for client in self.clients().values():
            if getattr(client, "session", None) is not None:
                self.sessions[client.session] = client

    def _connected(self, original):
        async def wrapped(client, *args, **kwargs):
            result = await original(client, *args, **kwargs)
            self.bind_sessions()
            return result

        return wrapped

    def _listed(self, original):
        async def wrapped(client, *args, **kwargs):
            previous = list(getattr(client, "tools", []))
            result = await original(client, *args, **kwargs)
            manager = self.service.manager()
            owned = (
                client in self.clients().values()
                or manager is not None
                and MCP_OWNER.get() is manager
            )
            if owned and self.service.management_active:
                try:
                    tools = list(result.tools)
                    cursor = getattr(result, "nextCursor", None)
                    seen = set()
                    while cursor:
                        if cursor in seen:
                            raise ValueError("MCP tool-list cursor did not advance")
                        seen.add(cursor)
                        page = await client.session.list_tools(cursor=cursor)
                        tools.extend(page.tools)
                        cursor = getattr(page, "nextCursor", None)
                    client.tools = tools
                    if hasattr(result, "model_copy"):
                        result = result.model_copy(
                            update={"tools": tools, "nextCursor": None}
                        )
                except Exception as exc:
                    client.tools = previous
                    self.service.failure("MCP complete tool inventory", exc)
                    return result
            # Initial connection is published by the manager after this call.
            if client in self.clients().values():
                try:
                    if self.service.management_active:
                        await self.reconcile(client)
                except Exception as exc:
                    self.service.failure("MCP native catalog reconciliation", exc)
            self.service.request_refresh()
            return result

        return wrapped

    def _notification(self, original):
        async def wrapped(session, notification):
            result = await original(session, notification)
            root = getattr(notification, "root", notification)
            method = getattr(root, "method", "")
            client = self.sessions.get(session)
            if method == "notifications/tools/list_changed" and client is not None:
                self.schedule(client)
            return result

        return wrapped

    def schedule(self, client):
        if (
            not self.patches.active
            or self.service.closed
            or not self.service.management_active
        ):
            return
        if client in self.refreshing:
            self.pending_refresh.add(client)
            return
        self.refreshing.add(client)

        async def refresh():
            try:
                while self.patches.active and self.service.management_active:
                    self.pending_refresh.discard(client)
                    await client.list_tools_and_save()
                    if client not in self.pending_refresh:
                        break
                    await asyncio.sleep(0.05)
            except Exception as exc:
                self.service.failure("MCP catalog refresh", exc)
            finally:
                self.refreshing.discard(client)
                self.pending_refresh.discard(client)

        task = self.service.spawn(refresh())
        self.pull_tasks.add(task)
        task.add_done_callback(self.pull_tasks.discard)

    async def reconcile(self, client):
        if not self.service.management_active:
            return
        from astrbot.core import sp
        from astrbot.core.agent.mcp_client import MCPTool

        manager = self.service.manager()
        name = next(
            (name for name, value in self.clients().items() if value is client), None
        )
        if manager is None or name is None:
            return
        while True:
            await self.activation_settled.wait()
            epoch = self.activation_epoch
            inactive = set(
                await sp.get_async("global", "global", "inactivated_llm_tools", [])
            )
            if not self.activation_pending and epoch == self.activation_epoch:
                break
        if not self.service.management_active or self.clients().get(name) is not client:
            return
        old = [
            tool
            for tool in manager.func_list
            if isinstance(unwrap(tool), MCPTool)
            and unwrap(tool).mcp_server_name == name
        ]
        previous = {tool.name: tool for tool in old}
        tools = []
        for spec in client.tools:
            tool = MCPTool(mcp_tool=spec, mcp_client=client, mcp_server_name=name)
            tool.active = tool.name not in inactive and getattr(
                previous.get(tool.name), "active", True
            )
            tools.append(tool)

        def fingerprint(items):
            return digest(
                [
                    [
                        tool.name,
                        tool.active,
                        unwrap(tool).mcp_tool.model_dump(mode="json"),
                        id(unwrap(tool).mcp_client),
                    ]
                    for tool in items
                ]
            )

        if fingerprint(old) == fingerprint(tools):
            return
        if self.clients().get(name) is not client:
            return
        old_ids = {id(tool) for tool in old}
        position = next(
            (i for i, tool in enumerate(manager.func_list) if id(tool) in old_ids),
            len(manager.func_list),
        )
        remaining = [tool for tool in manager.func_list if id(tool) not in old_ids]
        manager.func_list = remaining[:position] + tools + remaining[position:]

    async def _poll(self):
        while self.patches.active:
            seconds = self.service.policy.mcp_poll_seconds
            await asyncio.sleep(seconds or 60)
            if not self.service.management_active:
                continue
            self.bind_sessions()
            self.service.request_refresh()
            if seconds:
                for client in self.clients().values():
                    self.schedule(client)

    async def close(self):
        self.patches.close()
        for task in list(self.pull_tasks):
            task.cancel()
        if self.pull_tasks:
            await asyncio.gather(*list(self.pull_tasks), return_exceptions=True)
        if self.poll_task is not None:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
        self.sessions.clear()
