"""
gui/pages_settings.py —— 设置（编辑 config.json，保存后需重启软件生效）
"""
import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QHeaderView, QMessageBox, QCheckBox, QApplication,
                               QFileDialog, QInputDialog, QAbstractItemView)

from core.config import (CONFIG_JSON, USER_NAME, ACCOUNTS, SCRIPT_CHECK,
                         DOWNLOAD_DIR, EXPORT_DIR, MATERIAL_DIR, RUNTIME_DIR)
from core.api_client import health
from core.setup_wizard import _normalize_base
from gui.header import page_header
from gui.tool_panels import API_MAINTAINER_CODE, MAINTAINER_SHORTCUT


def _same_path(a, b):
    """判断两个目录字符串是否指向同一处（Windows 忽略大小写/分隔符）。"""
    return os.path.normcase(os.path.normpath(str(a))) == \
        os.path.normcase(os.path.normpath(str(b)))


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


def parse_lines(text):
    """剪贴板文本 -> 线路列表，供「粘贴导入」用。

    接受三种写法：「📋 复制线路」的输出 {"accounts": [...]}、纯数组 [...]、
    以及完整的 config.json（同样取其中的 accounts 字段）。
    接口地址已归一化补上 /api/v1 后缀；并发数非法时回到 1。
    解析不出来返回 None，调用方据此保持表格原样不动。
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    if isinstance(data, dict):
        data = data.get("accounts")
    if not isinstance(data, list):
        return None

    rows = []
    for item in data:
        if isinstance(item, str):                      # 允许只贴一串地址
            item = {"base": item}
        if not isinstance(item, dict):
            continue
        base = str(item.get("base", "")).strip()
        if not base:
            continue
        try:
            conc = int(float(item.get("concurrency", 1) or 1))
        except (TypeError, ValueError):
            conc = 1
        name = str(item.get("name", "")).strip() or f"acc{len(rows) + 1}"
        rows.append({"name": name, "base": _normalize_base(base),
                     "concurrency": max(conc, 1)})
    return rows or None


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

        # ---------- 输出目录：默认隐藏，Alt+W（Mac ⌘+W）口令解锁后才出现（不暴露入口） ----------
        self._dirs_unlocked = False
        self.dirs_box = QWidget()
        dbox = QVBoxLayout(self.dirs_box)
        dbox.setContentsMargins(0, 0, 0, 0)
        dbox.setSpacing(8)
        dbox.addWidget(QLabel("输出目录（留空＝用默认，修改保存后重启生效）："))
        self._dir_edits = {}
        for key, label, cur, dft in (
                ("output", "视频输出", DOWNLOAD_DIR, str(RUNTIME_DIR / "outputs")),
                ("export", "模板/导出", EXPORT_DIR, str(RUNTIME_DIR / "exports")),
                ("material", "素材目录", MATERIAL_DIR, str(RUNTIME_DIR / "material"))):
            r = QHBoxLayout()
            r.addWidget(QLabel(label + "："))
            ed = QLineEdit("" if _same_path(cur, dft) else cur)
            ed.setPlaceholderText("默认：" + dft)
            ed.setProperty("default", dft)
            b = QPushButton("浏览…")
            b.setObjectName("GhostBtn")
            b.clicked.connect(lambda _=False, e=ed: self._pick_dir(e))
            r.addWidget(ed, 1)
            r.addWidget(b)
            dbox.addLayout(r)
            self._dir_edits[key] = ed
        self.dirs_box.setVisible(False)
        lay.addWidget(self.dirs_box)

        lay.addWidget(QLabel("API 服务地址（一个地址 = 一个账号，双击单元格可编辑；"
                             "拖动/Ctrl 可多选行，删除只弹一次确认）："))
        self.acc_table = QTableWidget(0, 3)
        self.acc_table.setHorizontalHeaderLabels(["账号名", "接口地址", "并发数"])
        self.acc_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.acc_table.setColumnWidth(0, 110)
        self.acc_table.setColumnWidth(2, 70)
        # 行高兜底：样式表里 QTableWidget::item 有 padding 5px，默认行高（~25px）
        # 会把地址文字上下裁掉一截，双击编辑时尤其痛苦
        self.acc_table.verticalHeader().setDefaultSectionSize(36)
        # 整行选中 + 连续多选：鼠标按住拖就能圈好几行，配合批量删除
        self.acc_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.acc_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lay.addWidget(self.acc_table, 1)

        bar = QHBoxLayout()
        b_add = QPushButton("＋ 添加地址")
        b_add.setObjectName("GhostBtn")
        b_add.clicked.connect(lambda: self._add_row())
        b_del = QPushButton("🗑 删除所选行")
        b_del.setObjectName("GhostBtn")
        b_del.setToolTip("可先用鼠标拖动多选几行；删除只弹一次确认，一次删掉全部选中行")
        b_del.clicked.connect(self._del_rows)
        b_check = QPushButton("🔌 测试连通性")
        b_check.setObjectName("GhostBtn")
        b_check.clicked.connect(self._check)
        self.b_check = b_check
        b_copy = QPushButton("📋 复制线路")
        b_copy.setObjectName("GhostBtn")
        b_copy.setToolTip("把下面的线路列表复制成一段文本，微信/飞书发给同事，"
                          "对方点「📥 粘贴导入」即可，不必每人手敲 7 个地址")
        b_copy.clicked.connect(self._copy_lines)
        b_paste = QPushButton("📥 粘贴导入")
        b_paste.setObjectName("GhostBtn")
        b_paste.setToolTip("读取剪贴板里的线路配置覆盖当前表格（改完记得保存）；"
                           "支持「📋 复制线路」的输出、纯地址数组、或整份 config.json")
        b_paste.clicked.connect(self._paste_lines)
        b_save = QPushButton("💾 保存设置")
        b_save.clicked.connect(self._save)
        bar.addWidget(b_add)
        bar.addWidget(b_del)
        bar.addWidget(b_check)
        bar.addWidget(b_copy)
        bar.addWidget(b_paste)
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

        # 维护人入口：Alt+W（Mac ⌘+W）唤出口令框，验证通过才显示「输出目录」；
        # 仅当停在设置页时激活（show/hideEvent 开关），避免别处误触。
        self._sc_dirs = QShortcut(QKeySequence(MAINTAINER_SHORTCUT), self)
        self._sc_dirs.setContext(Qt.ShortcutContext.WindowShortcut)
        self._sc_dirs.activated.connect(self._summon_dirs)
        self._sc_dirs.setEnabled(False)

    def refresh(self):
        pass  # 不自动覆盖用户正在编辑的内容

    # ---------- 维护人入口：口令解锁隐藏的「输出目录」 ----------
    def _summon_dirs(self):
        if self._dirs_unlocked:               # 已展开则不重复要口令
            return
        code, ok = QInputDialog.getText(self, "维护人验证", "请输入维护人口令：",
                                        QLineEdit.EchoMode.Password)
        if not ok:
            return
        if code == API_MAINTAINER_CODE:
            self.dirs_box.setVisible(True)
            self._dirs_unlocked = True
        else:
            QMessageBox.warning(self, "口令错误", "维护人口令不正确")

    def _relock_dirs(self):
        self.dirs_box.setVisible(False)
        self._dirs_unlocked = False

    def showEvent(self, event):
        super().showEvent(event)
        self._sc_dirs.setEnabled(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._sc_dirs.setEnabled(False)

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

    def _del_rows(self):
        """批量删除：选中几删几，一次确认全删（旧实现无确认且只能删当前一行）"""
        rows = sorted({idx.row() for idx in self.acc_table.selectionModel().selectedRows()})
        if not rows:
            QMessageBox.information(self, "提示", "先用鼠标点选/拖选要删的线路行")
            return
        names = "、".join(self.acc_table.item(r, 0).text() or f"第{r + 1}行"
                          for r in rows[:6]) + ("…" if len(rows) > 6 else "")
        if QMessageBox.question(
                self, "删除线路",
                f"确定删除选中的 {len(rows)} 条线路？（{names}）\n"
                "删除后仍需点「💾 保存设置」才写入配置。") \
                != QMessageBox.StandardButton.Yes:
            return
        for r in reversed(rows):              # 从大到小删，行号不位移
            self.acc_table.removeRow(r)

    def _pick_dir(self, ed):
        start = ed.text().strip() or ed.property("default") or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "选择目录", start)
        if d:
            ed.setText(d)

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

    # ---------- 线路配置分享 ----------
    def _copy_lines(self):
        """线路列表 -> 剪贴板 JSON，同事粘贴导入即可，免去逐台手敲地址"""
        rows = self._rows()
        if not rows:
            QMessageBox.warning(self, "提示", "没有可复制的线路，请先填写接口地址")
            return
        QApplication.clipboard().setText(
            json.dumps({"accounts": rows}, ensure_ascii=False, indent=2))
        QMessageBox.information(
            self, "已复制",
            f"已复制 {len(rows)} 条线路到剪贴板，发给同事后在对方电脑\n"
            f"点「📥 粘贴导入」即可（导入后仍需点「💾 保存设置」并重启）。\n\n"
            "提醒：接口地址本身就是访问凭证，只发给内部同事。")

    def _paste_lines(self):
        """剪贴板 JSON -> 表格（覆盖前先确认，解析失败不碰现有内容）"""
        rows = parse_lines(QApplication.clipboard().text())
        if not rows:
            QMessageBox.warning(
                self, "格式不对",
                "剪贴板里不是可识别的线路配置。\n\n"
                "支持：本软件「📋 复制线路」的输出、纯接口地址数组、或整份 config.json")
            return
        if self.acc_table.rowCount():
            ret = QMessageBox.question(
                self, "导入线路",
                f"将用剪贴板里的 {len(rows)} 条线路替换当前 "
                f"{self.acc_table.rowCount()} 条，确定吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
        self.acc_table.setRowCount(0)
        for r in rows:
            self._add_row(r["name"], r["base"], r["concurrency"])
        QMessageBox.information(self, "已导入",
                                f"已导入 {len(rows)} 条线路，点「💾 保存设置」并重启生效")

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
        # 输出目录：只存与默认不同的自定义值；全空则删除 paths 键（回到默认）
        paths = {}
        for key, ed in self._dir_edits.items():
            v = ed.text().strip()
            if v and not _same_path(v, ed.property("default")):
                paths[key] = v
        if paths:
            data["paths"] = paths
        else:
            data.pop("paths", None)
        CONFIG_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        if self._dirs_unlocked:            # 保存后收回隐藏，下次再改需重新按口令
            self._relock_dirs()
        QMessageBox.information(self, "已保存", "设置已保存，重启软件后生效")
