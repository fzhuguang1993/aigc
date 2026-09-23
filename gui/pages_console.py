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
        b_cloud.setToolTip("向每条线路查询云端进行中任务数（结果缓存约 5 秒）；"
                           "接口不可用时降级为仅本机计数，并标⚠提醒")
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
        from registry.manager import ACCOUNTS, REG, is_active, first_check_done
        checked = first_check_done()      # 未跑过一轮探活前，healthy 只是默认值
        rows = REG.active()
        self.table.setRowCount(len(ACCOUNTS))
        sum_run = 0
        for i, acc in enumerate(ACCOUNTS):
            run = sum(1 for t in rows if t["account"] == acc.name
                      and is_active(t["status"]))
            sum_run += run
            cloud = self._cloud.get(acc.name)
            if cloud is None:
                cloud_txt, cloud_tip = "-", "尚未查询"
            else:
                load, src = cloud
                cloud_txt = str(load) if src == "cloud" else f"{load} ⚠仅本机"
                cloud_tip = ("含同事提交的任务" if src == "cloud" else
                             "云端负载接口不可用，此数只统计了本机，"
                             "看不到同事占了多少——选线会失真")
            if not checked:
                status_txt, status_color = "⚪ 待检测", "#8F959E"
            elif acc.healthy:
                status_txt, status_color = "🟢 正常", "#1FA45C"
            else:
                status_txt, status_color = "🔴 故障", "#E5484D"
            vals = [acc.name, status_txt, acc.base,
                    acc.concurrency, run, max(acc.concurrency - run, 0),
                    acc.fail_count, cloud_txt]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if c != 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c == 1:
                    item.setForeground(QColor(status_color))
                if c == 5 and int(v) == 0:
                    item.setForeground(QColor("#F5A623"))
                if c == 7:
                    item.setToolTip(cloud_tip)
                    if cloud and cloud[1] != "cloud":
                        item.setForeground(QColor("#E5484D"))
                self.table.setItem(i, c, item)
        degraded = sum(1 for v in self._cloud.values() if v and v[1] != "cloud")
        self.lbl_sum.setText(
            f"全部线路合计：本地进行中 {sum_run} 条 · "
            + (f"健康线路 {sum(1 for a in ACCOUNTS if a.healthy)}/{len(ACCOUNTS)}"
               if checked else f"共 {len(ACCOUNTS)} 条线路（还没跑过检测）")
            + (f" · ⚠ {degraded} 条线路云端接口不可用，负载仅含本机" if degraded else ""))

    def _fetch_cloud(self):
        """手动拉取云端负载（含 HTTP）；接口不可达时返回降级值并标记来源"""
        from registry.manager import ACCOUNTS, measure_load, invalidate_load_cache
        invalidate_load_cache()
        for acc in ACCOUNTS:
            try:
                self._cloud[acc.name] = measure_load(acc)
            except Exception:
                self._cloud[acc.name] = None
        self.refresh()
