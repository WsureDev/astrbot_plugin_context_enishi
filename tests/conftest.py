from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_context_manager.platforms import Channels
from astrbot_plugin_context_manager.policy import MediaPolicy
from astrbot_plugin_context_manager.settings import Settings
from astrbot_plugin_context_manager.storage import CaptionCache


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
        self.temporary_files = []
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

    def track_temporary_local_file(self, path):
        self.temporary_files.append(Path(path))

    def cleanup_temporary_local_files(self):
        for path in self.temporary_files:
            path.unlink(missing_ok=True)
        self.temporary_files.clear()


def text_factory(*, text):
    return {"type": "text", "text": text}


@pytest.fixture
def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (7, 5), (30, 60, 90)).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def service(tmp_path, image_bytes):
    config = {
        "channel_settings": [
            {
                "__template_key": "telegram",
                "image_mode": "reference_only",
                "caption_provider_id": "vision",
            },
            {
                "__template_key": "qq",
                "image_mode": "reference_only",
                "caption_provider_id": "vision",
            },
        ]
    }
    settings = Settings(config)
    cache = CaptionCache(tmp_path / "data")
    context = SimpleNamespace(
        get_config=lambda **kw: {},
        llm_generate=AsyncMock(
            return_value=SimpleNamespace(completion_text="蓝色背景，文字为示例")
        ),
    )
    channels = Channels(context, settings, [tmp_path / "temp"])
    policy = MediaPolicy(context, settings, channels, text_factory, cache)
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
    channels.release_transfers()
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
