"""
gui/launcher_ball.py —— 唤出面板的桌面悬浮球（实时网速版）

一颗常驻桌面、可拖拽、总在最前、不进任务栏的小圆球：鼠标移上去沿弧线放射出功能
扇形（与全局热键、托盘菜单共用同一个 summon 入口），移开自动收起。球面不再画搜索
放大镜，而是显示本机实时的上行/下行网速，配一圈随流量呼吸、旋转的"活动环"——
没有流量时安静待机，有流量时环越亮、转得越起劲，看着更有科技感。

为什么自绘、不塞子控件：透明置顶小窗里塞子控件会被原生样式在 macOS/Windows 之间
画得各不相同（同款坑见 gui/speed_ball.py 的注释）。整窗 WA_TranslucentBackground，
paintEvent 一把画球底 + 活动环 + 上下行速率，跨平台一致。

交互口径（本版）：
- 移入展开、移出收起：hover 是唯一唤起方式（单击不再承担展开），拖拽/右键保留；
  球与扇形是两个顶层窗，收起统一由调用方（main_window）用"宽限期计时器"裁定，
  避免鼠标从球穿到扇形途中被球的一条 Leave 误收（这就是以前"忽开忽关"的根因）；
- 拖拽跟手：按下时记"全局按下点相对窗口左上角"的偏移，移动用绝对定位
  move(global - off)，不再用逐帧增量累加——增量法在快速拖动时会被合并掉事件、丢帧，
  表现为"球落在鼠标后面不跟手"；
- 网速取数走 core 之外的可选依赖 psutil：没装（精简包）时优雅降级为待机 0 值，
  绝不让一颗装饰球把主程序带崩。
"""
import math
import time
from collections import deque

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import (QColor, QFont, QPainter, QPen, QLinearGradient,
                           QRadialGradient, QPolygonF, QPainterPath)
from PySide6.QtWidgets import QWidget, QApplication, QMenu

#: 画布边长：比球体大一圈，四周留出柔光铺展的空间（发光淡到透明后再到边界，
#: 不会被裁成方框——这正是上一版"丑陋方框线"的根因）
_BALL = 108
#: 画布四周留白：球半径 = 半边长 - 留白，柔光最远铺到半边长（窗口边界）处已全透明
_PAD = 16
_BG = QColor(20, 24, 32, 225)          # 深底半透明（与下载悬浮球同款观感）
_RING_IDLE = QColor(120, 132, 150, 70)  # 待机环：几乎透明的冷灰
_UP = QColor(51, 112, 255, 255)       # 上行：品牌蓝
_DOWN = QColor(46, 204, 160, 255)     # 下行：科技绿
_GLYPH = QColor("#EAF0F6")
_DIM = QColor(255, 255, 255, 150)

#: 采样/动画节奏：速率一秒够看；环的旋转/呼吸用更快的定时器抹平
_SAMPLE_MS = 1000
_ANIM_MS = 40
#: 把速率映射成"环的活跃度 0~1"的参考天花板（B/s）：到这里环最亮、转满速
_LEVEL_CEIL = 4 * 1024 * 1024
#: 折线波形的历史采样格数（每帧推进一格，像音频电平那样从右往左滚）
_WAVE_N = 40


def fmt_rate(bps):
    """字节/秒 → 紧凑串（球面很窄，尽量短）；无流量给 '—'。"""
    if not bps or bps <= 1:
        return "—"
    kb = bps / 1024.0
    if kb < 1024:
        return "%.0fK" % kb
    mb = kb / 1024.0
    return "%.1fM" % mb if mb < 100 else "%.0fM" % mb


