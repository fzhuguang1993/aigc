"""
store/output_store.py —— 成品库（扫描产物目录，汇聚产品/标签/审片标记）

成品库不落独立表：成品本就散在 DOWNLOAD_DIR（生成输出、混剪产物都在这），元数据分别
住在 runs/tasks.output（哪条任务抽的卡、产品/标签）与 file_marks（审片可用/不可用）。
这里以「盘上真实视频文件」为基准去 join 这三处，做成只读视图（计划允许"或视图"）——
永远与磁盘一致，不会有索引漂移；新生成的成品只要落在库里就能被扫到。

只读为主；要改标记走 processors.output_mark（它会同步改名与库）。duration 不做逐条
ffprobe（目录扫描会很慢），需要时由界面按需查。
"""
from datetime import datetime
import os
import re
import shutil
from pathlib import Path

VID_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_BADFN = re.compile(r'[\\/:*?"<>|]')


def _sanitize(s):
    """干掉文件名不允许的字符（跟 core.naming 同口径，改名落到盘上才不炸）"""
    return _BADFN.sub("_", str(s or "").strip())

# 「未分类」筛选哨兵：产品下拉选它 = 只看没有任何产品归属（既未绑定也 join 不到）的成品
UNBOUND = "__未分类__"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _norm(p):
    """归一化路径做 join 键：解符号链接，与 output_mark 同口径，避免同文件两条。"""
    try:
        return str(Path(str(p)).resolve())
    except OSError:
        return str(p)


def output_root():
    """混剪导出等的成品库根（storage.output_root，缺省 DOWNLOAD_DIR/成品库）。"""
    try:
        from core.config import storage_config
        return Path(storage_config()["output_root"])
    except Exception:
        from core.config import DOWNLOAD_DIR
        return Path(DOWNLOAD_DIR) / "成品库"


def scan_root():
    """成品库扫描根：只扫「成品库」目录（storage.output_root）。

    不再扫整个 DOWNLOAD_DIR——否则新生成在 outputs/日期/、归档进素材库的
    成品会被一锅端进成品库页。成品库 = 只展示已经「批量归档进成品库」的那些；
    任务刚出的片留在 outputs/日期/，经任务中心审片标可用后归档进来才看得到。
    """
    return output_root()


def _meta_maps():
    """扫库前一次性建好两张查找表，避免逐文件查库。

    out_meta: 归一化输出路径 → {product, tag, source_task_id}
    marks:    归一化路径 → mark（bad/ok，'' 不入表）"""
    from store import db

    task_by_id = {r["id"]: r for r in db.query("SELECT id, product, tag FROM tasks")}
    out_meta = {}
    # runs 逐条抽卡的 output 最细（一个任务多条），优先按 run 建键
    for r in db.query("SELECT output, task_id, product FROM runs "
                      "WHERE output IS NOT NULL AND output != ''"):
        t = task_by_id.get(r["task_id"]) or {}
        out_meta[_norm(r["output"])] = {
            "product": (t.get("product") or r.get("product") or ""),
            "tag": (t.get("tag") or ""),
            "source_task_id": int(r["task_id"] or 0),
        }
    # tasks.output（任务级最后一条产物）兜底，不覆盖 run 已建的路径
    for tid, t in task_by_id.items():
        rows = db.query("SELECT output FROM tasks WHERE id=?", (tid,))
        outp = (rows[0]["output"] if rows else "") or ""
        if outp.strip():
            out_meta.setdefault(_norm(outp), {
                "product": t.get("product") or "", "tag": t.get("tag") or "",
                "source_task_id": tid})
    marks = {}
    for r in db.query("SELECT path, mark FROM file_marks "
                      "WHERE mark IS NOT NULL AND mark != ''"):
        marks[_norm(r["path"])] = r["mark"]
    return out_meta, marks


def _videos_under(base):
    base = Path(base)
    if not base.exists():
        return []
    out = []
    for p in base.rglob("*"):
        try:
            if p.is_file() and p.suffix.lower() in VID_EXT:
                out.append(p)
        except OSError:
            continue
    return out


def binds_map():
    """人工绑定的产品：归一化路径 → 产品名（output_binds 表）。"""
    from store import db
    out = {}
    for r in db.query("SELECT path, product FROM output_binds"):
        out[_norm(r["path"])] = (r["product"] or "")
    return out


