# video_text_tools/watermark.py
"""批量水印 / 格式化处理（从 WatermarkWorker 提取，去除 Qt 依赖，改为回调式）"""
import os
import subprocess
from pathlib import Path
from typing import List, Optional

from .ffmpeg_utils import build_watermark_command, build_format_command


def process_videos(
    video_paths: List[str],
    watermark_path: str = "",
    params: Optional[dict] = None,
    output_dir: str = "",
    progress_callback=None,
    log_callback=None,
    should_stop=None,
) -> dict:
    """批量处理视频（水印或仅格式化）

    :param video_paths: 视频路径列表
    :param watermark_path: 水印图片路径；为空或文件不存在时走"仅格式化"模式
    :param params: 处理参数字典，支持以下键（均有默认值）：
        mode(1右下角/2碰撞反弹/3右下+碰撞), right_margin, bottom_y,
        speed_x, speed_y, top_margin, bottom_margin,
        video_bitrate, audio_bitrate, fps, resolution
    :param output_dir: 输出目录，为空时默认 桌面/movie_space/输出
    :param progress_callback: callable(current, total, name)
    :param log_callback: callable(message)
    :param should_stop: callable() -> bool，返回 True 时中断处理
    :return: {"success": n, "failed": n, "skipped": n, "output_dir": dir,
              "outputs": [输出文件路径...]}
    """
    params = params or {}
    log = log_callback or (lambda m: None)
    progress = progress_callback or (lambda c, t, n: None)
    stopped = should_stop or (lambda: False)

    total = len(video_paths)
    success_count = 0
    fail_count = 0
    skip_count = 0
    outputs: List[str] = []

    has_watermark = bool(watermark_path) and os.path.exists(watermark_path)

    # 输出目录，不存在则自动创建
    out_dir = output_dir or os.path.join(str(Path.home()), "Desktop", "movie_space", "输出")
    os.makedirs(out_dir, exist_ok=True)

    mode = params.get('mode', 1)
    right_margin = params.get('right_margin', 148)
    bottom_y = params.get('bottom_y', 1602)
    speed_x = params.get('speed_x', 0.05)
    speed_y = params.get('speed_y', 0.05)
    top_margin = params.get('top_margin', 50)
    bottom_margin = params.get('bottom_margin', 50)

    video_bitrate = params.get('video_bitrate', '4M')
    audio_bitrate = params.get('audio_bitrate', '44k')
    fps = params.get('fps', '30')
    resolution = params.get('resolution', '1080x1920')

    if has_watermark:
        log(f"🖼️ 水印模式 | 输出目录: {out_dir}")
    else:
        log(f"📦 仅格式化模式（无水印）| 输出目录: {out_dir}")

    counter = 1

    for idx, video_path in enumerate(video_paths, 1):
        if stopped():
            log("⚠️ 用户中断")
            break

        video_name = os.path.basename(video_path)
        progress(idx, total, video_name)
        log(f"\n[{idx}/{total}] 📹 处理: {video_name}")

        if not os.path.exists(video_path):
            log("  ⚠️ 源文件不存在，跳过")
            skip_count += 1
            continue

        ext = Path(video_path).suffix or ".mp4"

        if has_watermark:
            mode_names = {1: '右下角', 2: '碰撞反弹', 3: '右下+碰撞'}
            mode_name = mode_names.get(mode, '水印')
            output_name = f"{counter:03d}_水印_{mode_name}{ext}"
        else:
            output_name = f"{counter:03d}_格式化{ext}"

        output_path = os.path.join(out_dir, output_name)

        if os.path.exists(output_path):
            log(f"  ⏭️ 跳过: {output_name} 已存在")
            skip_count += 1
            counter += 1
            continue

        if has_watermark:
            cmd = build_watermark_command(
                video_path, output_path, watermark_path,
                right_margin, bottom_y,
                speed_x, speed_y,
                top_margin, bottom_margin,
                mode
            )
        else:
            cmd = build_format_command(
                video_path, output_path,
                video_bitrate, audio_bitrate,
                fps, resolution
            )

        try:
            log("  🎬 执行 FFmpeg...")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600
            )

            if result.returncode == 0:
                log(f"  ✅ 成功: {output_name}")
                outputs.append(output_path)
                success_count += 1
            else:
                error_msg = result.stderr[:300] if result.stderr else "未知错误"
                log(f"  ❌ 失败: {error_msg}")
                fail_count += 1

        except subprocess.TimeoutExpired:
            log("  ❌ 超时: 处理超过1小时")
            fail_count += 1
        except Exception as e:
            log(f"  ❌ 异常: {str(e)}")
            fail_count += 1

        counter += 1

    msg = f"处理完成！✅ 成功: {success_count}"
    if skip_count:
        msg += f"，⏭️ 跳过: {skip_count}"
    if fail_count:
        msg += f"，❌ 失败: {fail_count}"
    msg += f"\n📂 输出目录: {out_dir}"
    log(msg)

    return {
        "success": success_count,
        "failed": fail_count,
        "skipped": skip_count,
        "output_dir": out_dir,
        "outputs": outputs,
    }
