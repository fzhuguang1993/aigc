"""
gui/pages_dashboard.py —— 数据中台（BI 看板）：时间范围切换 + KPI 环比卡片 + 页签

页签（BI 式下划线 tab，选中态记住进 ui_state）：
- 「总览」：趋势/状态/产品/时长/线路/当日逐时节奏六图 + KPI 行；
- 「其他图表」：量与质（执行量柱+成功率线）、每日用时结构（排队/生成堆叠）、
  星期×小时热力图、失败原因 TOP5、产品产出排行；同样吃顶部时间范围。
- 自定义页签：「＋ 新建视图」建一块空白画布，画布右键 →「添加组件」
  （排行条/每日趋势柱/聚合表 × 维度 × 指标）；组件按住标题栏可拖动、
  右下角可拉大拉小、「—」可最小化（没有最大化：能自由缩放）；组件
  右键可编辑/重命名/删除，页签双击可重命名。全部定义（含几何位置）
  存 ui_state 键 dash_custom_tabs，重启后原样恢复。

交互：
- 悬停任意图的分段 → 自绘浅色圆角提示卡（不用原生 QToolTip，防黑底）；
- 单击某一柱/段/行/热力格 → 弹窗列那一部分的执行明细（维度换表头，带 # 编号列）；
- 点图空白处、或鼠标悬在图上按空格 → 弹窗列当前范围全部明细，方便核对数据源；
- 页头「今日汇报」→ 弹窗展示汇报正文，打开即自动复制进剪切板。

每个页签各自包在滚动区里：图在小窗口装不下，宁可滚动也不挤压图表
（环形图被压小后图例会叠字）。明细不内嵌页底：内嵌表与页面滚动互踩，
且同一张表反复 clear+insert 会堆出几百行空白（clear() 不删行结构）。
"""
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication, QColor, QPainter, QPen
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QComboBox, QPushButton, QSizePolicy, QScrollArea,
                               QPlainTextEdit, QDialog, QTableWidget,
                               QTableWidgetItem, QHeaderView, QAbstractItemView,
                               QTabBar, QStackedWidget, QLineEdit, QFormLayout,
                               QMessageBox, QInputDialog, QFrame)

from core.config import USER_NAME
from store import app_state, task_store
from gui.formatting import secs
from gui.header import page_header, Card, KpiCard
from gui.widgets import LoadingOverlay
from gui.window_frame import apply_rounded
from gui.menus import StyledMenu
from gui import ui_kit
from gui.ui_kit import COLORS
from gui.charts import (TrendChart, DonutChart, HBarChart, LineChart, ComboChart,
                        HeatmapChart, PALETTE, C_OK, C_OK2, C_FAIL, C_FAIL2,
                        C_CANCEL, C_CANCEL2, C_BLUE, C_BLUE2)
from gui.theme import tokenize


# ---------- 图表点击下钻：每种维度一套表头（取值函数统一返字符串） ----------
_STATUS_CN = {"completed": "成功", "failed": "失败", "error": "错误",
              "cancelled": "取消", "running": "运行中"}
_STATUS_COLOR = ui_kit.STATUS_COLORS


def _c_hm(r):
    return (r["started_at"] or "")[11:16]


def _c_md(r):
    return (r["started_at"] or "")[5:16]


def _c_prod(r):
    return str(r["product"] or "").strip() or "未填品名"


def _c_acct(r):
    return str(r["account"] or "").strip() or "-"


def _c_num(r):
    return str(r["num"] or "")


def _c_vdur(r):
    d = int(r["vdur"] or 0)
    return f"{d}秒" if d else "—"


def _c_status(r):
    return _STATUS_CN.get(str(r["status"] or "running"), str(r["status"]))


def _c_gen(r):
    # 早期记录没拆分生成/排队（gen_sec=0）：回退总用时，不拿 0 骗人
    return secs(int(r["gen_sec"] or 0) or int(r["duration"] or 0))


def _c_que(r):
    q = int(r["queued_sec"] or 0)
    return secs(q) if q else "—"


def _c_tot(r):
    return secs(int(r["duration"] or 0))


def _c_tail(r):
    """成功看成品文件名，失败看错因：一列承担两种收尾信息"""
    if str(r["status"] or "") == "completed":
        out = str(r["output"] or "").split(";")[0].strip()
        return Path(out).name if out else "—"
    return str(r["error"] or "").strip() or "—"


_DAY_COLS = [("时刻", _c_hm), ("品名", _c_prod), ("线路", _c_acct), ("编号", _c_num),
             ("视频时长", _c_vdur), ("状态", _c_status), ("生成用时", _c_gen),
             ("成品/原因", _c_tail)]
# 热力图行名（与 charts.HeatmapChart.WD 同序：行 0=周一…6=周日）
_WEEK_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 表头刻意按维度各不相同：点的是什么就突出什么（日→时刻、产品→线路/状态、
# 时长桶→生成/排队、线路→总用时/排队），不做一张万金油表糊弄
_DRILL_COLS = {
    "day": _DAY_COLS,
    "hour": _DAY_COLS,
    "wdhour": _DAY_COLS,
    "status": [("执行时间", _c_md), ("品名", _c_prod), ("线路", _c_acct),
               ("编号", _c_num), ("视频时长", _c_vdur), ("生成用时", _c_gen),
               ("成品/原因", _c_tail)],
    "product": [("执行时间", _c_md), ("线路", _c_acct), ("编号", _c_num),
                ("状态", _c_status), ("视频时长", _c_vdur), ("生成用时", _c_gen),
                ("成品/原因", _c_tail)],
    "product_in": [("执行时间", _c_md), ("品名", _c_prod), ("线路", _c_acct),
                   ("状态", _c_status), ("视频时长", _c_vdur), ("生成用时", _c_gen),
                   ("成品/原因", _c_tail)],
    "dur": [("执行时间", _c_md), ("品名", _c_prod), ("线路", _c_acct),
            ("编号", _c_num), ("生成用时", _c_gen), ("排队", _c_que),
            ("成品文件", _c_tail)],
    "account": [("执行时间", _c_md), ("品名", _c_prod), ("编号", _c_num),
                ("状态", _c_status), ("视频时长", _c_vdur), ("总用时", _c_tot),
                ("排队", _c_que), ("成品/原因", _c_tail)],
    "error": [("执行时间", _c_md), ("品名", _c_prod), ("线路", _c_acct),
              ("编号", _c_num), ("状态", _c_status), ("失败原因", _c_tail)],
}
# 全部数据弹窗（点图空白处/悬停按空格）：字段铺全，方便和数据源逐条核对
_ALL_COLS = [("执行时间", _c_md), ("品名", _c_prod), ("线路", _c_acct),
             ("编号", _c_num), ("状态", _c_status), ("视频时长", _c_vdur),
             ("生成用时", _c_gen), ("排队", _c_que), ("总用时", _c_tot),
             ("成品/原因", _c_tail)]


# ---------- 自定义视图（用户自助新建页签）：维度/指标/形态中文字典 ----------
# key 与 store.custom_agg 的 _CUSTOM_DIMS/_CUSTOM_METRICS 对齐
_DIMS = [("day", "按天"), ("product", "品名"), ("account", "线路"),
         ("status", "状态"), ("dur", "视频时长"), ("hour", "小时"),
         ("wd", "星期")]
# 指标 →（中文名, 单位种）：单位种决定格式化（整数/百分比/时长）
_METRICS = {"total": ("执行条数", "int"), "ok": ("成功条数", "int"),
            "fail": ("失败条数", "int"), "rate": ("成功率", "pct"),
            "avg_gen": ("平均生成用时", "sec"), "avg_queued": ("平均排队用时", "sec")}
