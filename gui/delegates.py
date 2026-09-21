"""
gui/delegates.py —— 表格自绘控件
ProgressDelegate：running 状态绘制「渐变填充 + 往复流动光带」的动画进度条，
条上标注大字号加粗百分比；无进度条行时由页面调用 set_anim_enabled(False) 停表。
"""
import math

from PySide6.QtCore import Qt, QRectF, QSize, QTimer, QElapsedTimer
from PySide6.QtGui import QColor, QPainter, QFont, QPen, QLinearGradient
from PySide6.QtWidgets import QStyledItemDelegate, QAbstractItemView

# 自定义数据角色：填充行数据时用 item.setData(ProgressRole, 0-100) 写入
ProgressRole = Qt.ItemDataRole.UserRole + 11


class ProgressDelegate(QStyledItemDelegate):
    BAR_H = 18
    ANIM_INTERVAL = 60          # ms：光带刷新间隔

    def __init__(self, parent=None):
        super().__init__(parent)
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(self.ANIM_INTERVAL)
        self._timer.timeout.connect(self._repaint_view)
        self._anim_on = False
        self._clock.start()

    # ---------- 动画开关 ----------
    def set_anim_enabled(self, on):
        """页面刷新时告知是否还有进度条行在跑，没有就停掉重绘定时器"""
        on = bool(on)
        if on == self._anim_on:
            return
        self._anim_on = on
        if on:
            self._timer.start()
        else:
            self._timer.stop()

    def _repaint_view(self):
        view = self.parent()
        if isinstance(view, QAbstractItemView):
            viewport = view.viewport()
            if viewport is not None:
                viewport.update()

    # ---------- 绘制 ----------
    def paint(self, painter, option, index):
        pct = index.data(ProgressRole)
        # 非进度条单元格：走默认绘制
        if pct is None:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(6, 4, -6, -4)
        pct = max(0, min(int(pct), 100))

        # 底槽
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#EFF0F1"))
        painter.drawRoundedRect(QRectF(rect), 5, 5)

        # 渐变填充：主蓝 → 青蓝
        fill_w = rect.width() * pct / 100.0
        if fill_w > 0:
            grad = QLinearGradient(rect.left(), 0, rect.left() + max(fill_w, 1), 0)
            grad.setColorAt(0.0, QColor("#3F7CFF"))
            grad.setColorAt(1.0, QColor("#00C2FF"))
            painter.setBrush(grad)
            painter.drawRoundedRect(QRectF(rect.x(), rect.y(),
                                           max(fill_w, 10), rect.height()), 5, 5)

            # 流动光带：在填充区内往复扫过（裁剪到填充圆角矩形内）
            if self._anim_on and fill_w > 30:
                band_w = min(46.0, fill_w * 0.5)
                # 0 → 1 → 0 的往复相位，周期约 1.6s
                phase = 0.5 - 0.5 * math.cos(self._clock.elapsed() / 800.0 * math.pi)
                x0 = rect.x() + (fill_w - band_w) * phase
                band = QLinearGradient(x0, 0, x0 + band_w, 0)
                band.setColorAt(0.0, QColor(255, 255, 255, 0))
                band.setColorAt(0.5, QColor(255, 255, 255, 120))
                band.setColorAt(1.0, QColor(255, 255, 255, 0))
                path_clip = painter.clipPath()
                painter.setClipRect(QRectF(rect.x(), rect.y(),
                                           max(fill_w, 10), rect.height()))
                painter.setBrush(band)
                painter.drawRect(QRectF(x0, rect.y(), band_w, rect.height()))
                painter.setClipPath(path_clip)

        # 条上标注百分比：加大加粗
        f = QFont(option.font)
        f.setPointSizeF(max(f.pointSizeF(), 10.5) + 1.5)
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(QPen(QColor("#FFFFFF") if pct >= 40 else QColor("#1F2329")))
        painter.drawText(QRectF(rect), Qt.AlignmentFlag.AlignCenter, f"{pct}%")
        painter.restore()

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QSize(s.width(), max(s.height(), self.BAR_H + 8))
