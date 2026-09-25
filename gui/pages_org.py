"""
gui/pages_org.py —— 组织结构管理页（仅管理员可见）

三个页签：成员 / 部门 / 线路归属。
- 成员：姓名、角色（admin/manager/member）、部门、启停、重置密码；
  组织未启用（还没有任何成员）时页顶提示"创建首个成员即开启登录"；
- 部门：单级平铺，删除部门只把成员 dept 置空，不删成员；
- 线路归属：数据权限的落点——runs/tasks 按 account（线路名）圈可见范围，
  每行一个下拉即时落库；未绑定的线路只有 admin 看得见。

写操作统一遵循 org_store 约定：返回 None=成功、str=错误原因，直接弹窗。
refresh() 被主窗口 2 秒定时驱动：全部按数据签名比对，没变不动 UI，
避免用户正在操作下拉/表格时被重建打断。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QDialog, QComboBox, QLineEdit,
                               QTabWidget, QTableWidget, QTableWidgetItem,
                               QListWidget, QListWidgetItem, QHeaderView,
                               QAbstractItemView, QMessageBox, QInputDialog)

from gui.header import page_header
from gui.window_frame import apply_rounded
from store import org_store

_COLS = ("姓名", "角色", "部门", "状态", "最近登录")


class _MemberDialog(QDialog):
    """新建/编辑成员共用：member=None 为新建（初始密码必填）"""

    def __init__(self, parent=None, member=None):
        super().__init__(parent)
        self._creating = member is None
        self.setWindowTitle("＋ 新建成员" if self._creating else "✏️ 编辑成员")
        self.setFixedWidth(360)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(8)

        lay.addWidget(QLabel("姓名："))
        self.ed_name = QLineEdit((member or {}).get("name", ""))
        self.ed_name.setPlaceholderText("登录时显示的名字")
        lay.addWidget(self.ed_name)

        if self._creating:
            lay.addWidget(QLabel("初始密码（至少 4 位）："))
            self.ed_pwd = QLineEdit()
            self.ed_pwd.setEchoMode(QLineEdit.EchoMode.Password)
            lay.addWidget(self.ed_pwd)

        lay.addWidget(QLabel("角色："))
        self.cb_role = QComboBox()
        for k, label in org_store.ROLES.items():
            self.cb_role.addItem(label, k)
        want = (member or {}).get("role", "member")
        self.cb_role.setCurrentIndex(max(0, self.cb_role.findData(want)))
        if not org_store.org_enabled():
            # 首个成员强制 admin：与 create_member 服务端口径一致，界面不骗人
            self.cb_role.setCurrentIndex(max(0, self.cb_role.findData("admin")))
            self.cb_role.setEnabled(False)
            tip = QLabel("组织尚未启用：创建首个成员后即开启登录，"
                         "首个成员固定为管理员，下次启动软件需要登录。")
            tip.setObjectName("InlineTip")
            tip.setWordWrap(True)
            lay.addWidget(tip)
        lay.addWidget(QLabel("部门（可留空）："))
        self.cb_dept = QComboBox()
        self.cb_dept.setEditable(True)
        self.cb_dept.addItem("")
        for d in org_store.list_depts():
            self.cb_dept.addItem(d)
        self.cb_dept.setCurrentText((member or {}).get("dept", ""))
        lay.addWidget(self.cb_dept)

        brow = QHBoxLayout()
        b_ok = QPushButton("保存")
        b_ok.clicked.connect(self.accept)
        b_no = QPushButton("取消")
        b_no.setObjectName("GhostBtn")
        b_no.clicked.connect(self.reject)
        brow.addWidget(b_ok, 1)
        brow.addWidget(b_no)
        lay.addLayout(brow)
        apply_rounded(self, show_min=False, show_max=False)

    def values(self):
        return {
            "name": self.ed_name.text().strip(),
            "pwd": getattr(self, "ed_pwd", None) and self.ed_pwd.text(),
            "role": self.cb_role.currentData(),
            "dept": self.cb_dept.currentText().strip(),
        }


class OrgPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(8)
        lay.addWidget(page_header("组织结构",
                                  "成员 · 部门 · 线路归属 — 登录后数据按角色自动圈定可见范围",
                                  icon="🏢"))
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self._build_members_tab()
        self._build_depts_tab()
        self._build_lines_tab()
        self._sig = {}                      # 各表数据签名：refresh 没变不动 UI
        self.refresh()

    # ---------------- 成员 ----------------
    def _build_members_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(8)
        bar = QHBoxLayout()
        for text, slot, obj in (
                ("＋ 新建成员", self._add_member, ""),
                ("✏️ 编辑", self._edit_member, "GhostBtn"),
                ("🔑 重置密码", self._reset_pwd, "GhostBtn"),
                ("⏹ 停用/启用", self._toggle_active, "GhostBtn"),
                ("🗑 删除", self._del_member, "GhostBtn")):
            b = QPushButton(text)
            b.setObjectName(obj)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        self.lbl_mem_hint = QLabel("")
        self.lbl_mem_hint.setObjectName("PageTip")
        bar.addWidget(self.lbl_mem_hint)
        v.addLayout(bar)

        self.tbl_mem = QTableWidget(0, len(_COLS))
        self.tbl_mem.setHorizontalHeaderLabels(_COLS)
        self.tbl_mem.verticalHeader().setVisible(False)
        self.tbl_mem.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_mem.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl_mem.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.tbl_mem.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        v.addWidget(w_ := self.tbl_mem, 1)
        self.tabs.addTab(w, "👥 成员")

    def _picked_member(self):
        row = self.tbl_mem.currentRow()
        if row < 0:
            QMessageBox.information(self, "提示", "请先在表格里选中一名成员")
            return ""
        return self.tbl_mem.item(row, 0).text()

    def _reload_members(self):
        rows = org_store.list_members()
        sig = tuple((r["name"], r["role"], r["dept"], r["active"],
                     r["last_login"]) for r in rows)
        if sig == self._sig.get("mem"):
            return
        self._sig["mem"] = sig
        keep = self.tbl_mem.currentRow()
        self.tbl_mem.setRowCount(len(rows))
        for i, m in enumerate(rows):
            cells = (m["name"], org_store.ROLES.get(m["role"], m["role"]),
                     m["dept"] or "—", "启用" if m["active"] else "已停用",
                     m["last_login"] or "—")
            for j, text in enumerate(cells):
                it = QTableWidgetItem(str(text))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.tbl_mem.setItem(i, j, it)
        if 0 <= keep < len(rows):
            self.tbl_mem.selectRow(keep)
        self.lbl_mem_hint.setText(
            "" if rows else "还没有成员：创建首个成员即开启登录（下次启动需登录）")

    def _add_member(self):
        dlg = _MemberDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        d = dlg.values()
        err = org_store.create_member(d["name"], d["pwd"], d["role"], d["dept"])
        if err:
            QMessageBox.warning(self, "无法创建", err)
        self.refresh()

    def _edit_member(self):
        name = self._picked_member()
        if not name:
            return
        m = org_store.get_member(name)
        if not m:
            return
        dlg = _MemberDialog(self, member=m)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        d = dlg.values()
        # 先改属性再改名：改名放最后，中途失败也不会出现"改到一半找不到人"
        err = org_store.update_member(name, role=d["role"], dept=d["dept"])
        if not err and d["name"] != name:
            err = org_store.rename_member(name, d["name"])
        if err:
            QMessageBox.warning(self, "未能保存", err)
        self.refresh()

    def _reset_pwd(self):
        name = self._picked_member()
        if not name:
            return
        text, ok = QInputDialog.getText(
            self, "重置密码", f"给「{name}」设置新密码（至少 4 位）：",
            QLineEdit.EchoMode.Password)
        if not ok:
            return
        err = org_store.set_password(name, text)
        if err:
            QMessageBox.warning(self, "重置失败", err)
        else:
            QMessageBox.information(self, "完成", f"「{name}」的密码已重置")

    def _toggle_active(self):
        name = self._picked_member()
        if not name:
            return
        m = org_store.get_member(name)
        if not m:
            return
        err = org_store.update_member(name, active=0 if m["active"] else 1)
        if err:
            QMessageBox.warning(self, "操作被拒", err)
        self.refresh()

    def _del_member(self):
        name = self._picked_member()
        if not name:
            return
        if QMessageBox.question(
                self, "删除成员",
                f"确定删除「{name}」？名下线路将退回未绑定（只有管理员可见）"
        ) != QMessageBox.StandardButton.Yes:
            return
        err = org_store.delete_member(name)
        if err:
            QMessageBox.warning(self, "无法删除", err)
        self.refresh()

    # ---------------- 部门 ----------------
    def _build_depts_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(8)
        bar = QHBoxLayout()
        for text, slot, obj in (
                ("＋ 添加部门", self._add_dept, ""),
                ("✏️ 重命名", self._rename_dept, "GhostBtn"),
                ("🗑 删除", self._del_dept, "GhostBtn")):
            b = QPushButton(text)
            b.setObjectName(obj)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        t = QLabel("部门只有一级（平铺）；删除部门只会把成员移出部门，不删成员")
        t.setObjectName("PageTip")
        bar.addWidget(t)
        v.addLayout(bar)
        self.lst_dept = QListWidget()
        v.addWidget(self.lst_dept, 1)
        self.tabs.addTab(w, "🏬 部门")

    def _reload_depts(self):
        names = org_store.list_depts()
        sig = tuple(names)
        if sig == self._sig.get("dept"):
            return
        self._sig["dept"] = sig
        keep = self.lst_dept.currentRow()
        self.lst_dept.clear()
        self.lst_dept.addItems(names)
        if 0 <= keep < len(names):
            self.lst_dept.setCurrentRow(keep)

    def _picked_dept(self):
        row = self.lst_dept.currentRow()
        return self.lst_dept.item(row).text() if row >= 0 else ""

    def _add_dept(self):
        text, ok = QInputDialog.getText(self, "添加部门", "部门名称：")
        if not ok:
            return
        err = org_store.add_dept(text)
        if err:
            QMessageBox.warning(self, "无法添加", err)
        self.refresh()

    def _rename_dept(self):
        old = self._picked_dept()
        if not old:
            QMessageBox.information(self, "提示", "请先选中一个部门")
            return
        text, ok = QInputDialog.getText(self, "重命名部门", "新名称：", text=old)
        if not ok:
            return
        err = org_store.rename_dept(old, text)
        if err:
            QMessageBox.warning(self, "无法重命名", err)
        self.refresh()

    def _del_dept(self):
        name = self._picked_dept()
        if not name:
            QMessageBox.information(self, "提示", "请先选中一个部门")
            return
        if QMessageBox.question(
                self, "删除部门",
                f"确定删除「{name}」？部门内成员会被移出（变无部门）"
        ) != QMessageBox.StandardButton.Yes:
            return
        org_store.delete_dept(name)
        self.refresh()

    # ---------------- 线路归属 ----------------
    def _build_lines_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(8)
        t = QLabel("看板/明细按「线路归属」圈可见范围：成员只看自己名下线路，"
                   "主管看本部门全部成员的线路；未绑定的线路只有管理员可见")
        t.setObjectName("InlineTip")
        t.setWordWrap(True)
        v.addWidget(t)
        self.tbl_line = QTableWidget(0, 2)
        self.tbl_line.setHorizontalHeaderLabels(("线路", "归属成员"))
        self.tbl_line.verticalHeader().setVisible(False)
        self.tbl_line.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self.tbl_line.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tbl_line.setColumnWidth(1, 220)
        v.addWidget(self.tbl_line, 1)
        self.tabs.addTab(w, "📡 线路归属")

    def _reload_lines(self):
        lines = org_store.all_lines()
        owners = org_store.line_owners()
        members = [m["name"] for m in org_store.list_members() if m["active"]]
        sig = (tuple(lines), tuple(sorted(owners.items())), tuple(members))
        if sig == self._sig.get("line"):
            return
        self._sig["line"] = sig
        self.tbl_line.setRowCount(len(lines))
        for i, acct in enumerate(lines):
            it = QTableWidgetItem(acct)
            it.setToolTip(acct)
            self.tbl_line.setItem(i, 0, it)
            cb = QComboBox()
            cb.addItem("（未绑定）", "")
            for name in members:
                cb.addItem(name, name)
            cur = owners.get(acct, "")
            if cur and cb.findData(cur) < 0:      # 归属人已被删：补进选项看得见
                cb.addItem(cur + "（已删除）", cur)
            cb.setCurrentIndex(max(0, cb.findData(cur)))
            # activated 只由用户操作触发（程序 setCurrentIndex 不响）；
            # PySide6 只有 int 重载，按下标定到当前项的 data
            cb.activated.connect(
                lambda _i, a=acct, c=cb: self._set_owner(a, c))
            self.tbl_line.setCellWidget(i, 1, cb)

    def _set_owner(self, account, combo):
        owner = combo.currentData() or ""
        err = org_store.set_line_owner(account, owner)
        if err:
            QMessageBox.warning(self, "绑定失败", err)
            self._sig.pop("line", None)           # 失败回滚不了就强制重排
        self._reload_lines()                      # 签名已变才真正重建

    # ---------------- 定时刷新入口（主窗口 2s 驱动） ----------------
    def refresh(self):
        self._reload_members()
        self._reload_depts()
        self._reload_lines()
