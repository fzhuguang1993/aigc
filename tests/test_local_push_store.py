"""
test_local_push_store.py —— 批量基建执行历史入库回读（含三个组织维度）

record() 收 BuildRecord 对象或同名字段 dict；account_label 落到 account 列；
customer_id/license_id/account_id 一并入库；recent 按组织列过滤、stats_by_level
按层级 LEFT JOIN 组织表取名聚合。用例前后清空 local_build_runs 与三张组织表。
"""
import pytest

from store import db, build_store
from video_text_tools.local_push.models import BuildRecord


@pytest.fixture(autouse=True)
def _clean():
    for t in ("local_build_runs", "local_customers", "local_licenses",
              "local_ad_accounts"):
        db.execute(f"DELETE FROM {t}")
    yield
    for t in ("local_build_runs", "local_customers", "local_licenses",
              "local_ad_accounts"):
        db.execute(f"DELETE FROM {t}")


def test_record_object_and_readback():
    rec = BuildRecord(platform="douyin", account_label="门店A-主户",
                      advertiser_id="1001", plan_name="门店A方案", ok=True,
                      project_id="proj_9", marketing_id="mk_8", asset_count=2,
                      created_at="2026-09-26 10:00:00")
    assert build_store.record(rec) is not None
    rows = build_store.recent()
    assert len(rows) == 1
    assert rows[0]["account"] == "门店A-主户"
    assert rows[0]["ok"] == 1
    assert rows[0]["project_id"] == "proj_9"
    assert rows[0]["asset_count"] == 2


def test_record_dict_and_stats():
    build_store.record_all([
        {"platform": "douyin", "account_label": "a", "plan_name": "P1",
         "ok": False, "message": "抖音本地推端点未配置：asset_upload_url"},
        {"platform": "douyin", "account": "b", "plan_name": "P2", "ok": True,
         "project_id": "p", "marketing_id": "m"},
    ])
    s = build_store.stats()
    assert s == {"total": 2, "ok": 1, "fail": 1}


def _org():
    """造一个客户/执照/账户，返回三 id。"""
    from store import local_org_store as lo
    cid = lo.save_customer({"id": "", "name": "连锁A", "owner": "", "remark": ""})
    lid = lo.save_license({"id": "", "customer_id": cid, "name": "主体A1",
                           "subject": "", "owner": "", "remark": ""})
    aid = lo.save_account({"id": "", "license_id": lid, "platform": "douyin",
                           "label": "门店A-主户", "advertiser_id": "1001",
                           "auth_type": "oauth", "secret": {"access_token": "t"},
                           "extra": {}, "owner": ""})
    return cid, lid, aid


def test_record_lands_org_columns_and_filter_readback():
    cid, lid, aid = _org()
    build_store.record(BuildRecord(platform="douyin", account_label="门店A-主户",
                                   advertiser_id="1001", plan_name="P1", ok=True,
                                   customer_id=cid, license_id=lid, account_id=aid))
    build_store.record(BuildRecord(platform="douyin", account_label="其它",
                                   plan_name="P2", ok=False, customer_id=999))
    # 按客户过滤只回一条，且组织列已落库
    rows = build_store.recent(customer_id=cid)
    assert len(rows) == 1 and rows[0]["customer_id"] == cid
    assert rows[0]["license_id"] == lid and rows[0]["account_id"] == aid
    assert build_store.recent(customer_id=999)[0]["ok"] == 0
    assert build_store.recent(account_id=aid)[0]["plan_name"] == "P1"


def test_stats_by_level_joins_names():
    cid, lid, aid = _org()
    build_store.record_all([
        {"platform": "douyin", "account_label": "门店A-主户", "plan_name": "P1",
         "ok": True, "customer_id": cid, "license_id": lid, "account_id": aid},
        {"platform": "douyin", "account_label": "门店A-主户", "plan_name": "P2",
         "ok": False, "customer_id": cid, "license_id": lid, "account_id": aid},
    ])
    cust = build_store.stats_by_level("customer")
    assert cust[0]["key_id"] == cid and cust[0]["name"] == "连锁A"
    assert cust[0]["total"] == 2 and cust[0]["ok"] == 1 and cust[0]["fail"] == 1
    lic = build_store.stats_by_level("license")
    assert lic[0]["name"] == "主体A1"
    acct = build_store.stats_by_level("account")
    assert acct[0]["name"] == "门店A-主户"
    with pytest.raises(ValueError):
        build_store.stats_by_level("unknown")
