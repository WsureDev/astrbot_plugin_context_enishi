from __future__ import annotations

import time
from typing import Any

from ..channels.content_reader import qq_message_payload, read_content
from ..channels.messages import local_record, qq_record, telegram_record, unix_time
from ..channels.native import ImageRef, field
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
        return qq_record(event, message)

    async def enrich_reply(self, event, part):
        part = dict(part)
        if platform_name(event) != "qq" or part.get("sender_id") or not part.get("id"):
            return part
        try:
            quoted = await self.action(event, "get_msg", message_id=part["id"])
            chat = str(field(quoted, "group_id", "") or field(quoted, "user_id", ""))
            if chat and chat != scope(event):
                return part
            mid = field(quoted, "message_id")
            if mid is not None and str(mid) != part["id"]:
                return part
            sender = field(quoted, "sender", {})
            if field(sender, "user_id") is not None:
                part["sender_id"] = str(field(sender, "user_id"))
            name = field(sender, "card") or field(sender, "nickname")
            if name:
                part["sender_nickname"] = str(name)
        except ChannelError:
            pass
        return part

    async def message(self, event: Any, message_id: str) -> dict:
        if platform_name(event) == "qq":
            payload = await qq_message_payload(self, event, message_id)
            result = self._render_qq_message(event, payload)
            for index, part in enumerate(result["message"]):
                if (
                    part["type"] != "Reply"
                    or part.get("sender_id")
                    or not part.get("id")
                ):
                    continue
                result["message"][index] = await self.enrich_reply(event, part)
                break
            return {"source": "napcat", **result}
        if platform_name(event) == "telegram":
            raw = field(event.message_obj, "raw_message")
            message = field(raw, "message") or field(raw, "effective_message")
            for candidate in (message, field(message, "reply_to_message")):
                if str(field(candidate, "message_id", "")) != str(message_id):
                    continue
                return {
                    "source": "telegram_payload",
                    **telegram_record(event, candidate),
                }
            raise ChannelError(
                "Telegram Bot API cannot fetch arbitrary message IDs; this message is absent from the current payload"
            )
        raise ChannelError("this platform has no message adapter")

    async def message_content(self, event, content_id):
        return await read_content(self, event, content_id)

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
                    stamp = unix_time(created)
                    if stamp is not None and stamp < cutoff:
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
                    results.append(local_record(record))
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
            output = {key: field(result, key) for key in keys}
            if output["user_id"] is not None:
                output["user_id"] = str(output["user_id"])
            return output
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
                "status": str(result.status),
            }
        raise ChannelError("this platform has no member adapter")
