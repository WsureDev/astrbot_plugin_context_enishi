from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mcp.types import CallToolResult, TextContent

from astrbot_plugin_context_enishi.context.compatibility import LegacyBatchDelivery


async def test_bridge_delivers_overlapping_batches_without_clearing_new_state(service):
    key = "batch"
    started, gate = asyncio.Event(), asyncio.Event()
    delivered = []

    async def claim(event):
        state = event.get_extra(key)
        if state["wait_started"]:
            return False
        state["wait_started"] = True
        started.set()
        await gate.wait()
        delivered.append(state["id"])
        event.set_extra(key, None)
        state["finished"] = True
        return True

    plugin = SimpleNamespace(_BATCH_WAIT_STATE_EXTRA=key, _claim_batch_wait_stage=claim)
    service.context.get_registered_star = lambda name: SimpleNamespace(star_cls=plugin)
    bridge = LegacyBatchDelivery(SimpleNamespace(context=service.context))
    tool = SimpleNamespace(name="comfyui_txt2img")
    first = dict(id=1, wait_started=False, finished=False)
    second = dict(id=2, wait_started=False, finished=False)
    service.event.set_extra(key, first)
    assert bridge.schedule(service.event, tool)
    await asyncio.wait_for(started.wait(), 1)
    service.event.set_extra(key, second)
    assert bridge.schedule(service.event, tool)
    assert bridge.schedule(service.event, tool)
    assert len(bridge.tasks) == 2
    gate.set()
    await asyncio.gather(*list(bridge.tasks))
    assert sorted(delivered) == [1, 2]
    assert first["finished"] and second["finished"]
    assert service.event.get_extra(key) is None


def test_pending_notice_copies_the_tool_result():
    original = CallToolResult(
        content=[TextContent(type="text", text="Batch submitted")]
    )
    observed = LegacyBatchDelivery.pending_notice(original)
    assert observed is not original
    assert len(original.content) == 1
    assert len(observed.content) == 2
    assert "Images are still pending" in observed.content[-1].text


async def test_bridge_skips_jobs_already_owned_by_existing_plugin(service):
    async def claim(event):
        raise AssertionError("already started batch must not be reclaimed")

    plugin = SimpleNamespace(
        _BATCH_WAIT_STATE_EXTRA="batch", _claim_batch_wait_stage=claim
    )
    service.context.get_registered_star = lambda name: SimpleNamespace(star_cls=plugin)
    service.event.set_extra("batch", dict(wait_started=True, finished=False))
    bridge = LegacyBatchDelivery(SimpleNamespace(context=service.context))
    assert not bridge.schedule(service.event, SimpleNamespace(name="comfyui_txt2img"))
    assert not bridge.tasks
