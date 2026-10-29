from __future__ import annotations

from ..channels.media import MediaSources
from ..channels.native import field
from ..identity import caption_data_directory
from ..observability import Diagnostics
from ..settings import MODE_DISABLED
from .components import prepare_component_context
from .group_flow.service import GroupFlow
from .media import MediaPolicy
from .runtime import RunnerIntegration
from .storage import CaptionCache


class ContextOrganizer:
    """Own model-message transformations, image policy and temporary-file lifecycle."""

    def __init__(self, context, settings, participates):
        self.context, self.settings, self.participates = context, settings, participates
        self.diagnostics = Diagnostics(settings.config)
        self.cache = self.channels = self.policy = None
        self.text_factory = None
        self.integration = RunnerIntegration(self)
        self.flow = GroupFlow(self)

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
        self.text_factory = text_factory
        self.cache = CaptionCache(caption_data_directory(data))
        self.channels = MediaSources(self.context, self.settings, [temporary])
        self.policy = MediaPolicy(
            self.context, self.settings, self.channels, text_factory, self.cache
        )
        try:
            self.flow.initialize(data / "flow")
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
        except BaseException:
            self.close()
            raise

    async def prepare_request(self, event, request):
        if (
            self.participates(event)
            and self.text_factory is not None
            and self.settings.awareness_for_event(event).enabled
        ):
            prepare_component_context(event, request, self.text_factory)
        if self.enabled_event(event):
            await self.policy.prepare_request(event, request)

    async def aclose(self):
        await self.flow.aclose()
        self.close()

    def close(self):
        self.flow.terminate()
        self.integration.terminate()
        if self.channels is not None:
            self.channels.release_transfers(preserve_consumers=True)
        if self.cache is not None:
            self.cache.close()
            self.cache = None
