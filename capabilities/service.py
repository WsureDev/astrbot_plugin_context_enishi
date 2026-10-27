from __future__ import annotations

import asyncio
import copy
import logging
import uuid
import weakref

from ..channels.native import field
from ..storage.files import run_io
from .artifacts import ArtifactStore
from .catalog import digest, skill_entries, tool_entries
from .controller import TOOL_RULES, RunController
from .integration import CapabilityIntegration
from .policy import META_NAMES, CapabilityPolicy, lazy_conflict, participates
from .registry import RegistryObserver
from .skills import SkillIntegration
from .state import Session, StateStore
from .tools import make_tools

try:
    from astrbot.api import logger
except ImportError:
    logger = logging.getLogger(__name__)


class CapabilityService:
    def __init__(self, context, config):
        self.context, self.config = context, config
        self.store = self.artifacts = None
        self.entries = {}
        self.scope_skills = {}
        self.skill_epoch = 0
        self.skill_boot = uuid.uuid4().hex
        self.revision = ""
        self.dirty = True
        self.tasks = set()
        self.run_refs = weakref.WeakValueDictionary()
        self.refresh_task = None
        self.closed = False
        self.loop = None
        self.errors = []
        self.save_lock = asyncio.Lock()
        self.catalog_lock = asyncio.Lock()
        self.catalog_generation = 0
        self.observer = RegistryObserver(self)
        self.skills = SkillIntegration(self)
        self.integration = CapabilityIntegration(self)

    @property
    def policy(self):
        return CapabilityPolicy.read(self.config)

    @property
    def management_active(self):
        return (
            not self.closed and self.policy.enabled and not lazy_conflict(self.context)
        )

    def enabled(self, event):
        return self.management_active and self.store is not None and participates(event)

    def manager(self):
        getter = getattr(self.context, "get_llm_tool_manager", None)
        return getter() if callable(getter) else None

    @staticmethod
    def plugin_map():
        try:
            from astrbot.core.star.star import star_map

            return star_map
        except ImportError:
            return {}

    async def initialize(self, directory, runner_class, executor_class):
        self.loop = asyncio.get_running_loop()
        from astrbot.core.agent.message import Message
        from astrbot.core.agent.tool import FunctionTool, ToolSet

        self.message_class, self.tool_class, self.tool_set_class = (
            Message,
            FunctionTool,
            ToolSet,
        )
        self.store = await asyncio.to_thread(StateStore, directory)
        self.artifacts = ArtifactStore(directory)
        try:
            self.observer.install()
            self.skills.install()
            self.integration.install(runner_class, executor_class)
            if self.management_active:
                for client in self.observer.clients().values():
                    await self.observer.reconcile(client)
            await self.refresh()
        except BaseException:
            await self.close()
            raise

    async def io(self, method, *args, **kwargs):
        if self.closed and (self.store is None or method != self.store.save):
            raise ValueError("能力管理已卸载")
        if kwargs:
            from functools import partial

            return await run_io(partial(method, *args, **kwargs), tasks=self.tasks)
        return await run_io(method, *args, tasks=self.tasks)

    def failure(self, operation, exc):
        message = f"{operation}: {type(exc).__name__}: {exc}"
        self.errors.append(message)
        self.errors[:] = self.errors[-20:]
        logger.warning("Enishi capabilities: %s", message)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)

        def done(task):
            self.tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                self.failure("background operation", task.exception())

        task.add_done_callback(done)
        return task

    def request_refresh(self):
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if self.loop is not None and current_loop is not self.loop:
            if not self.closed and not self.loop.is_closed():
                try:
                    self.loop.call_soon_threadsafe(self.request_refresh)
                except RuntimeError:
                    pass
            return
        self.dirty = True
        self.catalog_generation += 1
        if self.closed or self.store is None:
            return
        if self.refresh_task is None or self.refresh_task.done():

            async def refresh():
                await asyncio.sleep(0)
                while self.dirty and not self.closed:
                    await self.refresh_if_dirty()

            self.refresh_task = self.spawn(refresh())

    async def refresh_if_dirty(self):
        manager = self.manager()
        if manager is not None:
            current = digest(
                [
                    [
                        tool.name,
                        tool.description,
                        tool.parameters,
                        getattr(tool, "active", True),
                        id(tool),
                    ]
                    for tool in [
                        *manager.func_list,
                        *getattr(manager, "builtin_func_list", {}).values(),
                    ]
                ]
            )
            if current != getattr(self, "manager_fingerprint", None):
                self.dirty = True
        if self.dirty and not self.closed:
            await self.refresh()

    async def refresh(self):
        async with self.catalog_lock:
            return await self._refresh()

    async def _refresh(self):
        generation = self.catalog_generation
        manager = self.manager()
        entries = {}
        try:
            from astrbot.core.skills.skill_manager import SkillManager

            skill_manager = await asyncio.to_thread(SkillManager)
            skills = await asyncio.to_thread(
                skill_manager.list_skills,
                active_only=True,
                runtime="local",
                show_sandbox_path=False,
            )
            sandbox = await asyncio.to_thread(
                skill_manager.list_skills, active_only=True, runtime="sandbox"
            )
            entries.update(skill_entries(skills, local_metadata=True))
            entries.update(
                skill_entries(
                    [
                        skill
                        for skill in sandbox
                        if field(skill, "source_type") == "sandbox_only"
                    ]
                )
            )
        except (ImportError, OSError, ValueError) as exc:
            self.failure("Skill inventory", exc)
        manager = self.manager()
        if manager is not None:
            tools = list(field(manager.get_full_tool_set(), "tools", []))
            tools.extend(getattr(manager, "builtin_func_list", {}).values())
            entries.update(tool_entries(self.tool_set_class(tools), self.plugin_map()))
        self.entries = entries
        self.revision = digest(
            {key: [entry.revision, entry.identity] for key, entry in entries.items()}
        )
        if manager is not None:
            self.manager_fingerprint = digest(
                [
                    [
                        tool.name,
                        tool.description,
                        tool.parameters,
                        getattr(tool, "active", True),
                        id(tool),
                    ]
                    for tool in [
                        *manager.func_list,
                        *getattr(manager, "builtin_func_list", {}).values(),
                    ]
                ]
            )
        self.dirty = self.catalog_generation != generation
        self.observer.bind_sessions()
        self.discover(entries)
        await self.persist()

    def discover(self, entries):
        if self.store is not None and self.store.discover(
            entries, self.policy.new_group_mode
        ):
            self.persist_later()

    def protected(self, key):
        result = set()
        for run in list(self.run_refs.values()):
            if run.key == key and not run.closed:
                result.update(run.held)
                result.update(run.loaded_this_run)
                result.update(run.resources.expanded)
        return result

    async def persist(self):
        if self.store is None:
            return
        async with self.save_lock:
            snapshot = copy.deepcopy(self.store.snapshot())
            await self.io(self.store.save, snapshot)

    async def finish_context(self, context):
        run = getattr(context, "_enishi_capabilities", None)
        if run is not None:
            await run.resources.capture(context.messages)
            await self.persist()

    def persist_later(self):
        if self.store is not None and not self.closed:
            self.spawn(self.persist())

    async def prepare(self, event, request, *, context_limit=0):
        await self.refresh_if_dirty()
        tool_set = field(request, "func_tool") or self.tool_set_class()
        if any(tool.name in META_NAMES for tool in field(tool_set, "tools", [])):
            raise ValueError(
                "当前工具集存在同名 Enishi 管理工具，已保留原始工具集，请检查重复安装或重复变换"
            )
        skills = getattr(request, "_enishi_skill_entries", {})
        cid = field(field(request, "conversation"), "cid", "")
        key = event.unified_msg_origin + "|" + str(cid or "request:" + str(id(request)))
        session = self.store.sessions.setdefault(key, Session())
        run = RunController(self, event, request, tool_set, skills, key, session)
        run.context_limit = max(0, int(context_limit or 0))
        if run.entries:
            injected = make_tools(run, self.tool_class, self.tool_set_class)
        else:
            injected = tool_set
            run.injected = tool_set
            run.refresh()
        # Read requested resources before committing any request-field mutations.
        for entry in run.entries.values():
            if entry.kind == "skill" and self.store.mode(entry) == "primary":
                await run.load([entry.id])
        await run.prefetch()
        request.func_tool = injected
        if run.entries:
            request.system_prompt = (
                str(field(request, "system_prompt", "") or "") + "\n" + TOOL_RULES
            )
        self.run_refs[id(run)] = run
        return run

    def snapshot(self):
        groups = []
        if self.store is None:
            return {"ready": False}
        catalog = self.catalog_entries()
        for group, policy in sorted(self.store.groups.items()):
            entries = [entry for entry in catalog.values() if entry.group == group]
            groups.append(
                {
                    "id": group,
                    **policy,
                    "available": bool(entries),
                    "entries": [
                        {
                            "id": entry.id,
                            "name": entry.name,
                            "kind": entry.kind,
                            "description": entry.description,
                            "revision": entry.revision,
                            "override": self.store.tools.get(entry.id, {}).get(
                                "mode", "inherit"
                            ),
                            "mode": self.store.mode(entry),
                        }
                        for entry in entries
                    ],
                }
            )
        return {
            "ready": True,
            "enabled": self.policy.enabled,
            "conflict": lazy_conflict(self.context),
            "revision": self.revision,
            "new_group_mode": self.policy.new_group_mode,
            "idle_rounds": self.policy.idle_rounds,
            "fold_skills": self.policy.fold_skills,
            "archive_results": self.policy.archive_results,
            "groups": groups,
            "watchers_intact": self.observer.patches.intact()
            and self.skills.patches.intact()
            and self.integration.patches.intact(),
            "mcp_notifications": self.observer.notifications_supported,
            "errors": list(self.errors),
            "sessions": [
                {
                    "id": key,
                    "completed": session.completed,
                    "leases": {
                        key: lease.last for key, lease in session.leases.items()
                    },
                    "suppressed": sorted(session.suppressed),
                    "counters": session.counters,
                }
                for key, session in self.store.sessions.items()
            ],
            "runs": [
                {
                    "session": run.key,
                    "tools": len(run.injected.tools),
                    "schema_chars": run.schema_chars,
                    "estimated_schema_tokens": run.schema_tokens,
                    "completed": run.closed,
                }
                for run in list(self.run_refs.values())
                if run.injected is not None
            ],
        }

    def catalog_entries(self):
        catalog = {}
        for entries in self.scope_skills.values():
            catalog.update(entries)
        catalog.update(self.entries)
        return catalog

    async def close(self):
        self.closed = True
        self.skills.close()
        self.integration.close()
        await self.observer.close()
        # Drain producers first, then flush a final state snapshot.
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        await self.persist()
