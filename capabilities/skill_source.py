"""Read registered skills using the selected runtime's filesystem contract."""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath

from ..channels.native import field

DEFAULT_SANDBOX_BOOTER = "shipyard_neo"
NEO_WORKSPACE_ROOT = PurePosixPath("/workspace")
SKILL_FILENAME = "SKILL.md"


def neo_read_path(path):
    candidate = PurePosixPath(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(NEO_WORKSPACE_ROOT)
        except ValueError as exc:
            raise ValueError(
                f"Neo Skill 路径不在 {NEO_WORKSPACE_ROOT} 中：{path}"
            ) from exc
    if ".." in candidate.parts:
        raise ValueError(f"Neo Skill 路径包含未解析的上级目录：{path}")
    return candidate.as_posix()


def matches_skill_path(context, event, skill, path):
    registered = str(field(skill, "path", "")).replace("\\", "/")
    path = path.replace("\\", "/")
    if registered == path:
        return True
    config = context.get_config(umo=event.unified_msg_origin) or {}
    settings = config.get("provider_settings", {})
    if settings.get("computer_use_runtime") != "sandbox":
        return False
    if (
        settings.get("sandbox", {}).get("booter", DEFAULT_SANDBOX_BOOTER)
        != "shipyard_neo"
    ):
        return False
    try:
        return neo_read_path(registered) == PurePosixPath(path).as_posix()
    except ValueError:
        return False


async def read_skill(context, event, skill):
    config = context.get_config(umo=event.unified_msg_origin) or {}
    settings = config.get("provider_settings", {})
    runtime = settings.get("computer_use_runtime", "none")
    path = str(field(skill, "path", ""))
    if not path or path.replace("\\", "/").rsplit("/", 1)[-1] != SKILL_FILENAME:
        raise ValueError("Skill 没有有效的原生 SKILL.md 定位")
    if runtime in {"none", "local"} and field(skill, "source_type") != "sandbox_only":
        return await asyncio.to_thread(Path(path).read_text, encoding="utf-8-sig")
    if runtime == "none":
        raise ValueError("此 Skill 位于沙箱中，当前会话未启用沙箱运行环境")
    booter_type = settings.get("sandbox", {}).get("booter", DEFAULT_SANDBOX_BOOTER)
    read_path = path
    if booter_type == "shipyard_neo":
        read_path = neo_read_path(path)
    try:
        from astrbot.core.computer.computer_client import get_booter

        booter = await get_booter(context, event.unified_msg_origin)
        result = await booter.fs.read_file(
            read_path, encoding="utf-8", offset=None, limit=None
        )
        if result.get("success") is False or not isinstance(result.get("content"), str):
            raise ValueError(str(result.get("error") or "未返回完整文字内容"))
    except Exception as exc:
        raise ValueError(
            f"Skill 源文件读取失败（{runtime}/{booter_type}；源路径 {path}；"
            f"读取路径 {read_path}）：{type(exc).__name__}: {exc}。"
            "更换同一 Skill 的 ID 或名称不会改变读取路径。"
        ) from exc
    return result["content"]
