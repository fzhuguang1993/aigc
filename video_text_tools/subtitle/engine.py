"""
video_text_tools/subtitle/engine.py —— 单条/批量字幕编排（回调式、无 Qt）

run_one 串起一条视频的全流程，每步独立 try/except，任何失败都落到 SubtitleResult
(ok=False, error=归类, message=人话)——绝不冒泡到调用方、绝不静默：
  解析音轨 → faster-whisper 转写 → (可选)抽帧检测已有字幕 → (可选)导出 SRT
  → 卖点挑选(可选) + 渲染 ASS → (可选)烧录成片
临时副本（含 detect 抽的帧）统一落在视频旁的 .subtitle_tmp 目录，收尾整目录删掉，
不碰录屏原片与旁路 WAV。DepMissing/ModelNotReady 单独归类，GUI 据此弹不同安装/下载引导。

progress 契约与全项目一致：progress(cur, total, label)。
"""
import os
import shutil
from pathlib import Path

from . import audio
from .ass import write_ass
from .burn import build_subtitle_command, ffmpeg_has_ass, run_burn
from .models import (SubtitleError, SubtitleOptions, SubtitleResult,
                     DepMissing, ModelNotReady)
from .srt import write_srt


def _out_dir(opts, video):
    d = (getattr(opts, "out_dir", "") or "").strip() or str(Path(video).parent)
    Path(d).mkdir(parents=True, exist_ok=True)
    return d


def _work_dir(video):
    return str(Path(video).with_name(Path(video).stem + ".subtitle_tmp"))


def _write_txt(segments, path):
    """逐字稿纯文本落盘（一行一句、不带时间戳）；失败不抛，返回空串。"""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for s in segments:
                f.write((getattr(s, "text", "") or "") + "\n")
        return path
    except OSError:
        return ""


def run_one(video, opts=None, log=None, progress=None, should_stop=None):
    """处理一条视频，返回 SubtitleResult（不抛）。"""
    opts = opts or SubtitleOptions()
    res = SubtitleResult(video_path=video)
    work = _work_dir(video)
    fix_note = ""

    def _p(pct, label):
        if progress:
            progress(pct, 100, label)

    def _log(m):
        if log:
            log(m)

    try:
        # 1) 音轨 + 转写（依赖/模型异常在这里冒出来，交给下面的 except 归类）
        _p(5, Path(video).name)
        src, _is_temp = audio.resolve_audio_source(video, work)
        from ..asr import transcribe as tr
        segments, _full = tr.transcribe(src, opts.model_size, log=log,
                                        should_stop=should_stop)
        res.segments = segments
        if not segments:
            res.ok = True
            res.message = "未识别到语音（音轨可能为空）"
            return res

        out_dir = _out_dir(opts, video)
        stem = Path(video).stem

        # 1.5) 识别后语义纠错（可选）：整篇逐字稿送 DeepSeek 按语义改同音字，只换字不动时间戳。
        # 未配置 / 任何失败都保留原始识别、只往 message 追加一句说明，绝不抛、不 fail（同拆解口径）。
        if opts.asr_fix:
            _p(12, "DeepSeek 语义纠错…")
            from ..asr.fixer import apply_fix
            gloss = None
            try:
                from core.config import asr_glossary_load
                gloss = asr_glossary_load()
            except Exception:
                gloss = None
            changed, _cost, note = apply_fix(segments, glossary=gloss)
            if note:
                _log("  · " + note)
                fix_note = note

        # 1.6) 逐字稿纯文本（可选）：语音识别工具勾选“保存逐字稿”时落 .txt
        if opts.save_txt:
            res.txt_path = _write_txt(segments, os.path.join(out_dir, stem + ".txt"))

        # 2) 抽帧检测（可选）：确认已带字幕就跳过，不重复处理
        if opts.detect:
            if should_stop and should_stop():
                res.message = "已取消"
                return res
            _p(20, "检测是否已有字幕…")
            from .detect import has_burned_subtitle
            from core.config import doubao_vision_config
            verdict, reason = has_burned_subtitle(
                video, doubao_vision_config(), work, log=log,
                should_stop=should_stop)
            res.has_subtitle = verdict
            _log(f"  · 字幕检测：{reason}")
            if verdict is True:
                res.ok = True
                res.message = f"已含字幕，跳过（{reason}）"
                return res

        # 3) SRT（可选，纯文本兜底）
        if opts.gen_srt:
            res.srt_path = write_srt(segments, os.path.join(out_dir, stem + ".srt"))

        # 4) ASS（可选卖点高亮；无词表就是普通样式）
        terms = []
        if opts.highlight:
            _p(35, "分析卖点强调词…")
            from .highlight import pick_terms
            from core.config import doubao_vision_config
            terms = pick_terms(segments, doubao_vision_config(), log=log)
        res.highlight_terms = terms
        res.ass_path = write_ass(segments, opts.style, terms,
                                 os.path.join(out_dir, stem + ".ass"))

        # 5) 烧录（可选）：libass 不过就只出文件并说明，不假成功
        if opts.burn:
            if should_stop and should_stop():
                res.ok = True
                res.message = "字幕文件已出，烧录前被取消"
                return res
            _p(55, "烧录字幕…")
            from video_text_tools.ffmpeg_utils import get_ffmpeg_path
            from core.config import subtitle_config
            ffmpeg = get_ffmpeg_path()
            if not ffmpeg_has_ass(ffmpeg):
                res.ok = True
                res.message = "当前 ffmpeg 不含 libass，未烧录（字幕文件已出）"
                _log("  ⚠ " + res.message)
                return res
            suffix = subtitle_config().get("output_suffix") or "_字幕"
            out_mp4 = os.path.join(out_dir, stem + suffix + ".mp4")
            side = audio.find_sidecar(video)          # 无声 mp4 把旁路 WAV 混回
            cmd = build_subtitle_command(ffmpeg, video, res.ass_path, side, out_mp4)
            run_burn(cmd, log=log, should_stop=should_stop)
            res.burned_path = out_mp4

        res.ok = True
        res.message = "完成" + (("；" + fix_note) if fix_note else "")
        _p(100, "完成")
        return res
    except DepMissing as e:
        res.error = "DepMissing"
        res.message = str(e)
        return res
    except ModelNotReady as e:
        res.error = "ModelNotReady"
        res.message = str(e)
        return res
    except SubtitleError as e:
        res.error = "SubtitleError"
        res.message = str(e)
        return res
    except Exception as e:
        res.error = type(e).__name__
        res.message = f"{type(e).__name__}: {e}"
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_batch(videos, opts=None, log=None, progress=None, should_stop=None):
    """逐条跑，单条失败不中断整批；返回 [SubtitleResult]。"""
    opts = opts or SubtitleOptions()
    n = len(videos)
    out = []
    ok = 0
    for i, v in enumerate(videos):
        if should_stop and should_stop():
            break
        if progress:
            progress(i, n, os.path.basename(v))
        if log:
            log(f"[{i + 1}/{n}] {os.path.basename(v)}")
        r = run_one(v, opts, log=log, progress=None, should_stop=should_stop)
        out.append(r)
        ok += 1 if r.ok else 0
        if log:
            log(("  ✓ " if r.ok else "  ✗ ") + (r.message or ""))
    if progress:
        progress(n, n, "完成")
    if log:
        log(f"🎉 字幕处理结束：成功 {ok} / 共 {n}")
    return out
