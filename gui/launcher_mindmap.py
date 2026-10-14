"""
gui/launcher_mindmap.py —— Markdown 图形脑图（纯解析 + QGraphics 树状视图）

拆两块：
- parse_md_tree(text, title)：纯函数，把 Markdown 的 #/##/### 层级（并把无序列表项
  并入其父标题的子节点）解析成 Node 树；跳过代码围栏(```)里的 #。可离屏单测，不碰 Qt。
- MindMapView(QGraphicsView)：拿这棵树画横向树状脑图——根在左、子节点往右递归铺开，
  圆角矩形节点 + 贝塞尔连线；滚轮缩放、拖拽平移、点节点折叠/展开并在底部回显该段文本。
  纯 QPainter/QGraphics，无第三方渲染依赖。
- 展开层级：一棵几百个节点的长文档一次全画出来就是“一屏糊在一起”，等于没脑图。
  所以默认只展开一级（set_expand_depth(root, 1)），顶上再给一条工具条做
  「展开下一级 / 收起一级 / 只看一级 / 全部展开」。这里的“展开下一级”是
  **把所有可见的折叠节点各展开一层**（不是只展开点的那一个），与用户的说法一致。
  这几步都是吃 Node 树的纯函数，能脱离 Qt 单测。
- 导出：工具条上「导出图片」把当前画面存成 PNG / JPG / SVG，**不带任何水印**——
  本模块画的就是文档结构本身，没有加 Logo/文字的余地，导出的和屏上看到的一模一样。
"""
import os
import re

from PySide6.QtCore import QPoint, QRectF, Qt, QSize, QPointF
from PySide6.QtGui import (QColor, QBrush, QFont, QImage, QPainter, QPainterPath,
                           QPen)
from PySide6.QtWidgets import (QApplication, QGraphicsScene, QGraphicsView,
                               QHBoxLayout, QToolButton, QWidget)

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


def walk_tree(node):
    """深度优先走**整棵树**（折叠分支底下也算），用于批量改标记。"""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def walk_visible(node):
    """只走“当前看得见”的节点：折叠节点本身看得见，但它底下的分支看不见。"""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        if not n.collapsed:
            stack.extend(n.children)


def set_expand_depth(root, depth):
    """把树整成“只展开到第 depth 级”：depth 级以内的标题全展开，depth 级往后的全折叠。
    depth=1 → 只看见一级标题（默认口径）；depth 小于 1 按 1 处理（根永远展开）。
    就地改 collapsed 标记，返回被改动的节点数（0 表示已经是这个形态）。"""
    d = max(1, int(depth or 1))
    changed = 0
    for n in walk_tree(root):
        want = n.depth >= d           # 这一层的节点该折着（它底下的看不见）
        if bool(n.collapsed) != want:
            n.collapsed = want
            changed += 1
    return changed


def expand_next_level(root):
    """展开“下一级”：把看得见、还折着的那一层里**所有**节点都展开（一次只下探一层）。
    已经全部展开时返回 False，工具条据此把按钮置灰。"""
    target = None
    for n in walk_visible(root):
        if n.children and n.collapsed:
            if target is None or n.depth < target:
                target = n.depth
    if target is None:
        return False
    for n in walk_visible(root):
        if n.depth == target and n.children and n.collapsed:
            n.collapsed = False
    return True


def collapse_one_level(root):
    """收起“最后展开的那一层”：看得见、有子节点、还开着的最深一层全折上。
    与 expand_next_level 互为镜像；已经收到只剩一级时返回 False。
    根节点不参与（把根折上就是把整张图收成一颗孤点，不是用户要的“收起一级”）。"""
    target = None
    for n in walk_visible(root):
        if n.depth > 0 and n.children and not n.collapsed:
            if target is None or n.depth > target:
                target = n.depth
    if target is None:
        return False
    for n in walk_visible(root):
        if n.depth == target and n.children:
            n.collapsed = True
    return True


def expand_all(root):
    """全部展开（导出一张完整结构图时用它）。"""
    changed = 0
    for n in walk_tree(root):
        if n.collapsed:
            n.collapsed = False
            changed += 1
    return changed


def can_expand_deeper(root):
    """还有没有下一级可展开（不真的改树，只回答工具条的置灰问题）。"""
    return any(n.children and n.collapsed for n in walk_visible(root))


def can_collapse_deeper(root):
    """还有没有“一级”可收（根节点不算，见 collapse_one_level 的说明）。"""
    return any(n.depth > 0 and n.children and not n.collapsed
               for n in walk_visible(root))


# ---------- 图形视图 ----------
_NODE_W, _NODE_H = 168.0, 46.0
_GAP_X, _GAP_Y = 70.0, 18.0
_TEXT = "#E8EAED"
_WEAK = "#9AA4B0"
_ACCENT = "#3370FF"
_BG = QColor(28, 32, 38)          # 画布底色：导出图用它铺底，与屏上所见一致
_BAR_H = 30                        # 顶部工具条高度（靠 viewport 上边距留位）
_DEFAULT_DEPTH = 1                 # 默认只展开一级：长文档全铺开等于没脑图

