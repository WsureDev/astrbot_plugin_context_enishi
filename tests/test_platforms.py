from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_context_enishi.channels.media import ChannelError
from astrbot_plugin_context_enishi.channels.native import ImageRef

from .conftest import Event


async def test_qq_fetches_unseen_quote_and_nested_forward(service):
    event = Event(
        "aiocqhttp",
        chat="123",
        raw={"message_id": 2, "message": [{"type": "reply", "data": {"id": "unseen"}}]},
    )

    async def call(action, **params):
        if action == "get_msg":
            return {"message": [{"type": "forward", "data": {"id": "root"}}]}
        if params["message_id"] == "root":
            return {
                "messages": [
                    {
                        "content": [
                            {"type": "image", "data": {"file": "first.jpg"}},
                            {"type": "forward", "data": {"id": "child"}},
                        ]
                    }
                ]
            }
        return {
            "messages": [
                {"content": [{"type": "image", "data": {"file": "second.jpg"}}]}
            ]
        }

    event.bot.call_action.side_effect = call
    images = await service.channels.current_images(event)
    assert [i.ref.native_id for i in images] == ["first.jpg", "second.jpg"]
    assert [i.ref.forward_id for i in images] == ["root", "child"]


async def test_missing_quote_returns_empty_without_null_dereference(service):
    event = Event(
        "aiocqhttp",
        chat="123",
        raw={"message": [{"type": "reply", "data": {"id": "gone"}}]},
    )
    event.bot.call_action.side_effect = RuntimeError("gone")
    assert await service.channels.current_images(event) == []


async def test_qq_retrieval_refreshes_message_before_fetch(service, image_bytes):
    import base64

    event = Event("aiocqhttp", chat="123")
    event.bot.call_action.side_effect = [
        {
            "group_id": "123",
            "message": [{"type": "image", "data": {"file": "native.png"}}],
        },
        {"base64": base64.b64encode(image_bytes).decode()},
    ]
    ref = ImageRef("qq", "bot", "123", "image", "native.png", "unseen")
    path, _ = await service.channels.fetch_image(event, ref.uri)
    assert path.is_file()
    assert event.bot.call_action.call_args_list[0].args == ("get_msg",)
    assert event.bot.call_action.call_args_list[1].args == ("get_image",)


async def test_napcat_container_path_is_not_assumed_shared(service):
    with pytest.raises(ChannelError, match="not shared"):
        await service.channels.bytes_from_source("/app/.config/QQ/image.png")


async def test_qq_user_history_filter_and_cursor(service):
    event = Event("aiocqhttp", chat="123")
    event.bot.call_action.return_value = {
        "messages": [
            {
                "message_id": str(i),
                "message_seq": str(i),
                "time": time.time() - i,
                "sender": {"user_id": "99"},
                "message": [
                    {"type": "text", "data": {"text": str(i)}},
                    {"type": "image", "data": {"file": "native.png"}},
                ],
            }
            for i in range(1, 8)
        ]
    }
    result = await service.chat_sources.user_history(event, "99", 2, 1)
    assert len(result["messages"]) == 2 and result["next_cursor"] == "2"
    assert result["messages"][0]["image_ids"]
    assert not result["complete"]
    params = event.bot.call_action.call_args.kwargs
    assert params["disable_get_url"] and not params["parse_mult_msg"]


async def test_tg_history_is_explicitly_partial_and_never_leaks_token_url(service):
    record = SimpleNamespace(
        id=3,
        sender_id="99",
        created_at=datetime.now(UTC),
        content={
            "message": [
                {"type": "plain", "text": "发言"},
                {
                    "type": "image",
                    "url": "https://api.telegram.org/file/botSECRET/image.jpg",
                },
            ]
        },
    )
    service.context.message_history_manager = SimpleNamespace(
        get=AsyncMock(return_value=[record])
    )
    result = await service.chat_sources.user_history(service.event, "99", 20, 1)
    assert result["source"] == "astrbot_local" and not result["complete"]
    assert "SECRET" not in str(result)
    assert result["messages"][0]["local_record_id"] == "3"


async def test_tg_album_native_ids_are_preserved_without_index(service):
    event = Event(
        raw={
            "message": {
                "message_id": 1,
                "photo": [{"file_id": "album1"}],
                "reply_to_message": {"message_id": 0, "photo": [{"file_id": "quoted"}]},
            }
        },
        components=[
            SimpleNamespace(
                _channel_native_file_id="album1", _channel_native_message_id="1"
            ),
            SimpleNamespace(
                _channel_native_file_id="album2", _channel_native_message_id="2"
            ),
        ],
    )
    sources = await service.channels.current_images(event)
    assert [s.ref.native_id for s in sources] == ["album1", "album2", "quoted"]


async def test_tg_avatar_returns_downloadable_file_id(service):
    service.event.client.get_user_profile_photos.return_value = SimpleNamespace(
        photos=[[SimpleNamespace(file_id="avatar")]]
    )
    ref = await service.chat_sources.user_avatar(service.event, "99")
    assert ref.native_id == "avatar"


async def test_tg_hidden_avatar_is_a_capability_error(service):
    service.event.client.get_user_profile_photos.return_value = SimpleNamespace(
        photos=[]
    )
    with pytest.raises(ChannelError, match="no accessible"):
        await service.chat_sources.user_avatar(service.event, "99")


async def test_tg_native_message_reads_unseen_reply_payload(service):
    result = await service.chat_sources.message(service.event, "1")
    assert result["source"] == "telegram_payload"
    assert "native_unseen_reply" in result["image_ids"][0]


async def test_tg_arbitrary_message_does_not_claim_bot_api_history(service):
    with pytest.raises(ChannelError, match="cannot fetch arbitrary"):
        await service.chat_sources.message(service.event, "not-in-payload")


async def test_cross_chat_message_does_not_fall_back_to_file_fetch(service):
    event = Event("aiocqhttp", chat="123")
    event.bot.call_action.return_value = {"group_id": "456", "message": []}
    ref = ImageRef("qq", "bot", "123", "image", "native.jpg", "other-chat")
    with pytest.raises(ChannelError):
        await service.channels.fetch_image(event, ref.uri)
    assert event.bot.call_action.await_count == 1


async def test_nested_forward_node_data_message_retrieval(service):
    event = Event(
        "aiocqhttp",
        chat="123",
        raw={"message_id": 2, "message": [{"type": "reply", "data": {"id": "unseen"}}]},
    )
    event.bot.call_action.side_effect = [
        {"message": [{"type": "forward", "data": {"id": "root"}}]},
        {
            "messages": [
                {
                    "type": "node",
                    "data": {
                        "message": [
                            {
                                "type": "image",
                                "data": {
                                    "file": "native.jpg",
                                    "url": "https://gchat.qpic.cn/native",
                                },
                            }
                        ]
                    },
                }
            ]
        },
    ]
    images = await service.channels.current_images(event)
    assert images[0].ref.native_id == "native.jpg"
    event.bot.call_action.side_effect = None
    event.bot.call_action.return_value = {
        "messages": [
            {
                "type": "node",
                "data": {
                    "message": [
                        {
                            "type": "image",
                            "data": {
                                "file": "native.jpg",
                                "url": "https://gchat.qpic.cn/native",
                            },
                        }
                    ]
                },
            }
        ]
    }
    assert (
        await service.channels._qq_source(event, images[0].ref)
        == "https://gchat.qpic.cn/native"
    )
