"""
gui/charts.py —— BI 风格自绘图表（无第三方依赖）
TrendChart   每日堆叠柱状图：渐变圆角 + 网格刻度 + 顶部数值
DonutChart   状态占比环形图：中心大数字 + 右侧图例
HBarChart    线路分布横向条形图
"""
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import (QColor, QPainter, QFont, QPen, QLinearGradient,
                           QPainterPath, QBrush)
from PySide6.QtWidgets import QWidget

# 飞书风色板：绿/红/灰语义分明，主蓝克制
C_OK = QColor("#00B96B")
C_OK2 = QColor("#5CD39B")
C_FAIL = QColor("#F54A45")
C_FAIL2 = QColor("#FF928D")
C_CANCEL = QColor("#8F959E")
C_CANCEL2 = QColor("#B8BEC7")
C_GRID = QColor("#E8EAED")
C_AXIS = QColor("#8F959E")
C_DARK = QColor("#1F2329")
C_BLUE = QColor("#3370FF")     # 飞书主色
C_BLUE2 = QColor("#7FABFF")

FONT = "Microsoft YaHei"


def _nice_step(raw):
    for m in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000):
        if raw <= m:
            return m
    return 2000


class _Base(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(240)

    def _font(self, size, bold=False):
        f = QFont(FONT)
        f.setPointSizeF(size)
        f.setBold(bold)
        return f


class TrendChart(_Base):
    """每日执行量堆叠柱状图"""

    M_L, M_R, M_T, M_B = 44, 12, 30, 34   # 画布边距

    def __init__(self, title="执行趋势", parent=None):
        super().__init__(parent)
        self.title = title
        self.rows = []

    def set_data(self, rows):
        self.rows = rows or []
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()

        p.setFont(self._font(10.5, True)); p.setPen(C_DARK)
        p.drawText(QRectF(16, 6, w - 32, 20), Qt.AlignmentFlag.AlignLeft, self.title)

        cx0, cy0 = self.M_L, self.M_T
        cw, ch = w - self.M_L - self.M_R, h - self.M_T - self.M_B
        max_v = max((int(r["total"]) for r in self.rows), default=0)
        if max_v == 0:
            max_v = 4
        step = _nice_step(max_v / 4)
        top_v = step * 4
        if top_v < max_v:
            top_v = step * 5

        # 网格 + Y 轴刻度
        p.setFont(self._font(8.5))
        for i in range(5):
            v = top_v * i / 4
            y = cy0 + ch - ch * i / 4
            p.setPen(QPen(C_GRID, 1, Qt.PenStyle.SolidLine))
            p.drawLine(QPointF(cx0, y), QPointF(cx0 + cw, y))
            p.setPen(C_AXIS)
            p.drawText(QRectF(0, y - 9, cx0 - 8, 18),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       str(int(v)))

        n = len(self.rows)
        if n == 0:
            return
        slot = cw / n
        bw = min(slot * 0.52, 36)
        for i, r in enumerate(self.rows):
            x = cx0 + slot * i + (slot - bw) / 2
            total = int(r["total"])
            # 圆角柱整体 clip，再分段填色
            bar_h = ch * (total / top_v) if total else ch * 0.012
            y_top = cy0 + ch - bar_h
            radius = min(bw / 2, 6)
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, y_top, bw, bar_h), radius, radius)
            p.setClipPath(path)
            seg_y = cy0 + ch
            for key, c1, c2 in [("cancel", C_CANCEL, C_CANCEL2),
                                ("fail", C_FAIL, C_FAIL2),
                                ("ok", C_OK, C_OK2)]:
                cnt = int(r[key])
                if cnt <= 0:
                    continue
                sh = ch * cnt / top_v
                g = QLinearGradient(0, seg_y - sh, 0, seg_y)
                g.setColorAt(0, c2); g.setColorAt(1, c1)
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(g)
                p.drawRect(QRectF(x, seg_y - sh, bw, sh))
                seg_y -= sh
            if total == 0:
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#E6EBF3"))
                p.drawRect(QRectF(x, y_top, bw, bar_h))
            p.setClipping(False)

            # 顶部数值
            if total:
                p.setFont(self._font(8.5, True)); p.setPen(C_DARK)
                p.drawText(QRectF(x - 14, y_top - 16, bw + 28, 14),
                           Qt.AlignmentFlag.AlignCenter, str(total))
            # X 轴日期（标签过密时隔位显示）
            label = (r["d"] or "")[5:].replace("-", "/")
            if n <= 14 or i % 2 == 0:
                p.setFont(self._font(8.5)); p.setPen(C_AXIS)
                p.drawText(QRectF(x - 16, cy0 + ch + 4, bw + 32, 16),
                           Qt.AlignmentFlag.AlignCenter, label)

        # 图例（标题右侧）
        p.setFont(self._font(9))
        lx = w - 200
        for color, name in [(C_OK, "成功"), (C_FAIL, "失败"), (C_CANCEL, "取消")]:
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(color)
            p.drawRoundedRect(QRectF(lx, 9, 10, 10), 3, 3)
            p.setPen(C_AXIS)
            p.drawText(QRectF(lx + 14, 4, 46, 20), Qt.AlignmentFlag.AlignVCenter, name)
            lx += 60


