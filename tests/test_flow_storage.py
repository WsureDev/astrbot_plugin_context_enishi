from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from astrbot_plugin_context_enishi.context.group_flow.store import FlowStore


def record(mid):
    return dict(
        message_id=mid,
        timestamp=100,
        sender=dict(user_id="1001"),
        message_str="same text",
        message=[],
    )


def test_sequence_survives_trim_clear_and_restart_and_dedupes_by_id(tmp_path):
    store = FlowStore(tmp_path)
    for index in range(1, 5):
        assert (
            store.append("scope", record(str(index)), "native:" + str(index), {}, 2)
            == index
        )
    assert store.append("scope", record("4"), "native:4", {}, 2) == 4
    store = FlowStore(tmp_path)
    snapshot = store.window("scope", "conversation", 4)
    assert snapshot["missing"] == 2
    assert [row["seq"] for row in snapshot["records"]] == [3]
    store.clear("scope")
    assert FlowStore(tmp_path).append("scope", record("5"), "native:5", {}, 2) == 5


def test_cursor_updates_are_monotonic_and_scopes_do_not_overwrite(tmp_path):
    store = FlowStore(tmp_path)
    for scope in ("one", "two"):
        for index in range(1, 4):
            store.append(scope, record(str(index)), str(index), {}, 0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(store.commit, scope, "conversation", 3, 0)
            for scope in ("one", "two")
        ]
        assert all(future.result() for future in futures)
    store.commit("one", "conversation", 1, 0)
    assert store.window("one", "conversation", 4)["cursor"] == 3
    assert store.window("two", "conversation", 4)["cursor"] == 3
    store.boundary("one", "conversation", 3)
    assert not store.commit("one", "conversation", 2, 0)


def test_partial_state_write_recovers_sequence_from_atomic_log(tmp_path):
    store = FlowStore(tmp_path)
    store.append("scope", record("1"), "native:1", {}, 0)
    state = json.loads(store.state_path.read_text())
    state["flows"]["scope"]["last_seq"] = 0
    store.state_path.write_text(json.dumps(state))
    assert store.append("scope", record("1"), "native:1", {}, 0) == 1
    assert store.commit("scope", "conversation", 1, 0)


def test_new_conversation_boundary_preserves_other_conversations_cursor(tmp_path):
    store = FlowStore(tmp_path)
    for index in range(1, 4):
        store.append("scope", record(str(index)), str(index), {}, 0)
    store.commit("scope", "old-conversation", 1, 0)
    store.boundary("scope", "new-conversation", 3)
    assert store.window("scope", "old-conversation", 4)["cursor"] == 1
    assert store.window("scope", "new-conversation", 4)["cursor"] == 3


def test_corrupt_log_fails_without_silently_skipping_data(tmp_path):
    store = FlowStore(tmp_path)
    store.append("scope", record("1"), "native:1", {}, 0)
    store._path("scope").write_text("corrupt data\n")
    with pytest.raises(ValueError):
        store.append("scope", record("2"), "native:2", {}, 0)
    assert json.loads(store.state_path.read_text())["flows"]["scope"]["last_seq"] == 1
