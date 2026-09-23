"""
gui/pages_dashboard.py —— BI 看板：时间范围切换 + KPI 环比卡片 + 趋势/占比/线路三图 + 一键复制进度汇报
"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QComboBox, QPushButton, QSizePolicy, QInputDialog)

from core.config import USER_NAME
from store import app_state, task_store
from gui.formatting import secs
from gui.header import page_header, Card, KpiCard
from gui.widgets import LoadingOverlay
from gui.charts import TrendChart, DonutChart, HBarChart, C_OK, C_OK2, C_FAIL, C_FAIL2, C_CANCEL, C_CANCEL2


def _delta_text(cur, prev, up_is_good=True):
    if prev == 0 and cur == 0:
        return "持平", None
    if prev == 0:
        return f"▲ 新增 {cur}", True
    pct = (cur - prev) / prev * 100
    arrow = "▲" if pct >= 0 else "▼"
    good = (pct >= 0) == up_is_good
    return f"{arrow} {abs(pct):.0f}% 较上期", good


def _mom(cur, prev):
    """环比昨日：箭头+百分比；昨日为 0 时给文字，避免除零/无穷。"""
    if prev == 0 and cur == 0:
        return "持平"
    if prev == 0:
        return "昨日无"
    pct = (cur - prev) / prev * 100
    return f"{'▲' if pct >= 0 else '▼'}{abs(pct):.0f}%"


class DashboardPage(QWidget):
    # None 代表「全部」：从第一条记录累计至今，永不清零
    _RANGES = (1, 7, 14, 30, 90, None)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 14, 24, 14)
        lay.setSpacing(12)

        # ----- 页头 + 时间范围 -----
        head = QHBoxLayout()
        head.addWidget(page_header("数据看板", "执行量 · 成功率 · 趋势 · 线路分布"), 1)
        head.addWidget(QLabel("时间范围"))
        self.cb_range = QComboBox()
        self.cb_range.addItems(["今日", "近 7 天", "近 14 天", "近 30 天",
                                "近 90 天", "全部（累计）"])
        # 记住上次选的范围：不记就会“看累计→重启→又回到近 7 天”，
        # 数字变少会被当成“统计清零了”（先设值再接信号，避免构造时触发 refresh）
        saved = app_state.get("dash_range")
        self.cb_range.setCurrentIndex(saved if isinstance(saved, int)
                                      and 0 <= saved < len(self._RANGES) else 1)
        self.cb_range.currentIndexChanged.connect(self._on_range)
        head.addWidget(self.cb_range)
        self.b_copy = QPushButton("📋 复制进度汇报")
        self.b_copy.setObjectName("GhostBtn")
        self.b_copy.setToolTip("把「今日截至目前 vs 昨日」的执行统计复制成文字汇报，"
                               "含总量/环比、分产品、视频时长（≥10s/<10s）分桶；直接粘贴到群里")
        self.b_copy.clicked.connect(self._copy_report)
        head.addWidget(self.b_copy)
        lay.addLayout(head)

        # ----- KPI 行 -----
        kpis = QHBoxLayout()
        kpis.setSpacing(14)
        self.k_total = KpiCard("执行总条数", "▶", "#3370FF")
        self.k_ok = KpiCard("成功", "✔", "#00B96B")
        self.k_rate = KpiCard("成功率", "％", "#7F3FBF")
        self.k_fail = KpiCard("失败", "✖", "#F54A45")
        self.k_cancel = KpiCard("取消", "⊘", "#8F959E")
        self.k_run = KpiCard("正在运行", "◌", "#FF8D19")
        self.k_dur = KpiCard("平均生成时长", "⏱", "#0FB5AE")
        for k in (self.k_total, self.k_ok, self.k_rate, self.k_fail,
                  self.k_cancel, self.k_run, self.k_dur):
            k.setMinimumWidth(125)
            kpis.addWidget(k)
        lay.addLayout(kpis)

        # ----- 趋势 + 环形占比 -----
        mid = QHBoxLayout()
        mid.setSpacing(14)
        trend_card = Card()
        self.trend = TrendChart("每日执行趋势（成功 / 失败 / 取消 堆叠）")
        self.trend.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        trend_card.add(self.trend, 1)
        donut_card = Card()
        self.donut = DonutChart("状态占比")
        self.donut.setMinimumWidth(300)
        donut_card.add(self.donut, 1)
        mid.addWidget(trend_card, 3)
        mid.addWidget(donut_card, 2)
        lay.addLayout(mid, 3)

        # ----- 线路分布 -----
        self.hbar_card = Card(margins=(16, 14, 16, 10))
        self.hbar = HBarChart("各线路执行分布（总执行 / 成功数）")
        self.hbar.setMinimumHeight(150)
        self.hbar_card.add(self.hbar, 1)
        lay.addWidget(self.hbar_card, 2)

        self._loading = LoadingOverlay(self)

    def _days(self):
        return self._RANGES[self.cb_range.currentIndex()]

    def _on_range(self):
        app_state.set_value("dash_range", self.cb_range.currentIndex())
        self.refresh()

    @staticmethod
    def _scope(days):
        return ("累计（全部）" if days is None
                else "今日" if days == 1 else f"近 {days} 天")

    def refresh(self):
        st = task_store.range_stats(self._days())
        self._st = st
        cur, prev = st["cur"], st["prev"]
        days = st["days"]
        scope = self._scope(days)
        cum = days is None        # 累计口径没有“上一个等长周期”，环比全部隐掉

        def cmp(c, p, up_is_good):
            return ("累计至今", None) if cum else self._cmp(c, p, up_is_good)

        self.k_total.set_value(cur["total"], *cmp(cur["total"], prev["total"], True))
        self.k_ok.set_value(cur["ok"], *cmp(cur["ok"], prev["ok"], True))
        dt, up = _delta_text(round(cur["rate"]), round(prev["rate"]), True) \
            if prev["total"] else ("", None)
        self.k_rate.set_value(f"{cur['rate']}%", "累计至今" if cum else
                              (dt if prev["total"] else
                               ("—" if not cur["total"] else "全部基于本期")), up)
        self.k_fail.set_value(cur["fail"], *cmp(cur["fail"], prev["fail"], False))
        self.k_cancel.set_value(cur["cancel"], *cmp(cur["cancel"], prev["cancel"], False))
        run = st["running"]
        self.k_run.set_value(run, "云端生成中" if run else "空闲")
        # 成功执行的平均生成用时（秒，不含排队），与上期对比，越短越好
        avg_c, avg_p = cur.get("avg_dur") or 0, prev.get("avg_dur") or 0
        dt, up = _delta_text(round(avg_c), round(avg_p), False) if avg_p else ("", None)
        self.k_dur.set_value(secs(avg_c) if avg_c else "—",
                             "累计至今" if cum else
                             (dt if (avg_p and avg_c) else
                              ("—" if not avg_c else "均基于本期")), up)
        # 排队均值放 tooltip：同一条线只跑一个时，总用时里一大半是排队，
        # 不拆开来就说不清“为什么上次看 15 分钟、这次只要 5 分钟”
        q = cur.get("avg_queued") or 0
        self.k_dur.setToolTip(
                "只算视频在云端真正生成花的时间（开始跑→出片），不含排队"
                + (f"；本期平均排队 {secs(q)}" if q else "；本期没采到排队时间")
                + "\n没拆分（早期提交）的记录按总用时计，可用回填脚本补")

        self.trend.title = f"每日执行趋势 · {scope}（成功/失败/取消 堆叠）"
        self.trend.set_data(st["daily"])
        self.donut.set_data(
            [("成功", cur["ok"], C_OK, C_OK2),
             ("失败", cur["fail"], C_FAIL, C_FAIL2),
             ("取消", cur["cancel"], C_CANCEL, C_CANCEL2)],
            center_text=f"{cur['rate']}%", center_sub="成功率")
        self.hbar.title = f"各线路执行分布 · {scope}"
        self.hbar.set_data(st["accounts"])

    # ---------- 首次进入：先亮遮罩，再做重刷新（多聚合查询 + 三图重绘较慢） ----------
    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_first_shown", False):
            self._first_shown = True
            self._loading.show_overlay()
            QTimer.singleShot(50, self._first_load)

    def _first_load(self):
        try:
            self.refresh()
        finally:
            self._loading.hide_overlay()

    @staticmethod
    def _cmp(c, p, up_is_good):
        if p == 0 and c == 0:
            return "与上期持平", None
        if not up_is_good and c == 0 and p == 0:
            return "与上期持平", None
        return _delta_text(c, p, up_is_good)

    # ---------- 一键复制进度汇报（今日截至目前 vs 昨日，不受时间范围选择器影响） ----------
    def _copy_report(self):
        from datetime import datetime
        name, ok = QInputDialog.getText(
            self, "进度汇报", "汇报人姓名：",
            text=getattr(self, "_reporter", "") or USER_NAME or "")
        if not ok:
            return                      # 用户取消
        name = name.strip() or USER_NAME or "XX"
        self._reporter = name
        st = task_store.daily_report_stats()
        now = datetime.now()
        date_cn = f"{now.year}年{now.month}月{now.day}日"
        clock = now.strftime("%H:%M")

        total, okc = st["total"], st["ok"]
        rate = f"{okc / total * 100:.0f}%" if total else "—"
        known = st["long"] + st["short"]
        lp = f"{st['long'] / known * 100:.0f}%" if known else "—"
        sp = f"{st['short'] / known * 100:.0f}%" if known else "—"

        lines = [
            "📊 AIGC 数据汇报",
            f"姓名：{name}",
            f"日期：{date_cn}（截至目前 {clock}）",
            "",
            "【总体】",
            f"　总运行 {total} 次 ｜ 成功 {okc} 次 ｜ 成功率 {rate}",
            f"　环比昨日：运行 {_mom(total, st['y_total'])}，成功 {_mom(okc, st['y_ok'])}",
            "",
            "【分产品】",
        ]
        if st["products"]:
            for p, runs, oks in st["products"]:
                lines.append(f"　{p}：跑 {runs} 次，成功 {oks} 次")
        else:
            lines.append("　今日暂无执行记录")
        lines += [
            "",
            "【视频时长】",
            f"　10 秒以上：{st['long']} 条，占比 {lp} ｜ 环比昨日 {_mom(st['long'], st['y_long'])}",
            f"　10 秒以下：{st['short']} 条，占比 {sp} ｜ 环比昨日 {_mom(st['short'], st['y_short'])}",
        ]
        text = "\n".join(lines)
        QGuiApplication.clipboard().setText(text)
        self.b_copy.setText("✓ 已复制到剪切板")
        QTimer.singleShot(2500, lambda: self.b_copy.setText("📋 复制进度汇报"))
