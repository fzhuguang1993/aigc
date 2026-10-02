"""
gui/kit.py —— 全站共享 UI 组件底座（单一真源）

从「UI 组件库」画廊页 gui/pages_ui_kit.py 抽出的、可复用且无页面依赖的成品组件：
动效按钮 KitButton、交互表格 KitTable + 单元格委托、真圆角弹窗家族
（_draw_rounded_card / _RoundPopup / _DialogCardPainter / _round_dialog）、全站圆角
tooltip 气泡 TipBubble（_TipRouter 拦截原生 tooltip，单例只弹一个）、多级树下拉
TreeSelect、日期选择器族（ModernDatePicker / DateRangePicker / ModernDateEdit /
CompactCalendar）、浮动气泡滑块 KitSlider、可自由拉伸多行文本 ResizableTextEdit、
定宽 KPI 卡 _FlowKpiCard，以及标题/对比色/取色等小工具。画廊页与后续各业务页都从
这里复用，保证「同一套组件、同一口径」；颜色 / 字号 / 圆角一律吃 gui/ui_kit 令牌。
"""
import json
import re

from PySide6.QtCore import (Qt, QDate, QTimer, QPoint, QRect, QSize, QEvent,
                            QRectF, QPointF, Property, QPropertyAnimation,
                            QEasingCurve, QSortFilterProxyModel, QObject, Signal,
                            QMimeData)
from PySide6.QtGui import (QColor, QCursor, QGuiApplication, QPainter, QPen, QBrush,
                           QPainterPath, QFont, QPolygonF, QLinearGradient,
                           QStandardItemModel, QStandardItem, QKeySequence, QDrag,
                           QActionGroup)
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QLineEdit, QPlainTextEdit, QComboBox,
                               QSpinBox, QDoubleSpinBox, QDateEdit, QCheckBox,
                               QRadioButton, QTabWidget, QProgressBar, QSlider,
                               QTableWidget, QTableWidgetItem, QHeaderView,
                               QScrollArea, QFrame, QButtonGroup, QSizePolicy,
                               QMessageBox, QFormLayout, QSplitter, QCalendarWidget,
                               QTreeView, QAbstractItemView, QStyle, QMenu,
                               QStyleOptionSlider, QDialog, QApplication,
                               QTableWidgetSelectionRange, QStyledItemDelegate,
                               QGroupBox)

from gui import ui_kit
from gui.header import page_header, Card, KpiCard
from gui.widgets import FlowLayout
from gui.menus import StyledMenu

C = ui_kit.COLORS


# --------------------------------------------------------------------
# 小工具（纯展示用）
# --------------------------------------------------------------------
def _section_title(text):
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color:{C['text']}; font-size:15px; font-weight:700; background:transparent;")
    return lbl


def _sub_title(text):
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color:{C['primary']}; font-size:13px; font-weight:600; background:transparent;")
    # 允许折行：那些很长的说明标题（如“表格（列标题右键→…）”）若不换行，会把整个
    # 滚动内容区的 minimumWidth 顶到 ~1560，导致 FlowLayout（如 KPI 带）拿到的宽度永远
    # 比窗口还宽、怎么都不换行。开折行后内容宽度回落到视口宽，卡片才能按实际窗口宽度排。
    lbl.setWordWrap(True)
    return lbl


def _hint(text):
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color:{C['weak']}; font-size:12px; background:transparent;")
    lbl.setWordWrap(True)
    return lbl


def _contrast_text(hex_color):
    c = QColor(hex_color)
    lum = (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue())
    return C["text"] if lum > 150 else C["on_primary"]


def _row(*widgets, gap=10):
    h = QHBoxLayout()
    h.setSpacing(gap)
    for w in widgets:
        h.addWidget(w)
    h.addStretch(1)
    return h


def _mix(a, b, t):
    """两 QColor 按 t（0~1）线性插值。"""
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


def _lighten(hex_color, f):
    """把 hex 往白色方向提亮 f（0~1），给渐变胶囊取高光端色。"""
    return _mix(QColor(hex_color), QColor(255, 255, 255), f).name()


def _draw_rounded_card(pr, rect, radius, margin, fill=None, border=None):
    """在透明顶层上自绘一张圆角白卡 + 向外逐层变淡的软阴影（菜单/弹窗/通知共用）。
    margin 为四周留白（也是阴影活动带），内容布局需用同宽边距避开圆角。"""
    card = QRectF(rect).adjusted(margin, margin, -margin, -margin)
    sc = QColor(C["text"])
    pr.setPen(Qt.PenStyle.NoPen)
    steps = 6
    # 软阴影：整体调淡一档（原 8+28 太重，白底上一眼看出“发灰”），保留最内层一点点
    # 层次即可——描边已能勾出轮廓，阴影只负责把卡片从背景里轻轻托起来。
    for i in range(steps, 0, -1):
        off = i * (margin / steps)
        alpha = 3 + int(13 * (1 - i / steps))
        pr.setBrush(QColor(sc.red(), sc.green(), sc.blue(), alpha))
        pr.drawRoundedRect(card.adjusted(-off, -off, off, off),
                           radius + off, radius + off)
    pr.setBrush(QColor(fill or C["card"]))
    pr.setPen(QPen(QColor(border or C["border_popup"]), 1))
    pr.drawRoundedRect(card, radius, radius)


class _RoundPopup(QWidget):
    """真圆角顶层浮层基类：无边框 + 半透明背景，自绘一张圆角白卡 + 向外逐层变淡的
    软阴影。内容加进 self.content，四周阴影带(SHADOW)与卡片内边距(PAD)会自动避开圆角，
    子控件保持透明背景，让卡片底色透出——浮层四角才会是真圆弧（解决「弹出层圆角没有」）。"""
    SHADOW = 14
    PAD = 6

    def __init__(self, parent=None, radius=None):
        super().__init__(parent, Qt.WindowType.Popup
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.radius = radius if radius is not None else ui_kit.RADIUS["md"]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(self.SHADOW, self.SHADOW, self.SHADOW, self.SHADOW)
        self.content = QVBoxLayout()
        self.content.setContentsMargins(self.PAD, self.PAD, self.PAD, self.PAD)
        self.content.setSpacing(6)
        outer.addLayout(self.content)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        _draw_rounded_card(p, self.rect(), self.radius, self.SHADOW)
        p.end()


class TipBubble(_RoundPopup):
    """全站唯一的圆角 tooltip 气泡：接管所有 tooltip 的显示（_TipRouter 拦 QEvent.ToolTip）。

    为什么不修原生：QTipLabel 是独立的矩形顶层窗口，QSS 的 border-radius 只是在方窗里
    画了个圆角文本框（外直角内圆角）；而逐像素透明这类属性要赶在窗口创建前设才生效，
    对复用单例永远慢一拍——怎么调都是「直角套圆角」。这里直接换成自家圆角卡片体系：
    透明顶层 + 自绘白卡＝四角真圆角，自带一圈淡灰软阴影（_draw_rounded_card）。

    单例复用 → 屏幕上同一时刻永远只有一个 tooltip（修「第二个弹层盖掉第一个」）；
    ToolTip 窗口标志不抢焦点、不抓键盘，悬停即显、离开即隐。"""
    _INST = None
    MAX_W = 360

    @classmethod
    def instance(cls):
        if cls._INST is None:
            cls._INST = TipBubble()
        return cls._INST

    def __init__(self):
        super().__init__()
        # 基类默认 Popup（抓鼠标/键盘）；tooltip 要做到不打扰：换成 ToolTip 窗
        self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._lbl = QLabel()
        self._lbl.setTextFormat(Qt.TextFormat.PlainText)   # 贴的内容不当 HTML 解析
        self._lbl.setWordWrap(True)
        self._lbl.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._lbl.setStyleSheet(f"background:transparent;border:none;"
                                f"color:{C['text']};font-size:12px;")
        self.content.addWidget(self._lbl)
        self._src = None
        self._watch = QTimer(self)
        self._watch.setInterval(150)
        self._watch.timeout.connect(self._tick)

    def show_for(self, text, src=None):
        """在鼠标旁弹出：src 为发起 tooltip 的控件（用于判断「鼠标是否已离开」）。"""
        text = str(text or "").strip()
        if not text:
            self.hide()
            return
        self._src = src
        self._lbl.setText(text)
        # 宽度按内容单行宽封顶 MAX_W；高度按换行后的真实需求算
        w = min(self.MAX_W, max(60, self._lbl.sizeHint().width()))
        self._lbl.setFixedWidth(w)
        self._lbl.setFixedHeight(max(self._lbl.heightForWidth(w),
                                     self._lbl.sizeHint().height()))
        self.adjustSize()
        self.move(self._place(QCursor.pos() + QPoint(14, 18)))
        self.show()
        self._watch.start()

    def _place(self, pos):
        """默认跟鼠标右下；贴屏幕边时夹回可视区。"""
        scr = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        av = scr.availableGeometry()
        x = min(pos.x(), av.right() - self.width() - 4)
        y = min(pos.y(), av.bottom() - self.height() - 4)
        return QPoint(max(av.left() + 4, x), max(av.top() + 4, y))

    def _tick(self):
        """保活判定：鼠标还在气泡里或还在来源控件上→继续挂，否则收起。"""
        pos = QCursor.pos()
        if self.geometry().adjusted(-6, -6, 6, 6).contains(pos):
            return
        w = self._src
        if w is not None and w.isVisible() and \
                QRect(w.mapToGlobal(QPoint(0, 0)), w.size()).contains(pos):
            return
        self.hide()

    def hide(self):
        self._watch.stop()
        super().hide()


class _TipRouter(QObject):
    """全站原生 tooltip 拦截器：把即将弹出的 ToolTip 事件截下来、自己解析文案转投
    单例 TipBubble，并吃掉事件（return True）——原生 QTipLabel 方窗从此没机会露面，
    调用点 setToolTip 一行不改自动获得圆角软阴影；也只弹一颗窗口。

    为什么不能直接读事件里的文案：Qt 发出的 QTipEvent 在 PySide6 没有绑定类
    （到 Python 侧是光秃秃的 QHelpEvent，没有 text()），只能按 Qt 自己的取值链解析：
    条目视图→鼠标下指标的 ToolTipRole；菜单→鼠标下动作的 toolTip；其它→控件自身
    toolTip()。全站 tooltip 文案均来自这三条 setToolTip 途径（无自定义 ToolTip 处理），
    解析不到就静默隐去（与原生「空文案不弹」一致）。
    只拦 ToolTip：StatusTip（状态栏提示另一条链）不属 tooltip 弹层，不碰。"""

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Type.ToolTip:
            text = self._resolve_text(obj) if isinstance(obj, QWidget) else ""
            if text:
                TipBubble.instance().show_for(text, obj)
            else:
                TipBubble.instance().hide()
            return True                       # 吃掉：原生弹层不再露面
        if t in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel):
            TipBubble.instance().hide()       # 按下/滚动即隐（与原生 tooltip 行为一致）
        return False

    def _resolve_text(self, obj):
        if isinstance(obj, QMenu):
            a = obj.actionAt(obj.mapFromGlobal(QCursor.pos()))
            if a is not None and a.toolTip():
                return a.toolTip()
        if isinstance(obj, QAbstractItemView):
            idx = obj.indexAt(obj.viewport().mapFromGlobal(QCursor.pos()))
            if idx.isValid():
                d = idx.data(Qt.ItemDataRole.ToolTipRole)
                if d:
                    return str(d)
        if isinstance(obj, QWidget):
            return obj.toolTip()
        return ""


def install_tip_bubble(app):
    """装全站 tooltip 接管（theme.apply_theme 启动时调）。幂等：只装一次。"""
    if getattr(app, "_aigc_tip_router", None) is None:
        app._aigc_tip_router = _TipRouter(app)
        app.installEventFilter(app._aigc_tip_router)


_DIALOG_SHADOW = 18   # 无边框对话框四周撑出的阴影带（也是自绘卡片的外边距）


