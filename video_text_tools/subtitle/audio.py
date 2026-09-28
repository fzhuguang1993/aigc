"""
video_text_tools/subtitle/audio.py —— 给转写挑一个可用音轨来源

录屏产物是「无声 mp4（-an）+ 同目录同基名旁路 WAV」（录屏_…_内部声音.wav /
录屏_…_麦克风.wav）。Whisper 转写需要真实音频，所以：优先找旁路 WAV；没有就看视频
自身是否含音轨（普通成品视频常有）。

⚠ 关键陷阱：asr.transcribe 把 Path(输入).with_suffix('.wav') 当临时文件、转写完
再删掉它。若把旁路 WAV（本身就以 .wav 结尾）直接喂进去，临时名会 = 源文件，ffmpeg 会
「读它同时写它」→ 内容损坏，收尾还会把录音的原始音轨删没。所以旁路 WAV 一律先复制成
work_dir 下一个**不以 .wav 结尾**的临时副本（.src）再交出去，转写自建的 .wav 落在别处。
"""
import shutil
from pathlib import Path

from .models import SubtitleError

_SIDECAR_SUFFIXES = ("_内部声音.wav", "_麦克风.wav")


def find_sidecar(video):
    """按录屏命名约定找旁路音轨 WAV；返回存在且非 0 字节的第一个路径，否则 None。"""
    p = Path(video)
    for suf in _SIDECAR_SUFFIXES:
        cand = p.with_name(p.stem + suf)
        try:
            if cand.is_file() and cand.stat().st_size > 0:
                return str(cand)
        except OSError:
            continue
    return None


def video_has_audio(video):
    """视频自身是否含音轨（借 ffprobe 的 audio_bitrate 判定，取不到按无声）。"""
    from video_text_tools.ffmpeg_utils import get_video_info
    info = get_video_info(video) or {}
    return info.get("audio_bitrate") not in (None, "", "N/A")


def resolve_audio_source(video, work_dir):
    """返回 (喂给 transcribe 的路径, is_temp)。

    - 有旁路 WAV：复制到 work_dir/subtitle_src.src（刻意非 .wav 后缀，见模块头陷阱）→
      返回 (副本, True)，副本由调用方随 work_dir 一并清理，绝不动源 WAV。
    - 无旁路但视频自带音轨：直接用视频本身（transcribe 会在视频旁建临时 .wav 再自删）。
    - 都没有：抛 SubtitleError（录屏没勾录音就没法转写）。
    """
    side = find_sidecar(video)
    if side:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        tmp = Path(work_dir) / "subtitle_src.src"
        shutil.copy2(side, tmp)
        return str(tmp), True
    if video_has_audio(video):
        return str(video), False
    raise SubtitleError("无可用音轨：录屏时未勾选录音，无法转写字幕")
