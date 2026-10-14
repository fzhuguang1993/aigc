"""
gui/clipboard_hist.py —— 剪贴板历史：后台监听 + 分栏回看面板

两块职责：
  ClipboardMonitor  监听系统剪贴板 dataChanged，把复制过的文字/图片自动入库
                    （文字进 DB，图片落盘 CLIPBOARD_DIR）；带防抖、去重、自我回环抑制。
  ClipboardPanel    工具卡片面板：做成与快贴悬浮窗同款的深色面板，内嵌可复用
                    ClipboardView（搜索 / 全・文・图过滤 / 标题正文行 / 侧边悬停预览），
                    单击复制回剪贴板、删除选中、一键清空，并给一行快贴全局快捷键。

线程纪律：QClipboard 只能在主线程访问，监听全程跑在 GUI 线程，不起工作线程。
"""
import hashlib
import os

from PySide6.QtCore import (Qt, QObject, QTimer, QByteArray, QBuffer, QIODevice,
                            QSize, Signal)
from PySide6.QtGui import QPixmap, QKeySequence
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QKeySequenceEdit, QMessageBox,
                               QApplication)

from core.config import CLIPBOARD_DIR
from gui.clipboard_view import ClipboardView
from gui.header import page_header
from store import clipboard_store
from store.app_state import get as _pref_get, set_value as _pref_set

#: 监听开关（持久化，默认开：装了这功能就是想要"复制过就能找回"）
MONITOR_KEY = "clipboard_monitor_enabled"
#: 同一次复制可能连发多次 dataChanged；短延时合并成一条
DEBOUNCE_MS = 120


def monitor_enabled():
    return bool(_pref_get(MONITOR_KEY, True))


def set_monitor_enabled(on):
    _pref_set(MONITOR_KEY, bool(on))


def _img_signature(pm: QPixmap):
    """图片去重签名：(kind, w, h, sha1(编码字节))。相同图相同签名，不同图不误判。"""
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pm.save(buf, "PNG")
    buf.close()
    digest = hashlib.sha1(bytes(ba.data())).hexdigest()
    return ("image", pm.width(), pm.height(), digest)


