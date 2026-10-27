from __future__ import annotations

import hashlib
import json

from ..storage.files import read_json, write_json


class ArtifactStore:
    """Complete text/structured results, addressed within a single conversation."""

    def __init__(self, directory):
        self.directory = directory / "content"

    def put(
        self,
        session,
        owner,
        content,
        *,
        kind="tool_result",
        name="",
        original=None,
        path="",
    ):
        if not isinstance(content, (str, list, dict)):
            raise ValueError("只能归档文本和结构化工具结果")
        payload = dict(
            session=session, owner=owner, kind=kind, name=name, content=content
        )
        if original is not None:
            payload["original"] = original
        if path:
            payload["path"] = path
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        ref = hashlib.sha256(encoded.encode()).hexdigest()
        write_json(self.directory / (ref + ".json"), payload)
        return "content:" + ref

    def read(self, session, ref):
        if not ref.startswith("content:"):
            raise ValueError("未知内容引用")
        key = ref.removeprefix("content:")
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("无效内容引用")
        payload = read_json(self.directory / (key + ".json"), None)
        if payload is None or payload.get("session") != session:
            raise ValueError("内容引用不属于当前会话或已经不存在")
        return payload


def receipt(ref, owner):
    return json.dumps(
        {"enishi_content_ref": ref, "source": owner, "restore_with": "enishi_load"},
        ensure_ascii=False,
    )


def contains_pixels(content):
    if isinstance(content, str):
        return "data:image/" in content or "[工具图片转述]" in content
    if isinstance(content, list):
        return any(contains_pixels(item) for item in content)
    if isinstance(content, dict):
        mime = content.get("mimeType", content.get("mime_type", ""))
        if isinstance(mime, str) and mime.startswith("image/"):
            return True
        if content.get("type") in {"image", "image_url", "Image"}:
            return True
        return any(contains_pixels(value) for value in content.values())
    return False
