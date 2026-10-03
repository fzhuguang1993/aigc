"""
gui/header.py —— 统一页头与 KPI 卡片（飞书风格配色）
飞书色板：主蓝 #3370FF / 绿 #00B96B / 红 #F54A45 / 橙 #FF8D19 / 紫 #7F3FBF
正文 #1F2329 / 辅文 #646A73 / 弱文 #8F959E
"""
import math

from PySide6.QtCore import Qt, QTimer, QPointF
from PySide6.QtGui import (QColor, QPainter, QPen, QPainterPath, QLinearGradient)
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                               QSizePolicy, QSpacerItem)

from gui.ui_kit import COLORS, rgba

# 页头/KPI 常用色：不再自写十六进制，一律引用 ui_kit 令牌（值不变，改令牌即全站跟着变）
FS_BLUE = COLORS["primary"]
FS_TEXT = COLORS["text"]
FS_SUB = COLORS["sub"]
FS_WEAK = COLORS["weak"]

# 线路状态语义色（商务低饱和版）：墨绿 / 赭石 / 干枯玫瑰红，
# 替代刺眼的高饱和红绿灯（#1FA45C/#F5A623/#E5484D 看着老气）。
FS_GREEN = COLORS["line_ok"]
FS_AMBER = COLORS["line_alive"]
FS_RED = COLORS["line_down"]

# 线路状态灯三档，不是“绿/红”两档：探活接口未实现时（服务有话回但路径不对，
# 典型是 GET {base}/health 返回 HTML 404）画红灯会把人吓去删线路，画绿灯又是
# 谎称“测过了”。语义与 registry.manager.classify_probe 一一对应。
LIGHT_PENDING = ("⚪", "⚪ 待检测", FS_WEAK)      # 首轮探活还没回来
LIGHT_OK = ("🟢", "🟢 正常", FS_GREEN)          # 探活接口正常应答
LIGHT_ALIVE = ("🟡", "🟡 在线（探活路径未实现）", FS_AMBER)
LIGHT_DOWN = ("🔴", "🔴 故障", FS_RED)        # 连不上 / 5xx

# 灯色与四档语义一一对应，供 LightDot 动画取色（与 emoji 三件套同源，不另写判定）
LIGHT_KIND_COLOR = {"pending": FS_WEAK, "ok": FS_GREEN,
                    "alive": FS_AMBER, "down": FS_RED}


def line_light(acc, checked=True):
    """一条线路的展示三件套：(小灯, 状态文字, 颜色)"""
    if not checked:
        return LIGHT_PENDING
    if not acc.healthy:
        return LIGHT_DOWN
    if getattr(acc, "probe_state", None) == "alive":
        return LIGHT_ALIVE
    return LIGHT_OK


def light_kind(acc, checked=True):
    """把 line_light 的四档压成动画用的 kind 键（pending/ok/alive/down），
    与 emoji 三件套同一判定，避免两套状态各说各话。"""
    emoji = line_light(acc, checked)[0]
    return {"⚪": "pending", "🟢": "ok", "🟡": "alive", "🔴": "down"}.get(emoji, "pending")


class LightDot(QWidget):
    """线路状态指示灯（动画版）：绿灯缓慢呼吸、黄灯略快闪缩、
    红灯常亮、灰灯常亮空心圈。

    为什么自绘而不用 emoji：emoji 是静态字符，无法呼吸/闪。这里用 QTimer
    推相位、QPainter 画圆点；方块视图每张卡片持有一个持久实例，刷新只调
    set_kind 不重建，免得 2 秒定时刷新把呼吸节奏打断。"""
    D = 12                          # 圆点基准直径

    def __init__(self, kind="pending", parent=None):
        super().__init__(parent)
        self.setFixedSize(self.D + 6, self.D + 6)
        self._kind = kind
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)       # 20fps 足够顺滑

    def set_kind(self, kind):
        if kind != self._kind:
            self._kind = kind
            if kind not in ("ok", "alive"):
                self._phase = 0.0
            self.update()

    def _tick(self):
        # 只有呼吸/闪缩需要推进相位；常亮/空心停在不透明态，省 CPU
        if self._kind == "ok":
            self._phase += 0.045            # 缓慢呼吸：约 2.3s 一个循环
        elif self._kind == "alive":
            self._phase += 0.16             # 略快闪缩：约 0.65s 一个循环
        else:
            return
        if self._phase > math.tau:
            self._phase -= math.tau
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(LIGHT_KIND_COLOR.get(self._kind, FS_WEAK))
        cx, cy = self.width() / 2.0, self.height() / 2.0
        if self._kind == "pending":
            pen = QPen(color)
            pen.setWidthF(1.4)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            r = self.D / 2.0 - 1
            p.drawEllipse(QPointF(cx, cy), r, r)
            p.end()
            return
        if self._kind == "down":
            alpha, r = 255, self.D / 2.0            # 常亮实心
        elif self._kind == "ok":
            t = (math.sin(self._phase) + 1) / 2.0   # 0~1
            alpha = int(120 + 135 * t)              # 缓慢呼吸：亮度起伏
            r = self.D / 2.0 * (0.82 + 0.18 * t)
        else:  # alive：略快闪缩，幅度更大
            t = (math.sin(self._phase) + 1) / 2.0
            alpha = int(80 + 175 * t)
            r = self.D / 2.0 * (0.55 + 0.45 * t)
        color.setAlpha(alpha)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.end()


