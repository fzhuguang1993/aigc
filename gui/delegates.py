"""
gui/delegates.py —— 表格自绘控件
ProgressDelegate：running 状态绘制圆角图形进度条，条上标注实时百分比
"""
from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QColor, QPainter, QFont, QPen
from PySide6.QtWidgets import QStyledItemDelegate

# 自定义数据角色：填充行数据时用 item.setData(ProgressRole, 0-100) 写入
ProgressRole = Qt.ItemDataRole.UserRole + 11


class ProgressDelegate(QStyledItemDelegate):
    BAR_H = 16

    def paint(self, painter, option, index):
        pct = index.data(ProgressRole)
        # 非进度条单元格：走默认绘制
        if pct is None:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(6, 3, -6, -3)
        pct = max(0, min(int(pct), 100))

        # 底槽
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#EFF0F1"))
        painter.drawRoundedRect(QRectF(rect), 4, 4)
        # 填充
        fill_w = rect.width() * pct / 100.0
        if fill_w > 0:
            painter.setBrush(QColor("#3370FF"))
            painter.drawRoundedRect(QRectF(rect.x(), rect.y(),
                                            max(fill_w, 8), rect.height()), 4, 4)
        # 条上标注百分比
        f = QFont(option.font)
        f.setPointSizeF(max(f.pointSizeF() - 1, 7.5))
        f.setBold(True)
        painter.setFont(f)
        painter.setPen(QPen(QColor("#FFFFFF") if pct >= 35 else QColor("#1F2329")))
        painter.drawText(QRectF(rect), Qt.AlignmentFlag.AlignCenter, f"{pct}%")
        painter.restore()

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QSize(s.width(), max(s.height(), self.BAR_H + 6))
