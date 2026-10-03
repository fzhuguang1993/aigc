"""
gui/pages_console.py —— 控制台：接口线路负载监控（现代仪表盘版）

设计口径：
- 顶部「概览条」用一张大卡片给出 4 个关键指标（线路 / 健康 / 本机在跑 / 容量空余），
  一眼看清全局；
- 每条线路一张自绘卡片：左侧竖向状态色条 + 呼吸灯 + 状态胶囊，主体用「并发槽位格」
  （一格 = 一个并发额度，按占用率上色）替代丑陋的进度条，最下方一排细粒度指标；
- 网格视图按窗口宽度自适应列数；「详细列表」是紧凑表格（每线路一行），
  与卡片视图共用同一份数据；两视图都**绝不显示接口地址**——地址即访问凭证，
  商业版不能从任何展示面泄露（旧版宽卡/悬停 tooltip 都露过 base）。
- 网关模式：另在下方挂一张服务端线路池只读表（选线/排队由网关完成）。
"""
from PySide6.QtCore import Qt, QPoint, QTimer, QPropertyAnimation, QEasingCurve, QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTableWidgetItem, QHeaderView,
                               QScrollArea, QGridLayout,
                               QSizePolicy, QGraphicsDropShadowEffect)

from gui.header import page_header, LightDot, LIGHT_KIND_COLOR, FS_WEAK, Card
from gui.theme import tokenize
from gui.kit import KitTable

SRV_HEADERS = ["线路", "状态", "并发上限", "负载(实时)"]
LIST_HEADERS = ["线路", "状态", "本机在跑", "并发上限", "空余", "连续失败", "云端负载"]
CARD_MIN_W = 280          # 网格视图卡片最小宽度（用于自适应列数）
GRID_GAP = 16             # 网格间距
# —— 商务配色（低饱和）：沉稳钴蓝 / 墨绿 / 赭石 / 干枯玫瑰红 + 石墨中性 ——
C_BLUE = "#3D6BB3"     # 强调 / 占用槽位
C_GREEN = "#2F9E77"    # 健康
C_AMBER = "#C88A2E"    # 排满 / 提醒
C_RED = "#C14B4B"      # 故障 / 连败（替代刺眼的高饱和红 #E5484D）
C_INK = "#1F2A37"      # 主文字
C_SLATE = "#5B6472"    # 次级 / 中性统计
C_EMPTY = "#E6E9EF"    # 空槽位
# 槽位格配色
_PIP_EMPTY = C_EMPTY
_PIP_USED = C_BLUE        # 正常占用：蓝
_PIP_FULL = C_AMBER       # 排满：赭石
_PIP_DOWN = C_RED         # 线路故障：玫瑰红
_KIND_LABEL = {"ok": "正常", "alive": "在线", "down": "故障", "pending": "待检测"}
# 列表视图状态列的圆点（与卡片胶囊同一套四档语义）
_KIND_EMOJI = {"ok": "🟢", "alive": "🟡", "down": "🔴", "pending": "⚪"}


