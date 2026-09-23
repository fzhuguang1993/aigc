"""
gui/header.py —— 统一页头与 KPI 卡片（飞书风格配色）
飞书色板：主蓝 #3370FF / 绿 #00B96B / 红 #F54A45 / 橙 #FF8D19 / 紫 #7F3FBF
正文 #1F2329 / 辅文 #646A73 / 弱文 #8F959E
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel

FS_BLUE = "#3370FF"
FS_TEXT = "#1F2329"
FS_SUB = "#646A73"
FS_WEAK = "#8F959E"

# 线路状态灯三档，不是“绿/红”两档：探活接口未实现时（服务有话回但路径不对，
# 典型是 GET {base}/health 返回 HTML 404）画红灯会把人吓去删线路，画绿灯又是
# 谎称“测过了”。语义与 registry.manager.classify_probe 一一对应。
LIGHT_PENDING = ("⚪", "⚪ 待检测", FS_WEAK)      # 首轮探活还没回来
LIGHT_OK = ("🟢", "🟢 正常", "#1FA45C")          # 探活接口正常应答
LIGHT_ALIVE = ("🟡", "🟡 在线（探活路径未实现）", "#F5A623")
LIGHT_DOWN = ("🔴", "🔴 故障", "#E5484D")        # 连不上 / 5xx


def line_light(acc, checked=True):
    """一条线路的展示三件套：(小灯, 状态文字, 颜色)"""
    if not checked:
        return LIGHT_PENDING
    if not acc.healthy:
        return LIGHT_DOWN
    if getattr(acc, "probe_state", None) == "alive":
        return LIGHT_ALIVE
    return LIGHT_OK


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
    """紧凑单行页头：渐变竖条 + 标题 + 同行灰色副标题，总高 40px"""
    w = QWidget()
    w.setFixedHeight(40)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(10)
    lay.addWidget(_AccentBar(), 0, Qt.AlignmentFlag.AlignVCenter)
    lbl = QLabel(f"{icon}  {title}" if icon else title)
    lbl.setStyleSheet(f"font-size:17px; font-weight:700; color:{FS_TEXT}; background:transparent;")
    lay.addWidget(lbl)
    if subtitle:
        s = QLabel(subtitle)
        s.setStyleSheet(f"font-size:12px; color:{FS_WEAK}; background:transparent;")
        lay.addSpacing(4)
        lay.addWidget(s, 0, Qt.AlignmentFlag.AlignVCenter)
    lay.addStretch(1)
    return w


class Card(QWidget):
    """白色圆角卡片容器（可叠加 hover 描边）"""
    def __init__(self, parent=None, margins=(16, 14, 16, 14)):
        super().__init__(parent)
        self.setObjectName("Card")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(*margins)
        self.v.setSpacing(6)

    def add(self, w, stretch=0):
        self.v.addWidget(w, stretch)
        return w


class KpiCard(Card):
    """飞书风格 KPI：左侧浅色图标块，右侧名称+深色大数字，底部环比"""
    def __init__(self, name, icon, color, parent=None):
        super().__init__(parent, margins=(14, 12, 14, 10))
        self.color = color
        c = QColor(color)
        soft = f"rgba({c.red()},{c.green()},{c.blue()},0.12)"
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
        head.addStretch(1)
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
            col = "#00B96B" if up else "#F54A45"
            self.delta.setStyleSheet(f"font-size:11px; color:{col}; background:transparent;")

    def paintEvent(self, e):
        super().paintEvent(e)
        # 顶部 3px 彩条（避开左右圆角边）
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(self.color), 3))
        p.drawLine(11, 2, self.width() - 12, 2)
        p.end()