class _AccentBar(QWidget):
    """标题左侧的飞书蓝渐变竖条"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(5, 22)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        from PySide6.QtGui import QLinearGradient
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, QColor(FS_BLUE))
        g.setColorAt(1, QColor("#7FABFF"))
        p.setBrush(g)
        p.drawRoundedRect(self.rect().adjusted(0, 2, -1, -2), 2.5, 2.5)


def page_header(title, subtitle="", icon=""):
    """紧凑单行页头：渐变竖条 + 标题，总高 40px。

    subtitle 形参保留但**不再绘制**：各主栏目左上角那行灰色提示小字看着臃肿，已统一去掉；
    所有 `page_header(标题, 描述, ...)` 的旧调用无需改动（描述被忽略）。"""
    w = QWidget()
    w.setFixedHeight(40)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    lay.addWidget(_AccentBar(), 0, Qt.AlignmentFlag.AlignVCenter)
    lbl = QLabel(f"{icon}  {title}" if icon else title)
    lbl.setStyleSheet(f"font-size:17px; font-weight:700; color:{FS_TEXT}; background:transparent;")
    lay.addWidget(lbl)
    lay.addStretch(1)
    return w


class Sparkline(QWidget):
    """迷你趋势线：只画一条折线 + 下方淡渐变填充，无横纵轴/刻度/网格——
    填 KpiCard 右侧那一大片空白，让人一眼看到这指标的近期走势而不占地方。"""

    def __init__(self, values=None, color=FS_BLUE, parent=None):
        super().__init__(parent)
        self.setFixedSize(96, 42)
        self._values = list(values or [])
        self._color = QColor(color)

    def set_values(self, values, color=None):
        self._values = list(values or [])
        if color:
            self._color = QColor(color)
        self.update()

    def paintEvent(self, e):
        n = len(self._values)
        if n < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad = 4.0
        mn, mx = min(self._values), max(self._values)
        rng = (mx - mn) or 1
        xs = [pad + (w - 2 * pad) * i / (n - 1) for i in range(n)]
        ys = [h - pad - (h - 2 * pad) * (v - mn) / rng for v in self._values]
        pts = [QPointF(x, y) for x, y in zip(xs, ys)]
        # 折线下方淡渐变填充（顶部有色、底部透明）
        area = QPainterPath()
        area.moveTo(pts[0].x(), h)
        for pt in pts:
            area.lineTo(pt)
        area.lineTo(pts[-1].x(), h)
        area.closeSubpath()
        fill = QColor(self._color)
        fill.setAlpha(48)
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0, fill)
        g.setColorAt(1, QColor(255, 255, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawPath(area)
        # 折线本体
        pen = QPen(self._color)
        pen.setWidthF(1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        for a, b in zip(pts, pts[1:]):
            p.drawLine(a, b)
        # 末点高亮，暗示“当前值”
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(pts[-1], 2.4, 2.4)
        p.end()


class Card(QWidget):
    """白色圆角卡片容器（可叠加 hover 描边）；新刻度：边距 18/16、行距 8，
    与数据中台的卡内呼吸感对齐（KpiCard 等自带 margins 的不受影响）"""
    def __init__(self, parent=None, margins=(18, 16, 18, 16)):
        super().__init__(parent)
        self.setObjectName("Card")
        # ⚠ PySide6 只对「裸 QWidget 实例」自动开 WA_StyledBackground；
        # Python 子类不开——QSS 里 QWidget#Card 的白底/描边会被整层跳过
        # （历史上 KpiCard 看似白底其实是滚动区视口的白底兜着）
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(*margins)
        self.v.setSpacing(8)

    def add(self, w, stretch=0):
        self.v.addWidget(w, stretch)
        return w


class KpiCard(Card):
    """飞书风格 KPI：左侧浅色图标块，右侧名称+深色大数字，底部环比"""
    def __init__(self, name, icon, color, parent=None):
        super().__init__(parent, margins=(14, 12, 14, 10))
        self.color = color
        soft = rgba(color, 0.12)
        head = QHBoxLayout()
        head.setSpacing(10)
        badge = QLabel(icon)
        badge.setFixedSize(36, 36)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(
            f"background:{soft}; border-radius:9px; font-size:18px; color:{color};"
            "padding:0; margin:0;")
        head.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        t = QVBoxLayout()
        t.setSpacing(1)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(
            f"font-size:12px; color:{FS_SUB}; background:transparent;")
        self.value = QLabel("0")
        self.value.setStyleSheet(
            f"font-size:26px; font-weight:800; color:{FS_TEXT}; background:transparent;")
        t.addWidget(name_lbl)
        t.addWidget(self.value)
        head.addLayout(t)
        # addStretch() 在 PySide6 返回 None（拿不到句柄，日后无法移除）；
        # 这里握住真正的 QSpacerItem，set_sparkline / hide_sparkline 才能把弹簧收放。
        self._spacer = self._make_spacer(head)
        self._head = head
        self._spark = None
        self.v.addLayout(head)
        self.delta = QLabel("")
        self.delta.setStyleSheet(f"font-size:11px; color:{FS_WEAK}; background:transparent;")
        self.v.addWidget(self.delta)

    def set_value(self, v, delta_text="", up=None):
        self.value.setText(str(v))
        self.delta.setText(delta_text)
        if up is None:
            self.delta.setStyleSheet(f"font-size:11px; color:{FS_WEAK}; background:transparent;")
        else:
            col = COLORS["success"] if up else COLORS["danger"]
            self.delta.setStyleSheet(f"font-size:11px; color:{col}; background:transparent;")

    def set_sparkline(self, values, color=None):
        """在卡片右侧挂一条迷你趋势线（不传就不占位，老页面外观不变）。
        传入后去掉中部撑开的弹簧，让折线水平铺满中间到右侧那一段，
        消除「数字与折线之间一大截空白」。color 默认取卡片主色。"""
        col = color or self.color
        if self._spark is None:
            if self._spacer is not None:
                self._head.removeItem(self._spacer)
                self._spacer = None
            self._spark = Sparkline(values, col, self)
            self._spark.setSizePolicy(QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Preferred)
            self._head.addWidget(self._spark)
        else:
            self._spark.set_values(values, col)
        self._spark.show()

    def _make_spacer(self, layout):
        """往 layout 尾部加一根横向弹簧并返回其实例（addStretch 不返对象，只能自建）。"""
        sp = QSpacerItem(0, 0, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        layout.addItem(sp)
        return sp

    def hide_sparkline(self):
        """移掉右侧趋势线、把中部弹簧补回原样。
        配合 set_sparkline 做「显示 / 隐藏折线」开关；点数不足 2 无法成线时也走这里，
        免得留下一块 96×42 的空白。没挂过折线的卡片调它是空操作。"""
        if self._spark is not None:
            self._head.removeWidget(self._spark)
            self._spark.setParent(None)
            self._spark.deleteLater()
            self._spark = None
        if self._spacer is None:
            self._spacer = self._make_spacer(self._head)

    def paintEvent(self, e):
        super().paintEvent(e)
        # 顶部 3px 彩条（避开左右圆角边）
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(self.color), 3))
        p.drawLine(11, 2, self.width() - 12, 2)
        p.end()


def kpi_row(specs, spacing=14, min_width=125):
    """一排统一口径的 KpiCard（对标数据中台顶部的指标带）。

    specs = [(名称, 图标, 颜色), ...]；返回 (QHBoxLayout, [KpiCard, ...])，
    调用方把 layout 塞进页面布局、拿 cards 去 set_value。抽出来是为了让
    任务中心等非看板页也能一眼摆出同款的「顶部概览」，不必每页手抄一遍。"""
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    cards = []
    for name, icon, color in specs:
        k = KpiCard(name, icon, color)
        k.setMinimumWidth(min_width)
        lay.addWidget(k)
        cards.append(k)
    return lay, cards
