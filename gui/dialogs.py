"""
gui/dialogs.py —— 新建 / 编辑任务弹窗、查找/替换弹窗、批量绑定脚本弹窗
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QLineEdit,
                               QPlainTextEdit, QPushButton, QHBoxLayout, QLabel,
                               QCheckBox, QComboBox, QMessageBox, QSpinBox)

from store import product_store


class TaskDialog(QDialog):
    """task=None 为新建；编辑时传 {"num","product","script","prompt","remark"}
    品名/脚本均可留空：通版素材可以不关联任何产品/脚本
    编号不给改：它是文件命名（品名_编号_姓名）和数据行的锚，改了已有
    产物的对应关系就断了，所以表单里干脆不放编号输入框，只随 data() 透传"""

    def __init__(self, parent=None, task=None):
        super().__init__(parent)
        self.setWindowTitle("新建任务" if task is None else "编辑任务")
        self.setFixedSize(540, 520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)

        form = QFormLayout()
        self._num = str((task or {}).get("num") or "")   # 只做透传，不给编辑
        # 品名：可从产品中心下拉选择，也可手动输入新名称，通版素材可留空
        self.ed_product = QComboBox()
        self.ed_product.setEditable(True)
        self.ed_product.addItems(product_store.product_names())
        self.ed_product.setCurrentText((task or {}).get("product", ""))
        self.ed_product.lineEdit().setPlaceholderText("选择已建产品或直接输入；可留空（通版素材不关联产品）")
        self.lbl_ref = QLabel("")
        self.lbl_ref.setStyleSheet("color:#2F6FED; background:transparent;")
        # 备注：使用者自己标的管理记号（任务多了靠它分组），不参与任何业务判断
        self.ed_remark = QLineEdit((task or {}).get("remark", ""))
        self.ed_remark.setPlaceholderText("（可选）随手标一句：已过审 / 待重拍 / 只发视频号…"
                                          "；表格里双击备注列可就地改")
        self.ed_product.currentTextChanged.connect(self._update_ref_hint)
        self._update_ref_hint(self.ed_product.currentText())
        # 脚本：使用软件的人自己填（人物/场景等）；口播文案由系统从提示词自动识别，两者分开
        self.ed_script = QPlainTextEdit()
        self.ed_script.setPlainText((task or {}).get("script", ""))
        self.ed_script.setPlaceholderText("（可选，自己填）脚本：人物/场景/分镜拆解等创作文档；\n"
                                          "一个脚本可绑多条提示词，列表页右键可「批量绑定脚本」")
        self.ed_script.setFixedHeight(72)
        self.ed_prompt = QPlainTextEdit()
        self.ed_prompt.setPlainText((task or {}).get("prompt", ""))
        self.ed_prompt.setPlaceholderText("描述你想要的视频内容，越具体生成效果越好…\n"
                                          "支持单元格内多行：换行会原样保存并可预览")
        form.addRow("品 名", self.ed_product)
        form.addRow("", self.lbl_ref)
        form.addRow("备 注", self.ed_remark)
        form.addRow("脚 本", self.ed_script)
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
        if not name:
            self.lbl_ref.setText("ℹ 留空不关联产品：适合工厂流水线等通版素材")
            return
        n = len(product_store.images_for_product(name)) if name else 0
        p = product_store.get_by_name(name) if name else None
        if p and p["images"]:
            self.lbl_ref.setText(f"📎 产品中心已登记 {n} 张参考图，提交时自动上传")
        elif name and name in product_store.product_names():
            self.lbl_ref.setText("⚠ 该产品还没登记参考图（可去产品中心补充）")
        else:
            self.lbl_ref.setText(f"🆕 「{name}」尚未建档，保存时会提示是否新建产品")

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
        return {"num": self._num,
                "product": self.ed_product.currentText().strip(),
                "remark": self.ed_remark.text().strip(),
                "script": self.ed_script.toPlainText().strip(),
                "prompt": self.ed_prompt.toPlainText().strip()}

    @staticmethod
    def ask(parent, task=None):
        dlg = TaskDialog(parent, task)
        return dlg.data() if dlg.exec() == QDialog.DialogCode.Accepted else None


class ScriptBindDialog(QDialog):
    """批量绑定脚本：一个脚本多条提示词（单脚本多提示词场景）。
    留空保存 = 清除所选任务的脚本（不关联任何脚本，如工厂流水线片段）"""

    def __init__(self, parent=None, count=1, current=""):
        super().__init__(parent)
        self.setWindowTitle("批量绑定脚本")
        self.setFixedSize(520, 360)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lbl = QLabel(f"将以下脚本内容绑定到选中的 <b>{count}</b> 个任务（覆盖原有脚本）：")
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        self.ed_script = QPlainTextEdit()
        self.ed_script.setPlainText(current)
        self.ed_script.setPlaceholderText("脚本：人物/场景/分镜拆解等；留空保存则清除这些任务的脚本关联")
        lay.addWidget(self.ed_script, 1)
        btns = QHBoxLayout()
        btns.addStretch(1)
        b_cancel = QPushButton("取消")
        b_cancel.setObjectName("GhostBtn")
        b_cancel.clicked.connect(self.reject)
        b_ok = QPushButton("绑定")
        b_ok.clicked.connect(self.accept)
        btns.addWidget(b_cancel)
        btns.addWidget(b_ok)
        lay.addLayout(btns)

    @staticmethod
    def ask(parent, count=1, current=""):
        """返回脚本文本（可为空串=清除）；取消返回 None"""
        dlg = ScriptBindDialog(parent, count, current)
        return dlg.ed_script.toPlainText().strip() if \
            dlg.exec() == QDialog.DialogCode.Accepted else None


class FindReplaceDialog(QDialog):
    """提示词查找 / 替换弹窗：默认只“查找”（像 Excel），点「替换 »」才展开替换行。"""

    def __init__(self, parent=None, selected_count=0):
        super().__init__(parent)
        self.setWindowTitle("查找 / 替换提示词")
        self.setMinimumWidth(460)
        self.setMaximumWidth(460)
        self._selected_count = selected_count
        self._mode = "find"          # find=只查找(定位勾选)  replace=替换
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)

        lay.addWidget(QLabel("查找内容："))
        self.ed_find = QLineEdit()
        self.ed_find.setPlaceholderText("例如：男明星")
        lay.addWidget(self.ed_find)

        # 替换行：默认收起，点「替换 »」才展开（不展开就是纯搜索）
        self.lbl_replace = QLabel("替换为：")
        lay.addWidget(self.lbl_replace)
        self.ed_replace = QLineEdit()
        self.ed_replace.setPlaceholderText("例如：女明星（留空则删除该词）")
        lay.addWidget(self.ed_replace)

        self.cb_selected = QCheckBox("")
        self.cb_selected.setChecked(selected_count > 0)
        lay.addWidget(self.cb_selected)

        self.tip = QLabel("")
        self.tip.setWordWrap(True)
        self.tip.setObjectName("PageTip")
        lay.addWidget(self.tip)

        btns = QHBoxLayout()
        self.b_toggle = QPushButton("替换 »")
        self.b_toggle.setObjectName("GhostBtn")
        self.b_toggle.setToolTip("展开替换：填入替换词后「全部替换」；收起则只做查找定位")
        self.b_toggle.clicked.connect(self._toggle_mode)
        btns.addWidget(self.b_toggle)
        btns.addStretch(1)
        b_cancel = QPushButton("取消")
        b_cancel.setObjectName("GhostBtn")
        b_cancel.clicked.connect(self.reject)
        self.b_ok = QPushButton("查找全部")
        self.b_ok.clicked.connect(self.accept)
        btns.addWidget(b_cancel)
        btns.addWidget(self.b_ok)
        lay.addLayout(btns)

        self._apply_mode()

    def _toggle_mode(self):
        self._mode = "replace" if self._mode == "find" else "find"
        self._apply_mode()

    def _apply_mode(self):
        replacing = self._mode == "replace"
        verb = "替换" if replacing else "查找"
        self.lbl_replace.setVisible(replacing)
        self.ed_replace.setVisible(replacing)
        n = self._selected_count
        if n:
            self.cb_selected.setText(f"仅在选中的 {n} 个任务中{verb}（不勾则全部）")
            self.cb_selected.setVisible(True)
        else:
            self.cb_selected.setText(f"在全部任务中{verb}")
            self.cb_selected.setVisible(False)
        self.b_toggle.setText("« 查找" if replacing else "替换 »")
        self.b_ok.setText("全部替换" if replacing else "查找全部")
        self.tip.setText(
            "替换后这批命中的任务会自动勾选并排到列表最前面（每页条数不够时自动提升），"
            "直接点「执行选中」就只重跑它们。" if replacing else
            "「查找全部」不改提示词：按关键词找出的任务会自动勾选并置顶，可直接「执行选中」。")
        if not replacing:
            self.ed_replace.clear()        # 收起时不残留替换词，避免下次误替换
        self.adjustSize()

    @staticmethod
    def ask(parent, selected_count=0):
        dlg = FindReplaceDialog(parent, selected_count)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        find = dlg.ed_find.text()
        if not find:
            return None
        replacing = dlg._mode == "replace"
        return {"find": find, "replace": dlg.ed_replace.text() if replacing else "",
                "only_selected": dlg.cb_selected.isChecked(),
                "locate_only": not replacing}


class ForceRerunDialog(QDialog):
    """重跑确认弹窗。两种背景都要它能说清（同一个入口点下去，两类任务可能都有）：

    1. **已跑完但提示词没改**（force）：旧版只会一句“已经执行成功过”，这是原先唯一的
       弹窗形态；
    2. **还有执行在云端排队/生成中**（running）：不先取消就重提，新旧两份会同时占
       并发、各出一个成品（同事反馈现场）。这种情况多一个「⏹ 取消并重跑」按钮，
       并把它设为默认项。

    单条重跑时额外提供「抽卡次数」（AI 随机性，同一条提示词多跑几遍挑效果最好的）。"""

    MAX_REPEAT = 20

    def __init__(self, parent=None, task_id=None, count=1, allow_repeat=False,
                 params_note="", running=None):
        super().__init__(parent)
        self.setWindowTitle("强制重跑")
        self.setFixedWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        running = running or {}          # {任务ID: 正在进行的执行个数}
        # 本次选择：“取消并重跑”与“保留并直接重跑”是两个按钮、一份文案，
        # 靠 self.cancel_first 区分，不用 QDialog.Accepted 的变体玩花样
        self.cancel_first = False

        lines = []
        if count:
            if count == 1:
                lines.append(f"任务{task_id} 已经执行成功过，且提示词与上次执行相比没有改动。\n"
                             "确认要重跑一遍看看效果吗？")
            else:
                lines.append(f"勾选的 {count} 个任务都已执行成功过，且提示词都没有改动。\n"
                             "确认要把它们各重跑一遍吗？")
        if running:
            n_exec = sum(running.values())
            ids = "、".join(str(t) for t in sorted(running)[:6]) + ("…" if len(running) > 6 else "")
            lines.append(f"另有 {len(running)} 个任务（共 {n_exec} 个执行）还在云端排队/生成中："
                         f"任务{ids}")
            lines.append("直接重跑会让新旧两份同时占并发额度，而且各生成一个成品。")
        for i, text in enumerate(lines):
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            if i == len(lines) - 1 and running:
                lbl.setObjectName("PageWarn")     # 最后一句是要避开的那个坑，得显眼
            lay.addWidget(lbl)

        if params_note:
            # 把本次真正会用的参数写脸上：“改了时长/步数没生效”的困惑
            # 多半是没意识到重跑用的是工具栏当前值，而不是上次的值
            p = QLabel(f"本次将按【{params_note}】提交（工具栏当前值，可与上次不同）")
            p.setWordWrap(True)
            p.setObjectName("PageTip")
            lay.addWidget(p)

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
        btns.addWidget(b_cancel)
        if running:
            # “保留并直接重跑”得能点到（抽卡补跑、新旧两份都要），但不能是默认项
            b_keep = QPushButton("🔁 直接重跑")
            b_keep.setObjectName("GhostBtn")
            b_keep.setToolTip("不发取消请求，正在跑的那些继续跑完：\n"
                              "同一任务会新旧两份同时占并发，各自生成一个成品")
            b_keep.clicked.connect(self.accept)
            b_do = QPushButton("⏹ 取消并重跑")
            b_do.setDefault(True)          # 回车就是它：多数情况下这才是想要的
            b_do.setToolTip("先给还在跑的执行发取消请求，再提交本次的新任务")
            b_do.clicked.connect(self._pick_cancel_first)
            btns.addWidget(b_keep)
            btns.addWidget(b_do)
        else:
            b_ok = QPushButton("强制重跑")
            b_ok.setDefault(True)
            b_ok.clicked.connect(self.accept)
            btns.addWidget(b_ok)
        lay.addLayout(btns)

    def _pick_cancel_first(self):
        self.cancel_first = True
        self.accept()

    @staticmethod
    def ask(parent, task_id=None, count=1, allow_repeat=False, params_note="",
            running=None):
        """确认返回 {"repeat": n, "cancel_first": bool}；取消（跳过）返回 None"""
        dlg = ForceRerunDialog(parent, task_id=task_id, count=count,
                               allow_repeat=allow_repeat, params_note=params_note,
                               running=running)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        repeat = dlg.spin_repeat.value() if dlg.spin_repeat else 1
        return {"repeat": repeat, "cancel_first": dlg.cancel_first}
