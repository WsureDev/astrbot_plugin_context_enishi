"""Version 1 message projections shared by context and awareness domains."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from ..settings import platform_name
from .native import ImageRef, field, normalize_segments, qq_images, telegram_images
from .relations import (
    component_relations,
    identifier,
    merge_reply,
    qq_relations,
    telegram_name,
    telegram_relations,
)


def timestamp(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        value = value.timestamp()
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def telegram_payload(event):
    raw = field(event.message_obj, "raw_message")
    return field(raw, "message") or field(raw, "effective_message")


def message_scope(event):
    raw = field(event.message_obj, "raw_message")
    group = field(event.message_obj, "group")
    group_id = identifier(event.get_group_id())
    group_name = field(group, "group_name") or field(raw, "group_name") or None
    if platform_name(event) == "telegram":
        chat = field(telegram_payload(event), "chat")
        if group_id:
            group_id = identifier(field(chat, "id")) or group_id.split("#")[0]
        group_name = field(chat, "title") or group_name
    return dict(
        platform_id=str(event.get_platform_id()),
        platform_name=str(event.get_platform_name()),
        scope_key=str(event.unified_msg_origin),
        group_id=group_id,
        group_name=group_name,
        self_id=identifier(field(event.message_obj, "self_id")),
    )


def message_projection(event, kind="query"):
    return dict(
        schema_version=1,
        kind=kind,
        **message_scope(event),
        message_id=None,
        sender_id=None,
        sender_name=None,
        actor_type="unknown",
        timestamp=None,
        time_basis="unknown",
        reply_to=None,
        mentions=[],
        relations_complete=False,
        image_ids=[],
    )


def actor_type(sender_id, self_id, is_bot=False):
    if is_bot or sender_id is not None and sender_id == self_id:
        return "bot"
    if sender_id is not None:
        return "user"
    return "unknown"


def qq_message(event, payload):
    result = message_projection(event)
    sender = field(payload, "sender", {})
    mid = identifier(field(payload, "message_id"))
    uid = identifier(field(sender, "user_id"))
    stamp = timestamp(field(payload, "time"))
    reply, mentions, complete = qq_relations(payload)
    parts = normalize_segments(field(payload, "message", []))
    text = "".join(
        str(field(field(part, "data", {}), "text", ""))
        for part in parts
        if field(part, "type") == "text"
    )
    result.update(
        source="napcat",
        message_id=mid,
        sender_id=uid,
        sender_name=field(sender, "card") or field(sender, "nickname") or None,
        actor_type=actor_type(uid, result["self_id"]),
        timestamp=stamp,
        time_basis="platform_message" if stamp else "unknown",
        reply_to=reply,
        mentions=mentions,
        relations_complete=complete,
        text=text[:8000],
        image_ids=[image.ref.uri for image in qq_images(parts, event, mid or "")],
    )
    return result


def telegram_message(event, payload):
    result = message_projection(event)
    user = field(payload, "from_user") or field(payload, "from", {})
    uid = identifier(field(user, "id"))
    name = telegram_name(user)
    actor = actor_type(uid, result["self_id"], field(user, "is_bot", False))
    if field(payload, "sender_chat"):
        uid, name, actor = None, field(field(payload, "sender_chat"), "title"), "chat"
    reply, mentions, complete = telegram_relations(payload)
    stamp = timestamp(field(payload, "date"))
    result.update(
        source="telegram_payload",
        message_id=identifier(field(payload, "message_id")),
        sender_id=uid,
        sender_name=name,
        actor_type=actor,
        timestamp=stamp,
        time_basis="platform_message" if stamp else "unknown",
        reply_to=reply,
        mentions=mentions,
        relations_complete=complete,
        text=str(field(payload, "text") or field(payload, "caption") or "")[:8000],
        image_ids=[image.ref.uri for image in telegram_images(payload, event)],
    )
    return result


def current_message_metadata(event):
    """Build identity without network calls, temporary paths or duplicated body text."""
    if str(event.get_platform_name()).lower() == "cron":
        return dict(message_projection(event, "current"), actor_type="system")
    raw = field(event.message_obj, "raw_message")
    if platform_name(event) == "telegram":
        result = telegram_message(event, telegram_payload(event))
    else:
        result = qq_message(event, raw)
    result.pop("text", None)
    result["kind"] = "current"
    obj = event.message_obj
    # Only message events can use the framework ID as a native locator. QQ
    # notifications may have synthetic IDs; never expose those as message IDs.
    if result["message_id"] is None and (
        platform_name(event) == "telegram"
        and telegram_payload(event)
        or field(raw, "post_type") == "message"
    ):
        result["message_id"] = identifier(field(obj, "message_id"))
    if result["actor_type"] != "chat":
        result["sender_id"] = result["sender_id"] or identifier(
            field(field(obj, "sender"), "user_id") or event.get_sender_id()
        )
        getter = getattr(event, "get_sender_name", None)
        result["sender_name"] = (
            result["sender_name"]
            or field(field(obj, "sender"), "nickname")
            or (getter() if callable(getter) else None)
        )
        result["actor_type"] = actor_type(
            result["sender_id"], result["self_id"], result["actor_type"] == "bot"
        )
    received = timestamp(field(obj, "timestamp"))
    if received:
        result["received_at"] = received
        if result["timestamp"] is None:
            result.update(timestamp=received, time_basis="framework_received")
    components = field(obj, "message", []) or []
    reply, mentions = component_relations(components)
    if platform_name(event) == "telegram" and field(
        field(telegram_payload(event), "reply_to_message"), "sender_chat"
    ):
        # A framework Reply can carry Telegram's synthetic anonymous bot user.
        # Keep the raw chat author distinct from a real quoted user account.
        reply = None
    result["reply_to"] = merge_reply(result["reply_to"], reply)
    if platform_name(event) == "qq":
        for mention in mentions:
            matching = next(
                (
                    known
                    for known in result["mentions"]
                    if known["user_id"] == mention["user_id"]
                    and known["is_all"] == mention["is_all"]
                ),
                None,
            )
            if matching is None:
                result["mentions"].append(mention)
            elif matching["display_name"] is None:
                matching["display_name"] = mention["display_name"]
    if not result["relations_complete"] and components and not telegram_payload(event):
        result["mentions"] = mentions
        result["relations_complete"] = True
    if platform_name(event) == "telegram":
        for component in components:
            native_id = field(component, "_channel_native_file_id")
            mid = field(component, "_channel_native_message_id", "")
            if native_id:
                ref = ImageRef(
                    "telegram",
                    str(event.get_platform_id()),
                    str(event.get_group_id() or event.get_sender_id()),
                    "image",
                    str(native_id),
                    str(mid),
                )
                result["image_ids"].append(ref.uri)
        result["image_ids"] = list(dict.fromkeys(result["image_ids"]))
    return result