class NetSpeedometer:
    """盯网卡累计字节数，两次采样差 ÷ 时间差 = 上下行速率。纯逻辑、无 Qt、可单测。

    没装 psutil 时 net_io_counters 为 None，poll() 恒返 (0.0, 0.0)，界面显示待机。"""

    def __init__(self):
        try:
            import psutil
            self._io = psutil.net_io_counters
        except Exception:
            self._io = None
        self._last_ts = None
        self._last = (0, 0)             # 上次累计 (bytes_sent, bytes_recv)

    def poll(self):
        """返回 (up_bps, down_bps)。首次/取不到都给 0，不抛。"""
        if self._io is None:
            return 0.0, 0.0
        try:
            c = self._io()
            cur = (c.bytes_sent, c.bytes_recv)
        except Exception:
            return 0.0, 0.0
        now = time.monotonic()
        if self._last_ts is None:
            self._last_ts, self._last = now, cur
            return 0.0, 0.0
        dt = now - self._last_ts
        if dt <= 0:
            return 0.0, 0.0
        up = max(cur[0] - self._last[0], 0) / dt
        down = max(cur[1] - self._last[1], 0) / dt
        # 累计计数器换网卡/重启会回绕变小：这时按 0 处理，别让速率冒一个假尖峰
        if cur[0] < self._last[0] or cur[1] < self._last[1]:
            up = down = 0.0
        self._last_ts, self._last = now, cur
        return up, down


