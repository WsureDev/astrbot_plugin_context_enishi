from __future__ import annotations

from types import SimpleNamespace

from astrbot_plugin_context_enishi.capabilities.catalog import (
    find,
    skill_entries,
    tool_entries,
    tool_origin,
)
from astrbot_plugin_context_enishi.capabilities.state import StateStore

from .capability_support import Tool, ToolSet


def test_new_tools_inherit_group_and_new_default_only_applies_to_new_groups(tmp_path):
    store = StateStore(tmp_path)
    first = Tool("one")
    entries = tool_entries(ToolSet([first]))
    store.discover(entries, "folded")
    group = next(iter(entries.values())).group
    store.update(group, "primary", 9, name="My Tools")
    second = Tool("two")
    entries = tool_entries(ToolSet([first, second]))
    store.discover(entries, "folded")
    assert all(store.mode(entry) == "primary" for entry in entries.values())
    third = Tool("three", handler_module_path="data.plugins.another.main")
    new_entries = tool_entries(ToolSet([third]))
    store.discover(new_entries, "folded")
    assert store.mode(next(iter(new_entries.values()))) == "folded"
    store.save()
    restored = StateStore(tmp_path)
    assert restored.groups[group] == {
        "mode": "primary",
        "idle_rounds": 9,
        "name": "My Tools",
    }


def test_mcp_origin_comes_from_permission_wrapped_server_identity():
    raw = Tool("one")
    raw.mcp_server_name = "my server"
    wrapped = Tool("one")
    wrapped._wrapped = raw
    assert tool_origin(wrapped) == "mcp:my server"
    entries = tool_entries(ToolSet([wrapped]), freeze=True)
    entry = next(iter(entries.values()))
    assert entry.identity == id(raw)
    assert entry.value._wrapped is raw


def test_schema_and_description_preserved_and_active_run_schema_is_frozen():
    tool = Tool(
        "one",
        description="Full text\n" * 2000,
        parameters={
            "type": "object",
            "properties": {"q": {"type": "string", "enum": ["a", "b"]}},
            "required": ["q"],
        },
    )
    entry = next(iter(tool_entries(ToolSet([tool]), freeze=True).values()))
    assert entry.value.parameters == tool.parameters
    assert entry.value.description == tool.description
    tool.parameters["properties"]["q"]["enum"].append("c")
    assert entry.value.parameters["properties"]["q"]["enum"] == ["a", "b"]


def test_search_paginates_and_skill_description_is_not_shortened():
    tools = [Tool(f"same_{index:03}") for index in range(80)]
    entries = tool_entries(ToolSet(tools))
    first, total = find(entries, "same", limit=12)
    second, _ = find(entries, "same", offset=12, limit=12)
    assert total == 80 and not set(entry.id for entry in first).intersection(
        entry.id for entry in second
    )
    description = "Important condition " * 100 + "FINAL TRIGGER"
    skill = SimpleNamespace(
        name="documents",
        path="/tmp/documents/SKILL.md",
        description=description,
        source_type="local",
        plugin_name="",
    )
    selected, total = find(skill_entries([skill]), "documents")
    assert total == 1 and selected[0].description == description
