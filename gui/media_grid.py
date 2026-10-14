"""
gui/media_grid.py —— 共享「卡片网格」组件（成品库 / 素材库 / 混剪选择器共用）

把原先长在成品库页里的自绘卡片网格整体上收为通用组件：封面首帧（后台 ThumbWorker 抽）+
可上下滚动的标题 + 徽标行 + meta 行 + 左上角选择框，配合 QScrollArea+QGridLayout 自适应重排、
空白处橡皮筋框选即多选。行为与原成品库卡片网格一致（硬约束：成品库零回归）。

差异化用「回调/取值函数」参数化，组件本身不认识具体业务：
  - key_of(row)      → 选择集合用的唯一键（成品=path，素材=str(id)）
  - path_of(row)     → 抽首帧 / 预览用的文件路径
  - build_display(row)→ {name, tags, meta, tooltip, badge} 卡片文案（徽标/元信息由调用方给）
  - on_open(row)     → 单击（非多选态）打开预览
  - on_menu(row, global_pos) → 右键卡片菜单（回调里造菜单）
  - tile()         → 占位缩略图（缺省用 _thumb_tile）
组件内部维护多选开关、已勾选集合、缩略图缓存与后台抽取、橡皮筋框选与自适应重排。
"""
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QRect, QSize, QEvent, QPoint, Signal
from PySide6.QtGui import QPixmap, QColor, QPainter, QFont, QFontMetrics
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QScrollArea,
                               QGridLayout, QSizePolicy, QCheckBox, QRubberBand, QFrame)

from gui.theme import tokenize

# 可悬停预览的视频后缀（与 output_store.VID_EXT 同口径，额外兼容常见格式）
_VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv"}
# 悬停多久才弹预览（毫秒）：太短经过鼠标就弹、太长没反馈
_HOVER_MS = 600

# 卡片尺寸与网格间距（全库统一，素材库采用后即与成品库排版对齐）
CARD_W = 200
THUMB_H = 118
GRID_GAP = 14

# 卡片两种描边：普通（hover 变主蓝粗边 + 淡蓝底强调）/ 选中（主蓝粗边）
_CARD_STYLE = ("#MediaCard{background:#FFFFFF; border:1px solid #E5E6EB;"
               "border-radius:8px;}"
               "#MediaCard:hover{border:1px solid #3A6EE8; background:#F5F8FF;}")
_CARD_STYLE_SEL = ("#MediaCard{background:#F5F8FF; border:2px solid #3A6EE8;"
                   "border-radius:8px;}")
# 缩略图左上角的选择框：只留控件本身，不加任何底色贴片
_CHECK_STYLE = "QCheckBox{background:transparent;border:none;margin:6px;padding:2px;}"
# 右上角徽标位（如口播 🎙）：贴缩略图右上，无内容时隐藏
_BADGE_STYLE = ("QLabel{background:rgba(217,45,32,180); color:#FFFFFF;"
                "border-radius:4px;padding:1px 5px;font-size:11px;}")


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def _thumb_tile():
    """缩略占位：深色底 + 摄影机字形（不逐条解码首帧，扫描/刷新都很轻）。"""
    pm = QPixmap(CARD_W, THUMB_H)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#1F2329"))
    p.drawRoundedRect(0, 0, CARD_W, THUMB_H, 8, 8)
    p.setPen(QColor("#8A93A6"))
    f = QFont()
    f.setPointSize(30)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "\U0001F3AC")
    p.end()
    return pm


