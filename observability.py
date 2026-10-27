"""AstrBot logger spans for request preparation and background work."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

from .channels.native import field
from .identity import PLUGIN

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(PLUGIN)

DIAGNOSTIC_DEFAULTS = {
    "enabled": True,
    "log_group_messages": True,
    "slow_phase_seconds": 10,
    "include_task_stacks": True,
}
TRACE_EXTRA_KEY = "_enishi_diagnostic_trace"
TRACE_EVENT = ContextVar("enishi_diagnostic_event", default=None)


def task_stack(task):
    frames, seen = [], set()
    current = task.get_coro() if task is not None else None
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        frame = (
            getattr(current, "cr_frame", None)
            or getattr(current, "gi_frame", None)
            or getattr(current, "ag_frame", None)
        )
        if frame is not None:
            frames.append(
                f"{frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}"
            )
        current = (
            getattr(current, "cr_await", None)
            or getattr(current, "gi_yieldfrom", None)
            or getattr(current, "ag_await", None)
        )
    return frames


class Diagnostics:
    def __init__(self, config):
        self.config = config

    @property
    def options(self):
        return {**DIAGNOSTIC_DEFAULTS, **(self.config.get("diagnostics", {}) or {})}

    def emit(self, stage, event=None, *, level="info", **details):
        if not self.options["enabled"]:
            return
        event = event or TRACE_EVENT.get()
        if event is not None:
            getter = getattr(event, "get_extra", None)
            trace = getter(TRACE_EXTRA_KEY, None) if callable(getter) else None
            if not trace:
                trace = uuid.uuid4().hex
                setter = getattr(event, "set_extra", None)
                if callable(setter):
                    setter(TRACE_EXTRA_KEY, trace)
            details = {
                "trace": trace,
                "umo": field(event, "unified_msg_origin", ""),
                "message_id": str(field(field(event, "message_obj"), "message_id", "")),
                **details,
            }
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        if task is not None:
            details["task"] = task.get_name()
        getattr(logger, level)(
            "[%s] stage=%s %s",
            PLUGIN,
            stage,
            json.dumps(details, ensure_ascii=False, default=str),
        )

    def received(self, event):
        if not self.options["log_group_messages"]:
            return
        obj = field(event, "message_obj")
        mentions = []
        for part in field(obj, "message", []) or []:
            kind = field(part, "type", "")
            if str(field(kind, "value", kind)).lower() == "at":
                mentions.append(str(field(part, "qq", "")))
        stopped = getattr(event, "is_stopped", None)
        self.emit(
            "message.received",
            event,
            wake=field(event, "is_wake", False),
            role=field(event, "role", ""),
            self_id=field(obj, "self_id", ""),
            mentions=mentions,
            stopped=stopped() if callable(stopped) else None,
            plugins=field(event, "plugins_name"),
        )

    @asynccontextmanager
    async def lock(self, lock, stage, event=None):
        acquired = False
        try:
            async with self.span(stage + ".wait", event):
                await lock.acquire()
                acquired = True
            self.emit(stage + ".acquired", event)
            yield
        finally:
            if acquired:
                lock.release()
                self.emit(stage + ".released", event)

    @asynccontextmanager
    async def span(self, stage, event=None, **details):
        if not self.options["enabled"]:
            yield
            return
        started = time.monotonic()
        event = event or TRACE_EVENT.get()
        token = TRACE_EVENT.set(event)
        owner_task = asyncio.current_task()
        self.emit(stage + ".begin", event, **details)
        slow = float(self.options["slow_phase_seconds"])
        watcher = None
        if slow > 0:

            async def warn():
                await asyncio.sleep(slow)
                extra = {}
                if self.options["include_task_stacks"]:
                    extra["await_stack"] = task_stack(owner_task)
                self.emit(
                    stage + ".waiting",
                    event,
                    level="warning",
                    elapsed_seconds=time.monotonic() - started,
                    **details,
                    **extra,
                )

            watcher = asyncio.create_task(warn(), name="enishi-diagnostic:" + stage)
        try:
            yield
        except asyncio.CancelledError:
            self.emit(
                stage + ".cancelled",
                event,
                level="warning",
                elapsed_seconds=time.monotonic() - started,
                **details,
            )
            raise
        except Exception:
            self.emit(
                stage + ".failed",
                event,
                level="exception",
                elapsed_seconds=time.monotonic() - started,
                **details,
            )
            raise
        else:
            self.emit(
                stage + ".end",
                event,
                elapsed_seconds=time.monotonic() - started,
                **details,
            )
        finally:
            try:
                if watcher is not None:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
            finally:
                TRACE_EVENT.reset(token)
