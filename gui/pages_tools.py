"""
gui/pages_tools.py —— 工具中心
小工具以卡片形式上架：点击「✓ 可用」卡片弹出工具窗口（非模态，可同时开多个）。
新增工具两步：
  1. 在 gui/tool_panels.py 的 PANEL_FACTORIES 里登记 factory；
  2. 在下方 TOOLS 里加一条卡片信息（factory 传 None 显示为「规划中」）。
功能逻辑一律放 video_text_tools 包（纯功能、无 UI），本层只做界面。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QScrollArea, QSizePolicy, QDialog)

from gui.header import page_header
from gui.tool_panels import PANEL_FACTORIES

# 工具登记表：名称 / 图标 / 简介 / factory 名（None = 规划中）
TOOLS = [
    ("视频水印", "💧", "批量打静态/碰撞反弹水印，或统一格式化压制分辨率码率", "视频水印"),
    ("批量改名", "🏷", "数字/字母/罗马/希腊编号规则批量重命名，先预览再执行", "批量改名"),
    ("封面提取", "🖼", "查看视频参数（ffprobe），抽取指定帧生成封面图", "封面提取"),
    ("批量粘贴录入", "⌨", "剪贴板多行文本逐行自动粘贴（需辅助功能授权）", "批量粘贴录入"),
    ("SMB 上传", "📤", "成品视频批量上传到公司共享盘（需配置服务器账号）", "SMB 上传"),
    ("视频溯源", "🔎", "溯源码池取码、按规则重命名并入库（需内网 MySQL）", "视频溯源"),
    ("素材瘦身", "🗜", "把参考图批量压缩到接口要求的大小，避免上传失败", None),
    ("文案查重", "🔍", "提示词相似度检查，防止一批任务生成的视频互相雷同", None),
]


class ToolCard(QWidget):
    """单个工具卡片：可用时点击打开工具窗口"""

    def __init__(self, name, icon, desc, factory, on_open, parent=None):
        super().__init__(parent)
        self.setObjectName("ToolCard")
        self.tool_name = name
        self.factory = factory
        self._on_open = on_open
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
        state = QLabel("✓ 可用 · 点击打开" if factory else "🚧 规划中")
        state.setStyleSheet(
            "font-size:12px; color:#00A870; background:transparent;" if factory else
            "font-size:12px; color:#8F959E; background:transparent;")
        lay.addWidget(state)
        if factory:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if self.factory and e.button() == Qt.MouseButton.LeftButton \
                and self.rect().contains(e.position().toPoint()):
            self._on_open(self)
        super().mouseReleaseEvent(e)

    def enterEvent(self, e):
        if self.factory:
            self.setStyleSheet("#ToolCard { border:1px solid #3370FF; border-radius:12px; }")
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.setStyleSheet("")
        super().leaveEvent(e)


class ToolDialog(QDialog):
    """工具窗口：承载具体面板，非模态显示"""

    def __init__(self, name, factory, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"🧰 {name}")
        self.resize(880, 620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 8, 4, 4)
        self.panel = factory(self)
        lay.addWidget(self.panel)


class ToolsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._dialogs = {}          # name -> ToolDialog（保持引用防回收）
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(8)
        lay.addWidget(page_header("工具中心", "周边小工具集合 · 点击卡片打开 · 持续扩充", icon="🧰"))

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        holder = QWidget()
        grid = QHBoxLayout(holder)
        grid.setSpacing(14)
        grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        for name, icon, desc, fac_name in TOOLS:
            factory = PANEL_FACTORIES.get(fac_name) if fac_name else None
            card = ToolCard(name, icon, desc, factory, self._open)
            card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            grid.addWidget(card)
        grid.addStretch(1)
        area.setWidget(holder)
        lay.addWidget(area, 1)

        tip = QLabel("有想要的实用小工具？告诉维护人，排期上架～")
        tip.setObjectName("PageTip")
        lay.addWidget(tip)

    def _open(self, card):
        """点击卡片：已有窗口则前置，否则新建"""
        name = card.tool_name
        dlg = self._dialogs.get(name)
        if dlg is None or not dlg.isVisible():
            dlg = ToolDialog(name, card.factory, self)
            self._dialogs[name] = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def refresh(self):
        pass
