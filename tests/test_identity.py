from __future__ import annotations

from astrbot_plugin_context_enishi.context.storage import CaptionCache
from astrbot_plugin_context_enishi.identity import (
    LEGACY_PLUGIN,
    PLUGIN,
    caption_data_directory,
)


def test_rename_reuses_legacy_caption_text_without_moving_files(tmp_path):
    legacy = tmp_path / LEGACY_PLUGIN
    cache = CaptionCache(legacy)
    cache.save_caption("image", "已有转述", "provider")
    cache.close()
    current = tmp_path / PLUGIN
    selected = caption_data_directory(current)
    reopened = CaptionCache(selected)
    try:
        assert reopened.caption("image")["description"] == "已有转述"
        assert not current.exists()
        assert (legacy / "captions.sqlite3").is_file()
    finally:
        reopened.close()


def test_new_caption_cache_takes_priority_over_legacy(tmp_path):
    for name, description in ((LEGACY_PLUGIN, "旧缓存"), (PLUGIN, "新缓存")):
        cache = CaptionCache(tmp_path / name)
        cache.save_caption("image", description, "provider")
        cache.close()
    current = tmp_path / PLUGIN
    cache = CaptionCache(caption_data_directory(current))
    try:
        assert cache.caption("image")["description"] == "新缓存"
    finally:
        cache.close()


def test_fresh_install_uses_new_directory(tmp_path):
    current = tmp_path / PLUGIN
    assert caption_data_directory(current) == current
    assert not (tmp_path / LEGACY_PLUGIN).exists()
