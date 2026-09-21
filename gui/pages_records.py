"""
gui/pages_records.py —— 执行记录 + 实时日志
勾选框列 · 字段管理（显示/隐藏 + 拖拽排序）· 列排序 · 搜索 · 日期筛选 · 导出选中
"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QHeaderView,
                               QPlainTextEdit, QMessageBox, QLineEdit, QDateEdit,
                               QCheckBox, QAbstractItemView)

from store import task_store
from gui import log_sink
from gui.header import page_header
from gui.widgets import LoadingOverlay
from gui.tablekit import FieldManagerDialog, apply_field_layout, enable_drag_with_lock

DATA_HEADERS = ["开始时间", "编号", "品名", "账号", "状态", "结束时间", "用时", "输出文件", "错误信息"]
HEADERS = [""] + DATA_HEADERS
CHECK_COL = 0
COL_START, COL_NUM, COL_PRODUCT, COL_ACCOUNT, COL_STATUS, COL_END, \
    COL_DUR, COL_OUT, COL_ERR = range(1, 10)
LEFT_COLS = (COL_OUT, COL_ERR)          # 长文本列左对齐，其余居中
_COLORS = {"completed": "#00A870", "failed": "#F54A45", "error": "#F54A45",
           "cancelled": "#8F959E"}


def _fmt_dur(seconds):
    """执行用时展示：秒级精度，超过 1 分钟折算分秒"""
    try:
        d = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if d <= 0:
        return ""
    return f"{d // 60}分{d % 60}秒" if d >= 60 else f"{d}秒"


class RecordsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(6)

        top = QHBoxLayout()
        top.addWidget(page_header("执行记录",
                                  "每次提交留痕 · 点表头排序",  # noqa: E501
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
        self.de_start = QDateEdit()
        self.de_end = QDateEdit()
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
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setColumnWidth(CHECK_COL, 36)
        self.table.horizontalHeader().setSortIndicatorShown(False)
        enable_drag_with_lock(self.table, lock_count=1)
        self.table.horizontalHeader().setSectionResizeMode(COL_OUT, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(COL_ERR, QHeaderView.ResizeMode.Stretch)
        for c, w in {COL_START: 120, COL_NUM: 50, COL_PRODUCT: 120, COL_ACCOUNT: 70,
                     COL_STATUS: 90, COL_END: 120, COL_DUR: 70}.items():
            self.table.setColumnWidth(c, w)
        self.table.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self.table, 3)

        lay.addWidget(QLabel("实时日志"))
        self.log = QPlainTextEdit()
        self.log.setObjectName("LogBox")
        self.log.setReadOnly(True)
        lay.addWidget(self.log, 1)

        self._checked_rows = {}     # {run行数据id: row dict} 勾选保留
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
        self.de_start.setEnabled(on)
        self.de_end.setEnabled(on)
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
            s = self.de_start.date().toString("yyyy-MM-dd")
            e = self.de_end.date().toString("yyyy-MM-dd")
            if not (s <= d <= e):
                return False
        return True

    def refresh(self):
        # 首次可用时把日期控件默认设为最近7天
        if not self.de_start.property("_init"):
            from PySide6.QtCore import QDate
            self.de_start.setDate(QDate.currentDate().addDays(-7))
            self.de_end.setDate(QDate.currentDate())
            self.de_start.setProperty("_init", True)

        rows = task_store.list_runs()
        header = self.table.horizontalHeader()
        sort_col, sort_order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        if sort_col == CHECK_COL:
            sort_col = COL_START
        self.table.setSortingEnabled(False)
        self._syncing = True
        self.table.setRowCount(0)
        shown = 0
        for r in rows:
            if not self._match(r):
                continue
            i = self.table.rowCount()
            self.table.insertRow(i)
            ck = QTableWidgetItem()
            ck.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            ck.setData(Qt.ItemDataRole.UserRole, r["id"])
            ck.setCheckState(Qt.CheckState.Checked if r["id"] in self._checked_rows
                             else Qt.CheckState.Unchecked)
            ck.setToolTip("勾选后可导出这批记录")
            self.table.setItem(i, CHECK_COL, ck)
            vals = [(r["started_at"] or "")[5:16], r["num"], r["product"], r["account"],
                    r["status"] or "running", (r["finished_at"] or "")[5:16],
                    _fmt_dur(r.get("duration")),
                    r["output"] or "", r["error"] or ""]
            for c0, v in enumerate(vals):
                c = c0 + 1
                item = QTableWidgetItem(str(v))
                item.setToolTip(str(v))
                item.setData(Qt.ItemDataRole.UserRole, r["id"])
                if c == COL_DUR:
                    item.setData(Qt.ItemDataRole.DisplayRole,
                                 int(r.get("duration") or 0))   # 按秒数数值排序
                item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter
                                      | (Qt.AlignmentFlag.AlignLeft if c in LEFT_COLS
                                         else Qt.AlignmentFlag.AlignCenter))
                if c == COL_STATUS:
                    item.setForeground(QColor(_COLORS.get(v, "#FF8D19")))
                self.table.setItem(i, c, item)
            shown += 1
        self.table.setSortingEnabled(True)
        self.table.sortItems(sort_col, sort_order)
        self._syncing = False
        self.lbl_count.setText(f"显示 {shown}/{len(rows)} 条 · 已勾选 {len(self._checked_rows)}")
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
