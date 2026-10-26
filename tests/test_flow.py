from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from astrbot_plugin_context_enishi.channels.meta import DELTA_TAG, known_message_ids

from .conftest import text_factory
from .flow_support import (
    Run,
    bound_run,
    flow_request,
    flow_service,
    group_event,
    saved_history,
)
from .test_runtime import runtime as runtime


@pytest.fixture
def service(tmp_path):
    service = flow_service(tmp_path)
    yield service
    service.flow.terminate()


async def test_native_delta_keeps_same_text_authors_and_excludes_trigger(service):
    flow = service.flow
    for mid, uid in (("101", "1001"), ("102", "1002")):
        await flow.record(
            group_event(mid, "same long message " * 10, uid=uid, name="same name")
        )
    event, req = group_event("103"), flow_request()
    await flow.prepare(event, req, text_factory)
    await flow.prepare(event, req, text_factory)
    assert len(req.contexts) == 1
    delta = req.contexts[0]["content"]
    assert delta.count("<group_messages_delta>") == 1
    assert "1001" in delta and "1002" in delta
    assert known_message_ids(req.contexts) == {"101", "102"}
    assert known_message_ids(saved_history(req)) == {"101", "102", "103"}
    assert "schema_version" not in delta and "scope_key" not in delta
    run = bound_run(flow, event, req)
    assert flow.store.window(event.unified_msg_origin, "conversation", 4)["cursor"] == 0
    await service.manager.update_conversation(
        event.unified_msg_origin, "conversation", history=saved_history(req)
    )
    assert flow.store.window(event.unified_msg_origin, "conversation", 4)["cursor"] == 3
    assert run.finished


async def test_response_wrong_save_failure_and_retry_do_not_advance_early(service):
    event, req = group_event("1"), flow_request()
    await service.flow.prepare(event, req, text_factory)
    run = bound_run(service.flow, event, req)
    await service.manager.update_conversation(
        event.unified_msg_origin, "conversation", history=[]
    )
    await service.manager.update_conversation(
        event.unified_msg_origin, "conversation", history=saved_history(req)[:-1]
    )
    assert (
        service.flow.store.window(event.unified_msg_origin, "conversation", 2)["cursor"]
        == 0
    )
    service.manager.fail = True
    with pytest.raises(OSError):
        await service.manager.update_conversation(
            event.unified_msg_origin, "conversation", history=saved_history(req)
        )
    assert (
        service.flow.store.window(event.unified_msg_origin, "conversation", 2)["cursor"]
        == 0
    )
    service.manager.fail = False
    await service.manager.update_conversation(
        event.unified_msg_origin, "conversation", history=saved_history(req)
    )
    assert (
        service.flow.store.window(event.unified_msg_origin, "conversation", 2)["cursor"]
        == 1
    )
    assert run.finished


@pytest.mark.parametrize(
    "aborted,role,answer",
    [
        (True, "assistant", "interrupted"),
        (False, "err", "failure"),
        (False, "assistant", ""),
    ],
)
async def test_failed_and_aborted_runs_do_not_commit(service, aborted, role, answer):
    event, req = group_event("1"), flow_request()
    await service.flow.prepare(event, req, text_factory)
    bound_run(service.flow, event, req, aborted=aborted, role=role, answer=answer)
    await service.manager.update_conversation(
        event.unified_msg_origin, "conversation", history=saved_history(req)
    )
    assert (
        service.flow.store.window(event.unified_msg_origin, "conversation", 2)["cursor"]
        == 0
    )


@pytest.mark.parametrize("command", ["reset", "new"])
async def test_reset_new_generation_rejects_old_run_completion(service, command):
    await service.flow.record(group_event("1"))
    event, req = group_event("2"), flow_request()
    await service.flow.prepare(event, req, text_factory)
    command_event = group_event("3", "/" + command)
    command_event.set_extra("_clean_ltm_session", True)
    if command == "new":
        service.manager.current[event.unified_msg_origin] = "new-conversation"
    await service.flow.boundary(command_event)
    bound_run(service.flow, event, req)
    await service.manager.update_conversation(
        event.unified_msg_origin, "conversation", history=saved_history(req)
    )
    snapshot = service.flow.store.dump(event.unified_msg_origin)
    assert snapshot["flow"]["floor"] == 0 and snapshot["flow"]["generation"] == 1
    cid = await service.manager.get_curr_conversation_id(event.unified_msg_origin)
    next_req = flow_request(cid=cid)
    await service.flow.prepare(group_event("4"), next_req, text_factory)
    assert next_req.contexts == []


