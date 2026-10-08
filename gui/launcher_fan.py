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

from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget, QApplication, QMenu

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


def fan_candidates():
    """可加进扇形的全部候选（搜索面板 + 工具 + 页面），带唯一 key 供去重。

    kind：search=弹唤出面板；tool=summon_tool(name)；page=切主窗口到页栈索引。"""
    out = [{"kind": "search", "ref": "", "label": "搜索面板", "icon": "🔍"}]
    try:
        from gui.tools_registry import TOOLS
        for name, icon, _desc, _f in TOOLS:
            out.append({"kind": "tool", "ref": name, "label": name, "icon": icon})
    except Exception:
        pass
    for idx, label, icon in _page_items():
        out.append({"kind": "page", "ref": idx, "label": label, "icon": icon})
    return out


def _key(item):
    return "%s:%s" % (item.get("kind"), item.get("ref"))


def load_fan_items():
    """读扇形项（list[dict]）；没配过时给一组实用默认（搜索 + 任务中心 + 工具中心 + 设置）。"""
    raw = app_state.get(K_FAN_ITEMS)
    if isinstance(raw, list) and raw:
        return [dict(it) for it in raw if isinstance(it, dict)]
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
    """以球心为极点、朝屏内展开的放射菜单。on_pick(item) 选功能，on_add() 加功能。"""

    def __init__(self, on_pick, on_add, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._on_pick = on_pick
        self._on_add = on_add
        self._items = []
        self._points = []            # 与 _items + "+" 一一对应的按钮中心（窗口坐标）
        self._hover = -1
        self._ball = None
        self._font = QFont("Microsoft YaHei")

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
        self.hide()

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
        i = pick_at(self._points, (e.position().x(), e.position().y()))
        if i != self._hover:
            self._hover = i
            self.update()

    def leaveEvent(self, e):
        self._hover = -1
        self.update()
        super().leaveEvent(e)

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
            rect = _circle_rect(x, y, BTN)
            is_plus = i == total - 1
            p.setPen(QPen(_RING, 2))
            p.setBrush(_HL if i == self._hover else _BG)
            p.drawEllipse(rect)
            it = None if is_plus else self._items[i]
            glyph = "＋" if is_plus else (it.get("icon", "▫"))
            self._font.setPointSize(16 if is_plus else 18)
            p.setFont(self._font)
            p.setPen(_GLYPH)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, glyph)
            if not is_plus:
                # 短标签画在按钮下方（截断到 4 字，扇形是看图不是看字）
                label = str(it.get("label", ""))[:4]
                self._font.setPointSize(9)
                p.setFont(self._font)
                p.setPen(_GLYPH)
                lr = rect.adjusted(-10, BTN - 4, 10, BTN + 12)
                p.drawText(lr, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, label)
        p.end()

    # 失焦即收（激活后 Esc/点到别处都会触发）；再点球由 ball 的单击回调 toggle
    def focusOutEvent(self, e):
        QTimer.singleShot(0, self.hide_menu)
        super().focusOutEvent(e)


def _circle_rect(cx, cy, d):
    from PySide6.QtCore import QRectF
    return QRectF(cx - d / 2.0, cy - d / 2.0, d, d)
