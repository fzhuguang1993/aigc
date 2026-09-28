r"""
video_text_tools/subtitle/burn.py —— 字幕烧录命令构建 + libass 能力探测

用 ffmpeg 的 `ass` 滤镜把 .ass（含卖点上色）烤进画面。两个现实约束必须处理好：
  1) libass 依赖：ass 滤镜要 ffmpeg 编译时带 --enable-libass。缺了没法烧，但字幕文件
     照样能出——探测不过时上层降级为"只出文件不烧录"，绝不静默假成功。
  2) Windows 路径转义：滤镜串里盘符冒号 `:` 会被当选项分隔、反斜杠会被当转义。统一
     先把 \ 换 /、再把 : 转义成 \:，命令以 list 传参不过 shell，无需外层引号。
音频：录屏 mp4 是无声的（-an），烧录时把旁路 WAV 作第二路输入接回（-map 1:a + AAC）；
若视频本就自带音轨则不动音频（-c:a copy），避免二次压音。
"""
import re
import subprocess

from .models import SubtitleError

_HAS_ASS_RE = re.compile(r"(?:^|\s)ass(?:\s|$)", re.M)
_has_ass_cache = {}


def ffmpeg_has_ass(ffmpeg):
    """当前 ffmpeg 是否含 ass 滤镜（libass）。按可执行文件路径缓存探测结果。"""
    if ffmpeg in _has_ass_cache:
        return _has_ass_cache[ffmpeg]
    ok = False
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-filters"],
                           capture_output=True, text=True, timeout=20,
                           encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ok = bool(_HAS_ASS_RE.search((r.stdout or "") + (r.stderr or "")))
    except Exception:
        ok = False
    _has_ass_cache[ffmpeg] = ok
    return ok


def _ass_filter(ass_path):
    """把 ASS 路径转成 -vf 的 `ass=...` 取值（见模块头的 Windows 转义规则）。"""
    p = str(ass_path).replace("\\", "/").replace(":", "\\:")
    return f"ass={p}"


def build_subtitle_command(ffmpeg, video, ass_path, audio_wav, out):
    """拼烧录命令。audio_wav 非空 = 录屏无声 mp4，需把旁路 WAV 混回；
    audio_wav 为空 = 视频自带音轨，只重编视频、音频 copy。"""
    cmd = [ffmpeg, "-hide_banner", "-y"]
    if audio_wav:
        cmd += ["-i", video, "-i", audio_wav,
                "-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-i", video, "-map", "0:v:0", "-map", "0:a?"]
    cmd += ["-vf", _ass_filter(ass_path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "192k"] if audio_wav else ["-c:a", "copy"]
    cmd += ["-movflags", "+faststart", out]
    return cmd


def run_burn(cmd, log=None, should_stop=None):
    """跑烧录子进程（重编码，较长）。非 0 退出抛 SubtitleError 带 stderr 尾巴。"""
    from video_text_tools.ffmpeg_utils import _run_subprocess
    if log:
        log("  🎬 烧录字幕到画面…（重编码，耗时较长）")
    r = _run_subprocess(cmd, timeout=3600)
    if getattr(r, "returncode", 1) != 0:
        err = ((getattr(r, "stderr", "") or "")[-300:]).strip()
        raise SubtitleError(f"烧录失败：{err or 'ffmpeg 返回非零'}")
    return True
