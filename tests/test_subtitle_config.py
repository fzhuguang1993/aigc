"""
tests/test_subtitle_config.py —— subtitle_config 默认 + 覆盖深合并

从一开始就把 CONFIG_JSON 重定向到用例临时文件并重置启动缓存，避免像爆款拆解那样被
test_paths_config 的全局前提污染。default_style 深合并：只改个别键时其余仍走默认，
且不把内置默认字典就地改坏。
"""
import pytest

import core.config as cfg


@pytest.fixture(autouse=True)
def _isolate_config_json(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_JSON", tmp_path / "config.json")
    monkeypatch.setattr(cfg, "_JSON_CACHE", None)


def test_defaults_when_no_section():
    c = cfg.subtitle_config()
    assert c["model_size"] == "medium"
    assert c["output_suffix"] == "_字幕"
    assert c["default_style"]["font_size"] == 16
    assert c["default_style"]["primary"] == "#FFFFFF"


def test_override_deep_merges_style():
    cfg.write_section("subtitle", {
        "model_size": "small",
        "default_style": {"font_size": 24, "align": "top"},
    })
    cfg._JSON_CACHE = None
    c = cfg.subtitle_config()
    assert c["model_size"] == "small"
    st = c["default_style"]
    assert st["font_size"] == 24 and st["align"] == "top"
    assert st["primary"] == "#FFFFFF"          # 未覆盖的样式键仍走默认
    assert st["highlight"] == "#FFD400"
    # 深合并不能污染内置默认字典
    assert cfg.SUBTITLE_DEFAULTS["default_style"]["font_size"] == 16
