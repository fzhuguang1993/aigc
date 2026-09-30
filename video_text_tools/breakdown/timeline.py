"""
video_text_tools/breakdown/timeline.py —— 详情页时间轴纯逻辑（无 Qt，便于单测）

把 BreakdownResult 拍平成两条可点、可高亮的时间轴，供「拆解文档」屏渲染，
并给播放位置反算「当前落在哪一条」：

  shots  分镜轴：一条 = 一个分镜（frame_analyses[i] + 对齐的 segments[i]），
         ts 用作跳转与图集定位；
  lines  口播轴：一条 = 一句转写（transcript[i]），start 用作跳转。

图集与两条轴的对齐统一走 `index_at(times, sec)`：给定升序时间点与当前秒，返回
「最后一个 <= sec」的下标（早于首个返回 -1），详情页据此高亮条目、换图集大图。
"""


def index_at(times, sec):
    """升序 times 中「最后一个 <= sec」的下标；早于首个 / times 空 → -1。"""
    idx = -1
    for i, t in enumerate(times):
        if t <= sec:
            idx = i
        else:
            break
    return idx


def _split_prompts(result):
    """segments 与 frame_analyses 同序对齐（build_prompts 按分镜逐条产出）。"""
    return list(result.segments or [])


def build_timeline(result):
    """归一出 {"shots":[...], "lines":[...]}（缺数据给空表，绝不抛）。"""
    segs = _split_prompts(result)
    shots = []
    for i, a in enumerate(result.frame_analyses or []):
        seg = segs[i] if i < len(segs) else None
        shots.append({
            "idx": getattr(a, "idx", i + 1),
            "ts": float(getattr(a, "ts", 0) or 0),
            "time_range": (seg.time_range if seg else ""),
            "shot_size": getattr(a, "shot_size", ""),
            "camera": getattr(a, "camera", ""),
            "composition": getattr(a, "composition", ""),
            "transition": getattr(a, "transition", ""),
            "on_screen_text": getattr(a, "on_screen_text", ""),
            "emotion": getattr(a, "emotion", ""),
            "summary": (seg.summary if seg else ""),
            "visual_prompt": (seg.visual_prompt if seg else ""),
            "copy_prompt": (seg.copy_prompt if seg else ""),
            "shoot_prompt": (seg.shoot_prompt if seg else ""),
        })
    lines = []
    for i, s in enumerate(result.transcript or []):
        lines.append({
            "i": i,
            "start": float(getattr(s, "start", 0) or 0),
            "end": float(getattr(s, "end", 0) or 0),
            "text": getattr(s, "text", "") or "",
        })
    blocks = []
    for i, b in enumerate(getattr(result, "blocks", None) or []):
        blocks.append({
            "i": getattr(b, "index", i + 1),
            "type": getattr(b, "type", "") or "",
            "start": float(getattr(b, "start", 0) or 0),
            "end": float(getattr(b, "end", 0) or 0),
            "summary": getattr(b, "summary", "") or "",
        })
    return {"shots": shots, "lines": lines, "blocks": blocks}
