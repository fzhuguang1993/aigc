"""
gui/mouse_gesture.py —— 鼠标手势：按住右键划轨迹 → 识别 → 执行上下文命令

交互模型：在软件任意窗口里按住右键拖动，位移超过阈值即进入手势（屏幕画出
灰色轨迹），松开右键匹配已绑定的轨迹并执行命令；没划过阈值就是普通右键，
完全不影响原有右键菜单。

「同一个手势 = 一个命令」，命令的具体行为由焦点上下文决定（resolver 回调
交给主窗口实现）：同样「打开输出目录」，任务中心开全局 outputs/、屏幕录制
开录屏目录、素材提取开素材下载目录；「新建」在任务中心是新建任务、在产品
中心是新建产品。识别逻辑与界面解耦：encode_path 是纯函数（方向串），单测
不需要 Qt 运行环境。

轨迹存储：ui_state.json
  gesture_enabled  bool                总开关
  gesture_map      {命令id: {seq,pts}}  一条命令一条轨迹；seq 供显示、pts 供形状匹配
  （pts=归一化后 32 点 [[x,y]…]；旧格式（值为纯方向串）仍按方向串全等回退）

形状匹配（$1 Unistroke 思路，但不做旋转归一）：录制与触发都把轨迹归一化成
固定点数、质心居中、统一缩放的坐标，再算点对点平均距离，最小且低于阈值即命中。
不转正方向是因为 →/←/↑ 本就是不同命令，方向必须保留。
"""
import math

from PySide6.QtCore import (Qt, QEvent, QPoint, QRect, QTimer, Signal,
                            QObject)
from PySide6.QtGui import QColor, QPolygon, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

# 轨迹键名约定：_ 表示回到原点（闭合），. 表示位移过小没有明确方向
DIRECTIONS = ("u", "d", "l", "r", "ul", "ur", "dl", "dr", "_", ".")
DIR_ARROW = {"u": "↑", "d": "↓", "l": "←", "r": "→",
             "ul": "↖", "ur": "↗", "dl": "↙", "dr": "↘", "_": "○闭合",
             ".": "·微移"}

# 可绑定手势的命令（id, 显示名, 说明——说明里写清各上下文的差异）
GESTURE_COMMANDS = [
    ("close", "关闭当前窗口", "关掉当前工具窗口/弹窗；主窗口不会被误关"),
    ("refresh", "刷新当前页面", "工具面板有刷新能力的也会一并刷"),
    ("new", "新建（看当前上下文）", "任务中心＝新建任务；产品中心＝新建产品"),
    ("open_output", "打开输出目录（看上下文）",
     "任务中心＝全局输出目录；屏幕录制/素材提取＝各自的目录"),
    ("open_tools", "打开工具中心", "任意页面直达工具中心页"),
    ("open_dashboard", "打开数据中台", "任意页面直达数据中台页"),
]
COMMAND_LABELS = {cid: label for cid, label, _d in GESTURE_COMMANDS}


def gesture_display(seq):
    """方向串 → 人看的箭头串（'ur|dl|_' → '↗↘○闭合'）"""
    return "".join(DIR_ARROW.get(t, "?") for t in (seq or "").split("|") if t) or ""


def _as_xy(p):
    """轨迹点取值：引擎里存的是 QPoint，单测/手录可能传元组，两者都吃"""
    if hasattr(p, "x"):
        return p.x(), p.y()
    return p[0], p[1]


def _key_disp(dx, dy, origin):
    """把一段位移编码成方向键；回到原点（离起 20px 内）且已有多段 → 闭合键 _"""
    if abs(dx) >= abs(dy):
        return "r" if dx > 0 else "l"
    return "d" if dy > 0 else "u"


