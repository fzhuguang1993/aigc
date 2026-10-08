"""
gui/dialogs_index_settings.py —— 每盘符索引设置（从唤出搜索面板 ⚙ / 悬浮球 / 设置页进入）

为什么单独一个对话框、而不是把东西全塞进设置页那张卡：设置页的「本地文件搜索」
卡管的是**全局口径**（开关、范围、正文目录、重建），而这里管的是**按盘**——
勾选哪几个盘进索引、每盘各自最近更新到什么时候、单独立即重扫某个盘。两件事的
刷新节奏与交互完全不同（这里要点完不卡界面、时间戳每几秒自己动），拆开更清楚。

三个关键设计都有原因，别当理所当然：

1. **立即重扫走后台异步，绝不在点按钮的线程上扫盘**。全盘一趟实测几十秒到
   几百秒；直接在 UI 线程调 scan_once 就是"点一下整个软件假死"。这里只把请求
   塞进 file_watcher 的定向队列（reindex_roots），后台线程下一拍串行消费。
2. **每盘时间戳读 roots 表（root_status()），不 COUNT files 表**。95 万行冷缓存
   COUNT 要 4.6 秒，而这个对话框每秒都要刷状态行——同 status() 不 COUNT 的纪律。
3. **"全盘"与"指定盘"用同一份 custom_roots 事实源**：勾满所有盘＝写空列表
   （回到 local_drives() 全盘口径），部分勾选＝写选中的盘。不另立第二套开关。
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QFrame, QCheckBox,
                               QWidget)

from core import fileindex
from gui.theme import tokenize


def _fmt_age(ts):
    """把墙上时钟戳翻成"几分钟前 / 日期"：0 或空＝还没索引过。"""
    if not ts:
        return "未索引"
    d = max(0, int(time.time()) - int(ts))
    if d < 60:
        return "刚刚更新"
    if d < 3600:
        return "%d 分钟前" % (d // 60)
    if d < 86400:
        return "%d 小时前" % (d // 3600)
    if d < 7 * 86400:
        return "%d 天前" % (d // 86400)
    return time.strftime("上次 %Y-%m-%d", time.localtime(int(ts)))


def _fmt_n(v):
    try:
        return "{:,}".format(int(v))
    except (TypeError, ValueError):
        return "-"


class _DriveRow(QWidget):
    """一行一个盘：勾选框（是否纳入索引） + 最近更新时间 / 文件数 + 立即重扫此盘。"""

    def __init__(self, path, checked, on_reindex, on_toggle, parent=None):
        super().__init__(parent)
        self.path = str(path)
        self._on_reindex = on_reindex
        self._on_toggle = on_toggle
        h = QHBoxLayout(self)
        h.setContentsMargins(2, 3, 2, 3)
        h.setSpacing(10)
        self.ck = QCheckBox(self.path)
        self.ck.setChecked(bool(checked))
        self.ck.setMinimumWidth(120)
        self.ck.toggled.connect(lambda _s: self._toggled())
        h.addWidget(self.ck)
        self.lbl = QLabel("未索引")
        self.lbl.setStyleSheet(tokenize(
            "color:#8F959E; font-size:12px; background:transparent;"))
        h.addWidget(self.lbl, 1)
        self.btn = QPushButton("🔄 重扫此盘")
        self.btn.setObjectName("GhostBtn")
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.setToolTip("只重新扫描这个磁盘的文件名索引（后台异步，不卡界面）")
        self.btn.clicked.connect(lambda _c=False: self._reindex())
        h.addWidget(self.btn)

    def _toggled(self):
        cb = self._on_toggle
        if callable(cb):
            cb(self.path, self.ck.isChecked())

    def _reindex(self):
        cb = self._on_reindex
        if callable(cb):
            cb([self.path])

    def set_status(self, info):
        """info = root_status() 里对应这个盘的一项，或 None（还没索引）。"""
        if not info or not info.get("indexed"):
            self.lbl.setText("未索引")
            return
        self.lbl.setText("%s · %s 个文件 · %s" % (
            self.path, _fmt_n(info.get("files")), _fmt_age(info.get("last_scan"))))

    def set_reindex_enabled(self, on):
        self.btn.setEnabled(bool(on))


class IndexSettingsDialog(QDialog):
    """盘符级索引管理：勾选纳入索引的盘、看每盘最近更新时间、立即（异步）重扫。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🗂 本地文件索引设置")
        self.resize(520, 460)
        self._rows = {}                # path -> _DriveRow
        self._last_busy = False
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)

        # ---------- 顶部：总开关 ----------
        self.ck_enabled = QCheckBox("启用本地文件搜索（关闭后后台不再扫描，面板只搜工具）")
        self.ck_enabled.setChecked(fileindex.enabled())
        self.ck_enabled.toggled.connect(self._set_enabled)
        v.addWidget(self.ck_enabled)

        tip = QLabel("勾选要纳入文件名索引的磁盘，右侧看每盘各自最近更新到什么时候；"
                     "改完点下方「立即重建所选盘」。全部扫描都在后台跑，不卡界面。")
        tip.setWordWrap(True)
        tip.setStyleSheet(tokenize(
            "color:#646A73; font-size:12px; background:transparent;"))
        v.addWidget(tip)

        # ---------- 中部：盘符行（可滚动） ----------
        box = QFrame()
        box.setObjectName("Card")
        box.setStyleSheet(tokenize(
            "QFrame#Card { background:#FAFBFC; border:1px solid #DEE0E3;"
            " border-radius:8px; }"))
        bv = QVBoxLayout(box)
        bv.setContentsMargins(10, 8, 10, 8)
        bv.setSpacing(0)
        area = QScrollArea()
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidgetResizable(True)
        area.setStyleSheet(tokenize(
            "QScrollArea { background:transparent; border:none; }"
            "QScrollArea > QWidget > QWidget { background:transparent; }"))
        host = QWidget()
        self._rows_lay = QVBoxLayout(host)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(2)
        self._rows_lay.addStretch(1)
        area.setWidget(host)
        bv.addWidget(area)
        v.addWidget(box, 1)

        # ---------- 底部：动作行 ----------
        ar = QHBoxLayout()
        ar.setSpacing(10)
        self.btn_reindex = QPushButton("🔁 立即重建所选盘")
        self.btn_reindex.setObjectName("GhostBtn")
        self.btn_reindex.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reindex.clicked.connect(self._reindex_selected)
        ar.addWidget(self.btn_reindex)
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(tokenize(
            "color:#8F959E; font-size:12px; background:transparent;"))
        ar.addWidget(self.lbl_status, 1)
        b_close = QPushButton("关闭")
        b_close.setObjectName("GhostBtn")
        b_close.clicked.connect(self.accept)
        ar.addWidget(b_close)
        v.addLayout(ar)

        self._build_rows()
        self._refresh_status()

        # 盘在后台扫描时，这里的"最近更新时间/正在扫描"要自己动：可见期间每 2 秒刷。
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._refresh_status)
        self._timer.start()

    # ---------- 数据装配 ----------
    def _all_drives(self):
        """候选盘：固定盘清单 + 已存 custom_roots（可能含手动加的非固定盘）。去重排序。"""
        seen, out = set(), []
        for p in list(fileindex.local_drives()) + fileindex.custom_roots():
            key = str(p)
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def _selected(self):
        """当前纳入索引的盘：custom_roots 空＝全盘（用 local_drives 口径）。"""
        cur = fileindex.custom_roots()
        return set(cur) if cur else set(fileindex.local_drives())

    def _build_rows(self):
        sel = self._selected()
        for p in self._all_drives():
            row = _DriveRow(p, p in sel, self._reindex_roots, self._on_toggle)
            self._rows[p] = row
            self._rows_lay.insertWidget(self._rows_lay.count() - 1, row)

    # ---------- 交互 ----------
    def _set_enabled(self, on):
        fileindex.set_enabled(on)
        self._apply_enabled_ui()
        if on:
            from workers import file_watcher
            file_watcher.start()

    def _apply_enabled_ui(self):
        on = fileindex.enabled()
        for row in self._rows.values():
            row.setEnabled(on)
            row.set_reindex_enabled(on)
        self.btn_reindex.setEnabled(on)

    def _collect_checked(self):
        return [p for p, row in self._rows.items() if row.ck.isChecked()]

    def _on_toggle(self, path, checked):
        """勾选变化即写回 custom_roots：勾满所有盘＝写空（回全盘口径）。"""
        checked_set = set(self._collect_checked())
        all_drives = set(self._all_drives())
        if checked_set >= all_drives or not checked_set:
            fileindex.set_custom_roots([])     # 全不勾或全勾：回全盘口径
        else:
            fileindex.set_custom_roots(sorted(checked_set))

    def _reindex_roots(self, roots):
        """异步发起定向重扫：不阻塞界面；返回后台是否接手（False＝总开关关了）。"""
        from workers import file_watcher
        ok = file_watcher.reindex_roots(roots)
        self.lbl_status.setText(
            "已提交重扫，后台进行中…" if ok else "请先启用本地文件搜索")
        self._refresh_status()
        return ok

    def _reindex_selected(self):
        roots = self._collect_checked() or fileindex.local_drives()
        self._reindex_roots(roots)

    def _refresh_status(self):
        st = {r["root"]: r for r in fileindex.root_status()}
        for p, row in self._rows.items():
            row.set_status(st.get(p))
        from workers import file_watcher
        busy = file_watcher.busy() or file_watcher.has_pending()
        if busy:
            self.lbl_status.setText("后台正在扫描…")
            self._last_busy = True
        elif self._last_busy:
            self._last_busy = False
            self.lbl_status.setText("索引已更新")
        self._apply_enabled_ui()

    def done(self, r):
        # 关窗即停刷新定时器，别让它在背后继续查库
        try:
            self._timer.stop()
        except Exception:
            pass
        super().done(r)