class ClipboardMonitor(QObject):
    """系统剪贴板后台监听器（进程内单例 manager）。"""

    #: 新入库一条（"image"/"text"）：面板据此实时刷新，不用手动点刷新
    captured = Signal(str)

    def __init__(self):
        super().__init__()
        self._last_sig = None
        self._suppress = False
        self._connected = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)

    # ---- 挂/断信号 ----
    def start(self):
        if not monitor_enabled():
            return
        cb = QApplication.clipboard()
        if not self._connected:
            cb.dataChanged.connect(self._on_changed)
            self._connected = True
        # 用最新一条历史做种子：应用启动时剪贴板里往往还躺着上次复制的内容，
        # 一旦有事Changed 命中种子就不会把它重复入库。
        self._last_sig = clipboard_store.last_signature()
        try:
            from core.logger import log
            log.info("剪贴板历史监听已启动")
        except Exception:
            pass

    def stop(self):
        if self._connected:
            try:
                QApplication.clipboard().dataChanged.disconnect(self._on_changed)
            except (TypeError, RuntimeError):
                pass
            self._connected = False
        self._timer.stop()

    # ---- 触发 ----
    def _on_changed(self):
        if self._suppress:
            self._suppress = False
            return
        self._timer.start(DEBOUNCE_MS)     # 合并连发

    def _flush(self):
        cb = QApplication.clipboard()
        # PySide6 的 QClipboard 没有 hasImage()/hasText()（那是 Swing 的记忆）：
        # 图片用 image().isNull() 判，文字直接取 text() 判空。写错一次吞一次
        # AttributeError，表现就是“监听明明起了、复制却永远不进库”。
        try:
            img = cb.image()
            if img is not None and not img.isNull():
                pm = QPixmap.fromImage(img)
                sig = _img_signature(pm)
                if sig == self._last_sig:
                    return
                clipboard_store.add_image(pm)
                self._last_sig = sig
                self.captured.emit("image")
                return
            text = cb.text() or ""
            if not text.strip():
                return
            sig = ("text", text)
            if sig == self._last_sig:
                return
            clipboard_store.add_text(text)
            self._last_sig = sig
            self.captured.emit("text")
        except Exception as e:                       # 静默失败最难查：进库报错至少留一行
            try:
                from core.logger import log
                log.warning("剪贴板入库失败：%s" % e)
            except Exception:
                pass

    # ---- 外部写入协作 ----
    def note_external_copy(self, pm: QPixmap):
        """截图「复制」这类我们主动写剪贴板的图：先入库、记签名，并置一次
        自回环抑制。调用方必须在写剪贴板（setPixmap）**之前**调本方法——
        setPixmap 触发的 dataChanged 会被 _on_changed 命中 _suppress 直接吞掉，
        不会再走一遍 _flush。\n
        为什么不能只靠签名去重：高 DPI 下 setPixmap 后回读 cb.image() 再
        QPixmap.fromImage，尺寸与编码字节都与原图不同，_img_signature 对不上，
        于是同一次复制被入两条——就是“截图复制一次、历史里两张图”的成因。\n
        兜底计时：若这次写其实没改动剪贴板（内容与上次相同、不触发
        dataChanged），_suppress 会一直挂着吃掉下一次真复制，所以隔一会儿自己清掉。"""
        if pm is None or pm.isNull():
            return
        clipboard_store.add_image(pm)
        self._last_sig = _img_signature(pm)
        self._suppress = True         # 我们自己写剪贴板触发的 dataChanged：别再入一次
        QTimer.singleShot(DEBOUNCE_MS * 4, self._clear_suppress)
        self.captured.emit("image")

    def note_external_copy_text(self, text):
        """截图「识图取字」这类我们主动写进剪贴板的文字：先入库、记签名，
        并置一次自回环抑制。调用方必须在 setText 之前调本方法——setText
        触发的 dataChanged 会被 _on_changed 命中 _suppress 直接吞掉，不会再
        走一遍 _flush（与 note_external_copy 对图片的处理完全同款）。兜底计时同
        理：写空/内容未变不触发 dataChanged 时，隔一会儿自清 _suppress。"""
        t = (text or "").strip()
        if not t:
            return
        clipboard_store.add_text(t)
        self._last_sig = ("text", t)
        self._suppress = True
        QTimer.singleShot(DEBOUNCE_MS * 4, self._clear_suppress)
        self.captured.emit("text")

    def _clear_suppress(self):
        self._suppress = False

    def suppress_next(self):
        """面板把历史条目复制回剪贴板时调用：吞掉紧随的那一次 dataChanged，
        避免把已有的一条重新入库。"""
        self._suppress = True


#: 进程内单例：主窗口启动时 start()，退出时 stop()
manager = ClipboardMonitor()


# ====================================================================
# 历史面板
# ====================================================================
# 深色面板配色：刻意与 Ditto 式快贴悬浮窗（clipboard_quick）同一族，好让
# 「功能列表点进来的剪贴板」和「快捷键呼出的快贴窗」看起来是同一个界面。
# 直接写死十六进制而不走 theme 令牌：主题种子偏浅，会把深色映回浅色的。
_CARD_BG = "#1C2026"
_CARD_BORDER = "#2A2F36"
_D_TEXT = "#E8EAED"
_D_WEAK = "#9AA4B0"
_D_BTN_BG = "rgba(255, 255, 255, 0.06)"
_D_BTN_HOVER = "rgba(255, 255, 255, 0.12)"
_D_ACCENT = "#3370FF"


