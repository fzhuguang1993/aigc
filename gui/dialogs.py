"""
gui/dialogs.py —— 新建 / 编辑任务弹窗、搜索替换弹窗
"""
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QLineEdit,
                               QPlainTextEdit, QPushButton, QHBoxLayout, QLabel,
                               QCheckBox, QComboBox, QMessageBox, QSpinBox)

from store import product_store


class TaskDialog(QDialog):
    """task=None 为新建；编辑时传 {"num","product","prompt"}"""

    def __init__(self, parent=None, task=None):
        super().__init__(parent)
        self.setWindowTitle("新建任务" if task is None else "编辑任务")
        self.setFixedSize(520, 400)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)

        form = QFormLayout()
        self.ed_num = QLineEdit(str(task["num"]) if task else "")
        self.ed_num.setPlaceholderText("例如 1（用于文件命名）")
        # 品名：可从产品中心下拉选择，也可手动输入新名称
        self.ed_product = QComboBox()
        self.ed_product.setEditable(True)
        self.ed_product.addItems(product_store.product_names())
        self.ed_product.setCurrentText((task or {}).get("product", ""))
        self.ed_product.lineEdit().setPlaceholderText("选择已建产品或直接输入新品名")
        self.lbl_ref = QLabel("")
        self.lbl_ref.setStyleSheet("color:#2F6FED; background:transparent;")
        self.ed_product.currentTextChanged.connect(self._update_ref_hint)
        self._update_ref_hint(self.ed_product.currentText())
        self.ed_prompt = QPlainTextEdit()
        self.ed_prompt.setPlainText((task or {}).get("prompt", ""))
        self.ed_prompt.setPlaceholderText("描述你想要的视频内容，越具体生成效果越好…\n"
                                          "支持单元格内多行：换行会原样保存并可预览")
        if task:
            form.addRow("编 号", self.ed_num)      # 仅编辑时可改；新建时编号自增，不展示
        form.addRow("品 名", self.ed_product)
        form.addRow("", self.lbl_ref)
        form.addRow("提示词", self.ed_prompt)
        lay.addLayout(form)

        btns = QHBoxLayout()
        btns.addStretch(1)
        btn_to_products = QPushButton("去产品中心建档")
        btn_to_products.setObjectName("GhostBtn")
        btn_to_products.setToolTip("跳到产品中心新建产品并上传参考图")
        btn_to_products.clicked.connect(self._goto_products)
        btn_cancel = QPushButton("取消")
        btn_cancel.setObjectName("GhostBtn")
        btn_cancel.clicked.connect(self.reject)
        btn_ok = QPushButton("保 存")
        btn_ok.clicked.connect(self.accept)
        btns.addWidget(btn_to_products)
        btns.addStretch(1)
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_ok)
        lay.addLayout(btns)

    def _update_ref_hint(self, name):
        n = len(product_store.images_for_product(name)) if name else 0
        p = product_store.get_by_name(name) if name else None
        if p and p["images"]:
            self.lbl_ref.setText(f"📎 产品中心已登记 {n} 张参考图，提交时自动上传")
        elif name and name in product_store.product_names():
            self.lbl_ref.setText("⚠ 该产品还没登记参考图（可去产品中心补充）")
        elif name:
            self.lbl_ref.setText(f"🆕 「{name}」尚未建档，保存时会提示是否新建产品")
        else:
            self.lbl_ref.setText("")

    def _goto_products(self):
        """从弹窗直接跳到产品中心（顺手建档后保存任务并关窗）"""
        parent = self.parentWidget()
        while parent is not None and not hasattr(parent, "page_products"):
            parent = parent.parentWidget()
        if parent is None:
            return
        text = self.ed_product.currentText().strip()
        if text and text not in product_store.product_names():
            product_store.add_product(text)      # 顺手建档，省去重复输入
        parent.pages.setCurrentWidget(parent.page_products)
        nav = getattr(parent, "nav", None)
        if nav is not None:
            nav.setCurrentRow(1)
        self.done(QDialog.DialogCode.Accepted)   # 不走 accept()，避免重复弹建档提示

    def accept(self):
        """保存前检查：品名未在产品中心建档时，提示是否新建"""
        name = self.ed_product.currentText().strip()
        if name and name not in product_store.product_names():
            r = QMessageBox.question(
                self, "产品尚未建档",
                f"「{name}」还不在产品中心里。\n"
                "建档后可上传参考图（提交任务时自动上传）。\n\n"
                "是否现在在产品中心新建该产品？")
            if r == QMessageBox.StandardButton.Yes:
                product_store.add_product(name)
                self.ed_product.blockSignals(True)
                self.ed_product.clear()
                self.ed_product.addItems(product_store.product_names())
                self.ed_product.setCurrentText(name)
                self.ed_product.blockSignals(False)
                self.lbl_ref.setText("⚠ 新建产品还没有参考图，建议去产品中心补充")
        super().accept()

    def data(self):
        return {"num": self.ed_num.text().strip(),
                "product": self.ed_product.currentText().strip(),
                "prompt": self.ed_prompt.toPlainText().strip()}

    @staticmethod
    def ask(parent, task=None):
        dlg = TaskDialog(parent, task)
        return dlg.data() if dlg.exec() == QDialog.DialogCode.Accepted else None


