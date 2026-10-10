from __future__ import annotations

import asyncio
import base64
import io
import shutil
import tempfile
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakValueDictionary, finalize

import aiohttp
from PIL import Image, UnidentifiedImageError

from ..settings import Settings, platform_name, scope
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
from .transport import ChannelError, ChannelTransport

MAX_CAPTION_BYTES = 16 * 1024 * 1024
TRANSFER_CONTEXT = ContextVar("channel_media_transfer_context", default=None)
TRANSFER_CONSUMER = ContextVar("channel_media_transfer_consumer", default=None)


def delete_transfer_files(paths):
    for path in paths:
        path.unlink(missing_ok=True)


class TransferLease:
    """Background copies live until their actual consumer finishes."""

    def __init__(self, paths, image_urls, event_key):
        self.paths = paths
        self.image_urls = image_urls
        self.event_key = event_key
        self.started = False
        self.closed = False
        # A scheduled SDK task retains the ContextVar even if our worker
        # wrapper is removed before it starts. Its context then owns cleanup.
        self._cleanup = finalize(self, delete_transfer_files, paths)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._cleanup()


class MediaSources(ChannelTransport):
    def __init__(self, context: Any, settings: Settings, local_roots: list[Path]):
        super().__init__(context, settings)
        self.local_roots = [p.resolve() for p in local_roots]
        self.max_bytes = 20 * 1024 * 1024
        self._locks = WeakValueDictionary()
        self._transfers: dict[int, set[Path]] = {}
        self._transfer_owners: dict[Path, int | None] = {}
        self._active_consumers: set[int] = set()
        self._leased_paths = WeakValueDictionary()

    def register_runner(self, event, run_context):
        if event.get_extra("_channel_media_root_context") is None:
            event.set_extra("_channel_media_root_context", id(run_context))

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
        context = TRANSFER_CONTEXT.get()
        if field(field(context, "context"), "event") is not event:
            context = None
        consumer = TRANSFER_CONSUMER.get()
        if consumer is not None:
            consumer["events"].add(id(event))
            self._active_consumers.add(id(consumer))
        self._transfer_owners[path] = (
            id(consumer)
            if consumer is not None
            else id(context)
            if context is not None
            else None
        )
        tracker = getattr(event, "track_temporary_local_file", None)
        if (
            callable(tracker)
            and consumer is None
            and (
                context is None
                or event.get_extra("_channel_media_root_context") == id(context)
            )
        ):
            tracker(str(path))
        return path, metadata

    def release_transfers(
        self,
        event: Any = None,
        run_context: Any = None,
        preserve_consumers: bool = False,
    ):
        keys = [id(event)] if event is not None else list(self._transfers)
        for key in keys:
            self._release_owned(
                key,
                None if run_context is None else {None, id(run_context)},
                preserve_consumers,
            )

    def release_consumer(self, consumer):
        for key in consumer["events"]:
            self._release_owned(key, {id(consumer)})
        self._active_consumers.discard(id(consumer))

    def _release_owned(self, key, owners, preserve_consumers=False):
        paths = self._transfers.get(key, set())
        selected = {
            path
            for path in paths
            if (owners is None or self._transfer_owners.get(path) in owners)
            and (
                not preserve_consumers
                or self._transfer_owners.get(path) not in self._active_consumers
            )
        }
        for path in selected:
            path.unlink(missing_ok=True)
            self._transfer_owners.pop(path, None)
        paths.difference_update(selected)
        if not paths:
            self._transfers.pop(key, None)

    def lease_background_images(self, event: Any, sources: list[str]) -> TransferLease:
        originals = {p.resolve() for p in self._transfers.get(id(event), set())}
        copies, result = [], []
        try:
            for source in sources:
                source = str(source)
                path = (
                    Path(source.removeprefix("file://")).resolve()
                    if not source.startswith(
                        ("http://", "https://", "data:", "base64:")
                    )
                    else None
                )
                inherited = self._leased_paths.get(path)
                if path not in originals and (
                    inherited is None
                    or inherited.closed
                    or inherited.event_key != id(event)
                ):
                    result.append(source)
                    continue
                with tempfile.NamedTemporaryFile(
                    dir=path.parent,
                    prefix="channel_media_background_",
                    suffix=path.suffix,
                    delete=False,
                ) as output:
                    copy = Path(output.name)
                copies.append(copy)
                shutil.copyfile(path, copy)
                result.append(str(copy))
        except BaseException:
            for path in copies:
                path.unlink(missing_ok=True)
            raise
        lease = TransferLease(copies, result, id(event))
        for path in copies:
            self._leased_paths[path.resolve()] = lease
        return lease
