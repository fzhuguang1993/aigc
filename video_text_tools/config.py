# video_text_tools/config.py
"""工具包全部配置常量（移植到其他项目时只需改这里，或运行时覆盖）"""
import os
import sys

# ================================================================
# 视频文件支持格式
# ================================================================
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.flv', '.wmv'}

# ================================================================
# FFmpeg 配置
# ================================================================
FFMPEG_DEFAULT_VIDEO_BITRATE = "4M"
FFMPEG_DEFAULT_AUDIO_BITRATE = "44k"
FFMPEG_DEFAULT_FPS = 30
FFMPEG_WATERMARK_SCALE = 200

# ffmpeg / ffprobe 附加搜索目录（按优先级），找不到时回退到 PATH
_SEARCH = [
    os.path.dirname(os.path.abspath(__file__)),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bin'),
]
# PyInstaller onefile 解包目录（打包时 --add-data 内置的 assets\ffmpeg.exe）
if getattr(sys, 'frozen', False):
    _SEARCH.append(os.path.join(getattr(sys, '_MEIPASS', ''), 'assets'))
# 开发模式：项目根 assets\（维护人自备 ffmpeg.exe 放这里，不入库）
_SEARCH.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'assets'))
FFMPEG_SEARCH_DIRS = _SEARCH

# ================================================================
# SMB 共享配置（不用 SMB 上传可忽略）
# ================================================================
SMB_CONFIG = {
    "host": "",           # 例如 "192.168.6.148"
    "share_name": "",     # 例如 "运营素材"
    "username": "",
    "password": "",
    "remote_path": "",    # 例如 "溯源视频"
    "domain": "",
    "port": 445,
}

# ================================================================
# 溯源配置
# ================================================================
TRACE_CONFIG = {
    "date_format": "%Y%m%d",
    "name_format": "{trace_code}_{date}_{editor_initials}_{operator_initials}.MP4",
}

# ================================================================
# 数据库配置（仅 trace_utils 溯源码池功能使用）
# ================================================================
DB_CFG = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "",
    "database": "yunying_test",
    "charset": "utf8mb4",
}