class _DialogCardPainter(QObject):
    """接管对话框自身的 Paint 事件：去原生框后，在透明顶层上自绘一张圆角白卡 +
    明显描边 + 向外逐层变淡的软阴影——解决“对话框遇到白色背景直接融为一体”。
    返回 True 只吃掉对话框本体的绘制，子控件（图标 / 文字 / 按钮）照常各自绘制。"""

    def __init__(self, dlg, radius, margin):
        super().__init__(dlg)
        self._dlg, self._radius, self._margin = dlg, radius, margin

    def eventFilter(self, obj, ev):
        if obj is self._dlg and ev.type() == QEvent.Type.Paint:
            p = QPainter(self._dlg)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            # 描边取 border_strong（比弹层的 border_popup 更实），白底上也勾得出轮廓
            _draw_rounded_card(p, self._dlg.rect(), self._radius, self._margin,
                               border=C["border_strong"])
            p.end()
            return True
        return False


def _round_dialog(dlg, radius=None):
    """把 QMessageBox / QDialog 变成真圆角浮层：去原生框 + 逐像素透明，再由
    _DialogCardPainter 自绘圆角白卡 + 描边 + 软阴影。
    ① 旧版只靠 QSS border-radius，圆角外四角被原生方窗底填满 → 直角；
    ② 且 1px border_popup(#E5E7EB) 太浅，落在白卡片 / 白色桌面上直接融为一体。
    这里两件事一起解决：卡片自绘（透明顶层下四角真透明），描边加实 + 补一层软阴影。
    注意：QMessageBox 会在 show 时把自身布局边距重置成固定值，同步撑出的阴影带会被抹掉
    （子控件随即溢出到卡片外）。因此把“撑出阴影带”延到 show 落定后的 singleShot(0)。"""
    dlg.setWindowFlags(dlg.windowFlags()
                       | Qt.WindowType.FramelessWindowHint
                       | Qt.WindowType.NoDropShadowWindowHint)
    dlg.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    dlg.setAutoFillBackground(False)      # 防止自动补背景把阴影带填成白方底
    r = radius if radius is not None else ui_kit.RADIUS["lg"]
    cls = dlg.metaObject().className()
    # 对话框本体背景交给自绘（QSS 置透明，挡住 app 级样式补方底）；子控件样式照旧保留。
    # 按钮淡化：对话框里的确认/取消不再走 app 级实心主蓝（那样太浓、和白卡抢层次），
    # 改浅底 + 主蓝字 + 淡描边，悬停才略加深、按下回实心，视觉上“淡一点”。
    dlg.setStyleSheet(
        f"{cls}{{background:transparent;}}"
        f"{cls} QLabel{{background:transparent;border:none;color:{C['text']};}}"
        f"{cls} QPushButton{{min-width:72px;border-radius:8px;padding:6px 16px;"
        f" background:{C['primary_bg']};color:{C['primary']};border:1px solid {C['border']};}}"
        f"{cls} QPushButton:hover{{background:{C['primary_soft']};border-color:{C['primary']};}}"
        f"{cls} QPushButton:pressed{{background:{C['primary']};color:{C['on_primary']};}}")
    dlg._card_painter = _DialogCardPainter(dlg, r, _DIALOG_SHADOW)
    dlg.installEventFilter(dlg._card_painter)

    # 在对话框现有布局外撑出一圈阴影带（原有内边距保留），卡片画进这条带里面。
    # 幂等：_band_done 只撑一次；延后一帧执行避开 QMessageBox 在 showEvent 里的边距重置。
    dlg._band_done = False

    def _grow_band():
        if dlg._band_done:
            return
        dlg._band_done = True
        lay = dlg.layout()
        if lay is None:
            return
        m = lay.contentsMargins()
        lay.setContentsMargins(m.left() + _DIALOG_SHADOW, m.top() + _DIALOG_SHADOW,
                               m.right() + _DIALOG_SHADOW, m.bottom() + _DIALOG_SHADOW)
        lay.activate()
        dlg.adjustSize()

    QTimer.singleShot(0, _grow_band)
    return dlg


def install_rounded_messagebox():
    """把 QMessageBox 的四个静态便捷弹窗（information / warning / question /
    critical）统一接管成 _round_dialog 圆角浮层。

    为什么这样做而不是逐点改写：全站 260+ 处都调的是 `QMessageBox.information(...)
    这类静态便捷方法，内部自建自 exec、塞不进 _round_dialog。若逐点改成
    “实例 → _round_dialog → exec” 改动面极大且易错。这里在启动装样式时（
    theme.apply_theme）把四个静态方法换成同签名实现：构造实例→套 _round_dialog→exec，
    返回值仍是 StandardButton（与原生同语义），因此 **调用点一行不改**，
    `== QMessageBox.Yes` 等判断照旧成立（零行为变更）。

    幂等：重复调用只是重新绑定同一组函数，无副作用。"""
    B = QMessageBox.StandardButton
    I = QMessageBox.Icon

    def _show(icon, parent, title, text, buttons, default):
        box = QMessageBox(parent)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(text)
        if buttons is not None:
            box.setStandardButtons(buttons)
        # default 为 NoButton（非 0）时不显式指定，交给 QMessageBox 按角色自动选中
        if default is not None and default != B.NoButton:
            box.setDefaultButton(default)
        _round_dialog(box)
        return B(box.exec())

    def information(parent, title, text, buttons=B.Ok, default=B.NoButton, *a, **k):
        return _show(I.Information, parent, title, text, buttons, default)

    def warning(parent, title, text, buttons=B.Ok, default=B.NoButton, *a, **k):
        return _show(I.Warning, parent, title, text, buttons, default)

    def critical(parent, title, text, buttons=B.Ok, default=B.NoButton, *a, **k):
        return _show(I.Critical, parent, title, text, buttons, default)

    def question(parent, title, text, buttons=B.Yes | B.No, default=B.NoButton, *a, **k):
        return _show(I.Question, parent, title, text, buttons, default)

    QMessageBox.information = staticmethod(information)
    QMessageBox.warning = staticmethod(warning)
    QMessageBox.critical = staticmethod(critical)
    QMessageBox.question = staticmethod(question)


class _ShowTip(QObject):
    """“悬停即弹”的 tooltip 加速器：Enter 起 350ms 短延时后直接弹单例 TipBubble，
    比原生 ~700ms 灵敏，离开/按下即隐。与 _TipRouter 共用同一颗气泡——屏幕上同一
    时刻永远只有一个 tooltip，不会再出现「第二个弹层盖掉第一个」。"""

    def __init__(self, widget, text=""):
        super().__init__(widget)
        self._w = widget
        self._text = str(text or "")
        self._t = QTimer(widget)
        self._t.setSingleShot(True)
        self._t.setInterval(350)
        self._t.timeout.connect(self._show)
        widget.installEventFilter(self)

    def _show(self):
        if self._w.isVisible() and self._w.underMouse():
            TipBubble.instance().show_for(self._text or self._w.toolTip(), self._w)

    def eventFilter(self, obj, ev):
        if obj is self._w:
            if ev.type() == QEvent.Type.Enter:
                self._t.start()
            elif ev.type() in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress):
                self._t.stop()
                TipBubble.instance().hide()
        return False


def hover_tip(widget, text=""):
    """给 widget 挂一条悬停即显的 tooltip（350ms 即弹，不等系统默认延时）。
    text 省略＝直接用 widget 自己的 toolTip：同一控件别再叠两套不同文案，
    否则 350ms 先弹加速器文本、原生随后又用 tooltip 文本换掉内容（看着像弹了两个）。"""
    return _ShowTip(widget, text)


