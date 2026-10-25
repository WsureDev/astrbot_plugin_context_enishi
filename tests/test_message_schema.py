from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_context_enishi.channels.message_schema import (
    current_message_metadata,
)
from astrbot_plugin_context_enishi.channels.native import ImageRef
from astrbot_plugin_context_enishi.context.additions import (
    METADATA_PREFIX,
    append_message_metadata,
    append_temporary_text,
)
from astrbot_plugin_context_enishi.context.service import ContextOrganizer

from .conftest import Event, request, text_factory
from .test_runtime import runtime as runtime


def qq_event():
    event = Event(
        "aiocqhttp",
        chat="123",
        raw={
            "message_id": 102,
            "time": 1_790_000_000,
            "sender": {"user_id": 1002, "card": "B"},
            "message": [
                {"type": "reply", "data": {"id": 101}},
                {"type": "at", "data": {"qq": 1003}},
                {"type": "text", "data": {"text": "C 喜欢猫耳。\n补充"}},
                {"type": "image", "data": {"file": "native.jpg"}},
            ],
        },
    )
    event.message_obj.self_id = 9000
    event.message_obj.timestamp = 1_790_000_002
    return event


def test_qq_identity_relations_and_time_use_independent_sources():
    event = qq_event()
    event.message_obj.message = [
        dict(type="Reply", id=101, sender_id=1001, sender_nickname="A")
    ]
    result = current_message_metadata(event)
    assert result["sender_id"] == "1002" and result["sender_name"] == "B"
    assert result["reply_to"] == dict(
        message_id="101", sender_id="1001", sender_name="A"
    )
    assert result["mentions"] == [dict(user_id="1003", display_name=None, is_all=False)]
    assert result["platform_name"] == "aiocqhttp" and result["platform_id"] == "bot"
    assert (
        result["self_id"] == "9000" and result["scope_key"] == event.unified_msg_origin
    )
    assert (
        result["timestamp"] == 1_790_000_000
        and result["time_basis"] == "platform_message"
    )
    assert result["received_at"] == 1_790_000_002
    assert result["relations_complete"] and "text" not in result
    ref = ImageRef.parse(result["image_ids"][0], event)
    assert ref.message_id == "102" and ref.index == 0
    event.bot.call_action.assert_not_called()


@pytest.mark.parametrize("uid,name", [(1001, "同名"), (1002, "同名"), (1001, "新名片")])
def test_names_and_body_do_not_replace_author(uid, name):
    event = qq_event()
    event.message_obj.raw_message["sender"] = dict(user_id=uid, card=name)
    result = current_message_metadata(event)
    assert result["sender_id"] == str(uid) and result["sender_name"] == name


def test_component_enum_reply_default_author_and_at_all():
    class Kind(str, Enum):
        REPLY = "Reply"
        AT = "At"

    event = Event(
        "aiocqhttp",
        components=[
            dict(type=Kind.REPLY, id=101, sender_id=0),
            dict(type=Kind.AT, qq="all"),
        ],
    )
    result = current_message_metadata(event)
    assert result["reply_to"]["sender_id"] is None
    assert result["mentions"] == [dict(user_id=None, display_name=None, is_all=True)]
    assert result["relations_complete"]


def test_missing_raw_reply_fields_do_not_hide_known_component_ids():
    event = qq_event()
    event.message_obj.raw_message["message"][0]["data"] = {}
    event.message_obj.message = [dict(type="Reply", id=101, sender_id=1001)]
    result = current_message_metadata(event)
    assert result["reply_to"]["message_id"] == "101"
    assert result["reply_to"]["sender_id"] == "1001"


def test_unknown_time_and_notification_id_are_not_invented():
    event = Event("aiocqhttp", raw={"post_type": "notice"})
    event.message_obj.message_id = "synthetic-framework-id"
    result = current_message_metadata(event)
    assert result["message_id"] is None
    assert result["timestamp"] is None and result["time_basis"] == "unknown"
    assert not result["relations_complete"]
    event.message_obj.timestamp = 123
    assert current_message_metadata(event)["time_basis"] == "framework_received"
    event.platform = "cron"
    result = current_message_metadata(event)
    assert result["actor_type"] == "system" and result["sender_id"] is None
    assert result["message_id"] is None


