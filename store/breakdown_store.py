"""
store/breakdown_store.py —— 爆款拆解任务库入库（SQLite: breakdown_tasks 表）

一次拆解落一行：payload 存 BreakdownResult.to_dict() 的 JSON，封面/图集目录/Word
文档路径各存一列，供「拆解面板右列」与「拆解任务管理页」列卡片、点开详情页三屏联动。
查询走 db.query/execute（线程安全）。save 会回填 result.task_id。
"""
import json
from datetime import datetime
from pathlib import Path

from store import db


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _title(result):
    t = (getattr(result, "title", "") or "").strip()
    if t:
        return t
    vp = (getattr(result, "video_path", "") or "").strip()
    if vp:
        return Path(vp).stem
    return (getattr(result, "link", "") or "").strip()[:40]


def save(result, gallery_dir="", log=None):
    """把一条拆解结果写库，返回新行 id（回填到 result.task_id）。

    gallery_dir：本次图集落盘的持久目录（可空）；cover/report 从 result 上取。
    半成品（result.is_partial()）也入库，标 status='partial'，便于回看定位。
    """
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    status = "partial" if result.is_partial() else "ok"
    now = _now()
    tid = db.execute(
        "INSERT INTO breakdown_tasks(title, video_path, link, duration, "
        "shot_count, cover, gallery_dir, report, status, payload, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(_title(result)), str(getattr(result, "video_path", "") or ""),
         str(getattr(result, "link", "") or ""),
         float(getattr(result, "duration", 0) or 0),
         int(getattr(result, "shot_count", 0) or 0),
         str(getattr(result, "cover_path", "") or ""), str(gallery_dir or ""),
         str(getattr(result, "report_path", "") or ""), status, payload,
         now, now))
    result.task_id = int(tid or 0)
    if log:
        log(f"✓ 拆解任务已入库（#{tid}）")
    return tid


def list_tasks(limit=200, only_ok=False):
    """列表用（不含 payload，轻量）：最新在前。

    only_ok=True 只回完整任务（status='ok'），半成品/失败不进列表——
    面板右列与管理页都用它，确保“只有成功的任务进列表”。"""
    where = "WHERE status='ok'" if only_ok else ""
    return db.query(
        "SELECT id, title, video_path, link, duration, shot_count, cover, "
        "gallery_dir, report, status, created_at "
        f"FROM breakdown_tasks {where} ORDER BY id DESC LIMIT ?", (int(limit),))


def get(task_id):
    """取整行（含 payload）。无则 None。"""
    rows = db.query("SELECT * FROM breakdown_tasks WHERE id=?", (int(task_id),))
    return rows[0] if rows else None


def load_result(task_id):
    """反序列化重建 BreakdownResult（gallery 里已是绝对路径），供详情页用。

    无该行或 payload 损坏 → None。回填 task_id / report_path。
    """
    from video_text_tools.breakdown.models import BreakdownResult
    row = get(task_id)
    if not row:
        return None
    try:
        data = json.loads(row.get("payload") or "{}")
    except (ValueError, TypeError):
        return None
    result = BreakdownResult.from_dict(data)
    result.task_id = int(row.get("id") or task_id)
    if not result.report_path:
        result.report_path = row.get("report") or ""
    return result


def delete(task_id):
    """删任务行（连带清产品关联；图片目录保留，交目录清理，避免误删在别处引用的图）。"""
    db.execute("DELETE FROM breakdown_task_products WHERE task_id=?", (int(task_id),))
    db.execute("DELETE FROM breakdown_tasks WHERE id=?", (int(task_id),))


# ---------------- 产品关联（多对多，按产品沉淀） ----------------

def products_of(task_id):
    """某任务关联的产品 id 列表（无则空）。"""
    rows = db.query("SELECT product_id FROM breakdown_task_products WHERE task_id=?",
                    (int(task_id),))
    return [int(r["product_id"]) for r in rows]


def set_products(task_id, product_ids):
    """整体替换某任务的产品关联（多选面板保存用）。传空列表即清空。"""
    tid = int(task_id)
    db.execute("DELETE FROM breakdown_task_products WHERE task_id=?", (tid,))
    for pid in {int(p) for p in (product_ids or []) if p}:
        db.execute("INSERT OR IGNORE INTO breakdown_task_products(task_id, product_id) "
                   "VALUES(?,?)", (tid, pid))


def products_map(task_ids=None):
    """{task_id: [产品名, ...]}：一次查出全部关联并 JOIN 产品名，供卡片展示（不 N+1）。"""
    rows = db.query(
        "SELECT p.task_id AS tid, pr.name AS name "
        "FROM breakdown_task_products p JOIN products pr ON pr.id = p.product_id "
        "ORDER BY pr.name")
    out = {}
    want = None if task_ids is None else {int(x) for x in task_ids}
    for r in rows:
        tid = int(r["tid"])
        if want is not None and tid not in want:
            continue
        out.setdefault(tid, []).append(r["name"])
    return out


def task_ids_for_product(product_id):
    """反查：关联了某产品的任务 id 列表（按产品集中沉淀时筛选用）。"""
    rows = db.query("SELECT task_id FROM breakdown_task_products WHERE product_id=?",
                    (int(product_id),))
    return [int(r["task_id"]) for r in rows]
