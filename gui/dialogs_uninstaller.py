"""
gui/dialogs_uninstaller.py —— 工具中心「软件卸载清理」面板 + 残留确认弹窗

功能层全在 video_text_tools/software_manager.py（纯 winreg/ctypes，无 Qt）；
本层只做界面与线程调度：
  - 列表：枚举本机软件，即时搜索过滤；
  - 卸载：逐条二次确认后，后台线程跑 run_uninstall（不卡 UI），完成自动扫残留；
  - 清理：残留扫描只产候选，弹 ResidueDialog 让用户逐条勾选确认后才删。

安全口径：全程无「一键全卸 / 全删」，卸载按钮对受保护项置灰；耗时/高危动作都
放 ToolWorker(QThread)，UI 只收信号。非 Windows 由 factory 挡在门外（见 tool_panels）。
"""
from PySide6.QtCore import Qt, QFileInfo, QSize, QItemSelectionModel
from PySide6.QtGui import QIcon, QFont
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QDialog,
                               QDialogButtonBox, QMessageBox, QSizePolicy,
                               QFileIconProvider, QTextBrowser)

from gui.header import page_header
from gui.kit import KitTable, KitButton
from gui.menus import StyledMenu
from gui.tool_panels import BasePanel, ToolWorker
from gui.ui_kit import COLORS
from gui.window_frame import apply_rounded
from utils.desktop_utils import open_path
from video_text_tools import software_manager as sm


