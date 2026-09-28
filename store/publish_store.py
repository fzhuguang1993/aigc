"""
store/publish_store.py —— 一键发布历史入库（SQLite: publishes 表）

一次「视频 × 平台账号」的发布结果落一行。record() 既收 publish.models.PublishRecord
对象，也收同字段 dict，防上层忘了对象/字典口径。查询走 db.query/execute（线程安全）。
"""
from datetime import datetime

from store import db


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get(res, name, default=""):
    if isinstance(res, dict):
        return res.get(name, default)
    return getattr(res, name, default)


def record(res):
    """落一条发布结果。res: PublishRecord 或同名字段 dict。返回新行 id。"""
    ok = 1 if _get(res, "ok", False) else 0
    return db.execute(
        "INSERT INTO publishes(platform, account, title, video_path, ok, "
        "post_id, post_url, message, published_at, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (str(_get(res, "platform")), str(_get(res, "account_label") or _get(res, "account")),
         str(_get(res, "title")), str(_get(res, "video_path")), ok,
         str(_get(res, "post_id")), str(_get(res, "post_url")),
         str(_get(res, "message")), str(_get(res, "published_at")), _now()))


def record_all(records):
    """批量落库，返回写入条数。"""
    return sum(1 for r in records if record(r) is not None)


def recent(limit=200):
    return db.query("SELECT * FROM publishes ORDER BY id DESC LIMIT ?", (int(limit),))


def stats():
    """发布总量/成功/失败（供后续看板可选项）。"""
    r = db.query("SELECT COUNT(*) total, SUM(ok=1) ok, SUM(ok=0) fail"
                 " FROM publishes")[0]
    return {"total": int(r.get("total") or 0),
            "ok": int(r.get("ok") or 0), "fail": int(r.get("fail") or 0)}
