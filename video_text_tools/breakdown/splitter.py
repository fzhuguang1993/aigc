"""
video_text_tools/breakdown/splitter.py —— 板块/手动区间 → ffmpeg 切割 → 归档素材库

把一条拆解的营销板块（mode="auto" 取 result.blocks）或用户手选区间
（mode="manual" 传 [{type,start,end}]）逐段切成视频，按
`素材库/<产品>/<板块类型>/<标题>_<起-止>.<ext>` 归档，成功后写 material_clips 索引。

切割能力取自 ffmpeg_utils.cut_segment（默认 -c copy 快切，可 reencode 精确切）；
落盘根走 core.storage.get_storage（本轮本地）。命名口径与 processors/archiver 一致
（naming.sanitize 清非法字符、占用名挂 (2) 绝不覆盖）。
"""
from pathlib import Path

from core import naming
from .. import ffmpeg_utils as ff


def _mmss(sec):
    s = int(max(0, float(sec)))
    return f"{s // 60:02d}{s % 60:02d}"


def _fmt_range(start, end):
    return f"{_mmss(start)}-{_mmss(end)}"


def _title_slug(result):
    """片段名主干：优先视频文件名 stem，退标题，再退链接；剔非法字符、截 40。"""
    vp = (getattr(result, "video_path", "") or "").strip()
    stem = Path(vp).stem if vp else ""
    stem = stem or (getattr(result, "title", "") or "").strip() \
        or (getattr(result, "link", "") or "").strip()
    cleaned = naming.sanitize(stem).strip(" ._")
    return (cleaned or "素材")[:40]


def _free(p):
    """目标名被占用挂 (2)(3)：绝不覆盖已有片段（同板块重切/同名并存）。"""
    p = Path(p)
    if not p.exists():
        return p
    i = 2
    while True:
        alt = p.with_name(f"{p.stem}({i}){p.suffix}")
        if not alt.exists():
            return alt
        i += 1


def _material_root(lib_root=None):
    if lib_root:
        return Path(lib_root)
    from core.config import storage_config
    return Path(storage_config()["material_root"])


def plan_cuts(result, mode="auto", manual=None, product="", lib_root=None,
              reencode=False):
    """算切割清单（不动文件）：[{src,start,end,dst,block_type,product,duration,summary,
    source_task_id,reencode}]。源视频不在 → 返回空（调用方给提示）。

    auto 取 result.blocks；manual 用传入区间 [{type,start,end,summary?}]。
    起止非法（end<=start）的条目直接跳过。"""
    src = (getattr(result, "video_path", "") or "").strip()
    if not src or not Path(src).exists():
        return []
    ext = Path(src).suffix or ".mp4"
    root = _material_root(lib_root)
    title = _title_slug(result)
    prod_dir = naming.sanitize((product or "").strip()) or "未归类"
    task_id = int(getattr(result, "task_id", 0) or 0)

    if mode == "manual":
        raw = manual or []
    else:
        raw = [{"type": b.type, "start": b.start, "end": b.end,
                "summary": b.summary} for b in (getattr(result, "blocks", None) or [])]

    items = []
    for b in raw:
        try:
            start, end = float(b.get("start", 0)), float(b.get("end", 0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        btype_raw = str(b.get("type") or "").strip()
        btype = naming.sanitize(btype_raw) or "片段"
        name = f"{title}_{_fmt_range(start, end)}{ext}"
        dst = _free(root / prod_dir / btype / name)
        items.append({
            "src": src, "start": start, "end": end, "dst": str(dst),
            "block_type": btype_raw or "片段",
            "product": (product or "").strip(),
            "duration": round(end - start, 3),
            "summary": str(b.get("summary") or ""),
            "source_task_id": task_id, "reencode": bool(reencode),
        })
    return items


def run_cuts(items, log=None, storage=None):
    """执行切割并入库：每条 cut_segment 成功后写 material_clips。返回 {cut, failed}。

    任一失败只跳过那条、把可读原因写日志，绝不中断整批（部分成功仍有价值）。"""
    from store import material_store
    backend = (storage.backend if storage is not None
               else (_default_storage().backend if items else "local"))
    done, failed = [], []
    total = len(items or [])
    for i, it in enumerate(items or [], 1):
        if log:
            log(f"  ✂ 切割 {i}/{total}：{it['block_type']} "
                f"[{_fmt_range(it['start'], it['end'])}]")
        ok, reason = ff.cut_segment(it["src"], it["start"], it["end"], it["dst"],
                                     reencode=it.get("reencode", False))
        if not ok:
            failed.append((it["dst"], reason))
            if log:
                log(f"    ⚠ 跳过（{reason}）")
            continue
        cid = material_store.add(
            path=it["dst"], backend=backend, block_type=it["block_type"],
            product=it["product"], source_task_id=it.get("source_task_id", 0),
            start=it["start"], end=it["end"], duration=it["duration"],
            note=it.get("summary", ""))
        done.append({"id": cid, **{k: it[k] for k in
                                   ("dst", "block_type", "start", "end")}})
    if log:
        log(f"  {'✓' if done else '⚠'} 切割入素材库完成：成功 {len(done)}、"
            f"失败 {len(failed)}（库根见素材库页）")
    return {"cut": done, "failed": failed, "planned": total}


def _default_storage():
    from core.storage import get_storage
    return get_storage()
