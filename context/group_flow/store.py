"""Single-process atomic file ledger; one lock also protects shared cursor state."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from ...storage.files import atomic_text, read_json, write_json


def flow_hash(scope):
    return hashlib.sha256(scope.encode()).hexdigest()[:32]


class FlowStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.state_path = directory / "state.json"
        self.lock = threading.RLock()

    def _state(self):
        state = read_json(self.state_path, {})
        if not isinstance(state, dict):
            raise ValueError("flow state is not an object")
        state.setdefault("flows", {})
        state.setdefault("cursors", {})
        state.setdefault("cursor_meta", {})
        if any(
            not isinstance(state[key], dict)
            for key in ("flows", "cursors", "cursor_meta")
        ):
            raise ValueError("flow state sections are not objects")
        return state

    def _flow(self, state, scope):
        flow = state["flows"].setdefault(
            scope, dict(last_seq=0, generation=0, floor=0, meta={})
        )
        if not isinstance(flow, dict) or any(
            not isinstance(flow.get(key), int) or flow[key] < 0
            for key in ("last_seq", "generation", "floor")
        ):
            raise ValueError("flow watermarks are invalid")
        return flow

    def _path(self, scope):
        return self.directory / "logs" / (flow_hash(scope) + ".jsonl")

    def _records(self, scope):
        path = self._path(scope)
        if not path.is_file():
            return []
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        previous = 0
        for entry in records:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("seq"), int)
                or entry["seq"] <= previous
            ):
                raise ValueError(
                    "flow log has invalid or non-monotonic sequence numbers"
                )
            if not isinstance(entry.get("record"), dict):
                raise ValueError("flow log has no message record")
            if not isinstance(entry.get("event_key"), str) or not entry["event_key"]:
                raise ValueError("flow log has no event key")
            previous = entry["seq"]
        return records

    def _write_records(self, scope, records):
        atomic_text(
            self._path(scope),
            "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                for row in records
            ),
        )

    def _cursor_key(self, scope, conversation):
        return flow_hash(scope) + ":" + conversation

    def _set_cursor(self, state, scope, conversation, value):
        key = self._cursor_key(scope, conversation)
        state["cursors"][key] = max(int(state["cursors"].get(key, 0)), value)
        state["cursor_meta"][key] = dict(scope=scope, conversation_id=conversation)

    def append(self, scope, record, event_key, metadata, limit=5000):
        with self.lock:
            state = self._state()
            flow = self._flow(state, scope)
            records = self._records(scope)
            if records and records[-1]["seq"] > flow["last_seq"]:
                flow["last_seq"] = records[-1]["seq"]
                write_json(self.state_path, state)
            for entry in records:
                if entry["event_key"] == event_key:
                    return entry["seq"]
            latest = int(flow["last_seq"])
            if records:
                latest = max(latest, records[-1]["seq"])
            seq = latest + 1
            records.append(dict(seq=seq, event_key=event_key, record=record))
            if limit > 0:
                records = records[-limit:]
            self._write_records(scope, records)
            flow.update(last_seq=seq, meta=metadata)
            write_json(self.state_path, state)
            return seq

    def window(self, scope, conversation, trigger):
        with self.lock:
            state = self._state()
            flow = self._flow(state, scope)
            cursor = max(
                flow["floor"],
                int(state["cursors"].get(self._cursor_key(scope, conversation), 0)),
            )
            records = [
                entry
                for entry in self._records(scope)
                if cursor < entry["seq"] < trigger
            ]
            missing = max(0, trigger - cursor - 1 - len(records))
            return dict(
                records=records,
                cursor=cursor,
                generation=flow["generation"],
                missing=missing,
            )

    def commit(self, scope, conversation, target, generation):
        with self.lock:
            state = self._state()
            flow = self._flow(state, scope)
            if flow["generation"] != generation:
                return False
            if target > flow["last_seq"]:
                raise ValueError("cursor exceeds flow sequence watermark")
            self._set_cursor(state, scope, conversation, target)
            write_json(self.state_path, state)
            return True

    def boundary(self, scope, conversation, seq):
        with self.lock:
            state = self._state()
            flow = self._flow(state, scope)
            flow["generation"] += 1
            flow["last_seq"] = max(flow["last_seq"], seq)
            self._set_cursor(state, scope, conversation, seq)
            write_json(self.state_path, state)

    def clear(self, scope):
        with self.lock:
            state = self._state()
            flow = self._flow(state, scope)
            records = self._records(scope)
            if records:
                flow["last_seq"] = max(flow["last_seq"], records[-1]["seq"])
            flow.update(generation=flow["generation"] + 1, floor=flow["last_seq"])
            self._write_records(scope, [])
            write_json(self.state_path, state)

    def dump(self, scope):
        with self.lock:
            state = self._state()
            flow = dict(self._flow(state, scope))
            records = self._records(scope)
            if records:
                flow["last_seq"] = max(flow["last_seq"], records[-1]["seq"])
            cursors = {
                meta["conversation_id"]: state["cursors"][key]
                for key, meta in state["cursor_meta"].items()
                if meta.get("scope") == scope
            }
            return dict(flow=flow, cursors=cursors, records=records)
