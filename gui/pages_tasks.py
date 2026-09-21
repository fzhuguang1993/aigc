"""
gui/pages_tasks.py —— 任务中心
分页表格 · 图形进度条 · 视频双击预览 · 右键打开位置 · 悬停预览浮层 ·
搜索/替换/日期筛选 · 扫描新任务 · 导入导出（含模板）
"""
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Qt, QDate, QPoint
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
                               QMenu, QMessageBox, QFileDialog, QAbstractItemView,
                               QLineEdit, QDateEdit, QCheckBox)

from registry.manager import REG
from store import task_store, product_store
from workers.submit import do_submit, SubmitOptions
from gui.dialogs import TaskDialog, FindReplaceDialog
from gui.delegates import ProgressDelegate, ProgressRole
from gui.widgets import VideoPlayerDialog, HoverPreview
from gui.header import page_header
from gui.tablekit import FieldManagerDialog, apply_field_layout, enable_drag_with_lock

STATUS_COLORS = {"completed": "#00A870", "failed": "#F54A45", "error": "#F54A45",
                 "cancelled": "#8F959E", "submitted": "#3370FF", "queued": "#3370FF",
                 "starting": "#FF8D19", "cancelling": "#FF8D19"}
RETRYABLE = {"failed", "error", "cancelled"}
DATA_HEADERS = ["任务ID", "编号", "品名", "提示词", "状态", "账号",
                "运行", "成功", "口播文案", "更新时间", "输出文件"]
HEADERS = [""] + DATA_HEADERS          # 第 0 列：勾选框
CHECK_COL = 0
COL_PID, COL_NUM, COL_PRODUCT, COL_PROMPT, COL_STATUS, COL_ACCOUNT, \
    COL_RUNS, COL_OK, COL_SCRIPT, COL_UPDATED, COL_OUT = range(1, 12)
NUM_COLS = (COL_PID, COL_RUNS, COL_OK)
STRETCH_COLS = (COL_PROMPT, COL_OUT)
PREVIEW_COLS = {COL_PROMPT: "提示词", COL_SCRIPT: "口播文案"}   # 悬停浮层预览列
BAR_STATUS = ("running", "starting")   # 用图形进度条显示的状态
LEFT_COLS = {COL_PROMPT, COL_SCRIPT, COL_OUT}   # 这几列左对齐，其余居中


class SubmitWorker(QThread):
    """后台提交线程，避免上传参考图时卡住界面"""
    log_msg = Signal(str)
    all_done = Signal()

    def __init__(self, items, options):
        super().__init__()
        self.items = items
        self.options = options   # 提交瞬间的选项快照，避免全局态

    def run(self):
        for tid, product, prompt in self.items:
            jid, err, acc = do_submit(tid, product, prompt, self.options)
            if jid:
                self.log_msg.emit(f"✓ 任务{tid} 已提交 [{acc}]")
            else:
                self.log_msg.emit(f"✗ 任务{tid} 提交失败: {err}")
        self.all_done.emit()


class TasksPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(6)

        lay.addWidget(page_header("任务中心", "双击输出列播放 · 悬停看全文 · 右键更多", icon="📋"))

        # ---------- 工具栏 ----------
        bar = QHBoxLayout()
        b_new = QPushButton("＋ 新建任务")
        b_run = QPushButton("▶ 执行选中")
        b_runall = QPushButton("⏩ 执行全部待办")
        b_scan = QPushButton("🔍 扫描新任务")
        b_del = QPushButton("🗑 删除")
        b_io = QPushButton("📁 导入/导出")
        b_io.setMenu(self._build_io_menu())
        b_fields = QPushButton("⚟ 字段管理")
        b_fields.setObjectName("GhostBtn")
        b_fields.setToolTip("控制显示哪些列，拖动表头可直接调整列顺序")
        for b in (b_del, b_io, b_fields):
            b.setObjectName("GhostBtn")
        for b in (b_new, b_run, b_runall, b_scan, b_del, b_io, b_fields):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(QLabel("时长"))
        self.cb_duration = QComboBox()
        self.cb_duration.addItems([f"{i}秒" for i in range(5, 16)])   # 5-15 秒可选
        self.cb_duration.setCurrentIndex(0)
        bar.addWidget(self.cb_duration)
        bar.addWidget(QLabel("KOL"))
        self.cb_kol = QComboBox()
        self.cb_kol.addItems(["不使用"] + product_store.kol_names())
        bar.addWidget(self.cb_kol)
        lay.addLayout(bar)

        # ---------- 筛选栏 ----------
        fbar = QHBoxLayout()
        fbar.addWidget(QLabel("🔍"))
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索品名/编号/提示词/口播文案，回车过滤")
        self.ed_search.setFixedWidth(240)
        self.ed_search.returnPressed.connect(lambda: (self._goto_first_page(), self.refresh()))
        fbar.addWidget(self.ed_search)
        b_replace = QPushButton("🔁 搜索替换")
        b_replace.setObjectName("GhostBtn")
        b_replace.clicked.connect(self._find_replace)
        fbar.addWidget(b_replace)
        fbar.addSpacing(16)
        self.cb_date = QCheckBox("按日期筛选")
        self.cb_date.toggled.connect(self._toggle_date_filter)
        fbar.addWidget(self.cb_date)
        self.de_start = QDateEdit(QDate.currentDate().addDays(-7))
        self.de_end = QDateEdit(QDate.currentDate())
        for de in (self.de_start, self.de_end):
            de.setCalendarPopup(True)
            de.setDisplayFormat("yyyy-MM-dd")
            de.setEnabled(False)
            de.dateChanged.connect(self.refresh)
        fbar.addWidget(self.de_start)
        fbar.addWidget(QLabel("至"))
        fbar.addWidget(self.de_end)
        b_clear = QPushButton("✕ 清除筛选")
        b_clear.setObjectName("GhostBtn")
        b_clear.clicked.connect(self._clear_filters)
        fbar.addWidget(b_clear)
        fbar.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("PageTip")
        fbar.addWidget(self.lbl_count)
        lay.addLayout(fbar)

        # ---------- 表格 ----------
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setMouseTracking(True)
        self.table.setItemDelegate(ProgressDelegate(self.table))
        # 勾选列：固定宽度、不参与排序
        self.table.setColumnWidth(CHECK_COL, 36)
        self.table.horizontalHeader().setSortIndicatorShown(False)
        enable_drag_with_lock(self.table, lock_count=1)
        for c in STRETCH_COLS:
            self.table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        widths = {COL_PID: 60, COL_NUM: 50, COL_PRODUCT: 110, COL_STATUS: 130,
                  COL_ACCOUNT: 70, COL_RUNS: 50, COL_OK: 50, COL_SCRIPT: 90, COL_UPDATED: 95}
        for c, w in widths.items():
            self.table.setColumnWidth(c, w)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellEntered.connect(self._on_cell_entered)
        self.table.cellDoubleClicked.connect(self._on_cell_double)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self.table.viewport().installEventFilter(self)
        lay.addWidget(self.table)

        # ---------- 分页栏 ----------
        pbar = QHBoxLayout()
        self.lbl_sel = QLabel("已选 0 个")
        self.lbl_sel.setObjectName("PageTip")
        pbar.addWidget(self.lbl_sel)
        b_clearsel = QPushButton("清除选择")
        b_clearsel.setObjectName("GhostBtn")
        b_clearsel.clicked.connect(self._clear_selection)
        pbar.addWidget(b_clearsel)
        pbar.addStretch(1)
        self.b_prev = QPushButton("‹ 上一页")
        self.b_next = QPushButton("下一页 ›")
        self.b_prev.setObjectName("GhostBtn")
        self.b_next.setObjectName("GhostBtn")
        self.lbl_page = QLabel("第 1 / 1 页")
        self.cb_psize = QComboBox()
        self.cb_psize.addItems(["20 条/页", "50 条/页", "100 条/页", "全部"])
        self.cb_psize.setCurrentIndex(0)
        self.b_prev.clicked.connect(lambda: self._page_step(-1))
        self.b_next.clicked.connect(lambda: self._page_step(1))
        self.cb_psize.currentIndexChanged.connect(lambda: (self._goto_first_page(), self.refresh()))
        pbar.addWidget(self.b_prev)
        pbar.addWidget(self.lbl_page)
        pbar.addWidget(self.b_next)
        pbar.addSpacing(12)
        pbar.addWidget(QLabel("每页"))
        pbar.addWidget(self.cb_psize)
        lay.addLayout(pbar)

        self.lbl_tip = QLabel("")
        self.lbl_tip.setObjectName("InlineTip")
        lay.addWidget(self.lbl_tip)

        # ---------- 状态 ----------
        self.worker = None
        self._filtered = []        # [(tid, row), ...] 筛选后的全量
        self._selected_ids = set() # 跨页保留的选中集合
        self._page = 1
        self._syncing = False      # 恢复选中时屏蔽 selectionChanged
        self._player = None        # 视频窗口引用，防 GC
        self.hover = HoverPreview()  # 悬停预览浮层

        b_new.clicked.connect(self._new_task)
        b_del.clicked.connect(self._del_selected)
        b_run.clicked.connect(lambda: self._run(True))
        b_runall.clicked.connect(lambda: self._run(False))
        b_scan.clicked.connect(self._scan_new)
        b_fields.clicked.connect(self._manage_fields)

    # ============ 导入/导出菜单 ============
    def _build_io_menu(self):
        m = QMenu()
        m.addAction("📥 从 Excel 导入任务", self._import_excel)
        m.addAction("📄 下载导入模板", self._download_template)
        m.addSeparator()
        m.addAction("导出任务为 Excel", lambda: self._do_export("excel"))
        m.addAction("导出任务为 CSV", lambda: self._do_export("csv"))
        m.addAction("导出任务为 JSON", lambda: self._do_export("json"))
        m.addSeparator()
        m.addAction("导出执行记录", self._export_runs)
        return m

    def _download_template(self):
        try:
            path = task_store.write_import_template()
            if QMessageBox.question(self, "模板已生成",
                                    f"{path}\n\n是否打开所在文件夹？") \
                    == QMessageBox.StandardButton.Yes:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception as e:
            QMessageBox.critical(self, "生成失败", str(e))

    def _export_runs(self):
        try:
            self.lbl_tip.setText(f"已导出执行记录：{task_store.export_runs('excel')}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    # ============ 扫描新任务 ============
    def _scan_new(self):
        news = [tid for tid, _ in task_store.scan_new_rows()]
        if not news:
            self.lbl_tip.setText("扫描完成：没有新任务（新任务=有提示词且从未运行过）")
            return
        if QMessageBox.question(
                self, "扫描到新任务",
                f"发现 {len(news)} 个新任务（有提示词、未运行过）。\n是否帮你勾选它们？") \
                != QMessageBox.StandardButton.Yes:
            return
        self._selected_ids = set(news)
        # 跳到第一个新任务所在页
        pos = next((i for i, (tid, _) in enumerate(self._filtered) if tid in self._selected_ids), 0)
        self._page = pos // self._page_size() + 1
        self.refresh()
        self.lbl_tip.setText(f"已勾选 {len(news)} 个新任务，可直接点「执行选中」")

    # ============ 分页 ============
    def _page_size(self):
        return [20, 50, 100, 10 ** 9][self.cb_psize.currentIndex()]

    def _page_count(self):
        import math
        return max(1, math.ceil(len(self._filtered) / self._page_size()))

    def _goto_first_page(self):
        self._page = 1

    def _page_step(self, d):
        self._page = max(1, min(self._page + d, self._page_count()))
        self.hover.hide()
        self.refresh()

    # ============ 筛选 ============
    def _toggle_date_filter(self, on):
        self.de_start.setEnabled(on)
        self.de_end.setEnabled(on)
        self._goto_first_page()
        self.refresh()

    def _clear_filters(self):
        self.ed_search.clear()
        self.cb_date.setChecked(False)   # 触发 toggled→refresh
        self._goto_first_page()
        self.refresh()

    def _match_filter(self, row):
        kw = self.ed_search.text().strip().lower()
        if kw:
            hay = " ".join([str(row["品名"]), str(row["编号"]),
                            str(row["提示词"]), str(row["口播文案"])]).lower()
            if kw not in hay:
                return False
        if self.cb_date.isChecked():
            d = str(row["更新时间"])[:10]
            if not d:
                return False
            s = self.de_start.date().toString("yyyy-MM-dd")
            e = self.de_end.date().toString("yyyy-MM-dd")
            if not (s <= d <= e):
                return False
        return True

    # ============ 刷新（筛选→分页→填充）============
    def refresh(self):
        self.hover.hide()
        # KOL 列表跟随产品中心变化（保留当前选择）
        cur_kol = self.cb_kol.currentText()
        self.cb_kol.blockSignals(True)
        self.cb_kol.clear()
        self.cb_kol.addItems(["不使用"] + product_store.kol_names())
        idx = self.cb_kol.findText(cur_kol)
        self.cb_kol.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_kol.blockSignals(False)
        df = task_store.list_tasks_df()
        self._filtered = [(int(row["_id"]), row) for _, row in df.iterrows()
                          if self._match_filter(row)]
        self._page = max(1, min(self._page, self._page_count()))
        size = self._page_size()
        start = (self._page - 1) * size
        page_rows = self._filtered[start:start + size]

        active = {t["row_idx"]: t for t in REG.active()}
        header = self.table.horizontalHeader()
        sort_col, sort_order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        if sort_col == CHECK_COL:
            sort_col = COL_PID
        self.table.setSortingEnabled(False)
        self._syncing = True
        self.table.setRowCount(0)
        for tid, row in page_rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            status = str(row["状态"]).strip()
            at = active.get(tid)
            out_path = str(row["输出"]).split(";")[0].strip()
            for c in range(len(HEADERS)):
                item = self._make_item(tid, row, r, c, at, status, out_path)
                self.table.setItem(r, c, item)
            if at and at["status"] in BAR_STATUS:
                self.table.setRowHeight(r, 30)
        self.table.setSortingEnabled(True)
        self.table.sortItems(sort_col, sort_order)
        self._syncing = False

        self.lbl_count.setText(f"显示 {len(self._filtered)}/{len(df)} 条")
        self.lbl_page.setText(f"第 {self._page} / {self._page_count()} 页")
        self.b_prev.setEnabled(self._page > 1)
        self.b_next.setEnabled(self._page < self._page_count())
        self._update_sel_label()

    def _make_item(self, tid, row, r, c, at, status, out_path):
        item = QTableWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, tid)     # 行→任务ID，排序后仍可靠
        pct = None
        if c == CHECK_COL:
            item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            item.setCheckState(Qt.CheckState.Checked if tid in self._selected_ids
                               else Qt.CheckState.Unchecked)
            item.setToolTip("勾选后可跨页保留，支持批量执行/删除")
        elif c == COL_PID:
            item.setData(Qt.ItemDataRole.DisplayRole, tid)
        elif c == COL_NUM:
            item.setText(str(row["编号"]))
        elif c == COL_PRODUCT:
            item.setText(str(row["品名"]))
        elif c == COL_PROMPT:
            p = str(row["提示词"])
            item.setText(p[:40])
            item.setToolTip("")                          # 用悬停浮层替代默认气泡
        elif c == COL_STATUS:
            show = status or "待执行"
            color_key = status
            if at:
                show, color_key = at["status"], at["status"]
                if at["status"] in BAR_STATUS:
                    pct = int(at["progress"] or 0)
                    show = ""                            # 由 delegate 画进度条
            item.setText(show)
            item.setForeground(QColor(STATUS_COLORS.get(color_key, "#8A94A6")))
        elif c == COL_ACCOUNT:
            item.setText(str(row["账号"]))
        elif c == COL_RUNS:
            item.setData(Qt.ItemDataRole.DisplayRole, int(row["运行次数"] or 0))
        elif c == COL_OK:
            item.setData(Qt.ItemDataRole.DisplayRole, int(row["成功次数"] or 0))
        elif c == COL_SCRIPT:
            item.setText(str(row["口播文案"])[:30])
            item.setToolTip("")
        elif c == COL_UPDATED:
            item.setText(str(row["更新时间"])[5:16])
        elif c == COL_OUT:
            item.setText(Path(out_path).name if out_path else "")
            item.setData(Qt.ItemDataRole.UserRole + 2, out_path)   # 完整路径
            item.setToolTip(out_path)
        if pct is not None:
            item.setData(ProgressRole, pct)
        if c in LEFT_COLS:
            item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        else:
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    # ============ 选中管理（勾选框，跨页保留） ============
    def _row_tid(self, r):
        item = self.table.item(r, CHECK_COL)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_item_changed(self, item):
        if self._syncing or item.column() != CHECK_COL:
            return
        tid = item.data(Qt.ItemDataRole.UserRole)
        if tid is None:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._selected_ids.add(tid)
        else:
            self._selected_ids.discard(tid)
        self._update_sel_label()

    def _on_header_clicked(self, logical):
        """点勾选列表头：全选/取消本页"""
        if logical != CHECK_COL or self.table.rowCount() == 0:
            return
        items = [self.table.item(r, CHECK_COL) for r in range(self.table.rowCount())]
        all_on = all(i.checkState() == Qt.CheckState.Checked for i in items)
        self._syncing = True
        for i in items:
            i.setCheckState(Qt.CheckState.Unchecked if all_on
                            else Qt.CheckState.Checked)
        self._syncing = False
        page_ids = {i.data(Qt.ItemDataRole.UserRole) for i in items}
        if all_on:
            self._selected_ids -= page_ids
        else:
            self._selected_ids |= page_ids
        self._update_sel_label()

    def _clear_selection(self):
        self._selected_ids.clear()
        self._syncing = True
        for r in range(self.table.rowCount()):
            self.table.item(r, CHECK_COL).setCheckState(Qt.CheckState.Unchecked)
        self._syncing = False
        self._update_sel_label()

    def _update_sel_label(self):
        self.lbl_sel.setText(f"已选 {len(self._selected_ids)} 个")

    # ============ 悬停预览浮层 ============
    def _on_cell_entered(self, r, c):
        if c not in PREVIEW_COLS:
            self.hover.hide()
            return
        if self.table.item(r, c) is None:
            self.hover.hide()
            return
        text = self._filtered_full_text(r, c)
        rect = self.table.visualItemRect(self.table.item(r, c))
        pos = self.table.viewport().mapToGlobal(rect.bottomRight())
        self.hover.show_at(text, QPoint(pos.x() + 8, pos.y() + 4))

    def _filtered_full_text(self, r, c):
        tid = self._row_tid(r)
        for t, row in self._filtered:
            if t == tid:
                return str(row["提示词"] if c == COL_PROMPT else row["口播文案"])
        return ""

    def eventFilter(self, obj, e):
        from PySide6.QtCore import QEvent
        if obj is self.table.viewport() and e.type() == QEvent.Type.Leave:
            self.hover.hide()
        return super().eventFilter(obj, e)

    # ============ 双击 / 右键 ============
    def _on_cell_double(self, r, c):
        if c == COL_OUT:
            self._play_video(r)
        elif c != CHECK_COL:
            self._edit_current(r)

    def _play_video(self, r):
        item = self.table.item(r, COL_OUT)
        path = item.data(Qt.ItemDataRole.UserRole + 2) if item else ""
        if not path:
            QMessageBox.information(self, "提示", "该任务还没有输出视频")
            return
        if not Path(path).exists():
            QMessageBox.warning(self, "文件不存在", f"找不到视频文件：\n{path}")
            return
        self._player = VideoPlayerDialog(self, path)
        self._player.show()

    def _open_location(self, r):
        item = self.table.item(r, COL_OUT)
        path = item.data(Qt.ItemDataRole.UserRole + 2) if item else ""
        if path and Path(path).exists():
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            QMessageBox.information(self, "提示", "输出文件不存在或已被移动")

    def _on_context_menu(self, pos):
        """表格右键菜单：播放/打开位置/编辑/删除"""
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        r = idx.row()
        item_out = self.table.item(r, COL_OUT)
        path = item_out.data(Qt.ItemDataRole.UserRole + 2) if item_out else ""
        menu = QMenu(self)
        if path:
            menu.addAction("▶ 播放预览", lambda: self._play_video(r))
            menu.addAction("📂 打开存放位置", lambda: self._open_location(r))
            menu.addSeparator()
        menu.addAction("✎ 编辑任务", lambda: self._edit_current(r))
        menu.addAction("🧐 口播规范检测", lambda: self._check_script(r))
        menu.addAction("🗑 删除任务",
                       lambda: self._del_tasks({self._row_tid(r)}))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _check_script(self, r):
        """按产品规范卡 + 风控政策检测该任务的口播/提示词合规性"""
        tid = self._row_tid(r)
        t = task_store.get_task(tid)
        if not t:
            return
        has_script = bool((t["script_text"] or "").strip())
        text = t["script_text"] if has_script else (t["prompt"] or "")
        src = "口播文案" if has_script else "提示词（口播未提取，退而检提示词）"
        pname = t["product"] or ""
        spec = {}
        p = product_store.get_by_name(pname) if pname else None
        if p:
            spec = product_store.get_spec(p["id"])
        from gui.dialogs_spec import SpecCheckDialog
        title = (f"任务{t['num'] or tid} · {pname or '未填品名'} · 检测来源：{src}"
                 + ("" if has_script or (t["prompt"] or "").strip() else " · 无内容可检"))
        self._spec_dlg = SpecCheckDialog(self, title, text, spec, pname)
        self._spec_dlg.show()

    def _manage_fields(self):
        """字段管理：显示/隐藏列；顺序可在此拖，也可直接拖表头"""
        h = self.table.horizontalHeader()
        order = [h.logicalIndex(v) for v in range(1, self.table.columnCount())]
        hidden = {c for c in order if self.table.isColumnHidden(c)}
        cols = [(i, DATA_HEADERS[i - 1]) for i in range(1, len(HEADERS))]
        d = FieldManagerDialog(self, cols, order, hidden)
        if d.exec():
            apply_field_layout(self.table, d.order, d.hidden, first_locked=1)

    def _edit_current(self, r=None):
        if r is None:
            return
        tid = self._row_tid(r)
        t = task_store.get_task(tid)
        if not t:
            return
        data = TaskDialog.ask(self, {"num": t["num"], "product": t["product"],
                                     "prompt": t["prompt"]})
        if data:
            task_store.update_row(tid, **{"编号": data["num"], "品名": data["product"],
                                          "提示词": data["prompt"],
                                          "状态": "", "运行次数": 0})
            self.refresh()

    # ============ 操作 ============
    def _current_options(self):
        """从控件读取当前时长/KOL，生成提交选项快照"""
        return SubmitOptions(
            duration=5 + self.cb_duration.currentIndex(),
            kol=None if self.cb_kol.currentIndex() == 0 else self.cb_kol.currentText())

    def _new_task(self):
        data = TaskDialog.ask(self)
        if data and data["prompt"]:
            task_store.add_task(data["num"] or (task_store.task_count() + 1),
                                data["product"], data["prompt"])
            self.refresh()

    def _del_selected(self):
        if not self._selected_ids:
            self.lbl_tip.setText("没有可删除的任务：请先在表格左侧勾选")
            return
        self._del_tasks(set(self._selected_ids))

    def _del_tasks(self, tids):
        tids = {t for t in tids if t is not None}
        if not tids:
            return
        if QMessageBox.question(self, "确认",
                                f"删除选中的 {len(tids)} 个任务？（不影响已生成的视频）") \
                == QMessageBox.StandardButton.Yes:
            task_store.delete_tasks(list(tids))
            self._selected_ids -= set(tids)
            self.refresh()

    def _find_replace(self):
        sel = list(self._selected_ids)
        d = FindReplaceDialog.ask(self, selected_count=len(sel))
        if not d:
            return
        find, repl = d["find"], d["replace"]
        if d["only_selected"] and sel:
            tids = sel
        else:
            df = task_store.list_tasks_df()
            tids = [int(r["_id"]) for _, r in df.iterrows()]
        count = 0
        for tid in tids:
            t = task_store.get_task(tid)
            prompt = t["prompt"] or ""
            if find in prompt:
                task_store.update_row(tid, **{"提示词": prompt.replace(find, repl)})
                count += 1
        self.lbl_tip.setText(f"替换完成：{count} 个任务的提示词已更新"
                             if count else f"未找到包含「{find}」的提示词")
        self.refresh()

    def _run(self, selected_only):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "提示", "还有任务正在提交，请稍候…")
            return
        if selected_only:
            tids = sorted(self._selected_ids)
            if not tids:
                self.lbl_tip.setText("先在表格里勾选任务（第 1 列复选框，支持跨页保留），再点「执行选中」")
                return
        else:
            tids = [tid for tid, _ in self._filtered]
        items = []
        for tid in tids:
            t = task_store.get_task(tid)
            if not t:
                continue
            prompt = (t["prompt"] or "").strip()
            if not prompt:
                continue
            if t["status"] and t["status"] not in RETRYABLE and int(t["runs"] or 0) > 0:
                continue
            if t["status"] in RETRYABLE:
                task_store.update_row(tid, **{"状态": ""})
            items.append((tid, t["product"], prompt))
        if not items:
            self.lbl_tip.setText("没有可执行的任务：请确认已填写提示词，且任务尚未成功执行过")
            return
        self.lbl_tip.setText(f"正在提交 {len(items)} 个任务…")
        self.worker = SubmitWorker(items, self._current_options())
        self.worker.log_msg.connect(lambda m: self.lbl_tip.setText(m))
        self.worker.all_done.connect(
            lambda: self.lbl_tip.setText("提交完成，云端生成中…进度条将实时更新，完成后自动下载到 outputs/"))
        self.worker.start()

    def _import_excel(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择任务 Excel", "", "Excel 文件 (*.xlsx)")
        if not path:
            return
        try:
            n = task_store.import_from_excel(path)
            self.lbl_tip.setText(f"已导入 {n} 条任务")
            self._goto_first_page()
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "导入失败", str(e))

    def _do_export(self, fmt):
        try:
            self.lbl_tip.setText(f"已导出：{task_store.export_tasks(fmt)}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))
