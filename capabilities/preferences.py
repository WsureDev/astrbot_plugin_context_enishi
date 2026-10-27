"""Validated batch edits with optimistic concurrency and file rollback."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict

from .catalog import digest
from .policy import MODES, CapabilityPolicy


class PreferenceConflict(ValueError):
    pass


def settings_values(config):
    return asdict(CapabilityPolicy.read(config))


def validate_settings(value, config):
    if not isinstance(value, dict):
        raise ValueError("设置必须是对象")
    allowed = settings_values(config)
    if set(value) - allowed.keys():
        raise ValueError("包含未知配置项")
    for key in ("enabled", "fold_skills", "archive_results"):
        if key in value and type(value[key]) is not bool:
            raise ValueError(key + " 必须是布尔值")
    for key in ("idle_rounds", "mcp_poll_seconds", "mcp_refresh_timeout_seconds"):
        if key in value and (type(value[key]) is not int or value[key] < 0):
            raise ValueError(key + " 必须是非负整数")
    if value.get("idle_rounds") == 0:
        raise ValueError("闲置轮数必须大于 0")
    if value.get("mcp_refresh_timeout_seconds") == 0:
        raise ValueError("MCP 目录请求超时必须大于 0")
    updated = {**config.get("capabilities", {}), **value}
    CapabilityPolicy.read({"capabilities": updated})
    return updated


class CapabilityPreferences:
    def __init__(self, service):
        self.service = service

    def revision(self):
        store = self.service.store
        return digest(
            {
                "groups": store.groups,
                "tools": store.tools,
                "settings": settings_values(self.service.config),
            }
        )

    async def commit(self, value):
        if self.service.closed:
            raise ValueError("能力管理已卸载")
        task = asyncio.create_task(self._commit(value))
        self.service.tasks.add(task)
        task.add_done_callback(self.service.tasks.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A disconnected browser must not interrupt a multi-file save.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
            raise

    async def _commit(self, value):
        service = self.service
        if set(value) - {"revision", "groups", "tools", "settings"}:
            raise ValueError("包含未知修改项")
        async with service.save_lock:
            if value.get("revision") != self.revision():
                raise PreferenceConflict("配置已被更新，请核对最新内容后重新保存")
            staged = copy.copy(service.store)
            staged.groups = copy.deepcopy(service.store.groups)
            staged.tools = copy.deepcopy(service.store.tools)
            groups, tools = value.get("groups", []), value.get("tools", [])
            if not isinstance(groups, list) or not isinstance(tools, list):
                raise ValueError("组策略与工具策略必须是列表")
            catalog = service.catalog_entries()
            seen = set()
            for row in groups:
                if not isinstance(row, dict) or set(row) - {
                    "id",
                    "mode",
                    "idle_rounds",
                    "name",
                }:
                    raise ValueError("组策略格式不正确")
                key = row.get("id")
                if not isinstance(key, str) or key in seen:
                    raise ValueError("组 ID 缺失或重复")
                seen.add(key)
                previous = staged.groups.get(key, {})
                staged.update(
                    key,
                    row.get("mode", previous.get("mode")),
                    row.get("idle_rounds", previous.get("idle_rounds")),
                    name=row.get("name", previous.get("name")),
                )
            seen.clear()
            for row in tools:
                if not isinstance(row, dict) or set(row) != {"id", "mode"}:
                    raise ValueError("工具策略格式不正确")
                key, mode = row["id"], row["mode"]
                if not isinstance(key, str) or key not in catalog or key in seen:
                    raise ValueError("工具或 Skill 不存在，或 ID 重复")
                seen.add(key)
                if mode == "inherit":
                    staged.tools.pop(key, None)
                elif isinstance(mode, str) and mode in MODES:
                    staged.tools[key] = {"mode": mode}
                else:
                    raise ValueError("策略必须是 inherit、primary 或 folded")
            updated = validate_settings(value.get("settings", {}), service.config)
            settings_changed = bool(value.get("settings"))
            original_config = copy.deepcopy(service.config.get("capabilities", {}))
            snapshot = copy.deepcopy(staged.snapshot())
            await service.io(service.store.save, snapshot)
            if settings_changed:
                save = getattr(service.config, "save_config", None)
                try:
                    service.config["capabilities"] = updated
                    if callable(save):
                        save()
                except Exception:
                    service.config["capabilities"] = original_config
                    try:
                        if callable(save):
                            save()
                    finally:
                        await service.io(service.store.save, service.store.snapshot())
                    raise
            for row in groups:
                service.store.groups[row["id"]] = staged.groups[row["id"]]
            service.store.tools = staged.tools
        if settings_changed:
            if service.management_active:
                for client in service.observer.clients().values():
                    service.observer.schedule(client)
            service.request_refresh()
