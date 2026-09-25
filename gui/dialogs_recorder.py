"""
gui/dialogs_recorder.py —— 屏幕录制工具（工具中心面板）

部件一览：
  TargetGlow      录制目标外的「呼吸灯」指示框：选定目标时淡红色虚化呼吸，
                  开始录制后钉成纯红实线（方角、鼠标穿透、可跟随窗口移动），
                  旁边的人一眼知道在录哪块画面
  RegionOverlay   全屏半透明遮罩，拖拽框选录制区域（Esc 取消）
  WindowPicker    全屏遮罩，鼠标悬停吸附窗口高亮，点一下选定（Esc 取消）
  RecBar          置顶录制控制浮条：倒计时 / 红点计时 / 停止
  RecordingPanel  参数面板：目标（全屏/区域/窗口）+ 帧率码率 + 内/外声音

录制期间面板自动隐藏（不然录全屏会把控制台自己录进去），停止后恢复。
功能逻辑全在 video_text_tools.screen_recorder（纯逻辑层），本文件只做界面。
"""
import ctypes
import os
import shutil
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (Qt, QRect, QTimer, QEvent, Signal,
                            QPropertyAnimation, QFileInfo)
from PySide6.QtGui import (QColor, QFont, QIcon, QPainter, QPen)
from PySide6.QtWidgets import (QApplication, QDialog, QWidget, QComboBox,
                               QButtonGroup, QHBoxLayout, QVBoxLayout, QLabel,
                               QLineEdit, QPushButton, QMessageBox, QFileDialog,
                               QSizePolicy, QFileIconProvider)

from core.config import DOWNLOAD_DIR
from gui.header import page_header
from gui.tool_panels import BasePanel, _dep_missing_panel
from utils.desktop_utils import reveal_in_folder
from video_text_tools.ffmpeg_utils import get_ffmpeg_path
from video_text_tools.screen_recorder import (
    IS_WINDOWS, ERR_NOT_WINDOWS, Recorder, build_record_command,
    list_windows, list_audio_devices, is_mix_device, has_wasapi_loopback,
    rect_to_physical, screen_physical, virtual_desktop, get_window_rect,
    restore_window)

# 主题色（与 gui/theme.py 同一套：主蓝 #3370FF，警示红 #D83931 一族）
PRIMARY = "#3370FF"
RED_LOCK = "#E53935"        # 录制中的实线框
RED_SOFT = "#FF6B6B"        # 预览呼吸框
MIN_DRAG = 8                # 框选小于这个尺寸视为误点

# 呼吸框外扩的光晕范围（逻辑像素）；窗口要按它留边，不然光晕被屏幕边裁掉
GLOW_PAD = 26


# ====================================================================
# 窗口图标：hwnd -> 所属进程 exe -> shell 图标（下拉/点选浮标展示）
# ====================================================================
_ICON_CACHE = {}              # pid -> QIcon | None（同进程窗口共享）
_ICON_PROVIDER = None         # 懒建，QFileIconProvider 要求有 QApplication


def _window_pid(hwnd):
    try:
        u32 = ctypes.windll.user32
        u32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        pid = ctypes.c_uint(0)
        u32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return pid.value or None
    except Exception:
        return None


