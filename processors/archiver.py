"""
processors/archiver.py —— 批量归档：把 outputs/日期/ 里散着的成品，
按「日期 / 产品 / 标签」归进子文件夹，方便后续整理进素材库。

为什么以数据库为准逐条搬，而不去扫盘：
- 库里 tasks.output 存着每条成品落在哪、属于哪个任务（产品/标签都来自任务）；
- 扫盘只会看到一堆 mp4，不知道某个文件对应哪条任务、该进哪个标签夹。
所以只搬「库里记着且文件还在」的那些；同事手工丢进目录、库里没有的视频不认、不动。

搬完必须同步两处，否则任务中心双击会报「文件不存在」、清理也找不到标记：
- tasks/runs 里的输出路径（task_store.replace_output_path）；
- 审片标记以「文件当前路径」为键，跟着搬家（task_store.move_file_mark）。
只搬位置、不改文件名。
"""
import shutil
from pathlib import Path

from core.config import DOWNLOAD_DIR
from core import naming
from store import db, task_store

UNPRODUCT = "未填品名"      # 产品没填的落这（产品很关键，但不能因为没有就丢掉）
UNTAGGED = "未打标"         # 没打标签的落这，归档后再补打标签可重新归


def _split_top(path):
    """返回成品在输出根下的相对路径段（日期/文件名 两段）；不在根下或已进子目录 → None"""
    try:
        rel = Path(str(path)).resolve().relative_to(Path(DOWNLOAD_DIR).resolve())
    except (ValueError, OSError):
        return None
    return rel.parts


def target_dir(src, product, tag):
    """这条成品该去的目录：outputs/<日期>/<产品>/<标签>/

    只认顶层就是「日期/文件」两段的（历史平铺下载的位置）；已经在三层子目录里的
    视为已归档，返回 None 不重复动。产品/标签清洗成合法目录名，空值兜底成占位名。
    """
    parts = _split_top(src)
    if not parts or len(parts) != 2:
        return None
    date_seg = parts[0]
    prod = naming.sanitize((product or "").strip()) or UNPRODUCT
    tg = naming.sanitize((tag or "").strip()) or UNTAGGED
    return Path(DOWNLOAD_DIR).resolve() / date_seg / prod / tg


def _free(p):
    """目标名被占用就挂 (2)(3)：绝不覆盖别的成品（多半是另一条抽卡重名）"""
    p = Path(p)
    if not p.exists():
        return p
    i = 2
    while True:
        alt = p.with_name(f"{p.stem}({i}){p.suffix}")
        if not alt.exists():
            return alt
        i += 1


def plan():
    """算出归档清单（不动文件）：[{src, dst, product, tag}]，只收真正需要搬的。

    逐任务遍历其 output 里的每一条路径；文件不在的不算（可能已被清理）。
    """
    items, seen = [], set()
    for t in db.query("SELECT id, product, tag, output FROM tasks"
                      " WHERE output IS NOT NULL AND output != ''"):
        for src in task_store._split_outputs(t["output"]):
            sp = Path(src)
            key = str(_resolve_key(src))
            if key in seen or not sp.is_file():
                continue
            tgt = target_dir(src, t["product"], t["tag"])
            if tgt is None or tgt == sp.parent:
                continue                    # 已在子目录 / 已在正确位置：不动
            dst = _free(tgt / sp.name)
            seen.add(key)
            items.append({"src": str(sp), "dst": str(dst),
                          "product": (t["product"] or "").strip() or UNPRODUCT,
                          "tag": (t["tag"] or "").strip() or UNTAGGED})
    items.sort(key=lambda d: (d["product"], d["tag"], d["src"]))
    return items


def _resolve_key(path):
    try:
        return Path(str(path)).resolve()
    except OSError:
        return Path(str(path))


def summary(items):
    """把清单压成 (产品, 标签, 条数) 的汇总行，给确认弹窗看：搬之前先对一眼"""
    from collections import Counter
    c = Counter((it["product"], it["tag"]) for it in items)
    return [(p, t, n) for (p, t), n in sorted(c.items(), key=lambda x: (-x[1], x[0]))]


def run(items=None):
    """执行归档：items 省略＝现算 plan()。返回 {moved, failed, freed}"""
    if items is None:
        items = plan()
    moved, failed = [], []
    for it in items:
        src, dst = Path(it["src"]), Path(it["dst"])
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError as e:
            failed.append((str(src), f"{type(e).__name__}: {e}"))
            continue
        # 标记与输出路径都跟着搬：搬完任务中心双击、一键清理都还认得这条
        task_store.move_file_mark(str(src), str(dst))
        task_store.replace_output_path(str(src), str(dst))
        moved.append({"src": str(src), "dst": str(dst),
                      "product": it["product"], "tag": it["tag"]})
    return {"moved": moved, "failed": failed, "planned": len(items)}