async def test_two_groups_concurrently_save_without_losing_cursor_updates(service):
    prepared = []
    for chat in ("123", "456"):
        event, req = group_event("1", chat=chat), flow_request()
        await service.flow.prepare(event, req, text_factory)
        prepared.append((event, req, bound_run(service.flow, event, req)))
    await asyncio.gather(
        *(
            service.manager.update_conversation(
                event.unified_msg_origin, "conversation", history=saved_history(req)
            )
            for event, req, run in prepared
        )
    )
    for event, req, run in prepared:
        assert (
            service.flow.store.window(event.unified_msg_origin, "conversation", 2)[
                "cursor"
            ]
            == 1
        )


async def test_budget_reports_skipped_messages_and_native_ids_are_not_text_deduped(
    service,
):
    service.config["group_flow"] = dict(max_delta_messages=2, max_delta_chars=512)
    for index in range(5):
        await service.flow.record(group_event(str(index), "x" * 200))
    req = flow_request()
    await service.flow.prepare(group_event("trigger"), req, text_factory)
    delta = req.contexts[0]["content"]
    assert len(delta) <= 512
    assert "skipped_count=" in delta and "coverage=" in delta
    assert "trigger" not in known_message_ids(req.contexts)


@pytest.mark.parametrize("external,builtin", [(True, False), (False, True)])
async def test_external_or_builtin_injector_blocks_own_reception_and_delta(
    service, external, builtin
):
    if external:
        service.context.external = SimpleNamespace(
            activated=True, star_cls=SimpleNamespace(_cfg=lambda key, default: True)
        )
    service.context.builtin = builtin
    event, req = group_event("1"), flow_request()
    assert await service.flow.record(event) is None
    await service.flow.prepare(event, req, text_factory)
    assert req.contexts == [] and not req.extra_user_content_parts
    assert event.unified_msg_origin in service.flow.blocked


async def test_disabled_external_plugin_does_not_block_native_flow(service):
    service.context.external = SimpleNamespace(activated=False, star_cls=object())
    assert await service.flow.record(group_event("1")) == 1


async def test_llm_self_echo_skipped_but_other_bot_and_identical_user_messages_recorded(
    service,
):
    service.config["group_flow"] = dict(record_self_messages=True)
    event = group_event("1")
    service.flow.replies.response(
        event, SimpleNamespace(role="assistant", completion_text="known reply")
    )
    assert (
        await service.flow.record(group_event("echo", "known reply", uid="9000"))
        is None
    )
    assert (
        await service.flow.record(group_event("bot", "other bot message", uid="9000"))
        == 1
    )
    assert (
        await service.flow.record(group_event("user", "known reply", uid="1002")) == 2
    )


async def test_saved_native_ids_prevent_replay_after_save_cursor_crash_gap(service):
    await service.flow.record(group_event("1", "old text"))
    event, req = group_event("2"), flow_request()
    await service.flow.prepare(event, req, text_factory)
    persisted = saved_history(req)
    # Simulate a crash after AstrBot saved history but before the ledger callback.
    next_req = flow_request(persisted)
    await service.flow.prepare(group_event("3"), next_req, text_factory)
    assert (
        sum(
            message.get("content", "").startswith("<" + DELTA_TAG + ">")
            for message in next_req.contexts
            if isinstance(message.get("content"), str)
        )
        == 1
    )
    assert known_message_ids(next_req.contexts) == {"1", "2"}


def test_history_wrapper_unloads_without_overwriting_a_later_wrapper(service):
    ours = service.manager.update_conversation

    async def later(*args, **kwargs):
        return await ours(*args, **kwargs)

    service.manager.update_conversation = later
    service.flow.terminate()
    assert service.manager.update_conversation is later


async def test_image_mode_and_awareness_do_not_gate_flow(service):
    service.config["channel_settings"][0]["awareness"] = dict(enabled=False)
    assert await service.flow.record(group_event("1")) == 1
    service.config["channel_settings"][0]["context"]["group_flow_enabled"] = False
    assert await service.flow.record(group_event("2")) is None


async def test_real_astrbot_history_serialization_acknowledges_current_message(
    service, runtime
):
    await service.flow.record(group_event("1", "history"))
    event, req = group_event("2", "current"), flow_request()
    await service.flow.prepare(event, req, runtime.messages.TextPart)
    run = bound_run(service.flow, event, req)
    native = runtime.messages
    messages = [native.Message.model_validate(message) for message in req.contexts]
    messages.append(
        native.Message(
            role="user",
            content=[native.TextPart(text="current"), *req.extra_user_content_parts],
        )
    )
    messages.append(native.Message(role="assistant", content="answer"))
    persisted = native.dump_messages_with_checkpoints(messages)
    await service.manager.update_conversation(
        event.unified_msg_origin, "conversation", history=persisted
    )
    assert (
        service.flow.store.window(event.unified_msg_origin, "conversation", 3)["cursor"]
        == 2
    )
    assert run.finished


