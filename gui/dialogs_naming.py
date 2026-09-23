"""
gui/dialogs_naming.py —— 「设置 → 🏷 命名规则」的自定义对话框

做法：左边列出能用的字段，右边排顺序（可拖拽 / 上下移），下面选分隔符，
预览随时跟着变。存的是 core/naming 的 (tokens, sep)，落到 config.json。

为什么把顺序做成可拖的而不是填模板串：使用者是运营同事，让他们写
`{num}_{product}.mp4` 这种模板容易漏花括号、写错字段名；点两下 + 拖一拖
不会错，且预览就在下面，所见即所得。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QListWidget, QListWidgetItem,
                               QComboBox, QDialogButtonBox, QMessageBox,
                               QAbstractItemView)

from core import naming


class NamingRuleDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🏷 成品命名规则")
        self.resize(660, 520)
        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        tip = QLabel("勾选想要的字段并排好顺序，下载成品时就按这个顺序拼文件名。\n"
                     "保存后立即生效（不用重启）；只影响之后新下载的视频，已生成的不会改名。")
        tip.setObjectName("PageTip")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        body = QHBoxLayout()
        body.setSpacing(10)

        # ---- 左：可用字段（还没用上的） ----
        left = QVBoxLayout()
        left.addWidget(QLabel("可用字段"))
        self.avail = QListWidget()
        self.avail.setToolTip("双击或点「添加」把它放进右边的顺序里")
        self.avail.itemDoubleClicked.connect(self._add_clicked)
        left.addWidget(self.avail, 1)
        body.addLayout(left)

        mid = QVBoxLayout()
        mid.addStretch(1)
        b_add = QPushButton("添加 →")
        b_add.setObjectName("GhostBtn")
        b_add.clicked.connect(self._add_clicked)
        b_del = QPushButton("← 移除")
        b_del.setObjectName("GhostBtn")
        b_del.clicked.connect(self._remove_clicked)
        mid.addWidget(b_add)
        mid.addWidget(b_del)
        mid.addStretch(1)
        body.addLayout(mid)

        # ---- 右：当前顺序（可拖拽调序） ----
        right = QVBoxLayout()
        right.addWidget(QLabel("命名顺序（拖动可调整先后）"))
        self.order = QListWidget()
        self.order.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.order.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.order.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.order.setToolTip("按住某一项拖到新位置即可改顺序；也可用下面的上移/下移")
        self.order.itemDoubleClicked.connect(self._remove_clicked)
        self.order.itemSelectionChanged.connect(self._sync_move_btns)
        # 拖拽改顺序不一定发 selectionChanged（选中行可能根本没变），只靠上面
        # 那条接线预览会停在拖之前的顺序，所以额外挂模型的 rowsMoved。
        self.order.model().rowsMoved.connect(lambda *_: self._refresh_preview())
        right.addWidget(self.order, 1)
        rrow = QHBoxLayout()
        self._btn_up = QPushButton("↑ 上移")
        self._btn_up.setObjectName("GhostBtn")
        self._btn_up.clicked.connect(lambda: self._move(-1))
        self._btn_dn = QPushButton("↓ 下移")
        self._btn_dn.setObjectName("GhostBtn")
        self._btn_dn.clicked.connect(lambda: self._move(1))
        rrow.addWidget(self._btn_up)
        rrow.addWidget(self._btn_dn)
        right.addLayout(rrow)
        body.addLayout(right, 1)
        lay.addLayout(body, 1)

        # ---- 分隔符 + 预览 ----
        foot = QHBoxLayout()
        foot.addWidget(QLabel("字段之间用："))
        self.cb_sep = QComboBox()
        for key, label in naming.SEP_CHOICES.items():
            self.cb_sep.addItem(label, key)
        self.cb_sep.currentIndexChanged.connect(self._refresh_preview)
        foot.addWidget(self.cb_sep)
        foot.addStretch(1)
        lay.addLayout(foot)

        self.lbl_preview = QLabel()
        self.lbl_preview.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_preview.setStyleSheet(
            "background:#F7F8FA; border:1px dashed #C9CDD4; border-radius:6px;"
            "padding:10px 12px; color:#1F2329;")
        self.lbl_preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lbl_preview)
        self.lbl_note = QLabel()
        self.lbl_note.setObjectName("PageTip")
        self.lbl_note.setWordWrap(True)
        lay.addWidget(self.lbl_note)

        bb = QDialogButtonBox()
        b_reset = bb.addButton("↩ 恢复默认", QDialogButtonBox.ButtonRole.ResetRole)
        b_ok = bb.addButton("💾 保存并生效", QDialogButtonBox.ButtonRole.AcceptRole)
        b_no = bb.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        # 「保存」留默认蓝色主按钮，其它两个降成灰边，免得三个蓝块分不清点哪个
        for b in (b_reset, b_no):
            b.setObjectName("GhostBtn")
        b_reset.clicked.connect(self._reset)
        b_ok.clicked.connect(self._save)
        lay.addWidget(bb)

        tokens, sep = naming.rules()
        self._fill(tokens, sep)
        self._sync_move_btns()

    # ---------- 装载 ----------
    def _fill(self, tokens, sep):
        self.order.clear()
        self.avail.clear()
        for t in tokens:
            self._append(self.order, t)
        for t in naming.FIELD_KEYS:
            if t not in tokens:
                self._append(self.avail, t)
        for i in range(self.cb_sep.count()):
            if self.cb_sep.itemData(i) == sep:
                self.cb_sep.setCurrentIndex(i)
                break
        self._refresh_preview()

    @staticmethod
    def _append(listw, key):
        item = QListWidgetItem(f"{naming.FIELD_LABEL[key]}"
                               f"　（{naming.FIELD_SAMPLE[key]}）")
        item.setData(Qt.ItemDataRole.UserRole, key)
        listw.addItem(item)

    def _keys(self, listw):
        return [listw.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(listw.count())]

    # ---------- 交互 ----------
    def _add_clicked(self):
        self._transfer(self.avail, self.order)

    def _remove_clicked(self):
        self._transfer(self.order, self.avail)

    def _transfer(self, src, dst):
        item = src.currentItem()
        if item is None:
            return
        src.takeItem(src.row(item))
        self._append(dst, item.data(Qt.ItemDataRole.UserRole))
        dst.setCurrentRow(dst.count() - 1)
        self._refresh_preview()

    def _sync_move_btns(self):
        """上移/下移只在真能动的时候亮着：顶行再上移、末行再下移都是没反应"""
        r, n = self.order.currentRow(), self.order.count()
        self._btn_up.setEnabled(r > 0)
        self._btn_dn.setEnabled(0 <= r < n - 1)

    def _move(self, dx):
        r = self.order.currentRow()
        t = r + dx
        if r < 0 or not (0 <= t < self.order.count()):
            return
        item = self.order.takeItem(r)
        self.order.insertItem(t, item)
        self.order.setCurrentRow(t)
        self._refresh_preview()

    def _reset(self):
        self._fill(list(naming.DEFAULT_TOKENS), naming.DEFAULT_SEP)
        self.order.setCurrentRow(-1)

    # ---------- 预览 ----------
    def _tokens(self):
        return self._keys(self.order)

    def _sep(self):
        return self.cb_sep.currentData()

    def _refresh_preview(self):
        self._sync_move_btns()
        tokens = self._tokens()
        sep = self._sep()
        if not tokens:
            self.lbl_preview.setText("<span style='color:#F54A45'>"
                                     "至少要选一个字段</span>")
            self.lbl_note.setText("右边的命名顺序是空的，保存会被拦住。")
            return
        sample = naming.preview(tokens=tokens, sep=sep)
        self.lbl_preview.setText(
            f"示例成品：<b style='font-size:15px'>{sample}</b><br>"
            f"<span style='color:#8F959E'>{naming.describe(tokens, sep)}</span>")
        notes = []
        if naming.SEQ not in tokens:
            notes.append("没排「序号」：同一天多条成品不会自动续排 01、02，"
                         "撞名时文件名尾巴会挂 (2)、(3) 防止互相覆盖。")
        if "account" in tokens or "remark" in tokens:
            notes.append("「线路 / 备注」没值时那一段会整段消失（不会留个孤零零的分隔符）。")
        if "date" in tokens and "date_full" in tokens:
            notes.append("同时排了「日期」和「完整日期」，文件名里会有两段日期。")
        self.lbl_note.setText("\n".join(notes) if notes else
                              "同名文件会自动续排序号，不会互相覆盖。")

    # ---------- 保存 ----------
    def _save(self):
        tokens = self._tokens()
        if not tokens:
            QMessageBox.warning(self, "还差一步", "右边的命名顺序不能是空的")
            return
        naming.save_rules(tokens, self._sep())
        self.accept()
