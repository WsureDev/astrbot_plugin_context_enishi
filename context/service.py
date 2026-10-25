from __future__ import annotations

from ..channels.media import MediaSources
from ..channels.message_schema import current_message_metadata
from ..channels.native import field
from ..identity import caption_data_directory
from ..settings import MODE_DISABLED
from .additions import append_message_metadata
from .flow import FlowIntegration
from .media import MediaPolicy
from .runtime import RunnerIntegration
from .storage import CaptionCache


class ContextOrganizer:
    """Own model-message transformations, image policy and temporary-file lifecycle."""

    def __init__(self, context, settings, participates):
        self.context, self.settings, self.participates = context, settings, participates
        self.cache = self.channels = self.policy = None
        self.integration = RunnerIntegration(self)
        self.flow = FlowIntegration(self)

    def enabled_event(self, event):
        return (
            self.participates(event)
            and self.settings.for_event(event).mode != MODE_DISABLED
        )

    def enabled_for_runner(self, runner):
        event = field(field(runner.run_context, "context"), "event")
        return event is not None and self.participates(event)

    async def initialize(
        self, data, temporary, text_factory, runner_class, executor_class
    ):
        self.cache = CaptionCache(caption_data_directory(data))
        self.channels = MediaSources(self.context, self.settings, [temporary])
        self.policy = MediaPolicy(
            self.context, self.settings, self.channels, text_factory, self.cache
        )
        try:
            self.integration.install(runner_class, executor_class)
            from astrbot.core import astr_main_agent

            self.integration.defer_builtin_caption(astr_main_agent)
            try:
                from astrbot.core.platform.sources.telegram.tg_adapter import (
                    TelegramPlatformAdapter,
                )

                self.integration.preserve_telegram_ids(TelegramPlatformAdapter)
            except ImportError:
                pass
            self.flow.reconcile()
        except BaseException:
            self.close()
            raise

    async def prepare_request(self, event, request):
        if self.enabled_event(event):
            await self.policy.prepare_request(event, request)

    def prepare_metadata(self, event, request, text_factory):
        if (
            self.participates(event)
            and self.settings.for_event(event).message_metadata_enabled
        ):
            append_message_metadata(
                request, current_message_metadata(event), text_factory
            )

    def close(self):
        self.flow.terminate()
        self.integration.terminate()
        if self.channels is not None:
            self.channels.release_transfers(preserve_consumers=True)
        if self.cache is not None:
            self.cache.close()
            self.cache = None