#: 导出格式：扩展名 → (QImage 保存格式名, 中文名)；SVG 走另一条矢量通道
_RASTER_EXPORT = {".png": ("PNG", "PNG"), ".jpg": ("JPG", "JPG"),
                  ".jpeg": ("JPG", "JPG"), ".bmp": ("BMP", "BMP"),
                  ".webp": ("WEBP", "WEBP")}
_EXPORT_FILTER = ("PNG 图片 (*.png);;JPG 图片 (*.jpg);;"
                  "SVG 矢量图 (*.svg);;BMP 图片 (*.bmp)")


def _elide(fm, text, width):
    text = str(text or "")
    if fm.horizontalAdvance(text) <= width:
        return text
    while text and fm.horizontalAdvance(text + "…") > width:
        text = text[:-1]
    return text + "…"


class MindMapView(QGraphicsView):
    """横向树状脑图。show_tree(root) 重建画面（默认只展开一级）；
    点节点折叠/展开，顶部工具条做逐级展开/折叠与无水印导出。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._root = None
        self._pos = {}                    # id(node) -> 左上角 QRectF
        self._doc_name = ""               # 当前文档名：只当导出文件名的建议
        self._press = None                # (按下位置, 节点)：松手时才判“这一下算点还是算拖”
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(_BG)
        self.setStyleSheet("QGraphicsView { border:none; border-radius:8px; }")
        self._bar = self._build_bar()
        # 工具条是本视图的直接子控件（不是 viewport 的子控件）：靠这条上边距给它留位，
        # 否则图会从工具条底下开始画，按钮看着像贴在图上
        self.setViewportMargins(0, _BAR_H, 0, 0)

    # ---------- 工具条 ----------
    def _build_bar(self):
        bar = QWidget(self)
        bar.setGeometry(0, 0, max(self.width(), 320), _BAR_H)
        h = QHBoxLayout(bar)
        h.setContentsMargins(4, 3, 4, 3)
        h.setSpacing(5)
        self._btns = {}
        for key, text, tip, fn in (
                ("next", "⊞ 展开下一级", "把当前看得见的所有折叠节点各展开一层（不是只展开一个）",
                 lambda: self._apply_expand("next")),
                ("prev", "⊟ 收起一级", "收起最后展开的那一层（展开下一级的反向操作）",
                 lambda: self._apply_expand("prev")),
                ("one", "≡ 只看一级", "回到默认：只留一级标题，其余全折起来",
                 lambda: self._apply_expand("one")),
                ("all", "⊡ 全部展开", "展开所有层级（导出完整结构图时先用它）",
                 lambda: self._apply_expand("all"))):
            b = QToolButton(bar)
            b.setText(text)
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(fn)
            h.addWidget(b)
            self._btns[key] = b
        h.addStretch(1)
        b = QToolButton(bar)
        b.setText("💾 导出图片")
        b.setToolTip("导出无水印图片：PNG / JPG / BMP / SVG 矢量图")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(self._do_export)
        h.addWidget(b)
        self._btn_export = b
        bar.setStyleSheet(
            "QWidget { background:rgba(255,255,255,0.05); border:none; }"
            " QToolButton { color:%s; background:transparent;"
            " border:1px solid rgba(255,255,255,0.14); border-radius:5px;"
            " padding:1px 7px; font-size:12px; }"
            " QToolButton:hover { background:rgba(255,255,255,0.10); }"
            " QToolButton:disabled { color:%s; }" % (_TEXT, _WEAK))
        return bar

    def _apply_expand(self, kind):
        """工具条动作落在这棵树上，然后重建画面。"""
        root = self._root
        if root is None:
            return
        if kind == "next":
            expand_next_level(root)
        elif kind == "prev":
            collapse_one_level(root)
        elif kind == "one":
            set_expand_depth(root, _DEFAULT_DEPTH)
        elif kind == "all":
            expand_all(root)
        self._rebuild()

    def _sync_bar(self):
        """按钮可用性跟着树形态走：收无可收/展无可展时置灰，不当“点了没反应”"""
        root = self._root
        if root is None:
            return
        self._btns["next"].setEnabled(can_expand_deeper(root))
        self._btns["all"].setEnabled(can_expand_deeper(root))
        self._btns["prev"].setEnabled(can_collapse_deeper(root))
        # “只看一级”只在树里确实有二级往前的层级时才有意义
        self._btns["one"].setEnabled(
            any(n.depth > _DEFAULT_DEPTH for n in walk_tree(root)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._bar.setGeometry(0, 0, max(self.width(), 1), _BAR_H)

    def show_tree(self, root, name="", depth=None):
        """换一棵树来画。默认按 _DEFAULT_DEPTH 只展开一级（用户点名：全展开“预览很不便”）；
        depth 传 None 就用默认，传数字就展到那一级。"""
        self._root = root if isinstance(root, Node) else Node(str(root or ""))
        self._doc_name = str(name or "")
        set_expand_depth(self._root,
                         _DEFAULT_DEPTH if depth is None else int(depth))
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
        self._sync_bar()

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
        """按下先不得动手：左键按在节点上可能是要点（折叠/展开），也可能是想拖着看
        别处（本视图是 ScrollHandDrag）。是点还是拖，等松手时看位移（见 release）。
        以前这里在 press 里直接改折叠态并 accept 掉事件：拖拽起手就被吃掉，
        于是“按住节点拖”也照样把节点折上/展开，用户说的“不管松不松都收缩”。"""
        self._press = None
        if e.button() == Qt.MouseButton.LeftButton:
            node = self._node_at(e.position().toPoint())
            if node is not None:
                self._press = (QPoint(e.position().toPoint()), node)
        super().mousePressEvent(e)          # 交给视图：拖拽平移该照常起手

    def mouseReleaseEvent(self, e):
        p, self._press = self._press, None
        if (e.button() == Qt.MouseButton.LeftButton and p is not None
                and self._is_click(p[0], p[1], e.position().toPoint())):
            p[1].collapsed = not p[1].collapsed
            self._rebuild()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def _is_click(self, press_pos, node, release_pos):
        """“同一下点击”的判据：位移不超系统拖动阈值，且松手时还压在原来那个节点上。
        两条都要：只看位移会漏“按住节点拖了一下又拖回来”；只看命中会漏“原地拖”。"""
        tol = max(int(QApplication.startDragDistance()), 4)
        if (release_pos - press_pos).manhattanLength() > tol:
            return False
        return self._node_at(release_pos) is node

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

    # ---------- 导出（无水印） ----------
    def _export_name(self):
        """建议文件名：用文档名去掉后缀，加个“-脑图”。非法字符一律剔掉，
        不然带 : / 的标题会把保存对话框弄出一个存不了的默认名。"""
        base = os.path.splitext(str(self._doc_name or ""))[0]
        base = re.sub(r'[\\/:*?"<>|\r\n]', "", base).strip()
        return "%s-脑图" % base if base else "脑图"

    def _scene_rect(self):
        scene = self.scene()
        if scene is None:
            return QRectF()
        return scene.itemsBoundingRect().adjusted(-24, -24, 24, 24)

    def render_scene(self, painter, rect, w, h):
        """把场景里 rect 那一片画进传入的 painter（位图与 SVG 共用这一条）。
        只画节点与连线：不加水印、不加 Logo、不记时间，屏上看到什么就是什么。"""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self.scene().render(painter, QRectF(0, 0, float(w), float(h)), rect)

    def export_image(self, path, scale=2.0):
        """按扩展名导出：PNG/JPG/BMP/WEBP 走位图（默认 2 倍放大，图不够清晰就再调），
        .svg 走矢量。返回 (成功?, 给人看的一句话)。本函数不抛异常，调用方直接报结果。"""
        path = str(path or "")
        if not path:
            return False, "没选路径"
        if self.scene() is None:
            return False, "还没有可导出的内容"
        rect = self._scene_rect()
        if rect.isEmpty():
            return False, "脑图是空的，没什么可导出"
        s = max(float(scale or 1.0), 0.5)
        w = max(int(rect.width() * s), 1)
        h = max(int(rect.height() * s), 1)
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".svg":
                from PySide6.QtSvg import QSvgGenerator
                gen = QSvgGenerator()
                gen.setFileName(path)
                gen.setSize(QSize(w, h))
                gen.setViewBox(QRectF(0, 0, float(w), float(h)))
                gen.setTitle(self._export_name())
                p = QPainter(gen)
                p.fillRect(0, 0, w, h, _BG)          # SVG 默认透明底，铺一层与预览同色
                self.render_scene(p, rect, w, h)
                p.end()
                return True, "已导出 SVG 矢量图"
            fmt, label = _RASTER_EXPORT.get(ext, ("PNG", "PNG"))
            img = QImage(w, h, QImage.Format.Format_RGB32)
            img.fill(_BG)                            # JPG/BMP 不吃透明：先铺底
            p = QPainter(img)
            self.render_scene(p, rect, w, h)
            p.end()
            if img.save(path, fmt, 95 if fmt == "JPG" else -1):
                return True, "已导出 %s（%d×%d）" % (label, w, h)
            return False, "保存失败：路径不可写或磁盘满了"
        except Exception as e:                       # 导出失败绝不能把预览带崩
            return False, "导出失败：%s" % e

    def _do_export(self):
        """💾 按钮：选路径 -> 导出 -> 成/败各说一句。"""
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        path, sel = QFileDialog.getSaveFileName(
            self, "导出脑图（无水印）", self._export_name(), _EXPORT_FILTER)
        if not path:
            return
        if not os.path.splitext(path)[1]:
            # 用户只打了名字：按选中的过滤器补后缀，不留个“没后缀的文件”
            ext = ""
            for piece in sel or "":
                if "(*." in piece:
                    ext = piece.split("(*.")[1].rstrip(")").strip()
                    break
            path = "%s.%s" % (path, ext or "png")
        ok, msg = self.export_image(path)
        if ok:
            QMessageBox.information(self, "导出完成", "%s\n%s" % (msg, path))
        else:
            QMessageBox.warning(self, "导出失败", msg)