def _exe_path(pid):
    k32 = ctypes.windll.kernel32
    try:
        k32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x1000, 0, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            n = ctypes.c_uint(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return buf.value or None
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None
    return None


def window_icon(hwnd):
    """窗口所属应用图标的 QIcon；取不到给 None（不抛异常，界面降级）"""
    global _ICON_PROVIDER
    pid = _window_pid(hwnd)
    if pid is None:
        return None
    if pid not in _ICON_CACHE:
        icon = None
        path = _exe_path(pid)
        if path:
            try:
                if _ICON_PROVIDER is None:
                    _ICON_PROVIDER = QFileIconProvider()
                ic = _ICON_PROVIDER.icon(QFileInfo(path))
                icon = None if ic.isNull() else ic
            except Exception:
                icon = None
        _ICON_CACHE[pid] = icon
    return _ICON_CACHE[pid]


def _screens_info():
    """所有屏的逻辑几何 + DPR，元组格式与 screen_recorder 约定一致：
    (x, y, w, h, dpr)"""
    out = []
    for s in QApplication.screens():
        g = s.geometry()
        out.append((g.x(), g.y(), g.width(), g.height(), s.devicePixelRatio()))
    return out


def _logical_to_physical(x, y):
    """逻辑全局点 -> 物理全局点（按所在屏 DPR；混合 DPI 属长尾按所在屏近似）"""
    for sx, sy, sw, sh, dpr in _screens_info():
        if sx <= x < sx + sw and sy <= y < sy + sh:
            return int(round(x * dpr)), int(round(y * dpr))
    dpr = _screens_info()[0][4] if _screens_info() else 1.0
    return int(round(x * dpr)), int(round(y * dpr))


def _phys_to_logical(rect4):
    """物理矩形 -> 逻辑 QRect（按矩形中心所在屏的 DPR 反算）"""
    x, y, w, h = rect4
    screens = _screens_info()
    cx, cy = x + w / 2.0, y + h / 2.0
    for sx, sy, sw, sh, dpr in screens:
        px0, py0 = sx * dpr, sy * dpr
        if px0 <= cx < px0 + sw * dpr and py0 <= cy < py0 + sh * dpr:
            return QRect(round(x / dpr), round(y / dpr),
                         round(w / dpr), round(h / dpr))
    dpr = screens[0][4] if screens else 1.0
    return QRect(round(x / dpr), round(y / dpr), round(w / dpr), round(h / dpr))


def _ffmpeg_usable():
    """返回可用的 ffmpeg 路径，找不到给 None（查找链同水印工具）。"""
    p = get_ffmpeg_path()
    if p and os.path.isfile(p):
        return p
    return shutil.which(p) if p else None


def _fmt_secs(sec):
    sec = int(sec)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ====================================================================
# 目标呼吸指示框（方角，鼠标穿透，置顶）
# ====================================================================
class TargetGlow(QWidget):
    """录制目标外的淡红虚化「呼吸灯」外框；开始录制后 lock() 成纯红实线。

    - 独立置顶顶层窗 + WindowTransparentForInput：完全不吃鼠标，
      不挡用户在目标区域内的任何操作；
    - 所有线条一律画在目标矩形边界之外：gdigrab 只捕获边界以内的
      像素，这样红框只有电脑前的人看得见，不会被录进成片；
    - 方角、无阴影外壳（需求明确不要圆角），光晕由四层递减 alpha 的
      描边圈逐层画出，配 windowOpacity 1.8s 一个来回的补间 = 呼吸感；
    - 窗口目标可给 hwnd：内部 250ms 轮询 GetWindowRect 跟随移动/缩放，
      窗口被最小化时暂时隐藏，恢复可见自动重新出现。
    """

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool
                            | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._rect = None            # 逻辑矩形（全局坐标）
        self._hwnd = None
        self._locked = False
        self._pulse = QPropertyAnimation(self, b"windowOpacity", self)
        self._pulse.setDuration(1800)
        self._pulse.setStartValue(0.35)
        self._pulse.setKeyValueAt(0.5, 1.0)
        self._pulse.setEndValue(0.35)
        self._pulse.setLoopCount(-1)
        self._follow = QTimer(self)
        self._follow.setInterval(250)
        self._follow.timeout.connect(self._refresh_follow)

    # ---- 对外三态 ----
    def show_rect(self, logical_rect):
        """固定区域目标（框选结果）"""
        self._hwnd = None
        self._follow.stop()
        self._place(logical_rect)
        self._begin_breath()
        self.show()

    def show_window(self, hwnd):
        """窗口目标：跟随 hwnd 的实时矩形"""
        self._hwnd = hwnd
        self._refresh_follow()
        self._follow.start()
        self._begin_breath()
        self.show()

    def lock(self):
        """开录：呼吸停，钉成纯红实线。线全部贴边界外绘制，
        录像里不会出现——红框只给电脑前的人看"""
        self._locked = True
        self._pulse.stop()
        self.setWindowOpacity(1.0)
        self.update()

    # ---- 内部 ----
    def _begin_breath(self):
        self._locked = False
        self._pulse.start()

    def _place(self, rect):
        self._rect = rect
        m = GLOW_PAD
        self.setGeometry(rect.x() - m, rect.y() - m,
                         rect.width() + 2 * m, rect.height() + 2 * m)

    def _refresh_follow(self):
        if self._hwnd is None:
            return
        phys = get_window_rect(self._hwnd)
        if phys is None:                    # 最小化/关闭：先藏起来等它回来
            self.hide()
            return
        if not self.isVisible():
            self.show()
        self._place(_phys_to_logical(phys))

    def paintEvent(self, _e):
        if self._rect is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._rect.translated(-self.x(), -self.y())
        if self._locked:
            # 纯红：3px 实线 + 一圈淡淡的固定外晕；pen 中心线落在边界外
            # 2px/7px 处，最内侧像素也在捕获矩形之外，不会进录像
            pen = QPen(QColor(RED_LOCK))
            pen.setWidth(3)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r.adjusted(-2, -2, 2, 2))
            glow = QPen(QColor(229, 57, 53, 40))
            glow.setWidth(7)
            p.setPen(glow)
            p.drawRect(r.adjusted(-7, -7, 7, 7))
            return
        # 预览：方角淡红虚化呼吸——四层由内向外的递减 alpha 描边圈；
        # 圈心距边界 ≥ 半线宽，保证每一像素都在捕获区外
        rings = ((3, 150, 2), (7, 90, 5), (12, 45, 8), (18, 20, 8))
        for expand, alpha, width in rings:
            pen = QPen(QColor(255, 107, 107, alpha))
            pen.setWidth(width)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r.adjusted(-expand, -expand, expand, expand))
        # 虚线感：最内圈叠一层 DashLine（"虚化"而不刺眼）
        dash = QPen(QColor(255, 138, 128, 200))
        dash.setWidth(1)
        dash.setStyle(Qt.PenStyle.DashLine)
        p.setPen(dash)
        p.drawRect(r.adjusted(-3, -3, 3, 3))


