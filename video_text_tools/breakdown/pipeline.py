"""
video_text_tools/breakdown/pipeline.py —— 爆款拆解总编排

run(url, opts, log, progress, should_stop) 串起六个阶段，每阶段独立 try/except：
  解析下载 → 抽帧 → 转写 → 视觉分析 → 提示词生成 → 改写去重
任一阶段失败都写进 result.stage_status（"fail:原因"），能降级的继续降级跑
（如没装 faster-whisper，转写标 fail，画面拆解与提示词照出）——绝不静默（约束7）。

重依赖全延迟导入；凭证/白名单从 core.config 现读。临时帧图收尾即清（约束8：
半成品不写盘由 GUI 依据 result.is_partial() 决定，这里只管产数据）。
"""
import shutil
import threading
import time
import dataclasses
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import acquire, cache as cache_mod, frames as frames_mod, prompts as prompts_mod
from ..asr import transcribe as transcribe_mod
from ..asr.fixer import apply_fix
from .models import (BreakdownResult, DepMissing, ModelNotReady, ConfigError,
                     STAGE_ACQUIRE, STAGE_FRAMES, STAGE_TRANSCRIBE,
                     STAGE_VISION, STAGE_BLOCKS, STAGE_PROMPTS, STAGE_REWRITE,
                     FrameAnalysis, SegmentPrompts, BlockSegment, OverallAnalysis,
                     _segment_from_dict)

# 逐帧视觉分析提示词：要求模型按固定字段回 JSON，vision 里解析成 FrameAnalysis
FRAME_PROMPT = (
    "你是专业短视频分镜师。请分析第 {idx} 帧画面，只输出一个 JSON 对象，字段：\n"
    '{"shot_size":"景别(远/全/中/近/特写)","camera":"运镜(固定/推/拉/摇/移/跟/手持)",'
    '"composition":"构图要点","transition":"与上帧的转场(如无写\\"无\\")",'
    '"on_screen_text":"画面里出现的文字(无则空)","emotion":"画面传达的情绪"}\n'
    "不要多余解释文字。")

# 方舟 video_url 以 URL 传视频的上限（官方文档：超过走 Files API/TOS）
_VIDEO_URL_MAX_BYTES = 50 * 1024 * 1024


def _video_url_eligible(video_path, video_url):
    """直连模式可用性：公网 http(s) 直链 + 同一视频文件 ≤50MB。返回 (ok, 原因)。"""
    if not (video_url or "").lower().startswith("http"):
        return False, "解析接口未返回公网直链"
    try:
        size = Path(video_path).stat().st_size
    except OSError:
        return False, "本地视频文件不存在"
    if size > _VIDEO_URL_MAX_BYTES:
        return False, f"视频 {size // 1048576}MB 超过 50MB（方舟 video_url 上限）"
    return True, ""


def _work_dir(link):
    """本条链接的临时工作目录：BREAKDOWN_TMP/<时间戳>，存帧图，跑完即删"""
    from core.config import BREAKDOWN_TMP
    d = Path(BREAKDOWN_TMP) / f"{int(time.time() * 1000)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _progress(progress, pct, label):
    if progress:
        try:
            progress(pct, 100, label)
        except TypeError:
            progress(pct, label)


def _maybe_asr_fix(result, opts, glossary, log, progress):
    """把转写逐字稿送 DeepSeek 语义纠错、按编号回填替换每段文本（时间戳不动）。

    仅当调用方显式开 opts["asr_fix"] 时才发请求（默认关：pipeline 绝不擅自联网）。
    未配置 / 任何失败 → 保留原始识别、记一条 note，绝不抛、不 fail 拆解。
    纠错成本并进 result.cost（fix_calls/fix_tokens，不冲掉豆包 vision 的键）。
    具体纠正动作统一交给 asr.apply_fix（与字幕 engine、语音识别工具共用同一实现）。
    """
    if not opts.get("asr_fix") or not result.transcript:
        return
    _progress(progress, 42, "DeepSeek 语义纠错…")
    changed, cost, note = apply_fix(result.transcript, glossary=glossary,
                                    cfg=opts.get("asr_fix_cfg"))
    if cost:
        result.cost.update(cost)
    if changed:
        result.transcript_text = "\n".join(s.text for s in result.transcript)
    result.notes.append(note)
    if log:
        log(("✨ " if changed else "⚠ ") + note)


