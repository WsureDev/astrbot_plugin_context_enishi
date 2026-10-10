"""Use an existing plugin's delivery entry point without replacing its code."""

from __future__ import annotations

import asyncio
import inspect

from ..channels.native import field


class BatchEvent:
    """An older batch must not consume or clear a newer batch on the same event."""

    def __init__(self, event, key, state):
        self.event, self.key, self.state = event, key, state

    def __getattr__(self, name):
        return getattr(self.event, name)

    def get_extra(self, key, default=None):
        return self.state if key == self.key else self.event.get_extra(key, default)

    def set_extra(self, key, value):
        if key != self.key:
            return self.event.set_extra(key, value)
        if self.event.get_extra(key) is self.state:
            self.event.set_extra(key, value)


class LegacyBatchDelivery:
    def __init__(self, owner):
        self.owner = owner
        self.active = True
        self.tasks = set()
        self.pending = {}

    def schedule(self, event, tool):
        if not self.active or field(tool, "name") != "comfyui_txt2img":
            return False
        getter = getattr(self.owner.context, "get_registered_star", None)
        metadata = getter("astrbot_plugin_comfyui_pro") if callable(getter) else None
        plugin = field(metadata, "star_cls")
        key = field(plugin, "_BATCH_WAIT_STATE_EXTRA")
        claim = field(plugin, "_claim_batch_wait_stage")
        if not key or not inspect.iscoroutinefunction(claim):
            return False
        state = event.get_extra(key)
        if isinstance(state, dict) and id(state) in self.pending:
            return not self.pending[id(state)].done()
        if (
            not isinstance(state, dict)
            or state.get("wait_started")
            or state.get("finished")
        ):
            return False
        proxy = BatchEvent(event, key, state)

        async def deliver():
            try:
                await claim(proxy)
            except Exception:
                from astrbot.api import logger

                logger.exception(
                    "[context_enishi] ComfyUI batch delivery bridge failed"
                )
            finally:
                self.pending.pop(id(state), None)

        task = asyncio.create_task(deliver(), name="context-enishi-comfy-batch")
        self.pending[id(state)] = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    @staticmethod
    def pending_notice(result):
        notice = (
            "\nImages are still pending. Background collection and delivery are active. "
            "Do not claim the images have been generated or sent, and do not resubmit the batch."
        )
        if isinstance(result, str):
            return result + notice
        if hasattr(result, "model_copy") and isinstance(field(result, "content"), list):
            from mcp.types import TextContent

            return result.model_copy(
                update={
                    "content": [
                        *result.content,
                        TextContent(type="text", text=notice.strip()),
                    ]
                }
            )
        return result

    def detach(self):
        # A submitted job belongs to its consumer. Let the existing Comfy wait
        # stage finish (or reach its own timeout) even if this plugin unloads.
        self.active = False
