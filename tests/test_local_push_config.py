"""
test_local_push_config.py —— 批量基建端点与计划方案存取往返

覆盖 core.config 的 local_push 端点段与 local_plans 段：
- 端点默认值与 config.json 覆盖合并；
- 计划方案为明文列表，CRUD 与 upsert 行为正确。

本地推账户已从 config.json 迁入 SQLite 三级组织（store.local_org_store），
其凭证加密往返 / 就近继承 / 可见过滤的用例见 tests/test_local_org_store.py；
config 里的 list/save/delete_local_account 仅作一次性迁移源（deprecated），
不再在此单测其往返。

autouse fixture 把 CONFIG_JSON 重定向到用例临时文件并重置启动缓存：
既不碰共享配置家，也不会把 config.json 泄给依赖"无 config.json"前提的其它用例。
"""
import json

import pytest

import core.config as cfg


@pytest.fixture(autouse=True)
def _isolate_config_json(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_JSON", tmp_path / "config.json")
    monkeypatch.setattr(cfg, "_JSON_CACHE", None)
    monkeypatch.setattr(cfg, "USER_NAME", "tester")   # 有姓名 → 凭证真加密
    yield


def _raw():
    return json.loads(cfg.CONFIG_JSON.read_text(encoding="utf-8"))


def test_local_push_config_defaults_and_override():
    assert cfg.local_push_config()["douyin"]["api_base"] == "https://open.oceanengine.com"
    cfg.write_section("local_push", {"douyin": {"project_create_url": "https://x/pc"}})
    merged = cfg.local_push_config()["douyin"]
    assert merged["project_create_url"] == "https://x/pc"
    assert merged["api_base"] == "https://open.oceanengine.com"   # 默认仍在
    assert merged["asset_upload_url"] == ""                       # 未配仍为空


def test_plan_crud_and_upsert():
    pid = cfg.save_local_plan({"id": "", "name": "门店A方案", "promo_type": "store",
                               "budget": 300.0, "videos": ["a.mp4"]})
    assert pid
    items = cfg.list_local_plans()
    assert len(items) == 1
    assert items[0]["name"] == "门店A方案"

    # 同 id upsert 不新增行，只改字段
    cfg.save_local_plan({**items[0], "budget": 500.0})
    items = cfg.list_local_plans()
    assert len(items) == 1
    assert items[0]["budget"] == 500.0

    # 删除 + 再删不存在的
    assert cfg.delete_local_plan(pid) is True
    assert cfg.list_local_plans() == []
    assert cfg.delete_local_plan(pid) is False
