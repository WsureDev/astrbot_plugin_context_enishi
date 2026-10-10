from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_context_enishi.awareness.service import ChatAwareness
from astrbot_plugin_context_enishi.channels.transport import ChannelError
from astrbot_plugin_context_enishi.context.additions import append_temporary_text
from astrbot_plugin_context_enishi.context.service import ContextOrganizer
from astrbot_plugin_context_enishi.settings import Settings, migrate_channel_settings

from .conftest import Event, request, text_factory
from .test_runtime import runtime as runtime

NOW = 2_000_000_000


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_context_enishi.awareness.participants.time.time", lambda: NOW
    )


def qq_event():
    return Event(
        "aiocqhttp",
        chat="123",
        raw={
            "message_id": "current",
            "time": NOW,
            "sender": {"user_id": "99", "card": "当前发言者"},
        },
    )


def roster():
    return [
        dict(
            user_id="10",
            nickname="甲",
            card="同名",
            role="member",
            last_sent_time=NOW - 100,
        ),
        dict(
            user_id="20",
            nickname="乙",
            card="同名",
            role="admin",
            last_sent_time=NOW - 10,
        ),
        dict(user_id="30", nickname="未观察到发言", role="member", last_sent_time=0),
        dict(user_id="99", nickname="当前", role="member", last_sent_time=NOW - 20),
    ]


def qq_backend(event, pages=None, people=None):
    people = roster() if people is None else people

    async def call(action, **params):
        assert str(params.get("group_id")) == "123"
        if action == "get_group_info":
            return dict(group_id="123", group_name="测试群", member_count=len(people))
        if action == "get_group_member_list":
            return people
        if action == "get_group_msg_history":
            assert params["disable_get_url"] and not params["parse_mult_msg"]
            return {"messages": (pages or {}).get(params.get("message_seq", ""), [])}
        raise AssertionError(action)

    event.bot.call_action.side_effect = call


def message(mid, uid, seconds, name=""):
    return dict(
        message_id=mid,
        message_seq=mid,
        group_id="123",
        time=NOW - seconds,
        sender={"user_id": uid, "nickname": name},
        message=[{"type": "image", "data": {"url": "https://secret.invalid/image"}}],
    )


def awareness(service):
    return ChatAwareness(service.context, service.settings, lambda e: True)


async def test_recent_roster_is_sorted_by_last_speech_without_history_scan(
    service, clock
):
    event = qq_event()
    qq_backend(event)
    result = await awareness(service).participants(event)
    assert [person["user_id"] for person in result["participants"]] == [
        "99",
        "20",
        "10",
        "30",
    ]
    assert result["participants"][-1]["last_spoke_at"] is None
    assert result["participants"][1]["role"] == "admin"
    assert result["membership_complete"]
    assert all(person["observed_messages"] is None for person in result["participants"])
    assert [call.args[0] for call in event.bot.call_action.call_args_list] == [
        "get_group_info",
        "get_group_member_list",
    ]
    assert (
        result["participants"][1]["display_name"]
        == result["participants"][2]["display_name"]
    )


async def test_frequency_scans_later_pages_before_limiting_people_and_deduplicates(
    service, clock
):
    event = qq_event()
    first = [
        message("current", "99", 0),
        message("one", "20", 10),
        message("cursor", "20", 100),
    ]
    second = [
        message("cursor", "20", 100),
        *[message(f"old{i}", "10", 200 + i) for i in range(4)],
        message("boundary", "30", 4000),
    ]
    qq_backend(event, {"": first, "cursor": second})
    result = await awareness(service).participants(
        event, order="frequency", limit=1, hours=1
    )
    assert result["participants"][0]["user_id"] == "10"
    assert result["participants"][0]["observed_messages"] == 4
    assert result["has_more"] and result["activity"]["pages"] == 2
    assert result["activity"]["stop_reason"] == "window_boundary"
    assert not result["complete"]
    all_results = await awareness(service).participants(
        event, order="frequency", hours=1
    )
    assert (
        next(
            person
            for person in all_results["participants"]
            if person["user_id"] == "99"
        )["observed_messages"]
        == 1
    )
    assert "secret.invalid" not in json.dumps(all_results)