# ====================================================================
# 全屏遮罩公共底座
# ====================================================================
class _MaskBase(QDialog):
    """盖住整个虚拟桌面的半透明遮罩（选区/选窗共用），Esc 取消。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        box = QRect()
        for s in QApplication.screens():
            box = box.united(s.geometry())
        self.setGeometry(box)
        self._title_font = QFont("Microsoft YaHei", 10)

    def _draw_hint(self, p, rect, text, icon=None):
        """框上方的小标签：深底圆角托底，防文字糊在亮色桌面上看不清；
        icon 给 QIcon 时在文字左侧画 16px 应用图标"""
        p.setFont(self._title_font)
        pix = icon.pixmap(16, 16) if icon is not None else None
        pad = 16 + (20 if pix else 0)
        w = min(p.fontMetrics().horizontalAdvance(text) + pad,
                rect.width() + 40)
        ty = rect.top() - 30 if rect.top() - 30 > self.geometry().top() \
            else rect.bottom() + 6
        badge = QRect(rect.left(), ty, w, 24)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(20, 24, 32, 210))
        p.drawRoundedRect(badge, 6, 6)
        p.setPen(QColor("#FFFFFF"))
        tx = badge.left() + 8
        if pix is not None:
            p.drawPixmap(tx, badge.center().y() - 8, pix)
            tx += 20
        p.drawText(QRect(tx, badge.top(), badge.right() - 4 - tx,
                         badge.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   text)


# ====================================================================
# 区域框选遮罩
# ====================================================================
class RegionOverlay(_MaskBase):
    """拖拽框选录制区域：选区挖空透出真实桌面，松手即定稿，Esc 取消。

    坐标一律用 Qt 逻辑像素；注意不能 setFocus（全局键盘监听约定）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._sel = QRect()
        self._origin = None
        self._global = QRect()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._origin = e.position().toPoint()
            self._sel = QRect(self._origin, self._origin)
            self.update()

    def mouseMoveEvent(self, e):
        if self._origin is not None:
            self._sel = QRect(self._origin, e.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, e):
        if self._origin is None:
            return
        self._origin = None
        if self._sel.width() >= MIN_DRAG and self._sel.height() >= MIN_DRAG:
            self._global = self._sel.translated(self.geometry().topLeft())
            self.accept()
        else:
            self._sel = QRect()   # 误点：清掉重画
            self.update()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and self._sel.width() >= MIN_DRAG:
            self._global = self._sel.translated(self.geometry().topLeft())
            self.accept()
        super().keyPressEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 100))
        if not self._sel.isNull():
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(self._sel, Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            pen = QPen(QColor(PRIMARY))
            pen.setWidth(2)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self._sel)
            tip = f"{self._sel.width()} × {self._sel.height()}　松开选定 · Esc 取消"
            self._draw_hint(p, self._sel, tip)

    @staticmethod
    def pick(parent=None):
        """弹框选遮罩；返回全局逻辑 QRect，取消返回 None"""
        dlg = RegionOverlay(parent)
        return dlg._global if dlg.exec() == QDialog.DialogCode.Accepted else None