def bind(path, product, product_id=0):
    """把一条成品人工绑定到产品名（path 存归一化绝对路径）；product 为空等同解绑。"""
    from store import db
    name = str(product or "").strip()
    key = _norm(path)
    if not name:
        return unbind(path)
    db.execute(
        "INSERT OR REPLACE INTO output_binds(path, product, product_id, updated_at) "
        "VALUES(?,?,?,?)", (key, name, int(product_id or 0), _now()))
    return True


def unbind(path):
    """解除一条成品的人工绑定（回到自动 join / 未分类）。"""
    from store import db
    db.execute("DELETE FROM output_binds WHERE path=?", (_norm(path),))
    return True


def migrate_bind(old, new):
    """文件改名：人工绑定跟着走（不跟的话刚绑完就变孤儿，产品归属丢失）。"""
    from store import db
    o, n = _norm(old), _norm(new)
    if not o or o == n:
        return
    rows = db.query("SELECT product, product_id FROM output_binds WHERE path=?", (o,))
    if not rows:
        return
    db.execute("DELETE FROM output_binds WHERE path=?", (o,))
    for r in rows:
        db.execute("INSERT OR REPLACE INTO output_binds(path, product, product_id, updated_at) "
                   "VALUES(?,?,?,?)", (n, r["product"], r["product_id"], _now()))


def _free_target(dirpath, stem, suffix):
    """目标名被占用挂 (2)(3)：绝不覆盖别的成品（多半是撞名的另一条抽卡）"""
    cand = Path(dirpath) / f"{stem}{suffix}"
    if not cand.exists():
        return cand
    i = 2
    while True:
        alt = Path(dirpath) / f"{stem}({i}){suffix}"
        if not alt.exists():
            return alt
        i += 1


def rename(old_path, new_stem):
    """重命名一条成品：改盘 + 把按路径存库的绑定 / 审片标记 / 任务产物路径一起挪。

    new_stem 不含扩展名（扩展名沿用原文件）。返回 (是否成功, 现真实路径, 给人看的一句话)。
    改名带重试：播放器还占着句柄时 Windows 上改名会失败，等它松手几百毫秒再试。"""
    import time
    p = Path(str(old_path or ""))
    if not str(old_path or "").strip():
        return False, "", "没有可改名的文件"
    if not p.exists():
        return False, str(p), "文件不存在或已被移动"
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
    migrate_bind(str(p), str(target))
    from store import task_store
    task_store.move_file_mark(str(p), str(target))
    task_store.replace_output_path(str(p), str(target))
    migrate_display(str(p), str(target))
    return True, str(target), ""


def batch_delete(paths):
    """把选中成品送进【回收站】，并清掉按路径存的绑定 / 审片标记（runs/tasks 历史保留）。

    返回 (成功路径列表, [(路径, 原因)])。走回收站而不是真删：误勾了还能捞回来。"""
    from utils.desktop_utils import move_to_trash
    from store import task_store
    files = [str(p) for p in (paths or [])
             if str(p or "").strip() and Path(str(p)).exists()]
    ok, failed = move_to_trash(files) if files else ([], [])
    for p in ok:
        unbind(p)
        try:
            task_store.set_file_mark(p, "")
        except Exception:
            pass
    return ok, failed


def iter_outputs(root=None):
    """扫描库内所有视频，join 产品/标签/标记 + 人工绑定，按修改时间倒序产出行。

    产品口径：人工绑定（binds）优先于自动 join（runs/tasks），都没有则为空（未分类）。
    软件内回收站目录在扫描根之下，逐路排掉（文件移进回收站 = 从成品库消失）；
    额外带一行 display（外显名，空则由界面回落到「文件名去后缀」）。"""
    from processors import output_mark
    base = Path(root) if root else scan_root()
    out_meta, marks = _meta_maps()
    binds = binds_map()
    displays = display_map()
    trash = _norm(trash_root())
    rows = []
    for p in _videos_under(base):
        key = _norm(p)
        # 回收站目录（在扫描根之下）里的文件不算成品
        if key == trash or key.startswith(trash + os.sep):
            continue
        meta = out_meta.get(key, {})
        mark = marks.get(key) or (task_mark_from_name(output_mark, p))
        bound = binds.get(key) or ""
        product = bound or meta.get("product", "")
        try:
            st = p.stat()
            mtime = datetime.fromtimestamp(st.st_mtime)
        except OSError:
            st, mtime = None, None
        rows.append({
            "path": str(p),
            "name": p.name,
            "product": product,
            "bound": bool(bound),
            "tag": meta.get("tag", ""),
            "mark": mark or "",
            "display": displays.get(key, ""),
            "source_task_id": meta.get("source_task_id", 0),
            "duration": 0,
            "size": st.st_size if st else 0,
            "created_at": mtime.strftime("%Y-%m-%d %H:%M") if mtime else "",
            "_ts": mtime.timestamp() if mtime else 0,
        })
    rows.sort(key=lambda d: d["_ts"], reverse=True)
    return rows


