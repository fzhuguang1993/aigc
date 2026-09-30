"""
gui/dialogs_login.py —— 组织成员登录窗（账号 + 密码）

启用条件：org_members 表里有成员（org_store.org_enabled()）。
按账号定身份：输入成员姓名 + 密码，登录成功后身份/角色就是该成员在组织结构里
被设定的那样（admin/manager/member），不是让用户在下拉里“选身份”。
org_store.login() 校验通过即写入当前会话。

启动不再强制弹登录框：主窗口左下角侧栏「当前用户」处点「登录 / 切换用户」才弹本
对话框（require_login() 保留给需要启动门禁的调用方）。

注意：与 dialogs_license 相同，所有输入框不做自动聚焦（setFocus）——
本软件有全局键盘监听，弹窗抢焦点会吞掉用户的按键习惯。
"""
import re

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QFormLayout)

from gui.window_frame import apply_rounded
from store import org_store


class LoginDialog(QDialog):
    def __init__(self, parent=None, title="🔑 成员登录"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedWidth(360)
        self._lock_left = 0                 # 锁定剩余秒数（>0 时禁止提交）

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        t = QLabel(title)
        t.setObjectName("DialogTitle")
        lay.addWidget(t)

        form = QFormLayout()
        form.setSpacing(10)
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("成员姓名")
        form.addRow("账号：", self.ed_name)
        self.ed_pwd = QLineEdit()
        self.ed_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_pwd.setPlaceholderText("密码（回车登录）")
        self.ed_pwd.returnPressed.connect(self._submit)
        form.addRow("密码：", self.ed_pwd)
        lay.addLayout(form)

        self.lbl_err = QLabel("")
        self.lbl_err.setStyleSheet("color: #F53F3F;")
        self.lbl_err.setWordWrap(True)
        lay.addWidget(self.lbl_err)

        brow = QHBoxLayout()
        brow.setSpacing(10)
        self.b_ok = QPushButton("登 录")
        self.b_ok.clicked.connect(self._submit)
        self.b_cancel = QPushButton("取消")
        self.b_cancel.setObjectName("GhostBtn")
        self.b_cancel.clicked.connect(self.reject)
        brow.addWidget(self.b_ok, 1)
        brow.addWidget(self.b_cancel)
        lay.addLayout(brow)

        # 锁定倒计时：每秒一跳刷新提示，到 0 恢复提交
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._tick_lock)
        apply_rounded(self, show_min=False, show_max=False)

    # ---------------- 提交 / 锁定 ----------------
    def _submit(self):
        if self._lock_left > 0:
            return
        name = self.ed_name.text().strip()
        if not name:
            self.lbl_err.setText("请输入成员姓名")
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
            return
        self.lbl_err.setText(f"⏳ 已锁定，{self._lock_left} 秒后可重试")

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
    """备用启动门禁：组织未启用直接放行；启用则弹登录框，取消＝调用方退出。
    返回 True＝可以继续，False＝调用方退出应用。
    （当前 desktop 启动已不调用它——登录入口挪到主窗口左下角侧栏。）"""
    if not org_store.org_enabled():
        return True
    dlg = LoginDialog(parent)
    return dlg.exec() == QDialog.DialogCode.Accepted
