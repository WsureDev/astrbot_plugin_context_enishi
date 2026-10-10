from __future__ import annotations

import gc
import inspect
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_context_manager.integration import RunnerIntegration

from .conftest import image_count, picture, request


def owner(service):
    return SimpleNamespace(
        context=service.context,
        policy=service.policy,
        channels=service.channels,
        settings=service.settings,
        enabled_for_runner=lambda r: (
            service.settings.for_event(r.run_context.context.event).mode != "disabled"
        ),
        enabled_event=lambda e: service.settings.for_event(e).mode != "disabled",
    )


class RunnerBoundary:
    async def reset(self):
        self.request_context_manager = SimpleNamespace(process=self.process)

    async def process(self, messages, **kwargs):
        return messages

    def _transition_state(self, state):
        self.finished = state in {"DONE", "ERROR"}

    def done(self):
        return getattr(self, "finished", False)

    def _sanitize_contexts_for_provider(self, messages):
        return messages


async def test_context_guard_covers_multiple_tools_and_final_history(service):
    class Runner(RunnerBoundary):
        def __init__(self):
            self.req = request()
            self.run_context = SimpleNamespace(
                context=SimpleNamespace(event=service.event),
                messages=[{"role": "user", "content": [picture("historical")]}],
            )
            self.calls = []
            self.finished = False

        def _sanitize_contexts_for_provider(self, messages):
            return messages

        def done(self):
            return self.finished

        async def step(self):
            self.run_context.messages = await self.request_context_manager.process(
                self.run_context.messages
            )
            self.calls.append(
                self._sanitize_contexts_for_provider(self.run_context.messages)
            )
            yield "response"
            if len(self.calls) == 1:
                self.run_context.messages.append(
                    {"role": "user", "content": [picture("external-tool-image")]}
                )
            else:
                self._transition_state("DONE")

    integration = RunnerIntegration(owner(service))
    original = Runner.step
    integration.install(Runner)
    run = Runner()
    await run.reset()
    for _ in range(2):
        async for _ in run.step():
            pass
    assert all(image_count(call) == 0 for call in run.calls)
    assert image_count(run.run_context.messages) == 0
    integration.terminate()
    assert Runner.step is original


async def test_implicit_generation_cache_is_rejected_before_execution(service):
    calls = []

    class Runner(RunnerBoundary):
        async def step(self):
            yield None

        def _sanitize_contexts_for_provider(self, messages):
            return messages

    class Executor:
        @classmethod
        async def execute(cls, tool, run_context, **tool_args):
            async for item in cls._execute_local(tool, run_context, **tool_args):
                yield item

        @classmethod
        async def _execute_local(cls, tool, run_context, **tool_args):
            calls.append(tool_args)
            yield "executed"

    original = inspect.getattr_static(Executor, "execute")
    integration = RunnerIntegration(owner(service))
    integration.install(Runner, Executor)
    context = SimpleNamespace(context=SimpleNamespace(event=service.event))
    tool = SimpleNamespace(name="generate_image")
    results = [
        r async for r in Executor.execute(tool, context, use_context_images=True)
    ]
    assert results[0].isError and calls == []
    results = [
        r
        async for r in Executor.execute(
            tool,
            context,
            reference_images=["native-current-path"],
            use_context_images=False,
        )
    ]
    assert results == ["executed"] and not calls[-1]["use_context_images"]
    integration.terminate()
    assert inspect.getattr_static(Executor, "execute") is original


async def test_generation_native_ids_are_materialized_before_consumer(service):
    calls = []

    class Runner(RunnerBoundary):
        async def step(self):
            yield None

        def _sanitize_contexts_for_provider(self, messages):
            return messages

    class Executor:
        @classmethod
        async def execute(cls, tool, run_context, **tool_args):
            async for item in cls._execute_local(tool, run_context, **tool_args):
                yield item

        @classmethod
        async def _execute_local(cls, tool, run_context, **tool_args):
            calls.append(tool_args)
            yield "executed"

    integration = RunnerIntegration(owner(service))
    integration.install(Runner, Executor)
    source = (await service.channels.current_images(service.event))[1]
    context = SimpleNamespace(context=SimpleNamespace(event=service.event))
    async for _ in Executor.execute(
        SimpleNamespace(name="generate_image"),
        context,
        reference_images=[source.ref.uri],
    ):
        pass
    assert calls[0]["reference_images"][0].startswith(str(service.tmp))
    assert not calls[0]["use_context_images"]
    integration.terminate()


async def test_telegram_id_preservation_is_event_metadata_not_index(service):
    class Adapter:
        async def convert_message(self, update, context, get_reply=True):
            return SimpleNamespace(message=[SimpleNamespace(type="Image")])

    integration = RunnerIntegration(owner(service))
    original = Adapter.convert_message
    integration.preserve_telegram_ids(Adapter)
    update = SimpleNamespace(
        message=SimpleNamespace(
            photo=[SimpleNamespace(file_id="native_album")], message_id=12
        )
    )
    converted = await Adapter().convert_message(update, None)
    assert converted.message[0]._channel_native_file_id == "native_album"
    assert converted.message[0]._channel_native_message_id == "12"
    assert service.cache.db.execute("SELECT count(*) FROM captions").fetchone()[0] == 0
    integration.terminate()
    assert Adapter.convert_message is original


