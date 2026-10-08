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
from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget, QApplication, QMenu

#: 折叠直径（与下载悬浮球一致的量级，够点又不挡视线）
_BALL = 60
_BG = QColor(28, 32, 38, 232)          # 与唤出面板同款深灰半透明
_RING = QColor(51, 112, 255, 200)      # 品牌蓝描边
_GLYPH = QColor("#E8EAED")


class LauncherBall(QWidget):
    """桌面悬浮球：单击 on_single（展开扇形），双击 on_double（打开主程序），
    可拖拽，右键可收起/打开搜索/进索引设置。"""

    def __init__(self, on_single, on_double=None, on_search=None,
                 on_config=None, on_index=None, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 不激活：点球时别把面板的焦点抢乱（面板自己会 activateWindow）
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._on_single = on_single
        self._on_double = on_double
        self._on_search = on_search
        self._on_config = on_config
        self._on_index = on_index
        self._drag = None
        self._moved = False
        # 单击/双击判定：第一次抬起先挂一个“待定单击”定时器（间隔=系统双击阈值），
        # 到点前没有第二下就当真单击；来了第二下就取消单击、改判双击。这样“展开扇形”
        # 与“打开整个软件”能共用一只手而不冲突，也不依赖 Qt 自己的 DDouble 事件。
        self._pending_single = False
        self._expect_double = False
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._fire_single)
        self._font = QFont("Microsoft YaHei")
        self.setToolTip("单击展开功能扇形 · 双击打开主程序 · 可拖动 · 右键更多")
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

    # ---------- 拖拽 / 单击 / 双击 ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.position().toPoint()
            self._moved = False
            if self._pending_single:
                # 挂起的单击未到期又来了按下 → 这是双击的第二下：停表、改判双击
                self._click_timer.stop()
                self._pending_single = False
                self._expect_double = True

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
                # 拖动不算点击：存位置、把挂起的单击/双击都抹干净
                self._save_pos()
                self._pending_single = False
                self._expect_double = False
                self._click_timer.stop()
            elif self._expect_double:
                self._expect_double = False
                self._fire_double()
            else:
                # 第一次抬起：先挂一个待定单击，等第二下（阈值内）区分单击/双击
                self._pending_single = True
                self._click_timer.start(QApplication.doubleClickInterval())
        self._drag = None

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        act_open = menu.addAction("🔍 打开搜索")
        act_index = menu.addAction("🗂 索引设置")
        act_cfg = menu.addAction("⚙ 功能项设置…")
        menu.addSeparator()
        act_hide = menu.addAction("✕ 关闭悬浮球")
        chosen = menu.exec(e.globalPos())
        if chosen is act_open:
            self._fire_search()
        elif chosen is act_index:
            self._call(self._on_index)
        elif chosen is act_cfg:
            self._call(self._on_config)
        elif chosen is act_hide:
            try:
                from store import app_state
                app_state.set_value("launcher_ball_on", False)
            except Exception:
                pass
            hide_launcher_ball()

    def _call(self, cb):
        if callable(cb):
            cb()

    def _fire_single(self):
        self._pending_single = False
        self._call(self._on_single)

    def _fire_double(self):
        cb = self._on_double
        # 没配双击动作时退而求其次：双击也走单击（至少不“点了没反应”）
        self._call(cb if callable(cb) else self._on_single)

    def _fire_search(self):
        cb = self._on_search
        # 右键“打开搜索”：优先用专用的搜索回调，没配就回退到单击（展开扇形里含搜索）
        self._call(cb if callable(cb) else self._on_single)

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


def show_launcher_ball(on_single, on_double=None, on_search=None,
                       on_config=None, on_index=None, parent=None):
    """弹出（并替换）悬浮球，返回实例。

    on_single：单击（展开扇形）；on_double：双击（打开主程序）；on_search/on_config/
    on_index：右键菜单的三个入口。向后兼容：只传一个位置参时当作 on_single。"""
    global _ball_ref
    hide_launcher_ball()
    ball = LauncherBall(on_single, on_double, on_search, on_config, on_index, parent)
    _ball_ref = ball
    ball.show_ball()
    return ball


def hide_launcher_ball():
    """收起悬浮球（关开关时调）。"""
    global _ball_ref
    if _ball_ref is not None:
        b, _ball_ref = _ball_ref, None
        b.stop()
