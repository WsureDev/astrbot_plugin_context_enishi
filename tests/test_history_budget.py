from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from astrbot_plugin_context_enishi.channels.messages import event_meta
from astrbot_plugin_context_enishi.channels.meta import table
from astrbot_plugin_context_enishi.context.history.budget import HistoryWindow
from astrbot_plugin_context_enishi.context.history.markers import deduplicate_markers
from astrbot_plugin_context_enishi.context.history.structure import (
    content_size,
    delta_table,
    rounds,
)
from astrbot_plugin_context_enishi.context.service import ContextOrganizer
from astrbot_plugin_context_enishi.settings import HISTORY_DEFAULTS, Settings

from .conftest import Event, request, text_factory
from .flow_support import (
    bound_run,
    flow_request,
    flow_service,
    group_event,
    saved_history,
)
from .test_integration import RunnerBoundary
from .test_runtime import runtime as runtime


def delta(event, records):
    return {
        "role": "user",
        "content": table(records, event_meta(event), tag="group_messages_delta"),
    }


def window(messages, config=None, event=None, current_delta=None):
    event = event or Event()
    runner = SimpleNamespace(
        run_context=SimpleNamespace(
            messages=messages, context=SimpleNamespace(event=event)
        ),
        req=SimpleNamespace(_enishi_flow_delta=current_delta),
    )
    budget = HistoryWindow(Settings(config or {}), runner, text_factory)
    return budget, runner


def test_history_range_drops_complete_old_rounds_and_protects_current_chain():
    messages = [{"role": "system", "content": "persona"}]
    for index in range(5):
        messages.extend(
            [
                {"role": "user", "content": str(index) + "x" * 100},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": str(index),
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": str(index), "content": "result" * 20},
                {"role": "assistant", "content": "answer"},
            ]
        )
    current = {"role": "user", "content": "CURRENT" * 500}
    messages.append(current)
    cost = sum(content_size(message) for message in messages[1:5])
    budget, runner = window(
        messages, {"history_budget": {"max_chars": cost * 3, "target_chars": cost * 2}}
    )
    active_result = {
        "role": "tool",
        "tool_call_id": "current",
        "content": "HUGE" * 1000,
    }
    messages.extend(
        [
            {
                "role": "assistant",
                "tool_calls": [{"id": "current", "function": {"name": "lookup"}}],
            },
            active_result,
        ]
    )
    assert budget.apply(messages)
    assert (
        messages[0]["content"] == "persona"
        and current in messages
        and active_result in messages
    )
    past = messages[1 : messages.index(current)]
    assert sum(content_size(message) for message in past) <= cost * 3
    assert len(rounds(past, runner.run_context.context.event)) == 2
    declarations = {
        call["id"] for message in messages for call in message.get("tool_calls", [])
    }
    assert all(
        message["tool_call_id"] in declarations
        for message in messages
        if message["role"] == "tool"
    )
    assert runner.run_context._enishi_history_budget_stats["removed_rounds"] == 3


def test_group_budget_keeps_newest_whole_rows_and_current_delta_proof():
    event = Event()
    records = [
        {
            "message_id": str(index),
            "timestamp": 100 + index,
            "sender": {"user_id": "author"},
            "message_str": "text" * 100,
            "message": [],
        }
        for index in range(20)
    ]
    old = delta(event, records)
    fresh = delta(event, [{**records[-1], "message_id": "fresh"}])
    current = {"role": "user", "content": "current proof"}
    messages = [
        old,
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        fresh,
        current,
    ]
    group_limit = len(fresh["content"]) + 1800
    before_fresh = copy.deepcopy(fresh)
    budget, runner = window(
        messages, {"history_budget": {"max_group_chars": group_limit}}, event, fresh
    )
    assert budget.apply(messages)
    assert fresh == before_fresh and current in messages
    tables = [
        delta_table(message, event)
        for message in messages
        if delta_table(message, event) is not None
    ]
    assert (
        sum(
            len(message["content"])
            for message in messages
            if delta_table(message, event) is not None
        )
        <= group_limit
    )
    assert any("19|" in row for _, _, rows in tables for row in rows)
    assert not any(row.startswith("0|") for _, _, rows in tables for row in rows)
    assert runner.run_context._enishi_history_budget_stats["removed_group_rows"] > 0


