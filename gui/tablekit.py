"""
gui/tablekit.py —— 表格通用能力：勾选框列 + 字段管理（显示/隐藏 + 拖拽排序）+ 秒数单元格
任务中心与执行记录共用同一套逻辑。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel,
                               QListWidget, QListWidgetItem, QVBoxLayout,
                               QTableWidgetItem)

from gui.formatting import secs


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
    """字段管理：列表内拖拽调整顺序，勾选控制显示/隐藏。

    columns: [(logical, 标题), ...]  需要管理的列（不含勾选列）
    order:   [logical, ...]          当前从左到右的顺序
    hidden:  {logical, ...}          当前隐藏的列
    结果属性：.order（全部列的最终顺序，隐藏列排在后） .hidden
    """

    def __init__(self, parent, columns, order, hidden):
        super().__init__(parent)
        self.setWindowTitle("字段管理")
        self.resize(300, 460)
        self._title = dict(columns)
        lay = QVBoxLayout(self)
        tip = QLabel("拖动调整顺序 · 取消勾选即隐藏该列")
        tip.setStyleSheet("color:#646A73; font-size:12px;")
        lay.addWidget(tip)
        self.lst = QListWidget()
        self.lst.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.lst.setDefaultDropAction(Qt.DropAction.MoveAction)
        for lg in list(order) + [c for c, _ in columns if c not in order]:
            it = QListWidgetItem(self._title.get(lg, str(lg)))
            it.setData(Qt.ItemDataRole.UserRole, lg)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked if lg in hidden
                             else Qt.CheckState.Checked)
            self.lst.addItem(it)
        lay.addWidget(self.lst)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def accept(self):
        self.order = [self.lst.item(i).data(Qt.ItemDataRole.UserRole)
                      for i in range(self.lst.count())
                      if self.lst.item(i).checkState() == Qt.CheckState.Checked]
        all_cols = [self.lst.item(i).data(Qt.ItemDataRole.UserRole)
                    for i in range(self.lst.count())]
        self.hidden = {x for x in all_cols if x not in self.order}
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
