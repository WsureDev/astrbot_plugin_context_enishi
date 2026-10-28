from __future__ import annotations

import json

from ..channels.native import field
from .catalog import estimate_tokens
from .skill_source import read_skill


async def load(run, targets):
    if run.closed or not run.service.enabled(run.event):
        raise ValueError("此能力会话已经结束")
    staged = []
    for target in targets:
        if target.startswith("content:"):
            payload = await run.service.io(run.service.artifacts.read, run.key, target)
            if payload["owner"] not in run.entries:
                raise ValueError("内容来源在当前会话中不可用")
            staged.append((target, None, target, payload))
            continue
        entries = run.resolve(target)
        for entry in entries:
            if entry.id in run.authoritative and (
                entry.id not in run.service.entries
                or entry.kind == "tool"
                and (
                    run.service.entries[entry.id].identity != entry.identity
                    or run.service.entries[entry.id].revision != entry.revision
                )
            ):
                raise ValueError("工具来源已卸载或停用，请重新查询目录")
            if entry.kind == "skill":
                try:
                    content = await read_skill(
                        run.service.context, run.event, entry.value
                    )
                except (ValueError, OSError) as exc:
                    run.service.failure("Skill read [" + entry.id + "]", exc)
                    raise
                ref = await run.service.io(
                    run.service.artifacts.put,
                    run.key,
                    entry.id,
                    content,
                    kind="skill",
                    name=entry.name,
                    path=str(field(entry.value, "path", "")),
                )
                payload = await run.service.io(run.service.artifacts.read, run.key, ref)
                staged.append((target, entry, ref, payload))
            else:
                staged.append((target, entry, None, None))
    if run.closed or not run.service.enabled(run.event):
        raise ValueError("此能力会话已经结束")
    if run.context_limit > 0:
        selected = {
            entry.name: entry.value
            for entry in run.entries.values()
            if entry.kind == "tool" and run.visible(entry)
        }
        resources = dict(run.resources.expanded)
        for _, entry, ref, payload in staged:
            if entry is not None and entry.kind == "tool":
                selected[entry.name] = entry.value
            if ref is not None:
                resources[ref] = payload
        tools = run.service.tool_set_class([*run.meta_tools, *selected.values()])
        required = estimate_tokens(
            json.dumps(tools.openai_schema(), ensure_ascii=False)
        )
        required += sum(
            estimate_tokens(json.dumps(payload["content"], ensure_ascii=False))
            for payload in resources.values()
        )
        if required > run.context_limit * 0.82:
            raise ValueError(
                "所选完整定义和原文超过当前模型的上下文预算，请减少同时展开的项目；原文没有被截断"
            )
    result = []
    for target, entry, ref, payload in staged:
        if entry is not None:
            run.session.load(entry.id, entry.revision)
            if target == entry.group:
                run.session.suppressed.discard(entry.group)
            run.loaded_this_run.add(entry.id)
            run.held.add(entry.id)
            if target == entry.group:
                run.session.load(target)
                run.loaded_this_run.add(target)
            item = {"id": entry.id, "expanded": True}
            if ref is not None:
                previous = run.session.artifacts.get(entry.id)
                if previous is not None and previous != ref:
                    run.session.leases.pop(previous, None)
                    run.session.expanded_refs.discard(previous)
                    run.resources.expanded.pop(previous, None)
                run.session.artifacts[entry.id] = ref
                item["path"] = payload.get("path", "")
        else:
            item = {"id": ref, "expanded": True}
        if ref is not None:
            run.session.load(ref)
            run.session.expanded_refs.add(ref)
            run.loaded_this_run.add(ref)
            run.resources.expanded[ref] = payload
            run.session.resource_owners[ref] = payload["owner"]
            item["content_ref"] = ref
        result.append(item)
    run.refresh()
    await run.service.persist()
    return result


async def unload(run, targets):
    if run.closed or not run.service.enabled(run.event):
        raise ValueError("此能力会话已经结束")
    # Resolve all targets before making any changes.
    resolved = []
    for target in targets:
        if target.startswith("content:"):
            await run.service.io(run.service.artifacts.read, run.key, target)
            resolved.append((target, []))
        else:
            resolved.append((target, run.resolve(target)))
    for target, entries in resolved:
        if target.startswith("content:"):
            await run.service.io(run.service.artifacts.read, run.key, target)
            run.session.unload(target)
            run.session.expanded_refs.discard(target)
            run.resources.expanded.pop(target, None)
            continue
        for entry in entries:
            run.session.unload(entry.id)
            run.held.discard(entry.id)
            ref = run.session.artifacts.get(entry.id)
            if ref:
                run.session.unload(ref)
                run.session.expanded_refs.discard(ref)
                run.resources.expanded.pop(ref, None)
        if entries and target == entries[0].group:
            run.session.unload(target)
    run.refresh()
    await run.service.persist()
    return {"folded": targets}
