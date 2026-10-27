from __future__ import annotations

import json
import time


def make_tools(run, tool_class, tool_set_class):
    async def find_handler(event, query="", kind="", offset=0, limit=12):
        return run.search(query, kind, offset, limit)

    async def load_handler(event, ids):
        started = time.perf_counter()
        try:
            return json.dumps(await run.load(ids), ensure_ascii=False)
        except (ValueError, OSError) as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        finally:
            run.session.counters["load_ms"] += (time.perf_counter() - started) * 1000
            run.service.persist_later()

    async def unload_handler(event, ids):
        try:
            return json.dumps(await run.unload(ids), ensure_ascii=False)
        except (ValueError, OSError) as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    ids = {
        "type": "object",
        "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
        "required": ["ids"],
    }
    run.meta_tools = [
        tool_class(
            name="enishi_find",
            description="Discover folded tools, groups and skills eligible in this conversation. Exact names can be loaded directly.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["", "group", "tool", "skill", "content"],
                    },
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 60},
                },
            },
            handler=find_handler,
        ),
        tool_class(
            name="enishi_load",
            description="Expand exact capability/group IDs or unique names; content: references restore full archived text. Skill IDs read the full SKILL.md. Tools are callable next step.",
            parameters=ids,
            handler=load_handler,
        ),
        tool_class(
            name="enishi_unload",
            description="Fold capability/group IDs or content references for this conversation. Complete content remains restorable.",
            parameters=ids,
            handler=unload_handler,
        ),
    ]
    run.injected = tool_set_class()
    run.refresh()
    return run.injected
