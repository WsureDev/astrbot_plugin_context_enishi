"""Execute real AstrBot message and runner methods without importing its application."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import os
import time
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_context_enishi.context.runtime import RunnerIntegration

from .conftest import request
from .test_integration import owner


@pytest.fixture
def runtime():
    source = os.environ.get("ASTRBOT_SOURCE")
    if not source:
        pytest.skip(
            "set ASTRBOT_SOURCE to the AstrBot checkout for runtime-method tests"
        )
    root = Path(source)
    spec = importlib.util.spec_from_file_location(
        "channel_media_runtime_message", root / "astrbot/core/agent/message.py"
    )
    messages = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(messages)
    tree = ast.parse(
        (root / "astrbot/core/agent/runners/tool_loop_agent_runner.py").read_text()
    )
    runner_class = next(
        n
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "ToolLoopAgentRunner"
    )
    selected = {
        "reset",
        "_assemble_request_context_for_provider",
        "step",
        "_iter_llm_responses",
        "_sanitize_contexts_for_provider",
        "_complete_with_assistant_response",
    }
    methods = [
        n
        for n in runner_class.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in selected
    ]
    for method in methods:
        method.decorator_list = []

    class State(Enum):
        IDLE = 0
        RUNNING = 1
        DONE = 2
        ERROR = 3

    class Chain:
        def __init__(self, **kw):
            self.__dict__.update(kw)

        def message(self, text):
            self.text = text
            return self

    class Manager:
        def __init__(self, config):
            self.config = config
            self.seen = []

        async def process(self, messages, **kwargs):
            self.seen.append([message.model_dump() for message in messages])
            return messages

    class Stats:
        def __init__(self):
            self.time_to_first_token = 0

        def to_dict(self):
            return {}

    namespace = dict(
        AgentState=State,
        asyncio=asyncio,
        time=time,
        logger=logging.getLogger("test"),
        AgentResponse=Chain,
        AgentResponseData=Chain,
        MessageChain=Chain,
        Json=Chain,
        Message=messages.Message,
        TextPart=messages.TextPart,
        ThinkPart=messages.ThinkPart,
        ContextConfig=SimpleNamespace,
        ContextManager=Manager,
        AgentStats=Stats,
        EstimateTokenCounter=lambda: None,
        bind_checkpoint_messages=messages.bind_checkpoint_messages,
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            *methods,
        ],
        type_ignores=[],
    )
    exec(
        compile(
            ast.fix_missing_locations(module),
            "<read-only AstrBot runtime methods>",
            "exec",
        ),
        namespace,
    )
    compiled = type("RuntimeMethods", (), {name: namespace[name] for name in selected})
    return SimpleNamespace(messages=messages, compiled=compiled, State=State)


@pytest.mark.parametrize(
    "mode,expected", [("reference_only", 0), ("current_turn", 2), ("disabled", 3)]
)
async def test_actual_astrbot_step_provider_payload_and_history(
    service, runtime, mode, expected
):
    service.config["channel_settings"][0]["image_mode"] = mode
    service.policy.text_factory = runtime.messages.TextPart
    response = SimpleNamespace(
        is_chunk=False,
        usage=None,
        role="assistant",
        tools_call_name=[],
        reasoning_content=None,
        reasoning_signature=None,
        result_chain=None,
        completion_text="answer",
    )
    payloads, compactions = [], []

    class Runner(runtime.compiled):
        async def reset(self):
            pass

        def __init__(self):
            self.req = request(["current", "reply"])
            self.req.conversation = None
            self.req.model = "test"
            self.req.session_id = "session"
            self.req.func_tool = None
            self.provider = SimpleNamespace(
                provider_config={"modalities": []},
                text_chat=AsyncMock(side_effect=self.provider_call),
            )
            self.run_context = SimpleNamespace(
                context=SimpleNamespace(event=service.event),
                messages=[
                    runtime.messages.Message.model_validate(
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": "old"}}
                            ],
                        }
                    ),
                    runtime.messages.Message.model_validate(
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": "current"}},
                                {"type": "image_url", "image_url": {"url": "reply"}},
                            ],
                        }
                    ),
                ],
            )
            self._state = runtime.State.IDLE
            self._abort_signal = None
            self.request_max_retries = 1
            self.streaming = False
            self.stats = SimpleNamespace(
                start_time=time.time(), time_to_first_token=0, to_dict=lambda: {}
            )
            self.agent_hooks = SimpleNamespace(
                on_agent_begin=AsyncMock(), on_agent_done=AsyncMock()
            )
            self.request_context_manager = SimpleNamespace(process=self.process)

        async def provider_call(self, **kw):
            payloads.append(kw["contexts"])
            return response

        async def process(self, messages, **kw):
            compactions.append([m.model_dump() for m in messages])
            return messages

        async def _await_or_stop(self, awaitable):
            return await awaitable

        def _func_tool_for_provider(self):
            return None

        async def _iter_llm_responses_with_fallback(self):
            async for response in self._iter_llm_responses():
                yield response

        def _transition_state(self, state):
            self._state = state

        def _simple_print_message_role(self, *args):
            pass

        def _is_stop_requested(self):
            return False

        def _resolve_unconsumed_follow_ups(self):
            pass

        def done(self):
            return self._state in {runtime.State.DONE, runtime.State.ERROR}

    integration = RunnerIntegration(owner(service))
    integration.install(Runner)
    run = Runner()
    await run.reset()
    async for _ in run.step():
        pass

    def count(data):
        return sum(
            p["type"] == "image_url" for m in data for p in (m.get("content") or [])
        )

    assert (
        count([m.model_dump() if hasattr(m, "model_dump") else m for m in payloads[0]])
        == expected
    )
    assert count(compactions[0]) == expected
    saved = runtime.messages.dump_messages_with_checkpoints(run.run_context.messages)
    assert count(saved) == (3 if mode == "disabled" else 0)
    integration.terminate()


@pytest.mark.parametrize(
    "mode,expected", [("reference_only", 0), ("current_turn", 2), ("disabled", 3)]
)
async def test_actual_reset_attaches_guard_to_new_context_manager(
    service, runtime, mode, expected
):
    service.config["channel_settings"][0]["image_mode"] = mode
    service.policy.text_factory = runtime.messages.TextPart
    req = request(["current", "reply"])
    req.contexts = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "old"}}],
        }
    ]
    req.prompt, req.audio_urls = "question", []
    req.conversation, req.model, req.session_id, req.func_tool = (
        None,
        None,
        "session",
        None,
    )
    req.assemble_context = AsyncMock(
        return_value={
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": value}}
                for value in req.image_urls
            ],
        }
    )
    response = SimpleNamespace(
        is_chunk=False,
        usage=None,
        role="assistant",
        tools_call_name=[],
        reasoning_content=None,
        reasoning_signature=None,
        result_chain=None,
        completion_text="answer",
    )
    payloads = []

    async def provider_call(**kwargs):
        payloads.append(kwargs["contexts"])
        return response

    class Runner(runtime.compiled):
        async def _await_or_stop(self, awaitable):
            return await awaitable

        def _func_tool_for_provider(self):
            return None

        async def _iter_llm_responses_with_fallback(self):
            async for item in self._iter_llm_responses():
                yield item

        def _transition_state(self, state):
            self._state = state

        def _simple_print_message_role(self, *args):
            pass

        def _is_stop_requested(self):
            return False

        def _resolve_unconsumed_follow_ups(self):
            pass

        def done(self):
            return self._state in {runtime.State.DONE, runtime.State.ERROR}

    integration = RunnerIntegration(owner(service))
    original_step = Runner.step
    integration.install(Runner)
    run = Runner()
    await run.reset(
        provider=SimpleNamespace(
            provider_config={"modalities": []},
            text_chat=AsyncMock(side_effect=provider_call),
        ),
        request=req,
        run_context=SimpleNamespace(context=SimpleNamespace(event=service.event)),
        tool_executor=SimpleNamespace(),
        agent_hooks=SimpleNamespace(
            on_agent_begin=AsyncMock(), on_agent_done=AsyncMock()
        ),
    )
    try:
        [item async for item in run.step()]
        assert Runner.step is original_step

        def count(records):
            normalized = [
                record.model_dump() if hasattr(record, "model_dump") else record
                for record in records
            ]
            return sum(
                part["type"] == "image_url"
                for message in normalized
                for part in (message.get("content") or [])
                if isinstance(part, dict)
            )

        assert count(run.request_context_manager.seen[0]) == expected
        assert count(payloads[0]) == expected
        assert count(
            runtime.messages.dump_messages_with_checkpoints(run.run_context.messages)
        ) == (3 if mode == "disabled" else 0)
    finally:
        integration.terminate()
