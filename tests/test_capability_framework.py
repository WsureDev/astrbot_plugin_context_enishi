from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shlex
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from astrbot_plugin_context_enishi.capabilities.integration import CapabilityIntegration
from astrbot_plugin_context_enishi.capabilities.patches import Patches
from astrbot_plugin_context_enishi.capabilities.registry import RegistryObserver
from astrbot_plugin_context_enishi.capabilities.skills import SkillIntegration

from .capability_support import Tool, capability_request, run_for
from .capability_support import capabilities as capabilities
from .conftest import Event
from .test_runtime import runtime as runtime


def native_skill_renderer():
    source = os.environ.get("ASTRBOT_SOURCE")
    if not source:
        pytest.skip("set ASTRBOT_SOURCE for actual Skill rendering contracts")
    path = Path(source) / "astrbot/core/skills/skill_manager.py"
    tree = ast.parse(path.read_text())
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and (
            node.name == "build_skills_prompt"
            or node.name.startswith("_sanitize_")
            or node.name
            in {
                "_build_skill_read_command_example",
                "_default_sandbox_skill_path",
                "_is_windows_prompt_path",
            }
        )
    ]
    namespace = {
        "re": re,
        "shlex": shlex,
        "os": os,
        "SANDBOX_WORKSPACE_ROOT": "/workspace",
        "SANDBOX_SKILLS_ROOT": "skills",
        "_SKILL_NAME_RE": re.compile(r"^[\w.-]+$"),
    }
    constants = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id.endswith("_RE")
            for target in node.targets
        )
    ]
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            *constants,
            *functions,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace["build_skills_prompt"]


async def test_skill_folding_only_changes_native_section_and_keeps_complete_rules(
    capabilities,
):
    renderer = native_skill_renderer()
    module = ModuleType("native_skill_contract")
    module.build_skills_prompt = renderer
    skill = SimpleNamespace(
        name="documents",
        description="Original full description " * 1000,
        path="/workspace/skills/documents/SKILL.md",
        source_type="local",
    )

    async def native_prepare(req, cfg, context, event):
        req.system_prompt = (
            "PERSONA BEFORE\n"
            + module.build_skills_prompt([skill])
            + "\n[Sandbox path rule] AFTER\n[Tools policy] KEEP"
        )

    module._ensure_persona_and_skills = native_prepare
    integration = SkillIntegration(capabilities)
    integration.install(module)
    try:
        req = capability_request()
        await module._ensure_persona_and_skills(req, {}, capabilities.context, Event())
        assert req.system_prompt.startswith("PERSONA BEFORE\n")
        assert req.system_prompt.endswith(
            "[Sandbox path rule] AFTER\n[Tools policy] KEEP"
        )
        assert "Mandatory grounding" in req.system_prompt
        assert "Failure handling" in req.system_prompt
        assert "enishi_find" in req.system_prompt
        assert skill.description not in req.system_prompt
        assert (
            next(iter(req._enishi_skill_entries.values())).description
            == skill.description
        )
    finally:
        integration.close()
    assert module.build_skills_prompt is renderer


async def test_plugin_and_direct_context_mutations_invalidate_catalog(capabilities):
    changed = asyncio.Event()
    calls = []
    capabilities.request_refresh = lambda: changed.set()

    class Manager:
        def add_func(self, name):
            calls.append(name)

    class Context:
        def add_llm_tools(self, tool):
            calls.append(tool)

    observer = RegistryObserver(capabilities)
    observer.install(Manager, Context)
    try:
        Manager().add_func("plugin-tool")
        assert changed.is_set()
        changed.clear()
        Context().add_llm_tools("direct-tool")
        assert changed.is_set() and calls == ["plugin-tool", "direct-tool"]
    finally:
        await observer.close()


async def test_mcp_notification_does_not_block_receive_loop_and_preserves_callback(
    capabilities,
):
    listed = asyncio.Event()
    release = asyncio.Event()

    class Client:
        session = None

        async def list_tools_and_save(self):
            listed.set()
            await release.wait()

    class SDK:
        async def _received_notification(self, notification):
            self.original_called = True

    observer = RegistryObserver(capabilities)
    observer.install(type("Manager", (), {}), sdk_class=SDK)
    client, session = Client(), SDK()
    observer.sessions[session] = client
    notification = SimpleNamespace(
        root=SimpleNamespace(method="notifications/tools/list_changed")
    )
    try:
        await asyncio.wait_for(
            session._received_notification(notification), timeout=0.2
        )
        assert session.original_called
        await asyncio.wait_for(listed.wait(), timeout=0.2)
        observer.schedule(client)
        assert len(observer.refreshing) == 1
    finally:
        release.set()
        await observer.close()


