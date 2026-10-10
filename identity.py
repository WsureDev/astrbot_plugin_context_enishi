from __future__ import annotations

from pathlib import Path

PLUGIN = "astrbot_plugin_context_enishi"
LEGACY_PLUGIN = "astrbot_plugin_channel_media_tools"
LEGACY_PLUGINS = ("astrbot_plugin_context_manager", LEGACY_PLUGIN)


def caption_data_directory(current: Path) -> Path:
    """Reuse the old text cache without moving or overwriting existing data."""
    if not (current / "captions.sqlite3").is_file():
        for name in LEGACY_PLUGINS:
            legacy = current.parent / name
            if (legacy / "captions.sqlite3").is_file():
                return legacy
    return current
