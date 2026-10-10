from __future__ import annotations

from pathlib import Path

PLUGIN = "astrbot_plugin_context_manager"
LEGACY_PLUGIN = "astrbot_plugin_channel_media_tools"


def caption_data_directory(current: Path) -> Path:
    """Reuse the old text cache without moving or overwriting existing data."""
    legacy = current.parent / LEGACY_PLUGIN
    if (
        not (current / "captions.sqlite3").is_file()
        and (legacy / "captions.sqlite3").is_file()
    ):
        return legacy
    return current
