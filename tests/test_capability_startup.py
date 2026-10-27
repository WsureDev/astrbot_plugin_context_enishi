from __future__ import annotations

import sys
from types import ModuleType

from mcp import ClientSession

from astrbot_plugin_context_enishi.capabilities.service import CapabilityService
from astrbot_plugin_context_enishi.capabilities.web import CapabilityAPI

from .capability_support import Tool, ToolSet
from .test_runtime import runtime as runtime


async def test_startup_and_shutdown_restore_all_global_hooks(
    tmp_path, monkeypatch, runtime
):
    class Manager:
        def __init__(self):
            self.func_list = [Tool("one")]
            self.builtin_func_list = {}
            self.mcp_client_dict = {}

        def get_full_tool_set(self):
            return ToolSet(self.func_list)

        def add_func(self, tool):
            self.func_list.append(tool)

        def remove_func(self, name):
            self.func_list = [tool for tool in self.func_list if tool.name != name]

    class Context:
        def __init__(self):
            self.manager = Manager()
            self.registered_web_apis = []

        def get_llm_tool_manager(self):
            return self.manager

        def get_registered_star(self, name):
            return None

        def register_web_api(self, *args):
            self.registered_web_apis.append(args)

        def add_llm_tools(self, *tools):
            self.manager.func_list.extend(tools)

    class SkillManager:
        def list_skills(self, **kwargs):
            return []

    class Client:
        async def _do_connect(self, *args, **kwargs):
            pass

        async def list_tools_and_save(self):
            pass

    class Runner:
        async def reset(self, *args, **kwargs):
            pass

        def _transition_state(self, state):
            pass

        async def _handle_function_tools(self, *args, **kwargs):
            yield None

    core = ModuleType("astrbot.core")
    agent = ModuleType("astrbot.core.astr_main_agent")

    async def native_skills(*args, **kwargs):
        pass

    agent._ensure_persona_and_skills = native_skills
    agent.build_skills_prompt = lambda skills: "native"
    core.astr_main_agent = agent
    monkeypatch.setitem(sys.modules, core.__name__, core)
    for name, fields in (
        ("astrbot.core.agent.message", {"Message": runtime.messages.Message}),
        ("astrbot.core.agent.tool", {"FunctionTool": Tool, "ToolSet": ToolSet}),
        ("astrbot.core.agent.mcp_client", {"MCPClient": Client}),
        ("astrbot.core.provider.func_tool_manager", {"FunctionToolManager": Manager}),
        ("astrbot.core.star.context", {"Context": Context}),
        ("astrbot.core.skills.skill_manager", {"SkillManager": SkillManager}),
    ):
        module = ModuleType(name)
        module.__dict__.update(fields)
        monkeypatch.setitem(sys.modules, name, module)
    original = ClientSession._received_notification
    context = Context()
    service = CapabilityService(context, {"capabilities": {"enabled": True}})
    api = CapabilityAPI(context, service)
    await service.initialize(tmp_path, Runner, None)
    api.install()
    assert service.snapshot()["ready"]
    assert len(context.registered_web_apis) == 5
    assert service.store.groups["plugin:example"]["mode"] == "folded"
    api.close()
    await service.close()
    assert not context.registered_web_apis
    assert not service.tasks
    assert ClientSession._received_notification is original
    assert agent._ensure_persona_and_skills is native_skills
