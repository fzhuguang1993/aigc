"""
processors/output_mark.py —— 成品审片：把「可用 / 不可用」落到文件名和库里

为什么两件事必须绑在一起做：
1) 文件名上打记号（`_01_雷亮.mp4` → `_01_雷亮_不可用.mp4`）——同事在资源管理器
   里不打开软件也看得出哪些别发；
2) 库里记一笔 + 把 tasks/runs 里的路径改成新名字——不然任务中心那一格还指着
   老名字，双击就报「文件不存在」，「一键清理」也找不到它。

只做重命名与清理，不做界面：播放器与任务中心都调这里的函数。
"""
import re
import time
from pathlib import Path

from core.config import DOWNLOAD_DIR
from store import task_store
from utils.desktop_utils import move_to_trash

BAD = task_store.BAD_SUFFIX          # 文件名后缀：不可用

# 文件名尾部的记号：`_不可用`，允许带下载撞名时的 `(2)` 尾巴
_BAD_TAIL = re.compile(rf"[_\-]{re.escape(BAD)}(?:\(\d+\))?$")


def _key(path):
    """归一化路径做集合的键

    必须把符号链接解开：macOS 上临时目录是 /var → /private/var 的链，
    库里存的是 resolve 后的绝对路径，扫盘扫到的是带 /var 的写法，
    不归一同一份文件会被当成两条。"""
    try:
        return str(Path(str(path)).resolve())
    except OSError:
        return str(path)


def is_marked_bad(path):
    """光看文件名判断这条是不是已被标为不可用"""
    return bool(_BAD_TAIL.search(Path(str(path)).stem))


def _free_name(target, origin_stem):
    """目标名被占用就挂 (2)(3)：绝不覆盖别人的文件（同名多半是另一条抽卡）"""
    p = Path(target)
    if not p.exists():
        return p
    i = 2
    while True:
        alt = p.with_name(f"{origin_stem}({i}){p.suffix}")
        if not alt.exists():
            return alt
        i += 1


def _to_bad_path(path):
    p = Path(path)
    if _BAD_TAIL.search(p.stem):
        return p
    return _free_name(p.with_name(f"{p.stem}_{BAD}{p.suffix}"), f"{p.stem}_{BAD}")


def _to_clean_path(path):
    p = Path(path)
    m = _BAD_TAIL.search(p.stem)
    if not m:
        return p
    stem = p.stem[:m.start()]
    return _free_name(p.with_name(f"{stem}{p.suffix}"), stem)


def _rename(old, new, retries=3):
    """改名，失败重试几次

    要重试是因为「在播放器里点标记」时媒体后端还开着这个文件：Windows 上
    打开中的文件改不了名，播放器那边松手（stop + 清 source）到真正释放之间
    有个几百毫秒的窗口，不重试就会让用户点一次失败一次。"""
    if Path(old) == Path(new):
        return True, ""
    last = ""
    for i in range(max(retries, 1)):
        try:
            Path(old).rename(new)
            return True, ""
        except OSError as e:
            last = f"{type(e).__name__}: {e}"
            if i + 1 < retries:
                time.sleep(0.25)          # 等媒体后端把句柄放开
    return False, last


def set_mark(path, mark):
    """给一条成品打标记：mark 取 MARK_BAD / MARK_OK / ""（取消）

    返回 (是否成功, 文件现在的真实路径, 给人看的一句话)。
    标「不可用」＝文件名加记号；标「可用」和取消标记都要把记号摘掉，
    区别只在库里留不留那条记录。"""
    p = Path(str(path or ""))
    if not str(path or "").strip():
        return False, "", "没有可标记的文件"
    if not p.exists():
        return False, str(p), "文件不存在或已被移动"

    want_bad = mark == task_store.MARK_BAD
    target = _to_bad_path(p) if want_bad else _to_clean_path(p)
    if Path(target) != p:
        ok, err = _rename(p, target)
        if not ok:
            return False, str(p), f"重命名失败：{err}"
        task_store.move_file_mark(str(p), str(target))
        task_store.replace_output_path(str(p), str(target))
    task_store.set_file_mark(str(target),
                             task_store.MARK_BAD if want_bad
                             else (task_store.MARK_OK if mark else ""))
    if want_bad:
        msg = "已标记不可用" + ("并重命名文件" if str(target) != str(p) else "")
    elif mark:
        msg = "已标记可用" + ("并去掉文件名上的记号" if str(target) != str(p) else "")
    else:
        msg = "已取消标记"
    return True, str(target), msg


def mark_of(path):
    """这条成品现在的标记：优先看库，库里没有再看文件名（同事手改的也认）"""
    m = task_store.get_file_mark(path)
    if m:
        return m
    return task_store.MARK_BAD if is_marked_bad(path) else ""


def collect_bad(root=None):
    """把所有「不可用」的成品列出来：库里的标记 ∪ 文件名带记号的

    只查库会漏掉同事在资源管理器里自己改名的文件；只查文件名则库里的记录
    会一直挂在列表上当还存在。返回 [{path, exists, size}]，按路径排序。
    """
    found = {}                         # 归一化路径 → 一条记录
    for r in task_store.list_file_marks(task_store.MARK_BAD):
        found[_key(r["path"])] = {"path": str(r["path"]), "exists": False, "size": 0}
    base = Path(root or DOWNLOAD_DIR)
    if base.exists():
        for p in base.rglob(f"*{BAD}*.mp4"):
            if not p.is_file():
                continue
            found[_key(p)] = {"path": str(p), "exists": True, "size": p.stat().st_size}
    out = []
    for item in found.values():
        p = Path(item["path"])
        item["exists"] = p.is_file()
        item["size"] = p.stat().st_size if item["exists"] else 0
        out.append(item)
    out.sort(key=lambda d: d["path"])
    return out


def cleanup(paths=None):
    """把不可用的成品移进【回收站】，返回统计 dict

    只删文件 + 清掉对应的库标记，不删任务/执行记录：那次执行确实发生过，
    成功率与平均生成时长不该因为事后删片而变。
    paths 省略＝清全部（collect_bad 的结果）；传列表只清这些。
    """
    items = ([{"path": str(p)} for p in paths] if paths is not None
             else collect_bad())
    targets = [i["path"] for i in items if Path(i["path"]).is_file()]
    gone = [i["path"] for i in items if not Path(i["path"]).is_file()]
    # 体积要在删之前拿：进过回收站后原路径已经 stat 不到了
    sizes = {p: Path(p).stat().st_size for p in targets}
    ok, failed = move_to_trash(targets) if targets else ([], [])
    for p in ok + gone:                 # 文件早就不在了的：清掉孤儿标记
        task_store.set_file_mark(p, "")
    return {"trashed": ok, "freed": sum(sizes.get(p, 0) for p in ok),
            "failed": failed, "missing": gone}
