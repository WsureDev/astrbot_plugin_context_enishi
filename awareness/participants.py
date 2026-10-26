"""Participant facts and observed activity, independent of image context policies."""

from __future__ import annotations

import math
import time
import unicodedata
from dataclasses import dataclass
from dataclasses import field as data_field
from datetime import UTC, datetime

from ..channels.native import field
from ..channels.transport import ChannelError
from ..settings import platform_name, scope


def label(value, limit=80):
    return "".join(
        c for c in str(value or "") if not unicodedata.category(c).startswith("C")
    )[:limit]


def epoch(value):
    if isinstance(value, datetime):
        value = (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else 0.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def parse_time(value, default):
    if not str(value or "").strip():
        return default
    try:
        number = float(value)
    except (ValueError, TypeError):
        try:
            date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("时间需使用 Unix 秒或带时区的 ISO 8601 格式") from exc
        if date.tzinfo is None:
            raise ValueError("ISO 8601 时间必须包含时区")
        number = date.timestamp()
    if not math.isfinite(number) or number < 0:
        raise ValueError("时间戳必须是非负有限数值")
    return number


def current_speaker(event, now):
    if str(event.get_platform_name()).lower() == "cron":
        return dict(
            user_id=None,
            actor_type="system",
            display_name="后台或定时触发",
            last_spoke_at=None,
        )
    raw = field(event.message_obj, "raw_message", {})
    if platform_name(event) == "telegram":
        message = field(raw, "message") or field(raw, "effective_message") or {}
        sender_chat = field(message, "sender_chat")
        if sender_chat:
            return dict(
                user_id=None,
                actor_type="chat",
                display_name=label(field(sender_chat, "title")),
                last_spoke_at=epoch(field(message, "date")) or now,
            )
        user = field(message, "from_user") or field(message, "from", {})
        name = field(user, "full_name") or " ".join(
            filter(None, (field(user, "first_name"), field(user, "last_name")))
        )
        uid = str(field(user, "id") or event.get_sender_id())
        timestamp = epoch(field(message, "date")) or now
        key = event.get_extra("_current_platform_message_history_id")
        key = f"local:{key}" if key is not None else ""
    else:
        sender = field(raw, "sender", {})
        uid = str(field(sender, "user_id") or event.get_sender_id())
        name = field(sender, "card") or field(sender, "nickname")
        timestamp = epoch(field(raw, "time")) or now
        mid = field(raw, "message_id") or field(event.message_obj, "message_id")
        key = f"qq:{mid}" if mid else ""
    getter = getattr(event, "get_sender_name", None)
    name = name or (getter() if callable(getter) else "") or uid
    return dict(
        user_id=label(uid, 64),
        actor_type="user",
        display_name=label(name),
        last_spoke_at=timestamp,
        message_key=key,
    )


@dataclass
class ActivityScan:
    observations: list[dict] = data_field(default_factory=list)
    seen: set[str] = data_field(default_factory=set)
    pages: int = 0
    stop_reason: str = "scan_budget"
    warnings: list[str] = data_field(default_factory=list)


class ParticipantDirectory:
    def __init__(self, sources, settings):
        self.sources, self.settings = sources, settings

    async def chat_info(self, event):
        platform = platform_name(event)
        raw = field(event.message_obj, "raw_message", {})
        message = field(raw, "message") or field(raw, "effective_message") or {}
        chat = field(message, "chat", {}) if platform == "telegram" else {}
        info = dict(
            platform=platform,
            chat_id=scope(event),
            type="group" if event.get_group_id() else "private",
            name=label(field(chat, "title")),
            member_count=None,
        )
        if not event.get_group_id():
            return info
        try:
            if platform == "qq":
                result = await self.sources.action(
                    event,
                    "get_group_info",
                    group_id=event.get_group_id(),
                    no_cache=True,
                )
                if str(field(result, "group_id", event.get_group_id())) != str(
                    event.get_group_id()
                ):
                    raise ChannelError("group info belongs to another chat")
                info["name"] = label(field(result, "group_name"))
                count = field(result, "member_count")
                info["member_count"] = max(0, int(count)) if count is not None else None
            elif platform == "telegram":
                client = self.sources.client(event, telegram=True)
                chat_id = int(scope(event).split("#")[0])
                if callable(getattr(client, "get_chat", None)):
                    result = await self.sources.telegram_call(
                        event, "get_chat", chat_id=chat_id
                    )
                    if int(field(result, "id", chat_id)) != chat_id:
                        raise ChannelError("chat info belongs to another chat")
                    info["name"] = label(field(result, "title")) or info["name"]
                if callable(getattr(client, "get_chat_member_count", None)):
                    info["member_count"] = int(
                        await self.sources.telegram_call(
                            event, "get_chat_member_count", chat_id=chat_id
                        )
                    )
        except (ChannelError, ValueError, TypeError):
            info["info_available"] = False
        return info

    @staticmethod
    def _person(uid, name="", role=None, last=0, origin="observed"):
        return dict(
            user_id=label(uid, 64),
            display_name=label(name) or label(uid, 64),
            role=label(role, 32) or None,
            last_spoke_at=epoch(last) or None,
            observed_messages=None,
            origin=origin,
        )

    async def _roster(self, event):
        result = await self.sources.action(
            event, "get_group_member_list", group_id=event.get_group_id(), no_cache=True
        )
        if not isinstance(result, list):
            raise ChannelError("member list is not an array")
        people = {}
        for member in result:
            if str(field(member, "group_id", event.get_group_id())) != str(
                event.get_group_id()
            ):
                raise ChannelError("member list belongs to another chat")
            uid = str(field(member, "user_id", "") or "")
            if not uid:
                continue
            person = self._person(
                uid,
                field(member, "card") or field(member, "nickname"),
                field(member, "role"),
                field(member, "last_sent_time"),
                "napcat_roster",
            )
            person["nickname"] = label(field(member, "nickname"))
            person["group_card"] = label(field(member, "card"))
            people[uid] = person
        return people

    async def _scan(self, event, start, end, max_pages):
        scan = ActivityScan()
        platform = platform_name(event)
        manager = getattr(self.sources.context, "message_history_manager", None)
        if platform == "telegram" and manager is None:
            scan.stop_reason = "no_local_history"
            return scan
        cursor = ""
        for page in range(1, max_pages + 1):
            try:
                if platform == "qq":
                    params = dict(
                        group_id=event.get_group_id(),
                        count=100,
                        reverseOrder=True,
                        parse_mult_msg=False,
                        disable_get_url=True,
                        quick_reply=True,
                    )
                    if cursor:
                        params["message_seq"] = cursor
                    payload = await self.sources.action(
                        event, "get_group_msg_history", **params
                    )
                    records = field(payload, "messages", []) or []
                    if not isinstance(records, list):
                        raise ChannelError("history is not an array")
                else:
                    records = await manager.get(
                        platform_id=event.get_platform_id(),
                        user_id=event.unified_msg_origin,
                        page=page,
                        page_size=100,
                    )
                scan.pages = page
                if not records:
                    scan.stop_reason = "source_exhausted"
                    break
                page_times, new_records = [], 0
                for record in records:
                    if platform == "qq":
                        if str(field(record, "group_id", event.get_group_id())) != str(
                            event.get_group_id()
                        ):
                            raise ChannelError("history belongs to another chat")
                        mid = field(record, "message_id") or field(
                            record, "message_seq"
                        )
                        key = f"qq:{mid}" if mid else ""
                        sender = field(record, "sender", {})
                        uid = str(field(sender, "user_id", "") or "")
                        name = field(sender, "card") or field(sender, "nickname")
                        stamp = epoch(field(record, "time"))
                    else:
                        if (
                            str(field(record, "platform_id", event.get_platform_id()))
                            != str(event.get_platform_id())
                            or str(field(record, "user_id", event.unified_msg_origin))
                            != event.unified_msg_origin
                        ):
                            raise ChannelError("local history belongs to another scope")
                        mid = field(record, "id")
                        key = f"local:{mid}" if mid is not None else ""
                        uid = str(field(record, "sender_id", "") or "")
                        name = field(record, "sender_name", "")
                        stamp = epoch(field(record, "created_at"))
                        if field(field(record, "content", {}), "type") == "bot":
                            uid = ""
                    if stamp:
                        page_times.append(stamp)
                    if not key or key in scan.seen:
                        continue
                    scan.seen.add(key)
                    new_records += 1
                    if uid and uid != "bot" and stamp and start <= stamp <= end:
                        scan.observations.append(
                            dict(
                                user_id=uid, name=label(name), timestamp=stamp, key=key
                            )
                        )
                if not new_records:
                    scan.stop_reason = "stalled_source"
                    break
                if page_times and min(page_times) < start:
                    scan.stop_reason = "window_boundary"
                    break
                if platform == "telegram" and len(records) < 100:
                    scan.stop_reason = "source_exhausted"
                    break
                if platform == "qq":
                    oldest = min(
                        records, key=lambda record: epoch(field(record, "time"))
                    )
                    next_cursor = str(
                        field(oldest, "message_seq")
                        or field(oldest, "message_id")
                        or ""
                    )
                    if not next_cursor or next_cursor == cursor:
                        scan.stop_reason = "stalled_cursor"
                        break
                    cursor = next_cursor
            except Exception:
                scan.stop_reason = "source_error"
                scan.warnings.append("history_unavailable_or_scope_mismatch")
                break
        scan.observations.sort(key=lambda item: item["timestamp"], reverse=True)
        return scan

    async def participants(
        self, event, order="recent", limit=30, hours=24, start_time="", end_time=""
    ):
        if not event.get_group_id():
            raise ChannelError("群聊人员名单仅适用于群聊")
        platform = platform_name(event)
        if platform not in {"qq", "telegram"}:
            raise ChannelError("当前渠道不支持人员名单")
        if order not in {"recent", "frequency"}:
            raise ValueError("排序必须是 recent（最近发言）或 frequency（时段发言数）")
        selected = self.settings.awareness_for_event(event)
        limit = min(max(int(limit), 1), selected.max_participants)
        now = time.time()
        end = parse_time(end_time, now)
        if end > now + 5:
            raise ValueError("结束时间不能晚于当前时间")
        period = order == "frequency" or bool(start_time or end_time)
        hours = float(hours)
        if not math.isfinite(hours) or hours <= 0:
            raise ValueError("回溯小时数必须是正有限数值")
        start = parse_time(start_time, end - hours * 3600) if period else 0.0
        if start < 0 or start > end:
            raise ValueError("统计起始时间必须不晚于结束时间")
        chat = await self.chat_info(event)
        people, warnings, membership_complete = {}, [], False
        if platform == "qq":
            try:
                people = await self._roster(event)
                membership_complete = True
            except ChannelError:
                warnings.append("native_member_list_unavailable")
        if (
            membership_complete
            and chat["member_count"] is not None
            and len(people) != chat["member_count"]
        ):
            membership_complete = False
            warnings.append("roster_size_differs_from_member_count")
        if period:
            for person in people.values():
                person["native_last_spoke_at"] = person["last_spoke_at"]
                person["last_spoke_at"] = None
        scan = ActivityScan(stop_reason="native_recency")
        if period or platform == "telegram" or not membership_complete:
            scan = await self._scan(event, start, end, selected.scan_pages)
            for observation in scan.observations:
                uid = observation["user_id"]
                person = people.setdefault(uid, self._person(uid, observation["name"]))
                person["last_spoke_at"] = max(
                    person["last_spoke_at"] or 0, observation["timestamp"]
                )
                person["observed_messages"] = (person["observed_messages"] or 0) + 1
        current = current_speaker(event, now)
        uid = current.get("user_id")
        current_counted = False
        if (
            uid
            and uid not in {"bot", "0", "unknown", "cron"}
            and start <= current["last_spoke_at"] <= end
        ):
            person = people.setdefault(uid, self._person(uid, current["display_name"]))
            person["last_spoke_at"] = max(
                person["last_spoke_at"] or 0, current["last_spoke_at"]
            )
            key = current.get("message_key")
            if period and key and key not in scan.seen:
                person["observed_messages"] = (person["observed_messages"] or 0) + 1
                current_counted = True
            elif period and key in scan.seen:
                current_counted = any(
                    observation["key"] == key and observation["user_id"] == uid
                    for observation in scan.observations
                )
        if period:
            people = {
                uid: person
                for uid, person in people.items()
                if person["observed_messages"]
                or (
                    uid == current.get("user_id")
                    and start <= current["last_spoke_at"] <= end
                )
            }
        if membership_complete and any(
            person["origin"] != "napcat_roster" for person in people.values()
        ):
            membership_complete = False
            warnings.append("native_roster_does_not_include_all_observed_senders")
        ranked = sorted(
            people.values(),
            key=lambda person: (
                -(person["observed_messages"] or 0)
                if order == "frequency"
                else -(person["last_spoke_at"] or 0),
                -(person["last_spoke_at"] or 0),
                person["user_id"],
            ),
        )
        return dict(
            source="napcat"
            if platform == "qq"
            else "astrbot_local_and_current_payload",
            chat=chat,
            order=order,
            participants=ranked[:limit],
            total_known=len(ranked),
            has_more=len(ranked) > limit,
            membership_complete=membership_complete,
            complete=False,
            window=dict(
                start_time=start if period else None, end_time=end, timezone="UTC"
            ),
            activity=dict(
                pages=scan.pages,
                stop_reason=scan.stop_reason,
                counts_are_observed_only=True,
                current_message_counted=current_counted,
                time_basis="platform_message_time"
                if platform == "qq"
                else "local_record_created_at",
            ),
            warnings=[*warnings, *scan.warnings],
            coverage="NapCat native member snapshot; activity is limited to protocol-accessible scanned messages"
            if platform == "qq"
            else "Observed senders in existing local records and the current payload; Telegram Bot API cannot enumerate all group members or fetch arbitrary history",
        )
