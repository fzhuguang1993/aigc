"""
test_publish_config.py —— 一键发布账号的加密存取往返

覆盖 core.config 的 publish 段与 publish_accounts 段：
- 账号凭证（cookie 字典）以 USER_NAME 为盐加密落盘，盘上不见明文；
- 读取时解密回明文 dict；upsert / delete 行为正确。

autouse fixture 把 CONFIG_JSON 重定向到用例临时文件并重置启动缓存：
既不碰共享配置家，也不会把 config.json 泄给依赖"无 config.json"前提的其它用例
（吸取爆款拆解 test_paths_config 的污染教训，从一开始就隔离）。
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


def test_publish_config_defaults_and_override():
    assert cfg.publish_config()["douyin"]["post_url_base"] == "https://www.douyin.com/video/"
    cfg.write_section("publish", {"douyin": {"create_url": "https://x/create"}})
    merged = cfg.publish_config()["douyin"]
    assert merged["create_url"] == "https://x/create"
    assert merged["post_url_base"] == "https://www.douyin.com/video/"   # 默认仍在


def test_account_upsert_encrypted_roundtrip():
    acc = {"id": "", "platform": "douyin", "label": "主号",
           "auth_type": "cookie", "secret": {"cookie": "sessionid=SECRET_ABC"},
           "extra": {}}
    new_id = cfg.save_publish_account(acc)
    assert new_id

    # 盘上密文：原始 cookie 明文不得出现
    disk = cfg.CONFIG_JSON.read_text(encoding="utf-8")
    assert "SECRET_ABC" not in disk
    entry = _raw()["publish_accounts"][0]
    assert entry["secret_enc"].startswith("enc:")

    # 读回解密成明文 dict
    got = cfg.list_publish_accounts()
    assert len(got) == 1
    assert got[0]["label"] == "主号"
    assert got[0]["secret"]["cookie"] == "sessionid=SECRET_ABC"

    # 同 id upsert 不新增行，只换凭证
    acc2 = {**got[0], "secret": {"cookie": "sessionid=NEW_XYZ"}}
    cfg.save_publish_account(acc2)
    again = cfg.list_publish_accounts()
    assert len(again) == 1
    assert again[0]["secret"]["cookie"] == "sessionid=NEW_XYZ"


def test_account_delete():
    cid = cfg.save_publish_account({"id": "", "platform": "kuaishou",
                                    "label": "ks", "auth_type": "cookie",
                                    "secret": {"cookie": "a=b"}, "extra": {}})
    assert cfg.delete_publish_account(cid) is True
    assert cfg.list_publish_accounts() == []
    assert cfg.delete_publish_account(cid) is False      # 再删不存在的：False


def test_decrypt_mismatch_yields_empty_secret():
    """存进去用 tester 加密，换个人名读不到 → 该条 secret 置空、不抛。"""
    cfg.save_publish_account({"id": "", "platform": "douyin", "label": "x",
                              "auth_type": "cookie",
                              "secret": {"cookie": "top=1"}, "extra": {}})
    cfg._JSON_CACHE = None
    cfg.USER_NAME = "other_person"
    got = cfg.list_publish_accounts()
    assert got[0]["secret"] == {}
