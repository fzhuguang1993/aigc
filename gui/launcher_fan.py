"""
gui/launcher_fan.py —— 悬浮球单击展开的扇形（环形放射）功能菜单

一颗悬浮球只能做一件事太浪费：点球不是直接弹搜索，而是沿弧线放射出一圈功能入口
（搜索面板 / 常用工具 / 各页面），点哪颗走哪条；末尾一颗 "+" 就地加项。数据存
app_state（launcher_fan_items），可在设置页统一管理，也能在扇形上右键移除。

为什么自绘、不塞子控件：透明置顶小窗里塞 QPushButton/QLabel 会被原生样式在
macOS/Windows 之间画得各不相同（同款坑见 gui/launcher_ball.py 的注释）。这里整
个窗口 WA_TranslucentBackground，paintEvent 一把画圆底 + emoji + 短标签，命中用
鼠标坐标自己对——跨平台一致，也方便脱离界面单测（布局/命中都是纯函数）。

主打 Windows；mac 下圆角/阴影观感会打折（与球、唤出面板同一现象），验收以 Windows 为准。
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QPoint, QPointF, QTimer, QRectF
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget, QApplication, QMenu, QToolTip

from store import app_state

#: 数据键：扇形功能项（有序）与双击落点页
K_FAN_ITEMS = "launcher_fan_items"
K_DBL_PAGE = "launcher_dblclick_page"

#: 弧半径（球心到按钮中心）与按钮直径：够点、又不糊住球本身
RADIUS = 104
BTN = 52
#: 默认展开角度范围：朝屏内一侧张开约 120°，项多时自动加宽到 160°
SPREAD_MIN = 120
SPREAD_MAX = 160

_BG = QColor(28, 32, 38, 232)          # 与球/唤出面板同款深灰半透明
_RING = QColor(51, 112, 255, 200)      # 品牌蓝描边
_GLYPH = QColor("#E8EAED")
_HL = QColor(51, 112, 255, 90)         # 悬停高亮底


# ==================== 候选池与持久化 ====================
def _page_items():
    """页面候选：页栈索引固定，与 main_window._NAV_PAGES / 页栈顺序对齐。"""
    return [(0, "数据中台", "🖥️"), (1, "任务中心", "🎯"), (2, "产品中心", "🧩"),
            (3, "线路负载", "📡"), (4, "工具中心", "🧰"), (9, "拆解任务", "🔥"),
            (10, "素材库", "🎞"), (11, "成品库", "📦"), (12, "AI 混剪", "🎬"),
            (13, "批量基建", "🏗"), (6, "设置", "⚙️")]


def _item_visible(item):
    """扇形项是否该出现：按其所属模块的启用状态过滤。

    search / tool 都归「工具中心」（搜索面板、工具都靠 summon_tool/_summon_launcher，
    工具中心关掉就不露）；page 项看该页入口是否可见。"""
    from core import modules
    kind = item.get("kind")
    if kind in ("search", "tool"):
        return modules.is_enabled(modules.MODULE_TOOLS)
    if kind == "page":
        try:
            return modules.page_visible(int(item.get("ref") or 0))
        except (TypeError, ValueError):
            return True
    return True


def fan_candidates():
    """可加进扇形的全部候选（搜索面板 + 工具 + 页面），带唯一 key 供去重。

    kind：search=弹唤出面板；tool=summon_tool(name)；page=切主窗口到页栈索引。
    候选按已启用模块过滤：工具中心关掉就不出 search/tool，关掉的页也不出。"""
    out = [{"kind": "search", "ref": "", "label": "搜索面板", "icon": "🔍"}]
    try:
        from gui.tools_registry import TOOLS
        for name, icon, _desc, _f in TOOLS:
            out.append({"kind": "tool", "ref": name, "label": name, "icon": icon})
    except Exception:
        pass
    for idx, label, icon in _page_items():
        out.append({"kind": "page", "ref": idx, "label": label, "icon": icon})
    return [it for it in out if _item_visible(it)]


def _key(item):
    return "%s:%s" % (item.get("kind"), item.get("ref"))


def load_fan_items():
    """读扇形项（list[dict]）；没配过时给一组实用默认（搜索 + 任务中心 + 工具中心 + 设置）。

    存量和默认集都按已启用模块过滤：模块关掉后，其入口自动从扇形收起（不删记录，
    重装模块即回来）；若过滤后为空则退回默认集（默认集本身也已过滤）。"""
    raw = app_state.get(K_FAN_ITEMS)
    if isinstance(raw, list) and raw:
        kept = [dict(it) for it in raw
                if isinstance(it, dict) and _item_visible(it)]
        if kept:
            return kept
    idx = {(_k): it for _k, it in ((_key(c), c) for c in fan_candidates())}
    return [dict(idx[k]) for k in
            ("search:", "page:1", "page:4", "page:6") if k in idx]


def save_fan_items(items):
    return app_state.set_value(K_FAN_ITEMS,
                               [dict(it) for it in (items or []) if isinstance(it, dict)])


# ==================== 纯几何（脱离界面可测） ====================
def arc_angles(n, base_deg, spread_deg):
    """把 n 颗按钮等角铺在以 base_deg 为中心、spread_deg 为张角的弧上（度数表）。

    n<=1 时正落在 base_deg 上；n>=2 时对称展开。0°=正右，逆时针为正（数学约定），
    但屏幕 y 轴朝下，故 y 用 sin 时取反由调用方处理，这里只吐角度。"""
    if n <= 0:
        return []
    if n == 1:
        return [float(base_deg)]
    half = spread_deg / 2.0
    step = spread_deg / (n - 1)
    return [base_deg - half + step * i for i in range(n)]


def fan_positions(n, cx, cy, radius=RADIUS, base_deg=0.0, spread_deg=SPREAD_MIN):
    """返回 n 个按钮中心在窗口坐标里的 (x, y)。屏幕坐标 y 朝下，故用 -sin 让正角朝上。"""
    pts = []
    for a in arc_angles(n, base_deg, spread_deg):
        rad = math.radians(a)
        pts.append((cx + radius * math.cos(rad), cy - radius * math.sin(rad)))
    return pts


def pick_at(points, pos, r=BTN / 2):
    """命中测试：返回离 pos 最近且在半径 r 内的按钮下标，否则 -1。"""
    best, best_d = -1, None
    for i, (px, py) in enumerate(points):
        d = math.hypot(pos[0] - px, pos[1] - py)
        if d <= r and (best_d is None or d < best_d):
            best, best_d = i, d
    return best


def base_toward_center(bx, by, screen):
    """球心指向可用屏中心的方位角（度）：扇形朝屏内一侧展开，贴边也不会甩出界。

    screen = (x, y, w, h)。"""
    sx, sy, sw, sh = screen
    dx = (sx + sw / 2.0) - bx
    dy = (by - (sy + sh / 2.0))      # 取反：屏幕 y 朝下，翻转回数学约定（正角朝上）
    return math.degrees(math.atan2(dy, dx))


# ==================== 扇形小窗 ====================
class LauncherFan(QWidget):
    """以球心为极点、朝屏内展开的放射菜单。on_pick(item) 选功能，on_add() 加功能；
    on_enter()/on_leave() 把"鼠标进入/离开扇形这块区域"回报给调用方，与球共用同一条
    宽限期计时器——这样鼠标从球穿到扇形途中不会因球的一条 Leave 被误收。"""

    #: 悬停放大倍率与每帧缓动步长（纯放大视觉，不改命中圆心）
    _SCALE_HI = 1.24
    _SCALE_STEP = 0.28

    def __init__(self, on_pick, on_add, on_enter=None, on_leave=None, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 扇形不夺焦点：hover 模态下它只该被“看到”不该“激活”，否则一 show 就把主界面
        # 焦点抢了，还可能靠 focusOut 抢先收起（与宽限期相冲）。
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._on_pick = on_pick
        self._on_add = on_add
        self._on_enter = on_enter
        self._on_leave = on_leave
        self._items = []
        self._points = []            # 与 _items + "+" 一一对应的按钮中心（窗口坐标）
        self._scales = []            # 与 _points 一一对应的当前放大倍率（hover 缓动）
        self._hover = -1
        self._ball = None
        self._font = QFont("Microsoft YaHei")
        # 关键：开鼠标追踪，否则不按鼠标时 mouseMoveEvent 根本不触发——
        # 那会导致悬停不放大、移出不显名（之前"动效没做"的真因）。
        self.setMouseTracking(True)
        # 只在展开时跑：驱动 hover 放大的逐帧缓动，收起即停，不空转耗 CPU
        self._anim = QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._animate)

    # ---------- 对外 ----------
    def popup(self, ball, items=None):
        """贴着球展开：ball 提供球心屏幕坐标。item 数含末尾的 "+"。"""
        self._ball = ball
        self._items = list(items if items is not None else load_fan_items())
        self._recompute_and_show()

    def toggle(self, ball, items=None):
        if self.isVisible():
            self.hide_menu()
        else:
            self.popup(ball, items)

    def hide_menu(self):
        self._hover = -1
        self._anim.stop()
        QToolTip.hideText()
        self.hide()

    # ---------- 悬停回报（与球共用宽限期） ----------
    def enterEvent(self, e):
        # 鼠标进到扇形这块方框里：取消"移出收起"，别让它半路关了
        cb = self._on_enter
        if callable(cb):
            cb()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = -1
        QToolTip.hideText()
        cb = self._on_leave
        if callable(cb):
            cb()
        self._anim.start()
        self.update()
        super().leaveEvent(e)

    # ---------- 逐帧缓动：把悬停那颗的目标倍率平滑过去 ----------
    def _animate(self):
        target = [self._SCALE_HI if i == self._hover else 1.0
                  for i in range(len(self._points))]
        moving = False
        for i, t in enumerate(target):
            cur = self._scales[i] if i < len(self._scales) else 1.0
            if abs(cur - t) > 0.005:
                self._scales[i] = cur + (t - cur) * self._SCALE_STEP
                moving = True
            else:
                self._scales[i] = t
        self.update()
        if not moving and self._hover < 0:
            self._anim.stop()          # 都缓到位且没悬停：停表省电

    # ---------- 几何 ----------
    def _recompute_and_show(self):
        n = len(self._items) + 1          # 末尾 "+"
        spread = min(SPREAD_MAX, SPREAD_MIN + max(0, n - 4) * 12)
        scr = QApplication.primaryScreen()
        geo = scr.availableGeometry() if scr else None
        bx = self._ball.x() + self._ball.width() // 2 if self._ball else 0
        by = self._ball.y() + self._ball.height() // 2 if self._ball else 0
        screen = (geo.x(), geo.y(), geo.width(), geo.height()) if geo else (0, 0, 1920, 1080)
        base = base_toward_center(bx, by, screen)
        # 窗口是包住整段弧的方框，球心落在窗口中心
        side = 2 * (RADIUS + BTN)
        cx = cy = side / 2.0
        self._points = fan_positions(n, cx, cy, RADIUS, base, spread)
        self._scales = [1.0] * len(self._points)
        self._hover = -1              # 重排后旧下标失效，先清悬停避免指错
        self.setFixedSize(side, side)
        self.move(int(bx - cx), int(by - cy))
        self.show()
        self.raise_()
        self.update()

    # ---------- 交互 ----------
    def mousePressEvent(self, e):
        i = pick_at(self._points, (e.position().x(), e.position().y()))
        if i < 0:
            return
        if i == len(self._items):          # 最后一颗是 "+"
            self._add_menu(e.globalPos())
            return
        item = self._items[i]
        self.hide_menu()
        cb = self._on_pick
        if callable(cb):
            cb(item)

    def mouseMoveEvent(self, e):
        pos = (e.position().x(), e.position().y())
        # 命中半径略放宽到放大后的视觉尺寸，鼠标靠大圆边缘也算悬停
        i = pick_at(self._points, pos, r=BTN / 2 * self._SCALE_HI)
        if i != self._hover:
            self._hover = i
            if i >= 0:
                self._show_tip(i, e)
            else:
                QToolTip.hideText()
            self._anim.start()          # 倍率要变：唤醒缓动循环
            self.update()

    def _show_tip(self, i, e):
        """悬停某颗：浮窗显示它的功能名（"+" 那颗给提示）。"""
        if i == len(self._items):
            text = "＋ 添加功能"
        else:
            text = str(self._items[i].get("label", "")) or "未命名"
        gp = self.mapToGlobal(QPoint(int(self._points[i][0]),
                                     int(self._points[i][1])))
        QToolTip.showText(gp, text, self)

    def contextMenuEvent(self, e):
        """右键某颗 → 从扇形移除（"+" 那颗没有可移除的）。"""
        i = pick_at(self._points, (e.position().x(), e.position().y()))
        if 0 <= i < len(self._items):
            it = self._items[i]
            m = QMenu(self)
            m.addAction("✕ 从扇形移除「%s」" % it.get("label", ""),
                        lambda _i=i: self._remove_at(_i))
            m.exec(e.globalPos())

    def _add_menu(self, global_pos):
        """点 "+"：弹出还没在用的候选，选一个即 append 并就地重排。"""
        used = {_key(it) for it in self._items}
        cands = [c for c in fan_candidates() if _key(c) not in used]
        m = QMenu(self)
        if not cands:
            m.addAction("（没有可添加的了）", lambda: None)
        for c in cands:
            m.addAction("%s  %s" % (c["icon"], c["label"]),
                        lambda _c=c: self._append_item(_c))
        m.exec(global_pos)

    def _append_item(self, item):
        self._items.append(dict(item))
        save_fan_items(self._items)
        if self._ball is not None:
            self._recompute_and_show()

    def _remove_at(self, i):
        if 0 <= i < len(self._items):
            self._items.pop(i)
            save_fan_items(self._items)
            if self._ball is not None:
                self._recompute_and_show()

    # ---------- 自绘 ----------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        total = len(self._items) + 1
        for i in range(total):
            x, y = self._points[i]
            sc = self._scales[i] if i < len(self._scales) else 1.0
            d = BTN * sc                 # hover 放大：以同圆心把直径拉大
            rect = _circle_rect(x, y, d)
            is_plus = i == total - 1
            p.setPen(QPen(_RING, 2))
            p.setBrush(_HL if i == self._hover else _BG)
            p.drawEllipse(rect)
            it = None if is_plus else self._items[i]
            glyph = "＋" if is_plus else (it.get("icon", "▫"))
            self._font.setPointSize(int(round((16 if is_plus else 18) * sc)))
            p.setFont(self._font)
            p.setPen(_GLYPH)
            _draw_glyph_centered(p, self._font, glyph, x, y)
            if not is_plus:
                # 短标签画在按钮下方（截断到 4 字，扇形是看图不是看字）
                label = str(it.get("label", ""))[:4]
                self._font.setPointSize(9)
                p.setFont(self._font)
                p.setPen(_GLYPH)
                lr = rect.adjusted(-10, d - 4, 10, d + 12)
                p.drawText(lr, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, label)
        p.end()

    # 失焦即收（激活后 Esc/点到别处都会触发）；再点球由 ball 的单击回调 toggle
    def focusOutEvent(self, e):
        QTimer.singleShot(0, self.hide_menu)
        super().focusOutEvent(e)


def _circle_rect(cx, cy, d):
    from PySide6.QtCore import QRectF
    return QRectF(cx - d / 2.0, cy - d / 2.0, d, d)


def _draw_glyph_centered(p, font, text, cx, cy):
    """把单个图标按墨迹精确居中到 (cx, cy)。

    emoji 的逻辑行框上下留白不均，直接 AlignCenter 会看着“偏下”，故用
    tightBoundingRect 把墨迹垂直中心对到圆心；量不到墨迹（空 rect）时退回 AlignCenter。"""
    fm = QFontMetrics(font)
    tb = fm.tightBoundingRect(text)
    if tb.isNull() or tb.height() <= 0:
        p.drawText(_circle_rect(cx, cy, fm.height()),
                   Qt.AlignmentFlag.AlignCenter, text)
        return
    advance = fm.horizontalAdvance(text)
    baseline = cy - (tb.top() + tb.height() / 2.0)   # tb.top 为负（基线以上）
    p.drawText(QPointF(cx - advance / 2.0, baseline), text)
