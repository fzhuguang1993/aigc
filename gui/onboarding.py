"""
gui/onboarding.py —— 首次进入某功能的分步高亮向导（spotlight tour）

沿用任务中心 _GuideBubble 同源、已在生产验证的「无边框卡片贴在目标旁」思路，
抽成通用件，供数据中台 / 拆解任务 / 爆款拆解 / 素材提取 / 屏幕录制复用
（任务中心沿用其自带向导，不必接这里）。

刻意做成非侵入，鲁棒优先：
- 卡片与高亮环都是无边框 Tool 顶层窗、置顶，不抢鼠标、不弹模态，绝不挡住正常使用；
- 高亮环只是一个套住目标控件的描边框（对鼠标透明），不是全屏遮罩——
  万一取不到目标或定位失败，退化成居中提示卡，向导照样走得下去；
- 任一步目标为 None / 未显示 → 跳过该步；整体 try 包裹，异常直接收尾关窗；
- 看过一次写 app_state['seen_<feature>']，之后不再自动弹；提供 reset 便于重播。
"""
from PySide6.QtCore import Qt, QRect, QRectF, QPoint, Signal, QObject
from PySide6.QtGui import QColor, QPainter, QPen, QGuiApplication
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QFrame)

from store import app_state
from gui.theme import tokenize

_ACCENT = "#3370FF"
_RING_PAD = 5                # 描边框比目标四周各外扩这么多像素
_TOPFLAGS = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
             | Qt.WindowType.WindowStaysOnTopHint)
# 常驻引用：顶层窗无 C++ 父级，Python 对象一旦被 GC 窗口就消失；向导进行期间
# 把控制器挂在这里，收尾时移除，避免“卡片刚弹出来就被回收”。
_ACTIVE = set()


class _FocusRing(QWidget):
    """目标控件外围的高亮描边框：透明背景、对鼠标透明，只画一圈圆角边框。"""

    def __init__(self):
        super().__init__(None, _TOPFLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(_ACCENT))
        pen.setWidth(3)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        p.drawRoundedRect(r, 8, 8)
        p.end()


class _TourCard(QFrame):
    """步骤提示卡：标题 + 正文 + 上一步/下一步(进度)/跳过。无边框 Tool 窗内容。"""
    prev = Signal()
    next = Signal()
    skip = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, _TOPFLAGS)
        self.setStyleSheet(tokenize(f"QFrame#TourCard{{background:#FFFFFF;"
                           f"border:1px solid {_ACCENT};border-radius:10px;}}"))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)
        self.head = QLabel("")
        self.head.setStyleSheet(f"color:{_ACCENT};font-weight:bold;font-size:13px;")
        lay.addWidget(self.head)
        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setMinimumWidth(300)
        self.body.setMaximumWidth(360)
        lay.addWidget(self.body)
        row = QHBoxLayout()
        self.b_prev = QPushButton("上一步")
        self.b_prev.setObjectName("GhostBtn")
        self.b_skip = QPushButton("跳过")
        self.b_skip.setObjectName("GhostBtn")
        self.b_next = QPushButton("下一步")
        row.addStretch(1)
        row.addWidget(self.b_prev)
        row.addWidget(self.b_skip)
        row.addWidget(self.b_next)
        lay.addLayout(row)
        self.b_prev.clicked.connect(self.prev)
        self.b_next.clicked.connect(self.next)
        self.b_skip.clicked.connect(self.skip)

    def set_text(self, title, text, idx, total):
        self.head.setText(f"功能引导 · {idx}/{total}　{title}")
        self.body.setText(text)
        self.b_prev.setVisible(idx > 1)
        self.b_next.setText("开始使用" if idx >= total else "下一步")


class SpotlightTour(QObject):
    """把一串步骤（目标控件 getter + 标题 + 文案）走成分步高亮向导。

    steps：[(target, title, text), ...]；target 可为 QWidget、callable()->QWidget、
    或 None。取不到/不可见的目标会退化为居中卡（不画环），仍继续该步。"""

    def __init__(self, window, feature, steps, on_finish=None):
        super().__init__(window)
        self._win = window
        self._feature = feature
        self._steps = [s for s in (steps or []) if s]
        self._on_finish = on_finish
        self._i = 0
        self._ring = None
        self._card = None

    # ---- 生命周期 ----
    def start(self):
        if not self._steps:
            return self._finish(False)
        self._show(0)

    def _resolve(self, target):
        """把 step 的 target 归一成可见的 QWidget，否则 None。"""
        try:
            w = target() if callable(target) else target
        except Exception:
            return None
        if isinstance(w, QWidget) and w.isVisible():
            return w
        return None

    def _global_rect(self, w):
        g = w.mapToGlobal(QPoint(0, 0))
        return QRect(g, w.size())

    def _show(self, i):
        self._i = i
        self._cleanup_widgets()
        if i >= len(self._steps):
            return self._finish(True)
        target, title, text = self._steps[i]
        total = len(self._steps)
        tgt = self._resolve(target)
        card = _TourCard()
        card.set_text(title, text, i + 1, total)
        card.prev.connect(lambda: self._show(self._i - 1))
        card.next.connect(lambda: self._show(self._i + 1))
        card.skip.connect(self._on_skip)
        self._card = card
        scr = QGuiApplication.primaryScreen().availableGeometry()
        if tgt is not None:
            rect = self._global_rect(tgt)
            ring_geo = rect.adjusted(-_RING_PAD, -_RING_PAD, _RING_PAD, _RING_PAD)
            if ring_geo.intersects(scr):
                ring = _FocusRing()
                ring.setGeometry(ring_geo)
                ring.show()
                ring.raise_()
                self._ring = ring
            card.adjustSize()
            ax, ay = rect.left(), rect.bottom() + 10
            if ay + card.height() > scr.bottom():            # 下方放不下就翻到上方
                ay = rect.top() - card.height() - 10
        else:
            card.adjustSize()
            ax = scr.center().x() - card.width() // 2
            ay = scr.center().y() - card.height() // 2
        x = max(scr.left() + 8, min(ax, scr.right() - card.width() - 8))
        y = max(scr.top() + 8, min(ay, scr.bottom() - card.height() - 8))
        card.move(x, y)
        card.show()
        card.raise_()

    def _cleanup_widgets(self):
        for w in (self._card, self._ring):
            if w is not None:
                w.close()
        self._card = None
        self._ring = None

    def _on_skip(self):
        self._finish(False)

    def _finish(self, completed):
        self._cleanup_widgets()
        if completed or True:                 # 看过（含跳过）都算见过，不再自动弹
            app_state.set_value(f"seen_{self._feature}", True)
        _ACTIVE.discard(self)
        if self._on_finish:
            try:
                self._on_finish()
            except Exception:
                pass


# ---- 对外便捷接口 ----
def seen(feature):
    return bool(app_state.get(f"seen_{feature}"))


def reset(feature):
    """清除某功能的“已看过”标记（可在设置/帮助页挂“重播引导”入口）。"""
    app_state.set_value(f"seen_{feature}", False)


def maybe_run(window, feature, steps, on_finish=None):
    """未看过则起一个向导并返回控制器；已看过或无步骤返回 None。

    调用方无须持有返回值——本模块用 _ACTIVE 维持引用防 GC。异常一律吞掉，
    引导是锦上添花，绝不能因其失败影响主功能。"""
    try:
        if seen(feature):
            return None
        tour = SpotlightTour(window, feature, steps, on_finish)
        _ACTIVE.add(tour)
        tour.start()
        return tour
    except Exception:
        return None
