"""
tests/test_local_org_store.py —— 批量基建三级组织存取 / 加密 / 迁移 / 就近继承

覆盖 store.local_org_store：
- 客户 / 执照 / 账户三层 CRUD 与级联删除（严格树，不留孤儿）；
- 账户凭证加密往返：明文只在内存，盘上 secret_enc 是密文；编辑不带 secret 保留原凭证；
- config → DB 一次性迁移：幂等、无归属老账户收进「默认客户（迁移）/ 默认主体」；
- effective_owner 就近继承（账户 → 执照 → 客户取首个非空）与按 owner 的可见过滤。

conftest autouse 兜底 org 表/会话；本模块 fixture 负责三张组织表 + 迁移标记 +
把 CONFIG_JSON 隔到用例临时文件（迁移测试要读它）。
"""
import pytest

import core.config as cfg
from store import db, app_state, local_org_store as lo

_MIG_KEY = "local_push_migrated_v1"


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    for t in ("local_customers", "local_licenses", "local_ad_accounts"):
        db.execute(f"DELETE FROM {t}")
    app_state.set_value(_MIG_KEY, False)          # 复位迁移守卫，用例可各测一次
    monkeypatch.setattr(cfg, "CONFIG_JSON", tmp_path / "config.json")
    monkeypatch.setattr(cfg, "_JSON_CACHE", None)
    monkeypatch.setattr(cfg, "USER_NAME", "tester")
    yield
    for t in ("local_customers", "local_licenses", "local_ad_accounts"):
        db.execute(f"DELETE FROM {t}")
    app_state.set_value(_MIG_KEY, False)


def _mk_customer(name, owner=""):
    return lo.save_customer({"id": "", "name": name, "owner": owner, "remark": ""})


def _mk_license(cid, name, owner=""):
    return lo.save_license({"id": "", "customer_id": cid, "name": name,
                            "subject": "", "owner": owner, "remark": ""})


def _mk_account(lid, label, token="TOK", owner="", aid=""):
    return lo.save_account({"id": aid, "license_id": lid, "platform": "douyin",
                            "label": label, "advertiser_id": "1001",
                            "auth_type": "oauth",
                            "secret": ({"access_token": token} if token is not None else {}),
                            "extra": {}, "owner": owner})


# ---------------- 三级 CRUD + 级联删除 ----------------

def test_three_level_crud_and_cascade():
    cid = _mk_customer("连锁A")
    lid = _mk_license(cid, "主体A1")
    aid = _mk_account(lid, "门店A-主户")
    assert [c["name"] for c in lo.list_customers()] == ["连锁A"]
    assert [l["name"] for l in lo.list_licenses(cid)] == ["主体A1"]
    accts = lo.list_accounts(apply_permission=False)
    assert len(accts) == 1 and accts[0]["label"] == "门店A-主户"
    assert accts[0]["customer_id"] == cid and accts[0]["license_name"] == "主体A1"

    # 更新客户名（同 id upsert 不新增行）
    lo.save_customer({"id": cid, "name": "连锁A改", "owner": "", "remark": ""})
    assert lo.get_customer(cid)["name"] == "连锁A改"
    assert len(lo.list_customers()) == 1

    # 删客户级联删其下执照与账户
    assert lo.delete_customer(cid) is True
    assert lo.list_licenses(cid) == []
    assert lo.list_accounts(apply_permission=False) == []
    assert lo.delete_customer(cid) is False       # 再删返回 False


def test_delete_license_cascades_accounts_only():
    cid = _mk_customer("连锁B")
    lid = _mk_license(cid, "主体B1")
    _mk_account(lid, "账户1")
    _mk_account(lid, "账户2")
    assert lo.delete_license(lid) is True
    assert lo.list_accounts(apply_permission=False) == []
    assert lo.get_customer(cid) is not None        # 客户还在，只带走执照与账户


# ---------------- 凭证加密往返 ----------------