async def test_scan_budget_reports_partial_observed_counts(service, clock):
    service.config["channel_settings"][1]["awareness"] = {"scan_pages": 1}
    event = qq_event()
    qq_backend(event, {"": [message("one", "20", 10)]})
    result = await awareness(service).participants(event, order="frequency")
    assert result["activity"]["stop_reason"] == "scan_budget"
    assert result["activity"]["counts_are_observed_only"] and not result["complete"]


async def test_historical_window_uses_speech_in_window_not_later_native_recency(
    service, clock
):
    event = qq_event()
    people = roster()
    people[0]["last_sent_time"] = NOW
    qq_backend(
        event,
        {
            "": [
                message("a", "10", 300),
                message("b", "20", 200),
                message("bound", "30", 600),
            ]
        },
        people,
    )
    result = await awareness(service).participants(
        event, order="recent", start_time=str(NOW - 500), end_time=str(NOW - 100)
    )
    assert [person["user_id"] for person in result["participants"]] == ["20", "10"]
    assert result["participants"][1]["last_spoke_at"] == NOW - 300


@pytest.mark.parametrize(
    "args",
    [
        dict(order="wrong"),
        dict(hours=0),
        dict(hours=float("nan")),
        dict(end_time=str(NOW + 100)),
        dict(start_time=str(NOW), end_time=str(NOW - 1)),
        dict(start_time="2026-01-01T00:00:00"),
    ],
)
async def test_invalid_order_and_time_windows_fail_before_channel_queries(
    service, clock, args
):
    event = qq_event()
    with pytest.raises(ValueError):
        await awareness(service).participants(event, **args)
    event.bot.call_action.assert_not_called()


async def test_native_roster_failure_falls_back_to_observed_history(service, clock):
    event = qq_event()

    async def call(action, **params):
        if action == "get_group_info":
            return {"group_name": "测试群"}
        if action == "get_group_member_list":
            raise RuntimeError("unavailable")
        return {"messages": []}

    event.bot.call_action.side_effect = call
    result = await awareness(service).participants(event)
    assert not result["membership_complete"]
    assert result["participants"][0]["user_id"] == "99"
    assert "native_member_list_unavailable" in result["warnings"]


def local_record(event, rid, uid, seconds, role="user"):
    return SimpleNamespace(
        id=rid,
        platform_id=event.get_platform_id(),
        user_id=event.unified_msg_origin,
        sender_id=uid,
        sender_name=f"用户{uid}",
        created_at=datetime.fromtimestamp(NOW - seconds, UTC),
        content={
            "type": role,
            "message": [
                {
                    "type": "image",
                    "url": "https://api.telegram.org/file/botSECRET/image.jpg",
                }
            ],
        },
    )


async def test_tg_uses_real_session_and_topic_scope_and_never_enumerates_members(
    service, clock
):
    event = service.event
    event.extra["_current_platform_message_history_id"] = 9
    records = [
        local_record(event, 9, "99", 0),
        local_record(event, 8, "11", 10),
        local_record(event, 7, "11", 20),
        local_record(event, 6, "bot", 30, "bot"),
    ]
    service.context.message_history_manager = SimpleNamespace(
        get=AsyncMock(return_value=list(reversed(records)))
    )
    result = await awareness(service).participants(event, order="frequency", hours=1)
    assert result["participants"][0]["user_id"] == "11"
    assert result["participants"][0]["observed_messages"] == 2
    assert (
        next(person for person in result["participants"] if person["user_id"] == "99")[
            "observed_messages"
        ]
        == 1
    )
    assert result["source"] == "astrbot_local_and_current_payload"
    assert not result["membership_complete"] and not result["complete"]
    assert (
        service.context.message_history_manager.get.call_args.kwargs["user_id"]
        == event.unified_msg_origin
    )
    assert result["chat"]["chat_id"] == "-100#2"
    assert "SECRET" not in json.dumps(result)
    event.client.get_file.assert_not_called()


