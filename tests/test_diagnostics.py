from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot_plugin_context_enishi import observability
from astrbot_plugin_context_enishi.capabilities.request_trace import (
    INTERNAL_STAGE_MODULE,
    RequestTrace,
)
from astrbot_plugin_context_enishi.observability import Diagnostics

from .conftest import Event

TEST_SLOW_SECONDS = 0.001
TEST_TIMEOUT_SECONDS = 1


async def test_nested_phase_logs_use_same_astrbot_trace_and_emit_slow_wait_stack(
    monkeypatch,
):
    native_logger = Mock()
    monkeypatch.setattr(observability, "logger", native_logger)
    warning = asyncio.Event()
    native_logger.warning.side_effect = lambda *args, **kwargs: warning.set()
    diagnostics = Diagnostics(
        {"diagnostics": {"slow_phase_seconds": TEST_SLOW_SECONDS}}
    )
    event = Event()
    async with diagnostics.span("prepare", event):
        async with diagnostics.span("catalog"):
            await asyncio.wait_for(warning.wait(), TEST_TIMEOUT_SECONDS)
    infos = [json.loads(call.args[-1]) for call in native_logger.info.call_args_list]
    assert len({info["trace"] for info in infos}) == 1
    assert all(info["umo"] == event.unified_msg_origin for info in infos)
    stages = [call.args[2] for call in native_logger.info.call_args_list]
    assert stages == ["prepare.begin", "catalog.begin", "catalog.end", "prepare.end"]
    waiting = json.loads(native_logger.warning.call_args_list[0].args[-1])
    assert waiting["await_stack"] and waiting["elapsed_seconds"] >= TEST_SLOW_SECONDS
    assert observability.TRACE_EVENT.get() is None


async def test_failure_retains_full_exception_traceback(caplog, monkeypatch):
    monkeypatch.setattr(
        observability, "logger", logging.getLogger("diagnostics-native-contract")
    )
    diagnostics = Diagnostics({"diagnostics": {"slow_phase_seconds": 0}})
    with pytest.raises(ValueError, match="probe failure"):
        async with diagnostics.span("request", Event()):
            raise ValueError("probe failure")
    record = next(
        record for record in caplog.records if "request.failed" in record.message
    )
    assert record.exc_info[0] is ValueError
    assert str(record.exc_info[1]) == "probe failure"


async def test_diagnostic_lock_is_released_after_cancellation():
    diagnostics = Diagnostics({"diagnostics": {"slow_phase_seconds": 0}})
    lock = asyncio.Lock()
    acquired = asyncio.Event()

    async def worker():
        async with diagnostics.lock(lock, "test.lock"):
            acquired.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(worker())
    await acquired.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lock.locked()
    assert observability.TRACE_EVENT.get() is None


async def test_native_alias_and_session_wait_are_observed_and_restored(monkeypatch):
    native_logger = Mock()
    monkeypatch.setattr(observability, "logger", native_logger)
    core = ModuleType("astrbot.core")
    agent = ModuleType("astrbot.core.astr_main_agent")
    internal = ModuleType(INTERNAL_STAGE_MODULE)
    context, result, event = object(), object(), Event()
    lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire_lock(session_id):
        async with lock:
            yield session_id

    async def build_main_agent(event, plugin_context):
        return result

    agent.build_main_agent = build_main_agent
    internal.build_main_agent = build_main_agent
    manager = SimpleNamespace(acquire_lock=acquire_lock)
    internal.session_lock_manager = manager
    core.astr_main_agent = agent
    monkeypatch.setitem(sys.modules, "astrbot.core", core)
    monkeypatch.setitem(sys.modules, INTERNAL_STAGE_MODULE, internal)
    diagnostics = Diagnostics(
        {"diagnostics": {"slow_phase_seconds": TEST_SLOW_SECONDS}}
    )
    trace = RequestTrace(SimpleNamespace(context=context, diagnostics=diagnostics))
    trace.install()
    waiting = asyncio.Event()
    native_logger.warning.side_effect = lambda *args, **kwargs: waiting.set()
    await lock.acquire()

    async def process():
        trace.waiting(event)
        async with manager.acquire_lock(event.unified_msg_origin):
            return await internal.build_main_agent(event, context)

    task = asyncio.create_task(process())
    try:
        await asyncio.wait_for(waiting.wait(), TEST_TIMEOUT_SECONDS)
        assert not task.done()
        lock.release()
        assert await asyncio.wait_for(task, TEST_TIMEOUT_SECONDS) is result
        stages = [call.args[2] for call in native_logger.info.call_args_list]
        assert stages.count("native.build_agent.begin") == 1
        assert "native.session_lock.acquired" in stages
        assert not lock.locked()
        before = native_logger.info.call_count
        assert await agent.build_main_agent(event, object()) is result
        assert native_logger.info.call_count == before
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        trace.close()
    assert agent.build_main_agent is build_main_agent
    assert internal.build_main_agent is build_main_agent
    assert manager.acquire_lock is acquire_lock