_SHOWS = [("rank", "排行条形图"), ("trend", "每日趋势柱"), ("table", "聚合表格")]
_DIM_CN = dict(_DIMS)
# 能对上 runs_drill 口径的维度才开点击下钻（hour 自定义是多日合并、
# wd 单维没下钻口径，都不开，免得点了给一屏不相干的明细）
_DIM_DRILL = {"day": "day", "product": "product", "account": "account",
              "status": "status", "dur": "dur"}


def _fmt_metric(kind, v):
    """按单位种格式化指标值：时长走 secs，百分比留一位小数"""
    if kind == "pct":
        return f"{v:.1f}%"
    if kind == "sec":
        return secs(int(round(v))) if v else "—"
    return str(int(round(v)))


def _migrate_spec(s):
    """旧定义（一页签一张图）→ 画布式（widgets 列表）的就地转换，新定义原样返回。

    上一版「新建视图」直接建一张图；现在页签＝空白画布＋若干组件，
    加载时把单图转成一个左上角落位的组件，之后爱怎么拖怎么拖。"""
    if isinstance(s.get("widgets"), list):
        return s
    s["widgets"] = [{"wid": uuid4().hex[:8], "x": 20, "y": 20,
                     "w": 640, "h": 400, "show": s.get("show") or "rank",
                     "dim": s["dim"], "metric": s["metric"],
                     "title": s.get("name") or "", "min": 0}] \
        if s.get("dim") in _DIM_CN and s.get("metric") in _METRICS else []
    return s


