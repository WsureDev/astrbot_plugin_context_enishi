"""Extract native relationships without inferring people from display text."""

from __future__ import annotations

from ..settings import platform_name
from .content_refs import CONTENT_TYPES, ContentRef, content_type
from .native import field, normalize_segments


def native_id(value):
    if value is None or value == "":
        return None
    return str(value)


def user_name(user):
    return (
        field(user, "full_name")
        or " ".join(
            str(value)
            for value in (field(user, "first_name"), field(user, "last_name"))
            if value
        )
        or field(user, "username")
        or ""
    )


def component(kind, **values):
    return {
        "type": kind,
        **{
            key: value
            for key, value in values.items()
            if value is not None and value != ""
        },
    }


def referenced_component(event, kind, mid, index):
    values = {}
    if event is not None and mid and platform_name(event) == "qq":
        values["id"] = ContentRef.for_message(event, kind, mid, index).uri
    return component(kind.title(), **values)


def framework_components(parts, event=None, message_id=""):
    output = []
    counts = {}
    for part in parts or []:
        value = field(part, "type", "")
        kind = content_type(field(value, "value", value))
        if kind == "reply":
            output.append(
                component(
                    "Reply",
                    id=native_id(field(part, "id")),
                    sender_id=native_id(field(part, "sender_id") or None),
                    sender_nickname=field(part, "sender_nickname"),
                )
            )
        elif kind in {"at", "atall"}:
            output.append(
                component(
                    "At", qq=native_id(field(part, "qq")), name=field(part, "name")
                )
            )
        elif kind in CONTENT_TYPES:
            index = counts.get(kind, 0)
            output.append(referenced_component(event, kind, message_id, index))
            counts[kind] = index + 1
    return output


def qq_components(payload, event=None):
    output = []
    counts = {}
    mid = field(payload, "message_id")
    for part in normalize_segments(field(payload, "message", [])):
        kind, data = (
            content_type(field(part, "type", "")),
            field(part, "data", {}) or {},
        )
        if kind == "reply":
            output.append(
                component(
                    "Reply",
                    id=native_id(field(data, "id")),
                    sender_id=native_id(field(data, "sender_id")),
                    sender_nickname=field(data, "sender_nickname"),
                )
            )
        elif kind == "at":
            output.append(
                component(
                    "At", qq=native_id(field(data, "qq")), name=field(data, "name")
                )
            )
        elif kind in CONTENT_TYPES:
            index = counts.get(kind, 0)
            output.append(referenced_component(event, kind, mid, index))
            counts[kind] = index + 1
    return output


def telegram_components(payload):
    output = []
    reply = field(payload, "reply_to_message")
    if reply:
        user = field(reply, "from_user") or field(reply, "from", {})
        if field(reply, "sender_chat"):
            user = {}
        output.append(
            component(
                "Reply",
                id=native_id(field(reply, "message_id")),
                sender_id=native_id(field(user, "id")),
                sender_nickname=user_name(user),
            )
        )
    for text_key, entities_key in (
        ("text", "entities"),
        ("caption", "caption_entities"),
    ):
        text = str(field(payload, text_key, "") or "")
        for entity in field(payload, entities_key, []) or []:
            value = field(entity, "type", "")
            kind = str(field(value, "value", value))
            if kind not in {"mention", "text_mention"}:
                continue
            user = field(entity, "user", {})
            start = int(field(entity, "offset", 0)) * 2
            end = start + int(field(entity, "length", 0)) * 2
            name = text.encode("utf-16-le")[start:end].decode(
                "utf-16-le", errors="replace"
            )
            output.append(
                component(
                    "At", qq=native_id(field(user, "id")), name=user_name(user) or name
                )
            )
    return output


def merge_components(primary, extra):
    output = [dict(part) for part in primary]
    for part in extra:
        kind = part["type"]
        locator = {"At": "qq", "Image": "file"}.get(kind, "id")
        match = next(
            (
                known
                for known in output
                if known["type"] == kind
                and locator
                and part.get(locator) is not None
                and known.get(locator) == part.get(locator)
            ),
            None,
        )
        if match is None:
            output.append(dict(part))
        else:
            for key, value in part.items():
                if not match.get(key):
                    match[key] = value
    return output
