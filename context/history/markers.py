"""Deduplicate generated standalone image markers while retaining full IDs."""

from __future__ import annotations

import re

from ...channels.native import field

MARKER = re.compile(r"^\[图片 id=((?:qq|telegram)://[^\s\]]+)(?: 来源=([^\]]+))?\]$")


def deduplicate_markers(messages, text_factory):
    removed = 0
    for message in messages:
        parts = field(message, "content")
        if not isinstance(parts, list):
            continue
        seen, origins, output = set(), set(), []
        for part in parts:
            if field(part, "type") != "text":
                output.append(part)
                continue
            lines = str(field(part, "text", "")).splitlines()
            matches = [MARKER.fullmatch(line) for line in lines]
            if not lines or not all(matches):
                output.append(part)
                continue
            kept = []
            for line, match in zip(lines, matches):
                uri, origin = match.groups()
                if uri in seen and (origin is None or (uri, origin) in origins):
                    removed += 1
                    continue
                seen.add(uri)
                origins.add((uri, origin))
                kept.append(line)
            if not kept:
                continue
            replacement = "\n".join(kept)
            if replacement != field(part, "text"):
                if isinstance(part, dict):
                    part = {**part, "text": replacement}
                elif hasattr(part, "model_copy"):
                    part = part.model_copy(update={"text": replacement})
                else:
                    part = text_factory(text=replacement)
            output.append(part)
        if isinstance(message, dict):
            message["content"] = output
        else:
            message.content = output
    return removed