# ====================================================================
# 窗口点选遮罩
# ====================================================================
class WindowPicker(_MaskBase):
    """鼠标悬停吸附窗口高亮，点一下选定（自家面板/遮罩本身自动跳过）。

    ⚠ 命中判定不能用 WindowFromPoint：遮罩自己就是最顶层的窗，会把自己
    点到（然后被"排除自家进程"逻辑挡掉→永远选不中）。所以拿
    list_windows(exclude_self) 的候选列表自己做包含判定，取面积最小者
    （最贴手的小窗口优先）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cands = [(h, t, _phys_to_logical(r))
                       for h, t, r, _m in list_windows(exclude_self=True)]
        self._hover = None          # (hwnd, title, logical_rect)
        self._result = None

    def mouseMoveEvent(self, e):
        gp = self.mapToGlobal(e.position().toPoint())
        best = None
        for hwnd, title, r in self._cands:
            if r.contains(gp) and (best is None
                                   or r.width() * r.height()
                                   < best[2].width() * best[2].height()):
                best = (hwnd, title, r)
        if best != self._hover:
            self._hover = best
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._hover:
            self._result = self._hover
            self.accept()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 90))
        if self._hover is None:
            p.setPen(QColor("#E6E8EB"))
            p.setFont(QFont("Microsoft YaHei", 12))
            p.drawText(self.rect().adjusted(0, 40, 0, 0),
                       Qt.AlignmentFlag.AlignHCenter |
                       Qt.AlignmentFlag.AlignTop,
                       "移动鼠标悬停在要录制的窗口上，点击选定　·　Esc 取消")
            return
        # 命中窗口：挖空透出 + 蓝色高亮框 + 标题标签
        hwnd, title, r = self._hover
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.fillRect(r.translated(-self.x(), -self.y()), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        local = r.translated(-self.x(), -self.y())
        pen = QPen(QColor(PRIMARY))
        pen.setWidth(3)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(local)
        short = title if len(title) <= 30 else title[:29] + "…"
        self._draw_hint(p, local, f"{short}　{r.width()}×{r.height()}",
                        icon=window_icon(hwnd))

    @staticmethod
    def pick(parent=None):
        """返回选定的 (hwnd, 标题, 逻辑矩形)，取消给 None"""
        dlg = WindowPicker(parent)
        return dlg._result if dlg.exec() == QDialog.DialogCode.Accepted else None


# ====================================================================
# 录制控制浮条
# ====================================================================
class RecBar(QDialog):
    """置顶小浮条：录制目标 + ● 计时 + 停止。倒计时阶段先显示 3/2/1。

    深色圆角浮具（QSS 自绘，不走 apply_rounded——那套完整外壳对一条
    40px 的浮具过重）；按住空白处可拖走。
    """

    stopped = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        card = QWidget(self)
        card.setObjectName("RecBarCard")
        card.setStyleSheet(
            "#RecBarCard { background:#1F2329; border-radius:14px; border:1px solid #3A404B; }"
            "#RecBarCard QLabel { color:#E6E8EB; font-size:13px; background:transparent; }"
            "#RecBarCard QLabel#Dot { color:#E74C3C; font-size:11px; }"
            "#RecBarCard QLabel#Time { color:#FFFFFF; font-size:15px; font-weight:700;"
            " font-family:Consolas,monospace; }"
            "#RecBarCard QPushButton { background:#D83931; color:white;"
            " border:none; border-radius:8px; padding:6px 16px; font-size:13px; font-weight:600; }"
            "#RecBarCard QPushButton:hover { background:#F04438; }"
            "#RecBarCard QPushButton:disabled { background:#4A5160; color:#8F959E; }")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(16, 8, 16, 8)
        lay.setSpacing(10)
        self.lbl_dot = QLabel("●")
        self.lbl_dot.setObjectName("Dot")
        self.lbl_target = QLabel("")
        self.lbl_target.setStyleSheet("color:#B7BFC9;")
        self.lbl_state = QLabel("准备中")
        self.lbl_time = QLabel("00:00")
        self.lbl_time.setObjectName("Time")
        self.lbl_time.setMinimumWidth(52)
        self.b_stop = QPushButton("⏹ 停止录制")
        self.b_stop.setEnabled(False)
        self.b_stop.clicked.connect(self.stopped.emit)
        for w in (self.lbl_dot, self.lbl_target, self.lbl_state,
                  self.lbl_time, self.b_stop):
            lay.addWidget(w)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        self.adjustSize()
        geo = QApplication.primaryScreen().availableGeometry()
        self.move(geo.center().x() - self.width() // 2, geo.bottom() - self.height() - 28)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()

    def set_target(self, desc):
        self.lbl_target.setText(desc)
        self.adjustSize()

    def set_countdown(self, n):
        self.lbl_state.setText(f"{n} 秒后开始…")
        self.lbl_time.setText("")

    def start_running(self):
        self.lbl_state.setText("● 正在录制")
        self.b_stop.setEnabled(True)
        self.adjustSize()

    def set_time(self, sec):
        self.lbl_time.setText(_fmt_secs(sec))

    def closing(self):
        self.lbl_state.setText("收尾中…")
        self.lbl_dot.setStyleSheet("color:#8F959E;")
        self.b_stop.setEnabled(False)


# ====================================================================
# 小件工厂：卡片分组 / 胶囊选择按钮
# ====================================================================
def _card(title, icon=""):
    """白底圆角分组卡：#Card + 加粗小标题，返回 (卡片, 卡内布局)"""
    c = QWidget()
    c.setObjectName("Card")
    v = QVBoxLayout(c)
    v.setContentsMargins(18, 16, 18, 16)   # 与 gui.header.Card 新刻度一致
    v.setSpacing(8)
    head = QLabel(f"{icon}  <b>{title}</b>" if icon else f"<b>{title}</b>")
    head.setStyleSheet("background:transparent; font-size:13px; color:#1F2329;")
    v.addWidget(head)
    return c, v


def _chip(text, group):
    """主题胶囊选项（ChipBtn 的 checked 态即选中）。

    ⚠ 必须显式给 id：Qt6 的 addButton 不传 id 时自动分配的是负数
    （-2、-3…），idClicked 拿到的就不是按钮序，模式切换会全部落空。
    """
    b = QPushButton(text)
    b.setObjectName("ChipBtn")
    b.setCheckable(True)
    group.addButton(b, len(group.buttons()))   # id = 加入顺序
    return b


def _row(*ws, spacing=8):
    r = QHBoxLayout()
    r.setSpacing(spacing)
    for w in ws:
        r.addWidget(w)
    return r


def _tip(text):
    t = QLabel(text)
    t.setObjectName("PageTip")
    t.setStyleSheet("background:transparent;")
    return t


class _WindowCombo(QComboBox):
    """下拉展开前自动重扫窗口列表：面板开了一会儿再去选，新开的窗口
    也在列表里，不用记得先点 ⟳。还原回调由面板接线到 _refresh_windows。"""

    on_about_popup = None          # 面板实例化后赋值的回调

    def showPopup(self):
        if callable(self.on_about_popup):
            self.on_about_popup()
        super().showPopup()


