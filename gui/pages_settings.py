"""
gui/pages_settings.py —— 设置（编辑 config.json，保存后需重启软件生效）
"""
import json

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QHeaderView, QMessageBox, QCheckBox)

from core.config import CONFIG_JSON, USER_NAME, ACCOUNTS, SCRIPT_CHECK
from core.api_client import health
from core.setup_wizard import _normalize_base
from gui.header import page_header


class _CheckWorker(QThread):
    """后台连通性检测：逐个请求在线程里跑，不阻塞界面"""
    done = Signal(list)

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def run(self):
        results = []
        for a in self.rows:
            try:
                health(a["base"])
                results.append(f"🟢 {a['name']} 连通正常")
            except Exception as e:
                results.append(f"🔴 {a['name']} 无法连通：{type(e).__name__}")
        self.done.emit(results)


class SettingsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(8)

        head = page_header("设置", "修改保存后重启生效", icon="⚙️")
        head.setToolTip(f"配置文件：{CONFIG_JSON}")
        lay.addWidget(head)

        row = QHBoxLayout()
        row.addWidget(QLabel("姓名："))
        self.name_edit = QLineEdit(USER_NAME)
        self.name_edit.setFixedWidth(220)
        row.addWidget(self.name_edit)
        row.addStretch(1)
        lay.addLayout(row)

        lay.addWidget(QLabel("API 服务地址（一个地址 = 一个账号，双击单元格可编辑）："))
        self.acc_table = QTableWidget(0, 3)
        self.acc_table.setHorizontalHeaderLabels(["账号名", "接口地址", "并发数"])
        self.acc_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.acc_table.setColumnWidth(0, 110)
        self.acc_table.setColumnWidth(2, 70)
        lay.addWidget(self.acc_table, 1)

        bar = QHBoxLayout()
        b_add = QPushButton("＋ 添加地址")
        b_add.setObjectName("GhostBtn")
        b_add.clicked.connect(lambda: self._add_row())
        b_del = QPushButton("🗑 删除选中行")
        b_del.setObjectName("GhostBtn")
        b_del.clicked.connect(self._del_row)
        b_check = QPushButton("🔌 测试连通性")
        b_check.setObjectName("GhostBtn")
        b_check.clicked.connect(self._check)
        self.b_check = b_check
        b_save = QPushButton("💾 保存设置")
        b_save.clicked.connect(self._save)
        bar.addWidget(b_add)
        bar.addWidget(b_del)
        bar.addWidget(b_check)
        bar.addStretch(1)
        bar.addWidget(b_save)
        lay.addLayout(bar)

        self._load_current()
        self._checker = None

        # ---------- 脚本 AI 检测接口（规范卡口播检测用，可留空） ----------
        lay.addWidget(QLabel("脚本 AI 检测接口（可选 —— 「🧐 口播规范检测」的 AI 智能检测用，不配置也能用本地规则检测）："))
        crow = QHBoxLayout()
        self.ck_ai = QCheckBox("启用")
        self.ck_ai.setChecked(bool(SCRIPT_CHECK.get("enabled")))
        crow.addWidget(self.ck_ai)
        crow.addWidget(QLabel("地址"))
        self.ed_ai_url = QLineEdit(SCRIPT_CHECK.get("url", ""))
        self.ed_ai_url.setPlaceholderText("https://.../v1/chat/completions（OpenAI 兼容格式）")
        crow.addWidget(self.ed_ai_url, 2)
        crow.addWidget(QLabel("Key"))
        self.ed_ai_key = QLineEdit(SCRIPT_CHECK.get("api_key", ""))
        self.ed_ai_key.setEchoMode(QLineEdit.EchoMode.Password)
        crow.addWidget(self.ed_ai_key, 1)
        crow.addWidget(QLabel("模型"))
        self.ed_ai_model = QLineEdit(SCRIPT_CHECK.get("model", ""))
        self.ed_ai_model.setPlaceholderText("gpt-4o-mini")
        self.ed_ai_model.setFixedWidth(140)
        crow.addWidget(self.ed_ai_model)
        lay.addLayout(crow)

    def refresh(self):
        pass  # 不自动覆盖用户正在编辑的内容

    # ---------- 数据装载 ----------
    def _load_current(self):
        for a in ACCOUNTS:
            self._add_row(a["name"], a["base"], a["concurrency"])
        if self.acc_table.rowCount() == 0:
            self._add_row()

    def _add_row(self, name="acc1", base="", conc=1):
        r = self.acc_table.rowCount()
        self.acc_table.insertRow(r)
        self.acc_table.setItem(r, 0, QTableWidgetItem(name))
        self.acc_table.setItem(r, 1, QTableWidgetItem(base))
        self.acc_table.setItem(r, 2, QTableWidgetItem(str(conc)))

    def _del_row(self):
        r = self.acc_table.currentRow()
        if r >= 0:
            self.acc_table.removeRow(r)

    def _rows(self):
        out = []
        for r in range(self.acc_table.rowCount()):
            name = self.acc_table.item(r, 0).text().strip()
            base = self.acc_table.item(r, 1).text().strip()
            try:
                conc = int(self.acc_table.item(r, 2).text())
            except (TypeError, ValueError):
                conc = 1
            if base:
                out.append({"name": name or f"acc{r + 1}",
                            "base": _normalize_base(base),
                            "concurrency": max(conc, 1)})
        return out

    # ---------- 操作 ----------
    def _check(self):
        rows = self._rows()
        if not rows:
            QMessageBox.warning(self, "提示", "请先填写接口地址")
            return
        if self._checker is not None and self._checker.isRunning():
            return
        self.b_check.setEnabled(False)
        self.b_check.setText("⏳ 检测中…")
        self._checker = _CheckWorker(rows)
        self._checker.done.connect(self._check_done)
        self._checker.start()

    def _check_done(self, results):
        self.b_check.setEnabled(True)
        self.b_check.setText("🔌 测试连通性")
        QMessageBox.information(self, "连通性检测", "\n".join(results))

    def _save(self):
        accounts = self._rows()
        if not accounts:
            QMessageBox.warning(self, "提示", "至少保留一个接口地址")
            return
        data = {}
        if CONFIG_JSON.exists():        # 先读旧配置，合并写回，不丢其它字段
            try:
                data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data["user_name"] = self.name_edit.text().strip()
        data["accounts"] = accounts
        data["script_check"] = {
            "enabled": self.ck_ai.isChecked(),
            "url": self.ed_ai_url.text().strip(),
            "api_key": self.ed_ai_key.text().strip(),
            "model": self.ed_ai_model.text().strip(),
        }
        CONFIG_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        QMessageBox.information(self, "已保存", "设置已保存，重启软件后生效")
