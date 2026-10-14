"""
gui/clipboard_view.py —— 剪贴板历史的「可复用视图」：搜索 + 分栏过滤 + 标题正文行 + 侧边预览

为什么单开一层（而不是各写各的）：这套 UI 要在两个宿主里出现——
  · 「剪贴板历史」工具卡片（浅色、嵌在主窗里的常驻面板）；
  · Ditto 式全局快贴悬浮窗（深色无边框，热键直接呼出）。
两者配色/外框完全不同，但「怎么把一条历史画成 标题+正文、怎么搜索过滤、怎么
悬停出侧边预览」是同一套逻辑，收进本模块一份，两个宿主只差一个 dark 开关。

本层只依赖数据层 (store.clipboard_store) 与 Qt，不认识 clipboard_hist 的监听器：
「复制回剪贴板 / 粘贴到别处」由宿主决定（工具卡片只复制，快贴窗复制后还焦点+Ctrl+V），
视图只负责发 activateRequested 信号。
"""
from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QColor, QPalette, QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QToolButton, QButtonGroup, QSplitter, QFrame,
                               QSizePolicy, QPlainTextEdit, QScrollArea,
                               QAbstractItemView)

from store import clipboard_store
from store.app_state import get as _pref_get, set_value as _pref_set
from gui.theme import tokenize

#: 默认展示哪一栏（"text" / "image" / "all"）：用户点名默认文字优先
DEFAULT_VIEW_KEY = "clipboard_default_view"
_FILTERS = ("all", "text", "image")
_FILTER_LABEL = {"all": "全部", "text": "文字", "image": "图片"}

_THUMB = 46              # 列表行里图片缩略图边长
_BODY_MAX = 120          # 正文预览截断长度
_TITLE_MAX = 48


def default_view():
    v = str(_pref_get(DEFAULT_VIEW_KEY) or "text")
    return v if v in _FILTERS else "text"


def set_default_view(v):
    if v in _FILTERS:
        _pref_set(DEFAULT_VIEW_KEY, v)


def split_title_body(text):
    """正文 → (标题, 缩略正文)。标题取首行（过长截断），正文取其余行压成一段；
    只有一行的长文本则切前段当标题、剩下的当正文——与市面剪贴板管理器一致。"""
    text = str(text or "")
    if not text.strip():
        return "（空）", ""
    lines = text.splitlines()
    first = lines[0].strip()
    if len(lines) == 1:
        if len(first) <= _TITLE_MAX:
            return first, ""
        return first[:_TITLE_MAX], first[_TITLE_MAX:].strip()
    title = first if len(first) <= _TITLE_MAX else first[:_TITLE_MAX] + "…"
    body = " ".join(x.strip() for x in lines[1:] if x.strip())
    if len(body) > _BODY_MAX:
        body = body[:_BODY_MAX] + "…"
    return title, body


# --------------------------------------------------------------------
# 两套配色（浅色工具卡片 / 深色快贴悬浮窗）
# --------------------------------------------------------------------
def _palette(dark):
    if dark:
        return dict(
            text="#E8EAED", weak="#9AA4B0", accent="#3370FF", accent_text="#8AB4FF",
            row_bg="rgba(255, 255, 255, 0.05)", row_border="rgba(255, 255, 255, 0.10)",
            row_hover="rgba(255, 255, 255, 0.11)", cur_border="#3370FF",
            cur_bg="rgba(51, 112, 255, 0.24)", seg_bg="rgba(255, 255, 255, 0.06)",
            seg_checked="rgba(51, 112, 255, 0.28)", base="transparent",
            placeholder="#6B7480", line_bg="transparent")
    return dict(
        text="#1F2329", weak="#8F959E", accent="#3370FF", accent_text="#3370FF",
        row_bg="#F7F8FA", row_border="#E5E6EB", row_hover="#EFF1F5",
        cur_border="#3370FF", cur_bg="rgba(51, 112, 255, 0.12)", seg_bg="#F2F3F5",
        seg_checked="#3370FF", base="#FFFFFF", placeholder="#A6ABB3",
        line_bg="#FFFFFF")


