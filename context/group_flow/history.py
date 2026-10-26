"""A reversible post-save hook. Provider responses alone never acknowledge a delta."""

from __future__ import annotations

import functools
import inspect
import logging

logger = logging.getLogger(__name__)


def contains_proof(history, proof):
    found = False
    for message in history or []:
        if (
            found
            and message.get("role") == "assistant"
            and (message.get("content") or message.get("tool_calls"))
        ):
            return True
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if content == proof:
            found = True
        if isinstance(content, list) and any(
            isinstance(part, dict)
            and part.get("type") == "text"
            and part.get("text") == proof
            for part in content
        ):
            found = True
    return False


class HistorySaveHook:
    def __init__(self, owner):
        self.owner = owner
        self.active = False
        self.target = self.original = self.wrapper = None
        self.own = False

    def install(self, manager):
        method = getattr(manager, "update_conversation", None)
        if not callable(method) or not inspect.iscoroutinefunction(method):
            return False
        signature = inspect.signature(method)
        if not {"unified_msg_origin", "conversation_id", "history"}.issubset(
            signature.parameters
        ):
            return False
        self.active = True
        self.target, self.original = manager, method
        self.own = "update_conversation" in vars(manager)
        hook = self

        @functools.wraps(method)
        async def after_save(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            result = await method(*args, **kwargs)
            if hook.active and bound.arguments.get("history") is not None:
                try:
                    scope = bound.arguments["unified_msg_origin"]
                    conversation = bound.arguments.get("conversation_id")
                    if not conversation:
                        conversation = await manager.get_curr_conversation_id(scope)
                    await hook.owner.saved(
                        scope, conversation, bound.arguments["history"]
                    )
                except Exception:
                    # A failed ledger acknowledgement must not turn a successful
                    # AstrBot history save into a failed model task.
                    logger.exception("Enishi could not acknowledge saved group flow")
            return result

        self.wrapper = after_save
        manager.update_conversation = after_save
        return True

    def terminate(self):
        self.active = False
        if self.target is not None and self.target.update_conversation is self.wrapper:
            if self.own:
                self.target.update_conversation = self.original
            else:
                delattr(self.target, "update_conversation")
