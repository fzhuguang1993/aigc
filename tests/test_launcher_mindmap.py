"""
tests/test_launcher_mindmap.py —— Markdown 脑图：层级解析 + 逐级展开 + 无水印导出

parse_md_tree 与那批展开函数都是纯函数（只吃一棵树），钉死它们就不用真排版；
视图那一层再加几条离屏用例：默认只展开一级（长文档全铺开等于没脑图）、
导出真落得出 PNG/JPG/SVG 三种文件。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 早于任何 PySide6 导入

import pytest                     # noqa: E402
from PySide6.QtWidgets import QApplication              # noqa: E402
from gui.launcher_mindmap import (                     # noqa: E402
    MindMapView, Node, can_collapse_deeper, can_expand_deeper, collapse_one_level,
    expand_all, expand_next_level, parse_md_tree, set_expand_depth,
    walk_tree, walk_visible,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _titles(node):
    return [node.title] + [t for c in node.children for t in _titles(c)]


def test_heading_hierarchy():
    root = parse_md_tree("# A\n## B\n### C\n## D", title="doc")
    assert root.depth == 0 and root.title == "doc"
    a = root.children[0]
    assert a.title == "A" and a.depth == 1
    b, d = a.children
    assert b.title == "B" and d.title == "D" and b.depth == 2
    assert b.children[0].title == "C" and b.children[0].depth == 3


def test_deeper_heading_does_not_nest_wrong():
    # # A -> ## B -> # E：E 回到根下，不是 B 的孩子
    root = parse_md_tree("# A\n## B\n# E")
    assert [c.title for c in root.children] == ["A", "E"]
    assert root.children[0].children[0].title == "B"


def test_bullets_attach_to_current_heading():
    root = parse_md_tree("# A\n- x\n- y\n## B")
    a = root.children[0]
    # 两个列表项和 ## B（二级标题）都挂在 # A 下，按源码顺序
    assert [c.title for c in a.children] == ["x", "y", "B"]
    assert a.children[0].depth == 2          # 列表项挂在 A(depth1) 下一层
    assert a.children[0].children == []       # 列表项是叶子


def test_code_fence_hash_ignored():
    root = parse_md_tree("# 真标题\n```\n# 这不是标题\n- 也不是\n```\n## 子")
    assert root.children[0].title == "真标题"
    # 真标题之下只有"子"，代码围栏里的 # / - 都不算节点
    assert [c.title for c in root.children[0].children] == ["子"]


def test_chinese_and_body_text():
    root = parse_md_tree("# 中文标题\n这是正文一行")
    node = root.children[0]
    assert node.title == "中文标题"
    assert "这是正文一行" in node.body


def test_empty_and_malformed_returns_renderable_root():
    for bad in ("", None, "   ", "\n\n", "#", "########"):
        root = parse_md_tree(bad)
        assert isinstance(root, Node)
        assert root.depth == 0


# ---------------- 逐级展开（纯函数，不靠排版） ----------------

_DOC = "# A\n## A1\n### A11\n## A2\n# B\n## B1"


def _tree():
    return parse_md_tree(_DOC, title="文档.md")


def _seen(root):
    """当前看得见的节点标题（walk_visible 是栈式 DFS，同层顺序不定，统一排序比）"""
    return sorted(n.title for n in walk_visible(root))


def test_set_expand_depth_one_only_shows_first_level():
    """默认口径：只看一级标题（用户点名“全展开反而预览不便”）"""
    root = _tree()
    set_expand_depth(root, 1)
    assert _seen(root) == sorted(["文档.md", "A", "B"])


def test_set_expand_depth_clamps_below_one():
    """depth<1 按 1 处理：把根折上就是把整张图收成一颗孤点，那不是“收起”"""
    root = _tree()
    set_expand_depth(root, 0)
    assert root.collapsed is False
    assert _seen(root) == sorted(["文档.md", "A", "B"])


def test_expand_next_level_opens_the_whole_next_layer():
    """“展开下一级”是整层一起开，不是只开一个节点（用户原话：“展开所有的下一级”）"""
    root = _tree()
    set_expand_depth(root, 1)
    assert expand_next_level(root) is True
    assert _seen(root) == sorted(["文档.md", "A", "B", "A1", "A2", "B1"])
    assert expand_next_level(root) is True            # 再下探一层：A11 出来了
    assert "A11" in _seen(root)
    assert can_expand_deeper(root) is False
    assert expand_next_level(root) is False           # 已经全开了，按钮该置灰


def test_collapse_one_level_is_the_mirror():
    root = _tree()
    set_expand_depth(root, 1)
    assert can_collapse_deeper(root) is False         # 只剩一级：再收就没得收了
    expand_next_level(root)
    assert can_collapse_deeper(root) is True
    assert collapse_one_level(root) is True
    assert _seen(root) == sorted(["文档.md", "A", "B"])
    assert can_collapse_deeper(root) is False


def test_expand_all_covers_every_node():
    root = _tree()
    set_expand_depth(root, 1)
    expand_all(root)
    assert _seen(root) == sorted(n.title for n in walk_tree(root))


def test_set_expand_depth_two():
    """“按二级目录展开”：depth=2 就是展开到二级、三级往后全折着"""
    root = _tree()
    set_expand_depth(root, 2)
    assert _seen(root) == sorted(["文档.md", "A", "B", "A1", "A2", "B1"])
    assert "A11" not in _seen(root)


# ---------------- 视图：默认层级 + 无水印导出 ----------------

def test_view_defaults_to_one_level(qapp):
    """面板里打开一个长 md：默认只展开一级，且“还能往下展开/已经无可收”"""
    v = MindMapView()
    try:
        v.show_tree(parse_md_tree(_DOC, title="文档.md"), "文档.md")
        assert can_collapse_deeper(v._root) is False
        assert can_expand_deeper(v._root) is True
        # 显式传 depth 就得听用户的
        v.show_tree(parse_md_tree(_DOC, title="文档.md"), "文档.md", depth=2)
        assert can_collapse_deeper(v._root) is True
    finally:
        v.deleteLater()


@pytest.mark.parametrize("ext", [".png", ".jpg", ".bmp", ".svg"])
def test_export_image_writes_a_real_file(qapp, tmp_path, ext):
    """导出要多格式、要落得出真文件：水印一个都不加（导出前后场景里只有节点与连线）"""
    v = MindMapView()
    try:
        v.show_tree(parse_md_tree(_DOC, title="文档.md"), "文档.md")
        p = tmp_path / ("out%s" % ext)
        ok, msg = v.export_image(str(p))
        assert ok, msg
        assert p.exists() and p.stat().st_size > 0
        if ext == ".svg":
            data = p.read_text(encoding="utf-8", errors="ignore")
            assert "<svg" in data
        else:
            from PySide6.QtGui import QImage
            img = QImage(str(p))
            assert not img.isNull() and img.width() > 100 and img.height() > 100
    finally:
        v.deleteLater()


def test_export_empty_tree_says_so_instead_of_crashing(qapp, tmp_path):
    v = MindMapView()
    try:
        ok, msg = v.export_image(str(tmp_path / "x.png"))
        assert ok is False and msg
    finally:
        v.deleteLater()


def test_export_name_strips_illegal_chars(qapp):
    """建议文件名带非法字符会把保存对话框弄出一个存不了的默认名"""
    v = MindMapView()
    try:
        v.show_tree(parse_md_tree("# A", title="x"), 'a/b:c*d?.md')
        assert v._export_name() == "abcd-脑图"
        v.show_tree(parse_md_tree("# A"), "")
        assert v._export_name() == "脑图"
    finally:
        v.deleteLater()


# ---------------- 交互：点一下才折叠，按住拖拽不该改形态 ----------------

def _a_parent_node(v):
    """拿一个“看得见、有孩子”的节点（默认只展开一级，所以一级标题正合适）"""
    return next(n for n in walk_visible(v._root) if n.depth == 1 and n.children)


def _spot(v, node):
    """节点矩形中心在 viewport 里的坐标（QGraphicsView 收的是视口坐标；
    mapToViewport 在 Qt6 是 protected，所以走 viewport.mapFrom(view)）"""
    return v.viewport().mapFrom(v, v.mapFromScene(v._pos[id(node)].center()))


def _mk_view(qapp):
    v = MindMapView()
    v.resize(900, 600)
    v.show_tree(_tree(), "文档.md")
    v.show()
    qapp.processEvents()
    return v


def test_pressing_a_node_does_not_toggle_until_release(qapp):
    """按下那一下不得先动手：用户要的正是“按住不放 = 拖拽”"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    v = _mk_view(qapp)
    try:
        n = _a_parent_node(v)
        before = n.collapsed
        QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         _spot(v, n))
        assert n.collapsed == before, "按下就改状态，拖拽永远拖不动"
        QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, _spot(v, n))
        assert n.collapsed != before, "原地按下再松开＝点击，要折叠/展开"
    finally:
        v.deleteLater()


def test_dragging_from_a_node_pans_instead_of_toggling(qapp):
    """按住节点拖出去：那是平移视图，松手不该把节点折上/展开"""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    v = _mk_view(qapp)
    try:
        n = _a_parent_node(v)
        before = n.collapsed
        p = _spot(v, n)
        QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
        QTest.mouseMove(v.viewport(), p + QPoint(60, 40))    # PySide6 这一重载不接按钮参数
        QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier, p + QPoint(60, 40))
        assert n.collapsed == before, "拖拽被当成了点击"
    finally:
        v.deleteLater()
