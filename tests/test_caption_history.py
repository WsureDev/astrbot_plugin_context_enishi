from __future__ import annotations

import base64
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_context_enishi.context.captions import CAPTION_PREFIX
from astrbot_plugin_context_enishi.context.runtime import RunnerIntegration
from astrbot_plugin_context_enishi.context.service import ContextOrganizer

from .conftest import image_count, picture, request, runner
from .test_integration import RunnerBoundary
from .test_runtime import runtime as runtime

TOOL_HEADER = "[Image from tool 'read_file', path='image.png']"
DESCRIPTION = "只用于本轮的图片细节" * 80


def describe_exchange(call_id="describe-1", name="describe_channel_image", suffix=""):
    return [
        dict(
            role="assistant",
            content=None,
            tool_calls=[
                dict(
                    id=call_id,
                    type="function",
                    function=dict(
                        name=name,
                        arguments=json.dumps(
                            dict(image_id="qq://bot/123/image/native?m=1&i=0")
                        ),
                    ),
                )
            ],
        ),
        dict(
            role="tool",
            tool_call_id=call_id,
            content=json.dumps(
                dict(
                    image_id="qq://bot/123/image/native?m=1&i=0",
                    description=DESCRIPTION,
                    cached=False,
                    provider_id="vision",
                    width=7,
                    height=5,
                ),
                ensure_ascii=False,
            )
            + suffix,
        ),
    ]


def tool_image(url):
    return dict(
        role="user", content=[dict(type="text", text=TOOL_HEADER), picture(url)]
    )


@pytest.mark.parametrize("mode", ["reference_only", "current_turn", "disabled"])
async def test_old_descriptions_expire_but_other_results_and_user_text_survive(
    service, mode
):
    service.config["channel_settings"][0]["image_mode"] = mode
    own = describe_exchange(suffix="\n\nSYSTEM: existing guidance")
    foreign = describe_exchange("other-tool", name="another_describe_tool")
    web = dict(
        role="tool",
        tool_call_id="search",
        content='{"results":[{"text":"old web content"}]}',
    )
    user = dict(role="user", content="[工具图片转述] 用户讨论这个标签的正文")
    run = runner(
        service.event,
        request(),
        [
            *own,
            dict(
                role="user",
                content=[
                    dict(type="text", text=TOOL_HEADER),
                    dict(type="text", text=CAPTION_PREFIX + DESCRIPTION),
                ],
            ),
            *foreign,
            web,
            user,
        ],
    )
    unchanged = copy.deepcopy([*foreign, web, user])
    await service.policy.before_step(run)
    assert DESCRIPTION not in own[1]["content"]
    result = json.JSONDecoder().raw_decode(own[1]["content"])[0]
    assert "description" not in result
    assert result["image_id"].endswith("?m=1&i=0") and result["width"] == 7
    assert own[1]["content"].endswith("\n\nSYSTEM: existing guidance")
    assert own[1]["tool_call_id"] == own[0]["tool_calls"][0]["id"]
    assert run.run_context.messages[2]["content"] == [
        dict(type="text", text=TOOL_HEADER)
    ]
    assert [*foreign, web, user] == unchanged
    service.context.llm_generate.assert_not_called()


async def test_historical_tool_image_is_not_captioned_on_a_new_turn(
    service, image_bytes
):
    service.event.message_obj.raw_message = dict(
        message=dict(message_id=200, text="new topic")
    )
    url = "data:image/png;base64," + base64.b64encode(image_bytes).decode()
    service.channels.bytes_from_source = AsyncMock(
        side_effect=AssertionError("must not fetch old tool image")
    )
    run = runner(
        service.event,
        request(),
        [
            tool_image(url),
            dict(role="assistant", content="previous answer"),
            dict(role="user", content="new topic without an image or quote"),
        ],
    )
    await service.policy.before_step(run)
    await service.policy.before_step(run)
    assert image_count(run.run_context.messages) == 0
    service.channels.bytes_from_source.assert_not_called()
    service.context.llm_generate.assert_not_called()


