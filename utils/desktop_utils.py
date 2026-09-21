"""
utils/desktop_utils.py —— 跨平台打开文件 / 定位到文件夹
Windows 用 os.startfile / explorer /select,；macOS 用 open / open -R；Linux 用 xdg-open。
"""
import os
import subprocess
import sys
from pathlib import Path


def open_path(path):
    """用系统默认程序打开文件或文件夹；空路径忽略，缺失的目录先创建再打开
    
    返回：成功 True，失败 False（不会抛异常）
    """
    s = str(path or "").strip()
    if not s:
        return False
    p = Path(s)
    try:
        # 已存在 → 直接打开；不存在且“像目录”（无扩展名）→ 先创建再打开；
        # 不存在且“像文件”（有扩展名）→ 不创建伪目录，直接返回失败
        if not p.exists():
            if p.suffix:
                return False
            p.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(p))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True
    except Exception:
        return False


def reveal_in_folder(path):
    """打开文件所在文件夹，并选中该文件（Linux 无选中概念，退化为打开所在目录）"""
    path = str(Path(path).resolve())
    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])
