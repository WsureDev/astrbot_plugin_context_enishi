from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_context_enishi.awareness.sources import ChatSources
from astrbot_plugin_context_enishi.channels.messages import (
    event_meta,
    event_record,
    qq_record,
)
from astrbot_plugin_context_enishi.channels.meta import table
from astrbot_plugin_context_enishi.channels.native import normalize_segments
from astrbot_plugin_context_enishi.channels.transport import ChannelError
from astrbot_plugin_context_enishi.context.service import ContextOrganizer
from astrbot_plugin_context_enishi.settings import Settings

from .conftest import request, text_factory
from .flow_support import flow_request, flow_service, group_event

CARD = {
    "app": "com.tencent.miniapp_01",
    "view": "view_8C8E89B49BE609866298ADDFF2DBABA4",
    "meta": {
        "detail_1": {"qqdocurl": "https://b23.tv/example?a=1&b=2", "desc": "卡片标题"}
    },
}


def rich_event(kind="json", data=None, mid="42"):
    event = group_event(mid, "")
    if data is None:
        data = {"data": json.dumps(CARD, ensure_ascii=False)}
    event.message_obj.raw_message["group_id"] = event.get_group_id()
    event.message_obj.raw_message["message"] = [{"type": kind, "data": data}]
    return event


def sources(event):
    source = ChatSources(None, Settings({}))
    event.bot.call_action = AsyncMock(return_value=event.message_obj.raw_message)
    return source


@pytest.mark.parametrize(
    "kind",
    [
        "json",
        "miniapp",
        "xml",
        "markdown",
        "music",
        "contact",
        "location",
        "file",
        "record",
        "video",
        "forward",
        "onlinefile",
        "flashtransfer",
        "mface",
    ],
)
def test_nontext_segments_keep_only_type_and_scoped_locator(kind):
    event = rich_event(kind)
    record = event_record(event)
    assert record["message"] == [
        {"type": kind.title(), "id": f"qq://bot/123/{kind}/42?i=0"}
    ]
    rendered = table([record], event_meta(event))
    assert "com.tencent" not in rendered and "b23.tv" not in rendered
    assert kind.title() + '(id="qq://bot/123/' in rendered


def test_multiple_cards_use_type_ordinals_and_framework_does_not_duplicate_them():
    event = rich_event()
    first = event.message_obj.raw_message["message"][0]
    event.message_obj.raw_message["message"] += [
        {"type": "text", "data": {"text": "看看这两张卡片"}},
        first,
    ]
    event.message_obj.message = [
        {"type": "Json", "data": CARD},
        {"type": "Plain", "text": "看看这两张卡片"},
        {"type": "Json", "data": CARD},
    ]
    record = event_record(event)
    assert len(record["message"]) == 2
    assert record["message_str"] == "看看这两张卡片"
    assert [part["id"].split("?i=")[1] for part in record["message"]] == ["0", "1"]


def test_framework_conversion_does_not_invent_unreadable_segment_types():
    event = rich_event("forward", {"id": "native-forward"})
    event.message_obj.message = [{"type": "Nodes", "nodes": []}]
    assert event_record(event)["message"] == [
        {"type": "Forward", "id": "qq://bot/123/forward/42?i=0"}
    ]


async def test_card_reader_preserves_exact_original_and_exposes_links_without_network_fetch():
    event = rich_event()
    original = event.message_obj.raw_message["message"][0]["data"]["data"]
    source = sources(event)
    reference = event_record(event)["message"][0]["id"]
    result = await source.message_content(event, reference)
    assert result["data"]["data"] == original
    assert result["features"]["kind"] == "miniapp"
    assert result["features"]["links"] == [
        {"field": "meta.detail_1.qqdocurl", "url": CARD["meta"]["detail_1"]["qqdocurl"]}
    ]
    event.bot.call_action.assert_awaited_once_with("get_msg", message_id="42")
    event.bot.call_action.return_value = {"status": "failed"}
    with pytest.raises(ChannelError):
        await source.message_content(event, reference)


@pytest.mark.parametrize("data", [{"data": CARD}, {"data": "{malformed json"}])
async def test_object_or_malformed_json_is_returned_without_rewriting(data):
    event = rich_event(data=data)
    source = sources(event)
    result = await source.message_content(
        event, event_record(event)["message"][0]["id"]
    )
    assert result["data"] == data


