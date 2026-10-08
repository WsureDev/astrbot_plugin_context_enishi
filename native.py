from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from .settings import platform_name, scope


def field(value: Any, name: str, default: Any = None) -> Any:
    return (
        value.get(name, default)
        if isinstance(value, dict)
        else getattr(value, name, default)
    )


@dataclass(frozen=True)
class ImageRef:
    platform: str
    account: str
    chat: str
    kind: str
    native_id: str
    message_id: str = ""
    index: int = 0
    forward_id: str = ""
    node: str = ""

    @property
    def uri(self) -> str:
        query = {"m": self.message_id, "i": str(self.index)}
        if self.forward_id:
            query.update(f=self.forward_id, n=self.node)
        path = "/".join(
            quote(v, safe="") for v in (self.chat, self.kind, self.native_id)
        )
        return f"{self.platform}://{quote(self.account, safe='')}/{path}?{urlencode(query)}"

    @classmethod
    def parse(cls, value: str, event: Any) -> ImageRef:
        platform = platform_name(event)
        account = str(event.get_platform_id())
        chat = scope(event)
        if not isinstance(value, str) or not value.strip() or len(value) > 8192:
            raise ValueError("invalid native image ID")
        value = value.strip()
        if "://" not in value:
            if (
                "/" in value
                or "\\" in value
                or value.startswith(("data:", "base64:", "."))
            ):
                raise ValueError("expected a platform-native file ID")
            return cls(platform, account, chat, "image", value)
        parsed = urlsplit(value)
        parts = [unquote(p) for p in parsed.path.lstrip("/").split("/")]
        if len(parts) != 3:
            raise ValueError("invalid native image URI")
        if (
            parsed.scheme != platform
            or unquote(parsed.netloc) != account
            or parts[0] != chat
        ):
            raise ValueError("image belongs to another channel")
        query = parse_qs(parsed.query)
        index = int(query.get("i", ["0"])[0])
        if index < 0 or index > 10000 or parts[1] not in {"image", "message", "avatar"}:
            raise ValueError("invalid native image locator")
        if not parts[2] or (
            parts[1] != "message" and ("/" in parts[2] or "\\" in parts[2])
        ):
            raise ValueError("invalid native file ID")
        return cls(
            platform,
            account,
            chat,
            parts[1],
            parts[2],
            query.get("m", [""])[0],
            index,
            query.get("f", [""])[0],
            query.get("n", [""])[0],
        )


@dataclass
class SourceImage:
    ref: ImageRef
    url: str = ""
    origin: str = "current"

    @property
    def marker(self) -> str:
        return f"[图片 id={self.ref.uri} 来源={self.origin}]"


def telegram_images(
    message: Any, event: Any, origin: str = "current"
) -> list[SourceImage]:
    """Read native file IDs from a payload, including previously unseen replies."""
    images: list[SourceImage] = []
    photo = field(message, "photo") or []
    candidates = [photo[-1]] if photo else []
    document = field(message, "document")
    if document and str(field(document, "mime_type", "")).startswith("image/"):
        candidates.append(document)
    sticker = field(message, "sticker")
    if sticker:
        if field(sticker, "is_animated", False) or field(sticker, "is_video", False):
            sticker = field(sticker, "thumbnail")
        if sticker:
            candidates.append(sticker)
    for index, media in enumerate(candidates):
        native_id = str(field(media, "file_id", "") or "")
        if native_id:
            images.append(
                SourceImage(
                    ImageRef(
                        "telegram",
                        str(event.get_platform_id()),
                        scope(event),
                        "image",
                        native_id,
                        str(field(message, "message_id", "") or ""),
                        index,
                    ),
                    origin=origin,
                )
            )
    return images


def qq_images(
    segments: Any,
    event: Any,
    message_id: str = "",
    origin: str = "current",
    forward_id: str = "",
    node: str = "",
    depth: int = 0,
    max_depth: int = 6,
) -> list[SourceImage]:
    images: list[SourceImage] = []
    if depth > max_depth:
        return images
    segments = normalize_segments(segments)
    image_index = 0
    for position, segment in enumerate(segments):
        data = field(segment, "data", {}) or {}
        kind = str(field(segment, "type", "")).lower()
        if kind == "image":
            native_id = str(field(data, "file_id", "") or field(data, "file", "") or "")
            if (
                native_id.startswith(("http:", "https:", "file:", "base64:", "data:"))
                or "/" in native_id
                or "\\" in native_id
            ):
                native_id = ""
            ref_kind = "image" if native_id else "message"
            native_id = native_id or message_id or forward_id
            if native_id:
                ref = ImageRef(
                    "qq",
                    str(event.get_platform_id()),
                    scope(event),
                    ref_kind,
                    native_id,
                    message_id,
                    image_index,
                    forward_id,
                    node,
                )
                images.append(
                    SourceImage(ref, str(field(data, "url", "") or ""), origin)
                )
            image_index += 1
        elif kind == "node":
            child = node_content(segment)
            path = f"{node}.{position}" if node else str(position)
            images.extend(
                qq_images(
                    child,
                    event,
                    message_id,
                    origin,
                    forward_id,
                    path,
                    depth + 1,
                    max_depth,
                )
            )
        elif kind in {"forward", "forward_msg", "nodes"}:
            nested_id = str(
                field(data, "id", "") or field(data, "message_id", "") or forward_id
            )
            for position, nested in enumerate(
                forward_nodes(field(data, "content", []))
            ):
                images.extend(
                    qq_images(
                        node_content(nested),
                        event,
                        message_id,
                        origin,
                        nested_id,
                        str(position),
                        depth + 1,
                        max_depth,
                    )
                )
    return images


def forward_nodes(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("messages") or payload.get("message") or []
    return []


def normalize_segments(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
        if isinstance(decoded, list):
            return decoded
    except (ValueError, TypeError):
        pass
    result, cursor = [], 0
    for match in re.finditer(r"\[CQ:([^,\]]+)((?:,[^\]]*)?)\]", value):
        if match.start() > cursor:
            result.append(
                {"type": "text", "data": {"text": value[cursor : match.start()]}}
            )
        data = {}
        for pair in match.group(2).lstrip(",").split(","):
            key, separator, item = pair.partition("=")
            if separator:
                data[key] = html.unescape(item)
        result.append({"type": match.group(1), "data": data})
        cursor = match.end()
    if cursor < len(value):
        result.append({"type": "text", "data": {"text": value[cursor:]}})
    return result


def node_content(node: Any) -> list[Any]:
    data = field(node, "data", {}) or {}
    return normalize_segments(
        field(node, "message")
        or field(node, "content")
        or field(data, "message")
        or field(data, "content")
        or []
    )


def forward_ids(segments: Any, depth: int = 0, max_depth: int = 6) -> list[str]:
    if depth > max_depth:
        return []
    ids = []
    for segment in normalize_segments(segments):
        kind = field(segment, "type")
        data = field(segment, "data", {}) or {}
        if kind in {"forward", "forward_msg", "nodes"}:
            native_id = field(data, "id") or field(data, "message_id")
            if native_id:
                ids.append(str(native_id))
            else:
                for node in forward_nodes(field(data, "content", [])):
                    ids.extend(forward_ids(node_content(node), depth + 1, max_depth))
        elif kind == "node":
            ids.extend(forward_ids(node_content(segment), depth + 1, max_depth))
    return list(dict.fromkeys(ids))
