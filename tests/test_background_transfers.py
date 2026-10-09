from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_channel_media_tools.integration import RunnerIntegration

from .test_integration import owner


@pytest.mark.parametrize("cancel", [False, True])
async def test_background_images_survive_parent_and_cleanup_at_consumer_end(
    service, cancel
):
    parent_path, _ = await service.channels.fetch_image(service.event, "native_current")
    gate, entered = asyncio.Event(), asyncio.Event()
    jobs, paths = [], []

    class Runner:
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

    class Runner:
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