def test_secret_encrypted_roundtrip_and_preserved_on_edit():
    cid = _mk_customer("连锁C")
    lid = _mk_license(cid, "主体C1")
    aid = _mk_account(lid, "门店C-主户", token="SECRET_ABC")

    # 盘上是密文、无明文
    raw = db.query("SELECT secret_enc FROM local_ad_accounts WHERE id=?", (aid,))[0]
    assert raw["secret_enc"].startswith("enc:")
    assert "SECRET_ABC" not in raw["secret_enc"]

    # 读回解密成明文
    got = lo.get_account(aid)
    assert got["secret"]["access_token"] == "SECRET_ABC"

    # 编辑不带 secret（secret=None 走 save 时传空 dict）→ 保留原凭证
    lo.save_account({"id": aid, "license_id": lid, "platform": "douyin",
                     "label": "门店C-改名", "advertiser_id": "1001",
                     "auth_type": "oauth", "secret": {}, "extra": {}, "owner": ""})
    again = lo.get_account(aid)
    assert again["label"] == "门店C-改名"
    assert again["secret"]["access_token"] == "SECRET_ABC"   # 密文没被空值覆盖


# ---------------- config → DB 一次性迁移 ----------------

def test_migrate_from_config_idempotent():
    # 用废弃的 config 写口造一条遗留加密账户
    acc_id = cfg.save_local_account({
        "id": "", "platform": "douyin", "label": "老账户X", "advertiser_id": "9001",
        "auth_type": "oauth", "secret": {"access_token": "OLD_TOK"}, "extra": {}})
    assert acc_id
    # 迁移前 DB 空、标记复位
    assert lo.list_accounts(apply_permission=False) == []

    assert lo.migrate_from_config() is True
    moved = lo.list_accounts(apply_permission=False)
    assert len(moved) == 1 and moved[0]["label"] == "老账户X"
    # 无归属老账户被收进默认客户/默认主体
    assert moved[0]["customer_name"] == "默认客户（迁移）"
    assert moved[0]["license_name"] == "默认主体"
    # 同机同人盐一致：搬库后仍能解出明文
    assert lo.get_account(moved[0]["id"])["secret"]["access_token"] == "OLD_TOK"

    # 幂等：标记已置，二次调用不再搬
    assert lo.migrate_from_config() is False
    assert len(lo.list_accounts(apply_permission=False)) == 1


# ---------------- 就近继承 + 按 owner 可见过滤 ----------------

def _eff(acct_row):
    return lo._effective_owner(acct_row)


def test_effective_owner_nearest_inheritance():
    cid = _mk_customer("连锁D", owner="张三")
    lid_inherit = _mk_license(cid, "执照继承")           # 无 owner → 继承客户张三
    lid_override = _mk_license(cid, "执照李四", owner="李四")
    a1 = _mk_account(lid_inherit, "账户继承")            # 无 owner → 张三
    a2 = _mk_account(lid_override, "账户继承执照")        # 无 owner → 李四（执照优先客户）
    a3 = _mk_account(lid_inherit, "账户自带王五", owner="王五")   # 账户级覆盖 → 王五

    rows = {r["label"]: r for r in lo._account_rows()}
    assert _eff(rows["账户继承"]) == "张三"
    assert _eff(rows["账户继承执照"]) == "李四"
    assert _eff(rows["账户自带王五"]) == "王五"
    assert (a1 and a2 and a3)


def test_visible_filter_by_owner(monkeypatch):
    from store import org_store
    cid = _mk_customer("连锁E", owner="张三")
    lid = _mk_license(cid, "执照E1")
    _mk_account(lid, "张三的户")                          # effective=张三
    cid2 = _mk_customer("连锁F", owner="李四")
    lid2 = _mk_license(cid2, "执照F1")
    _mk_account(lid2, "李四的户")                         # effective=李四

    # admin/单机：visible_owners None → 全可见
    monkeypatch.setattr(org_store, "visible_owners", lambda: None)
    assert {a["label"] for a in lo.list_accounts()} == {"张三的户", "李四的户"}
    assert len(lo.list_customers_visible()) == 2

    # member 只看自己（张三）负责的
    monkeypatch.setattr(org_store, "visible_owners", lambda: ["张三"])
    assert {a["label"] for a in lo.list_accounts()} == {"张三的户"}
    assert {c["name"] for c in lo.list_customers_visible()} == {"连锁E"}
    assert {l["name"] for l in lo.list_licenses_visible()} == {"执照E1"}