# ====================================================================
# 录制面板
# ====================================================================
class RecordingPanel(BasePanel):
    """工具中心「屏幕录制」面板。

    不走 BasePanel 的 ToolWorker 骨架：录屏子进程本身非阻塞，面板用
    QTimer 轮询进程存活与计时即可。启动/停止按钮借 make_run_row 的外观，
    断掉默认信号后接自己的状态机。
    """

    def _build(self, outer):
        outer.addWidget(page_header("屏幕录制",
                                    "全屏 / 区域框选 / 指定窗口，按帧率码率保存 MP4",
                                    icon="🎥"))
        if not IS_WINDOWS:
            outer.addWidget(_dep_missing_panel(ERR_NOT_WINDOWS))
            return

        self._rec = None            # Recorder 实例（录制中非 None）
        self._bar = None            # RecBar
        self._glow = None           # TargetGlow（录制目标指示框）
        self._poll = QTimer(self)   # 进程存活轮询
        self._poll.setInterval(500)
        self._poll.timeout.connect(self._tick)
        self._region = None         # 框选结果（全局逻辑矩形）
        self._win = None            # 点选/下拉选定的窗口 (hwnd, 标题, 逻辑矩形)
        self._out_path = ""
        self._audio_paths = []      # 本次录制的旁路 WAV（完成提示用）
        self._filter_on = False
        self._sys_spec = None       # 内部声音的实现方式 ("system", None|名字)
        self._sys_desc = ""

        # ---- 卡片 1：录制目标 ----
        c1, v1 = _card("录制目标", "🎬")
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        chip_fs = _chip("🖥  全屏", self._mode_group)
        chip_rg = _chip("⬚  框选区域", self._mode_group)
        chip_wd = _chip("🎯  指定窗口", self._mode_group)
        chip_fs.setChecked(True)
        v1.addLayout(_row(chip_fs, chip_rg, chip_wd))

        self.cb_screen = QComboBox()
        self.cb_screen.addItem("主屏幕", -1)
        self.cb_screen.addItem("所有屏幕", -2)
        for i, s in enumerate(_screens_info()):
            if i == 0:
                continue
            self.cb_screen.addItem(f"屏幕{i + 1}  {int(s[2])}×{int(s[3])}", i)
        self.row_screen = QWidget()
        self.row_screen.setLayout(self._lb_row(_tip("全屏范围"), self.cb_screen))
        v1.addWidget(self.row_screen)

        self.b_pick = QPushButton("⬚  开始框选")
        self.lbl_region = _tip("还没框选：点左边按钮，在屏幕上拖一个方框")
        self.row_region = QWidget()
        rr = QHBoxLayout(self.row_region)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.addWidget(self.b_pick)
        rr.addWidget(self.lbl_region, 1)
        v1.addWidget(self.row_region)

        self.b_pickwin = QPushButton("🎯  在屏幕上点选")
        self.cb_window = _WindowCombo()
        self.cb_window.setSizePolicy(QSizePolicy.Policy.Expanding,
                                     QSizePolicy.Policy.Fixed)
        self.b_wrefresh = QPushButton("⟳")
        self.b_wrefresh.setObjectName("GhostBtn")
        self.b_wrefresh.setFixedWidth(36)
        self.lbl_win = _tip("也可以从下拉里挑（含已最小化的，开录时自动还原）；"
                            "录制中指示框会跟着窗口移动")
        wr = QHBoxLayout()
        wr.setContentsMargins(0, 0, 0, 0)
        wr.addWidget(self.b_pickwin)
        wr.addWidget(self.cb_window, 1)
        wr.addWidget(self.b_wrefresh)
        self.row_window = QWidget()
        self.row_window.setLayout(wr)
        v1.addWidget(self.row_window)
        v1.addWidget(self.lbl_win)
        outer.addWidget(c1)

        # ---- 卡片 2：画面与声音 ----
        c2, v2 = _card("画面与声音", "🎞")
        self.cb_fps = QComboBox()
        self.cb_fps.addItems(["10", "15", "24", "30"])
        self.cb_fps.setCurrentText("30")
        self.cb_fps.setFixedWidth(80)
        self.cb_br = QComboBox()
        self.cb_br.setEditable(True)
        self.cb_br.addItems(["2M", "4M", "8M", "16M"])
        self.cb_br.setCurrentText("8M")
        self.cb_br.setFixedWidth(90)
        fr = _row(_tip("帧率"), self.cb_fps, _tip("视频码率"), self.cb_br)
        fr.addStretch(1)
        v2.addLayout(fr)

        self._audio_group = QButtonGroup(self)
        # 非互斥：内部/外部声音可同时勾选，各自单独落一条 WAV 音轨；
        # 「不录音」的互斥关系在 _audio_toggled 里手工维护
        self._audio_group.setExclusive(False)
        self.chip_a_none = _chip("🔇  不录音", self._audio_group)
        self.chip_a_sys = _chip("🔊  内部声音（系统播放）", self._audio_group)
        self.chip_a_mic = _chip("🎤  外部声音（麦克风）", self._audio_group)
        self.chip_a_none.setChecked(True)
        v2.addLayout(_row(self.chip_a_none, self.chip_a_sys, self.chip_a_mic))
        v2.addWidget(_tip("内部 / 外部声音可同时勾选：每个音源单独存为 WAV 音轨，"
                          "视频文件不含声轨，方便剪辑"))

        self.cb_mic = QComboBox()
        self.cb_mic.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Fixed)
        self.b_arefresh = QPushButton("⟳")
        self.b_arefresh.setObjectName("GhostBtn")
        self.b_arefresh.setFixedWidth(36)
        self.row_mic = QWidget()
        mr = QHBoxLayout(self.row_mic)
        mr.setContentsMargins(0, 0, 0, 0)
        mr.addWidget(_tip("麦克风设备"))
        mr.addWidget(self.cb_mic, 1)
        mr.addWidget(self.b_arefresh)
        v2.addWidget(self.row_mic)

        self.lbl_audio = _tip("")
        v2.addWidget(self.lbl_audio)
        outer.addWidget(c2)

        # ---- 卡片 3：保存位置 ----
        c3, v3 = _card("保存位置", "📁")
        self.ed_out = QLineEdit(str(Path(DOWNLOAD_DIR) / "录屏"))
        self.b_dir = QPushButton("浏览…")
        self.b_dir.setObjectName("GhostBtn")
        v3.addLayout(_row(self.ed_out, self.b_dir))
        v3.addWidget(_tip("文件名自动生成：录屏_20260926_120000.mp4"))
        outer.addWidget(c3)

        self.make_log_box(outer, height=96)
        self.make_run_row(outer, "⏺ 开始录制")
        # 借骨架外观，换掉行为：录屏不需要 ToolWorker/进度条状态机
        self.b_run.clicked.disconnect()
        self.b_stop.clicked.disconnect()
        self.b_run.clicked.connect(self._start_rec)
        self.b_stop.clicked.connect(self._stop_rec)
        self.bar.setVisible(False)
        self.b_stop.setEnabled(False)

        # ---- 信号接线 ----
        self._mode_group.idClicked.connect(self._mode_changed)
        self.b_pick.clicked.connect(self._pick_region)
        self.b_pickwin.clicked.connect(self._pick_window)
        self.cb_window.activated.connect(self._window_from_combo)
        # 下拉展开前先重扫一遍：列表永远新鲜，⟳ 保留作手动刷新
        self.cb_window.on_about_popup = self._refresh_windows
        self.b_wrefresh.clicked.connect(self._refresh_windows)
        self.b_dir.clicked.connect(self._pick_dir)
        # 非互斥组用 buttonToggled：idClicked 只在按下时发，代码回勾
        # （如自动弹回「不录音」）不会触发，联动会漏
        self._audio_group.buttonToggled.connect(self._audio_toggled)
        self.b_arefresh.clicked.connect(self._refresh_audios)

        self._mode_changed(0)
        self._refresh_windows()
        self._refresh_audios()

    @staticmethod
    def _lb_row(*ws):
        r = QHBoxLayout()
        r.setContentsMargins(0, 0, 0, 0)
        for w in ws:
            r.addWidget(w)
        r.addStretch(1)
        return r

    # ----------------------------------------------------------------
    # 界面联动
    # ----------------------------------------------------------------
    def _mode_index(self):
        """当前选中的目标胶囊序号（0/1/2）"""
        btn = self._mode_group.checkedButton()
        return self._mode_group.buttons().index(btn) if btn else 0

    def _mode_changed(self, idx):
        # QButtonGroup.idClicked 的 id 即按钮序（_chip 里显式 setId 保证）
        self.row_screen.setVisible(idx == 0)
        self.row_region.setVisible(idx == 1)
        self.row_window.setVisible(idx == 2)
        self.lbl_win.setVisible(idx == 2)
        # 切换目标就把上一轮的选定和指示框作废，防止拿着旧目标开录
        self._drop_target()

    def _drop_target(self):
        if self._glow:
            self._glow.close()
            self._glow = None

    def _pick_region(self):
        rect = RegionOverlay.pick(self.window())
        if rect is not None:
            self._region = rect
            self.lbl_region.setText(
                f"已选 {rect.width()}×{rect.height()}"
                f" @ ({rect.x()}, {rect.y()})")
            self._get_glow().show_rect(rect)

    def _pick_window(self):
        got = WindowPicker.pick(self.window())
        if got is None:
            return
        hwnd, title, logical = got
        self._win = (hwnd, title, logical)
        self.lbl_win.setText(f"已选定：{title}")
        self._get_glow().show_window(hwnd)

    def _window_from_combo(self, index):
        """下拉里直接选（数据里带 hwnd，指示框同样能跟随）"""
        data = self.cb_window.itemData(index)
        if not data:
            return
        hwnd, title, phys = data
        logical = _phys_to_logical(phys)
        self._win = (hwnd, title, logical)
        self.lbl_win.setText(f"已选定：{title}")
        self._get_glow().show_window(hwnd)

    def _get_glow(self):
        if self._glow is None:
            self._glow = TargetGlow()
        return self._glow

    def _refresh_windows(self):
        keep = self.cb_window.currentData()
        self.cb_window.clear()
        wins = list_windows()
        for hwnd, title, (x, y, w, h_), minimized in wins:
            short = title if len(title) <= 26 else title[:25] + "…"
            ic = window_icon(hwnd)
            # 最小化窗的矩形是屏幕外占位，显示尺寸没意义，改标状态
            tail = "已最小化·开录时自动还原" if minimized else f"{w}×{h_}"
            self.cb_window.addItem(ic or QIcon(),      # 应用图标，认窗口靠它
                                   f"{short}　{tail}",
                                   (hwnd, title, (x, y, w, h_)))
        if not wins:
            self.cb_window.addItem("（没有发现可录制的窗口）", None)
        elif keep:
            for i in range(self.cb_window.count()):
                d = self.cb_window.itemData(i)
                if d and d[0] == keep[0]:
                    self.cb_window.setCurrentIndex(i)
                    break

    # ---- 音频 ----
    _audio_sync = False        # 联动回勾时防递归（buttonToggled 会自触发）

    def _audio_toggled(self, btn, on):
        """「不录音」与两个音源互斥的手工维护（多选组）：
        勾任一音源 → 取消不录音；两个音源全取消 → 弹回不录音；
        直接取消不录音 → 没有音源选中，同样弹回。"""
        if self._audio_sync:
            return
        self._audio_sync = True
        try:
            if btn is self.chip_a_none and on:
                self.chip_a_sys.setChecked(False)
                self.chip_a_mic.setChecked(False)
            elif btn is not self.chip_a_none and on:
                self.chip_a_none.setChecked(False)
            if (not self.chip_a_none.isChecked()
                    and not self.chip_a_sys.isChecked()
                    and not self.chip_a_mic.isChecked()):
                self.chip_a_none.setChecked(True)
        finally:
            self._audio_sync = False
        self._audio_changed()

    def _audio_changed(self):
        mic = self.chip_a_mic.isChecked()
        self.row_mic.setVisible(mic)
        parts = []
        if self.chip_a_sys.isChecked() and self._sys_desc:
            parts.append(f"内部声音实现方式：{self._sys_desc}")
        if mic and not self.cb_mic.count():
            parts.append("没有枚举到麦克风设备：可点 ⟳ 重试，或改用其它音源")
        self.lbl_audio.setText("；".join(parts))

    def _refresh_audios(self):
        ffmpeg = _ffmpeg_usable()
        devices = list_audio_devices(ffmpeg) if ffmpeg else []
        mics = [n for n in devices if not is_mix_device(n)]
        mixes = [n for n in devices if is_mix_device(n)]

        self.cb_mic.clear()
        for n in mics:
            self.cb_mic.addItem(n, n)

        self._sys_spec = None
        self._sys_desc = ""
        if ffmpeg and has_wasapi_loopback(ffmpeg):
            self._sys_spec, self._sys_desc = ("system", None), "WASAPI 环回（当前 ffmpeg 支持）"
        elif mixes:
            self._sys_spec = ("system", mixes[0])
            self._sys_desc = f"立体声混音设备（{mixes[0]}）"
        can_sys = bool(ffmpeg) and self._sys_spec is not None
        self.chip_a_sys.setEnabled(can_sys)
        self.chip_a_sys.setToolTip(
            self._sys_desc if can_sys else
            "录不到系统内部声音：当前 ffmpeg 没有 WASAPI 环回，也没发现"
            "「立体声混音」类设备（可在声音设置里启用后点 ⟳）")
        self.chip_a_mic.setEnabled(bool(mics))
        if not mics:
            self.chip_a_mic.setToolTip("没有枚举到麦克风设备")
        self._audio_changed()

    def _audio_specs(self):
        """选中的音源列表（可内部+外部两个）：[('system',…) 和/或 ('mic', 名字)]"""
        specs = []
        if self.chip_a_sys.isChecked() and self._sys_spec:
            specs.append(self._sys_spec)
        if self.chip_a_mic.isChecked():
            name = self.cb_mic.currentData()
            if name:
                specs.append(("mic", name))
        return specs

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if d:
            self.ed_out.setText(d)

    # ----------------------------------------------------------------
    # 录制状态机：空闲 ->（呼吸预览）->（3-2-1 倒计时）-> 录制中 -> 收尾
    # ----------------------------------------------------------------
    def _target_desc(self):
        idx = self._mode_index()
        if idx == 0:
            return "🖥 全屏·" + self.cb_screen.currentText()
        if idx == 1:
            return f"⬚ 区域 {self._region.width()}×{self._region.height()}"
        return f"🎯 {self._win[1]}"

    def _collect_params(self):
        """校验并组录制计划 [(命令, 产物)]（首项视频，其后音轨）；
        不合法/缺 ffmpeg 时弹提示返回 None"""
        ffmpeg = _ffmpeg_usable()
        if not ffmpeg:
            QMessageBox.information(
                self, "缺少 ffmpeg",
                "屏幕录制需要 ffmpeg.exe（与视频水印工具同一依赖）。\n\n"
                "· 正式版：随安装包内置，请确认从官方安装包启动；\n"
                "· 开发版：把 ffmpeg.exe 放到项目 assets\\ 目录或 PATH 中。")
            return None
        idx = self._mode_index()
        physical = None
        title = None
        if idx == 0:
            sel = self.cb_screen.currentData()
            screens = _screens_info()
            if sel == -2:
                physical = virtual_desktop(screens)
            elif sel is not None and sel > 0:
                physical = screen_physical(screens[sel])
            # sel == -1 主屏：不传 offset/size，gdigrab 默认就是主屏
        elif idx == 1:
            if self._region is None:
                QMessageBox.information(self, "提示", "请先框选录制区域")
                return None
            physical = rect_to_physical(
                (self._region.x(), self._region.y(),
                 self._region.width(), self._region.height()),
                _screens_info())
        else:
            if self._win is None:
                QMessageBox.information(
                    self, "提示", "请先点「在屏幕上点选」选中要录制的窗口，"
                    "或从下拉里挑一个")
                return None
            title = self._win[1]
            # 最小化窗 gdigrab 抓不了（ffmpeg 会卡在找窗口）：现在还原，
            # 3-2-1 倒计时足够窗口展开；用户中途自己最小化的也不卡录
            restore_window(self._win[0])

        out_dir = self.ed_out.text().strip() or str(Path(DOWNLOAD_DIR) / "录屏")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "目录不可用", str(e))
            return None
        name = datetime.now().strftime("录屏_%Y%m%d_%H%M%S.mp4")
        out_path = os.path.join(out_dir, name)
        plans = build_record_command(
            ffmpeg, ("window" if idx == 2 else ("region" if idx == 1 else "fullscreen")),
            out_path, fps=self.cb_fps.currentText(), bitrate=self.cb_br.currentText(),
            physical=physical, title=title, audio=self._audio_specs())
        return plans

    def _start_rec(self):
        if self._rec is not None:
            return
        plans = self._collect_params()
        if plans is None:
            return
        self._out_path = plans[0][1]
        self._audio_paths = [p for _c, p in plans[1:]]
        self._rec = Recorder(plans)

        w = self.window()
        if w is not self and not self._filter_on:
            w.installEventFilter(self)     # 关窗时先停录，防 ffmpeg 变孤儿
            self._filter_on = True

        # 呼吸预览框：非全屏目标在倒计时前就到位（选定那一刻其实已经在呼吸）
        self._bar = RecBar()
        self._bar.stopped.connect(self._stop_rec)
        self._bar.set_target(self._target_desc())
        self._bar.show()
        self.b_run.setEnabled(False)
        self._countdown(3)

    def _countdown(self, n):
        if self._rec is None:
            return
        if n > 0:
            self._bar.set_countdown(n)
            QTimer.singleShot(1000, lambda: self._countdown(n - 1))
            return
        err = self._rec.start()
        if err:
            self._append_log(f"❌ {err}")
            QMessageBox.warning(self, "启动录制失败", err)
            self._bar.close()
            self._bar = None
            self._rec = None
            self.b_run.setEnabled(True)
            return
        # 开录：呼吸框钉成纯红，隐藏宿主面板
        if self._glow:
            self._glow.lock()
        w = self.window()
        if w is not self:
            w.hide()
        self._bar.start_running()
        self.b_stop.setEnabled(True)
        self._poll.start()
        self._append_log("⏺ 开始录制…")

    def _tick(self):
        if self._rec is None:
            return
        if self._rec.alive():
            self._bar.set_time(self._rec.elapsed())
            return
        # 进程中途退出（ffmpeg 报错/用户从别处杀进程）：当作已停止收场
        secs = self._rec.elapsed()
        self._rec = None
        self._finish(None, secs, unexpected=True)

    def _stop_rec(self):
        """停止：先让浮条文案上屏（singleShot 下一拍再阻塞收尾）"""
        if self._rec is None:
            return
        self.b_stop.setEnabled(False)
        if self._bar:
            self._bar.closing()
        QTimer.singleShot(0, self._do_stop)

    def _do_stop(self):
        if self._rec is None:
            return
        secs = self._rec.elapsed()
        err = self._rec.stop()
        self._rec = None
        self._append_log(f"⏹ 停止录制（{_fmt_secs(secs)}）")
        self._finish(err, secs)

    def _finish(self, err, secs, unexpected=False):
        self._poll.stop()
        if self._bar:
            self._bar.close()
            self._bar = None
        if self._glow:
            self._glow.close()
            self._glow = None
        w = self.window()
        if w is not self:
            w.show()
        self.b_run.setEnabled(True)
        self.b_stop.setEnabled(False)
        if unexpected:
            err = err or "录制进程意外退出（可能桌面会话变化或 ffmpeg 崩溃）"
        if err:
            self._append_log(f"❌ {err}")
            QMessageBox.warning(self, "录制异常", err)
            return
        size = os.path.getsize(self._out_path) if os.path.isfile(self._out_path) else 0
        self._append_log(f"🎉 录制完成：{self._out_path}（{_fmt_secs(secs)}，"
                         f"{size / 1024 / 1024:.1f} MB）")
        for p in self._audio_paths:
            if os.path.isfile(p):          # 0 字节的旁路 WAV 已被 Recorder 清掉
                self._append_log(
                    f"　🔊 音轨：{os.path.basename(p)}"
                    f"（{os.path.getsize(p) / 1024 / 1024:.1f} MB）")
        reveal_in_folder(self._out_path)

    # ----------------------------------------------------------------
    # 宿主窗口关闭时先停录（ToolDialog 是通用壳，不加录制特判，这里补）
    # ----------------------------------------------------------------
    def eventFilter(self, obj, e):
        if obj is self.window() and e.type() == QEvent.Type.Close \
                and self._rec is not None and self._rec.alive():
            self._do_stop()
        return super().eventFilter(obj, e)
