"""
gui/pages_tools.py —— 工具中心（预留）
后续的小工具统一在这里以卡片形式上架。
新增工具：在 TOOLS 里登记一条，并提供一个 factory(parent) -> QWidget 即可，
未实现的工具会显示为「规划中」。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QScrollArea, QSizePolicy)

from gui.header import page_header

# 工具登记表：名称 / 图标 / 简介 / factory（None = 规划中）
TOOLS = [
    ("素材瘦身", "🗜", "把参考图批量压缩到接口要求的大小，避免上传失败", None),
    ("文案查重", "🔍", "提示词相似度检查，防止一批任务生成的视频互相雷同", None),
    ("批量改名", "🏷", "输出视频按 品名+日期+编号 一键重命名，方便归档", None),
    ("封面提取", "🖼", "从成品视频自动抽第一帧/清晰度最高帧做封面图", None),
]


class ToolCard(QWidget):
    """单个工具占位卡片"""

    def __init__(self, name, icon, desc, factory, parent=None):
        super().__init__(parent)
        self.setObjectName("ToolCard")
        self.factory = factory
        self.setFixedSize(250, 150)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(6)
        h = QHBoxLayout()
        badge = QLabel(icon)
        badge.setFixedSize(34, 34)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet("background:#EAF1FF; border-radius:9px; font-size:17px;")
        t = QLabel(name)
        t.setStyleSheet("font-size:14px; font-weight:700; color:#1F2329; background:transparent;")
        h.addWidget(badge)
        h.addWidget(t)
        h.addStretch(1)
        lay.addLayout(h)
        d = QLabel(desc)
        d.setWordWrap(True)
        d.setStyleSheet("font-size:12px; color:#646A73; background:transparent;")
        lay.addWidget(d)
        lay.addStretch(1)
        state = QLabel("✓ 可用" if factory else "🚧 规划中")
        state.setStyleSheet(
            "font-size:12px; color:#00A870; background:transparent;" if factory else
            "font-size:12px; color:#8F959E; background:transparent;")
        lay.addWidget(state)


class ToolsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(8)
        lay.addWidget(page_header("工具中心", "周边小工具集合 · 持续扩充", icon="🧰"))

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        holder = QWidget()
        grid = QHBoxLayout(holder)
        grid.setSpacing(14)
        grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        for name, icon, desc, factory in TOOLS:
            card = ToolCard(name, icon, desc, factory)
            card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            grid.addWidget(card)
        grid.addStretch(1)
        area.setWidget(holder)
        lay.addWidget(area, 1)

        tip = QLabel("有想要的实用小工具？告诉维护人，排期上架～")
        tip.setObjectName("PageTip")
        lay.addWidget(tip)

    def refresh(self):
        pass