class UninstallerPanel(BasePanel):
    """软件列表 + 逐条卸载 / 清理的主面板。"""

    _COLS = ["软件名称", "版本", "发布者", "大小", "安装日期"]

    #: 分组渲染顺序与标题（category -> 中文名）
    _GROUPS = [("app", "常用软件"),
               ("system", "Windows 系统组件 / 自带"),
               ("driver", "驱动程序")]

    #: 列标题 -> 记录字段（供点表头排序用）
    _COL_KEY = {"软件名称": "name", "版本": "version", "发布者": "publisher",
                "大小": "estimated_size", "安装日期": "install_date"}

    def _build(self, outer):
        self._icon_cache = {}       # 图标按文件路径缓存，避免每次刷新重建
        # 默认按安装日期降序（最近装的在前）；分组默认：主组展开，系统/驱动收起
        self._sort_key = "install_date"
        self._sort_desc = True
        self._expanded = {"app": True, "system": False, "driver": False}
        self._rows = []             # 表格行 -> 行描述（group / rec），供选中映射与重建
        outer.addWidget(page_header(
            "软件卸载清理",
            "检测本机已装软件 · 卸载前逐条确认 · 卸载后扫描注册表/文件残留（勾选后才删）",
            icon="🧹"))

        # ---- 搜索 + 刷新 ----
        row = QHBoxLayout()
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("按名称 / 发布者过滤…")
        self.ed_search.textChanged.connect(self._refresh_table)
        row.addWidget(self.ed_search, 1)
        self.lbl_count = QLabel("未加载")
        self.lbl_count.setObjectName("PageTip")
        row.addWidget(self.lbl_count)
        b_refresh = KitButton("🔄 刷新列表", kind="ghost", obj_name="GhostBtn", explain=False)
        b_refresh.clicked.connect(self.reload)
        row.addWidget(b_refresh)
        outer.addLayout(row)

        # ---- 软件表 ----
        self.table = KitTable(0, len(self._COLS), self._COLS,
                              checkbox=False, select="rows", multi=True)
        self.table.setIconSize(QSize(22, 22))
        self.table.horizontalHeader().setSectionResizeMode(
            0, self.table.horizontalHeader().ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        # 点表头排序（不用 setSortingEnabled，避免打散分组行）：手动接 sectionClicked
        hh = self.table.horizontalHeader()
        hh.setSortIndicatorShown(True)
        hh.sectionClicked.connect(self._on_header_clicked)
        hh.setSortIndicator(self._COLS.index("安装日期"), Qt.SortOrder.DescendingOrder)
        # 点分组标题行：展开 / 收起该组
        self.table.cellClicked.connect(self._on_cell_clicked)
        outer.addWidget(self.table, 1)

        # ---- 操作按钮 ----
        act = QHBoxLayout()
        self.b_batch = KitButton("🤖 批量自动卸载清理", kind="primary", explain=False)
        self.b_batch.clicked.connect(self._start_batch)
        self.b_official = KitButton("卸载（官方向导）", kind="ghost", obj_name="GhostBtn", explain=False)
        self.b_official.clicked.connect(lambda: self._start_uninstall("gui"))
        self.b_silent = KitButton("卸载（静默）", kind="ghost", obj_name="GhostBtn", explain=False)
        self.b_silent.clicked.connect(lambda: self._start_uninstall("silent"))
        self.b_clean = KitButton("扫描并清理残留", kind="ghost", obj_name="GhostBtn", explain=False)
        self.b_clean.clicked.connect(self._start_scan)
        self.b_openloc = QPushButton("📂 打开安装目录")
        self.b_openloc.setObjectName("GhostBtn")
        self.b_openloc.clicked.connect(self._open_location)
        for b in (self.b_batch, self.b_official, self.b_silent, self.b_clean, self.b_openloc):
            b.setMinimumHeight(34)
            act.addWidget(b)
        act.addStretch(1)
        outer.addLayout(act)

        self.make_log_box(outer, 150)

        self._all = []          # 全量记录
        self._shown = []        # 当前过滤后的全量命中（跨分组，仅用于计数与搜索）
        self.reload()

    # ---------------- 列表加载 / 过滤 ----------------
    def reload(self):
        self._append_log("🔍 正在扫描已安装软件…")
        self._all = sm.list_installed()
        self._refresh_table()
        self._append_log(f"共发现 {len(self._all)} 个可卸载软件。")

    def _refresh_table(self, *_):
        q = self.ed_search.text().strip().lower()
        hit = [r for r in self._all
               if not q or q in r["name"].lower() or q in r["publisher"].lower()]
        self._shown = hit
        t = self.table
        t.setRowCount(0)              # 先清空：下面逐行 insertRow，避免旧 item 被别的表拥有
        t.clearSpans()                # 清掉上一轮残留的跨列合并，防止旧 span 吞掉新行单元格
        self._rows = []
        visible = 0
        for gkey, glabel in self._GROUPS:
            group = self._sort_records(
                [r for r in hit if r.get("category", "app") == gkey])
            if not group:
                continue
            # 搜索时（q 非空）命中组自动展开；否则跟随手动展开态
            expanded = bool(q) or self._expanded.get(gkey, False)
            self._append_group_header(t, gkey, glabel, len(group), expanded)
            if not expanded:
                continue
            for r in group:
                self._append_software_row(t, r)
                visible += 1
        self.lbl_count.setText(f"当前显示 {visible} / 共 {len(self._all)} 个")
        self._sync_buttons()

    def _append_group_header(self, t, gkey, glabel, count, expanded):
        """插一行跨列分组标题（可点击展开/收起，不可被当作软件选中）。"""
        row = t.rowCount()
        t.insertRow(row)
        mark = "▾" if expanded else "▸"
        it = _mkitem(f"{mark} {glabel}（{count}）")
        f = QFont()
        f.setBold(True)
        it.setFont(f)
        it.setForeground(QColor_(COLORS["text"]))
        it.setBackground(QColor_(COLORS["bg_field"]))
        it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        it.setData(Qt.ItemDataRole.UserRole, ("group", gkey))
        t.setItem(row, 0, it)
        # 横向跨列合并：setSpan(row, col, rowspan, colspan)——rowspan=1、colspan=全部列
        t.setSpan(row, 0, 1, len(self._COLS))
        self._rows.append({"type": "group", "key": gkey, "count": count})

    def _append_software_row(self, t, r):
        """插一行软件记录（第 0 列带图标 + UserRole 挂记录本身）。"""
        row = t.rowCount()
        t.insertRow(row)
        vals = [r["name"], r["version"], r["publisher"],
                sm.human_size(r["estimated_size"]), sm.human_date(r["install_date"])]
        for c, text in enumerate(vals):
            it = _mkitem(text)          # 每轮都建新项：复用旧 item 会踩 Qt「已被其它表拥有」告警
            it.setToolTip(text)
            if c == 0:
                it.setData(Qt.ItemDataRole.UserRole, ("rec", r))
                ic = self._icon_for(r)
                if not ic.isNull():
                    it.setIcon(ic)
            t.setItem(row, c, it)
        if r.get("protected"):
            t.item(row, 0).setForeground(QColor_(COLORS["danger"]))
            t.item(row, 0).setToolTip(f"{r['name']}（本程序自身，受保护禁止卸载）")
        self._rows.append({"type": "rec", "record": r})

    def _sort_records(self, recs):
        """对一个分组内的记录排序；方向只作用于组内，不改分组顺序与收起态。"""
        key, desc = self._sort_key, self._sort_desc

        def cmpkey(r):
            if key == "estimated_size":
                return int(r.get("estimated_size") or 0)
            return str(r.get(key) or "").lower()

        if key == "install_date":
            # 有日期的按方向排；无日期的稳定垫到末尾（不随方向翻转）
            has = [r for r in recs if str(r.get("install_date") or "")]
            none = [r for r in recs if not str(r.get("install_date") or "")]
            has.sort(key=cmpkey, reverse=desc)
            none.sort(key=lambda r: str(r.get("name") or "").lower())
            return has + none
        return sorted(recs, key=cmpkey, reverse=desc)

    def _on_header_clicked(self, col):
        """点表头：同列翻转升降序，异列切新列（默认降序）；更新排序箭头并重绘。"""
        if col < 0 or col >= len(self._COLS):
            return
        key = self._COL_KEY.get(self._COLS[col])
        if not key:
            return
        if key == self._sort_key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key = key
            self._sort_desc = True
        order = (Qt.SortOrder.DescendingOrder if self._sort_desc
                 else Qt.SortOrder.AscendingOrder)
        self.table.horizontalHeader().setSortIndicator(col, order)
        self._refresh_table()

    def _on_cell_clicked(self, row, _col):
        """点分组标题行：翻转该组展开态并重绘（软件行不拦截，选择照常）。"""
        if 0 <= row < len(self._rows):
            d = self._rows[row]
            if d["type"] == "group":
                self._expanded[d["key"]] = not self._expanded.get(d["key"], False)
                self._refresh_table()

    def _selected(self):
        """从当前选中行取记录（经 self._rows 映射，避开分组行导致的行号与记录错位）。"""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        if 0 <= row < len(self._rows):
            d = self._rows[row]
            if d["type"] == "rec":
                return d["record"]
        return None

    def _selected_records(self):
        """返回当前选中的所有记录（多选）；分组标题行不可选中，天然被跳过。"""
        out = []
        for idx in self.table.selectionModel().selectedRows():
            row = idx.row()
            if 0 <= row < len(self._rows):
                d = self._rows[row]
                if d["type"] == "rec":
                    out.append(d["record"])
        return out

    def _select_all_silent(self):
        """右键辅助：在当前行里选中所有「非受保护且可静默卸载」的软件。"""
        sel = self.table.selectionModel()
        sel.clearSelection()
        n = 0
        for row, d in enumerate(self._rows):
            if d["type"] != "rec" or d["record"].get("protected"):
                continue
            if not sm.silent_capable(d["record"]):
                continue
            idx = self.table.model().index(row, 0)
            sel.select(idx, QItemSelectionModel.SelectionFlag.Select
                       | QItemSelectionModel.SelectionFlag.Rows)
            n += 1
        self._sync_buttons()
        self._append_log(f"已选中 {n} 个可静默卸载的软件（其余需手动卸载）。")

    def _clear_selection(self):
        self.table.clearSelection()

    def _icon_for(self, record):
        """从软件自身文件取图标（.ico/.png 直接加载，.exe/.dll 走系统图标）；缓存。"""
        path = sm.icon_source_path(record)
        if not path:
            return QIcon()
        hit = self._icon_cache.get(path)
        if hit is not None:
            return hit
        low = path.lower()
        if low.endswith((".ico", ".png", ".svg", ".jpg", ".jpeg", ".bmp")):
            ic = QIcon(path)
        else:
            ic = QFileIconProvider().icon(QFileInfo(path))
        self._icon_cache[path] = ic
        return ic

    def _sync_buttons(self):
        r = self._selected()
        has = r is not None
        self.b_silent.setEnabled(has and not r["protected"])
        self.b_official.setEnabled(has and not r["protected"])
        self.b_clean.setEnabled(has)
        self.b_openloc.setEnabled(has and bool(r["install_location"]))

    # ---------------- 右键菜单 ----------------
    def _row_menu(self, pos):
        m = StyledMenu(self)
        # 批量选择辅助（不依赖是否已选中某行）
        m.addAction("选择所有可静默卸载项", self._select_all_silent)
        m.addAction("清除选择", self._clear_selection)
        r = self._selected()
        if r is not None:
            m.addSeparator()
            a1 = m.addAction("卸载（官方向导）", lambda: self._do_uninstall(r, "gui"))
            m.addAction("卸载（静默）", lambda: self._do_uninstall(r, "silent"))
            m.addSeparator()
            m.addAction("扫描并清理残留", lambda: self._scan_residue(r))
            if r["protected"]:
                a1.setEnabled(False)
        m.exec(self.table.mapToGlobal(pos))

    def _open_location(self):
        r = self._selected()
        if r and r["install_location"]:
            open_path(r["install_location"])

    # ---------------- 卸载流程 ----------------
    def _start_uninstall(self, mode):
        r = self._selected()
        if r is None:
            QMessageBox.information(self, "提示", "请先在列表里选中一个软件")
            return
        self._do_uninstall(r, mode)

    def _do_uninstall(self, record, mode):
        if record["protected"]:
            QMessageBox.warning(self, "受保护", "本程序自身受保护，禁止卸载。")
            return
        how = "官方卸载向导" if mode == "gui" else "静默卸载"
        tip = (f"将卸载：{record['name']}  {record['version']}\n\n"
               f"方式：{how}\n\n"
               "卸载是难以撤销的操作，确认继续？")
        if QMessageBox.question(self, "确认卸载", tip,
                                QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) != QMessageBox.Yes:
            return
        self._set_busy(True)
        self._append_log(f"\n=== 开始卸载：{record['name']}（{how}）===")

        def fn(log, progress, should_stop):
            progress(0, 1, record["name"])
            res = sm.run_uninstall(record, mode, log=log)
            progress(1, 1, "完成")
            res["record"] = record
            return res
        w = ToolWorker(fn, self)
        w.log.connect(self._append_log)
        w.done.connect(self._on_uninstall_done)
        w.start()

    def _on_uninstall_done(self, res):
        self._set_busy(False)
        if isinstance(res, Exception):
            self._append_log(f"❌ 卸载异常：{res}")
            QMessageBox.warning(self, "卸载失败", str(res))
            return
        if not res.get("ok"):
            self._append_log(f"❌ {res.get('msg', '卸载未完成')}")
            QMessageBox.warning(self, "卸载未完成", res.get("msg", "卸载未完成"))
            return
        self._append_log(f"✅ {res.get('msg', '卸载完成')}")
        record = res["record"]
        self.reload()
        # 卸载成功后自动扫残留并弹确认窗
        self._scan_residue(record)

    # ---------------- 批量自动卸载清理 ----------------
    def _start_batch(self):
        recs = self._selected_records()
        if not recs:
            QMessageBox.information(
                self, "提示",
                "请先在列表里选中要卸载的软件（按住 Ctrl/Shift 多选，"
                "或右键「选择所有可静默卸载项」）。")
            return
        dlg = BatchConfirmDialog(self, recs)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self._append_log("已取消批量卸载，未执行任何操作。")
            return
        targets = dlg.chosen_records()
        if not targets:
            QMessageBox.information(self, "提示", "没有可自动卸载的项（均为受保护或无静默口）。")
            return
        self._set_busy(True)
        self._append_log(
            f"\n=== 批量自动卸载清理：{len(targets)} 个（保守模式，仅删高置信残留）===")

        def fn(log, progress, should_stop):
            return sm.auto_uninstall_and_clean(
                targets, log=log, progress=progress, should_stop=should_stop)
        w = ToolWorker(fn, self)
        w.log.connect(self._append_log)
        w.done.connect(self._on_batch_done)
        w.start()

    def _on_batch_done(self, res):
        self._set_busy(False)
        self.reload()
        if isinstance(res, Exception):
            self._append_log(f"❌ 批量卸载异常：{res}")
            QMessageBox.warning(self, "批量失败", str(res))
            return
        BatchResultDialog(self, res).exec()

    # ---------------- 残留扫描 / 清理 ----------------
    def _start_scan(self):
        r = self._selected()
        if r is None:
            QMessageBox.information(self, "提示", "请先选中一个软件")
            return
        self._scan_residue(r)

    def _scan_residue(self, record):
        self._set_busy(True)
        self._append_log(f"🧹 正在扫描「{record['name']}」的残留…")

        def fn(log, progress, should_stop):
            progress(0, 1, "扫描")
            items = sm.scan_residue(record)
            progress(1, 1, "完成")
            return {"items": items, "record": record}
        w = ToolWorker(fn, self)
        w.log.connect(self._append_log)
        w.done.connect(self._on_scan_done)
        w.start()

    def _on_scan_done(self, res):
        self._set_busy(False)
        if isinstance(res, Exception):
            self._append_log(f"❌ 扫描异常：{res}")
            QMessageBox.warning(self, "扫描失败", str(res))
            return
        items = res["items"]
        record = res["record"]
        if not items:
            self._append_log("未发现可清理的残留（干净）。")
            QMessageBox.information(self, "残留扫描", "未发现残留文件 / 注册表项。")
            return
        self._append_log(f"发现 {len(items)} 处候选残留，请在弹窗里勾选要删除的项。")
        dlg = ResidueDialog(self, record, items)
        chosen = dlg.run()
        if chosen is None:               # 取消
            self._append_log("已取消清理，未删除任何内容。")
            return
        if not chosen:
            self._append_log("未勾选任何残留项，未删除。")
            return
        self._do_remove(record, chosen)

    def _do_remove(self, record, items):
        self._set_busy(True)
        self._append_log(f"🗑 开始清理 {len(items)} 处残留…")

        def fn(log, progress, should_stop):
            total = len(items)
            for i in range(0, total, 1):
                progress(i, total, items[i].get("path", ""))
            res = sm.remove_residue(items, log=log)
            progress(total, total, "完成")
            return res
        w = ToolWorker(fn, self)
        w.log.connect(self._append_log)
        w.done.connect(self._on_remove_done)
        w.start()

    def _on_remove_done(self, res):
        self._set_busy(False)
        if isinstance(res, Exception):
            self._append_log(f"❌ 清理异常：{res}")
            return
        ok = sum(1 for r in res if r["ok"])
        fail = len(res) - ok
        self._append_log(f"清理完成：成功 {ok} · 失败/跳过 {fail}")
        if fail:
            QMessageBox.information(
                self, "部分未清理",
                f"有 {fail} 处未能删除（见日志）。若涉及系统级注册表项，"
                "可能需要以管理员身份重新运行本程序后再清理。")

    # ---------------- 忙碌态 ----------------
    def _set_busy(self, busy):
        for b in (self.b_batch, self.b_official, self.b_silent, self.b_clean):
            b.setEnabled(not busy)
        if not busy:
            self._sync_buttons()


def _mkitem(text):
    from PySide6.QtWidgets import QTableWidgetItem
    return QTableWidgetItem(text)


def QColor_(hexstr):
    from PySide6.QtGui import QColor
    return QColor(hexstr)


class ResidueDialog(QDialog):
    """残留预览确认窗：逐条勾选，默认全不勾，用户确认后才交给上层删除。"""

    def __init__(self, parent, record, items):
        super().__init__(parent)
        self._items = items
        self.setWindowTitle("清理残留 · " + record["name"])
        self.resize(720, 480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(10)

        warn = QLabel("⚠ 删除注册表项不可撤销；文件/目录优先移入回收站（可恢复）。"
                      "请只勾选确认属于该软件的内容。")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color:{COLORS['danger']}; font-size:13px; font-weight:600;"
                           f" background:transparent;")
        lay.addWidget(warn)

        self.table = KitTable(0, 4, ["类型", "路径", "大小", ""], checkbox=False,
                              select="rows", multi=True)
        self._checkboxes = []
        self._fill()
        lay.addWidget(self.table, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("删除勾选项")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        apply_rounded(self, title="清理残留", resizable=True,
                      show_min=False, show_max=True)

    def _fill(self):
        kind_cn = {"dir": "目录", "file": "文件", "reg": "注册表"}
        t = self.table
        t.setRowCount(len(self._items))
        for i, it in enumerate(self._items):
            t.setItem(i, 0, _mkitem(kind_cn.get(it["kind"], it["kind"])))
            t.setItem(i, 1, _mkitem(it["path"]))
            t.setItem(i, 2, _mkitem(sm.human_size(it.get("size", 0))))
            # 第 3 列放勾选框
            chk = t.make_check_item(False)
            t.setItem(i, 3, chk)
            self._checkboxes.append(chk)

    def run(self):
        """modal 弹出；返回选中的残留项 list，取消返回 None。"""
        if self.exec() != QDialog.DialogCode.Accepted:
            return None
        return [it for i, it in enumerate(self._items)
                if self._checkboxes[i].checkState() == Qt.CheckState.Checked]


def _esc(s):
    """HTML 转义（软件名/发布者可能含 & < > 等字符）。"""
    from html import escape
    return escape(str(s))


class BatchConfirmDialog(QDialog):
    """批量卸载前的汇总确认窗：列出可自动卸载 / 将跳过（无静默口）/ 受保护，红色风险提示。

    默认焦点在「取消」；没有可自动卸载项时「确认执行」置灰。"""

    def __init__(self, parent, records):
        super().__init__(parent)
        self._targets = [r for r in records
                         if not r.get("protected") and sm.silent_capable(r)]
        self._manual = [r for r in records
                        if not r.get("protected") and not sm.silent_capable(r)]
        self._protected = [r for r in records if r.get("protected")]
        self.setWindowTitle("批量自动卸载清理 · 确认")
        self.resize(640, 480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(10)

        title = QLabel(f"共选中 {len(records)} 个：可自动卸载 <b>{len(self._targets)}</b> 个 · "
                       f"需手动卸载 {len(self._manual)} 个 · 受保护跳过 {len(self._protected)} 个")
        title.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(title)

        warn = QLabel("⚠ 保守模式：卸载后仅自动删除「软件自身安装目录」与其「注册表卸载项」"
                      "（文件进回收站）；名称匹配到的公共目录 / 配置键一律只列出不删。"
                      "建议以管理员身份运行以减少逐条 UAC 弹窗。")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color:{COLORS['danger']}; font-size:13px; font-weight:600;"
                           f" background:transparent;")
        lay.addWidget(warn)

        html = [f"<h4>将自动静默卸载并清理（{len(self._targets)}）</h4>"]
        html.append("<ul>" + "".join(f"<li>{_esc(r['name'])}</li>"
                                      for r in self._targets) + "</ul>")
        if self._manual:
            html.append(f"<h4>将跳过（无静默卸载口，需手动）（{len(self._manual)}）</h4>")
            html.append("<ul>" + "".join(f"<li>{_esc(r['name'])}</li>"
                                          for r in self._manual) + "</ul>")
        if self._protected:
            html.append(f"<h4>受保护跳过（{len(self._protected)}）</h4>")
            html.append("<ul>" + "".join(f"<li>{_esc(r['name'])}</li>"
                                          for r in self._protected) + "</ul>")
        tb = QTextBrowser()
        tb.setOpenExternalLinks(False)
        tb.setHtml("".join(html))
        lay.addWidget(tb, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = bb.button(QDialogButtonBox.Ok)
        ok.setText("确认执行")
        ok.setEnabled(bool(self._targets))
        cancel = bb.button(QDialogButtonBox.Cancel)
        cancel.setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        cancel.setDefault(True)
        cancel.setFocus()

        apply_rounded(self, title="批量卸载确认", resizable=True,
                      show_min=False, show_max=True)

    def chosen_records(self):
        return list(self._targets)


class BatchResultDialog(QDialog):
    """批量执行结果汇总：已卸载并清理 / 已卸载但有未自动删残留 / 需手动 / 失败 / 受保护。"""

    def __init__(self, parent, reports):
        super().__init__(parent)
        self.setWindowTitle("批量卸载清理 · 结果")
        self.resize(680, 500)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(10)

        done_clean, done_residue, manual, failed, protected = [], [], [], [], []
        for rep in reports:
            st = rep.get("status")
            if st == "uninstalled":
                (done_residue if rep.get("kept_low") else done_clean).append(rep)
            elif st == "skipped_nosilent":
                manual.append(rep)
            elif st == "skipped_protected":
                protected.append(rep)
            else:
                failed.append(rep)

        stat = QLabel(f"✅ 已卸载并清理 {len(done_clean)} · 🧹 已卸载(有残留未自动删) "
                      f"{len(done_residue)} · ✋ 需手动 {len(manual)} · ❌ 失败 {len(failed)} · "
                      f"🔒 受保护 {len(protected)}")
        stat.setWordWrap(True)
        lay.addWidget(stat)

        tb = QTextBrowser()
        tb.setOpenExternalLinks(False)
        tb.setHtml(self._html(done_clean, done_residue, manual, failed, protected))
        lay.addWidget(tb, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        bb.button(QDialogButtonBox.Ok).setText("关闭")
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
        apply_rounded(self, title="批量结果", resizable=True,
                      show_min=False, show_max=True)

    def _html(self, done_clean, done_residue, manual, failed, protected):
        parts = []
        if done_clean:
            parts.append(f"<h4>已卸载并自动清理（{len(done_clean)}）</h4><ul>"
                         + "".join(f"<li>{_esc(r['name'])}：清理 {len(r.get('cleaned', []))} 项残留</li>"
                                   for r in done_clean) + "</ul>")
        if done_residue:
            parts.append(f"<h4>已卸载，但仍有低置信残留未自动删除（{len(done_residue)}）"
                         f"——请自行判断下列路径是否属于该软件：</h4>")
            for r in done_residue:
                parts.append(f"<p><b>{_esc(r['name'])}</b></p><ul>"
                             + "".join(f"<li><code>{_esc(p)}</code></li>"
                                       for p in r.get("kept_low", [])) + "</ul>")
        if manual:
            parts.append(f"<h4>需手动卸载（无静默卸载口，{len(manual)}）</h4><ul>"
                         + "".join(f"<li>{_esc(r['name'])}</li>"
                                   for r in manual) + "</ul>")
        if failed:
            parts.append(f"<h4>卸载失败（{len(failed)}）</h4><ul>"
                         + "".join(f"<li>{_esc(r['name'])}：{_esc(r.get('msg', ''))}</li>"
                                   for r in failed) + "</ul>")
        if protected:
            parts.append(f"<h4>受保护已跳过（{len(protected)}）</h4><ul>"
                         + "".join(f"<li>{_esc(r['name'])}</li>"
                                   for r in protected) + "</ul>")
        return "".join(parts) if parts else "<p>无处理结果。</p>"
