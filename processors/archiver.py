"""
processors/archiver.py —— 批量归档：把「标了可用」的成品归进【成品库 / 素材库】。

为什么要归档而不是自动进库：
- 任务刚生成的视频留在 DOWNLOAD_DIR/日期/，是「在制品」——还没审、可能不合格；
- 只有人工点了「👍 可用」的成品才值得沉淀进库。所以归档只收可用标记的那几条。

以数据库为准逐条搬（不去扫盘）：tasks.output 存着每条成品落在哪、属于哪个任务，
产品/标签都来自任务；扫盘只会看到一堆 mp4，不知道某文件对应哪条任务。

目的地二选一（DEST_OUTPUT=成品库 / DEST_MATERIAL=素材库，根目录来自 storage 配置）：
- 成品库：文件搬进 <成品库>/<产品>/<标签>/，成品库页按盘扫描即可见；
- 素材库：文件搬进 <素材库>/<产品>/<标签>/，并登记一行 material_clips（类型记为
  标签，缺省「成品」），否则读表渲染的素材库页看不到它。

搬完同步三处，否则任务中心双击报「文件不存在」、清理也找不到标记：
- tasks/runs 里的输出路径（task_store.replace_output_path）；
- 审片标记以「文件当前路径」为键，跟着搬家（task_store.move_file_mark）；
- 已在任一库目录下的文件视为「已归档」，不再重复搬（幂等）。只搬位置、不改文件名。
"""
import shutil
from pathlib import Path

from core import naming
from store import db, task_store

UNPRODUCT = "未填品名"      # 产品没填的落这（产品很关键，但不能因为没有就丢掉）
UNTAGGED = "未打标"         # 没打标签的落这，归档后再补打标签可重新归

DEST_OUTPUT = "output"        # 归档目的地：成品库
DEST_MATERIAL = "material"    # 归档目的地：素材库
DEST_LABEL = {DEST_OUTPUT: "成品库", DEST_MATERIAL: "素材库"}


def roots():
    """两个库的根目录（storage 配置解析，留空回退 DOWNLOAD_DIR 下默认子目录）。"""
    from core.config import storage_config
    sc = storage_config()
    return {"output": Path(sc["output_root"]), "material": Path(sc["material_root"])}


def dest_root(dest):
    return roots()[DEST_MATERIAL if dest == DEST_MATERIAL else DEST_OUTPUT]


def _resolve_key(path):
    try:
        return str(Path(str(path)).resolve())
    except OSError:
        return str(path)


def _under(path, root):
    """path 是否落在 root 目录下（含自身）——用来判断「已经进过某个库」。"""
    try:
        Path(str(path)).resolve().relative_to(Path(str(root)).resolve())
        return True
    except (ValueError, OSError):
        return False


def target_dir(product, tag, root):
    """这条成品在该库里的落点目录：<库根>/<产品>/<标签>/（清洗成合法目录名）。"""
    prod = naming.sanitize((product or "").strip()) or UNPRODUCT
    tg = naming.sanitize((tag or "").strip()) or UNTAGGED
    return Path(str(root)).resolve() / prod / tg


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
    """算出可归档清单（不动文件）：只收「文件还在、且标了『可用』」的成品。

    - 没标可用 / 标了不可用的：不在归档范围（留在 outputs/日期/ 里，等审片或清理）；
    - 已经在成品库或素材库目录下的：视为已归档，跳过（幂等，避免二次搬运）。
    返回 [{src, name, product, tag}]，目标目录由 run 按目的地算。
    """
    marks = task_store.marks_by_path()          # {归一化路径: 标记}
    rts = roots()
    lib_roots = [rts["output"], rts["material"]]
    items, seen = [], set()
    for t in db.query("SELECT id, product, tag, output FROM tasks"
                      " WHERE output IS NOT NULL AND output != ''"):
        for src in task_store._split_outputs(t["output"]):
            sp = Path(src)
            key = _resolve_key(src)
            if key in seen or not sp.is_file():
                continue
            if marks.get(key, "") != task_store.MARK_OK:
                continue                        # 只归档标了「可用」的
            if any(_under(sp, r) for r in lib_roots):
                continue                        # 已在某个库目录下：已归档，不再动
            seen.add(key)
            items.append({"src": key, "name": sp.name,
                          "product": (t["product"] or "").strip(),
                          "tag": (t["tag"] or "").strip()})
    items.sort(key=lambda d: (d["product"], d["tag"], d["src"]))
    return items


def summary(items):
    """把清单压成 (产品, 标签, 条数) 的汇总行，给确认弹窗看：搬之前先对一眼"""
    from collections import Counter
    c = Counter((it["product"] or UNPRODUCT, it["tag"] or UNTAGGED) for it in items)
    return [(p, t, n) for (p, t), n in sorted(c.items(), key=lambda x: (-x[1], x[0]))]


def _register_material(path, product, tag):
    """归档进素材库：登记一行 material_clips（素材库页读表渲染，不登记就看不见）。

    板块类型取任务的「标签」（如 开场钩子 / 情景剧…），没标签兜底成「成品」；
    产品沿用任务产品。时长留 0（不逐条 ffprobe，素材库页按需展示）。"""
    from store import material_store
    material_store.add(path=path, block_type=(tag or "").strip() or "成品",
                       product=(product or "").strip())


def run(items=None, dest=DEST_OUTPUT):
    """执行归档：把可用成品搬进指定库（dest=成品库/素材库）。返回 {moved, failed, planned, dest}

    去重后逐条：目标目录 <库根>/<产品>/<标签>/，撞名挂 (2)(3)；搬完同步审片标记与
    任务/执行输出路径；归档进素材库的额外登记 material_clips 一行。
    """
    if items is None:
        items = plan()
    root = dest_root(dest)
    moved, failed = [], []
    for it in items:
        src = Path(it["src"])
        if not src.is_file():
            failed.append((str(src), "文件已不存在"))
            continue
        tgt_dir = target_dir(it["product"], it["tag"], root)
        dst = _free(tgt_dir / src.name)
        try:
            tgt_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError as e:
            failed.append((str(src), f"{type(e).__name__}: {e}"))
            continue
        # 标记与输出路径都跟着搬：搬完任务中心双击、一键清理都还认得这条
        task_store.move_file_mark(str(src), str(dst))
        task_store.replace_output_path(str(src), str(dst))
        if dest == DEST_MATERIAL:
            _register_material(str(dst), it["product"], it["tag"])
        moved.append({"src": str(src), "dst": str(dst),
                      "product": it["product"] or UNPRODUCT,
                      "tag": it["tag"] or UNTAGGED})
    return {"moved": moved, "failed": failed, "planned": len(items), "dest": dest}
