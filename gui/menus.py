"""
gui/menus.py —— 全站统一的右键 / 下拉菜单模板（StyledMenu）

主题里给 QMenu 写了 border-radius，但 Windows 的菜单是独立顶层弹窗：圆角之外那一圈
仍被原生窗底 + 系统投影填成方角——这就是"做了圆角、最外延容器还是有个直角"的根因。
把修角动作收敛到这一个模板，全站所有右键菜单 / setMenu 下拉 / 子菜单都走 StyledMenu：

① 真圆角：置 FramelessWindowHint + NoDropShadowWindowHint + WA_TranslucentBackground，
   圆角外面真正变成透明，不再漏方底（属性须在窗口句柄建好前的 __init__ 里设）；
② 补投影：自绘一圈软阴影填进保留的 padding 带里，补回被关掉的系统投影，不显"平"；
③ 容器背景交给自绘，item 的悬停配色仍由 theme.py 的 app 级 QSS（QMenu::item）负责。

用法：把 gui 里所有 QMenu(...) 换成 StyledMenu(...) 即可（含子菜单），
构造参数与 QMenu 一致——可传 (parent) 或 (title, parent)。
"""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QMenu, QWidget

from gui.ui_kit import COLORS

RADIUS = 10                     # 卡片圆角半径（与 ui_kit.RADIUS["lg"] 对齐）
MARGIN = 12                     # 四周留白：既是 items 的 padding，也是自绘软阴影的活动带
_SHADOW_STEPS = 6               # 阴影分层数（越多越细腻）


class StyledMenu(QMenu):
    """带真圆角 + 自绘软阴影的菜单；行为与 QMenu 完全一致，只是换了外壳。"""

    def __init__(self, *args):
        title, parent = "", None
        for a in args:
            if isinstance(a, QWidget):
                parent = a
            else:
                title = str(a)
        super().__init__(title, parent)
        # ① 去原生框 + 系统投影，逐像素透明：圆角外不再被方底填满
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # ② 容器背景透明、用 padding 腾出阴影带（items 随内容区一起内缩，不会盖住圆角）
        self.setStyleSheet(
            "QMenu { background: transparent; border: none;"
            f" padding: {MARGIN}px; }}")

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(self.rect()).adjusted(MARGIN, MARGIN, -MARGIN, -MARGIN)
        # 软阴影：从卡片边缘向外一圈圈变淡（画在 padding 带内，不会被窗口边界裁掉）
        # 阴影取正文色（text）的低位透明度，与全站墨色调校一致
        sc = QColor(COLORS["text"])
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(_SHADOW_STEPS, 0, -1):
            off = i * (MARGIN / _SHADOW_STEPS)
            alpha = 4 + int(16 * (1 - i / _SHADOW_STEPS))
            p.setBrush(QColor(sc.red(), sc.green(), sc.blue(), alpha))
            p.drawRoundedRect(card.adjusted(-off, -off, off, off),
                              RADIUS + off, RADIUS + off)
        # 卡片本体 + 1px 描边（白底上也勾得出轮廓）
        p.setBrush(QColor(COLORS["card"]))
        p.setPen(QPen(QColor(COLORS["border_popup"]), 1))
        p.drawRoundedRect(card, RADIUS, RADIUS)
        p.end()
        # 背景已透明，super 只会画各项（不会用方底覆盖上面的圆角卡片）
        super().paintEvent(e)
