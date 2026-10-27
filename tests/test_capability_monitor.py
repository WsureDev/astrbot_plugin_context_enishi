from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot_plugin_context_enishi.capabilities.integration import CapabilityIntegration
from astrbot_plugin_context_enishi.context.runtime import RunnerIntegration

from .capability_support import Tool, capability_request
from .capability_support import capabilities as capabilities
from .test_integration import owner
from .test_runtime import runtime as runtime


@pytest.mark.parametrize(
    "monitor_version,source_variable",
    [("current", "MONITOR_CURRENT_SOURCE"), ("old", "MONITOR_COMPAT_SOURCE")],
)
@pytest.mark.parametrize("media_first", [True, False])
async def test_monitor_observes_expansion_without_wrapper_replacement(
    capabilities,
    runtime,
    monkeypatch,
    service,
    monitor_version,
    source_variable,
    media_first,
):
    source = os.environ.get(source_variable)
    if not source:
        pytest.skip("set " + source_variable + " for Monitor compatibility")
    package = ModuleType("enishi_monitor_contract_" + monitor_version)
    package.__path__ = [str(Path(source))]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    probe_class = importlib.import_module(package.__name__ + ".probe").RunnerProbe

    class Provider:
        provider_config = {"id": "test", "max_context_tokens": 128000}

        async def text_chat(self, **kwargs):
            payloads.append(kwargs)
            return SimpleNamespace(
                role="assistant", is_chunk=False, completion_text="ok", usage=None
            )

    class Runner(runtime.compiled):
        def _transition_state(self, state):
            self._state = state

        def done(self):
            return self._state in (runtime.State.DONE, runtime.State.ERROR)

        def was_aborted(self):
            return False

        def _sanitize_contexts_for_provider(self, messages):
            return [message.model_dump() for message in messages]

        def _func_tool_for_provider(self):
            return self.req.func_tool

        async def _await_or_stop(self, awaitable):
            return await awaitable

        async def _handle_function_tools(self, req, llm_response):
            yield None

    class Executor:
        @classmethod
        async def execute(cls, tool, run_context, **tool_args):
            async for result in cls._execute_local(tool, run_context, **tool_args):
                yield result

        @classmethod
        async def _execute_local(cls, tool, run_context, **kwargs):
            yield await tool.handler(run_context.context.event, **kwargs)

    snapshots = []
    payloads = []
    failures = Mock()
    monitor = probe_class(
        SimpleNamespace(
            store=SimpleNamespace(redact_secrets=True, max_text_chars=20000),
            start_llm=lambda *args: {
                "id": "call",
                "clock": time.perf_counter(),
                "provider_model": "test",
            },
            capture_llm_input=lambda span, payload: snapshots.append(payload),
            finish_span=Mock(),
            record_failure=failures,
        )
    )
    assert monitor.install(Runner, Executor)
    observed = (Runner.step, Runner._iter_llm_responses, Executor.__dict__["execute"])
    integration = CapabilityIntegration(capabilities)
    media = RunnerIntegration(owner(service))
    if media_first:
        media.install(Runner, Executor)
        integration.install(Runner, Executor)
    else:
        integration.install(Runner, Executor)
        media.install(Runner, Executor)
    capabilities.message_class = runtime.messages.Message
    request = capability_request([Tool("uncommon")])
    request.model, request.session_id = None, "session"
    request.contexts, request.image_urls, request.audio_urls = [], [], []
    request.extra_user_content_parts = []

    async def assemble():
        return {"role": "user", "content": [{"type": "text", "text": "request"}]}

    request.assemble_context = assemble
    runner = Runner()
    context = SimpleNamespace(context=SimpleNamespace(event=service.event), messages=[])
    try:
        await runner.reset(
            provider=Provider(),
            request=request,
            run_context=context,
            tool_executor=Executor(),
            agent_hooks=SimpleNamespace(),
        )
        await context._enishi_capabilities.load(["uncommon"])
        [response async for response in runner._iter_llm_responses()]
        assert (
            Runner.step,
            Runner._iter_llm_responses,
            Executor.__dict__["execute"],
        ) == observed
        if monitor_version == "current":
            assert len(snapshots) == 1 and '"name": "uncommon"' in snapshots[0]
        assert payloads[0]["func_tool"].get_tool("uncommon") is not None
        assert monitor.snapshot()["enabled"]
        failures.assert_not_called()
        integration.close()
        assert monitor.snapshot()["enabled"]
    finally:
        media.terminate()
        integration.close()
        monitor.restore()