def task_mark_from_name(output_mark, p):
    """库里没记号时看文件名：带 `_不可用` 后缀即视为 bad（同事手改名也认）。"""
    try:
        return output_mark.MARK_BAD if output_mark.is_marked_bad(str(p)) else ""
    except Exception:
        return ""


def list_outputs(product="", tag="", mark="", hide_bad=False, limit=800, root=None):
    rows = iter_outputs(root=root)
    if product == UNBOUND:
        rows = [r for r in rows if not r["product"]]
    elif product:
        rows = [r for r in rows if r["product"] == product]
    if tag:
        rows = [r for r in rows if r["tag"] == tag]
    if mark:
        rows = [r for r in rows if r["mark"] == mark]
    if hide_bad:
        rows = [r for r in rows if r["mark"] != "bad"]
    return rows[:limit]


def _distinct(field):
    vals = []
    seen = set()
    for r in iter_outputs():
        v = r.get(field) or ""
        if v and v not in seen:
            seen.add(v)
            vals.append(v)
    return sorted(vals)


def distinct_products():
    return _distinct("product")


def distinct_tags():
    return _distinct("tag")


def to_clip(path):
    """把一条成品路径打包成混剪可认的 clip（不做 AI 分类，type 留空由用户归）。"""
    p = Path(path)
    return {"path": str(p), "block_type": "成品", "product": "",
            "duration": 0, "label": p.name}


# ---------------- 软件内回收站 ----------------

def trash_root():
    """软件内回收站目录：scan_root 下的专用子目录（成品「移入回收站」物理挪到这，
    不进系统回收站；扫描成品库时按路径前缀排掉它，界面上就等价于「从成品库消失」）。"""
    return Path(scan_root()) / "_回收站"


def _migrate_all_paths(old, new):
    """文件换了位置：把按路径存库的四类元数据一起迁到新路径。

    人工绑定 / 外显名 / 审片标记 / 任务产物路径。移入回收站、还原、改名都走它，
    保证元数据始终跟着那条文件走，不会一挪就变孤儿。"""
    migrate_bind(old, new)
    migrate_display(old, new)
    from store import task_store
    task_store.move_file_mark(old, new)
    task_store.replace_output_path(old, new)


def to_trash(paths):
    """把成品移入【软件内回收站】：物理移动到回收站目录（不进系统回收站），记一行留痕。

    返回 (成功列表, [(路径, 原因)])；成功项是回收站里的新真实路径。
    元数据随文件迁到新路径，还原时再迁回；撞名挂 (2)(3) 绝不覆盖。"""
    from store import db
    tr = trash_root()
    tr.mkdir(parents=True, exist_ok=True)
    ok, failed = [], []
    for raw in [str(x) for x in (paths or []) if str(x or "").strip()]:
        src = Path(raw)
        if not src.exists():
            failed.append((raw, "文件不存在或已被移动"))
            continue
        target = _free_target(tr, src.stem, src.suffix)
        try:
            shutil.move(str(src), str(target))
        except OSError as e:
            failed.append((raw, f"移入失败：{type(e).__name__}: {e}"))
            continue
        _migrate_all_paths(str(src), str(target))
        try:
            size = target.stat().st_size
        except OSError:
            size = 0
        db.execute(
            "INSERT OR REPLACE INTO output_trash(path, origin_path, name, size, trashed_at) "
            "VALUES(?,?,?,?,?)",
            (_norm(target), _norm(src), target.name, size, _now()))
        ok.append(str(target))
    return ok, failed


