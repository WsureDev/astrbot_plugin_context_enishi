"""Expose compact current-message references independently of image/flow modes."""

from __future__ import annotations

from ..channels.content_refs import CONTENT_TYPES
from ..channels.messages import event_meta, event_record
from ..channels.meta import CONTENT_RULES, CURRENT_TAG, current_table
from ..channels.native import field
from ..settings import platform_name


def prepare_component_context(event, request, text_factory):
    if platform_name(event) != "qq":
        return
    record = event_record(event)
    if not any(
        part["type"].lower() in CONTENT_TYPES and part.get("id")
        for part in record["message"]
    ):
        return
    parts = list(field(request, "extra_user_content_parts", []) or [])
    if not any(
        str(field(part, "text", "")).startswith("<" + CURRENT_TAG + ">\n")
        for part in parts
    ):
        parts.append(text_factory(text=current_table(record, event_meta(event))))
        request.extra_user_content_parts = parts
    prompt = str(field(request, "system_prompt", "") or "")
    if CONTENT_RULES not in prompt:
        request.system_prompt = prompt + "\n" + CONTENT_RULES
