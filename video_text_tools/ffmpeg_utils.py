# video_text_tools/ffmpeg_utils.py
"""FFmpeg / FFprobe 相关功能：视频信息、缩略图、水印与格式化命令构建"""
import os
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import (
    FFMPEG_DEFAULT_VIDEO_BITRATE, FFMPEG_DEFAULT_AUDIO_BITRATE,
    FFMPEG_DEFAULT_FPS, FFMPEG_WATERMARK_SCALE, FFMPEG_SEARCH_DIRS
)


def _run_subprocess(cmd, **kwargs):
    """跨平台 subprocess.run，Windows 下隐藏控制台窗口。

    ffmpeg/ffprobe 写到管道的是 UTF-8 字节；不显式指定 encoding 时 Windows 会拿
    locale（GBK）解码，中文文件名/报错里的非 GBK 字节会触发 UnicodeDecodeError，
    把读取线程炸掉且 stdout 变 None。统一按 UTF-8 + replace 解码。
    """
    kwargs.setdefault('capture_output', True)
    kwargs.setdefault('text', True)
    kwargs.setdefault('encoding', 'utf-8')
    kwargs.setdefault('errors', 'replace')
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def _find_exe(names: list) -> str:
    """在附加搜索目录与 PATH 中查找可执行文件"""
    for base_dir in FFMPEG_SEARCH_DIRS:
        for name in names:
            path = os.path.join(base_dir, name)
            if os.path.exists(path):
                return path
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return names[0]  # 回退到裸命令名，交给系统 PATH 解析


def get_ffmpeg_path() -> str:
    """获取 FFmpeg 路径（优先使用同目录下的 ffmpeg.exe / ffmpeg）"""
    return _find_exe(['ffmpeg.exe', 'ffmpeg'])


def get_ffprobe_path() -> str:
    """获取 ffprobe 路径"""
    return _find_exe(['ffprobe.exe', 'ffprobe'])


def get_video_info(file_path: str) -> Optional[dict]:
    """使用 ffprobe 获取视频信息（宽高/帧率/编码/码率/时长/横竖屏/音频码率）"""
    try:
        ffprobe = get_ffprobe_path()
        cmd = [
            ffprobe, '-v', 'quiet',
            '-print_format', 'json',
            '-show_format',
            '-show_streams',
            file_path
        ]
        result = _run_subprocess(cmd, timeout=10)
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        info = {
            'width': 'N/A', 'height': 'N/A', 'fps': 'N/A',
            'codec': 'N/A', 'bitrate': 'N/A', 'duration': 'N/A',
            'orientation': 'N/A', 'audio_bitrate': 'N/A'
        }

        # 解析视频流信息
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video':
                info['width'] = stream.get('width', 'N/A')
                info['height'] = stream.get('height', 'N/A')
                info['codec'] = stream.get('codec_name', 'N/A')

                # 解析帧率
                fps = stream.get('r_frame_rate', '0/0')
                if '/' in str(fps):
                    try:
                        num, den = fps.split('/')
                        info['fps'] = f"{int(num) / int(den):.2f}" if int(den) > 0 else 'N/A'
                    except Exception:
                        info['fps'] = 'N/A'

                # 判断横竖屏
                if info['width'] != 'N/A' and info['height'] != 'N/A':
                    info['orientation'] = '竖屏' if info['width'] < info['height'] else '横屏'

        # 解析格式信息（码率、时长）
        fmt = data.get('format', {})
        bitrate = fmt.get('bit_rate', '0')
        if bitrate not in ('0', 'N/A'):
            info['bitrate'] = f"{int(bitrate) / 1000:.0f} kbps"

        duration = fmt.get('duration', '0')
        if duration not in ('0', 'N/A'):
            try:
                sec = float(duration)
                info['duration'] = f"{int(sec // 60)}:{int(sec % 60):02d}"
            except Exception:
                info['duration'] = 'N/A'

        # 解析音频码率
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'audio':
                abr = stream.get('bit_rate', '0')
                if abr not in ('0', 'N/A'):
                    info['audio_bitrate'] = f"{int(abr) / 1000:.0f} kbps"
                    break

        return info
    except Exception:
        return None


