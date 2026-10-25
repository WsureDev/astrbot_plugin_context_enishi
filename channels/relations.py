"""Read only explicit reply and mention relationships from platform structures."""

from __future__ import annotations

from .native import field, normalize_segments


def identifier(value):
    if value is None or value == "":
        return None
    return str(value)


def telegram_name(user):
    return (
        field(user, "full_name")
        or " ".join(filter(None, (field(user, "first_name"), field(user, "last_name"))))
        or field(user, "username")
        or None
    )


def reply_reference(message_id=None, sender_id=None, sender_name=None):
    return dict(
        message_id=identifier(message_id),
        sender_id=identifier(sender_id),
        sender_name=sender_name or None,
    )


def component_relations(components):
    reply, mentions = None, []
    for component in components:
        component_type = field(component, "type", "")
        kind = str(field(component_type, "value", component_type)).lower()
        if kind == "reply":
            reply = reply_reference(
                field(component, "id"),
                field(component, "sender_id") or None,
                field(component, "sender_nickname"),
            )
        elif kind in {"at", "atall"}:
            uid = identifier(field(component, "qq"))
            is_all = kind == "atall" or uid == "all"
            mentions.append(
                dict(
                    user_id=None if is_all else uid,
                    display_name=field(component, "name") or None,
                    is_all=is_all,
                )
            )
    return reply, mentions


def qq_relations(payload):
    segments = field(payload, "message")
    reply, mentions = None, []
    for segment in normalize_segments(segments):
        data = field(segment, "data", {}) or {}
        kind = field(segment, "type")
        if kind == "reply":
            reply = reply_reference(
                field(data, "id"), field(data, "sender_id"), field(data, "sender_name")
            )
        elif kind == "at":
            uid = identifier(field(data, "qq"))
            mentions.append(
                dict(
                    user_id=None if uid == "all" else uid,
                    display_name=field(data, "name") or None,
                    is_all=uid == "all",
                )
            )
    return reply, mentions, isinstance(segments, (list, str))


def telegram_relations(message):
    quoted = field(message, "reply_to_message")
    reply = None
    if quoted:
        user = field(quoted, "from_user") or field(quoted, "from", {})
        if field(quoted, "sender_chat"):
            user = {}
        reply = reply_reference(
            field(quoted, "message_id"), field(user, "id"), telegram_name(user)
        )
    mentions = []
    for text_key, entities_key in (
        ("text", "entities"),
        ("caption", "caption_entities"),
    ):
        text = str(field(message, text_key, "") or "")
        for entity in field(message, entities_key, []) or []:
            entity_type = field(entity, "type", "")
            kind = str(field(entity_type, "value", entity_type))
            if kind not in {"mention", "text_mention"}:
                continue
            user = field(entity, "user", {})
            start = int(field(entity, "offset", 0)) * 2
            end = start + int(field(entity, "length", 0)) * 2
            name = text.encode("utf-16-le")[start:end].decode(
                "utf-16-le", errors="replace"
            )
            mentions.append(
                dict(
                    user_id=identifier(field(user, "id")),
                    display_name=telegram_name(user) or name or None,
                    is_all=False,
                )
            )
    # External/deleted replies have no recoverable author or message locator here.
    complete = bool(message) and not bool(field(message, "external_reply"))
    return reply, mentions, complete


def merge_reply(primary, supplement):
    if primary is None:
        return supplement
    if supplement is None:
        return primary
    if (
        primary["message_id"] is not None
        and supplement["message_id"] is not None
        and primary["message_id"] != supplement["message_id"]
    ):
        return primary
    return {key: value or supplement.get(key) for key, value in primary.items()}
