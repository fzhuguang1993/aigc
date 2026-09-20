"""
gui/pages_risk.py —— 风控中心：平台风控 + 产品风控政策的浏览/维护
政策里的「禁用词」会被口播规范检测（checkers.spec_checker）自动引用；
后续接入 AI 接口后，政策正文也会随脚本一起送外部模型做语义审查。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QLineEdit, QListWidget,
                               QListWidgetItem, QPlainTextEdit, QComboBox,
                               QCheckBox, QMessageBox, QSplitter, QDialogButtonBox)

from store import risk_store
from gui.header import page_header, Card


class RiskPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(8)

        lay.addWidget(page_header(
            "风控中心", "查阅/维护平台与产品风控政策 · 禁用词自动参与「口播规范检测」", icon="🛡"))

        bar = QHBoxLayout()
        b_np = QPushButton("＋ 新建平台政策")
        b_nr = QPushButton("＋ 新建产品政策")
        b_del = QPushButton("🗑 删除当前")
        b_del.setObjectName("GhostBtn")
        bar.addWidget(b_np)
        bar.addWidget(b_nr)
        bar.addWidget(b_del)
        bar.addStretch(1)
        bar.addWidget(QLabel("🔍"))
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索标题/正文/禁用词")
        self.ed_search.setFixedWidth(220)
        self.ed_search.textChanged.connect(self.reload_list)
        bar.addWidget(self.ed_search)
        lay.addLayout(bar)

        split = QSplitter()
        lay.addWidget(split, 1)

        self.lst = QListWidget()
        self.lst.setMinimumWidth(240)
        self.lst.currentItemChanged.connect(self._on_pick)
        split.addWidget(self.lst)

        form = Card(margins=(18, 14, 18, 14))
        split.addWidget(form)
        f = form.v

        row = QHBoxLayout()
        row.addWidget(QLabel("类别"))
        self.cb_scope = QComboBox()
        self.cb_scope.addItem(risk_store.SCOPE_LABELS[risk_store.SCOPE_PLATFORM],
                              risk_store.SCOPE_PLATFORM)
        self.cb_scope.addItem(risk_store.SCOPE_LABELS[risk_store.SCOPE_PRODUCT],
                              risk_store.SCOPE_PRODUCT)
        row.addWidget(self.cb_scope)
        row.addWidget(QLabel("标题"))
        self.ed_title = QLineEdit()
        self.ed_title.setPlaceholderText("例：抖音医疗器械类管控")
        row.addWidget(self.ed_title, 1)
        f.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("适用产品"))
        self.ed_applies = QLineEdit()
        self.ed_applies.setPlaceholderText("留空 = 适用全部产品；多个产品用逗号分隔（名称包含即命中）")
        row.addWidget(self.ed_applies, 1)
        f.addLayout(row)

        f.addWidget(QLabel("政策正文（供人工浏览查阅，AI 接入后随脚本一并送审）"))
        self.ed_content = QPlainTextEdit()
        self.ed_content.setPlaceholderText("规则要点、管控范围、违规后果……")
        f.addWidget(self.ed_content, 2)

        f.addWidget(QLabel("禁用词（逗号或换行分隔，检测时自动生效）"))
        self.ed_banned = QPlainTextEdit()
        self.ed_banned.setPlaceholderText("例：根治,100%有效,无副作用")
        self.ed_banned.setFixedHeight(64)
        f.addWidget(self.ed_banned, 1)

        row = QHBoxLayout()
        self.ck_active = QCheckBox("启用（停用后不参与口播检测，但仍可查看）")
        self.ck_active.setChecked(True)
        row.addWidget(self.ck_active)
        row.addStretch(1)
        bb = QDialogButtonBox()
        b_save = bb.addButton("💾 保存政策", QDialogButtonBox.ButtonRole.AcceptRole)
        b_save.clicked.connect(self._save)
        row.addWidget(bb)
        f.addLayout(row)

        self.lbl_hint = QLabel("提示：新产品建议先在产品中心填「规范卡」，风控中心放跨产品通用的平台规则。")
        self.lbl_hint.setObjectName("PageTip")
        lay.addWidget(self.lbl_hint)

        self._rid = None          # 正在编辑的政策 id（None=新建）
        b_np.clicked.connect(lambda: self._new(risk_store.SCOPE_PLATFORM))
        b_nr.clicked.connect(lambda: self._new(risk_store.SCOPE_PRODUCT))
        b_del.clicked.connect(self._delete)
        split.setSizes([260, 700])
        self.reload_list()

    # ---------------- 列表 ----------------
    def refresh(self):
        pass    # 政策是手动维护的，不做定时刷新覆盖用户编辑

    def reload_list(self):
        sel = self._rid
        self.lst.blockSignals(True)
        self.lst.clear()
        for r in risk_store.list_rules(keyword=self.ed_search.text().strip()):
            tag = risk_store.SCOPE_LABELS.get(r["scope"], r["scope"])
            applies = (r["applies"] or "").strip()
            label = f"[{tag}] {r['title']}" + (f"（{applies}）" if applies else "")
            if not r["active"]:
                label += " · 已停用"
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, r["id"])
            it.setToolTip(r["content"] or "")
            self.lst.addItem(it)
        self.lst.blockSignals(False)
        if sel is not None:
            for i in range(self.lst.count()):
                if self.lst.item(i).data(Qt.ItemDataRole.UserRole) == sel:
                    self.lst.setCurrentRow(i)
                    break

    def _load(self, r):
        self._rid = r["id"]
        idx = self.cb_scope.findData(r["scope"])
        self.cb_scope.setCurrentIndex(max(idx, 0))
        self.ed_title.setText(r["title"])
        self.ed_applies.setText(r["applies"] or "")
        self.ed_content.setPlainText(r["content"] or "")
        self.ed_banned.setPlainText(r["banned"] or "")
        self.ck_active.setChecked(bool(r["active"]))

    def _clear_form(self):
        self._rid = None
        self.ed_title.clear()
        self.ed_applies.clear()
        self.ed_content.clear()
        self.ed_banned.clear()
        self.ck_active.setChecked(True)

    def _on_pick(self, cur, prev=None):
        if cur is None:
            return
        r = risk_store.get_rule(cur.data(Qt.ItemDataRole.UserRole))
        if r:
            self._load(r)

    # ---------------- 操作 ----------------
    def _new(self, scope):
        self._clear_form()
        idx = self.cb_scope.findData(scope)
        self.cb_scope.setCurrentIndex(max(idx, 0))
        self.lst.setCurrentRow(-1)
        self.ed_title.setFocus()

    def _save(self):
        title = self.ed_title.text().strip()
        if not title:
            QMessageBox.information(self, "提示", "请填写政策标题")
            return
        scope = self.cb_scope.currentData()
        args = (scope, title, self.ed_applies.text().strip(),
                self.ed_content.toPlainText().strip(), self.ed_banned.toPlainText().strip())
        if self._rid is None:
            self._rid = risk_store.add_rule(*args)
            self.lbl_hint.setText(f"已新增政策「{title}」，其禁用词立即参与口播规范检测")
        else:
            risk_store.update_rule(self._rid, *args,
                                   active=1 if self.ck_active.isChecked() else 0)
            self.lbl_hint.setText(f"政策「{title}」已保存")
        self.reload_list()

    def _delete(self):
        cur = self.lst.currentItem()
        if cur is None:
            QMessageBox.information(self, "提示", "请先在左侧选择要删除的政策")
            return
        if QMessageBox.question(self, "确认删除",
                                f"删除政策「{cur.text()}」？\n\n删除后其禁用词不再参与口播检测。") \
                != QMessageBox.StandardButton.Yes:
            return
        risk_store.delete_rule(cur.data(Qt.ItemDataRole.UserRole))
        self._clear_form()
        self.reload_list()
