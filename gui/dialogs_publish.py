"""
gui/dialogs_publish.py —— 工具中心「一键发布」面板

纯 UI：选成品视频 + 填共享元数据 + 勾选平台账号 → 后台 ToolWorker 跑
video_text_tools.publish.runner.publish_batch，逐条回报、结果落 publishes 表。
账号凭证（cookie / OAuth token）是使用者自有的平台登录态，经 core.config 用
姓名加密存 config.json；本面板不套维护人口令（区别于素材提取的接口密钥）。

独立成模块避开 tool_panels 循环导入（在此拿 BasePanel/ToolWorker，tool_panels
又要延迟导入本模块的 PublishPanel）。
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QLineEdit, QComboBox, QPlainTextEdit, QDialog,
                               QTableWidget, QTableWidgetItem, QHeaderView,
                               QAbstractItemView, QMessageBox, QFormLayout,
                               QFileDialog, QGroupBox, QWidget)

from gui.header import page_header
from gui.tool_panels import BasePanel, ToolWorker, FileListWidget, VIDEO_EXT


def _split_tags(s):
    for sep in ("，", ",", " "):
        s = s.replace(sep, "\n")
    return [t for t in (x.strip().lstrip("#") for x in s.split("\n")) if t]


# ====================================================================
# 账号编辑对话框（使用者自有的平台凭证，不套维护人口令）
# ====================================================================
class AccountDialog(QDialog):
    def __init__(self, parent=None, account=None):
        super().__init__(parent)
        self._edit = account
        self.setWindowTitle("编辑发布账号" if account else "添加发布账号")
        self.resize(520, 360)
        v = QVBoxLayout(self)
        tip = QLabel("凭证仅用于把成品发布到你自己的平台账号，以使用人姓名加密存本机，"
                     "不明文落盘、不上传任何服务器。")
        tip.setWordWrap(True)
        tip.setObjectName("PageTip")
        v.addWidget(tip)

        form = QFormLayout()
        self.cb_platform = QComboBox()
        from video_text_tools.publish.models import PLATFORMS
        for key, name in PLATFORMS:
            self.cb_platform.addItem(name, key)
        self.ed_label = QLineEdit()
        self.ed_label.setPlaceholderText("账号别名，如：主号 / 小号A")
        self.cb_auth = QComboBox()
        form.addRow("平台：", self.cb_platform)
        form.addRow("别名：", self.ed_label)
        form.addRow("鉴权方式：", self.cb_auth)
        v.addLayout(form)

        # cookie 面板：整段粘贴
        self.ed_cookie = QPlainTextEdit()
        self.ed_cookie.setPlaceholderText(
            "粘贴创作者中心登录后的整串 Cookie（如 sessionid=...; ...）")
        # oauth 面板：access_token / refresh_token
        oauth = QWidget()
        of = QFormLayout(oauth)
        of.setContentsMargins(0, 0, 0, 0)
        self.ed_at = QLineEdit()
        self.ed_at.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_rt = QLineEdit()
        self.ed_rt.setEchoMode(QLineEdit.EchoMode.Password)
        of.addRow("Access Token：", self.ed_at)
        of.addRow("Refresh Token：", self.ed_rt)
        self._pages = {}
        from video_text_tools.publish.models import AUTH_COOKIE, AUTH_OAUTH
        holder = QGroupBox()
        hv = QVBoxLayout(holder)
        hv.setContentsMargins(0, 4, 0, 0)
        hv.addWidget(self.ed_cookie)
        hv.addWidget(oauth)
        self._pages[AUTH_COOKIE] = self.ed_cookie
        self._pages[AUTH_OAUTH] = oauth
        v.addWidget(holder)

        bb = QHBoxLayout()
        b_ok = QPushButton("💾 保存")
        b_ok.clicked.connect(self._accept)
        b_no = QPushButton("取消")
        b_no.setObjectName("GhostBtn")
        b_no.clicked.connect(self.reject)
        bb.addStretch(1)
        bb.addWidget(b_ok)
        bb.addWidget(b_no)
        v.addLayout(bb)

        self.cb_platform.currentIndexChanged.connect(self._reload_auth)
        if account:
            self._load_account(account)
        self._reload_auth()

    def _reload_auth(self, *_):
        from video_text_tools.publish import supported_auth
        from video_text_tools.publish.models import AUTH_TYPE_LABELS
        plat = self.cb_platform.currentData()
        keep = self.cb_auth.currentData()
        self.cb_auth.clear()
        for a in (supported_auth(plat) or ["cookie"]):
            self.cb_auth.addItem(AUTH_TYPE_LABELS.get(a, a), a)
        if keep:
            i = self.cb_auth.findData(keep)
            if i >= 0:
                self.cb_auth.setCurrentIndex(i)
        self._switch_page()

    def _switch_page(self, *_):
        from video_text_tools.publish.models import AUTH_COOKIE
        auth = self.cb_auth.currentData() or AUTH_COOKIE
        for k, w in self._pages.items():
            w.setVisible(k == auth)

    def _load_account(self, acct):
        i = self.cb_platform.findData(acct.platform)
        if i >= 0:
            self.cb_platform.setCurrentIndex(i)
        self.cb_auth.blockSignals(True)
        j = self.cb_auth.findData(acct.auth_type)
        if j >= 0:
            self.cb_auth.setCurrentIndex(j)
        self.cb_auth.blockSignals(False)
        self.ed_label.setText(acct.label or "")
        self._switch_page()
        secret = acct.secret or {}
        if acct.auth_type == "cookie":
            self.ed_cookie.setPlainText(secret.get("cookie", ""))
        else:
            self.ed_at.setText(secret.get("access_token", ""))
            self.ed_rt.setText(secret.get("refresh_token", ""))

    def _accept(self):
        label = self.ed_label.text().strip()
        if not label:
            QMessageBox.information(self, "提示", "请填写账号别名")
            return
        from video_text_tools.publish.models import Account, AUTH_COOKIE
        auth = self.cb_auth.currentData() or AUTH_COOKIE
        if auth == AUTH_COOKIE:
            cookie = self.ed_cookie.toPlainText().strip()
            if not cookie:
                QMessageBox.information(self, "提示", "请粘贴 Cookie")
                return
            secret = {"cookie": cookie}
        else:
            at = self.ed_at.text().strip()
            if not at:
                QMessageBox.information(self, "提示", "请填写 Access Token")
                return
            secret = {"access_token": at, "refresh_token": self.ed_rt.text().strip()}
        self._result = Account(id=(self._edit.id if self._edit else ""),
                               platform=self.cb_platform.currentData(),
                               label=label, auth_type=auth, secret=secret,
                               extra=(self._edit.extra if self._edit else {}))
        self.accept()

    def result_account(self):
        return getattr(self, "_result", None)


# ====================================================================
# 发布面板
# ====================================================================
class PublishPanel(BasePanel):
    """一键发布：选成品视频 + 勾选平台账号 → 矩阵分发 → 结果落库。"""

    def _build(self, outer):
        outer.addWidget(page_header(
            "一键发布",
            "选择成品视频与平台账号，一键分发到抖音 / 快手 / 小红书 / 视频号",
            icon="🚀"))

        self.files = FileListWidget("待发布视频")
        outer.addWidget(self.files)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.ed_title = QLineEdit()
        self.ed_title.setPlaceholderText("标题（多文件时按文件名自动补序号区分）")
        self.ed_desc = QPlainTextEdit()
        self.ed_desc.setFixedHeight(56)
        self.ed_desc.setPlaceholderText("简介/正文描述")
        self.ed_tags = QLineEdit()
        self.ed_tags.setPlaceholderText("话题标签，逗号或空格分隔，如：好物推荐,抖音小助手")
        self.cb_vis = QComboBox()
        self.cb_vis.addItem("公开", "public")
        self.cb_vis.addItem("仅自己可见", "private")
        self.ed_cover = QLineEdit()
        b_cover = QPushButton("浏览…")
        b_cover.setObjectName("GhostBtn")
        b_cover.clicked.connect(self._pick_cover)
        rc = QHBoxLayout()
        rc.addWidget(self.ed_cover, 1)
        rc.addWidget(b_cover)
        form.addRow("标题：", self.ed_title)
        form.addRow("简介：", self.ed_desc)
        form.addRow("标签：", self.ed_tags)
        form.addRow("可见性：", self.cb_vis)
        form.addRow("封面：", rc)
        outer.addLayout(form)

        # ---- 账号区 ----
        head = QHBoxLayout()
        head.addWidget(QLabel("<b>发布账号</b>（勾选即参与本次发布）"))
        head.addStretch(1)
        for text, slot in (("＋ 添加", self._add_account), ("✎ 编辑", self._edit_account),
                           ("🗑 删除", self._del_account), ("🔄 刷新", self._load_accounts),
                           ("🔌 检测登录", self._check_login)):
            b = QPushButton(text)
            b.setObjectName("GhostBtn")
            b.clicked.connect(slot)
            head.addWidget(b)
        outer.addLayout(head)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["选", "平台", "别名", "鉴权", "登录态"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setFixedHeight(150)
        outer.addWidget(self.table)

        self.make_log_box(outer, height=140)
        self._load_accounts()
        self.make_run_row(outer, "▶ 一键发布")

    # ------------------------------------------------------------------
    # 账号表
    # ------------------------------------------------------------------
    def _load_accounts(self):
        self.table.setRowCount(0)
        try:
            from video_text_tools.publish import accounts as acc_api
            accts = acc_api.list_accounts()
        except Exception as e:
            self._append_log(f"⚠ 读取账号失败：{e}")
            return
        from video_text_tools.publish.models import platform_label, AUTH_TYPE_LABELS
        for a in accts:
            row = self.table.rowCount()
            self.table.insertRow(row)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, a)
            self.table.setItem(row, 0, chk)
            self.table.setItem(row, 1, QTableWidgetItem(platform_label(a.platform)))
            self.table.setItem(row, 2, QTableWidgetItem(a.label))
            self.table.setItem(row, 3, QTableWidgetItem(
                AUTH_TYPE_LABELS.get(a.auth_type, a.auth_type)))
            self.table.setItem(row, 4, QTableWidgetItem("—"))

    def _row_account(self, row):
        it = self.table.item(row, 0)
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _current_account(self):
        row = self.table.currentRow()
        return self._row_account(row) if row >= 0 else None

    def _add_account(self):
        dlg = AccountDialog(self)
        if dlg.exec():
            self._save(dlg.result_account())

    def _edit_account(self):
        acct = self._current_account()
        if acct is None:
            QMessageBox.information(self, "提示", "请先在表里选中一个账号")
            return
        dlg = AccountDialog(self, account=acct)
        if dlg.exec():
            self._save(dlg.result_account())

    def _save(self, acct):
        if acct is None:
            return
        try:
            from video_text_tools.publish import accounts as acc_api
            acc_api.save_account(acct)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self._append_log(f"✓ 已保存账号「{acct.label}」（凭证已按姓名加密落盘）")
        self._load_accounts()

    def _del_account(self):
        acct = self._current_account()
        if acct is None:
            QMessageBox.information(self, "提示", "请先在表里选中一个账号")
            return
        if QMessageBox.question(self, "删除账号",
                                f"确定删除「{acct.label}」？其凭证会一并清除。") \
                != QMessageBox.StandardButton.Yes:
            return
        from video_text_tools.publish import accounts as acc_api
        acc_api.delete_account(acct.id)
        self._append_log(f"🗑 已删除账号「{acct.label}」")
        self._load_accounts()

    def _pick_cover(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择封面图", "",
                                           "图片 (*.png *.jpg *.jpeg *.webp)")
        if p:
            self.ed_cover.setText(p)

    # ---- 检测登录（后台线程，逐账号跑 check_auth）----
    def _check_login(self):
        accts = self._checked_accounts() or self._all_accounts()
        if not accts:
            QMessageBox.information(self, "提示", "没有可检测的账号")
            return
        self.b_run.setEnabled(False)
        rows = [self._row_of(a) for a in accts]

        def fn(log, progress, should_stop):
            from video_text_tools.publish import get_adapter
            out = []
            for a in accts:
                ad = get_adapter(a.platform, a.auth_type)
                if ad is None:
                    out.append((a.id, False, "无适配器"))
                    continue
                ok, msg = ad.check_auth(a, log=log)
                out.append((a.id, ok, msg))
                progress(len(out), len(accts), a.label)
            return out

        self._ck_worker = ToolWorker(fn, self)
        self._ck_worker.log.connect(self._append_log)
        self._ck_worker.progress.connect(self._on_progress)
        self._ck_worker.done.connect(lambda res, _r=rows: self._on_check_done(res))
        self._ck_worker.start()

    def _on_check_done(self, res):
        self._ck_worker = None
        self.b_run.setEnabled(True)
        self.bar.setValue(0)
        if isinstance(res, Exception):
            QMessageBox.warning(self, "检测失败", str(res))
            return
        by_id = {aid: (ok, msg) for aid, ok, msg in res}
        for row in range(self.table.rowCount()):
            a = self._row_account(row)
            if a and a.id in by_id:
                ok, msg = by_id[a.id]
                self.table.item(row, 4).setText(("✅ " if ok else "❌ ") + msg)

    def _row_of(self, acct):
        for row in range(self.table.rowCount()):
            a = self._row_account(row)
            if a and a.id == acct.id:
                return row
        return -1

    def _all_accounts(self):
        return [self._row_account(r) for r in range(self.table.rowCount())
                if self._row_account(r) is not None]

    def _checked_accounts(self):
        out = []
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked:
                a = self._row_account(row)
                if a:
                    out.append(a)
        return out

    # ------------------------------------------------------------------
    # 主任务：视频 × 账号 矩阵
    # ------------------------------------------------------------------
    def _build_items(self, paths):
        from video_text_tools.publish.models import PublishItem
        base = self.ed_title.text().strip()
        desc = self.ed_desc.toPlainText().strip()
        tags = _split_tags(self.ed_tags.text())
        cover = self.ed_cover.text().strip()
        vis = self.cb_vis.currentData() or "public"
        many = len(paths) > 1
        items = []
        for p in paths:
            stem = Path(p).stem
            if base:
                title = f"{base}-{stem}" if many else base
            else:
                title = stem
            items.append(PublishItem(video_path=p, title=title, desc=desc,
                                     tags=list(tags), cover_path=cover,
                                     visibility=vis))
        return items

    def _task(self):
        paths = self.files.paths()
        if not paths:
            QMessageBox.information(self, "提示", "请先添加要发布的视频")
            return None
        accts = self._checked_accounts()
        if not accts:
            QMessageBox.information(self, "提示", "请至少勾选一个发布账号")
            return None
        if QMessageBox.question(
                self, "确认发布",
                f"将真实发布 {len(paths)} 条视频到 {len(accts)} 个平台账号"
                f"（共 {len(paths) * len(accts)} 次），确认执行？") \
                != QMessageBox.StandardButton.Yes:
            return None
        items = self._build_items(paths)

        def fn(log, progress, should_stop):
            from video_text_tools.publish import publish_batch
            from store import publish_store
            recs = publish_batch(items, accts, log=log, progress=progress,
                                 should_stop=should_stop)
            try:
                publish_store.record_all(recs)     # 历史落库（DB 线程安全）
            except Exception as e:
                log(f"⚠ 发布历史入库失败（不影响发布结果）：{e}")
            return recs
        return fn

    def on_result(self, recs):
        if not isinstance(recs, list):
            return
        ok = sum(1 for r in recs if r.ok)
        self._append_log(f"🎉 本轮结束：成功 {ok} / 共 {len(recs)}"
                         + ("（含失败，详见上方日志与发布历史）" if ok < len(recs) else ""))