class SlotGauge(QWidget):
    """并发槽位格：conc 个胶囊格子，前 run 格按占用状态上色。

    一格代表一个并发额度，比进度条更贴合“槽位满自动排队”的语义——能直接数出
    还剩几个空位，而不是靠条的长短去估。"""
    H = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(self.H)
        self._run, self._conc, self._kind = 0, 0, "pending"

    def set(self, run, conc, kind):
        vals = (run, conc, kind)
        if vals != (self._run, self._conc, self._kind):
            self._run, self._conc, self._kind = vals
            self.update()

    def paintEvent(self, e):
        n = max(self._conc, 0)
        if n <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        n = min(n, 24)                       # 超多并发时封顶，剩余靠右侧数字说明
        gap = 4
        w = (self.width() - gap * (n - 1)) / n
        if w < 4:                            # 挤不下就退化成一条整块占用条
            self._paint_bar(p)
            return
        if self._kind == "down":
            used = _PIP_DOWN
        elif self._run >= self._conc:
            used = _PIP_FULL
        else:
            used = _PIP_USED
        r = self.H / 2.0
        x = 0.0
        for i in range(n):
            p.setBrush(QColor(used if i < min(self._run, n) else _PIP_EMPTY))
            p.drawRoundedRect(QRectF(x, 0, w, self.H), r, r)
            x += w + gap
        p.end()

    def _paint_bar(self, p):
        h = self.H
        ratio = 0 if not self._conc else min(self._run / self._conc, 1.0)
        p.setBrush(QColor(_PIP_EMPTY))
        p.drawRoundedRect(QRectF(0, 0, self.width(), h), h / 2, h / 2)
        col = _PIP_DOWN if self._kind == "down" else (_PIP_FULL if ratio >= 1 else _PIP_USED)
        p.setBrush(QColor(col))
        p.drawRoundedRect(QRectF(0, 0, self.width() * ratio, h), h / 2, h / 2)


