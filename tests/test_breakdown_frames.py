"""
tests/test_breakdown_frames.py —— 抽帧逻辑（mock ffmpeg 子进程，无需真装 ffmpeg）

校验：每 interval 秒栅格 + 场景切换时刻合并去重；逐张抽帧产 FrameShot；
抽帧命令非零返回 → FrameError。
"""
import video_text_tools.ffmpeg_utils as fu
from video_text_tools.breakdown import frames
from video_text_tools.breakdown.models import FrameError


class R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _fake_factory(tmp_path, duration="12.0", scene_pts="3.2\n7.0"):
    """按命令内容分派：ffprobe 报时长、showinfo 报场景 pts_time、抽帧写 jpg"""
    def fake_run(cmd, **kw):
        joined = " ".join(map(str, cmd))
        if "format=duration" in joined:
            return R(stdout=duration)
        if "showinfo" in joined:
            err = "".join(f"n: 0 pts: 1 pts_time:{p}\n" for p in scene_pts.split())
            return R(stderr=err)
        if "-frames:v" in joined:
            out = cmd[-1]
            open(out, "wb").write(b"\xff\xd8fake")     # 建出 jpg 让 exists() 通过
            return R(returncode=0)
        return R(returncode=0)
    return fake_run


def _patch(monkeypatch, tmp_path, **kw):
    monkeypatch.setattr(fu, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(fu, "get_ffprobe_path", lambda: "ffprobe")
    monkeypatch.setattr(fu, "_run_subprocess", _fake_factory(tmp_path, **kw))


def test_extract_frames_grid_and_scene_merged(tmp_path, monkeypatch):
    _patch(monkeypatch, tmp_path, duration="12.0", scene_pts="3.2\n7.0")
    shots = frames.extract_frames("v.mp4", str(tmp_path / "f"), interval=3,
                                  scene_thresh=0.3)
    ts = [s.ts for s in shots]
    # 栅格 0,3,6,9,12 + 场景 3.2,7.0，min_gap=0.9 去掉 3.2（距 3 仅 0.2）
    assert ts == [0.0, 3.0, 6.0, 7.0, 9.0, 12.0]
    assert any(s.is_scene for s in shots)              # 7.0 是场景帧
    assert all(s.path.endswith(".jpg") for s in shots)
    assert [s.idx for s in shots] == list(range(1, len(shots) + 1))


def test_extract_frames_dedup_scene_only(tmp_path, monkeypatch):
    # 时长取不到（0）：只剩场景切换帧
    _patch(monkeypatch, tmp_path, duration="0", scene_pts="5.0\n5.2")
    shots = frames.extract_frames("v.mp4", str(tmp_path / "f"), interval=3)
    assert [s.ts for s in shots] == [5.0]              # 5.2 距 5.0 太近被去重


def test_extract_frames_error(tmp_path, monkeypatch):
    monkeypatch.setattr(fu, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(fu, "get_ffprobe_path", lambda: "ffprobe")

    def boom(cmd, **kw):
        joined = " ".join(map(str, cmd))
        if "format=duration" in joined:
            return R(stdout="10.0")
        if "showinfo" in joined:
            return R(stderr="")
        return R(returncode=1, stderr="boom bad codec")

    monkeypatch.setattr(fu, "_run_subprocess", boom)
    try:
        frames.extract_frames("v.mp4", str(tmp_path / "f"), interval=2)
        assert False, "应抛 FrameError"
    except FrameError as e:
        assert "抽帧失败" in str(e)


def test_dedup_helper():
    assert frames._dedup([0, 3, 3.2, 6, 12], 0.9) == [0, 3, 6, 12]
    assert frames._dedup([-1, 0, 1], 0.5) == [0, 1]
