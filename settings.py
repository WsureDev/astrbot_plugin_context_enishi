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
CONTEXT_KEYS = (
    "image_mode",
    "caption_provider_id",
    "caption_fallback_provider_ids",
    "caption_cache_ttl_hours",
    "max_caption_chars",
)


def migrate_channel_settings(config):
    """Convert legacy flat image settings, retaining scope and independent awareness."""
    changed = False
    rules = config.get("channel_settings", [])
    if not isinstance(rules, list):
        return False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        legacy = {key: rule[key] for key in CONTEXT_KEYS if key in rule}
        if legacy:
            context = dict(rule.get("context", {}) or {})
            context.update(legacy)
            rule["context"] = context
            for key in legacy:
                del rule[key]
            changed = True
    return changed


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


@dataclass(frozen=True)
class AwarenessPolicy:
    enabled: bool = True
    inject_environment: bool = False
    summary_members: int = 8
    summary_max_chars: int = 1600
    max_participants: int = 100
    scan_pages: int = 10


class Settings:
    def __init__(self, config: Any):
        self.config = config

    def _rule(self, event: Any) -> dict:
        rules = self.config.get("channel_settings", [])
        if not isinstance(rules, list):
            raise ValueError("请在渠道上下文设置中添加 QQ 或 Telegram 渠道")
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
        return selected

    def for_event(self, event: Any) -> Policy:
        rule = self._rule(event)
        selected = rule.get("context", rule)
        if not isinstance(selected, dict):
            raise ValueError("上下文整理设置必须是一个配置对象")
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

    def awareness_for_event(self, event: Any) -> AwarenessPolicy:
        selected = self._rule(event).get("awareness", {})
        if not isinstance(selected, dict):
            raise ValueError("聊天感知增强设置必须是一个配置对象")
        return AwarenessPolicy(
            enabled=bool(selected.get("enabled", True)),
            inject_environment=bool(selected.get("inject_environment", False)),
            summary_members=min(max(int(selected.get("summary_members", 8)), 1), 30),
            summary_max_chars=min(
                max(int(selected.get("summary_max_chars", 1600)), 256), 8192
            ),
            max_participants=min(
                max(int(selected.get("max_participants", 100)), 1), 500
            ),
            scan_pages=min(max(int(selected.get("scan_pages", 10)), 1), 50),
        )

    def flow_enabled(self, event):
        return bool(
            self._rule(event).get("context", {}).get("group_flow_enabled", False)
        )

    def flow_options(self):
        selected = self.config.get("group_flow", {}) or {}
        if not isinstance(selected, dict):
            raise ValueError("群聊流水设置必须是一个配置对象")
        max_chars = min(max(int(selected.get("max_delta_chars", 16000)), 0), 1000000)
        if max_chars:
            max_chars = max(max_chars, 512)
        return dict(
            max_log_records=min(
                max(int(selected.get("max_log_records", 5000)), 0), 100000
            ),
            max_delta_messages=min(
                max(int(selected.get("max_delta_messages", 0)), 0), 10000
            ),
            max_delta_chars=max_chars,
            record_self_messages=bool(selected.get("record_self_messages", False)),
            record_empty_messages=bool(selected.get("record_empty_messages", True)),
        )

    def integer(self, key: str, default: int, minimum: int, maximum: int) -> int:
        return min(max(int(self.config.get(key, default)), minimum), maximum)