class _DashTabBar(QTabBar):
    """数据中台页签栏：双击自定义页签触发重命名（前两个固定页签双击无效）。

    QTabBar 没有内建的可编辑页签，双击弹输入框是最省事也最 BI 的改法；
    改名回调由页面注入（只有页面知道 页签序号 → spec 的映射）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rename_cb = None

    def mouseDoubleClickEvent(self, e):
        i = self.tabAt(e.position().toPoint())
        if i is not None and self.rename_cb:
            self.rename_cb(i)
        super().mouseDoubleClickEvent(e)


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
    """环比昨日同时段：箭头+百分比；昨日为 0 时给文字，避免除零/无穷。"""
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
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 14, 24, 0)
        outer.setSpacing(0)

        # ----- 页头 + 时间范围（汇报改成页头按钮：点击弹窗、打开即自动复制）-----
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 12)
        head.addWidget(page_header("数据中台",
                                   "总览 + 其他图表 + 自定义页签 · 点图出明细弹窗，"
                                   "点空白/悬停按空格看全部"), 1)
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
        self.b_report = QPushButton("📋 今日汇报")
        self.b_report.setObjectName("GhostBtn")
        self.b_report.setToolTip("今日截至目前 vs 昨日同时段的进度汇报；\n"
                                 "点击弹窗查看，打开时已自动复制进剪切板，直接粘贴到群里")
        self.b_report.clicked.connect(self._open_report)
        head.addWidget(self.b_report)
        self.b_records = QPushButton("🕘 查看执行记录")
        self.b_records.setObjectName("GhostBtn")
        self.b_records.setToolTip("每次提交的留痕与实时日志（原「执行记录」页，现已并入数据中台）")
        self.b_records.clicked.connect(self._open_records)
        head.addWidget(self.b_records)
        outer.addLayout(head)

        # ----- 页签栏（BI 式下划线）：总览 | 其他图表；时间范围/汇报按钮在页头共用 -----
        self.tabs = _DashTabBar()
        self.tabs.setObjectName("DashTabs")
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.addTab("  总览  ")
        self.tabs.addTab("  其他图表  ")
        self.tabs.setStyleSheet(
            "QTabBar{background:transparent;}"
            f"QTabBar::tab{{background:transparent;color:{COLORS['sub']};font-size:13px;"
            "padding:6px 18px;margin-right:8px;border:none;"
            "border-bottom:2px solid transparent;}"
            f"QTabBar::tab:selected{{color:{COLORS['primary']};font-weight:600;"
            f"border-bottom:2px solid {COLORS['primary']};}}"
            f"QTabBar::tab:hover{{color:{COLORS['text']};}}")
        self.tabs.currentChanged.connect(self._on_tab)
        self.tabs.rename_cb = self._rename_tab
        # 自定义页签带 × 可关；总览/其他图表两个固定页签的 × 在
        # _fix_close_buttons 里摘掉（只给新建的页签留关闭按钮）
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        tabrow = QHBoxLayout()
        tabrow.setContentsMargins(0, 0, 0, 0)
        tabrow.addWidget(self.tabs)
        self.b_addtab = QPushButton("＋ 新建视图")
        self.b_addtab.setObjectName("GhostBtn")
        self.b_addtab.setToolTip("建一块空白画布：画布右键 → 添加组件，\n"
                                 "组件拖标题栏移位、拖右下角缩放、双击页签改名")
        self.b_addtab.clicked.connect(self._new_custom)
        tabrow.addWidget(self.b_addtab)
        tabrow.addStretch(1)
        outer.addLayout(tabrow)
        outer.addSpacing(8)
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background:transparent;")
        outer.addWidget(self._stack, 1)

        # ----- Tab1 总览：滚动容器，内容装不下时页滚不压图 -----
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 10, 14)      # 右侧留滚动条位
        lay.setSpacing(12)
        area.setWidget(body)
        self._stack.addWidget(area)
        self._area = area                     # 总览页滚动容器（六图装不下时页滚）

        # ----- KPI 行 -----
        kpis = QHBoxLayout()
        kpis.setSpacing(14)
        self.k_total = KpiCard("执行总条数", "▶", COLORS["primary"])
        self.k_ok = KpiCard("成功", "✔", COLORS["success"])
        self.k_rate = KpiCard("成功率", "％", COLORS["purple"])
        self.k_fail = KpiCard("失败", "✖", COLORS["danger"])
        self.k_cancel = KpiCard("取消", "⊘", COLORS["weak"])
        self.k_run = KpiCard("正在运行", "◌", COLORS["warning"])
        self.k_dur = KpiCard("平均生成时长", "⏱", COLORS["info"])
        for k in (self.k_total, self.k_ok, self.k_rate, self.k_fail,
                  self.k_cancel, self.k_run, self.k_dur):
            k.setMinimumWidth(125)
            kpis.addWidget(k)
        lay.addLayout(kpis)

        # ----- 趋势 + 环形占比 -----
        mid = QHBoxLayout()
        mid.setSpacing(14)
        trend_card = Card()
        trend_card.setMinimumHeight(280)
        self.trend = TrendChart("每日执行趋势（成功 / 失败 / 取消 堆叠）")
        self.trend.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        trend_card.add(self.trend, 1)
        donut_card = Card()
        donut_card.setMinimumHeight(280)
        self.donut = DonutChart("状态占比")
        self.donut.setMinimumWidth(300)
        donut_card.add(self.donut, 1)
        mid.addWidget(trend_card, 3)
        mid.addWidget(donut_card, 2)
        lay.addLayout(mid)

        # ----- 产品分布 + 视频时长占比 + 线路分布 -----
        mid2 = QHBoxLayout()
        mid2.setSpacing(14)
        prod_card = Card()
        prod_card.setMinimumHeight(260)
        self.prod_donut = DonutChart("产品执行分布")
        # 说明文字不再挂 setToolTip：那走原生 QToolTip，部分 Win10 机器上黑底看不清；
        # 产品多时标题里带「含归并」，口径一眼可见
        prod_card.add(self.prod_donut, 1)
        dur_card = Card()
        dur_card.setMinimumHeight(260)
        self.dur_donut = DonutChart("视频时长占比")
        dur_card.add(self.dur_donut, 1)
        self.hbar_card = Card(margins=(16, 14, 16, 10))
        self.hbar_card.setMinimumHeight(260)
        self.hbar = HBarChart("各线路执行分布（总执行 / 成功数）")
        self.hbar.setMinimumHeight(150)
        self.hbar_card.add(self.hbar, 1)
        mid2.addWidget(prod_card, 2)
        mid2.addWidget(dur_card, 2)
        mid2.addWidget(self.hbar_card, 3)
        lay.addLayout(mid2)

        # ----- 当日执行节奏：逐时折线（固定只看今天，不随时间范围变）-----
        line_card = Card()
        line_card.setMinimumHeight(230)
        self.hourly = LineChart("今日执行节奏（按小时 · 固定只看当日）")
        line_card.add(self.hourly, 1)
        lay.addWidget(line_card)

        # ================= Tab2 其他图表：效率 · 质量 · 产能下钻 =================
        area2 = QScrollArea()
        area2.setWidgetResizable(True)
        area2.setFrameShape(QScrollArea.Shape.NoFrame)
        area2.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        body2 = QWidget()
        body2.setStyleSheet("background:transparent;")
        lay2 = QVBoxLayout(body2)
        lay2.setContentsMargins(0, 0, 10, 14)
        lay2.setSpacing(12)
        area2.setWidget(body2)
        self._stack.addWidget(area2)

        # 行1：量与质（柱+线复合图） | 每日用时结构（排队/生成堆叠柱）
        r2a = QHBoxLayout()
        r2a.setSpacing(14)
        self.rate_chart = ComboChart("每日执行量与成功率")
        self.rate_chart.setSizePolicy(QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Expanding)
        rate_card = Card()
        rate_card.setMinimumHeight(280)
        rate_card.add(self.rate_chart, 1)
        r2a.addWidget(rate_card, 3)
        self.dur_trend = TrendChart()          # 堆叠段换成「排队+生成」口径
        self.dur_trend.series = [("que", QColor("#FF8D19"), QColor("#FFC57A")),
                                 ("gen", C_OK, C_OK2)]
        self.dur_trend.legend = [(QColor("#FF8D19"), "排队"), (C_OK, "生成")]
        self.dur_trend.top_fmt = lambda v: secs(int(v))
        self.dur_trend.col_fmt = self._dur_col_text
        dur_card = Card()
        dur_card.setMinimumHeight(280)
        dur_card.add(self.dur_trend, 1)
        r2a.addWidget(dur_card, 2)
        lay2.addLayout(r2a)

        # 行2：星期×小时执行密度热力图（整宽：24 列要横向空间）
        heat_card = Card()
        heat_card.setMinimumHeight(300)
        self.heat = HeatmapChart("星期 × 小时 执行密度")
        heat_card.add(self.heat, 1)
        lay2.addWidget(heat_card)

        # 行3：失败原因 TOP5 | 产品产出排行（都是 HBarChart 换文案/标签口径）
        r2b = QHBoxLayout()
        r2b.setSpacing(14)
        self.err_bar = HBarChart("失败原因 TOP5")
        self.err_bar.label_w = 170
        self.err_bar.row_label = lambda r: (r["account"] or "")[:18] + \
            ("…" if len(str(r["account"] or "")) > 18 else "")
        self.err_bar.value_fn = lambda r: f"{r['total']} 条"
        self.err_bar.tip_fn = lambda r: f"{r['account']}\n失败 {r['total']} 次 · 点行看明细"
        err_card = Card(margins=(16, 14, 16, 10))
        err_card.setMinimumHeight(260)
        err_card.add(self.err_bar, 1)
        r2b.addWidget(err_card, 3)
        self.prod_bar = HBarChart("产品产出排行")
        prod_card = Card(margins=(16, 14, 16, 10))
        prod_card.setMinimumHeight(260)
        prod_card.add(self.prod_bar, 1)
        r2b.addWidget(prod_card, 2)
        lay2.addLayout(r2b)

        # 每张图声明自己被点时是什么维度；看板据此查库、换表头、开弹窗：
        # 点柱/段/行/热力格 → 那一部分明细；点空白 / 悬停按空格 → 全部明细
        for chart, kind in ((self.trend, "day"), (self.donut, "status"),
                            (self.prod_donut, "product"), (self.dur_donut, "dur"),
                            (self.hbar, "account"), (self.hourly, "hour"),
                            (self.rate_chart, "day"), (self.dur_trend, "day"),
                            (self.heat, "wdhour"), (self.err_bar, "error"),
                            (self.prod_bar, "product")):
            chart.drill_kind = kind
            chart.segment_clicked.connect(self._on_drill)
            chart.drill_all.connect(self._on_drill_all)
        self._prod_rest = []      # 本次刷新中被「其他」归并的产品名
        self._detail_dlg = None   # 明细弹窗复用单例：再点另一个维度直接换内容

        # ----- 自定义页签：从 ui_state 恢复（「＋ 新建视图」搭的画布）-----
        # 脏定义（手改过 json / 升级前的旧字段）直接丢掉，不让它拖炸整页；
        # 上一版的单图页签经 _migrate_spec 转成画布式（组件挂在左上角）
        saved_tabs = app_state.get("dash_custom_tabs")
        self._custom_specs = [_migrate_spec(s) for s in saved_tabs
                              if isinstance(s, dict) and s.get("id")
                              and str(s.get("name") or "").strip()] \
            if isinstance(saved_tabs, list) else []
        self._custom_views = {}
        for spec in self._custom_specs:
            self._attach_view(spec)
        self._fix_close_buttons()

        # 上次停在哪个页签：重启后回到原处（自定义页签建好后再恢复，索引才准）
        saved_tab = app_state.get("dash_tab")
        self.tabs.setCurrentIndex(saved_tab if isinstance(saved_tab, int)
                                  and 0 <= saved_tab < self.tabs.count() else 0)
        self._stack.setCurrentIndex(self.tabs.currentIndex())

        self._loading = LoadingOverlay(self)

    def _days(self):
        return self._RANGES[self.cb_range.currentIndex()]

    def _on_range(self):
        app_state.set_value("dash_range", self.cb_range.currentIndex())
        self.refresh()

    def _on_tab(self, i):
        """切页签：内容栈跟着切 + 记住停在哪页（重启回原处）"""
        self._stack.setCurrentIndex(i)
        app_state.set_value("dash_tab", i)

    # ---------- 自定义页签：新建 / 重命名 / 删除，定义（含组件几何）存 dash_custom_tabs ----------
    def _attach_view(self, spec):
        """挂一个自定义页签（追加到末尾）；增删过程静默信号，索引对齐后手动同步"""
        view = CustomView(self, spec)
        self._custom_views[spec["id"]] = view
        self.tabs.blockSignals(True)
        self.tabs.addTab(f"  {spec['name']}  ")
        self._stack.addWidget(view)
        self.tabs.blockSignals(False)
        return view

    def _destroy_view(self, tid, at=None):
        """拆掉一个自定义页签：tab 与内容栈同步删；at＝tab 序号（调用方
        已先把 spec 从列表里拿掉时必须传，否则 _tab_index 找不到会残留空页签）"""
        view = self._custom_views.pop(tid, None)
        if view is None:
            return
        i = at if at is not None else self._tab_index(tid)
        self.tabs.blockSignals(True)
        if i is not None:
            self.tabs.removeTab(i)
        self._stack.removeWidget(view)
        view.setParent(None)
        self.tabs.blockSignals(False)
        self._stack.setCurrentIndex(min(self.tabs.currentIndex(),
                                        self._stack.count() - 1))

    def _tab_index(self, tid):
        """spec id → tab 序号（前两个固定页签占 0/1，自定义按 _custom_specs 顺序排）"""
        for n, s in enumerate(self._custom_specs):
            if s["id"] == tid:
                return 2 + n
        return None

    def _save_custom(self):
        app_state.set_value("dash_custom_tabs", self._custom_specs)

    def _fix_close_buttons(self):
        """总览/其他图表是固定页签：摘掉 ×，只让自定义页签可关"""
        for i in range(min(2, self.tabs.count())):
            self.tabs.setTabButton(i, QTabBar.ButtonPosition.RightSide, None)

    def _select_tab(self, i):
        self.tabs.setCurrentIndex(i)
        self._stack.setCurrentIndex(i)
        app_state.set_value("dash_tab", i)

    def _new_custom(self):
        """新建视图＝空白画布：进去右键→添加组件（BI 式）；重名自动编号"""
        names = {s["name"] for s in self._custom_specs}
        name, n = "新视图", 1
        while name in names:
            n += 1
            name = f"新视图 {n}"
        spec = {"id": uuid4().hex[:8], "name": name, "widgets": []}
        self._custom_specs.append(spec)
        self._save_custom()
        self._attach_view(spec)
        self._fix_close_buttons()
        self._select_tab(self._tab_index(spec["id"]))

    def _rename_tab(self, i):
        """双击自定义页签改名；前两个固定页签是系统定义，不让改"""
        if i < 2 or i - 2 >= len(self._custom_specs):
            return
        spec = self._custom_specs[i - 2]
        text, ok = QInputDialog.getText(self, "重命名页签", "页签名称：",
                                        QLineEdit.EchoMode.Normal, spec["name"])
        text = (text or "").strip()
        if ok and text and text != spec["name"]:
            spec["name"] = text
            self.tabs.setTabText(i, f"  {text}  ")
            self._save_custom()

    def _remove_custom(self, tid):
        spec = next((s for s in self._custom_specs if s["id"] == tid), None)
        if spec is None:
            return
        n = len(spec.get("widgets") or [])
        r = QMessageBox.question(
            self, "删除视图",
            f"确定删除页签「{spec['name']}」？"
            + (f"页签里的 {n} 个组件会一并删除，" if n else "")
            + "删除后无法恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        i = self._tab_index(tid)          # 删 spec 前先拿好页签序号
        self._custom_specs = [s for s in self._custom_specs if s["id"] != tid]
        self._save_custom()
        was = self.tabs.currentIndex()
        self._destroy_view(tid, at=i)
        self._select_tab(min(was, self.tabs.count() - 1))

    def _close_tab(self, i):
        # 页签上的 ×：前两个固定页签没 ×，按到也走不到这里（双保险）
        if i < 2 or i - 2 >= len(self._custom_specs):
            return
        self._remove_custom(self._custom_specs[i - 2]["id"])

    def _open_records(self):
        from gui.pages_records import RecordsDialog
        RecordsDialog(self).exec()

    def _open_report(self):
        """今日汇报弹窗：打开即自动把正文复制进剪切板"""
        ReportDialog(self, self._report_text()).exec()

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

        # 产品执行分布：超过色板容量的产品归「其他」，环形图例不挤爆
        prods = st["products"]
        segs = [(p, runs, QColor(PALETTE[i][0]), QColor(PALETTE[i][1]))
                for i, (p, runs, _ok) in enumerate(prods[:len(PALETTE) - 1])]
        rest = sum(runs for _p, runs, _ok in prods[len(PALETTE) - 1:])
        if rest:
            segs.append(("其他", rest, QColor(PALETTE[-1][0]), QColor(PALETTE[-1][1])))
        # 记下被「其他」归并的产品名：点这一块下钻时按产品名集合查明细
        self._prod_rest = [p for p, _n, _o in prods[len(PALETTE) - 1:]]
        self.prod_donut.title = (f"产品执行分布 · {scope}"
                                 + ("（含归并）" if self._prod_rest else ""))
        self.prod_donut.set_data(segs, center_text=str(cur["total"]),
                                 center_sub="执行条数")

        # 视频时长占比：四桶固定色（短→长：蓝/绿/橙/青），中心是入桶总数
        dcols = (PALETTE[0], PALETTE[1], PALETTE[2], PALETTE[4])
        self.dur_donut.title = f"视频时长占比 · {scope}（只计成功）"
        self.dur_donut.set_data(
            [(d["label"], d["value"], QColor(c1), QColor(c2))
             for d, (c1, c2) in zip(st["durations"], dcols)],
            center_text=str(sum(d["value"] for d in st["durations"])),
            center_sub="成功条数")

        # 当日逐时节奏：永远是今天的口径，切时间范围也不重算它（refresh 里
        # 顺手重设无害，hourly 数据本来就是 range_stats 固定按今天查的）
        self.hourly.set_data(st["hourly"])

        self._refresh_extra(days, st, cur)
        # 自定义页签同样吃顶部时间范围
        for view in self._custom_views.values():
            view.refresh(days)

    def _refresh_extra(self, days, st, cur):
        """「其他图表」页签的五图：吃同一份时间范围，跟着 refresh 一起刷"""
        scope = self._scope(days)
        ex = task_store.extra_stats(days)

        # 量与质：执行量柱 + 成功率线（成功率在 range_stats 的 ok/total 上现算）
        rows = [dict(r, rate=(r["ok"] / r["total"] * 100) if r["total"] else 0.0)
                for r in st["daily"]]
        self.rate_chart.title = f"每日执行量与成功率 · {scope}"
        self.rate_chart.set_data(rows)

        # 每日用时结构：排队+生成堆叠；早期没拆分的记录进不了图，
        # 标题里把口径写死，免得被当成“那几天没干活”
        self.dur_trend.title = (f"每日用时结构 · {scope}"
                                "（排队/生成 · 仅计可拆分记录）")
        self.dur_trend.set_data(ex["dur_daily"][-60:])

        # 星期×小时热力图
        self.heat.title = f"星期 × 小时 执行密度 · {scope}"
        self.heat.set_data(ex["heatmap"])

        # 失败原因 TOP5：reason 借用 account 字段（HBarChart 的通用行键），
        # 下钻时 _drill_key 原样吐回来给 runs_drill("error", 原因原文)
        self.err_bar.title = f"失败原因 TOP5 · {scope}"
        self.err_bar.set_data([{"account": e["reason"], "total": e["count"],
                                "ok": 0} for e in ex["errors"]])

        # 产品产出排行：range_stats.products 是 (品名, 总条数, 成功数)，取前 8
        self.prod_bar.title = f"产品产出排行 · {scope}"
        self.prod_bar.set_data([{"account": p, "total": n, "ok": ok}
                                for p, n, ok in st["products"][:8]])

    @staticmethod
    def _dur_col_text(r):
        """用时堆叠柱的悬停文案（dur_daily 行：n=可拆分成功条数）"""
        d = (r["d"] or "")[5:].replace("-", "/")
        return (f"{d} · {r['n']} 条成功（可拆分记录）\n"
                f"平均排队 {secs(int(r['que']))} · 平均生成 {secs(int(r['gen']))}")

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

    # ---------- 图表点击下钻：维度决定查询条件与表头，结果弹窗展示 ----------
    def _on_drill(self, kind, key):
        days = self._days()
        if kind == "product" and key == "其他":
            rows = task_store.runs_drill("product_in", self._prod_rest, days)
            kind = "product_in"
        else:
            rows = task_store.runs_drill(kind, key, days)
        scope = self._scope(days)
        # 逐分支求值，不能写成字典字面量——int(key) 之类只对自家 kind 合法
        if kind == "day":
            desc = f"{key} · 当日全部执行"
        elif kind == "hour":
            desc = f"今日 {int(key):02d}:00 ~ {int(key):02d}:59 的执行"
        elif kind == "status":
            desc = f"{scope} · 状态「{key}」"
        elif kind == "product":
            desc = f"{scope} · 产品「{key}」"
        elif kind == "product_in":
            desc = f"{scope} · 其余产品（饼图「其他」）"
        elif kind == "dur":
            desc = f"{scope} · 视频时长「{key}」· 只计成功"
        elif kind == "account":
            desc = f"{scope} · 线路「{key or '-'}」"
        elif kind == "error":
            desc = f"{scope} · 失败原因「{(key or '')[:40]}」"
        elif kind == "wdhour":
            wd, h = key
            desc = (f"{scope} · {_WEEK_CN[int(wd)]} "
                    f"{int(h):02d}:00 ~ {int(h):02d}:59 的执行")
        else:
            desc = str(key)
        more = "（仅列最近 300 条，看全部请点图空白处）" if len(rows) >= 300 else ""
        self._show_detail(f"🔎 {desc} · 共 {len(rows)} 条{more}",
                          rows, _DRILL_COLS.get(kind, _DAY_COLS))

    def _on_drill_all(self, kind):
        """点图空白处 / 悬停按空格：当前范围的全部执行明细，弹窗带 # 编号列，
        行数和数据源（执行记录页/导出）能逐条对上"""
        days = self._days()
        rows = task_store.runs_drill("all", None, days, limit=None)
        self._show_detail(f"🔎 {self._scope(days)} · 全部执行明细 · 共 {len(rows)} 条",
                          rows, _ALL_COLS)

    def _show_detail(self, title, rows, cols):
        # 复用同一个弹窗（父挂主窗口：切页面不丢）；再次下钻原地换内容
        if self._detail_dlg is None:
            self._detail_dlg = DetailDialog(self.window())
        self._detail_dlg.fill(title, rows, cols)
        self._detail_dlg.show()
        self._detail_dlg.raise_()
        self._detail_dlg.activateWindow()

    # ---------- 今日汇报：页头按钮弹窗，打开即自动复制 ----------
    # 口径：今日截至目前 vs 昨日同时段，不受时间范围选择器影响
    def _report_text(self):
        from datetime import datetime
        st = task_store.daily_report_stats()
        now = datetime.now()
        date_cn = f"{now.year}年{now.month}月{now.day}日"
        clock = now.strftime("%H:%M")
        name = USER_NAME or "XX"

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
            f"　环比昨日同时段：运行 {_mom(total, st['y_total'])}，成功 {_mom(okc, st['y_ok'])}",
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
            "【视频时长】（只计成功）",
            f"　10 秒以上：{st['long']} 条，占比 {lp} ｜ 环比昨日同时段 {_mom(st['long'], st['y_long'])}",
            f"　10 秒以下：{st['short']} 条，占比 {sp} ｜ 环比昨日同时段 {_mom(st['short'], st['y_short'])}",
            "",
            "【审片标记】",
            f"　目前标记可用：{st['avail']} 条（累计）",
        ]
        text = "\n".join(lines)
        return text


class ReportDialog(QDialog):
    """今日汇报弹窗：正文只读展示，打开即自动复制进剪切板，按钮可重复制。

    从页底常驻卡改成页头按钮弹窗：不占看板空间，不留常驻内容。
    无边框圆角外壳由 apply_rounded 装配（固定尺寸弹窗自动补标题栏高度）。"""

    def __init__(self, parent=None, text=""):
        super().__init__(parent)
        self.setWindowTitle("今日进度汇报")
        self.setFixedSize(600, 560)
        self._text = text or ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("📊 今日进度汇报")
        title.setObjectName("DialogTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.b_copy = QPushButton("📋 复制汇报")
        head.addWidget(self.b_copy)
        b_close = QPushButton("关闭")
        b_close.setObjectName("GhostBtn")
        b_close.clicked.connect(self.reject)
        head.addWidget(b_close)
        lay.addLayout(head)

        hint = QLabel("今日截至目前 vs 昨日同时段 · 汇报人用「设置」里的姓名 · "
                      "不受数据中台时间范围影响；打开本窗口已自动复制，直接去群里粘贴")
        hint.setStyleSheet(tokenize("font-size:11px;color:#8F959E;background:transparent;"))
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setPlainText(self._text)
        self.view.setStyleSheet(
            tokenize("QPlainTextEdit{background:#F7F8FA;border:1px solid #E8EAED;"
            "border-radius:8px;font-size:12.5px;color:#1F2329;}"))
        lay.addWidget(self.view, 1)
        self.b_copy.clicked.connect(self._copy)
        apply_rounded(self, show_min=False, show_max=False)
        # 打开即自动复制：延一拍等窗口显示完再写剪切板，不会被启动过程中的其它写入盖掉
        QTimer.singleShot(0, self._copy)

    def _copy(self):
        if not self._text:
            return
        QGuiApplication.clipboard().setText(self._text)
        self.b_copy.setText("✓ 已复制到剪切板")
        QTimer.singleShot(2500, lambda: self.b_copy.setText("📋 复制汇报"))


class DetailDialog(QDialog):
    """下钻明细弹窗：首列 # 编号 + 维度表头，表内自带滚动。

    为什么不内嵌页底：① 内嵌表与整页滚动互踩（定时刷新时代偷滚动位）；
    ② QTableWidget.clear() 只清单元格不删行结构，同一张表反复
    clear+insertRow 会堆出几百上千行空白——弹窗每次 fill 用
    setRowCount 整表重建，天然没有残留；③ 弹窗可与看板并排，
    对着图核对数据源方便。编号按返回顺序（开始时间倒序）行号，
    排序后数字会跟着行走，仍能定位到数据源里的那一条。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("执行明细")
        # 窗口尺寸跟着“整体放大 50%”走（1040×640 的 1.5 倍），并钳在
        # 可用屏区 92% 内：小屏上弹窗压满屏幕就没法对着看板看了
        scr = QGuiApplication.primaryScreen()
        av = scr.availableGeometry() if scr else None
        w = min(1560, int(av.width() * 0.92)) if av else 1560
        h = min(960, int(av.height() * 0.92)) if av else 960
        self.resize(w, h)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 14, 20, 16)
        lay.setSpacing(8)
        self.lbl = QLabel("")
        self.lbl.setObjectName("DialogTitle")
        lay.addWidget(self.lbl)
        self.table = QTableWidget(0, 0)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(True)
        lay.addWidget(self.table, 1)
        foot = QHBoxLayout()
        self.lbl_hint = QLabel("首列 # 是按开始时间倒序的行号，用于和数据源逐条核对"
                               " · 点表头可排序 · Esc 关闭")
        self.lbl_hint.setStyleSheet(tokenize("font-size:11px;color:#8F959E;background:transparent;"))
        foot.addWidget(self.lbl_hint, 1)
        b_close = QPushButton("关闭")
        b_close.setObjectName("GhostBtn")
        b_close.clicked.connect(self.reject)
        foot.addWidget(b_close)
        lay.addLayout(foot)
        apply_rounded(self, title="执行明细")

    def fill(self, title, rows, cols):
        self.lbl.setText(title)
        t = self.table
        t.setSortingEnabled(False)
        headers = ["#"] + [h for h, _f in cols]
        t.setColumnCount(len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setRowCount(len(rows))           # 整表重建：不残留旧行
        for i, r in enumerate(rows):
            it0 = QTableWidgetItem(str(i + 1))
            it0.setTextAlignment(Qt.AlignmentFlag.AlignVCenter
                                 | Qt.AlignmentFlag.AlignCenter)
            it0.setForeground(QColor("#8F959E"))
            t.setItem(i, 0, it0)
            for c, (head, fn) in enumerate(cols, start=1):
                v = str(fn(r) or "")
                it = QTableWidgetItem(v)
                it.setToolTip(v)
                leftish = ("品名" in head) or ("成品" in head) or ("原因" in head)
                it.setTextAlignment(
                    Qt.AlignmentFlag.AlignVCenter
                    | (Qt.AlignmentFlag.AlignLeft if leftish
                       else Qt.AlignmentFlag.AlignCenter))
                if head == "状态":
                    it.setForeground(QColor(_STATUS_COLOR.get(v, "#FF8D19")))
                t.setItem(i, c, it)
        h = t.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        t.setColumnWidth(0, 52)
        for c, (head, _f) in enumerate(cols, start=1):
            last = c == len(cols)
            h.setSectionResizeMode(
                c, QHeaderView.ResizeMode.Stretch if last
                else QHeaderView.ResizeMode.Interactive)
            if not last:
                t.setColumnWidth(c, 150 if ("品名" in head or "成品" in head)
                                 else max(70, len(head) * 16))
        t.setSortingEnabled(True)
        t.scrollToTop()

    def keyPressEvent(self, e):
        # 无边框后原生 Esc 关闭没了（QDialog 靠系统窗口处理），自家补上
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)