async def test_xml_markdown_and_unknown_card_fields_remain_complete():
    for kind, data in [
        (
            "xml",
            {
                "data": '<msg url="https://example.test/?a=1&amp;b=2"><item>原文</item></msg>'
            },
        ),
        (
            "markdown",
            {"content": "[链接](https://example.test/)\n" + "保留正文" * 10000},
        ),
        (
            "json",
            {
                "data": json.dumps(
                    {
                        "app": "com.tencent.structmsg",
                        "meta": {"news": {"jumpUrl": "https://example.test/"}},
                        "unknown_field": "完整" * 10000,
                    }
                )
            },
        ),
    ]:
        event = rich_event(kind, data)
        source = sources(event)
        result = await source.message_content(
            event, event_record(event)["message"][0]["id"]
        )
        assert result["data"] == data
        assert result["features"]["links"]


async def test_mixed_type_ordinals_select_requested_second_card():
    event = rich_event()
    event.message_obj.raw_message["message"] += [
        {"type": "text", "data": {"text": "https://plain.test"}},
        {"type": "xml", "data": {"data": "<msg/>"}},
        {"type": "json", "data": {"data": '{"second":true}'}},
    ]
    source = sources(event)
    result = await source.message_content(event, "qq://bot/123/json/42?i=1")
    assert result["data"]["data"] == '{"second":true}'
    assert (
        "https://plain.test"
        in qq_record(event, event.message_obj.raw_message)["message_str"]
    )
    with pytest.raises(ChannelError, match="没有这个类型或序号"):
        await source.message_content(event, "qq://bot/123/json/42?i=2")


async def test_forward_reader_uses_native_forward_id_after_verifying_parent_message():
    event = rich_event("forward", {"id": "forward-native"})
    source = sources(event)
    forwarded = {
        "messages": [
            {
                "message_id": 99,
                "message": [{"type": "json", "data": {"data": json.dumps(CARD)}}],
            }
        ]
    }
    event.bot.call_action.side_effect = [event.message_obj.raw_message, forwarded]
    result = await source.message_content(
        event, event_record(event)["message"][0]["id"]
    )
    assert result["forward"] == forwarded
    assert event.bot.call_action.await_args_list[1].args == ("get_forward_msg",)
    assert event.bot.call_action.await_args_list[1].kwargs == {
        "message_id": "forward-native"
    }


@pytest.mark.parametrize(
    "reference",
    [
        "qq://other/123/json/42?i=0",
        "qq://bot/other/json/42?i=0",
        "qq://bot/123/image/native?m=42&i=0",
        "qq://bot/123/json/42?i=-1",
    ],
)
async def test_foreign_invalid_and_image_ids_do_not_query_napcat(reference):
    event = rich_event()
    source = sources(event)
    with pytest.raises(ValueError):
        await source.message_content(event, reference)
    event.bot.call_action.assert_not_called()


async def test_backend_scope_or_message_id_mismatch_is_not_accepted():
    event = rich_event()
    source = sources(event)
    for changes in ({"group_id": "other"}, {"message_id": "999"}):
        event.bot.call_action.return_value = dict(
            event.message_obj.raw_message, **changes
        )
        with pytest.raises(ChannelError):
            await source.message_content(event, "qq://bot/123/json/42?i=0")


def test_cq_encoded_json_uses_same_locator_without_copying_payload_into_message_str():
    event = rich_event()
    raw = (
        json.dumps(CARD)
        .replace("&", "&amp;")
        .replace(",", "&#44;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
    )
    event.message_obj.raw_message["message"] = "[CQ:json,data=" + raw + "]"
    event.message_obj.message_str = event.message_obj.raw_message["message"]
    record = event_record(event)
    assert record["message_str"] == ""
    assert record["message"][0]["id"] == "qq://bot/123/json/42?i=0"
    assert (
        json.loads(
            normalize_segments(event.message_obj.raw_message["message"])[0]["data"][
                "data"
            ]
        )
        == CARD
    )


async def test_compact_current_reference_is_present_when_image_and_flow_are_disabled():
    event = rich_event()
    organizer = ContextOrganizer(None, Settings({}), lambda event: True)
    organizer.text_factory = text_factory
    req = request()
    await organizer.prepare_request(event, req)
    await organizer.prepare_request(event, req)
    assert len(req.extra_user_content_parts) == 1
    assert "qq://bot/123/json/42?i=0" in req.extra_user_content_parts[0]["text"]
    assert "get_channel_message_content" in req.system_prompt
    assert "com.tencent" not in str(req.extra_user_content_parts)


async def test_flow_records_and_injects_only_compact_card_reference(tmp_path):
    service = flow_service(tmp_path)
    try:
        await service.flow.record(rich_event())
        req = flow_request()
        await service.flow.prepare(group_event("43", "提取卡片链接"), req, text_factory)
        assert "qq://bot/123/json/42?i=0" in str(req.contexts)
        assert "com.tencent" not in str(req.contexts)
        assert "get_channel_message_content" in req.system_prompt
    finally:
        await service.flow.aclose()
