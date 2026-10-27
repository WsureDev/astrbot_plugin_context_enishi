from __future__ import annotations

import json
from collections import defaultdict, deque

from ..channels.native import field
from .artifacts import contains_pixels, receipt
from .catalog import digest
from .state import Lease

MEDIA_TOOLS = {
    "describe_channel_image",
    "fetch_channel_image",
    "get_channel_user_avatar",
}


def plain(content):
    if isinstance(content, list):
        return [
            part.model_dump(mode="json") if hasattr(part, "model_dump") else part
            for part in content
        ]
    return content


def bindings(messages):
    names = {}
    for index, message in enumerate(messages):
        if field(message, "role") == "assistant":
            for call in field(message, "tool_calls", []) or []:
                names[field(call, "id")] = field(field(call, "function"), "name", "")
        if field(message, "role") == "tool":
            call_id = field(message, "tool_call_id")
            yield index, message, names.get(call_id, "")


def signature(message, name):
    return digest(
        [field(message, "tool_call_id"), name, plain(field(message, "content"))]
    )


class ResultArchive:
    def __init__(self, run):
        self.run = run
        self.complete_results = defaultdict(deque)
        self.bound = set()

    def allowed(self, call_id):
        entry = self.run.entries.get(self.run.calls.get(call_id))
        return entry is not None and entry.name not in MEDIA_TOOLS

    async def save(self, call_id, content, original=None):
        run = self.run
        owner = run.call_skills.get(call_id, run.calls[call_id])
        kind = "tool_result"
        if call_id in run.call_skills:
            kind = "skill_read"
        ref = await run.service.io(
            run.service.artifacts.put,
            run.key,
            owner,
            content,
            original=original,
            kind=kind,
        )
        run.session.artifacts[call_id] = ref
        run.session.resource_owners[ref] = owner
        stamp = run.session.completed
        if not run.closed:
            stamp += 1
        run.session.leases[ref] = Lease(stamp)
        run.loaded_this_run.add(ref)
        return ref

    async def capture_result(self, call_id, result):
        if not self.run.service.policy.archive_results or not self.allowed(call_id):
            return
        if hasattr(result, "model_dump"):
            original = result.model_dump(mode="json")
        else:
            original = result
        if contains_pixels(original):
            return
        if isinstance(result, str):
            text = result
        else:
            parts = field(result, "content")
            if not isinstance(parts, list):
                return
            if not parts and field(result, "structuredContent") is None:
                return
            texts = []
            for part in parts:
                if field(part, "type") == "text":
                    texts.append(field(part, "text", ""))
                elif field(part, "type") == "resource" and isinstance(
                    field(field(part, "resource"), "text"), str
                ):
                    texts.append(field(field(part, "resource"), "text"))
                else:
                    texts = None
                    break
            if texts is None or field(result, "structuredContent") is not None:
                text = json.dumps(original, ensure_ascii=False)
            else:
                text = "\n\n".join(texts)
        ref = await self.save(call_id, text, original)
        self.complete_results[call_id].append(ref)

    async def capture(self, messages):
        run = self.run
        if not run.service.policy.archive_results:
            return
        for index, message, name in bindings(messages):
            call_id = field(message, "tool_call_id")
            if not self.allowed(call_id) or index < run.call_boundaries.get(call_id, 0):
                continue
            key = signature(message, name)
            marker = (key, run.call_epochs.get(call_id, 0))
            if marker in self.bound:
                continue
            if key in run.session.result_refs and not self.complete_results[call_id]:
                self.bound.add(marker)
                continue
            content = plain(field(message, "content"))
            if not isinstance(content, (str, list, dict)) or contains_pixels(content):
                continue
            queue = self.complete_results[call_id]
            if queue:
                ref = queue.popleft()
            else:
                ref = await self.save(call_id, content)
            previous = run.session.result_refs.get(key)
            if previous is not None and previous != ref:
                # Reused call IDs plus identical previews still need distinct provenance.
                text = content
                if not isinstance(text, str):
                    text = json.dumps(text, ensure_ascii=False)
                replacement = text + "\n\n" + receipt(ref, run.calls[call_id])
                if isinstance(message, dict):
                    message["content"] = replacement
                else:
                    message.content = replacement
                key = signature(message, name)
            run.session.result_refs[key] = ref
            self.bound.add(marker)

    def reference(self, message, name):
        return self.run.session.result_refs.get(signature(message, name))

    def bind_receipt(self, message, name, ref):
        self.run.session.result_refs[signature(message, name)] = ref
