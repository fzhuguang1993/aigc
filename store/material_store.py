"""
store/material_store.py —— 素材库片段索引（SQLite: material_clips 表）

板块切出的每个片段落一行：path（存储侧真实路径）+ backend + block_type/product +
源拆解任务 id + 起止时长。素材库页按 类型/产品 筛选、预览、定位、删除都走这里。
删除只删索引行、保留盘上文件（与拆解图库同口径，避免误删他处引用的片段）。
"""
from datetime import datetime
import re
import time
from pathlib import Path

from store import db

_BADFN = re.compile(r'[\\/:*?"<>|]')


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sanitize(s):
    return _BADFN.sub("_", str(s or "").strip())


def _norm(p):
    try:
        return str(Path(str(p)).resolve())
    except OSError:
        return str(p)


def _free_target(dirpath, stem, suffix):
    """目标名已被别的文件占着：挂 (2)(3)，绝不覆盖。"""
    cand = Path(dirpath) / f"{stem}{suffix}"
    if not cand.exists():
        return cand
    i = 2
    while True:
        alt = Path(dirpath) / f"{stem}({i}){suffix}"
        if not alt.exists():
            return alt
        i += 1


def add(path, block_type="", product="", source_task_id=0, start=0.0, end=0.0,
        duration=0.0, backend="local", note="", log=None):
    """新增一个素材片段行，返回 id。path 为空拒绝（避免脏行）。"""
    path = str(path or "").strip()
    if not path:
        return 0
    if not duration:
        try:
            duration = max(0.0, float(end) - float(start))
        except (TypeError, ValueError):
            duration = 0.0
    cid = db.execute(
        "INSERT INTO material_clips(path, backend, block_type, product, "
        "source_task_id, start, end, duration, note, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (path, str(backend or "local"), str(block_type or "").strip(),
         str(product or "").strip(), int(source_task_id or 0),
         float(start or 0), float(end or 0), float(duration or 0),
         str(note or ""), _now()))
    if log:
        log(f"✓ 素材入库（#{cid}）：{block_type or '片段'}")
    return int(cid or 0)


def list_clips(block_type="", product="", limit=500):
    """按 类型/产品 过滤列片段（最新在前）；空条件不过滤。"""
    cond, args = [], []
    if block_type:
        cond.append("block_type=?")
        args.append(str(block_type))
    if product:
        cond.append("product=?")
        args.append(str(product))
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    args.append(int(limit))
    return db.query(
        f"SELECT * FROM material_clips {where} ORDER BY id DESC LIMIT ?", tuple(args))


def list_by_type(block_type, limit=500):
    return list_clips(block_type=block_type, limit=limit)


def list_by_product(product, limit=500):
    return list_clips(product=product, limit=limit)


def get(clip_id):
    rows = db.query("SELECT * FROM material_clips WHERE id=?", (int(clip_id),))
    return rows[0] if rows else None


def distinct_types():
    """已入库的全部板块类型（去重、非空），供筛选下拉。"""
    rows = db.query("SELECT DISTINCT block_type FROM material_clips "
                    "WHERE block_type IS NOT NULL AND block_type != '' "
                    "ORDER BY block_type")
    return [r["block_type"] for r in rows]


def distinct_products():
    rows = db.query("SELECT DISTINCT product FROM material_clips "
                    "WHERE product IS NOT NULL AND product != '' "
                    "ORDER BY product")
    return [r["product"] for r in rows]


def delete(clip_id, log=None):
    """删索引行（保留盘上文件）。返回是否删了行。"""
    existed = get(clip_id) is not None
    db.execute("DELETE FROM material_clips WHERE id=?", (int(clip_id),))
    if log:
        log(f"{'✓' if existed else '⚠'} 素材索引删除（#{clip_id}）：盘上文件保留")
    return existed


def set_products(clip_ids, product):
    """批量把若干片段归属到产品名（直接改 material_clips.product 列）。

    product 传空 = 清空归属（回到未分类）。返回受影响行数。"""
    name = str(product or "").strip()
    n = 0
    for cid in (clip_ids or []):
        cur = get(cid)
        if cur is None:
            continue
        db.execute("UPDATE material_clips SET product=? WHERE id=?", (name, int(cid)))
        n += 1
    return n


def rename(clip_id, new_stem):
    """重命名片段文件并同步索引里的 path（new_stem 不含扩展名）。

    只改自己这行：material_clips.path 是唯一引用点，改完扫库/预览都对得上。
    带重试（播放器占着句柄时 Windows 改名会失败）与撞名挂 (2)。返回 (ok, 新路径, 一句话)。"""
    row = get(clip_id)
    if row is None:
        return False, "", "片段索引不存在"
    p = Path(str(row["path"] or ""))
    if not p.exists():
        return False, str(p), "片段文件不存在或已被移动"
    stem = _sanitize(new_stem) or p.stem
    target = _free_target(p.parent, stem, p.suffix)
    if target == p:
        return True, str(p), "名称未变化"
    last = ""
    for _ in range(3):
        try:
            p.rename(target)
            last = ""
            break
        except OSError as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(0.25)
    if last:
        return False, str(p), f"重命名失败：{last}（文件可能正在播放，关掉播放器再试）"
    db.execute("UPDATE material_clips SET path=? WHERE id=?", (str(target), int(clip_id)))
    return True, str(target), ""


def delete_with_files(clip_ids):
    """删索引 + 片段文件进【回收站】。返回 (已删索引id列表, [(路径, 原因)])。

    文件没删成的（如被占用）保留其索引行，避免盘上还在、索引却没了的孤儿。"""
    from utils.desktop_utils import move_to_trash
    rows = [get(int(c)) for c in (clip_ids or [])]
    rows = [r for r in rows if r is not None]
    files = [r["path"] for r in rows if Path(str(r["path"] or "")).exists()]
    _ok, failed = move_to_trash(files) if files else ([], [])
    failed_keys = {_norm(p) for p, _m in failed}
    done = []
    for r in rows:
        if _norm(r["path"]) in failed_keys:
            continue
        delete(r["id"])
        done.append(int(r["id"]))
    return done, failed