class DonutChart(_Base):
    """状态占比环形图：中心显示成功率"""

    def __init__(self, title="状态分布", parent=None):
        super().__init__(parent)
        self.title = title
        self.segs = []       # [(label, value, QColor...2个渐变端)]
        self.center_text = ""
        self.center_sub = ""

    def set_data(self, segs, center_text="", center_sub=""):
        self.segs = segs
        self.center_text = center_text
        self.center_sub = center_sub
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setFont(self._font(10.5, True)); p.setPen(C_DARK)
        p.drawText(QRectF(16, 6, w - 32, 20), Qt.AlignmentFlag.AlignLeft, self.title)

        total = sum(v for _, v, _, _ in self.segs)
        d = min(w * 0.42, h - 66)
        box = QRectF(18, (h - d) / 2 + 8, d, d)
        pen_w = d * 0.22
        start = 90.0   # 12 点方向
        if total == 0:
            p.setPen(QPen(C_GRID, pen_w)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(box.adjusted(pen_w / 2, pen_w / 2, -pen_w / 2, -pen_w / 2))
        else:
            for _, v, c1, c2 in self.segs:
                if v <= 0:
                    continue
                span = 360.0 * v / total
                g = QLinearGradient(box.topLeft(), box.bottomRight())
                g.setColorAt(0, c2); g.setColorAt(1, c1)
                p.setPen(QPen(QBrush(g), pen_w, Qt.PenStyle.SolidLine,
                              Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                inner = box.adjusted(pen_w / 2, pen_w / 2, -pen_w / 2, -pen_w / 2)
                p.drawArc(inner, int(-start * 16), int(-span * 16))
                start += span
        # 中心文字
        cx = box.center()
        p.setPen(C_DARK); p.setFont(self._font(d * 0.17, True))
        p.drawText(QRectF(cx.x() - d / 2, cx.y() - d * 0.12, d, d * 0.24),
                   Qt.AlignmentFlag.AlignCenter, self.center_text)
        p.setPen(C_AXIS); p.setFont(self._font(8.5))
        p.drawText(QRectF(cx.x() - d / 2, cx.y() + d * 0.10, d, 16),
                   Qt.AlignmentFlag.AlignCenter, self.center_sub)

        # 图例
        lx = box.right() + 26
        ly = h / 2 - len(self.segs) * 11
        p.setFont(self._font(9.5))
        for label, v, c1, _ in self.segs:
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(c1)
            p.drawRoundedRect(QRectF(lx, ly - 5, 10, 10), 3, 3)
            p.setPen(C_DARK)
            pct = f"{v / total * 100:.0f}%" if total else "0%"
            p.drawText(QRectF(lx + 16, ly - 10, 140, 20),
                       Qt.AlignmentFlag.AlignVCenter, f"{label}  {v}  ({pct})")
            ly += 24


class HBarChart(_Base):
    """线路分布：横向圆角条"""

    LABEL_W = 80

    def __init__(self, title="线路负载分布", parent=None):
        super().__init__(parent)
        self.title = title
        self.setMinimumHeight(180)
        self.rows = []     # [{"name", "total", "ok"}]

    def set_data(self, rows):
        self.rows = rows or []
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setFont(self._font(10.5, True)); p.setPen(C_DARK)
        p.drawText(QRectF(16, 6, w - 32, 20), Qt.AlignmentFlag.AlignLeft, self.title)
        if not self.rows:
            p.setFont(self._font(9.5)); p.setPen(C_AXIS)
            p.drawText(QRectF(0, 30, w, h - 30), Qt.AlignmentFlag.AlignCenter,
                       "该时间段暂无执行数据")
            return
        max_v = max(int(r["total"]) for r in self.rows) or 1
        row_h = min(34, (h - 46) / len(self.rows))
        bar_area = w - self.LABEL_W - 80
        for i, r in enumerate(self.rows):
            y = 36 + i * row_h
            bh = row_h - 12
            p.setFont(self._font(9.5)); p.setPen(C_DARK)
            p.drawText(QRectF(16, y - 2, self.LABEL_W - 12, bh + 4),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       str(r["account"] or "-"))
            x0 = self.LABEL_W + 14
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#F2F5FA"))
            p.drawRoundedRect(QRectF(x0, y, bar_area, bh), bh / 2, bh / 2)
            fw = max(bar_area * int(r["total"]) / max_v, bh)
            g = QLinearGradient(x0, 0, x0 + fw, 0)
            g.setColorAt(0, C_BLUE); g.setColorAt(1, C_BLUE2)
            p.setBrush(g)
            p.drawRoundedRect(QRectF(x0, y, fw, bh), bh / 2, bh / 2)
            p.setPen(C_DARK)
            p.drawText(QRectF(x0 + fw + 8, y - 2, 96, bh + 4),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       f"共{r['total']} · 成{int(r['ok'] or 0)}")