class CustomView(QWidget):
    """自定义页签＝自由布局的 BI 画布。

    空画布右键 →「添加组件」（排行条/每日趋势柱/聚合表 × 维度 × 指标）；
    组件拖标题栏移位、拖右下角改大小、「—」最小化（没有最大化：
    能自由缩放就没必要最大化）；组件右键可编辑/重命名/删除。
    几何和定义都写进 spec["widgets"]，改完立刻 page._save_custom() 落盘，
    重启原样恢复。时间范围统一吃页头（refresh 由看板驱动，逐组件喂数）。"""

    _NEW_W, _NEW_H = 620, 400    # 新组件默认尺寸
    _STEP = 28                   # 级联落位偏移，免得多个组件叠在同一位置

    def __init__(self, page, spec, parent=None):
        super().__init__(parent)
        self.page = page
        self.spec = spec          # 直接引用：panel 改 wspec 就是改 _custom_specs 里的元素
        self._days = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 10, 14)      # 与其他页签的滚动内容同规
        lay.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        self.canvas = _Canvas(self)
        scroll.setWidget(self.canvas)
        lay.addWidget(scroll, 1)
        self.panels = {}
        for w in self.spec.get("widgets") or []:
            # 脏组件（手改过 json / 维度指标已下线）静默跳过，不拖炸整页
            if isinstance(w, dict) and w.get("dim") in _DIM_CN \
                    and w.get("metric") in _METRICS:
                self._make_panel(w)
        self.canvas.sync_size()

    # ---------- 组件生命周期 ----------
    def _make_panel(self, wspec):
        p = WidgetPanel(self, wspec)
        self.panels[wspec["wid"]] = p
        return p

    def _cur_days(self):
        # 新建/编辑组件时页面可能还没 refresh 过，兜底用页头当前选择
        return self._days if self._days is not None else self.page._days()

    def prompt_add_widget(self):
        """画布右键菜单入口：向导选形态×维度×指标，级联落位放新组件"""
        n = len(self.panels)
        wspec = {"wid": uuid4().hex[:8], "x": 20 + n * self._STEP,
                 "y": 20 + n * self._STEP, "w": self._NEW_W, "h": self._NEW_H,
                 "show": "rank", "dim": "product", "metric": "total",
                 "title": "", "min": 0}
        dlg = CustomViewDialog(self, wspec)
        if not dlg.exec():
            return
        wspec.update(dlg.result)
        self.spec.setdefault("widgets", []).append(wspec)
        p = self._make_panel(wspec)
        self.canvas.sync_size()
        self._save()
        p.refresh(self._cur_days())
        self.canvas.update()          # 从空画布的提示文案切过来

    def edit_widget(self, panel):
        """组件右键→编辑数据：形态/维度/指标都可能换，重建内容最快"""
        dlg = CustomViewDialog(self, panel.w)
        if not dlg.exec():
            return
        panel.w.update(dlg.result)
        panel.rebuild()
        self._save()
        panel.refresh(self._cur_days())

    def rename_widget(self, panel):
        text, ok = QInputDialog.getText(self, "重命名组件", "组件标题：",
                                        QLineEdit.EchoMode.Normal,
                                        panel.w.get("title", ""))
        if ok:
            panel.w["title"] = text.strip()
            panel.apply_title()
            self._save()

    def remove_panel(self, panel):
        self.spec["widgets"] = [w for w in self.spec.get("widgets") or []
                                if w.get("wid") != panel.w.get("wid")]
        self.panels.pop(panel.w.get("wid"), None)
        panel.setParent(None)
        self.canvas.sync_size()
        self.canvas.update()          # 删掉最后一个组件时要重新出提示
        self._save()

    def geom_changed(self):
        """拖拽/缩放/最小化落定：同步画布可滚范围＋立刻持久化"""
        self.canvas.sync_size()
        self._save()

    def _save(self):
        self.page._save_custom()

    # ---------- 数据刷新 ----------
    def refresh(self, days):
        self._days = days
        for p in self.panels.values():
            p.refresh(days)


