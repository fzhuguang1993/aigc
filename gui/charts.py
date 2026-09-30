"""
gui/charts.py —— BI 风格自绘图表（无第三方依赖）
TrendChart   每日堆叠柱状图：渐变圆角 + 网格刻度 + 顶部数值（堆叠段可参数化，
             默认成功/失败/取消，换上 series/top_fmt 也能画排队/生成平均用时）
DonutChart   占比环形图：中心大数字 + 右侧图例（长标签自动省略号）
HBarChart    横向条形图：左标签 + 渐变条 + 右侧数值（行标签/数值文案可定制）
LineChart    当日逐时折线图（总执行/成功两条线）
ComboChart   每日执行量柱 + 成功率折线（右轴 0~100%）：量与质一起看
HeatmapChart 星期×小时执行密度热力图：产能分布一眼可见

各图都支持悬停：鼠标移到某一柱/某一段/某一行/某一时上，弹自绘的浅色
圆角提示卡（_ChartTip）给出那一份的具体数字（命中区在 paintEvent 里顺手记下）。
不再用原生 QToolTip：部分 Windows 环境它不吃全局 QSS/调色板，黑底看不清。

各图都支持点击下钻：单击某一柱/段/行时发 segment_clicked(维度, 目标)，
点空白处或悬停时按空格发 drill_all(维度)＝看该维度全部数据；
两类结果都由数据中台弹窗列明细（不再内嵌页底，避开滚动互踩与旧表残留空行）。
"""
import math

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QGuiApplication, QPainter,
                           QPen, QLinearGradient, QPainterPath, QBrush)
from PySide6.QtWidgets import QWidget

from gui.ui_kit import COLORS

# 飞书风色板：绿/红/灰语义分明，主蓝克制。凡 ui_kit 令牌里有同值语义色的一律
# 取令牌（改品牌色时图表跟着走）；浅渐变端/网格这类无对应令牌的保留 hex。
C_OK = QColor(COLORS["success"])      # #00B96B
C_OK2 = QColor("#5CD39B")
C_FAIL = QColor(COLORS["danger"])     # #F54A45
C_FAIL2 = QColor("#FF928D")
C_CANCEL = QColor(COLORS["weak"])     # #8F959E
C_CANCEL2 = QColor("#B8BEC7")
C_GRID = QColor("#E8EAED")
C_AXIS = QColor(COLORS["weak"])       # #8F959E
C_DARK = QColor(COLORS["text"])       # #1F2329
C_BLUE = QColor(COLORS["primary"])    # #3370FF 飞书主色
C_BLUE2 = QColor("#7FABFF")
# 多分类环形图色板（产品/时长这些非状态语义的维度用）：主色取令牌，浅色渐变端保留 hex
PALETTE = [(COLORS["primary"], "#7FABFF"), (COLORS["success"], "#5CD39B"),
           (COLORS["warning"], "#FFC57A"), (COLORS["purple"], "#B388FF"),
           (COLORS["info"], "#6FE0DA"), (COLORS["danger"], "#FF928D"),
           (COLORS["weak"], "#B8BEC7")]

FONT = "Microsoft YaHei"


def _nice_step(raw):
    for m in (1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000):
        if raw <= m:
            return m
    return 2000


