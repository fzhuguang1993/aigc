"""
gui/dialogs_tags.py —— 「设置 → 🏷 内容标签」词库管理对话框

维护一份规范的标签清单（增删、排序、恢复默认）：任务表只能从这里挑标签，
批量归档时目录名也用它。写盘走 core.tags（config.json 的 tags 段），保存即生效。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QListWidget,
                               QLineEdit, QPushButton, QLabel, QMessageBox,
                               QDialogButtonBox, QListWidgetItem)

from core import tags as tag_lib


class TagManagerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("内容标签词库")
        self.resize(360, 420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)

        tip = QLabel("给任务打内容标签用的一组词，也是批量归档时的子文件夹名。\n"
                     "在这里新增/删除、拖动顺序；任务表「标签」列下拉就是这份。")
        tip.setStyleSheet("color:#6B7280;")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.lst = QListWidget()
        self.lst.setDragDropMode(QListWidget.DragDropMode.InternalMove)   # 拖动排序
        self.lst.setDefaultDropAction(Qt.DropAction.MoveAction)
        for t in tag_lib.load():
            self.lst.addItem(QListWidgetItem(t))
        lay.addWidget(self.lst, 1)

        row = QHBoxLayout()
        self.ed = QLineEdit()
        self.ed.setPlaceholderText("新标签名，回车加入")
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

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                              QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Save).setText("💾 保存词库")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # ---------- 列表编辑 ----------
    def _names(self):
        return [self.lst.item(i).text() for i in range(self.lst.count())]

    def _add(self):
        name = self.ed.text().strip()
        if not name:
            return
        if name in self._names():
            QMessageBox.information(self, "已存在", f"标签「{name}」已在词库里")
            return
        self.lst.addItem(QListWidgetItem(name))
        self.ed.clear()

    def _remove(self):
        it = self.lst.currentItem()
        if it is None:
            QMessageBox.information(self, "提示", "先选中要删除的标签")
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
                                "把词库恢复成内置的默认标签？当前列表会被替换。") \
                != QMessageBox.StandardButton.Yes:
            return
        self.lst.clear()
        for t in tag_lib.DEFAULT_TAGS:
            self.lst.addItem(QListWidgetItem(t))

    # ---------- 保存 ----------
    def accept(self):
        names = self._names()
        if not [n for n in names if n.strip()]:
            QMessageBox.warning(self, "词库不能为空", "至少保留一个标签，否则任务没法打标签")
            return
        tag_lib.set_all(names)      # 去重清洗后写盘并立即生效
        super().accept()
