from __future__ import annotations

from types import SimpleNamespace

from astrbot_plugin_context_enishi.context.flow import FlowIntegration

from .conftest import Event


class Flow:
    def _record_from_event(self, event):
        return {"text": "发了一张[图片]", "message_id": "123"}

    def _format_delta(self, records):
        return "\n".join(r["text"] for r in records)


def make_owner(service, flow):
    return SimpleNamespace(
        context=SimpleNamespace(
            get_registered_star=lambda n: SimpleNamespace(star_cls=flow)
        ),
        settings=service.settings,
        enabled_event=lambda e: service.settings.for_event(e).mode != "disabled",
    )


def test_flow_retains_native_id_in_existing_text(service):
    flow = Flow()
    integration = FlowIntegration(make_owner(service, flow))
    integration.reconcile()
    record = flow._record_from_event(service.event)
    assert "native_current" in record["text"]
    assert not hasattr(flow, "image_index")
    integration.terminate()
    assert flow._record_from_event(service.event)["text"] == "发了一张[图片]"


def test_legacy_qq_placeholder_can_use_message_id_without_index(service):
    flow = Flow()
    integration = FlowIntegration(make_owner(service, flow))
    integration.reconcile()
    record = {
        "platform_name": "aiocqhttp",
        "platform_id": "bot",
        "group_id": "123",
        "sender_id": "99",
        "message_id": "unseen",
        "text": "[图片] [图片]",
    }
    rendered = flow._format_delta([record])
    assert "/message/unseen" in rendered and "i=1" in rendered
    assert record["text"] == "[图片] [图片]"
    integration.terminate()


def test_flow_off_does_not_change_existing_records(service):
    service.config["channel_settings"][0]["image_mode"] = "disabled"
    flow = Flow()
    integration = FlowIntegration(make_owner(service, flow))
    integration.reconcile()
    assert flow._record_from_event(Event())["text"] == "发了一张[图片]"
    integration.terminate()