class _ChartTip(QWidget):
    """图表悬停提示卡（单例）：自绘白底圆角 + 软阴影。

    为什么不用 QToolTip：部分 Windows 环境下原生 tooltip 不接受全局 QSS
    与 palette（黑底白字/几乎不可读）；自绘窗口走逐像素 alpha，
    圆角不带黑角，观感与系统样式彻底解耦。"""
    _inst = None

    @classmethod
    def inst(cls):
        if cls._inst is None:
            cls._inst = _ChartTip()
        return cls._inst

    def __init__(self):
        super().__init__(None, Qt.WindowType.ToolTip
                         | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        f = QFont(FONT)
        f.setPointSizeF(10)
        self.setFont(f)
        self._lines = []

    def show_tip(self, global_pos, text):
        self._lines = str(text).split("\n")
        fm = self.fontMetrics()
        w = max((fm.horizontalAdvance(t) for t in self._lines), default=40) + 28
        h = len(self._lines) * (fm.height() + 3) + 20
        self.resize(int(w), int(h))
        x, y = global_pos.x() + 14, global_pos.y() + 16
        scr = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
        if scr is not None:
            av = scr.availableGeometry()
            if x + w > av.right():
                x = global_pos.x() - w - 10
            if y + h > av.bottom():
                y = global_pos.y() - h - 14
            x, y = max(av.left() + 2, x), max(av.top() + 2, y)
        self.move(int(x), int(y))
        self.show()
        self.raise_()
        self.update()

    def hide_tip(self):
        self.hide()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        box = QRectF(self.rect()).adjusted(1, 1, -5, -5)      # 右下留阴影位
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(31, 35, 41, 26))
        p.drawRoundedRect(box.adjusted(2, 3, 2, 3), 9, 9)     # 软阴影
        p.setBrush(QColor(COLORS["card"]))
        p.setPen(QPen(QColor(COLORS["border"]), 1))
        p.drawRoundedRect(box, 8, 8)                          # 白卡本体
        p.setPen(QColor(COLORS["text"]))
        fm = p.fontMetrics()
        y = 10 + fm.height()
        for t in self._lines:
            p.drawText(QRectF(box.left() + 13, y - fm.height() - 1,
                              box.width() - 24, fm.height() + 4),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, t)
            y += fm.height() + 3
        p.end()


class _Base(QWidget):
    #: 点击下钻的维度键（day/status/product/dur/account/hour，由看板配）；
    #: 空串＝不响应点击
    drill_kind = ""
    #: (维度, 点击目标)：看板据此查明细并换表头，弹窗展示
    segment_clicked = Signal(str, object)
    #: 点图空白处 / 悬停按空格：不挑分段，看该图维度下的全部数据
    drill_all = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(240)
        self.setMouseTracking(True)     # 悬停查分段信息靠 mouseMove（不需按键）
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))   # 可点击下钻的暗示

    def _font(self, size, bold=False):
        f = QFont(FONT)
        f.setPointSizeF(size)
        f.setBold(bold)
        return f

    def _tip_at(self, pos, text):
        """在鼠标处弹自绘浅色提示卡；空文本＝收起"""
        tip = _ChartTip.inst()
        if text:
            tip.show_tip(self.mapToGlobal(pos), text)
        else:
            tip.hide_tip()

    def leaveEvent(self, e):
        _ChartTip.inst().hide_tip()
        super().leaveEvent(e)

    def _drill_key(self, pos):
        """点击点→下钻目标（子类按自己的命中区实现）；None＝点了空白"""
        return None

    def enterEvent(self, e):
        # 悬停即拿键盘焦点：用户要求“鼠标移到某个部分按空格”就命中本图
        #（程序 setFocus 不受 focusPolicy 限制，不必给图开 Tab 焦点）
        if self.drill_kind:
            self.setFocus(Qt.FocusReason.OtherFocusReason)
        super().enterEvent(e)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Space and self.drill_kind:
            _ChartTip.inst().hide_tip()
            self.drill_all.emit(self.drill_kind)
            e.accept()
            return
        super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if (e.button() == Qt.MouseButton.LeftButton and self.drill_kind):
            _ChartTip.inst().hide_tip()
            k = self._drill_key(e.position().toPoint())
            if k is None:
                # 点到空白：不挑分段，弹窗看该维度全部数据
                self.drill_all.emit(self.drill_kind)
            else:
                self.segment_clicked.emit(self.drill_kind, k)
            e.accept()
            return
        super().mousePressEvent(e)


