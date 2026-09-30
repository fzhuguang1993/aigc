"""
store/build_store.py —— 批量基建执行历史入库（SQLite: local_build_runs 表）

一次「方案 × 本地推账户」的搭建结果落一行，带客户/执照/账户三个组织维度，
供按层级归集统计。record() 既收 local_push.models.BuildRecord 对象，也收同字段
dict，防上层忘了对象/字典口径。查询走 db.query/execute（线程安全）。
"""
from datetime import datetime

from store import db


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get(res, name, default=""):
    if isinstance(res, dict):
        return res.get(name, default)
    return getattr(res, name, default)


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def record(res):
    """落一条搭建结果。res: BuildRecord 或同名字段 dict。返回新行 id。"""
    ok = 1 if _get(res, "ok", False) else 0
    return db.execute(
        "INSERT INTO local_build_runs(platform, account, advertiser_id, plan_name, "
        "ok, project_id, marketing_id, asset_count, message, created_at, "
        "customer_id, license_id, account_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(_get(res, "platform")),
         str(_get(res, "account_label") or _get(res, "account")),
         str(_get(res, "advertiser_id")), str(_get(res, "plan_name")), ok,
         str(_get(res, "project_id")), str(_get(res, "marketing_id")),
         int(_get(res, "asset_count", 0) or 0),
         str(_get(res, "message")), str(_get(res, "created_at")) or _now(),
         _int(_get(res, "customer_id", 0)), _int(_get(res, "license_id", 0)),
         _int(_get(res, "account_id", 0))))


def record_all(records):
    """批量落库，返回写入条数。"""
    return sum(1 for r in records if record(r) is not None)


def recent(limit=200, customer_id=None, license_id=None, account_id=None):
    sql = "SELECT * FROM local_build_runs"
    conds, args = [], []
    for col, val in (("customer_id", customer_id), ("license_id", license_id),
                     ("account_id", account_id)):
        if val is not None:
            conds.append(f"{col}=?"); args.append(int(val))
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT ?"
    return db.query(sql, args + [int(limit)])


def stats():
    """搭建总量/成功/失败（全局）。"""
    r = db.query("SELECT COUNT(*) total, SUM(ok=1) ok, SUM(ok=0) fail"
                 " FROM local_build_runs")[0]
    return {"total": int(r.get("total") or 0),
            "ok": int(r.get("ok") or 0), "fail": int(r.get("fail") or 0)}


# 层级 → (runs 里的列, 名称来源表, 名称列)
_LEVELS = {
    "customer": ("customer_id", "local_customers", "name"),
    "license": ("license_id", "local_licenses", "name"),
    "account": ("account_id", "local_ad_accounts", "label"),
}


def stats_by_level(level):
    """按 客户/执照/账户 维度分组聚合搭建 total/ok/fail。

    返回 [{key_id, name, total, ok, fail}]；名称 LEFT JOIN 对应组织表取得，
    组织节点被删则 name 为空（历史仍在，按 id 归组）。"""
    if level not in _LEVELS:
        raise ValueError(f"未知统计层级：{level}（可选 customer/license/account）")
    col, tbl, ncol = _LEVELS[level]
    rows = db.query(
        f"SELECT r.{col} AS key_id, t.{ncol} AS name,"
        f" COUNT(*) AS total, SUM(r.ok=1) AS ok, SUM(r.ok=0) AS fail"
        f" FROM local_build_runs r LEFT JOIN {tbl} t ON r.{col}=t.id"
        f" GROUP BY r.{col} ORDER BY total DESC", ())
    return [{"key_id": int(rr.get("key_id") or 0), "name": rr.get("name") or "",
             "total": int(rr.get("total") or 0), "ok": int(rr.get("ok") or 0),
             "fail": int(rr.get("fail") or 0)} for rr in rows]
