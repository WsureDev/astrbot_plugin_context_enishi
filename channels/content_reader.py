"""Fetch original OneBot segment data on demand; never cache card bodies."""

from __future__ import annotations

from ..settings import scope
from .content_details import content_features
from .content_refs import ContentRef, content_type
from .native import field, normalize_segments
from .transport import ChannelError


async def qq_message_payload(transport, event, message_id):
    payload = await transport.action(event, "get_msg", message_id=str(message_id))
    if not isinstance(payload, dict):
        raise ChannelError("message is unavailable")
    actual_chat = str(field(payload, "group_id", "") or field(payload, "user_id", ""))
    if actual_chat and actual_chat != scope(event):
        raise ChannelError("message belongs to another chat")
    if payload.get("message_id") is not None and str(payload["message_id"]) != str(
        message_id
    ):
        raise ChannelError("message ID mismatch")
    return {**payload, "message_id": str(message_id)}


async def read_content(transport, event, content_id):
    ref = ContentRef.parse(content_id, event)
    payload = await qq_message_payload(transport, event, ref.message_id)
    segments = [
        part
        for part in normalize_segments(field(payload, "message", []))
        if content_type(field(part, "type", "")) == ref.kind
    ]
    if ref.index >= len(segments):
        raise ChannelError(
            "原消息中没有这个类型或序号的组件；请重新查询消息，不能用其他组件替代"
        )
    segment = segments[ref.index]
    data = field(segment, "data", {})
    if not isinstance(data, dict):
        raise ChannelError("原消息组件 data 格式不可读取")
    result = dict(
        ok=True,
        source="napcat",
        id=ref.uri,
        message_id=ref.message_id,
        type=field(segment, "type"),
        index=ref.index,
        data=data,
        features=content_features(ref.kind, data),
    )
    if ref.kind == "forward" and data.get("id") and not data.get("content"):
        result["forward"] = await transport.action(
            event, "get_forward_msg", message_id=str(data["id"])
        )
    return result
