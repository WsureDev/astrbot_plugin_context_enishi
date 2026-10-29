"""Identify card envelopes and expose exact links without fetching their targets."""

from __future__ import annotations

import json
import re
from xml.etree import ElementTree

URL_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
MARKDOWN_URL = re.compile(r"(?:https?://)[^\s<>\[\]()\"']+")
MINIAPP_PREFIX = "com.tencent.miniapp"
FORWARD_APP = "com.tencent.multimsg"
LINK_APP = "com.tencent.structmsg"


def links_in(value, path=""):
    output = []
    if isinstance(value, dict):
        for key, item in value.items():
            field = str(key)
            if path:
                field = path + "." + field
            output.extend(links_in(item, field))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            output.extend(links_in(item, f"{path}[{index}]"))
    elif isinstance(value, str) and URL_PREFIX.match(value):
        output.append({"field": path, "url": value})
    return output


def content_features(kind, data):
    output = {}
    raw = data.get("data")
    if kind in {"json", "miniapp"}:
        try:
            decoded = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError) as exc:
            return {"decode_error": str(exc)}
        if isinstance(decoded, dict):
            app = str(decoded.get("app", ""))
            output.update(app=app, view=decoded.get("view", ""))
            output["links"] = links_in(decoded)
            if app.startswith(MINIAPP_PREFIX) or kind == "miniapp":
                output["kind"] = "miniapp"
            elif app == FORWARD_APP:
                output["kind"] = "forward_card"
            elif app == LINK_APP:
                output["kind"] = "structured_card"
            else:
                output["kind"] = "json_card"
    elif kind == "xml" and isinstance(raw, str):
        try:
            root = ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            return {"decode_error": str(exc)}
        output["links"] = []
        for node in root.iter():
            output["links"].extend(links_in(node.attrib, node.tag))
            if node.text and URL_PREFIX.match(node.text.strip()):
                output["links"].append({"field": node.tag, "url": node.text.strip()})
    elif kind == "markdown":
        text = str(data.get("content") or data.get("markdown") or "")
        output["links"] = [
            {"field": "content", "url": match.group()}
            for match in MARKDOWN_URL.finditer(text)
        ]
    else:
        output["links"] = links_in(data)
    return output
