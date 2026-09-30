"""
video_text_tools/breakdown/prompts.py —— 3 类提示词生成 + 整体分析 + 改写去重

都复用已配置的豆包接入点（DoubaoVision.chat_text，纯文本），不引第二个模型服务：
  1. build_prompts：逐分镜出「画面生成 / 文案改写 / 复刻拍摄」3 类提示词 + 该段概要；
  2. build_analysis：整体分析出钩子描述/评分(0-10)/爆点因素/情绪曲线/内容公式/复刻蓝图；
  3. rewrite_for_dedup：把文案提示词整体改写一遍，规避查重与版权（约束6）。

模型返回按约定给 JSON；解析失败不硬崩——退化成用画面分析字段直接拼提示词，
保证「降级也能出可用结果」，整体分析失败则留空由 pipeline 标注。
"""
import json
import re

from .models import SegmentPrompts, OverallAnalysis, BlockSegment

_JSON_RE = re.compile(r"[\{\[].*[\}\]]", re.S)


def _fmt_ts(sec):
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


def _extract_json(text):
    m = _JSON_RE.search(text or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (ValueError, TypeError):
        return None


def build_shots(frame_analyses, transcript=None):
    """把逐帧分析整理成「分镜」列表：每帧一条，带时间区间与该区间口播。

    返回 [{idx, start, end, time_range, shot_size, camera, ..., speech}]。
    """
    if not frame_analyses:
        return []
    times = [a.ts for a in frame_analyses]
    segs = transcript or []
    shots = []
    for i, a in enumerate(frame_analyses):
        start = a.ts
        end = times[i + 1] if i + 1 < len(times) else a.ts + 3
        # 该时间窗内起始落在其中的口播句拼接（粗对齐即可，供模型参考语气/信息点）
        speech = " ".join(s.text for s in segs if start <= s.start < end)
        shots.append({
            "idx": a.idx, "start": start, "end": end,
            "time_range": f"{_fmt_ts(start)}-{_fmt_ts(end)}",
            "shot_size": a.shot_size, "camera": a.camera,
            "composition": a.composition, "transition": a.transition,
            "on_screen_text": a.on_screen_text, "emotion": a.emotion,
            "raw": a.raw, "speech": speech,
        })
    return shots


def _shot_table(shots):
    lines = []
    for s in shots:
        lines.append(
            f"[{s['idx']}] {s['time_range']} 景别:{s['shot_size']} 运镜:{s['camera']} "
            f"构图:{s['composition']} 转场:{s['transition']} 文字:{s['on_screen_text']} "
            f"情绪:{s['emotion']} 口播:{s['speech']}")
    return "\n".join(lines)


def _fallback_prompts(shots):
    """模型没给出可用 JSON 时，直接用画面字段拼一份能用的 3 类提示词"""
    out = []
    for s in shots:
        visual = (f"{s['time_range']} {s['shot_size']}，{s['camera']}，构图{s['composition']}，"
                  f"主体文字「{s['on_screen_text']}」，情绪{s['emotion']}").strip("，")
        copy_p = s["speech"] or s["on_screen_text"]
        shoot = (f"{s['shot_size']}机位，{s['camera']}，{s['transition']}衔接；"
                 f"打光与构图参考：{s['composition']}")
        out.append(SegmentPrompts(index=s["idx"], time_range=s["time_range"],
                                  visual_prompt=visual, copy_prompt=copy_p,
                                  shoot_prompt=shoot, summary=s["emotion"]))
    return out


# 每批送豆包的分镜/文案条数：条数一多，单次输出会被 max_tokens 截断（实测 60 条
# ×5字段必炸“空/非数组”）；分批让每批输出量可控，某批失败只影响那几条、逐条兜底。
_PROMPT_BATCH = 12     # 提示词每批分镜数（5 字段/条，输出较大）
_REWRITE_BATCH = 30    # 改写每批文案数（仅 index+copy_prompt，输出较小）

_PROMPTS_HEADER = (
    "你是短视频复刻专家。下面是每条爆款视频的分镜画面分析表。请为每一分镜产出三类提示词：\n"
    "1) visual_prompt 画面生成提示词（可直接喂给 AI 视频/图像模型，含景别/运镜/构图/光线/主体）；\n"
    "2) copy_prompt 文案改写提示词（在原意基础上换词换句式，避免与原文重复）；\n"
    "3) shoot_prompt 复刻拍摄提示词（机位/道具/演员走位/转场的可执行清单）。\n"
    '严格输出 JSON 数组，每项形如 '
    '{"index":1,"time_range":"00:00-00:03","summary":"...",'
    '"visual_prompt":"...","copy_prompt":"...","shoot_prompt":"..."}，'
    "不要多余文字。\n\n分镜表：\n")


def _prompts_chunk(chunk, vision):
    """一批分镜 → 与 chunk 等长的列表（每条 SegmentPrompts 或 None=该条没拿到）。

    按**位置**对齐（不用模型回的 index，免得它每批从 1 重新编号串位）；模型给少了、
    或整批非数组 → 对应位置回 None，交调用方逐条兜底。"""
    data = _extract_json(vision.chat_text(_PROMPTS_HEADER + _shot_table(chunk),
                                          temperature=0.5, max_tokens=6000))
    if isinstance(data, dict):
        data = data.get("segments") or data.get("items") or []
    rows = [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
    out = []
    for i, shot in enumerate(chunk):
        item = rows[i] if i < len(rows) else None
        if item is None:
            out.append(None)
        else:
            out.append(SegmentPrompts(
                index=shot["idx"],
                time_range=str(item.get("time_range", shot.get("time_range", ""))),
                visual_prompt=str(item.get("visual_prompt", "")),
                copy_prompt=str(item.get("copy_prompt", "")),
                shoot_prompt=str(item.get("shoot_prompt", "")),
                summary=str(item.get("summary", ""))))
    return out


def build_prompts(shots, vision, log=None):
    """逐分镜生成 3 类提示词。返回 [SegmentPrompts]；模型没给到的那几条退化为 _fallback_prompts。

    分批调用（每批≤_PROMPT_BATCH）：60 条×5字段单次输出会被 max_tokens 截断→
    JSON 解析不出（实测“空/非数组”整批降级）；分批后每批输出量可控，单批失败/给少
    只影响那几条（按位置对齐，缺的逐条兜底），绝不整批丢。log 打心跳便于看进度。
    """
    if not shots:
        return []
    total = len(shots)
    chunks = [shots[i:i + _PROMPT_BATCH] for i in range(0, total, _PROMPT_BATCH)]
    if log:
        if len(chunks) == 1:
            log(f"  ⏳ 生成分镜提示词：{total} 个分镜、单次请求（通常 1~3 分钟）…")
        else:
            log(f"  ⏳ 生成分镜提示词：{total} 个分镜，分 {len(chunks)} 批逐批生成"
                f"（每批≤{_PROMPT_BATCH}，防输出截断）…")
    merged = []
    last_err = ""
    for ci, chunk in enumerate(chunks):
        try:
            merged.extend(_prompts_chunk(chunk, vision))
        except Exception as e:
            last_err = str(e)
            merged.extend([None] * len(chunk))       # 整批失败：这批全部留空待兜底
        if log and len(chunks) > 1:
            log(f"     …提示词第 {ci + 1}/{len(chunks)} 批完成")
    fb = _fallback_prompts(shots)
    out, n_model = [], 0
    for shot, p, f in zip(shots, merged, fb):
        if p is None:
            out.append(f)                             # 该条模型没给：用画面字段直拼
        else:
            out.append(p)
            n_model += 1
    if log:
        if n_model == 0:
            log(f"  ⚠ 分镜提示词生成失败（{last_err or '模型无有效返回'}），已降级为画面字段直拼")
        elif n_model < total:
            log(f"  ✓ 分镜提示词完成：{total} 条（其中 {total - n_model} 条降级为画面字段直拼）")
        else:
            log(f"  ✓ 分镜提示词完成：{total} 条")
    return out


def build_analysis(shots, transcript_text, vision, log=None):
    """整体分析：钩子评分/爆点因素/情绪曲线/内容公式/复刻蓝图。返回 OverallAnalysis。"""
    table = _shot_table(shots) if shots else "（无分镜画面）"
    prompt = (
        "你是爆款内容分析师。基于下面一条短视频的分镜画面与口播逐字稿，做一次可复用的整体分析。\n"
        "严格输出 JSON 对象，字段：\n"
        '{"hook_desc":"前3秒钩子描述","hook_score":"0-10分","factors":"爆点因素(分点用；分隔)",'
        '"emotion_curve":"情绪曲线(起承转合)","formula":"内容公式(可套用模板)",'
        '"blueprint":"复刻蓝图(分步骤)"}\n\n'
        f"【分镜】\n{table}\n\n【口播逐字稿】\n{transcript_text or '（无，画面拆解即可）'}")
    if log:
        log("  ⏳ 生成整体分析（单次大请求，通常 1~2 分钟）…")
    try:
        d = _extract_json(vision.chat_text(prompt, temperature=0.4, max_tokens=3000))
        if not isinstance(d, dict):
            raise ValueError("非对象")
        if log:
            log("  ✓ 整体分析完成")
        return OverallAnalysis(
            hook_desc=str(d.get("hook_desc", "")),
            hook_score=str(d.get("hook_score", "")),
            factors=str(d.get("factors", "")),
            emotion_curve=str(d.get("emotion_curve", "")),
            formula=str(d.get("formula", "")),
            blueprint=str(d.get("blueprint", "")))
    except Exception as e:
        if log:
            log(f"  ⚠ 整体分析失败（留空）：{e}")
        return OverallAnalysis()


def build_prompts_and_analysis(frame_analyses, transcript, vision, log=None):
    """一把出「分镜提示词 + 整体分析」。transcript 是 [TranscriptSegment] 或纯文本。"""
    shots = build_shots(frame_analyses,
                        transcript if isinstance(transcript, list) else None)
    if isinstance(transcript, list):
        transcript_text = "\n".join(s.text for s in transcript)
    else:
        transcript_text = transcript or ""
    if log:
        log(f"  分镜整理完成：{len(shots)} 个分镜，随后两次大请求：提示词 → 整体分析")
    segments = build_prompts(shots, vision, log=log)
    overall = build_analysis(shots, transcript_text, vision, log=log)
    return segments, overall


# 板块类型白名单兜底（配置缺失时用）：与计划 A1 默认一致。
BLOCKS_TYPES_DEFAULT = ["钩子", "痛点", "产品介绍", "权威背书", "使用场景", "行动号召"]

_BLOCKS_HEADER = (
    "你是短视频营销结构分析师。下面是一条带货短视频的分镜画面表与口播逐字稿。\n"
    "请把它按「营销板块」切成连续的时间段，每段标注属于哪一类。\n"
    "板块类型只能从这个白名单里选：{types}。\n"
    "严格输出 JSON 数组，每项形如 "
    '{{"type":"板块类型","start":起始秒,"end":结束秒,"summary":"一句话说明"}}，\n'
    "要求：start/end 为秒且 0<=start<end、各段按时间先后不重叠、并覆盖全片；不要多余文字。\n\n"
)


def _coerce_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_blocks(raw, types, duration):
    """模型回的板块列表 → 白名单过滤 + 时间钳位 + 去重叠，返回 [BlockSegment]。

    逐条校验：type 不在白名单、时间非数字、钳入 [0,duration] 后 start>=end → 丢弃；
    再按 start 升序，与上一段重叠（start < 上一段 end）的丢弃。全程绝不抛，给多少收多少。"""
    if duration is None or duration <= 0:
        return []
    cand = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type", "")).strip()
        if t not in types:
            continue
        start, end = _coerce_num(item.get("start")), _coerce_num(item.get("end"))
        if start is None or end is None:
            continue
        start = min(max(start, 0.0), duration)
        end = min(max(end, 0.0), duration)
        if start >= end:
            continue
        cand.append((start, end, t, str(item.get("summary", ""))))
    cand.sort(key=lambda x: x[0])
    out, last_end = [], 0.0
    for start, end, t, summary in cand:
        if start < last_end:          # 与已收段重叠 → 丢弃
            continue
        out.append(BlockSegment(index=len(out) + 1, type=t, start=start, end=end,
                                summary=summary))
        last_end = end
    return out


def build_blocks(shots, transcript_text, vision, types=None, log=None, duration=None):
    """按营销板块切时间轴。返回 [BlockSegment]；解析/调用失败降级为单块「整片」，绝不静默。

    types 为板块类型白名单（留空用 BLOCKS_TYPES_DEFAULT）；duration 缺省时从分镜表推算。
    复用 _extract_json 与整体分析同款单次大请求；降级仍标 ok（板块是锦上添花，不拖累主链）。"""
    types = [str(t).strip() for t in (types or BLOCKS_TYPES_DEFAULT) if str(t).strip()] \
        or BLOCKS_TYPES_DEFAULT
    if duration is None:
        duration = max((s.get("end", 0) for s in (shots or [])), default=0.0)
    duration = float(duration or 0)

    def _whole(note):
        blk = BlockSegment(index=1, type="整片", start=0.0, end=duration,
                           summary=note) if duration > 0 else None
        return ([blk], blk is not None) if blk else ([], False)

    table = _shot_table(shots) if shots else "（无分镜画面）"
    prompt = (_BLOCKS_HEADER.format(types="、".join(types)) +
              f"【分镜】\n{table}\n\n【口播逐字稿】\n{transcript_text or '（无）'}")
    if log:
        log(f"  ⏳ 板块拆分：按 {'/'.join(types)} 切时间轴（单次请求，通常 1~2 分钟）…")
    if duration <= 0:
        if log:
            log("  ⚠ 板块拆分跳过：未推算出视频时长")
        return [], False
    try:
        data = _extract_json(vision.chat_text(prompt, temperature=0.4, max_tokens=3000))
        if isinstance(data, dict):
            data = data.get("blocks") or data.get("items") or []
        blocks = _normalize_blocks(data, types, duration)
        if not blocks:
            raise ValueError("无有效板块")
        if log:
            log(f"  ✓ 板块拆分完成：{len(blocks)} 个板块")
        return blocks, True
    except Exception as e:
        blocks, ok = _whole(f"板块拆分失败（{str(e)[:60]}），回退整片")
        if log:
            log(f"  ⚠ 板块拆分失败，已降级为整片单块：{e}")
        return blocks, ok


def rewrite_for_dedup(segments, vision, log=None):
    """对文案提示词整体改写一遍，规避查重与版权（约束6）。就地更新 copy_prompt。

    分批调用（每批≤_REWRITE_BATCH）防输出截断；失败保留原提示词并把原因写进日志，
    绝不清空。返回是否有成功改写。
    """
    if not segments:
        return True
    todo = [s for s in segments if s.copy_prompt]
    if not todo:
        return True
    chunks = [todo[i:i + _REWRITE_BATCH] for i in range(0, len(todo), _REWRITE_BATCH)]
    if log:
        batch_note = (f"（分 {len(chunks)} 批）" if len(chunks) > 1 else "（单次大请求）")
        log(f"  ⏳ 改写去重 {len(todo)} 条文案{batch_note}，通常 1~2 分钟…")
    changed = 0
    last_err = ""
    for ci, ch in enumerate(chunks):
        numbered = "\n".join(f"{s.index}. {s.copy_prompt}" for s in ch)
        prompt = (
            "下面是若干条短视频文案提示词。请在保持原意与卖点的前提下逐条改写，"
            "替换句式与用词、调整语序，确保彼此以及与原文之间都尽量不重复，规避平台查重与版权风险。"
            "严格输出 JSON 数组，每项 {\"index\":编号,\"copy_prompt\":\"改写后\"}，不要多余文字。\n\n"
            + numbered)
        try:
            data = _extract_json(vision.chat_text(prompt, temperature=0.7, max_tokens=5000))
            if isinstance(data, dict):
                data = data.get("items") or []
            by_idx = {}
            for item in data if isinstance(data, list) else []:
                if isinstance(item, dict) and item.get("copy_prompt"):
                    by_idx[int(item.get("index", 0))] = str(item["copy_prompt"])
            for s in ch:
                new = by_idx.get(s.index)
                if new:
                    s.copy_prompt = new
                    changed += 1
        except Exception as e:
            last_err = str(e)
        if log and len(chunks) > 1:
            log(f"     …改写第 {ci + 1}/{len(chunks)} 批完成")
    if changed:
        if log:
            log(f"  ✓ 改写去重完成：改 {changed} 条")
        return True
    if log:
        log(f"  ⚠ 改写去重失败：{last_err or '模型无有效返回'}")
    return False