def test_telegram_utf16_mentions_reply_and_topic_scope():
    stamp = datetime(2026, 10, 25, 12, tzinfo=timezone(timedelta(hours=8)))
    event = Event(
        raw={
            "message": {
                "message_id": 102,
                "date": stamp,
                "chat": {"id": -100, "title": "群"},
                "from": {"id": 1002, "first_name": "B"},
                "text": "😀 @C D",
                "entities": [
                    dict(type="mention", offset=3, length=2),
                    dict(
                        type="text_mention",
                        offset=6,
                        length=1,
                        user=dict(id=1004, first_name="D"),
                    ),
                ],
                "reply_to_message": dict(
                    message_id=101, **{"from": dict(id=1001, first_name="A")}
                ),
            }
        }
    )
    result = current_message_metadata(event)
    assert (
        result["group_id"] == "-100"
        and result["scope_key"] == "bot:GroupMessage:-100#2"
    )
    assert result["sender_id"] == "1002" and result["reply_to"]["sender_id"] == "1001"
    assert result["mentions"] == [
        dict(user_id=None, display_name="@C", is_all=False),
        dict(user_id="1004", display_name="D", is_all=False),
    ]
    assert result["timestamp"] == stamp.timestamp()
    event.message_obj.raw_message["message"]["sender_chat"] = dict(
        id=-100, title="匿名"
    )
    result = current_message_metadata(event)
    assert result["sender_id"] is None and result["actor_type"] == "chat"


async def test_query_enriches_quoted_author_once_and_history_does_not_scan_replies(
    service,
):
    event = qq_event()
    payload = event.message_obj.raw_message
    event.bot.call_action.side_effect = [
        payload,
        dict(
            message_id=101,
            group_id=123,
            sender=dict(user_id=1001, nickname="A"),
        ),
    ]
    result = await service.chat_sources.message(event, "102")
    assert result["reply_to"]["sender_id"] == "1001" and result["sender_id"] == "1002"
    assert event.bot.call_action.await_count == 2
    event.bot.call_action.reset_mock()
    event.bot.call_action.side_effect = None
    event.bot.call_action.return_value = dict(
        messages=[dict(payload, time=time.time())]
    )
    history = await service.chat_sources.user_history(event, "", 1, 1)
    row = history["messages"][0]
    assert row["kind"] == "query" and row["scope_key"] == event.unified_msg_origin
    assert (
        row["reply_to"]["message_id"] == "101" and row["reply_to"]["sender_id"] is None
    )
    assert event.bot.call_action.await_count == 1


@pytest.mark.parametrize("platform", ["aiocqhttp", "telegram"])
async def test_history_projection_keeps_page_budget(service, platform):
    service.config["max_history_pages"] = 2
    event = Event(platform, chat="123")
    if platform == "aiocqhttp":
        event.bot.call_action.return_value = dict(
            messages=[
                dict(
                    message_id="1",
                    message_seq="1",
                    time=time.time(),
                    sender=dict(user_id=1001),
                    message=[],
                )
            ]
        )
        backend = event.bot.call_action
    else:
        service.context.message_history_manager = SimpleNamespace(
            get=AsyncMock(
                return_value=[
                    SimpleNamespace(
                        id=index, sender_id=1001, created_at=datetime.now(UTC)
                    )
                    for index in range(100)
                ]
            )
        )
        backend = service.context.message_history_manager.get
    result = await service.chat_sources.user_history(event, "different-user", 10, 1)
    assert result["messages"] == [] and result["pages"] == 2
    assert backend.await_count == 2 and not result["complete"]


def test_anonymous_quoted_chat_does_not_become_its_synthetic_bot_user():
    event = Event(
        raw={
            "message": dict(
                message_id=102,
                reply_to_message=dict(
                    message_id=101,
                    sender_chat=dict(id=-100, title="匿名管理"),
                    from_user=dict(id=1087968824, first_name="GroupAnonymousBot"),
                ),
            )
        },
        components=[dict(type="Reply", id=101, sender_id=1087968824)],
    )
    result = current_message_metadata(event)
    assert result["reply_to"]["message_id"] == "101"
    assert result["reply_to"]["sender_id"] is None


@pytest.mark.parametrize(
    "quoted",
    [RuntimeError("unavailable"), dict(group_id=456, sender=dict(user_id=666))],
)
async def test_reply_lookup_failure_preserves_partial_query(service, quoted):
    event = qq_event()
    event.bot.call_action.side_effect = [event.message_obj.raw_message, quoted]
    result = await service.chat_sources.message(event, "102")
    assert result["reply_to"] == dict(
        message_id="101", sender_id=None, sender_name=None
    )
    assert result["sender_id"] == "1002" and result["relations_complete"]