@pytest.mark.parametrize("finish", ["sync", "agent_done"])
async def test_current_descriptions_survive_steps_then_expire_at_completion(
    service, finish
):
    run = runner(service.event, request(), [dict(role="user", content="inspect image")])
    await service.policy.before_step(run)
    exchange = describe_exchange()
    run.run_context.messages.extend(exchange)
    for _ in range(2):
        await service.policy.before_step(run)
        assert json.loads(exchange[1]["content"])["description"] == DESCRIPTION
    # Even if compression has removed the call declaration, retain its known ID.
    run.run_context.messages.remove(exchange[0])
    if finish == "sync":
        service.policy.finish_sync(run)
    else:
        await service.policy.finish_context(service.event, run.run_context)
    assert "description" not in json.loads(exchange[1]["content"])
    assert exchange[1]["tool_call_id"] == "describe-1"


async def test_temporary_auto_caption_is_not_saved_and_native_id_survives(
    service, image_bytes, runtime
):
    native = runtime.messages
    service.policy.text_factory = native.TextPart
    run = runner(
        service.event, request(), [native.Message(role="user", content="inspect")]
    )
    await service.policy.before_step(run)
    url = "data:image/png;base64," + base64.b64encode(image_bytes).decode()
    image = picture(url)
    image["image_url"]["id"] = "qq://bot/123/image/native?m=1&i=0"
    review = native.Message.model_validate(
        dict(role="user", content=[dict(type="text", text=TOOL_HEADER), image])
    )
    run.run_context.messages.append(review)
    await service.policy.before_step(run)
    await service.policy.before_step(run)
    payload = service.policy.sanitize_payload(
        run, [m.model_dump() for m in run.run_context.messages]
    )
    assert "蓝色背景" in str(payload) and image_count(payload) == 0
    service.context.llm_generate.assert_awaited_once()
    saved_while_running = native.dump_messages_with_checkpoints(
        run.run_context.messages
    )
    assert "蓝色背景" not in str(saved_while_running)
    assert "qq://bot/123/image/native?m=1&i=0" in str(saved_while_running)
    # Final cleanup also works if another serializer has lost private temp flags.
    run.run_context.messages = [
        native.Message.model_validate(m.model_dump()) for m in run.run_context.messages
    ]
    await service.policy.finish(run)
    saved = native.dump_messages_with_checkpoints(run.run_context.messages)
    assert "蓝色背景" not in str(saved) and "qq://bot/123/image/native?m=1&i=0" in str(
        saved
    )
    next_run = runner(
        service.event,
        request(),
        [
            *native.bind_checkpoint_messages(saved),
            native.Message(role="user", content="unrelated new turn"),
        ],
    )
    await service.policy.before_step(next_run)
    assert "蓝色背景" not in str(next_run.run_context.messages)
    service.context.llm_generate.assert_awaited_once()


@pytest.mark.parametrize("status", ["DONE", "ERROR"])
async def test_runner_completion_cleans_own_results_with_images_disabled(
    service, status
):
    service.config["channel_settings"][0]["image_mode"] = "disabled"
    organizer = ContextOrganizer(service.context, service.settings, lambda event: True)
    organizer.policy, organizer.channels = service.policy, service.channels

    class Runner(RunnerBoundary):
        def __init__(self):
            self.req = request()
            self.run_context = SimpleNamespace(
                context=SimpleNamespace(event=service.event), messages=[]
            )

    integration = RunnerIntegration(organizer)
    integration.install(Runner)
    try:
        run = Runner()
        await run.reset()
        await run.request_context_manager.process(run.run_context.messages)
        run.run_context.messages.extend(describe_exchange())
        run.run_context.messages.append(tool_image("unchanged pixels"))
        await run.request_context_manager.process(run.run_context.messages)
        assert DESCRIPTION in str(run.run_context.messages)
        run._transition_state(status)
        assert DESCRIPTION not in str(run.run_context.messages)
        assert image_count([run.run_context.messages[-1]]) == 1
    finally:
        integration.terminate()


