"""
store/clipboard_store.py —— 剪贴板历史的读写层

存法沿用 BREAKDOWN_LIBRARY 的「文件落盘 + DB 存路径」范式：
- 文字：正文直接进 clipboard_history.content 列；
- 图片：QPixmap 存成 CLIPBOARD_DIR/<id>.png，DB 只存 path 与尺寸，删行连带删文件。

为什么单开一个模块而不是塞进 db.py：db.py 是通用连接层（query/execute），
业务表各自的读写口径放各自的 store（org_store / build_store / breakdown_store
都是这个套路），剪贴板历史也照此归位。
"""
import os
from datetime import datetime

from PySide6.QtGui import QPixmap

from core.config import CLIPBOARD_DIR
from store import db

#: 各类历史保留上限：超了删最旧（图片连带删落盘文件），防止无限膨胀占盘。
MAX_TEXT = 200
MAX_IMAGE = 100

_KIND_TEXT = "text"
_KIND_IMAGE = "image"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_dir():
    try:
        os.makedirs(CLIPBOARD_DIR, exist_ok=True)
    except OSError:
        pass


def _preview_of_text(text):
    """列表显示用的首行摘要：压掉换行，截断到 80 字。"""
    first = (text or "").strip().splitlines()
    first = first[0] if first else ""
    return first[:80] + ("…" if len(first) > 80 else "")


def add_text(text):
    """写入一条文字；空白内容不入库，返回新行 id（失败给 None）。"""
    text = str(text or "")
    if not text.strip():
        return None
    row_id = db.execute(
        "INSERT INTO clipboard_history(kind, content, preview, created_at)"
        " VALUES('text', ?, ?, ?)",
        (text, _preview_of_text(text), _now()))
    prune()
    return row_id


def add_image(qpix: QPixmap):
    """把复制到的图片落盘成 PNG 并存一行；空图给 None，返回 (id, path)。"""
    if qpix is None or qpix.isNull():
        return None
    _ensure_dir()
    w, h = qpix.width(), qpix.height()
    ts = _now()
    # 先插占位拿 id，再用 <id>.png 落盘回写 path：文件名与主键一一对应，
    # 不用 uuid，删除/清理时按 path 精确回收，不会残留孤儿文件。
    row_id = db.execute(
        "INSERT INTO clipboard_history(kind, preview, w, h, created_at)"
        " VALUES('image', ?, ?, ?, ?)",
        ("%d×%d" % (w, h), w, h, ts))
    if row_id is None:
        return None
    path = os.path.join(CLIPBOARD_DIR, "%d.png" % row_id)
    if not qpix.save(path, "PNG"):
        db.execute("DELETE FROM clipboard_history WHERE id=?", (row_id,))
        return None
    db.execute("UPDATE clipboard_history SET path=? WHERE id=?", (path, row_id))
    prune()
    return row_id, path


def list_items(kind=None):
    """按时间倒序返回历史行；kind 给 'text'/'image' 只取该类，None 取全部。"""
    if kind in (_KIND_TEXT, _KIND_IMAGE):
        return db.query(
            "SELECT * FROM clipboard_history WHERE kind=?"
            " ORDER BY id DESC", (kind,))
    return db.query("SELECT * FROM clipboard_history ORDER BY id DESC")


def get(item_id):
    rows = db.query("SELECT * FROM clipboard_history WHERE id=?", (item_id,))
    return rows[0] if rows else None


def delete(item_id):
    """删一行；图片连带删落盘文件（文件没了就当没删过，DB 行照删）。"""
    row = get(item_id)
    if row is None:
        return False
    if row.get("kind") == _KIND_IMAGE and row.get("path"):
        _remove_file(row["path"])
    db.execute("DELETE FROM clipboard_history WHERE id=?", (item_id,))
    return True


def clear_all():
    """清空整张历史表并删干 CLIPBOARD_DIR 里的遗留 PNG。"""
    db.execute("DELETE FROM clipboard_history")
    _purge_dir()


def last_signature():
    """最近一条的「去重签名」：(kind, 内容或尺寸)，供监听器判断是否和上次相同。

    点击历史条目把内容复制回剪贴板时，会再次触发 dataChanged；签名相同就能识别出
    "这是自己刚写回去的"，跳过，不会把同一条重复入库。无历史给 None。"""
    rows = db.query(
        "SELECT kind, content, w, h FROM clipboard_history ORDER BY id DESC LIMIT 1")
    if not rows:
        return None
    r = rows[0]
    if r.get("kind") == _KIND_IMAGE:
        return (_KIND_IMAGE, r.get("w"), r.get("h"))
    return (_KIND_TEXT, r.get("content"))


def prune(max_text=MAX_TEXT, max_image=MAX_IMAGE):
    """各类各留最近 N 条，超出的最旧行删掉（图片连带删文件）。"""
    _prune_kind(_KIND_TEXT, max_text)
    _prune_kind(_KIND_IMAGE, max_image)


def _prune_kind(kind, keep):
    if keep is None or keep <= 0:
        keep = 1
    rows = db.query(
        "SELECT id, path FROM clipboard_history WHERE kind=? ORDER BY id DESC",
        (kind,))
    for r in rows[keep:]:
        if kind == _KIND_IMAGE and r.get("path"):
            _remove_file(r["path"])
        db.execute("DELETE FROM clipboard_history WHERE id=?", (r["id"],))


def _remove_file(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _purge_dir():
    try:
        for name in os.listdir(CLIPBOARD_DIR):
            if name.lower().endswith(".png"):
                _remove_file(os.path.join(CLIPBOARD_DIR, name))
    except OSError:
        pass