class ConsoleCard(QWidget):
    """一条线路的卡片（自绘背景/描边/左侧状态色条 + 悬停浮起）。

    持一个持久 LightDot，刷新只就地改数值/状态、绝不重建，否则 2 秒定时刷新
    会把呼吸灯动画相位清零（灯会卡在最亮那一下）。"""
    _REST = (16, QPoint(0, 2), QColor(30, 41, 59, 20))
    _HOVER = (26, QPoint(0, 7), QColor(61, 107, 179, 48))

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMinimumWidth(CARD_MIN_W - 24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._kind = "pending"
        self._hover = False

        # 悬停浮起阴影（QSS 无法补间，用属性动画做“抬起来”的动效）
        self._shadow = QGraphicsDropShadowEffect(self)
        b, off, col = self._REST
        self._shadow.setBlurRadius(b)
        self._shadow.setOffset(off)
        self._shadow.setColor(col)
        self.setGraphicsEffect(self._shadow)
        self._an_blur = QPropertyAnimation(self._shadow, b"blurRadius", self)
        self._an_off = QPropertyAnimation(self._shadow, b"offset", self)
        for a in (self._an_blur, self._an_off):
            a.setDuration(170)
            a.setEasingCurve(QEasingCurve.Type.OutCubic)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 15, 18, 15)
        outer.setSpacing(11)

        # 头：灯 + 名称 + 状态胶囊
        head = QHBoxLayout()
        head.setSpacing(9)
        self.dot = LightDot("pending")
        self.name = QLabel(name)
        self.name.setStyleSheet(tokenize("font-size:15px;font-weight:800;color:#1F2329;background:transparent;"))
        self.pill = QLabel("待检测")
        self.pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.name, 1)
        head.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(head)

        # 槽位格 + 计数
        bar_row = QHBoxLayout()
        bar_row.setSpacing(12)
        self.gauge = SlotGauge()
        self.count = QLabel("0 / 0")
        self.count.setStyleSheet(f"font-size:13px;font-weight:800;color:{C_BLUE};background:transparent;")
        bar_row.addWidget(self.gauge, 1)
        bar_row.addWidget(self.count, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(bar_row)

        # 细粒度指标一排
        m = QHBoxLayout()
        m.setSpacing(22)
        self.vals = {}
        for key, cap in (("local", "本机"), ("cloud", "云端"), ("free", "空余"), ("fail", "连败")):
            box, val = self._metric(cap)
            self.vals[key] = val
            m.addWidget(box)
        m.addStretch(1)
        outer.addLayout(m)

    @staticmethod
    def _metric(cap):
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(1)
        t = QLabel(cap)
        t.setStyleSheet(tokenize("font-size:11px;color:#8F959E;background:transparent;"))
        val = QLabel("-")
        val.setStyleSheet(tokenize("font-size:15px;font-weight:800;color:#1F2329;background:transparent;"))
        v.addWidget(t)
        v.addWidget(val)
        return box, val

    # ---------- 悬停浮起 ----------
    def enterEvent(self, e):
        self._hover = True
        self._animate(self._HOVER)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self._animate(self._REST)
        super().leaveEvent(e)

    def _animate(self, target):
        blur, off, col = target
        self._shadow.setColor(col)
        self._an_blur.stop(); self._an_blur.setStartValue(self._shadow.blurRadius()); self._an_blur.setEndValue(blur); self._an_blur.start()
        self._an_off.stop(); self._an_off.setStartValue(self._shadow.offset()); self._an_off.setEndValue(off); self._an_off.start()
        self.update()

    # ---------- 自绘背景 / 描边 / 左侧状态色条 ----------
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        border = QColor("#BCCCE6") if self._hover else QColor("#E7EAF0")
        p.setPen(QPen(border, 1))
        p.setBrush(QColor("#FFFFFF"))
        p.drawRoundedRect(rect, 16, 16)
        # 左侧竖向状态色条（上下内缩，避开圆角）
        col = QColor(LIGHT_KIND_COLOR.get(self._kind, FS_WEAK))
        rail = QRectF(rect.left() + 9, rect.top() + 16, 5, rect.height() - 32)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(rail, 2.5, 2.5)
        p.end()

    def _set_pill(self, kind, text):
        col = LIGHT_KIND_COLOR.get(kind, FS_WEAK)
        c = QColor(col)
        self.pill.setText(text)
        # ⚠ Qt 样式表只认 #AARRGGBB（透明度在前），写成 “{col}22” 会被当成
        # 暗橄榄色；用 rgba() 才能拿到柔和的半透明同色底
        self.pill.setStyleSheet(
            f"background:rgba({c.red()},{c.green()},{c.blue()},0.14); color:{col};"
            "border-radius:11px;padding:3px 11px;font-size:12px;font-weight:700;")

    def update_line(self, name, kind, run, conc, fail, cloud_txt, cloud_tip):
        """就地刷新；kind 是 header.light_kind 压出的四档键。
        ⚠ 不接收也不展示 base：接口地址是访问凭证，展示面一律不碰。"""
        self._kind = kind
        self.name.setText(name)
        self.dot.set_kind(kind)
        self._set_pill(kind, _KIND_LABEL.get(kind, "待检测"))
        self.gauge.set(run, conc, kind)
        free = max(conc - run, 0)
        self.count.setText(f"{run} / {conc}")
        # 计数颜色与槽位格同一口径：故障红 / 排满赭 / 正常蓝
        cnt_col = C_RED if kind == "down" else (C_AMBER if (conc and free <= 0) else C_BLUE)
        self.count.setStyleSheet(
            f"font-size:13px;font-weight:800;background:transparent;color:{cnt_col};")
        self._set_val(self.vals["local"], str(run), C_INK)
        self._set_val(self.vals["cloud"], cloud_txt, C_RED if "⚠" in cloud_txt else C_INK)
        self._set_val(self.vals["free"], str(free), C_AMBER if free == 0 else C_INK)
        self._set_val(self.vals["fail"], str(fail), C_RED if fail else C_INK)
        self.setToolTip(f"{name}\n本机进行中 {run} · 并发上限 {conc} · 空余 {free}\n{cloud_tip}")
        self.update()

    @staticmethod
    def _set_val(lbl, text, color):
        lbl.setText(text)
        lbl.setStyleSheet(f"font-size:15px;font-weight:800;color:{color};background:transparent;")


class _Stat(QWidget):
    """概览条里的一格指标：左浅色图标徽章 + 右（小标签 / 大数字）。"""
    def __init__(self, icon, cap, color=C_BLUE):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(20, 0, 20, 0)
        row.setSpacing(13)
        c = QColor(color)
        badge = QLabel(icon)
        badge.setFixedSize(44, 44)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(
            f"background:rgba({c.red()},{c.green()},{c.blue()},0.12);"
            "border-radius:12px;font-size:22px;")
        col = QVBoxLayout()
        col.setSpacing(2)
        self.value = QLabel("0")
        self.value.setStyleSheet(tokenize("font-size:24px;font-weight:800;color:#1F2329;background:transparent;"))
        cap_lbl = QLabel(cap)
        cap_lbl.setStyleSheet(f"font-size:12px;color:{FS_WEAK};background:transparent;")
        col.addWidget(self.value)
        col.addWidget(cap_lbl)
        row.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(col)
        row.addStretch(1)

    def set(self, text, color="#1F2329"):
        self.value.setText(str(text))
        self.value.setStyleSheet(f"font-size:24px;font-weight:800;color:{color};background:transparent;")


class ConsolePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 14, 24, 16)
        lay.setSpacing(14)

        top = QHBoxLayout()
        top.addWidget(page_header("线路负载", "槽位满自动排队 · 连败 3 次自动切换", icon="📡"), 1)
        self._b_view = QPushButton("▦ 卡片网格")
        self._b_view.setObjectName("GhostBtn")
        self._b_view.clicked.connect(self._toggle_view)
        top.addWidget(self._b_view)
        b_cloud = QPushButton("🔄 查询云端负载")
        b_cloud.setObjectName("GhostBtn")
        b_cloud.setToolTip("向每条线路查询云端进行中任务数（结果缓存约 5 秒）；"
                           "接口不可用时降级为仅本机计数，并标⚠提醒")
        b_cloud.clicked.connect(self._fetch_cloud)
        top.addWidget(b_cloud)
        lay.addLayout(top)

        # ---------- 概览条 ----------
        strip = QWidget()
        strip.setObjectName("SummaryCard")
        srow = QHBoxLayout(strip)
        srow.setContentsMargins(0, 16, 0, 16)
        srow.setSpacing(0)
        self.st_total = _Stat("🔌", "线路总数", C_SLATE)
        self.st_healthy = _Stat("🟢", "健康线路", C_GREEN)
        self.st_run = _Stat("▶", "本机在跑", C_BLUE)
        self.st_free = _Stat("🈳", "空余槽位", C_SLATE)
        for i, st in enumerate((self.st_total, self.st_healthy, self.st_run, self.st_free)):
            if i:
                div = QWidget()
                div.setFixedWidth(1)
                div.setStyleSheet("background:#EDEFF2;")
                srow.addWidget(div)
            srow.addWidget(st, 1)
        lay.addWidget(strip)

        # ---------- 卡片网格 ----------
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        self.grid_host = QWidget()
        self.grid_host.setStyleSheet("background:transparent;")
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(2, 4, 2, 4)
        self.grid.setSpacing(GRID_GAP)
        self.scroll.setWidget(self.grid_host)
        lay.addWidget(self.scroll, 1)
        self._cards = {}          # name -> ConsoleCard（持久，不随刷新重建）
        self._names = None
        self._last_cols = None

        # ---------- 详细列表（紧凑表格，每线路一行） ----------
        # 旧版「列表」只是单列宽卡换着法子排，不是列表；这里是真的表格。
        # 列里没有接口地址（商业版红线），状态用四档圆点+文字+语义色。
        self.list_table = KitTable(0, len(LIST_HEADERS), LIST_HEADERS, checkbox=False, select=None)
        self.list_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lsh = self.list_table.horizontalHeader()
        for c, w in {0: 170, 1: 110, 2: 90, 3: 90, 4: 70, 5: 90}.items():
            lsh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            self.list_table.setColumnWidth(c, w)
        lsh.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        # 详细列表包进白卡（与卡片网格视图同一视觉）；显隐由 list_card 统一控制
        self.list_card = Card(margins=(12, 10, 12, 10))
        self.list_card.v.addWidget(self.list_table)
        self.list_card.setVisible(False)
        lay.addWidget(self.list_card, 1)

        from store import app_state
        v = app_state.get("console_view", "grid")
        self._view = "list" if v in ("list", "block") else "grid"
        # block（旧值）= 多列，映射到 grid

        # ---------- 网关模式：服务端线路池（只读） ----------
        from core.config import GATEWAY_MODE
        self._gateway = GATEWAY_MODE
        self.srv_table = None
        if GATEWAY_MODE:
            srv_card = Card(margins=(16, 14, 16, 12))
            srv_card.v.setSpacing(10)
            srv_card.v.addWidget(QLabel("云端线路池（由网关统一调度，仅展示）："))
            self.srv_table = KitTable(0, len(SRV_HEADERS), SRV_HEADERS, checkbox=False, select=None)
            sh = self.srv_table.horizontalHeader()
            for c, w in {0: 120, 2: 90, 3: 110}.items():
                sh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
                self.srv_table.setColumnWidth(c, w)
            sh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.srv_table.setMaximumHeight(220)
            srv_card.v.addWidget(self.srv_table)
            lay.addWidget(srv_card)

        self._cloud = {}   # name -> (负载, 来源) or None

    # ================= 视图切换 =================
    def _toggle_view(self):
        from store import app_state
        self._view = "grid" if self._view == "list" else "list"
        app_state.set_value("console_view", self._view)
        self._apply_view()

    def _target_cols(self):
        if self._view == "list":
            return 1
        avail = max(self.scroll.viewport().width(), CARD_MIN_W + GRID_GAP)
        return max(1, min(4, avail // (CARD_MIN_W + GRID_GAP)))

    def _apply_view(self):
        block = self._view == "grid"
        self._b_view.setText("☰ 详细列表" if block else "▦ 卡片网格")
        self._b_view.setToolTip("切成紧凑列表：每线路一行" if block else "切成多列自适应卡片")
        self.scroll.setVisible(block)
        self.list_card.setVisible(not block)
        self._last_cols = None
        self._regrid()

    def _regrid(self):
        cols = self._target_cols()
        self._last_cols = cols
        while self.grid.count():
            self.grid.takeAt(0)                 # 只移布局项，卡片仍由 self._cards 持有
        for idx, (_name, card) in enumerate(self._cards.items()):
            self.grid.addWidget(card, idx // cols, idx % cols)
        for c in range(cols):
            self.grid.setColumnStretch(c, 1)
        nrows = (len(self._cards) + cols - 1) // cols
        self.grid.setRowStretch(nrows, 1)        # 尾部吃掉多余竖向空间

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if (getattr(self, "_view", None) == "grid" and getattr(self, "_cards", None)
                and self._target_cols() != self._last_cols):
            self._regrid()

    def _rebuild(self, names):
        """账号集合变化时才重建卡片；未变则复用，保住 LightDot 呼吸相位。"""
        old = dict(self._cards)
        self._cards = {}
        for n in names:
            self._cards[n] = old.get(n) or ConsoleCard(n)
        for c in old.values():
            c.setParent(None)
        self._regrid()

    # ================= 刷新 =================
    def refresh(self):
        from registry.manager import ACCOUNTS, REG, is_active, first_check_done
        from gui.header import light_kind
        checked = first_check_done()      # 未跑过一轮探活前，healthy 只是默认值
        rows = REG.active()
        names = [a.name for a in ACCOUNTS]
        if names != self._names:
            self._names = names
            self._rebuild(names)
            self._apply_view()

        sum_run = total_conc = healthy_n = 0
        list_rows = []
        for acc in ACCOUNTS:
            run = sum(1 for t in rows if t["account"] == acc.name and is_active(t["status"]))
            sum_run += run
            total_conc += acc.concurrency
            if acc.healthy and checked:
                healthy_n += 1
            cloud = self._cloud.get(acc.name)
            if cloud is None:
                cloud_txt, cloud_tip = "-", "尚未查询云端负载（点右上「查询云端负载」）"
            else:
                load, src = cloud
                cloud_txt = str(load) if src == "cloud" else f"{load} ⚠"
                cloud_tip = ("含同事提交的任务" if src == "cloud" else
                             "云端负载接口不可用，此数只统计了本机，看不到同事占了多少")
            kind = light_kind(acc, checked)
            list_rows.append((acc.name, kind, run, acc.concurrency,
                              acc.fail_count, cloud_txt, cloud_tip))
            card = self._cards.get(acc.name)
            if card is not None:
                card.update_line(acc.name, kind, run, acc.concurrency,
                                 acc.fail_count, cloud_txt, cloud_tip)
        if self._view == "list":
            self._update_list_table(list_rows)

        free_total = max(total_conc - sum_run, 0)
        self.st_total.set(len(ACCOUNTS), C_INK)
        self.st_healthy.set(
            f"{healthy_n}/{len(ACCOUNTS)}",
            C_GREEN if (checked and healthy_n == len(ACCOUNTS) and ACCOUNTS)
            else (C_RED if checked and healthy_n == 0 else C_AMBER))
        self.st_run.set(sum_run, C_BLUE if sum_run else C_SLATE)
        self.st_free.set(f"{free_total}/{total_conc}", C_INK)
        if self._gateway:
            self._refresh_gateway_hint(free_total, sum_run)

    def _update_list_table(self, list_rows):
        """详细列表：线路一行，状态/空余/连败/云端降级都上色，一眼扫完"""
        t = self.list_table
        t.setRowCount(len(list_rows))
        for i, (name, kind, run, conc, fail, cloud_txt, cloud_tip) in enumerate(list_rows):
            free = max(conc - run, 0)
            vals = (name, f"{_KIND_EMOJI.get(kind, '⚪')} {_KIND_LABEL.get(kind, '待检测')}",
                    run, conc, free, fail, cloud_txt)
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c == 1:
                    it.setForeground(QColor(LIGHT_KIND_COLOR.get(kind, FS_WEAK)))
                elif c == 4 and free == 0:
                    it.setForeground(QColor(C_AMBER))      # 排满：赭石提醒
                elif c in (5, 6) and (fail or "⚠" in cloud_txt):
                    it.setForeground(QColor(C_RED))        # 连败 / 云端降级：玫瑰红
                if c == 6:
                    it.setToolTip(cloud_tip)
                t.setItem(i, c, it)

    def _refresh_gateway_hint(self, free_total, sum_run):
        # 概览条足矣，网关表在 _fetch_server_lines 里刷；此处不额外占位
        pass

    def _fetch_cloud(self):
        """手动拉取云端负载（含 HTTP）；接口不可达时返回降级值并标记来源"""
        from registry.manager import ACCOUNTS, measure_load, invalidate_load_cache
        invalidate_load_cache()
        for acc in ACCOUNTS:
            try:
                self._cloud[acc.name] = measure_load(acc)
            except Exception:
                self._cloud[acc.name] = None
        if self._gateway:
            self._fetch_server_lines()
        self.refresh()

    def _fetch_server_lines(self):
        """网关模式：拉服务端线路池（/api/v1/lines，鉴权头由 api_client 统一注入）"""
        from registry.manager import ACCOUNTS
        from core.api_client import gateway_lines
        from gui.header import line_light
        from types import SimpleNamespace
        try:
            items = (gateway_lines(ACCOUNTS[0].base) or {}).get("items", []) if ACCOUNTS else []
        except Exception:
            items = []
        self.srv_table.setRowCount(len(items))
        for i, it in enumerate(items):
            fake = SimpleNamespace(healthy=bool(it.get("healthy")),
                                   probe_state=it.get("probe_state"),
                                   fail_count=0)
            status_txt, status_color = line_light(fake, True)[1:]
            load = it.get("load", 0)
            if it.get("load_degraded"):
                load = f"{load} ⚠仅本机"
            vals = [it.get("name", ""), status_txt, it.get("concurrency", "-"), load]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c == 1:
                    item.setForeground(QColor(status_color))
                self.srv_table.setItem(i, c, item)
