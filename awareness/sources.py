from __future__ import annotations

import time
from typing import Any

from ..channels.native import (
    ImageRef,
    field,
    normalize_segments,
    qq_images,
    telegram_images,
)
from ..channels.transport import ChannelError, ChannelTransport
from ..settings import platform_name, scope


class ChatSources(ChannelTransport):
    """Read chat facts and existing history; do not edit model messages or store pixels."""

    async def user_avatar(self, event: Any, user_id: str) -> ImageRef:
        user_id = user_id or str(event.get_sender_id())
        if not user_id.lstrip("-").isdigit():
            raise ChannelError("avatar lookup requires a numeric platform user ID")
        if platform_name(event) == "qq":
            return ImageRef(
                "qq", str(event.get_platform_id()), scope(event), "avatar", user_id
            )
        if platform_name(event) == "telegram":
            photos = await self.telegram_call(
                event, "get_user_profile_photos", user_id=int(user_id), limit=1
            )
            if not photos.photos:
                raise ChannelError("no accessible profile photo")
            return ImageRef(
                "telegram",
                str(event.get_platform_id()),
                scope(event),
                "image",
                photos.photos[0][-1].file_id,
            )
        raise ChannelError("this platform has no avatar adapter")

    def _render_qq_message(self, event: Any, message: Any) -> dict:
        mid = str(field(message, "message_id", ""))
        parts = normalize_segments(field(message, "message", []))
        images = qq_images(parts, event, mid)
        text = (
            "".join(
                str(field(field(s, "data", {}), "text", ""))
                for s in parts
                if field(s, "type") == "text"
            )
            if isinstance(parts, list)
            else str(parts)
        )
        return {
            "message_id": mid,
            "sender_id": str(field(field(message, "sender", {}), "user_id", "")),
            "timestamp": field(message, "time", 0),
            "text": text[:8000],
            "image_ids": [i.ref.uri for i in images],
        }

    async def message(self, event: Any, message_id: str) -> dict:
        if platform_name(event) == "qq":
            payload = await self.action(event, "get_msg", message_id=str(message_id))
            if not isinstance(payload, dict):
                raise ChannelError("message is unavailable")
            actual_chat = str(
                field(payload, "group_id", "") or field(payload, "user_id", "")
            )
            if actual_chat and actual_chat != scope(event):
                raise ChannelError("message belongs to another chat")
            return {"source": "napcat", **self._render_qq_message(event, payload)}
        if platform_name(event) == "telegram":
            raw = field(event.message_obj, "raw_message")
            message = field(raw, "message") or field(raw, "effective_message")
            for candidate in (message, field(message, "reply_to_message")):
                if str(field(candidate, "message_id", "")) != str(message_id):
                    continue
                date = field(candidate, "date", 0)
                timestamp = date.timestamp() if hasattr(date, "timestamp") else date
                return {
                    "source": "telegram_payload",
                    "message_id": str(message_id),
                    "sender_id": str(
                        field(
                            field(candidate, "from_user", field(candidate, "from", {})),
                            "id",
                            "",
                        )
                    ),
                    "timestamp": timestamp,
                    "text": str(
                        field(candidate, "text", "") or field(candidate, "caption", "")
                    )[:8000],
                    "image_ids": [
                        i.ref.uri for i in telegram_images(candidate, event, "reply")
                    ],
                }
            raise ChannelError(
                "Telegram Bot API cannot fetch arbitrary message IDs; this message is absent from the current payload"
            )
        raise ChannelError("this platform has no message adapter")

    async def user_history(
        self, event: Any, user_id: str, limit: int, days: int, before_id: str = ""
    ) -> dict:
        limit = min(
            max(int(limit), 1),
            self.settings.integer("max_history_results", 100, 1, 500),
        )
        cutoff = time.time() - min(max(int(days), 1), 365) * 86400
        max_pages = self.settings.integer("max_history_pages", 10, 1, 50)
        results: list[dict] = []
        pages = 0
        cursor = before_id
        exhausted = False
        if platform_name(event) == "qq":
            if not event.get_group_id():
                raise ChannelError("QQ history lookup requires a group event")
            seen: set[str] = set()
            while pages < max_pages and len(results) < limit:
                params = {
                    "group_id": event.get_group_id(),
                    "count": 100,
                    "reverseOrder": True,
                    "parse_mult_msg": False,
                    "disable_get_url": True,
                    "quick_reply": True,
                }
                if cursor:
                    params["message_seq"] = cursor
                payload = await self.action(event, "get_group_msg_history", **params)
                messages = field(payload, "messages", []) or []
                pages += 1
                if not messages:
                    exhausted = True
                    break
                messages = sorted(
                    messages, key=lambda m: field(m, "time", 0), reverse=True
                )
                oldest = messages[-1]
                new_cursor = str(
                    field(oldest, "message_seq", "") or field(oldest, "message_id", "")
                )
                for message in messages:
                    mid = str(field(message, "message_id", ""))
                    if (
                        not mid
                        or mid in seen
                        or mid == before_id
                        or field(message, "time", 0) < cutoff
                    ):
                        continue
                    seen.add(mid)
                    sender = str(field(field(message, "sender", {}), "user_id", ""))
                    if not user_id or sender == str(user_id):
                        results.append(self._render_qq_message(event, message))
                if field(oldest, "time", 0) < cutoff or new_cursor == cursor:
                    exhausted = True
                    cursor = new_cursor
                    break
                cursor = new_cursor
            if len(results) > limit:
                cursor = results[limit - 1]["message_id"]
                exhausted = False
            return {
                "source": "napcat",
                "messages": results[:limit],
                "pages": pages,
                "next_cursor": cursor,
                "scan_exhausted": exhausted,
                "complete": False,
                "coverage": "protocol-accessible history within the configured scan budget",
            }
        if platform_name(event) == "telegram":
            manager = getattr(self.context, "message_history_manager", None)
            if manager is None:
                raise ChannelError(
                    "Telegram Bot API has no group-history endpoint; no local history manager"
                )
            for page in range(1, max_pages + 1):
                records = await manager.get(
                    platform_id=event.get_platform_id(),
                    user_id=scope(event),
                    page=page,
                    page_size=100,
                )
                pages = page
                if not records:
                    exhausted = True
                    break
                for record in records:
                    created = field(record, "created_at")
                    if created and created.timestamp() < cutoff:
                        continue
                    local_id = str(field(record, "id", ""))
                    if (
                        before_id
                        and local_id.isdigit()
                        and int(local_id) >= int(before_id)
                    ):
                        continue
                    sender = str(field(record, "sender_id", ""))
                    if user_id and sender != str(user_id):
                        continue
                    content = field(record, "content", {}) or {}
                    parts = field(content, "message", [])
                    text = "".join(
                        str(field(p, "text", ""))
                        if field(p, "type") in {"text", "plain"}
                        else "[图片，原生ID未保存]"
                        if field(p, "type") == "image"
                        else ""
                        for p in parts
                    )
                    results.append(
                        {
                            "local_record_id": local_id,
                            "sender_id": sender,
                            "timestamp": created.timestamp() if created else 0,
                            "text": text[:8000],
                        }
                    )
                if len(results) >= limit or len(records) < 100:
                    exhausted = len(records) < 100
                    break
            results.sort(key=lambda r: r["timestamp"], reverse=True)
            return {
                "source": "astrbot_local",
                "messages": results[:limit],
                "pages": pages,
                "next_cursor": results[min(limit, len(results)) - 1]["local_record_id"]
                if results
                else "",
                "complete": False,
                "scan_exhausted": exhausted,
                "coverage": "only records already stored locally; Telegram Bot API cannot retrieve arbitrary group history",
            }
        raise ChannelError("this platform has no history adapter")

    async def member_info(self, event: Any, user_id: str) -> dict:
        user_id = user_id or str(event.get_sender_id())
        if platform_name(event) == "qq":
            result = await self.action(
                event,
                "get_group_member_info",
                group_id=event.get_group_id(),
                user_id=user_id,
                no_cache=True,
            )
            keys = (
                "user_id",
                "nickname",
                "card",
                "role",
                "join_time",
                "last_sent_time",
            )
            return {key: field(result, key) for key in keys}
        if platform_name(event) == "telegram":
            chat_id = int(scope(event).split("#")[0])
            result = await self.telegram_call(
                event, "get_chat_member", chat_id=chat_id, user_id=int(user_id)
            )
            user = result.user
            return {
                "user_id": user.id,
                "username": user.username,
                "name": " ".join(filter(None, (user.first_name, user.last_name))),
                "status": str(result.status),
            }
        raise ChannelError("this platform has no member adapter")
