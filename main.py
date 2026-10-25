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

from .awareness.service import ChatAwareness
from .channels.native import field
from .channels.transport import ChannelError
from .context.additions import append_temporary_text
from .context.service import ContextOrganizer
from .identity import PLUGIN
from .settings import MODE_LABELS, Settings, migrate_channel_settings, platform_name


class EnishiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        if migrate_channel_settings(config):
            save = getattr(config, "save_config", None)
            if callable(save):
                save()
        self.settings = Settings(config)
        self.organizer = ContextOrganizer(context, self.settings, self.participates)
        self.awareness = ChatAwareness(context, self.settings, self.participates)

    async def initialize(self):
        data = Path(StarTools.get_data_dir(PLUGIN))
        temporary = Path(get_astrbot_temp_path())
        await self.organizer.initialize(
            data, temporary, TextPart, ToolLoopAgentRunner, FunctionToolExecutor
        )
        logger.info("[%s] context organization and chat awareness initialized", PLUGIN)

    @filter.on_astrbot_loaded()
    async def attach_flow(self):
        self.organizer.flow.reconcile()

    @filter.on_plugin_loaded()
    async def attach_loaded_flow(self, metadata):
        if field(metadata, "name", metadata) == "astrbot_plugin_group_context_flow":
            self.organizer.flow.reconcile()

    @filter.on_plugin_unloaded()
    async def detach_unloaded_flow(self, metadata):
        if field(metadata, "name", metadata) == "astrbot_plugin_group_context_flow":
            self.organizer.flow.terminate()

    def participates(self, event):
        plugins = getattr(event, "plugins_name", None)
        if plugins is not None and PLUGIN not in plugins:
            return False
        return platform_name(event) in {"qq", "telegram"}

    @filter.on_llm_request(priority=-2_147_483_647)
    async def prepare_media(self, event: AstrMessageEvent, request):
        await self.organizer.prepare_request(event, request)
        self.organizer.prepare_metadata(event, request, TextPart)
        hint = await self.awareness.request_hint(event)
        if hint is not None:
            append_temporary_text(request, hint, self.awareness.HINT_PREFIX, TextPart)

    @filter.on_agent_done(priority=-2_147_483_647)
    async def remove_persistent_pixels(
        self, event: AstrMessageEvent, run_context, response
    ):
        if self.participates(event):
            # The state-transition guard covers error and aborted completion.
            await self.organizer.policy.finish_context(event, run_context)

    @filter.on_using_llm_tool(priority=-2_147_483_647)
    async def reserve_background_images(self, event: AstrMessageEvent, tool, tool_args):
        if self.organizer.enabled_event(event):
            self.organizer.integration.prepare_background_call(event, tool, tool_args)

    @filter.llm_tool(name="describe_channel_image")
    async def describe_channel_image(
        self,
        event: AstrMessageEvent,
        image_id: str,
        question: str = "描述图片内容，包括可见文字和重要细节。",
    ) -> str:
        """通过独立转述模型读取渠道原生图片 ID，主模型只接收文字，结果带缓存。

        完整转述仅用于本轮任务，后续历史保留图片 ID 与短占位；再次需要时按 ID 查询。

        Args:
            image_id(string): 图片标记里的完整 ID，或当前渠道的原生文件 ID。
            question(string): 想从图片中了解的问题；不同问题分别缓存。
        """
        try:
            return json.dumps(
                await self.organizer.policy.describe_image(event, image_id, question),
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
            path, metadata = await self.organizer.channels.fetch_image(event, image_id)
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
            self.awareness.require_enabled(event)
            return json.dumps(
                await self.awareness.sources.user_history(
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
            self.awareness.require_enabled(event)
            ref = await self.awareness.sources.user_avatar(event, user_id)
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
            self.awareness.require_enabled(event)
            return json.dumps(
                await self.awareness.sources.message(event, message_id),
                ensure_ascii=False,
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
            self.awareness.require_enabled(event)
            return json.dumps(
                await self.awareness.sources.member_info(event, user_id),
                ensure_ascii=False,
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

    @filter.llm_tool(name="get_group_participants")
    async def get_group_participants(
        self,
        event: AstrMessageEvent,
        order: str = "recent",
        limit: int = 30,
        hours: float = 24,
        start_time: str = "",
        end_time: str = "",
    ) -> str:
        """查询当前群的人员名单，默认按最近发言顺序；可按时段内观察到的发言数排序。

        QQ 使用 NapCat 成员快照与协议可读历史；TG 使用已有本地记录和当前消息，
        不能枚举全部群成员。统计受扫描预算限制，observed_messages 是已观察到的
        次数，不是完整发言数；结合 membership_complete、activity 和 coverage 使用。

        Args:
            order(string): recent 按最近发言，frequency 按时段发言次数从多到少。
            limit(number): 返回人数，默认 30，受当前渠道最大返回人数设置限制。
            hours(number): frequency 默认回溯 24 小时；显式起始时间优先。
            start_time(string): 可选 Unix 秒字符串或含时区的 ISO 8601 起始时间。
            end_time(string): 可选结束时间，格式同上；默认当前时间，不可使用未来时间。
        """
        try:
            return json.dumps(
                await self.awareness.participants(
                    event,
                    order=order,
                    limit=limit,
                    hours=hours,
                    start_time=start_time,
                    end_time=end_time,
                ),
                ensure_ascii=False,
            )
        except (ChannelError, ValueError, TypeError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @filter.llm_tool(name="get_chat_environment")
    async def get_chat_environment(
        self, event: AstrMessageEvent, limit: int = 8
    ) -> str:
        """按需查询当前聊天环境：群名、当前发言者、引用对象及少量最近活跃人员。

        数据仅来自当前渠道。TG 人员范围为已有记录中观察到的发言者，不是完整群名单。

        Args:
            limit(number): 最多列出多少位最近活跃人员，默认 8。
        """
        try:
            return json.dumps(
                await self.awareness.environment(event, limit), ensure_ascii=False
            )
        except (ChannelError, ValueError, TypeError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    async def terminate(self):
        self.organizer.close()
