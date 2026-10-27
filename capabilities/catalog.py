from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..channels.meta import cell
from ..channels.native import field as get
from .policy import META_NAMES


def unwrap(tool):
    seen = set()
    while id(tool) not in seen:
        seen.add(id(tool))
        wrapped = getattr(tool, "_wrapped", None)
        if wrapped is None:
            break
        tool = wrapped
    return tool


def tool_origin(tool, plugin_map=None):
    original = unwrap(tool)
    server = getattr(original, "mcp_server_name", None)
    if server:
        return "mcp:" + str(server)
    module = getattr(tool, "handler_module_path", None) or getattr(
        original, "handler_module_path", ""
    )
    metadata = (plugin_map or {}).get(module)
    if metadata is not None and getattr(metadata, "name", None):
        return "plugin:" + metadata.name
    if module and module.startswith("data.plugins."):
        return "plugin:" + module.split(".")[2]
    name = tool.name
    if any(part in name for part in ("file_", "upload_file", "download_file", "grep")):
        return "builtin:files"
    if any(part in name for part in ("execute_", "shell", "browser", "cua_")):
        return "builtin:computer"
    if name in {"send_message_to_user", "get_group_message_history"}:
        return "builtin:messaging"
    return "builtin:other"


@dataclass(frozen=True)
class Entry:
    id: str
    kind: str
    group: str
    name: str
    description: str
    revision: str
    value: object = field(compare=False, repr=False)
    identity: int = 0


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def estimate_tokens(text):
    chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
    return int(chinese * 0.6 + (len(text) - chinese) * 0.3)


def tool_entries(tool_set, plugin_map=None, *, freeze=False):
    entries = {}
    for tool in get(tool_set, "tools", []) or []:
        if tool.name in META_NAMES or not getattr(tool, "active", True):
            continue
        group = tool_origin(tool, plugin_map)
        key = group + "/" + tool.name
        identity = id(unwrap(tool))
        if freeze:
            tool = copy.copy(tool)
            tool.parameters = copy.deepcopy(tool.parameters)
        entries[key] = Entry(
            key,
            "tool",
            group,
            tool.name,
            tool.description or "",
            digest([tool.name, tool.description, tool.parameters]),
            tool,
            identity,
        )
    return entries


def group_entries(entries):
    groups = {}
    for entry in entries.values():
        groups.setdefault(entry.group, []).append(entry)
    return {
        key: Entry(
            key,
            "group",
            key,
            key.partition(":")[2],
            f"{len(children)} capabilities",
            digest([child.revision for child in children]),
            children,
        )
        for key, children in groups.items()
    }


def skill_entries(skills, *, local_metadata=False):
    entries = {}
    for skill in skills:
        source = str(get(skill, "source_type", "local"))
        owner = str(get(skill, "plugin_name", "") or source)
        group = "skill:" + source + ":" + owner
        name, path = str(get(skill, "name", "")), str(get(skill, "path", ""))
        if not name or not path:
            continue
        if source == "workspace":
            owner = str(PurePosixPath(path.replace("\\", "/")).parent.parent)
            group = "skill:" + source + ":" + owner
        stamp = None
        if local_metadata and source != "sandbox_only":
            try:
                stat = Path(path).stat()
                stamp = [stat.st_mtime_ns, stat.st_size]
            except OSError:
                pass
        key = group + "/" + name
        entries[key] = Entry(
            key,
            "skill",
            group,
            name,
            str(get(skill, "description", "") or ""),
            digest([path, get(skill, "description", ""), stamp]),
            skill,
        )
    return entries


def find(entries, query="", kind="", offset=0, limit=12):
    query = query.strip().casefold()
    terms = re.findall(r"[\w.-]+", query)
    if re.fullmatch(r"[\u4e00-\u9fff]{5,}", query):
        terms += [query[index : index + 2] for index in range(len(query) - 1)]
    matches = []
    for entry in entries.values():
        if kind and entry.kind != kind:
            continue
        exact = query in {
            entry.id.casefold(),
            entry.name.casefold(),
            entry.group.casefold(),
        }
        haystack = (entry.id + " " + entry.description).casefold()
        score = sum(1 for term in terms if term in haystack)
        if not query or exact or score:
            matches.append((int(exact), score, entry.id, entry))
    matches.sort(key=lambda row: (-row[0], -row[1], row[2]))
    offset, limit = max(0, offset), max(1, min(60, limit))
    return [row[3] for row in matches[offset : offset + limit]], len(matches)


def render(entries, *, offset=0, total=None, modes=None, states=None):
    if total is None:
        total = len(entries)
    has_skills = any(entry.kind == "skill" for entry in entries)
    fields = "FIELDS id|kind|group|name|description|mode"
    if states is not None:
        fields += "|status"
    if has_skills:
        fields += "|path"
    lines = [
        f"META source=enishi_catalog total={total} offset={offset}",
        fields,
    ]
    for entry in entries:
        values = [
            entry.id,
            entry.kind,
            entry.group,
            entry.name,
            entry.description,
            (modes or {}).get(entry.group, "folded"),
        ]
        if states is not None:
            values.append(states.get(entry.id, "folded"))
        if has_skills:
            values.append(get(entry.value, "path", "") if entry.kind == "skill" else "")
        lines.append("|".join(cell(value) for value in values))
    return "\n".join(lines)
