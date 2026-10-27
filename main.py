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
from .capabilities.service import CapabilityService
from .capabilities.web import CapabilityAPI
from .channels.native import field
from .channels.transport import ChannelError
from .context.additions import append_temporary_text
from .context.service import ContextOrganizer
from .identity import PLUGIN
from .settings import MODE_LABELS, Settings, migrate_channel_settings, platform_name

EARLY_GROUP_PRIORITY = 2_147_483_627
LAST_LLM_PRIORITY = -2_147_483_647
BOUNDARY_PRIORITY = -2_147_483_627


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
        self.capabilities = CapabilityService(context, config)
        self.capability_api = CapabilityAPI(context, self.capabilities)

    async def initialize(self):
        data = Path(StarTools.get_data_dir(PLUGIN))
        temporary = Path(get_astrbot_temp_path())
        await self.organizer.initialize(
            data, temporary, TextPart, ToolLoopAgentRunner, FunctionToolExecutor
        )
        try:
            await self.capabilities.initialize(
                data / "capabilities", ToolLoopAgentRunner, FunctionToolExecutor
            )
            self.capability_api.install()
        except BaseException:
            self.capability_api.close()
            try:
                await self.capabilities.close()
            finally:
                await self.organizer.aclose()
            raise
        logger.info("[%s] context organization and chat awareness initialized", PLUGIN)

    @filter.on_astrbot_loaded()
    async def attach_group_history_save(self):
        self.organizer.flow.attach_save_hook()
        self.capabilities.request_refresh()

    @filter.on_plugin_loaded()
    async def refresh_loaded_capabilities(self, metadata):
        self.capabilities.request_refresh()

    @filter.on_plugin_unloaded()
    async def refresh_unloaded_capabilities(self, metadata):
        self.capabilities.request_refresh()

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE, priority=EARLY_GROUP_PRIORITY
    )
    async def record_group_message(self, event: AstrMessageEvent):
        self.organizer.diagnostics.received(event)
        try:
            async with self.organizer.diagnostics.span("flow.record", event):
                await self.organizer.flow.record(event)
        except (ChannelError, OSError, ValueError):
            logger.exception("[%s] could not record group flow", PLUGIN)

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE, priority=LAST_LLM_PRIORITY
    )
    async def log_group_dispatch_complete(self, event: AstrMessageEvent):
        self.organizer.diagnostics.emit(
            "message.handlers_complete", event, wake=field(event, "is_wake", False)
        )

    @filter.on_decorating_result(priority=BOUNDARY_PRIORITY)
    async def mark_group_boundary(self, event: AstrMessageEvent):
        try:
            await self.organizer.flow.boundary(event)
        except (ChannelError, OSError, ValueError):
            logger.exception("[%s] could not mark group flow boundary", PLUGIN)

    def participates(self, event):
        plugins = getattr(event, "plugins_name", None)
        if plugins is not None and PLUGIN not in plugins:
            return False
        return platform_name(event) in {"qq", "telegram"}

    @filter.on_waiting_llm_request()
    async def log_waiting_request(self, event: AstrMessageEvent):
        self.capabilities.request_trace.waiting(event)
        self.organizer.diagnostics.emit("llm.waiting_for_session", event)

    @filter.on_llm_response()
    async def log_llm_response(self, event: AstrMessageEvent, response):
        self.organizer.diagnostics.emit(
            "llm.response",
            event,
            role=field(response, "role"),
            completion_chars=len(field(response, "completion_text", "") or ""),
            tool_calls=field(response, "tools_call_name", []),
        )

    @filter.on_llm_request(priority=LAST_LLM_PRIORITY)
    async def prepare_media(self, event: AstrMessageEvent, request):
        async with self.organizer.diagnostics.span("llm.request_hook", event):
            try:
                async with self.organizer.diagnostics.span("flow.prepare", event):
                    await self.organizer.flow.prepare(event, request, TextPart)
            except (ChannelError, OSError, ValueError):
                logger.exception("[%s] group flow injection stopped", PLUGIN)
            async with self.organizer.diagnostics.span("media.prepare", event):
                await self.organizer.prepare_request(event, request)
            async with self.organizer.diagnostics.span("awareness.hint", event):
                hint = await self.awareness.request_hint(event)
            if hint is not None:
                append_temporary_text(
                    request, hint, self.awareness.HINT_PREFIX, TextPart
                )
            self.organizer.diagnostics.emit(
                "llm.request_ready",
                event,
                history_messages=len(field(request, "contexts", []) or []),
                tools=len(field(field(request, "func_tool"), "tools", []) or []),
            )

    @filter.on_agent_done(priority=LAST_LLM_PRIORITY)
    async def remove_persistent_pixels(
        self, event: AstrMessageEvent, run_context, response
    ):
        self.organizer.diagnostics.emit(
            "agent.done",
            event,
            response_role=field(response, "role"),
            messages=len(field(run_context, "messages", []) or []),
        )
        if self.participates(event):
            # The state-transition guard covers error and aborted completion.
            await self.organizer.policy.finish_context(event, run_context)
            self.organizer.flow.finish(event, run_context, response)
        await self.capabilities.finish_context(run_context)

    @filter.on_using_llm_tool(priority=LAST_LLM_PRIORITY)
    async def reserve_background_images(self, event: AstrMessageEvent, tool, tool_args):
        if self.organizer.enabled_event(event):
            self.organizer.integration.prepare_background_call(event, tool, tool_args)

    @filter.llm_tool(name="describe_channel_image")
    async def describe_channel_image(
        self,
        event: AstrMessageEvent,
        image_ids: list[str],
        question: str = "描述图片内容，包括可见文字和重要细节。",
    ) -> str:
        """批量转述 1–6 张渠道图片，由独立视觉模型分析，主模型只接收文字，结果带缓存。

        建议把有关联的图片放在同一组批量总结，例如同一人连续发送的多张图、同一事件或连续截图。
        多图按输入顺序拼接，带 IMAGE 1–6 序号和间隔像素带，先逐图辨认再综合分析；超过 6 张请分组调用。

        完整转述仅用于本轮任务，后续历史保留原图片 ID，不写已转述占位；再次需要时按 ID 查询。

        Args:
            image_ids(array[string]): 1–6 个完整图片 ID，按希望分析的顺序排列；单图也传数组，不要省略原生 ID 的定位参数。
            question(string): 对这组图片的问题，可要求逐图解读、比较或联合总结；不同问题分别缓存。
        """
        try:
            return json.dumps(
                await self.organizer.policy.describe_images(event, image_ids, question),
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
            return self.awareness.message_text(
                event,
                await self.awareness.sources.user_history(
                    event, user_id, limit, days, before_id
                ),
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
            return self.awareness.message_text(
                event,
                await self.awareness.sources.message(event, message_id),
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
            return self.awareness.member_text(
                event,
                await self.awareness.sources.member_info(event, user_id),
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
            return self.awareness.participants_text(
                event,
                await self.awareness.participants(
                    event,
                    order=order,
                    limit=limit,
                    hours=hours,
                    start_time=start_time,
                    end_time=end_time,
                ),
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
            return self.awareness.environment_text(
                event, await self.awareness.environment(event, limit)
            )
        except (ChannelError, ValueError, TypeError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @filter.command("enishi_flow_status")
    async def group_flow_status(self, event: AstrMessageEvent):
        try:
            result = await self.organizer.flow.status(event)
            yield event.plain_result(
                "Enishi 群聊流水：\n"
                + "\n".join(f"{key}: {value}" for key, value in result.items())
            )
        except (ChannelError, ValueError, OSError) as exc:
            yield event.plain_result(str(exc))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("enishi_flow_clear")
    async def clear_group_flow(self, event: AstrMessageEvent):
        try:
            await self.organizer.flow.clear(event)
            yield event.plain_result(
                "已清空当前群的 Enishi 原始流水；AstrBot 会话历史保留。"
            )
        except (ChannelError, ValueError, OSError) as exc:
            yield event.plain_result(str(exc))

    async def terminate(self):
        self.capability_api.close()
        try:
            await self.capabilities.close()
        finally:
            await self.organizer.aclose()