class ClipboardPanel(QWidget):
    """剪贴板历史工具卡片：整块做成与快贴悬浮窗同款的深色面板——内嵌
    ClipboardView(dark=True)（搜索 + 全/文/图过滤 + 标题正文行 + 侧边悬停预览），
    深色工具条给监听开关、刷新、删除选中、清盘、打开目录，另配一行「快贴快捷键」
    就地改全局呼出键。面板嵌在主窗里、焦点本就属于我们，单击/回车都只复制回剪贴板
    （不存在“贴回别的软件”，所以不接管粘贴）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 8, 18, 14); root.setSpacing(10)
        root.addWidget(page_header("剪贴板历史",
                    "后台记录复制过的文字与图片；按快捷键可呼出快贴悬浮窗，选中一条即复制回剪贴板"))

        # ---- 深色卡片：与快贴窗同款外观（视图透明底，靠这块深色底透出）----
        card = QWidget()
        card.setObjectName("ClipDarkCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card.setStyleSheet(
            "QWidget#ClipDarkCard { background:%s; border:1px solid %s;"
            " border-radius:12px; }" % (_CARD_BG, _CARD_BORDER))
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12); cl.setSpacing(10)

        # ---- 主体视图（深色版，与快贴窗同一 dark 开关）：先建，顶栏按钮要引用它的方法 ----
        self.view = ClipboardView(dark=True, parent=card)
        self.view.activateRequested.connect(self._copy_back)
        self.view.copyRequested.connect(self._copy_back)

        cl.addLayout(self._build_toolbar())
        cl.addLayout(self._build_hotkey_row())
        cl.addWidget(self.view, 1)
        root.addWidget(card, 1)

        # 同步开关显示态（不触发 toggled 副作用）
        self._sync_mon_btn()

        # 监听器是进程单例；面板可能反复开关，信号直接接 self._on_captured（
        # PySide 会在面板销毁时自动断开对它的连接，不会泄漏野指针回调）。
        manager.captured.connect(self._on_captured)
        self.refresh()

    # ---- 深色小按钮（贴齐快贴窗的胶囊样式，不用浅色 GhostBtn）----
    def _dark_btn(self, text, tip="", checkable=False):
        b = QPushButton(text)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if tip:
            b.setToolTip(tip)
        b.setCheckable(checkable)
        b.setStyleSheet(
            "QPushButton { color:%s; background:%s; border:1px solid %s;"
            " border-radius:7px; padding:5px 12px; font-size:12px; }"
            "QPushButton:hover { background:%s; }"
            "QPushButton:checked { background:%s; color:#FFFFFF; border-color:%s; }"
            % (_D_TEXT, _D_BTN_BG, _CARD_BORDER, _D_BTN_HOVER, _D_ACCENT, _D_ACCENT))
        return b

    def _build_toolbar(self):
        head = QHBoxLayout(); head.setSpacing(8)
        self.chk_mon = self._dark_btn("● 监听中", "开/关后台监听", checkable=True)
        self.chk_mon.toggled.connect(self._toggle_monitor)
        head.addWidget(self.chk_mon)
        for text, tip, slot in (("↻ 刷新", "重新读取历史", self.view.reload),
                                ("🗑 删除选中", "删除当前高亮这一条", self.view.delete_current),
                                ("📂 打开图片目录", "查看落盘的截图", self._open_dir),
                                ("🗑 清空全部", "删除全部剪贴板历史（含落盘图片）", self._clear)):
            b = self._dark_btn(text, tip)
            b.clicked.connect(slot)
            head.addWidget(b)
        head.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet(
            "color:%s; font-size:12px; background:transparent;" % _D_WEAK)
        head.addWidget(self.lbl_count)
        return head

    def _sync_mon_btn(self):
        on = monitor_enabled()
        self.chk_mon.blockSignals(True)
        self.chk_mon.setChecked(on)
        self.chk_mon.setText("● 监听中" if on else "○ 已暂停")
        self.chk_mon.blockSignals(False)

    # ---- 快捷键行（与设置页共用 tools_registry 那一份事实）----
    def _build_hotkey_row(self):
        from gui.tools_registry import clipboard_shortcut
        h = QHBoxLayout()
        h.setSpacing(8)
        cap = QLabel("🔥 快贴快捷键")
        cap.setStyleSheet("color:%s; font-size:12px; background:transparent;" % _D_TEXT)
        h.addWidget(cap)
        self.edit_hotkey = QKeySequenceEdit()
        self.edit_hotkey.setKeySequence(QKeySequence(clipboard_shortcut()))
        self.edit_hotkey.setFixedWidth(160)
        self.edit_hotkey.setClearButtonEnabled(False)
        self.edit_hotkey.setStyleSheet(
            "QKeySequenceEdit, QLineEdit { color:%s; background:%s;"
            " border:1px solid %s; border-radius:7px; padding:4px 8px; font-size:12px;"
            " selection-background-color:%s; }"
            % (_D_TEXT, _D_BTN_BG, _CARD_BORDER, _D_ACCENT))
        self.edit_hotkey.keySequenceChanged.connect(self._save_hotkey)
        h.addWidget(self.edit_hotkey)
        b = self._dark_btn("清除")
        b.clicked.connect(self._clear_hotkey)
        h.addWidget(b)
        self.lbl_hotkey = QLabel("")
        self.lbl_hotkey.setStyleSheet(
            "color:%s; font-size:12px; background:transparent;" % _D_WEAK)
        h.addWidget(self.lbl_hotkey)
        h.addStretch(1)
        return h

    def _save_hotkey(self, seq):
        from gui.tools_registry import set_clipboard_shortcut
        text = QKeySequence(seq).toString() if seq else ""
        if not text:
            return                       # 清空交给「清除」按钮，别在这儿误写空值
        eff = set_clipboard_shortcut(text)
        self.edit_hotkey.blockSignals(True)
        self.edit_hotkey.setKeySequence(QKeySequence(eff))
        self.edit_hotkey.blockSignals(False)
        self.lbl_hotkey.setText("✓ 已注册：按 %s 可在任意软件呼出快贴" % eff)

    def _clear_hotkey(self):
        from gui.tools_registry import set_clipboard_shortcut
        self.edit_hotkey.clear()
        set_clipboard_shortcut("")
        self.lbl_hotkey.setText("已清除全局快捷键")

    def _on_captured(self, _kind=None):
        self.refresh()          # 剪贴View 只读存储，重画一次就带上新复制的

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()          # 重新打开（复用缓存面板）时补上期间新复制的

    # ---- 数据 ----
    def refresh(self):
        self.view.reload()
        images = clipboard_store.list_items("image")
        texts = clipboard_store.list_items("text")
        self.lbl_count.setText("图片 %d · 文字 %d" % (len(images), len(texts)))

    # ---- 交互 ----
    def _copy_back(self, row):
        """把一条历史原样写回剪贴板；先 suppress_next 免得监听器把已有条目重复入库。"""
        if not row:
            return
        manager.suppress_next()
        cb = QApplication.clipboard()
        if row.get("kind") == "image":
            pm = QPixmap(row.get("path") or "")
            if pm.isNull():
                QMessageBox.warning(self, "复制失败", "图片文件已不存在")
                return
            cb.setPixmap(pm)
        else:
            cb.setText(row.get("content") or "")

    def _clear(self):
        if QMessageBox.question(self, "清空历史",
                                "确定删除全部剪贴板历史（含落盘图片）？",
                                QMessageBox.StandardButton.Yes
                                | QMessageBox.StandardButton.No
                                ) != QMessageBox.StandardButton.Yes:
            return
        clipboard_store.clear_all()
        self.refresh()

    def _open_dir(self):
        from utils.desktop_utils import open_path
        try:
            os.makedirs(CLIPBOARD_DIR, exist_ok=True)
        except OSError:
            pass
        open_path(CLIPBOARD_DIR)

    def _toggle_monitor(self, on):
        set_monitor_enabled(on)
        self.chk_mon.setText("● 监听中" if on else "○ 已暂停")
        if on:
            manager.start()
        else:
            manager.stop()
