"""
gui/thumb_cache.py —— 视频首帧缩略图的缓存与后台抽取（成品库 / 素材库共用）

卡片/图标视图要显示真实首帧，但整目录逐条同步跑 ffmpeg 会把界面卡死。沿用录屏库
成熟的做法：按「绝对路径 + mtime」把首帧缓存在磁盘（文件一改键自动变、不脏读旧图），
只在后台线程抽取缺的那些，抽好把 QImage 回抛主线程上色（QPixmap 不能跨线程）。

与录屏页各自独立（这里是通用 thumbs 目录）；核心逻辑复用 ffmpeg_utils.get_video_thumbnail
（太短取不到 1 秒处就退到首帧）。抽不到就回空 QImage，界面保留占位、不报错。
"""
import hashlib
import os
import tempfile
from collections import deque
from pathlib import Path
import threading

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

_DIR = None


def cache_dir():
    """缩略图缓存目录（LOCALAPPDATA/aigc/thumbs，退化到系统临时目录）。"""
    global _DIR
    if _DIR is None:
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        _DIR = str(Path(base) / "aigc" / "thumbs")
        os.makedirs(_DIR, exist_ok=True)
    return _DIR


def png_path(path, mtime):
    """缓存键含 mtime：文件重录/改名后自然失效，不复用旧图。"""
    key = (hashlib.md5(os.path.abspath(str(path)).encode("utf-8")).hexdigest()
           + f"_{int(mtime or 0)}")
    return os.path.join(cache_dir(), key + ".png")


def _mtime_of(path):
    try:
        return os.path.getmtime(str(path))
    except OSError:
        return 0


def ensure_image(path, mtime=None):
    """同步取首帧（供后台线程调用，勿在主线程用）：命中缓存直接读盘，否则抽一次。

    返回 QImage（可能为 null，表示抽不到，界面据此保留占位）。"""
    if mtime is None:
        mtime = _mtime_of(path)
    png = png_path(path, mtime)
    if not (os.path.isfile(png) and os.path.getsize(png) > 0):
        try:
            from video_text_tools.ffmpeg_utils import get_video_thumbnail
            if not get_video_thumbnail(str(path), png, 1.0):
                get_video_thumbnail(str(path), png, 0.0)   # 太短退到首帧
        except Exception:
            return QImage()
    img = QImage()
    if os.path.isfile(png):
        loaded = QImage(png)
        if not loaded.isNull():
            img = loaded
    return img


class ThumbWorker(QThread):
    """后台抽首帧：ready(path, QImage)。submit 覆盖待办队列（连续刷新不积压），
    已缓存的一律跳过、只补缺的；主线程用 path 找对应卡片/条目上色。"""
    ready = Signal(str, QImage)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._q = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False

    def submit(self, jobs):
        """jobs: [(path, mtime), ...]。整队替换，避免旧待办堆积。"""
        with self._lock:
            self._q = deque(jobs)
        self._wake.set()

    def request_stop(self):
        self._stop = True
        self._wake.set()

    def run(self):
        while not self._stop:
            with self._lock:
                job = self._q.popleft() if self._q else None
            if job is None:
                self._wake.wait(0.3)
                self._wake.clear()
                continue
            path, mtime = job
            if self._stop:
                break
            img = ensure_image(path, mtime)
            if not self._stop:
                self.ready.emit(str(path), img)