def list_trash():
    """回收站列表：以 output_trash 行为准，磁盘上已消失的孤儿行顺手清掉。"""
    from store import db
    rows = []
    for r in db.query("SELECT path, origin_path, name, size, trashed_at FROM output_trash "
                      "ORDER BY trashed_at DESC"):
        p = Path(r["path"])
        if not p.exists():
            db.execute("DELETE FROM output_trash WHERE path=?", (r["path"],))
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        rows.append({
            "path": str(p),
            "name": p.name,
            "origin_path": r["origin_path"] or "",
            "size": st.st_size,
            "trashed_at": r["trashed_at"] or "",
            "created_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "display": display_map().get(_norm(p), ""),
        })
    return rows


def restore(paths):
    """从回收站还原：物理移回 origin_path 所在目录（撞名重排），元数据随路径迁回。

    返回 (成功列表, [(路径, 原因)])。"""
    from store import db
    ok, failed = [], []
    for raw in [str(x) for x in (paths or []) if str(x or "").strip()]:
        rows = db.query("SELECT origin_path FROM output_trash WHERE path=?", (_norm(raw),))
        src = Path(raw)
        if not rows:
            failed.append((raw, "不在回收站记录里"))
            continue
        if not src.exists():
            db.execute("DELETE FROM output_trash WHERE path=?", (_norm(raw),))
            failed.append((raw, "文件已丢失"))
            continue
        origin = Path(rows[0]["origin_path"] or "")
        dest_dir = origin.parent if str(origin) else scan_root()
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            target = _free_target(dest_dir, src.stem, src.suffix)
            shutil.move(str(src), str(target))
        except OSError as e:
            failed.append((raw, f"移回失败：{type(e).__name__}: {e}"))
            continue
        _migrate_all_paths(str(src), str(target))
        db.execute("DELETE FROM output_trash WHERE path=?", (_norm(raw),))
        ok.append(str(target))
    return ok, failed


def purge(paths):
    """彻底删除：真删磁盘文件 + 清掉回收站行与所有按路径存的元数据。不可逆。"""
    from store import db
    from store import task_store
    ok, failed = [], []
    for raw in [str(x) for x in (paths or []) if str(x or "").strip()]:
        src = Path(raw)
        if src.exists():
            try:
                src.unlink()
            except OSError as e:
                failed.append((raw, f"删除失败：{type(e).__name__}: {e}（文件可能正在播放）"))
                continue
        key = _norm(raw)
        db.execute("DELETE FROM output_trash WHERE path=?", (key,))
        db.execute("DELETE FROM output_display WHERE path=?", (key,))
        unbind(raw)
        try:
            task_store.set_file_mark(raw, "")
        except Exception:
            pass
        ok.append(raw)
    return ok, failed


def clear_trash():
    """清空回收站：把里面每条都彻底删除。返回 (成功列表, 失败列表)。"""
    return purge([r["path"] for r in list_trash()])


# ---------------- 外显名称（别名） ----------------

def display_map():
    """成品外显名：归一化路径 → 显示名（output_display 表，空串不入表）。"""
    from store import db
    out = {}
    for r in db.query("SELECT path, display FROM output_display"):
        name = (r["display"] or "").strip()
        if name:
            out[_norm(r["path"])] = name
    return out


def set_display(path, display):
    """给一条成品设外显名（只存显示层，绝不改物理文件名）；display 为空＝清除自定义。"""
    from store import db
    key = _norm(path)
    name = str(display or "").strip()
    if not name:
        db.execute("DELETE FROM output_display WHERE path=?", (key,))
        return True
    db.execute("INSERT OR REPLACE INTO output_display(path, display, updated_at) VALUES(?,?,?)",
               (key, name, _now()))
    return True


def migrate_display(old, new):
    """文件改名/移动：外显名跟着走（与 migrate_bind 同口径，不跟就丢自定义显示名）。"""
    from store import db
    o, n = _norm(old), _norm(new)
    if not o or o == n:
        return
    rows = db.query("SELECT display FROM output_display WHERE path=?", (o,))
    if not rows:
        return
    db.execute("DELETE FROM output_display WHERE path=?", (o,))
    for r in rows:
        db.execute("INSERT OR REPLACE INTO output_display(path, display, updated_at) VALUES(?,?,?)",
                   (n, r["display"], _now()))


def default_display(row):
    """卡片底部显示名口径：有自定义外显名用它，否则回落到「文件名去后缀」。"""
    d = (row.get("display") or "").strip()
    if d:
        return d
    name = row.get("name") or ""
    return Path(name).stem if name else ""
