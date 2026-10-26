"""Expire plugin-owned image descriptions without removing tool-call records."""

from __future__ import annotations

import json

from ..channels.native import field

TOOL_IMAGE_PREFIX = "[Image from tool '"
CAPTION_PREFIX = "[工具图片转述] "


def is_tool_image_message(message):
    parts = field(message, "content")
    return (
        field(message, "role") == "user"
        and isinstance(parts, list)
        and any(
            field(part, "type") == "text"
            and str(field(part, "text", "")).startswith(TOOL_IMAGE_PREFIX)
            for part in parts
        )
    )


def _expired_result(text):
    start = len(text) - len(text.lstrip())
    try:
        payload, end = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return text
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("image_id"), str)
        or not isinstance(payload.get("description"), str)
    ):
        return text
    del payload["description"]
    return text[:start] + json.dumps(payload, ensure_ascii=False) + text[end:]


class CaptionHistory:
    def __init__(self):
        self.description_calls = set()

    def process(self, messages, text_factory, *, expire):
        for message in messages:
            if field(message, "role") == "assistant":
                for call in field(message, "tool_calls", []) or []:
                    call_id = field(call, "id")
                    if not call_id:
                        continue
                    if (
                        field(field(call, "function"), "name")
                        == "describe_channel_image"
                    ):
                        self.description_calls.add(call_id)
                    else:
                        self.description_calls.discard(call_id)
            if not expire:
                continue
            content = field(message, "content")
            if is_tool_image_message(message):
                output = []
                for part in content:
                    if field(part, "type") == "text" and str(
                        field(part, "text", "")
                    ).startswith(CAPTION_PREFIX):
                        continue
                    output.append(part)
                self._set_content(message, output)
            elif (
                field(message, "role") == "tool"
                and field(message, "tool_call_id") in self.description_calls
            ):
                if isinstance(content, str):
                    self._set_content(message, _expired_result(content))
                elif isinstance(content, list):
                    output = []
                    for part in content:
                        if field(part, "type") == "text":
                            text = field(part, "text", "")
                            replacement = _expired_result(text)
                            if replacement != text:
                                part = text_factory(text=replacement)
                        output.append(part)
                    self._set_content(message, output)

    @staticmethod
    def _set_content(message, content):
        if isinstance(message, dict):
            message["content"] = content
        else:
            message.content = content
