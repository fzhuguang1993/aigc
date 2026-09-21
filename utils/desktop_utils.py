"""
utils/desktop_utils.py —— 跨平台打开文件 / 定位到文件夹
Windows 用 os.startfile / explorer /select,；macOS 用 open / open -R；Linux 用 xdg-open。
"""
import os
import subprocess
import sys
from pathlib import Path


def open_path(path):
    """用系统默认程序打开文件或文件夹"""
    path = str(Path(path))
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def reveal_in_folder(path):
    """打开文件所在文件夹，并选中该文件（Linux 无选中概念，退化为打开所在目录）"""
    path = str(Path(path).resolve())
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])
