"""Stateless, scoped references to native non-image message segments."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from ..settings import platform_name, scope

CONTENT_TYPES = frozenset(
    {
        "json",
        "xml",
        "miniapp",
        "markdown",
        "music",
        "contact",
        "location",
        "file",
        "record",
        "video",
        "forward",
        "node",
        "onlinefile",
        "flashtransfer",
        "face",
        "mface",
        "poke",
        "dice",
        "rps",
    }
)
CONTENT_TYPE_ALIASES = {"nodes": "node", "forward_msg": "forward"}
CONTENT_READER = "get_channel_message_content"


def content_type(value):
    kind = str(value).lower()
    return CONTENT_TYPE_ALIASES.get(kind, kind)


@dataclass(frozen=True)
class ContentRef:
    account: str
    chat: str
    kind: str
    message_id: str
    index: int = 0

    @property
    def uri(self):
        path = "/".join(
            quote(value, safe="") for value in (self.chat, self.kind, self.message_id)
        )
        return (
            f"qq://{quote(self.account, safe='')}/{path}?{urlencode({'i': self.index})}"
        )

    @classmethod
    def for_message(cls, event, kind, message_id, index):
        return cls(
            str(event.get_platform_id()), scope(event), kind, str(message_id), index
        )

    @classmethod
    def parse(cls, value, event):
        if not isinstance(value, str) or not value:
            raise ValueError("请传入消息组件的完整 id")
        parsed = urlsplit(value)
        parts = [unquote(part) for part in parsed.path.lstrip("/").split("/")]
        if parsed.scheme != "qq" or platform_name(event) != "qq" or len(parts) != 3:
            raise ValueError("此工具需要 QQ 消息组件 id；图片仍使用读图工具")
        if unquote(parsed.netloc) != str(event.get_platform_id()) or parts[0] != scope(
            event
        ):
            raise ValueError("消息组件不属于当前机器人或会话")
        query = parse_qs(parsed.query)
        indexes = query.get("i", [])
        if (
            parts[1] not in CONTENT_TYPES
            or not parts[2]
            or len(indexes) != 1
            or not indexes[0].isdigit()
        ):
            raise ValueError("消息组件定位无效")
        return cls(
            unquote(parsed.netloc), parts[0], parts[1], parts[2], int(indexes[0])
        )
