"""
tests/test_subtitle_audio.py —— 音轨来源解析（钉住"绝不覆写源旁路 WAV"的陷阱）

breakdown.transcribe 会把 Path(输入).with_suffix('.wav') 当临时文件、用完删。所以
resolve_audio_source 对旁路 WAV 必须先复制成一个**非 .wav 后缀**的临时副本再交出去，
并且绝不删除/覆盖录音原始文件。
"""
import pytest

from video_text_tools.subtitle import audio
from video_text_tools.subtitle.models import SubtitleError


def _mk(tmp_path, name, content="data"):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_find_sidecar_prefers_internal_voice(tmp_path):
    _mk(tmp_path, "录屏_1.mp4")
    _mk(tmp_path, "录屏_1_麦克风.wav", "mic")
    _mk(tmp_path, "录屏_1_内部声音.wav", "sys")
    got = audio.find_sidecar(str(tmp_path / "录屏_1.mp4"))
    assert got.endswith("录屏_1_内部声音.wav")           # 内部声音优先


def test_find_sidecar_skips_empty(tmp_path):
    _mk(tmp_path, "v.mp4")
    _mk(tmp_path, "v_内部声音.wav", "")                  # 0 字节 → 跳过
    _mk(tmp_path, "v_麦克风.wav", "mic")
    assert audio.find_sidecar(str(tmp_path / "v.mp4")).endswith("v_麦克风.wav")


def test_resolve_copies_sidecar_and_keeps_source(tmp_path, monkeypatch):
    video = _mk(tmp_path, "v.mp4")
    side = _mk(tmp_path, "v_内部声音.wav", "recording")
    work = tmp_path / "work"
    src, is_temp = audio.resolve_audio_source(str(video), str(work))

    assert is_temp is True
    assert src.endswith("subtitle_src.src")             # 刻意非 .wav，规避覆写陷阱
    assert side.is_file() and side.read_text() == "recording"   # 源 WAV 原封不动
    assert side.stat().st_size > 0


def test_resolve_falls_back_to_video_audio(tmp_path, monkeypatch):
    video = _mk(tmp_path, "v.mp4")                       # 无旁路 WAV
    monkeypatch.setattr(audio, "video_has_audio", lambda v: True)
    src, is_temp = audio.resolve_audio_source(str(video), str(tmp_path / "w"))
    assert src == str(video) and is_temp is False


def test_resolve_no_audio_raises(tmp_path, monkeypatch):
    video = _mk(tmp_path, "v.mp4")
    monkeypatch.setattr(audio, "video_has_audio", lambda v: False)
    with pytest.raises(SubtitleError):
        audio.resolve_audio_source(str(video), str(tmp_path / "w"))