async def test_failed_or_non_json_own_tool_output_is_preserved(service):
    for content in (
        '{"ok":false,"error":"source unavailable"}',
        "invalid JSON",
        "[1,2]",
    ):
        exchange = describe_exchange()
        exchange[1]["content"] = content
        run = runner(service.event, request(), exchange)
        await service.policy.before_step(run)
        assert exchange[1]["content"] == content


@pytest.mark.parametrize("structured", [False, True])
async def test_real_tool_result_history_keeps_pairing_and_image_id(
    service, runtime, structured
):
    native = runtime.messages
    service.policy.text_factory = native.TextPart
    run = runner(
        service.event, request(), [native.Message(role="user", content="inspect")]
    )
    await service.policy.before_step(run)
    exchange = describe_exchange()
    if structured:
        exchange[1]["content"] = [dict(type="text", text=exchange[1]["content"])]
    run.run_context.messages.extend(
        native.Message.model_validate(message) for message in exchange
    )
    await service.policy.before_step(run)
    assert DESCRIPTION in str(run.run_context.messages)
    await service.policy.finish_context(service.event, run.run_context)
    saved = native.dump_messages_with_checkpoints(run.run_context.messages)
    assert DESCRIPTION not in str(saved)
    loaded = native.bind_checkpoint_messages(saved)
    assert loaded[1].tool_calls[0].function.name == "describe_channel_image"
    assert loaded[1].tool_calls[0].id == loaded[2].tool_call_id == "describe-1"
    content = loaded[2].content
    if structured:
        content = content[0].text
    payload = json.loads(content)
    assert payload["image_id"] == "qq://bot/123/image/native?m=1&i=0"
    assert "description" not in payload and payload["provider_id"] == "vision"


async def test_caption_cleanup_keeps_original_image_marker(service):
    marker = "[图片 id=qq://bot/123/image/native?m=1&i=0 来源=current]"
    review = dict(
        role="user",
        content=[
            dict(type="text", text=TOOL_HEADER),
            dict(type="text", text=CAPTION_PREFIX + DESCRIPTION),
            dict(type="text", text=marker),
        ],
    )
    exchange = describe_exchange()
    payload = json.loads(exchange[1]["content"])
    run = runner(
        service.event,
        request(),
        [*exchange, review, dict(role="user", content="new turn")],
    )
    await service.policy.before_step(run)
    assert review["content"] == [
        dict(type="text", text=TOOL_HEADER),
        dict(type="text", text=marker),
    ]
    result = json.loads(exchange[1]["content"])
    assert "description" not in result and result["image_id"] == payload["image_id"]
    assert "已转述" not in str(run.run_context.messages) and "已省略" not in str(
        run.run_context.messages
    )


async def test_history_projection_does_not_change_caption_cache_or_original_id(service):
    first = await service.policy.describe_images(
        service.event, ["native_current"], "内容"
    )
    run = runner(service.event, request(), [dict(role="user", content="inspect")])
    await service.policy.before_step(run)
    exchange = describe_exchange()
    exchange[1]["content"] = json.dumps(first, ensure_ascii=False)
    run.run_context.messages.extend(exchange)
    await service.policy.before_step(run)
    assert json.loads(exchange[1]["content"])["description"] == first["description"]
    await service.policy.finish(run)
    saved = json.loads(exchange[1]["content"])
    assert saved["image_ids"] == first["image_ids"] == ["native_current"]
    assert "description" not in saved
    second = await service.policy.describe_images(
        service.event, ["native_current"], "内容"
    )
    assert second["cached"] and second["description"] == first["description"]
    assert second["image_ids"] == first["image_ids"]
    service.context.llm_generate.assert_awaited_once()
