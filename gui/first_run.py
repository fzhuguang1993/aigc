"""
gui/first_run.py —— 首次启动配置弹窗（生成 config.json，格式与 setup_wizard 一致）
"""
import json

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QMessageBox)

from core.config import CONFIG_JSON
from core.setup_wizard import _normalize_base


class FirstRunDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("首次配置")
        self.setFixedSize(540, 440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 24, 28, 24)

        title = QLabel("欢迎使用 AIGC 视频助手")
        title.setObjectName("DialogTitle")
        lay.addWidget(title)

        lay.addWidget(QLabel("你的姓名（用于视频文件命名）："))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：张三")
        lay.addWidget(self.name_edit)

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
        urls = [l.strip() for l in self.urls_edit.toPlainText().splitlines() if l.strip()]
        if not name:
            QMessageBox.warning(self, "提示", "请填写姓名")
            return
        if not urls:
            QMessageBox.warning(self, "提示", "请至少填写一个 API 地址")
            return
        accounts = [{"name": f"acc{i + 1}", "base": _normalize_base(u), "concurrency": 1}
                    for i, u in enumerate(urls)]
        CONFIG_JSON.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_JSON.write_text(
            json.dumps({"user_name": name, "accounts": accounts},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        self.accept()
