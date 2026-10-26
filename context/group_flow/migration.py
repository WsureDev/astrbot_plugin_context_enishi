"""Scoped copy-in/copy-out for Flow 0.2.2; source workloads are never edited."""

from __future__ import annotations

import hashlib
import json
import re
import uuid

from ...channels.message_components import component
from ...channels.messages import event_meta
from ...channels.meta import components
from .files import atomic_text, read_json, write_json
from .store import flow_hash

IMAGE_MARKER = re.compile(r"\[(?:图片|Image) id=((?:qq|telegram)://[^\s\]]+)\]")


def external_id(event):
    return f"{event.get_platform_id()}:group:{event.get_group_id()}"


def imported_record(row):
    saved = row.get("enishi_record")
    if isinstance(saved, dict):
        return saved
    text = str(row.get("text") or "")
    images = [
        component("Image", file=match.group(1)) for match in IMAGE_MARKER.finditer(text)
    ]
    return dict(
        # Old Flow mixed native IDs with locally generated fallback strings.
        # Preserve the original record for export, but do not pretend to know
        # whether an unannotated legacy ID is a valid platform query locator.
        message_id=None,
        timestamp=row.get("timestamp") or None,
        sender={
            key: value
            for key, value in dict(
                user_id=str(row.get("sender_id") or ""), nickname=row.get("sender_name")
            ).items()
            if value
        },
        message_str=IMAGE_MARKER.sub("", text).strip(),
        message=images,
    )


def import_flow(store, source, event):
    flow_id = external_id(event)
    hashed = flow_hash(flow_id)
    path = source / "logs" / (hashed + ".jsonl")
    if not path.is_file():
        raise ValueError("no external Flow log exists for this group/topic")
    raw = path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    records, last = [], 0
    for row in rows:
        seq = row.get("seq")
        if (
            not isinstance(seq, int)
            or seq <= last
            or row.get("flow_id", flow_id) != flow_id
        ):
            raise ValueError("external Flow log has invalid sequence or group identity")
        if str(row.get("platform_id", event.get_platform_id())) != str(
            event.get_platform_id()
        ) or str(row.get("group_id", event.get_group_id())) != str(
            event.get_group_id()
        ):
            raise ValueError("external Flow log belongs to another scope")
        last = seq
        record = imported_record(row)
        key = "import:" + str(seq)
        if record.get("message_id"):
            key = "native:" + str(record["message_id"])
        records.append(dict(seq=seq, event_key=key, record=record, legacy=row))
    state = read_json(source / "state.json", {})
    cursors = {}
    for key, value in state.get("cursors", {}).items():
        if not key.startswith(hashed + ":"):
            continue
        metadata = state.get("cursor_meta", {}).get(key, {})
        if (
            metadata.get("unified_msg_origin", event.unified_msg_origin)
            != event.unified_msg_origin
        ):
            raise ValueError(
                "legacy Flow mixed framework scopes; use a new boundary instead of importing"
            )
        cursors[key.split(":", 1)[1]] = value
    fingerprint = hashlib.sha256(
        (raw + json.dumps(cursors, sort_keys=True)).encode()
    ).hexdigest()
    result = store.import_records(
        event.unified_msg_origin, records, cursors, event_meta(event), fingerprint
    )
    result["unverified_message_ids"] = sum(
        not entry["record"].get("message_id") for entry in records
    )
    return result


def _export_row(event, entry):
    record = entry["record"]
    if entry.get("legacy"):
        result = dict(entry["legacy"])
    else:
        text = record.get("message_str", "")
        annotations = components(record.get("message"))
        if annotations:
            text += " " + annotations
        result = dict(
            message_id=record.get("message_id") or entry["event_key"],
            platform_id=str(event.get_platform_id()),
            platform_name=str(event.get_platform_name()),
            flow_id=external_id(event),
            group_id=str(event.get_group_id()),
            group_name=event_meta(event).get("group.group_name", ""),
            sender_id=record.get("sender", {}).get("user_id", ""),
            sender_name=record.get("sender", {}).get("nickname", ""),
            self_id=event_meta(event).get("self_id", ""),
            timestamp=record.get("timestamp") or 0,
            text=text,
        )
    result.update(
        seq=entry["seq"],
        enishi_record=record,
        enishi_native_id=bool(record.get("message_id")),
    )
    return result


def export_flow(store, event, conversation):
    snapshot = store.dump(event.unified_msg_origin)
    flow = snapshot["flow"]
    rows = [_export_row(event, entry) for entry in snapshot["records"]]
    cursors = {
        cid: max(cursor, flow["floor"]) for cid, cursor in snapshot["cursors"].items()
    }
    if conversation:
        cursors.setdefault(conversation, flow["floor"])
    if not rows and flow["last_seq"]:
        # External Flow derives its next sequence from the retained log. An
        # explicit consumed anchor preserves the watermark after local clearing.
        anchor = dict(
            message_id=None, timestamp=None, sender={}, message_str="", message=[]
        )
        rows = [
            _export_row(
                event,
                dict(
                    seq=flow["last_seq"],
                    event_key="enishi-boundary:" + str(flow["last_seq"]),
                    record=anchor,
                ),
            )
        ]
        if conversation:
            cursors[conversation] = flow["last_seq"]
    target = (
        store.directory.parent
        / "flow_exports"
        / flow_hash(event.unified_msg_origin)
        / uuid.uuid4().hex[:12]
    )
    flow_id = external_id(event)
    hashed = flow_hash(flow_id)
    source_state = (
        store.directory.parent.parent
        / "astrbot_plugin_group_context_flow"
        / "state.json"
    )
    state = read_json(source_state, {})
    for section in ("cursors", "cursor_meta"):
        values = state.setdefault(section, {})
        if not isinstance(values, dict):
            raise ValueError("external Flow state has invalid cursor sections")
        state[section] = {
            key: value
            for key, value in values.items()
            if not key.startswith(hashed + ":")
        }
    for cid, cursor in cursors.items():
        key = hashed + ":" + cid
        state["cursors"][key] = cursor
        state["cursor_meta"][key] = dict(
            flow_id=flow_id,
            conversation_id=cid,
            unified_msg_origin=event.unified_msg_origin,
        )
    atomic_text(
        target / "logs" / (hashed + ".jsonl"),
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
    )
    write_json(target / "state.json", state)
    write_json(
        target / "cursor_patch.json",
        {
            section: {
                key: value
                for key, value in state[section].items()
                if key.startswith(hashed + ":")
            }
            for section in ("cursors", "cursor_meta")
        },
    )
    return dict(
        path=str(target),
        records=len(snapshot["records"]),
        cursors=len(cursors),
        latest_seq=flow["last_seq"],
    )