class _Canvas(QWidget):
    """自由画布：不用布局，组件按绝对坐标摆放；淡点网格方便对齐；
    空白处右键弹「添加组件」（点到组件上由组件自己的菜单接手）。"""

    PAD = 60        # 组件贴到右下角时多留的边，拖角缩放不会被滚动区卡住

    def __init__(self, view):
        super().__init__()
        self.view = view
        self.setMinimumSize(640, 420)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#FBFCFE"))
        p.setPen(QColor("#E8EBF0"))
        for y in range(18, self.height(), 26):
            for x in range(18, self.width(), 26):
                p.drawPoint(x, y)
        if not self.view.panels:
            f = p.font()
            f.setPointSizeF(11)
            p.setFont(f)
            p.setPen(QColor("#A2A8B4"))
            p.drawText(self.rect().adjusted(40, 0, -40, 0),
                       Qt.AlignmentFlag.AlignCenter,
                       "空白画布：右键 → ＋ 添加组件\n"
                       "组件可拖标题栏移位、拖右下角缩放、“—”最小化")
        super().paintEvent(e)

    def contextMenuEvent(self, e):
        m = StyledMenu(self)
        m.addAction("＋ 添加组件…", self.view.prompt_add_widget)
        m.exec(e.globalPos())

    def sync_size(self):
        """可滚范围＝最远组件右下角＋余量：拖到哪滚到哪，不会把组件甩出可视区"""
        w = h = 0
        for p in self.view.panels.values():
            w = max(w, p.x() + p.width())
            h = max(h, p.y() + p.height())
        self.setMinimumSize(max(640, w + self.PAD), max(420, h + self.PAD))