def encode_path(points, step=14):
    """屏幕坐标轨迹 → 方向串（'ur|dl|_' 形式：'|' 是分隔符，独立元素 '_' 表闭合）。

    逐点累积位移：合成位移达到 step 才吃掉一段（保持起点，残量带入下一段），
    因此慢划和快划得到同一个串；轨迹末尾回到起点记为闭合 '_'。"""
    if not points:
        return ""
    keys = []
    ox, oy = _as_xy(points[0])
    sx, sy = ox, oy
    for p in points[1:]:
        x, y = _as_xy(p)
        dx, dy = x - sx, y - sy
        if abs(dx) < step and abs(dy) < step:
            continue
        keys.append(_key_disp(dx, dy, (ox, oy)))
        # 吃掉该方向分量：垂直分量留给下一段（斜线划会被拆成两个主轴方向）
        sx += step if dx > 0 else -step if dx < 0 else 0
        sy += step if dy > 0 else -step if dy < 0 else 0
        if (x - ox) ** 2 + (y - oy) ** 2 < 400 and len(keys) >= 3:
            keys.append("_")
            ox, oy = x, y
            sx, sy = x, y
    return "|".join(keys)


# ---------- 形状匹配：$1 Unistroke（重采样 + 居中 + 统一缩放，不旋转） ----------
RESAMPLE_POINTS = 32        # 每条轨迹重采样成固定点数，才能逐点比距离
SHAPE_MAX_DIST = 0.20       # 归一化（长边=1）后点对点平均距离上限，越小越严


def _resample(points, n=RESAMPLE_POINTS):
    """沿折线按弧长等距重采样成 n 个点；点太少/没长度 → None"""
    pts = [_as_xy(p) for p in points]
    clean = [pts[0]]
    for p in pts[1:]:
        if abs(p[0] - clean[-1][0]) + abs(p[1] - clean[-1][1]) > 1e-6:
            clean.append(p)
    if len(clean) < 2:
        return None
    cum = [0.0]
    for i in range(len(clean) - 1):
        cum.append(cum[-1] + math.hypot(clean[i + 1][0] - clean[i][0],
                                         clean[i + 1][1] - clean[i][1]))
    total = cum[-1]
    if total <= 1e-6:
        return None
    out, idx = [], 0
    for k in range(n):
        target = total * k / (n - 1)
        while idx < len(cum) - 1 and cum[idx + 1] < target:
            idx += 1
        if idx >= len(cum) - 1:
            out.append(clean[-1])
            continue
        seg = (cum[idx + 1] - cum[idx]) or 1e-9
        t = (target - cum[idx]) / seg
        x0, y0 = clean[idx]
        x1, y1 = clean[idx + 1]
        out.append((x0 + t * (x1 - x0), y0 + t * (y1 - y0)))
    return out


def normalize_shape(points):
    """屏幕轨迹 → 归一化形状点列表 [[x,y]×n]：重采样→质心移到原点→按长边统一缩放。
    统一缩放（x/y 同比例）保留长宽比；不做旋转，保留方向。"""
    rs = _resample(points)
    if not rs:
        return None
    cx = sum(x for x, _ in rs) / len(rs)
    cy = sum(y for _, y in rs) / len(rs)
    rs = [(x - cx, y - cy) for x, y in rs]
    w = max(x for x, _ in rs) - min(x for x, _ in rs)
    h = max(y for _, y in rs) - min(y for _, y in rs)
    scale = 1.0 / max(w, h, 1e-6)
    return [[round(x * scale, 4), round(y * scale, 4)] for x, y in rs]


def shape_distance(a, b):
    """两条归一化形状的平均点对点欧氏距离（越小越像；长度不一致/空 → inf）"""
    if not a or not b or len(a) != len(b):
        return float("inf")
    s = 0.0
    for (x0, y0), (x1, y1) in zip(a, b):
        s += math.hypot(x1 - x0, y1 - y0)
    return s / len(a)


def match_shape(shape, shapes):
    """shape 对 {cid: 归一化形状} 取最近邻；距离≤阈值返回 (cid, dist)，否则 (None, dist)"""
    best, bestd = None, SHAPE_MAX_DIST
    for cid, sh in shapes.items():
        d = shape_distance(shape, sh)
        if d < bestd:
            best, bestd = cid, d
    return best, bestd


class _Trail(QWidget):
    """轨迹指示线：无边框置顶、对鼠标完全透明，只负责把线画出来"""

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._pts = []
        self._geo = QRect()

    def begin(self, pos):
        scr = QApplication.screenAt(pos) or QApplication.primaryScreen()
        self._geo = scr.geometry()
        self.setGeometry(self._geo)
        self._pts = [pos - self._geo.topLeft()]
        self.show()
        self.raise_()

    def add_point(self, pos):
        self._pts.append(pos - self._geo.topLeft())
        self.update()

    def end(self):
        self.hide()
        self._pts = []

    def paintEvent(self, _e):
        if len(self._pts) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(51, 112, 255, 200), 5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        pts = [QPoint(int(pt.x()), int(pt.y())) for pt in self._pts]
        p.drawPolyline(QPolygon(pts))
        p.setBrush(QColor(51, 112, 255))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(pts[0], 6, 6)
        p.end()


