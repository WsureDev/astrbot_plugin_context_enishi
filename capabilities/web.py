from __future__ import annotations


class CapabilityAPI:
    def __init__(self, context, service):
        self.context, self.service = context, service
        self.handlers = []

    def install(self):
        for route, handler, methods in (
            ("state", self.state, ["GET"]),
            ("group", self.group, ["POST"]),
            ("tool", self.tool, ["POST"]),
            ("settings", self.settings, ["POST"]),
            ("refresh", self.refresh, ["POST"]),
        ):
            self.context.register_web_api(
                "/astrbot_plugin_context_enishi/capabilities/" + route,
                handler,
                methods,
                "Enishi 工具与 Skill 管理",
            )
            self.handlers.append(handler)

    async def body(self):
        from astrbot.api.web import request

        value = await request.get_json()
        if not isinstance(value, dict):
            raise ValueError("请求必须是 JSON 对象")
        return value

    async def state(self):
        from astrbot.api.web import json_response

        return json_response(self.service.snapshot())

    async def group(self):
        from astrbot.api.web import json_response

        try:
            value = await self.body()
            self.service.store.update(
                value["group"],
                value["mode"],
                value.get("idle_rounds"),
                name=value.get("name"),
            )
            await self.service.persist()
            return json_response({"ok": True})
        except (KeyError, ValueError, TypeError) as exc:
            return json_response({"error": str(exc)}, status_code=400)

    async def tool(self):
        from astrbot.api.web import json_response

        try:
            value = await self.body()
            key, mode = value["id"], value["mode"]
            if key not in self.service.catalog_entries():
                raise ValueError("工具或 Skill 已经不存在")
            if mode == "inherit":
                self.service.store.tools.pop(key, None)
            elif mode in {"primary", "folded"}:
                self.service.store.tools[key] = {"mode": mode}
            else:
                raise ValueError("策略必须是 inherit、primary 或 folded")
            await self.service.persist()
            return json_response({"ok": True})
        except (KeyError, ValueError, TypeError) as exc:
            return json_response({"error": str(exc)}, status_code=400)

    async def settings(self):
        from astrbot.api.web import json_response

        from .policy import CapabilityPolicy

        try:
            value = await self.body()
            allowed = {
                "enabled",
                "new_group_mode",
                "idle_rounds",
                "mcp_poll_seconds",
                "fold_skills",
                "archive_results",
            }
            if set(value) - allowed:
                raise ValueError("包含未知配置项")
            for key in ("enabled", "fold_skills", "archive_results"):
                if key in value and not isinstance(value[key], bool):
                    raise ValueError(key + " 必须是布尔值")
            for key in ("idle_rounds", "mcp_poll_seconds"):
                if key in value and (type(value[key]) is not int or value[key] < 0):
                    raise ValueError(key + " 必须是非负整数")
            if value.get("idle_rounds") == 0:
                raise ValueError("闲置轮数必须大于 0")
            updated = {**self.service.config.get("capabilities", {}), **value}
            CapabilityPolicy.read({"capabilities": updated})
            self.service.config["capabilities"] = updated
            save = getattr(self.service.config, "save_config", None)
            if callable(save):
                save()
            if self.service.management_active:
                for client in self.service.observer.clients().values():
                    await self.service.observer.reconcile(client)
            self.service.request_refresh()
            return json_response({"ok": True})
        except (ValueError, TypeError) as exc:
            return json_response({"error": str(exc)}, status_code=400)

    async def refresh(self):
        from astrbot.api.web import json_response

        if self.service.management_active:
            for client in self.service.observer.clients().values():
                self.service.observer.schedule(client)
            if self.service.observer.pull_tasks:
                import asyncio

                await asyncio.gather(
                    *list(self.service.observer.pull_tasks), return_exceptions=True
                )
        await self.service.refresh()
        return json_response(self.service.snapshot())

    def close(self):
        routes = getattr(self.context, "registered_web_apis", None)
        if isinstance(routes, list):
            routes[:] = [row for row in routes if row[1] not in self.handlers]
        self.handlers.clear()
