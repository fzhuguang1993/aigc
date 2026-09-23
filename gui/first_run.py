"""
gui/first_run.py —— 首次启动配置弹窗（生成 config.json，格式与 setup_wizard 一致）
"""
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QMessageBox)

from core.config import defaults_accounts
from core.setup_wizard import _normalize_base, _freeze_accounts, save_first_config


class FirstRunDialog(QDialog):
    """首次配置：exe 已内置接口地址时只填姓名（同事不该背地址）；
    没内置才退回让使用者自己粘地址。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("首次配置")
        self._defaults = defaults_accounts()
        self.setFixedSize(540, 440 if not self._defaults else 300)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 24)

        title = QLabel("欢迎使用 AIGC 视频助手")
        title.setObjectName("DialogTitle")
        lay.addWidget(title)

        lay.addWidget(QLabel("你的姓名（用于视频文件命名）："))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：张三")
        lay.addWidget(self.name_edit)

        if self._defaults:
            tip = QLabel(f"✅ 接口地址已由维护人配好（共 {len(self._defaults)} 条线路），"
                         "填个姓名就能开始。如需增减线路去「⚙️ 设置」。")
            tip.setObjectName("InlineTip")
            tip.setWordWrap(True)
            lay.addWidget(tip)
        else:
            lay.addWidget(QLabel("API 服务地址（每行一个，至少填一个）："))
            self.urls_edit = QPlainTextEdit()
            self.urls_edit.setPlaceholderText(
                "http://106.75.1.98:7860\nhttps://xxx.pod.compshare.cn")
            self.urls_edit.setFixedHeight(130)
            lay.addWidget(self.urls_edit)

        btn = QPushButton("保存并开始使用")
        btn.clicked.connect(self._save)
        lay.addWidget(btn)

    def _save(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请填写姓名")
            return
        if self._defaults:
            accounts = self._defaults
        else:
            urls = [l.strip() for l in self.urls_edit.toPlainText().splitlines()
                    if l.strip()]
            if not urls:
                QMessageBox.warning(self, "提示", "请至少填写一个 API 地址")
                return
            accounts = [{"name": f"acc{i + 1}", "base": _normalize_base(u),
                         "concurrency": 1} for i, u in enumerate(urls)]
        # 开发机（未打包）+ 内置线路：只存姓名，不往 config.json 抄一份地址，
        # 否则以后改 config_local.py 会被优先级更高的 config.json 盖住
        save_first_config(name, accounts, freeze_accounts=_freeze_accounts(self._defaults))
        self.accept()
