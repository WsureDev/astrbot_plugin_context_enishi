from __future__ import annotations

import time
from typing import Any

from ..channels.message_schema import (
    actor_type,
    message_projection,
    qq_message,
    telegram_message,
    timestamp,
)
from ..channels.native import ImageRef, field
from ..channels.relations import identifier, telegram_name
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
        return qq_message(event, message)

    async def enrich_reply(self, event, projection):
        """At most one lookup; unavailable authors stay unknown without failing the turn."""
        reply = projection.get("reply_to")
        if (
            platform_name(event) != "qq"
            or not reply
            or not reply.get("message_id")
            or reply.get("sender_id")
        ):
            return projection
        try:
            quoted = await self.action(event, "get_msg", message_id=reply["message_id"])
            actual_chat = str(
                field(quoted, "group_id", "") or field(quoted, "user_id", "")
            )
            if actual_chat and actual_chat != scope(event):
                raise ChannelError("quoted message belongs to another chat")
            quoted_id = identifier(field(quoted, "message_id"))
            if quoted_id is not None and quoted_id != reply["message_id"]:
                raise ChannelError("quoted message ID mismatch")
            sender = field(quoted, "sender", {})
            projection["reply_to"] = dict(
                reply,
                sender_id=identifier(field(sender, "user_id")),
                sender_name=reply.get("sender_name")
                or field(sender, "card")
                or field(sender, "nickname")
                or None,
            )
        except ChannelError:
            pass
        return projection

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
            payload = dict(payload)
            payload.setdefault("message_id", str(message_id))
            return await self.enrich_reply(
                event, self._render_qq_message(event, payload)
            )
        if platform_name(event) == "telegram":
            raw = field(event.message_obj, "raw_message")
            message = field(raw, "message") or field(raw, "effective_message")
            for candidate in (message, field(message, "reply_to_message")):
                if str(field(candidate, "message_id", "")) != str(message_id):
                    continue
                return telegram_message(event, candidate)
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
                    user_id=event.unified_msg_origin,
                    page=page,
                    page_size=100,
                )
                pages = page
                if not records:
                    exhausted = True
                    break
                for record in records:
                    if (
                        str(field(record, "platform_id", event.get_platform_id()))
                        != str(event.get_platform_id())
                        or str(field(record, "user_id", event.unified_msg_origin))
                        != event.unified_msg_origin
                    ):
                        raise ChannelError("local history belongs to another scope")
                    created = field(record, "created_at")
                    stamp = timestamp(created)
                    if stamp is not None and stamp < cutoff:
                        continue
                    local_id = str(field(record, "id", ""))
                    if (
                        before_id
                        and local_id.isdigit()
                        and int(local_id) >= int(before_id)
                    ):
                        continue
                    sender = identifier(field(record, "sender_id"))
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
                    projection = message_projection(event)
                    projection.update(
                        source="astrbot_local",
                        local_record_id=local_id,
                        sender_id=sender,
                        sender_name=field(record, "sender_name") or None,
                        actor_type=actor_type(
                            sender,
                            projection["self_id"],
                            field(content, "type") == "bot",
                        ),
                        timestamp=stamp,
                        time_basis="local_record_created" if stamp else "unknown",
                        text=text[:8000],
                        coverage="local record only; native message ID, relations and image IDs were not saved",
                    )
                    results.append(projection)
                if len(results) >= limit or len(records) < 100:
                    exhausted = len(records) < 100
                    break
            results.sort(key=lambda r: r["timestamp"] or 0, reverse=True)
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
            member = {key: field(result, key) for key in keys}
            member["user_id"] = identifier(member["user_id"])
            member["display_name"] = member["card"] or member["nickname"] or None
            return member
        if platform_name(event) == "telegram":
            chat_id = int(scope(event).split("#")[0])
            result = await self.telegram_call(
                event, "get_chat_member", chat_id=chat_id, user_id=int(user_id)
            )
            user = result.user
            return {
                "user_id": str(user.id),
                "username": user.username,
                "name": " ".join(filter(None, (user.first_name, user.last_name))),
                "display_name": telegram_name(user),
                "status": str(result.status),
            }
        raise ChannelError("this platform has no member adapter")
