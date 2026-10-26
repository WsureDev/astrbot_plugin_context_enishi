"""Model-facing META views. Acquisition data stays separate from serialization."""

from __future__ import annotations

from ..channels.message_components import component, merge_components
from ..channels.messages import event_meta, event_record
from ..channels.meta import CURRENT_FIELDS, rows, table


def messages(event, result):
    metadata = event_meta(event)
    metadata.update(
        {
            key: result[key]
            for key in (
                "source",
                "pages",
                "next_cursor",
                "scan_exhausted",
                "complete",
                "coverage",
            )
            if key in result
        }
    )
    records = result.get("messages")
    if records is None:
        records = [result]
    return table(records, metadata)


def members(event, result):
    metadata = event_meta(event)
    metadata.update(
        {
            key: result[key]
            for key in (
                "source",
                "order",
                "total_known",
                "has_more",
                "membership_complete",
                "complete",
                "coverage",
            )
            if key in result
        }
    )
    for key in ("window", "activity"):
        for name, value in (result.get(key) or {}).items():
            metadata[key + "." + name] = value
    if result.get("warnings"):
        metadata["warnings"] = ";".join(result["warnings"])
    chat = result.get("chat") or {}
    if chat.get("name"):
        metadata["group.group_name"] = chat["name"]
    if chat.get("member_count") is not None:
        metadata["group.member_count"] = chat["member_count"]
    records = []
    for person in result.get("participants", []):
        records.append(
            dict(
                user_id=person.get("user_id"),
                nickname=person.get("display_name"),
                role=person.get("role"),
                last_spoke_at=person.get("last_spoke_at"),
                observed_messages=person.get("observed_messages"),
                origin=person.get("origin"),
            )
        )
    return table(
        records,
        metadata,
        fields=(
            "user_id",
            "nickname",
            "role",
            "last_spoke_at",
            "observed_messages",
            "origin",
        ),
    )


def member(event, result):
    result = dict(result)
    result["nickname"] = (
        result.get("card")
        or result.get("nickname")
        or result.get("name")
        or result.get("username")
    )
    fields = ["user_id", "nickname"]
    fields.extend(
        key
        for key in ("role", "status", "username", "join_time", "last_sent_time")
        if result.get(key) is not None
    )
    return table([result], event_meta(event), fields=tuple(fields))


def environment(event, result):
    record = event_record(event)
    reply = result.get("reply_to")
    if reply:
        supplement = component(
            "Reply",
            id=reply.get("message_id"),
            sender_id=reply.get("user_id"),
            sender_nickname=reply.get("display_name"),
        )
        record["message"] = merge_components(record["message"], [supplement])
    header, _, listing = members(event, result).partition("\n")
    return header + "\n" + rows([record], CURRENT_FIELDS) + "\nMEMBERS\n" + listing
