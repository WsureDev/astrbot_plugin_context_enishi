from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MODE_DISABLED = "disabled"
MODE_CURRENT_TURN = "current_turn"
MODE_REFERENCE_ONLY = "reference_only"
MODE_LABELS = {
    MODE_DISABLED: "关闭",
    MODE_CURRENT_TURN: "仅本轮消息及引用允许图片内容",
    MODE_REFERENCE_ONLY: "始终使用图片 ID 引用图片",
}


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
    mode: str = MODE_DISABLED
    caption_provider_id: str = ""
    caption_fallback_provider_ids: tuple[str, ...] = ()
    caption_cache_ttl_hours: int = 0
    max_caption_chars: int = 4000


class Settings:
    def __init__(self, config: Any):
        self.config = config

    def for_event(self, event: Any) -> Policy:
        rules = self.config.get("channel_settings", [])
        if not isinstance(rules, list):
            raise ValueError("请在渠道图片设置中添加 QQ 或 Telegram 渠道")
        selected, specificity = {}, -1
        for rule in rules:
            if not isinstance(rule, dict) or rule.get(
                "__template_key"
            ) != platform_name(event):
                continue
            limits = rule.get("scope", {}) or {}
            platform_id = str(limits.get("platform_id", "") or "").strip()
            chat_id = str(limits.get("chat_id", "") or "").strip()
            if platform_id and platform_id != str(event.get_platform_id()):
                continue
            if chat_id and chat_id != scope(event):
                continue
            score = int(bool(platform_id)) + int(bool(chat_id)) * 2
            if score >= specificity:
                selected, specificity = rule, score
        mode = str(selected.get("image_mode", MODE_DISABLED))
        if mode not in MODE_LABELS:
            raise ValueError("请从图片处理方式中选择有效选项")
        fallbacks = selected.get("caption_fallback_provider_ids", []) or []
        if not isinstance(fallbacks, list):
            raise ValueError("请通过备用图片转述模型选择器选择模型")
        return Policy(
            mode=mode,
            caption_provider_id=str(
                selected.get("caption_provider_id", "") or ""
            ).strip(),
            caption_fallback_provider_ids=tuple(
                dict.fromkeys(str(p).strip() for p in fallbacks if str(p).strip())
            ),
            caption_cache_ttl_hours=max(
                int(selected.get("caption_cache_ttl_hours", 0)), 0
            ),
            max_caption_chars=min(
                max(int(selected.get("max_caption_chars", 4000)), 100), 16000
            ),
        )

    def integer(self, key: str, default: int, minimum: int, maximum: int) -> int:
        return min(max(int(self.config.get(key, default)), minimum), maximum)
