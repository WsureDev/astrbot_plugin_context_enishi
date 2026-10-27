from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_context_enishi.capabilities.catalog import skill_entries

from .capability_support import Tool, run_for
from .capability_support import capabilities as capabilities


async def test_idle_result_folds_independently_and_restores_original_with_pairing(
    capabilities,
):
    tool = Tool("one")
    run = await run_for(capabilities, [tool])
    await run.load(["one"])
    run.calls_started(
        SimpleNamespace(tools_call_name=["one"], tools_call_ids=["call-one"])
    )
    run.tool_started("one")
    text = "Full result\n" * 200000 + "TAIL"
    message = {"role": "tool", "tool_call_id": "call-one", "content": text}
    await run.before_step([message], capabilities.message_class)
    ref = run.session.artifacts["call-one"]
    run.finish(True)
    for _ in range(5):
        run = await run_for(capabilities, [tool])
        # Calling the service again does not renew its old result.
        run.tool_started("one")
        run.finish(True)
    run = await run_for(capabilities, [tool])
    await run.before_step([message], capabilities.message_class)
    assert message["tool_call_id"] == "call-one"
    assert json.loads(message["content"])["enishi_content_ref"] == ref
    await run.load([ref])
    await run.before_step([message], capabilities.message_class)
    assert message["content"] == text


async def test_restored_result_survives_original_tool_turn_truncation(capabilities):
    run = await run_for(capabilities, [Tool("one")])
    ref = await capabilities.io(
        capabilities.artifacts.put, run.key, "plugin:example/one", "complete"
    )
    await run.load([ref])
    messages = []
    await run.before_step(messages, capabilities.message_class)
    assert len(messages) == 1 and "complete" in messages[0]["content"][0]["text"]
    assert messages[0]["_no_save"]
    await run.before_step(messages, capabilities.message_class)
    assert len(messages) == 1


async def test_content_reference_is_isolated_and_media_is_not_archived(capabilities):
    first = await run_for(capabilities, [Tool("one")], cid="one")
    ref = await capabilities.io(
        capabilities.artifacts.put, first.key, "plugin:example/one", "private"
    )
    second = await run_for(capabilities, [Tool("one")], cid="two")
    with pytest.raises(ValueError):
        await second.load([ref])
    first.calls["image"] = "plugin:example/one"
    await first.before_step(
        [
            {
                "role": "tool",
                "tool_call_id": "image",
                "content": "data:image/png;base64,abc",
            }
        ],
        capabilities.message_class,
    )
    assert "image" not in first.session.artifacts


async def test_skill_full_text_is_temporary_restorable_and_not_truncated(
    capabilities, tmp_path
):
    path = tmp_path / "SKILL.md"
    text = "# Skill\n" + "complete instructions\n" * 20000 + "FINAL REQUIREMENT"
    path.write_text(text)
    skill = SimpleNamespace(
        name="documents",
        path=str(path),
        description="Full description",
        source_type="local",
    )
    run = await run_for(capabilities, skills=skill_entries([skill]))
    result = await run.load(["documents"])
    ref = result[0]["content_ref"]
    messages = []
    await run.before_step(messages, capabilities.message_class)
    assert text in messages[0]["content"][0]["text"] and messages[0]["_no_save"]
    await run.unload(["documents"])
    await run.before_step(messages, capabilities.message_class)
    assert not messages
    await run.load([ref])
    await run.before_step(messages, capabilities.message_class)
    assert text in messages[0]["content"][0]["text"]


async def test_full_tool_result_archived_before_native_preview(capabilities):
    import mcp.types

    run = await run_for(capabilities, [Tool("one")])
    await run.load(["one"])
    run.calls_started(SimpleNamespace(tools_call_name=["one"], tools_call_ids=["call"]))
    run.tool_started("one")
    full = "complete result\n" * 100000 + "TAIL"
    result = mcp.types.CallToolResult(
        content=[mcp.types.TextContent(type="text", text=full)]
    )
    await run.resources.capture_result("call", result)
    message = {
        "role": "tool",
        "tool_call_id": "call",
        "content": "native preview; original spilled to file",
    }
    await run.before_step([message], capabilities.message_class)
    assert message["content"] == "native preview; original spilled to file"
    ref = run.session.artifacts["call"]
    archived = await capabilities.io(capabilities.artifacts.read, run.key, ref)
    assert archived["content"] == full
    assert archived["original"]["content"][0]["text"] == full
    await run.load([ref])
    await run.before_step([message], capabilities.message_class)
    assert message["content"] == full


async def test_reused_tool_call_id_does_not_replace_older_result_with_new_content(
    capabilities,
):
    first = await run_for(capabilities, [Tool("one")])
    await first.load(["one"])
    first.calls_started(
        SimpleNamespace(tools_call_name=["one"], tools_call_ids=["reused"])
    )
    first.tool_started("one")
    old = {"role": "tool", "tool_call_id": "reused", "content": "old payload"}
    await first.before_step([old], capabilities.message_class)
    old_ref = first.session.artifacts["reused"]
    first.finish(True)
    second = await run_for(capabilities, [Tool("one")])
    runner = SimpleNamespace(run_context=SimpleNamespace(messages=[old]))
    second.runner = lambda: runner
    second.calls_started(
        SimpleNamespace(tools_call_name=["one"], tools_call_ids=["reused"])
    )
    second.tool_started("one")
    new = {"role": "tool", "tool_call_id": "reused", "content": "new payload"}
    await second.before_step([old, new], capabilities.message_class)
    new_ref = second.session.artifacts["reused"]
    assert old_ref != new_ref
    await second.unload([old_ref, new_ref])
    await second.before_step([old, new], capabilities.message_class)
    assert json.loads(old["content"])["enishi_content_ref"] == old_ref
    assert json.loads(new["content"])["enishi_content_ref"] == new_ref
    await second.before_step([old, new], capabilities.message_class)
    assert json.loads(old["content"])["enishi_content_ref"] == old_ref


async def test_caption_description_is_not_duplicated_into_generic_archive(capabilities):
    run = await run_for(capabilities, [Tool("describe_channel_image")])
    await run.load(["describe_channel_image"])
    run.calls_started(
        SimpleNamespace(
            tools_call_name=["describe_channel_image"], tools_call_ids=["image"]
        )
    )
    run.tool_started("describe_channel_image")
    message = {
        "role": "tool",
        "tool_call_id": "image",
        "content": '{"image_id":"native","description":"caption"}',
    }
    await run.before_step([message], capabilities.message_class)
    assert "image" not in run.session.artifacts


async def test_structured_only_tool_result_remains_complete_and_restorable(
    capabilities,
):
    import mcp.types

    run = await run_for(capabilities, [Tool("one")])
    await run.load(["one"])
    run.calls_started(
        SimpleNamespace(tools_call_name=["one"], tools_call_ids=["structured"])
    )
    run.tool_started("one")
    data = {"values": list(range(2000)), "tail": "END"}
    result = mcp.types.CallToolResult(content=[], structuredContent=data)
    await run.resources.capture_result("structured", result)
    message = {
        "role": "tool",
        "tool_call_id": "structured",
        "content": "The tool returned no content.",
    }
    await run.before_step([message], capabilities.message_class)
    ref = run.session.artifacts["structured"]
    await run.load([ref])
    await run.before_step([message], capabilities.message_class)
    assert json.loads(message["content"])["structuredContent"] == data
