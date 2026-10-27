from __future__ import annotations

import pytest

from .capability_support import Tool, publish, run_for
from .capability_support import capabilities as capabilities
from .conftest import Event


async def test_load_changes_same_toolset_and_unload_persists_across_rounds(
    capabilities,
):
    tool = Tool("one")
    publish(capabilities, [tool])
    run = await run_for(capabilities, [tool])
    identity = run.request.func_tool
    assert [tool.name for tool in identity.tools] == [
        "enishi_find",
        "enishi_load",
        "enishi_unload",
    ]
    await run.load(["plugin:example"])
    assert run.request.func_tool is identity
    assert identity.get_tool("one") is not None
    await run.unload(["one"])
    assert identity.get_tool("one") is None
    run.finish(True)
    second = await run_for(capabilities, [tool])
    assert second.injected.get_tool("one") is None
    await second.load(["one"])
    assert second.injected.get_tool("one") is not None


async def test_five_completed_idle_user_rounds_fold_before_next_round(capabilities):
    tool = Tool("one")
    run = await run_for(capabilities, [tool])
    await run.load(["one"])
    run.tool_started("one")
    run.finish(True)
    for _ in range(4):
        run = await run_for(capabilities, [tool])
        assert run.injected.get_tool("one") is not None
        run.finish(True)
    last = await run_for(capabilities, [tool])
    assert last.injected.get_tool("one") is not None
    last.finish(True)
    next_run = await run_for(capabilities, [tool])
    assert next_run.injected.get_tool("one") is None
    assert next_run.session.counters["auto_unloads"] > 0


async def test_failed_round_and_repeated_steps_do_not_advance_idle_clock(capabilities):
    tool = Tool("one")
    run = await run_for(capabilities, [tool])
    await run.load(["one"])
    for _ in range(8):
        await run.before_step([], capabilities.message_class)
    assert run.session.completed == 0
    run.finish(False)
    assert run.session.completed == 0
    run.finish(True)
    assert run.session.completed == 0


async def test_mcp_use_renews_group_and_existing_group_new_tool_follows_state(
    capabilities,
):
    first, second = Tool("one"), Tool("two")
    first.mcp_server_name = second.mcp_server_name = "server"
    run = await run_for(capabilities, [first])
    await run.load(["mcp:server"])
    run.finish(True)
    run = await run_for(capabilities, [first, second])
    assert run.injected.get_tool("two") is not None
    for _ in range(6):
        run.tool_started("two")
        run.finish(True)
        run = await run_for(capabilities, [first, second])
        assert run.injected.get_tool("one") is not None


async def test_other_conversation_cannot_inherit_loaded_tools(capabilities):
    tool = Tool("one")
    first = await run_for(capabilities, [tool], cid="one")
    await first.load(["one"])
    first.finish(True)
    second = await run_for(capabilities, [tool], cid="two")
    assert second.injected.get_tool("one") is None


async def test_primary_tools_do_not_expire_and_new_tools_follow_group(capabilities):
    tool = Tool("one")
    first = await run_for(capabilities, [tool])
    capabilities.store.update("plugin:example", "primary")
    first.finish(True)
    for _ in range(8):
        run = await run_for(capabilities, [tool])
        assert run.injected.get_tool("one") is not None
        run.finish(True)
    second = Tool("two")
    run = await run_for(capabilities, [tool, second])
    assert run.injected.get_tool("two") is not None


async def test_invalid_batch_load_is_atomic(capabilities):
    run = await run_for(capabilities, [Tool("one")])
    before = list(run.injected.tools)
    with pytest.raises(ValueError):
        await run.load(["one", "nonexistent"])
    assert run.injected.tools == before and not run.session.leases


async def test_explicit_name_prefetch_has_no_extra_meta_step(capabilities):
    run = await run_for(capabilities, [Tool("one_tool")], prompt="use one_tool")
    assert run.injected.get_tool("one_tool") is not None
    assert run.session.counters["prefetches"] == 1


async def test_native_removal_or_replacement_cannot_use_stale_handler(capabilities):
    tool = Tool("one")
    publish(capabilities, [tool])
    run = await run_for(capabilities, [tool])
    await run.load(["one"])
    publish(capabilities, [Tool("one")])
    run.refresh()
    assert run.injected.get_tool("one") is None
    with pytest.raises(ValueError):
        await run.load(["one"])


async def test_single_tool_reload_does_not_reenable_manually_folded_siblings(
    capabilities,
):
    tools = [Tool("one"), Tool("two")]
    first = await run_for(capabilities, tools)
    capabilities.store.update("plugin:example", "primary")
    await first.unload(["plugin:example"])
    await first.load(["one"])
    assert first.injected.get_tool("one") is not None
    assert first.injected.get_tool("two") is None
    first.finish(True)
    second = await run_for(capabilities, [*tools, Tool("new_sibling")])
    assert second.injected.get_tool("one") is not None
    assert second.injected.get_tool("new_sibling") is None


async def test_inflight_request_is_protected_from_parallel_round_aging(capabilities):
    tool = Tool("one")
    first = await run_for(capabilities, [tool])
    await first.load(["one"])
    first.finish(True)
    inflight = await run_for(capabilities, [tool])
    for _ in range(6):
        other = await run_for(capabilities, [tool])
        other.finish(True)
    inflight.refresh()
    assert inflight.injected.get_tool("one") is not None
    inflight.tool_started("one")
    inflight.finish(True)
    after = await run_for(capabilities, [tool])
    assert after.injected.get_tool("one") is not None


async def test_model_budget_rejects_oversized_expansion_without_partial_schema(
    capabilities,
):
    tool = Tool("big", description="long definition " * 1000)
    run = await run_for(capabilities, [tool])
    run.context_limit = 1000
    with pytest.raises(ValueError, match="预算"):
        await run.load(["big"])
    assert run.injected.get_tool("big") is None
    assert not run.session.leases


async def test_native_restoration_preserves_other_plugin_suffixes(capabilities):
    from astrbot_plugin_context_enishi.capabilities.controller import TOOL_RULES

    from .capability_support import capability_request

    request = capability_request([Tool("one")])
    request.system_prompt = "PERSONA\nFOLDED\nOTHER POLICY"
    request._enishi_skill_blocks = [("FOLDED", "NATIVE FULL SECTION")]
    original = request.func_tool
    run = await capabilities.prepare(Event(), request)
    message = {"role": "system", "content": request.system_prompt + "\nLATER POLICY"}
    run.restore_native([message])
    assert request.func_tool is original
    assert TOOL_RULES not in message["content"]
    assert (
        message["content"] == "PERSONA\nNATIVE FULL SECTION\nOTHER POLICY\nLATER POLICY"
    )


async def test_empty_candidate_set_stays_empty_and_still_advances_idle_clock(
    capabilities,
):
    tool = Tool("one")
    first = await run_for(capabilities, [tool])
    await first.load(["one"])
    first.finish(True)
    for _ in range(5):
        empty = await run_for(capabilities, [])
        assert not empty.injected.tools
        assert empty.request.system_prompt == "persona\n[Other plugin policy] keep it"
        empty.finish(True)
    next_run = await run_for(capabilities, [tool])
    assert next_run.injected.get_tool("one") is None