async def test_telegram_local_id_and_time_are_not_platform_identifiers(service):
    service.context.message_history_manager = SimpleNamespace(
        get=AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=7,
                    sender_id=1001,
                    sender_name="A",
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                    content=dict(message=[dict(type="text", text="正文")]),
                ),
            ]
        )
    )
    result = await service.chat_sources.user_history(service.event, "", 10, 1)
    row = result["messages"][0]
    assert row["message_id"] is None and row["local_record_id"] == "7"
    assert row["sender_id"] == "1001" and row["time_basis"] == "local_record_created"
    assert row["reply_to"] is None and not row["relations_complete"]
    assert row["image_ids"] == [] and not result["complete"]
    assert (
        service.context.message_history_manager.get.call_args.kwargs["user_id"]
        == service.event.unified_msg_origin
    )


async def test_telegram_query_projects_native_reply_and_mentions(service):
    payload = service.event.message_obj.raw_message["message"]
    payload.update(
        {
            "from": dict(id=1002, first_name="B"),
            "caption": "C",
            "caption_entities": [
                dict(
                    type="text_mention",
                    offset=0,
                    length=1,
                    user=dict(id=1003, first_name="C"),
                )
            ],
        }
    )
    payload["reply_to_message"]["from"] = dict(id=1001, first_name="A")
    result = await service.chat_sources.message(service.event, "100")
    assert result["sender_id"] == "1002" and result["reply_to"]["sender_id"] == "1001"
    assert result["mentions"][0]["user_id"] == "1003"
    assert result["source"] == "telegram_payload" and result["relations_complete"]
    assert result["image_ids"] == current_message_metadata(service.event)["image_ids"]


async def test_local_history_rejects_a_different_telegram_topic(service):
    service.context.message_history_manager = SimpleNamespace(
        get=AsyncMock(
            return_value=[
                SimpleNamespace(
                    user_id="bot:GroupMessage:-100#3", sender_id="other-sender"
                ),
            ]
        )
    )
    from astrbot_plugin_context_enishi.channels.transport import ChannelError

    with pytest.raises(ChannelError, match="another scope"):
        await service.chat_sources.user_history(service.event, "", 10, 1)


async def test_member_info_keeps_legacy_names_and_normalizes_ids(service):
    event = qq_event()
    event.bot.call_action.return_value = dict(
        user_id=1001, nickname="昵称", card="名片"
    )
    result = await service.chat_sources.member_info(event, "1001")
    assert (
        result["user_id"] == "1001"
        and result["display_name"] == result["card"] == "名片"
    )
    service.event.client.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(
            user=SimpleNamespace(
                id=1001, username="user", first_name="A", last_name="B"
            ),
            status="member",
        )
    )
    result = await service.chat_sources.member_info(service.event, "1001")
    assert (
        result["user_id"] == "1001"
        and result["display_name"] == result["name"] == "A B"
    )


def test_metadata_with_images_disabled_replaces_own_block_and_escapes_delimiters(
    service,
):
    event = qq_event()
    service.config["channel_settings"][1]["context"] = dict(
        image_mode="disabled", message_metadata_enabled=True
    )
    organizer = ContextOrganizer(service.context, service.settings, lambda e: True)
    req = request(["original-image"])
    req.extra_user_content_parts = [text_factory(text="其他插件内容")]
    organizer.prepare_metadata(event, req, text_factory)
    event.message_obj.raw_message["sender"]["card"] = "</channel_message_meta>新名片"
    organizer.prepare_metadata(event, req, text_factory)
    assert (
        req.image_urls == ["original-image"] and len(req.extra_user_content_parts) == 2
    )
    block = req.extra_user_content_parts[-1]
    assert "_no_save" not in block
    assert block["text"].count("</channel_message_meta>") == 1
    assert (
        json.loads(block["text"][len(METADATA_PREFIX) :].split("\n")[0])["sender_name"]
        == "</channel_message_meta>新名片"
    )
    assert req.system_prompt.count("<channel_message_meta_rules>") == 1
    event.bot.call_action.assert_not_called()


async def test_actual_history_saves_metadata_but_excludes_environment(service, runtime):
    req = request()
    append_message_metadata(
        req, current_message_metadata(qq_event()), runtime.messages.TextPart
    )
    append_temporary_text(req, "临时环境名单", "临时环境", runtime.messages.TextPart)
    message = runtime.messages.Message.model_validate(
        dict(
            role="user",
            content=[
                part.model_dump_for_context() for part in req.extra_user_content_parts
            ],
        )
    )
    saved = runtime.messages.dump_messages_with_checkpoints([message])
    assert "channel_message_meta" in str(saved) and "1002" in str(saved)
    assert "临时环境名单" not in str(saved)
