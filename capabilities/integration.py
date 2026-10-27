from __future__ import annotations

import inspect
import weakref

from ..channels.native import field
from .patches import Patches


class CapabilityIntegration:
    def __init__(self, service):
        self.service = service
        self.patches = Patches()
        self.runs = weakref.WeakKeyDictionary()
        self.hook_targets = set()

    def install(self, runner_class, executor_class):
        self.patches.add(runner_class, "reset", self._reset)
        self.patches.add(runner_class, "_transition_state", self._transition)
        self.patches.add(runner_class, "_handle_function_tools", self._tool_calls)
        if executor_class is not None:
            for name in ("_execute_local", "_execute_mcp"):
                self.patches.add(executor_class, name, self._execute)

    def _reset(self, original):
        signature = inspect.signature(original)

        async def wrapped(runner, *args, **kwargs):
            bound = signature.bind(runner, *args, **kwargs)
            request = bound.arguments.get("request")
            context = bound.arguments.get("run_context")
            event = field(field(context, "context"), "event")
            self.service.diagnostics.emit("runner.reset.enter", event)
            run = None
            if request is not None and self.service.enabled(event):
                original_tools = getattr(request, "func_tool", None)
                original_prompt = getattr(request, "system_prompt", "")
                try:
                    if bound.arguments.get("tool_schema_mode", "full") != "full":
                        raise ValueError("Enishi 工具收纳需要 AstrBot 工具模式为 full")
                    provider = bound.arguments.get("provider")
                    limit = field(
                        field(provider, "provider_config", {}), "max_context_tokens", 0
                    )
                    async with self.service.diagnostics.span(
                        "capabilities.prepare", event
                    ):
                        run = await self.service.prepare(
                            event, request, context_limit=limit
                        )
                    if not self.patches.active or not self.service.enabled(event):
                        raise ValueError("能力管理已在请求装配期间关闭")
                except Exception as exc:
                    if run is not None:
                        run.finish(False)
                        run = None
                    request.func_tool = original_tools
                    prompt = original_prompt or ""
                    for folded, native in getattr(request, "_enishi_skill_blocks", []):
                        prompt = prompt.replace(folded, native, 1)
                    request.system_prompt = prompt
                    self.service.failure("request preparation rolled back", exc)
            try:
                async with self.service.diagnostics.span("runner.native_reset", event):
                    result = await original(runner, *args, **kwargs)
            except BaseException:
                if run is not None:
                    request.func_tool = run.full_tool_set
                    request.system_prompt = run.original_system_prompt
                    run.finish(False)
                raise
            if run is not None:
                self.runs[runner] = run
                run.runner = weakref.ref(runner)
                run.resources.current_user = next(
                    (
                        message
                        for message in reversed(runner.run_context.messages)
                        if field(message, "role") == "user"
                    ),
                    None,
                )
                object.__setattr__(runner.run_context, "_enishi_capabilities", run)
                self._attach_process(runner, run)
                self._attach_usage_hook(runner.agent_hooks)
            return result

        return wrapped

    def _attach_usage_hook(self, hooks):
        if id(hooks) in self.hook_targets or not callable(
            getattr(hooks, "on_tool_start", None)
        ):
            return

        def factory(original):
            async def started(run_context, tool, tool_args):
                run = getattr(run_context, "_enishi_capabilities", None)
                if run is not None and self.service.enabled(run.event):
                    call_id = run.tool_started(tool.name)
                    if call_id is not None and not getattr(
                        tool, "is_background_task", False
                    ):
                        run.started_calls.append((tool.name, call_id))
                return await original(run_context, tool, tool_args)

            return started

        try:
            self.patches.add(hooks, "on_tool_start", factory)
            self.hook_targets.add(id(hooks))
        except (TypeError, AttributeError) as exc:
            self.service.failure("tool-start hook adapter", exc)

    def _attach_process(self, runner, run):
        manager = runner.request_context_manager
        patches = Patches()
        run.process_patch = patches

        def factory(original):
            async def process(messages, *args, **kwargs):
                if self.patches.active and self.service.enabled(run.event):
                    try:
                        async with self.service.diagnostics.span(
                            "capabilities.project", run.event, messages=len(messages)
                        ):
                            await run.before_step(messages, self.service.message_class)
                    except (OSError, ValueError, TypeError) as exc:
                        run.resources.remove_ephemeral(messages)
                        self.service.failure(
                            "context projection left original content intact", exc
                        )
                else:
                    run.restore_native(messages)
                config = getattr(manager, "config", None)
                limit = getattr(config, "max_context_tokens", 0)
                if (
                    limit > 0
                    and self.patches.active
                    and self.service.enabled(run.event)
                ):
                    config.max_context_tokens = max(1, limit - run.schema_tokens)
                    kwargs = {**kwargs, "trusted_token_usage": 0}
                try:
                    return await original(messages, *args, **kwargs)
                finally:
                    if limit > 0:
                        config.max_context_tokens = limit

            return process

        patches.add(manager, "process", factory)

    def _transition(self, original):
        def wrapped(runner, *args, **kwargs):
            result = original(runner, *args, **kwargs)
            run = self.runs.get(runner)
            if run is not None and runner.done():
                self.service.diagnostics.emit(
                    "runner.done",
                    run.event,
                    state=str(
                        field(field(runner, "_state"), "name", field(runner, "_state"))
                    ),
                )
                state = field(runner, "_state")
                name = str(field(state, "name", state))
                aborted = getattr(runner, "was_aborted", lambda: False)()
                run.finish(name != "ERROR" and not aborted)
            return result

        return wrapped

    def _tool_calls(self, original):
        async def wrapped(runner, *args, **kwargs):
            run = self.runs.get(runner)
            response = args[1] if len(args) > 1 else kwargs.get("llm_response")
            if (
                run is not None
                and response is not None
                and self.service.enabled(run.event)
            ):
                await self.service.refresh_if_dirty()
                run.refresh()
                run.calls_started(response)
            iterator = original(runner, *args, **kwargs)
            try:
                async for value in iterator:
                    yield value
            finally:
                await iterator.aclose()

        return wrapped

    def _execute(self, original):
        async def wrapped(cls, tool, run_context, **kwargs):
            run = getattr(run_context, "_enishi_capabilities", None)
            call_id = None
            if run is not None and self.service.enabled(run.event):
                for index, (name, started_id) in enumerate(run.started_calls):
                    if name == tool.name:
                        call_id = started_id
                        run.started_calls.pop(index)
                        break
                else:
                    call_id = run.tool_started(tool.name)
            iterator = original(cls, tool, run_context, **kwargs)
            try:
                async for value in iterator:
                    if run is not None and call_id is not None:
                        try:
                            await run.resources.capture_result(call_id, value)
                        except (ValueError, OSError, TypeError) as exc:
                            self.service.failure("complete tool result archive", exc)
                    yield value
            finally:
                await iterator.aclose()

        return wrapped

    def close(self):
        self.patches.close()
        for runner, run in list(self.runs.items()):
            run.restore_native(runner.run_context.messages)
            run.process_patch.close()
            run.finish(False)
        self.runs.clear()
        self.hook_targets.clear()
