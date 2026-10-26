"""Run untouched monitor 0.4.1 and Comfy 2.4.2 source with deterministic I/O."""

from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import inspect
import logging
import os
import re
import sys
import time
import traceback
import uuid
from contextlib import aclosing
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import mcp.types
import pytest

from astrbot_plugin_context_enishi.context.runtime import RunnerIntegration

from .conftest import request
from .test_integration import RunnerBoundary, owner


def source_root(variable):
    value = os.environ.get(variable)
    if not value:
        pytest.skip(f"set {variable} to an untouched compatibility-source checkout")
    return Path(value)


@pytest.fixture
def old_monitor(monkeypatch):
    root = source_root("MONITOR_COMPAT_SOURCE")
    assert 'VERSION = "0.4.1"' in (root / "main.py").read_text()
    package = ModuleType("media_test_monitor_041")
    package.__path__ = [str(root)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return importlib.import_module(f"{package.__name__}.probe").RunnerProbe
    finally:
        sys.dont_write_bytecode = previous


@pytest.mark.parametrize("media_first", [False, True])
async def test_old_monitor_keeps_identity_health_and_records_across_reload(
    service, old_monitor, media_first
):
    class Runner(RunnerBoundary):
        def __init__(self):
            self.req = request()
            self.run_context = SimpleNamespace(
                context=SimpleNamespace(event=service.event), messages=[]
            )

        async def _iter_llm_responses(self, *, include_model=True):
            yield SimpleNamespace(
                is_chunk=False, role="assistant", completion_text="ok"
            )

        async def _iter_llm_responses_with_fallback(self):
            async for result in self._iter_llm_responses():
                yield result

        async def step(self):
            await self.request_context_manager.process(self.run_context.messages)
            async for result in self._iter_llm_responses():
                yield result
            async for result in Executor.execute(
                SimpleNamespace(name="test"), self.run_context
            ):
                yield result
            self._transition_state("DONE")

    class Executor:
        @classmethod
        async def execute(cls, tool, run_context, **tool_args):
            async for result in cls._execute_local(tool, run_context, **tool_args):
                yield result

        @classmethod
        async def _execute_local(cls, tool, run_context, **args):
            yield {"content": "ok"}

    monitor_owner = SimpleNamespace(
        store=SimpleNamespace(max_text_chars=20000),
        start_llm=Mock(side_effect=lambda *a: {"clock": time.perf_counter()}),
        start_tool=Mock(side_effect=lambda *a: {"clock": time.perf_counter()}),
        finish_span=Mock(),
        end_task=Mock(),
        record_failure=Mock(),
    )
    probe = old_monitor(monitor_owner)
    integration = RunnerIntegration(owner(service))

    def build_retry(provider_label, *, retry_rate_limits, max_attempts=None):
        return None

    class HTTP:
        async def send(self, request, *args, **kwargs):
            return SimpleNamespace(status_code=200)

    retry_modules = (
        SimpleNamespace(AsyncRetrying=lambda *args, **kwargs: None),
        SimpleNamespace(_build_retrying=build_retry),
        HTTP,
    )
    try:
        if media_first:
            integration.install(Runner, Executor)
            assert probe.install(Runner, Executor)
        else:
            assert probe.install(Runner, Executor)
            integration.install(Runner, Executor)
        assert probe.retry.install(Runner, *retry_modules)
        monitored = (
            Runner.step,
            Runner._iter_llm_responses,
            inspect.getattr_static(Executor, "execute"),
        )
        for cycle in range(3):
            if cycle:
                integration.terminate()
                assert probe.snapshot()["enabled"]
                integration.install(Runner, Executor)
            assert monitored == (
                Runner.step,
                Runner._iter_llm_responses,
                inspect.getattr_static(Executor, "execute"),
            )
            assert probe.snapshot()["enabled"]
            assert probe.retry.snapshot()["ok"]
            run = Runner()
            await run.reset()
            [result async for result in run.step()]
        assert monitor_owner.start_llm.call_count == 3
        assert monitor_owner.start_tool.call_count == 3
        monitor_owner.record_failure.assert_not_called()
        integration.terminate()
        assert probe.snapshot()["enabled"]
        assert probe.retry.snapshot()["ok"]
        probe.restore()
        assert probe.install(Runner, Executor)
        assert probe.retry.install(Runner, *retry_modules)
        assert probe.snapshot()["enabled"] and probe.retry.snapshot()["ok"]
    finally:
        integration.terminate()
        probe.restore()


class Chain:
    def __init__(self, chain=None):
        self.chain = chain or []

    def message(self, text):
        self.chain.append(("text", text))
        return self


@pytest.fixture
def core_local_tool():
    root = source_root("ASTRBOT_SOURCE")
    tree = ast.parse((root / "astrbot/core/astr_agent_tool_exec.py").read_text())
    executor = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "FunctionToolExecutor"
    )
    local = next(
        node
        for node in executor.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_execute_local"
    )
    local.decorator_list = []
    call = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "call_local_llm_tool"
    )

    class Tool:
        def __init__(self, name, handler):
            self.name, self.handler = name, handler

        async def call(self, context, **args):
            return None

    namespace = dict(
        asyncio=asyncio,
        inspect=inspect,
        traceback=traceback,
        mcp=mcp,
        logger=logging.getLogger("core-tool-test"),
        FunctionTool=Tool,
        MessageChain=Chain,
        MessageEventResult=type("MessageEventResult", (), {}),
        CommandResult=type("CommandResult", (), {}),
        ShellSessionTool=type("ShellSessionTool", (), {}),
        aclosing=aclosing,
    )
    helper = root / "astrbot/core/utils/async_generator.py"
    if helper.is_file():
        spec = importlib.util.spec_from_file_location(
            "enishi_test_native_iterator", helper
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        namespace["iterate_in_task"] = module.iterate_in_task
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            call,
            local,
        ],
        type_ignores=[],
    )
    exec(
        compile(
            ast.fix_missing_locations(module),
            str(root / "astrbot/core/astr_agent_tool_exec.py"),
            "exec",
        ),
        namespace,
    )
    return SimpleNamespace(execute=classmethod(namespace["_execute_local"]), Tool=Tool)


