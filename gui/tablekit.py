"""
gui/tablekit.py —— 表格通用能力：勾选框列 + 字段管理（显示/隐藏 + 拖拽排序）+ 秒数单元格
任务中心与执行记录共用同一套逻辑。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QHBoxLayout,
                               QListWidget, QListWidgetItem, QVBoxLayout,
                               QTableWidgetItem, QCheckBox, QGroupBox,
                               QScrollArea, QWidget, QPushButton)

from gui.formatting import secs
from gui.window_frame import apply_rounded


class SecsItem(QTableWidgetItem):
    """秒数单元格：显示成「4分58秒」，点表头排序仍按秒数比大小

    不这么做两头总有一头错：QTableWidget 内建排序比的是单元格文本，“10分30秒”
    会被排到“4分58秒”前面；而只挂 DisplayRole=数字又会把格子显示成 298。
    重载 < 才是文字和顺序两头都对的做法。"""
    # 秒数存哪个角色：UserRole 已被“行→任务ID”占了；EditRole 也不能用——
    # QTableWidgetItem 没人给它存过数据时会回落到显示文本，拿回来是“4分58秒”而不是 298
    SEC_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, seconds=0, parent=None):
        super().__init__(parent)
        self.set_secs(seconds)

    def set_secs(self, seconds):
        try:
            d = int(seconds or 0)
        except (TypeError, ValueError):
            d = 0
        self.setData(self.SEC_ROLE, d)
        self.setText(secs(d))

    def seconds(self):
        return int(self.data(self.SEC_ROLE) or 0)

    def __lt__(self, other):
        if isinstance(other, SecsItem):
            return self.seconds() < other.seconds()
        return super().__lt__(other)


class FieldManagerDialog(QDialog):
    """字段管理弹窗（对标推广后台）：左＝按分类勾选要显示的字段，右＝拖动调整列顺序。

    columns: [(logical, 标题), ...]  需要管理的列（不含锁定列，锁定列由
             apply_field_layout 的 first_locked 保护，这里传进来的都是可管列）
    order:   [logical, ...]          当前可见列从左到右的顺序（隐藏列可不传）
    hidden:  {logical, ...}          当前隐藏的列
    categories: [(分类名, [标题, ...]), ...]  给了左侧就分组，否则单组平铺
    default_order / default_hidden：  给了才显示「↺ 恢复默认」按钮
    结果属性：.order（确认后最终可见列顺序） .hidden（隐藏列集合）
    """

    def __init__(self, parent, columns, order, hidden, categories=None,
                 default_order=None, default_hidden=None,
                 title="字段管理",
                 tip="勾选要显示的字段 · 右侧拖动调整列顺序 · 点「确定」应用"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 460)
        self._title = dict(columns)              # logical → 标题
        self._col_by_name = {t: c for c, t in columns}
        self._all_cols = [c for c, _ in columns]
        self._def_order = default_order
        self._def_hidden = set(default_hidden or ())
        self._syncing = False
        self._boxes = {}                         # logical → QCheckBox

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(10)
        head = QLabel(tip)
        head.setObjectName("PageTip")
        head.setWordWrap(True)
        v.addWidget(head)

        body = QHBoxLayout()
        body.setSpacing(14)
        # ---------- 左：分类勾选（显示 / 隐藏）----------
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)
        groups = categories or [("全部字段", [t for _, t in columns])]
        for gname, names in groups:
            box = QGroupBox(gname)
            gv = QVBoxLayout(box)
            gv.setContentsMargins(10, 6, 10, 8)
            gv.setSpacing(4)
            for name in names:
                col = self._col_by_name.get(name)
                if col is None:
                    continue
                cb = QCheckBox(name)
                cb.setChecked(col not in hidden)
                cb.setCursor(Qt.CursorShape.PointingHandCursor)
                cb.toggled.connect(lambda _on, c=col: self._on_toggle(c))
                gv.addWidget(cb)
                self._boxes[col] = cb
            lv.addWidget(box)
        lv.addStretch(1)
        lscroll = QScrollArea()
        lscroll.setWidgetResizable(True)
        lscroll.setFrameShape(QScrollArea.Shape.NoFrame)
        lscroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        lscroll.setWidget(left)
        lscroll.setFixedWidth(240)
        body.addWidget(lscroll)

        # ---------- 右：顺序预览（拖动 = 表格列最终顺序）----------
        rbox = QVBoxLayout()
        rbox.setSpacing(4)
        rlabel = QLabel("显示顺序 · 上下拖动调整")
        rlabel.setObjectName("PageTip")
        rbox.addWidget(rlabel)
        self.lst = QListWidget()
        self.lst.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.lst.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.lst.setToolTip("这里就是表格列的最终顺序：拖动条目调整，点「确定」应用")
        for col in order:
            if col in self._title and col not in hidden:
                self._add_item(col)
        rbox.addWidget(self.lst, 1)
        body.addLayout(rbox, 1)
        v.addLayout(body, 1)

        # ---------- 底部：全选 / 全不选 / 恢复默认 + 确定 / 取消 ----------
        foot = QHBoxLayout()
        b_all = QPushButton("全选")
        b_all.setObjectName("GhostBtn")
        b_all.clicked.connect(lambda: self._set_all(True))
        b_none = QPushButton("全不选")
        b_none.setObjectName("GhostBtn")
        b_none.clicked.connect(lambda: self._set_all(False))
        foot.addWidget(b_all)
        foot.addWidget(b_none)
        if default_order is not None:
            b_reset = QPushButton("↺ 恢复默认")
            b_reset.setObjectName("GhostBtn")
            b_reset.clicked.connect(self._reset_default)
            foot.addWidget(b_reset)
        foot.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        foot.addWidget(bb)
        v.addLayout(foot)

        self.order = list(order)
        self.hidden = set(hidden)
        apply_rounded(self, show_min=False, show_max=False)

    def _add_item(self, col):
        it = QListWidgetItem(self._title.get(col, str(col)))
        it.setData(Qt.ItemDataRole.UserRole, col)
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsDragEnabled)
        self.lst.addItem(it)

    def _row_of(self, col):
        for i in range(self.lst.count()):
            if self.lst.item(i).data(Qt.ItemDataRole.UserRole) == col:
                return i
        return None

    def _on_toggle(self, col):
        """左侧勾选变化：即时增删右侧预览条目（同步期不重入）"""
        if self._syncing:
            return
        self._syncing = True
        vis = self._boxes[col].isChecked()
        row = self._row_of(col)
        if vis and row is None:
            self._add_item(col)
        elif not vis and row is not None:
            self.lst.takeItem(row)
        self._syncing = False

    def _set_all(self, on):
        self._syncing = True
        for col, cb in self._boxes.items():
            cb.setChecked(on)
            row = self._row_of(col)
            if on and row is None:
                self._add_item(col)
            elif not on and row is not None:
                self.lst.takeItem(row)
        self._syncing = False

    def _reset_default(self):
        self._syncing = True
        self.lst.clear()
        for col in self._all_cols:
            self._boxes[col].setChecked(col not in self._def_hidden)
        for col in (self._def_order or []):
            if col in self._title and col not in self._def_hidden:
                self._add_item(col)
        self._syncing = False

    def accept(self):
        self.order = [self.lst.item(i).data(Qt.ItemDataRole.UserRole)
                      for i in range(self.lst.count())]
        self.hidden = {c for c in self._all_cols if c not in self.order}
        super().accept()


def apply_field_layout(table, order, hidden, first_locked=1):
    """把 order/hidden 应用到 QTableWidget。

    first_locked: 前 N 列（勾选列等）固定在最左，不参与重排。
    注意：order/hidden 使用逻辑列号；锁定列不要出现在 hidden 里。
    """
    h = table.horizontalHeader()
    total = table.columnCount()
    locked = list(range(first_locked))
    full = locked + [c for c in order if c not in locked] \
        + [c for c in range(total) if c not in locked and c not in order]
    for lg in range(total):
        table.setColumnHidden(lg, lg in hidden)
    guard = {"on": False}

    def _do():
        guard["on"] = True
        for i, lg in enumerate(full):
            v = h.visualIndex(lg)
            if v != i:
                h.moveSection(v, i)
        guard["on"] = False

    if getattr(table, "_ft_guard", None) is None:
        table._ft_guard = guard
    _do()


def enable_drag_with_lock(table, lock_count=1):
    """开启表头拖拽调序，并锁定最左 lock_count 列不被拖走。"""
    h = table.horizontalHeader()
    h.setSectionsMovable(True)

    def on_moved(logical, old_v, new_v):
        if getattr(table, "_ft_moving", False):
            return
        # 若锁定列被挪位，复位到最左
        bad = [lg for lg in range(lock_count) if h.visualIndex(lg) != lg]
        if bad:
            table._ft_moving = True
            for lg in range(lock_count):          # 依次把锁定列拉回原位
                h.moveSection(h.visualIndex(lg), lg)
            table._ft_moving = False

    h.sectionMoved.connect(on_moved)