async def test_unload_does_not_overwrite_another_plugins_later_wrapper(service):
    class Runner(RunnerBoundary):
        async def step(self):
            yield None

        def _sanitize_contexts_for_provider(self, messages):
            return messages

    integration = RunnerIntegration(owner(service))
    integration.install(Runner)
    ours = Runner.reset

    async def later(runner):
        await ours(runner)

    Runner.reset = later
    integration.terminate()
    assert Runner.reset is later and not integration.active


async def test_step_identity_and_generator_close_are_untouched(service):
    closed = []

    class Runner(RunnerBoundary):
        req = request()
        run_context = SimpleNamespace(
            context=SimpleNamespace(event=service.event), messages=[]
        )

        def _sanitize_contexts_for_provider(self, messages):
            return messages

        def done(self):
            return False

        async def step(self):
            try:
                yield "partial"
            finally:
                closed.append(True)

    integration = RunnerIntegration(owner(service))
    original = Runner.step
    integration.install(Runner)
    assert Runner.step is original
    iterator = Runner().step()
    assert await anext(iterator) == "partial"
    await iterator.aclose()
    assert closed == [True]
    integration.terminate()


async def test_reference_only_defers_core_caption_without_changing_shared_config(
    service,
):
    from dataclasses import dataclass

    @dataclass
    class Config:
        provider_settings: dict

    module = SimpleNamespace(_decorate_llm_request=AsyncMock())
    config = Config(
        {"default_image_caption_provider_id": "core-caption", "other": True}
    )
    integration = RunnerIntegration(owner(service))
    integration.defer_builtin_caption(module)
    await module._decorate_llm_request(service.event, None, service.context, config)
    received = integration.patches[0][2].call_args.args[3]
    assert received.provider_settings["default_image_caption_provider_id"] == ""
    assert received.provider_settings["other"]
    assert (
        config.provider_settings["default_image_caption_provider_id"] == "core-caption"
    )
    integration.terminate()


async def test_current_turn_keeps_core_caption_configuration(service):
    from dataclasses import dataclass

    @dataclass
    class Config:
        provider_settings: dict

    service.config["channel_settings"][0]["image_mode"] = "current_turn"
    original = AsyncMock()
    module = SimpleNamespace(_decorate_llm_request=original)
    config = Config({"default_image_caption_provider_id": "core-caption"})
    integration = RunnerIntegration(owner(service))
    integration.defer_builtin_caption(module)
    await module._decorate_llm_request(service.event, None, service.context, config)
    assert original.call_args.args[3] is config
    integration.terminate()


async def test_finished_runner_is_not_retained_by_instance_patches(service):
    class Manager:
        async def process(self, messages, **kwargs):
            return messages

    class Runner(RunnerBoundary):
        def __init__(self):
            self.req = request()
            self.run_context = SimpleNamespace(
                context=SimpleNamespace(event=service.event), messages=[]
            )

        async def reset(self):
            self.request_context_manager = Manager()

    integration = RunnerIntegration(owner(service))
    integration.install(Runner)
    run = Runner()
    await run.reset()
    runner_ref, manager_ref = weakref.ref(run), weakref.ref(run.request_context_manager)
    del run
    gc.collect()
    assert runner_ref() is None and manager_ref() is None
    assert not integration.instances
    integration.terminate()


async def test_channel_excluded_from_policy_keeps_its_images(service):
    class Runner(RunnerBoundary):
        def __init__(self):
            self.req = request()
            self.run_context = SimpleNamespace(
                context=SimpleNamespace(event=service.event),
                messages=[{"role": "user", "content": [picture("preserve")]}],
            )

    disabled_owner = owner(service)
    disabled_owner.enabled_for_runner = lambda runner: False
    disabled_owner.enabled_event = lambda event: False
    integration = RunnerIntegration(disabled_owner)
    integration.install(Runner)
    run = Runner()
    await run.reset()
    await run.request_context_manager.process(run.run_context.messages)
    run._transition_state("DONE")
    assert image_count(run.run_context.messages) == 1
    integration.terminate()


async def test_tool_wrapper_preserves_executor_subclass_dispatch(service):
    class Executor:
        value = "base"

        @classmethod
        async def _execute_local(cls, tool, run_context, **args):
            yield cls.value

    class Specialized(Executor):
        value = "specialized"

    integration = RunnerIntegration(owner(service))
    integration.install(RunnerBoundary, Executor)
    try:
        context = SimpleNamespace(context=SimpleNamespace(event=service.event))
        results = [
            item
            async for item in Specialized._execute_local(
                SimpleNamespace(name="test"), context
            )
        ]
        assert results == ["specialized"]
    finally:
        integration.terminate()
