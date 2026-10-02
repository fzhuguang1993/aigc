"""
gui/tablekit.py —— 表格通用能力：勾选框列 + 字段管理（显示/隐藏 + 拖拽排序）+ 秒数单元格
任务中心与执行记录共用同一套逻辑。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QHBoxLayout,
                               QVBoxLayout, QTableWidgetItem, QPushButton, QCheckBox)

from gui.formatting import secs
from gui.kit import FieldManager
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
    """字段管理弹窗：薄封装 UI 库组件 FieldManager（左分类勾选 / 右流式拖拽排序）。

    对外契约保持不变：构造签名、结果属性 .order（确认后最终可见列顺序）/.hidden
    （隐藏列集合）与 apply_field_layout 直接对接；三个调用方无需改动。

    columns: [(logical, 标题), ...] 需要管理的列（不含锁定列）
    order:   [logical, ...] 当前可见列从左到右顺序
    hidden:  {logical, ...} 当前隐藏的列
    categories: [(分类名, [标题, ...]), ...] 给了左侧按分类分组，否则单组平铺
    default_order / default_hidden：给了才显示「↺ 恢复默认」按钮
    """

    def __init__(self, parent, columns, order, hidden, categories=None,
                 default_order=None, default_hidden=None,
                 title="字段管理",
                 tip="勾选要显示的字段 · 右侧拖动调整列顺序 · 点「确定」应用"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(600, 500)

        fields, cats = self._to_fields(columns, categories)
        self.fm = FieldManager(fields, order, hidden, categories=cats)

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(10)
        head = QLabel(tip)
        head.setObjectName("PageTip")
        head.setWordWrap(True)
        v.addWidget(head)
        v.addWidget(self.fm, 1)

        foot = QHBoxLayout()
        b_all = QPushButton("全选")
        b_all.setObjectName("GhostBtn")
        b_all.clicked.connect(lambda: self.fm.set_all(True))
        b_none = QPushButton("全不选")
        b_none.setObjectName("GhostBtn")
        b_none.clicked.connect(lambda: self.fm.set_all(False))
        foot.addWidget(b_all)
        foot.addWidget(b_none)
        if default_order is not None:
            b_reset = QPushButton("↺ 恢复默认")
            b_reset.setObjectName("GhostBtn")
            b_reset.clicked.connect(
                lambda: self.fm.reset(default_order, default_hidden))
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

    @staticmethod
    def _to_fields(columns, categories):
        """把调用方的 (logical,标题) + [(分类,[标题])] 映射成 FieldManager 的
        (id,label,category) 字段表与 [(分类,[ids])] 分组；categories 缺省则单组平铺。"""
        col_by_title = {t: c for c, t in columns}
        if not categories:
            return [(c, t, "字段") for c, t in columns], None
        fields, cats, covered = [], [], set()
        for gname, titles in categories:
            ids = []
            for t in titles:
                c = col_by_title.get(t)
                if c is None:
                    continue
                ids.append(c)
                fields.append((c, t, gname))
                covered.add(c)
            if ids:
                cats.append((gname, ids))
        leftover = [(c, t) for c, t in columns if c not in covered]
        if leftover:
            fields += [(c, t, "其他") for c, t in leftover]
            cats.append(("其他", [c for c, _ in leftover]))
        return fields, cats

    def accept(self):
        self.order = self.fm.order
        self.hidden = self.fm.hidden
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


def mount_header_checkbox(table, col, on_toggle, state_provider=None):
    """在表头某一列挂一枚**可见**的全选勾选框（对标主流后台：勾选列表头就该有个框）。

    为什么需要：以前只有「点表头那一格」才全选，格子上没框，用户根本不知道能全选、
    也不知去哪全选。这里摆一枚三态框：本页全选=Checked、部分=PartiallyChecked、全不选=Unchecked。

    on_toggle(state): 用户点框后回调（state 为点击后的新勾选态；宿主页据此全选/清空本页）。
    state_provider(): 返回当前应有的三态；调用方在数据变化后调 cb.sync_state() 同步框态。
    返回的 QCheckBox 以表头为父，随列宽 / 移动 / 横向滚动自动重定位；并带两个便捷方法：
    reposition() 重新摆位、sync_state() 按 provider 刷新三态。
    """
    header = table.horizontalHeader()
    cb = QCheckBox(header)
    cb.setCursor(Qt.CursorShape.PointingHandCursor)
    cb.setText("")                       # 只要那个方框，别占文字位
    guard = {"on": False}

    def reposition():
        if header.isSectionHidden(col):
            cb.hide()
            return
        w, h = header.sectionSize(col), header.height()
        x = header.sectionViewportPosition(col)
        box = 18                          # 紧凑方框，在该列里水平/垂直居中
        cb.setGeometry(x + max(0, (w - box) // 2), max(0, (h - box) // 2), box, box)
        cb.show()
        cb.raise_()

    def sync_state():
        if state_provider is None:
            return
        guard["on"] = True               # 程序设态不触发 on_toggle（否则会误全选/清空）
        cb.setCheckState(state_provider())
        guard["on"] = False

    def _changed(_state):
        if guard["on"]:
            return
        on_toggle(cb.checkState())
        sync_state()                       # 宿主改完行勾选后回写真实三态

    cb.stateChanged.connect(_changed)
    cb.reposition = reposition
    cb.sync_state = sync_state
    for sig in (header.sectionResized, header.sectionMoved, header.geometriesChanged):
        sig.connect(lambda *a: reposition())
    table.horizontalScrollBar().valueChanged.connect(lambda *a: reposition())
    reposition()
    return cb
