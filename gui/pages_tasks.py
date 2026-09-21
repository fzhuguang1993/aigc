"""
gui/pages_tasks.py —— 任务中心
分页表格 · 图形进度条 · 视频双击预览 · 右键打开位置 · 悬停预览浮层 ·
搜索/替换/日期筛选 · 扫描新任务 · 导入导出（含模板）
"""
from pathlib import Path
import random
import time

from PySide6.QtCore import QThread, Signal, Qt, QDate, QPoint
from PySide6.QtGui import QColor, QCursor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
                               QMenu, QMessageBox, QFileDialog, QAbstractItemView,
                               QLineEdit, QDateEdit, QCheckBox, QInputDialog)

from registry.manager import REG
from core.config import DEFAULT_STEPS, SUBMIT_JITTER
from store import task_store, product_store, app_state
from utils.desktop_utils import reveal_in_folder
from workers.submit import do_submit, cancel_one, SubmitOptions
from gui.dialogs import (TaskDialog, FindReplaceDialog, ForceRerunDialog,
                         ScriptBindDialog)
from gui.delegates import ProgressDelegate, ProgressRole
from gui.widgets import VideoPlayerDialog, HoverPreview
from gui.header import page_header
from gui.tablekit import FieldManagerDialog, apply_field_layout, enable_drag_with_lock

STATUS_COLORS = {"completed": "#00A870", "failed": "#F54A45", "error": "#F54A45",
                 "cancelled": "#8F959E", "submitted": "#3370FF", "queued": "#3370FF",
                 "starting": "#FF8D19", "cancelling": "#FF8D19"}
RETRYABLE = {"failed", "error", "cancelled"}
# 列表字段全量展示（含以前的隐藏技术字段），“字段管理”里逐个可勾可拖：
# 任务一多，使用者需要自己能控制看哪几列、列序怎么排
DATA_HEADERS = ["任务ID", "编号", "品名", "备注", "脚本", "提示词", "状态", "时长",
                "执行用时", "账号", "运行", "成功", "取消", "口播文案", "分镜数",
                "更新时间", "输出文件", "job_id", "URL"]
HEADERS = [""] + DATA_HEADERS          # 第 0 列：勾选框
CHECK_COL = 0
COL_PID, COL_NUM, COL_PRODUCT, COL_REMARK, COL_SCRIPT, COL_PROMPT, COL_STATUS, \
    COL_DUR, COL_RUNSEC, COL_ACCOUNT, COL_RUNS, COL_OK, COL_CANCEL, COL_VOICE, \
    COL_STORY, COL_UPDATED, COL_OUT, COL_JOB, COL_URL = range(1, len(DATA_HEADERS) + 1)
NUM_COLS = (COL_PID, COL_RUNS, COL_OK, COL_CANCEL, COL_DUR, COL_RUNSEC, COL_STORY)
STRETCH_COLS = (COL_PROMPT, COL_OUT)
# 单击/空格弹出全文预览的三列（提示词/脚本/口播文案），值即 df 列名
PREVIEW_COLS = {COL_PROMPT: "提示词", COL_SCRIPT: "脚本", COL_VOICE: "口播文案"}
BAR_STATUS = ("running", "starting")   # 用图形进度条显示的状态
LEFT_COLS = {COL_PROMPT, COL_SCRIPT, COL_VOICE, COL_OUT, COL_REMARK,
             COL_JOB, COL_URL}   # 这几列左对齐，其余居中
# 技术字段：默认收起（不删列，去“字段管理”能勾出来），新手看到的还是原来的宽度
DEFAULT_HIDDEN = (COL_JOB, COL_URL)
# 状态下拉：“全部”+ 按使用者能读懂的口径分组（不暴露云端原始状态词）
STATUS_FILTERS = [("全部", None),
                  ("待执行", ("",)),
                  ("排队/提交", ("submitted", "queued")),
                  ("进行中", ("running", "starting", "cancelling")),
                  ("完成", ("completed", "succeeded")),
                  ("失败", ("failed", "error", "timeout")),
                  ("已取消", ("cancelled",))]
NO_TAG = "（无备注）"        # 备注下拉的虚拟选项：一筛就只看没标的
BLANK = "（未填）"


class SubmitWorker(QThread):
    """后台提交线程，避免上传参考图时卡住界面"""
    log_msg = Signal(str)
    all_done = Signal(list)      # 参数：[(任务ID, 失败原因), …]，全部成功时为空列表

    def __init__(self, items, options):
        super().__init__()
        self.items = items
        self.options = options   # 提交瞬间的选项快照，避免全局态

    def run(self):
        failed = []
        for i, (tid, product, prompt) in enumerate(self.items):
            if i:
                # 两条之间错开随机间隔：三台配置相同的电脑同时点「执行」时，
                # 撞同一毫秒就会一起选中同一条线路
                time.sleep(random.uniform(*SUBMIT_JITTER))
            jid, err, acc = do_submit(tid, product, prompt, self.options)
            if jid:
                self.log_msg.emit(f"✓ 任务{tid} 已提交 [{acc}]")
            else:
                failed.append((tid, err))
                self.log_msg.emit(f"✗ 任务{tid} 提交失败: {err}")
        self.all_done.emit(failed)


class TasksPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(6)

        lay.addWidget(page_header("任务中心", "双击输出列播放 · 单击/空格弹出全文 · 任意键关闭 · 右键更多", icon="📋"))

        # ---------- 工具栏 ----------
        bar = QHBoxLayout()
        b_new = QPushButton("＋ 新建任务")
        b_run = QPushButton("▶ 执行选中")
        b_runall = QPushButton("⏩ 执行全部待办")
        b_scan = QPushButton("🔍 扫描新任务")
        b_cancel = QPushButton("⏹ 取消选中")
        b_del = QPushButton("🗑 删除")
        b_io = QPushButton("📁 导入/导出")
        # 注意：QPushButton.setMenu 配合全局样式表会导致点击无反应，改为手动弹出菜单
        self._io_menu = self._build_io_menu()
        b_io.clicked.connect(
            lambda: self._io_menu.exec(b_io.mapToGlobal(QPoint(0, b_io.height()))))
        b_fields = QPushButton("⚟ 字段管理")
        b_fields.setObjectName("GhostBtn")
        b_fields.setToolTip("控制显示哪些列，拖动表头可直接调整列顺序")
        for b in (b_cancel, b_del, b_io, b_fields):
            b.setObjectName("GhostBtn")
        for b in (b_new, b_run, b_runall, b_scan, b_cancel, b_del, b_io, b_fields):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(QLabel("时长"))
        self.cb_duration = QComboBox()
        self.cb_duration.addItems([f"{i}秒" for i in range(2, 16)])   # 2-15 秒可选
        self.cb_duration.setCurrentIndex(3)                           # 默认 5 秒
        bar.addWidget(self.cb_duration)
        bar.addWidget(QLabel("步数"))
        self.cb_steps = QComboBox()                                   # AI 生成步数（1-50）
        self.cb_steps.addItems([str(i) for i in range(1, 51)])
        self.cb_steps.setCurrentIndex(DEFAULT_STEPS - 1)
        self.cb_steps.setToolTip("生成步数：越大细节越好、耗时更长（1-50）\n"
                                 "作为 parameters.inference_steps 传给云端，下次启动沿用本次值")
        bar.addWidget(self.cb_steps)
        bar.addWidget(QLabel("KOL"))
        self.cb_kol = QComboBox()
        self.cb_kol.addItems(["不使用"] + product_store.kol_names())
        bar.addWidget(self.cb_kol)
        lay.addLayout(bar)
        self._restore_exec_params()          # 沿用上次用过的时长/步数/KOL

        # ---------- 筛选栏 ----------
        fbar = QHBoxLayout()
        fbar.addWidget(QLabel("🔍"))
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索品名/编号/脚本/提示词/口播文案，回车过滤")
        self.ed_search.setFixedWidth(240)
        self.ed_search.returnPressed.connect(lambda: (self._goto_first_page(), self.refresh()))
        fbar.addWidget(self.ed_search)
        b_replace = QPushButton("🔁 搜索替换/定位")
        b_replace.setObjectName("GhostBtn")
        b_replace.setToolTip("批量改写提示词；改完自动勾选并置顶那批命中的任务，直接「执行选中」")
        b_replace.clicked.connect(self._find_replace)
        fbar.addWidget(b_replace)
        fbar.addSpacing(12)
        # 下拉筛选：任务上百条后光靠搜索框拦不住，要能按品名/状态/备注直接分档
        self.cb_f_product = QComboBox()
        self.cb_f_status = QComboBox()
        self.cb_f_remark = QComboBox()
        for lbl, cb in (("品名", self.cb_f_product),
                        ("状态", self.cb_f_status),
                        ("备注", self.cb_f_remark)):
            cb.setMinimumContentsLength(8)
            cb.setToolTip(f"按{lbl}筛选任务（与搜索框/日期是“并且”关系）")
            cb.currentIndexChanged.connect(lambda *_a: (self._goto_first_page(),
                                                        self.refresh()))
            fbar.addWidget(QLabel(lbl))
            fbar.addWidget(cb)
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
        b_clear.setToolTip("清空搜索/日期条件，并取消「置顶聚集」（回到完全按编号的顺序）")
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
        widths = {COL_PID: 60, COL_NUM: 50, COL_PRODUCT: 110, COL_REMARK: 110,
                  COL_SCRIPT: 90, COL_STATUS: 130, COL_DUR: 52, COL_RUNSEC: 80,
                  COL_ACCOUNT: 70, COL_RUNS: 50, COL_OK: 50, COL_CANCEL: 50,
                  COL_VOICE: 90, COL_STORY: 56, COL_UPDATED: 95,
                  COL_JOB: 110, COL_URL: 160}
        for c, w in widths.items():
            self.table.setColumnWidth(c, w)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellEntered.connect(self._on_cell_entered)
        self.table.cellClicked.connect(self._on_cell_click)
        self.table.cellDoubleClicked.connect(self._on_cell_double)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self.table.viewport().installEventFilter(self)
        self.table.installEventFilter(self)      # 空格键弹全文预览用
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
        self._gather_ids = set()   # 上一批需要置顶聚集的任务（替换命中/新导入）
        self._user_sort_col = None # 用户点过的排序列；None=未排序，保留聚集顺序
        self._page = 1
        self._syncing = False      # 恢复选中时屏蔽 selectionChanged
        self._player = None        # 视频窗口引用，防 GC
        self.hover = HoverPreview()  # 全文预览浮层（单击/空格唤起，任意键关闭）
        self._hover_cell = None      # 鼠标最后所在的 (row, col)，空格键弹整列用

        b_new.clicked.connect(self._new_task)
        b_del.clicked.connect(self._del_selected)
        b_cancel.clicked.connect(self._cancel_selected)
        b_run.clicked.connect(lambda: self._run(True))
        b_runall.clicked.connect(lambda: self._run(False))
        b_scan.clicked.connect(self._scan_new)
        b_fields.clicked.connect(self._manage_fields)
        self._restore_field_layout()      # 沿用上次列宽/列序/显隐

    # ============ 导入/导出菜单 ============
    def _build_io_menu(self):
        m = QMenu(self)
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
                reveal_in_folder(path)
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
        # 新任务可能零散分布在多页：只勾这批并置顶聚集（旧勾选不混进来，
        # 避免“执行选中”误跑到用户之前随手勾的任务），分页档位自动抬升
        self._selected_ids = set()
        n = self._gather_matches(news)
        self.lbl_tip.setText(f"已勾选并置顶 {n} 个新任务，可直接点「执行选中」")

    # ============ 分页 ============
    def _page_size(self):
        return [20, 50, 100, 10 ** 9][self.cb_psize.currentIndex()]

    _SIZES = (20, 50, 100)

    def _grow_page_size(self, need):
        """把每页显示档位抬到能装下 need 条的最小档；超过 100 条直接切「全部」"""
        target = 3 if need > self._SIZES[-1] else next(
            (i for i, s in enumerate(self._SIZES) if s >= need), 3)
        if self.cb_psize.currentIndex() < target:
            self.cb_psize.setCurrentIndex(target)

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
        self._gather_ids = set()
        self._user_sort_col = None
        self.ed_search.clear()
        self.cb_date.setChecked(False)   # 触发 toggled→refresh
        for cb in (self.cb_f_product, self.cb_f_status, self.cb_f_remark):
            cb.blockSignals(True)        # 不逐个触发 refresh
            cb.setCurrentIndex(0)
            cb.blockSignals(False)
        self._goto_first_page()
        self.refresh()

    def _match_filter(self, row):
        return (self._match_kw(row) and self._match_choice(row)
                and self._match_date(row))

    def _sync_filter_choices(self):
        """筛选候选跟着数据走：新建/导入后不必重启就能筛到新产品。

        重建期间必屏蔽信号：否则 addItem 会再触发一次 refresh（自调递归）。"""
        ch = task_store.filter_choices()
        for cb, items in ((self.cb_f_product,
                           [("", "全部"), (BLANK, BLANK)] + [(p, p) for p in ch["products"]]),
                          (self.cb_f_remark,
                           [("", "全部"), (NO_TAG, NO_TAG)] + [(t, t) for t in ch["remarks"]])):
            keep = cb.currentData()
            cb.blockSignals(True)
            cb.clear()
            for data, label in items:
                cb.addItem(label, data)
            idx = cb.findData(keep)
            cb.setCurrentIndex(idx if idx >= 0 else 0)
            cb.blockSignals(False)
        if self.cb_f_status.count() != len(STATUS_FILTERS):   # 固定档位，只填一次
            self.cb_f_status.blockSignals(True)
            self.cb_f_status.clear()
            for label, group in STATUS_FILTERS:
                self.cb_f_status.addItem(label, group)
            self.cb_f_status.blockSignals(False)

    def _match_choice(self, row):
        """品名/状态/备注三个下拉：选了具体值就必须命中（选“全部”＝不限）"""
        for cb, col, blank in ((self.cb_f_product, "品名", BLANK),
                               (self.cb_f_remark, "备注", NO_TAG)):
            want = cb.currentData()
            if not want:
                continue
            got = str(row[col]).strip()
            if want == blank:
                if got:
                    return False
            elif got != want:
                return False
        group = self.cb_f_status.currentData()
        if group and str(row["状态"]).strip() not in group:
            return False
        return True

    def _match_kw(self, row):
        """搜索关键词命中（品名/编号/脚本/提示词/口播文案/备注）；未填关键词视为命中"""
        kw = self.ed_search.text().strip().lower()
        if not kw:
            return True
        hay = " ".join([str(row["品名"]), str(row["编号"]), str(row["脚本"]),
                        str(row["提示词"]), str(row["口播文案"]),
                        str(row["备注"])]).lower()
        return kw in hay

    def _match_date(self, row):
        if self.cb_date.isChecked():
            d = str(row["更新时间"])[:10]
            if not d:
                return False
            s = self.de_start.date().toString("yyyy-MM-dd")
            e = self.de_end.date().toString("yyyy-MM-dd")
            if not (s <= d <= e):
                return False
        return True

    def _gather_matches(self, tids):
        """把一批任务挑出来：全部勾选 + 置顶聚集 + 分页档位抬到装得下。

        搜索替换命中、扫描新任务、Excel 导入（上传旧提示词）共用——任务
        可能零散分布在各页，必须聚成一排且处于勾选态，才能直接「执行选中」。
        只抬档位不降级（尊重用户选的档位）；点「✖ 清除筛选」或手点列头则回到默认序。"""
        tids = {int(t) for t in tids if t is not None}
        if not tids:
            return 0
        self._selected_ids |= tids
        self._gather_ids = tids
        self._user_sort_col = None    # 聚集优先于上一次表头排序，否则置顶会被打回
        self._grow_page_size(len(tids))
        self._goto_first_page()
        self.refresh()
        return len(tids)

    # ============ 刷新（筛选→分页→填充）============
    def refresh(self):
        if not self.hover.pinned:            # 定时刷新不打扰正在阅读的钉住浮层
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
        self._sync_filter_choices()
        self._filtered = [(int(row["_id"]), row)
                          for _, row in df.iterrows() if self._match_filter(row)]
        # 刚挑出的那一批置顶聚集（稳定排序，其余保持原序）——否则新导入的
        # 任务排在最后，与已排队的旧任务混在一起根本找不着
        if self._gather_ids:
            self._filtered.sort(key=lambda x: x[0] not in self._gather_ids)
        self._page = max(1, min(self._page, self._page_count()))
        size = self._page_size()
        start = (self._page - 1) * size
        page_rows = self._filtered[start:start + size]

        active_map = {}
        for t in REG.active():                 # 同一任务可能并发多个 job（强制重跑/抽卡）
            active_map.setdefault(t["row_idx"], []).append(t)
        header = self.table.horizontalHeader()
        # 表头排序只在用户真点过列头时生效：否则 setSortingEnabled(True) 会按默认
        # 指示列（任务ID升序）重排，刚置顶聚集的那批（ID最大）会被换回页底
        sort_col = self._user_sort_col
        sort_order = header.sortIndicatorOrder()
        if sort_col is None:
            header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        elif sort_col == CHECK_COL:
            sort_col = COL_PID
        self.table.setSortingEnabled(False)
        self._syncing = True
        self.table.setRowCount(0)
        has_bar = False
        for tid, row in page_rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            status = str(row["状态"]).strip()
            ats = active_map.get(tid)
            out_path = str(row["输出"]).split(";")[0].strip()
            for c in range(len(HEADERS)):
                item = self._make_item(tid, row, r, c, ats, status, out_path)
                self.table.setItem(r, c, item)
            if ats and any(t["status"] in BAR_STATUS for t in ats):
                self.table.setRowHeight(r, 34)
                has_bar = True
        self.table.setSortingEnabled(True)
        if sort_col is not None and sort_col >= 0:
            self.table.sortItems(sort_col, sort_order)
        self._syncing = False
        # 有进度条行才开移动画定时器，平时完全静默
        self.table.itemDelegate().set_anim_enabled(has_bar)

        self.lbl_count.setText(f"显示 {len(self._filtered)}/{len(df)} 条")
        self.lbl_page.setText(f"第 {self._page} / {self._page_count()} 页")
        self.b_prev.setEnabled(self._page > 1)
        self.b_next.setEnabled(self._page < self._page_count())
        self._update_sel_label()

    def _make_item(self, tid, row, r, c, ats, status, out_path):
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
        elif c == COL_REMARK:
            rk = str(row["备注"])
            item.setText(rk[:24])
            item.setToolTip((rk or "（双击可写备注，支持先勾选再批量写）")
                            if not rk or len(rk) <= 24 else rk)
        elif c == COL_JOB:
            item.setText(str(row["job_id"]))
            item.setToolTip(str(row["job_id"]))
        elif c == COL_URL:
            u = str(row["URL"])
            item.setText(u[:40])
            item.setToolTip(u)
        elif c == COL_SCRIPT:
            s = str(row["脚本"])
            item.setText(s[:30])
            item.setToolTip("")                          # 单击/空格看全文
        elif c == COL_PROMPT:
            p = str(row["提示词"])
            item.setText(p[:40])
            item.setToolTip("")                          # 用悬停浮层替代默认气泡
        elif c == COL_STATUS:
            show = status or "待执行"
            color_key = status
            if ats:
                if len(ats) == 1:
                    at0 = ats[0]
                    show, color_key = at0["status"], at0["status"]
                    if at0["status"] in BAR_STATUS:
                        pct = int(at0["progress"] or 0)
                        show = ""                        # 由 delegate 画进度条
                else:
                    n = len(ats)
                    color_key = "running"
                    show = f"执行中×{n}"
                    bars = [int(t["progress"] or 0) for t in ats
                            if t["status"] in BAR_STATUS]
                    if bars:
                        pct = min(bars)                  # 多路并发时以最慢一条代表整体
                        show = ""
                    item.setToolTip(f"{n} 个执行正在进行（抽卡/重跑），进度条显示最慢的一条")
            item.setText(show)
            item.setForeground(QColor(STATUS_COLORS.get(color_key, "#8A94A6")))
        elif c == COL_DUR:
            try:
                d = int(row["时长"] or 0)
            except (TypeError, ValueError):
                d = 0
            if d:
                item.setData(Qt.ItemDataRole.DisplayRole, d)   # 数值排序
            item.setText(f"{d}秒" if d else "—")
            item.setToolTip("本任务最后一次提交选的视频时长（执行后自动登记）")
        elif c == COL_RUNSEC:
            try:
                d = int(row["执行用时"] or 0)
            except (TypeError, ValueError):
                d = 0
            if d:
                item.setData(Qt.ItemDataRole.DisplayRole, d)   # 数值排序
            item.setText(f"{d // 60}分{d % 60}秒" if d >= 60 else (f"{d}秒" if d else "—"))
            item.setToolTip("最近一次有用时记录的云端执行耗时")
        elif c == COL_ACCOUNT:
            item.setText(str(row["账号"]))
        elif c == COL_RUNS:
            item.setData(Qt.ItemDataRole.DisplayRole, int(row["运行次数"] or 0))
        elif c == COL_OK:
            item.setData(Qt.ItemDataRole.DisplayRole, int(row["成功次数"] or 0))
        elif c == COL_CANCEL:
            item.setData(Qt.ItemDataRole.DisplayRole, int(row["取消次数"] or 0))
        elif c == COL_VOICE:
            item.setText(str(row["口播文案"])[:30])
            item.setToolTip("")
        elif c == COL_STORY:
            try:
                n = int(row["分镜数"] or 0)
            except (TypeError, ValueError):
                n = 0
            if n:
                item.setData(Qt.ItemDataRole.DisplayRole, n)   # 数值排序
                item.setText(str(n))
            else:
                item.setText("")                          # 识别不出就留空
            item.setToolTip("从提示词自动识别（镜头/分镜编号），识别不出留空")
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
        """点列头：记下用户排序意图（聚集排序让位于手动排序）；
        点勾选列表头 = 全选/取消本页"""
        if logical != CHECK_COL:
            self._user_sort_col = logical
            self._gather_ids = set()
            return
        if self.table.rowCount() == 0:
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

    # ============ 全文预览浮层：单击或空格弹出，任意键关闭 ============
    def _on_cell_entered(self, r, c):
        # 不再悬停自动弹（会挡鼠标移入浮层的路），只记录光标位置供空格键使用
        self._hover_cell = (r, c)

    def _on_cell_click(self, r, c):
        if c in PREVIEW_COLS:
            self._popup_preview(r, c)

    def _popup_preview(self, r, c):
        if c not in PREVIEW_COLS or self.table.item(r, c) is None:
            return
        text = self._filtered_full_text(r, c)
        if not text.strip():
            self.hover.hide()
            return
        pos = QCursor.pos()
        self.hover.show_pinned(text, QPoint(pos.x() + 12, pos.y() + 12), rich_text=True)

    def _filtered_full_text(self, r, c):
        col_name = PREVIEW_COLS.get(c)
        if not col_name:
            return ""
        tid = self._row_tid(r)
        for t, row in self._filtered:
            if t == tid:
                return str(row[col_name])
        return ""

    def eventFilter(self, obj, e):
        from PySide6.QtCore import QEvent
        if obj is self.table and e.type() == QEvent.Type.KeyPress \
                and e.key() == Qt.Key.Key_Space and self._hover_cell:
            self._popup_preview(*self._hover_cell)
            return True                     # 消费掉，避免表格另行处理空格
        if obj is self.table.viewport() and e.type() == QEvent.Type.Leave:
            self.hover.hide_soon()          # 钉住模式不受影响（浮层自行判断）
        return super().eventFilter(obj, e)

    # ============ 双击 / 右键 ============
    def _on_cell_double(self, r, c):
        if c == COL_OUT:
            self._play_video(r)
        elif c == COL_REMARK:
            self._edit_remark(r)         # 备注要的是“随手一笔”，不弹整个任务窗
        elif c != CHECK_COL:
            self._edit_current(r)

    def _edit_remark(self, r):
        """写备注：先勾选多个就一次批量写，没勾选只改当前行；留空保存＝清除。

        备注不参与执行/迭代/重跑判定，纯粹给使用者自己分组用。"""
        tids = sorted(self._selected_ids) or [self._row_tid(r)]
        tids = [t for t in tids if t is not None]
        if not tids:
            return
        single = len(tids) == 1
        cur = ((task_store.get_task(tids[0]) or {}).get("remark") or "") if single else ""
        text, ok = QInputDialog.getText(
            self, "写备注" if single else f"写备注（{len(tids)} 个任务）",
            f"任务{tids[0]} 备注：" if single
            else f"一次写入 {len(tids)} 个已勾选任务（留空保存＝清除备注）：",
            QLineEdit.EchoMode.Normal, cur)
        if not ok:
            return
        for tid in tids:
            task_store.update_row(tid, **{"备注": text.strip()})
        self.refresh()

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
            reveal_in_folder(path)
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
        ats = REG.get_all_by_row(self._row_tid(r))
        if ats:
            if len(ats) == 1:
                menu.addAction("⏹ 取消执行（云端）", lambda: self._cancel(ats[0]))
            else:
                menu.addAction(f"⏹ 取消全部执行 ×{len(ats)}（云端）",
                               lambda: self._cancel_many(ats))
            menu.addSeparator()
        if path:
            menu.addAction("▶ 播放预览", lambda: self._play_video(r))
            menu.addAction("📂 打开存放位置", lambda: self._open_location(r))
            menu.addSeparator()
        menu.addAction("✎ 编辑任务", lambda: self._edit_current(r))
        n_sel = len(self._selected_ids)
        menu.addAction(("🏷 写备注（已选 " + str(n_sel) + " 个）") if n_sel
                       else "🏷 写备注（当前任务）",
                       lambda: self._edit_remark(r))
        menu.addAction(f"📝 批量绑定脚本（{'已选 ' + str(n_sel) + ' 个' if n_sel else '当前 1 个'}）",
                       lambda: self._bind_script(r))
        menu.addAction("🧐 口播规范检测", lambda: self._check_script(r))
        menu.addAction("🗑 删除任务",
                       lambda: self._del_tasks({self._row_tid(r)}))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ============ 取消云端任务 ============
    def _cancel(self, at):
        """向云端发送取消请求；终态由轮询线程回写"""
        tid = at["row_idx"]
        if QMessageBox.question(
                self, "取消任务",
                f"任务{tid} 正在{at['status']}（{int(at['progress'] or 0)}%），"
                "确认取消？") != QMessageBox.StandardButton.Yes:
            return
        self._do_cancel(at)

    def _cancel_many(self, ats):
        """同一任务并发多路执行（抽卡/重跑）时，一次确认全部取消"""
        tid = ats[0]["row_idx"]
        if QMessageBox.question(
                self, "取消任务",
                f"任务{tid} 当前有 {len(ats)} 个执行正在进行（抽卡/重跑），"
                "确认全部取消？") != QMessageBox.StandardButton.Yes:
            return
        ok = fail = 0
        for at in ats:
            if cancel_one(at):
                ok += 1
            else:
                fail += 1
        if fail:
            QMessageBox.warning(self, "部分取消失败",
                                f"{ok} 个已发送取消请求，{fail} 个失败，详见日志")
        else:
            self.lbl_tip.setText(f"⏹ 任务{tid} 的 {ok} 个执行均已发送取消请求，等待云端确认…")
        self.refresh()

    def _do_cancel(self, at):
        tid = at["row_idx"]
        if cancel_one(at):
            self.lbl_tip.setText(f"⏹ 任务{tid} 已发送取消请求，等待云端确认…")
        else:
            QMessageBox.warning(self, "取消失败",
                                f"任务{tid} 取消请求发送失败，详见日志")
        self.refresh()

    def _cancel_selected(self):
        targets = []
        for tid in sorted(self._selected_ids):
            targets.extend(REG.get_all_by_row(tid))
        if not targets:
            self.lbl_tip.setText("没有可取消的任务：勾选的任务均未在执行")
            return
        if len(targets) == 1:
            self._cancel(targets[0])          # 只有一个执行：沿用单条确认文案
            return
        # 多个执行只弹一次确认，不再每条弹一个窗口
        n_task = len({at["row_idx"] for at in targets})
        if QMessageBox.question(
                self, "批量取消任务",
                f"选中的 {n_task} 个任务共有 {len(targets)} 个执行正在进行，"
                "确认全部取消？") != QMessageBox.StandardButton.Yes:
            return
        ok = fail = 0
        for at in targets:
            if cancel_one(at):
                ok += 1
            else:
                fail += 1
        if fail:
            QMessageBox.warning(self, "部分取消失败",
                                f"{ok} 个已发送取消请求，{fail} 个失败，详见日志")
        else:
            self.lbl_tip.setText(f"⏹ {n_task} 个任务的 {ok} 个执行均已发送取消请求，等待云端确认…")
        self.refresh()

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
        """字段管理：所有列表字段都在此显示/隐藏；顺序可在此拖，也可直接拖表头"""
        h = self.table.horizontalHeader()
        order = [h.logicalIndex(v) for v in range(1, self.table.columnCount())]
        hidden = {c for c in order if self.table.isColumnHidden(c)}
        cols = [(i, DATA_HEADERS[i - 1]) for i in range(1, len(HEADERS))]
        d = FieldManagerDialog(self, cols, order, hidden)
        if d.exec():
            apply_field_layout(self.table, d.order, d.hidden, first_locked=1)
            # 不存就会踩坑：每次开软件都要重新藏一遍 job_id/URL、重新拖一次列序
            app_state.set_value("tasks_fields",
                                {"order": list(d.order),
                                 "hidden": sorted(d.hidden)})

    def _restore_field_layout(self):
        """启动时恢复列布局；没存过时把技术字段（job_id/URL）默认收起。

        版本升级会增减列：只接受落在当前表头范围内的逻辑号，新列接在末尾。"""
        n = len(DATA_HEADERS)
        valid = set(range(1, n + 1))
        saved = app_state.get("tasks_fields") or {}
        known = [c for c in (saved.get("order") or [])
                 if isinstance(c, int) and c in valid]
        order = known + [c for c in range(1, n + 1) if c not in known]
        hidden = {c for c in (saved.get("hidden") or [])
                  if isinstance(c, int) and c in valid}
        if not known:                       # 首次使用（或升级后新增的技术列）
            hidden |= set(DEFAULT_HIDDEN)
        else:
            hidden |= {c for c in DEFAULT_HIDDEN if c not in known}
        apply_field_layout(self.table, order, hidden, first_locked=1)

    def _edit_current(self, r=None):
        if r is None:
            return
        tid = self._row_tid(r)
        t = task_store.get_task(tid)
        if not t:
            return
        data = TaskDialog.ask(self, {"num": t["num"], "product": t["product"],
                                     "script": t["script"], "prompt": t["prompt"],
                                     "remark": t["remark"]})
        if data:
            upd = {"编号": data["num"], "品名": data["product"],
                   "脚本": data["script"], "备注": data["remark"]}
            if data["prompt"] != (t["prompt"] or ""):
                # 改了提示词才重置执行态；只改脚本不影响迭代/重跑判定
                upd.update({"提示词": data["prompt"], "状态": "", "运行次数": 0})
            task_store.update_row(tid, **upd)
            self.refresh()

    def _bind_script(self, r):
        """单脚本多提示词：把一份脚本批量绑到勾选的任务（无勾选时仅当前行）；
        留空保存 = 清除关联（工厂流水线片段等不绑定任何脚本）"""
        tids = sorted(self._selected_ids) or [self._row_tid(r)]
        tids = [t for t in tids if t is not None]
        if not tids:
            return
        first = task_store.get_task(tids[0])
        current = (first or {}).get("script", "") or ""
        text = ScriptBindDialog.ask(self, count=len(tids), current=current)
        if text is None:
            return
        for tid in tids:
            task_store.update_row(tid, **{"脚本": text})
        self.lbl_tip.setText(
            f"已{'清除' if not text else '绑定'} {len(tids)} 个任务的脚本"
            + ("（留空=不关联任何脚本）" if not text else ""))
        self.refresh()

    # ============ 操作 ============
    def _restore_exec_params(self):
        """沿用上次执行用的时长/步数/KOL。

        不沿用就会踩坑：跑一批 10 秒任务后重启软件，下拉默默回到 5 秒，
        使用者以为“改了没生效”“怎么又变成原来的了”。"""
        d = app_state.get("exec_params") or {}
        dur = d.get("duration")
        if isinstance(dur, int) and 2 <= dur <= 15:
            self.cb_duration.setCurrentIndex(dur - 2)
        steps = d.get("steps")
        if isinstance(steps, int) and 1 <= steps <= 50:
            self.cb_steps.setCurrentIndex(steps - 1)
        kol = d.get("kol")
        if kol:
            idx = self.cb_kol.findText(kol)
            if idx >= 0:
                self.cb_kol.setCurrentIndex(idx)
        for cb in (self.cb_duration, self.cb_steps, self.cb_kol):
            cb.currentIndexChanged.connect(self._save_exec_params)

    def _save_exec_params(self, *_):
        app_state.set_value("exec_params", {
            "duration": 2 + self.cb_duration.currentIndex(),
            "steps": int(self.cb_steps.currentText()),
            "kol": None if self.cb_kol.currentIndex() == 0 else self.cb_kol.currentText()})

    def _current_options(self):
        """从控件读取当前时长/步数/KOL，生成提交选项快照。

        这三个是「本批任务怎么跑」的全局参数，不是任务自带字段：
        列表里的「时长」列只是上次提交的留痕，改参数要改这里。
        """
        return SubmitOptions(
            duration=2 + self.cb_duration.currentIndex(),
            steps=int(self.cb_steps.currentText()),
            kol=None if self.cb_kol.currentIndex() == 0 else self.cb_kol.currentText())

    def _new_task(self):
        data = TaskDialog.ask(self)
        if data and data["prompt"]:
            task_store.add_task(data["num"] or (task_store.task_count() + 1),
                                data["product"], data["prompt"],
                                script=data.get("script", ""),
                                remark=data.get("remark", ""))
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
        locate_only = d.get("locate_only")      # 只找不改：按关键词挑一批任务出来
        if d["only_selected"] and sel:
            tids = sel
        else:
            df = task_store.list_tasks_df()
            tids = [int(r["_id"]) for _, r in df.iterrows()]
        count = 0
        hit_ids = []                       # 含旧提示词的任务：处理后选中并置顶
        for tid in tids:
            t = task_store.get_task(tid)
            prompt = t["prompt"] or ""
            if find in prompt:
                if not locate_only:
                    task_store.update_row(tid, **{"提示词": prompt.replace(find, repl)})
                hit_ids.append(tid)
                count += 1
        if count:
            n = self._gather_matches(hit_ids)
            self.lbl_tip.setText(
                f"已按「{find}」勾选并置顶 {n} 个任务，可直接「执行选中」（提示词未改动）"
                if locate_only else
                f"替换完成：{n} 个任务的提示词已更新，已勾选并置顶集中显示，可直接「执行选中」")
        else:
            self.lbl_tip.setText(f"未找到包含「{find}」的提示词")
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
        iterated = 0
        force = []                       # 已完成但提示词没改过，需确认是否强制重跑
        for tid in tids:
            t = task_store.get_task(tid)
            if not t:
                continue
            prompt = (t["prompt"] or "").strip()
            if not prompt:
                continue
            if t["status"] and t["status"] not in RETRYABLE and int(t["runs"] or 0) > 0:
                # 已完成过：若提示词在上次执行后又改过 → 自动识别为“迭代执行”；
                # 没改过 → 收集到 force，交给用户确认是否强制重跑（而不是静默跳过）
                if not task_store.iter_ready(tid):
                    force.append((tid, t["product"], prompt))
                    continue
                iterated += 1
            # 已取消/失败的任务不提前清状态：提交成功时 _register_success 会写 submitted。
            # 旧实现先写空串再提交，一旦提交失败，行就永远停在“待执行”，
            # 看不出到底发没发出去（内测现场：422 全失败后任务看起来像排队中）
            items.append((tid, t["product"], prompt))

        repeated = 1
        options = self._current_options()      # 一次点击一个快照，弹窗与提交用的是同一组参数
        if force:
            single = len(force) == 1 and not items
            d = ForceRerunDialog.ask(self, task_id=force[0][0] if single else None,
                                     count=len(force), allow_repeat=single,
                                     params_note=f"{options.duration}秒 / {options.steps} 步")
            if d is not None:
                repeated = d["repeat"]
                for it in force:
                    items.extend([it] * repeated)

        if not items:
            self.lbl_tip.setText("没有可执行的任务：请确认已填写提示词；改过提示词的已完成任务会自动按迭代重跑")
            return
        self.lbl_tip.setText(f"正在提交 {len(items)} 个任务"
                             f"（本次：{options.duration}秒 / {options.steps} 步）"
                             + (f"，含 {iterated} 个提示词迭代" if iterated else "")
                             + (f"；任务{force[0][0]} 抽 {repeated} 次卡"
                                if repeated > 1 and len(force) == 1
                                else f"；含 {len(force)} 个强制重跑" if force else "")
                             + "…")
        self.worker = SubmitWorker(items, options)
        # 每条进度都带上本批参数：否则一行「✓ 任务6 已提交」就把上面那句
        # 「本次：10秒 / 20 步」冲掉，使用者无从判断这次到底用的什么参数
        snap = f"（本次：{options.duration}秒 / {options.steps} 步）"
        self.worker.log_msg.connect(
            lambda m, s=snap: self.lbl_tip.setText(f"{m} {s}"))
        self.worker.all_done.connect(self._on_submit_done)
        self.worker.start()

    def _on_submit_done(self, failed):
        """提交结果必留痕：失败只写一行灰色小字会被看成“改了没生效”
        （云端拒绝参数时就是这样：一行提示转瞬即逝，表格里的旧视频、旧时长还在）"""
        self.refresh()
        if not failed:
            self.lbl_tip.setText("提交完成，云端生成中…进度条将实时更新，完成后自动下载到 outputs/")
            return
        reasons = "\n".join(f"  任务{tid}：{why}" for tid, why in failed[:8])
        more = f"\n  …共 {len(failed)} 条" if len(failed) > 8 else ""
        QMessageBox.warning(
            self, "部分任务未提交成功",
            f"{len(failed)} 个任务没提交上去，云端队列里根本不会有它们，\n"
            f"列表里看到的仍是上次跑出来的视频和参数。\n\n{reasons}{more}\n\n"
            "具体报文已写入日志。修正原因后重新【执行选中】即可。")
        self.lbl_tip.setText(f"⚠ {len(failed)} 个任务提交失败（见弹窗与日志），"
                             f"未提交到云端")

    def _import_excel(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择任务 Excel", "", "Excel 文件 (*.xlsx)")
        if not path:
            return
        try:
            n, dup, hit_ids = task_store.import_from_excel(path)
            msg = f"已导入 {n} 条任务"
            if dup:
                msg += f"；跳过重复 {dup} 条（提示词已存在）"
            if hit_ids:
                # 这份文件涉及的任务（新建的 + 提示词已存在的旧任务）勾选并置顶聚集
                self._gather_matches(hit_ids)
                msg += f"；已选中并置顶这 {len(hit_ids)} 条任务"
            self.lbl_tip.setText(msg)
        except Exception as e:
            QMessageBox.critical(self, "导入失败", str(e))

    def _do_export(self, fmt):
        try:
            self.lbl_tip.setText(f"已导出：{task_store.export_tasks(fmt)}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))
