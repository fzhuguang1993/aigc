"""
tests/test_local_push_org_auth.py —— 三级组织与员工角色打通的可见性

用真实 org_store 成员/会话切换角色，经 local_push.accounts.list_accounts() 端到端
验证「就近继承 + 按 owner 过滤」在三种角色下的可见账户集合：
- member 只看自己负责的；
- manager 看本部门 active 成员（含自己）负责的并集；
- admin 不限；
- 账户级 owner 覆盖上级（把别人的户改挂到自己名下即对自己可见）。

fixture 里把迁移标记置起、CONFIG_JSON 隔空，避免历史 config 干扰本次账户集。
"""
import pytest

import core.config as cfg
from store import db, app_state, org_store, local_org_store as lo
from video_text_tools.local_push import accounts as acc_api


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    for t in ("local_customers", "local_licenses", "local_ad_accounts"):
        db.execute(f"DELETE FROM {t}")
    app_state.set_value("local_push_migrated_v1", True)   # 跳过 config 迁移
    monkeypatch.setattr(cfg, "CONFIG_JSON", tmp_path / "config.json")
    monkeypatch.setattr(cfg, "_JSON_CACHE", None)
    yield
    for t in ("local_customers", "local_licenses", "local_ad_accounts"):
        db.execute(f"DELETE FROM {t}")


def _seed_members():
    org_store.create_member("老板", "1234")                      # 首个强制 admin
    org_store.create_member("小张", "1234", "member", "一部")
    org_store.create_member("小李", "1234", "member", "一部")
    org_store.create_member("主管", "1234", "manager", "一部")
    org_store.create_member("甲木", "1234", "member", "二部")


def _acct(owner):
    cid = lo.save_customer({"id": "", "name": f"客户{owner}", "owner": owner, "remark": ""})
    lid = lo.save_license({"id": "", "customer_id": cid, "name": f"执照{owner}",
                           "subject": "", "owner": "", "remark": ""})
    lo.save_account({"id": "", "license_id": lid, "platform": "douyin",
                     "label": f"户-{owner}", "advertiser_id": "1", "auth_type": "oauth",
                     "secret": {"access_token": "t"}, "extra": {}, "owner": ""})


def _labels():
    return {a.label for a in acc_api.list_accounts()}


def test_member_sees_only_own():
    _seed_members()
    _acct("小张")
    _acct("小李")
    _acct("甲木")
    assert org_store.login("小张", "1234") is None
    assert _labels() == {"户-小张"}


def test_manager_union_of_dept():
    _seed_members()
    _acct("小张")
    _acct("小李")
    _acct("甲木")
    assert org_store.login("主管", "1234") is None
    assert _labels() == {"户-小张", "户-小李"}        # 二部甲木不在内


def test_admin_sees_all():
    _seed_members()
    _acct("小张")
    _acct("小李")
    _acct("甲木")
    assert org_store.login("老板", "1234") is None
    assert _labels() == {"户-小张", "户-小李", "户-甲木"}


def test_account_level_override_changes_visibility():
    """客户/执照挂小张，但某账户 owner 改成小李：小张看不到、小李看得到。"""
    _seed_members()
    cid = lo.save_customer({"id": "", "name": "客户X", "owner": "小张", "remark": ""})
    lid = lo.save_license({"id": "", "customer_id": cid, "name": "执照X",
                           "subject": "", "owner": "", "remark": ""})
    lo.save_account({"id": "", "license_id": lid, "platform": "douyin",
                     "label": "继承户", "advertiser_id": "1", "auth_type": "oauth",
                     "secret": {"access_token": "t"}, "extra": {}, "owner": ""})
    lo.save_account({"id": "", "license_id": lid, "platform": "douyin",
                     "label": "覆盖户", "advertiser_id": "2", "auth_type": "oauth",
                     "secret": {"access_token": "t"}, "extra": {}, "owner": "小李"})

    assert org_store.login("小张", "1234") is None
    assert _labels() == {"继承户"}                    # 覆盖户 effective=小李，不给小张
    assert org_store.login("小李", "1234") is None
    assert _labels() == {"覆盖户"}
