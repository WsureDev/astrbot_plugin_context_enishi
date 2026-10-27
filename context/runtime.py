from __future__ import annotations

import functools
import inspect
from contextvars import ContextVar
from dataclasses import replace
from weakref import WeakKeyDictionary, ref

from ..channels.media import TRANSFER_CONSUMER, TRANSFER_CONTEXT
from ..channels.native import field
from ..settings import MODE_REFERENCE_ONLY
from .compatibility import LegacyBatchDelivery
from .history import budget as history_budget

BACKGROUND_IMAGES = ContextVar("channel_media_background_images", default=None)
BACKGROUND_RUN = ContextVar("channel_media_background_run", default=False)


async def scoped_iterator(iterator, variable, value):
    try:
        while True:
            token = variable.set(value)
            try:
                item = await anext(iterator)
            except StopAsyncIteration:
                return
            finally:
                variable.reset(token)
            yield item
    finally:
        token = variable.set(value)
        try:
            await iterator.aclose()
        finally:
            variable.reset(token)


class RunnerIntegration:
    """Reversible runtime wrappers; production source files are never edited."""

    def __init__(self, owner):
        self.owner = owner
        self.patches = []
        self.active = True
        self.generation = None
        self.delivery = LegacyBatchDelivery(owner)
        self.instances = WeakKeyDictionary()

    def install(self, runner_class, executor_class=None):
        if self.patches:
            return
        reset = runner_class.reset
        transition = runner_class._transition_state
        sanitize = runner_class._sanitize_contexts_for_provider
        if not inspect.iscoroutinefunction(reset) or not all(
            callable(method) for method in (transition, sanitize)
        ):
            raise RuntimeError(
                "unsupported AstrBot runner; media isolation cannot be guaranteed"
            )
        previous = getattr(reset, "_channel_media_owner", None)
        if previous is not None and previous.active:
            raise RuntimeError(
                "another active context manager plugin instance owns the runner"
            )
        self.active = True
        self.delivery.active = True
        generation = self.generation = object()
        integration = self

        @functools.wraps(reset)
        async def wrapped_reset(runner, *args, **kwargs):
            result = await reset(runner, *args, **kwargs)
            if integration.current(generation):
                integration.attach_runner(runner, generation)
            return result

        wrapped_reset._channel_media_owner = self

        @functools.wraps(transition)
        def wrapped_transition(runner, *args, **kwargs):
            result = transition(runner, *args, **kwargs)
            if integration.current(generation) and runner.done():
                flow = getattr(integration.owner, "flow", None)
                if flow is not None:
                    flow.runner_finished(runner)
                if integration.owner.enabled_for_runner(runner):
                    integration.owner.policy.finish_sync(runner)
                else:
                    event = field(field(runner.run_context, "context"), "event")
                    if event is not None:
                        integration.owner.channels.release_transfers(
                            event, run_context=runner.run_context
                        )
            return result

        @functools.wraps(sanitize)
        def wrapped_sanitize(runner, messages):
            payload = sanitize(runner, messages)
            return (
                integration.owner.policy.sanitize_payload(runner, payload)
                if integration.current(generation)
                and integration.owner.enabled_for_runner(runner)
                else payload
            )

        # The existing monitor owns step, _iter_llm_responses and execute.
        # Keep those exact function objects, including across reload/unload.
        self._replace(runner_class, "reset", wrapped_reset)
        self._replace(runner_class, "_transition_state", wrapped_transition)
        self._replace(runner_class, "_sanitize_contexts_for_provider", wrapped_sanitize)
        handle = getattr(runner_class, "_handle_function_tools", None)
        if handle is not None:

            @functools.wraps(handle)
            async def wrapped_tools(runner, *args, **kwargs):
                iterator = scoped_iterator(
                    handle(runner, *args, **kwargs), BACKGROUND_IMAGES, None
                )
                iterator = scoped_iterator(
                    iterator, TRANSFER_CONTEXT, runner.run_context
                )
                try:
                    async for result in iterator:
                        yield result
                finally:
                    await iterator.aclose()

            self._replace(runner_class, "_handle_function_tools", wrapped_tools)
        if executor_class is not None:
            for name in ("_execute_local", "_execute_mcp"):
                if hasattr(executor_class, name):
                    self._wrap_tool_boundary(executor_class, name, generation)
            if hasattr(executor_class, "_execute_handoff_background"):
                self._wrap_background_submission(executor_class, generation)
            for name in ("_do_handoff_background", "_execute_background"):
                if hasattr(executor_class, name):
                    self._lease_background_worker(executor_class, name, generation)

    def current(self, generation):
        return self.active and self.generation is generation

    def attach_runner(self, runner, generation):
        manager = runner.request_context_manager
        process = manager.process
        integration = self
        history = history_budget.HistoryWindow(
            self.owner.settings, runner, self.owner.policy.text_factory
        )
        event = field(field(runner.run_context, "context"), "event")
        if event is not None:
            self.owner.channels.register_runner(event, runner.run_context)
        flow = getattr(self.owner, "flow", None)
        if flow is not None:
            flow.bind_runner(runner)

        @functools.wraps(process)
        async def wrapped_process(*args, **kwargs):
            if integration.current(generation) and integration.owner.enabled_for_runner(
                runner
            ):
                await integration.owner.policy.before_step(runner)
                messages = (
                    args[0]
                    if args
                    else kwargs.get("messages", runner.run_context.messages)
                )
                if history.apply(messages):
                    kwargs = {**kwargs, "trusted_token_usage": 0}
            return await process(*args, **kwargs)

        wrapped_process._channel_media_original = process
        manager.process = wrapped_process
        try:
            self.instances[manager] = ref(wrapped_process)
        except TypeError:
            # Some third-party managers are not weak-referenceable. Their
            # instance wrapper still becomes inert when the generation ends.
            pass

    def _wrap_tool_boundary(self, executor_class, name, generation):
        execute = inspect.getattr_static(executor_class, name).__func__
        integration = self

        @functools.wraps(execute)
        async def wrapped(cls, tool, run_context, **tool_args):
            event = field(field(run_context, "context"), "event")
            if (
                integration.current(generation)
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
                    token = TRANSFER_CONTEXT.set(run_context)
                    try:
                        for reference in references:
                            if str(reference).startswith(("qq://", "telegram://")):
                                path, _ = await integration.owner.channels.fetch_image(
                                    event, reference
                                )
                                converted.append(str(path))
                            else:
                                converted.append(reference)
                    finally:
                        TRANSFER_CONTEXT.reset(token)
                    if converted:
                        tool_args["reference_images"] = converted
                    tool_args["use_context_images"] = False
            iterator = execute(cls, tool, run_context, **tool_args)
            iterator = scoped_iterator(iterator, TRANSFER_CONTEXT, run_context)
            try:
                async for result in iterator:
                    if (
                        integration.current(generation)
                        and result is not None
                        and not field(result, "isError", False)
                        and BACKGROUND_RUN.get()
                        and integration.owner.enabled_event(event)
                        and integration.delivery.schedule(event, tool)
                    ):
                        result = integration.delivery.pending_notice(result)
                    yield result
            finally:
                await iterator.aclose()

        self._replace(executor_class, name, classmethod(wrapped))

    def reserve_images(self, event, args):
        sources = args.get("image_urls") or []
        if isinstance(sources, str):
            sources = [sources]
        prepared = BACKGROUND_IMAGES.get()
        if (
            prepared is not None
            and not prepared.started
            and not prepared.closed
            and prepared.event_key == id(event)
            and sources == prepared.image_urls
        ):
            return prepared
        lease = self.owner.channels.lease_background_images(event, sources)
        if lease.paths:
            args["image_urls"] = lease.image_urls
            return lease
        return None

    def prepare_background_call(self, event, tool, args):
        if not self.active or not isinstance(args, dict):
            return
        background = field(tool, "is_background_task", False) or (
            field(tool, "agent") is not None and args.get("background_task")
        )
        # Only our own ContextVar is changed. The tool generator scopes it to
        # each advance, and the SDK's created task inherits the lease.
        BACKGROUND_IMAGES.set(self.reserve_images(event, args) if background else None)

    def _wrap_background_submission(self, executor_class, generation):
        original = inspect.getattr_static(
            executor_class, "_execute_handoff_background"
        ).__func__
        integration = self

        @functools.wraps(original)
        async def wrapped(cls, tool, run_context, **args):
            event = field(field(run_context, "context"), "event")
            lease = (
                integration.reserve_images(event, args)
                if integration.current(generation)
                else None
            )
            iterator = scoped_iterator(
                original(cls, tool, run_context, **args), BACKGROUND_IMAGES, lease
            )
            accepted = False
            try:
                async for result in iterator:
                    accepted |= result is not None and not field(
                        result, "isError", False
                    )
                    yield result
            finally:
                await iterator.aclose()
                if lease is not None and not accepted and not lease.started:
                    lease.close()

        self._replace(
            executor_class, "_execute_handoff_background", classmethod(wrapped)
        )

    def _lease_background_worker(self, executor_class, name, generation):
        original = inspect.getattr_static(executor_class, name).__func__
        integration = self

        @functools.wraps(original)
        async def wrapped(cls, tool, run_context, *args, **kwargs):
            lease = BACKGROUND_IMAGES.get()
            if lease is None and integration.current(generation):
                event = field(field(run_context, "context"), "event")
                lease = integration.reserve_images(event, kwargs)
            if lease is not None:
                lease.started = True
            token = BACKGROUND_RUN.set(True)
            consumer = {"events": set()}
            consumer_token = TRANSFER_CONSUMER.set(consumer)
            try:
                return await original(cls, tool, run_context, *args, **kwargs)
            finally:
                BACKGROUND_RUN.reset(token)
                TRANSFER_CONSUMER.reset(consumer_token)
                integration.owner.channels.release_consumer(consumer)
                if lease is not None:
                    lease.close()

        self._replace(executor_class, name, classmethod(wrapped))

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
        local = name in vars(target)
        setattr(target, name, wrapper)
        self.patches.append((target, name, original, wrapper, local))

    def terminate(self):
        self.active = False
        self.delivery.detach()
        for manager, wrapper_ref in list(self.instances.items()):
            wrapper = wrapper_ref()
            if wrapper is not None and manager.process is wrapper:
                manager.process = wrapper._channel_media_original
        self.instances.clear()
        for target, name, original, wrapper, local in reversed(self.patches):
            if inspect.getattr_static(target, name) is wrapper:
                if local:
                    setattr(target, name, original)
                else:
                    delattr(target, name)
        self.patches.clear()
