"""
gui/clipboard_quick.py —— Ditto / Win+V 式剪贴板快贴悬浮窗：一个全局键呼出，敲回车贴回原处

为什么是独立悬浮窗而不是工具卡片：热键触发时前台属于“用户刚才正在打字的别的软件”，
要的就是“按一下 → 选一条 → 内容自动回到那个软件里”。工具卡片是嵌在主窗里的常驻面板，
够不着这个“贴回别人窗口”的动作。形态参考 gui/dialogs_launcher.py 那套深色无边框唤出窗：
置顶 + 失焦自动收起 + 拿到键盘焦点才谈打字过滤。

粘贴三步（缺一步就贴不上）：
  1) 弹出前先记住房前台窗口句柄（get_foreground_hwnd）；
  2) 选中后把内容写进剪贴板（先 suppress_next，免得监听器把已有条目重复入库）；
  3) 收起本窗 → 把焦点还给那扇窗（focus_hwnd）→ 延后一拍模拟 Ctrl+V（send_paste）。
"""
from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QApplication)

from gui import window_foreground
from gui.clipboard_hist import manager
from gui.clipboard_view import ClipboardView
from gui.theme import tokenize
from gui.window_frame import apply_rounded
from store import clipboard_store

#: 深色半透明卡片底（与唤出面板同一族配色，故意避开 theme 种子色免被映回浅色）
_PANEL_BG = QColor(28, 32, 38, 232)
_PANEL_BORDER = QColor(255, 255, 255, 30)
_TEXT = "#E8EAED"
_WEAK = "#9AA4B0"

_DFT_W, _DFT_H = 900, 540
_MIN_W, _MIN_H = 620, 380


