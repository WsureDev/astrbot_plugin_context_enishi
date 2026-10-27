from __future__ import annotations

import json

from ..channels.native import field
from .artifacts import receipt
from .results import ResultArchive, bindings


def set_content(message, content):
    if isinstance(message, dict):
        message["content"] = content
    else:
        message.content = content


class RunResources:
    def __init__(self, run):
        self.run = run
        self.ephemeral = []
        self.expanded = {}
        self.archive = ResultArchive(run)
        self.current_user = None
        self.attached = []
        self.part_ids = set()
        self.original_strings = {}

    def remove_ephemeral(self, messages):
        owned = {id(message) for message in self.ephemeral}
        messages[:] = [message for message in messages if id(message) not in owned]
        self.ephemeral.clear()
        for message in self.attached:
            content = field(message, "content")
            if isinstance(content, list):
                output = [part for part in content if id(part) not in self.part_ids]
                original = self.original_strings.get(id(message))
                if (
                    original is not None
                    and len(output) == 1
                    and field(output[0], "text") == original
                ):
                    set_content(message, original)
                else:
                    set_content(message, output)
        self.attached.clear()
        self.part_ids.clear()
        self.original_strings.clear()

    async def capture(self, messages):
        await self.archive.capture(messages)

    async def capture_result(self, call_id, result):
        await self.archive.capture_result(call_id, result)

    async def project(self, messages, message_factory):
        run = self.run
        self.remove_ephemeral(messages)
        await self.capture(messages)
        present = set()
        for _, message, name in bindings(messages):
            ref = self.archive.reference(message, name)
            if ref is None:
                continue
            present.add(ref)
            owner = run.session.resource_owners.get(ref, "")
            if not run.session.active(ref, run.idle_for_owner(owner)):
                if owner == "":
                    payload = await run.service.io(
                        run.service.artifacts.read, run.key, ref
                    )
                    owner = payload["owner"]
                replacement = receipt(ref, owner)
                current = field(message, "content")
                if not isinstance(current, str):
                    current = json.dumps(current, ensure_ascii=False, default=str)
                if (
                    len(current) > len(replacement)
                    or current == replacement
                    or ref in run.session.suppressed
                ):
                    set_content(message, replacement)
                    self.archive.bind_receipt(message, name, ref)
            elif ref in self.expanded:
                set_content(message, self.expanded[ref]["content"])
        for ref, payload in list(self.expanded.items()):
            if not run.session.active(ref, run.idle_for_owner(payload["owner"])):
                self.expanded.pop(ref, None)
                continue
            # A restored result also works after its original call was truncated.
            if ref in present:
                continue
            content = payload["content"]
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            location = ""
            if payload.get("path"):
                location = " path=" + json.dumps(payload["path"], ensure_ascii=False)
            text = f"<enishi_resource ref={json.dumps(ref)} source={json.dumps(payload['owner'])}{location}>\n{content}\n</enishi_resource>"
            message = message_factory(
                role="user",
                content=[
                    {
                        "type": "text",
                        "text": text,
                        "_no_save": True,
                    }
                ],
            )
            current = self.current_user
            if current is not None and any(item is current for item in messages):
                parts = field(message, "content")
                content = field(current, "content")
                if isinstance(content, str):
                    self.original_strings[id(current)] = content
                    content = field(
                        message_factory(
                            role="user", content=[{"type": "text", "text": content}]
                        ),
                        "content",
                    )
                set_content(current, [*(content or []), *parts])
                self.part_ids.update(id(part) for part in parts)
                if all(item is not current for item in self.attached):
                    self.attached.append(current)
                continue
            if isinstance(message, dict):
                message["_no_save"] = True
            else:
                message._no_save = True
            messages.append(message)
            self.ephemeral.append(message)
