"""
tests/test_block_categories.py —— 素材板块类别词库（config breakdown.blocks_types）

把 CONFIG_JSON 指到临时文件隔离，验证：默认六类、写盘保留同段其它键（vision_mode/fps）、
清洗去重、增删与恢复默认。保存即生效靠 breakdown_config 现读 config，不依赖启动缓存。
"""
import json

import pytest

from core import block_categories


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """把 config.json 重定向到临时文件，读写都落这里、不碰真实配置。"""
    import core.config as C
    path = tmp_path / "config.json"
    monkeypatch.setattr(C, "CONFIG_JSON", path)
    monkeypatch.setattr(C, "_JSON_CACHE", None)      # 清启动缓存，逼现读
    yield path


def _seed(path, section):
    path.write_text(json.dumps({"breakdown": section}, ensure_ascii=False),
                    encoding="utf-8")


def test_load_default_when_absent(cfg):
    assert block_categories.load() == list(block_categories.DEFAULT_CATEGORIES)


def test_set_all_preserves_other_keys(cfg):
    _seed(cfg, {"vision_mode": "frames", "fps": 1.0, "blocks_types": ["钩子"]})
    from core.config import breakdown_config
    block_categories.set_all(["钩子", "痛点", "痛点"])      # 去重
    assert block_categories.load() == ["钩子", "痛点"]
    sec = breakdown_config()
    assert sec["vision_mode"] == "frames" and sec["fps"] == 1.0   # 其它键保留
    assert sec["blocks_types"] == ["钩子", "痛点"]


def test_clean_and_add_remove(cfg):
    _seed(cfg, {"blocks_types": ["钩子"]})
    block_categories.add("含/非法:符")
    got = block_categories.load()
    assert "含_非法_符" in got                          # 非法字符清洗为下划线
    block_categories.add("钩子")                        # 重复不加
    assert got.count("钩子") == 1
    block_categories.remove("钩子")
    assert "钩子" not in block_categories.load()


def test_restore_default(cfg):
    _seed(cfg, {"blocks_types": ["自定义"]})
    block_categories.restore_default()
    assert block_categories.load() == list(block_categories.DEFAULT_CATEGORIES)