class FindReplaceDialog(QDialog):
    """提示词搜索替换弹窗"""

    def __init__(self, parent=None, selected_count=0):
        super().__init__(parent)
        self.setWindowTitle("搜索替换提示词")
        self.setFixedWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)

        lay.addWidget(QLabel("查找内容："))
        self.ed_find = QLineEdit()
        self.ed_find.setPlaceholderText("例如：男明星")
        lay.addWidget(self.ed_find)

        lay.addWidget(QLabel("替换为："))
        self.ed_replace = QLineEdit()
        self.ed_replace.setPlaceholderText("例如：女明星（留空则删除该词）")
        lay.addWidget(self.ed_replace)

        self.cb_selected = QCheckBox(f"仅替换选中的 {selected_count} 个任务（不勾则全部）")
        self.cb_selected.setChecked(selected_count > 0)
        lay.addWidget(self.cb_selected)

        btns = QHBoxLayout()
        btns.addStretch(1)
        b_cancel = QPushButton("取消")
        b_cancel.setObjectName("GhostBtn")
        b_cancel.clicked.connect(self.reject)
        b_ok = QPushButton("全部替换")
        b_ok.clicked.connect(self.accept)
        btns.addWidget(b_cancel)
        btns.addWidget(b_ok)
        lay.addLayout(btns)

    @staticmethod
    def ask(parent, selected_count=0):
        dlg = FindReplaceDialog(parent, selected_count)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        find = dlg.ed_find.text()
        if not find:
            return None
        return {"find": find, "replace": dlg.ed_replace.text(),
                "only_selected": dlg.cb_selected.isChecked()}


class ForceRerunDialog(QDialog):
    """已完成、但提示词没改过的任务被要求重跑时的确认弹窗；
    单条重跑时额外提供「抽卡次数」（AI 随机性，同一条提示词多跑几遍挑效果最好的）"""

    MAX_REPEAT = 20

    def __init__(self, parent=None, task_id=None, count=1, allow_repeat=False):
        super().__init__(parent)
        self.setWindowTitle("强制重跑")
        self.setFixedWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)

        if count == 1:
            text = (f"任务{task_id} 已经执行成功过，且提示词与上次执行相比没有改动。\n"
                    "确认要原样重跑一遍看看效果吗？")
        else:
            text = (f"勾选的 {count} 个任务都已执行成功过，且提示词都没有改动。\n"
                    "确认要把它们原样各重跑一遍吗？")
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)

        self.spin_repeat = None
        if allow_repeat:
            lay.addSpacing(6)
            hint = QLabel("也可以让 AI「大力出奇迹」——同一条提示词一次多跑几遍，抽到满意的那条：")
            hint.setWordWrap(True)
            hint.setObjectName("PageTip")
            lay.addWidget(hint)
            row = QHBoxLayout()
            row.addWidget(QLabel("执行次数："))
            self.spin_repeat = QSpinBox()
            self.spin_repeat.setRange(1, self.MAX_REPEAT)
            self.spin_repeat.setValue(3)
            self.spin_repeat.setSuffix(" 次")
            self.spin_repeat.setToolTip(f"1-{self.MAX_REPEAT} 次，次数越多越容易抽到理想效果，"
                                        "也会占用更多云端并发额度")
            row.addWidget(self.spin_repeat)
            row.addStretch(1)
            lay.addLayout(row)

        btns = QHBoxLayout()
        btns.addStretch(1)
        b_cancel = QPushButton("跳过")
        b_cancel.setObjectName("GhostBtn")
        b_cancel.clicked.connect(self.reject)
        b_ok = QPushButton("强制重跑")
        b_ok.clicked.connect(self.accept)
        btns.addWidget(b_cancel)
        btns.addWidget(b_ok)
        lay.addLayout(btns)

    @staticmethod
    def ask(parent, task_id=None, count=1, allow_repeat=False):
        """确认返回 {"repeat": n}；取消返回 None"""
        dlg = ForceRerunDialog(parent, task_id=task_id, count=count,
                               allow_repeat=allow_repeat)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        repeat = dlg.spin_repeat.value() if dlg.spin_repeat else 1
        return {"repeat": repeat}
