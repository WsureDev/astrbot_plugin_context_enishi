from __future__ import annotations

from ..channels.native import field


def append_temporary_text(request, text, prefix, text_factory):
    """Compose a request-local text addition without modifying saved history."""
    parts = list(field(request, "extra_user_content_parts", []) or [])
    parts = [
        part
        for part in parts
        if not (
            field(part, "type") == "text"
            and str(field(part, "text", "")).startswith(prefix)
        )
    ]
    addition = text_factory(text=text)
    if hasattr(addition, "mark_as_temp"):
        addition.mark_as_temp()
    elif isinstance(addition, dict):
        addition["_no_save"] = True
    parts.append(addition)
    request.extra_user_content_parts = parts