@pytest.fixture
def old_comfy():
    root = source_root("COMFY_COMPAT_SOURCE")
    source = (root / "main.py").read_text()
    assert '"2.4.2"' in source
    tree = ast.parse(source)
    plugin = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ComfyUIPlugin"
    )
    selected = {"comfyui_txt2img", "_run_batch_wait_stage", "_claim_batch_wait_stage"}
    constant_names = {
        "_BATCH_WAIT_STATE_EXTRA",
        "_BATCH_WAIT_TIMEOUT_SECONDS",
        "_MAX_REPEATED_IMAGES_PER_REQUEST",
    }
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in plugin.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in constant_names
    }
    methods = [
        node
        for node in plugin.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name in selected
    ]
    for method in methods:
        method.decorator_list = []
    namespace = dict(
        asyncio=asyncio,
        logger=logging.getLogger("old-comfy-test"),
        re=re,
        uuid=uuid,
        Path=Path,
        MessageChain=Chain,
        Image=SimpleNamespace(fromFileSystem=lambda path: ("image", path)),
        Plain=lambda text: ("text", text),
        Node=lambda **args: SimpleNamespace(**args),
        Nodes=lambda nodes: SimpleNamespace(type="forward", nodes=nodes),
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
        compile(ast.fix_missing_locations(module), str(root / "main.py"), "exec"),
        namespace,
    )
    return type(
        "UntouchedComfy242",
        (),
        {**{name: namespace[name] for name in selected}, **constants},
    )