async def test_actual_runner_reset_provider_payload_and_managed_content_persistence(
    capabilities, runtime
):
    class Runner(runtime.compiled):
        def _transition_state(self, state):
            self._state = state

        def done(self):
            return self._state in (runtime.State.DONE, runtime.State.ERROR)

        def was_aborted(self):
            return False

    class Executor:
        @classmethod
        async def _execute_local(cls, tool, run_context, **kwargs):
            yield await tool.handler(run_context.context.event, **kwargs)

    provider = SimpleNamespace(
        provider_config={"id": "p", "max_context_tokens": 128000}
    )
    event = Event()
    req = capability_request([Tool("original")])
    req.contexts, req.image_urls, req.audio_urls = [], [], []
    req.extra_user_content_parts = []

    async def assemble():
        return {"role": "user", "content": [{"type": "text", "text": "question"}]}

    req.assemble_context = assemble
    capabilities.message_class = runtime.messages.Message
    integration = CapabilityIntegration(capabilities)
    integration.install(Runner, Executor)
    runner = Runner()
    context = SimpleNamespace(context=SimpleNamespace(event=event), messages=[])
    try:
        await runner.reset(
            provider=provider,
            request=req,
            run_context=context,
            tool_executor=Executor(),
            agent_hooks=SimpleNamespace(),
        )
        run = context._enishi_capabilities
        assert len(req.func_tool.tools) == 3
        load = req.func_tool.get_tool("enishi_load")
        result = [
            value
            async for value in Executor._execute_local(load, context, ids=["original"])
        ]
        assert "error" not in json.loads(result[0])
        assert req.func_tool.get_tool("original") is not None
        ref = await capabilities.io(
            capabilities.artifacts.put,
            run.key,
            "plugin:example/original",
            "FULL CONTENT",
        )
        await run.load([ref])
        await runner.request_context_manager.process(context.messages)
        assert any(
            "FULL CONTENT" in str(message.content) for message in context.messages
        )
        saved = runtime.messages.dump_messages_with_checkpoints(context.messages)
        assert "FULL CONTENT" not in json.dumps(saved)
        runner._transition_state(runtime.State.DONE)
        assert run.session.completed == 1
        runner._transition_state(runtime.State.DONE)
        assert run.session.completed == 1
    finally:
        integration.close()


async def test_patch_restoration_keeps_outer_wrappers_and_original_classmethod_binding():
    class Target:
        @classmethod
        async def execute(cls, value):
            yield value

    original = Target.__dict__["execute"]
    patches = Patches()

    def factory(original):
        async def wrapped(cls, value):
            async for item in original(cls, value):
                yield item + 1

        return wrapped

    patches.add(Target, "execute", factory)
    assert [value async for value in Target.execute(1)] == [2]
    patches.close()
    assert Target.__dict__["execute"] is original
    assert [value async for value in Target.execute(1)] == [1]


async def test_background_tool_start_renews_lease_without_consuming_user_round(
    capabilities,
):
    tool = Tool("background")
    tool.is_background_task = True
    run = await run_for(capabilities, [tool])
    await run.load(["background"])
    run.calls_started(
        SimpleNamespace(tools_call_name=["background"], tools_call_ids=["bg"])
    )

    class Hooks:
        async def on_tool_start(self, run_context, tool, tool_args):
            self.called = True

    hooks = Hooks()
    integration = CapabilityIntegration(capabilities)
    integration._attach_usage_hook(hooks)
    context = SimpleNamespace(_enishi_capabilities=run)
    try:
        await hooks.on_tool_start(run_context=context, tool=tool, tool_args={})
        assert hooks.called and "plugin:example/background" in run.used
        assert not run.started_calls
        assert run.session.completed == 0
    finally:
        integration.close()


async def test_sync_registration_from_worker_thread_preserves_native_result(
    capabilities,
):
    capabilities.loop = asyncio.get_running_loop()
    refreshed = asyncio.Event()

    async def refresh():
        capabilities.dirty = False
        refreshed.set()

    capabilities.refresh_if_dirty = refresh

    class Manager:
        def add_func(self, name):
            return "registered " + name

    observer = capabilities.observer
    observer.install(Manager)
    result = await asyncio.to_thread(Manager().add_func, "thread-tool")
    assert result == "registered thread-tool"
    await asyncio.wait_for(refreshed.wait(), timeout=1)


async def test_request_failure_restores_full_tools_and_owned_skill_section(
    capabilities,
):
    class Runner:
        async def reset(self, *, provider, request, run_context):
            self.req = request
            return "native success"

    integration = CapabilityIntegration(capabilities)
    integration.install(Runner, None)
    request = capability_request([Tool("one")])
    request.system_prompt = "PERSONA\nFOLDED SKILL\nOTHER POLICY"
    request._enishi_skill_blocks = [("FOLDED SKILL", "NATIVE SKILL")]
    original = request.func_tool

    async def fail(event, request, **kwargs):
        request.func_tool = None
        request.system_prompt = "partial mutation"
        raise RuntimeError("simulated failure")

    capabilities.prepare = fail
    try:
        result = await Runner().reset(
            provider=SimpleNamespace(provider_config={}),
            request=request,
            run_context=SimpleNamespace(context=SimpleNamespace(event=Event())),
        )
        assert result == "native success"
        assert request.func_tool is original
        assert request.system_prompt == "PERSONA\nNATIVE SKILL\nOTHER POLICY"
    finally:
        integration.close()