class _PanelBar(QWidget):
    """组件标题栏：整条都是拖拽手柄（按住空白处拖＝移动组件）；
    右侧「—」按钮最小化，没有最大化按钮（能自由缩放）。"""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self._off = None
        self.setFixedHeight(WidgetPanel.T_H)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 2, 0)
        lay.setSpacing(6)
        self.lbl = QLabel()
        self.lbl.setStyleSheet(tokenize("font-size:12px;font-weight:600;color:#1F2329;"
                               "background:transparent;"))
        self.sub = QLabel()
        self.sub.setStyleSheet(tokenize("font-size:11px;color:#8F959E;"
                               "background:transparent;"))
        b_min = QPushButton("—")
        b_min.setFixedSize(26, 20)
        b_min.setCursor(Qt.CursorShape.PointingHandCursor)
        b_min.setStyleSheet(
            tokenize("QPushButton{border:none;border-radius:4px;color:#646A73;"
            "background:transparent;font-size:12px;}"
            "QPushButton:hover{background:#EFF1F5;}"
            "QPushButton:pressed{background:#E2E5EC;}"))
        b_min.setToolTip("最小化 / 还原（最小化后只留这条标题栏）")
        b_min.clicked.connect(panel.toggle_min)
        lay.addWidget(self.lbl)
        lay.addWidget(self.sub, 1)
        lay.addWidget(b_min)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._off = e.globalPosition().toPoint() - self.panel.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._off is None:
            return
        pt = e.globalPosition().toPoint() - self._off
        pan = self.panel
        pan.move(max(0, pt.x()), max(0, pt.y()))   # 左上角不许拖出负坐标
        pan.view.canvas.sync_size()

    def mouseReleaseEvent(self, e):
        if self._off is not None:
            self._off = None
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            self.panel.geom_done()