@pytest.mark.parametrize("direct_send", [False, True])
@pytest.mark.parametrize("background", [False, True])
async def test_unchanged_old_comfy_background_batch_is_delivered_once(
    service, old_comfy, core_local_tool, direct_send, background
):
    plugin = old_comfy()
    plugin.api = SimpleNamespace(resolve_workflow_filename=lambda value: "default.json")
    plugin._check_access = lambda event: (True, "")
    plugin._extract_lora_control_tags = lambda prompt: (prompt, [])
    plugin._check_sensitive = lambda *args: (True, [])
    plugin._check_cooldown = lambda event: (True, 0)
    plugin._clear_draw_failures = lambda event: None
    plugin._remember_draw_failure = lambda *args, **kwargs: None
    plugin._get_forward_user_id = lambda event: "bot"
    gate, entered = asyncio.Event(), asyncio.Event()

    async def submit(*args, **kwargs):
        return [
            {"index": i, "status": "submitted", "prompt_id": str(i)} for i in (1, 2)
        ]

    async def collect(item, **kwargs):
        entered.set()
        await gate.wait()
        return {"index": item["index"], "status": "success", "path": Path("stub.png")}

    plugin._submit_repeated_batch = submit
    plugin._collect_submitted_batch_result = collect
    service.context.get_registered_star = lambda name: SimpleNamespace(star_cls=plugin)
    sent = []

    async def send(message):
        sent.append(message)

    service.event.send = send

    class Runner(RunnerBoundary):
        pass

    class Executor:
        @classmethod
        async def execute(cls, tool, run_context, **args):
            async for result in cls._execute_local(tool, run_context, **args):
                yield result

        _execute_local = core_local_tool.execute

        @classmethod
        async def _execute_background(cls, tool, run_context, **args):
            return [result async for result in cls.execute(tool, run_context, **args)]

    integration = RunnerIntegration(owner(service))
    before = {
        name: inspect.getattr_static(old_comfy, name)
        for name in (
            "comfyui_txt2img",
            "_claim_batch_wait_stage",
            "_run_batch_wait_stage",
        )
    }
    integration.install(Runner, Executor)
    tool = core_local_tool.Tool("comfyui_txt2img", plugin.comfyui_txt2img)
    context = SimpleNamespace(
        context=SimpleNamespace(event=service.event), tool_call_timeout=1
    )
    foreground_task = None
    try:
        args = dict(prompt="test", count=2, direct_send=direct_send)
        if background:
            result = await Executor._execute_background(tool, context, **args)
            assert "Images are still pending" in result[0].content[-1].text
        else:
            result = [item async for item in Executor.execute(tool, context, **args)]
            assert not integration.delivery.tasks
            assert "Images are still pending" not in result[0].content[-1].text
            foreground_task = asyncio.create_task(
                plugin._claim_batch_wait_stage(service.event)
            )
        assert "Batch submitted" in result[0].content[0].text
        await asyncio.wait_for(entered.wait(), 1)
        assert not await plugin._claim_batch_wait_stage(service.event)
        assert not sent
        # Unloading the later plugin does not cancel already accepted images.
        integration.terminate()
        gate.set()
        await asyncio.gather(
            *list(integration.delivery.tasks),
            *([foreground_task] if foreground_task else []),
        )
        assert len(sent) == (3 if direct_send else 1)
        if direct_send:
            assert [chain.chain[0][0] for chain in sent] == ["image", "image", "text"]
        else:
            nodes = sent[0].chain[0].nodes
            assert [node.content[0][0] for node in nodes] == ["image", "image", "text"]
        assert service.event.get_extra(plugin._BATCH_WAIT_STATE_EXTRA) is None
        assert all(
            inspect.getattr_static(old_comfy, name) is method
            for name, method in before.items()
        )
    finally:
        gate.set()
        await asyncio.gather(
            *list(integration.delivery.tasks),
            *([foreground_task] if foreground_task else []),
            return_exceptions=True,
        )
        integration.terminate()