def get_video_thumbnail(file_path: str, output_path: str, time_pos: float = 1.0) -> bool:
    """从视频中提取缩略图"""
    try:
        ffmpeg = get_ffmpeg_path()
        cmd = [
            ffmpeg, '-i', file_path, '-ss', str(time_pos),
            '-vframes', '1', '-vf', f'scale={FFMPEG_WATERMARK_SCALE}:-1',
            '-y', output_path
        ]
        result = _run_subprocess(cmd, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def build_format_command(
    input_path: str, output_path: str,
    video_bitrate: str = "4M", audio_bitrate: str = "44k",
    fps: str = "30", resolution: str = "1080x1920"
) -> list:
    """构建仅格式化（无水印）的FFmpeg命令"""
    ffmpeg_path = get_ffmpeg_path()

    res_part = resolution.split(" ")[0]
    parts = res_part.split("x")
    w, h = int(parts[0]), int(parts[1])

    vf = (
        f"scale={w}:{h}:flags=lanczos,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    )

    return [
        ffmpeg_path,
        '-i', input_path,
        '-vf', vf,
        '-c:v', 'libx264',
        '-b:v', video_bitrate,
        '-r', str(fps),
        '-c:a', 'aac',
        '-b:a', audio_bitrate,
        '-y', output_path
    ]


def build_watermark_command(
    input_path: str, output_path: str, watermark_path: str,
    right_margin: int, bottom_y: int,
    speed_x: float, speed_y: float,
    top_margin: int, bottom_margin: int,
    position_mode: int
) -> list:
    """构建水印处理的FFmpeg命令

    position_mode: 1=右下角固定 2=碰撞反弹 3=右下角+碰撞反弹
    """
    ffmpeg_path = get_ffmpeg_path()
    wm_scale = FFMPEG_WATERMARK_SCALE

    if position_mode == 1:  # 右下角固定
        filter_complex = (
            f'[1:v]scale={wm_scale}:-1,format=rgba[wm];'
            f'[0:v]scale=1080:1920:flags=lanczos,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,format=rgb24[bg];'
            f'[bg][wm]overlay=W-{wm_scale}-{right_margin}:{bottom_y}:alpha=1'
        )
    elif position_mode == 2:  # 碰撞反弹
        scroll_range_y = f"(H-{wm_scale}-{top_margin}-{bottom_margin})"
        scroll_range_x = f"(W-{wm_scale})"
        filter_complex = (
            f'[1:v]scale={wm_scale}:-1,format=rgba[wm];'
            f'[0:v]scale=1080:1920:flags=lanczos,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,format=rgb24[bg];'
            f'[bg][wm]overlay=x={scroll_range_x}*abs(sin(t*{speed_x})):y={top_margin}+{scroll_range_y}*abs(cos(t*{speed_y})):alpha=1'
        )
    else:  # 右下角+碰撞反弹
        scroll_range_y = f"(H-{wm_scale}-{top_margin}-{bottom_margin})"
        scroll_range_x = f"(W-{wm_scale})"
        filter_complex = (
            f'[1:v]scale={wm_scale}:-1,format=rgba[wm1];'
            f'[1:v]scale={wm_scale}:-1,format=rgba[wm2];'
            f'[0:v]scale=1080:1920:flags=lanczos,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,format=rgb24[bg];'
            f'[bg][wm1]overlay=W-{wm_scale}-{right_margin}:{bottom_y}:alpha=1[bg1];'
            f'[bg1][wm2]overlay=x={scroll_range_x}*abs(sin(t*{speed_x})):y={top_margin}+{scroll_range_y}*abs(cos(t*{speed_y})):alpha=1'
        )

    # 正确参数：-i 原视频 -i 水印图，最后才是输出文件
    return [
        ffmpeg_path,
        '-i', input_path,
        '-i', watermark_path,
        '-filter_complex', filter_complex,
        '-c:v', 'libx264',
        '-b:v', FFMPEG_DEFAULT_VIDEO_BITRATE,
        '-r', str(FFMPEG_DEFAULT_FPS),
        '-c:a', 'aac',
        '-b:a', FFMPEG_DEFAULT_AUDIO_BITRATE,
        '-y', output_path
    ]


def generate_output_filename(input_path: str, position_mode: int) -> str:
    """生成带水印的输出文件名"""
    mode_names = {1: '右下角', 2: '碰撞反弹', 3: '右下角+碰撞反弹'}
    base_name = Path(input_path).stem
    today = datetime.now().strftime("%Y-%m-%d")
    mode_name = mode_names.get(position_mode, '水印')
    return f"{today}_{base_name}_水印_{mode_name}.mp4"


# ----------------------------------------------------------------------
# 片段切割 / 拼接（爆款拆解→素材库→混剪共用）
#   build_* 只拼命令不执行（便于单测断言）；cut_segment/concat_clips 负责落盘与回因。
# ----------------------------------------------------------------------
def build_cut_command(src, start, end, dst, reencode=False, ffmpeg=None):
    """拼一条切割命令：默认 -ss/-to 输入侧定位 + -c copy（快、无损、按关键帧）。

    reencode=True 走精确切（解码重编，不受关键帧间距限制，但慢）。"""
    ffmpeg = ffmpeg or get_ffmpeg_path()
    cmd = [ffmpeg, '-y', '-ss', f'{float(start):.3f}', '-to', f'{float(end):.3f}',
           '-i', str(src)]
    if reencode:
        cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-c:a', 'aac']
    else:
        cmd += ['-c', 'copy']
    cmd.append(str(dst))
    return cmd


def cut_segment(src, start, end, dst, reencode=False, timeout=600):
    """从 src 切 [start,end] 秒到 dst。返回 (ok, 原因)；参数非法/执行失败都给可读原因。"""
    try:
        start, end = float(start), float(end)
    except (TypeError, ValueError):
        return False, '起止时间非法'
    if end <= start or start < 0:
        return False, f'区间非法（{start}~{end}）'
    if not Path(src).exists():
        return False, f'源视频不存在：{src}'
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    cmd = build_cut_command(src, start, end, dst, reencode=reencode)
    try:
        result = _run_subprocess(cmd, timeout=timeout)
    except Exception as e:
        return False, f'ffmpeg 执行异常：{e}'
    if result.returncode != 0 or not Path(dst).exists():
        err = (result.stderr or result.stdout or '').strip()[-200:]
        return False, f'切割失败：{err or result.returncode}'
    return True, ''


def write_concat_list(clips, list_path):
    """把片段列表写成 concat 清单文件（单引号转义），返回清单路径。"""
    lines = []
    for p in clips:
        safe = Path(p).as_posix().replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    Path(list_path).parent.mkdir(parents=True, exist_ok=True)
    Path(list_path).write_text('\n'.join(lines), encoding='utf-8')
    return list_path


def build_concat_command(list_path, dst, reencode=False, ffmpeg=None):
    """用 concat 封装器拼拼接命令（清单走 -f concat -safe 0）。reencode=True 时重编统一规格。"""
    ffmpeg = ffmpeg or get_ffmpeg_path()
    cmd = [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', str(list_path)]
    if reencode:
        cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-c:a', 'aac']
    else:
        cmd += ['-c', 'copy']
    cmd.append(str(dst))
    return cmd


def concat_clips(clips, dst, reencode=False, timeout=1800):
    """把多个片段按序拼成 dst（临时清单跑完即删）。返回 (ok, 原因)。"""
    clips = [c for c in (clips or []) if Path(c).exists()]
    if not clips:
        return False, '无可拼接片段'
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    fd, list_path = tempfile.mkstemp(suffix='.txt', prefix='concat_')
    os.close(fd)
    try:
        write_concat_list(clips, list_path)
        cmd = build_concat_command(list_path, dst, reencode=reencode)
        try:
            result = _run_subprocess(cmd, timeout=timeout)
        except Exception as e:
            return False, f'ffmpeg 执行异常：{e}'
        if result.returncode != 0 or not Path(dst).exists():
            err = (result.stderr or result.stdout or '').strip()[-200:]
            return False, f'拼接失败：{err or result.returncode}'
        return True, ''
    finally:
        Path(list_path).unlink(missing_ok=True)