class _TreeFilter(QSortFilterProxyModel):
    """层级树搜索过滤：开递归过滤后，命中节点会连同祖先链一起保留，
    输入关键词即把匹配分支拉出来、其余整枝隐藏（解决“三四千条里逐层找”）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRecursiveFilteringEnabled(True)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterKeyColumn(0)


class KitButton(QPushButton):
    """画廊示例按钮：自绘以支持真实动效。

    - 悬停：背景色向 hover 态平滑过渡（hoverT 0→1，QPropertyAnimation 驱动）。
    - 按下：整体轻微回弹缩放（pressT 0→1，绘制时按中心缩放 0.96 + 变暗）。
    - 点击：弹窗说明这颗是什么变体 / 干什么用 / 吃什么令牌（explain=False 时不弹，
      交给按钮自己的演示动作，如弹菜单/走进度）。
    """

    def __init__(self, text, kind="primary", obj_name="", desc="",
                 explain=True, parent=None):
        super().__init__(text, parent)
        self._kind = kind
        self._desc = desc or text
        self._explain = explain
        if obj_name:
            self.setObjectName(obj_name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(34)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._hoverT = 0.0
        self._pressT = 0.0
        self.clicked.connect(self._on_click)

    # ---- Qt 动态属性：给 QPropertyAnimation 平滑插值用（改动即 update 重绘）----
    def _gh(self):
        return self._hoverT

    def _sh(self, v):
        self._hoverT = v
        self.update()

    def _gp(self):
        return self._pressT

    def _sp(self, v):
        self._pressT = v
        self.update()

    hoverT = Property(float, _gh, _sh)
    pressT = Property(float, _gp, _sp)

    # ---- 动效驱动 ----
    def _anim(self, prop, end, dur, ease):
        start = self._hoverT if prop == "hoverT" else self._pressT
        key = "_an_" + prop
        old = getattr(self, key, None)
        if old is not None:
            old.stop()
        a = QPropertyAnimation(self, prop.encode("utf-8"), self)
        a.setDuration(dur)
        a.setStartValue(start)
        a.setEndValue(end)
        a.setEasingCurve(ease)
        setattr(self, key, a)
        a.start()

    def enterEvent(self, e):
        self._anim("hoverT", 1.0, 130, QEasingCurve.Type.OutCubic)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._anim("hoverT", 0.0, 220, QEasingCurve.Type.OutCubic)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._anim("pressT", 1.0, 70, QEasingCurve.Type.OutQuad)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._anim("pressT", 0.0, 170, QEasingCurve.Type.OutQuad)
        super().mouseReleaseEvent(e)

    def _style_for(self):
        # 统一口径（用户定的）：默认颜色深、鼠标移入变浅。所有实心变体都按这个方向。
        k = self._kind
        if k == "ghost":
            # 幽灵钮本体是白底，不变深；悬停只把描边/文字转主蓝（底色保持白）
            return (C["card"], C["ghost_text"], C["card"], C["primary"],
                    C["border"], C["primary"])
        if k == "danger":
            # 默认实心红（深）→ 悬停变浅红（往白方向提亮）
            return (C["danger"], C["on_primary"], _lighten(C["danger"], 0.18),
                    C["on_primary"], None, None)
        if k == "chip":
            # 淡蓝胶囊：默认 primary_soft（略深）→ 悬停 primary_bg（更浅）
            return (C["primary_soft"], C["primary"], C["primary_bg"], C["primary"],
                    C["chip_border"], C["primary"])
        if k == "sidebar":
            # 侧栏深色钮：默认深板底 → 悬停转亮蓝（变浅）
            return (C["sidebar_item"], C["sidebar_text"], C["primary"], C["on_primary"],
                    C["sidebar_border"], C["primary"])
        # primary 默认：深蓝 #3370FF → 悬停浅蓝 #5A8BFF
        return (C["primary"], C["on_primary"], C["primary_hover"], C["on_primary"],
                None, None)

    def paintEvent(self, e):
        bg, fg, hbg, hfg, brd, hbrd = self._style_for()
        h, p = self._hoverT, self._pressT
        en = self.isEnabled()
        checked = self.isCheckable() and self.isChecked()
        if checked:
            bg, fg, brd = C["primary"], C["on_primary"], C["primary"]
        if en:
            cur = _mix(QColor(bg), QColor(hbg), h)
            curfg = _mix(QColor(fg), QColor(hfg), h)
            bcol = QColor(hbrd if h > 0.5 else brd) if brd else None
        else:
            cur, curfg = QColor(C["disabled_bg"]), QColor(C["weak"])
            bcol = QColor(C["disabled_border"])
        if p > 0:
            cur = _mix(cur, QColor(int(cur.red() * 0.82), int(cur.green() * 0.82),
                                   int(cur.blue() * 0.82)), p)
        pr = QPainter(self)
        pr.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        s = 1.0 - 0.04 * p
        if s < 1.0:
            ctr = QPointF(r.center())
            pr.translate(ctr)
            pr.scale(s, s)
            pr.translate(-ctr)
        rad = ui_kit.RADIUS["md"]
        path = QPainterPath()
        path.addRoundedRect(QRectF(r), rad, rad)
        pr.fillPath(path, cur)
        if bcol is not None:
            pen = QPen(bcol)
            pen.setWidthF(1.0)
            pr.setPen(pen)
            pr.drawPath(path)
        else:
            pr.setPen(Qt.PenStyle.NoPen)
        pr.setFont(self.font())
        pr.setPen(QColor(curfg))
        pr.drawText(r, Qt.AlignmentFlag.AlignCenter, self.text())
        pr.end()

    def _on_click(self):
        # 危险钮：先弹「确认提示」，用户确认后才算执行（不可逆动作的标准安全范式）。
        if self._kind == "danger":
            box = QMessageBox(self.window())
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("危险操作确认")
            box.setText("这是一个危险操作（示例）。")
            box.setInformativeText(self._desc + "\n\n确定要执行吗？")
            yes = box.addButton("确认执行", QMessageBox.ButtonRole.AcceptRole)
            no = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(no)          # 默认落在「取消」，防误触
            _round_dialog(box)
            box.exec()
            if box.clickedButton() is yes:
                done = QMessageBox(self.window())
                done.setIcon(QMessageBox.Icon.Information)
                done.setWindowTitle("已确认")
                done.setText("危险操作已执行（示例，仅演示）。")
                _round_dialog(done)
                done.exec()
            return
        if self._explain:
            info = QMessageBox(self.window())
            info.setIcon(QMessageBox.Icon.Information)
            info.setWindowTitle("这是什么按钮 · " + self.text())
            info.setText(self._desc)
            _round_dialog(info)
            info.exec()


# --------------------------------------------------------------------
# 多级联动地区下拉：省 → 市 → 区/县 → 乡镇 → 村（逐级以实际数据为准）
# --------------------------------------------------------------------
REGION_LEVELS = ["省", "市", "区/县", "乡镇", "村"]
# 样例树（真实项目应接后端区划数据）：每层 dict 的 key 就是该层选项。
REGION_DATA = {
    "广东省": {
        "深圳市": {
            "福田区": {"南园街道": {"南园社区": {}, "园岭社区": {}},
                       "华强北街道": {"华强社区": {}}},
            "南山区": {"粤海街道": {"科技园社区": {}}},
        },
        "广州市": {"天河区": {"天园街道": {"东员村": {}}}},
    },
    "北京市": {
        "北京市": {
            "朝阳区": {"建外街道": {"北神社区": {}}},
            "海淀区": {"中关村街道": {"科里社区": {}}},
        },
    },
    "四川省": {"成都市": {"武侯区": {"桂溪街道": {"大源村": {}}}}},
}


class _FieldButton(QPushButton):
    """下拉字段按钮：文字左对齐 + 右侧一个 ▾ 箭头（箭头子控件穿透鼠标，整颗可点）。"""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._caret = QLabel("▾", self)
        self._caret.setStyleSheet(f"color:{C['sub']}; background:transparent;")
        self._caret.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._caret.adjustSize()

    def setText(self, t):
        super().setText(t)
        self._place()

    def resizeEvent(self, e):
        self._place()
        super().resizeEvent(e)

    def _place(self):
        self._caret.move(self.width() - self._caret.width() - 10,
                         (self.height() - self._caret.height()) // 2)


class TreeSelect(QWidget):
    """单个下拉里呈现多级地区树（省→市→区/县→乡镇→村）：点一下弹出一个带层级的
    树形浮层，逐层展开、选中任一节点即回填完整路径。不是多个下拉框联动——
    一框装下整个层级（对齐用户「下拉列表里面多层级」的口径）。"""

    def __init__(self, data=None, parent=None, placeholder="请选择所在地区…"):
        super().__init__(parent)
        self._data = data or REGION_DATA
        self._value = ""
        self._placeholder = placeholder
        self._btn = _FieldButton(placeholder)
        self._btn.setObjectName("TreeSelectField")
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # 字段宽度限住，不在布局里拉满（之前默认最大宽度就是这个原因）
        self._btn.setMinimumWidth(220)
        self._btn.setMaximumWidth(320)
        self.setMaximumWidth(320)
        self._btn.setStyleSheet(
            f"QPushButton#TreeSelectField{{background:{C['card']};color:{C['weak']};"
            f"border:1px solid {C['border']};border-radius:{ui_kit.RADIUS['md']}px;"
            f"padding:6px 26px 6px 10px;text-align:left;}}"
            f"QPushButton#TreeSelectField:hover{{border-color:{C['primary']};}}")
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self._btn)
        h.addStretch(1)
        self._pop = None
        self._btn.clicked.connect(self._toggle)

    # 把数据树灌进 QStandardItemModel：每层 key 是一个节点，有下级则可展开
    def _fill(self, parent_item, node):
        for k, v in node.items():
            it = QStandardItem(k)
            it.setEditable(False)
            parent_item.appendRow(it)
            if isinstance(v, dict) and v:
                self._fill(it, v)

    def _build_pop(self):
        # 真圆角浮层：顶部一个搜索框 + 默认全部折叠的层级树（省得三四千条一次展开）。
        # 点一级展开下一级（级联），点到叶子即回填整条路径；搜索命中会自动拉出所在分支。
        pop = _RoundPopup(self)
        search = QLineEdit()
        search.setPlaceholderText("搜索地区，命中会自动展开所在分支…")
        search.setClearButtonEnabled(True)
        search.setStyleSheet(
            f"QLineEdit{{background:{C['bg_field']};border:1px solid {C['border']};"
            f"border-radius:{ui_kit.RADIUS['md']}px;padding:6px 8px;}}")
        pop.content.addWidget(search)
        tree = QTreeView()
        tree.setFrameShape(QFrame.Shape.NoFrame)
        src = QStandardItemModel()
        self._fill(src.invisibleRootItem(), self._data)
        proxy = _TreeFilter(pop)
        proxy.setSourceModel(src)
        tree.setModel(proxy)
        tree.setHeaderHidden(True)
        tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tree.setAnimated(True)
        tree.setIndentation(18)
        tree.setRootIsDecorated(True)
        tree.setExpandsOnDoubleClick(False)   # 单击即逐级展开，对齐“点一个才展开下一个”
        tree.setStyleSheet(
            f"QTreeView{{background:transparent;border:none;color:{C['text']};"
            f"outline:none;}}"
            f"QTreeView::item{{height:30px;border-radius:6px;}}"
            f"QTreeView::item:hover{{background:{C['primary_bg']};}}"
            f"QTreeView::item:selected{{background:{C['primary_soft']};"
            f"color:{C['primary']};}}"
            f"QTreeView::branch{{background:transparent;}}")
        tree.clicked.connect(lambda idx: self._on_tree_click(tree, idx))
        search.textChanged.connect(lambda t: self._on_search(tree, proxy, t))
        pop._tree, pop._proxy = tree, proxy
        pop.content.addWidget(tree)
        return pop

    def _on_tree_click(self, tree, idx):
        if not idx.isValid():
            return
        m = tree.model()
        if m.rowCount(idx) > 0:            # 有下级：点一下只展开/收起这一级
            tree.setExpanded(idx, not tree.isExpanded(idx))
        else:                              # 叶子：选中、回填完整路径并收起
            self._pick(tree, idx)

    def _pick(self, tree, idx):
        parts = []
        cur = idx
        while cur.isValid():
            parts.insert(0, cur.data())
            cur = cur.parent()
        self._value = " / ".join(parts)
        self._btn.setText(self._value)
        self._btn.setStyleSheet(
            f"QPushButton#TreeSelectField{{background:{C['card']};color:{C['text']};"
            f"border:1px solid {C['border']};border-radius:{ui_kit.RADIUS['md']}px;"
            f"padding:6px 26px 6px 10px;text-align:left;}}"
            f"QPushButton#TreeSelectField:hover{{border-color:{C['primary']};}}")
        tree.window().close()

    def _on_search(self, tree, proxy, text):
        proxy.setFilterFixedString(text.strip())
        if text.strip():
            tree.expandAll()             # 搜索时把命中分支整条展开，一眼看到
        else:
            tree.collapseAll()           # 清空搜索回到“默认全折叠、逐层点”的原状

    def _toggle(self):
        if self._pop is None:
            self._pop = self._build_pop()
        self._pop.resize(max(self._btn.width(), 320), 340)
        self._pop.move(self._btn.mapToGlobal(QPoint(0, self._btn.height() + 2)))
        # 延后一帧再弹：按钮 clicked 后 Qt 会紧接派发一次释放事件，若同步 show
        # 一个 Qt.Popup，它会被这次释放立即关掉——看着就是“点了没反应”。
        QTimer.singleShot(0, self._pop.show)

    def path(self):
        return self._value


# --------------------------------------------------------------------
# 现代化日期选择器：白底字段式按钮 + 点击弹出重新样式的日历浮层
# --------------------------------------------------------------------
_CAL_QSS = (
    f"QCalendarWidget QWidget{{background:{C['card']};color:{C['text']};}}"
    f"QCalendarWidget QWidget#qt_calendar_navigationbar{{background:{C['card']};"
    f"border:none;border-bottom:1px solid {C['divider']};}}"
    f"QCalendarWidget QToolButton{{background:transparent;color:{C['text']};"
    f"border:none;border-radius:6px;padding:4px 8px;font-weight:600;}}"
    f"QCalendarWidget QToolButton:hover{{background:{C['primary_soft']};color:{C['primary']};}}"
    f"QCalendarWidget QToolButton::menu-indicator{{image:none;}}"
    f"QCalendarWidget QMenu{{background:{C['card']};border:1px solid {C['border_popup']};"
    f"border-radius:8px;}}"
    f"QCalendarWidget QSpinBox{{background:{C['card']};border:1px solid {C['border']};"
    f"border-radius:6px;padding:2px;}}"
    f"QCalendarWidget QAbstractItemView:enabled{{background:{C['card']};outline:none;"
    f"selection-background-color:{C['primary']};selection-color:{C['on_primary']};}}"
    f"QCalendarWidget QAbstractItemView:disabled{{color:{C['weak']};}}")


class _DatePickerPopup(_RoundPopup):
    """真圆角日历浮层：点外部自动收起；选中日期回调宿主。背景由 _RoundPopup 自绘
    圆角白卡 + 软阴影，左侧一列快捷选项（今天/昨天/近7天/近30天/近90天）。"""

    # (文案, 相对今天的天数偏移)：近 N 天取该窗口起始日（今天-(N-1)）
    PRESETS = (("今天", 0), ("昨天", -1), ("近7天", -6),
               ("近30天", -29), ("近90天", -89))

    def __init__(self, owner):
        super().__init__(owner)
        row = QHBoxLayout()
        row.setSpacing(6)
        # 左侧快捷选项栏
        side = QVBoxLayout()
        side.setSpacing(4)
        for text, off in self.PRESETS:
            b = QPushButton(text)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton{{background:transparent;color:{C['primary']};"
                f"border:none;border-radius:6px;padding:6px 12px;text-align:left;}}"
                f"QPushButton:hover{{background:{C['primary_soft']};}}")
            b.clicked.connect(lambda _c, o=off: self._pick_offset(o))
            side.addWidget(b)
        side.addStretch(1)
        side_w = QWidget()
        side_w.setLayout(side)
        side_w.setStyleSheet("background:transparent;")
        side_w.setMinimumWidth(88)
        row.addWidget(side_w)
        # 右侧日历
        self.cal = QCalendarWidget()
        self.cal.setGridVisible(False)
        self.cal.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.cal.setHorizontalHeaderFormat(
            QCalendarWidget.HorizontalHeaderFormat.SingleLetterDayNames)
        self.cal.setStyleSheet(_CAL_QSS)
        self.cal.clicked.connect(self._pick)
        row.addWidget(self.cal)
        self.content.addLayout(row)
        self._owner = owner

    def _pick_offset(self, off):
        self._owner.set_date(QDate.currentDate().addDays(off))
        self.close()

    def _pick(self, d):
        self._owner.set_date(d)
        self.close()


class ModernDatePicker(QPushButton):
    """一个字段式按钮，点开弹出日历浮层选择日期（比原生 QDateEdit 日历现代）。"""

    def __init__(self, date=None, parent=None):
        super().__init__(parent)
        self._date = date or QDate.currentDate()
        self.setObjectName("DateField")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(180)
        self.setStyleSheet(
            f"QPushButton#DateField{{background:{C['card']};color:{C['text']};"
            f"border:1px solid {C['border']};border-radius:{ui_kit.RADIUS['md']}px;"
            f"padding:6px 10px;text-align:left;}}"
            f"QPushButton#DateField:hover{{border-color:{C['primary']};}}"
            f"QPushButton#DateField:pressed{{background:{C['primary_bg']};}}")
        self._popup = None
        self._sync()
        self.clicked.connect(self._toggle)

    def set_date(self, d):
        self._date = d
        self._sync()

    def _sync(self):
        self.setText("🗓  " + self._date.toString("yyyy 年 MM 月 dd 日"))

    def _toggle(self):
        if self._popup is None:
            self._popup = _DatePickerPopup(self)
        self._popup.cal.setSelectedDate(self._date)
        self._popup.adjustSize()
        self._popup.move(self.mapToGlobal(QPoint(0, self.height() + 4)))
        self._popup.show()


# --------------------------------------------------------------------
# 日期区间选择器：字段式按钮弹出「快捷范围 + 起始/结束两个日历」的圆角浮层
# --------------------------------------------------------------------
class _DateRangePopup(_RoundPopup):
    """区间日历浮层：左侧快捷（近 N 天＝一段范围，不是一天），右侧两个日历分别选
    “起始 / 结束”。点快捷直接确定；手动选完点“确定”回填。"""

    # (文案, 起始偏移, 结束偏移)：相对今天；结束 0=今天，近 N 天取 今天-(N-1)~今天
    PRESETS = (("今天", 0, 0), ("昨天", -1, -1), ("近7天", -6, 0),
               ("近30天", -29, 0), ("近90天", -89, 0))

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner
        self._start = owner._start
        self._end = owner._end
        row = QHBoxLayout()
        row.setSpacing(8)
        # 左侧快捷范围
        side = QVBoxLayout()
        side.setSpacing(4)
        for text, so, eo in self.PRESETS:
            b = QPushButton(text)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton{{background:transparent;color:{C['primary']};"
                f"border:none;border-radius:6px;padding:6px 12px;text-align:left;}}"
                f"QPushButton:hover{{background:{C['primary_soft']};}}")
            b.clicked.connect(lambda _c, s=so, e=eo: self._apply_preset(s, e))
            side.addWidget(b)
        side.addStretch(1)
        side_w = QWidget()
        side_w.setLayout(side)
        side_w.setStyleSheet("background:transparent;")
        side_w.setMinimumWidth(88)
        row.addWidget(side_w)
        # 右侧：起始 / 结束 两个日历
        self._cal_start = self._mk_cal()
        self._cal_end = self._mk_cal()
        self._cal_start.clicked.connect(self._pick_start)
        self._cal_end.clicked.connect(self._pick_end)
        for cap, cal in (("起始", self._cal_start), ("结束", self._cal_end)):
            col = QVBoxLayout()
            col.setSpacing(2)
            t = QLabel(cap)
            t.setStyleSheet(f"color:{C['sub']};font-size:12px;background:transparent;"
                            "font-weight:600;")
            col.addWidget(t)
            col.addWidget(cal)
            row.addLayout(col)
        self.content.addLayout(row)
        # 底部：范围文字 + 确定
        foot = QHBoxLayout()
        self._lbl = QLabel()
        self._lbl.setStyleSheet(f"color:{C['primary']};font-weight:600;"
                                "background:transparent;")
        foot.addWidget(self._lbl)
        foot.addStretch(1)
        ok = KitButton("确定", kind="primary", explain=False)
        ok.clicked.connect(self._commit)
        foot.addWidget(ok)
        self.content.addLayout(foot)
        self._refresh()

    def _mk_cal(self):
        cal = QCalendarWidget()
        cal.setGridVisible(False)
        cal.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        cal.setHorizontalHeaderFormat(
            QCalendarWidget.HorizontalHeaderFormat.SingleLetterDayNames)
        cal.setStyleSheet(_CAL_QSS)
        return cal

    def _apply_preset(self, so, eo):
        today = QDate.currentDate()
        self._start = today.addDays(so)
        self._end = today.addDays(eo)
        self._commit()

    def _pick_start(self, d):
        self._start = d
        if self._end < self._start:
            self._end = d
        self._refresh()

    def _pick_end(self, d):
        self._end = d
        if self._start > self._end:
            self._start = d
        self._refresh()

    def _refresh(self):
        self._cal_start.setSelectedDate(self._start)
        self._cal_end.setSelectedDate(self._end)
        n = self._start.daysTo(self._end) + 1
        self._lbl.setText(f"{self._start.toString('yyyy-MM-dd')} ~ "
                          f"{self._end.toString('yyyy-MM-dd')}（{n} 天）")

    def _commit(self):
        self._owner.set_range(self._start, self._end)
        self.close()


class DateRangePicker(QPushButton):
    """字段式按钮：点开日历浮层选一段日期区间（近 N 天是一个范围）。单选日期
    用不带快捷的 ModernDateEdit；要区间就用这颗。

    set_range 回填时发 changed 信号（日历弹层提交 / 外部设值都会触发），
    并用 get_range() 取当前 (start, end)——给需要“选完即时过滤”的业务页用。"""

    changed = Signal()

    def __init__(self, start=None, end=None, parent=None):
        super().__init__(parent)
        today = QDate.currentDate()
        self._end = end or today
        self._start = start or self._end.addDays(-6)
        self.setObjectName("DateField")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(240)
        self.setStyleSheet(
            f"QPushButton#DateField{{background:{C['card']};color:{C['text']};"
            f"border:1px solid {C['border']};border-radius:{ui_kit.RADIUS['md']}px;"
            f"padding:6px 10px;text-align:left;}}"
            f"QPushButton#DateField:hover{{border-color:{C['primary']};}}"
            f"QPushButton#DateField:pressed{{background:{C['primary_bg']};}}")
        self._popup = None
        self._sync()
        self.clicked.connect(self._toggle)

    def set_range(self, s, e):
        self._start, self._end = (s, e) if s <= e else (e, s)
        self._sync()
        self.changed.emit()

    def get_range(self):
        """当前选定的 (起始 QDate, 结束 QDate)。"""
        return self._start, self._end

    def _sync(self):
        n = self._start.daysTo(self._end) + 1
        self.setText(f"📅  {self._start.toString('yyyy-MM-dd')} ~ "
                     f"{self._end.toString('yyyy-MM-dd')}（{n} 天）")

    def _toggle(self):
        if self._popup is None:
            self._popup = _DateRangePopup(self)
        self._popup._start = self._start
        self._popup._end = self._end
        self._popup._refresh()
        self._popup.adjustSize()
        self._popup.move(self.mapToGlobal(QPoint(0, self.height() + 4)))
        # 同 TreeSelect：延后一帧再弹，避开按钮 clicked 紧接的释放事件把 Popup 立即关掉
        QTimer.singleShot(0, self._popup.show)


# --------------------------------------------------------------------
# 带浮动百分比气泡的滑块：拖动时在拇指上方浮现半透明气泡，静止即隐藏
# --------------------------------------------------------------------
class KitSlider(QSlider):
    """带浮动百分比气泡的滑块：拖动时在拇指正上方浮现一个深色圆角气泡包住数字，
    气泡跟随拇指平滑移动；松手即隐。拇指位置用 QStyle 的 handle 子控件矩形精确
    取得（不再靠估算），拖动时逐像素跟随，观感顺滑。"""

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._vert = (orientation == Qt.Orientation.Vertical)
        if self._vert:
            # 垂直滑块：缩小占地（之前 180×72 显得太大，把整行顶得很高）；
            # 右侧留一点气泡带即可，不够宽时 _place_bubble 会自动翻到拇指左侧
            self.setMinimumHeight(120)
            self.setMinimumWidth(40)
        else:
            self.setMinimumHeight(58)          # 顶部留一条带放气泡（子控件会被父矩形裁剪）
        self.setTickInterval(0)
        self._bubble = QLabel(self)
        self._bubble.setObjectName("KitBubble")
        self._bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bubble.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # 关键：开 WA_StyledBackground，QSS 的深色底才会真的画出来——
        # 之前没开，背景透明、白字落在白底上看不见，看着就像“只有白色数字”。
        self._bubble.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._bubble.setStyleSheet(
            "QLabel#KitBubble{background:rgba(31,35,41,0.92);color:#FFFFFF;"
            "border-radius:7px;padding:3px 9px;font-size:12px;font-weight:600;}")
        self._bubble.hide()
        # 气泡水平位置用属性动画平滑跟随拇指（避免拖动时一顿一顿地跳）
        self._bx = QPropertyAnimation(self._bubble, b"pos", self)
        self._bx.setDuration(90)
        self._bx.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.sliderPressed.connect(lambda: self._show_bubble(True))
        self.sliderReleased.connect(self._on_release)
        self.valueChanged.connect(lambda: self._place_bubble(animated=self.isSliderDown()))
        self.sliderMoved.connect(lambda _v: self._place_bubble(animated=True))

    def _fmt(self):
        return f"{self.value()}%" if self.maximum() == 100 else str(self.value())

    def _show_bubble(self, on):
        if on:
            self._place_bubble()
            self._bubble.show()
            self._bubble.raise_()
        elif not self.isSliderDown():
            self._bubble.hide()

    def _on_release(self):
        # 松手：若鼠标已不在滑块上则收起；还悬停着就再显示一小会儿由 leave 兜底
        if not self.underMouse():
            self._bubble.hide()

    def leaveEvent(self, e):
        if not self.isSliderDown():
            self._bubble.hide()
        super().leaveEvent(e)

    def _handle_rect(self):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderHandle, self)

    def _place_bubble(self, animated=False):
        self._bubble.setText(self._fmt())
        self._bubble.adjustSize()
        bw, bh = self._bubble.width(), self._bubble.height()
        hr = self._handle_rect()
        if self._vert:
            # 垂直：气泡跟到拇指右侧，居中对齐拇指；右侧放不下就换到左侧
            by = max(2, min(hr.center().y() - bh // 2, self.height() - bh - 2))
            bx = hr.right() + 6
            if bx + bw > self.width() - 2:
                bx = hr.left() - bw - 6
            bx = max(2, bx)
        else:
            bx = max(2, min(hr.center().x() - bw // 2, self.width() - bw - 2))
            # 再往上搜一点（留 5px 缝）——用户反馈气泡仍与拇指重叠，往上一点点就够了
            by = max(0, hr.top() - bh - 5)
        target = QPoint(bx, by)
        if animated and self._bubble.isVisible():
            self._bx.stop()
            self._bx.setEndValue(target)
            self._bx.start()
        else:
            self._bubble.move(target)


# --------------------------------------------------------------------
# 多行文本：右下角自由拉伸把手（上/下/左/右都能拉），带最小显示区域
# --------------------------------------------------------------------
class _SizeGrip(QWidget):
    """右下角斜纹把手：按住往任意方向拖，宿主随之自由改宽/改高（最小尺寸钳制）。"""

    def __init__(self, host):
        super().__init__(host)
        self._host = host
        self.setFixedSize(18, 18)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        self._start = None
        self._base = None

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self._host.underMouse() or self._start is not None
        pen = QPen(QColor(C["primary"] if on else C["border_strong"]))
        pen.setWidthF(1.3)
        p.setPen(pen)
        w, h = self.width(), self.height()
        # 三条平行于右下角 45° 斜线的短斜纹（经典拉伸把手纹样）
        for i in range(3):
            t = 5 + i * 4
            p.drawLine(w - t, h - 2, w - 2, h - t)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._start = QCursor.pos()
            self._base = QSize(self._host.width(), self._host.height())
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._start is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            cur = QCursor.pos()
            d = cur - self._start
            nw = max(self._host.minimumWidth(), self._base.width() + d.x())
            nh = max(self._host.minimumHeight(), self._base.height() + d.y())
            self._host.resize(nw, nh)
            self.update()
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._start is not None:
            self._start = None
            self.update()
            e.accept()
            return
        super().mouseReleaseEvent(e)


class ResizableTextEdit(QWidget):
    """多行文本 + 右下角自由拉伸把手：上下左右均可拖大拖小，但有最小显示区域。
    放进布局时需以对齐方式加入（AlignLeft|AlignTop），布局才尊重它的自定义尺寸。"""
    GRIP = 18

    def __init__(self, min_w=220, min_h=90, placeholder="", parent=None):
        super().__init__(parent)
        self.setMinimumSize(min_w, min_h)
        self.resize(min_w + 160, min_h + 24)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText(placeholder)
        v.addWidget(self.edit)
        self._grip = _SizeGrip(self)

    def resizeEvent(self, e):
        self._grip.move(self.width() - self.GRIP, self.height() - self.GRIP)
        self._grip.raise_()
        super().resizeEvent(e)


# --------------------------------------------------------------------
# 表格：仅表头右键可设本列对齐/换行；行高可拖；单元格其他处不可设
# --------------------------------------------------------------------
class _KitItemDelegate(QStyledItemDelegate):
    """表格单元格委托：
    ① 关掉原生焦点虚线框——选中 / 复制的方框统一由 KitTable.paintEvent 自绘，
       尺寸与整格一致（原生焦点框内缩一圈，看着比单元格小）；
    ② 双击内联编辑时把编辑器加宽到视口右缘（Excel 式向右溢出），长文本不被窄格遮挡。"""

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        # 去掉 HasFocus 位：格子里那圈原生焦点点线就不再画了
        option.state &= ~QStyle.StateFlag.State_HasFocus

    def createEditor(self, parent, option, index):
        ed = super().createEditor(parent, option, index)
        # 内联编辑框改成方角（border-radius:0）+ 主蓝描边：app 级 QLineEdit 的 8px
        # 圆角会和直角单元格/表格外框相冲（“圆角里套直角黑边”）；方角才能贴进格子里。
        ed.setStyleSheet(
            f"QLineEdit{{background:{C['card']};color:{C['text']};"
            f"border:1px solid {C['primary']};border-radius:0;padding:2px 6px;}}")
        return ed

    def updateEditorGeometry(self, editor, option, index):
        r = option.rect
        parent = editor.parent()
        vp = parent.rect() if parent is not None else r
        # 从当前格左缘一直铺到视口右缘，至少保持格宽；双击改长文本时能看全
        w = max(r.width(), vp.right() - r.left() - 2)
        editor.setGeometry(r.left(), r.top(), w, max(r.height(), 28))


class SortableTableHeader(QHeaderView):
    """每列都画排序箭头的表头（对标飞书 / Excel）：
    · 默认可见——非活动列画淡灰 ▼（表示「这列默认按降序」），当前排序列画主蓝 ▼/▲；
    · 右缘预留 ARROW_W 给箭头，文字在其左侧省略号截断——列再窄也不与文字重合；
    · 勾选 / 锁定列（no_arrow_cols）不画箭头、文字占满整格；
    · 首点某列＝按降序（Qt 默认新列升序，这里翻转成符合直觉的降序），再点同一列才翻转。

    只做「绘制 + 首点降序」两件事：点击 / 拖动 / 调整列宽 / sectionClicked 等一律沿用
    QHeaderView 内建行为（不覆写 mousePress），因此接入方已有的排序、列锁定、点表头
    全选等逻辑完全不受影响。排序方向直接读内建 sortIndicatorSection()/sortIndicatorOrder()，
    与宿主 setSortIndicator/sortItems 单一真源。"""

    ARROW_W = 16          # 右缘留给箭头的宽度
    PAD_L = 8             # 文字左内边距（对齐 QSS QHeaderView::section 的 padding）

    def __init__(self, orientation=Qt.Orientation.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setSectionsClickable(True)
        self.setHighlightSections(False)
        # 关掉内建箭头，箭头一律由本类自绘（否则窄列会与文字挤在一起、且只有当前列有箭头）
        self.setSortIndicatorShown(False)
        self._no_arrow = set()        # 不画箭头的列（勾选 / 锁定）
        self._last_col = None         # 上一次点过的排序列（决定「同列翻转 / 新列降序」）

    def set_no_arrow_cols(self, cols):
        self._no_arrow = set(cols)

    def handle_section_click(self, logical):
        """点某列文本区 → 该列成为当前排序列并定方向：
        · 首次点这列（或点了新列）＝降序（符合「默认降序」直觉；Qt 内建对新列默认升序，这里翻正）；
        · 连着点同一列＝在降/升之间翻转。
        自持 _last_col 判定，不依赖基类是否在点击时改动指示列——本类把 setSortIndicatorShown(False)
        关掉了内建箭头，基类松开鼠标时并不会自动拨动指示列，早期「读 super 前后差值」的判据在这张
        表头上永远不成立（首点 / 翻转全失效）。勾选 / 锁定列（no_arrow_cols）直接跳过：它们的点击
        交给宿主（如点勾选列表头全选）。"""
        if logical is None or logical < 0 or logical >= self.count():
            return
        if logical in self._no_arrow:
            return
        if logical == self._last_col:
            cur = self.sortIndicatorOrder()
            new = (Qt.SortOrder.AscendingOrder
                   if cur == Qt.SortOrder.DescendingOrder
                   else Qt.SortOrder.DescendingOrder)
        else:
            new = Qt.SortOrder.DescendingOrder
        self._last_col = logical
        self.setSortIndicator(logical, new)
        return new

    def mouseReleaseEvent(self, e):
        """松开：先让基类跑完内建点击 / 拖列 / sectionClicked 语义，再据「点的是文本、不是拖动列
        边界」自订方向。sectionOffset 非 0 表示这是一次拖动改列位，不当成排序点击。"""
        logical = (self.logicalIndexAt(e.position().toPoint())
                   if e.button() == Qt.MouseButton.LeftButton else -1)
        was_move = self.sectionOffset() != 0
        super().mouseReleaseEvent(e)
        if logical >= 0 and not was_move and self.sectionsClickable():
            self.handle_section_click(logical)

    def _text(self, logical_index):
        h = self.model().headerData(logical_index, self.orientation(),
                                    Qt.ItemDataRole.DisplayRole)
        # PySide6 会把 headerData 直接转成 Python 值（多为 str）；兼容 QVariant 旧行为
        if h is None:
            return ""
        if isinstance(h, str):
            return h
        return h.toString() if hasattr(h, "toString") else str(h)

    def paintSection(self, painter, rect, logical_index):
        # 勾选 / 锁定列：不画箭头、文字占满整格，交给基类原样渲染
        if logical_index in self._no_arrow:
            super().paintSection(painter, rect, logical_index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 背景铺满整格（含箭头带），复刻 QSS：浅底 + 底部 1px 分隔线（悬停略深一档）
        bg = C["bg_field"] if self.underMouse() else C["disabled_bg"]
        painter.fillRect(rect, QColor(bg))
        painter.setPen(QColor(C["border_popup"]))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        # 文字：只在「去掉箭头带」的左区绘制并右侧省略，从根上杜绝与箭头重合
        text_rect = QRect(rect.left(), rect.top(), rect.width() - self.ARROW_W, rect.height())
        painter.setPen(QColor(C["table_head"]))
        fm = painter.fontMetrics()
        avail = max(0, text_rect.width() - self.PAD_L - 2)
        elided = fm.elidedText(self._text(logical_index),
                               Qt.TextElideMode.ElideRight, avail)
        painter.drawText(text_rect.adjusted(self.PAD_L, 0, -2, 0),
                         int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                         elided)
        # 箭头：当前排序列=主蓝、方向随内建指示；其余列=淡灰 ▼（默认降序示意）
        active = self.sortIndicatorSection()
        if logical_index == active:
            up = (self.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder)
            color = QColor(C["primary"])
        else:
            up = False
            color = QColor(C["weak"])
        self._draw_arrow(painter, rect, up, color)
        painter.restore()

    def _draw_arrow(self, painter, rect, up, color):
        cx = rect.right() - self.ARROW_W // 2
        cy = rect.center().y()
        w, h = 4, 3
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        if up:
            poly = QPolygonF([QPointF(cx - w, cy + h), QPointF(cx + w, cy + h),
                              QPointF(cx, cy - h)])
        else:
            poly = QPolygonF([QPointF(cx - w, cy - h), QPointF(cx + w, cy - h),
                              QPointF(cx, cy + h)])
        painter.drawPolygon(poly)


class _MarqueeLayer(QWidget):
    """复制后在选区外画 Excel 式「行走的虚线框」；也可平时给当前选区画一圈实线环。
    做成 viewport 的透明叠层子控件：任何 QTableWidget 都能获得（不必是 KitTable），
    TableColumnKit attach 时自动挂上——把复制反馈从 KitTable 私有变成全站通用。"""

    def __init__(self, table, show_selection_ring=False, parent=None):
        super().__init__(table.viewport())
        self._t = table
        self._ring = show_selection_ring
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self._ranges = []
        self._phase = 0
        self._timer = QTimer(self)
        self._timer.setInterval(220)
        self._timer.timeout.connect(self._tick)
        self.setGeometry(table.viewport().rect())
        self.show()

    def start(self, ranges):
        self._ranges = list(ranges)
        self.raise_()
        self.sync()
        self._timer.start()

    def stop(self):
        if self._ranges:
            self._ranges = []
            self._timer.stop()
            self.update()

    def sync(self):
        self.setGeometry(self._t.viewport().rect())
        self.update()

    def _tick(self):
        self._phase = (self._phase + 1) % 8
        self.update()

    def _rect(self, rng):
        m = self._t.model()
        tl = self._t.visualRect(m.index(rng.topRow(), rng.leftColumn()))
        br = self._t.visualRect(m.index(rng.bottomRow(), rng.rightColumn()))
        return QRect(tl.left(), tl.top(),
                     br.right() - tl.left() + 1, br.bottom() - tl.top() + 1)

    def paintEvent(self, e):
        if not self._ranges and not self._ring:
            return
        p = QPainter(self)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self._ranges:
            pen = QPen(QColor(C["text"]))
            pen.setWidthF(1.2)
            pen.setDashPattern([3, 3])
            pen.setDashOffset(self._phase * 0.75)   # 逐帧偏移→虚线“行走”
            p.setPen(pen)
            for rng in self._ranges:
                p.drawRect(self._rect(rng))
        elif self._ring:
            pen = QPen(QColor(C["primary"]))
            pen.setWidthF(1.2)
            p.setPen(pen)
            for rng in self._t.selectedRanges():
                p.drawRect(self._rect(rng))
        p.end()


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _fmt_key(s, fmt):
    """字段格式比较键：number 取首个数字（去千分位逗号）；date 按分隔段拆出数字、
    逐段补零拼成定长整数（年 4 位、其余 2 位，「2026年9月9日」与「2026-09-09」同轴）；
    其余按文本。"""
    if fmt == "number":
        m = _NUM_RE.search(str(s).replace(",", ""))
        return float(m.group()) if m else float("-inf")
    if fmt == "date":
        parts = [p for p in re.split(r"\D+", str(s)) if p]
        buf = "".join(p if len(p) == 4 else p.zfill(2) for p in parts)
        return int((buf + "0" * 14)[:14]) if buf else 0
    return str(s)


class TableColumnKit(QObject):
    """可复用表格装配器，attach 到任意 QTableWidget 补上两件事（被 KitTable 复用=单一真源）：
    ① 表头右键 → 一级菜单：居中调整 / 顺序调整 / 字段格式 三个二级菜单；
      每个二级菜单尾部带「默认展开」，选中后右键直达该二级菜单（不再出一级菜单）；
      顺序调整＝升序/降序排列（不画排序箭头提示），字段格式（文本/数字/日期）决定比较口径；
      选择与列格式经 store.app_state 按页面键持久化（丢了不影响业务）；
    ② Ctrl+C 复制选中区（制表符分隔）并在选区外画 Excel 式「行走的虚线框」，Esc 取消；
      可选 show_selection_ring=True 时平时给选区画一圈实线环（KitTable 用）。
    除表头右键与 Copy/Esc 两个键外，绝不触碰各表自己的委托、选择模式、排序、单元格
    右键业务菜单等配置。用法：TableColumnKit(table, baseline_h=None, show_selection_ring=False)；
    实例以 table 为父，生命周期随表。baseline_h 缺省取默认行高，作为「关换行」复位基准。"""

    # 对齐位掩码：水平位与垂直位分开管理，切换行 / 只改某一个方向时另一方向不受影响
    _HMASK = (int(Qt.AlignmentFlag.AlignLeft) | int(Qt.AlignmentFlag.AlignRight)
              | int(Qt.AlignmentFlag.AlignHCenter) | int(Qt.AlignmentFlag.AlignJustify))
    _VMASK = (int(Qt.AlignmentFlag.AlignTop) | int(Qt.AlignmentFlag.AlignVCenter)
              | int(Qt.AlignmentFlag.AlignBottom))

    def __init__(self, table, baseline_h=None, show_selection_ring=False):
        super().__init__(table)
        self._t = table
        self._wrap = table.wordWrap()   # 跟随表格现状，菜单首项文案才准确
        self._col_h = {}                # col → 水平对齐基准（仅水平位）
        self._col_v = {}                # col → 垂直对齐基准（仅垂直位）
        # 表头右键菜单偏好：列格式（col→text/number/date）与「默认展开」的二级菜单
        self._col_fmt = {}
        self._expand = None
        prefs = self._load_prefs()
        self._expand = prefs.get("expand") if prefs.get("expand") in ("align", "order", "format") else None
        try:
            self._col_fmt = {int(k): v for k, v in (prefs.get("fmt") or {}).items()
                             if v in ("text", "number", "date")}
        except (TypeError, ValueError):
            self._col_fmt = {}
        vh = table.verticalHeader()
        self._row_h0 = (baseline_h if baseline_h is not None
                        else vh.defaultSectionSize())
        hh = table.horizontalHeader()
        hh.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        hh.installEventFilter(self)
        # 复制行走虚线框（+ 可选选区实线环）：做成 viewport 叠层，任何表都能获得
        self._marquee = _MarqueeLayer(table, show_selection_ring)
        table.installEventFilter(self)              # 捕获 Ctrl+C / Esc
        table.viewport().installEventFilter(self)   # 视口尺寸变化时同步叠层
        table.selectionModel().selectionChanged.connect(self._marquee.update)
        hh.sectionResized.connect(lambda *a: self._marquee.sync())
        hh.sectionMoved.connect(lambda *a: self._marquee.sync())
        table.verticalScrollBar().valueChanged.connect(lambda *a: self._marquee.sync())
        table.horizontalScrollBar().valueChanged.connect(lambda *a: self._marquee.sync())

    def set_col_align(self, col, halign):
        # 只改水平对齐（左/中/右）：存进 _col_h 的水平位，垂直位与换行位一律不动
        self._col_h[col] = int(halign) & self._HMASK
        self._apply_col(col)

    def set_col_valign(self, col, valign):
        # 只改垂直对齐（顶端/居中/底端）：存进 _col_v 的垂直位，水平位与换行位一律不动。
        # 与水平对齐彼此独立，这样“垂直居中”不会把已设的靠左/居中/靠右冲回默认。
        self._col_v[col] = int(valign) & self._VMASK
        self._apply_col(col)

    def _apply_col(self, col):
        # 组合该行对齐 = 水平基准(_col_h) + 垂直基准(_col_v) + 换行位（按开关）。
        # 没通过菜单设过的列，基准回退取单元格当前对齐的对应位；当前既没标垂直位
        # 就是 Qt 默认的垂直居中，故回退默认给 AlignVCenter——保证开/关换行时水平与
        # 垂直都原样保留（早期把 base 硬取 AlignLeft 且抹 VCenter 是“换行前后不一致”的根因）。
        word = int(Qt.TextFlag.TextWordWrap)
        left = int(Qt.AlignmentFlag.AlignLeft)
        vcenter = int(Qt.AlignmentFlag.AlignVCenter)
        for r in range(self._t.rowCount()):
            it = self._t.item(r, col)
            if it:
                cur = int(it.textAlignment())
                h = self._col_h.get(col, (cur & self._HMASK) or left)
                v = self._col_v.get(col, (cur & self._VMASK) or vcenter)
                it.setTextAlignment(Qt.AlignmentFlag(h | v | (word if self._wrap else 0)))

    def toggle_wrap(self):
        self._wrap = not self._wrap
        # 视图级 wordWrap 直接跟随开关：关→单行 + … 省略（绝不折行，从根上消除“两排字”），
        # 开→按列宽折行。item 的 TextWordWrap 标志（_apply_col）作为叠加保险，两者同向。
        self._t.setWordWrap(self._wrap)
        for c in range(self._t.columnCount()):
            self._apply_col(c)
        vh = self._t.verticalHeader()
        if self._wrap:
            # 开换行：按内容自适应行高，长文本整格撑开多行
            vh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            self._t.resizeRowsToContents()
        else:
            # 关换行：ResizeToContents 不会自动缩回，必须切回 Interactive 并逐行复位到一行基准高。
            vh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            t = self._t
            h0 = self._row_h0

            def _reset_heights():
                # setRowHeight 只在行高模式已落到 Interactive 时才生效；若同一帧里模式尚未
                # 完成切换（不同机器/时序下可能发生），此时设值会被 ResizeToContents 忽略，行就
                # 卡在换行时的两行高。故立即复位一次 + 事件循环后再兜底复位一次（幂等）。
                for r in range(t.rowCount()):
                    t.setRowHeight(r, h0)

            _reset_heights()
            QTimer.singleShot(0, _reset_heights)
        return self._wrap

    def eventFilter(self, obj, ev):
        t = ev.type()
        if obj is self._t.horizontalHeader() and t == QEvent.Type.ContextMenu:
            col = self._t.horizontalHeader().logicalIndexAt(ev.pos())
            if col >= 0:
                self._show_header_menu(col, ev.globalPos())
                return True
        elif obj is self._t and t == QEvent.Type.KeyPress:
            if ev.matches(QKeySequence.StandardKey.Copy):
                self._copy_selection()
                return True
            if ev.key() == Qt.Key.Key_Escape:      # Esc 取消“行走的虚线框”
                self._marquee.stop()
        elif obj is self._t.viewport() and t == QEvent.Type.Resize:
            self._marquee.sync()
        return False

    def _copy_selection(self):
        """选中区拼成制表符文本写剪贴板（多行多列都行）；无选中则复制当前格。
        复制后让叠层在选区外画「行走的虚线框」（Esc 或重新选中会停）。"""
        t = self._t
        app = QApplication.instance()
        if app is None:
            return
        ranges = t.selectedRanges()
        if not ranges:
            it = t.currentItem()
            if it is not None:
                app.clipboard().setText(it.text())
                ranges = [QTableWidgetSelectionRange(it.row(), it.column(),
                                                     it.row(), it.column())]
            else:
                return
        lines = []
        for r in ranges:
            for row in range(r.topRow(), r.bottomRow() + 1):
                cells = []
                for col in range(r.leftColumn(), r.rightColumn() + 1):
                    it = t.item(row, col)
                    cells.append(it.text() if it is not None else "")
                lines.append("\t".join(cells))
        app.clipboard().setText("\n".join(lines))
        self._marquee.start(ranges)

    # ---- 表头右键菜单：一级 = 居中调整 / 顺序调整 / 字段格式 三个二级菜单 ----
    # 每个二级菜单尾部带「默认展开」：选中后右键直达该二级菜单；直达面板里给
    # 「打开完整菜单」兑底回退。纯构建（_build_header_menus）不 exec，单测可直接校验结构。
    _SUB_ORDER = ("align", "order", "format")
    _SUB_TITLE = {"align": "居中调整", "order": "顺序调整", "format": "字段格式"}

    def _build_header_menus(self, col):
        """构建三个二级菜单及 action→处理函数映射（不 exec）；返回 {key: (QMenu, handlers)}。"""
        A = Qt.AlignmentFlag
        So = Qt.SortOrder
        plan = {}

        m = StyledMenu(self._SUB_TITLE["align"])
        a_l = m.addAction("本列靠左")
        a_c = m.addAction("本列居中")
        a_r = m.addAction("本列靠右")
        m.addSeparator()
        a_v = m.addAction("本列垂直居中")
        a_w = m.addAction("取消自动换行" if self._wrap else "自动换行")
        plan["align"] = (m, {
            a_l: lambda: self.set_col_align(col, A.AlignLeft),
            a_c: lambda: self.set_col_align(col, A.AlignHCenter),
            a_r: lambda: self.set_col_align(col, A.AlignRight),
            a_v: lambda: self.set_col_valign(col, A.AlignVCenter),
            a_w: self.toggle_wrap,
        })

        m = StyledMenu(self._SUB_TITLE["order"])
        a_asc = m.addAction("升序排列")
        a_desc = m.addAction("降序排列")
        plan["order"] = (m, {
            a_asc: lambda: self._apply_sort(col, So.AscendingOrder),
            a_desc: lambda: self._apply_sort(col, So.DescendingOrder),
        })

        m = StyledMenu(self._SUB_TITLE["format"])
        cur = self._col_fmt.get(col, "text")
        grp = QActionGroup(m)          # 三选一：同组 checkable 天然互斥
        handlers = {}
        for val, label in (("text", "文本"), ("number", "数字"), ("date", "日期")):
            act = m.addAction(label)
            act.setCheckable(True)
            act.setChecked(cur == val)
            grp.addAction(act)
            handlers[act] = (lambda v=val: self._set_col_format(col, v))
        plan["format"] = (m, handlers)

        # 每个二级菜单尾部：默认展开（再点一次可取消，radio 语义自持）
        for key, (m, handlers) in plan.items():
            m.addSeparator()
            act = m.addAction("默认展开")
            act.setCheckable(True)
            act.setChecked(self._expand == key)
            handlers[act] = (lambda k=key: self._toggle_default_expand(k))
        return plan

    def _exec_full_menu(self, plan, gpos):
        """一级菜单：按固定顺序挂三个二级菜单，返回被选中的 action（或 None）。"""
        menu = StyledMenu(self._t)
        for key in self._SUB_ORDER:
            menu.addMenu(plan[key][0])
        return menu.exec(gpos)

    def _show_header_menu(self, col, gpos):
        plan = self._build_header_menus(col)
        chosen = None
        if self._expand in plan:
            # 右键直达默认展开的二级菜单：附一条「打开完整菜单」回退项（点了即解除默认）
            sub, _h = plan[self._expand]
            sub.addSeparator()
            back = sub.addAction("⋯ 打开完整菜单")
            chosen = sub.exec(gpos)
            if chosen is back:
                self._expand = None
                self._save_prefs()
                plan = self._build_header_menus(col)
                chosen = self._exec_full_menu(plan, gpos)
        else:
            chosen = self._exec_full_menu(plan, gpos)
        if chosen is None:
            return
        for _m, handlers in plan.values():
            fn = handlers.get(chosen)
            if fn is not None:
                fn()
                return

    def _toggle_default_expand(self, key):
        self._expand = None if self._expand == key else key
        self._save_prefs()

    def _set_col_format(self, col, fmt):
        self._col_fmt[col] = fmt
        self._save_prefs()

    def _apply_sort(self, col, order):
        """菜单排序：按「字段格式」比大小；不画排序箭头提示（升降序走菜单，箭头让位）。"""
        t = self._t
        hdr = t.horizontalHeader()
        hdr.setSortIndicator(-1, order)      # -1＝清空内建指示，表头不保留激活箭头
        fmt = self._col_fmt.get(col, "text")
        if fmt == "text":
            t.sortItems(col, order)          # 文本：交给 item 比较（SecsItem 等自持 __lt 的照旧生效）
            return
        ncol = t.columnCount()
        was = t.isSortingEnabled()
        t.setSortingEnabled(False)           # 手动换位期间关掉自动排，免得 setItem 逐格触发重排
        try:
            rows = list(range(t.rowCount()))
            grid = {r: [t.takeItem(r, c) for c in range(ncol)] for r in rows}

            def key(r):
                it = grid[r][col]
                return _fmt_key(it.text() if it is not None else "", fmt)

            rows.sort(key=key, reverse=(order == Qt.SortOrder.DescendingOrder))
            for new_r, old_r in enumerate(rows):
                for c in range(ncol):
                    it = grid[old_r][c]
                    if it is not None:
                        t.setItem(new_r, c, it)
        finally:
            t.setSortingEnabled(was)
        t.viewport().update()

    # ---- 偏好持久化：按页面类名做键，丢了不影响业务 ----
    def _pref_key(self):
        name = self._t.objectName()
        if name:
            return f"colkit::{name}"
        w = self._t.parent()
        while w is not None:
            cls = type(w).__name__
            if cls.endswith("Page"):
                return f"colkit::{cls}"
            w = w.parent()
        return None

    def _load_prefs(self):
        key = self._pref_key()
        if not key:
            return {}
        try:
            from store import app_state
            v = app_state.get(key, {})
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    def _save_prefs(self):
        key = self._pref_key()
        if not key:
            return
        try:
            from store import app_state
            app_state.set_value(key, {"expand": self._expand,
                                      "fmt": {str(c): f for c, f in self._col_fmt.items()}})
        except Exception:
            pass

    def enable_sort_arrows(self, no_arrow_cols=(0,)):
        """把表格当前水平表头换成 SortableTableHeader（每列画排序箭头）：
        迁移全局开关 / 默认对齐 / 逐列 resize 模式与列宽 / 可拖动开关，重挂表头右键
        过滤器与叠层同步信号（旧表头已随之析构），并把 no_arrow_cols 列排除箭头。

        供「想默认开排序箭头、又不想在建表时手写表头」的页面在建表配置完成后调用一次；
        不需要首点降序以外的排序行为——点击 / 拖列 / 改列宽 / sectionClicked 全沿用内建。
        返回新表头，供调用方进一步接线（如挂全选框 / 连自定义排序槽）。"""
        table = self._t
        old = table.horizontalHeader()
        ncol = table.columnCount()
        modes = [old.sectionResizeMode(c) for c in range(ncol)]
        widths = [table.columnWidth(c) for c in range(ncol)]
        new = SortableTableHeader()
        new.setSectionsClickable(old.sectionsClickable())
        new.setSectionsMovable(old.sectionsMovable())
        new.setHighlightSections(old.highlightSections())
        new.setDefaultAlignment(old.defaultAlignment())
        new.setStretchLastSection(old.stretchLastSection())
        new.setTextElideMode(old.textElideMode())
        new.setDefaultSectionSize(old.defaultSectionSize())
        new.setSortIndicatorShown(False)
        # 换掉表头（旧表头交给视图销毁）：先装新头，再把逐列配置搬回去
        table.setHorizontalHeader(new)
        new.set_no_arrow_cols(no_arrow_cols)
        for c, m in enumerate(modes):
            new.setSectionResizeMode(c, m)
        for c, (m, w) in enumerate(zip(modes, widths)):
            if m != QHeaderView.ResizeMode.Stretch:      # Stretch 列宽由视图自管，别硬设
                table.setColumnWidth(c, w)
        # 本 kit 的表头右键过滤器原装在旧表头上（已析构）：重装到新表头
        new.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        new.installEventFilter(self)
        # 复制叠层原连在旧表头的 sectionResized/Moved 上：一并接到新表头，列变动作照同步
        new.sectionResized.connect(lambda *a: self._marquee.sync())
        new.sectionMoved.connect(lambda *a: self._marquee.sync())
        return new


class KitTable(QTableWidget):
    def __init__(self, rows, cols, headers, parent=None):
        super().__init__(rows, cols, parent)
        self.setHorizontalHeaderLabels(headers)
        hh = self.horizontalHeader()
        hh.setSectionsClickable(True)
        hh.setHighlightSections(False)
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)   # 列宽可拖
        vh = self.verticalHeader()
        vh.setVisible(True)
        # 行高用 Interactive + 手动控制：默认一行基准高；开换行时按内容撑高，
        # 关换行时逐行复位到基准高（ResizeToContents 关换行不会自动缩回，反而重现“两行”bug）
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        vh.setDefaultSectionSize(38)
        # 默认一行：直接关掉视图级 wordWrap（这才是“折不折行”的总开关）——列宽超出部分用
        # … 省略（配合下面 ElideRight），悬停靠 tooltip 看完整内容。只靠 item 不带 TextWordWrap
        # 标志不够：视图 wordWrap 默认为 True 时会无视 item 标志、照样按列宽折行，行高一旦
        # 回到一行高就被裁成“两排字”（即用户反馈的“自动换行前/后不一致”）。toggle_wrap 里切这个开关。
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)  # 单元格右键不弹
        # 单元格：可选中（配合 Ctrl+C 复制）；双击走内联编辑（像 Excel 在格子里改）
        self.setItemDelegate(_KitItemDelegate(self))   # 去原生焦点框 + 编辑框加宽
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                             | QAbstractItemView.EditTrigger.EditKeyPressed)
        # 编辑提交后把 tooltip 同步成最新内容，悬停才能看到改后的全文（等值守卫防信号回环）
        self.itemChanged.connect(self._sync_tooltip)
        # 列对齐 / 换行 / 复制行走虚线框 / 选区实线环 全部交给可复用装配器（单一真源）：
        # 都在 TableColumnKit 里，KitTable 仅复用、不再自带一份。放在 wordWrap 已置
        # False 之后创建，装配器读到的初始 _wrap 才与实际一致。
        self._colkit = TableColumnKit(self, baseline_h=38, show_selection_ring=True)

    def _sync_tooltip(self, it):
        # setToolTip 也会触发 dataChanged→itemChanged，不同值才写，避免无限回环
        if it is not None and it.toolTip() != it.text():
            it.setToolTip(it.text())

    # —— 列对齐 / 自动换行的对外方法转发给 _colkit（保持 KitTable 既有 API 不变）——
    def set_col_align(self, col, halign):
        self._colkit.set_col_align(col, halign)

    def set_col_valign(self, col, valign):
        self._colkit.set_col_valign(col, valign)

    def toggle_wrap(self):
        return self._colkit.toggle_wrap()

    # 复制行走虚线框 / 选区实线环 / 表头右键列对齐 均由 self._colkit（TableColumnKit）承担，
    # KitTable 不再自带一份。_edit_cell 保留备用。

    def _edit_cell(self, row, col):
        """已改为 Excel 式双击内联编辑，此弹框编辑保留备用（当前不接线）。"""
        it = self.item(row, col)
        if it is None:
            return
        head = self.horizontalHeaderItem(col)
        title = head.text() if head else f"第 {col + 1} 列"
        dlg = QDialog(self.window())
        dlg.setWindowTitle(f"编辑单元格 · {title} · 第 {row + 1} 行")
        dlg.setModal(True)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(16, 16, 16, 16)
        v.setSpacing(10)
        editor = QPlainTextEdit()
        editor.setPlainText(it.text())
        editor.setMinimumSize(360, 160)
        v.addWidget(editor)
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        ok = QPushButton("确定")
        ok.setDefault(True)
        cancel.clicked.connect(dlg.reject)
        ok.clicked.connect(dlg.accept)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        v.addLayout(btns)
        _round_dialog(dlg)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            it.setText(editor.toPlainText())   # setText 不动对齐/换行标志


# --------------------------------------------------------------------
# 日期选择器变体（供选型）：紧凑内嵌日历卡片 + 现代换皮 QDateEdit
# --------------------------------------------------------------------
class CompactCalendar(QWidget):
    """内嵌式日历：不用点开，直接展开在页面里；顶部快捷「今天」。适合侧栏/弹窗。"""

    def __init__(self, date=None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"background:{C['card']};border:1px solid {C['border_popup']};"
            f"border-radius:{ui_kit.RADIUS['card']}px;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        self.cal = QCalendarWidget()
        self.cal.setGridVisible(False)
        self.cal.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.cal.setHorizontalHeaderFormat(
            QCalendarWidget.HorizontalHeaderFormat.SingleLetterDayNames)
        self.cal.setStyleSheet(_CAL_QSS)
        if date:
            self.cal.setSelectedDate(date)
        lay.addWidget(self.cal)
        row = QHBoxLayout()
        today = KitButton("今天", kind="ghost", obj_name="GhostBtn", explain=False)
        today.clicked.connect(lambda: self.cal.setSelectedDate(QDate.currentDate()))
        row.addWidget(today)
        row.addStretch(1)
        self.date_lbl = QLabel(date.toString("yyyy-MM-dd") if date else "")
        self.date_lbl.setStyleSheet(f"color:{C['primary']};font-weight:600;"
                                    "background:transparent;")
        self.cal.clicked.connect(lambda d: self.date_lbl.setText(d.toString("yyyy-MM-dd")))
        row.addWidget(self.date_lbl)
        lay.addLayout(row)


class ModernDateEdit(QDateEdit):
    """原生 QDateEdit 换皮：白底圆角、右侧日历小按钮点开日历弹层（保留键盘输入习惯）。"""

    def __init__(self, date=None, parent=None):
        super().__init__(parent)
        self.setCalendarPopup(True)
        self.setDate(date or QDate.currentDate())
        self.setDisplayFormat("yyyy 年 MM 月 dd 日")
        self.setStyleSheet(
            f"QDateEdit{{background:{C['card']};color:{C['text']};"
            f"border:1px solid {C['border']};border-radius:{ui_kit.RADIUS['md']}px;"
            f"padding:6px 8px;min-height:20px;}}"
            f"QDateEdit:focus{{border-color:{C['primary']};}}")
        cal = self.calendarWidget()
        if cal is not None:
            cal.setStyleSheet(_CAL_QSS)


class _FlowKpiCard(KpiCard):
    """画廊演示用：宽度钉成固定 W（供 FlowLayout 按此宽定位），高度只当“最小高”。
    原生 KpiCard 内部 value/name 两个 QLabel 与固定 96px 的 spark 会把自身最小宽撑到 ~310，
    setFixedWidth 压不动它（而 FlowLayout 又是按 sizeHint 定位）；这里显式覆盖 sizeHint
    把宽锁到目标宽，高度取 max(最小高, 内容自然高)——内容装不下时卡片会自己长高。
    （FlowLayout 按 sizeHint 定位、并用 sizeHint().height() 设行高，所以高度只要写进 sizeHint 即可自适应。）"""

    def __init__(self, w, min_h, name, icon, color, parent=None):
        super().__init__(name, icon, color, parent)
        self._fw, self._minh = w, min_h
        self.value.setMinimumWidth(0)     # 放开数字标签最小宽（spark 的最小宽由调用方放开）
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)

    def sizeHint(self):
        # 宽固定；高取“最小高”与“内容自然高”的较大值，让内容多时卡片自动长高
        natural = super().sizeHint().height()
        return QSize(self._fw, max(self._minh, natural))

    def minimumSizeHint(self):
        return QSize(self._fw, self._minh)


# --------------------------------------------------------------------
# 字段管理组件：左分类勾选 / 右流式拖拽排序（从任务中心抽进组件库，全站复用）
#   · 左：按 category 分组勾选，勾选即自动追加到右侧；也可按住 ☰ 拖到右侧落点插入
#   · 右：FlowLayout 按胶囊实际宽度自动换行排布；☰ 把手 + 跟随光标拖拽调序；× 移除
# 数据用 (id, label, category) 三元组，id 由调用方给（如逻辑列号），组件不解释语义。
# --------------------------------------------------------------------
_FM_MIME = "application/x-aigc-fieldmgr"


class _FMDrag:
    """拖拽跟随：按住超过阈值即把控件 grab 成图跟随光标拖出，MIME 携带 {src,id,index}。"""

    def _fm_press_ev(self, e):
        self._fm_press = (e.position().toPoint()
                          if e.button() == Qt.MouseButton.LeftButton else None)

    def _fm_try_drag(self, e, src, fid, index=0):
        if (getattr(self, "_fm_press", None) is not None
                and (e.buttons() & Qt.MouseButton.LeftButton)
                and (e.position().toPoint() - self._fm_press).manhattanLength()
                >= QApplication.startDragDistance()):
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(_FM_MIME, json.dumps(
                {"src": src, "id": fid, "index": index}).encode("utf-8"))
            drag.setMimeData(mime)
            pm = self.grab()      # 右侧整枚胶囊跟随光标（左侧已改纯勾选、无把手）
            drag.setPixmap(pm)
            drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
            self._fm_press = None
            drag.exec(Qt.DropAction.MoveAction)
            return True
        return False


class _FMOrderBar(QWidget):
    """右侧流式容器：装顺序胶囊；内部拖动调序，并在落点画一条闪烁竖线提示插入位。"""

    def __init__(self, mgr, parent=None):
        super().__init__(parent)
        self.mgr = mgr
        self.setObjectName("FMOrderBar")
        self.setAcceptDrops(True)
        self._flow = FlowLayout(self, hgap=6, vgap=8)
        self._flow.setContentsMargins(8, 8, 8, 8)
        # 拖拽落点指示：drag 期间按插入位画一条闪烁竖线（普通拖拽该有的反馈）
        self._drop_index = None
        self._blink_on = False
        self._blink = QTimer(self)
        self._blink.setInterval(420)
        self._blink.timeout.connect(self._tick_blink)

    def _tick_blink(self):
        self._blink_on = not self._blink_on
        self.update()

    def _chips(self):
        out = []
        for i in range(self._flow.count()):
            it = self._flow.itemAt(i)
            w = it.widget() if it is not None else None
            if isinstance(w, _FMOrderChip):
                out.append((i, w))
        return out

    def insert_index_at(self, pt):
        """把落点换算成扁平序列插入位：按行（top）分组，同行按 x。"""
        chips = self._chips()
        if not chips:
            return 0
        rows = {}
        for idx, c in chips:
            rows.setdefault(c.geometry().top(), []).append((idx, c))
        tops = sorted(rows)
        target = tops[0]
        for t in tops:
            if pt.y() >= t:
                target = t
            else:
                break
        row = rows[target]
        for idx, c in row:
            if pt.x() < c.geometry().center().x():
                return idx
        return row[-1][0] + 1

    def _caret_rect(self, index):
        """插入位 index → 落点竖线的几何（x=胶囊缝隙，y=该行胶囊上下沿）。"""
        cw = [c for _, c in self._chips()]
        m = self._flow.contentsMargins()
        if not cw:
            return QRectF(m.left(), m.top() + 2, 0, 18)
        if index < len(cw):
            g = cw[index].geometry()
            x = g.left() - self._flow.hgap / 2.0
        else:
            g = cw[-1].geometry()
            x = g.right() + self._flow.hgap / 2.0 + 1
        return QRectF(x, g.top(), 0, g.height())

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(_FM_MIME):
            self._blink.start()
            self._blink_on = True
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(_FM_MIME):
            idx = self.insert_index_at(e.position().toPoint())
            if idx != self._drop_index:
                self._drop_index = idx
                self._blink_on = True
                self.update()
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._end_marker()

    def dropEvent(self, e):
        pt = e.position().toPoint()
        self._end_marker()
        self.mgr.handle_drop(pt, e)
        e.acceptProposedAction()

    def _end_marker(self):
        self._blink.stop()
        self._drop_index = None
        self._blink_on = False
        self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drop_index is None or not self._blink_on:
            return
        r = self._caret_rect(self._drop_index)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(C["primary"]))
        pen.setWidthF(2.0)
        p.setPen(pen)
        p.drawLine(int(r.x()), int(r.top()), int(r.x()), int(r.bottom()))
        p.end()


class _FMOrderChip(_FMDrag, QFrame):
    """右侧一枚已选字段：[☰ 把手][名称][×]。整枚可拖（跟随光标）调序；× 移除并回勾左框。"""

    def __init__(self, fid, mgr, index, parent=None):
        super().__init__(parent)
        self.fid, self.mgr, self.index = fid, mgr, index
        self.setObjectName("FMChip")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(28)   # 真胶囊：高度固定 28，配合 QSS 的 14px 圆角＝两端全半圆
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 0, 8, 0)
        h.setSpacing(5)
        g = QLabel("☰")
        g.setObjectName("FMGrip")
        h.addWidget(g)
        t = QLabel(mgr.label_of(fid))
        t.setObjectName("FMChipText")
        h.addWidget(t)
        x = QPushButton("×")
        x.setObjectName("FMChipX")
        x.setFixedSize(15, 15)
        x.setCursor(Qt.CursorShape.PointingHandCursor)
        x.clicked.connect(lambda: mgr.remove(fid))
        h.addWidget(x)
        self._fm_press = None

    def mousePressEvent(self, e):
        self._fm_press_ev(e)
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._fm_try_drag(e, "order", self.fid, self.index):
            e.accept()
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._fm_press = None
        super().mouseReleaseEvent(e)


class FieldManager(QWidget):
    """字段管理：左分类勾选 / 右流式拖拽排序。字段用 (id,label,category)。"""

    changed = Signal()

    def __init__(self, fields, order, hidden, categories=None, size=(560, 360),
                 parent=None):
        super().__init__(parent)
        self.setObjectName("FieldManager")
        self._fields = list(fields)
        self._labels = {fid: lab for fid, lab, _ in self._fields}
        self._ids = [fid for fid, _, _ in self._fields]
        hid = set(hidden or ())
        self._order = [f for f in (order or []) if f in self._labels and f not in hid]
        if categories:
            self._groups = [(nm, [f for f in ids if f in self._labels])
                            for nm, ids in categories]
        else:
            bucket, seq = {}, []
            for fid, _, cat in self._fields:
                if cat not in bucket:
                    bucket[cat] = []
                    seq.append(cat)
                bucket[cat].append(fid)
            self._groups = [(c, bucket[c]) for c in seq]
        self._boxes = {}
        self._syncing = False

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        # ---------- 左：字段池（纯 QCheckBox 逐行勾选，无把手；圆角外框与右侧统一）----------
        left_host = QWidget()
        lv = QVBoxLayout(left_host)
        lv.setContentsMargins(12, 12, 12, 12)
        lv.setSpacing(10)
        for gname, ids in self._groups:
            gb = QGroupBox(gname)
            # 勾选框流式排布：从左往右一行摆多个，放不下自动换行（不要一排只放一个）
            gv = FlowLayout(gb, hgap=10, vgap=4)
            gv.setContentsMargins(2, 6, 2, 2)
            for fid in ids:
                if fid not in self._labels:
                    continue
                cb = QCheckBox(self.label_of(fid))
                cb.setCursor(Qt.CursorShape.PointingHandCursor)
                cb.toggled.connect(lambda on, f=fid: self.toggle(f, on))
                gv.addWidget(cb)
                self._boxes[fid] = cb
            lv.addWidget(gb)
        lv.addStretch(1)
        lscroll = QScrollArea()
        lscroll.setWidgetResizable(True)
        lscroll.setFrameShape(QScrollArea.Shape.NoFrame)
        lscroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lscroll.setStyleSheet(
            "QScrollArea,QScrollArea>QWidget>QWidget{background:transparent;border:none;}")
        lscroll.setWidget(left_host)
        frame_l = QFrame()
        frame_l.setObjectName("FMPool")
        frame_l.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        frame_l.setFixedWidth(220)
        fl = QVBoxLayout(frame_l)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.addWidget(lscroll)
        outer.addWidget(frame_l)

        # ---------- 右：显示顺序（流式胶囊 + 拖拽落点光标；圆角外框 + 竖向滚动看全）----------
        right = QVBoxLayout()
        right.setSpacing(0)
        self._bar = _FMOrderBar(self)
        self._right = QScrollArea()
        self._right.setWidgetResizable(True)
        self._right.setFrameShape(QScrollArea.Shape.NoFrame)
        self._right.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._right.setStyleSheet(
            "QScrollArea,QScrollArea>QWidget>QWidget{background:transparent;border:none;}")
        self._right.setWidget(self._bar)
        frame_r = QFrame()
        frame_r.setObjectName("FMOrderWrap")
        frame_r.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        fr = QVBoxLayout(frame_r)
        fr.setContentsMargins(0, 0, 0, 0)
        fr.addWidget(self._right)
        right.addWidget(frame_r, 1)
        outer.addLayout(right, 1)

        self._apply_style()
        self._sync_boxes()
        self._rebuild_bar()
        if size:
            self.setFixedSize(*size)   # 固定尺寸，不做自适应

    # ---------- 对外 ----------
    def label_of(self, fid):
        return self._labels.get(fid, str(fid))

    @property
    def order(self):
        return list(self._order)

    @property
    def hidden(self):
        return {f for f in self._ids if f not in self._order}

    def set_all(self, on):
        self._syncing = True
        for fid in self._ids:
            self._boxes[fid].setChecked(on)
        self._order = list(self._ids) if on else []
        self._syncing = False
        self._rebuild_bar()
        self.changed.emit()

    def reset(self, default_order, default_hidden):
        dh = set(default_hidden or ())
        self._syncing = True
        for fid in self._ids:
            self._boxes[fid].setChecked(fid not in dh)
        self._order = [f for f in (default_order or [])
                       if f in self._labels and f not in dh]
        self._syncing = False
        self._rebuild_bar()
        self.changed.emit()

    # ---------- 交互 ----------
    def toggle(self, fid, on):
        if self._syncing:
            return
        if on and fid not in self._order:
            self._order.append(fid)
        elif not on and fid in self._order:
            self._order.remove(fid)
        self._rebuild_bar()
        self.changed.emit()

    def remove(self, fid):
        if fid in self._order:
            self._order.remove(fid)
        b = self._boxes.get(fid)
        if b is not None:
            b.blockSignals(True)
            b.setChecked(False)
            b.blockSignals(False)
        self._rebuild_bar()
        self.changed.emit()

    def handle_drop(self, pt, event):
        try:
            payload = json.loads(bytes(event.mimeData().data(_FM_MIME)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        src, fid = payload.get("src"), payload.get("id")
        if fid not in self._labels:
            return
        pos = self._bar.insert_index_at(pt)
        if src == "pool":
            if fid in self._order:
                self._order.remove(fid)
            self._order.insert(max(0, min(pos, len(self._order))), fid)
            b = self._boxes.get(fid)
            if b is not None and not b.isChecked():
                b.blockSignals(True)
                b.setChecked(True)
                b.blockSignals(False)
        elif src == "order":
            frm = int(payload.get("index", 0))
            if 0 <= frm < len(self._order) and self._order[frm] == fid:
                self._order.pop(frm)
                if pos > frm:
                    pos -= 1
                self._order.insert(max(0, min(pos, len(self._order))), fid)
        self._rebuild_bar()
        self.changed.emit()
        event.acceptProposedAction()

    # ---------- 内部 ----------
    def _sync_boxes(self):
        self._syncing = True
        for fid in self._ids:
            self._boxes[fid].setChecked(fid in self._order)
        self._syncing = False

    def _rebuild_bar(self):
        while self._bar._flow.count():
            it = self._bar._flow.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.deleteLater()
        for i, fid in enumerate(self._order):
            self._bar._flow.addWidget(_FMOrderChip(fid, self, i))
        self._bar.updateGeometry()
        self._sync_order_height()

    def _sync_order_height(self):
        """右侧放在滚动容器里，FlowLayout 的高度靠 heightForWidth 手动喂给 min height，
        这样字段多时才会出现竖向滚动条（组件固定尺寸→宽度恒定，算一次即准）。"""
        w = self._right.viewport().width()
        if w <= 0:
            return
        self._bar.setMinimumHeight(max(self._bar._flow.heightForWidth(w), 40))

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_order_height()
        QTimer.singleShot(0, self._sync_order_height)

    def _apply_style(self):
        soft = ui_kit.rgba(C["primary"], 0.10)
        hover = ui_kit.rgba(C["primary"], 0.18)
        line = ui_kit.rgba(C["primary"], 0.35)
        card_r = ui_kit.RADIUS["card"]
        self.setStyleSheet(
            # 左字段池与右顺序区共用一套圆角外框（浅底 + 1px 边 + card 圆角），两边对称
            f"#FMPool,#FMOrderWrap{{background:{C['bg_field']};"
            f"border:1px solid {C['border']};border-radius:{card_r}px;}}"
            # 分类标题：外框已给边框，组内不再描边（避免双层框），只留标题文字；
            # margin-top 给标题留出一条带高（标题画在 margin 区），否则标题会压住首行勾选框
            f"#FieldManager QGroupBox{{border:none;background:transparent;"
            f"font-weight:600;color:{C['sub']};margin-top:18px;padding:0;}}"
            f"#FieldManager QGroupBox::title{{subcontrol-origin:margin;left:2px;padding:0;}}"
            f"#FieldManager QCheckBox{{color:{C['text']};font-size:13px;"
            f"background:transparent;spacing:6px;}}"
            f"#FMGrip{{color:{C['sub']};font-size:12px;background:transparent;}}"
            f"#FMOrderBar{{background:transparent;border:none;}}"
            f"#FMChip{{background:{soft};border:1px solid {line};"
            f"border-radius:{ui_kit.RADIUS['pill']}px;}}"
            f"#FMChip:hover{{background:{hover};border:1px solid {C['primary']};}}"
            f"#FMChip #FMChipText{{color:{C['text']};font-size:13px;"
            f"background:transparent;border:none;}}"
            f"#FMChipX{{background:transparent;border:none;color:{C['weak']};"
            f"font-weight:700;padding:0;}}"
            f"#FMChipX:hover{{color:{C['danger']};}}")
