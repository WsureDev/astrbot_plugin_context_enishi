from __future__ import annotations

import ast
import base64
import io
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from astrbot_plugin_context_enishi.channels.media import ChannelError
from astrbot_plugin_context_enishi.channels.native import ImageRef
from astrbot_plugin_context_enishi.context.captions import CaptionHistory
from astrbot_plugin_context_enishi.context.image_batch import (
    BACKGROUND,
    GAP,
    LABEL_HEIGHT,
    compose_images,
)

from .conftest import text_factory
from .test_main import Config
from .test_main import plugin_class as plugin_class

COLORS = [
    (220, 20, 30),
    (10, 160, 30),
    (20, 40, 220),
    (230, 160, 10),
    (130, 20, 200),
    (10, 180, 190),
]


def image_data(color, size=(300, 180), *, mode="RGB", exif=None):
    output = io.BytesIO()
    with Image.new(mode, size, color) as image:
        if exif is None:
            image.save(output, format="PNG")
        else:
            image.save(output, format="JPEG", exif=exif)
    return output.getvalue()


def assert_color(actual, expected):
    assert max(abs(a - b) for a, b in zip(actual, expected)) <= 5


def test_six_image_grid_has_separate_numbered_panels_and_pixel_gaps():
    data, layout = compose_images([image_data(color) for color in COLORS])
    assert layout["rows"] == 3 and layout["columns"] == 2
    assert layout["gap_pixels"] == GAP and GAP > 0
    labels = []
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (layout["width"], layout["height"])
        for panel, color in zip(layout["panels"], COLORS):
            assert_color(
                image.getpixel(
                    (
                        panel["x"] + panel["width"] // 2,
                        panel["y"] + panel["height"] // 2,
                    )
                ),
                color,
            )
            labels.append(
                image.crop(
                    (
                        panel["x"],
                        panel["y"] - LABEL_HEIGHT,
                        panel["x"] + panel["width"],
                        panel["y"],
                    )
                ).tobytes()
            )
        assert len(set(labels)) == 6
        assert_color(image.getpixel((GAP // 2, image.height // 2)), BACKGROUND)
        assert_color(image.getpixel((image.width // 2, image.height // 2)), BACKGROUND)
        row_gap = GAP + LABEL_HEIGHT + 180 + GAP // 2
        assert_color(image.getpixel((image.width // 4, row_gap)), BACKGROUND)


def test_collage_preserves_aspect_orientation_and_transparency():
    exif = Image.Exif()
    exif[274] = 6
    data, layout = compose_images(
        [
            image_data((200, 10, 10), (160, 80), exif=exif),
            image_data((0, 0, 0, 0), (80, 160), mode="RGBA"),
            image_data((10, 20, 200), (2400, 1200)),
        ]
    )
    first, transparent, large = layout["panels"]
    assert (first["width"], first["height"]) == (80, 160)
    assert large["width"] / large["height"] == 2
    assert large["width"] == 2048
    with Image.open(io.BytesIO(data)) as image:
        assert_color(
            image.getpixel((transparent["x"] + 40, transparent["y"] + 80)),
            (255, 255, 255),
        )


async def test_batch_caption_uses_one_collage_and_caches_order_and_question(service):
    ids = [
        ImageRef(
            "telegram", "bot", "-100#2", "image", f"native_{index}", "42", index
        ).uri
        for index in range(6)
    ]
    sources = {key: image_data(color) for key, color in zip(ids, COLORS)}

    async def read(event, key, **kwargs):
        data = sources[key]
        return data, service.channels.image_metadata(data)

    service.channels.read_image = AsyncMock(side_effect=read)
    first = await service.policy.describe_images(service.event, ids, "比较这些图的关联")
    assert first["image_ids"] == ids and first["image_count"] == 6
    assert [image["index"] for image in first["images"]] == list(range(1, 7))
    call = service.context.llm_generate.call_args.kwargs
    assert len(call["image_urls"]) == 1 and call["contexts"] == []
    assert "6 张独立图片" in call["prompt"] and "比较这些图的关联" in call["prompt"]
    with Image.open(
        io.BytesIO(base64.b64decode(call["image_urls"][0].split(",", 1)[1]))
    ) as image:
        assert image.size == (first["collage"]["width"], first["collage"]["height"])
    second = await service.policy.describe_images(
        service.event, ids, "比较这些图的关联"
    )
    assert second["cached"] and second["image_ids"] == ids
    assert service.context.llm_generate.await_count == 1
    assert service.channels.read_image.await_count == 12
    reversed_result = await service.policy.describe_images(
        service.event, ids[::-1], "比较这些图的关联"
    )
    assert not reversed_result["cached"] and reversed_result["image_ids"] == ids[::-1]
    await service.policy.describe_images(service.event, ids, "分别读出每张图的文字")
    assert service.context.llm_generate.await_count == 3
    assert "data:image" not in json.dumps(first)


@pytest.mark.parametrize(
    "ids", [[], ["native"] * 7, "native", [None], ["native", "/tmp/a.png"]]
)
async def test_invalid_batch_is_rejected_before_reading_sources(service, ids):
    service.channels.read_image = AsyncMock()
    with pytest.raises((ChannelError, ValueError)):
        await service.policy.describe_images(service.event, ids, "内容")
    service.channels.read_image.assert_not_called()
    service.context.llm_generate.assert_not_called()


async def test_batch_source_failure_never_silently_changes_numbering(
    service, image_bytes
):
    service.channels.read_image = AsyncMock(
        side_effect=[
            (image_bytes, service.channels.image_metadata(image_bytes)),
            ChannelError("expired"),
        ]
    )
    with pytest.raises(ChannelError, match="第 2 张图片.*second.*expired"):
        await service.policy.describe_images(service.event, ["first", "second"], "关联")
    service.context.llm_generate.assert_not_called()


@pytest.mark.parametrize("structured", [False, True])
def test_batch_description_expires_but_every_full_native_id_survives(structured):
    ids = [
        f"qq://bot/123/image/image_{index}.jpg?m=42&i={index}&f=forward&n=0.{index}"
        for index in range(6)
    ]
    payload = {
        "image_ids": ids,
        "image_count": 6,
        "description": "本轮逐图内容",
        "collage": {"rows": 3, "columns": 2},
    }
    content = json.dumps(payload, ensure_ascii=False)
    if structured:
        content = [text_factory(text=content)]
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "batch",
                    "function": {
                        "name": "describe_channel_image",
                        "arguments": json.dumps({"image_ids": ids}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "batch", "content": content},
    ]
    history = CaptionHistory()
    history.process(messages, text_factory, expire=False)
    assert "本轮逐图内容" in str(messages)
    history.process(messages, text_factory, expire=True)
    output = messages[1]["content"]
    if structured:
        output = output[0]["text"]
    saved = json.loads(output)
    assert saved["image_ids"] == ids and "description" not in saved
    assert messages[1]["tool_call_id"] == "batch"
    assert "已转述" not in output and "已省略" not in output


async def test_tool_entrypoint_accepts_arrays_and_reports_limit(service, plugin_class):
    plugin = plugin_class(service.context, Config(service.config))
    plugin.organizer.policy = service.policy
    result = json.loads(
        await plugin.describe_channel_image(service.event, ["native_current"], "内容")
    )
    assert result["image_ids"] == ["native_current"] and result["collage"] is None
    assert (
        json.loads(await plugin.describe_channel_image(service.event, ["native"] * 7))[
            "ok"
        ]
        is False
    )


def test_actual_astrbot_registration_publishes_string_array_and_batch_guidance(
    plugin_class,
):
    import docstring_parser

    source = os.environ.get("ASTRBOT_SOURCE")
    if not source:
        pytest.skip("set ASTRBOT_SOURCE for native tool registration")
    root = Path(source)
    manager = ast.parse(
        (root / "astrbot/core/provider/func_tool_manager.py").read_text()
    )
    constants = {}
    for node in manager.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "SUPPORTED_TYPES",
                    "PY_TO_JSON_TYPE",
                }:
                    constants[target.id] = ast.literal_eval(node.value)
    path = root / "astrbot/core/star/register/star_handler.py"
    tree = ast.parse(path.read_text())
    register = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "register_llm_tool"
    )
    tools = SimpleNamespace(add_func=Mock())
    namespace = {
        **constants,
        "re": re,
        "docstring_parser": docstring_parser,
        "llm_tools": tools,
        "EventType": SimpleNamespace(OnCallingFuncToolEvent=object()),
        "get_handler_or_create": lambda function, event: SimpleNamespace(
            handler=function
        ),
    }
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            register,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    namespace["register_llm_tool"]()(plugin_class.describe_channel_image)
    name, arguments, description, _ = tools.add_func.call_args.args
    assert name == "describe_channel_image"
    image_ids = next(arg for arg in arguments if arg["name"] == "image_ids")
    assert image_ids["type"] == "array" and image_ids["items"] == {"type": "string"}
    assert (
        "同一人" in description and "6" in description and "间隔像素带" in description
    )
