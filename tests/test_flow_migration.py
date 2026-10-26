from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot_plugin_context_enishi.context.group_flow.migration import (
    export_flow,
    external_id,
    import_flow,
)
from astrbot_plugin_context_enishi.context.group_flow.store import FlowStore, flow_hash

from .flow_support import group_event


def external_source(directory, event, *, scope=None, cursor=8):
    hashed = flow_hash(external_id(event))
    log = directory / "logs" / (hashed + ".jsonl")
    log.parent.mkdir(parents=True)
    rows = [
        dict(
            seq=seq,
            message_id=str(seq),
            flow_id=external_id(event),
            platform_id=event.get_platform_id(),
            group_id=event.get_group_id(),
            sender_id="1001",
            sender_name="A",
            timestamp=100,
            text="hello [图片 id=qq://bot/123/image/native?m=9&i=0]",
        )
        for seq in (8, 9)
    ]
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    key = hashed + ":conversation"
    state = dict(
        cursors={key: cursor},
        cursor_meta={
            key: dict(
                flow_id=external_id(event),
                conversation_id="conversation",
                unified_msg_origin=scope or event.unified_msg_origin,
            )
        },
    )
    (directory / "state.json").write_text(json.dumps(state))
    return rows, log


def test_import_is_copy_only_preserves_sequences_cursors_and_marks_unknown_ids(
    tmp_path,
):
    event = group_event("10")
    source = tmp_path / "external"
    rows, path = external_source(source, event)
    before = path.read_bytes()
    store = FlowStore(tmp_path / "enishi" / "flow")
    result = import_flow(store, source, event)
    assert result == dict(records=2, cursors=1, latest_seq=9, unverified_message_ids=2)
    assert path.read_bytes() == before
    snapshot = store.dump(event.unified_msg_origin)
    assert snapshot["cursors"]["conversation"] == 8
    assert [entry["seq"] for entry in snapshot["records"]] == [8, 9]
    assert snapshot["records"][1]["record"]["message_id"] is None
    assert snapshot["records"][1]["legacy"]["message_id"] == "9"
    assert snapshot["records"][1]["record"]["message"][0]["file"].endswith("?m=9&i=0")
    with pytest.raises(ValueError, match="not empty"):
        import_flow(store, source, event)


@pytest.mark.parametrize(
    "wrong_scope,cursor", [("other:GroupMessage:123", 8), (None, 99)]
)
def test_bad_import_does_not_publish_partial_destination(tmp_path, wrong_scope, cursor):
    event = group_event("10")
    source = tmp_path / "external"
    external_source(source, event, scope=wrong_scope, cursor=cursor)
    store = FlowStore(tmp_path / "enishi" / "flow")
    with pytest.raises(ValueError):
        import_flow(store, source, event)
    assert not store.state_path.exists()


def test_import_export_round_trip_keeps_new_native_ids_and_relationships(tmp_path):
    event = group_event("10")
    source = tmp_path / "external"
    external_source(source, event)
    store = FlowStore(tmp_path / "enishi" / "flow")
    import_flow(store, source, event)
    message = dict(
        message_id="10",
        timestamp=200,
        sender=dict(user_id="1002", nickname="B"),
        message_str="new message",
        message=[dict(type="Reply", id="9", sender_id="1001")],
    )
    assert store.append(event.unified_msg_origin, message, "native:10", {}, 0) == 10
    assert store.commit(event.unified_msg_origin, "conversation", 10, 1)
    exported = export_flow(store, event, "conversation")
    target = Path(exported["path"])
    assert target.is_relative_to(store.directory.parent)
    restored = FlowStore(tmp_path / "restored" / "flow")
    import_flow(restored, target, event)
    snapshot = restored.dump(event.unified_msg_origin)
    assert snapshot["cursors"]["conversation"] == 10
    assert snapshot["records"][-1]["record"] == message
    assert snapshot["records"][0]["legacy"]["message_id"] == "8"


def test_empty_export_retains_consumed_sequence_anchor_for_external_flow(tmp_path):
    event = group_event("1")
    store = FlowStore(tmp_path / "flow")
    store.append(
        event.unified_msg_origin,
        dict(message_id="1", sender={}, message=[]),
        "native:1",
        {},
        0,
    )
    store.clear(event.unified_msg_origin)
    exported = Path(export_flow(store, event, "conversation")["path"])
    hashed = flow_hash(external_id(event))
    rows = [
        json.loads(line)
        for line in (exported / "logs" / (hashed + ".jsonl")).read_text().splitlines()
    ]
    state = json.loads((exported / "state.json").read_text())
    assert rows[-1]["seq"] == state["cursors"][hashed + ":conversation"] == 1


def test_interrupted_import_is_blocked_then_can_resume_same_source(
    tmp_path, monkeypatch
):
    event = group_event("10")
    source = tmp_path / "external"
    external_source(source, event)
    store = FlowStore(tmp_path / "flow")
    original = store._write_records

    def fail(*args):
        raise OSError("interrupted import")

    monkeypatch.setattr(store, "_write_records", fail)
    with pytest.raises(OSError):
        import_flow(store, source, event)
    with pytest.raises(ValueError, match="incomplete"):
        store.window(event.unified_msg_origin, "conversation", 10)
    monkeypatch.setattr(store, "_write_records", original)
    assert import_flow(store, source, event)["records"] == 2


def test_export_preserves_other_groups_external_cursors(tmp_path):
    event = group_event("1")
    root = tmp_path / "plugin_data"
    external = root / "astrbot_plugin_group_context_flow"
    external.mkdir(parents=True)
    other_key = flow_hash("other-flow") + ":other-conversation"
    original = dict(
        cursors={other_key: 50}, cursor_meta={other_key: dict(flow_id="other-flow")}
    )
    (external / "state.json").write_text(json.dumps(original))
    store = FlowStore(root / "enishi" / "flow")
    store.append(
        event.unified_msg_origin,
        dict(message_id="1", sender={}, message=[]),
        "native:1",
        {},
        0,
    )
    result = export_flow(store, event, "conversation")
    state = json.loads((Path(result["path"]) / "state.json").read_text())
    assert state["cursors"][other_key] == 50
    assert json.loads((external / "state.json").read_text()) == original
    patch = json.loads((Path(result["path"]) / "cursor_patch.json").read_text())
    assert other_key not in patch["cursors"]


def test_admin_clear_can_abandon_incomplete_import_without_reusing_sequences(
    tmp_path, monkeypatch
):
    event = group_event("10")
    source = tmp_path / "external"
    external_source(source, event)
    store = FlowStore(tmp_path / "flow")
    original = store._write_records

    def fail(*args):
        raise OSError("interrupted import")

    monkeypatch.setattr(store, "_write_records", fail)
    with pytest.raises(OSError):
        import_flow(store, source, event)
    monkeypatch.setattr(store, "_write_records", original)
    store.clear(event.unified_msg_origin)
    assert (
        store.append(
            event.unified_msg_origin,
            dict(message_id="10", sender={}, message=[]),
            "native:10",
            {},
            0,
        )
        == 10
    )
