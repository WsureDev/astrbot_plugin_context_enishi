from __future__ import annotations

import re
from types import SimpleNamespace

from .native import ImageRef, field, qq_images, telegram_images
from .settings import MODE_DISABLED, platform_name, scope

PLACEHOLDER = re.compile(r"\[(?:图片|Image)\]")


class FlowIntegration:
    """Keep native IDs in the existing Flow text, without a new reception index."""

    def __init__(self, owner):
        self.owner = owner
        self.active = True
        self.patches = []
        self.target = None

    def reconcile(self):
        metadata = self.owner.context.get_registered_star(
            "astrbot_plugin_group_context_flow"
        )
        target = field(metadata, "star_cls")
        if target is None or target is self.target:
            return
        if not callable(getattr(target, "_record_from_event", None)) or not callable(
            getattr(target, "_format_delta", None)
        ):
            return
        self.terminate()
        self.active = True
        self.target = target
        original_record = target._record_from_event
        original_format = target._format_delta
        integration = self

        def record(event):
            result = original_record(event)
            if not integration.active or not integration.owner.enabled_event(event):
                return result
            raw = field(event.message_obj, "raw_message")
            if platform_name(event) == "telegram":
                images = telegram_images(
                    field(raw, "message") or field(raw, "effective_message"), event
                )
                for component in field(event.message_obj, "message", []):
                    native_id = field(component, "_channel_native_file_id", "")
                    if native_id:
                        images.append(
                            SimpleNamespace(
                                ref=ImageRef(
                                    "telegram",
                                    str(event.get_platform_id()),
                                    scope(event),
                                    "image",
                                    native_id,
                                    field(component, "_channel_native_message_id", ""),
                                )
                            )
                        )
            elif platform_name(event) == "qq":
                images = qq_images(
                    field(raw, "message", []), event, str(field(raw, "message_id", ""))
                )
            else:
                return result
            ids = list(dict.fromkeys(image.ref.uri for image in images))
            if ids:
                position = 0

                def replace(match):
                    nonlocal position
                    if position >= len(ids):
                        return match.group()
                    replacement = f"[图片 id={ids[position]}]"
                    position += 1
                    return replacement

                text = PLACEHOLDER.sub(replace, result["text"])
                if position < len(ids):
                    text += " " + " ".join(f"[图片 id={i}]" for i in ids[position:])
                result = {**result, "text": text}
            return result

        def format_delta(records):
            if not integration.active:
                return original_format(records)
            output = []
            for record in records:
                text = str(record.get("text", ""))
                if (
                    record.get("platform_name") == "aiocqhttp"
                    and PLACEHOLDER.search(text)
                    and record.get("message_id")
                ):
                    fake = SimpleNamespace(
                        get_platform_name=lambda: "aiocqhttp",
                        get_platform_id=lambda: record["platform_id"],
                        get_group_id=lambda: record["group_id"],
                        get_sender_id=lambda: record["sender_id"],
                        unified_msg_origin=f"{record['platform_id']}:GroupMessage:{record['group_id']}",
                    )
                    if integration.owner.settings.for_event(fake).mode != MODE_DISABLED:
                        index = 0

                        def replace_legacy(match):
                            nonlocal index
                            ref = ImageRef(
                                "qq",
                                record["platform_id"],
                                record["group_id"],
                                "message",
                                record["message_id"],
                                record["message_id"],
                                index,
                            )
                            index += 1
                            return f"[图片 id={ref.uri}]"

                        text = PLACEHOLDER.sub(replace_legacy, text)
                output.append({**record, "text": text})
            return original_format(output)

        for name, replacement in (
            ("_record_from_event", record),
            ("_format_delta", format_delta),
        ):
            self.patches.append(
                (
                    target,
                    name,
                    getattr(target, name),
                    replacement,
                    name in target.__dict__,
                )
            )
            setattr(target, name, replacement)

    def terminate(self):
        self.active = False
        for target, name, original, replacement, own in reversed(self.patches):
            if getattr(target, name) is replacement:
                if own:
                    setattr(target, name, original)
                else:
                    delattr(target, name)
        self.patches.clear()
        self.target = None