async def test_disabling_reception_allows_existing_successful_tasks_to_drain(service):
    event, req = group_event("1"), flow_request()
    await service.flow.prepare(event, req, text_factory)
    run = bound_run(service.flow, event, req)
    service.config["channel_settings"][0]["context"]["group_flow_enabled"] = False
    await service.manager.update_conversation(
        event.unified_msg_origin, "conversation", history=saved_history(req)
    )
    assert (
        service.flow.store.window(event.unified_msg_origin, "conversation", 2)["cursor"]
        == 1
    )
    assert not service.flow.pending and run.finished


async def test_failed_command_does_not_create_a_boundary_and_repeated_success_is_idempotent(
    service,
):
    event = group_event("1", "/reset")
    await service.flow.boundary(event)
    assert service.flow.store.dump(event.unified_msg_origin)["flow"]["generation"] == 0
    event.set_extra("_clean_ltm_session", True)
    await service.flow.boundary(event)
    await service.flow.boundary(event)
    assert service.flow.store.dump(event.unified_msg_origin)["flow"]["generation"] == 1


async def test_topics_keep_independent_ledgers_even_with_same_native_message_id(
    service,
):
    first, second = group_event("1", chat="-100#2"), group_event("1", chat="-100#3")
    assert await service.flow.record(first) == 1
    assert await service.flow.record(second) == 1
    assert (
        service.flow.store.dump(first.unified_msg_origin)["records"][0]["record"][
            "message_id"
        ]
        == "1"
    )
    assert (
        service.flow.store.dump(second.unified_msg_origin)["records"][0]["record"][
            "message_id"
        ]
        == "1"
    )


async def test_unload_reload_restores_one_save_wrapper(service, tmp_path):
    original = service.flow.hook.original
    service.flow.terminate()
    assert service.manager.update_conversation == original
    service.flow.initialize(tmp_path / "flow")
    event, req = group_event("1"), flow_request()
    await service.flow.prepare(event, req, text_factory)
    run = bound_run(service.flow, event, req)
    await service.manager.update_conversation(
        event.unified_msg_origin, "conversation", history=saved_history(req)
    )
    assert (
        service.flow.store.window(event.unified_msg_origin, "conversation", 2)["cursor"]
        == 1
    )
    assert run.finished


async def test_cancellation_drains_record_write_before_migration_can_start(
    service, monkeypatch
):
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = service.flow.store.append
    event = group_event("1")

    def blocked_append(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=3)
        return original(*args)

    monkeypatch.setattr(service.flow.store, "append", blocked_append)
    task = asyncio.create_task(service.flow.record(event))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert service.flow.receiving[event.unified_msg_origin] == 1
    task.cancel()
    await asyncio.sleep(0)
    assert service.flow.receiving[event.unified_msg_origin] == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.flow.receiving[event.unified_msg_origin] == 0
    assert service.flow.store.dump(event.unified_msg_origin)["flow"]["last_seq"] == 1


async def test_unload_drains_inflight_file_writer_before_reload(
    service, monkeypatch, tmp_path
):
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = service.flow.store.append

    def blocked_append(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=3)
        return original(*args)

    monkeypatch.setattr(service.flow.store, "append", blocked_append)
    writing = asyncio.create_task(service.flow.record(group_event("1")))
    await entered.wait()
    closing = asyncio.create_task(service.flow.aclose())
    await asyncio.sleep(0)
    assert not closing.done()
    assert await service.flow.record(group_event("2")) is None
    release.set()
    await asyncio.gather(writing, closing)
    service.flow.initialize(tmp_path / "flow")
    assert await service.flow.record(group_event("2")) == 2


async def test_cancelled_task_releases_pending_without_advancing_cursor(service):
    ready = asyncio.Event()
    event = group_event("1")

    async def work():
        req = flow_request()
        await service.flow.prepare(event, req, text_factory)
        run = Run(event, req)
        service.flow.bind_runner(run)
        ready.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert not service.flow.pending
    assert (
        service.flow.store.window(event.unified_msg_origin, "conversation", 2)["cursor"]
        == 0
    )
