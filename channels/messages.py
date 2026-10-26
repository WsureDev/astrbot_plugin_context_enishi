"""Small projections using AstrBotMessage, MessageMember and component field names."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from ..settings import platform_name, scope
from .message_components import (
    component,
    framework_components,
    merge_components,
    native_id,
    qq_components,
    telegram_components,
    user_name,
)
from .native import ImageRef, field, normalize_segments, qq_images, telegram_images


def unix_time(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        value = value.timestamp()
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if math.isfinite(number) and number > 0:
        return number
    return None


def telegram_payload(event):
    raw = field(event.message_obj, "raw_message")
    return field(raw, "message") or field(raw, "effective_message")


def event_meta(event):
    obj = event.message_obj
    group = field(obj, "group")
    metadata = {
        "platform_meta.id": str(event.get_platform_id()),
        "session_id": str(field(obj, "session_id") or scope(event)),
    }
    if event.get_group_id():
        group_id = str(event.get_group_id())
        name = field(group, "group_name")
        if platform_name(event) == "telegram":
            chat = field(telegram_payload(event), "chat")
            group_id = str(field(chat, "id") or group_id.split("#")[0])
            name = field(chat, "title") or name
        metadata["group.group_id"] = group_id
        if name:
            metadata["group.group_name"] = str(name)
    self_id = native_id(field(obj, "self_id"))
    if self_id:
        metadata["self_id"] = self_id
    return metadata


def _record(mid, stamp, uid, name, text, parts):
    return dict(
        message_id=native_id(mid),
        timestamp=unix_time(stamp),
        sender={
            key: value
            for key, value in {"user_id": native_id(uid), "nickname": name}.items()
            if value
        },
        message_str=str(text or "")[:8000],
        message=parts,
    )


def qq_record(event, payload):
    sender = field(payload, "sender", {})
    mid = native_id(field(payload, "message_id"))
    segments = normalize_segments(field(payload, "message", []))
    text = "".join(
        str(field(field(part, "data", {}), "text", ""))
        for part in segments
        if field(part, "type") == "text"
    )
    parts = qq_components(payload)
    parts.extend(
        component("Image", file=image.ref.uri)
        for image in qq_images(segments, event, mid or "")
    )
    return _record(
        mid,
        field(payload, "time"),
        field(sender, "user_id"),
        field(sender, "card") or field(sender, "nickname"),
        text,
        parts,
    )


def telegram_record(event, payload):
    user = field(payload, "from_user") or field(payload, "from", {})
    uid, name = field(user, "id"), user_name(user)
    chat = field(payload, "sender_chat")
    if chat:
        uid, name = None, field(chat, "title")
    parts = telegram_components(payload)
    parts.extend(
        component("Image", file=image.ref.uri)
        for image in telegram_images(payload, event)
    )
    return _record(
        field(payload, "message_id"),
        field(payload, "date"),
        uid,
        name,
        field(payload, "text") or field(payload, "caption"),
        parts,
    )


def event_record(event):
    obj = event.message_obj
    raw = field(obj, "raw_message")
    if platform_name(event) == "telegram":
        payload = telegram_payload(event)
        result = telegram_record(event, payload)
    else:
        result = qq_record(event, raw)
    if str(event.get_platform_name()).lower() == "cron":
        return _record(None, None, None, None, "", [])
    if field(raw, "post_type") == "notice":
        result["message_id"] = None
    elif not result["message_id"]:
        result["message_id"] = native_id(field(obj, "message_id"))
    anonymous = bool(field(telegram_payload(event), "sender_chat"))
    if not anonymous:
        sender = result["sender"]
        sender.setdefault(
            "user_id",
            native_id(field(field(obj, "sender"), "user_id") or event.get_sender_id()),
        )
        name_getter = getattr(event, "get_sender_name", None)
        name = field(field(obj, "sender"), "nickname")
        if not name and callable(name_getter):
            name = name_getter()
        if name:
            sender.setdefault("nickname", str(name))
    if not result["timestamp"]:
        result["timestamp"] = unix_time(field(obj, "timestamp"))
    if not result["message_str"]:
        text = field(obj, "message_str")
        getter = getattr(event, "get_message_str", None)
        if text is None and callable(getter):
            text = getter()
        result["message_str"] = str(text or "")[:8000]
    components = framework_components(field(obj, "message", []))
    if platform_name(event) == "telegram" and telegram_payload(event):
        # Native entities distinguish numeric IDs from @usernames, including
        # anonymous chat senders. Framework conversions can lose that distinction.
        components = [
            part for part in components if part["type"] not in {"At", "Reply"}
        ]
    result["message"] = merge_components(result["message"], components)
    for part in field(obj, "message", []) or []:
        native = field(part, "_channel_native_file_id")
        if native:
            ref = ImageRef(
                "telegram",
                str(event.get_platform_id()),
                scope(event),
                "image",
                str(native),
                str(field(part, "_channel_native_message_id", "")),
            )
            result["message"] = merge_components(
                result["message"], [component("Image", file=ref.uri)]
            )
    return result


def local_record(record):
    content = field(record, "content", {}) or {}
    text, parts = [], []
    for part in field(content, "message", []) or []:
        kind = str(field(part, "type", "")).lower()
        if kind in {"text", "plain"}:
            text.append(str(field(part, "text", "")))
        elif kind == "image":
            parts.append(component("Image"))
    result = _record(
        None,
        field(record, "created_at"),
        field(record, "sender_id"),
        field(record, "sender_name"),
        "".join(text),
        parts,
    )
    result["local_record_id"] = native_id(field(record, "id"))
    return result
