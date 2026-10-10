from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_context_manager.integration import (
    BACKGROUND_IMAGES,
    RunnerIntegration,
)
from astrbot_plugin_context_manager.platforms import TRANSFER_CONTEXT

from .test_integration import RunnerBoundary, owner


@pytest.mark.parametrize("cancel", [False, True])
async def test_background_images_survive_parent_and_cleanup_at_consumer_end(
    service, cancel
):
    parent_path, _ = await service.channels.fetch_image(service.event, "native_current")
    gate, entered = asyncio.Event(), asyncio.Event()
    jobs, paths = [], []

    class Runner(RunnerBoundary):
        async def step(self):
            yield None

        def _sanitize_contexts_for_provider(self, messages):
            return messages

    class Executor:
        @classmethod
        async def _do_handoff_background(cls, tool, run_context, **args):
            paths.extend(Path(p) for p in args["image_urls"])
            entered.set()
            await gate.wait()
            assert all(path.read_bytes() for path in paths)

        @classmethod
        async def execute(cls, tool, run_context, **args):
            async for result in cls._execute_handoff_background(
                tool, run_context, **args
            ):
                yield result

        @classmethod
        async def _execute_handoff_background(cls, tool, run_context, **args):
            jobs.append(
                asyncio.create_task(
                    cls._do_handoff_background(tool, run_context, **args)
                )
            )
            yield {"content": "submitted"}

    integration = RunnerIntegration(owner(service))
    integration.install(Runner, Executor)
    context = SimpleNamespace(context=SimpleNamespace(event=service.event))
    tool = SimpleNamespace(name="transfer_to_subagent", agent=object())
    try:
        [
            r
            async for r in Executor.execute(
                tool, context, background_task=True, image_urls=[str(parent_path)]
            )
        ]
        await entered.wait()
        service.channels.release_transfers(service.event)
        service.event.cleanup_temporary_local_files()
        assert not parent_path.exists()
        assert paths and all(path.exists() and path != parent_path for path in paths)
        if cancel:
            jobs[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await jobs[0]
        else:
            gate.set()
            await jobs[0]
        assert all(not path.exists() for path in paths)
    finally:
        gate.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        integration.terminate()


async def test_unloading_media_plugin_keeps_started_consumers_files(service):
    original, _ = await service.channels.fetch_image(service.event, "native_current")
    lease = service.channels.lease_background_images(service.event, [str(original)])
    copies = [Path(p) for p in lease.image_urls]
    service.channels.release_transfers()
    assert all(path.exists() for path in copies)
    lease.close()
    assert all(not path.exists() for path in copies)


async def test_unrelated_paths_are_not_copied_or_deleted(service):
    unrelated = service.tmp / "unrelated.png"
    unrelated.write_bytes(b"owned by another tool")
    lease = service.channels.lease_background_images(
        service.event, [str(unrelated), "https://example.com/image.png"]
    )
    assert not lease.paths
    assert lease.image_urls == [str(unrelated), "https://example.com/image.png"]
    lease.close()
    assert unrelated.exists()


async def test_background_tool_without_image_parameter_keeps_signature(service):
    received = []

    class Runner(RunnerBoundary):
        async def step(self):
            yield None

        def _sanitize_contexts_for_provider(self, messages):
            return messages

    class Executor:
        @classmethod
        async def execute(cls, tool, run_context, **args):
            received.append(args)
            yield {"content": "submitted"}

    integration = RunnerIntegration(owner(service))
    integration.install(Runner, Executor)
    try:
        context = SimpleNamespace(context=SimpleNamespace(event=service.event))
        [
            r
            async for r in Executor.execute(
                SimpleNamespace(name="background_tool", is_background_task=True),
                context,
                input="test",
            )
        ]
        assert received == [{"input": "test"}]
    finally:
        integration.terminate()


async def test_parent_cleanup_does_not_delete_child_owned_transfers(service):
    parent = SimpleNamespace(context=SimpleNamespace(event=service.event))
    child = SimpleNamespace(context=SimpleNamespace(event=service.event))
    files = []
    for context in (parent, child):
        service.channels.register_runner(service.event, context)
        token = TRANSFER_CONTEXT.set(context)
        try:
            path, _ = await service.channels.fetch_image(
                service.event, "native_current"
            )
            files.append(path)
        finally:
            TRANSFER_CONTEXT.reset(token)
    assert files[0] in service.event.temporary_files
    assert files[1] not in service.event.temporary_files
    service.channels.release_transfers(service.event, run_context=parent)
    service.event.cleanup_temporary_local_files()
    assert not files[0].exists() and files[1].read_bytes()
    service.channels.release_transfers(service.event, run_context=child)
    assert not files[1].exists()


async def test_nested_consumer_owns_independent_copy(service):
    path, _ = await service.channels.fetch_image(service.event, "native_current")
    first = service.channels.lease_background_images(service.event, [str(path)])
    second = service.channels.lease_background_images(service.event, first.image_urls)
    assert first.image_urls != second.image_urls
    service.channels.release_transfers(service.event)
    first.close()
    assert all(Path(path).read_bytes() for path in second.image_urls)
    second.close()
    assert all(not Path(path).exists() for path in second.image_urls)


async def test_native_tool_hook_reserves_files_before_background_worker_starts(service):
    original, _ = await service.channels.fetch_image(service.event, "native_current")
    paths, jobs = [], []
    gate, entered = asyncio.Event(), asyncio.Event()
    tool = SimpleNamespace(name="background_tool", is_background_task=True)
    context = SimpleNamespace(context=SimpleNamespace(event=service.event))

    class Runner(RunnerBoundary):
        run_context = context

        async def _handle_function_tools(self, *args):
            params = {"image_urls": [str(original)]}
            integration.prepare_background_call(service.event, tool, params)
            async for result in Executor.execute(tool, context, **params):
                yield result

    class Executor:
        @classmethod
        async def execute(cls, tool, run_context, **args):
            jobs.append(
                asyncio.create_task(cls._execute_background(tool, run_context, **args))
            )
            yield {"content": "submitted"}

        @classmethod
        async def _execute_background(cls, tool, run_context, **args):
            paths.extend(Path(value) for value in args["image_urls"])
            entered.set()
            await gate.wait()
            assert all(path.read_bytes() for path in paths)

    integration = RunnerIntegration(owner(service))
    integration.install(Runner, Executor)
    try:
        [result async for result in Runner()._handle_function_tools()]
        assert BACKGROUND_IMAGES.get() is None and TRANSFER_CONTEXT.get() is None
        service.channels.release_transfers(service.event)
        service.event.cleanup_temporary_local_files()
        assert not original.exists()
        await asyncio.wait_for(entered.wait(), 1)
        assert all(path.exists() for path in paths)
        gate.set()
        await asyncio.gather(*jobs)
        assert all(not path.exists() for path in paths)
    finally:
        gate.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        integration.terminate()


async def test_cancelled_background_consumer_cleans_its_new_files(service):
    context = SimpleNamespace(context=SimpleNamespace(event=service.event))
    service.channels.register_runner(service.event, context)
    paths = []
    entered, gate = asyncio.Event(), asyncio.Event()

    class Runner(RunnerBoundary):
        pass

    class Executor:
        @classmethod
        async def _execute_background(cls, tool, run_context, **args):
            path, _ = await service.channels.fetch_image(
                service.event, "native_current"
            )
            paths.append(path)
            entered.set()
            await gate.wait()

    integration = RunnerIntegration(owner(service))
    integration.install(Runner, Executor)
    job = asyncio.create_task(
        Executor._execute_background(SimpleNamespace(name="test"), context)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        service.channels.release_transfers(service.event, run_context=context)
        service.event.cleanup_temporary_local_files()
        assert paths[0].read_bytes()
        integration.terminate()
        service.channels.release_transfers(preserve_consumers=True)
        assert paths[0].read_bytes()
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert not paths[0].exists()
    finally:
        gate.set()
        await asyncio.gather(job, return_exceptions=True)
        integration.terminate()
