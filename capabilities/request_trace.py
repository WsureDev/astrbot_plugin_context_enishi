"""Observe native preparation stages without changing their results or timeouts."""

from __future__ import annotations

import asyncio
import inspect
import sys
import weakref
from contextlib import AsyncExitStack, asynccontextmanager

from ..channels.native import field
from .patches import Patches

NATIVE_PREPARATION_STAGES = {
    "build_main_agent": "native.build_agent",
    "_select_provider": "native.select_provider",
    "_get_session_conv": "native.load_conversation",
    "_decorate_llm_request": "native.decorate_request",
    "_ensure_persona_and_skills": "native.persona_skills",
    "_process_quote_message": "native.quoted_message",
    "_apply_workspace_extra_prompt": "native.workspace_prompt",
    "_apply_kb": "native.knowledge_retrieval",
}
INTERNAL_STAGE_MODULE = (
    "astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal"
)
INTERNAL_PREPARATION_STAGES = {
    "build_main_agent": "native.build_agent",
    "_prepare_file_attachments": "native.file_attachments",
    "prepare_request_images": "native.prepare_images",
}


class RequestTrace:
    def __init__(self, service):
        self.service = service
        self.patches = Patches()
        self.waiters = weakref.WeakKeyDictionary()

    def install(self):
        from astrbot.core import astr_main_agent

        internal = sys.modules.get(INTERNAL_STAGE_MODULE)
        if internal is not None:
            for name, stage in INTERNAL_PREPARATION_STAGES.items():
                self.patches.add(internal, name, self.wrap(stage))
            manager = getattr(internal, "session_lock_manager", None)
            if manager is not None:
                self.patches.add(manager, "acquire_lock", self.lock)
        for name, stage in NATIVE_PREPARATION_STAGES.items():
            self.patches.add(astr_main_agent, name, self.wrap(stage))
        self.service.diagnostics.emit(
            "diagnostics.native_hooks", methods=[row[1] for row in self.patches.rows]
        )

    def waiting(self, event):
        task = asyncio.current_task()
        if task is not None:
            self.waiters[task] = event

    def lock(self, original):
        @asynccontextmanager
        async def traced(*args, **kwargs):
            event = self.waiters.get(asyncio.current_task())
            if event is None:
                async with original(*args, **kwargs) as value:
                    yield value
                return
            async with AsyncExitStack() as stack:
                async with self.service.diagnostics.span(
                    "native.session_lock.wait", event
                ):
                    value = await stack.enter_async_context(original(*args, **kwargs))
                self.service.diagnostics.emit("native.session_lock.acquired", event)
                try:
                    yield value
                finally:
                    self.service.diagnostics.emit(
                        "native.session_lock.releasing", event
                    )

        return traced

    def wrap(self, stage):
        def factory(original):
            signature = inspect.signature(original)

            async def traced(*args, **kwargs):
                bound = signature.bind(*args, **kwargs)
                context = bound.arguments.get("plugin_context")
                if context is not None and context is not self.service.context:
                    return await original(*args, **kwargs)
                event = bound.arguments.get("event")
                async with self.service.diagnostics.span(stage, event):
                    result = await original(*args, **kwargs)
                    if stage in {"native.build_agent", "native.select_provider"}:
                        self.service.diagnostics.emit(
                            stage + ".result",
                            event,
                            returned_none=result is None,
                            selected_provider=field(
                                field(result, "provider_config", {}), "id"
                            ),
                        )
                    return result

            return traced

        return factory

    def close(self):
        self.patches.close()
        self.waiters.clear()
