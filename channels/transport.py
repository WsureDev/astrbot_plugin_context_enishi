from __future__ import annotations

import asyncio
from typing import Any

from ..settings import Settings


class ChannelError(Exception):
    """A recoverable platform capability or retrieval failure."""


class ChannelTransport:
    def __init__(self, context: Any, settings: Settings):
        self.context = context
        self.settings = settings
        self.timeout = settings.integer("request_timeout_seconds", 30, 1, 120)

    def client(self, event: Any, telegram: bool = False):
        current = getattr(event, "client" if telegram else "bot", None)
        if current is not None:
            return current
        getter = getattr(self.context, "get_platform_inst", None)
        instance = getter(event.get_platform_id()) if callable(getter) else None
        current = getattr(instance, "client", None)
        if current is not None:
            return current
        getter = getattr(instance, "get_client", None)
        return getter() if callable(getter) else None

    async def action(self, event: Any, name: str, **params: Any) -> Any:
        bot = self.client(event)
        call = getattr(bot, "call_action", None) or getattr(
            getattr(bot, "api", None), "call_action", None
        )
        if not callable(call):
            raise ChannelError("OneBot client is unavailable in this event")
        try:
            result = await asyncio.wait_for(call(name, **params), self.timeout)
        except Exception as exc:
            raise ChannelError(f"{name} failed ({type(exc).__name__})") from exc
        if isinstance(result, dict) and result.get("status") == "failed":
            raise ChannelError(f"{name} returned failure")
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    async def telegram_call(self, event: Any, name: str, **params: Any) -> Any:
        client = self.client(event, telegram=True)
        method = getattr(client, name, None)
        if not callable(method):
            raise ChannelError("Telegram client is unavailable in this event")
        try:
            return await asyncio.wait_for(method(**params), self.timeout)
        except Exception as exc:
            raise ChannelError(f"{name} failed ({type(exc).__name__})") from exc
