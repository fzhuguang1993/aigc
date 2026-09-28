"""
video_text_tools/breakdown/frames.py —— 每 N 秒抽帧 + 场景切换检测

用现有 ffmpeg（video_text_tools.ffmpeg_utils）：
  1. ffprobe 取时长，生成固定间隔时间栅格（0, interval, 2*interval …）；
  2. `select='gt(scene,t)',showinfo` 跑一遍解析 stderr 的 pts_time，得场景切换时刻；
  3. 两批时间点合并、按最小间隔去重；
  4. 每个时刻 `-ss TS -i video -frames:v 1` 抽一张 JPEG。

抽帧失败抛 FrameError，交 pipeline 标「抽帧」阶段。全程只依赖 _run_subprocess，
测试里 mock 它即可，无需真装 ffmpeg。
"""
import re
from pathlib import Path

from .models import FrameShot, FrameError

_PTS_RE = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")


def _duration_seconds(video, ffprobe, run):
    """ffprobe 取整数秒时长；取不到返回 0（调用方据此只按场景帧走）"""
    cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", video]
    r = run(cmd, timeout=15)
    try:
        return float((r.stdout or "").strip())
    except (ValueError, AttributeError):
        return 0.0


def _detect_scenes(video, scene_thresh, ffmpeg, run):
    """跑一遍场景检测，返回切换时刻列表（秒）。检测不到就返回空，不视为错误。"""
    cmd = [ffmpeg, "-i", video, "-vf",
           f"select='gt(scene,{scene_thresh})',showinfo",
           "-an", "-f", "null", "-"]
    try:
        r = run(cmd, timeout=300)
    except Exception:
        return []
    blob = (getattr(r, "stderr", "") or "") + (getattr(r, "stdout", "") or "")
    return [round(float(m), 3) for m in _PTS_RE.findall(blob)]


def _dedup(times, min_gap):
    """时间戳排序去重：相邻差 < min_gap 视为同一帧，保留较早的"""
    out = []
    for t in sorted(round(x, 3) for x in times if x >= 0):
        if not out or t - out[-1] >= min_gap:
            out.append(t)
    return out


def extract_frames(video, work_dir, interval=3, scene_thresh=0.3, log=None):
    """抽关键帧，返回 [FrameShot]（idx 从 1 起，ts 秒，path 落盘 jpg，is_scene 是否场景帧）。

    interval：固定抽帧间隔（秒）；scene_thresh：场景切换灵敏度（0-1，越小越敏感）。
    合并「每 interval 秒」栅格与场景切换时刻，去重后逐张抽。一帧未出 → FrameError。
    """
    from video_text_tools.ffmpeg_utils import (
        get_ffmpeg_path, get_ffprobe_path, _run_subprocess)

    def _log(msg):
        if log:
            log(msg)

    ffmpeg, ffprobe, run = get_ffmpeg_path(), get_ffprobe_path(), _run_subprocess
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    dur = _duration_seconds(video, ffprobe, run)
    grid = [i * interval for i in range(int(dur // interval) + 1)] if dur > 0 else []
    scenes = _detect_scenes(video, scene_thresh, ffmpeg, run)
    min_gap = max(0.5, interval * 0.3)
    times = _dedup(grid + scenes, min_gap)
    if not times:
        raise FrameError("未能确定抽帧时间点（视频可能损坏或时长为 0）")

    scene_set = {round(s, 1) for s in scenes}
    shots = []
    for i, ts in enumerate(times, start=1):
        out = work / f"frame_{i:03d}.jpg"
        cmd = [ffmpeg, "-ss", str(ts), "-i", video, "-frames:v", "1",
               "-q:v", "2", "-y", str(out)]
        r = run(cmd, timeout=60)
        if getattr(r, "returncode", 1) != 0 or not out.exists():
            err = ((getattr(r, "stderr", "") or "")[-200:]).strip()
            raise FrameError(f"抽帧失败（{ts}s）：{err or 'ffmpeg 返回非零'}")
        shots.append(FrameShot(idx=i, ts=ts, path=str(out),
                               is_scene=round(ts, 1) in scene_set))
    if not shots:
        raise FrameError("没抽出任何帧")
    _log(f"  ✓ 抽帧 {len(shots)} 张（含场景切换 {sum(1 for s in shots if s.is_scene)} 张）")
    return shots