class ClipboardQuickPanel(QDialog):
    """全局呼出的剪贴板快贴窗：搜索/过滤/悬停预览交给内嵌 ClipboardView，本类只管
    悬浮窗形态、失焦收起、键盘分派与“复制+粘回原窗”这套动作。"""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("📋 剪贴板快贴")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._armed = False                 # 是否已“站稳”，见 changeEvent 失焦收起
        self._pinned = False                # 钉住：失焦不自动收起
        self._target_hwnd = 0               # 弹出前的前台窗口，粘贴时把焦点还给它

        self.view = ClipboardView(dark=True, parent=self)
        self.view.activateRequested.connect(self._paste_row)
        self.view.copyRequested.connect(self._copy_only)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(8)
        outer.addWidget(self.view, 1)
        outer.addLayout(self._build_tips())

        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DFT_W, _DFT_H)
        apply_rounded(self, resizable=True, corner_grip=True,
                      show_min=False, show_max=False, add_titlebar=False,
                      card_bg=_PANEL_BG, border=_PANEL_BORDER)

        # ↑↓/Tab/Del/Enter 在搜索框有焦点时也要能改选中/切换过滤：挂事件过滤器接管
        self.view.search.installEventFilter(self)

    def _build_tips(self):
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        for k, v in (("Enter", "粘贴到原处"), ("Ctrl+Enter", "仅复制"),
                     ("↑↓", "选择"), ("Tab", "文字/图片/全部"),
                     ("Del", "删除"), ("Esc", "关闭")):
            kb = QLabel(k)
            kb.setStyleSheet(tokenize(
                "color:#8AB4FF; font-size:12px; font-weight:600;"
                " background:rgba(51, 112, 255, 0.24); border-radius:4px; padding:1px 6px;"))
            tb = QLabel(v)
            tb.setStyleSheet(tokenize(
                "color:%s; font-size:12px; background:transparent;" % _WEAK))
            h.addWidget(kb)
            h.addWidget(tb, 0, Qt.AlignmentFlag.AlignVCenter)
        h.addStretch(1)
        self.btn_pin = QLabel("📌 未钉住")
        self.btn_pin.setStyleSheet(tokenize(
            "color:%s; font-size:12px; background:transparent;" % _WEAK))
        self.btn_pin.setToolTip("Shift+P 切换：钉住后失焦不自动收起")
        h.addWidget(self.btn_pin)
        return h

    # ---------- 呼出 / 收起 ----------
    def summon(self):
        """弹出并居中偏上；再按一次（已置顶活动时）收起。"""
        if self.isVisible() and self.isActiveWindow() and not self._pinned:
            self.hide()
            return
        # 抢在显示前记住房前台：show() 之后前台就变成我们自己了，句柄拿不到目标的
        self._target_hwnd = window_foreground.get_foreground_hwnd()
        self.view.reload()
        self._center_on_screen()
        self.show()
        self.raise_()
        self.activateWindow()
        window_foreground.raise_to_front(self)     # 过前台锁，真拿到键盘焦点
        self.view.search.setFocus()
        self._armed = False
        QTimer.singleShot(150, self._arm)

    def _arm(self):
        self._armed = True

    def _center_on_screen(self):
        scr = QApplication.primaryScreen()
        geo = scr.availableGeometry() if scr is not None else None
        if geo is None:
            return
        self.move(geo.x() + (geo.width() - self.width()) // 2,
                  geo.y() + int(geo.height() * 0.18))

    def changeEvent(self, e):
        """失焦收起（除非钉住）；与唤出面板同一口径，只在“站稳后”判。"""
        if (e.type() == QEvent.Type.ActivationChange and self._armed
                and not self._pinned and not self.isActiveWindow()):
            self.hide()
            return
        super().changeEvent(e)

    # ---------- 键盘分派 ----------
    def eventFilter(self, obj, e):
        if obj is self.view.search and e.type() == QEvent.Type.KeyPress:
            return self._dispatch_key(e)
        return super().eventFilter(obj, e)

    def _dispatch_key(self, e):
        k = e.key()
        mods = e.modifiers()
        if k in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            self.view.step(1 if k == Qt.Key.Key_Down else -1)
            return True
        if k == Qt.Key.Key_Tab or (k == Qt.Key.Key_Backtab):
            self.view.cycle_filter()
            return True
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            if ctrl:
                self._copy_current()
            else:
                self.view.activate_current()
            return True
        if k == Qt.Key.Key_Delete or k == Qt.Key.Key_Backspace:
            if k == Qt.Key.Key_Delete:
                self.view.delete_current()
                return True
            return False                       # Backspace 留给搜索框退格
        if k in (Qt.Key.Key_Escape,):
            self.hide()
            return True
        if k == Qt.Key.Key_P and bool(mods & Qt.KeyboardModifier.ShiftModifier):
            self._toggle_pin()
            return True
        if k in (Qt.Key.Key_Home, Qt.Key.Key_End):
            n = self.view.count()
            if n:
                self.view.set_current(0 if k == Qt.Key.Key_Home else n - 1)
            return True
        return False

    def _toggle_pin(self):
        self._pinned = not self._pinned
        self.btn_pin.setText("📌 已钉住" if self._pinned else "📌 未钉住")

    # ---------- 复制 / 粘贴动作 ----------
    def _content_to_clipboard(self, row):
        """把一条历史原样写回剪贴板；先 suppress_next 免得监听器重复入库。"""
        manager.suppress_next()
        cb = QApplication.clipboard()
        if row.get("kind") == "image":
            pm = QPixmap(row.get("path") or "")
            if pm.isNull():
                return False
            cb.setPixmap(pm)
        else:
            cb.setText(row.get("content") or "")
        return True

    def _copy_only(self, row):
        """仅复制回剪贴板（工具卡片单击 / 快贴 Ctrl+Enter）：不粘贴、不收窗也可。"""
        self._content_to_clipboard(row)

    def _copy_current(self):
        row = self.view.current_row()
        if row is not None:
            self._copy_only(row)

    def _paste_row(self, row):
        """回车/双击：复制内容 → 收起 → 焦点还给原窗 → 延后一拍模拟 Ctrl+V。"""
        if row is None or not self._content_to_clipboard(row):
            return
        self.hide()
        target = self._target_hwnd
        if target and window_foreground.focus_hwnd(target):
            # 焦点切换要落定再按键，太快发键目标窗还没拿到前台，Ctrl+V 会打进空气
            QTimer.singleShot(60, window_foreground.send_paste)
        # 拿不回原窗（被关了/非 Windows）：内容已在剪贴板，用户可自行 Ctrl+V

    # ---------- 状态提示（宿主/设置页读注册结果用） ----------
    def refresh_counts(self):
        return len(clipboard_store.list_items("image")), \
            len(clipboard_store.list_items("text"))


#: 进程内懒建单例：热键/托盘/工具卡片共用同一个窗（构造要 QApplication，不能在 import 时就建）
_INST = None


def instance():
    """懒建并复用同一个快贴窗；首次调用必须已在 QApplication 之后。"""
    global _INST
    if _INST is None:
        _INST = ClipboardQuickPanel()
    return _INST


def toggle(parent=None):
    """全局热键 / 托盘入口：呼出或收起快贴窗。"""
    instance().summon()
    return instance()
