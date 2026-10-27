from __future__ import annotations

from ..channels.native import field
from .preferences import PreferenceConflict


class CapabilityAPI:
    def __init__(self, context, service):
        self.context, self.service = context, service
        self.handlers = []

    def install(self):
        for route, handler, methods in (
            ("state", self.state, ["GET"]),
            ("commit", self.commit, ["POST"]),
            ("detail", self.detail, ["POST"]),
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

        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError("请求必须是 JSON 对象")
        return value

    async def state(self):
        from astrbot.api.web import json_response

        return json_response(self.service.snapshot())

    async def commit(self):
        from astrbot.api.web import json_response

        try:
            await self.service.preferences.commit(await self.body())
            return json_response({"ok": True, "state": self.service.snapshot()})
        except PreferenceConflict as exc:
            return json_response({"error": str(exc)}, status_code=409)
        except (KeyError, ValueError, TypeError) as exc:
            return json_response({"error": str(exc)}, status_code=400)
        except OSError as exc:
            self.service.failure("保存配置", exc)
            return json_response(
                {
                    "error": "写入配置失败，修改未生效。请检查插件目录的写入权限和磁盘空间。"
                },
                status_code=500,
            )

    async def detail(self):
        from astrbot.api.web import json_response

        try:
            value = await self.body()
            entry = self.service.catalog_entries().get(value.get("id"))
            if entry is None:
                return json_response({"error": "该能力已不在目录中"}, status_code=404)
            return json_response(
                {
                    "id": entry.id,
                    "parameters": field(entry.value, "parameters", {}),
                    "path": field(entry.value, "path", ""),
                }
            )
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
