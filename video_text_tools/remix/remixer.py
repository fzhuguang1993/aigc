"""
video_text_tools/remix/remixer.py —— 混剪排序建议 + 拼接清单 + 导出

- suggest_order(clips, vision=None)：让豆包按营销叙事给一个 clip 索引排列；
  任何异常/非法排列（越界、缺项、非数组）一律回退用户原序，绝不静默丢片段。
- plan_remix(clips, name, out_dir=None, reencode=None, probe=None)：算导出目标路径与
  是否重编码（源规格不一致才重编），不动文件。
- run_remix(clips, name, ...)：按当前顺序 concat 拼接到 成品库/混剪/<日期>_<名>.mp4。

clip 约定：{"path", "block_type", "product", "duration", "label"}。
"""
import json
import re
from datetime import datetime
from pathlib import Path

from core import naming
from .. import ffmpeg_utils as ff

_JSON_RE = re.compile(r"[\[\{].*[\]\}]", re.S)

# 营销叙事的默认板块顺序（豆包没给合理排列时，按这个类型优先级排，比原序更可读）
_TYPE_PRIORITY = ["钩子", "痛点", "产品介绍", "权威背书", "使用场景", "行动号召"]


def _extract_json(text):
    m = _JSON_RE.search(text or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (ValueError, TypeError):
        return None


def _order_prompt(clips):
    lines = []
    for i, c in enumerate(clips):
        lines.append(f"[{i}] 类型={c.get('block_type') or '片段'} "
                     f"时长={float(c.get('duration') or 0):.1f}s "
                     f"名={c.get('label') or Path(str(c.get('path',''))).name}")
    return (
        "你是短视频混剪师。下面是待拼接的若干片段（每条前面的 [编号] 是它的 id）。\n"
        "请按爆款营销叙事（钩子→痛点→产品介绍→权威背书→使用场景→行动号召）"
        "给出一个最抓人的播放顺序。\n"
        "严格只输出一个 JSON 数组，元素是片段的 [编号]，必须恰好覆盖所有编号各一次、"
        "不得新增或遗漏，例如 [2,0,1,3]。不要任何多余文字。\n\n片段：\n" + "\n".join(lines))


def _valid_perm(arr, n):
    """arr 是否是 0..n-1 的一个完整排列。"""
    if not isinstance(arr, list) or len(arr) != n:
        return False
    seen = set()
    for x in arr:
        try:
            i = int(x)
        except (TypeError, ValueError):
            return False
        if not (0 <= i < n) or i in seen:
            return False
        seen.add(i)
    return True


def _priority_order(clips):
    """兜底：按 _TYPE_PRIORITY 稳定排序的索引排列（未知类型排最后，保持原相对序）。"""
    def key(pair):
        i, c = pair
        bt = c.get("block_type") or ""
        try:
            p = _TYPE_PRIORITY.index(bt)
        except ValueError:
            p = len(_TYPE_PRIORITY)
        return (p, i)
    return [i for i, _ in sorted(enumerate(clips), key=key)]


def _default_vision():
    from ..breakdown.vision import DoubaoVision
    from core.config import doubao_vision_config
    return DoubaoVision(doubao_vision_config())


def suggest_order(clips, vision=None, log=None):
    """返回一个索引排列（相对入参 clips 的新顺序）。

    豆包给的排列非法或调用失败 → 先按板块优先级兜底，优先级也全空则原序。绝不吞片段。"""
    n = len(clips or [])
    if n <= 1:
        return list(range(n))
    fallback = _priority_order(clips)
    try:
        v = vision or _default_vision()
        raw = v.chat_text(_order_prompt(clips), temperature=0.3, max_tokens=800)
    except Exception as e:
        if log:
            log(f"  ⚠ AI 顺序建议不可用（{e}），改按板块优先级排序")
        return fallback
    arr = _extract_json(raw)
    # 模型可能回 {"order":[...]} 或直接 [...]
    if isinstance(arr, dict):
        arr = arr.get("order") or arr.get("indices") or arr.get("result")
    if _valid_perm(arr, n):
        if log:
            log("  ✓ AI 已给出推荐播放顺序")
        return [int(x) for x in arr]
    if log:
        log("  ⚠ AI 返回的排列非法（越界/缺项），已回退按板块优先级排序")
    return fallback


def apply_order(clips, order):
    """按排列取片段；排列不完整时补齐遗漏项，保证一个不丢。"""
    clips = list(clips or [])
    picked, seen = [], set()
    for i in order or []:
        if 0 <= i < len(clips) and i not in seen:
            seen.add(i)
            picked.append(clips[i])
    for i, c in enumerate(clips):
        if i not in seen:
            picked.append(c)
    return picked


def _specs_same(clips, probe):
    """所有片段分辨率/帧率是否一致（拿不到信息视为一致，走快 concat）。"""
    sig = None
    for c in clips:
        try:
            info = probe(str(c.get("path", ""))) or {}
        except Exception:
            info = {}
        cur = (str(info.get("width")), str(info.get("height")), str(info.get("fps")))
        if cur == ("N/A", "N/A", "N/A") or not any(x != "N/A" for x in cur):
            continue                              # 探不到就跳过，不因此强制重编码
        if sig is None:
            sig = cur
        elif cur != sig:
            return False
    return True


def plan_remix(clips, name="", out_dir=None, reencode=None, probe=None):
    """算混剪导出计划（不动文件）：{clips:[path...], dst, reencode}。空片段回 None。

    out_dir 缺省 = 成品库根/混剪；reencode=None 时自动探测（规格不一致才重编码）。"""
    clips = [c for c in (clips or []) if str(c.get("path", "")).strip()]
    if not clips:
        return None
    if out_dir is None:
        from store import output_store
        out_dir = Path(output_store.output_root()) / "混剪"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = naming.sanitize((name or "").strip()) or "混剪"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = _free(out_dir / f"{stamp}_{stem}.mp4")
    if reencode is None:
        probe = probe or ff.get_video_info
        reencode = not _specs_same(clips, probe)
    return {"clips": [str(c["path"]) for c in clips], "dst": str(dst),
            "reencode": bool(reencode)}


def _free(p):
    p = Path(p)
    if not p.exists():
        return p
    i = 2
    while True:
        alt = p.with_name(f"{p.stem}({i}){p.suffix}")
        if not alt.exists():
            return alt
        i += 1


def run_remix(clips, name="", out_dir=None, reencode=None, log=None,
              order=None, concat=None):
    """拼接导出：先按 order（若给）排好，再 plan 再 concat。返回 {ok, dst, reason, count}。

    concat 可注入（默认 ff.concat_clips），便于无 ffmpeg 环境跑测。"""
    do_concat = concat or ff.concat_clips
    if order:
        clips = apply_order(clips, order)
    plan = plan_remix(clips, name=name, out_dir=out_dir, reencode=reencode)
    if not plan:
        return {"ok": False, "reason": "无可拼接片段", "dst": "", "count": 0}
    if log:
        log(f"  ✂ 混剪拼接 {len(plan['clips'])} 段"
            f"（{'重编码统一规格，较慢' if plan['reencode'] else '快速直拼'}）…")
    ok, reason = do_concat(plan["clips"], plan["dst"], reencode=plan["reencode"])
    if log:
        log(f"  {'✓ 混剪完成' if ok else '⚠ 混剪失败'}：{plan['dst'] if ok else reason}")
    return {"ok": bool(ok), "dst": plan["dst"] if ok else "",
            "reason": "" if ok else reason, "count": len(plan["clips"])}
