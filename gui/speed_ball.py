"""
gui/speed_ball.py —— 下载期间的「悬浮球」实时速率浮窗

半透明、可折叠的桌面小球（360 悬浮球那种）：默认缩成一颗小圆球显示当前速率
（如 `↓ 1.2M/s`），点一下展开看「本次新增 / 目录总计 / 已用时长」，可拖拽、
总在最前、不进任务栏。数据源是**盯目标目录（模型目录）体积增长**算写入速率——
faster-whisper 走 huggingface_hub 内部下载、拿不到逐字节回调，但下载确实在往
MODELS_DIR 写盘（连未完成的 .incomplete 也算），故目录增长即下载速率的可靠代理。

设计要点：
- 折叠/展开都自绘（paintEvent），不塞子控件，避免透明窗口样式陷阱；
- 纯逻辑 FolderSpeedometer 不依赖 Qt，可单测；
- 全局单例：show_speed_ball 前自动收掉上一个，下载结束 hide_speed_ball 收球。
"""
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget, QApplication

# ---- 视觉常量 ----
_BALL = 64                          # 折叠态直径
_PANEL_W, _PANEL_H = 208, 132       # 展开态面板
_WINDOW = 15.0                      # 近期速率滑动窗口（秒）：HF 每 ~10MB 才刷一次盘，
                                    # 窗口拉大才能把突发均摊成稳定速率，不至于忽 0 忽飙
_BG = QColor(20, 24, 32, 210)      # 半透明深底（不透明度走 alpha 通道，非 setWindowOpacity）
_RING_IDLE = QColor(255, 255, 255, 70)
_RING_RUN = QColor(0, 168, 112, 220)   # 有速率时的绿色环
_FG = QColor("#FFFFFF")
_DIM = QColor(255, 255, 255, 165)


def fmt_bytes(n):
    """字节数 → 人类可读（B/KB/MB/GB）。"""
    f = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GB"


def fmt_rate(bps):
    """字节/秒 → 紧凑速率串（悬浮球窄，尽量短）。"""
    if bps <= 0:
        return "空闲"
    kb = bps / 1024
    return f"{kb:.0f}K/s" if kb < 1024 else f"{kb / 1024:.1f}M/s"


def fmt_elapsed(sec):
    sec = int(max(sec, 0))
    return f"{sec // 60:02d}:{sec % 60:02d}"


class FolderSpeedometer:
    """盯一个目录的总字节增长，算近 ~窗口期平均写入速率。纯逻辑、无 Qt。"""

    def __init__(self, watch_dir, window=_WINDOW):
        self.watch_dir = str(watch_dir)
        self.window = window
        self._hist = deque()          # [(ts, total)]
        self._base_total = None
        self._start_ts = None

    def _total(self):
        """递归求目录内所有文件字节和（下载中途的 .incomplete 也计入）。"""
        base = Path(self.watch_dir)
        try:
            if not base.exists():
                return 0
            total = 0
            for f in base.rglob("*"):
                try:
                    if f.is_file():
                        total += f.stat().st_size
                except OSError:
                    continue
            return total
        except OSError:
            return 0

    def tick(self):
        """采样一次，返回 {total, session, rate, elapsed}。"""
        now = time.time()
        total = self._total()
        if self._base_total is None:
            self._base_total = total
            self._start_ts = now
        self._hist.append((now, total))
        while len(self._hist) > 2 and now - self._hist[0][0] > self.window:
            self._hist.popleft()
        rate = 0.0
        if len(self._hist) >= 2:
            t0, v0 = self._hist[0]
            dt = now - t0
            if dt > 0:
                rate = max((total - v0) / dt, 0.0)
        elapsed = now - (self._start_ts or now)
        session = max(total - self._base_total, 0)
        # 平均速率：自本次采样基线起的累计/耗时，刷盘间隙里也能给出稳定参考
        avg = session / elapsed if elapsed > 0 else 0.0
        return {
            "total": total,
            "session": session,
            "rate": rate,
            "avg": avg,
            "elapsed": elapsed,
        }