def test_group_delta_and_tool_image_messages_do_not_split_tool_rounds():
    event = Event()
    block = delta(
        event,
        [{"message_id": "1", "message_str": "ambient", "sender": {}, "message": []}],
    )
    chain = [
        block,
        {"role": "user", "content": "task"},
        {"role": "assistant", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "[Image from tool 'read_file']"}],
        },
        {"role": "assistant", "content": "answer"},
    ]
    assert rounds(chain, event) == [chain]


def test_duplicate_image_markers_keep_every_id_and_source_and_leave_user_text():
    ids = [
        f"qq://bot/123/image/{index}.jpg?m=42&i={index}&f=fwd&n=0.{index}"
        for index in range(6)
    ]
    parts = [
        text_factory(text="\n".join(f"[图片 id={value} 来源=reply]" for value in ids))
    ]
    parts.extend(text_factory(text=f"[图片 id={value}]") for value in ids * 2)
    ordinary = text_factory(text="用户讨论 [图片 id=" + ids[0] + "]")
    parts.append(ordinary)
    messages = [{"role": "user", "content": parts}]
    assert deduplicate_markers(messages, text_factory) == 12
    assert len(messages[0]["content"]) == 2
    assert messages[0]["content"][-1] is ordinary
    assert all(value in messages[0]["content"][0]["text"] for value in ids)
    assert "来源=reply" in messages[0]["content"][0]["text"]


def test_small_configured_limits_are_not_replaced_by_hidden_minimums():
    values = {
        "enabled": True,
        "max_chars": 100,
        "target_chars": 50,
        "max_group_chars": 0,
    }
    assert Settings({"history_budget": values}).history_options() == values
    assert Settings({}).history_options() == HISTORY_DEFAULTS
    with pytest.raises(ValueError, match="不能大于"):
        Settings(
            {"history_budget": {"max_chars": 100, "target_chars": 200}}
        ).history_options()
    messages = [
        {"role": "user", "content": "old" * 100},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "current"},
    ]
    budget, _ = window(messages, {"history_budget": {"enabled": False}})
    original = copy.deepcopy(messages)
    assert not budget.apply(messages) and messages == original


async def test_tiny_group_budget_omits_delta_without_breaking_save_acknowledgement(
    tmp_path,
):
    service = flow_service(tmp_path)
    service.config["history_budget"] = {"max_group_chars": 1}
    try:
        await service.flow.record(group_event("old", "old message"))
        event, req = group_event("current"), flow_request()
        await service.flow.prepare(event, req, text_factory)
        assert req.contexts == [] and req._enishi_flow_delta is None
        proof = req.extra_user_content_parts[-1]["text"]
        assert "injected_count=0" in proof and "skipped_count=1" in proof
        bound_run(service.flow, event, req)
        await service.manager.update_conversation(
            event.unified_msg_origin, "conversation", history=saved_history(req)
        )
        assert (
            service.flow.store.window(event.unified_msg_origin, "conversation", 3)[
                "cursor"
            ]
            == 2
        )
    finally:
        await service.flow.aclose()


async def test_native_runner_budget_keeps_monitor_methods_and_saves_current_exactly(
    service, runtime
):
    class Runner(RunnerBoundary):
        step = runtime.compiled.step

    service.config["history_budget"] = {
        "max_chars": 400,
        "target_chars": 200,
        "max_group_chars": 0,
    }
    service.config["channel_settings"][0]["image_mode"] = "disabled"
    original_step = getattr(Runner, "step", None)
    messages = [runtime.messages.Message(role="system", content="persona")]
    for index in range(4):
        messages.extend(
            [
                runtime.messages.Message(role="user", content="old" * 100),
                runtime.messages.Message(role="assistant", content="answer"),
            ]
        )
    current = runtime.messages.Message(role="user", content="current exact")
    messages.append(current)
    runner = Runner()
    runner.req = request()
    runner.run_context = SimpleNamespace(
        context=SimpleNamespace(event=service.event), messages=messages
    )
    organizer = ContextOrganizer(service.context, service.settings, lambda event: True)
    organizer.policy, organizer.channels = service.policy, service.channels
    integration = organizer.integration
    integration.install(Runner)
    try:
        await runner.reset()
        result = await runner.request_context_manager.process(
            messages, trusted_token_usage=100000
        )
        assert result[-1] is current
        saved = runtime.messages.dump_messages_with_checkpoints(result)
        assert saved[-1]["content"] == "current exact"
        assert sum(message.get("content") == "old" * 100 for message in saved) == 1
        assert (
            runner.run_context._enishi_history_budget_stats["history_chars_after"]
            <= 400
        )
        assert getattr(Runner, "step", None) is original_step
    finally:
        integration.terminate()