class TrendChart(_Base):
    """每日堆叠柱状图：默认成功/失败/取消三段；换上 series/legend/top_field
    也能画别的堆叠口径（如“平均排队 + 平均生成”的用时结构）"""

    M_L, M_R, M_T, M_B = 44, 12, 30, 34   # 画布边距

    def __init__(self, title="执行趋势", parent=None):
        super().__init__(parent)
        self.title = title
        self.rows = []
        self._slots = []           # [(x0, x1, row)] 悬停命中区（paint 时重建）
        # 堆叠段：[(字段, 主色, 浅色)]，自下往上画；图例/顶部数值/悬停文案
        # 都可换，默认保持状态口径不变
        self.series = [("cancel", C_CANCEL, C_CANCEL2),
                       ("fail", C_FAIL, C_FAIL2),
                       ("ok", C_OK, C_OK2)]
        self.legend = [(C_OK, "成功"), (C_FAIL, "失败"), (C_CANCEL, "取消")]
        self.top_field = "total"        # 柱高与顶部数值的字段
        self.top_fmt = None             # 顶部数值格式化（None＝整数）
        self.col_fmt = None             # 悬停提示文案（None＝状态口径默认）

    def set_data(self, rows):
        self.rows = rows or []
        self.update()

    def col_text(self, r):
        """悬停一列时的提示正文（col_fmt 可换整套文案）"""
        if self.col_fmt:
            return self.col_fmt(r)
        d = (r["d"] or "")[5:].replace("-", "/")
        return (f"{d} 共 {r['total']} 条\n"
                f"成功 {r['ok']} · 失败 {r['fail']} · 取消 {r['cancel']}")

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        text = ""
        for x0, x1, r in self._slots:
            if x0 <= pos.x() < x1:
                text = self.col_text(r)
                break
        self._tip_at(pos, text)

    def _drill_key(self, pos):
        """点到哪一天→该日日期（下钻当天全部执行明细）"""
        for x0, x1, r in self._slots:
            if x0 <= pos.x() < x1:
                return r["d"]
        return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()

        p.setFont(self._font(10.5, True)); p.setPen(C_DARK)
        p.drawText(QRectF(16, 6, w - 32, 20), Qt.AlignmentFlag.AlignLeft, self.title)

        cx0, cy0 = self.M_L, self.M_T
        cw, ch = w - self.M_L - self.M_R, h - self.M_T - self.M_B
        max_v = max((int(r[self.top_field]) for r in self.rows), default=0)
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
        self._slots = []
        if n == 0:
            return
        slot = cw / n
        bw = min(slot * 0.52, 36)
        for i, r in enumerate(self.rows):
            self._slots.append((cx0 + slot * i, cx0 + slot * (i + 1), r))
            x = cx0 + slot * i + (slot - bw) / 2
            total = int(r[self.top_field])
            # 圆角柱整体 clip，再分段填色
            bar_h = ch * (total / top_v) if total else ch * 0.012
            y_top = cy0 + ch - bar_h
            radius = min(bw / 2, 6)
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, y_top, bw, bar_h), radius, radius)
            p.setClipPath(path)
            seg_y = cy0 + ch
            for key, c1, c2 in self.series:
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
                           Qt.AlignmentFlag.AlignCenter,
                           self.top_fmt(total) if self.top_fmt else str(total))
            # X 轴日期（标签过密时隔位显示）
            label = (r["d"] or "")[5:].replace("-", "/")
            if n <= 14 or i % 2 == 0:
                p.setFont(self._font(8.5)); p.setPen(C_AXIS)
                p.drawText(QRectF(x - 16, cy0 + ch + 4, bw + 32, 16),
                           Qt.AlignmentFlag.AlignCenter, label)

        # 图例（标题右侧）
        p.setFont(self._font(9))
        lx = w - 200
        for color, name in self.legend:
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
        self._hit = None     # (环心x, y, 直径d, 笔宽, [(起始角,跨度,段idx)])，paint 时重建
        self._legend = []    # [(y0, y1, 段idx)] 图例行也给悬停（比环好点中）

    def set_data(self, segs, center_text="", center_sub=""):
        self.segs = segs
        self.center_text = center_text
        self.center_sub = center_sub
        self.update()

    def seg_text(self, i):
        label, v, _c1, _c2 = self.segs[i]
        total = sum(s[1] for s in self.segs) or 1
        return f"{label}\n{v} 条 · 占比 {v / total * 100:.1f}%"

    def _seg_at(self, pos):
        """悬停/点击命中哪一段（图例行比环好点中，两者都认）；None＝空白"""
        for y0, y1, i in self._legend:
            if y0 <= pos.y() < y1 and pos.x() >= self.width() * 0.45:
                return i
        if self._hit:
            cx, cy, d, pen_w, arcs = self._hit
            dx, dy = pos.x() - cx, pos.y() - cy
            r = math.hypot(dx, dy)
            # 环带：笔宽盖住的半径圈（RoundCap 两头各余一点，命中区放宽）
            if d / 2 - pen_w * 1.2 <= r <= d / 2 + pen_w * 0.2:
                ang = (math.degrees(math.atan2(dx, -dy))) % 360   # 12点起顺时针
                for acc, size, i in arcs:
                    if acc <= ang < acc + size:
                        return i
        return None

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        i = self._seg_at(pos)
        self._tip_at(pos, self.seg_text(i) if i is not None else "")

    def _drill_key(self, pos):
        i = self._seg_at(pos)
        return self.segs[i][0] if i is not None else None

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
        self._hit = None
        arcs = []
        if total == 0:
            p.setPen(QPen(C_GRID, pen_w)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(box.adjusted(pen_w / 2, pen_w / 2, -pen_w / 2, -pen_w / 2))
        else:
            acc = 0.0
            for i, (_, v, c1, c2) in enumerate(self.segs):
                if v <= 0:
                    continue
                span = 360.0 * v / total
                arcs.append((acc, span, i))
                acc += span
                g = QLinearGradient(box.topLeft(), box.bottomRight())
                g.setColorAt(0, c2); g.setColorAt(1, c1)
                p.setPen(QPen(QBrush(g), pen_w, Qt.PenStyle.SolidLine,
                              Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                inner = box.adjusted(pen_w / 2, pen_w / 2, -pen_w / 2, -pen_w / 2)
                p.drawArc(inner, int(-start * 16), int(-span * 16))
                start += span
            self._hit = (box.center().x(), box.center().y(), d, pen_w, arcs)
        # 中心文字
        cx = box.center()
        p.setPen(C_DARK); p.setFont(self._font(d * 0.17, True))
        p.drawText(QRectF(cx.x() - d / 2, cx.y() - d * 0.12, d, d * 0.24),
                   Qt.AlignmentFlag.AlignCenter, self.center_text)
        p.setPen(C_AXIS); p.setFont(self._font(8.5))
        p.drawText(QRectF(cx.x() - d / 2, cx.y() + d * 0.10, d, 16),
                   Qt.AlignmentFlag.AlignCenter, self.center_sub)

        # 图例：长标签按剩余宽度截成省略号；段数多时从顶部往下排，
        # 不再用 “h/2 - n*11” 定位（段数一多起点算成负数，图例整排被裁掉）
        lx = box.right() + 26
        avail = max(70.0, w - lx - 20)
        p.setFont(self._font(9.5))
        fm = p.fontMetrics()
        ly = max(34.0, (h - len(self.segs) * 24) / 2 + 10)
        self._legend = []
        for i, (label, v, c1, _) in enumerate(self.segs):
            self._legend.append((ly - 12, ly + 12, i))
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(c1)
            p.drawRoundedRect(QRectF(lx, ly - 5, 10, 10), 3, 3)
            p.setPen(C_DARK)
            pct = f"{v / total * 100:.0f}%" if total else "0%"
            text = f"{label}  {v}  ({pct})"
            text = fm.elidedText(text, Qt.TextElideMode.ElideRight, int(avail) - 16)
            p.drawText(QRectF(lx + 16, ly - 10, avail - 16, 20),
                       Qt.AlignmentFlag.AlignVCenter, text)
            ly += 24


class HBarChart(_Base):
    """横向圆角条排行：默认线路口径（account/total/ok）；行标签、右侧数值、
    悬停文案都可换（失败原因 TOP 这类长标签图换 label_w + row_label + value_fn）"""

    def __init__(self, title="线路分布", parent=None):
        super().__init__(parent)
        self.title = title
        self.setMinimumHeight(180)
        self.rows = []     # [{"account", "total", "ok"}]（行标签字段可换见 row_label）
        self._band = []    # [(y0, y1, row)] 整行悬停命中区
        self.label_w = 80             # 左标签列宽（长文本图可调宽）
        self.row_label = None         # (row)->左标签；None = r["account"]
        self.value_fn = None          # (row)->右侧文案；None = “共X · 成Y”
        self.tip_fn = None            # (row)->悬停正文；None = 线路口径默认

    def set_data(self, rows):
        self.rows = rows or []
        self.update()

    def _label(self, r):
        if self.row_label:
            return self.row_label(r)
        return str(r["account"] or "-")

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        text = ""
        for y0, y1, r in self._band:
            if y0 <= pos.y() < y1:
                if self.tip_fn:
                    text = self.tip_fn(r)
                else:
                    ok = int(r["ok"] or 0)
                    text = (f"{r['account'] or '-'}\n"
                            f"共执行 {r['total']} 次 · 成功 {ok} 次")
                break
        self._tip_at(pos, text)

    def _drill_key(self, pos):
        """点到哪一行→行键（空线路给空串，明细查 account=''）"""
        for y0, y1, r in self._band:
            if y0 <= pos.y() < y1:
                return r["account"] or ""
        return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setFont(self._font(10.5, True)); p.setPen(C_DARK)
        p.drawText(QRectF(16, 6, w - 32, 20), Qt.AlignmentFlag.AlignLeft, self.title)
        if not self.rows:
            self._band = []
            p.setFont(self._font(9.5)); p.setPen(C_AXIS)
            p.drawText(QRectF(0, 30, w, h - 30), Qt.AlignmentFlag.AlignCenter,
                       "该时间段暂无执行数据")
            return
        max_v = max(int(r["total"]) for r in self.rows) or 1
        row_h = min(34, (h - 46) / len(self.rows))
        bar_area = w - self.label_w - 96
        self._band = []
        for i, r in enumerate(self.rows):
            y = 36 + i * row_h
            self._band.append((y - 6, y - 6 + row_h, r))
            bh = row_h - 12
            p.setFont(self._font(9.5)); p.setPen(C_DARK)
            p.drawText(QRectF(16, y - 2, self.label_w - 12, bh + 4),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       self._label(r))
            x0 = self.label_w + 14
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#F2F5FA"))
            p.drawRoundedRect(QRectF(x0, y, bar_area, bh), bh / 2, bh / 2)
            fw = max(bar_area * int(r["total"]) / max_v, bh)
            g = QLinearGradient(x0, 0, x0 + fw, 0)
            g.setColorAt(0, C_BLUE); g.setColorAt(1, C_BLUE2)
            p.setBrush(g)
            p.drawRoundedRect(QRectF(x0, y, fw, bh), bh / 2, bh / 2)
            p.setPen(C_DARK)
            val = (self.value_fn(r) if self.value_fn else
                   f"共{r['total']} · 成{int(r['ok'] or 0)}")
            p.drawText(QRectF(x0 + fw + 8, y - 2, 120, bh + 4),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       val)


class LineChart(_Base):
    """当日逐时折线：总执行/成功两条线（数据固定只看当日，不随时间范围变）"""

    M_L, M_R, M_T, M_B = 40, 14, 30, 30

    def __init__(self, title="今日执行节奏", parent=None):
        super().__init__(parent)
        self.title = title
        self.rows = []       # [{"h": "09", "total": 3, "ok": 2}, ...]
        self._slots = []     # [(x0, x1, row)]

    def set_data(self, rows):
        self.rows = rows or []
        self.update()

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        text = ""
        for x0, x1, r in self._slots:
            if x0 <= pos.x() < x1:
                text = (f"{int(r['h']):02d}:00 - {int(r['h']):02d}:59\n"
                        f"执行 {r['total']} 条 · 成功 {r['ok']} 条")
                break
        self._tip_at(pos, text)

    def _drill_key(self, pos):
        """点到哪个钟点→两位小时字符串（下钻今日该时段明细）"""
        for x0, x1, r in self._slots:
            if x0 <= pos.x() < x1:
                return r["h"]
        return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()
        p.setFont(self._font(10.5, True)); p.setPen(C_DARK)
        p.drawText(QRectF(16, 6, w - 32, 20), Qt.AlignmentFlag.AlignLeft, self.title)

        # 图例（标题右侧）：蓝＝总执行、绿＝成功
        p.setFont(self._font(9))
        lx = w - 160
        for color, name in [(C_BLUE, "总执行"), (C_OK, "成功")]:
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(color)
            p.drawRoundedRect(QRectF(lx, 9, 10, 10), 3, 3)
            p.setPen(C_AXIS)
            p.drawText(QRectF(lx + 14, 4, 56, 20), Qt.AlignmentFlag.AlignVCenter, name)
            lx += 70

        cx0, cy0 = self.M_L, self.M_T
        cw, ch = w - self.M_L - self.M_R, h - self.M_T - self.M_B
        n = len(self.rows)
        self._slots = []
        if n == 0:
            p.setFont(self._font(9.5)); p.setPen(C_AXIS)
            p.drawText(QRectF(0, 30, w, h - 30), Qt.AlignmentFlag.AlignCenter,
                       "今日暂无执行记录")
            return
        max_v = max(int(r["total"]) for r in self.rows) or 4
        step = _nice_step(max_v / 4)
        top_v = step * 4
        if top_v < max_v:
            top_v = step * 5

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

        slot = cw / n
        pts = lambda key: [QPointF(cx0 + slot * (i + 0.5),
                                   cy0 + ch - ch * int(r[key]) / top_v)
                           for i, r in enumerate(self.rows)]
        for key, col in (("total", C_BLUE), ("ok", C_OK)):
            pl = pts(key)
            p.setPen(QPen(col, 2))
            for a, b in zip(pl, pl[1:]):
                p.drawLine(a, b)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(col)
            for pt in pl:
                p.drawEllipse(pt, 2.6, 2.6)

        # X 轴小时：点数多时隔位显示，不然 24 个标签挤成一团
        p.setFont(self._font(8.5)); p.setPen(C_AXIS)
        for i, r in enumerate(self.rows):
            self._slots.append((cx0 + slot * i, cx0 + slot * (i + 1), r))
            if n <= 12 or i % 3 == 0:
                p.drawText(QRectF(cx0 + slot * i, cy0 + ch + 4, slot, 16),
                           Qt.AlignmentFlag.AlignCenter, f"{int(r['h']):02d}")


class ComboChart(_Base):
    """每日执行量（浅蓝柱）+ 成功率（紫线 · 右轴 0~100%）：
    量与质一张图看完——单看堆叠柱看不出“跑得多但质置在滑坡”"""

    M_L, M_R, M_T, M_B = 40, 44, 30, 34   # 右边距给成功率刻度
    C_RATE = QColor(COLORS["purple"])          # 与 KPI「成功率」同紫色

    def __init__(self, title="每日执行量与成功率", parent=None):
        super().__init__(parent)
        self.title = title
        self.rows = []       # [{"d", "total", "ok", "rate"}] rate 0~100
        self._slots = []

    def set_data(self, rows):
        self.rows = rows or []
        self.update()

    def _tip_text(self, r):
        d = (r["d"] or "")[5:].replace("-", "/")
        return (f"{d} 执行 {r['total']} 条 · 成功 {r['ok']} 条\n"
                f"成功率 {r['rate']:.0f}%")

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        text = ""
        for x0, x1, r in self._slots:
            if x0 <= pos.x() < x1:
                text = self._tip_text(r)
                break
        self._tip_at(pos, text)

    def _drill_key(self, pos):
        for x0, x1, r in self._slots:
            if x0 <= pos.x() < x1:
                return r["d"]
        return None

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()
        p.setFont(self._font(10.5, True)); p.setPen(C_DARK)
        p.drawText(QRectF(16, 6, w - 32, 20), Qt.AlignmentFlag.AlignLeft, self.title)

        # 图例（标题右侧）：浅蓝方块＝执行量，紫点线＝成功率
        p.setFont(self._font(9))
        lx = w - 210
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(C_BLUE)
        p.drawRoundedRect(QRectF(lx, 9, 10, 10), 3, 3)
        p.setPen(C_AXIS)
        p.drawText(QRectF(lx + 14, 4, 60, 20), Qt.AlignmentFlag.AlignVCenter, "执行量")
        lx += 74
        p.setPen(QPen(self.C_RATE, 2)); p.drawLine(QPointF(lx, 14), QPointF(lx + 16, 14))
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(self.C_RATE)
        p.drawEllipse(QPointF(lx + 8, 14), 2.6, 2.6)
        p.setPen(C_AXIS)
        p.drawText(QRectF(lx + 20, 4, 66, 20), Qt.AlignmentFlag.AlignVCenter, "成功率")

        cx0, cy0 = self.M_L, self.M_T
        cw, ch = w - self.M_L - self.M_R, h - self.M_T - self.M_B
        max_v = max((int(r["total"]) for r in self.rows), default=0) or 4
        step = _nice_step(max_v / 4)
        top_v = step * 4
        if top_v < max_v:
            top_v = step * 5

        # 网格 + 左轴（条数）+ 右轴（0~100%，固定五档）
        p.setFont(self._font(8.5))
        for i in range(5):
            y = cy0 + ch - ch * i / 4
            p.setPen(QPen(C_GRID, 1, Qt.PenStyle.SolidLine))
            p.drawLine(QPointF(cx0, y), QPointF(cx0 + cw, y))
            p.setPen(C_AXIS)
            p.drawText(QRectF(0, y - 9, cx0 - 8, 18),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       str(int(top_v * i / 4)))
            p.drawText(QRectF(cx0 + cw + 8, y - 9, self.M_R - 12, 18),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       f"{25 * i}%")

        n = len(self.rows)
        self._slots = []
        if n == 0:
            p.setFont(self._font(9.5)); p.setPen(C_AXIS)
            p.drawText(QRectF(0, 30, w, h - 30), Qt.AlignmentFlag.AlignCenter,
                       "该时间段暂无执行数据")
            return
        slot = cw / n
        bw = min(slot * 0.52, 36)
        pts = []
        for i, r in enumerate(self.rows):
            self._slots.append((cx0 + slot * i, cx0 + slot * (i + 1), r))
            x = cx0 + slot * i + (slot - bw) / 2
            total = int(r["total"])
            bar_h = ch * (total / top_v) if total else ch * 0.012
            g = QLinearGradient(0, cy0 + ch - bar_h, 0, cy0 + ch)
            g.setColorAt(0, C_BLUE2); g.setColorAt(1, C_BLUE)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(g)
            p.drawRoundedRect(QRectF(x, cy0 + ch - bar_h, bw, bar_h),
                              min(bw / 2, 6), min(bw / 2, 6))
            # 成功率只连有执行的天（0 执行日不画 0%，那不是质量那是没干活）
            if total:
                pts.append(QPointF(cx0 + slot * (i + 0.5),
                                   cy0 + ch - ch * min(float(r["rate"]), 100.0) / 100.0))
            label = (r["d"] or "")[5:].replace("-", "/")
            if n <= 14 or i % 2 == 0:
                p.setFont(self._font(8.5)); p.setPen(C_AXIS)
                p.drawText(QRectF(x - 16, cy0 + ch + 4, bw + 32, 16),
                           Qt.AlignmentFlag.AlignCenter, label)
        if len(pts) >= 2:
            p.setPen(QPen(self.C_RATE, 2))
            for a, b in zip(pts, pts[1:]):
                p.drawLine(a, b)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(self.C_RATE)
            for pt in pts:
                p.drawEllipse(pt, 2.6, 2.6)


class HeatmapChart(_Base):
    """星期×小时执行密度热力图：行＝周一…周日，列＝0…23 时，
    格色由白渐变到主蓝——产能习不习惯晚上/周末跑活，一眼看出来。
    点一格→那一档（本周几 × 几点）的执行明细（叠当前时间范围）。"""

    WD = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    M_L, M_T, M_B = 46, 40, 30          # 左留给星期名，顶留给时刻刻度

    def __init__(self, title="星期 × 小时 执行密度", parent=None):
        super().__init__(parent)
        self.title = title
        self.grid = [[0] * 24 for _ in range(7)]
        self._box = None     # (x0, y0, cw, ch) 色块区，paint 时重建

    def set_data(self, grid):
        self.grid = grid or [[0] * 24 for _ in range(7)]
        self.update()

    def _at(self, pos):
        """鼠标点→(星期 0=周一..6=周日, 小时)；None＝点了空白"""
        if not self._box:
            return None
        x0, y0, cw, ch = self._box
        if not (x0 <= pos.x() < x0 + cw and y0 <= pos.y() < y0 + ch):
            return None
        h = int((pos.x() - x0) / (cw / 24))
        wd = int((pos.y() - y0) / (ch / 7))
        return (min(wd, 6), min(h, 23))

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        cell = self._at(pos)
        text = ""
        if cell is not None:
            wd, h = cell
            text = (f"{self.WD[wd]} {h:02d}:00 ~ {h:02d}:59\n"
                    f"执行 {self.grid[wd][h]} 条")
        self._tip_at(pos, text)

    def _drill_key(self, pos):
        return self._at(pos)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setFont(self._font(10.5, True)); p.setPen(C_DARK)
        p.drawText(QRectF(16, 6, w - 32, 20), Qt.AlignmentFlag.AlignLeft, self.title)

        max_v = max((v for row in self.grid for v in row), default=0)
        x0, y0 = 16.0 + self.M_L, float(self.M_T)
        cw, ch = w - self.M_L - 30.0, h - self.M_T - self.M_B - 24.0
        self._box = (x0, y0, cw, ch)
        cell_w, cell_h = cw / 24, ch / 7
        fm_gap = 2.0                        # 格间距

        # 时刻刻度（顶）+ 星期名（左）
        p.setFont(self._font(8.5)); p.setPen(C_AXIS)
        for hh in range(24):
            if hh % 3 == 0:
                p.drawText(QRectF(x0 + cell_w * hh, y0 - 16, cell_w * 3, 14),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           f"{hh:02d}")
        for wd in range(7):
            p.setPen(C_AXIS)
            p.drawText(QRectF(6, y0 + cell_h * wd, self.M_L - 12, cell_h),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       self.WD[wd])

        # 色块：空格子浅底；有数由白→主蓝按 sqrt 提亮（线性渐变下小值全自，
        # 开平方后中低密度也能看出台阶）
        for wd in range(7):
            for hh in range(24):
                v = self.grid[wd][hh]
                rect = QRectF(x0 + cell_w * hh + fm_gap / 2,
                              y0 + cell_h * wd + fm_gap / 2,
                              max(cell_w - fm_gap, 1), max(cell_h - fm_gap, 1))
                if v <= 0 or max_v <= 0:
                    p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#F5F7FA"))
                else:
                    t = (v / max_v) ** 0.5 * 0.92 + 0.08
                    col = QColor(255 - int((255 - 51) * t),
                                 255 - int((255 - 112) * t),
                                 255 - int((255 - 255) * t))
                    p.setPen(Qt.PenStyle.NoPen); p.setBrush(col)
                p.drawRoundedRect(rect, 3, 3)

        # 右下小图例：浅→深渐变条 + 峰值标注
        lg_w, lg_h = 90.0, 8.0
        lx, ly = w - lg_w - 40, h - 16
        grad = QLinearGradient(lx, 0, lx + lg_w, 0)
        grad.setColorAt(0, QColor("#F5F7FA")); grad.setColorAt(1, C_BLUE)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(grad)
        p.drawRoundedRect(QRectF(lx, ly, lg_w, lg_h), 3, 3)
        p.setFont(self._font(8.5)); p.setPen(C_AXIS)
        p.drawText(QRectF(lx - 24, ly - 4, 24, 16),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "少")
        p.drawText(QRectF(lx + lg_w + 6, ly - 4, 40, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"多 {max_v}")
