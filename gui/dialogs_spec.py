"""
gui/dialogs_spec.py —— 规范卡编辑器 + 口播规范检测结果对话框
"""
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QPlainTextEdit, QTextBrowser,
                               QDialogButtonBox)
from PySide6.QtGui import QGuiApplication

from store import product_store as ps
from checkers import spec_checker

_LEVEL_COLOR = {"高": "#F54A45", "中": "#FF8D19", "提示": "#3370FF"}


class SpecCardDialog(QDialog):
    """产品规范卡：套餐/价格口径/活动口径/禁用词等模板化字段"""

    def __init__(self, parent, product):
        super().__init__(parent)
        self.pid = product["id"]
        self.setWindowTitle(f"规范卡 —— {product['name']}")
        self.resize(560, 660)
        lay = QVBoxLayout(self)
        tip = QLabel("定义这个品「卖什么、怎么报价、哪些话不能说」；"
                     "任务中心的「🧐 口播规范检测」会按此卡逐项核对口播脚本。")
        tip.setObjectName("PageTip")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.editors = {}
        spec = ps.get_spec(self.pid)
        for name, placeholder in ps.SPEC_FIELDS:
            lab = QLabel(name)
            lab.setStyleSheet("font-weight:700; color:#1F2329;")
            ed = QPlainTextEdit()
            ed.setPlaceholderText(placeholder)
            ed.setFixedHeight(48 if name in ("禁用词", "资质/备案号") else 64)
            ed.setPlainText(spec.get(name, ""))
            lay.addWidget(lab)
            lay.addWidget(ed)
            self.editors[name] = ed

        bb = QDialogButtonBox()
        b_ok = bb.addButton("💾 保存规范卡", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        b_ok.clicked.connect(self._save)
        lay.addWidget(bb)

    def _save(self):
        ps.set_spec(self.pid, {k: e.toPlainText().strip() for k, e in self.editors.items()})
        self.accept()


class _RemoteWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, text, spec, pname):
        super().__init__()
        self.args = (text, spec, pname)

    def run(self):
        try:
            ok, out = spec_checker.check_remote(*self.args)
        except Exception as e:
            ok, out = False, f"AI 检测异常：{e}"
        self.done.emit(ok, out)


class SpecCheckDialog(QDialog):
    """检测结果展示：本地规则结论 + 预留 AI 智能检测按钮（后台线程调用）"""

    def __init__(self, parent, title, text, spec, product_name=""):
        super().__init__(parent)
        self.setWindowTitle("🧐 口播规范检测")
        self.resize(620, 560)
        self._text, self._spec, self._pname = text, spec, product_name
        lay = QVBoxLayout(self)

        head = QLabel(f"<b>{title}</b>")
        lay.addWidget(head)
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        lay.addWidget(self.view, 1)

        issues = spec_checker.check_local(text, spec, product_name)
        self._local_html = self._render_local(issues)
        self.view.setHtml(self._local_html)

        bar = QHBoxLayout()
        configured = spec_checker.remote_configured()
        self.b_ai = QPushButton("🤖 AI 智能检测" + ("" if configured else "（未配置接口）"))
        self.b_ai.setObjectName("GhostBtn")
        self.b_ai.setEnabled(configured)
        if not configured:
            self.b_ai.setToolTip("暂未配置外部 AI 接口：到「设置 → 脚本 AI 检测接口」填入地址后即可启用。\n"
                                 "本地规则检测（禁用词/价格口径/必含话术）已可用。")
        self.b_ai.clicked.connect(self._run_ai)
        b_copy = QPushButton("📋 复制结果")
        b_copy.setObjectName("GhostBtn")
        b_copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(
            self.view.toPlainText()))
        b_close = QPushButton("关闭")
        b_close.setObjectName("GhostBtn")
        b_close.clicked.connect(self.accept)
        bar.addWidget(self.b_ai)
        bar.addStretch(1)
        bar.addWidget(b_copy)
        bar.addWidget(b_close)
        lay.addLayout(bar)
        self._worker = None

    @staticmethod
    def _render_local(issues):
        rows = []
        for it in issues:
            c = _LEVEL_COLOR.get(it["level"], "#646A73")
            rows.append(f"<tr><td valign='top' style='padding:3px 8px 3px 0'>"
                        f"<span style='color:{c};font-weight:700'>【{it['level']}】</span>"
                        f"</td><td style='padding:3px 0'>{it['msg']}</td></tr>")
        if rows:
            body = "<table>" + "".join(rows) + "</table>"
            head = (f"<p style='color:#1F2329'><b>本地规则检测：发现 {len(issues)} 处问题</b>"
                    f"（禁用词 / 价格口径 / 必含话术 / 活动兜底）</p>")
        else:
            body = ("<p style='color:#00B96B;font-weight:700'>✔ 本地规则检测通过，"
                    "未发现禁用词、价格口径、必含话术问题。</p>")
            head = ""
        return (f"<div style='font-size:13px'>{head}{body}"
                f"<p style='color:#8F959E;font-size:12px'>说明：本地规则覆盖字面违规；"
                f"语义级风险（夸大暗示、场景违规）需配置 AI 接口后补充检测。</p></div>")

    def _run_ai(self):
        self.b_ai.setEnabled(False)
        self.b_ai.setText("⏳ AI 检测中…")
        self._worker = _RemoteWorker(self._text, self._spec, self._pname)
        self._worker.done.connect(self._ai_done)
        self._worker.start()

    def _ai_done(self, ok, out):
        self.b_ai.setEnabled(True)
        self.b_ai.setText("🤖 AI 智能检测")
        safe = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        color = "#1F2329" if ok else "#F54A45"
        self.view.setHtml(self._local_html +
                          f"<hr><p style='font-weight:700'>🤖 AI 智能检测结论</p>"
                          f"<pre style='white-space:pre-wrap;color:{color}'>{safe}</pre>")