async def test_tg_missing_history_has_only_current_speaker_without_fabricated_count(
    service, clock
):
    result = await awareness(service).participants(service.event, order="frequency")
    assert len(result["participants"]) == 1
    assert result["participants"][0]["user_id"] == "99"
    assert result["participants"][0]["observed_messages"] is None
    assert not result["activity"]["current_message_counted"]
    assert result["activity"]["stop_reason"] == "no_local_history"


async def test_tg_history_from_another_topic_is_not_exposed(service, clock):
    record = local_record(service.event, 1, "private-other-sender", 0)
    record.user_id = "bot:GroupMessage:-100#3"
    service.context.message_history_manager = SimpleNamespace(
        get=AsyncMock(return_value=[record])
    )
    result = await awareness(service).participants(service.event)
    assert "private-other-sender" not in json.dumps(result)
    assert result["activity"]["stop_reason"] == "source_error"


async def test_default_tools_only_performs_no_queries_or_request_mutation(service):
    aware = awareness(service)
    req = request(["current-image"])
    before = copy.deepcopy(req.__dict__)
    assert await aware.request_hint(service.event) is None
    assert req.__dict__ == before
    service.event.bot.call_action.assert_not_called()
    service.event.client.get_file.assert_not_called()
    service.context.llm_generate.assert_not_called()


async def test_awareness_and_context_modes_are_independent(service, clock):
    service.config["channel_settings"][1]["image_mode"] = "disabled"
    event = qq_event()
    qq_backend(event)
    result = await awareness(service).participants(event)
    assert result["participants"]
    service.config["channel_settings"][1]["image_mode"] = "reference_only"
    service.config["channel_settings"][1]["awareness"] = {"enabled": False}
    aware = awareness(service)
    with pytest.raises(ChannelError, match="未启用"):
        await aware.participants(event)
    organizer = ContextOrganizer(service.context, service.settings, lambda event: True)
    organizer.policy = service.policy
    req = request(["pixels"])
    await organizer.prepare_request(service.event, req)
    assert not req.image_urls


async def test_opt_in_hint_is_bounded_and_composed_as_temporary_text(service):
    service.config["channel_settings"][0]["awareness"] = {
        "inject_environment": True,
        "summary_max_chars": 400,
        "summary_members": 30,
    }
    aware = awareness(service)
    aware.environment = AsyncMock(
        return_value={
            "chat": {"chat_id": "-100#2"},
            "current_speaker": {"user_id": "99"},
            "source": "test",
            "participants": [
                {"user_id": str(i), "display_name": "测试昵称" * 20} for i in range(30)
            ],
        }
    )
    hint = await aware.request_hint(service.event)
    assert hint and len(hint) <= 400
    req = request()
    req.extra_user_content_parts = [text_factory(text="原有内容")]
    append_temporary_text(req, hint, aware.HINT_PREFIX, text_factory)
    append_temporary_text(req, hint, aware.HINT_PREFIX, text_factory)
    assert len(req.extra_user_content_parts) == 2
    assert req.extra_user_content_parts[-1]["_no_save"]
    assert req.system_prompt == "persona"


async def test_private_chat_environment_available_but_roster_is_not(service):
    event = Event(chat="")
    snapshot = await awareness(service).environment(event)
    assert snapshot["chat"]["type"] == "private"
    with pytest.raises(ChannelError, match="仅适用于群聊"):
        await awareness(service).participants(event)


