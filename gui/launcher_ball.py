"""
gui/launcher_ball.py —— 唤出面板的桌面悬浮球

一颗常驻桌面、可拖拽、总在最前、不进任务栏的小圆球：点一下就弹起唤出搜索面板
（与全局热键、托盘菜单共用同一个 summon 入口）。给"不想每次都按键呼出、又懒得
把它挂进任务栏"的人——鼠标过去点一口即开，等价于 Spotlight/uTools 那颗托盘球。

设计要点（对齐 gui/speed_ball.py 的悬浮球套路）：
- 无边框 + 半透明 + Tool 窗：不抢焦点、不进任务栏、盖在别的软件上面；
- 按下-抬起没移动＝单击唤出，移动了＝拖拽换位置（同一只手两种意图，不冲突）；
- 位置存 app_state，重启还在老地方；关掉开关即收起，开关联动在设置页做；
- 纯自绘（paintEvent），不塞子控件，避开透明窗口的样式坑。
"""
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget, QApplication, QMenu

#: 折叠直径（与下载悬浮球一致的量级，够点又不挡视线）
_BALL = 60
_BG = QColor(28, 32, 38, 232)          # 与唤出面板同款深灰半透明
_RING = QColor(51, 112, 255, 200)      # 品牌蓝描边
_GLYPH = QColor("#E8EAED")


class LauncherBall(QWidget):
    """桌面悬浮球：单击 on_click（一般是弹唤出面板），可拖拽，右键可收起。"""

    def __init__(self, on_click, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 不激活：点球时别把面板的焦点抢乱（面板自己会 activateWindow）
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._on_click = on_click
        self._drag = None
        self._moved = False
        self._font = QFont("Microsoft YaHei")
        self.setToolTip("点我打开搜索 · 可拖动 · 右键关闭")
        self.setFixedSize(_BALL, _BALL)

    # ---------- 对外 ----------
    def show_ball(self):
        """按上次保存的位置出现（没有就贴右侧偏上）。"""
        self._restore_pos()
        self.show()

    def stop(self):
        self.hide()
        self.deleteLater()

    # ---------- 位置记忆 ----------
    def _restore_pos(self):
        try:
            from store import app_state
            pos = app_state.get("launcher_ball_pos")
            if isinstance(pos, (list, tuple)) and len(pos) == 2:
                self.move(int(pos[0]), int(pos[1]))
                return
        except Exception:
            pass
        scr = QApplication.primaryScreen()
        geo = scr.availableGeometry() if scr else None
        if geo:
            self.move(geo.right() - _BALL - 24, geo.top() + int(geo.height() * 0.2))

    def _save_pos(self):
        try:
            from store import app_state
            app_state.set_value("launcher_ball_pos", [self.x(), self.y()])
        except Exception:
            pass

    # ---------- 拖拽 / 点击 ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.position().toPoint()
            self._moved = False

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            delta = e.position().toPoint() - self._drag
            if delta.manhattanLength() > 4:
                self._moved = True
                self.move(self.pos() + delta)
                self._drag = e.position().toPoint()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if self._moved:
                self._save_pos()
            else:
                self._summon()
        self._drag = None

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        act_open = menu.addAction("🔍 打开搜索")
        act_hide = menu.addAction("✕ 关闭悬浮球")
        chosen = menu.exec(e.globalPos())
        if chosen is act_open:
            self._summon()
        elif chosen is act_hide:
            try:
                from store import app_state
                app_state.set_value("launcher_ball_on", False)
            except Exception:
                pass
            hide_launcher_ball()

    def _summon(self):
        cb = self._on_click
        if callable(cb):
            cb()

    # ---------- 自绘 ----------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        m = 3
        rect = self.rect().adjusted(m, m, -m, -m)
        p.setPen(QPen(_RING, 2))
        p.setBrush(_BG)
        p.drawEllipse(rect)
        self._font.setPointSize(20)
        p.setFont(self._font)
        p.setPen(_GLYPH)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "🔍")
        p.end()


# ---- 全局单例：同一时刻只留一颗球 ----
_ball_ref = None


def show_launcher_ball(on_click, parent=None):
    """弹出（并替换）悬浮球，返回实例。"""
    global _ball_ref
    hide_launcher_ball()
    ball = LauncherBall(on_click, parent)
    _ball_ref = ball
    ball.show_ball()
    return ball


def hide_launcher_ball():
    """收起悬浮球（关开关时调）。"""
    global _ball_ref
    if _ball_ref is not None:
        b, _ball_ref = _ball_ref, None
        b.stop()
