"""
video_text_tools/breakdown/gallery.py —— 拆解任务的图集与封面抽取（纯逻辑，无 Qt）

拆解跑完后，按分镜时间戳逐张抽帧存成一个「图集」（外加一张封面），交给任务库持久化，
供详情页「图集 | 播放器 | 拆解文档」三屏联动显示。时间点取自 BreakdownResult：
优先 frame_analyses 的 ts（每分镜一张），无分镜时退口播 transcript 的 start。

抽帧复用现有 ffmpeg（video_text_tools.ffmpeg_utils），命令口径与 frames.py 一致
（`-ss TS -i video -frames:v 1 -q:v 2`）。单张失败只跳过、不阻断整条；视频缺失或
一张都抽不到时返回 ("", [])，绝不抛异常——图库只是锦上添花，不能拖垮拆解交付。

测试里 monkeypatch video_text_tools.ffmpeg_utils._run_subprocess 即可，无需真装 ffmpeg。
"""
import shutil
from pathlib import Path


def _times(result):
    """驱动图集的时间点列表（升序，秒）：优先分镜 ts，退口播 start。"""
    ts = [float(getattr(a, "ts", 0) or 0) for a in (result.frame_analyses or [])]
    if not ts:
        ts = [float(getattr(s, "start", 0) or 0) for s in (result.transcript or [])]
    return sorted(t for t in ts if t >= 0)


def _dedup(times, min_gap=0.5):
    """相邻间隔 < min_gap 视为同一帧，保留较早的（与 frames.py 同思路）。"""
    out = []
    for t in times:
        if not out or t - out[-1] >= min_gap:
            out.append(round(t, 3))
    return out


def _sample(times, max_n):
    """超过 max_n 张时等间隔抽样（含首尾），把图集规模压到可控。"""
    if not max_n or len(times) <= max_n:
        return times
    if max_n == 1:
        return [times[0]]
    step = (len(times) - 1) / (max_n - 1)
    return [times[round(i * step)] for i in range(max_n)]


def build_gallery(result, out_dir, log=None, max_n=40):
    """按 result 的时间戳抽一张图集 + 一张封面，落进 out_dir。

    返回 (cover_path, gallery)：cover_path 是封面 jpg 绝对路径（抽不到给 ""）；
    gallery 是 [{"idx":1基序号, "ts":秒, "path":jpg绝对路径}]，按时间升序。
    视频不存在或一张都抽不到 → ("", [])，不抛。
    """
    def _log(msg):
        if log:
            log(msg)

    video = str(getattr(result, "video_path", "") or "")
    if not video or not Path(video).exists():
        _log("⚠ 视频文件不存在，跳过图集抽取")
        return "", []

    times = _sample(_dedup(_times(result)), max_n)
    if not times:
        return "", []

    from video_text_tools.ffmpeg_utils import get_ffmpeg_path, _run_subprocess
    ffmpeg, run = get_ffmpeg_path(), _run_subprocess
    work = Path(out_dir)
    gal_dir = work / "gallery"
    gal_dir.mkdir(parents=True, exist_ok=True)

    def _grab(ts, dest):
        cmd = [ffmpeg, "-ss", str(ts), "-i", video, "-frames:v", "1",
               "-q:v", "2", "-y", str(dest)]
        try:
            r = run(cmd, timeout=60)
        except Exception:
            return False
        return getattr(r, "returncode", 1) == 0 and dest.exists()

    gallery = []
    for i, ts in enumerate(times, start=1):
        out = gal_dir / f"f{i:03d}.jpg"
        if _grab(ts, out):
            gallery.append({"idx": i, "ts": ts, "path": str(out.resolve())})

    if not gallery:
        _log("⚠ 图集未抽出任何帧（视频可能损坏）")
        return "", []

    cover = work / "cover.jpg"
    try:
        shutil.copyfile(gallery[0]["path"], cover)
        cover_path = str(cover.resolve())
    except OSError:
        cover_path = gallery[0]["path"]

    result.cover_path = cover_path
    result.gallery = gallery
    _log(f"✓ 图集抽取完成：{len(gallery)} 张 + 封面")
    return cover_path, gallery
