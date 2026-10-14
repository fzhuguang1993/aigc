"""
gui/pages_output_lib.py —— 成品库（生成产物 + 混剪结果，卡片式浏览）

对标「拆解任务」页的卡片形态：每条成品一张卡（缩略占位 / 文件名 / 产品徽标 /
审片角标 / 大小·日期），点卡片预览、右键可 预览 / 定位 / 绑定产品 / 标记 / 送混剪。
数据源 store.output_store（扫盘只读视图，永远与磁盘一致）；产品归属优先「人工绑定」
（右键绑定，落 output_binds 表），其次自动 join（runs/tasks）。可按 产品（含未分类）
/ 标记 / 名称 搜索筛选。

卡片网格（封面首帧 + 滚动标题 + 徽标 + 框选多选 + 自适应重排）整体上收为共享组件
gui.media_grid.MediaGrid，素材库/混剪选择器同款；本页只保留成品库业务（筛选/操作/回收站）。

顶部标注当前存储后端（本轮本地）。整目录扫描不便宜，定时刷新节流到每 6 秒一次，
并靠「行签名」判是否真变了，没变就不重建卡片（防闪烁/防抖）。
"""
import time

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QObject, QEvent
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QComboBox, QLineEdit, QMessageBox, QDialog, QInputDialog,
                               QSplitter, QPlainTextEdit, QToolButton, QApplication)

from store import output_store
from core import video_categories
from utils.desktop_utils import open_path, reveal_in_folder
from gui.header import page_header, Card
from gui.menus import StyledMenu
from gui.widgets import VideoPlayerDialog
from gui.dialogs_product import pick_product
from gui.media_grid import MediaGrid, fmt_size
from gui.tree_filter import TreeFilterPanel

_AUTO_SEC = 6.0      # 定时刷新最小间隔（整目录扫描不便宜，别 2 秒刷一次）

_MARK_LABEL = {"ok": "🟢可用", "bad": "🔴不可用"}

# 右键「排序」子菜单项：生成日期（文件时间）/ 名称，两个方向都可切
_SORT_ITEMS = (("time_desc", "生成日期（新→旧）"), ("time_asc", "生成日期（旧→新）"),
               ("name_asc", "名称（A→Z）"), ("name_desc", "名称（Z→A）"))

# 顶部「成品库 / 回收站」分段切换：选中高亮主蓝，未选灰底
_VIEW_ON = ("QPushButton{background:#3A6EE8;color:#FFFFFF;border:none;"
            "border-radius:6px;padding:4px 12px;font-weight:600;}")
_VIEW_OFF = ("QPushButton{background:#F2F3F5;color:#4E5969;"
             "border:1px solid #E5E6EB;border-radius:6px;padding:4px 12px;}")


class OutputAsrWorker(QThread):
    """成品库口播增量转写：单后台线程串行跑 Whisper，不碰任何 Qt 对象。

    入队只接「库里尚无 transcript 行」的视频（每条只转写一次）；无就绪模型时调用方不建本
    worker（静默跳过、不弹）； Vad 已内置于 transcribe，纯 BGM/无声自然出空文本→has_speech=0。
    每完成一条用 done_one 信号回主线程（路/正文/是否有口播/模型/耗时秒/错误）。
    单条失败不再静默丢弃：带错误回主线程记入日志流水（不写库→下次可重试）。"""

    done_one = Signal(str, str, int, str, float, str)   # path, text, has_speech, model, 耗时, 错误

    def __init__(self, size, paths, parent=None):
        super().__init__(parent)
        self._size = size
        self._paths = [str(p) for p in (paths or []) if str(p or "").strip()]
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        from video_text_tools.asr import transcribe as tr
        for path in self._paths:
            if self._stop:
                break
            t0 = time.perf_counter()
            try:
                if not Path(path).exists():
                    continue
                segments, full_text = tr.transcribe(
                    path, size=self._size, should_stop=lambda: self._stop)
                text = (full_text or "").strip()
                has = 1 if (text and segments) else 0
                self.done_one.emit(path, text, has, self._size,
                                   time.perf_counter() - t0, "")
            except Exception as e:
                # 单条失败（缺依赖/解码错）：带错误回主线程记流水，不写库→下次可重试
                self.done_one.emit(path, "", 0, self._size,
                                   time.perf_counter() - t0, str(e))


