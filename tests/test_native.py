from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrbot_plugin_context_enishi.channels.native import (
    ImageRef,
    qq_images,
    telegram_images,
)
from astrbot_plugin_context_enishi.settings import Settings

from .conftest import Event


def test_unseen_reply_is_self_contained():
    event = Event()
    payload = {
        "message_id": 1,
        "photo": [{"file_id": "small"}, {"file_id": "native_unseen"}],
    }
    source = telegram_images(payload, event, "reply")[0]
    decoded = ImageRef.parse(source.ref.uri, Event())
    assert decoded.native_id == "native_unseen"
    assert decoded.message_id == "1"
    assert "-100%232" in source.ref.uri


def test_bare_native_id_needs_no_registry():
    assert ImageRef.parse("native_file_id", Event()).native_id == "native_file_id"


@pytest.mark.parametrize(
    "change", [{"account": "other"}, {"chat": "-200"}, {"platform": "aiocqhttp"}]
)
def test_cross_channel_reference_is_rejected(change):
    source = ImageRef("telegram", "bot", "-100#2", "image", "native").uri
    with pytest.raises(ValueError, match="another channel"):
        ImageRef.parse(source, Event(**change))


@pytest.mark.parametrize(
    "value",
    [
        "/etc/passwd",
        "../image.png",
        "data:image/png;base64,aaa",
        "file:///tmp/test.png",
    ],
)
def test_paths_are_not_native_ids(value):
    with pytest.raises(ValueError):
        ImageRef.parse(value, Event())


def test_qq_message_locator_when_segment_has_no_file():
    event = Event("aiocqhttp", chat="123")
    images = qq_images(
        [
            {"type": "image", "data": {"url": "https://example/a"}},
            {"type": "image", "data": {"file": "native-md5.jpg"}},
        ],
        event,
        "999",
    )
    assert images[0].ref.kind == "message"
    assert images[0].ref.native_id == "999"
    assert images[1].ref.native_id == "native-md5.jpg"
    assert images[1].ref.index == 1


def test_nested_forward_keeps_native_locator():
    event = Event("aiocqhttp", chat="123")
    images = qq_images(
        [
            {
                "type": "node",
                "data": {"content": [{"type": "image", "data": {"file": "md5.png"}}]},
            }
        ],
        event,
        "9",
        "reply_forward",
        "resid",
        "0",
    )
    ref = ImageRef.parse(images[0].ref.uri, event)
    assert ref.forward_id == "resid" and ref.node == "0.0"


def test_document_and_animated_sticker_use_downloadable_ids():
    event = Event()
    source = telegram_images(
        SimpleNamespace(
            message_id=2,
            photo=[],
            document=SimpleNamespace(mime_type="image/png", file_id="document"),
            sticker=SimpleNamespace(
                is_animated=True, thumbnail=SimpleNamespace(file_id="thumb")
            ),
        ),
        event,
    )
    assert [s.ref.native_id for s in source] == ["document", "thumb"]


def test_channel_override_precedence():
    event = Event()
    settings = Settings(
        {
            "channel_settings": [
                {
                    "__template_key": "telegram",
                    "image_mode": "reference_only",
                    "caption_provider_id": "tg",
                },
                {
                    "__template_key": "telegram",
                    "image_mode": "current_turn",
                    "scope": {"platform_id": "bot"},
                },
                {
                    "__template_key": "telegram",
                    "image_mode": "disabled",
                    "caption_provider_id": "tg",
                    "scope": {"platform_id": "bot", "chat_id": "-100#2"},
                },
            ]
        }
    )
    assert settings.for_event(event).mode == "disabled"
    assert settings.for_event(event).caption_provider_id == "tg"


def test_background_event_inherits_real_platform_configuration():
    event = Event(platform="cron")
    event.context_obj = SimpleNamespace(
        get_platform_inst=lambda p: SimpleNamespace(config={"type": "telegram"})
    )
    assert (
        Settings(
            {
                "channel_settings": [
                    {"__template_key": "telegram", "image_mode": "reference_only"}
                ]
            }
        )
        .for_event(event)
        .mode
        == "reference_only"
    )


def test_qq_cq_string_native_id_is_parsed():
    event = Event("aiocqhttp", chat="123")
    images = qq_images(
        "图片[CQ:image,file=native.jpg,url=https://gchat.qpic.cn/a?x=1&amp;y=2]",
        event,
        "9",
    )
    assert images[0].ref.native_id == "native.jpg"
    assert "&y=2" in images[0].url


def test_forward_node_data_message_shape_matches_napcat():
    event = Event("aiocqhttp", chat="123")
    images = qq_images(
        [
            {
                "type": "node",
                "data": {
                    "message": [{"type": "image", "data": {"file": "native.jpg"}}]
                },
            }
        ],
        event,
        "9",
    )
    assert images[0].ref.native_id == "native.jpg"