def test_legacy_flat_settings_migrate_once_without_disabling_image_policy():
    config = {
        "channel_settings": [
            {
                "__template_key": "qq",
                "scope": {"chat_id": "123"},
                "image_mode": "reference_only",
                "caption_provider_id": "vision",
                "context": {"image_mode": "disabled"},
                "awareness": {"enabled": False},
            }
        ]
    }
    assert migrate_channel_settings(config)
    selected = Settings(config)
    assert selected.for_event(qq_event()).mode == "reference_only"
    assert not selected.awareness_for_event(qq_event()).enabled
    assert config["channel_settings"][0]["scope"] == {"chat_id": "123"}
    assert not migrate_channel_settings(config)


async def test_qq_environment_includes_quoted_sender_but_not_quoted_pixels(
    service, clock
):
    event = qq_event()
    event.message_obj.raw_message["message"] = [
        {"type": "reply", "data": {"id": "quoted"}}
    ]
    qq_backend(event)
    backend = event.bot.call_action.side_effect

    async def call(action, **params):
        if action == "get_msg":
            return dict(
                group_id="123",
                sender={"user_id": "10", "card": "被引用者"},
                message=[
                    {
                        "type": "image",
                        "data": {"url": "https://private-image.invalid/SECRET"},
                    }
                ],
            )
        return await backend(action, **params)

    event.bot.call_action.side_effect = call
    result = await awareness(service).environment(event)
    assert result["reply_to"]["user_id"] == "10"
    assert "SECRET" not in json.dumps(result)


async def test_anonymous_tg_sender_is_not_fabricated_as_a_user(service, clock):
    service.event.message_obj.raw_message = {
        "message": {
            "date": datetime.fromtimestamp(NOW, UTC),
            "sender_chat": {"id": "-100", "title": "匿名管理员"},
            "from_user": {"id": "synthetic-bot"},
        }
    }
    result = await awareness(service).environment(service.event)
    assert result["current_speaker"]["actor_type"] == "chat"
    assert result["current_speaker"]["user_id"] is None
    assert not result["participants"]


async def test_naive_local_record_time_is_treated_as_utc(service, clock):
    record = local_record(service.event, 1, "11", 30)
    record.created_at = record.created_at.replace(tzinfo=None)
    service.context.message_history_manager = SimpleNamespace(
        get=AsyncMock(return_value=[record])
    )
    result = await awareness(service).participants(
        service.event, order="frequency", hours=1
    )
    person = next(
        person for person in result["participants"] if person["user_id"] == "11"
    )
    assert person["last_spoke_at"] == NOW - 30 and person["observed_messages"] == 1


async def test_background_trigger_does_not_create_a_fictitious_group_member(
    service, clock
):
    event = qq_event()
    event.platform = "cron"
    event.context_obj = SimpleNamespace(
        get_platform_inst=lambda platform: SimpleNamespace(config={"type": "aiocqhttp"})
    )
    qq_backend(event)
    result = await awareness(service).environment(event)
    assert result["current_speaker"]["actor_type"] == "system"
    assert result["current_speaker"]["user_id"] is None
    listing = await awareness(service).participants(event, order="frequency", hours=1)
    assert not listing["participants"]


async def test_real_astrbot_serialization_excludes_opt_in_hint_from_history(
    service, runtime
):
    req = request()
    append_temporary_text(
        req,
        "[Context Enishi 聊天环境数据]\n成员资料",
        ChatAwareness.HINT_PREFIX,
        runtime.messages.TextPart,
    )
    parts = [
        runtime.messages.TextPart(text="用户消息").model_dump_for_context(),
        *[part.model_dump_for_context() for part in req.extra_user_content_parts],
    ]
    message = runtime.messages.Message.model_validate(
        {"role": "user", "content": parts}
    )
    assert "成员资料" in str(message.model_dump())
    saved = runtime.messages.dump_messages_with_checkpoints([message])
    assert "成员资料" not in str(saved)
    assert "用户消息" in str(saved)
