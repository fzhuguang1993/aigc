"""
tests/test_breakdown_gallery.py —— 图集/封面抽取（mock ffmpeg，无需真装）

校验 build_gallery：按分镜时间戳逐张抽帧成图集、首帧复制成封面、回填 result；
视频缺失或一张抽不到 → ("", []) 不抛；单帧失败只跳过；超规模等间隔抽样。
"""
from pathlib import Path

import video_text_tools.ffmpeg_utils as fu
from video_text_tools.breakdown import gallery
from video_text_tools.breakdown.models import BreakdownResult, FrameAnalysis


class R:
    def __init__(self, returncode=0):
        self.returncode = returncode


def _patch(monkeypatch, fail_ts=()):
    """-frames:v 命令写一张占位 jpg（命中 fail_ts 时间点则返回非零模拟失败）"""
    def fake_run(cmd, **kw):
        joined = " ".join(map(str, cmd))
        if "-frames:v" in joined:
            for t in fail_ts:
                if f"-ss {t}" in joined:
                    return R(returncode=1)
            out = cmd[-1]
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"\xff\xd8fake")
            return R(0)
        return R(0)
    monkeypatch.setattr(fu, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(fu, "_run_subprocess", fake_run)


def _result(tmp_path, ts_list):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    fa = [FrameAnalysis(idx=i + 1, ts=t) for i, t in enumerate(ts_list)]
    return BreakdownResult(title="t", video_path=str(video), frame_analyses=fa)


def test_build_gallery_by_shot_timestamps(tmp_path, monkeypatch):
    _patch(monkeypatch)
    r = _result(tmp_path, [0.0, 3.0, 6.0])
    cover, gal = gallery.build_gallery(r, str(tmp_path / "lib"))
    assert cover and Path(cover).exists() and Path(cover).name == "cover.jpg"
    assert [g["ts"] for g in gal] == [0.0, 3.0, 6.0]
    assert all(Path(g["path"]).exists() for g in gal)
    assert [g["idx"] for g in gal] == [1, 2, 3]
    # 回填到 result
    assert r.cover_path == cover and len(r.gallery) == 3


def test_video_missing_returns_empty(tmp_path, monkeypatch):
    _patch(monkeypatch)
    r = BreakdownResult(title="t", video_path=str(tmp_path / "nope.mp4"),
                        frame_analyses=[FrameAnalysis(idx=1, ts=0.0)])
    cover, gal = gallery.build_gallery(r, str(tmp_path / "lib"))
    assert cover == "" and gal == []


def test_single_frame_failure_is_skipped(tmp_path, monkeypatch):
    _patch(monkeypatch, fail_ts=("3.0",))
    r = _result(tmp_path, [0.0, 3.0, 6.0])
    cover, gal = gallery.build_gallery(r, str(tmp_path / "lib"))
    assert [g["ts"] for g in gal] == [0.0, 6.0]       # 3.0 抽失败被跳过
    assert cover and Path(cover).exists()


def test_sample_caps_gallery_size(tmp_path, monkeypatch):
    _patch(monkeypatch)
    r = _result(tmp_path, [float(i) for i in range(10)])   # 10 个时间点
    _, gal = gallery.build_gallery(r, str(tmp_path / "lib"), max_n=3)
    assert len(gal) == 3
    assert gal[0]["ts"] == 0.0 and gal[-1]["ts"] == 9.0    # 含首尾


def test_no_times_returns_empty(tmp_path, monkeypatch):
    _patch(monkeypatch)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    r = BreakdownResult(title="t", video_path=str(video))   # 无分镜无口播
    cover, gal = gallery.build_gallery(r, str(tmp_path / "lib"))
    assert cover == "" and gal == []
