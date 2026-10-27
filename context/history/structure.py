"""Identify owned group deltas and preserve tool chains as whole user rounds."""

from __future__ import annotations

import json

from ...channels.meta import DELTA_TAG, _metadata
from ...channels.native import field
from ...settings import scope
from ..captions import is_tool_image_message

DELTA_OPEN = "<" + DELTA_TAG + ">"
DELTA_CLOSE = "</" + DELTA_TAG + ">"
FIELDS_PREFIX = "FIELDS "


def content_size(message):
    value = message
    if hasattr(message, "model_dump"):
        value = message.model_dump(mode="json", exclude_none=True)
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    )


def delta_table(message, event):
    content = field(message, "content")
    if field(message, "role") != "user" or not isinstance(content, str):
        return None
    lines = content.splitlines()
    if len(lines) < 4 or lines[0] != DELTA_OPEN or lines[-1] != DELTA_CLOSE:
        return None
    metadata = _metadata(lines[1])
    skipped = metadata.get("skipped_count", 0)
    if type(skipped) is not int or skipped < 0:
        return None
    if metadata.get("platform_meta.id") != str(event.get_platform_id()):
        return None
    if metadata.get("group.group_id") != str(event.get_group_id()).split("#")[0]:
        return None
    if metadata.get("session_id") != str(
        field(event.message_obj, "session_id") or scope(event)
    ):
        return None
    if not lines[2].startswith(FIELDS_PREFIX):
        return None
    return metadata, lines[2], lines[3:-1]


def render_delta(metadata, fields, rows):
    header = "META " + " ".join(
        key + "=" + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        for key, value in metadata.items()
    )
    return "\n".join([DELTA_OPEN, header, fields, *rows, DELTA_CLOSE])


def rounds(messages, event):
    if not messages:
        return []
    starts, pending_delta, pending_calls = [0], None, set()
    for index, message in enumerate(messages):
        if delta_table(message, event) is not None:
            if pending_delta is None:
                pending_delta = index
            continue
        role = field(message, "role")
        if role == "user" and not is_tool_image_message(message) and not pending_calls:
            start = index
            if pending_delta is not None:
                start = pending_delta
            if start > starts[-1]:
                starts.append(start)
        pending_delta = None
        if role == "assistant":
            pending_calls.update(
                field(call, "id") for call in field(message, "tool_calls", []) or []
            )
        elif role == "tool":
            pending_calls.discard(field(message, "tool_call_id"))
    return [
        messages[start:end] for start, end in zip(starts, [*starts[1:], len(messages)])
    ]
