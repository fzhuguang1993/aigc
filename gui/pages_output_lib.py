"""
gui/pages_output_lib.py —— 成品库（生成产物 + 混剪结果，卡片式浏览）

对标「拆解任务」页的卡片形态：每条成品一张卡（缩略占位 / 文件名 / 产品徽标 /
审片角标 / 大小·日期），点卡片预览、右键可 预览 / 定位 / 绑定产品 / 标记 / 送混剪。
数据源 store.output_store（扫盘只读视图，永远与磁盘一致）；产品归属优先「人工绑定」
（右键绑定，落 output_binds 表），其次自动 join（runs/tasks）。可按 产品（含未分类）
/ 标记 / 名称 搜索筛选。

顶部标注当前存储后端（本轮本地）。整目录扫描不便宜，定时刷新节流到每 6 秒一次，
并靠「行签名」判是否真变了，没变就不重建卡片（防闪烁/防抖）。
"""
import time

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QRect, QSize, QEvent
from PySide6.QtGui import QPixmap, QColor, QPainter, QFont, QFontMetrics
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QComboBox, QLineEdit, QScrollArea, QGridLayout,
                               QSizePolicy, QMessageBox, QFrame, QDialog,
                               QListWidget, QListWidgetItem, QAbstractItemView, QCheckBox,
                               QRubberBand, QInputDialog)

from store import output_store
from utils.desktop_utils import open_path, reveal_in_folder
from gui.header import page_header
from gui.menus import StyledMenu
from gui.widgets import VideoPlayerDialog
from gui.theme import tokenize

_AUTO_SEC = 6.0      # 定时刷新最小间隔（整目录扫描不便宜，别 2 秒刷一次）
CARD_W = 200
THUMB_H = 118
GRID_GAP = 14

_MARK_LABEL = {"ok": "🟢可用", "bad": "🔴不可用"}

# 右键「排序」子菜单项：生成日期（文件时间）/ 名称，两个方向都可切
_SORT_ITEMS = (("time_desc", "生成日期（新→旧）"), ("time_asc", "生成日期（旧→新）"),
               ("name_asc", "名称（A→Z）"), ("name_desc", "名称（Z→A）"))

# 卡片两种描边：普通（hover 变主蓝粗边 + 淡蓝底做强调）/ 选中（主蓝粗边）
_CARD_STYLE = ("#OlCard{background:#FFFFFF; border:1px solid #E5E6EB;"
               "border-radius:8px;}"
               "#OlCard:hover{border:1px solid #3A6EE8; background:#F5F8FF;}")
_CARD_STYLE_SEL = ("#OlCard{background:#F5F8FF; border:2px solid #3A6EE8;"
                   "border-radius:8px;}")
# 缩略图左上角的选择框：只留控件本身，不加任何底色贴片（用户反馈：周围那圈半透白不要）
_CHECK_STYLE = "QCheckBox{background:transparent;border:none;margin:6px;padding:2px;}"

# 顶部「成品库 / 回收站」分段切换：选中高亮主蓝，未选灰底
_VIEW_ON = ("QPushButton{background:#3A6EE8;color:#FFFFFF;border:none;"
            "border-radius:6px;padding:4px 12px;font-weight:600;}")
_VIEW_OFF = ("QPushButton{background:#F2F3F5;color:#4E5969;"
             "border:1px solid #E5E6EB;border-radius:6px;padding:4px 12px;}")


def _fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


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


class _MarqueeLabel(QWidget):
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
        """按字符贪心换行（成品名多为 CJK/无空格，逐字排最稳）。"""
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