class LauncherBall(QWidget):
    """桌面悬浮球：移入 on_hover（展开扇形）、移出 on_leave（请求收起），
    双击 on_double（打开主程序），可拖拽，右键可关闭/打开搜索/进索引设置。"""

    def __init__(self, on_hover, on_double=None, on_search=None,
                 on_config=None, on_index=None, on_leave=None,
                 on_close=None, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 不激活：悬停/点球时别抢主界面焦点（唤出面板自己会 activateWindow）
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._on_hover = on_hover
        self._on_double = on_double
        self._on_search = on_search
        self._on_config = on_config
        self._on_index = on_index
        self._on_leave = on_leave
        self._on_close = on_close

        # 拖拽：按下点相对窗口左上角的偏移（绝对定位法，见模块注释）
        self._drag_off = None
        self._moved = False
        # 双击判定：第一下抬起挂一个待定定时器，到点前再有一下＝双击，否则什么也不做
        # （本版单击不担责，展开交给 hover）
        self._pending = False
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._on_pending_timeout)

        # 速率采样 + 环动画
        self._meter = NetSpeedometer()
        self._up = 0.0
        self._down = 0.0
        self._level = 0.0               # 当前平滑后的活跃度 0~1（驱动环亮度/转速）
        self._phase = 0.0               # 活动环旋转角（度）
        # hover 发光：移入时置真，_glow 向 1 缓动（与网速无关，保证“移入就亮”）
        self._hovered = False
        self._glow = 0.0
        # 上下行折线波形的历史缓冲（归一化 0~1），每帧 append 新值、自动左移
        self._wave_down = deque(maxlen=_WAVE_N)
        self._wave_up = deque(maxlen=_WAVE_N)
        self._font = QFont("Microsoft YaHei")

        self._sample_timer = QTimer(self)
        self._sample_timer.setInterval(_SAMPLE_MS)
        self._sample_timer.timeout.connect(self._sample)
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(_ANIM_MS)
        self._anim_timer.timeout.connect(self._animate)

        self.setFixedSize(_BALL, _BALL)
        self.setToolTip("移入展开功能 · 双击打开主程序 · 可拖动 · 右键更多")

    # ---------- 对外 ----------
    def show_ball(self):
        """按上次保存的位置出现（没有就贴右侧偏上），并开始采样/动画。"""
        self._restore_pos()
        # 先把两条波形缓冲铺平（无流量时先画一条水平中心线，不至于开局空）
        if len(self._wave_down) < _WAVE_N:
            self._wave_down = deque([0.0] * _WAVE_N, maxlen=_WAVE_N)
            self._wave_up = deque([0.0] * _WAVE_N, maxlen=_WAVE_N)
        self.show()
        self._sample()
        self._sample_timer.start()
        self._anim_timer.start()

    def stop(self):
        self._sample_timer.stop()
        self._anim_timer.stop()
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

    # ---------- 速率采样 / 环动画 ----------
    def _sample(self):
        self._up, self._down = self._meter.poll()

    def _animate(self):
        # 活跃度：总流量归一到 0~1，向目标做指数平滑（约 0.25 步长），别一格一跳
        dn = min(self._down / float(_LEVEL_CEIL), 1.0)
        up = min(self._up / float(_LEVEL_CEIL), 1.0)
        self._level += (min(dn + up, 1.0) - self._level) * 0.25
        if self._level < 0.001:
            self._level = 0.0
        # hover 发光：不管有没有流量，移入就把 _glow 拉满、移出回到 0
        self._glow += ((1.0 if self._hovered else 0.0) - self._glow) * 0.18
        # 折线波形：每帧推进一个新采样（从右往左滚），无流量时自然回平
        self._wave_down.append(dn)
        self._wave_up.append(up)
        # 环转速随活跃度加快：静止 0.6°/帧，满载约 6°/帧
        self._phase = (self._phase + 0.6 + self._level * 5.4) % 360.0
        self.update()

    # ---------- hover：移入展开 / 移出收起 ----------
    def enterEvent(self, e):
        # 悬停即唤起（收起由调用方在球 Leave、扇形也 Leave 时统一定夺）
        self._set_hover(True)
        self._fire_hover()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._set_hover(False)
        self._fire_leave()
        super().leaveEvent(e)

    def _set_hover(self, on):
        """记录鼠标是否移入（驱动周圈发光，与网速无关），由 _animate 缓动到目标。"""
        self._hovered = bool(on)

    def _fire_hover(self):
        self._call(self._on_hover)

    def _fire_leave(self):
        self._call(self._on_leave)

    # ---------- 拖拽 / 双击 ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            gp = e.globalPosition().toPoint()
            self._drag_off = gp - self.pos()      # 记下"按下点相对窗口左上角"的偏移
            self._moved = False

    def mouseMoveEvent(self, e):
        if self._drag_off is not None:
            gp = e.globalPosition().toPoint()
            if (gp - self._drag_off - self.pos()).manhattanLength() > 4:
                self._moved = True
            if self._moved:
                # 绝对定位：窗口左上角 = 当前全局点 - 固定偏移，逐帧都跟手
                self.move(gp - self._drag_off)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if self._moved:
                # 拖动不算点击：存位置、抹掉待定双击
                self._save_pos()
                self._pending = False
                self._click_timer.stop()
            elif self._pending:
                # 已有待定第一下 → 这是第二下：双击
                self._click_timer.stop()
                self._pending = False
                self._fire_double()
            else:
                # 第一下：挂待定，等第二下（阈值内）判双击
                self._pending = True
                self._click_timer.start(QApplication.doubleClickInterval())
        self._drag_off = None

    def _on_pending_timeout(self):
        # 待定到点没等到第二下：本版单击不担责，只清状态
        self._pending = False

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
            self._hide_requested()

    def _hide_requested(self):
        """右键「关闭悬浮球」：不能只 hide 球自己。

        扇形菜单是另一个顶层窗，不归球管：关了球不收扇形，就会在桌面上留一排没人
        认领的图标（用户报的“关了悬浮球，展开的内容居然还没关闭”）。交给调用方的
        on_close（主窗口那里顺手把扇形与宽限期计时器一并抹掉）；没传回调时退回
        “只收球”的老行为，至少不能抛。"""
        cb = self._on_close
        if callable(cb):
            self._call(cb)
            return
        try:
            from store import app_state
            app_state.set_value("launcher_ball_on", False)
        except Exception:
            pass
        hide_launcher_ball()

    def _call(self, cb):
        if callable(cb):
            cb()

    def _fire_double(self):
        cb = self._on_double
        self._call(cb)

    def _fire_search(self):
        cb = self._on_search
        # 右键"打开搜索"：优先用专用搜索回调
        self._call(cb)

    # ---------- 自绘 ----------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self.rect().center()
        cx0, cy0 = float(c.x()), float(c.y())
        # 按实际窗口半边长算球半径（不写死 _BALL）：留出 _PAD 给外圈柔光铺展，
        # 保证发光淡到全透明后才抵达边界——不会再被裁成方框。
        half = min(self.width(), self.height()) / 2.0
        R = max(half - _PAD, 8.0)
        hover = self._glow

        # 1) 落影：球底一团柔和暗光，把球"抬"离桌面（治"太扁平"的第一步）
        shadow = QRadialGradient(QPointF(cx0, cy0 + R * 0.52), R * 1.05)
        shadow.setColorAt(0.0, QColor(0, 0, 0, 95))
        shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(shadow)
        p.drawEllipse(QPointF(cx0, cy0 + R * 0.46), R * 0.96, R * 0.66)

        # 2) 移入光晕：用径向渐变的柔光（青蓝），到边缘 alpha 淡到 0——绝不再有方框
        if hover > 0.01:
            aura_r = R + _PAD * (0.35 + 0.65 * hover)
            halo = QRadialGradient(QPointF(cx0, cy0), aura_r)
            core = QColor(96, 205, 255)
            core.setAlpha(int(30 + 110 * hover))
            mid = QColor(96, 205, 255)
            mid.setAlpha(int(70 * hover))
            halo.setColorAt(0.0, core)
            halo.setColorAt(min(R / aura_r, 0.98), mid)
            halo.setColorAt(1.0, QColor(96, 205, 255, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(halo)
            p.drawEllipse(QPointF(cx0, cy0), aura_r, aura_r)

        # 3) 球体：三段径向渐变（左上高光→中调→底部暗边），拉出球面立体感
        body = QRadialGradient(QPointF(cx0 - R * 0.38, cy0 - R * 0.44), R * 1.95)
        body.setColorAt(0.0, QColor(82, 98, 128, 255))
        body.setColorAt(0.45, QColor(36, 45, 63, 250))
        body.setColorAt(1.0, QColor(7, 10, 17, 250))
        p.setPen(QPen(_RING_IDLE, 1.5))
        p.setBrush(body)
        p.drawEllipse(QPointF(cx0, cy0), R, R)

        # 4) 内圈暗角：靠边一圈压暗，边缘收拢更像球
        vig = QRadialGradient(QPointF(cx0, cy0), R)
        vig.setColorAt(0.0, QColor(0, 0, 0, 0))
        vig.setColorAt(0.72, QColor(0, 0, 0, 0))
        vig.setColorAt(1.0, QColor(0, 0, 0, 70))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(vig)
        p.drawEllipse(QPointF(cx0, cy0), R, R)

        # 5) 顶部镜面高光：上亮下透明的渐变反光，玻璃质感
        gloss = QRadialGradient(QPointF(cx0 - R * 0.22, cy0 - R * 0.52), R * 0.95)
        gloss.setColorAt(0.0, QColor(255, 255, 255, 78))
        gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(gloss)
        p.drawEllipse(QPointF(cx0 - R * 0.16, cy0 - R * 0.50), R * 0.60, R * 0.42)

        # 6) 雷达扫描弧：随流量变亮变长，末端一颗亮点沿弧游走
        glow = max(self._level, hover)
        span = 55 + int(140 * self._level)
        ri = R - 2.5
        arc = QRectF(cx0 - ri, cy0 - ri, ri * 2, ri * 2)
        grad = QLinearGradient(arc.topLeft(), arc.bottomRight())
        c1 = QColor(_UP); c1.setAlpha(int(70 + 160 * glow))
        c2 = QColor(_DOWN); c2.setAlpha(int(70 + 160 * glow))
        grad.setColorAt(0.0, c1)
        grad.setColorAt(1.0, c2)
        pen = QPen(grad, 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(arc, int(-self._phase * 16), int(-span * 16))
        # 扫描前沿亮头：弧的尾端那颗会随 phase 转动的小光点
        a_end = math.radians(-self._phase - span)
        hx, hy = cx0 + ri * math.cos(a_end), cy0 - ri * math.sin(a_end)
        dot = QColor(200, 245, 255); dot.setAlpha(int(90 + 150 * glow))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(dot)
        p.drawEllipse(QPointF(hx, hy), 2.4, 2.4)

        # 7) 横穿的折线波形（音频电平式）
        self._paint_wave(p, cx0, cy0, R)

        # 8) 速率角标：往球心方向收到 0.6R，不再贴着圆周边缘（治"字太靠边"）
        self._font.setBold(True)
        self._font.setPointSize(9)
        p.setFont(self._font)
        inset = R * 0.60
        p.setPen(_DOWN)
        p.drawText(QRectF(cx0 - R, cy0 - inset - 7, R * 2, 14),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   "↓ " + fmt_rate(self._down))
        p.setPen(_UP)
        p.drawText(QRectF(cx0 - R, cy0 + inset - 7, R * 2, 14),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   "↑ " + fmt_rate(self._up))
        p.end()

    def _paint_wave(self, p, cx0, cy0, R):
        """横穿球心的上下行折线波形（音频电平式）。

        两个关键改进让它真正“看得见”：
        ① 用开方做感知放大——正常浏览只有几十~几百 KB/s，归一到 4MB 天花板后几乎
          是 0，不放大就是一条死平线（上一版“没效果”的原因）；开方后小流量也抬得起。
        ② 叠一圈随相位起伏的待机微动——没流量时也是一条轻微跳动、缓缓左滚的电平线，
          而不是静止直线，看着是“活的”。下行在中线上方（绿）、上行下方（蓝），裁进圆内。"""
        wd = list(self._wave_down)
        wu = list(self._wave_up)
        if not wd:
            return
        n = _WAVE_N
        half_w = R * 0.76
        amp = R * 0.50
        left = cx0 - half_w
        step = (2 * half_w) / (n - 1)
        clip = QPainterPath()
        clip.addEllipse(QPointF(cx0, cy0), R - 1, R - 1)
        p.save()
        p.setClipPath(clip)
        for series, col, up in ((wd, _DOWN, False), (wu, _UP, True)):
            poly = QPolygonF()
            for i in range(n):
                v = series[i] if i < len(series) else 0.0
                v = math.sqrt(v) if v > 0 else 0.0     # 感知放大
                ambient = 0.06 * (0.5 + 0.5 * math.sin(i * 0.55 - self._phase * 0.16))
                x = left + step * i
                dy = (v * 0.9 + ambient) * amp
                poly.append(QPointF(x, cy0 + dy if up else cy0 - dy))
            pen = QPen(col, 2)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPolyline(poly)
        p.restore()


# ---- 全局单例：同一时刻只留一颗球 ----
_ball_ref = None


def show_launcher_ball(on_hover, on_double=None, on_search=None,
                       on_config=None, on_index=None, on_leave=None,
                       on_close=None, parent=None):
    """弹出（并替换）悬浮球，返回实例。

    on_hover：鼠标移入（展开扇形）；on_leave：移出（请求收起，宽限期由调用方裁定）；
    on_double：双击（打开主程序）；on_search/on_config/on_index：右键菜单三入口；
    on_close：右键“关闭悬浮球”——由调用方把球、扇形、待收起计时器一并收干净。"""
    global _ball_ref
    hide_launcher_ball()
    ball = LauncherBall(on_hover, on_double, on_search, on_config, on_index,
                        on_leave, on_close, parent)
    _ball_ref = ball
    ball.show_ball()
    return ball


def hide_launcher_ball():
    """收起悬浮球（关开关时调）。"""
    global _ball_ref
    if _ball_ref is not None:
        b, _ball_ref = _ball_ref, None
        b.stop()