class MarqueeLabel(QWidget):
    """标题标签：静止时单行放不下就省略号；鼠标移入（由卡片 start_scroll 驱动）改成
    按宽度自动换行后「上下滚动」，几行滚完再回到顶部循环——很快就能读完长标题。
    自身不吃鼠标事件（透明于鼠标），进/出由外层卡片 enterEvent/leaveEvent 统一触发。"""

    def __init__(self, text="", bold=False, color="#1F2329", px=11, lines=2, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._text = str(text or "")
        self._color = color
        self._voff = 0
        self._scrolling = False
        self._lines = None          # 换行后的行列表（移入滚动时才有值）
        self._total = 0             # 换行后总高
        self._f = QFont()
        self._f.setPointSize(px)
        self._f.setBold(bold)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(QFontMetrics(self._f).height() * max(1, lines))

    def set_text(self, text):
        self._text = str(text or "")
        self._voff = 0
        self._scrolling = False
        self._lines = None
        self._timer.stop()
        self.update()

    def _fm(self):
        return QFontMetrics(self._f)

    def _wrap(self, width):
        """按字符贪心换行（名称多为 CJK/无空格，逐字排最稳）。"""
        fm = self._fm()
        out, cur = [], ""
        for ch in self._text:
            if ch == "\n":
                if cur:
                    out.append(cur)
                    cur = ""
                continue
            cand = cur + ch
            if fm.horizontalAdvance(cand) <= width or not cur:
                cur = cand
            else:
                out.append(cur)
                cur = ch
        if cur:
            out.append(cur)
        return out or [self._text]

    def start_scroll(self):
        # 卡片鼠标移入时调用：算好换行，行数超过可视高度才上下滚，否则静态显全部行
        if self.width() <= 0:
            return
        lh = self._fm().height()
        self._lines = self._wrap(max(1, self.width() - 2))
        self._total = len(self._lines) * lh
        if self._total > self.height() + 1:
            self._voff = 0
            self._scrolling = True
            self._timer.start()
        else:
            self._scrolling = False
            self._timer.stop()
        self.update()

    def stop_scroll(self):
        self._scrolling = False
        self._lines = None
        self._timer.stop()
        self.update()

    def _tick(self):
        maxoff = max(0, self._total - self.height())
        self._voff += 1
        if self._voff > maxoff:      # 滚到底回顶循环
            self._voff = 0
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setFont(self._f)
        p.setPen(QColor(self._color))
        fm = self._fm()
        w, h = self.width(), self.height()
        lh = fm.height()
        if self._lines is None:
            # 静止态：单行放不下用省略号
            tw = fm.horizontalAdvance(self._text)
            txt = fm.elidedText(self._text, Qt.TextElideMode.ElideRight, w - 2) \
                if tw > w - 2 else self._text
            p.drawText(0, 0, w, h,
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, txt)
        else:
            # 移入态：换行后上下滚（放不下裁剪平移；放得下整段居中显出）
            p.setClipRect(0, 0, w, h)
            y = -self._voff if self._scrolling else max(0, (h - self._total) // 2)
            for ln in self._lines:
                p.drawText(0, int(y), w, lh,
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, ln)
                y += lh
        p.end()


class _BadgeLabel(QLabel):
    """右上角徽标（如口播 🎙）的可交互版：有文本时鼠标移入/点击回吏信号；
    无文本时隐藏、收不到事件（纯装饰）。没人接收信号时等同普通 QLabel。"""
    clicked = Signal()
    hovered = Signal()

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            e.accept()
            return
        super().mousePressEvent(e)

    def enterEvent(self, e):
        self.hovered.emit()
        super().enterEvent(e)


class _HoverVideoPreview(QWidget):
    """悬停浮层静音预览：单个可复用的无边框小窗，盖在被悬停卡片的缩略图上，
    静音自动循环播；对鼠标透明（事件穿到下层卡片），移开卡片即停并隐藏。

    仅在真正悬停到有路径的视频时才惰性构造（避免离屏/无多媒体依赖场景平白实例化播放器）。"""

    def __init__(self):
        from PySide6.QtWidgets import QGraphicsView, QGraphicsScene, QVBoxLayout
        from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
        from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
        from PySide6.QtCore import QUrl, QSizeF
        super().__init__(None, Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._QUrl = QUrl
        self._QSizeF = QSizeF
        self._M = QMediaPlayer
        self.scene = QGraphicsScene(self)
        self.item = QGraphicsVideoItem()
        self.item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.scene.addItem(self.item)
        self.view = QGraphicsView(self.scene, self)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setStyleSheet("QGraphicsView{background:#000;border:none;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setMuted(True)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoSink(self.item.videoSink())
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(lambda *_: self.stop_hide())
        self._path = ""

    def play_at(self, path, global_pos, size):
        w, h = int(size.width()), int(size.height())
        self.setFixedSize(w, h)
        self.view.setFixedSize(w, h)
        self.item.setSize(self._QSizeF(w, h))
        self.scene.setSceneRect(0, 0, w, h)
        self.view.resetTransform()
        self._path = path
        self.move(global_pos)
        self.player.setSource(self._QUrl.fromLocalFile(path))
        self.player.play()
        self.show()
        self.raise_()

    def _on_status(self, st):
        if st == self._M.MediaStatus.Loaded:
            self.player.play()
        elif st == self._M.MediaStatus.EndOfMedia and self._path:
            self.player.setPosition(0)
            self.player.play()          # 循环看个片段

    def stop_hide(self):
        self._path = ""
        try:
            self.player.stop()
            self.player.setSource(self._QUrl())
        except Exception:
            pass
        self.hide()


class MediaCard(QFrame):
    """单张媒体卡：缩略占位/首帧 + 标题（滚动）+ 徽标行 + meta 行 + 左上选择框。

    整卡可点开（mouseRelease，非多选态）；多选态点任意处切换勾选；右键出操作菜单。
    文案全部来自 build_display（调用方按业务算好 name/tags/meta/tooltip/badge）。"""

    def __init__(self, row, display, *, key, on_open, on_menu, on_check,
                 multi_on, tile, on_preview=None, on_hover=None, on_hover_end=None,
                 on_badge=None, parent=None):
        super().__init__(parent)
        self.row = row
        self._key = key
        self._on_open = on_open
        self._on_menu = on_menu
        self._on_check = on_check
        self._on_preview = on_preview
        self._on_hover = on_hover
        self._on_hover_end = on_hover_end
        self._on_badge = on_badge
        self._is_multi = multi_on or (lambda: False)
        self.setObjectName("MediaCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(_CARD_STYLE)
        self.setFixedSize(CARD_W, THUMB_H + 112)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        # 缩略图、选择框、右上角徽标叠在同一格：框浮左上、徽标浮右上，不占额外行高
        wrap = QWidget()
        g = QGridLayout(wrap)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(0)
        self.thumb = QLabel()
        self.thumb.setFixedSize(CARD_W, THUMB_H)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setPixmap(tile())
        g.addWidget(self.thumb, 0, 0)
        self.chk = QCheckBox()
        self.chk.setStyleSheet(_CHECK_STYLE)
        self.chk.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chk.setToolTip("可勾选；也可在空白处按住鼠标拖动框选（右键空白→退出多选可关闭）")
        self.chk.toggled.connect(self._on_toggled)
        self.chk.setVisible(False)   # 多选默认关：选框藏起来，点「多选」后才出现
        g.addWidget(self.chk, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.badge = _BadgeLabel("", wrap)
        self.badge.setStyleSheet(_BADGE_STYLE)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setVisible(False)
        if on_badge is not None:
            # 徒标可交互：移入/点击都回吏（row, kind, 徒标控件）——页面据此弹脚本
            self.badge.clicked.connect(lambda: on_badge(self.row, "click", self.badge))
            self.badge.hovered.connect(lambda: on_badge(self.row, "hover", self.badge))
        v.addWidget(wrap)
        self._wrap = wrap
        name = display.get("name") or ""
        tags = display.get("tags") or ""
        meta = display.get("meta") or ""
        tooltip = display.get("tooltip") or name
        badge = display.get("badge") or ""
        if badge:
            self.set_badge(badge)
        # 名称：长名移入后自动换行上下滚动
        self._lbl_name = MarqueeLabel(name, bold=True, color="#1F2329", px=10)
        self._lbl_name.setToolTip(tooltip)
        self.thumb.setToolTip(tooltip)
        info = QWidget()
        iv = QVBoxLayout(info)
        iv.setContentsMargins(12, 8, 12, 10)
        iv.setSpacing(3)
        iv.addWidget(self._lbl_name)
        self.lbl_tags = QLabel(tags)
        self.lbl_tags.setStyleSheet(
            "font-size:11px; color:#7C5CFF; background:transparent; border:none;")
        iv.addWidget(self.lbl_tags)
        self.lbl_meta = QLabel(meta)
        self.lbl_meta.setStyleSheet(
            tokenize("font-size:11px; color:#8F959E; background:transparent; border:none;"))
        iv.addWidget(self.lbl_meta)
        v.addWidget(info, 1)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        # 徽标贴缩略图右上角（用绝对定位浮层；随卡片宽度固定在顶部右侧）
        if self.badge.isVisible():
            self.badge.adjustSize()
            bw = self.badge.sizeHint().width()
            self.badge.move(CARD_W - bw - 8, 8)

    def set_badge(self, text):
        self.badge.setText(text)
        self.badge.adjustSize()
        bw = self.badge.sizeHint().width()
        self.badge.move(CARD_W - bw - 8, 8)
        self.badge.setVisible(bool(text))
        self.badge.raise_()

    def enterEvent(self, e):
        self._lbl_name.start_scroll()
        if self._on_hover is not None:
            self._on_hover(self)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._lbl_name.stop_scroll()
        if self._on_hover_end is not None:
            self._on_hover_end(self)
        super().leaveEvent(e)

    def set_image(self, img):
        """后台抽好的首帧上色：按卡片尺寸铺满（多余裁掉），抽不到保留占位。"""
        if img is None or img.isNull():
            return
        pm = QPixmap.fromImage(img).scaled(
            CARD_W, THUMB_H, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation)
        self.thumb.setPixmap(pm)

    def _on_toggled(self, checked):
        self.setStyleSheet(_CARD_STYLE_SEL if checked else _CARD_STYLE)
        if self._on_check is not None:
            self._on_check(self._key, checked)

    def set_selected(self, checked):
        """回填选中态（重建卡片时按已选集合恢复）：屏蔽信号避免回调重复计数。"""
        self.chk.blockSignals(True)
        self.chk.setChecked(bool(checked))
        self.chk.blockSignals(False)
        self.setStyleSheet(_CARD_STYLE_SEL if checked else _CARD_STYLE)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            if self._is_multi():
                self.chk.toggle()   # 多选模式：整卡点击只切换勾选，不跳预览
            else:
                self._on_open(self.row)
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        self._on_menu(self.row, e.globalPos())

    def mouseDoubleClickEvent(self, e):
        # pick 模式：单击走勾选（多选常开），预览改用双击
        if self._on_preview is not None and e.button() == Qt.MouseButton.LeftButton:
            self._on_preview(self.row)
        super().mouseDoubleClickEvent(e)


class MediaGrid(QWidget):
    """卡片网格容器：持有一块 QScrollArea + 自绘卡片，管多选/框选/首帧/自适应重排。

    调用方通过构造参数注入业务取值/回调（见模块 docstring）。选择集合、多选开关、
    缩略图缓存都封在组件内；勾选/多选变化通过 on_selection_changed 通知调用方刷标签。"""

    def __init__(self, *, key_of, path_of, build_display, on_open, on_menu,
                 tile=_thumb_tile, on_selection_changed=None, open_on_double=False,
                 pick=False, hover_preview=False, on_badge=None, parent=None):
        super().__init__(parent)
        self._key_of = key_of
        self._path_of = path_of
        self._build_display = build_display
        self._on_open_cb = on_open
        self._on_menu_cb = on_menu
        self._tile = tile or _thumb_tile
        self._on_sel_changed = on_selection_changed or (lambda: None)
        self._pick = bool(pick)
        self._open_on_double = bool(open_on_double) or self._pick
        self._on_badge_cb = on_badge
        self._hover_enabled = bool(hover_preview)
        self._hover = None            # 懒建的共享悬停预览浮层（_HoverVideoPreview）
        self._hover_card = None       # 当前悬停目标卡
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(_HOVER_MS)
        self._hover_timer.timeout.connect(self._hover_fire)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._area = QScrollArea()
        self._area.setWidgetResizable(True)
        self._area.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self._holder = QWidget()
        self._grid = QGridLayout(self._holder)
        self._grid.setSpacing(GRID_GAP)
        self._grid.setContentsMargins(10, 14, 10, 14)
        self._area.setWidget(self._holder)
        lay.addWidget(self._area, 1)

        self._rows = []
        self._cards = []
        self._cols = 0
        self._multi = self._pick     # pick 模式：多选常开，只用于收集勾选
        self._selected = set()      # 勾选中的 key 集合（跨搜索/筛选保留）
        self._visible = []          # 当前渲染出来的 key（供「全选/计数」用）
        self._by_key = {}
        self._rubber = None
        self._rub_anchor = None
        self._holder.installEventFilter(self)
        self._holder.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._holder.customContextMenuRequested.connect(self._empty_menu_show)
        # 首帧缩略图：按 path 缓存已抽好的图，后台只补缺的；重建卡片时直接复用
        self._thumbs = {}
        self._by_path = {}
        from gui.thumb_cache import ThumbWorker
        self._thumb_worker = ThumbWorker(self)
        self._thumb_worker.ready.connect(self._on_thumb)
        self._thumb_worker.start()

    # ---------------- 视图切换的空白右键（可选） ----------------
    def set_empty_menu(self, factory):
        """空白处右键菜单工厂：callable()->QMenu（None 关掉）。由调用方按业务给。"""
        self._empty_menu_factory = factory
        self._holder.customContextMenuRequested.disconnect()
        if factory is not None:
            self._holder.customContextMenuRequested.connect(self._empty_menu_show)

    def _empty_menu_show(self, pos):
        factory = getattr(self, "_empty_menu_factory", None)
        if factory is None:
            return
        menu = factory()
        if menu is not None:
            menu.exec(self._holder.mapToGlobal(pos))

    # ---------------- 数据渲染 ----------------
    def set_rows(self, rows):
        """重建卡片：rows 已由调用方筛选/排序好；组件负责造卡、提交缺的封面、重排。"""
        self._hover_timer.stop()
        self._hide_hover()
        self._rows = list(rows or [])
        for c in self._cards:
            c.deleteLater()
        self._cards = []
        self._by_key = {}
        self._by_path = {}
        pending = []
        while self._grid.count():
            self._grid.takeAt(0)
        self._visible = []
        for r in self._rows:
            key = str(self._key_of(r))
            path = str(self._path_of(r) or "")
            display = self._build_display(r) or {}
            card = MediaCard(r, display, key=key, on_open=self._open,
                             on_menu=self._menu, on_check=self._handle_check,
                             multi_on=self.multi_on, tile=self._tile,
                             on_preview=(self._on_open_cb if self._open_on_double else None),
                             on_hover=(self._hover_enter if self._hover_enabled else None),
                             on_hover_end=(self._hover_leave if self._hover_enabled else None),
                             on_badge=self._on_badge_cb,
                             parent=self._holder)
            card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            card.set_selected(key in self._selected)
            card.chk.setVisible(self._multi)   # 重建卡片后保持多选开关态
            self._cards.append(card)
            self._by_key[key] = card
            self._visible.append(key)
            if path:
                self._by_path[path] = card
                img = self._thumbs.get(path)
                if img is not None:
                    card.set_image(img)
                else:
                    pending.append((path, r.get("_ts")))
        self._thumb_worker.submit(pending)
        self.relayout()

    def count(self):
        return len(self._cards)

    def _on_thumb(self, path, img):
        """后台抽好一帧：存缓存并给仍在屏上的对应卡片上色。"""
        if img is None or img.isNull():
            return
        self._thumbs[path] = img
        card = self._by_path.get(path)
        if card is not None:
            card.set_image(img)

    def stop_worker(self):
        self._hover_timer.stop()
        self._hide_hover()
        if self._hover is not None:
            self._hover.deleteLater()
            self._hover = None
        self._thumb_worker.request_stop()
        self._thumb_worker.wait(1500)

    def relayout(self):
        w = self._area.viewport().width()
        cols = max(1, (w - 8 + GRID_GAP) // (CARD_W + GRID_GAP))
        if cols != self._cols:
            self._cols = cols
        while self._grid.count():
            self._grid.takeAt(0)
        for i, card in enumerate(self._cards):
            self._grid.addWidget(card, i // cols, i % cols,
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._grid.setRowStretch(self._grid.rowCount(), 1)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self.relayout)

    # ---------------- 交互回调 ----------------
    def _open(self, row):
        if self._open_on_double:
            return                      # pick 模式：单击不预览，交勾选；预览走双击
        self._on_open_cb(row)

    def _menu(self, row, global_pos):
        self._on_menu_cb(row, global_pos)

    def _handle_check(self, key, checked):
        """卡片选框变化：更新已选集合并通知调用方刷标签。"""
        if checked:
            self._selected.add(key)
        else:
            self._selected.discard(key)
        self._on_sel_changed()

    # ---------------- 多选 / 选择集合 ----------------
    def multi_on(self):
        return self._multi

    def set_multi(self, on):
        """开/关多选：开 = 卡片出选框、点卡片任意位置即勾选；关 = 收起选框并清空。"""
        if bool(on) == self._multi:
            return
        self._multi = bool(on)
        for c in self._cards:
            c.chk.setVisible(self._multi)
        if not self._multi:
            self.clear_sel()
        self._on_sel_changed()

    def selected(self):
        return set(self._selected)

    def selected_rows(self):
        """已选且仍在当前数据里的行，按 set_rows 顺序（重命名序号按这个走）。"""
        return [r for r in self._rows if str(self._key_of(r)) in self._selected]

    def select_all(self):
        for k in self._visible:
            self._selected.add(k)
        for c in self._cards:
            c.set_selected(True)
        self._on_sel_changed()

    def clear_sel(self):
        self._selected.clear()
        for c in self._cards:
            c.set_selected(False)
        self._on_sel_changed()

    def invert(self):
        """反选：已勾的去掉、没勾的勾上（多选未开时先自动开启）。"""
        self.set_multi(True)
        for c in self._cards:
            c.chk.setChecked(not c.chk.isChecked())

    def discard_keys(self, keys):
        """把若干 key 移出已选集合（删除/还原后已消失的行，不残留脏选）。"""
        for k in (keys or []):
            self._selected.discard(str(k))

    def set_badge(self, key, text):
        """给仍在屏上的对应卡片设/清右上角徽标（异步后台完成后补 🎙，不重建卡片）。"""
        card = self._by_key.get(str(key))
        if card is not None:
            card.set_badge(text or "")

    # ---------------- 悬停静音预览 ----------------
    def _hover_enter(self, card):
        if not self._hover_enabled:
            return
        self._hover_card = card
        self._hover_timer.start()          # 重计防抖（卡片间来回移动只留最后一个）

    def _hover_leave(self, card):
        if not self._hover_enabled:
            return
        self._hover_timer.stop()
        if self._hover is not None and self._hover_card is card:
            self._hide_hover()

    def _hover_fire(self):
        card = self._hover_card
        if card is None or not card.isVisible():
            return
        path = str(self._path_of(card.row) or "")
        if (not path or Path(path).suffix.lower() not in _VIDEO_EXT
                or not Path(path).exists()):
            return
        if self._hover is None:
            try:
                self._hover = _HoverVideoPreview()
            except Exception:
                self._hover_enabled = False   # 多媒体不可用：静默关悬停，不再重试
                return
        self._hover.play_at(path, card.thumb.mapToGlobal(QPoint(0, 0)), card.thumb.size())

    def _hide_hover(self):
        self._hover_card = None
        if self._hover is not None:
            self._hover.stop_hide()

    def cancel_hover(self):
        """外部（如徒标弹脚本时）主动取消悬停预览，避免两个浮层叠在一起。"""
        self._hover_timer.stop()
        self._hide_hover()

    # ---------------- 橡皮筋框选 ----------------
    def eventFilter(self, obj, e):
        """卡片网格空白处按住拖动 = 橡皮筋框选，框到即自动开启多选并勾上；
        多选态下单击空白（没拖动）= 清空勾选。卡片自身点击由 MediaCard 分流。"""
        if obj is self._holder:
            t = e.type()
            if (t == QEvent.Type.MouseButtonPress
                    and e.button() == Qt.MouseButton.LeftButton):
                self._rub_anchor = e.position().toPoint()
                if self._rubber is None:
                    self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self._holder)
                self._rubber.setGeometry(QRect(self._rub_anchor, QSize()))
                self._rubber.show()
            elif (t == QEvent.Type.MouseMove and self._rubber is not None
                    and self._rubber.isVisible() and self._rub_anchor is not None):
                self._rubber.setGeometry(
                    QRect(self._rub_anchor, e.position().toPoint()).normalized())
                return True
            elif (t == QEvent.Type.MouseButtonRelease
                    and e.button() == Qt.MouseButton.LeftButton
                    and self._rubber is not None
                    and self._rub_anchor is not None):
                # 只收尾「一次由左键按下发起、还没消费掉」的框选手势；
                # 右键释放不得进来——否则会用残留脏 geometry 误判为「框到东西」而误开多选。
                rect = self._rubber.geometry()
                self._rubber.hide()
                self._rub_anchor = None
                if rect.width() <= 4 and rect.height() <= 4:
                    if self._multi:      # 单击空白：开着多选就清空
                        self.clear_sel()
                else:
                    self.set_multi(True)  # 框选即多选
                    for c in self._cards:
                        c.chk.setChecked(c.geometry().intersects(rect))
                return True
        return super().eventFilter(obj, e)
