from __future__ import annotations

import ast
import copy
import json
import logging
import os
from pathlib import Path

import pytest


def test_schema_defaults_and_save_validate_with_actual_astrbot_code():
    source = os.environ.get("ASTRBOT_SOURCE")
    if not source:
        pytest.skip("set ASTRBOT_SOURCE for native configuration validation")
    root = Path(source)
    constants = ast.parse((root / "astrbot/core/config/default.py").read_text())
    default_types = ast.literal_eval(
        next(
            n.value
            for n in constants.body
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "DEFAULT_VALUE_MAP"
                for t in n.targets
            )
        )
    )
    config_ast = ast.parse((root / "astrbot/core/config/astrbot_config.py").read_text())
    klass = next(
        n
        for n in config_ast.body
        if isinstance(n, ast.ClassDef) and n.name == "AstrBotConfig"
    )
    defaults = next(
        n
        for n in klass.body
        if isinstance(n, ast.FunctionDef)
        and n.name == "_config_schema_to_default_config"
    )
    service_ast = ast.parse(
        (root / "astrbot/dashboard/services/config_service.py").read_text()
    )
    names = {"try_cast", "_expect_type", "_validate_template_list", "validate_config"}
    helpers = [
        n
        for n in service_ast.body
        if isinstance(n, ast.FunctionDef) and n.name in names
    ]
    future = ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0
    )
    namespace = {
        "DEFAULT_VALUE_MAP": default_types,
        "logger": logging.getLogger("config-test"),
        "copy": copy,
    }
    module = ast.Module(body=[future, defaults, *helpers], type_ignores=[])
    exec(
        compile(
            ast.fix_missing_locations(module), "<native AstrBot config methods>", "exec"
        ),
        namespace,
    )
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text())
    data = namespace["_config_schema_to_default_config"](None, schema)
    errors, validated = namespace["validate_config"](
        copy.deepcopy(data), schema, is_core=False
    )
    assert errors == []
    assert len(validated["channel_settings"]) == 2
    assert validated["channel_settings"][0]["context"]["image_mode"] == "disabled"
    assert validated["channel_settings"][1]["context"]["caption_cache_ttl_hours"] == 0
    assert validated["channel_settings"][1]["awareness"]["inject_environment"] is False
