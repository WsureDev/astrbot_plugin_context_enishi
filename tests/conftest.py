from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_channel_media_tools.platforms import Channels
from astrbot_plugin_channel_media_tools.policy import MediaPolicy
from astrbot_plugin_channel_media_tools.settings import Settings
from astrbot_plugin_channel_media_tools.storage import Cache


class Event:
    def __init__(
        self,
        platform="telegram",
        account="bot",
        chat="-100#2",
        raw=None,
        components=None,
    ):
        self.platform, self.account, self.chat = platform, account, chat
        self.message_obj = SimpleNamespace(
            raw_message=raw, message=components or [], message_id="100"
        )
        self.unified_msg_origin = f"{account}:GroupMessage:{chat}"
        self.plugins_name = None
        self.extra = {}
        self.client = SimpleNamespace(
            get_file=AsyncMock(), get_user_profile_photos=AsyncMock()
        )
        self.bot = SimpleNamespace(call_action=AsyncMock())

    def get_platform_name(self):
        return self.platform

    def get_platform_id(self):
        return self.account

    def get_group_id(self):
        return self.chat

    def get_sender_id(self):
        return "99"

    def get_extra(self, key, default=None):
        return self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value


def text_factory(*, text):
    return {"type": "text", "text": text}


@pytest.fixture
def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (7, 5), (30, 60, 90)).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def service(tmp_path, image_bytes):
    config = {"default_mode": "A", "caption_provider_id": "vision", "channels": "{}"}
    settings = Settings(config)
    cache = Cache(tmp_path / "data", tmp_path / "temp", 3600, 1024 * 1024)
    context = SimpleNamespace(
        get_config=lambda **kw: {},
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(completion_text="蓝色背景，文字为示例")
        ),
    )
    channels = Channels(context, settings, cache, [tmp_path / "temp"])
    policy = MediaPolicy(context, settings, channels, text_factory)
    event = Event(
        raw={
            "message": {
                "message_id": 100,
                "photo": [{"file_id": "native_current"}],
                "reply_to_message": {
                    "message_id": 1,
                    "photo": [{"file_id": "native_unseen_reply"}],
                },
            }
        }
    )
    media = SimpleNamespace(
        file_size=len(image_bytes),
        download_as_bytearray=AsyncMock(return_value=bytearray(image_bytes)),
    )
    event.client.get_file.return_value = media
    result = SimpleNamespace(
        config=config,
        settings=settings,
        cache=cache,
        context=context,
        channels=channels,
        policy=policy,
        event=event,
        media=media,
        tmp=tmp_path,
    )
    yield result
    cache.close()


def request(images=None):
    return SimpleNamespace(
        image_urls=images or [], extra_user_content_parts=[], system_prompt="persona"
    )


def runner(event, req, messages):
    return SimpleNamespace(
        req=req,
        run_context=SimpleNamespace(
            context=SimpleNamespace(event=event), messages=messages
        ),
    )


def picture(url, native_id=None):
    return {"type": "image_url", "image_url": {"url": url, "id": native_id}}


def image_count(messages):
    return sum(
        1
        for m in messages
        for p in (m.get("content", []) if isinstance(m, dict) else m.content)
        if isinstance(p, dict) and p.get("type") == "image_url"
    )
