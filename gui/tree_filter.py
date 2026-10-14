"""
gui/tree_filter.py —— 素材库/成品库共用的「标签树 + 产品树」筛选面板

一棵竖排容器里上下两棵可折叠的 QTreeWidget：上=标签树、下=产品树。每棵树结构：
根节点「全部（N）」+ 每个取值一个子节点「值（计数）」；单选一个子节点即按该值过滤右侧
卡片网格，点根节点＝不过滤。选中变化通过信号回吐给页面，页面再去驱动顶栏同名下拉
（树为权威、下拉与树双向同步由页面负责，本组件只管渲染与回吐当前值）。

组件不认识业务：取值/计数由调用方算好传入（成品库标签=tag、素材库标签=block_type）。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                               QTreeWidget, QTreeWidgetItem)

_ARROW_OPEN, _ARROW_CLOSE = "▾", "▸"


class _TreeSection(QWidget):
    """一个可折叠小标题 + 一棵单选过滤树。

    折叠态自己记（不靠 tree.isVisible()：外层面板没 show 时 isVisible() 恒为假，
    会把「展开」误判成「收起」）。折叠时通过 expanded_changed 通知面板改拉伸权重，
    让本段收缩到只剩标题高度——否则外层给的固定权重会把唯一的小标题顶到中间。"""
    picked = Signal(str)          # 选中值（''=全部）
    expanded_changed = Signal(bool)   # 展开态变化（True=展开）

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self._title = title
        self._value = ""
        self._mute = False
        self._expanded = True     # 默认展开（与初始 setVisible 一致）
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        self.btn = QPushButton(f"{_ARROW_OPEN} {title}")
        self.btn.setObjectName("AccHead")
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.clicked.connect(self._toggle)
        head.addWidget(self.btn, 1)
        v.addLayout(head)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.setIndentation(14)
        self.tree.setRootIsDecorated(True)
        self.tree.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.tree.currentItemChanged.connect(self._on_change)
        v.addWidget(self.tree, 1)

    def is_expanded(self):
        return self._expanded

    def _toggle(self):
        self._expanded = not self._expanded
        self.tree.setVisible(self._expanded)
        self.btn.setText(f"{_ARROW_OPEN if self._expanded else _ARROW_CLOSE} {self._title}")
        self.expanded_changed.emit(self._expanded)

    def set_items(self, pairs, total, current=""):
        """pairs=[(值, 计数)]（已排序）；total=全部条数；current=应回选的当前值。"""
        self._value = current or ""
        self._mute = True
        self.tree.clear()
        root = QTreeWidgetItem([f"全部（{total}）"])
        root.setData(0, Qt.ItemDataRole.UserRole, "")
        f = root.font(0)
        f.setBold(True)
        root.setFont(0, f)
        self.tree.addTopLevelItem(root)
        target = root
        for value, count in pairs:
            node = QTreeWidgetItem([f"{value or '未分类'}（{count}）"])
            node.setData(0, Qt.ItemDataRole.UserRole, value)
            root.addChild(node)
            if (value or "") == self._value:
                target = node
        root.setExpanded(True)
        self.tree.setCurrentItem(target)
        self._mute = False

    def select_value(self, value):
        """外部（下拉）回选：不触发 picked（避免两路同步互相打转）。"""
        value = value or ""
        if value == self._value:
            return
        self._value = value
        self._mute = True
        root = self.tree.topLevelItem(0)
        node = root
        if root is not None and value:
            for i in range(root.childCount()):
                ch = root.child(i)
                if (ch.data(0, Qt.ItemDataRole.UserRole) or "") == value:
                    node = ch
                    break
        if node is not None:
            self.tree.setCurrentItem(node)
        self._mute = False

    def _on_change(self, cur, _prev):
        if self._mute or cur is None:
            return
        value = cur.data(0, Qt.ItemDataRole.UserRole) or ""
        if value == self._value:
            return
        self._value = value
        self.picked.emit(value)


class TreeFilterPanel(QWidget):
    """标签树 + 产品树 的组合面板（外层由页面包进白卡 Card）。"""
    tag_picked = Signal(str)
    product_picked = Signal(str)

    def __init__(self, tag_title="标签", product_title="产品", parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)
        self._lay = v
        self.tag = _TreeSection(tag_title)
        self.product = _TreeSection(product_title)
        self.tag.picked.connect(self.tag_picked)
        self.product.picked.connect(self.product_picked)
        v.addWidget(self.tag, 1)       # 索引 0
        v.addWidget(self.product, 1)   # 索引 1
        v.addStretch(0)                # 索引 2：两棵都收起时把标题钉在顶部
        self.tag.expanded_changed.connect(self._relayout)
        self.product.expanded_changed.connect(self._relayout)

    def _relayout(self, *_a):
        """按两棵树的展开态动态分配纵向权重：展开的占满剩余、收起的只留标题高度，
        都收起时补一段底部伸展把两个小标题固定在顶部（修「收起后按钮跑到中间」）。"""
        te = self.tag.is_expanded()
        pe = self.product.is_expanded()
        self._lay.setStretch(0, 1 if te else 0)
        self._lay.setStretch(1, 1 if pe else 0)
        self._lay.setStretch(2, 0 if (te or pe) else 1)

    def set_tag(self, pairs, total, current=""):
        self.tag.set_items(pairs, total, current)

    def set_product(self, pairs, total, current=""):
        self.product.set_items(pairs, total, current)

    def select_tag(self, value):
        self.tag.select_value(value)

    def select_product(self, value):
        self.product.select_value(value)
