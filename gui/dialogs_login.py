"""
gui/dialogs_login.py —— 组织成员登录窗（结构：左选成员 + 右输密码）

启用条件：org_members 表里有成员（org_store.org_enabled()）。
desktop 启动链在 db.init() 之后、MainWindow 之前调 require_login()，
未通过登录直接退出；主窗口「切换用户」也复用本对话框。

注意：与 dialogs_license 相同，所有输入框不做自动聚焦（setFocus）——
本软件有全局键盘监听，弹窗抢焦点会吞掉用户的按键习惯。
"""
import re

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QListWidget,
                               QListWidgetItem, QWidget)

from gui.window_frame import apply_rounded
from store import org_store


class _MemberRow(QWidget):
    """成员列表项：姓名加粗 + 角色/部门灰色副文字（两行小卡片）"""

    def __init__(self, m):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(1)
        name = QLabel(m["name"])
        name.setStyleSheet("font-weight: bold;")
        sub = f"{org_store.ROLES.get(m['role'], m['role'])}"
        if m.get("dept"):
            sub += f" · {m['dept']}"
        dep = QLabel(sub)
        dep.setStyleSheet("color: #8FA3BF; font-size: 11px;")
        lay.addWidget(name)
        lay.addWidget(dep)


class LoginDialog(QDialog):
    def __init__(self, parent=None, title="🔐 选择成员登录"):
        super().__init__(parent)
        self.setWindowTitle("成员登录")
        self.setFixedWidth(520)
        self._lock_left = 0                 # 锁定剩余秒数（>0 时禁止提交）

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(10)
        t = QLabel(title)
        t.setObjectName("DialogTitle")
        lay.addWidget(t)

        body = QHBoxLayout()
        body.setSpacing(14)
        col_l = QVBoxLayout()
        col_l.addWidget(QLabel("成员："))
        self.lst = QListWidget()
        self.lst.setObjectName("MemberList")
        self.lst.setMinimumHeight(240)
        self.lst.itemSelectionChanged.connect(self._show_hint)
        col_l.addWidget(self.lst, 1)
        body.addLayout(col_l, 1)

        col_r = QVBoxLayout()
        self.ed_pwd = QLineEdit()
        self.ed_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_pwd.setPlaceholderText("密码（回车登录）")
        self.ed_pwd.returnPressed.connect(self._submit)
        col_r.addWidget(QLabel("密码："))
        col_r.addWidget(self.ed_pwd)
        self.lbl_err = QLabel("")
        self.lbl_err.setStyleSheet("color: #F53F3F;")
        self.lbl_err.setWordWrap(True)
        col_r.addWidget(self.lbl_err)
        self.lbl_hint = QLabel("点选左侧成员，输入密码登录")
        self.lbl_hint.setObjectName("InlineTip")
        self.lbl_hint.setWordWrap(True)
        col_r.addWidget(self.lbl_hint)
        col_r.addStretch(1)
        brow = QHBoxLayout()
        self.b_ok = QPushButton("登 录")
        self.b_ok.clicked.connect(self._submit)
        self.b_cancel = QPushButton("取消退出")
        self.b_cancel.setObjectName("GhostBtn")
        self.b_cancel.clicked.connect(self.reject)
        brow.addWidget(self.b_ok, 1)
        brow.addWidget(self.b_cancel)
        col_r.addLayout(brow)
        body.addLayout(col_r, 1)
        lay.addLayout(body)

        self.reload_members()
        # 锁定倒计时：每秒一跳刷新提示，到 0 恢复提交
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._tick_lock)
        apply_rounded(self, show_min=False, show_max=False)

    # ---------------- 数据 ----------------
    def reload_members(self):
        """填 active 成员；默认选中第一个（只选行不抢焦点，见文件头注释）"""
        self.lst.clear()
        for m in org_store.list_members():
            if not m["active"]:
                continue
            it = QListWidgetItem()
            w = _MemberRow(m)
            it.setSizeHint(w.sizeHint())
            it.setData(Qt.ItemDataRole.UserRole, m["name"])
            self.lst.addItem(it)
            self.lst.setItemWidget(it, w)
        if self.lst.count():
            self.lst.setCurrentRow(0)
        else:
            self.lbl_err.setText("没有可登录的成员（全部被停用），请用救急通道处理")

    def _picked(self):
        it = self.lst.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else ""

    def _show_hint(self):
        name = self._picked()
        if name and self._lock_left <= 0:
            self.lbl_hint.setText(f"当前选择：{name}")

    # ---------------- 提交 / 锁定 ----------------
    def _submit(self):
        if self._lock_left > 0:
            return
        name = self._picked()
        if not name:
            self.lbl_err.setText("请先选择成员")
            return
        err = org_store.login(name, self.ed_pwd.text())
        if err is None:
            self.accept()
            return
        self.lbl_err.setText(err)
        self.ed_pwd.clear()
        left = _parse_lock_secs(err)
        if left:
            self._lock_left = left
            self.b_ok.setEnabled(False)
            self._tick.start()

    def _tick_lock(self):
        self._lock_left -= 1
        if self._lock_left <= 0:
            self._lock_left = 0
            self._tick.stop()
            self.b_ok.setEnabled(True)
            self.lbl_err.setText("")
            self._show_hint()
            return
        self.lbl_hint.setText(f"⏳ 已锁定，{self._lock_left} 秒后可重试")

    def reject(self):
        self._tick.stop()
        super().reject()


def _parse_lock_secs(msg):
    """从 login() 的中文报错里抠出锁定秒数：「请 N 秒后再试」/「锁定 N 分钟」"""
    m = re.search(r"(\d+)\s*秒后再试", msg or "")
    if m:
        return max(int(m.group(1)), 1)
    m = re.search(r"锁定\s*(\d+)\s*分钟", msg or "")
    if m:
        return int(m.group(1)) * 60
    return 0


def require_login(parent=None):
    """启动门禁统一入口：组织未启用直接放行；启用则必须登录成功。
    返回 True＝可以继续启动，False＝调用方退出应用。"""
    if not org_store.org_enabled():
        return True
    dlg = LoginDialog(parent)
    return dlg.exec() == QDialog.DialogCode.Accepted
