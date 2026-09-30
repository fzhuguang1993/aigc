"""
gui/dialogs_blocks.py —— 「设置 → 🎬 素材类别」板块类别管理

维护一份规范的板块类别清单（增删、排序、恢复默认）：拆解详情页「✂ 切割入素材库」
手动归段时的类型下拉、素材库页的「类型」筛选下拉，都取自这份（core.block_categories，
写 config.json 的 breakdown.blocks_types 段，保存即生效）。与「内容标签」同一形态。
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout, QListWidget,
                               QLineEdit, QPushButton, QLabel, QMessageBox,
                               QListWidgetItem)

from core import block_categories
from gui.window_frame import apply_rounded


class BlockCategoryEditor(QWidget):
    """素材板块类别清单编辑器（可内嵌：设置页展开编辑栏；也可装进对话框）。
    点「保存类别」写盘并发 saved 信号。"""
    saved = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        tip = QLabel("爆款拆解切片段时用来归档的板块类型，也是素材库「类型」筛选、\n"
                     "混剪挑片的一组词。在这里新增/删除、拖动顺序；切割对话框与素材库\n"
                     "下拉就是这份。改完保存立即生效，不用重启。")
        tip.setStyleSheet("color:#6B7280;")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.lst = QListWidget()
        self.lst.setMinimumHeight(160)
        self.lst.setDragDropMode(QListWidget.DragDropMode.InternalMove)   # 拖动排序
        self.lst.setDefaultDropAction(Qt.DropAction.MoveAction)
        for t in block_categories.load():
            self.lst.addItem(QListWidgetItem(t))
        lay.addWidget(self.lst, 1)

        row = QHBoxLayout()
        self.ed = QLineEdit()
        self.ed.setPlaceholderText("新类别名，回车加入")
        self.ed.returnPressed.connect(self._add)
        b_add = QPushButton("＋ 添加")
        b_add.setObjectName("GhostBtn")
        b_add.clicked.connect(self._add)
        row.addWidget(self.ed, 1)
        row.addWidget(b_add)
        lay.addLayout(row)

        brow = QHBoxLayout()
        for label, fn in (("－ 删除选中", self._remove),
                          ("↑ 上移", lambda: self._move(-1)),
                          ("↓ 下移", lambda: self._move(1)),
                          ("↺ 恢复默认", self._restore)):
            b = QPushButton(label)
            b.setObjectName("GhostBtn")
            b.clicked.connect(fn)
            brow.addWidget(b)
        brow.addStretch(1)
        lay.addLayout(brow)

        b_save = QPushButton("💾 保存类别")
        b_save.clicked.connect(self._save)
        lay.addWidget(b_save)

    # ---------- 列表编辑 ----------
    def _names(self):
        return [self.lst.item(i).text() for i in range(self.lst.count())]

    def _add(self):
        name = self.ed.text().strip()
        if not name:
            return
        if name in self._names():
            QMessageBox.information(self, "已存在", f"类别「{name}」已在清单里")
            return
        self.lst.addItem(QListWidgetItem(name))
        self.ed.clear()

    def _remove(self):
        it = self.lst.currentItem()
        if it is None:
            QMessageBox.information(self, "提示", "先选中要删除的类别")
            return
        self.lst.takeItem(self.lst.row(it))

    def _move(self, step):
        r = self.lst.currentRow()
        t = r + step
        if r < 0 or not (0 <= t < self.lst.count()):
            return
        it = self.lst.takeItem(r)
        self.lst.insertItem(t, it)
        self.lst.setCurrentRow(t)

    def _restore(self):
        if QMessageBox.question(self, "恢复默认",
                                "把类别清单恢复成内置的默认六类？当前列表会被替换。") \
                != QMessageBox.StandardButton.Yes:
            return
        self.lst.clear()
        for t in block_categories.DEFAULT_CATEGORIES:
            self.lst.addItem(QListWidgetItem(t))

    # ---------- 保存 ----------
    def _save(self):
        names = self._names()
        if not [n for n in names if n.strip()]:
            QMessageBox.warning(self, "类别不能为空", "至少保留一个类别，否则素材没法归档")
            return
        block_categories.set_all(names)      # 去重清洗后写盘并立即生效
        self.saved.emit()


class BlockCategoryDialog(QDialog):
    """把 BlockCategoryEditor 装进对话框（保留独立弹窗入口）。"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("素材板块类别")
        self.resize(360, 440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        ed = BlockCategoryEditor(self)
        ed.saved.connect(self.accept)
        lay.addWidget(ed)
        apply_rounded(self, show_min=False, show_max=False)
