# video_text_tools/file_utils.py
"""文件扫描与文件名解析"""
import os
import re
import sys
from pathlib import Path

from .config import VIDEO_EXTENSIONS


def get_base_dir():
    """获取程序运行的基础目录（兼容 PyInstaller 打包后）"""
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def is_video_file(file_path: str) -> bool:
    """判断是否为支持的视频文件"""
    if not os.path.isfile(file_path):
        return False
    return Path(file_path).suffix.lower() in VIDEO_EXTENSIONS


def get_sorted_video_files(folder_path: str) -> list:
    """获取文件夹内排序后的视频文件列表"""
    if not os.path.exists(folder_path):
        return []

    video_files = []
    for f in sorted(os.listdir(folder_path)):
        f_path = os.path.join(folder_path, f)
        if is_video_file(f_path):
            video_files.append(f_path)
    return video_files


def get_video_files(folder_path: str) -> list:
    """获取文件夹内视频文件列表（不排序）"""
    video_ext = tuple(VIDEO_EXTENSIONS)
    res = []
    if not os.path.isdir(folder_path):
        return res
    for name in os.listdir(folder_path):
        full = os.path.join(folder_path, name)
        if os.path.isfile(full) and name.lower().endswith(video_ext):
            res.append(full)
    return res


def extract_trace_code(filename: str):
    """
    从文件名中提取溯源码
    文件名格式: 溯源码_日期_剪辑首拼_运营首拼.MP4
    例如: 0FSd_20260623_LSM_LSM.MP4 -> 0FSd
    """
    pattern = r'^([A-Za-z0-9]{4})_'
    match = re.match(pattern, filename)
    if match:
        return match.group(1)
    return None


def has_trace_code(filename: str) -> bool:
    """检查文件名是否包含溯源码"""
    return extract_trace_code(filename) is not None
