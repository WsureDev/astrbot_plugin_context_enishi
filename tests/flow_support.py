from __future__ import annotations

import copy
from types import SimpleNamespace

from astrbot_plugin_context_enishi.context.group_flow.service import GroupFlow
from astrbot_plugin_context_enishi.identity import PLUGIN
from astrbot_plugin_context_enishi.settings import Settings

from .conftest import Event, request


def group_event(mid, text="message", uid="99", name="B", chat="123", account="bot"):
    event = Event(
        "aiocqhttp",
        account=account,
        chat=chat,
        raw=dict(
            post_type="message",
            message_id=mid,
            time=2_000_000_000,
            sender=dict(user_id=uid, card=name),
            message=[dict(type="text", data=dict(text=text))],
        ),
    )
    event.message_obj.message_id = mid
    event.message_obj.message_str = text
    event.message_obj.timestamp = 2_000_000_001
    event.message_obj.self_id = "9000"
    event.get_sender_id = lambda: uid
    event.get_sender_name = lambda: name
    event.get_message_str = lambda: text
    return event


class Conversations:
    def __init__(self):
        self.current = {}
        self.histories = {}
        self.fail = False

    async def get_curr_conversation_id(self, unified_msg_origin):
        return self.current.get(unified_msg_origin, "conversation")

    async def update_conversation(
        self,
        unified_msg_origin,
        conversation_id=None,
        history=None,
        title=None,
        persona_id=None,
        token_usage=None,
    ):
        if self.fail:
            raise OSError("simulated history write failure")
        if history is not None:
            self.histories[(unified_msg_origin, conversation_id)] = copy.deepcopy(
                history
            )


class Run:
    def __init__(self, event, req):
        self.req = req
        self.run_context = SimpleNamespace(
            context=SimpleNamespace(event=event), messages=[]
        )
        self.aborted = False
        self.finished = False

    def was_aborted(self):
        return self.aborted

    def done(self):
        return self.finished


def flow_service(tmp_path):
    config = dict(
        channel_settings=[
            dict(
                __template_key="qq",
                context=dict(image_mode="disabled", group_flow_enabled=True),
            )
        ]
    )
    conversations = Conversations()
    context = SimpleNamespace(
        conversation_manager=conversations, external=None, builtin=False
    )
    context.get_registered_star = lambda name: context.external
    context.get_config = lambda **kw: dict(
        provider_ltm_settings=dict(group_icl_enable=context.builtin)
    )
    owner = SimpleNamespace(context=context, settings=Settings(config))
    owner.participates = lambda event: (
        event.plugins_name is None or PLUGIN in event.plugins_name
    )
    flow = GroupFlow(owner)
    flow.initialize(tmp_path / "flow")
    return SimpleNamespace(
        flow=flow, config=config, context=context, manager=conversations
    )


def flow_request(contexts=None, cid="conversation"):
    req = request()
    req.contexts = list(contexts or [])
    req.conversation = SimpleNamespace(cid=cid)
    return req


def bound_run(flow, event, req, *, aborted=False, role="assistant", answer="answer"):
    run = Run(event, req)
    flow.bind_runner(run)
    run.aborted = aborted
    flow.finish(
        event, run.run_context, SimpleNamespace(role=role, completion_text=answer)
    )
    run.finished = True
    return run


def saved_history(req):
    return [
        *req.contexts,
        dict(
            role="user",
            content=[
                dict(type="text", text="trigger"),
                *copy.deepcopy(req.extra_user_content_parts),
            ],
        ),
        dict(role="assistant", content="answer"),
    ]