class _OutputCard(QFrame):
    """单张成品卡：缩略占位 + 文件名/产品徽标/标记·大小·日期。

    整卡可点开预览（mouseRelease）；右键出操作菜单。产品徽标：人工绑定标 🖇、
    自动归属直接显名、都没有显「未分类」。"""

    def __init__(self, row, on_open, on_menu, on_check=None, is_multi=None,
                 show_physical=False, trash=False, parent=None):
        super().__init__(parent)
        self.row = row
        self._path = row.get("path") or ""
        self._on_open = on_open
        self._on_menu = on_menu
        self._on_check = on_check
        # 多选是否开启由页面提供：开启时点卡片任意位置只切换勾选，不再跳播放
        self._is_multi = is_multi or (lambda: False)
        self.setObjectName("OlCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(_CARD_STYLE)
        self.setFixedSize(CARD_W, THUMB_H + 112)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        # 缩略图与选择框叠在同一格：框浮在左上角，不占额外行高
        wrap = QWidget()
        g = QGridLayout(wrap)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(0)
        self.thumb = QLabel()
        self.thumb.setFixedSize(CARD_W, THUMB_H)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setPixmap(_thumb_tile())
        g.addWidget(self.thumb, 0, 0)
        self.chk = QCheckBox()
        self.chk.setStyleSheet(_CHECK_STYLE)
        self.chk.setCursor(Qt.CursorShape.PointingHandCursor)
        self.chk.setToolTip("可勾选；也可在空白处按住鼠标拖动框选（右键空白→退出多选可关闭）")
        self.chk.toggled.connect(self._on_toggled)
        self.chk.setVisible(False)   # 多选默认关：选框藏起来，点「多选」后才出现
        g.addWidget(self.chk, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        v.addWidget(wrap)
        r = row
        # 名称：物理名带后缀，外显名去后缀（或用户自定义）；长名移入后自动换行上下滚动
        if show_physical:
            name = r.get("name") or Path(r.get("path") or "").name
        else:
            name = output_store.default_display(r) or (r.get("name") or "")
        if trash:
            tags = "🗑 已在回收站"
        else:
            prod = r.get("product") or ""
            tags = (("🖇 " if r.get("bound") else "🏷 ") + prod) if prod else "🏷 未分类"
        flag = _MARK_LABEL.get(r.get("mark"), "")
        meta = (f"{_fmt_size(r.get('size'))} · {(r.get('created_at') or '')[5:16]}"
                + (f" · {flag}" if flag else ""))
        # 下方文字：左右留白加大（原来 10px 太挤），名称单独一行用上下滚动标题控件
        info = QWidget()
        iv = QVBoxLayout(info)
        iv.setContentsMargins(12, 8, 12, 10)
        iv.setSpacing(3)
        self._lbl_name = _MarqueeLabel(name, bold=True, color="#1F2329", px=10)
        self._lbl_name.setToolTip(name)
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

    def enterEvent(self, e):
        # 鼠标移入：长名自动换行上下滚动（CSS :hover 同时做描边/底色强调）
        self._lbl_name.start_scroll()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._lbl_name.stop_scroll()
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
            self._on_check(self._path, checked)

    def set_selected(self, checked):
        """页面回填选中态（重建卡片时按已选集合恢复）：屏蔽信号避免回调重复计数。"""
        self.chk.blockSignals(True)
        self.chk.setChecked(bool(checked))
        self.chk.blockSignals(False)
        self.setStyleSheet(_CARD_STYLE_SEL if checked else _CARD_STYLE)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            if self._is_multi():
                self.chk.toggle()   # 多选模式：整卡点击只切换勾选，不跳播放
            else:
                self._on_open(self.row)
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        self._on_menu(self.row, e.globalPos())


class OutputLibPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(8)
        lay.addWidget(page_header("成品库",
                                  "点卡片预览 · 空白处拖动框选即多选 · 右键卡片：移入回收站/设显示名 · 右键空白：多选/全选/反选/排序",
                                  icon="📦"))

        bar = QHBoxLayout()
        bar.setSpacing(8)
        # 页内视图切换：成品库 / 回收站（同一卡片网格复用；回收站态卡片给还原/彻底删除）
        self.b_view_lib = QPushButton("📦 成品库")
        self.b_view_lib.setCursor(Qt.CursorShape.PointingHandCursor)
        self.b_view_lib.clicked.connect(lambda: self._set_view("lib"))
        self.b_view_trash = QPushButton("🗑 回收站")
        self.b_view_trash.setCursor(Qt.CursorShape.PointingHandCursor)
        self.b_view_trash.clicked.connect(lambda: self._set_view("trash"))
        bar.addWidget(self.b_view_lib)
        bar.addWidget(self.b_view_trash)
        bar.addSpacing(10)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索文件名…")
        self.ed_search.setFixedWidth(200)
        self.ed_search.textChanged.connect(lambda _=None: self._render())
        bar.addWidget(self.ed_search)
        bar.addWidget(QLabel("产品："))
        self.lbl_product = bar.itemAt(bar.count() - 1).widget()
        self.cb_product = QComboBox()
        self.cb_product.currentIndexChanged.connect(lambda _=None: self._reload(force=True))
        bar.addWidget(self.cb_product)
        bar.addWidget(QLabel("标记："))
        self.lbl_mark = bar.itemAt(bar.count() - 1).widget()
        self.cb_mark = QComboBox()
        self.cb_mark.addItems(["全部", "可用", "不可用", "未标记"])
        self._mark_val = {"可用": "ok", "不可用": "bad", "未标记": "none"}
        self.cb_mark.currentIndexChanged.connect(lambda _=None: self._reload(force=True))
        bar.addWidget(self.cb_mark)
        bar.addStretch(1)
        # ---------- 多选入口不在顶栏：框选自动开启，右键空白处可开关多选/全选/反选 ----------
        self.lbl_sel = QLabel("已选 0 条")
        self.lbl_sel.setObjectName("PageTip")
        self.lbl_sel.setVisible(False)
        bar.addWidget(self.lbl_sel)
        # 操作入口：主题里默认 QPushButton 就是实心蓝（显眼）；setMenu 会自动画箭头，
        # 文本里别再手写 ▾，否则两颗箭头
        self.b_ops = QPushButton("⚙ 操作")
        self._ops_menu = StyledMenu(self)
        self._ops_menu.aboutToShow.connect(self._rebuild_ops_menu)
        self.b_ops.setMenu(self._ops_menu)
        bar.addWidget(self.b_ops)
        self.lbl_hint = QLabel("")
        self.lbl_hint.setObjectName("PageTip")
        bar.addWidget(self.lbl_hint)
        b_ref = QPushButton("🔄 刷新")
        b_ref.setObjectName("GhostBtn")
        b_ref.clicked.connect(lambda: self._reload(force=True))
        bar.addWidget(b_ref)
        b_root = QPushButton("📂 打开成品库目录")
        b_root.setObjectName("GhostBtn")
        b_root.clicked.connect(self._open_root)
        bar.addWidget(b_root)
        lay.addLayout(bar)

        self._area = QScrollArea()
        self._area.setWidgetResizable(True)
        self._area.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self._holder = QWidget()
        self._grid = QGridLayout(self._holder)
        self._grid.setSpacing(GRID_GAP)
        self._grid.setContentsMargins(10, 14, 10, 14)
        self._area.setWidget(self._holder)
        lay.addWidget(self._area, 1)

        self._lbl_empty = QLabel("成品库还没有内容：生成任务产出、或到「🎬 AI 混剪」导出，都会落在这里。")
        self._lbl_empty.setObjectName("PageTip")
        self._lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_empty.setWordWrap(True)
        self._lbl_empty.setVisible(False)
        lay.addWidget(self._lbl_empty)

        self._rows = []
        self._cards = []
        self._cols = 0
        self._sig = None
        self._media = None
        self._last_auto = 0.0
        # 多选：跨搜索/筛选保留已选路径；当前可见路径（供「全选/计数」用）
        self._multi = False
        self._sort = "time_desc"   # 默认排序：生成日期新→旧
        self._view = "lib"          # lib=成品库 / trash=回收站（页内视图切换）
        self._show_physical = False  # 卡片底部名称：False=外显名(去后缀/自定义)，True=物理名(带后缀)
        self._selected = set()
        self._visible = []
        # 鼠标框选（卡片网格没有原生橡皮筋，手写）：空白处拖动即自动开启多选
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
        self.rebuild_filters()
        self._update_view_buttons()
        self._reload(force=True)

    # ---------------- 数据 ----------------
    def current_backend(self):
        try:
            from core.config import storage_config
            return str(storage_config().get("backend") or "local")
        except Exception:
            return "local"

    def rebuild_filters(self):
        """产品下拉 = 全部 / 未分类 / 库里出现过的产品（保留当前选择）。"""
        self._fill(self.cb_product, ["未分类"] + output_store.distinct_products())

    @staticmethod
    def _fill(combo, values):
        cur = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("全部")
        for v in values:
            combo.addItem(v)
        idx = combo.findText(cur)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _picked_product(self):
        t = self.cb_product.currentText()
        if t == "未分类":
            return output_store.UNBOUND
        return "" if self.cb_product.currentIndex() <= 0 else t

    def refresh(self):
        """定时刷新（MainWindow 每 2 秒调）：整目录扫描较重，节流到每 6 秒一次。"""
        now = time.monotonic()
        if now - self._last_auto >= _AUTO_SEC:
            self._reload(force=True)

    def _reload(self, force=False):
        if self._is_trash():
            # 回收站视图：数据源换成 output_store.list_trash()（产品/标记筛选对它无意义）
            rows = self._sorted(output_store.list_trash())
            sig = tuple((r["path"], r.get("display", "")) for r in rows)
        else:
            product = self._picked_product()
            mark = self._mark_val.get(self.cb_mark.currentText(), "")
            rows = output_store.list_outputs(product=product,
                                             mark=(mark if mark != "none" else ""))
            if mark == "none":
                rows = [r for r in rows if not r["mark"]]
            rows = self._sorted(rows)
            sig = tuple((r["path"], r["product"], r["mark"], r["bound"],
                         r.get("display", "")) for r in rows)
        self._last_auto = time.monotonic()
        self._rows = rows
        if force or sig != self._sig:
            self._sig = sig
            self._render()

    def _render(self):
        kw = (self.ed_search.text() or "").strip().lower()
        for c in self._cards:
            c.deleteLater()
        self._cards = []
        self._by_path = {}
        pending = []
        while self._grid.count():
            self._grid.takeAt(0)
        shown = 0
        self._visible = []
        for r in self._rows:
            if kw and kw not in (r.get("name") or "").lower():
                continue
            card = _OutputCard(r, self._open, self._menu, self._on_check,
                               is_multi=self._multi_on,
                               show_physical=self._show_physical, trash=self._is_trash(),
                               parent=self._holder)
            card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            card.set_selected(r["path"] in self._selected)
            card.chk.setVisible(self._multi_on())   # 重建卡片后保持多选开关态
            self._cards.append(card)
            path = r["path"]
            self._by_path[path] = card
            self._visible.append(path)
            img = self._thumbs.get(path)
            if img is not None:
                card.set_image(img)
            else:
                pending.append((path, r.get("_ts")))
            shown += 1
        self._thumb_worker.submit(pending)
        self.lbl_hint.setText(
            f"共 {len(self._rows)} 条 · 显示 {shown} 条 · 后端：{self.current_backend()}")
        self._update_sel_label()
        if self._is_trash():
            self._lbl_empty.setText(
                "回收站是空的：成品库里的成品右键「移入回收站」后，会先待在这里，可随时还原。")
        else:
            self._lbl_empty.setText(
                "成品库还没有内容：生成任务产出、或到「🎬 AI 混剪」导出，都会落在这里。")
        self._lbl_empty.setVisible(shown == 0)
        self._relayout()

    def _on_thumb(self, path, img):
        """后台抽好一帧：存缓存并给仍在屏上的对应卡片上色。"""
        if img is None or img.isNull():
            return
        self._thumbs[path] = img
        card = self._by_path.get(path)
        if card is not None:
            card.set_image(img)

    def closeEvent(self, e):
        self._thumb_worker.request_stop()
        self._thumb_worker.wait(1500)
        super().closeEvent(e)

    def _relayout(self):
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
        QTimer.singleShot(0, self._relayout)

    # ---------------- 交互 ----------------
    def _open(self, row):
        path = row.get("path") or ""
        if not path or not Path(path).exists():
            QMessageBox.information(self, "提示", "成品文件不存在或已被移动")
            return
        self._media = VideoPlayerDialog(self, path)
        self._media.show()

    def _menu(self, row, global_pos):
        """单卡片右键：成品库态给预览/定位/绑定/设显示名/标记/送混剪/移入回收站；
        回收站态只给还原/定位/彻底删除。"""
        menu = StyledMenu(self)
        if self._is_trash():
            menu.addAction("♻ 还原", lambda: self._do_restore([row]))
            menu.addAction("📁 打开所在文件夹", lambda: self._reveal(row["path"]))
            menu.addSeparator()
            menu.addAction("🗑 彻底删除", lambda: self._do_purge([row]))
            menu.exec(global_pos)
            return
        menu.addAction("👁 预览", lambda: self._open(row))
        menu.addAction("📁 打开所在文件夹", lambda: self._reveal(row["path"]))
        menu.addSeparator()
        menu.addAction("🏷 绑定产品…", lambda: self._bind_product([row]))
        if row.get("bound"):
            menu.addAction("✖ 解除绑定", lambda: self._unbind_product([row]))
        menu.addAction("🏷 设置显示名称…", lambda: self._set_display_one(row))
        menu.addSeparator()
        menu.addAction("🟢 标记可用", lambda: self._set_mark([row], "ok"))
        menu.addAction("🔴 标记不可用", lambda: self._set_mark([row], "bad"))
        menu.addAction("⚪ 取消标记", lambda: self._set_mark([row], ""))
        menu.addSeparator()
        menu.addAction("🎬 送混剪", lambda: self._send_remix([row]))
        menu.addSeparator()
        menu.addAction("🗑 移入回收站", lambda: self._do_trash([row]))
        menu.exec(global_pos)

    def _reveal(self, path):
        if Path(path).exists():
            reveal_in_folder(path)
        else:
            QMessageBox.information(self, "提示", "文件不存在")

    def _bind_product(self, rows):
        """右键「绑定产品」：选一个产品人工归属（优先于自动 join）。"""
        picked = _pick_product(self)
        if picked is None:
            return
        pid, name = picked
        for r in rows:
            output_store.bind(r["path"], name, pid)
        self._reload(force=True)

    def _unbind_product(self, rows):
        for r in rows:
            output_store.unbind(r["path"])
        self._reload(force=True)

    def _set_mark(self, rows, mark):
        from store import task_store
        from processors import output_mark
        target = {"ok": task_store.MARK_OK, "bad": task_store.MARK_BAD}.get(mark, "")
        moved = 0
        for r in rows:
            ok, _new, _msg = output_mark.set_mark(r["path"], target)
            moved += 1 if ok else 0
        if moved < len(rows):
            QMessageBox.information(
                self, "部分未成功",
                f"{len(rows) - moved} 条标记失败（文件可能被占用播放中，稍后再试）")
        self._reload(force=True)

    def _send_remix(self, rows):
        """把选中成品路径写入暂存（app_state），混剪页读取后可加入拼接清单。"""
        from store import app_state
        paths = [r["path"] for r in rows if r and r.get("path")]
        staged = list(app_state.get("remix_stage") or [])
        for p in paths:
            if p not in staged:
                staged.append(p)
        app_state.set_value("remix_stage", staged)
        QMessageBox.information(
            self, "已送混剪",
            f"已把 {len(paths)} 条送入混剪暂存。\n请到「🎬 AI 混剪」页点「＋ 从暂存加入」。")

    def _open_root(self):
        root = output_store.scan_root()
        root.mkdir(parents=True, exist_ok=True)
        open_path(str(root))

    # ---------------- 多选 / 批量操作 ----------------
    def _multi_on(self):
        return self._multi

    def _set_multi(self, on):
        """开/关多选：开 = 卡片出选框、点卡片任意位置即勾选（触发区=整张卡）；
        关 = 收起选框并清空。开启入口：空白处拖动框选自动开，或右键空白处手动开。"""
        if on == self._multi:
            return
        self._multi = on
        for c in self._cards:
            c.chk.setVisible(on)
        if not on:
            self._clear_sel()
        self._update_sel_label()

    def _empty_menu_show(self, pos):
        self._empty_menu().exec(self._holder.mapToGlobal(pos))

    def _empty_menu(self):
        """空白处右键：最顶是多选开关，严格跟随当前多选态 self._multi——
        未开显示「开启多选」，已在多选态显示「退出多选」（含已选 0/1 条的情况）。
        文案与实际状态一一对应，点击必定切换生效。
        成品库态：开启后列 全选/反选/绑定/批量设显示名/批量重命名/移入回收站，常驻排序与名称显示切换。
        回收站态：开启后列 全选/反选/还原/彻底删除/清空回收站，只常驻排序。

        注意：菜单项一律不用 setCheckable——windowsvista 会在项前画原生小圆框+对钩，
        很丑；当前状态靠文案表达（排序用「✓ 」文本前缀）。"""
        m = StyledMenu(self)
        if self._is_trash():
            if self._multi:
                m.addAction("退出多选", lambda: self._set_multi(False))
                m.addSeparator()
                m.addAction("全选", self._act_select_all)
                m.addAction("反选", self._act_invert)
                m.addSeparator()
                m.addAction("♻ 还原选中", self._act_restore)
                m.addAction("🗑 彻底删除选中", self._act_purge)
                m.addAction("🧹 清空回收站", self._act_clear_trash)
            else:
                m.addAction("开启多选", lambda: self._set_multi(True))
            m.addSeparator()
            m.addMenu(self._build_sort_menu())
            return m
        if self._multi:
            m.addAction("退出多选", lambda: self._set_multi(False))
            m.addSeparator()
            m.addAction("全选", self._act_select_all)
            m.addAction("反选", self._act_invert)
            m.addSeparator()
            m.addAction("🏷 绑定产品…", self._act_bind)
            m.addAction("🏷 批量设置显示名称…", self._act_set_display)
            m.addAction("✏ 批量重命名…", self._act_rename)
            m.addAction("🗑 移入回收站", self._act_delete)
        else:
            m.addAction("开启多选", lambda: self._set_multi(True))
        m.addSeparator()
        m.addAction(self._physical_toggle_text(), self._toggle_physical)
        m.addSeparator()
        m.addMenu(self._build_sort_menu())
        return m

    def _physical_toggle_text(self):
        """名称显示切换项文案：跟随当前态——正显示物理名时提供「切回外显名」，反之亦然。"""
        return "🏷 显示外显名称（去后缀）" if self._show_physical else "📄 显示物理名称（带后缀）"

    def _build_sort_menu(self):
        """「↕ 排序」子菜单：悬停展开；当前方式前缀「✓」（纯文本，不用勾选框）。"""
        sm = StyledMenu("↕ 排序", self)
        for mode, label in _SORT_ITEMS:
            text = f"✓ {label}" if mode == self._sort else f"　 {label}"
            sm.addAction(text, lambda _=False, mo=mode: self._pick_sort(mo))
        return sm

    def _pick_sort(self, mode):
        self._sort = mode
        self._reload(force=True)

    def _sorted(self, rows):
        """展示层排序（不动数据层）：日期按 created_at 字符串（定长可字典序），
        名称忽略大小写；库里原始序就是时间新→旧，time_desc 直接沿用。"""
        if self._sort == "name_asc":
            return sorted(rows, key=lambda r: (r.get("name") or "").lower())
        if self._sort == "name_desc":
            return sorted(rows, key=lambda r: (r.get("name") or "").lower(), reverse=True)
        if self._sort == "time_asc":
            return sorted(rows, key=lambda r: (r.get("created_at") or ""))
        if self._sort == "time_desc":
            return sorted(rows, key=lambda r: (r.get("created_at") or ""), reverse=True)
        return rows

    def eventFilter(self, obj, e):
        """在卡片网格的空白处按住拖动 = 橡皮筋框选，框到即自动开启多选并勾上；
        多选态下单击空白（没拖动）= 清空勾选。卡片自身的点击由 _OutputCard 分流。"""
        if obj is self._holder:
            t = e.type()
            if (t == QEvent.Type.MouseButtonPress
                    and e.button() == Qt.MouseButton.LeftButton):
                self._rub_anchor = e.position().toPoint()
                if self._rubber is None:
                    self._rubber = QRubberBand(
                        QRubberBand.Shape.Rectangle, self._holder)
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
                # 只收尾「一次由左键按下发起、还没消费掉」的框选手势：
                # 右键释放不得进来——否则会用上一段框选残留的大 geometry 误判为
                # 「框到东西」而直接开启多选（QRubberBand 对象一旦建就常驻、geometry 是脏的）。
                rect = self._rubber.geometry()
                self._rubber.hide()
                self._rub_anchor = None
                if rect.width() <= 4 and rect.height() <= 4:
                    # 单击空白：没开多选时什么都不做；开着则清空
                    if self._multi:
                        self._clear_sel()
                else:
                    self._set_multi(True)   # 框选即多选
                    for c in self._cards:
                        c.chk.setChecked(c.geometry().intersects(rect))
                return True
        return super().eventFilter(obj, e)

    def _on_check(self, path, checked):
        """卡片选框变化：更新已选集合与计数（跨搜索/筛选保留）。"""
        if checked:
            self._selected.add(path)
        else:
            self._selected.discard(path)
        self._update_sel_label()

    def _update_sel_label(self):
        n = len(self._selected)
        self.lbl_sel.setText(f"已选 {n} 条")
        self.lbl_sel.setVisible(n > 0)
        # 多选/已勾选时，批量入口改名“批量操作”；setMenu 自带箭头，文本不写 ▾
        self.b_ops.setText("⚙ 批量操作" if (self._multi or n) else "⚙ 操作")

    def _select_all(self):
        for p in self._visible:
            self._selected.add(p)
        for c in self._cards:
            c.set_selected(True)
        self._update_sel_label()

    def _clear_sel(self):
        self._selected.clear()
        for c in self._cards:
            c.set_selected(False)
        self._update_sel_label()

    def _selected_rows(self):
        """已选且仍在当前数据里的行，按库默认序（修改时间倒序）——重命名序号按这个走。"""
        return [r for r in self._rows if r["path"] in self._selected]

    def _rebuild_ops_menu(self):
        """「⚙ 操作/批量操作」按钮菜单：按当前视图重建（成品库给绑定/设显示名/重命名/移入回收站；
        回收站给还原/彻底删除/清空）。"""
        m = self._ops_menu
        m.clear()
        if self._is_trash():
            m.addAction("♻ 还原选中", self._act_restore)
            m.addAction("🗑 彻底删除选中", self._act_purge)
            m.addSeparator()
            m.addAction("🧹 清空回收站", self._act_clear_trash)
        else:
            m.addAction("🏷 绑定产品…", self._act_bind)
            m.addAction("🏷 批量设置显示名称…", self._act_set_display)
            m.addSeparator()
            m.addAction("✏ 批量重命名…", self._act_rename)
            m.addSeparator()
            m.addAction("🗑 移入回收站", self._act_delete)

    def _require_sel(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "先选几条",
                                    "先空白处拖动框选，或右键空白处→开启多选后点选要操作的成品。")
        return rows

    def _act_select_all(self):
        self._set_multi(True)
        self._select_all()

    def _act_invert(self):
        """反选：已勾的去掉、没勾的勾上（多选未开时先自动开启）。"""
        self._set_multi(True)
        for c in self._cards:
            c.chk.setChecked(not c.chk.isChecked())

    def _act_bind(self):
        rows = self._require_sel()
        if rows:
            self._bind_product(rows)
            self._clear_sel()

    def _act_rename(self):
        rows = self._require_sel()
        if not rows:
            return
        from gui.dialogs_rename import BatchRenameDialog
        dlg = BatchRenameDialog(self, rows)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.plan:
            return
        done, failed = 0, []
        for row, stem in dlg.plan:
            ok, _new, msg = output_store.rename(row["path"], stem)
            if ok:
                done += 1
            else:
                failed.append(f"{row.get('name') or row['path']}：{msg}")
        self._clear_sel()
        self._reload(force=True)
        if failed:
            QMessageBox.warning(self, "部分未改名",
                                f"成功 {done} 条，失败 {len(failed)} 条：\n"
                                + "\n".join(failed[:8])
                                + ("\n…" if len(failed) > 8 else ""))
        else:
            self.lbl_hint.setText(f"已重命名 {done} 条成品")

    def _act_delete(self):
        """批量「移入回收站」（软件内回收站）：走 to_trash，不进系统回收站。"""
        rows = self._require_sel()
        if rows:
            self._do_trash(rows)

    # ---------------- 回收站 / 显示名 动作 ----------------
    def _is_trash(self):
        return self._view == "trash"

    def _set_view(self, view):
        """切换页内视图（成品库/回收站）：退出多选、清空已选，重列。"""
        if view == self._view:
            return
        self._view = view
        self._set_multi(False)
        self._clear_sel()
        self._update_view_buttons()
        self._reload(force=True)

    def _update_view_buttons(self):
        """分段按钮高亮 + 回收站计数 + 回收站态隐藏产品/标记筛选。"""
        self.b_view_lib.setStyleSheet(_VIEW_ON if self._view == "lib" else _VIEW_OFF)
        self.b_view_trash.setStyleSheet(_VIEW_ON if self._view == "trash" else _VIEW_OFF)
        try:
            n = len(output_store.list_trash())
        except Exception:
            n = 0
        self.b_view_trash.setText(f"🗑 回收站({n})")
        show_filters = self._view == "lib"
        for w in (self.lbl_product, self.cb_product, self.lbl_mark, self.cb_mark):
            w.setVisible(show_filters)

    def _toggle_physical(self):
        """切换卡片底部名称：外显名（去后缀/自定义）↔ 物理名（带后缀）。"""
        self._show_physical = not self._show_physical
        self._render()

    def _do_trash(self, rows):
        """把选中成品移入软件内回收站（物理移动，可随时还原）。"""
        paths = [r["path"] for r in rows if r and r.get("path")]
        if not paths:
            return
        if QMessageBox.question(
                self, "移入回收站",
                f"把选中的 {len(paths)} 条成品移入软件内回收站？\n"
                "（文件会物理移动到回收站目录，不进系统回收站；可随时还原，"
                "在回收站里选「彻底删除」才会真删）"
        ) != QMessageBox.StandardButton.Yes:
            return
        ok, failed = output_store.to_trash(paths)
        self._selected -= set(ok)
        self._clear_sel()
        self._update_view_buttons()
        self._reload(force=True)
        if failed:
            QMessageBox.warning(self, "部分未移入",
                                f"成功 {len(ok)} 条，失败 {len(failed)} 条：\n"
                                + "\n".join(f"{p}：{m}" for p, m in failed[:8])
                                + ("\n…" if len(failed) > 8 else ""))
        else:
            self.lbl_hint.setText(f"已把 {len(ok)} 条移入回收站")

    def _do_restore(self, rows):
        """从回收站还原：物理移回原目录，重回成品库。"""
        paths = [r["path"] for r in rows if r and r.get("path")]
        if not paths:
            return
        ok, failed = output_store.restore(paths)
        self._clear_sel()
        self._update_view_buttons()
        self._reload(force=True)
        if failed:
            QMessageBox.warning(self, "部分未还原",
                                f"成功 {len(ok)} 条，失败 {len(failed)} 条：\n"
                                + "\n".join(f"{p}：{m}" for p, m in failed[:8]))
        else:
            self.lbl_hint.setText(f"已还原 {len(ok)} 条回成品库")

    def _do_purge(self, rows):
        """彻底删除：真删磁盘文件，不可恢复。"""
        paths = [r["path"] for r in rows if r and r.get("path")]
        if not paths:
            return
        if QMessageBox.question(
                self, "彻底删除",
                f"从磁盘彻底删除 {len(paths)} 条？此操作不可恢复！"
        ) != QMessageBox.StandardButton.Yes:
            return
        ok, failed = output_store.purge(paths)
        self._clear_sel()
        self._update_view_buttons()
        self._reload(force=True)
        if failed:
            QMessageBox.warning(self, "部分未删除",
                                f"成功 {len(ok)} 条，失败 {len(failed)} 条：\n"
                                + "\n".join(f"{p}：{m}" for p, m in failed[:8]))
        else:
            self.lbl_hint.setText(f"已彻底删除 {len(ok)} 条")

    def _act_restore(self):
        rows = self._require_sel()
        if rows:
            self._do_restore(rows)

    def _act_purge(self):
        rows = self._require_sel()
        if rows:
            self._do_purge(rows)

    def _act_clear_trash(self):
        rows = output_store.list_trash()
        if not rows:
            return
        if QMessageBox.question(
                self, "清空回收站",
                f"彻底删除回收站里全部 {len(rows)} 条？此操作不可恢复！"
        ) != QMessageBox.StandardButton.Yes:
            return
        ok, _failed = output_store.clear_trash()
        self._clear_sel()
        self._update_view_buttons()
        self._reload(force=True)
        self.lbl_hint.setText(f"已清空回收站（{len(ok)} 条）")

    def _act_set_display(self):
        """批量设置显示名称：复用重命名弹窗的字段规则，但只写外显名（不动物理文件名）。"""
        rows = self._require_sel()
        if not rows:
            return
        from gui.dialogs_rename import BatchRenameDialog
        dlg = BatchRenameDialog(self, rows, title="批量设置显示名称",
                                ok_text="🏷 应用显示名称")
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.plan:
            return
        for row, stem in dlg.plan:
            output_store.set_display(row["path"], stem)
        self._show_physical = False   # 设完外显名自动切回外显名显示
        self._clear_sel()
        self._reload(force=True)
        self.lbl_hint.setText(f"已为 {len(dlg.plan)} 条设置显示名称")

    def _set_display_one(self, row):
        """单张卡设显示名：输一个外显名（不改文件名，存 output_display）。"""
        cur = output_store.default_display(row)
        text, ok = QInputDialog.getText(self, "设置显示名称",
                                        "外显名称（不会修改文件名）：", text=cur)
        if not ok:
            return
        output_store.set_display(row["path"], text.strip())
        self._show_physical = False
        self._reload(force=True)


def _pick_product(parent):
    """产品单选框：返回 (id, name)，取消返回 None。人工绑定成品归属用。"""
    from store import product_store
    items = product_store.list_products(product_store.TYPE_PRODUCT)
    dlg = QDialog(parent)
    dlg.setWindowTitle("绑定产品")
    dlg.resize(320, 420)
    v = QVBoxLayout(dlg)
    tip = QLabel("选择这条成品归属的产品（人工绑定优先于系统自动识别）：")
    tip.setWordWrap(True)
    v.addWidget(tip)
    lst = QListWidget()
    lst.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    for it in items:
        row = QListWidgetItem(it["name"])
        row.setData(Qt.ItemDataRole.UserRole, int(it["id"]))
        lst.addItem(row)
    if not items:
        lst.addItem("（产品中心还没有产品，请先到「产品中心」新增）")
    v.addWidget(lst, 1)
    bb = QHBoxLayout()
    bb.addStretch(1)
    b_ok = QPushButton("绑定")
    b_ok.setDefault(True)
    b_ok.clicked.connect(dlg.accept)
    b_no = QPushButton("取消")
    b_no.setObjectName("GhostBtn")
    b_no.clicked.connect(dlg.reject)
    bb.addWidget(b_ok)
    bb.addWidget(b_no)
    v.addLayout(bb)
    lst.itemDoubleClicked.connect(lambda _=None, d=dlg: d.accept())
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    cur = lst.currentItem()
    if cur is None or cur.data(Qt.ItemDataRole.UserRole) is None:
        return None
    return int(cur.data(Qt.ItemDataRole.UserRole)), cur.text()
