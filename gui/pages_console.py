"""
gui/pages_console.py —— 控制台：接口线路负载监控
每条线路：健康状态 / 并发额度 / 本地进行中 / 剩余槽位 / 云端实时负载（手动拉取，带缓存）
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QHeaderView,
                               QAbstractItemView)

from gui.header import page_header

HEADERS = ["账号", "线路状态", "接口地址", "并发上限", "本地进行中", "剩余槽位",
           "健康检查连败", "云端负载(实时)"]
_RUN_STATES = ("queued", "running", "starting")


class ConsolePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.addWidget(page_header("线路负载",
                                  "槽位满自动排队 · 连败 3 次自动切换",
                                  icon="📡"), 1)
        b_cloud = QPushButton("🔄 查询云端负载")
        b_cloud.setObjectName("GhostBtn")
        b_cloud.setToolTip("向每条线路查询云端进行中任务数（结果缓存约 5 秒，接口关闭时显示为 -）")
        b_cloud.clicked.connect(self._fetch_cloud)
        top.addWidget(b_cloud)
        lay.addLayout(top)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        hh = self.table.horizontalHeader()
        for c, w in {0: 90, 1: 90, 3: 80, 4: 100, 5: 80, 6: 110, 7: 110}.items():
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(c, w)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table)

        self.lbl_sum = QLabel("")
        self.lbl_sum.setObjectName("InlineTip")
        lay.addWidget(self.lbl_sum)

        self._cloud = {}   # name -> 云端负载 or None

    def refresh(self):
        from registry.manager import ACCOUNTS, REG
        rows = REG.active()
        self.table.setRowCount(len(ACCOUNTS))
        sum_run = 0
        for i, acc in enumerate(ACCOUNTS):
            run = sum(1 for t in rows if t["account"] == acc.name
                      and t["status"] in _RUN_STATES)
            sum_run += run
            cloud = self._cloud.get(acc.name)
            vals = [acc.name, "🟢 正常" if acc.healthy else "🔴 故障", acc.base,
                    acc.concurrency, run, max(acc.concurrency - run, 0),
                    acc.fail_count, "-" if cloud is None else cloud]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if c != 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c == 1:
                    item.setForeground(QColor("#1FA45C" if acc.healthy else "#E5484D"))
                if c == 5 and int(v) == 0:
                    item.setForeground(QColor("#F5A623"))
                self.table.setItem(i, c, item)
        self.lbl_sum.setText(
            f"全部线路合计：本地进行中 {sum_run} 条 · "
            f"健康线路 {sum(1 for a in ACCOUNTS if a.healthy)}/{len(ACCOUNTS)}")

    def _fetch_cloud(self):
        """手动拉取云端负载（含 HTTP，接口关闭时快速失败显示 -）"""
        from registry.manager import ACCOUNTS, get_account_load, invalidate_load_cache
        invalidate_load_cache()
        for acc in ACCOUNTS:
            try:
                self._cloud[acc.name] = get_account_load(acc)
            except Exception:
                self._cloud[acc.name] = None
        self.refresh()
