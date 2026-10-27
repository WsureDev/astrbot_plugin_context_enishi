"""One scoped history injector, with generations and acknowledgements after save."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path

from ...channels.messages import event_meta, event_record
from ...channels.meta import (
    CURRENT_TAG,
    DELTA_TAG,
    META_RULES,
    current_table,
    known_message_ids,
    table,
)
from ...channels.native import field
from ...channels.transport import ChannelError
from ...storage.files import run_io
from .history import HistorySaveHook, contains_proof
from .replies import SelfReplies
from .store import FlowStore

logger = logging.getLogger(__name__)
EXTERNAL_FLOW = "astrbot_plugin_group_context_flow"


@dataclass
class Pending:
    scope: str
    conversation: str
    target: int
    generation: int
    proof: str
    created: float
    request_id: int
    event: object
    request: object
    runner: object = None
    task: object = None
    done_callback: object = None
    success: bool = False


class GroupFlow:
    def __init__(self, owner):
        self.owner = owner
        self.context, self.settings = owner.context, owner.settings
        self.store = None
        self.hook = HistorySaveHook(self)
        self.pending = {}
        self.requests = {}
        self.replies = SelfReplies()
        self.active = False
        self.blocked = {}
        self.locks = weakref.WeakValueDictionary()
        self.work = set()
        self.closed = False

    def initialize(self, directory: Path):
        self.closed = False
        self.store = FlowStore(directory)
        self.attach_save_hook()
        if not self.active:
            logger.warning(
                "Enishi group flow is unavailable: no supported conversation save hook"
            )

    def attach_save_hook(self):
        if not self.active and self.store is not None:
            self.active = self.hook.install(
                getattr(self.context, "conversation_manager", None)
            )

    async def _io(self, method, *args):
        if self.closed:
            raise ChannelError("Enishi 流水已卸载")
        return await run_io(method, *args, tasks=self.work)

    def conflict(self, event):
        getter = getattr(self.context, "get_registered_star", None)
        metadata = getter(EXTERNAL_FLOW) if callable(getter) else None
        selected = getattr(event, "plugins_name", None)
        if (
            metadata is not None
            and field(metadata, "activated", True)
            and (selected is None or EXTERNAL_FLOW in selected)
        ):
            target = field(metadata, "star_cls")
            if target is not None:
                config = field(metadata, "config", {}) or {}
                getter = getattr(target, "_cfg", None)
                enabled = config.get("flow_settings", config).get("enabled", True)
                if callable(getter):
                    enabled = getter("enabled", True)
                if enabled:
                    return "external Flow is still enabled in this scope"
        cfg = self.context.get_config(umo=event.unified_msg_origin) or {}
        if cfg.get("provider_ltm_settings", {}).get("group_icl_enable", False):
            return "AstrBot built-in group context injection is still enabled"
        return ""

    def enabled(self, event):
        if (
            not self.active
            or not event.get_group_id()
            or str(event.get_platform_name()).lower() == "cron"
        ):
            return False
        if not self.owner.participates(event) or not self.settings.flow_enabled(event):
            return False
        getter = getattr(event, "get_message_type", None)
        if callable(getter):
            message_type = getter()
            if str(field(message_type, "value", message_type)) != "GroupMessage":
                return False
        reason = self.conflict(event)
        scope = event.unified_msg_origin
        if reason:
            if self.blocked.get(scope) != reason:
                logger.warning("Enishi group flow stopped for %s: %s", scope, reason)
            self.blocked[scope] = reason
            return False
        self.blocked.pop(scope, None)
        return True

    async def record(self, event, *, force=False):
        if not self.enabled(event):
            return None
        record = event_record(event)
        config = self.settings.flow_options()
        self_id = str(field(event.message_obj, "self_id", "") or "")
        sender = str(record["sender"].get("user_id") or "")
        if self_id and sender == self_id:
            if (
                self.replies.matches(event.unified_msg_origin, record["message_str"])
                or not config["record_self_messages"]
            ):
                return None
        if (
            not force
            and not config["record_empty_messages"]
            and not (record["message_str"].strip() or record["message"])
        ):
            return None
        key = record["message_id"]
        if key:
            key = "native:" + key
        else:
            key = (
                "local:"
                + hashlib.sha256(
                    json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()
            )
        scope = event.unified_msg_origin
        lock = self.locks.get(scope)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[scope] = lock
        async with lock:
            if self.closed:
                return None
            return await self._io(
                self.store.append,
                scope,
                record,
                key,
                event_meta(event),
                config["max_log_records"],
            )

    async def prepare(self, event, request, text_factory):
        if not self.enabled(event) or not field(field(request, "conversation"), "cid"):
            return
        previous = self.requests.get(id(request))
        if previous in self.pending:
            pending = self.pending[previous]
            runner = pending.runner() if pending.runner else None
            if pending.request() is request and (
                pending.runner is None or runner is not None and not runner.done()
            ):
                return
            self._drop(previous)
        trigger = await self.record(event, force=True)
        if trigger is None:
            return
        scope, cid = event.unified_msg_origin, request.conversation.cid
        current = event_record(event)
        metadata = event_meta(event)
        window = await self._io(self.store.window, scope, cid, trigger)
        if self.closed or not self.active:
            return
        raw_contexts = field(request, "contexts", []) or []
        if isinstance(raw_contexts, str):
            raw_contexts = json.loads(raw_contexts)
        if not isinstance(raw_contexts, list) or not all(
            isinstance(message, dict) for message in raw_contexts
        ):
            raise ValueError("group flow requires a list of AstrBot context messages")
        previous_delta = getattr(request, "_enishi_flow_delta", None)
        contexts = [
            message for message in raw_contexts if message is not previous_delta
        ]
        seen = known_message_ids(contexts, metadata)
        entries = [
            entry
            for entry in window["records"]
            if not entry["record"].get("message_id")
            or entry["record"]["message_id"] not in seen
        ]
        options = self.settings.flow_options()
        skipped = window["missing"]
        if (
            options["max_delta_messages"]
            and len(entries) > options["max_delta_messages"]
        ):
            skipped += len(entries) - options["max_delta_messages"]
            entries = entries[-options["max_delta_messages"] :]
        records = [entry["record"] for entry in entries]
        delta_meta = dict(metadata)
        budget = options["max_delta_chars"]
        while records:
            if skipped:
                delta_meta["skipped_count"] = skipped
                delta_meta["coverage"] = (
                    "older messages skipped by retention or delta budget; they will not be replayed after this task is saved"
                )
            rendered = table(records, delta_meta, tag=DELTA_TAG)
            if not budget or len(rendered) <= budget:
                break
            records.pop(0)
            skipped += 1
        if skipped:
            delta_meta.update(
                skipped_count=skipped,
                coverage="older messages skipped by retention or delta budget; they will not be replayed after this task is saved",
            )
        if records or skipped:
            rendered = table(records, delta_meta, tag=DELTA_TAG)
            if budget and len(rendered) > budget:
                rendered = table(
                    [],
                    {
                        "skipped_count": skipped,
                        "coverage": "older messages skipped by the delta budget",
                    },
                    tag=DELTA_TAG,
                )
            added = dict(role="user", content=rendered)
            contexts.append(added)
            request._enishi_flow_delta = added
        else:
            request._enishi_flow_delta = None
        request.contexts = contexts
        current_meta = dict(metadata, source="enishi_live", injected_count=len(records))
        if not records and not skipped:
            current_meta["coverage"] = (
                "No additional group messages to inject from Enishi's own live log; "
                "only messages received while enabled are available."
            )
        proof = current_table(current, current_meta)
        parts = [
            part
            for part in field(request, "extra_user_content_parts", []) or []
            if not str(field(part, "text", "")).startswith("<" + CURRENT_TAG + ">\n")
        ]
        parts.append(text_factory(text=proof))
        request.extra_user_content_parts = parts
        system = str(field(request, "system_prompt", "") or "")
        if META_RULES not in system:
            request.system_prompt = system + "\n" + META_RULES
        self._prune()
        run_id = uuid.uuid4().hex
        try:
            request_reference = weakref.ref(request)
        except TypeError:

            def request_reference():
                return request

        self.pending[run_id] = Pending(
            scope,
            str(cid),
            trigger,
            window["generation"],
            proof,
            time.monotonic(),
            id(request),
            event,
            request_reference,
        )
        self.requests[id(request)] = run_id

    def _prune(self):
        for run_id, pending in list(self.pending.items()):
            runner = pending.runner() if pending.runner else None
            if (
                pending.request() is None
                or time.monotonic() - pending.created > 1800
                and (runner is None or runner.done())
            ):
                self._drop(run_id)

    def _drop(self, run_id):
        pending = self.pending.pop(run_id, None)
        if pending is not None:
            if self.requests.get(pending.request_id) == run_id:
                self.requests.pop(pending.request_id, None)
            task = pending.task() if pending.task else None
            if task is not None and pending.done_callback is not None:
                task.remove_done_callback(pending.done_callback)

    def bind_runner(self, runner):
        run_id = self.requests.get(id(runner.req))
        pending = self.pending.get(run_id)
        if pending is not None:
            pending.runner = weakref.ref(runner)
            object.__setattr__(runner.run_context, "_enishi_flow_run_id", run_id)
            task = asyncio.current_task()
            if task is not None:
                pending.task = weakref.ref(task)

                def completed(task):
                    if task.cancelled() or task.exception() is not None:
                        self._drop(run_id)

                pending.done_callback = completed
                task.add_done_callback(completed)

    def runner_finished(self, runner):
        state = field(runner, "_state")
        state_name = str(field(state, "name", state))
        if state_name == "ERROR" or getattr(runner, "was_aborted", lambda: False)():
            self._drop(getattr(runner.run_context, "_enishi_flow_run_id", None))

    def finish(self, event, run_context, response):
        self.replies.response(event, response)
        run_id = getattr(run_context, "_enishi_flow_run_id", None)
        pending = self.pending.get(run_id)
        if pending is None:
            return
        runner = pending.runner() if pending.runner else None
        aborted = runner is None or bool(
            getattr(runner, "was_aborted", lambda: False)()
        )
        if (
            aborted
            or field(response, "role") != "assistant"
            or not field(response, "completion_text")
        ):
            self._drop(run_id)
            return
        pending.success = True
        pending.created = time.monotonic()

    async def saved(self, scope, conversation, history):
        if not self.active or not conversation:
            return
        for run_id, pending in list(self.pending.items()):
            if (pending.scope, pending.conversation) != (
                scope,
                conversation,
            ) or not pending.success:
                continue
            if not contains_proof(history, pending.proof):
                continue
            current = await self.context.conversation_manager.get_curr_conversation_id(
                scope
            )
            if current == conversation:
                await self._io(
                    self.store.commit,
                    scope,
                    conversation,
                    pending.target,
                    pending.generation,
                )
            self._drop(run_id)

    async def boundary(self, event):
        if not self.enabled(event) or not event.get_extra("_clean_ltm_session", False):
            return
        text = field(event.message_obj, "message_str", "") or ""
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            text = getter()
        command = str(text).strip().lstrip("/").split(" ", 1)[0]
        if command not in {"reset", "new"}:
            return
        if event.get_extra("_enishi_flow_boundary_marked", False):
            return
        seq = await self.record(event, force=True)
        cid = await self.context.conversation_manager.get_curr_conversation_id(
            event.unified_msg_origin
        )
        if seq and cid:
            event.set_extra("_enishi_flow_boundary_marked", True)
            await self._io(self.store.boundary, event.unified_msg_origin, cid, seq)
            for run_id, pending in list(self.pending.items()):
                if pending.scope == event.unified_msg_origin:
                    self._drop(run_id)
            self.replies.remember(
                event.unified_msg_origin, "Conversation reset successfully."
            )
            self.replies.remember(event.unified_msg_origin, "New conversation created.")
            self.replies.remember(
                event.unified_msg_origin, "Switched to new conversation: " + cid[:4]
            )

    async def status(self, event):
        if not event.get_group_id() or self.store is None:
            raise ChannelError("群聊流水只支持群聊")
        cid = await self.context.conversation_manager.get_curr_conversation_id(
            event.unified_msg_origin
        )
        snapshot = await self._io(self.store.dump, event.unified_msg_origin)
        return dict(
            enabled=self.settings.flow_enabled(event),
            running=self.enabled(event),
            blocked=self.conflict(event),
            records=len(snapshot["records"]),
            latest_seq=snapshot["flow"]["last_seq"],
            cursor=max(snapshot["flow"]["floor"], snapshot["cursors"].get(cid, 0)),
            pending=sum(
                p.scope == event.unified_msg_origin for p in self.pending.values()
            ),
        )

    async def clear(self, event):
        if not event.get_group_id():
            raise ChannelError("群聊流水只支持群聊")
        await self._io(self.store.clear, event.unified_msg_origin)
        for run_id, pending in list(self.pending.items()):
            if pending.scope == event.unified_msg_origin:
                self._drop(run_id)

    def terminate(self):
        self.closed = True
        self.active = False
        self.hook.terminate()
        for run_id in list(self.pending):
            self._drop(run_id)
        self.requests.clear()

    async def aclose(self):
        self.terminate()
        if self.work:
            await asyncio.gather(*list(self.work), return_exceptions=True)