class SpeedBall(QWidget):
    """悬浮球本体。show_ball() 起、stop() 收。"""

    def __init__(self, parent, watch_dir, title="模型下载"):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._meter = FolderSpeedometer(watch_dir)
        self._title = title
        self._expanded = False
        self._snap = {"total": 0, "session": 0, "rate": 0.0, "avg": 0.0, "elapsed": 0.0}
        self._drag = None            # 拖拽时窗口内按下偏移
        self._moved = False
        self._font = QFont("Microsoft YaHei")
        self.setFixedSize(_BALL, _BALL)
        self._timer = QTimer(self)
        self._timer.setInterval(700)
        self._timer.timeout.connect(self._on_tick)

    # ---- 对外 ----
    def show_ball(self):
        self._place_default()
        self.show()
        self._timer.start()
        self._on_tick()

    def stop(self):
        self._timer.stop()
        self.hide()
        self.deleteLater()

    # ---- 位置：默认贴主屏右上角 ----
    def _place_default(self):
        screen = QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else None
        if not geo:
            return
        self.move(geo.right() - _BALL - 24, geo.top() + 60)

    # ---- 采样刷新 ----
    def _on_tick(self):
        self._snap = self._meter.tick()
        self.update()

    # ---- 折叠/展开：保持右下角不动，避免展开时飞出屏幕 ----
    def _toggle(self):
        old = self.geometry()
        anchor = old.bottomRight()
        self._expanded = not self._expanded
        w, h = (_PANEL_W, _PANEL_H) if self._expanded else (_BALL, _BALL)
        self.setFixedSize(w, h)
        self.move(anchor.x() - w, anchor.y() - h)
        self.update()

    def _close_rect(self):
        return self.rect().adjusted(_PANEL_W - 26, 6, -8, 24)

    # ---- 拖拽 / 点击 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            p = e.position().toPoint()
            if self._expanded and self._close_rect().contains(p):
                self.stop()
                hide_speed_ball()
                return
            self._drag = p
            self._moved = False

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            delta = e.position().toPoint() - self._drag
            if delta.manhattanLength() > 4:
                self._moved = True
                self.move(self.pos() + delta)
                self._drag = e.position().toPoint()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and not self._moved:
            self._toggle()                  # 没拖动 = 单击：折叠/展开
        self._drag = None

    def _disp_rate(self):
        """展示用速率：优先近期速率；刷盘间隙瞬时为 0 时回落到平均，避免误显“空闲”。"""
        return self._snap["rate"] or self._snap["avg"]

    # ---- 自绘 ----
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._expanded:
            self._paint_panel(p)
        else:
            self._paint_ball(p)
        p.end()

    def _paint_ball(self, p):
        disp = self._disp_rate()
        m = 3
        rect = self.rect().adjusted(m, m, -m, -m)
        p.setPen(QPen(_RING_RUN if disp > 0 else _RING_IDLE, 2))
        p.setBrush(_BG)
        p.drawEllipse(rect)
        # 上行箭头 + 紧凑速率
        self._font.setPointSize(9)
        p.setFont(self._font)
        p.setPen(_FG)
        p.drawText(rect.adjusted(0, -6, 0, -6),
                   Qt.AlignmentFlag.AlignCenter, "↓ " + fmt_rate(disp))
        self._font.setPointSize(7)
        p.setFont(self._font)
        p.setPen(_DIM)
        p.drawText(rect.adjusted(0, 12, 0, 0),
                   Qt.AlignmentFlag.AlignCenter, fmt_bytes(self._snap["session"]))

    def _paint_panel(self, p):
        r = self.rect().adjusted(2, 2, -2, -2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_BG)
        p.drawRoundedRect(r, 12, 12)
        # 标题
        self._font.setPointSize(9)
        p.setFont(self._font)
        p.setPen(_DIM)
        p.drawText(r.adjusted(14, 10, -30, 22),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._title + " · 实时速率")
        # 关闭 ×
        cr = self._close_rect()
        p.setPen(QPen(_DIM, 2))
        p.drawLine(cr.topLeft() + QPoint(3, 3), cr.bottomRight() + QPoint(-3, -3))
        p.drawLine(cr.topRight() + QPoint(-3, 3), cr.bottomLeft() + QPoint(3, -3))
        # 大速率（展示用：近期优先，刷盘间隙回落平均）
        self._font.setPointSize(18)
        p.setFont(self._font)
        p.setPen(_FG)
        p.drawText(r.adjusted(14, 24, -14, 44),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   "↓ " + fmt_rate(self._disp_rate()))
        # 明细四行
        s = self._snap
        self._font.setPointSize(9)
        p.setFont(self._font)
        p.setPen(_DIM)
        lines = (f"近期 {fmt_rate(s['rate'])} · 平均 {fmt_rate(s['avg'])}",
                 f"本次新增  {fmt_bytes(s['session'])}",
                 f"目录总计  {fmt_bytes(s['total'])}",
                 f"已用时长  {fmt_elapsed(s['elapsed'])}（点球收起 · ×关闭）")
        y = 66
        for ln in lines:
            p.drawText(r.adjusted(14, y, -12, y + 15),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, ln)
            y += 15


# ---- 全局单例 ----
_ball_ref = None


def show_speed_ball(parent, watch_dir, title="模型下载"):
    """弹出（并替换）悬浮球，返回实例。"""
    global _ball_ref
    hide_speed_ball()
    ball = SpeedBall(parent, watch_dir, title)
    _ball_ref = ball
    ball.show_ball()
    return ball


def hide_speed_ball():
    """收起悬浮球（下载结束时调）。"""
    global _ball_ref
    if _ball_ref is not None:
        b, _ball_ref = _ball_ref, None
        b.stop()