class _Grip(QWidget):
    """右下角缩放手柄：斜着拖改组件大小（下限 260×170），松手即落盘。"""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self._g0 = None
        self._sz0 = None
        self.setFixedSize(18, 18)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self.setToolTip("拖动改变大小")

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#B8BEC7"), 1.2))
        x0, y0 = self.width() - 4, self.height() - 4
        for k in range(3):
            p.drawLine(x0 - k * 4, y0, x0, y0 - k * 4)
        super().paintEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._g0 = e.globalPosition().toPoint()
            self._sz0 = (self.panel.width(), self.panel.height())

    def mouseMoveEvent(self, e):
        if self._g0 is None:
            return
        d = e.globalPosition().toPoint() - self._g0
        pan = self.panel
        pan.resize(max(WidgetPanel.MIN_W, self._sz0[0] + d.x()),
                   max(WidgetPanel.MIN_H, self._sz0[1] + d.y()))
        pan.view.canvas.sync_size()     # 拖大途中就扩可滚范围，不等松手

    def mouseReleaseEvent(self, e):
        if self._g0 is not None:
            self._g0 = None
            self.panel.geom_done()


class WidgetPanel(QFrame):
    """画布上的一个图表/表格组件：白底卡片＋可拖标题栏＋右下角缩放手柄。

    渲染与下钻规则与单图时代一致：趋势柱的时间轴锁死按天；口径对得上
    runs_drill 的维度（日/产品/线路/状态/时长）才开点击下钻。图内不画标题
    （标题在组件自己的标题栏上），图例位腾给数据。"""

    T_H = 30                      # 标题栏高
    MIN_W, MIN_H = 260, 170       # 缩放下限：再小图表就画不下去了

    def __init__(self, view, wspec, parent=None):
        super().__init__(parent or view.canvas)
        self.view = view
        self.w = wspec
        self.table = None
        self.chart = None
        self._full_h = 0          # 最小化前的实际高度，还原时用
        self.setObjectName("WidgetPanel")
        # 子类不自开 WA_StyledBackground：不补这行，下面内联样式的白底画不出
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            tokenize("#WidgetPanel{background:#FFFFFF;border:1px solid #E5E8EF;"
            "border-radius:8px;}"))
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(8, 4, 8, 8)
        self._lay.setSpacing(4)
        self.bar = _PanelBar(self)
        self._lay.addWidget(self.bar)
        self.body = QWidget()
        self.body.setStyleSheet("background:transparent;")
        self._bl = QVBoxLayout(self.body)
        self._bl.setContentsMargins(0, 0, 0, 0)
        self._bl.setSpacing(0)
        self._lay.addWidget(self.body, 1)
        self.grip = _Grip(self)
        self.move(int(wspec.get("x", 20)), int(wspec.get("y", 20)))
        self.resize(max(self.MIN_W, int(wspec.get("w", 620))),
                    max(self.MIN_H, int(wspec.get("h", 400))))
        self._build_content()
        if wspec.get("min"):
            self.set_min(True)

    # ---------- 内容三形态（与单图版同口径） ----------
    def _build_content(self):
        show = self.w.get("show", "rank")
        if show == "trend":
            self.w["dim"] = "day"         # 趋势柱的时间轴只能是按天
        dim = self.w.get("dim", "product")
        self._mlab, self._kind = _METRICS.get(self.w.get("metric", "total"),
                                              _METRICS["total"])
        self.table = None
        self.chart = None
        if show == "table":
            t = QTableWidget(0, 3)
            t.setHorizontalHeaderLabels(
                [_DIM_CN.get(dim, dim), self._mlab, "执行次数"])
            t.setAlternatingRowColors(True)
            t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            t.setSelectionBehavior(
                QAbstractItemView.SelectionBehavior.SelectRows)
            t.verticalHeader().setVisible(False)
            t.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.Stretch)
            # 表格不自吃右键：事件传给组件菜单（编辑/重命名/删除）
            t.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            t.viewport().setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            self.table = t
            self._bl.addWidget(t)
        elif show == "trend":
            ch = TrendChart("")
            ch.series = [("ok", C_BLUE, C_BLUE2)]
            ch.legend = [(C_BLUE, self._mlab)]
            ch.top_fmt = lambda x: _fmt_metric(self._kind, x)
            ch.col_fmt = self._col_text
            self.chart = ch
            self._bl.addWidget(ch, 1)
        else:
            ch = HBarChart("")
            ch.label_w = 120
            ch.row_label = lambda r: self._short(self._dim_label(r["account"]))
            ch.value_fn = lambda r: _fmt_metric(self._kind, r["v"])
            ch.tip_fn = lambda r: (f"{self._dim_label(r['account'])}\n"
                                   f"{self._mlab} {_fmt_metric(self._kind, r['v'])}"
                                   f" · 执行 {r['n']} 次")
            self.chart = ch
            self._bl.addWidget(ch, 1)
        drill = _DIM_DRILL.get(self.w.get("dim"))
        if drill and self.chart is not None:
            self.chart.drill_kind = drill
            self.chart.segment_clicked.connect(self.view.page._on_drill)
            self.chart.drill_all.connect(self.view.page._on_drill_all)
        self.apply_title()

    def rebuild(self):
        """编辑组件定义后重造内容（换形态就是换图，整建比补丁干净）"""
        while self._bl.count():
            it = self._bl.takeAt(0)
            wd = it.widget()
            if wd is not None:
                wd.setParent(None)
        self._build_content()
        if self.w.get("min"):
            self.body.setVisible(False)

    def apply_title(self):
        base = self.w.get("title") or \
            f"{_DIM_CN.get(self.w.get('dim'), '')}{self._mlab}"
        self.bar.lbl.setText(self._short(base, 14))
        self.bar.lbl.setToolTip(base)

    # ---------- 移动 / 缩放 / 最小化 ----------
    def geom_done(self):
        """拖拽或拖角落定：几何写回 spec 并持久化（最小化中不覆盖尺寸）"""
        self.w["x"], self.w["y"] = self.x(), self.y()
        if not self.w.get("min"):
            self.w["w"], self.w["h"] = self.width(), self.height()
        self.view.geom_changed()

    def resizeEvent(self, e):
        self.grip.move(self.width() - self.grip.width(),
                       self.height() - self.grip.height())
        self.grip.raise_()
        super().resizeEvent(e)

    def toggle_min(self):
        self.set_min(not self.w.get("min"))
        self.geom_done()

    def set_min(self, m):
        self.w["min"] = 1 if m else 0
        self.body.setVisible(not m)
        self.grip.setVisible(not m)
        if m:
            self._full_h = self.height()
            mgn = self._lay.contentsMargins()
            self.setFixedHeight(self.T_H + mgn.top() + mgn.bottom())
        else:
            self.setMinimumHeight(self.MIN_H)
            self.setMaximumHeight(16777215)
            self.resize(self.width(),
                        max(self.MIN_H, self._full_h
                            or int(self.w.get("h", self.MIN_H))))
        self.update()

    def contextMenuEvent(self, e):
        m = StyledMenu(self)
        m.addAction("✎ 编辑数据…", lambda: self.view.edit_widget(self))
        m.addAction("✏ 重命名组件", lambda: self.view.rename_widget(self))
        m.addAction("↩ 还原" if self.w.get("min") else "— 最小化",
                    self.toggle_min)
        m.addSeparator()
        m.addAction("🗑 删除组件", lambda: self.view.remove_panel(self))
        m.exec(e.globalPos())

    # ---------- 渲染（组值→展示标签：下钻键仍是原文，store 那边对得上） ----------
    def _dim_label(self, k):
        dim = self.w.get("dim")
        if dim == "status":
            return _STATUS_CN.get(k, k)
        if dim == "wd":
            try:
                return _WEEK_CN[int(k)]
            except (TypeError, ValueError):
                return str(k)
        if dim == "hour":
            return f"{k}:00"
        if dim == "product":
            return k or "未填品名"
        if dim == "account":
            return k or "-"
        return k

    @staticmethod
    def _short(s, n=16):
        s = str(s)
        return s if len(s) <= n else s[:n] + "…"

    def _col_text(self, r):
        d = (r["d"] or "")[5:].replace("-", "/")
        return (f"{d} · {self._mlab} {_fmt_metric(self._kind, r['v'])}\n"
                f"当日执行 {r['n']} 次")

    def refresh(self, days):
        rows = task_store.custom_agg(self.w["dim"], self.w["metric"], days)
        self.bar.sub.setText(f"{_DIM_CN.get(self.w['dim'], '')} × {self._mlab}"
                             f" · {self.view.page._scope(days)} · {len(rows)} 组")
        if self.table is not None:
            t = self.table
            t.setRowCount(len(rows))          # 整表重建，不残留旧行
            for i, r in enumerate(rows):
                for c, text in ((0, self._dim_label(r["k"])),
                                (1, _fmt_metric(self._kind, r["v"])),
                                (2, str(r["n"]))):
                    it = QTableWidgetItem(text)
                    it.setTextAlignment(Qt.AlignmentFlag.AlignVCenter
                                        | Qt.AlignmentFlag.AlignCenter)
                    it.setToolTip(text)
                    t.setItem(i, c, it)
            return
        if self.chart is None:
            return
        if isinstance(self.chart, TrendChart):
            self.chart.set_data(
                [{"d": r["k"], "total": int(round(r["v"])),
                  "ok": int(round(r["v"])), "fail": 0, "cancel": 0,
                  "v": r["v"], "n": r["n"]} for r in rows][-60:])
        else:
            self.chart.set_data(
                [{"account": r["k"], "total": int(round(r["v"])),
                  "ok": 0, "v": r["v"], "n": r["n"]} for r in rows[:12]])


