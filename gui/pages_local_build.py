"""
gui/pages_local_build.py —— 批量基建 · 三级组织独立页（客户 / 执照 / 账户 / 搭建）

从一期工具卡片升级为左侧导航独立页（页栈索引 13）。四合一：
- ① 客户：顶层商业归属，一个客户下可挂多张执照；
- ② 执照/主体：中层，对应营业执照主体，下挂多个本地推账户；
- ③ 账户：叶子，凭证按姓名加密落 SQLite（local_org_store），支持层级/逐个混合授权；
- ④ 批量搭建：把一期 BuildPanel 的方案 × 账户矩阵、日志、历史统计并进来，
  账户来源改为「按当前下钻 + 权限过滤后的可见账户」。

三个分层 Tab（客户 / 执照 / 账户）+ 批量搭建，页顶一条面包屑随「选中客户 → 选中执照」
实时下钻。数据权限复用 org_store.visible_owners()：admin/单机不限，member 只看
自己负责的、manager 看本部门。刷新由主窗 2 秒定时驱动，全部按数据签名比对，
没变不动 UI（对齐 pages_org 范式）。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QComboBox, QLineEdit, QDialog,
                               QFormLayout, QTabWidget, QTableWidget,
                               QTableWidgetItem, QHeaderView, QAbstractItemView,
                               QMessageBox, QPlainTextEdit, QProgressBar)

from store import app_state
from gui.header import page_header
from gui.tablekit import FieldManagerDialog, apply_field_layout
from gui.kit import TableColumnKit
from gui.tool_panels import ToolWorker
from gui.dialogs_build import LocalAccountDialog, PlanDialog, _split_list  # noqa: F401
from gui.theme import tokenize


def _owner_text(val):
    return val or "继承"


def _short_time(val):
    """DB 时间戳（YYYY-MM-DD HH:MM:SS）截成 MM-DD HH:MM，更新时间列更窄。"""
    s = str(val or "")
    return s[5:16] if len(s) >= 16 else (s or "—")


def _auth_type_text(val):
    """认证方式代号 → 展示名（目前只有 OAuth，留映射位以后扩展）。"""
    return {"oauth": "OAuth"}.get(str(val or "").strip().lower(), str(val or "—"))


class _CustomerDialog(QDialog):
    """客户新增/编辑：名称 + 备注 +（启用组织时）负责成员下拉。"""

    def __init__(self, parent=None, data=None, owners=None, org_on=True):
        super().__init__(parent)
        self._edit = data
        self.setWindowTitle("编辑客户" if data else "新建客户")
        self.resize(460, 220)
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("客户名称，如：某某连锁餐饮")
        self.ed_remark = QLineEdit()
        self.ed_remark.setPlaceholderText("备注（可空）")
        form.addRow("客户名称：", self.ed_name)
        form.addRow("备注：", self.ed_remark)
        self.cb_owner = None
        if org_on:
            self.cb_owner = QComboBox()
            self.cb_owner.addItem("（不限 · 仅管理员可见）", "")
            for nm in (owners or []):
                self.cb_owner.addItem(nm, nm)
            form.addRow("负责成员：", self.cb_owner)
        v.addLayout(form)
        v.addStretch(1)
        bb = QHBoxLayout()
        b_ok = QPushButton("💾 保存")
        b_ok.clicked.connect(self._accept)
        b_no = QPushButton("取消")
        b_no.setObjectName("GhostBtn")
        b_no.clicked.connect(self.reject)
        bb.addStretch(1)
        bb.addWidget(b_ok)
        bb.addWidget(b_no)
        v.addLayout(bb)
        if data:
            self.ed_name.setText(data.get("name") or "")
            self.ed_remark.setText(data.get("remark") or "")
            if self.cb_owner is not None:
                i = self.cb_owner.findData(data.get("owner") or "")
                if i >= 0:
                    self.cb_owner.setCurrentIndex(i)

    def _accept(self):
        if not self.ed_name.text().strip():
            QMessageBox.information(self, "提示", "请填写客户名称")
            return
        self._result = {
            "id": (self._edit or {}).get("id", ""),
            "name": self.ed_name.text().strip(),
            "remark": self.ed_remark.text().strip(),
            "owner": (self.cb_owner.currentData() if self.cb_owner else "") or ""}
        self.accept()

    def values(self):
        return getattr(self, "_result", None)


class _LicenseDialog(QDialog):
    """执照/主体新增/编辑：名称 + 主体说明 +（启用组织时）负责成员；归属固定客户。"""

    def __init__(self, parent=None, data=None, customer_id=0, customer_name="",
                 owners=None, org_on=True):
        super().__init__(parent)
        self._edit = data
        self._customer_id = int(customer_id or 0)
        self.setWindowTitle("编辑执照/主体" if data else "新建执照/主体")
        self.resize(480, 260)
        v = QVBoxLayout(self)
        top = QLabel(f"归属客户：{customer_name or '未选择（将挂在未分组下）'}")
        top.setObjectName("PageTip")
        v.addWidget(top)
        form = QFormLayout()
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("执照/主体名称，如：某某有限公司")
        self.ed_subject = QLineEdit()
        self.ed_subject.setPlaceholderText("营业执照主体名 / 统一社会信用代码（可空）")
        form.addRow("执照名称：", self.ed_name)
        form.addRow("主体说明：", self.ed_subject)
        self.cb_owner = None
        if org_on:
            self.cb_owner = QComboBox()
            self.cb_owner.addItem("（继承所属客户）", "")
            for nm in (owners or []):
                self.cb_owner.addItem(nm, nm)
            form.addRow("负责成员：", self.cb_owner)
        v.addLayout(form)
        v.addStretch(1)
        bb = QHBoxLayout()
        b_ok = QPushButton("💾 保存")
        b_ok.clicked.connect(self._accept)
        b_no = QPushButton("取消")
        b_no.setObjectName("GhostBtn")
        b_no.clicked.connect(self.reject)
        bb.addStretch(1)
        bb.addWidget(b_ok)
        bb.addWidget(b_no)
        v.addLayout(bb)
        if data:
            self.ed_name.setText(data.get("name") or "")
            self.ed_subject.setText(data.get("subject") or "")
            self._customer_id = int(data.get("customer_id") or customer_id or 0)
            if self.cb_owner is not None:
                i = self.cb_owner.findData(data.get("owner") or "")
                if i >= 0:
                    self.cb_owner.setCurrentIndex(i)

    def _accept(self):
        if not self.ed_name.text().strip():
            QMessageBox.information(self, "提示", "请填写执照名称")
            return
        self._result = {
            "id": (self._edit or {}).get("id", ""),
            "customer_id": self._customer_id,
            "name": self.ed_name.text().strip(),
            "subject": self.ed_subject.text().strip(),
            "owner": (self.cb_owner.currentData() if self.cb_owner else "") or ""}
        self.accept()

    def values(self):
        return getattr(self, "_result", None)


class LocalBuildPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._sel_customer = 0          # 当前下钻选中客户 id（0=全部）
        self._sel_license = 0           # 当前下钻选中执照 id（0=全部）
        self._ck_worker = None          # 检测授权后台线程
        self._build_worker = None       # 批量搭建后台线程
        self._sig = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(8)
        lay.addWidget(page_header(
            "批量基建", "三级组织（客户 › 执照 › 账户）+ 多账户批量搭计划", icon="🏗"))
        self.lbl_crumb = QLabel()
        self.lbl_crumb.setObjectName("PageTip")
        lay.addWidget(self.lbl_crumb)
        self.tabs = QTabWidget()
        # 页签显式套用数据中台「墨线式」：不满宽拉伸、去基线、字号加大让标签更醒目
        bar = self.tabs.tabBar()
        bar.setExpanding(False)
        bar.setDrawBase(False)
        bar.setStyleSheet(
            tokenize("QTabBar::tab{background:transparent;color:#646A73;font-size:14px;"
            "padding:9px 22px;margin-right:10px;border:none;"
            "border-bottom:2px solid transparent;}"
            "QTabBar::tab:selected{color:#3370FF;font-weight:600;"
            "border-bottom:2px solid #3370FF;}"
            "QTabBar::tab:hover{color:#1F2329;}"))
        lay.addWidget(self.tabs, 1)
        self._build_customer_tab()
        self._build_license_tab()
        self._build_account_tab()
        self._build_build_tab()
        # 沿用上次记忆的列布局（与任务中心同款：弹窗管理列显隐/顺序）
        for tb, key in ((self.tbl_cust, "build_cust_fields"),
                        (self.tbl_lic, "build_lic_fields"),
                        (self.tbl_acct, "build_acct_fields"),
                        (self.tbl_bacct, "build_bacct_fields"),
                        (self.tbl_plan, "build_plan_fields")):
            self._restore_fields(tb, key)
        self.refresh()

    # ----------------------------------------------------------------
    # 组织/权限读取小工具（统一从这里走，避免各 Tab 各写一套）
    # ----------------------------------------------------------------
    def _org_on(self):
        from store import org_store
        return org_store.org_enabled()

    def _owners(self):
        from store import org_store
        if not org_store.org_enabled():
            return []
        return [m["name"] for m in org_store.list_members() if m.get("active")]

    def _store(self):
        from store import local_org_store
        local_org_store.migrate_from_config()     # 幂等守卫
        return local_org_store

    def _visible_accounts(self):
        """按当前会话权限过滤后的可见账户（LocalAccount 列表）。"""
        from video_text_tools.local_push import accounts as acc_api
        return acc_api.list_accounts()

    def _set_crumb(self):
        parts = ["全部客户"]
        if self._sel_customer:
            c = self._store().get_customer(self._sel_customer)
            parts = [c["name"] if c else f"客户#{self._sel_customer}"]
            if self._sel_license:
                l = self._store().get_license(self._sel_license)
                parts.append(l["name"] if l else f"执照#{self._sel_license}")
        self.lbl_crumb.setText("　›　".join(parts))

    # ----------------------------------------------------------------
    # ① 客户 Tab
    # ----------------------------------------------------------------
    def _build_customer_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(8)
        bar = QHBoxLayout()
        for text, slot, obj in (("＋ 新建客户", self._add_customer, ""),
                                ("✎ 编辑", self._edit_customer, "GhostBtn"),
                                ("🗑 删除", self._del_customer, "GhostBtn"),
                                ("⤵ 下钻（看其执照/账户）", self._drill_customer, "GhostBtn")):
            b = QPushButton(text)
            b.setObjectName(obj)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        t = QLabel("客户是三级组织的顶层；选中后点「下钻」把执照/账户页过滤到该客户")
        t.setObjectName("PageTip")
        bar.addWidget(t)
        v.addLayout(bar)
        self.tbl_cust = self._make_table(
            ["客户名称", "负责成员", "执照数", "账户数", "备注", "更新时间"],
            stretch_cols=[0, 4], widths={1: 110, 2: 80, 3: 80, 5: 140},
            num_cols={2, 3})
        bar.insertWidget(bar.count() - 1, self._field_btn(self.tbl_cust, "build_cust_fields", "字段管理 · 客户"))
        v.addWidget(self.tbl_cust, 1)
        self.tabs.addTab(w, "客户")

    def _make_table(self, cols, stretch_cols=None, widths=None, num_cols=None):
        """任务中心同款表格观感：斑马纹 + 表头点选排序 + 拖列调序 + 分列定宽。
        stretch_cols：自动撑满的文本列（默认末列）；widths：{列号: 宽度}；
        num_cols：按数值大小排序的计数列（避免 10 排到 9 前）。"""
        tb = QTableWidget(0, len(cols))
        tb.setHorizontalHeaderLabels(cols)
        TableColumnKit(tb)              # 统一列交互：表头右键靠左/中/右/换行
        tb.setAlternatingRowColors(True)          # 斑马纹（对齐任务中心）
        tb.verticalHeader().setVisible(False)
        tb.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tb.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tb.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        tb.setWordWrap(False)
        tb.setSortingEnabled(True)                # 点表头排序（对齐任务中心）
        tb._num_cols = set(num_cols or ())
        hh = tb.horizontalHeader()
        hh.setSectionsMovable(True)               # 拖表头调列序（对齐任务中心）
        hh.setHighlightSections(False)
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for c in (stretch_cols if stretch_cols is not None else [len(cols) - 1]):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        for c, w in (widths or {}).items():
            tb.setColumnWidth(c, w)
        return tb

    # ----------------------------------------------------------------
    # 字段管理（弹窗 · 推广后台式）：与任务中心共用同一套 FieldManagerDialog
    #   前 first_locked 列（名称列 / 勾选列）锁定，不参与显隐与调序；布局记在 app_state
    # ----------------------------------------------------------------
    def _field_btn(self, table, key, title, first_locked=1):
        """生成「⚟ 字段管理」胶囊按钮：点开弹窗管理该表列的显隐与顺序（会被记住）。"""
        b = QPushButton("⚟ 字段管理")
        b.setObjectName("ChipBtn")
        b.setProperty("accent", "1")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip("弹窗管理列显示/隐藏与顺序：左侧勾选，右侧拖动调序（设置会被记住）")
        b.clicked.connect(lambda _=False, t=table, k=key, ti=title, fl=first_locked:
                          self._manage_fields(t, k, ti, fl))
        return b

    def _manage_fields(self, table, key, title, first_locked=1):
        """弹窗编辑列布局，确定后套用并持久化到 app_state[key]。"""
        n = table.columnCount()
        headers = [(table.horizontalHeaderItem(i).text() if table.horizontalHeaderItem(i)
                    else str(i)) for i in range(n)]
        mgmt = [(i, headers[i]) for i in range(first_locked, n)]
        if not mgmt:
            QMessageBox.information(self, "提示", "这张表没有可管理的列")
            return
        vis = [c for c in range(first_locked, n) if not table.isColumnHidden(c)]
        hidden = {c for c in range(first_locked, n) if table.isColumnHidden(c)}
        dlg = FieldManagerDialog(self, mgmt, vis, hidden, title=title,
                                 default_order=[c for c, _ in mgmt], default_hidden=set())
        if dlg.exec():
            apply_field_layout(table, dlg.order, dlg.hidden, first_locked=first_locked)
            app_state.set_value(key, {"order": list(dlg.order), "hidden": sorted(dlg.hidden)})

    def _restore_fields(self, table, key, first_locked=1):
        """启动时套用上次记忆的列布局；没存过或越界的逻辑号自动忽略（升级增减列不炸）。"""
        saved = app_state.get(key) or {}
        n = table.columnCount()
        valid = set(range(first_locked, n))
        known = [c for c in (saved.get("order") or []) if isinstance(c, int) and c in valid]
        if not known:
            return
        order = known + [c for c in range(first_locked, n) if c not in known]
        hidden = {c for c in (saved.get("hidden") or []) if isinstance(c, int) and c in valid}
        apply_field_layout(table, order, hidden, first_locked=first_locked)

    def _reload_customers(self):
        st = self._store()
        rows = st.list_customers_visible()
        accts = self._visible_accounts()
        cust_acct, cust_lic = {}, {}
        for a in accts:
            cid = int(getattr(a, "customer_id", 0) or 0)
            cust_acct[cid] = cust_acct.get(cid, 0) + 1
        for c in rows:
            cust_lic[c["id"]] = len(st.list_licenses_visible(c["id"]))
        sig = tuple((c["id"], c["name"], c.get("owner") or "",
                     cust_lic.get(c["id"], 0), cust_acct.get(c["id"], 0),
                     c.get("remark") or "", c.get("updated_at") or "") for c in rows)
        if sig == self._sig.get("cust"):
            return
        self._sig["cust"] = sig
        keep = self._picked_id(self.tbl_cust)
        self.tbl_cust.setSortingEnabled(False)    # 填充期停排序，避免边填边重排
        self.tbl_cust.setRowCount(len(rows))
        for i, c in enumerate(rows):
            self._put_row(self.tbl_cust, i, [
                c["name"], _owner_text(c.get("owner") or ""),
                str(cust_lic.get(c["id"], 0)), str(cust_acct.get(c["id"], 0)),
                c.get("remark") or "—", _short_time(c.get("updated_at"))],
                key=c["id"])
        self._restore_pick(self.tbl_cust, keep if keep is not None else self._sel_customer)
        self.tbl_cust.setSortingEnabled(True)

    def _picked_id(self, table):
        row = table.currentRow()
        if row < 0:
            return None
        it = table.item(row, 0)
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _put_row(self, table, row, cells, key=None):
        num_cols = getattr(table, "_num_cols", set())
        for j, text in enumerate(cells):
            it = QTableWidgetItem(str(text))
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter if j else
                                Qt.AlignmentFlag.AlignVCenter)
            if j == 0 and key is not None:
                it.setData(Qt.ItemDataRole.UserRole, int(key))
            if j in num_cols and str(text).lstrip("-").isdigit():
                it.setData(Qt.ItemDataRole.DisplayRole, int(text))   # 数值排序
            table.setItem(row, j, it)

    def _restore_pick(self, table, key):
        if key is None:
            return
        for r in range(table.rowCount()):
            it = table.item(r, 0)
            if it and it.data(Qt.ItemDataRole.UserRole) == int(key):
                table.selectRow(r)
                return

    def _add_customer(self):
        dlg = _CustomerDialog(self, owners=self._owners(), org_on=self._org_on())
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.values():
            self._store().save_customer(dlg.values())
            self._sig.pop("cust", None)
            self.refresh()

    def _edit_customer(self):
        cid = self._picked_id(self.tbl_cust)
        if cid is None:
            QMessageBox.information(self, "提示", "请先在表里选中一个客户")
            return
        data = self._store().get_customer(cid)
        dlg = _CustomerDialog(self, data=data, owners=self._owners(),
                              org_on=self._org_on())
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.values():
            self._store().save_customer(dlg.values())
            self._sig.pop("cust", None)
            self.refresh()

    def _del_customer(self):
        cid = self._picked_id(self.tbl_cust)
        if cid is None:
            QMessageBox.information(self, "提示", "请先在表里选中一个客户")
            return
        c = self._store().get_customer(cid)
        if QMessageBox.question(
                self, "删除客户",
                f"确定删除客户「{c['name'] if c else cid}」？其下所有执照与账户将一并删除。") \
                != QMessageBox.StandardButton.Yes:
            return
        self._store().delete_customer(cid)
        if self._sel_customer == cid:
            self._sel_customer = 0
            self._sel_license = 0
        for k in ("cust", "lic", "acct"):
            self._sig.pop(k, None)
        self.refresh()

    def _drill_customer(self):
        cid = self._picked_id(self.tbl_cust)
        if cid is None:
            QMessageBox.information(self, "提示", "请先在表里选中一个客户")
            return
        self._sel_customer = int(cid)
        self._sel_license = 0
        self._sig.pop("lic", None)
        self._sig.pop("acct", None)
        self._set_crumb()
        self.refresh()
        self.tabs.setCurrentIndex(1)

    # ----------------------------------------------------------------
    # ② 执照 Tab
    # ----------------------------------------------------------------
    def _build_license_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(8)
        bar = QHBoxLayout()
        for text, slot, obj in (("＋ 新建执照", self._add_license, ""),
                                ("✎ 编辑", self._edit_license, "GhostBtn"),
                                ("🗑 删除", self._del_license, "GhostBtn"),
                                ("⤵ 下钻（看其账户）", self._drill_license, "GhostBtn"),
                                ("↺ 返回全部客户", self._reset_drill, "GhostBtn")):
            b = QPushButton(text)
            b.setObjectName(obj)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        self.lbl_lic_scope = QLabel()
        self.lbl_lic_scope.setObjectName("PageTip")
        bar.addWidget(self.lbl_lic_scope)
        v.addLayout(bar)
        self.tbl_lic = self._make_table(
            ["执照/主体", "归属客户", "主体说明", "负责成员", "账户数", "更新时间"],
            stretch_cols=[0, 2], widths={1: 130, 3: 110, 4: 80, 5: 140},
            num_cols={4})
        bar.insertWidget(bar.count() - 1, self._field_btn(self.tbl_lic, "build_lic_fields", "字段管理 · 执照"))
        v.addWidget(self.tbl_lic, 1)
        self.tabs.addTab(w, "执照")

    def _current_customer_name(self):
        if not self._sel_customer:
            return ""
        c = self._store().get_customer(self._sel_customer)
        return c["name"] if c else ""

    def _reload_licenses(self):
        st = self._store()
        rows = st.list_licenses_visible(
            self._sel_customer if self._sel_customer else None)
        accts = self._visible_accounts()
        lic_acct = {}
        for a in accts:
            lid = int(getattr(a, "license_id", 0) or 0)
            lic_acct[lid] = lic_acct.get(lid, 0) + 1
        cust_name = {}                              # 客户 id → 名称（去重查库）
        for l in rows:
            cid = int(l.get("customer_id") or 0)
            if cid and cid not in cust_name:
                c = st.get_customer(cid)
                cust_name[cid] = c["name"] if c else ""
        sig = tuple((l["id"], l["name"], cust_name.get(int(l.get("customer_id") or 0), ""),
                     l.get("subject") or "", l.get("owner") or "",
                     lic_acct.get(l["id"], 0), l.get("updated_at") or "") for l in rows)
        if sig == self._sig.get("lic"):
            return
        self._sig["lic"] = sig
        self.lbl_lic_scope.setText(
            f"当前范围：{self._current_customer_name() or '全部客户'}")
        keep = self._picked_id(self.tbl_lic)
        self.tbl_lic.setSortingEnabled(False)     # 填充期停排序
        self.tbl_lic.setRowCount(len(rows))
        for i, l in enumerate(rows):
            self._put_row(self.tbl_lic, i, [
                l["name"], cust_name.get(int(l.get("customer_id") or 0), "") or "未分组",
                l.get("subject") or "—", _owner_text(l.get("owner") or ""),
                str(lic_acct.get(l["id"], 0)), _short_time(l.get("updated_at"))], key=l["id"])
        self._restore_pick(self.tbl_lic, keep if keep is not None else self._sel_license)
        self.tbl_lic.setSortingEnabled(True)

    def _add_license(self):
        st = self._store()
        cid = self._sel_customer
        if not cid:
            QMessageBox.information(
                self, "提示", "请先在「① 客户」页选中一个客户（或点下钻）再建执照")
            return
        dlg = _LicenseDialog(self, customer_id=cid,
                             customer_name=self._current_customer_name(),
                             owners=self._owners(), org_on=self._org_on())
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.values():
            st.save_license(dlg.values())
            self._sig.pop("lic", None)
            self.refresh()

    def _edit_license(self):
        lid = self._picked_id(self.tbl_lic)
        if lid is None:
            QMessageBox.information(self, "提示", "请先在表里选中一张执照")
            return
        st = self._store()
        data = st.get_license(lid)
        dlg = _LicenseDialog(self, data=data,
                             customer_id=(data or {}).get("customer_id", 0),
                             customer_name=self._current_customer_name()
                             or ((st.get_customer((data or {}).get("customer_id", 0)) or {}).get("name", "")),
                             owners=self._owners(), org_on=self._org_on())
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.values():
            st.save_license(dlg.values())
            self._sig.pop("lic", None)
            self.refresh()

    def _del_license(self):
        lid = self._picked_id(self.tbl_lic)
        if lid is None:
            QMessageBox.information(self, "提示", "请先在表里选中一张执照")
            return
        l = self._store().get_license(lid)
        if QMessageBox.question(
                self, "删除执照",
                f"确定删除执照「{l['name'] if l else lid}」？其下账户将一并删除。") \
                != QMessageBox.StandardButton.Yes:
            return
        self._store().delete_license(lid)
        if self._sel_license == lid:
            self._sel_license = 0
        for k in ("lic", "acct", "cust"):
            self._sig.pop(k, None)
        self.refresh()

    def _drill_license(self):
        lid = self._picked_id(self.tbl_lic)
        if lid is None:
            QMessageBox.information(self, "提示", "请先在表里选中一张执照")
            return
        self._sel_license = int(lid)
        self._sig.pop("acct", None)
        self._set_crumb()
        self.refresh()
        self.tabs.setCurrentIndex(2)

    def _reset_drill(self):
        self._sel_customer = 0
        self._sel_license = 0
        for k in ("cust", "lic", "acct"):
            self._sig.pop(k, None)
        self._set_crumb()
        self.refresh()

    # ----------------------------------------------------------------
    # ③ 账户 Tab
    # ----------------------------------------------------------------
    def _build_account_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(8)
        bar = QHBoxLayout()
        for text, slot, obj in (("＋ 添加账户", self._add_account, ""),
                                ("✎ 编辑", self._edit_account, "GhostBtn"),
                                ("🗑 删除", self._del_account, "GhostBtn"),
                                ("🔄 刷新", self._force_reload_accounts, "GhostBtn"),
                                ("🔌 检测授权", self._check_auth, "GhostBtn")):
            b = QPushButton(text)
            b.setObjectName(obj)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        self.lbl_acct_scope = QLabel()
        self.lbl_acct_scope.setObjectName("PageTip")
        bar.addWidget(self.lbl_acct_scope)
        v.addLayout(bar)
        self.tbl_acct = self._make_table(
            ["别名", "平台", "归属（客户›执照）", "账户ID", "认证方式", "负责成员", "授权状态"],
            stretch_cols=[2],
            widths={0: 120, 1: 100, 3: 150, 4: 90, 5: 100, 6: 140})
        bar.insertWidget(bar.count() - 1, self._field_btn(self.tbl_acct, "build_acct_fields", "字段管理 · 账户"))
        v.addWidget(self.tbl_acct, 1)
        self.tabs.addTab(w, "账户")

    def _drilled_accounts(self):
        """按当前下钻过滤的可见账户（执照优先于客户；都没选＝全部可见）。"""
        accts = self._visible_accounts()
        if self._sel_license:
            return [a for a in accts if int(getattr(a, "license_id", 0) or 0) == self._sel_license]
        if self._sel_customer:
            return [a for a in accts if int(getattr(a, "customer_id", 0) or 0) == self._sel_customer]
        return accts

    def _reload_accounts(self):
        from video_text_tools.local_push.models import platform_label
        accts = self._drilled_accounts()
        sig = tuple((a.id, a.label, a.platform, a.advertiser_id, a.org_display(),
                     a.auth_type, a.owner or "") for a in accts)
        if sig == self._sig.get("acct"):
            return
        self._sig["acct"] = sig
        scope = "全部可见账户"
        if self._sel_license:
            scope = f"执照「{self._current_license_name()}」下账户"
        elif self._sel_customer:
            scope = f"客户「{self._current_customer_name()}」下账户"
        self.lbl_acct_scope.setText(f"当前范围：{scope}")
        self.tbl_acct.setSortingEnabled(False)    # 填充期停排序
        self.tbl_acct.setRowCount(len(accts))
        for i, a in enumerate(accts):
            self._put_row(self.tbl_acct, i, [
                a.label, platform_label(a.platform), a.org_display(),
                a.advertiser_id or "—", _auth_type_text(a.auth_type),
                _owner_text(a.owner or ""), self._auth_cache.get(a.id, "—")])
            # 对象塞进别名单元格 UserRole+1，编辑/删除时按行号取回
            it = self.tbl_acct.item(i, 0)
            if it is not None:
                it.setData(Qt.ItemDataRole.UserRole + 1, a)
        self.tbl_acct.setSortingEnabled(True)

    @property
    def _auth_cache(self):
        # 检测结果缓存（跨刷新保留，按账户 id）
        if not hasattr(self, "_auth_map"):
            self._auth_map = {}
        return self._auth_map

    def _current_license_name(self):
        if not self._sel_license:
            return ""
        l = self._store().get_license(self._sel_license)
        return l["name"] if l else ""

    def _row_account(self, row):
        it = self.tbl_acct.item(row, 0)
        return it.data(Qt.ItemDataRole.UserRole + 1) if it else None

    def _force_reload_accounts(self):
        self._sig.pop("acct", None)
        self.refresh()

    def _account_dialog(self, account=None):
        st = self._store()
        # 归属执照下拉来源：当前客户下的可见执照；没有下钻则取全部可见执照
        lic_rows = st.list_licenses_visible(
            self._sel_customer if self._sel_customer else None)
        licenses = [{"id": l["id"], "name": l["name"]} for l in lic_rows]
        default_lid = self._sel_license or (
            int(account.license_id) if account and getattr(account, "license_id", 0) else 0)
        return LocalAccountDialog(self, account=account, licenses=licenses,
                                  owners=self._owners(), org_on=self._org_on(),
                                  default_license_id=default_lid)

    def _add_account(self):
        dlg = self._account_dialog()
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._save_account(dlg.result_account())

    def _edit_account(self):
        row = self.tbl_acct.currentRow()
        acct = self._row_account(row) if row >= 0 else None
        if acct is None:
            QMessageBox.information(self, "提示", "请先在表里选中一个账户")
            return
        dlg = self._account_dialog(account=acct)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._save_account(dlg.result_account())

    def _save_account(self, acct):
        if acct is None:
            return
        from video_text_tools.local_push import accounts as acc_api
        try:
            acc_api.save_account(acct)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        for k in ("acct", "cust", "lic"):
            self._sig.pop(k, None)
        self.refresh()

    def _del_account(self):
        row = self.tbl_acct.currentRow()
        acct = self._row_account(row) if row >= 0 else None
        if acct is None:
            QMessageBox.information(self, "提示", "请先在表里选中一个账户")
            return
        if QMessageBox.question(
                self, "删除账户",
                f"确定删除「{acct.label}」？其凭证会一并清除。") \
                != QMessageBox.StandardButton.Yes:
            return
        from video_text_tools.local_push import accounts as acc_api
        acc_api.delete_account(acct.id)
        self._auth_cache.pop(acct.id, None)
        self._sig.pop("acct", None)
        self.refresh()

    # ---- 检测授权（后台线程，逐账户 check_auth）----
    def _check_auth(self):
        accts = self._drilled_accounts()
        if not accts:
            QMessageBox.information(self, "提示", "当前范围没有可检测的账户")
            return
        def fn(log, progress, should_stop):
            from video_text_tools.local_push import get_adapter
            out = []
            for a in accts:
                if should_stop():
                    break
                ad = get_adapter(a.platform)
                if ad is None:
                    out.append((a.id, False, "无适配器"))
                    continue
                ok, msg = ad.check_auth(a, log=log)
                out.append((a.id, ok, msg))
                progress(len(out), len(accts), a.label)
            return out
        self._ck_worker = ToolWorker(fn, self)
        self._ck_worker.log.connect(lambda m: None)
        self._ck_worker.progress.connect(lambda *a: None)
        self._ck_worker.done.connect(self._on_check_done)
        self._ck_worker.start()

    def _on_check_done(self, res):
        self._ck_worker = None
        if isinstance(res, Exception):
            QMessageBox.warning(self, "检测失败", str(res))
            return
        for aid, ok, msg in (res or []):
            self._auth_cache[aid] = ("✅ " if ok else "❌ ") + msg
        self._sig.pop("acct", None)
        self._reload_accounts()

    # ----------------------------------------------------------------
    # ④ 批量搭建 Tab
    # ----------------------------------------------------------------
    def _build_build_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(8)
        # 账户勾选区（来源＝下钻过滤后的可见账户）
        bar = QHBoxLayout()
        bar.addWidget(QLabel("<b>本次搭建账户</b>（勾选参与，来源已按组织下钻+权限过滤）"))
        bar.addStretch(1)
        b_all = QPushButton("全选/反选")
        b_all.setObjectName("GhostBtn")
        b_all.clicked.connect(self._toggle_all_accts)
        bar.addWidget(b_all)
        v.addLayout(bar)
        self.tbl_bacct = QTableWidget(0, 5)
        self.tbl_bacct.setHorizontalHeaderLabels(
            ["选", "别名", "归属（客户›执照）", "账户ID", "授权状态"])
        TableColumnKit(self.tbl_bacct)  # 统一列交互：表头右键靠左/中/右/换行
        self.tbl_bacct.verticalHeader().setVisible(False)
        self.tbl_bacct.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_bacct.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.tbl_bacct.setFixedHeight(150)
        bar.addWidget(self._field_btn(self.tbl_bacct, "build_bacct_fields", "字段管理 · 搭建账户"))
        v.addWidget(self.tbl_bacct)

        # 方案区
        bar2 = QHBoxLayout()
        bar2.addWidget(QLabel("<b>计划方案</b>（方案模板存本机，可反复用）"))
        bar2.addStretch(1)
        for text, slot in (("＋ 新建", self._add_plan), ("✎ 编辑", self._edit_plan),
                           ("🗑 删除", self._del_plan)):
            b = QPushButton(text)
            b.setObjectName("GhostBtn")
            b.clicked.connect(slot)
            bar2.addWidget(b)
        v.addLayout(bar2)
        self.tbl_plan = QTableWidget(0, 5)
        self.tbl_plan.setHorizontalHeaderLabels(["选", "方案名", "推广类型", "日预算", "素材"])
        TableColumnKit(self.tbl_plan)   # 统一列交互：表头右键靠左/中/右/换行
        self.tbl_plan.verticalHeader().setVisible(False)
        self.tbl_plan.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_plan.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.tbl_plan.setFixedHeight(130)
        bar2.addWidget(self._field_btn(self.tbl_plan, "build_plan_fields", "字段管理 · 计划方案"))
        v.addWidget(self.tbl_plan)

        # 日志 + 进度 + 启动/停止
        self.log = QPlainTextEdit()
        self.log.setObjectName("LogBox")
        self.log.setReadOnly(True)
        self.log.setFixedHeight(120)
        v.addWidget(self.log)
        run = QHBoxLayout()
        self.b_run = QPushButton("▶ 开始搭建")
        self.b_run.clicked.connect(self._on_run)
        self.b_stop = QPushButton("⏹ 停止")
        self.b_stop.setObjectName("GhostBtn")
        self.b_stop.setEnabled(False)
        self.b_stop.clicked.connect(self._on_stop)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        run.addWidget(self.b_run)
        run.addWidget(self.b_stop)
        run.addWidget(self.bar, 1)
        v.addLayout(run)

        # 历史分层统计
        hbar = QHBoxLayout()
        hbar.addWidget(QLabel("<b>执行历史 · 分层统计</b>"))
        self.cb_level = QComboBox()
        for key, name in (("customer", "按客户"), ("license", "按执照"), ("account", "按账户")):
            self.cb_level.addItem(name, key)
        self.cb_level.currentIndexChanged.connect(self._reload_stats)
        hbar.addWidget(self.cb_level)
        hbar.addStretch(1)
        v.addLayout(hbar)
        self.tbl_stat = self._make_table(
            ["对象", "总数", "成功", "失败"],
            stretch_cols=[0], widths={1: 80, 2: 80, 3: 80}, num_cols={1, 2, 3})
        self.tbl_stat.setFixedHeight(140)
        v.addWidget(self.tbl_stat)
        self.tabs.addTab(w, "批量搭建")

    # ---- 搭建账户/方案表 ----
    def _reload_build_accounts(self):
        accts = self._drilled_accounts()
        sig = tuple(a.id for a in accts)
        if sig != self._sig.get("bacct"):
            self._sig["bacct"] = sig
            self.tbl_bacct.setRowCount(len(accts))
            for i, a in enumerate(accts):
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                chk.setCheckState(Qt.CheckState.Unchecked)
                chk.setData(Qt.ItemDataRole.UserRole, a)
                self.tbl_bacct.setItem(i, 0, chk)
                self.tbl_bacct.setItem(i, 1, QTableWidgetItem(a.label))
                self.tbl_bacct.setItem(i, 2, QTableWidgetItem(a.org_display()))
                self.tbl_bacct.setItem(i, 3, QTableWidgetItem(a.advertiser_id or "—"))
                self.tbl_bacct.setItem(i, 4, QTableWidgetItem(self._auth_cache.get(a.id, "—")))

    def _toggle_all_accts(self):
        checked = any(self.tbl_bacct.item(r, 0).checkState() != Qt.CheckState.Checked
                      for r in range(self.tbl_bacct.rowCount()))
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for r in range(self.tbl_bacct.rowCount()):
            self.tbl_bacct.item(r, 0).setCheckState(state)

    def _checked_build_accounts(self):
        out = []
        for r in range(self.tbl_bacct.rowCount()):
            if self.tbl_bacct.item(r, 0).checkState() == Qt.CheckState.Checked:
                a = self.tbl_bacct.item(r, 0).data(Qt.ItemDataRole.UserRole)
                if a:
                    out.append(a)
        return out

    def _reload_plans(self):
        from video_text_tools.local_push import plans as plan_api
        from video_text_tools.local_push.models import PROMO_LABELS
        try:
            plans = plan_api.list_plans()
        except Exception as e:
            self._append_log(f"⚠ 读取计划方案失败：{e}")
            return
        sig = tuple(p.id for p in plans)
        if sig != self._sig.get("plan"):
            self._sig["plan"] = sig
            self.tbl_plan.setRowCount(len(plans))
            for i, p in enumerate(plans):
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                chk.setCheckState(Qt.CheckState.Unchecked)
                chk.setData(Qt.ItemDataRole.UserRole, p)
                self.tbl_plan.setItem(i, 0, chk)
                self.tbl_plan.setItem(i, 1, QTableWidgetItem(p.display_name()))
                self.tbl_plan.setItem(i, 2, QTableWidgetItem(
                    PROMO_LABELS.get(p.promo_type, p.promo_type)))
                self.tbl_plan.setItem(i, 3, QTableWidgetItem(f"{p.budget:.2f} 元/天"))
                self.tbl_plan.setItem(i, 4, QTableWidgetItem(f"{len(p.videos)} 条"))

    def _row_plan(self, row):
        it = self.tbl_plan.item(row, 0)
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _add_plan(self):
        dlg = PlanDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._save_plan(dlg.result_plan())

    def _edit_plan(self):
        row = self.tbl_plan.currentRow()
        plan = self._row_plan(row) if row >= 0 else None
        if plan is None:
            QMessageBox.information(self, "提示", "请先在表里选中一个方案")
            return
        dlg = PlanDialog(self, plan=plan)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._save_plan(dlg.result_plan())

    def _save_plan(self, plan):
        if plan is None:
            return
        from video_text_tools.local_push import plans as plan_api
        try:
            plan_api.save_plan(plan)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self._sig.pop("plan", None)
        self._reload_plans()

    def _del_plan(self):
        row = self.tbl_plan.currentRow()
        plan = self._row_plan(row) if row >= 0 else None
        if plan is None:
            QMessageBox.information(self, "提示", "请先在表里选中一个方案")
            return
        if QMessageBox.question(
                self, "删除方案", f"确定删除方案「{plan.display_name()}」？") \
                != QMessageBox.StandardButton.Yes:
            return
        from video_text_tools.local_push import plans as plan_api
        plan_api.delete_plan(plan.id)
        self._sig.pop("plan", None)
        self._reload_plans()

    def _checked_plans(self):
        out = []
        for r in range(self.tbl_plan.rowCount()):
            if self.tbl_plan.item(r, 0).checkState() == Qt.CheckState.Checked:
                p = self._row_plan(r)
                if p:
                    out.append(p)
        return out

    # ---- 搭建执行 ----
    def _append_log(self, msg):
        self.log.appendPlainText(str(msg))

    def _on_run(self):
        accts = self._checked_build_accounts()
        if not accts:
            QMessageBox.information(self, "提示", "请至少勾选一个本地推账户")
            return
        plans = self._checked_plans()
        if not plans:
            QMessageBox.information(self, "提示", "请至少勾选一个计划方案")
            return
        for p in plans:
            if not p.videos:
                QMessageBox.warning(self, "提示",
                                    f"方案「{p.display_name()}」未配置素材视频，请先编辑补齐")
                return
        if QMessageBox.question(
                self, "确认搭建",
                f"将在 {len(accts)} 个账户下按 {len(plans)} 个方案真实创建"
                f"「项目 → 营销 → 素材」（共 {len(accts) * len(plans)} 次），确认执行？") \
                != QMessageBox.StandardButton.Yes:
            return

        def fn(log, progress, should_stop):
            from video_text_tools.local_push import build_batch
            from store import build_store
            recs = build_batch(plans, accts, log=log, progress=progress,
                               should_stop=should_stop)
            try:
                build_store.record_all(recs)
            except Exception as e:
                log(f"⚠ 搭建历史入库失败（不影响搭建结果）：{e}")
            return recs
        self.b_run.setEnabled(False)
        self.b_stop.setEnabled(True)
        self.bar.setValue(0)
        self._build_worker = ToolWorker(fn, self)
        self._build_worker.log.connect(self._append_log)
        self._build_worker.progress.connect(self._on_progress)
        self._build_worker.done.connect(self._on_done)
        self._build_worker.start()

    def _on_stop(self):
        if self._build_worker:
            self._build_worker.stop()
            self._append_log("⏹ 收到停止请求…")

    def _on_progress(self, cur, total, name):
        self.bar.setValue(int(cur / max(total, 1) * 100))

    def _on_done(self, res):
        self.b_run.setEnabled(True)
        self.b_stop.setEnabled(False)
        self._build_worker = None
        if isinstance(res, Exception):
            self._append_log(f"❌ 执行异常：{res}")
            QMessageBox.warning(self, "执行失败", str(res))
            return
        if isinstance(res, list):
            ok = sum(1 for r in res if r.ok)
            self._append_log(f"🎉 本轮结束：成功 {ok} / 共 {len(res)}"
                             + ("（含失败，详见上方日志与分层统计）" if ok < len(res) else ""))
        self._reload_stats()

    def _reload_stats(self):
        from store import build_store
        level = self.cb_level.currentData() or "customer"
        try:
            stats = build_store.stats_by_level(level)
        except Exception as e:
            stats = []
            self._append_log(f"⚠ 统计失败：{e}")
        self.tbl_stat.setSortingEnabled(False)    # 填充期停排序
        self.tbl_stat.setRowCount(len(stats))
        for i, s in enumerate(stats):
            self._put_row(self.tbl_stat, i, [
                s["name"] or f"#{s['key_id'] or '未分组'}",
                str(s["total"]), str(s["ok"]), str(s["fail"])], key=s["key_id"])
        self.tbl_stat.setSortingEnabled(True)

    # ----------------------------------------------------------------
    # 定时刷新入口（主窗 2s 驱动）
    # ----------------------------------------------------------------
    def refresh(self):
        self._set_crumb()
        self._reload_customers()
        self._reload_licenses()
        self._reload_accounts()
        self._reload_build_accounts()
        self._reload_plans()
        self._reload_stats()
