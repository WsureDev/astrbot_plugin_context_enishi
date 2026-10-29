from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest

from astrbot_plugin_context_enishi.awareness.service import ChatAwareness
from astrbot_plugin_context_enishi.capabilities.service import CapabilityService
from astrbot_plugin_context_enishi.capabilities.web import CapabilityAPI
from astrbot_plugin_context_enishi.channels.native import field
from astrbot_plugin_context_enishi.channels.transport import ChannelError
from astrbot_plugin_context_enishi.context.additions import append_temporary_text
from astrbot_plugin_context_enishi.context.service import ContextOrganizer
from astrbot_plugin_context_enishi.identity import PLUGIN
from astrbot_plugin_context_enishi.settings import (
    MODE_LABELS,
    Settings,
    migrate_channel_settings,
    platform_name,
)

from .conftest import request, text_factory


@pytest.fixture
def plugin_class():
    path = Path(__file__).parents[1] / "main.py"
    tree = ast.parse(path.read_text())
    klass = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "EnishiPlugin"
    )
    for node in klass.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.decorator_list = []

    class Star:
        def __init__(self, context):
            pass

    namespace = dict(
        Star=Star,
        json=json,
        Path=Path,
        PLUGIN=PLUGIN,
        ContextOrganizer=ContextOrganizer,
        ChatAwareness=ChatAwareness,
        CapabilityService=CapabilityService,
        CapabilityAPI=CapabilityAPI,
        Settings=Settings,
        migrate_channel_settings=migrate_channel_settings,
        field=field,
        platform_name=platform_name,
        MODE_LABELS=MODE_LABELS,
        append_temporary_text=append_temporary_text,
        TextPart=text_factory,
        ChannelError=ChannelError,
        logger=logging.getLogger("main-test"),
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            klass,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace["EnishiPlugin"]


class Config(dict):
    saved = 0

    def save_config(self):
        self.saved += 1


def test_entrypoint_migrates_legacy_settings_once_and_has_separate_subsystems(
    service, plugin_class
):
    config = Config(service.config)
    plugin = plugin_class(service.context, config)
    assert config.saved == 1
    assert config["channel_settings"][0]["context"]["image_mode"] == "reference_only"
    assert plugin.organizer.settings is plugin.awareness.settings
    assert plugin.organizer is not plugin.awareness
    plugin_class(service.context, config)
    assert config.saved == 1


async def test_entrypoint_disabled_awareness_tools_do_not_query_backend(
    service, plugin_class
):
    config = Config(
        {
            "channel_settings": [
                {
                    "__template_key": "telegram",
                    "context": {"image_mode": "disabled"},
                    "awareness": {"enabled": False},
                }
            ]
        }
    )
    plugin = plugin_class(service.context, config)
    for call in (
        plugin.get_group_participants(service.event),
        plugin.get_chat_environment(service.event),
        plugin.get_channel_member_info(service.event),
        plugin.get_channel_user_avatar(service.event),
        plugin.get_channel_message(service.event, "1"),
        plugin.get_channel_message_content(service.event, "qq://bot/123/json/1?i=0"),
        plugin.get_channel_user_history(service.event),
    ):
        assert json.loads(await call)["ok"] is False
    service.event.client.get_file.assert_not_called()
    service.event.bot.call_action.assert_not_called()


async def test_entrypoint_default_request_has_no_automatic_environment_data(
    service, plugin_class
):
    config = Config(
        {
            "channel_settings": [
                {"__template_key": "telegram", "context": {"image_mode": "disabled"}}
            ]
        }
    )
    plugin = plugin_class(service.context, config)
    req = request(["existing-image"])
    await plugin.prepare_media(service.event, req)
    assert req.image_urls == ["existing-image"]
    assert req.system_prompt == "persona" and not req.extra_user_content_parts
    service.event.client.get_file.assert_not_called()