class _EscCloseFilter(QObject):
    """挂在 QApplication 上的临时事件过滤器：捕获任意处的 Esc 键→收起宿主浮窗。
    浮窗自身不抢焦点（WA_ShowWithoutActivating），收不到按键，故走应用级过滤。"""

    def __init__(self, bubble):
        super().__init__(bubble)
        self._bubble = bubble

    def eventFilter(self, obj, e):
        if (e.type() == QEvent.Type.KeyPress
                and e.key() == Qt.Key.Key_Escape):
            self._bubble.hide()
            return True
        return super().eventFilter(obj, e)


class _ScriptBubble(QWidget):
    """口播脚本悬停浮窗：无边框小提示窗，展示逐字稿 + 复制；单例复用，
    鼠标移入徽标时弹在徽标旁（越界自动翻到左/上侧），✕ 收起。
    只给悬停用（非模态）；点击徽标走完整弹窗 _view_transcript。
    鼠标移出「徽标 + 浮窗」两个范围即刻收起（轮询光标位置，任一在框内都留着）；
    任意处按 Esc 也能手动关闭。Alt+Tab 切到其它应用时自动消失（无 WindowStaysOnTopHint）。"""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint)   # ← 去掉 WindowStaysOnTopHint
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)  # 不抢焦点
        self.setObjectName("ScriptBubble")
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 10)
        v.setSpacing(6)
        head = QHBoxLayout()
        self.lbl_title = QLabel()
        self.lbl_title.setObjectName("PageTip")
        head.addWidget(self.lbl_title, 1)
        # ✕ 用 QToolButton+autoRaise，避免 GhostBtn 内边距把字形挤出 FixedWidth 只显外框
        b_close = QToolButton()
        b_close.setText("\u2715")
        b_close.setAutoRaise(True)
        b_close.setFixedSize(20, 20)
        b_close.setCursor(Qt.CursorShape.PointingHandCursor)
        b_close.clicked.connect(self.hide)
        head.addWidget(b_close)
        v.addLayout(head)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        v.addWidget(self.view, 1)
        b_copy = QPushButton("\U0001f4cb 复制全部")
        b_copy.setObjectName("GhostBtn")
        b_copy.clicked.connect(lambda: (self.view.selectAll(), self.view.copy()))
        v.addWidget(b_copy)
        self.resize(380, 300)
        # 光标一旦同时离开「徽标」和「浮窗」→ 立即收起：用一个短轮询定时器查全局光标
        # 位置，比依赖 enter/leave 可靠（直接从徽标移到别处、从不进浮窗也能及时收）。
        self._anchor = None        # 触发本次弹出的徽标控件（判定「是否还在 item 上」）
        self._poll = QTimer(self)
        self._poll.setInterval(80)
        self._poll.timeout.connect(self._check_cursor)
        self._esc_filter = _EscCloseFilter(self)   # 捕获全局 Esc 关闭（浮窗不抢焦点）
        from gui.theme import tokenize
        self.setStyleSheet(tokenize(
            "#ScriptBubble{background:#FFFFFF;border:1px solid #C9CDD4;"
            "border-radius:8px;}"))

    def show_for(self, title, text, pos, anchor=None):
        self._anchor = anchor
        self.lbl_title.setText(f"\U0001f399 {title} \u00b7 共 {len(text)} 字")
        self.view.setPlainText(text)
        from PySide6.QtGui import QGuiApplication
        scr = QGuiApplication.primaryScreen().availableGeometry()
        x, y = int(pos.x()), int(pos.y())
        if x + self.width() > scr.right():        # 右侧装不下→改放徽标左侧
            x = int(pos.x()) - self.width() - 12
        x = max(scr.left() + 4, min(x, scr.right() - self.width() - 4))
        if y + self.height() > scr.bottom():
            y = scr.bottom() - self.height() - 4
        y = max(scr.top() + 4, y)
        self.move(x, y)
        self.show()
        self.raise_()
        QApplication.instance().installEventFilter(self._esc_filter)
        self._poll.start()

    def _check_cursor(self):
        """光标是否还在徽标或浮窗任一方框内；都不在 → 立即收起。"""
        from PySide6.QtGui import QCursor
        gp = QCursor.pos()
        if self.geometry().contains(gp):
            return                                  # 还在浮窗内（允许阅读/复制）
        a = self._anchor
        if a is not None and a.isVisible():
            try:
                if a.rect().contains(a.mapFromGlobal(gp)):
                    return                          # 还在徽标上
            except RuntimeError:
                self._anchor = None                 # 徽标已随卡片销毁
        self.hide()

    def hideEvent(self, e):
        self._poll.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._esc_filter)
        super().hideEvent(e)


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
        self.cb_product.currentIndexChanged.connect(lambda _=None: self._on_product_combo())
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
        b_asr_log = QPushButton("📜 口播日志")
        b_asr_log.setObjectName("GhostBtn")
        b_asr_log.setToolTip("查看口播识别的执行统计与逐条流水日志")
        b_asr_log.clicked.connect(self._open_asr_log)
        bar.addWidget(b_asr_log)
        b_ref = QPushButton("🔄 刷新")
        b_ref.setObjectName("GhostBtn")
        b_ref.clicked.connect(lambda: self._reload(force=True))
        bar.addWidget(b_ref)
        b_root = QPushButton("📂 打开成品库目录")
        b_root.setObjectName("GhostBtn")
        b_root.clicked.connect(self._open_root)
        bar.addWidget(b_root)
        lay.addLayout(bar)

        # 卡片网格用共享组件（成品库/素材库同款排版）：本页只喂取值/回调
        self.grid = MediaGrid(key_of=lambda r: r["path"],
                              path_of=lambda r: r["path"],
                              build_display=self._display,
                              on_open=self._open,
                              on_menu=self._menu,
                              on_selection_changed=self._update_sel_label,
                              hover_preview=True,
                              on_badge=self._badge_hit)
        self.grid.set_empty_menu(self._empty_menu)

        self._lbl_empty = QLabel("成品库还没有内容：生成任务产出、或到「🎬 AI 混剪」导出，都会落在这里。")
        self._lbl_empty.setObjectName("PageTip")
        self._lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_empty.setWordWrap(True)
        self._lbl_empty.setVisible(False)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(6)
        rv.addWidget(self.grid, 1)
        rv.addWidget(self._lbl_empty)

        # 左侧「视频类别树 + 产品树」筛选面板（包白卡，对齐产品页 tree_card 视觉）
        self.panel = TreeFilterPanel(tag_title="视频类别", product_title="产品")
        self.panel.tag_picked.connect(self._on_tree_tag)
        self.panel.product_picked.connect(self._on_tree_product)
        self.left_card = Card(margins=(8, 8, 8, 8))
        self.left_card.v.addWidget(self.panel)
        self.left_card.setFixedWidth(220)   # 固定最小宽度 220，不可拖宽

        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(self.left_card)
        self.split.addWidget(right)
        self.split.setCollapsible(0, False)   # 左栏固定
        self.split.setSizes([220, 780])
        lay.addWidget(self.split, 1)

        self._rows = []
        self._base = []           # 全量库行（做树计数 + 本地筛选的唯一数据源）
        self._sig = None
        self._tree_sig = None
        self._media = None
        self._last_auto = 0.0
        self._sort = "time_desc"   # 默认排序：生成日期新→旧
        self._view = "lib"          # lib=成品库 / trash=回收站（页内视图切换）
        self._show_physical = False  # 卡片底部名称：False=外显名(去后缀/自定义)，True=物理名(带后缀)
        self._tag = ""             # 视频类别树选中的 category（''=全部）；产品/标记仍走顶栏下拉
        self._tree_sync = False     # 树驱动下拉时置真，避免下拉回调二次刷新
        from store import app_state
        self._tree_visible = bool(app_state.get("output_tree_visible", True))
        self._asr = None               # 后台口播转写 worker（单线程串行；None=空闲）
        self._asr_log = self._load_asr_log()  # 历史流水（已持久化，最多500条）+ 本会话追加
        self._asr_failed = set()       # 本会话已失败/跳过的路径（不每 6 秒热重试）
        self._asr_model = ""           # 最近一次用到的模型 size
        self._bubble = None            # 悬停脚本浮窗（单例复用）
        self.rebuild_filters()
        self._update_view_buttons()
        self._apply_tree_visible()
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
            # 回收站视图：数据源换成 output_store.list_trash()（产品/标记/标签筛选对它无意义）
            self._base = []
            rows = self._sorted(output_store.list_trash())
            sig = tuple((r["path"], r.get("display", "")) for r in rows)
        else:
            # 一次全量扫库做唯一数据源：树计数与产品/标记/标签筛选都在本地切
            self._base = output_store.list_outputs()
            rows = self._filtered(self._base)
            sig = tuple((r["path"], r["product"], r["mark"], r["bound"],
                         r.get("display", "")) for r in rows)
        self._last_auto = time.monotonic()
        self._rows = rows
        self._refresh_trees()
        if force or sig != self._sig:
            self._sig = sig
            self._render()
        # 库刷完顺手提交待识别队列（开关开 + 模型就绪才跑；无则静默跳过）
        self._maybe_asr()

    def _display(self, r):
        """卡片文案：名称（物理/外显）、徽标（产品/回收站）、meta（大小·日期·标记）。
        交给 MediaGrid 的 build_display；物理/回收站显示态由本页开关决定。"""
        if self._show_physical:
            name = r.get("name") or Path(r.get("path") or "").name
        else:
            name = output_store.default_display(r) or (r.get("name") or "")
        if self._is_trash():
            tags = "🗑 已在回收站"
        else:
            prod = r.get("product") or ""
            tags = (("🖇 " if r.get("bound") else "🏷 ") + prod) if prod else "🏷 未分类"
        flag = _MARK_LABEL.get(r.get("mark"), "")
        meta = (f"{fmt_size(r.get('size'))} · {(r.get('created_at') or '')[5:16]}"
                + (f" · {flag}" if flag else ""))
        badge = "🎙 口播" if r.get("has_speech") else ""
        return {"name": name, "tags": tags, "meta": meta, "tooltip": name,
                "badge": badge}

    def _render(self):
        kw = (self.ed_search.text() or "").strip().lower()
        rows = self._rows
        if kw:
            rows = [r for r in rows if kw in (r.get("name") or "").lower()]
        self.grid.set_rows(rows)
        shown = self.grid.count()
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

    # ---------------- 筛选（产品/标记/标签 + 树） ----------------
    def _filtered(self, base):
        """本地按 产品/标记/标签 过滤（数据源是同一次全量 base，不再二扫）。"""
        prod = self._picked_product()
        mark = self._mark_val.get(self.cb_mark.currentText(), "")
        out = []
        for r in base:
            if prod == output_store.UNBOUND:
                if r["product"]:
                    continue
            elif prod:
                if r["product"] != prod:
                    continue
            if self._tag and (r.get("category") or "") != self._tag:
                continue
            if mark == "none":
                if r["mark"]:
                    continue
            elif mark:
                if r["mark"] != mark:
                    continue
            out.append(r)
        return self._sorted(out)

    @staticmethod
    def _count_pairs(rows, field, unbound_label=""):
        """按 field 统计取值→计数；空值归 unbound_label（不给则跳过）。名称排序，
        「未分类」置底。回 [(显示值, 计数)]（显示值＝原值或 unbound_label）。"""
        cnt = {}
        for r in rows:
            v = (r.get(field) or "").strip()
            if not v:
                if unbound_label:
                    cnt[unbound_label] = cnt.get(unbound_label, 0) + 1
                continue
            cnt[v] = cnt.get(v, 0) + 1
        named = sorted((k, c) for k, c in cnt.items() if k != unbound_label) \
            if unbound_label else sorted(cnt.items())
        if unbound_label and unbound_label in cnt:
            named = named + [(unbound_label, cnt[unbound_label])]
        return named

    def _prod_display(self):
        """当前产品下拉→树显示值：全部=index0→''；否则用文本（含「未分类」）。"""
        if self.cb_product.currentIndex() <= 0:
            return ""
        return self.cb_product.currentText()

    def _refresh_trees(self):
        """用 base 重算两棵树的取值/计数（只在 base 签名变了才重建，否则只回选），
        避免定时刷新把树的展开态反复抝。"""
        if self._is_trash():
            self.panel.set_tag([], 0, "")
            self.panel.set_product([], 0, "")
            self._tree_sig = None
            return
        base = self._base
        pairs_tag = self._count_pairs(base, "category", unbound_label="未分类")
        pairs_prod = self._count_pairs(base, "product", unbound_label="未分类")
        tree_sig = (len(base), tuple(pairs_prod), tuple(pairs_tag))
        cur_prod = self._prod_display()
        if tree_sig != self._tree_sig:
            self._tree_sig = tree_sig
            self.panel.set_tag(pairs_tag, len(base), current=self._tag)
            self.panel.set_product(pairs_prod, len(base), current=cur_prod)
        else:
            self.panel.select_tag(self._tag)
            self.panel.select_product(cur_prod)

    def _on_product_combo(self):
        if self._tree_sync:
            return
        self._reload(force=True)

    def _on_tree_product(self, value):
        """树选了某个产品（''=全部/「未分类」/产品名）→ 同步顶栏下拉后刷新。"""
        self._tree_sync = True
        try:
            if not value:
                self.cb_product.setCurrentIndex(0)
            else:
                idx = self.cb_product.findText(value)
                if idx >= 0:
                    self.cb_product.setCurrentIndex(idx)
        finally:
            self._tree_sync = False
        self._reload(force=True)

    def _on_tree_tag(self, value):
        self._tag = value or ""
        self._reload(force=True)

    def _apply_tree_visible(self):
        self.left_card.setVisible(self._tree_visible and not self._is_trash())

    def _toggle_tree(self):
        self._tree_visible = not self._tree_visible
        from store import app_state
        app_state.set_value("output_tree_visible", self._tree_visible)
        self._apply_tree_visible()

    # ---------------- 口播识别（增量后台 Whisper） ----------------
    @staticmethod
    def _asr_autorun():
        from store import app_state
        return bool(app_state.get("output_asr_autorun", True))

    def _toggle_asr(self):
        """⚙ 操作菜单开/关自动识别：关→停当前 worker；开→刷一次（顺带提交待识别）。"""
        from store import app_state
        new = not self._asr_autorun()
        app_state.set_value("output_asr_autorun", new)
        if not new and self._asr is not None and self._asr.isRunning():
            self._asr.request_stop()
            self._asr.wait(2000)
            self._asr = None
        elif new:
            self._reload(force=True)

    def _load_asr_log(self):
        """从 app_state 恢复持久化流水，失败给空列表。"""
        import json
        from store import app_state
        try:
            raw = app_state.get("output_asr_log")
            data = json.loads(raw) if raw else []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_asr_log(self):
        """持久化当前流水（保留最近500条）。"""
        import json
        from store import app_state
        try:
            app_state.set_value("output_asr_log",
                                json.dumps(self._asr_log[-500:], ensure_ascii=False))
        except Exception:
            pass

    def _maybe_asr(self):
        """库刷完提交待识别队列：仅成品库视图 + 开关开 + 模型就绪；上一条 worker 未跑完不重开。

        只队「尚无 transcript 行」（row['transcribed'] 为假）的视频，每条只转写一次；
        无就绪模型→静默跳过（不弹不提示）。"""
        if self._is_trash() or not self._asr_autorun():
            return
        if self._asr is not None and self._asr.isRunning():
            return
        try:
            from video_text_tools.asr import transcribe as tr
            size = tr.best_ready_size()
        except Exception:
            size = None
        if not size:
            return                       # 无就绪模型：静默不跑
        pending = [r["path"] for r in self._base
                   if r.get("path")
                   and r.get("voiceover_marked")      # 只队已标记为口播素材的
                   and not r.get("transcribed")
                   and r["path"] not in self._asr_failed]
        if not pending:
            return
        self._asr = OutputAsrWorker(size, pending, self)
        self._asr.done_one.connect(self._on_asr_done)
        self._asr.start()

    def _on_asr_done(self, path, text, has_speech, model, elapsed, error):
        """worker 回主线程：记流水；失败不写库（下次可重试），成功写库 + 对应卡片补徽标。"""
        name = Path(path).name
        text = text or ""
        self._asr_model = model or self._asr_model
        self._asr_log.append({
            "name": name, "path": path, "has_speech": int(has_speech or 0),
            "chars": len(text.strip()), "elapsed": float(elapsed or 0.0),
            "model": model or "", "error": error or "",
            "at": time.strftime("%H:%M:%S"),
        })
        if error:
            self._asr_failed.add(path)
            self.lbl_hint.setText(f"🎙 口播：{name} 识别失败（已跳过，下次重试）")
            self._save_asr_log()
            return
        output_store.set_transcript(path, text, has_speech, model)
        for r in self._base:
            if r.get("path") == path:
                r["transcribed"] = True
                r["has_speech"] = 1 if (has_speech and text.strip()) else 0
                r["transcript"] = text
                break
        if has_speech and text.strip():
            self.grid.set_badge(path, "🎙 口播")
        self._save_asr_log()

    def _view_transcript(self, row):
        """右键「查看/复制口播」：只读弹框展示逐字稿（可全选复制）。"""
        text = (row.get("transcript") or "").strip()
        if not text:
            QMessageBox.information(self, "无口播", "这条未识别到口播（或尚未转写）。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"口播逐字稿 · {output_store.default_display(row) or row.get('name', '')}")
        dlg.resize(560, 460)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"共 {len(text)} 字"))
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text)
        v.addWidget(view, 1)
        row_b = QHBoxLayout()
        b_copy = QPushButton("📋 复制全部")
        b_copy.setObjectName("GhostBtn")
        b_copy.clicked.connect(lambda: (view.selectAll(), view.copy(),
                                        self.lbl_hint.setText("已复制口播到剪切板")))
        b_close = QPushButton("关闭")
        b_close.setObjectName("GhostBtn")
        b_close.clicked.connect(dlg.accept)
        row_b.addWidget(b_copy)
        row_b.addStretch(1)
        row_b.addWidget(b_close)
        v.addLayout(row_b)
        dlg.exec()

    # ---------------- 徒标交互：悬停/点击看口播脚本 ----------------
    def _badge_hit(self, row, kind, badge):
        """卡片右上角 🎙 徒标被移入/点击：有脚本才弹。悬停→轻量浮窗，点击→完整弹窗。"""
        text = (row.get("transcript") or "").strip()
        if not text:
            return
        self.grid.cancel_hover()        # 不叠悬停视频预览
        if kind == "click":
            self._view_transcript(row)
            return
        from PySide6.QtCore import QPoint
        tl = badge.mapToGlobal(badge.rect().topRight())
        if self._bubble is None:
            self._bubble = _ScriptBubble(self)
        title = output_store.default_display(row) or (row.get("name") or "")
        self._bubble.show_for(title, text, QPoint(tl.x() + 6, tl.y()), anchor=badge)

    # ---------------- 口播识别日志 ----------------
    def _open_asr_log(self):
        """📜 口播日志：执行统计（库内/已识别/有口播/待识别/失败 + 模型 + 耗时）+ 逐条流水。"""
        base = self._base
        n_total = len(base)
        n_tr = sum(1 for r in base if r.get("transcribed"))
        n_sp = sum(1 for r in base if r.get("has_speech"))
        pend = sum(1 for r in base if r.get("path") and not r.get("transcribed")
                   and r["path"] not in self._asr_failed)
        total_s = sum(e["elapsed"] for e in self._asr_log)
        model = self._asr_model
        if not model:
            try:
                from video_text_tools.asr import transcribe as tr
                model = tr.best_ready_size() or "—"
            except Exception:
                model = "—"
        head = (f"库内 {n_total} 条 · 已识别 {n_tr} · 有口播 {n_sp} · 待识别 {pend} · 本会话失败 {len(self._asr_failed)}\n"
                f"当前模型：{model} · 累计已处理 {len(self._asr_log)} 条 · 累计耗时 {total_s:.1f}s")
        dlg = QDialog(self)
        dlg.setWindowTitle("口播识别 · 执行统计与日志")
        dlg.resize(680, 480)
        v = QVBoxLayout(dlg)
        hl = QLabel(head)
        hl.setWordWrap(True)
        hl.setObjectName("PageTip")
        v.addWidget(hl)
        body = QPlainTextEdit()
        body.setReadOnly(True)
        lines = []
        for e in self._asr_log:
            if e["error"]:
                st = "❌ 失败：" + (e["error"][:80] or "未知错误")
            elif e["has_speech"] and e["chars"]:
                st = f"✅ 有口播 {e['chars']} 字"
            else:
                st = "⚪ 无口播"
            lines.append(f"{e['at']}  {e['name']}  ·  {st}  ·  {e['elapsed']:.1f}s  ·  {e['model']}")
        body.setPlainText("\n".join(reversed(lines)) if lines else
                          "本会话还没有口播识别流水（可能：未开启自动识别 / 无就绪模型 / 已全部识别）。")
        v.addWidget(body, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        b_close = QPushButton("关闭")
        b_close.setObjectName("GhostBtn")
        b_close.clicked.connect(dlg.accept)
        row.addWidget(b_close)
        v.addLayout(row)
        dlg.exec()

    def closeEvent(self, e):
        self.grid.stop_worker()
        if self._asr is not None and self._asr.isRunning():
            self._asr.request_stop()
            self._asr.wait(2000)
        if self._bubble is not None:
            self._bubble.hide()
        super().closeEvent(e)

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
        menu.addAction("🎬 设置视频类别…", lambda: self._set_category_one(row))
        if (row.get("category") or "").strip():
            menu.addAction("✖ 清除视频类别", lambda: self._apply_category([row], ""))
        menu.addMenu(self._build_voiceover_menu([row]))
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
        picked = pick_product(self)
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

    def ingest_files(self, paths):
        """右键「入库到成品库」落地：把视频复制进成品库扫描根（撞名挂 (2)），刷一次库。

        成品库是“扫盘只读视图”（不建独立表），文件必须在扫描根下才会出现，所以采用“复制”
        而非移动（保留原文件）；返回成功条数。"""
        import shutil
        root = output_store.scan_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        done = 0
        for p in (paths or []):
            src = Path(str(p or ""))
            if not src.is_file():
                continue
            target = output_store._free_target(root, src.stem, src.suffix)
            try:
                shutil.copy2(str(src), str(target))
                done += 1
            except OSError:
                continue
        if done:
            self._reload(force=True)
        return done

    # ---------------- 口播素材标记 / 逐字稿（多级右键） ----------------
    def _build_voiceover_menu(self, rows):
        """「🎙 口播 ▸」子菜单（单卡传 [row]，批量传选中行）：
        标记/取消口播素材、清除口播稿、查看/复制口播。"""
        m = StyledMenu("🎙 口播", self)
        marked = all(r.get("voiceover_marked") for r in rows if r)
        if marked:
            m.addAction("取消口播标记", lambda: self._set_voiceover(rows, False))
        else:
            m.addAction("标记为口播素材", lambda: self._set_voiceover(rows, True))
        if any((r.get("transcript") or "").strip() or r.get("transcribed") for r in rows):
            m.addAction("清除口播稿", lambda: self._clear_voiceover_script(rows))
        if len(rows) == 1 and (rows[0].get("transcript") or "").strip():
            m.addSeparator()
            m.addAction("🎙 查看/复制口播", lambda: self._view_transcript(rows[0]))
        return m

    def _set_voiceover(self, rows, flag):
        """批量标记/取消口播素材；标记后下次库刷新会自动排队跑 ASR。"""
        paths = [r["path"] for r in rows if r and r.get("path")]
        if not paths:
            return
        if flag:
            output_store.mark_voiceover(paths)
            self.lbl_hint.setText(f"已标记 {len(paths)} 条为口播素材")
        else:
            output_store.unmark_voiceover(paths)
            self.lbl_hint.setText(f"已取消 {len(paths)} 条口播标记")
        self._reload(force=True)

    def _clear_voiceover_script(self, rows):
        """清除选中成品的口播逐字稿（保留口播标记，只清文字）。"""
        paths = [r["path"] for r in rows if r and r.get("path")]
        if not paths:
            return
        for p in paths:
            output_store.clear_transcript(p)
        self.lbl_hint.setText(f"已清除 {len(paths)} 条口播稿")
        self._reload(force=True)

    # ---------------- 视频类别（手动归类，写入 output_categories） ----------------
    def _set_category_one(self, row):
        self._pick_category([row])

    def _pick_category(self, rows):
        """从词库选一个视频类别（含「自定义…」）写入 output_categories；当前类别预选。"""
        cats = video_categories.load()
        cur = (rows[0].get("category") or "") if rows else ""
        items = (([cur] if cur and cur in cats else []) + cats + ["自定义…"])
        # 去重保序（当前类别若在词库里，提到首位预选）
        seen, opts = set(), []
        for c in items:
            if c not in seen:
                seen.add(c)
                opts.append(c)
        pick, ok = QInputDialog.getItem(self, "设置视频类别", "选择类别（可自定义）：",
                                        opts, opts.index(cur) if cur in opts else 0, False)
        if not ok:
            return
        if pick == "自定义…":
            pick, ok = QInputDialog.getText(self, "自定义视频类别", "新类别名：")
            if not ok:
                return
            pick = pick.strip()
            if not pick:
                return
            video_categories.add(pick)         # 自定义项进词库，下次可选
        self._apply_category(rows, pick)

    def _apply_category(self, rows, category):
        """把一批成品归到某个视频类别（category='' 则清除分类）。"""
        paths = [r["path"] for r in rows if r and r.get("path")]
        if not paths:
            return
        for p in paths:
            output_store.set_category(p, category)
        self.lbl_hint.setText((f"已清除 {len(paths)} 条视频类别" if not category
                               else f"已为 {len(paths)} 条设视频类别：{category}"))
        self._reload(force=True)

    def _act_set_category(self):
        rows = self._require_sel()
        if rows:
            self._pick_category(rows)
            self._clear_sel()

    def _act_mark_voiceover(self):
        rows = self._require_sel()
        if rows:
            self._set_voiceover(rows, True)
            self._clear_sel()

    def _act_unmark_voiceover(self):
        rows = self._require_sel()
        if rows:
            self._set_voiceover(rows, False)
            self._clear_sel()

    def _act_clear_voiceover(self):
        rows = self._require_sel()
        if rows:
            self._clear_voiceover_script(rows)
            self._clear_sel()

    # ---------------- 多选 / 批量操作 ----------------
    def _set_multi(self, on):
        """开/关多选（转交网格组件）：开 = 卡片出选框、点卡片任意位置即勾选；
        关 = 收起选框并清空。开启入口：空白处拖动框选自动开，或右键空白处手动开。"""
        self.grid.set_multi(on)

    def _multi_on(self):
        return self.grid.multi_on()

    def _empty_menu(self):
        """空白处右键：最顶是多选开关，严格跟随当前多选态——
        未开显示「开启多选」，已在多选态显示「退出多选」（含已选 0/1 条的情况）。
        文案与实际状态一一对应，点击必定切换生效。
        成品库态：开启后列 全选/反选/绑定/批量设显示名/批量重命名/移入回收站，常驻排序与名称显示切换。
        回收站态：开启后列 全选/反选/还原/彻底删除/清空回收站，只常驻排序。

        注意：菜单项一律不用 setCheckable——windowsvista 会在项前画原生小圆框+对钩，
        很丑；当前状态靠文案表达（排序用「✓ 」文本前缀）。"""
        m = StyledMenu(self)
        multi = self.grid.multi_on()
        if self._is_trash():
            if multi:
                m.addAction("退出多选", lambda: self.grid.set_multi(False))
                m.addSeparator()
                m.addAction("全选", self._act_select_all)
                m.addAction("反选", self._act_invert)
                m.addSeparator()
                m.addAction("♻ 还原选中", self._act_restore)
                m.addAction("🗑 彻底删除选中", self._act_purge)
                m.addAction("🧹 清空回收站", self._act_clear_trash)
            else:
                m.addAction("开启多选", lambda: self.grid.set_multi(True))
            m.addSeparator()
            m.addMenu(self._build_sort_menu())
            return m
        if multi:
            m.addAction("退出多选", lambda: self.grid.set_multi(False))
            m.addSeparator()
            m.addAction("全选", self._act_select_all)
            m.addAction("反选", self._act_invert)
            m.addSeparator()
            m.addAction("🏷 绑定产品…", self._act_bind)
            m.addAction("🏷 批量设置显示名称…", self._act_set_display)
            m.addAction("✏ 批量重命名…", self._act_rename)
            m.addAction("🗑 移入回收站", self._act_delete)
        else:
            m.addAction("开启多选", lambda: self.grid.set_multi(True))
        m.addSeparator()
        m.addAction(self._physical_toggle_text(), self._toggle_physical)
        m.addSeparator()
        m.addAction("🌲 " + ("隐藏筛选树" if self._tree_visible else "显示筛选树"),
                    self._toggle_tree)
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

    def _update_sel_label(self):
        n = len(self.grid.selected())
        self.lbl_sel.setText(f"已选 {n} 条")
        self.lbl_sel.setVisible(n > 0)
        # 多选/已勾选时，批量入口改名“批量操作”；setMenu 自带箭头，文本不写 ▾
        self.b_ops.setText("⚙ 批量操作" if (self.grid.multi_on() or n) else "⚙ 操作")

    def _clear_sel(self):
        self.grid.clear_sel()

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
            m.addAction("🎙 自动识别口播：" + ("开" if self._asr_autorun() else "关"),
                        self._toggle_asr)
            m.addAction("🌲 " + ("隐藏筛选树" if self._tree_visible else "显示筛选树"),
                        self._toggle_tree)
            m.addSeparator()
            m.addAction("🏷 绑定产品…", self._act_bind)
            m.addAction("🎬 批量设置视频类别…", self._act_set_category)
            m.addAction("🏷 批量设置显示名称…", self._act_set_display)
            vm = StyledMenu("🎙 口播", m)
            vm.addAction("批量标记为口播素材", self._act_mark_voiceover)
            vm.addAction("批量取消口播标记", self._act_unmark_voiceover)
            vm.addAction("批量清除口播稿", self._act_clear_voiceover)
            m.addMenu(vm)
            m.addSeparator()
            m.addAction("✏ 批量重命名…", self._act_rename)
            m.addSeparator()
            m.addAction("🗑 移入回收站", self._act_delete)

    def _require_sel(self):
        rows = self.grid.selected_rows()
        if not rows:
            QMessageBox.information(self, "先选几条",
                                    "先空白处拖动框选，或右键空白处→开启多选后点选要操作的成品。")
        return rows

    def _act_select_all(self):
        self.grid.set_multi(True)
        self.grid.select_all()

    def _act_invert(self):
        """反选：已勾的去掉、没勾的勾上（多选未开时先自动开启）。"""
        self.grid.set_multi(True)
        self.grid.invert()

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
        self.grid.set_multi(False)
        self.grid.clear_sel()
        self._update_view_buttons()
        self._apply_tree_visible()
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
        self.grid.discard_keys(ok)
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
