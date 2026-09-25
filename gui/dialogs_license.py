"""
gui/dialogs_license.py —— 卡密激活 / 续费窗口（商用网关模式）

三种打开场景共用同一个窗口：
- 首次启动未激活（desktop 启动拦截，mode="activate"，取消则退出程序）；
- 到期/被封后重新激活（主窗口 24h 复核弹出）；
- 设置页主动续费（mode="renew"，可取消不影响使用）。

注意：窗口内所有输入框都不做自动聚焦（setFocus）——本软件有全局
键盘监听，弹窗抢焦点会吞掉用户的按键习惯，与既有对话框保持一致。
"""
from datetime import datetime

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QMessageBox,
                               QApplication)

from core import license as lic


def _fmt_ts(ts):
    if not ts:
        return "-"
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")


class _ActivateWorker(QThread):
    """网络激活放线程里跑：服务器无响应时界面不卡死"""
    done = Signal(bool, object)          # (成功, info dict 或 错误消息 str)

    def __init__(self, card_key):
        super().__init__()
        self.card_key = card_key

    def run(self):
        try:
            ok, info = lic.activate(self.card_key)
        except Exception as e:            # 兜底：任何异常都不该让线程崩掉无回音
            ok, info = False, f"激活过程出错：{type(e).__name__}"
        self.done.emit(ok, info)


from gui.window_frame import apply_rounded


class LicenseDialog(QDialog):
    def __init__(self, parent=None, mode="activate", message=""):
        """mode: "activate" 首次/重新激活；"renew" 续费（文案不同，逻辑一样）"""
        super().__init__(parent)
        self._renew = mode == "renew"
        self.setWindowTitle("输入卡密续费" if self._renew else "软件激活")
        self.setFixedWidth(460)
        self._worker = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 28, 22)
        lay.setSpacing(10)

        title = QLabel("💳 续费" if self._renew else "🔑 激活软件")
        title.setObjectName("DialogTitle")
        lay.addWidget(title)

        tip = message or ("输入新的卡密即可续费，剩余天数自动叠加。" if self._renew else
                          "请输入卡密激活软件。卡密绑定本机，一卡一机。")
        lbl_tip = QLabel(tip)
        lbl_tip.setObjectName("InlineTip")
        lbl_tip.setWordWrap(True)
        lay.addWidget(lbl_tip)

        # 已有授权时显示当前到期时间，续费时能看清"加在哪"
        expire = lic.expire_at()
        if expire:
            row = QHBoxLayout()
            row.addWidget(QLabel("当前到期时间："))
            v = QLabel(_fmt_ts(expire) + f"（剩余 {lic.days_remaining()} 天）")
            v.setObjectName("InlineTip")
            row.addWidget(v, 1)
            lay.addLayout(row)

        lay.addWidget(QLabel("卡密："))
        self.ed_card = QLineEdit()
        self.ed_card.setPlaceholderText("例如：AB2D-EF6H-JK8M-NPQR")
        self.ed_card.setClearButtonEnabled(True)
        # 不 setFocus：见文件头注释（全局键盘监听陷阱）
        self.ed_card.returnPressed.connect(self._submit)
        lay.addWidget(self.ed_card)

        mrow = QHBoxLayout()
        mrow.addWidget(QLabel("本机机器码："))
        self._machine = lic.machine_code()
        ml = QLabel(self._machine)
        ml.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        mrow.addWidget(ml, 1)
        b_copy = QPushButton("📋 复制")
        b_copy.setObjectName("GhostBtn")
        b_copy.setToolTip("把机器码发给售后，用于换机/查卡")
        b_copy.clicked.connect(self._copy_machine)
        mrow.addWidget(b_copy)
        lay.addLayout(mrow)

        lay.addSpacing(4)
        brow = QHBoxLayout()
        b_ok = QPushButton("激活" if not self._renew else "立即续费")
        b_ok.clicked.connect(self._submit)
        b_cancel = QPushButton("稍后再说" if self._renew else "取消退出")
        b_cancel.setObjectName("GhostBtn")
        b_cancel.clicked.connect(self.reject)
        brow.addWidget(b_ok, 1)
        brow.addWidget(b_cancel)
        lay.addLayout(brow)
        self._b_ok = b_ok
        self._b_cancel = b_cancel
        apply_rounded(self, show_min=False, show_max=False)

    # ---------------- 交互 ----------------
    def _copy_machine(self):
        QApplication.clipboard().setText(self._machine)
        QMessageBox.information(self, "已复制", "机器码已复制到剪贴板。")

    def _submit(self):
        if self._worker is not None and self._worker.isRunning():
            return
        card = self.ed_card.text().strip()
        if not card:
            QMessageBox.warning(self, "提示", "请输入卡密")
            return
        self._b_ok.setEnabled(False)
        self._b_ok.setText("⏳ 正在激活…")
        self._worker = _ActivateWorker(card)
        self._worker.done.connect(self._done)
        self._worker.start()

    def _done(self, ok, info):
        self._b_ok.setEnabled(True)
        self._b_ok.setText("立即续费" if self._renew else "激活")
        if not ok:
            QMessageBox.warning(self, "激活失败", str(info))
            return
        QMessageBox.information(
            self, "激活成功",
            f"授权有效期至：{_fmt_ts(info.get('expire_at'))}\n"
            f"剩余天数：约 {info.get('days_remaining', lic.days_remaining())} 天")
        self.accept()

    def reject(self):
        """关闭窗口前先掐掉未完成的请求回调（线程随对话框销毁不安全）"""
        if self._worker is not None and self._worker.isRunning():
            self._worker.done.disconnect()
            self._worker.wait(3)
        super().reject()


def ensure_valid(parent=None):
    """启动/到期统一入口：已过 license.check_on_startup 就返回 True；
    否则弹激活窗，用户激活成功返回 True，放弃返回 False（调用方据此退出）。
    开发直连模式（未配 GATEWAY_BASE）直接放行。"""
    ok, info = lic.check_on_startup()
    if ok:
        return True
    dlg = LicenseDialog(parent, mode="activate",
                        message=info if isinstance(info, str) else "")
    return dlg.exec() == QDialog.DialogCode.Accepted