class MouseGestureEngine(QObject):
    """挂在 QApplication 事件过滤器上的识别器：右键划出→匹配→派发。

    record_mode：设置页录制态——划完只发 record_finished(方向串, 归一化形状)，不派发；
    没划出轨迹松开则发 record_cancelled()（误点右键不该打断录制流程）。"""

    record_finished = Signal(str, object)
    record_cancelled = Signal()

    TRIGGER_PX = 16          # 右键按下后位移超过这个像素才认为是手势
    COMMAND_THRESHOLD = 1    # 至少一个方向键才算一条轨迹

    def __init__(self, parent=None):
        super().__init__(parent)
        self._app = QApplication.instance()
        self._trail = _Trail()
        self._resolver = None
        self._origin = None
        self._active = False
        self._points = []
        self._suppress_menu = False
        self.record_mode = False
        if self._app is not None:
            self._app.installEventFilter(self)

    def set_resolver(self, fn):
        self._resolver = fn

    def start_record(self):
        self.record_mode = True

    def stop_record(self):
        self.record_mode = False

    # ---- 事件过滤：只吃右键三件套 + 拖动期的 ContextMenu ----
    def eventFilter(self, obj, event):
        et = event.type()
        if et == QEvent.Type.MouseButtonPress \
                and event.button() == Qt.MouseButton.RightButton:
            self._origin = event.globalPosition().toPoint()
            self._active = False
            self._points = [self._origin]
        elif et == QEvent.Type.MouseMove and self._origin is not None:
            pos = event.globalPosition().toPoint()
            if not self._active:
                dx, dy = pos.x() - self._origin.x(), pos.y() - self._origin.y()
                if dx * dx + dy * dy >= self.TRIGGER_PX * self.TRIGGER_PX:
                    self._active = True
                    self._trail.begin(self._origin)
            if self._active:
                self._points.append(pos)
                self._trail.add_point(pos)
        elif et == QEvent.Type.MouseButtonRelease \
                and event.button() == Qt.MouseButton.RightButton:
            was_active = self._active
            self._reset_watch()
            if was_active:
                self._suppress_menu = True
                QTimer.singleShot(300, lambda: setattr(self, "_suppress_menu", False))
                self._finish(event.globalPosition().toPoint())
        elif et == QEvent.Type.ContextMenu and self._suppress_menu:
            return True                    # 手势刚松开：别把右键菜单弹出来
        return super().eventFilter(obj, event)

    def _reset_watch(self):
        self._origin = None
        self._active = False

    def _finish(self, end_pos):
        self._trail.end()
        seq = encode_path(self._points)
        shape = normalize_shape(self._points)
        if self.record_mode:
            self.record_mode = False
            keys = [k for k in seq.split("|") if k]
            if len(keys) >= self.COMMAND_THRESHOLD and shape:
                self.record_finished.emit(seq, shape)
            else:
                self.record_cancelled.emit()
            return
        if not seq or seq == "_":
            return
        from store import app_state
        if not app_state.get("gesture_enabled", True):
            return
        cmd = self._match_cmd(seq, shape)
        if cmd and self._resolver is not None:
            self._resolver(cmd)

    def _match_cmd(self, seq, shape):
        """先拿归一化形状做形状匹配（容忍划动差异）；没形状/没命中再回退到
        方向串全等（兼容旧格式：gesture_map 值是纯方向串）"""
        from store import app_state
        gmap = app_state.get("gesture_map") or {}
        shapes = {cid: ent["pts"] for cid, ent in gmap.items()
                  if isinstance(ent, dict) and ent.get("pts")}
        if shapes and shape:
            cid, _ = match_shape(shape, shapes)
            if cid:
                return cid
        for cid, ent in gmap.items():
            s = ent.get("seq") if isinstance(ent, dict) else ent
            if s == seq:
                return cid
        return None
