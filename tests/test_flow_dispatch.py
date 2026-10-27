"""Run AstrBot's actual waking/process stages for plain group messages."""

from __future__ import annotations

import ast
import enum
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from .conftest import text_factory
from .flow_support import flow_request, flow_service, group_event
from .test_main import plugin_class as plugin_class


def native_class(root, relative, name, namespace):
    tree = ast.parse((root / relative).read_text())
    node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == name
    )
    node.decorator_list = []
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            node,
        ],
        type_ignores=[],
    )
    exec(
        compile(ast.fix_missing_locations(module), str(root / relative), "exec"),
        namespace,
    )
    return namespace[name]


async def test_native_pipeline_records_unmentioned_messages_then_injects_them(
    tmp_path, plugin_class
):
    source = os.environ.get("ASTRBOT_SOURCE")
    if not source:
        pytest.skip("set ASTRBOT_SOURCE for native group message dispatch")
    root = Path(source)
    namespace = dict(
        enum=enum, Enum=enum.Enum, Stage=object, HandlerFilter=object, FILTERS=[]
    )
    message_type = native_class(
        root, "astrbot/core/platform/message_type.py", "MessageType", namespace
    )
    event_type = native_class(
        root,
        "astrbot/core/star/filter/event_message_type.py",
        "EventMessageType",
        namespace,
    )
    namespace["MESSAGE_TYPE_2_EVENT_MESSAGE_TYPE"] = {
        message_type.GROUP_MESSAGE: event_type.GROUP_MESSAGE
    }
    event_filter = native_class(
        root,
        "astrbot/core/star/filter/event_message_type.py",
        "EventMessageTypeFilter",
        namespace,
    )(event_type.GROUP_MESSAGE)
    service = flow_service(tmp_path)
    plugin = object.__new__(plugin_class)
    plugin.organizer = SimpleNamespace(
        flow=service.flow, diagnostics=service.flow.diagnostics
    )
    handler = SimpleNamespace(
        event_filters=[event_filter],
        handler_module_path="enishi.main",
        handler_full_name="enishi.record_group_message",
    )
    namespace.update(
        logger=logging.getLogger("flow-dispatch-test"),
        EventType=SimpleNamespace(AdapterMessageEvent="group-event"),
        star_handlers_registry=SimpleNamespace(
            get_handlers_by_event_type=lambda *a, **kw: [handler]
        ),
        SessionPluginManager=SimpleNamespace(
            filter_handlers_by_session=AsyncMock(
                side_effect=lambda event, handlers: handlers
            )
        ),
    )
    for name in ("At", "AtAll", "Reply", "PermissionTypeFilter", "CommandGroupFilter"):
        namespace[name] = type(name, (), {})
    waking = native_class(
        root,
        "astrbot/core/pipeline/waking_check/stage.py",
        "WakingCheckStage",
        namespace,
    )()
    config = dict(
        admins_id=[],
        wake_prefix=[],
        plugin_set=["*"],
        provider_settings=dict(enable=True),
    )
    waking.ctx = SimpleNamespace(astrbot_config=config)
    waking.unique_session = False
    waking.ignore_bot_self_message = False
    waking.ignore_at_all = False
    waking.friend_message_needs_wake_prefix = False
    waking.disable_builtin_commands = False
    waking._umo_auto_name_recorder = SimpleNamespace(schedule=Mock())
    process = native_class(
        root, "astrbot/core/pipeline/process_stage/stage.py", "ProcessStage", namespace
    )()

    async def record_only(event):
        assert event.get_extra("activated_handlers") == [handler]
        await plugin.record_group_message(event)
        if False:
            yield

    async def unexpected_llm(event):
        pytest.fail("plain group message must not call the LLM")
        yield

    process.ctx = SimpleNamespace(astrbot_config=config)
    process.star_request_sub_stage = SimpleNamespace(process=record_only)
    process.agent_sub_stage = SimpleNamespace(process=unexpected_llm)
    try:
        for mid, uid in (("1", "1001"), ("2", "1002")):
            event = group_event(mid, "ordinary chat", uid=uid)
            event.get_message_type = lambda: message_type.GROUP_MESSAGE
            event.message_str = "ordinary chat"
            event.get_messages = lambda: []
            event.is_private_chat = lambda: False
            event.get_self_id = lambda: "9000"
            event._extras = event.extra

            def get_extra(key=None, default=None):
                if key is None:
                    return event.extra
                return event.extra.get(key, default)

            event.get_extra = get_extra
            event.is_at_or_wake_command = False
            event._has_send_oper = False
            event.call_llm = False
            event.stop_event = Mock()
            await waking.process(event)
            [result async for result in process.process(event)]
            event.stop_event.assert_not_called()
            assert not event.is_at_or_wake_command
        req = flow_request()
        trigger = group_event("3", "summarize the group", uid="1003")
        await service.flow.prepare(trigger, req, text_factory)
        delta = req.contexts[0]["content"]
        assert "1001" in delta and "1002" in delta and "1003" not in delta
        assert delta.count("ordinary chat") == 2
        assert "injected_count=2" in req.extra_user_content_parts[-1]["text"]
        assert (
            service.flow.store.dump(trigger.unified_msg_origin)["flow"]["last_seq"] == 3
        )
    finally:
        await service.flow.aclose()
