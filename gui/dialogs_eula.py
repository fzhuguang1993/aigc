"""
gui/dialogs_eula.py —— 用户协议：10 秒倒计时读完才能同意，拒绝即退出

放在启动最早处（首配之前）：首次运行弹出，读秒结束前「同意」按钮不可点，
避免用户没看就一路点下去。同意态按 EULA_VERSION 记进 app_state——日后条款有
大改动只需把版本号 +1，老用户会重新被要求确认一次。
"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QPlainTextEdit, QApplication)

from store import app_state
from gui.window_frame import apply_rounded

# 条款版本：改动实质条款时 +1，会让已同意过的用户重新确认一次
EULA_VERSION = 1
STATE_KEY = "eula_accepted"
COUNTDOWN = 10          # 最少阅读秒数

EULA_TEXT = """AIGC 视频助手 · 用户协议

请在点击「同意」前完整阅读本协议。继续使用本软件即表示你已阅读并同意以下条款。

一、软件许可
1. 本软件「AIGC 视频助手」由开发者授权你在个人或团队工作内容中安装、使用。
2. 授权为非独占、不可转让、不可再许可的普通使用权，不代表你获得任何知识产权的转让。

二、使用限制
1. 不得对本软件进行反向工程、反编译、反汇编或以其它方式试图获取源代码。
2. 不得复制、出租、出借、出售、散布或以本软件制作衍生作品用于再分发。
3. 不得利用本软件制作、发布违反法律法规、侵犯他人合法权益的内容。
4. 不得用于规避本软件的授权、限流、访问控制等保护机制。

三、第三方接口与费用
1. 本软件对接的若干 AI / 云服务接口（如视频生成、语音识别、图像理解、翻译、
   内容解析等）为第三方收费服务，需由你自行申请账号、密钥并承担相应费用。
2. 相关接口的可用性、计费规则、数据政策由第三方决定，本软件不对其作出任何保证，
   亦不对你的账号欠费、额度耗尽、接口变更或中断承担责任。

四、数据与隐私
1. 你的任务、素材、成品与配置默认存储在你本机（运行目录及系统用户配置目录），
   本软件不会主动将你的本地文件上传到除所用第三方接口以外的任何服务器。
2. 使用第三方接口时，你提交的提示词、素材等数据会按该接口服务商的政策被传输与处理，
   请自行阅读并遵守对应服务商的条款。
3. 请妥善备份你的工作成果；因你误删、设备故障、系统环境或不可抗力导致的数据丢失，
   本软件不承担恢复或赔偿责任。

五、无担保与责任限制
1. 本软件按「现状」提供，在法律允许的最大范围内，不作任何明示或默示担保，
   包括对适销性、特定用途适用性、不侵权以及生成结果准确性的担保。
2. 本软件生成的视频、文案、字幕等结果仅供参考，你需自行审核其合规性、准确性与
   适配性后再对外发布；因使用或无法使用本软件造成的任何直接或间接损失，
   开发者不承担责任，包括但不限于利润、商誉、数据或业务的损失。

六、条款更新与终止
1. 开发者有权在未来更新本协议；更新后的版本会随软件发布，重大变更会再次请你确认。
2. 你若不同意本协议或后续变更，应立即停止使用并卸载本软件。
3. 若你违反本协议，授权自动终止；你应删除本软件及其全部副本。

七、其它
1. 本协议构成你与开发者之间就使用本软件的完整约定。
2. 在法律允许的最大范围内，本协议的最终解释权归开发者所有。

（本协议为通用模板，不构成法律意见；如有商业发布需求，建议咨询专业法律人士。）"""


class EULADialog(QDialog):
    """滚动展示协议全文；底部「拒绝并退出」+「同意(N 秒)」，倒计时结束前同意不可点。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._accepted = False
        self._left = COUNTDOWN
        self.setWindowTitle("用户协议")
        self.resize(640, 560)
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(12)

        title = QLabel("请在使用前阅读以下用户协议")
        title.setObjectName("DialogTitle")
        v.addWidget(title)

        body = QPlainTextEdit()
        body.setReadOnly(True)
        body.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        body.setPlainText(EULA_TEXT)
        v.addWidget(body, 1)

        self.hint = QLabel("")
        self.hint.setObjectName("PageTip")
        v.addWidget(self.hint)

        row = QHBoxLayout()
        row.addStretch(1)
        b_no = QPushButton("拒绝并退出")
        b_no.setObjectName("GhostBtn")
        b_no.clicked.connect(self.reject)
        self.b_yes = QPushButton(f"同意（{self._left}）")
        self.b_yes.setDefault(True)
        self.b_yes.setEnabled(False)
        self.b_yes.clicked.connect(self._on_accept)
        row.addWidget(b_no)
        row.addWidget(self.b_yes)
        v.addLayout(row)
        apply_rounded(self, show_min=False, show_max=False)

        self._update_hint()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    def _update_hint(self):
        self.hint.setText(f"请阅读以上条款，还需 {self._left} 秒才能同意。")

    def _tick(self):
        self._left -= 1
        if self._left <= 0:
            self._timer.stop()
            self._left = 0
            self.b_yes.setEnabled(True)
            self.b_yes.setText("同意并继续")
            self.hint.setText("已阅读完毕，点击「同意并继续」开始使用。")
        else:
            self.b_yes.setText(f"同意（{self._left}）")
            self._update_hint()

    def _on_accept(self):
        self._accepted = True
        self.accept()

    def reject(self):
        # 点「拒绝」或标题栏 ✕（走 reject）都视为未同意
        self._accepted = False
        super().reject()


def is_accepted():
    return app_state.get(STATE_KEY) == EULA_VERSION


def ensure_accepted(parent=None):
    """已按当前版本同意过直接返回 True；否则弹协议框，同意返回 True、拒绝返回 False。"""
    if is_accepted():
        return True
    dlg = EULADialog(parent)
    ok = dlg.exec() == QDialog.DialogCode.Accepted and dlg._accepted
    if ok:
        app_state.set_value(STATE_KEY, EULA_VERSION)
    return ok
