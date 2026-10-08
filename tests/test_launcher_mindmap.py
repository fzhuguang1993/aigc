"""
tests/test_launcher_mindmap.py —— Markdown 图形脑图的纯解析函数

parse_md_tree 是这整块里唯一能脱离 Qt 单测的纯函数（层级/列表并入/代码围栏忽略），
钉死它，剩下的 QGraphicsView 只是把树画出来，交给离屏冒烟即可。
"""
from gui.launcher_mindmap import Node, parse_md_tree


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
