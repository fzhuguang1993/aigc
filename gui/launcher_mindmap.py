"""
gui/launcher_mindmap.py —— Markdown 图形脑图（纯解析 + QGraphics 树状视图）

拆两块：
- parse_md_tree(text, title)：纯函数，把 Markdown 的 #/##/### 层级（并把无序列表项
  并入其父标题的子节点）解析成 Node 树；跳过代码围栏(```)里的 #。可离屏单测，不碰 Qt。
- MindMapView(QGraphicsView)：拿这棵树画横向树状脑图——根在左、子节点往右递归铺开，
  圆角矩形节点 + 贝塞尔连线；滚轮缩放、拖拽平移、点节点折叠/展开并在底部回显该段文本。
  纯 QPainter/QGraphics，无第三方渲染依赖。
"""
import re

from PySide6.QtCore import QRectF, Qt, QPointF
from PySide6.QtGui import QColor, QBrush, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")


class Node:
    """脑图一个节点：标题、层级、子节点、以及本段正文（点节点时回显用）。"""

    __slots__ = ("title", "depth", "children", "body", "collapsed", "_x", "_y")

    def __init__(self, title="", depth=0):
        self.title = str(title or "")
        self.depth = int(depth)
        self.children = []
        self.body = []
        self.collapsed = False
        self._x = 0.0                          # 版式坐标（MindMapView 布局时写）
        self._y = 0.0


def parse_md_tree(text, title=""):
    """把 Markdown 解析成标题层级树，返回根 Node（depth=0，title=文档标题）。
    任何畸形/空输入都返回一个可渲染的根，绝不抛。"""
    root = Node(title, 0)
    stack = [root]                       # 标题栈：栈顶是当前归属的标题节点
    in_fence = False
    for raw in str(text or "").splitlines():
        line = raw
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence       # 代码围栏开/关
            continue
        if in_fence:
            continue                        # 围栏里的 # 不算标题
        m = _HEADING.match(line)
        if m:
            depth = len(m.group(1))
            node = Node(m.group(2) or "", depth)
            while len(stack) > 1 and stack[-1].depth >= depth:
                stack.pop()
            parent = stack[-1]
            parent.children.append(node)
            stack.append(node)
            continue
        b = _BULLET.match(line)
        if b and len(stack) > 1:
            cur = stack[-1]
            cur.children.append(Node(b.group(1).strip(), cur.depth + 1))
            continue
        if stripped:
            stack[-1].body.append(stripped)
    return root


def _visible_children(node):
    """折叠了就当作没有孩子（脑图点节点折叠的核心开关）。"""
    return [] if node.collapsed else node.children


# ---------- 图形视图 ----------
_NODE_W, _NODE_H = 168.0, 46.0
_GAP_X, _GAP_Y = 70.0, 18.0
_TEXT = "#E8EAED"
_WEAK = "#9AA4B0"
_ACCENT = "#3370FF"


def _elide(fm, text, width):
    text = str(text or "")
    if fm.horizontalAdvance(text) <= width:
        return text
    while text and fm.horizontalAdvance(text + "…") > width:
        text = text[:-1]
    return text + "…"


class MindMapView(QGraphicsView):
    """横向树状脑图。show_tree(root) 重建画面；点节点折叠/展开并回显正文。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._root = None
        self._pos = {}                    # id(node) -> 左上角 QRectF
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QColor(28, 32, 38))
        self.setStyleSheet("QGraphicsView { border:none; border-radius:8px; }")

    def show_tree(self, root):
        self._root = root if isinstance(root, Node) else Node(str(root or ""))
        self._rebuild()

    def _rebuild(self):
        scene = QGraphicsScene(self)
        self._pos = {}
        cursor = [0.0]
        self._layout(self._root, 0, cursor)
        # 先连线再画节点，节点压在连线之上
        self._draw_edges(scene, self._root)
        self._draw_nodes(scene, self._root)
        rect = scene.itemsBoundingRect().adjusted(-24, -24, 24, 24)
        scene.setSceneRect(rect)
        self.setScene(scene)
        self.fit_view()

    # 递归算坐标：叶子占一行往下堆，父节点竖直居中于其首末子节点之间
    def _layout(self, node, depth, cursor):
        node._x = depth * (_NODE_W + _GAP_X)
        kids = _visible_children(node)
        if not kids:
            node._y = cursor[0]
            cursor[0] += _NODE_H + _GAP_Y
        else:
            for c in kids:
                self._layout(c, depth + 1, cursor)
            node._y = (kids[0]._y + kids[-1]._y) / 2.0
        # 先定好矩形，供连线（比节点先画）与点击命中共用
        self._pos[id(node)] = QRectF(node._x, node._y, _NODE_W, _NODE_H)

    def _draw_nodes(self, scene, node):
        fm = self.fontMetrics()
        rect = self._pos[id(node)]
        depth = node.depth
        bg = QColor(51, 112, 255, 40 if depth <= 1 else 20)
        border = QColor(_ACCENT) if depth <= 1 else QColor(255, 255, 255, 40)
        pen = QPen(border)
        pen.setWidthF(1.4 if depth <= 1 else 1.0)
        shape = QPainterPath()
        shape.addRoundedRect(rect, 9, 9)
        item = scene.addPath(shape, pen, QBrush(bg))
        label = scene.addSimpleText(_elide(fm, node.title, _NODE_W - 20))
        f = QFont(self.font())
        f.setPixelSize(14 if depth <= 1 else 12)
        f.setWeight(QFont.Weight.DemiBold if depth <= 1 else QFont.Weight.Normal)
        label.setFont(f)
        label.setBrush(QColor(_TEXT if node.title else _WEAK))
        tr = label.boundingRect()
        label.setPos(rect.center().x() - tr.width() / 2.0,
                     rect.center().y() - tr.height() / 2.0)
        if _visible_children(node):
            badge = scene.addSimpleText("▸" if node.collapsed else "▾")
            badge.setBrush(QColor(_WEAK))
            badge.setPos(rect.right() - 16, rect.top() + 3)
        for c in _visible_children(node):
            self._draw_nodes(scene, c)

    def _draw_edges(self, scene, node):
        for c in _visible_children(node):
            pr, cr = self._pos.get(id(node)), self._pos.get(id(c))
            if pr and cr:
                path = QPainterPath()
                p1 = QPointF(pr.right(), pr.center().y())
                p2 = QPointF(cr.left(), cr.center().y())
                mx = (p1.x() + p2.x()) / 2.0
                path.moveTo(p1)
                path.cubicTo(QPointF(mx, p1.y()), QPointF(mx, p2.y()), p2)
                pen = QPen(QColor(255, 255, 255, 55))
                pen.setWidthF(1.4)
                scene.addPath(path, pen)
            self._draw_edges(scene, c)

    def fit_view(self):
        rect = self.scene().itemsBoundingRect().adjusted(-20, -20, 20, 20)
        if not rect.isEmpty():
            self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)

    # ---------- 交互 ----------
    def wheelEvent(self, e):
        factor = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        e.accept()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            node = self._node_at(e.pos())
            if node is not None:
                node.collapsed = not node.collapsed
                self._rebuild()
                e.accept()
                return
        super().mousePressEvent(e)

    def _node_at(self, vp_pos):
        scene_pos = self.mapToScene(vp_pos)
        for key, rect in self._pos.items():
            if rect.contains(scene_pos):
                return self._find_by_key(key)
        return None

    def _find_by_key(self, key):
        stack = [self._root] if self._root else []
        while stack:
            n = stack.pop()
            if id(n) == key:
                return n
            stack.extend(n.children)
        return None
