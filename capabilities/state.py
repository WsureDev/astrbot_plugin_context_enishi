from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..storage.files import read_json, write_json
from .policy import MODES


@dataclass
class Lease:
    last: int
    revision: str = ""


@dataclass
class Session:
    completed: int = 0
    leases: dict[str, Lease] = field(default_factory=dict)
    suppressed: set[str] = field(default_factory=set)
    artifacts: dict[str, str] = field(default_factory=dict)
    resource_owners: dict[str, str] = field(default_factory=dict)
    result_refs: dict[str, str] = field(default_factory=dict)
    evicted: dict[str, int] = field(default_factory=dict)
    expanded_refs: set[str] = field(default_factory=set)
    counters: dict[str, int | float] = field(
        default_factory=lambda: {
            "loads": 0,
            "unloads": 0,
            "reloads": 0,
            "quick_reloads": 0,
            "auto_unloads": 0,
            "prefetches": 0,
            "management_steps": 0,
            "load_ms": 0,
            "max_schema_tokens": 0,
            "max_visible_tools": 0,
        }
    )

    def load(self, key, revision=""):
        if key in self.evicted:
            self.counters["reloads"] += 1
            if self.completed - self.evicted.pop(key) <= 2:
                self.counters["quick_reloads"] += 1
        if key not in self.leases:
            self.counters["loads"] += 1
        self.suppressed.discard(key)
        self.leases[key] = Lease(self.completed + 1, revision)

    def unload(self, key):
        self.leases.pop(key, None)
        self.suppressed.add(key)
        self.counters["unloads"] += 1

    def active(self, key, idle_rounds):
        lease = self.leases.get(key)
        return lease is not None and self.completed - lease.last < idle_rounds


class StateStore:
    def __init__(self, directory):
        self.path = directory / "state.json"
        raw = read_json(self.path, {})
        self.groups = raw.get("groups", {})
        self.tools = raw.get("tools", {})
        self.sessions = {}
        for key, row in raw.get("sessions", {}).items():
            self.sessions[key] = Session(
                completed=int(row.get("completed", 0)),
                leases={k: Lease(**v) for k, v in row.get("leases", {}).items()},
                suppressed=set(row.get("suppressed", [])),
                artifacts=row.get("artifacts", {}),
                resource_owners=row.get("resource_owners", {}),
                result_refs=row.get("result_refs", {}),
                evicted=row.get("evicted", {}),
                expanded_refs=set(row.get("expanded_refs", [])),
                counters={**Session().counters, **row.get("counters", {})},
            )

    def discover(self, entries, default_mode):
        changed = False
        for entry in entries.values():
            if entry.group not in self.groups:
                self.groups[entry.group] = {
                    "mode": default_mode,
                    "idle_rounds": None,
                    "name": entry.group.partition(":")[2],
                }
                changed = True
        return changed

    def mode(self, entry):
        return self.tools.get(entry.id, {}).get(
            "mode", self.groups.get(entry.group, {}).get("mode", "folded")
        )

    def idle(self, entry, default):
        return self.groups.get(entry.group, {}).get("idle_rounds") or default

    def update(self, group, mode, idle_rounds=None, *, name=None):
        if group not in self.groups:
            raise ValueError("未知工具组")
        if mode not in MODES:
            raise ValueError("组策略必须是 primary 或 folded")
        if idle_rounds is not None and (
            type(idle_rounds) is not int or idle_rounds < 1
        ):
            raise ValueError("闲置轮数必须是正整数")
        if name is not None and not isinstance(name, str):
            raise ValueError("显示名必须是文本")
        display_name = self.groups[group].get("name", group.partition(":")[2])
        if name is not None:
            display_name = name.strip() or group.partition(":")[2]
        self.groups[group] = {
            "mode": mode,
            "idle_rounds": idle_rounds,
            "name": display_name,
        }

    def snapshot(self):
        return {
            "groups": self.groups,
            "tools": self.tools,
            "sessions": {
                key: {
                    **asdict(state),
                    "suppressed": sorted(state.suppressed),
                    "expanded_refs": sorted(state.expanded_refs),
                }
                for key, state in self.sessions.items()
            },
        }

    def save(self, snapshot=None):
        if snapshot is None:
            snapshot = self.snapshot()
        write_json(self.path, snapshot)
