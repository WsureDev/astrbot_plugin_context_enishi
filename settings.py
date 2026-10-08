from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def platform_name(event: Any) -> str:
    name = str(event.get_platform_name()).lower()
    if name == "cron":
        context = getattr(event, "context_obj", None)
        getter = getattr(context, "get_platform_inst", None)
        instance = getter(event.get_platform_id()) if callable(getter) else None
        config = getattr(instance, "config", {}) or {}
        name = str(config.get("type", name)).lower()
    return {"aiocqhttp": "qq", "onebot": "qq", "napcat": "qq"}.get(name, name)


def scope(event: Any) -> str:
    return str(event.get_group_id() or event.get_sender_id())


@dataclass(frozen=True)
class Policy:
    mode: str = "off"
    caption_provider_id: str = ""


class Settings:
    def __init__(self, config: Any):
        self.config = config

    def for_event(self, event: Any) -> Policy:
        values = {
            "mode": self.config.get("default_mode", "off"),
            "caption_provider_id": self.config.get("caption_provider_id", ""),
        }
        rules = self.config.get("channels", "{}")
        if isinstance(rules, str):
            rules = json.loads(rules or "{}")
        if not isinstance(rules, dict):
            raise ValueError("channels must be a JSON object")
        for key in (
            platform_name(event),
            "platform:" + str(event.get_platform_id()),
            "session:" + str(event.unified_msg_origin),
        ):
            rule = rules.get(key, {})
            if isinstance(rule, str):
                rule = {"mode": rule}
            if not isinstance(rule, dict):
                raise ValueError("channel rules must be objects or mode strings")
            values.update(rule)
        mode = str(values["mode"]).strip().upper()
        mode = {"OFF": "off", "关": "off", "关闭": "off"}.get(mode, mode)
        if mode not in {"off", "A", "B"}:
            raise ValueError("image mode must be off, A or B")
        return Policy(mode, str(values.get("caption_provider_id", "")))

    def integer(self, key: str, default: int, minimum: int, maximum: int) -> int:
        return min(max(int(self.config.get(key, default)), minimum), maximum)
