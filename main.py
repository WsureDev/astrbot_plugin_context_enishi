from __future__ import annotations

import json
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .flow import FlowIntegration
from .identity import PLUGIN, caption_data_directory
from .integration import RunnerIntegration
from .native import field
from .platforms import ChannelError, Channels
from .policy import MediaPolicy
from .settings import MODE_DISABLED, MODE_LABELS, Settings, platform_name
from .storage import CaptionCache


class ContextManagerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.settings = Settings(config)
        self.cache = None
        self.channels = None
        self.policy = None
        self.integration = RunnerIntegration(self)
        self.flow = FlowIntegration(self)

    async def initialize(self):
        data = Path(StarTools.get_data_dir(PLUGIN))
        temporary = Path(get_astrbot_temp_path())
        self.cache = CaptionCache(caption_data_directory(data))
        self.channels = Channels(self.context, self.settings, [temporary])
        self.policy = MediaPolicy(
            self.context, self.settings, self.channels, TextPart, self.cache
        )
        try:
            self.integration.install(ToolLoopAgentRunner, FunctionToolExecutor)
            from astrbot.core import astr_main_agent

            self.integration.defer_builtin_caption(astr_main_agent)
            try:
                from astrbot.core.platform.sources.telegram.tg_adapter import (
                    TelegramPlatformAdapter,
                )

                self.integration.preserve_telegram_ids(TelegramPlatformAdapter)
            except ImportError:
                logger.info("[%s] Telegram adapter is not installed", PLUGIN)
            self.flow.reconcile()
        except BaseException:
            self.flow.terminate()
            self.integration.terminate()
            self.cache.close()
            self.cache = None
            raise
        logger.info(
            "[%s] initialized; no incoming-image index or reception hook", PLUGIN
        )

    @filter.on_astrbot_loaded()
    async def attach_flow(self):
        self.flow.reconcile()

    @filter.on_plugin_loaded()
    async def attach_loaded_flow(self, metadata):
        if field(metadata, "name", metadata) == "astrbot_plugin_group_context_flow":
            self.flow.reconcile()

    @filter.on_plugin_unloaded()
    async def detach_unloaded_flow(self, metadata):
        if field(metadata, "name", metadata) == "astrbot_plugin_group_context_flow":
            self.flow.terminate()

    def enabled_event(self, event):
        plugins = getattr(event, "plugins_name", None)
        if plugins is not None and PLUGIN not in plugins:
            return False
        return (
            platform_name(event) in {"qq", "telegram"}
            and self.settings.for_event(event).mode != MODE_DISABLED
        )

    def enabled_for_runner(self, runner):
        event = field(field(runner.run_context, "context"), "event")
        return event is not None and self.enabled_event(event)

    @filter.on_llm_request(priority=-2_147_483_647)
    async def prepare_media(self, event: AstrMessageEvent, request):
        if self.enabled_event(event):
            await self.policy.prepare_request(event, request)

    @filter.on_agent_done(priority=-2_147_483_647)
    async def remove_persistent_pixels(
        self, event: AstrMessageEvent, run_context, response
    ):
        if self.enabled_event(event):
            # The state-transition guard covers error and aborted completion.
            await self.policy.finish_context(event, run_context)

    @filter.on_using_llm_tool(priority=-2_147_483_647)
    async def reserve_background_images(self, event: AstrMessageEvent, tool, tool_args):
        if self.enabled_event(event):
            self.integration.prepare_background_call(event, tool, tool_args)

    @filter.llm_tool(name="describe_channel_image")
    async def describe_channel_image(
        self,
        event: AstrMessageEvent,
        image_id: str,
        question: str = "描述图片内容，包括可见文字和重要细节。",
    ) -> str:
        """通过独立转述模型读取渠道原生图片 ID，主模型只接收文字，结果带缓存。

        Args:
            image_id(string): 图片标记里的完整 ID，或当前渠道的原生文件 ID。
            question(string): 想从图片中了解的问题；不同问题分别缓存。
        """
        try:
            return json.dumps(
                await self.policy.describe_image(event, image_id, question),
                ensure_ascii=False,
            )
        except (ChannelError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @filter.llm_tool(name="fetch_channel_image")
    async def fetch_channel_image(self, event: AstrMessageEvent, image_id: str) -> str:
        """每次从来源读取渠道原生图片 ID，提供本轮临时文件和元信息；不缓存原图，不向主模型返回像素。

        Args:
            image_id(string): 图片标记里的完整 ID，或当前渠道的原生文件 ID。
        """
        try:
            path, metadata = await self.channels.fetch_image(event, image_id)
            return json.dumps(
                {
                    "ok": True,
                    "image_id": image_id,
                    "path": str(path),
                    "temporary": True,
                    "valid_for": "current_turn",
                    **metadata,
                },
                ensure_ascii=False,
            )
        except (ChannelError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @filter.llm_tool(name="get_channel_user_history")
    async def get_channel_user_history(
        self,
        event: AstrMessageEvent,
        user_id: str = "",
        limit: int = 20,
        days: int = 1,
        before_id: str = "",
    ) -> str:
        """查询当前群中某人的历史发言。QQ 读取 NapCat，TG 仅查询已保存本地记录并标明覆盖范围。

        Args:
            user_id(string): 渠道原生用户 ID；空字符串查询所有人。
            limit(number): 返回条数，默认 20。
            days(number): 回溯天数，默认 1。
            before_id(string): QQ 分页游标；TG 的本地历史记录 ID，不是 Telegram 消息 ID。
        """
        try:
            return json.dumps(
                await self.channels.user_history(
                    event, user_id, limit, days, before_id
                ),
                ensure_ascii=False,
            )
        except (ChannelError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @filter.llm_tool(name="get_channel_user_avatar")
    async def get_channel_user_avatar(
        self, event: AstrMessageEvent, user_id: str = ""
    ) -> str:
        """取得某人的头像原生图片 ID，随后可转述或获取头像文件。

        Args:
            user_id(string): 渠道原生用户 ID，留空使用当前发言者。
        """
        try:
            ref = await self.channels.user_avatar(event, user_id)
            return json.dumps({"ok": True, "image_id": ref.uri}, ensure_ascii=False)
        except (ChannelError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @filter.llm_tool(name="get_channel_message")
    async def get_channel_message(
        self, event: AstrMessageEvent, message_id: str
    ) -> str:
        """按原生消息 ID 查询消息文字及图片 ID。QQ 使用 NapCat；TG 仅能读取当前载荷中的当前或引用消息。

        Args:
            message_id(string): 渠道原生消息 ID，不是 AstrBot 本地历史记录 ID。
        """
        try:
            return json.dumps(
                await self.channels.message(event, message_id), ensure_ascii=False
            )
        except (ChannelError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @filter.llm_tool(name="get_channel_member_info")
    async def get_channel_member_info(
        self, event: AstrMessageEvent, user_id: str = ""
    ) -> str:
        """查询当前群成员的昵称、名片和角色，使用渠道原生用户 ID。

        Args:
            user_id(string): 渠道原生用户 ID，留空使用当前发言者。
        """
        try:
            return json.dumps(
                await self.channels.member_info(event, user_id), ensure_ascii=False
            )
        except (ChannelError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @filter.command("渠道图片模式")
    async def show_policy(self, event: AstrMessageEvent):
        selected = self.settings.for_event(event)
        yield event.plain_result(
            f"渠道：{event.get_platform_id()}\n图片处理方式：{MODE_LABELS[selected.mode]}\n"
            f"转述模型：{selected.caption_provider_id or '使用 AstrBot 图片转述模型'}\n"
            f"备用转述模型：{', '.join(selected.caption_fallback_provider_ids) or '未选择'}"
        )

    async def terminate(self):
        self.flow.terminate()
        self.integration.terminate()
        if self.channels is not None:
            self.channels.release_transfers(preserve_consumers=True)
        if self.cache is not None:
            self.cache.close()
