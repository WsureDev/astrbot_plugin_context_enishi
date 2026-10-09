from __future__ import annotations

import functools
import inspect
from dataclasses import replace

from .native import field
from .settings import MODE_REFERENCE_ONLY


class RunnerIntegration:
    """Reversible runtime wrappers; production source files are never edited."""

    def __init__(self, owner):
        self.owner = owner
        self.patches = []
        self.active = True

    def install(self, runner_class, executor_class=None):
        if self.patches:
            return
        step = runner_class.step
        sanitize = runner_class._sanitize_contexts_for_provider
        if not inspect.isasyncgenfunction(step) or not callable(sanitize):
            raise RuntimeError(
                "unsupported AstrBot runner; media isolation cannot be guaranteed"
            )
        previous = getattr(step, "_channel_media_owner", None)
        if previous is not None and previous.active:
            raise RuntimeError(
                "another active channel media plugin instance owns the runner"
            )
        self.active = True
        integration = self

        @functools.wraps(step)
        async def wrapped_step(runner):
            if integration.active and integration.owner.enabled_for_runner(runner):
                await integration.owner.policy.before_step(runner)
            iterator = step(runner)
            try:
                async for response in iterator:
                    yield response
            finally:
                await iterator.aclose()
                if (
                    integration.active
                    and integration.owner.enabled_for_runner(runner)
                    and runner.done()
                ):
                    await integration.owner.policy.finish(runner)
                elif integration.active and runner.done():
                    event = field(field(runner.run_context, "context"), "event")
                    if event is not None:
                        integration.owner.channels.release_transfers(event)

        wrapped_step._channel_media_owner = self

        @functools.wraps(sanitize)
        def wrapped_sanitize(runner, messages):
            payload = sanitize(runner, messages)
            return (
                integration.owner.policy.sanitize_payload(runner, payload)
                if integration.active and integration.owner.enabled_for_runner(runner)
                else payload
            )

        self._replace(runner_class, "step", wrapped_step)
        self._replace(runner_class, "_sanitize_contexts_for_provider", wrapped_sanitize)
        if executor_class is not None:
            execute = executor_class.execute

            @functools.wraps(execute)
            async def wrapped_execute(cls, tool, run_context, **tool_args):
                event = field(field(run_context, "context"), "event")
                if (
                    integration.active
                    and event is not None
                    and integration.owner.enabled_event(event)
                ):
                    if field(tool, "name") == "generate_image":
                        import mcp.types

                        implicit = tool_args.get("use_context_images", False)
                        implicit = implicit is True or str(implicit).lower() in {
                            "true",
                            "1",
                            "yes",
                        }
                        if implicit:
                            yield mcp.types.CallToolResult(
                                content=[
                                    mcp.types.TextContent(
                                        type="text",
                                        text=(
                                            "Channel media policy requires explicit references. Call fetch_channel_image for the desired image IDs, "
                                            "then call generate_image with reference_images=[returned paths] and use_context_images=false."
                                        ),
                                    )
                                ],
                                isError=True,
                            )
                            return
                        references = tool_args.get("reference_images") or []
                        if isinstance(references, str):
                            references = [references]
                        converted = []
                        for reference in references:
                            if str(reference).startswith(("qq://", "telegram://")):
                                path, _ = await integration.owner.channels.fetch_image(
                                    event, reference
                                )
                                converted.append(str(path))
                            else:
                                converted.append(reference)
                        if converted:
                            tool_args["reference_images"] = converted
                        tool_args["use_context_images"] = False
                iterator = execute(tool, run_context, **tool_args)
                try:
                    async for result in iterator:
                        yield result
                finally:
                    await iterator.aclose()

            self._replace(executor_class, "execute", classmethod(wrapped_execute))

    def preserve_telegram_ids(self, adapter_class):
        original = adapter_class.convert_message
        integration = self

        @functools.wraps(original)
        async def wrapped(adapter, update, context, get_reply=True):
            message = await original(adapter, update, context, get_reply)
            if message is not None and integration.active:
                raw = field(update, "message")
                photo = field(raw, "photo") or []
                media = photo[-1] if photo else field(raw, "document")
                sticker = field(raw, "sticker")
                if sticker:
                    media = (
                        field(sticker, "thumbnail")
                        if field(sticker, "is_animated", False)
                        or field(sticker, "is_video", False)
                        else sticker
                    )
                for component in field(message, "message", []):
                    kind = str(field(component, "type", "")).lower()
                    if (
                        kind in {"image", "componenttype.image"}
                        and media
                        and field(media, "file_id")
                    ):
                        # Preserve metadata on the existing event component before
                        # album merging discards earlier Updates. No lookup index.
                        object.__setattr__(
                            component, "_channel_native_file_id", media.file_id
                        )
                        object.__setattr__(
                            component,
                            "_channel_native_message_id",
                            str(field(raw, "message_id", "")),
                        )
            return message

        self._replace(adapter_class, "convert_message", wrapped)

    def defer_builtin_caption(self, main_agent_module):
        original = main_agent_module._decorate_llm_request
        integration = self

        @functools.wraps(original)
        async def wrapped(event, req, plugin_context, config, provider=None):
            if integration.active and integration.owner.enabled_event(event):
                if (
                    integration.owner.settings.for_event(event).mode
                    == MODE_REFERENCE_ONLY
                ):
                    # Core may eagerly caption before on_llm_request when the
                    # main model lacks vision. Reference-only mode delegates to the
                    # on-demand tool and its cache, using a request-local copy.
                    settings = config.provider_settings or plugin_context.get_config(
                        umo=event.unified_msg_origin
                    ).get("provider_settings", {})
                    config = replace(
                        config,
                        provider_settings={
                            **settings,
                            "default_image_caption_provider_id": "",
                        },
                    )
            return await original(event, req, plugin_context, config, provider=provider)

        self._replace(main_agent_module, "_decorate_llm_request", wrapped)

    def _replace(self, target, name, wrapper):
        original = inspect.getattr_static(target, name)
        setattr(target, name, wrapper)
        self.patches.append((target, name, original, wrapper))

    def terminate(self):
        self.active = False
        for target, name, original, wrapper in reversed(self.patches):
            if inspect.getattr_static(target, name) is wrapper:
                setattr(target, name, original)
        self.patches.clear()
