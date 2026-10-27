from __future__ import annotations

from dataclasses import dataclass

from ..identity import PLUGIN

MODES = {"primary", "folded"}
META_NAMES = {"enishi_find", "enishi_load", "enishi_unload"}
DEFAULT_MCP_REFRESH_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class CapabilityPolicy:
    enabled: bool = False
    new_group_mode: str = "folded"
    idle_rounds: int = 5
    mcp_poll_seconds: int = 60
    mcp_refresh_timeout_seconds: int = DEFAULT_MCP_REFRESH_TIMEOUT_SECONDS
    fold_skills: bool = True
    archive_results: bool = True

    @classmethod
    def read(cls, config):
        raw = config.get("capabilities", {}) or {}
        mode = raw.get("new_group_mode", "folded")
        if mode not in MODES:
            raise ValueError("工具组策略必须是 primary 或 folded")
        timeout = raw.get(
            "mcp_refresh_timeout_seconds", DEFAULT_MCP_REFRESH_TIMEOUT_SECONDS
        )
        if type(timeout) is not int or timeout <= 0:
            raise ValueError("MCP 目录刷新超时必须是正整数")
        return cls(
            enabled=bool(raw.get("enabled", False)),
            new_group_mode=mode,
            idle_rounds=max(1, int(raw.get("idle_rounds", 5))),
            mcp_poll_seconds=max(0, int(raw.get("mcp_poll_seconds", 60))),
            mcp_refresh_timeout_seconds=timeout,
            fold_skills=bool(raw.get("fold_skills", True)),
            archive_results=bool(raw.get("archive_results", True)),
        )


def participates(event):
    selected = getattr(event, "plugins_name", None)
    return event is not None and (
        selected is None or "*" in selected or PLUGIN in selected
    )


def lazy_conflict(context):
    getter = getattr(context, "get_registered_star", None)
    metadata = getter("astrbot_plugin_lazy_tools") if callable(getter) else None
    if metadata is None or not getattr(metadata, "activated", True):
        return ""
    plugin = getattr(metadata, "star_cls", None)
    if plugin is None:
        return ""
    config = getattr(getattr(plugin, "runtime", None), "config", {})
    plugin_config = getattr(plugin, "plugin_config", {})
    if config.get("enabled", True) and plugin_config.get("enabled", True):
        if config.get("mode", "lazy") == "lazy" or config.get("skills_enabled", True):
            return "Lazy Tools 正在管理工具或 Skills，请先关闭其采集变换"
    return ""