class _ClipRow(QFrame):
    """列表里的一条：文字行「标题 + 缩略正文」上下排；图片行「缩略图 + 尺寸 + 时间」。

    走 objectName 选择器 + 动态属性 cur（改属性后 repolish 生效），避开应用级
    样式表把 QLabel/QListWidget 的默认样式盖过来。焦点一律不给，键盘始终归宿主。"""

    clicked = Signal(object)
    entered = Signal(object)
    doubleClicked = Signal(object)

    def __init__(self, row, pal, parent=None):
        super().__init__(parent)
        self.row = dict(row or {})
        self._pal = pal
        self.setObjectName("ClipRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        p = pal
        self.setStyleSheet(tokenize(
            "QFrame#ClipRow { background:%s; border:1px solid %s; border-radius:8px; }"
            "QFrame#ClipRow:hover { background:%s; }"
            "QFrame#ClipRow[cur=\"1\"] { border-color:%s; background:%s; }"
            % (p["row_bg"], p["row_border"], p["row_hover"],
               p["cur_border"], p["cur_bg"])))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(10)
        kind = self.row.get("kind")
        if kind == "image":
            lay.addWidget(self._thumb())
            lay.addLayout(self._text_block(
                self.row.get("preview") or "图片", self._image_meta()))
        else:
            title, body = split_title_body(self.row.get("content"))
            lay.addLayout(self._text_block(title, body))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def _thumb(self):
        lb = QLabel()
        lb.setFixedSize(_THUMB, _THUMB)
        lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        path = self.row.get("path") or ""
        pm = QPixmap(path) if path else QPixmap()
        if not pm.isNull():
            lb.setPixmap(pm.scaled(_THUMB, _THUMB, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation))
            lb.setStyleSheet(tokenize(
                "background:%s; border-radius:6px;" % self._pal["row_hover"]))
        else:
            lb.setText("🖼")
            lb.setStyleSheet(tokenize("font-size:20px; color:%s;" % self._pal["weak"]))
        return lb

    def _image_meta(self):
        created = (self.row.get("created_at") or "").split(" ")
        return created[-1] if len(created) > 1 else (self.row.get("created_at") or "")

    def _text_block(self, title, body):
        v = QVBoxLayout()
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        t = QLabel(title)
        t.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        t.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        t.setStyleSheet(tokenize(
            "color:%s; font-size:13px; font-weight:600; background:transparent;"
            % self._pal["text"]))
        t.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        v.addWidget(t)
        if body:
            b = QLabel(body)
            b.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            b.setStyleSheet(tokenize(
                "color:%s; font-size:12px; background:transparent;" % self._pal["weak"]))
            b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            v.addWidget(b)
        return v

    def set_current(self, on):
        self.setProperty("cur", "1" if on else "")
        st = self.style()
        st.unpolish(self)
        st.polish(self)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit(self)
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def enterEvent(self, e):
        self.entered.emit(self)
        super().enterEvent(e)


class _PreviewPane(QWidget):
    """右侧预览：文字给只读全文（可滚），图片给等比缩放大图。悬停/切换当前行时刷新。"""

    def __init__(self, pal, parent=None):
        super().__init__(parent)
        self._pal = pal
        self.setMinimumWidth(240)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)
        cap = QLabel("预览")
        cap.setStyleSheet(tokenize(
            "color:%s; font-size:12px; font-weight:600; background:transparent;"
            % pal["weak"]))
        lay.addWidget(cap)
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setStyleSheet(tokenize(
            "QPlainTextEdit { background:%s; color:%s; border:1px solid %s;"
            " border-radius:8px; font-size:13px; }"
            % (pal["base"], pal["text"], pal["row_border"])))
        self._img = QLabel()
        self._img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img.setStyleSheet(tokenize("color:%s; background:transparent;" % pal["weak"]))
        self._img.setVisible(False)
        lay.addWidget(self._text, 1)
        lay.addWidget(self._img, 1)
        self.show_item(None)

    def show_item(self, row):
        if row is None:
            self._text.setVisible(True)
            self._img.setVisible(False)
            self._text.setPlainText("")
            self._text.setPlaceholderText("把鼠标移到左侧条目上，这里显示完整内容")
            return
        if row.get("kind") == "image":
            pm = QPixmap(row.get("path") or "")
            self._text.setVisible(False)
            self._img.setVisible(True)
            if pm.isNull():
                self._img.setText("（图片文件已丢失）")
            else:
                avail = max(self._img.width(), 120)
                self._img.setPixmap(pm.scaled(
                    avail, 100000, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
        else:
            self._text.setVisible(True)
            self._img.setVisible(False)
            self._text.setPlainText(row.get("content") or "")
            self._text.setPlaceholderText("")


class ClipboardView(QWidget):
    """可复用剪贴板历史视图。宿主按 activateRequested 决定「复制回剪贴板」还是「粘贴到别处」。"""

    #: 用户对某一条“确认”（回车/双击/右键→粘贴）：宿主据此复制回或快贴
    activateRequested = Signal(object)         # row(dict)
    #: 需要把某一条原样复制回剪贴板但不粘贴（工具卡片单击 / 快贴 Ctrl+Enter）
    copyRequested = Signal(object)             # row(dict)
    #: 删除某一条（视图自己会从存储删并重画，顺带通知宿主）
    deleted = Signal(object)                   # row(dict)

    def __init__(self, dark=False, parent=None):
        super().__init__(parent)
        self._pal = _palette(dark)
        self._rows = []                 # 当前可见的行 dict 列表（过滤后）
        self._all = []                  # 全部历史（未过滤）
        self._cur = -1                  # 当前选中下标
        self._filter = default_view()   # all / text / image
        self.setStyleSheet(tokenize(
            "QWidget#ClipView { background:transparent; }"))
        self.setObjectName("ClipView")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        root.addLayout(self._build_head())

        self._split = QSplitter(Qt.Orientation.Horizontal)
        self._build_list()
        self._preview = _PreviewPane(self._pal)
        self._split.addWidget(self._list)
        self._split.addWidget(self._preview)
        self._split.setStretchFactor(0, 1)
        self._split.setStretchFactor(1, 1)
        self._split.setSizes([460, 400])
        root.addWidget(self._split, 1)

        self.reload()

    # ---- 顶部：搜索 + 过滤 ----
    def _build_head(self):
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        p = self._pal
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索剪贴板历史…")
        self.search.setStyleSheet(tokenize(
            "QLineEdit { background:%s; color:%s; border:1px solid %s;"
            " border-radius:8px; padding:6px 10px; font-size:13px; }"
            % (p["line_bg"], p["text"], p["row_border"])))
        pal = self.search.palette()
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(p["placeholder"]))
        self.search.setPalette(pal)
        self.search.textChanged.connect(self._apply_filter)
        h.addWidget(self.search, 1)
        # 三态分段：全部/文字/图片，选择同时写回“默认视图”偏好
        self._seg = QWidget()
        sh = QHBoxLayout(self._seg)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.setSpacing(2)
        self._seg_btns = {}
        grp = QButtonGroup(self._seg)
        grp.setExclusive(True)
        for mode in _FILTERS:
            b = QToolButton(self._seg)
            b.setText(_FILTER_LABEL[mode])
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setObjectName("ClipSeg")
            b.setChecked(mode == self._filter)
            b.clicked.connect(lambda _=False, m=mode: self.set_filter(m, persist=True))
            grp.addButton(b)
            sh.addWidget(b)
            self._seg_btns[mode] = b
        self._seg.setStyleSheet(tokenize(
            "QToolButton#ClipSeg { color:%s; background:%s; border:none;"
            " border-radius:6px; padding:5px 10px; font-size:12px; }"
            "QToolButton#ClipSeg:checked { color:%s; background:%s; }"
            % (p["text"], p["seg_bg"], p["accent_text"], p["seg_checked"])))
        h.addWidget(self._seg)
        return h

    # ---- 列表 ----
    def _build_list(self):
        self._list = QListWidget()
        p = self._pal
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setViewMode(QListWidget.ViewMode.ListMode)
        self._list.setUniformItemSizes(True)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._list.setMouseTracking(True)
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setStyleSheet(tokenize(
            "QListWidget { background:transparent; border:1px solid %s;"
            " border-radius:10px; }"
            "QListWidget::item { border:none; padding:0; }"
            "QListWidget::item:selected { background:transparent; }"
            % p["row_border"]))
        self._list.itemEntered.connect(self._on_entered)
        vp = self._list.viewport()
        vp.installEventFilter(self)

    # ---- 数据 ----
    def reload(self):
        """从存储重读全部历史，再按当前过滤/搜索重画。"""
        self._all = clipboard_store.list_items()
        self._apply_filter(self.search.text() if hasattr(self, "search") else "")

    def _match(self, row, q):
        if self._filter != "all" and row.get("kind") != self._filter:
            return False
        if not q:
            return True
        if row.get("kind") == "image":
            return q in (row.get("preview") or "") or q.lower() in "image"
        return q.lower() in (row.get("content") or "").lower()

    def _apply_filter(self, q=""):
        q = str(q or "").strip()
        self._rows = [r for r in self._all if self._match(r, q)]
        self._rebuild()

    def _rebuild(self):
        self._list.clear()
        w = max(self._list.viewport().width() - 4, 260)
        for r in self._rows:
            it = QListWidgetItem()
            row = _ClipRow(r, self._pal)
            it.setSizeHint(QSize(w, max(row.sizeHint().height(), 52)))
            self._list.addItem(it)
            self._list.setItemWidget(it, row)
            row.clicked.connect(lambda _w=row: self._on_click(_w))
            row.doubleClicked.connect(lambda _w=row: self._on_activate(_w))
            row.entered.connect(lambda _w=row: self._on_row_entered(_w))
        self._cur = 0 if self._rows else -1
        self._paint_cur()
        self._sync_preview()

    def _relayout_widths(self):
        """视口宽度变了就重算每行宽度（setItemWidget 只认 item 的 sizeHint 宽）。"""
        w = max(self._list.viewport().width() - 4, 260)
        for i in range(self._list.count()):
            it = self._list.item(i)
            h = it.sizeHint().height()
            it.setSizeHint(QSize(w, h))

    def eventFilter(self, obj, e):
        from PySide6.QtCore import QEvent
        if obj is self._list.viewport() and e.type() == QEvent.Type.Resize:
            self._relayout_widths()
        return super().eventFilter(obj, e)

    # ---- 选择 / 预览 ----
    def rows(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)

    def current_index(self):
        return self._cur

    def current_row(self):
        return self._rows[self._cur] if 0 <= self._cur < len(self._rows) else None

    def set_current(self, index):
        self._cur = max(-1, min(len(self._rows) - 1, index))
        self._paint_cur()
        self._sync_preview()
        if 0 <= self._cur < self._list.count():
            self._list.scrollToItem(self._list.item(self._cur),
                                    QAbstractItemView.ScrollHint.EnsureVisible)

    def step(self, delta):
        if not self._rows:
            return False
        base = self._cur if self._cur >= 0 else (0 if delta > 0 else len(self._rows) - 1)
        self.set_current(base + delta)
        return True

    def _paint_cur(self):
        for i in range(self._list.count()):
            w = self._list.itemWidget(self._list.item(i))
            if isinstance(w, _ClipRow):
                w.set_current(i == self._cur)

    def _sync_preview(self):
        self._preview.show_item(self.current_row())

    def _on_entered(self, item):
        self._preview.show_item(
            self._rows[self._list.row(item)]
            if 0 <= self._list.row(item) < len(self._rows) else None)

    def _on_row_entered(self, row):
        self._preview.show_item(row.row)

    def _on_click(self, row):
        idx = self._rows.index(row.row) if row.row in self._rows else -1
        if idx >= 0:
            self.set_current(idx)
        self.copyRequested.emit(row.row)

    def _on_activate(self, row):
        idx = self._rows.index(row.row) if row.row in self._rows else -1
        if idx >= 0:
            self.set_current(idx)
        self.activateRequested.emit(row.row)

    # ---- 对外动作 ----
    def activate_current(self):
        """宿主收到 Enter/双击时调用：把当前条“确认”出去。"""
        row = self.current_row()
        if row is not None:
            self.activateRequested.emit(row)
            return True
        return False

    def delete_current(self):
        """删除当前条（右键/按 Delete）：真删存储并重画。"""
        row = self.current_row()
        if row is None:
            return False
        clipboard_store.delete(row.get("id"))
        self.deleted.emit(row)
        self.reload()
        return True

    def cycle_filter(self):
        i = _FILTERS.index(self._filter)
        self.set_filter(_FILTERS[(i + 1) % len(_FILTERS)], persist=True)

    def set_filter(self, mode, persist=False):
        if mode not in _FILTERS:
            return
        self._filter = mode
        for m, b in self._seg_btns.items():
            b.setChecked(m == mode)
        if persist:
            set_default_view(mode)
        self._apply_filter(self.search.text())

    def filter_mode(self):
        return self._filter