def run(url, opts=None, log=None, progress=None, should_stop=None):
    """拆解一条链接，返回 BreakdownResult（含 stage_status / cost）。"""
    opts = opts or {}

    def _log(msg):
        if log:
            log(msg)

    def _stopped():
        return bool(should_stop and should_stop())

    # 进度单调化：拆解各阶段百分比只允许前进、不回退——并行/回退时不把进度条来回拽
    _prog_lock = threading.Lock()
    _prog_state = {"pct": -1}

    def _prog(pct, label):
        with _prog_lock:
            if pct <= _prog_state["pct"]:
                return
            _prog_state["pct"] = pct
            _progress(progress, pct, label)

    result = BreakdownResult(link=url)
    # 有 out_dir 就把视频落到那里（帧图仍是其下 _frames 临时目录，跑完删）；
    # 没给就整体落临时目录 BREAKDOWN_TMP/<时间戳>。
    if opts.get("out_dir"):
        work = Path(opts["out_dir"])
        work.mkdir(parents=True, exist_ok=True)
    else:
        work = _work_dir(url)

    # ---------- 阶段缓存 / 断点续跑（调用方显式开 opts["cache"] 才生效） ----------
    # 命中的阶段跳过下载 / 转写 / 豆包（省接口费与本地耗时）；缺的阶段才补跑。
    # 全部产物按 link_key 归到同一目录，同一作品天然一致，故各阶段可独立命中。
    # force=强制重跑：全程重算并覆盖写缓存。默认不开缓存，保持既有调用方行为不变。
    cache_enabled = bool(opts.get("cache"))
    resume = opts.get("resume", True)
    force = opts.get("force", False)
    cache_dir = opts.get("cache_dir") or None
    _ck = (opts.get("cache_key")
           or (cache_mod.link_key(url) if (cache_enabled and url) else "")) \
        if cache_enabled else ""

    def _hit(stage):
        return bool(_ck) and resume and not force and \
            cache_mod.is_stage_ok(_ck, stage, base=cache_dir)

    def _save(stage, obj):
        if _ck:
            cache_mod.save_stage(_ck, stage, obj, base=cache_dir)

    try:
        # ---------- 1. 解析下载（可命中缓存：复用已下载视频，免解析免下载） ----------
        _prog(5, "解析下载…")
        acq = cache_mod.load_stage(_ck, STAGE_ACQUIRE, base=cache_dir) \
            if _hit(STAGE_ACQUIRE) else None
        cvid = cache_mod.video_path(_ck, base=cache_dir) if _ck else None
        if acq is not None and cvid is not None and cvid.exists():
            video = str(work / cvid.name)
            shutil.copy2(cvid, video)
            title = acq.get("title", "")
            video_url = acq.get("video_url", "")
            result.video_path, result.title = video, title
            result.notes += list(acq.get("notes") or [])
            result.mark(STAGE_ACQUIRE, ok=True)
            _log("⏭ 命中缓存：复用已下载视频，跳过解析下载")
        else:
            try:
                video, title, notes, video_url = acquire.resolve_and_download(
                    url, str(work), opts.get("hosts_video"), log=log,
                    api_cfg=opts.get("api_cfg"))
                result.video_path, result.title = video, title
                result.notes += notes
                result.mark(STAGE_ACQUIRE, ok=True)
            except Exception as e:
                result.mark(STAGE_ACQUIRE, ok=False, reason=str(e))
                _log(f"✗ 解析下载失败：{e}")
                return result                     # 没视频后面都无从谈起
            if _ck:
                cache_mod.save_video_from(_ck, video, base=cache_dir)
                _save(STAGE_ACQUIRE, {"title": title, "video_url": video_url,
                                      "notes": list(notes or [])})

        if _stopped():
            return result

        # ---------- 视觉模式判定 ----------
        # video_url 直连（默认）：把解析接口的去水印公网直链一次传豆包、免逐帧上传，规避 429；
        # 条件不满足（无直链/>50MB/用户选 frames）走传统「抽帧+逐帧」链路。
        use_video_url = False
        if opts.get("vision_mode", "video_url") == "video_url":
            ok, why = _video_url_eligible(video, video_url)
            use_video_url = ok
            if not ok:
                result.notes.append(f"video_url 直连不可用（{why}），改用逐帧抽取")
                _log(f"  ⚠ video_url 直连不可用（{why}），回退逐帧抽取链路")

        def _do_frames():
            """阶段2 本地抽帧：成功回 shots；失败标 fail 回 None（调用方决定中止）。"""
            _prog(20, "抽帧…")
            try:
                shots = frames_mod.extract_frames(
                    video, str(work / "_frames"),
                    interval=int(opts.get("interval", 3)),
                    scene_thresh=float(opts.get("scene_thresh", 0.3)), log=log)
            except Exception as e:
                result.mark(STAGE_FRAMES, ok=False, reason=str(e))
                _log(f"✗ 抽帧失败：{e}")
                return None
            result.frames = shots
            result.shot_count = len(shots)
            result.shot_durations = "、".join(f"{s.ts:.0f}s" for s in shots[:20])
            result.mark(STAGE_FRAMES, ok=True)
            return shots

        # ---------- 阶段缓存快照：转写 / 视觉 / 提示词各自可独立命中 ----------
        # 同一 link_key 对应同一作品，已缓存的阶段产物天然可用，不必因上游重算而作废。
        tr_hit = cache_mod.load_stage(_ck, STAGE_TRANSCRIBE, base=cache_dir) \
            if _hit(STAGE_TRANSCRIBE) else None
        vi_hit = cache_mod.load_stage(_ck, STAGE_VISION, base=cache_dir) \
            if _hit(STAGE_VISION) else None
        pr_hit = cache_mod.load_stage(_ck, STAGE_PROMPTS, base=cache_dir) \
            if _hit(STAGE_PROMPTS) else None
        # 板块拆分：可选的锦上添花阶段（默认开），同样支持命中缓存
        use_blocks = bool(opts.get("use_blocks", True))
        bl_hit = cache_mod.load_stage(_ck, STAGE_BLOCKS, base=cache_dir) \
            if _hit(STAGE_BLOCKS) else None

        # 抽帧：仅「逐帧模式且视觉未命中」才做（直连模式靠 video_url；视觉命中则免抽）。
        shots = None
        if not use_video_url and vi_hit is None:
            shots = _do_frames()
            if shots is None:
                return result                 # 没帧无法做视觉拆解
            if _stopped():
                return result

        # ---------- 豆包客户端：仅在视觉/提示词真要调用时才建 ----------
        # 视觉与提示词都命中缓存时，整条拆解可完全离线复用（即便本机没配豆包）。
        vision = None
        if vi_hit is None or pr_hit is None or (use_blocks and bl_hit is None):
            from core.config import doubao_vision_config
            from .vision import DoubaoVision
            try:
                vision = DoubaoVision(opts.get("doubao_cfg") or doubao_vision_config())
            except ConfigError as e:
                result.mark(STAGE_VISION, ok=False, reason=str(e))
                _log(f"✗ 视觉分析未配置：{e}")
                return result                 # 没画面分析，提示词/整体分析也无意义

        # ---------- 转写所需：是否启用 Whisper + 模型档位 + 领域词库 ----------
        # 互斥开关：不启用 Whisper 时，整条「转写 + DeepSeek 语义纠错」都跳过——
        # 画面拆解（豆包分镜 ts）与图集/分镜时间轴联动不依赖 Whisper，照常产出；
        # 只有详情页「口播逐字稿逐句高亮」这一条轴需要 Whisper，本次不产出（留空）。
        use_whisper = opts.get("use_whisper", True)
        size = opts.get("model_size", transcribe_mod.DEFAULT_SIZE)
        glossary = opts.get("glossary")
        if glossary is None:
            try:
                from core.config import asr_glossary_load
                glossary = asr_glossary_load()
            except Exception:
                glossary = []
        asr_prompt = "、".join(glossary) if glossary else None

        def _do_transcribe():
            """阶段3 转写 + 3.5 语义纠错（可降级；可命中缓存；就地写 result，绝不抛）。"""
            if tr_hit is not None:
                result.transcript = [_segment_from_dict(x)
                                     for x in (tr_hit.get("segments") or [])]
                result.transcript_text = tr_hit.get("text", "")
                result.mark(STAGE_TRANSCRIBE, ok=True)
                _log("⏭ 命中缓存：跳过 Whisper 转写")
                return
            if not use_whisper:
                result.notes.append("已按选择跳过口播转写（未启用 Whisper）")
                _log("⏭ 跳过转写：本次未启用 Whisper，仅做画面拆解（分镜/图集联动不受影响）")
                return
            _prog(35, "转写口播…")
            try:
                segs, text = transcribe_mod.transcribe(
                    video, size, log=log, should_stop=should_stop,
                    initial_prompt=asr_prompt)
                result.transcript, result.transcript_text = segs, text
                result.mark(STAGE_TRANSCRIBE, ok=True)
            except (DepMissing, ModelNotReady) as e:
                result.mark(STAGE_TRANSCRIBE, ok=False, reason=str(e))
                _log(f"⚠ 跳过转写（可降级）：{e}")
            except Exception as e:
                result.mark(STAGE_TRANSCRIBE, ok=False, reason=str(e))
                _log(f"⚠ 转写失败（画面拆解继续）：{e}")
            if _stopped():
                return
            # 3.5 语义纠错（可选，DeepSeek）：未开关/未配置/任一步失败 → 原样保留识别，
            # 只记一条 note，绝不弹窗、绝不 fail 整条拆解。进度走单调 _prog。
            _maybe_asr_fix(result, opts, glossary, log,
                           lambda p, t, lab="": _prog(p, lab or "语义纠错…"))
            # 转写成功才落缓存（含纠错后的最终文本），下次命中直接复用
            if result.stage_ok(STAGE_TRANSCRIBE):
                _save(STAGE_TRANSCRIBE, {
                    "segments": [dataclasses.asdict(s) for s in result.transcript],
                    "text": result.transcript_text})

        def _do_vision():
            """阶段4 视觉分析（可命中缓存；就地写 result）。返回 True=拿到 analyses / False=整体失败(中止)。
            直连失败自动回退「抽帧+逐帧」；客户端已在外层建好，ConfigError 不在此路径。"""
            if vi_hit is not None:
                result.frame_analyses = [FrameAnalysis.from_dict(x)
                                         for x in (vi_hit.get("frame_analyses") or [])]
                result.shot_count = vi_hit.get("shot_count", len(result.frame_analyses))
                result.shot_durations = vi_hit.get("shot_durations", "")
                result.mark(STAGE_VISION, ok=True)
                _log("⏭ 命中缓存：跳过豆包视觉分析")
                return True
            def _per_frame(shots_):
                _prog(50, "逐帧视觉分析…")
                try:
                    an = vision.analyze_frames(
                        shots_, opts.get("frame_prompt", FRAME_PROMPT),
                        concurrency=int(opts.get("concurrency", 1)), log=log,
                        inter_frame_delay=float(opts.get("inter_frame_delay", 1.5)),
                        progress=lambda done, tot: _prog(
                            50 + int(25 * done / max(1, tot)),
                            f"逐帧视觉分析 {done}/{tot}…"))
                    result.frame_analyses = an
                    result.cost.update(vision.cost())
                    # 整批帧都失败（非“个别帧”）才算视觉分析失败
                    if an and all(str(a.raw).startswith("分析失败") for a in an):
                        raise RuntimeError(an[0].raw)
                    result.mark(STAGE_VISION, ok=True)
                    _save(STAGE_VISION, {
                        "frame_analyses": [a.to_dict() for a in result.frame_analyses],
                        "shot_count": result.shot_count,
                        "shot_durations": result.shot_durations})
                    return True
                except Exception as e:
                    result.mark(STAGE_VISION, ok=False, reason=str(e))
                    _log(f"✗ 视觉分析失败：{e}")
                    return False

            if not use_video_url:
                return _per_frame(shots)
            _prog(50, "video_url 直连解析…")
            try:
                an = vision.analyze_video(
                    video_url, opts.get("video_prompt"),
                    fps=float(opts.get("fps", 0.5)), log=log)
                result.frame_analyses = an
                result.shot_count = len(an)
                result.shot_durations = "、".join(f"{a.ts:.0f}s" for a in an[:20])
                result.cost.update(vision.cost())
                result.mark(STAGE_VISION, ok=True)
                _save(STAGE_VISION, {
                    "frame_analyses": [a.to_dict() for a in result.frame_analyses],
                    "shot_count": result.shot_count,
                    "shot_durations": result.shot_durations})
                return True
            except Exception as e:            # 含 VisionError：绝不静默，明示原因后回退逐帧
                _log(f"⚠ video_url 直连失败，已回退逐帧抽取：{e}")
                result.notes.append(
                    f"video_url 直连失败（{str(e)[:80]}），已自动回退逐帧抽取")
                result.cost.update(vision.cost())
                shots2 = _do_frames()          # 直连模式此前未抽帧：回退时补执行
                if shots2 is None:
                    return False
                return _per_frame(shots2)

        if use_video_url and use_whisper:
            # ---------- 并行：转写(本地 Whisper) ‖ 豆包直连解析（进度只前进） ----------
            _prog(35, "并行：转写口播 + 豆包直连解析…")
            cfg_err, vi_ok = None, False
            with ThreadPoolExecutor(max_workers=2) as ex:
                fut_tr = ex.submit(_do_transcribe)
                fut_vi = ex.submit(_do_vision)
                try:
                    vi_ok = fut_vi.result()
                except ConfigError as e:
                    cfg_err = e                # 理论已在外层拦住，兜底不再走到
                try:
                    fut_tr.result()            # 转写内部已降级，不应抛
                except Exception as e:
                    _log(f"⚠ 转写线程异常（画面拆解继续）：{e}")
            if cfg_err is not None:
                result.mark(STAGE_VISION, ok=False, reason=str(cfg_err))
                _log(f"✗ 视觉分析未配置：{cfg_err}")
                return result
            if vi_ok is False:
                return result
        else:
            _do_transcribe()
            if _stopped():
                return result
            if _do_vision() is False:
                return result

        if _stopped():
            return result
        analyses = result.frame_analyses

        # ---------- 4.5 板块拆分（可选、可命中缓存；降级为整片单块，绝不 fail 主链） ----------
        # 依赖已完成的画面分析与口播文本，故排在视觉之后、提示词之前。build_blocks 内部已
        # 对解析/调用失败降级为单块「整片」；本阶段只可能因“无时长”而空，一律标 ok 不污染 is_partial。
        if use_blocks:
            _prog(70, "板块拆分…")
            if bl_hit is not None:
                result.blocks = [BlockSegment.from_dict(x)
                                 for x in (bl_hit.get("blocks") or [])]
                result.mark(STAGE_BLOCKS, ok=True)
                _log("⏭ 命中缓存：跳过板块拆分")
            else:
                shots_b = prompts_mod.build_shots(analyses, result.transcript)
                dur_b = max((s.get("end", 0) for s in shots_b), default=0.0)
                blocks, ok = prompts_mod.build_blocks(
                    shots_b, result.transcript_text, vision,
                    types=opts.get("block_types"), log=log, duration=dur_b)
                result.blocks = blocks
                result.mark(STAGE_BLOCKS, ok=True)   # 辅助阶段：失败也不标 fail
                if not blocks:
                    result.notes.append("板块拆分未产出（无有效时长）")
                elif ok:
                    _save(STAGE_BLOCKS, {"blocks": [b.to_dict() for b in blocks]})
            if _stopped():
                return result

        # ---------- 5. 提示词生成 + 整体分析（可命中缓存） ----------
        _prog(75, "生成提示词与整体分析…")
        if pr_hit is not None:
            result.segments = [SegmentPrompts.from_dict(x)
                               for x in (pr_hit.get("segments") or [])]
            result.overall = OverallAnalysis.from_dict(pr_hit.get("overall") or {})
            result.duration = float(pr_hit.get("duration") or 0.0)
            result.mark(STAGE_PROMPTS, ok=True)
            _log("⏭ 命中缓存：跳过提示词生成")
        else:
            try:
                segments, overall = prompts_mod.build_prompts_and_analysis(
                    analyses, result.transcript, vision, log=log)
                result.segments, result.overall = segments, overall
                # 时长取分镜时间轴（直连模式无本地帧，统一用 frame_analyses 的 ts）
                result.duration = max((a.ts for a in analyses), default=0.0)
                result.mark(STAGE_PROMPTS, ok=True)
                _save(STAGE_PROMPTS, {
                    "segments": [s.to_dict() for s in result.segments],
                    "overall": result.overall.to_dict(),
                    "duration": result.duration})
            except Exception as e:
                result.mark(STAGE_PROMPTS, ok=False, reason=str(e))
                _log(f"✗ 提示词生成失败：{e}")

        # ---------- 6. 改写去重（可选，失败不致命；视觉未配置=全走缓存时跳过） ----------
        if result.stage_ok(STAGE_PROMPTS) and vision is not None and not _stopped():
            _prog(90, "文案改写去重…")
            try:
                ok = prompts_mod.rewrite_for_dedup(result.segments, vision, log=log)
                result.mark(STAGE_REWRITE, ok=ok,
                            reason="" if ok else "改写返回为空，保留原提示词")
                if not ok:
                    _log("⚠ 改写去重未生效，保留原提示词")
            except Exception as e:
                result.mark(STAGE_REWRITE, ok=False, reason=str(e))
                _log(f"⚠ 改写去重失败（保留原提示词）：{e}")

        _prog(100, "完成")
        return result
    finally:
        # 收尾清帧图（视频是否保留交调用方，这里只清 work 下的临时帧）
        if not opts.get("keep_work"):
            shutil.rmtree(work / "_frames", ignore_errors=True)