class CustomViewDialog(QDialog):
    """组件属性向导：展示形态 × 维度 × 指标＋组件标题（新建/编辑共用）。

    视图本身不再是单张图（那是一页自由画布），这里配的是画布上的一个
    组件，只负形态/维度/指标/标题，几何位置由拖拽管理。趋势柱的时间轴
    只能是「按天」，选它时把维度锁死——不锁就会配出「没有时间的趋势图」
    这种自相矛盾的东西。无边框圆角外壳同其他弹窗。"""

    def __init__(self, parent=None, wspec=None):
        super().__init__(parent)
        self._w = dict(wspec or {})
        editing = bool(self._w.get("wid"))
        self.setWindowTitle("组件属性" if editing else "添加组件")
        self.setFixedSize(520, 400)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(10)
        title = QLabel("✎ 组件属性" if editing else "＋ 添加组件")
        title.setObjectName("DialogTitle")
        lay.addWidget(title)
        hint = QLabel("组件跟随页头「时间范围」刷新；排行/趋势可点柱下钻明细，"
                      "表格形态只看不下钻。位置与大小：拖标题栏 / 拖右下角。")
        hint.setStyleSheet(tokenize("font-size:11px;color:#8F959E;background:transparent;"))
        hint.setWordWrap(True)
        lay.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(12)
        self.e_title = QLineEdit()
        self.e_title.setPlaceholderText("留空自动按「维度+指标」命名")
        self.cb_show = QComboBox()
        for key, lab in _SHOWS:
            self.cb_show.addItem(lab, key)
        self.cb_dim = QComboBox()
        for key, lab in _DIMS:
            self.cb_dim.addItem(lab, key)
        self.cb_metric = QComboBox()
        for key, (lab, _k) in _METRICS.items():
            self.cb_metric.addItem(lab, key)
        form.addRow("组件标题", self.e_title)
        form.addRow("展示形态", self.cb_show)
        form.addRow("维度（分组）", self.cb_dim)
        form.addRow("指标（数值）", self.cb_metric)
        lay.addLayout(form)

        btns = QHBoxLayout()
        btns.addStretch(1)
        b_ok = QPushButton("保存" if editing else "添加到画布")
        b_ok.clicked.connect(self._ok)
        b_no = QPushButton("取消")
        b_no.setObjectName("GhostBtn")
        b_no.clicked.connect(self.reject)
        btns.addWidget(b_ok)
        btns.addWidget(b_no)
        lay.addLayout(btns)

        self.result = None
        # 回填（编辑态）：先设形态再设维度，锁联动按形态走
        if editing:
            self.e_title.setText(self._w.get("title", ""))
            for cb, val in ((self.cb_show, self._w.get("show")),
                            (self.cb_dim, self._w.get("dim")),
                            (self.cb_metric, self._w.get("metric"))):
                i = cb.findData(val)
                if i >= 0:
                    cb.setCurrentIndex(i)
        self.cb_show.currentIndexChanged.connect(self._sync_dim)
        self._sync_dim()
        apply_rounded(self, show_min=False, show_max=False)

    def _sync_dim(self):
        trend = self.cb_show.currentData() == "trend"
        self.cb_dim.setEnabled(not trend)
        if trend:
            i = self.cb_dim.findData("day")
            if i >= 0:
                self.cb_dim.setCurrentIndex(i)

    def _ok(self):
        show = self.cb_show.currentData()
        dim = "day" if show == "trend" else self.cb_dim.currentData()
        metric = self.cb_metric.currentData()
        self.result = {"title": self.e_title.text().strip() or
                       f"{_DIM_CN[dim]}{_METRICS[metric][0]}",
                       "show": show, "dim": dim, "metric": metric}
        self.accept()

    def keyPressEvent(self, e):
        # 同 DetailDialog：无边框后 Esc 要自己接
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)
