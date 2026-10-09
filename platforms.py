from __future__ import annotations

import asyncio
import base64
import io
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakValueDictionary

import aiohttp
from PIL import Image, UnidentifiedImageError

from .native import (
    ImageRef,
    SourceImage,
    field,
    forward_ids,
    forward_nodes,
    node_content,
    normalize_segments,
    qq_images,
    telegram_images,
)
from .settings import Settings, platform_name, scope

MAX_CAPTION_BYTES = 16 * 1024 * 1024


class ChannelError(Exception):
    """A recoverable platform capability or retrieval failure."""


class Channels:
    def __init__(self, context: Any, settings: Settings, local_roots: list[Path]):
        self.context = context
        self.settings = settings
        self.local_roots = [p.resolve() for p in local_roots]
        self.timeout = settings.integer("request_timeout_seconds", 30, 1, 120)
        self.max_bytes = 20 * 1024 * 1024
        self._locks = WeakValueDictionary()
        self._transfers: dict[int, set[Path]] = {}

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

    async def current_images(self, event: Any) -> list[SourceImage]:
        """Resolve only this invocation's payload and references, never reception history."""
        raw = field(event.message_obj, "raw_message")
        platform = platform_name(event)
        images: list[SourceImage] = []
        if platform == "telegram":
            message = field(raw, "message") or field(raw, "effective_message")
            for component in field(event.message_obj, "message", []):
                native_id = field(component, "_channel_native_file_id", "")
                if native_id:
                    images.append(
                        SourceImage(
                            ImageRef(
                                "telegram",
                                str(event.get_platform_id()),
                                scope(event),
                                "image",
                                native_id,
                                field(component, "_channel_native_message_id", ""),
                            ),
                            origin="current",
                        )
                    )
            images.extend(telegram_images(message, event))
            reply = field(message, "reply_to_message")
            images.extend(telegram_images(reply, event, "reply"))
            external = field(message, "external_reply")
            images.extend(telegram_images(external, event, "external_reply"))
        elif platform == "qq":
            segments = normalize_segments(field(raw, "message", []))
            message_id = str(
                field(raw, "message_id", field(event.message_obj, "message_id", ""))
            )
            images.extend(qq_images(segments, event, message_id))
            replies = (
                [
                    str(field(field(s, "data", {}), "id", ""))
                    for s in segments
                    if str(field(s, "type", "")).lower() == "reply"
                ]
                if isinstance(segments, list)
                else []
            )
            if not replies:
                replies = [
                    str(field(c, "id", ""))
                    for c in field(event.message_obj, "message", [])
                    if str(field(c, "type", "")).lower()
                    in {"reply", "componenttype.reply"}
                ]
            provider_settings = self.context.get_config(
                umo=event.unified_msg_origin
            ).get("provider_settings", {})
            parser = provider_settings.get("quoted_message_parser", {}) or {}
            max_fetch = min(int(parser.get("max_forward_fetch", 32)), 128)
            max_depth = min(int(parser.get("max_forward_node_depth", 6)), 32)
            fetch_count = 0
            for reply_id in dict.fromkeys(replies):
                if not reply_id:
                    continue
                try:
                    quoted = await self.action(event, "get_msg", message_id=reply_id)
                except ChannelError:
                    continue
                images.extend(
                    qq_images(
                        field(quoted, "message", []),
                        event,
                        reply_id,
                        "reply",
                        max_depth=max_depth,
                    )
                )
                pending = forward_ids(field(quoted, "message", []), max_depth=max_depth)
                seen: set[str] = set()
                while pending and fetch_count < max_fetch:
                    fid = pending.pop(0)
                    if not fid or fid in seen:
                        continue
                    seen.add(fid)
                    fetch_count += 1
                    try:
                        payload = await self.action(
                            event, "get_forward_msg", message_id=fid
                        )
                    except ChannelError:
                        continue
                    for index, node in enumerate(forward_nodes(payload)):
                        content = node_content(node)
                        images.extend(
                            qq_images(
                                content,
                                event,
                                reply_id,
                                "reply_forward",
                                fid,
                                str(index),
                                max_depth=max_depth,
                            )
                        )
                        pending.extend(forward_ids(content, max_depth=max_depth))
        return list({image.ref.uri: image for image in images}.values())

    async def _qq_source(self, event: Any, ref: ImageRef) -> str:
        if ref.kind == "avatar":
            if not ref.native_id.isdigit():
                raise ChannelError("QQ avatar requires a numeric user ID")
            return f"https://q1.qlogo.cn/g?b=qq&nk={ref.native_id}&s=640"
        selected: list[SourceImage] = []
        if ref.forward_id:
            payload = await self.action(
                event, "get_forward_msg", message_id=ref.forward_id
            )
            nodes = forward_nodes(payload)
            if ref.node:
                positions = [int(v) for v in ref.node.split(".")]
                if (
                    not positions
                    or any(p < 0 for p in positions)
                    or positions[0] >= len(nodes)
                ):
                    raise ChannelError("forward node is unavailable")
                node = nodes[positions[0]]
                content = node_content(node)
                for index in positions[1:]:
                    if index >= len(content):
                        raise ChannelError("nested forward node is unavailable")
                    content = node_content(content[index])
                selected = qq_images(content, event, ref.message_id)
        elif ref.message_id or ref.kind == "message":
            payload = await self.action(
                event, "get_msg", message_id=ref.message_id or ref.native_id
            )
            actual_chat = str(
                field(payload, "group_id", "") or field(payload, "user_id", "")
            )
            if actual_chat and actual_chat != scope(event):
                raise ValueError("message belongs to another chat")
            selected = qq_images(field(payload, "message", []), event, ref.message_id)
        matching = next(
            (
                i
                for i in selected
                if ref.kind == "image" and i.ref.native_id == ref.native_id
            ),
            None,
        )
        if matching is None and ref.kind == "message" and ref.index < len(selected):
            matching = selected[ref.index]
        if matching and matching.url:
            return matching.url
        token = matching.ref.native_id if matching else ref.native_id
        result = await self.action(event, "get_image", file=token)
        value = str(field(result, "url", "") or field(result, "file", "") or "")
        if value.startswith(("http://", "https://")):
            return value
        encoded = field(result, "base64", "")
        if encoded:
            return "base64://" + encoded
        return value

    async def bytes_from_source(
        self, source: str, max_bytes: int | None = None
    ) -> bytes:
        limit = self.max_bytes if max_bytes is None else max_bytes
        if not isinstance(source, str) or not source:
            raise ChannelError("image source is unavailable")
        if source.startswith("data:image/"):
            if "," not in source:
                raise ChannelError("invalid image encoding")
            source = "base64://" + source.split(",", 1)[1]
        if source.startswith("base64://"):
            encoded = source[9:]
            if len(encoded) > limit * 4 // 3 + 8:
                raise ChannelError(
                    "图片大小达到或超过 16 MB，不进行模型转述"
                    if max_bytes is not None
                    else "image exceeds the size limit"
                )
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ChannelError("invalid image encoding") from exc
        elif source.startswith(("https://", "http://")):
            parsed = urlsplit(source)
            if parsed.username or parsed.password:
                raise ChannelError("credential-bearing image URL is unsupported")
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            ) as session:
                async with session.get(source) as response:
                    if response.status != 200:
                        raise ChannelError(
                            f"image download returned HTTP {response.status}"
                        )
                    if (
                        response.content_length is not None
                        and response.content_length > limit
                    ):
                        raise ChannelError(
                            "图片大小达到或超过 16 MB，不进行模型转述"
                            if max_bytes is not None
                            else "image exceeds the size limit"
                        )
                    chunks, total = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > limit:
                            raise ChannelError(
                                "图片大小达到或超过 16 MB，不进行模型转述"
                                if max_bytes is not None
                                else "image exceeds the size limit"
                            )
                        chunks.append(chunk)
                    data = b"".join(chunks)
        else:
            path = Path(source.removeprefix("file://")).resolve()
            if not any(path.is_relative_to(root) for root in self.local_roots):
                raise ChannelError(
                    "protocol returned a local path not shared with AstrBot"
                )
            if not path.is_file():
                raise ChannelError("local image is missing")
            if path.stat().st_size > limit:
                raise ChannelError(
                    "图片大小达到或超过 16 MB，不进行模型转述"
                    if max_bytes is not None
                    else "image exceeds the size limit"
                )
            data = path.read_bytes()
        if not data:
            raise ChannelError("empty image")
        if len(data) > limit:
            raise ChannelError(
                "图片大小达到或超过 16 MB，不进行模型转述"
                if max_bytes is not None
                else "image exceeds the size limit"
            )
        return data

    @staticmethod
    def image_metadata(data: bytes) -> dict:
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 40_000_000:
                    raise ChannelError("image dimensions are too large")
                mime = Image.MIME.get(image.format, "application/octet-stream")
                extension = {
                    "JPEG": ".jpg",
                    "PNG": ".png",
                    "WEBP": ".webp",
                    "GIF": ".gif",
                }.get(image.format, ".img")
                image.verify()
                return {
                    "width": image.width,
                    "height": image.height,
                    "mime_type": mime,
                    "extension": extension,
                    "bytes": len(data),
                }
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
        ) as exc:
            raise ChannelError("invalid or unsafe image") from exc

    async def read_image(
        self, event: Any, value: str, max_bytes: int | None = None
    ) -> tuple[bytes, dict]:
        ref = ImageRef.parse(value, event)
        limit = self.max_bytes if max_bytes is None else max_bytes
        key = ref.uri
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                if ref.platform == "telegram":
                    media = await self.telegram_call(
                        event, "get_file", file_id=ref.native_id
                    )
                    if field(media, "file_size", 0) and media.file_size > limit:
                        raise ChannelError(
                            "图片大小达到或超过 16 MB，不进行模型转述"
                            if max_bytes is not None
                            else "Telegram image exceeds the size limit"
                        )
                    data = bytes(
                        await asyncio.wait_for(
                            media.download_as_bytearray(), self.timeout
                        )
                    )
                elif ref.platform == "qq":
                    try:
                        source = await self._qq_source(event, ref)
                    except ChannelError:
                        if ref.kind != "image":
                            raise
                        result = await self.action(
                            event, "get_image", file=ref.native_id
                        )
                        source = str(
                            field(result, "url", "") or field(result, "file", "") or ""
                        )
                        if field(result, "base64"):
                            source = "base64://" + result["base64"]
                    data = await self.bytes_from_source(source, max_bytes=max_bytes)
                else:
                    raise ChannelError("this platform has no native image adapter")
                if len(data) > limit:
                    raise ChannelError(
                        "图片大小达到或超过 16 MB，不进行模型转述"
                        if max_bytes is not None
                        else "image exceeds the size limit"
                    )
                return data, self.image_metadata(data)
            except ChannelError:
                raise
            except Exception as exc:
                raise ChannelError(
                    f"native image retrieval failed ({type(exc).__name__})"
                ) from exc

    async def fetch_image(self, event: Any, value: str) -> tuple[Path, dict]:
        """Materialize a fresh transfer file for this turn, with no reuse cache."""
        data, metadata = await self.read_image(event, value)
        if not self.local_roots:
            raise ChannelError("temporary file directory is unavailable")
        directory = self.local_roots[0]
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=directory,
            prefix="channel_media_transfer_",
            suffix=metadata["extension"],
            delete=False,
        ) as output:
            output.write(data)
            path = Path(output.name)
        self._transfers.setdefault(id(event), set()).add(path)
        tracker = getattr(event, "track_temporary_local_file", None)
        if callable(tracker):
            tracker(str(path))
        return path, metadata

    def release_transfers(self, event: Any = None):
        keys = [id(event)] if event is not None else list(self._transfers)
        for key in keys:
            for path in self._transfers.pop(key, set()):
                path.unlink(missing_ok=True)

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
