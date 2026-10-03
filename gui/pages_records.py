"""
gui/pages_records.py —— 执行记录 + 实时日志
勾选框列 · 字段管理（显示/隐藏 + 拖拽排序）· 列排序 · 搜索 · 日期筛选 · 导出选中
"""
from PySide6.QtCore import Qt, QTimer, QDate
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTableWidgetItem, QHeaderView,
                               QPlainTextEdit, QMessageBox, QLineEdit,
                               QCheckBox, QAbstractItemView, QDialog)

from store import task_store
from gui import log_sink
from gui.formatting import secs
from gui.header import page_header, Card
from gui.widgets import LoadingOverlay
from gui.tablekit import (FieldManagerDialog, apply_field_layout, enable_drag_with_lock,
                          SecsItem)
from gui.kit import KitTable, DateRangePicker

DATA_HEADERS = ["开始时间", "编号", "品名", "账号", "状态", "结束时间",
                "总用时", "生成", "排队", "输出文件", "错误信息"]
# 「总用时」= 提交→完成（含在云端排队），「生成」= 云端开始跑→出片，两者差的就是排队。
# 早期没存云端时间戳的记录拆不出来，「生成」列只能给个「—」——不拿总用时冒充生成耗时。
HEADERS = [""] + DATA_HEADERS
CHECK_COL = 0
COL_START, COL_NUM, COL_PRODUCT, COL_ACCOUNT, COL_STATUS, COL_END, \
    COL_DUR, COL_GEN, COL_QUEUE, COL_OUT, COL_ERR = range(1, 12)
LEFT_COLS = (COL_OUT, COL_ERR)          # 长文本列左对齐，其余居中
DUR_COLS = (COL_DUR, COL_GEN, COL_QUEUE)   # 三个时长列按秒数排序
_DUR_KEY = {COL_DUR: "duration", COL_GEN: "gen_sec", COL_QUEUE: "queued_sec"}
_COLORS = {"completed": "#00A870", "failed": "#F54A45", "error": "#F54A45",
           "cancelled": "#8F959E"}


class RecordsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 14, 24, 12)
        lay.setSpacing(10)

        top = QHBoxLayout()
        top.addWidget(page_header("执行记录",
                                  "每次提交留痕 · 默认最新在前 · 点表头排序",  # noqa: E501
                                  icon="🕘"), 1)
        b_exp = QPushButton("📤 导出全部记录")
        b_exp.setObjectName("GhostBtn")
        b_exp.clicked.connect(self._export)
        top.addWidget(b_exp)
        b_sel = QPushButton("📤 导出选中")
        b_sel.setObjectName("GhostBtn")
        b_sel.clicked.connect(self._export_selected)
        top.addWidget(b_sel)
        b_fields = QPushButton("⚟ 字段管理")
        b_fields.setObjectName("GhostBtn")
        b_fields.setToolTip("控制显示哪些列，拖动表头可直接调整列顺序")
        b_fields.clicked.connect(self._manage_fields)
        top.addWidget(b_fields)
        lay.addLayout(top)

        # ---------- 筛选栏 ----------
        fbar = QHBoxLayout()
        fbar.addWidget(QLabel("🔍"))
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索编号/品名/账号/输出/错误，回车过滤")
        self.ed_search.setFixedWidth(240)
        self.ed_search.returnPressed.connect(self.refresh)
        fbar.addWidget(self.ed_search)
        fbar.addSpacing(16)
        self.cb_date = QCheckBox("按日期筛选")
        self.cb_date.toggled.connect(self._toggle_date_filter)
        fbar.addWidget(self.cb_date)
        # 统一用组件库里的日期区间选择器（一颗按钮弹日历），与任务中心同口径，
        # 不再摆两个裸 QDateEdit；默认最近 7 天，勾选后启用。
        self.dr_date = DateRangePicker(QDate.currentDate().addDays(-7),
                                       QDate.currentDate())
        self.dr_date.setEnabled(False)
        self.dr_date.changed.connect(self.refresh)
        fbar.addWidget(self.dr_date)
        b_clear = QPushButton("✕ 清除筛选")
        b_clear.setObjectName("GhostBtn")
        b_clear.clicked.connect(self._clear_filters)
        fbar.addWidget(b_clear)
        fbar.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("PageTip")
        fbar.addWidget(self.lbl_count)
        # 筛选栏收进一张白卡（对标数据中台的分区卡），不再裸摆在灰底上
        ctrl = Card(margins=(14, 10, 14, 10))
        ctrl.v.addLayout(fbar)
        lay.addWidget(ctrl)

        # ---------- 表格 ----------
        content = Card(margins=(12, 10, 12, 10))
        content.v.setSpacing(8)
        # 标准表格地基 KitTable（UI 库基准）：首列勾选框 + 表头三态全选框（内置 checkbox）、
        # 只读、斑马纹、隐藏行号、列对齐/换行/Ctrl+C/表头右键多级菜单（含「顺序调整」）。
        # 排序：表头升降序箭头与点击排序已按「严格按 UI 库样式」移除，升降序统一走右键菜单。
        self.table = KitTable(
            0, len(HEADERS), HEADERS,
            zebra=True, row_number=False, select=None, edit=False)
        enable_drag_with_lock(self.table, lock_count=1)
        self.table.horizontalHeader().setSectionResizeMode(COL_OUT, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(COL_ERR, QHeaderView.ResizeMode.Stretch)
        for c, w in {COL_START: 120, COL_NUM: 50, COL_PRODUCT: 120, COL_ACCOUNT: 70,
                     COL_STATUS: 90, COL_END: 120, COL_DUR: 70,
                     COL_GEN: 70, COL_QUEUE: 62}.items():
            self.table.setColumnWidth(c, w)
        self.table.itemChanged.connect(self._on_item_changed)
        # 表头全选框（KitTable 内置）：点成选中→勾选本页可见记录 / 否则清空
        self.table.check_all_toggled.connect(self._on_check_all_toggle)
        # 「顺序调整」菜单由页面接管（默认按开始时间倒序，菜单可改列/方向）
        self.table.add_header_menu("顺序调整", [
            ("升序排列", lambda col: self._manual_sort(col, Qt.SortOrder.AscendingOrder)),
            ("降序排列", lambda col: self._manual_sort(col, Qt.SortOrder.DescendingOrder)),
        ], key="order")
        content.v.addWidget(self.table, 3)

        content.v.addWidget(QLabel("实时日志"))
        self.log = QPlainTextEdit()
        self.log.setObjectName("LogBox")
        self.log.setReadOnly(True)
        content.v.addWidget(self.log, 1)
        lay.addWidget(content, 1)

        self._checked_rows = {}     # {run行数据id: row dict} 勾选保留
        self._user_sort_col = None    # 用户经右键菜单「顺序调整」排过的列；None=默认按开始时间倒序
        self._user_sort_order = None  # 对应升降序（Qt.SortOrder）
        self._loading = LoadingOverlay(self)
        self._first_shown = False

    # ---------- 首次进入：先亮遮罩，再做重刷新 ----------
    def showEvent(self, e):
        super().showEvent(e)
        if not self._first_shown:
            self._first_shown = True
            self._loading.show_overlay()
            QTimer.singleShot(50, self._first_load)

    def _first_load(self):
        try:
            self.refresh()
        finally:
            self._loading.hide_overlay()

    # ---------- 筛选 ----------
    def _toggle_date_filter(self, on):
        self.dr_date.setEnabled(on)
        self.refresh()

    def _clear_filters(self):
        self.ed_search.clear()
        self.cb_date.setChecked(False)
        self.refresh()

    def _match(self, r):
        kw = self.ed_search.text().strip().lower()
        if kw:
            hay = " ".join([str(r["num"]), str(r["product"]), str(r["account"]),
                            str(r["output"]), str(r["error"])]).lower()
            if kw not in hay:
                return False
        if self.cb_date.isChecked():
            d = (r["started_at"] or "")[:10]
            s, e = self.dr_date.get_range()
            if not (s.toString("yyyy-MM-dd") <= d <= e.toString("yyyy-MM-dd")):
                return False
        return True

    def refresh(self):
        rows = task_store.list_runs()
        # 默认「最新在前」按开始时间倒序；用户经右键菜单「顺序调整」排过则用其列/方向
        # （不再有表头箭头 / 点击排序）。
        sort_col = self._user_sort_col if self._user_sort_col not in (None, CHECK_COL) else COL_START
        sort_order = self._user_sort_order or Qt.SortOrder.DescendingOrder
        self._syncing = True
        self.table.setRowCount(0)
        shown = 0
        for r in rows:
            if not self._match(r):
                continue
            i = self.table.rowCount()
            self.table.insertRow(i)
            ck = self.table.make_check_item(r["id"] in self._checked_rows)
            ck.setData(Qt.ItemDataRole.UserRole, r["id"])
            ck.setToolTip("勾选后可导出这批记录")
            self.table.setItem(i, CHECK_COL, ck)
            vals = [(r["started_at"] or "")[5:16], r["num"], r["product"], r["account"],
                    r["status"] or "running", (r["finished_at"] or "")[5:16],
                    secs(r.get("duration")),
                    secs(r.get("gen_sec")), secs(r.get("queued_sec")),
                    r["output"] or "", r["error"] or ""]
            for c0, v in enumerate(vals):
                c = c0 + 1
                # 三个时长列走 SecsItem：显示「4分58秒」而排序按秒数
                item = (SecsItem(r.get(_DUR_KEY[c])) if c in DUR_COLS
                        else QTableWidgetItem(str(v)))
                item.setToolTip(str(v))
                item.setData(Qt.ItemDataRole.UserRole, r["id"])
                if c == COL_GEN:
                    item.setToolTip("云端开始跑→出片（不含排队）；「—」＝这条是早期记录，"
                                    "当时没存云端时间戳，可跑回填脚本补")
                item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter
                                      | (Qt.AlignmentFlag.AlignLeft if c in LEFT_COLS
                                         else Qt.AlignmentFlag.AlignCenter))
                if c == COL_STATUS:
                    item.setForeground(QColor(_COLORS.get(v, "#FF8D19")))
                self.table.setItem(i, c, item)
            shown += 1
        self.table.sortItems(sort_col, sort_order)   # sortItems 不依赖 setSortingEnabled
        self._syncing = False
        self.lbl_count.setText(f"显示 {shown}/{len(rows)} 条 · 已勾选 {len(self._checked_rows)}")
        self.table.refresh_check_all_state()
        for m in log_sink.drain():
            self.log.appendPlainText(m)

    def _export(self):
        try:
            path = task_store.export_runs("excel")
            QMessageBox.information(self, "已导出", path)
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _export_selected(self):
        if not self._checked_rows:
            QMessageBox.information(self, "提示", "请先在表格第 1 列勾选要导出的记录")
            return
        try:
            path = task_store.export_runs_rows(list(self._checked_rows.values()), "excel")
            QMessageBox.information(self, "已导出", path)
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _manage_fields(self):
        h = self.table.horizontalHeader()
        order = [h.logicalIndex(v) for v in range(1, self.table.columnCount())]
        hidden = {c for c in order if self.table.isColumnHidden(c)}
        cols = [(i, DATA_HEADERS[i - 1]) for i in range(1, len(HEADERS))]
        d = FieldManagerDialog(self, cols, order, hidden)
        if d.exec():
            apply_field_layout(self.table, d.order, d.hidden, first_locked=1)

    def _on_item_changed(self, item):
        if getattr(self, "_syncing", False) or item.column() != CHECK_COL:
            return
        rid = item.data(Qt.ItemDataRole.UserRole)
        if rid is None:
            return
        if item.checkState() == Qt.CheckState.Checked:
            for r in task_store.list_runs():
                if r["id"] == rid:
                    self._checked_rows[rid] = r
                    break
        else:
            self._checked_rows.pop(rid, None)
        self.lbl_count.setText(f"已勾选 {len(self._checked_rows)}")
        self.table.refresh_check_all_state()

    def _check_items(self):
        return [self.table.item(r, CHECK_COL)
                for r in range(self.table.rowCount())
                if self.table.item(r, CHECK_COL) is not None]

    def _manual_sort(self, col, order):
        """表头右键菜单「顺序调整」：记下排序列/方向后刷新（未排过则默认按开始时间倒序）。
        勾选列不参与排序。"""
        if col is None or col < 0 or col == CHECK_COL:
            return
        self._user_sort_col = col
        self._user_sort_order = order
        self.refresh()

    def _on_check_all_toggle(self, select):
        """表头全选框（KitTable 内置信号 check_all_toggled(bool)）：True→勾选本页可见记录；
        False→取消本页可见勾选。只动当前可见行，不影响其它筛选下已保在 _checked_rows 里的记录。"""
        byid = {r["id"]: r for r in task_store.list_runs()} if select else None
        self._syncing = True
        for it in self._check_items():
            rid = it.data(Qt.ItemDataRole.UserRole)
            it.setCheckState(Qt.CheckState.Checked if select
                             else Qt.CheckState.Unchecked)
            if select and rid in byid:
                self._checked_rows[rid] = byid[rid]
            elif not select:
                self._checked_rows.pop(rid, None)
        self._syncing = False
        self.lbl_count.setText(f"已勾选 {len(self._checked_rows)}")
        self.table.refresh_check_all_state()


from gui.window_frame import apply_rounded


class RecordsDialog(QDialog):
    """以模态弹窗承载「执行记录」页：数据中台不再单列左侧导航，
    改由页头按钮弹出。直接复用 RecordsPage 的全部逻辑（筛选/排序/导出），
    RecordsPage 首次 showEvent 会自动加载，这里无需额外刷新。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🕘 执行记录")
        self.resize(1120, 720)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.page = RecordsPage()
        lay.addWidget(self.page)
        apply_rounded(self, show_min=False, show_max=False)
