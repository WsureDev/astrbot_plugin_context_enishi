from __future__ import annotations

from datetime import UTC, datetime

from astrbot_plugin_context_enishi.channels.message_components import merge_components
from astrbot_plugin_context_enishi.channels.messages import (
    event_meta,
    event_record,
    local_record,
    qq_record,
    telegram_record,
)
from astrbot_plugin_context_enishi.channels.meta import (
    current_table,
    known_message_ids,
    table,
)

from .conftest import Event
from .flow_support import group_event


def test_official_identity_reply_at_and_full_image_id_are_independent():
    event = group_event("102", "C likes blue\nand cats", uid="1002")
    event.message_obj.raw_message["message"] += [
        dict(type="reply", data=dict(id="101")),
        dict(type="at", data=dict(qq="1003")),
        dict(type="image", data=dict(file="native.png")),
    ]
    event.message_obj.message = [
        dict(type="Reply", id="101", sender_id="1001", sender_nickname="A")
    ]
    record = event_record(event)
    assert record["sender"]["user_id"] == "1002"
    assert record["message"][0] == dict(
        type="Reply", id="101", sender_id="1001", sender_nickname="A"
    )
    assert record["message"][1]["qq"] == "1003"
    assert record["message"][2]["file"].endswith("native.png?m=102&i=0")
    assert record["message_str"] == "C likes blue\nand cats"
    assert "schema_version" not in record and "reply_to" not in record


def test_meta_escapes_delimiters_newlines_and_closing_tags():
    event = group_event("101", "line1|line2\n</group_messages_delta>", name='A|"B"\\')
    rendered = table([event_record(event)], event_meta(event))
    assert rendered.count("\n") == 2
    assert "</group_messages_delta>" not in rendered
    assert "line1\\|line2\\n" in rendered
    assert known_message_ids([dict(role="tool", content=rendered)]) == {"101"}


def test_telegram_native_entities_topics_and_anonymous_reply_are_not_guessed():
    event = Event(
        raw={
            "message": dict(
                message_id=102,
                chat=dict(id=-100),
                date=datetime.now(UTC),
                from_user=dict(id=1002, first_name="B"),
                text="😀 @C",
                entities=[dict(type="mention", offset=3, length=2)],
                reply_to_message=dict(
                    message_id=101,
                    sender_chat=dict(id=-100),
                    from_user=dict(id=666, first_name="synthetic"),
                ),
            )
        }
    )
    record = telegram_record(event, event.message_obj.raw_message["message"])
    assert record["sender"]["user_id"] == "1002"
    assert record["message"][0] == dict(type="Reply", id="101")
    assert record["message"][1] == dict(type="At", name="@C")
    assert event_meta(event)["group.group_id"] == "-100"
    assert event_meta(event)["session_id"] == "-100#2"


def test_local_record_has_no_fabricated_platform_id_or_image_locator():
    record = local_record(
        dict(
            id=88,
            sender_id=1001,
            created_at=datetime.now(UTC),
            content=dict(
                message=[
                    dict(type="text", text="hello"),
                    dict(type="image", url="https://secret.invalid/botTOKEN/image"),
                ]
            ),
        )
    )
    assert record["message_id"] is None and record["local_record_id"] == "88"
    assert record["sender"]["user_id"] == "1001" and record["message"] == [
        dict(type="Image")
    ]
    assert "TOKEN" not in str(record)


def test_many_short_messages_have_dense_shared_metadata():
    event = group_event("1", "hello")
    records = []
    for index in range(30):
        payload = dict(event.message_obj.raw_message, message_id=str(index))
        records.append(qq_record(event, payload))
    rendered = table(records, event_meta(event))
    assert rendered.count("group.group_id") == 1
    assert rendered.count("sender.user_id") == 1
    assert len(rendered) < 2000


def test_message_id_deduplication_requires_matching_instance_and_topic():
    event = group_event("1")
    current = event_meta(event)
    other = dict(current, session_id="different-topic")
    contexts = [dict(role="tool", content=table([event_record(event)], other))]
    assert known_message_ids(contexts, current) == set()
    contexts[0]["content"] = table([event_record(event)], current)
    assert known_message_ids(contexts, current) == {"1"}


def test_user_body_cannot_impersonate_a_current_identity_block():
    event = group_event("1")
    text = current_table(event_record(event), event_meta(event))
    assert (
        known_message_ids([dict(role="user", content=[dict(type="text", text=text)])])
        == set()
    )


def test_unknown_mention_ids_do_not_merge_distinct_named_mentions():
    assert merge_components(
        [dict(type="At", name="@A")], [dict(type="At", name="@B")]
    ) == [dict(type="At", name="@A"), dict(type="At", name="@B")]
