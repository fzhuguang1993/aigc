"""
gui/pages_remix.py —— AI 混剪（MVP）：选片段 → 排序 → concat 拼接导出

从素材库 / 成品库 / 混剪暂存勾选片段进清单，可拖拽调整顺序（或点「🧠 AI 建议顺序」
让豆包按营销叙事排一版），导出的顺序就是清单从上到下。拼接走 ffmpeg concat：源规格
不一致时自动重编码统一。导出落 成品库/混剪/<日期>_<名>.mp4，之后在成品库页可见。

明确边界：不做 AI 生成画面，'AI' 只给顺序建议。排序建议与拼接都在后台线程跑。
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QLineEdit, QComboBox, QAbstractItemView, QDialog, QDialogButtonBox,
                               QToolButton, QSplitter)

from store import material_store, output_store
from utils.desktop_utils import open_path, reveal_in_folder
from gui.header import page_header, Card
from gui.widgets import VideoPlayerDialog
from gui.menus import StyledMenu
from gui.media_grid import MediaGrid

from video_text_tools.remix import remixer


def _clip_from_material(r):
    return {"path": r["path"], "block_type": r.get("block_type") or "片段",
            "product": r.get("product") or "", "duration": float(r.get("duration") or 0),
            "label": Path(r["path"]).name}


def _clip_from_output(r):
    return {"path": r["path"], "block_type": "成品",
            "product": r.get("product") or "", "duration": 0.0,
            "label": r.get("name") or Path(r["path"]).name}


def _clip_from_path(p):
    return {"path": str(p), "block_type": "成品", "product": "",
            "duration": 0.0, "label": Path(str(p)).name}


# 选素材排序选项（key, 中文名）：多条件快速定位。
_PICKER_SORTS = (("default", "默认（加入顺序）"),
                 ("name_asc", "名称 A→Z"),
                 ("name_desc", "名称 Z→A"),
                 ("dur_desc", "时长（长→短）"),
                 ("dur_asc", "时长（短→长）"),
                 ("tag_dur", "标签↑ · 时长↓"),
                 ("prod_dur", "产品↑ · 时长↓"))


class _SectionBox(QWidget):
    """可收缩的单选筛选列表：小标题行带 ▼/▶ 按钮，点击切换列表显隐；
    点击列表项调 on_pick(value)。首个自动为“全部”（value=""）。"""

    def __init__(self, title, on_pick, parent=None):
        super().__init__(parent)
        self._title = title
        self._expanded = True
        self._on_pick = on_pick
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        self._btn = QToolButton()
        self._btn.setText(f"\u25bc {title}")
        self._btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._btn.setAutoRaise(True)
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.clicked.connect(self._toggle)
        v.addWidget(self._btn)
        self._list = QListWidget()
        self._list.setMaximumHeight(160)
        self._list.itemClicked.connect(
            lambda it: self._on_pick(str(it.data(Qt.ItemDataRole.UserRole) or "")))
        v.addWidget(self._list)

    def set_items(self, pairs):
        """重建列表；第一个固定为“全部”（value=""），随后为 pairs=[(label, value)]。
        保留当前选中项。"""
        cur = self._cur_val()
        self._list.blockSignals(True)
        self._list.clear()
        it0 = QListWidgetItem("全部")
        it0.setData(Qt.ItemDataRole.UserRole, "")
        self._list.addItem(it0)
        for label, val in pairs:
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, val)
            self._list.addItem(it)
        for i in range(self._list.count()):
            if str(self._list.item(i).data(Qt.ItemDataRole.UserRole) or "") == cur:
                self._list.setCurrentRow(i)
                break
        else:
            self._list.setCurrentRow(0)
        self._list.blockSignals(False)

    def _cur_val(self):
        it = self._list.currentItem()
        return str(it.data(Qt.ItemDataRole.UserRole) or "") if it else ""

    def _toggle(self):
        self._expanded = not self._expanded
        self._list.setVisible(self._expanded)
        self._btn.setText(f"{'\u25bc' if self._expanded else '\u25b6'} {self._title}")


class _PickerDialog(QDialog):
    """通用多选挑选：items=[(label, clip)]；卡片网格 pick 模式，确认后回吐勾选的 clip 列表。

    pick 模式：卡片常开勾选、单击只勾选不预览、双击预览；勾选集合按 clip 对象 id 归集，
    确认后 selected_rows() 按入参顺序回吐 clip（接口对外不变，_open_picker 逻辑不变）。"""

    def __init__(self, parent, title, items):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 600)
        self._entries = [clip for _label, clip in items]
        self._picked = []
        self._media = None
        self._shown = []
        self._tag_filter = ""    # 当前标签筛选值（""=全部）
        self._prod_filter = ""   # 当前产品筛选值（""=全部，"__unbound__"=未分类）
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        # 顶部筛选行：搜索 + 排序 + 显示统计
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索名称/路径…")
        self.ed_search.setFixedWidth(200)
        self.ed_search.textChanged.connect(lambda *_: self._apply_filter())
        bar.addWidget(self.ed_search)
        bar.addStretch(1)
        bar.addWidget(QLabel("排序："))
        self.cb_sort = QComboBox()
        self.cb_sort.addItems([lbl for _k, lbl in _PICKER_SORTS])
        self._sort_keys = [k for k, _l in _PICKER_SORTS]
        self.cb_sort.currentIndexChanged.connect(lambda *_: self._apply_filter())
        bar.addWidget(self.cb_sort)
        self.lbl_shown = QLabel("")
        self.lbl_shown.setObjectName("PageTip")
        bar.addWidget(self.lbl_shown)
        v.addLayout(bar)

        self.grid = MediaGrid(key_of=lambda r: str(id(r)),
                              path_of=lambda r: r.get("path") or "",
                              build_display=self._display,
                              on_open=self._preview,
                              on_menu=self._menu,
                              on_selection_changed=self._update_shown,
                              pick=True)
        self.grid.set_empty_menu(self._empty_menu)

        # 左侧可收缩筛选面板
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)
        self.sec_tag = _SectionBox("标签（板块类型）",
                                   lambda v: self._set_filter("tag", v))
        self.sec_prod = _SectionBox("产品",
                                     lambda v: self._set_filter("prod", v))
        lv.addWidget(self.sec_tag)
        lv.addWidget(self.sec_prod)
        lv.addStretch(1)
        left.setFixedWidth(160)

        rc = Card(margins=(8, 8, 8, 8))
        rc.v.addWidget(self.grid, 1)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(rc)
        split.setStretchFactor(1, 1)
        split.setSizes([160, 720])
        v.addWidget(split, 1)

        v.addWidget(QLabel("勾选要加入的片段 · 双击预览 · 左侧可收缩面板按标签/产品筛选"))
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)
        self._fill_filters()
        self._apply_filter()

    def _set_filter(self, which, val):
        if which == "tag":
            self._tag_filter = val
        else:
            self._prod_filter = val
        self._apply_filter()
    
    def _fill_filters(self):
        """按本批 entries 的取值重建两个筛选段（含“未分类”）。"""
        tag_cnt: dict = {}
        prod_cnt: dict = {}
        for c in self._entries:
            t = (c.get("block_type") or "").strip()
            p = (c.get("product") or "").strip()
            tag_cnt[t] = tag_cnt.get(t, 0) + 1
            prod_cnt[p] = prod_cnt.get(p, 0) + 1
        tag_pairs = [(f"{k or '未分类'} ({tag_cnt[k]})", k or "__unbound__")
                     for k in sorted(tag_cnt.keys()) if k]  # 非空分类单独列出
        if "" in tag_cnt:
            tag_pairs.insert(len(tag_pairs),
                             (f"未分类 ({tag_cnt['']})",  "__unbound__"))
        prod_pairs = [(f"{k or '未分类'} ({prod_cnt[k]})", k or "__unbound__")
                      for k in sorted(prod_cnt.keys()) if k]
        if "" in prod_cnt:
            prod_pairs.insert(len(prod_pairs),
                              (f"未分类 ({prod_cnt['']})", "__unbound__"))
        self.sec_tag.set_items(tag_pairs)
        self.sec_prod.set_items(prod_pairs)
    
    def _apply_filter(self):
        """按搜索/标签/产品过滤 + 多条件排序，重建卡片（勾选集合跳重建保持）。"""
        if not hasattr(self, "grid"):        # 构造期下拉 addItems 会先发一次信号，此时网格未就绪
            return
        kw = (self.ed_search.text() or "").strip().lower()
        tag = self._tag_filter
        prod = self._prod_filter
        out = []
        for c in self._entries:
            bt = (c.get("block_type") or "").strip()
            cp = (c.get("product") or "").strip()
            if tag == "__unbound__":
                if bt:
                    continue
            elif tag and bt != tag:
                continue
            if prod == "__unbound__":
                if cp:
                    continue
            elif prod and cp != prod:
                continue
            if kw and kw not in ((c.get("label") or "") + " " + (c.get("path") or "")).lower():
                continue
            out.append(c)
        key = self._sort_keys[self.cb_sort.currentIndex()] if self.cb_sort.count() else "default"
        if key != "default":
            def _name(c):
                return (c.get("label") or Path(c.get("path") or "").name).lower()
    
            def _dur(c):
                return float(c.get("duration") or 0)
            if key == "name_asc":
                out.sort(key=_name)
            elif key == "name_desc":
                out.sort(key=_name, reverse=True)
            elif key == "dur_desc":
                out.sort(key=_dur, reverse=True)
            elif key == "dur_asc":
                out.sort(key=_dur)
            elif key == "tag_dur":
                out.sort(key=lambda c: ((c.get("block_type") or ""), -_dur(c)))
            elif key == "prod_dur":
                out.sort(key=lambda c: ((c.get("product") or ""), -_dur(c)))
        self._shown = out
        self.grid.set_rows(out)
        self._update_shown()

    def _update_shown(self, *_a):
        self.lbl_shown.setText(
            f"显示 {len(self._shown)}/{len(self._entries)} · 已选 {len(self.grid.selected())}")

    def _display(self, clip):
        return {"name": clip.get("label") or Path(clip.get("path") or "").name,
                "tags": f"🎬 {clip.get('block_type') or '片段'}",
                "meta": _fmt(clip.get("duration") or 0),
                "tooltip": clip.get("path") or ""}

    def _preview(self, clip):
        p = (clip or {}).get("path") or ""
        if not p or not Path(p).exists():
            QMessageBox.information(self, "提示", "片段文件不存在或已被移动")
            return
        self._media = VideoPlayerDialog(self, p)
        self._media.show()

    def _menu(self, clip, global_pos):
        menu = StyledMenu(self)
        menu.addAction("👁 预览", lambda: self._preview(clip))
        p = (clip or {}).get("path") or ""
        if p and Path(p).exists():
            menu.addAction("📁 打开所在文件夹", lambda: reveal_in_folder(p))
        menu.exec(global_pos)

    def _empty_menu(self):
        menu = StyledMenu(self)
        menu.addAction("☑ 全选", self.grid.select_all)
        menu.addAction("⇄ 反选", self.grid.invert)
        menu.addAction("✕ 清空勾选", self.grid.clear_sel)
        return menu

    def _accept(self):
        # 从全量 entries 按勾选集合回吏（包括当前被筛掉但仍勾选的），保留加入顺序
        sel = self.grid.selected()
        self._picked = [c for c in self._entries if str(id(c)) in sel]
        self.grid.stop_worker()
        self.accept()

    def reject(self):
        self.grid.stop_worker()
        super().reject()

    def clips(self):
        return self._picked


class RemixPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 14, 24, 12)
        lay.setSpacing(8)
        lay.addWidget(page_header("AI 混剪",
                                  "选片段 · 排序 · 一键拼接导出 · 规格不一致自动重编码",
                                  icon="🎬"))

        bar = QHBoxLayout()
        bar.addWidget(QLabel("作品名："))
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("给这条混剪起个名（留空=混剪）")
        bar.addWidget(self.ed_name, 1)
        for text, slot in (("＋ 素材库", self._add_material),
                           ("＋ 成品库", self._add_output),
                           ("＋ 从暂存加入", self._add_staging)):
            b = QPushButton(text)
            b.setObjectName("GhostBtn")
            b.clicked.connect(slot)
            bar.addWidget(b)
        lay.addWidget(_wrap(bar))

        ops = QHBoxLayout()
        b_ai = QPushButton("🧠 AI 建议顺序")
        b_ai.clicked.connect(self._ai_order)
        b_tmpl = QPushButton("🏗 模板拼装")
        b_tmpl.setObjectName("GhostBtn")
        b_tmpl.setToolTip("自定义段落模板，逐槽从素材库选片")
        b_tmpl.clicked.connect(self._open_template_editor)
        b_auto = QPushButton("🤖 AI 自动拼装")
        b_auto.setObjectName("GhostBtn")
        b_auto.setToolTip("按板块类型序列自动从素材库取片，每类最多 N 条")
        b_auto.clicked.connect(self._open_auto_template)
        b_export = QPushButton("▶ 导出混剪")
        b_export.setObjectName("PrimaryBtn")
        b_export.clicked.connect(self._export)
        b_clear = QPushButton("🗑 清空清单")
        b_clear.setObjectName("GhostBtn")
        b_clear.clicked.connect(self.list_clear)
        ops.addWidget(b_ai)
        ops.addWidget(b_tmpl)
        ops.addWidget(b_auto)
        ops.addWidget(b_export)
        ops.addWidget(b_clear)
        ops.addStretch(1)
        self.lbl_count = QLabel("0 段")
        ops.addWidget(self.lbl_count)
        lay.addWidget(_wrap(ops))
        self.b_ai = b_ai
        self.b_export = b_export

        card = Card(margins=(10, 10, 10, 10))
        lay.addWidget(card, 1)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.itemDoubleClicked.connect(self._preview_item)
        card.v.addWidget(self.list)
        lay.addWidget(card, 1)

        self.log = _LogBox()
        lay.addWidget(self.log)

        self._media = None
        self._worker = None
        self._append("提示：先添加片段（素材库/成品库/暂存），拖拽排序，再导出。")

    # ---------------- 添加片段 ----------------
    def showEvent(self, e):
        super().showEvent(e)
        # 进页面时若有混剪暂存，主动提示可一键加入（不自动加，避免误入）
        from store import app_state
        if app_state.get("remix_stage"):
            self._append(f"混剪暂存有 {len(app_state.get('remix_stage'))} 条，"
                         "点「＋ 从暂存加入」把它们并入清单。")

    def _add_material(self):
        rows = material_store.list_clips()
        if not rows:
            QMessageBox.information(self, "素材库为空", "先去拆解详情页「✂ 切割入素材库」产出片段")
            return
        items = [(f"{r['block_type'] or '片段'} · {Path(r['path']).name} · "
                  f"{_fmt(r['duration'])}", _clip_from_material(r)) for r in rows]
        self._open_picker("从素材库添加片段", items)

    def _add_output(self):
        rows = output_store.list_outputs(hide_bad=True)
        if not rows:
            QMessageBox.information(self, "成品库为空", "产物目录里还没有可混剪的成品视频")
            return
        items = [(f"{r['product'] or '未归类'} · {r['name']}", _clip_from_output(r))
                 for r in rows]
        self._open_picker("从成品库添加片段", items)

    def _add_staging(self):
        from store import app_state
        paths = list(app_state.get("remix_stage") or [])
        exist = {it.data(Qt.ItemDataRole.UserRole)["path"] for it in
                 self._items()}
        added = 0
        for p in paths:
            if p and p not in exist and Path(p).exists():
                self._append_clip(_clip_from_path(p))
                added += 1
        app_state.set_value("remix_stage", [])
        self._append(f"从暂存加入 {added} 条（暂存已清空）。")

    def _open_picker(self, title, items):
        dlg = _PickerDialog(self, title, items)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            n = 0
            for clip in dlg.clips():
                self._append_clip(clip)
                n += 1
            self._append(f"加入 {n} 段。")

    # ---------------- 模板拼装 / AI 自动拼装 ----------------
    def _open_template_editor(self):
        """自定义模板：用户编排 block_type 序列，逐槽从素材库选片，按模板顺序载入清单。"""
        from gui.dialogs_remix_template import TemplateEditorDialog
        rows = material_store.list_clips(limit=2000)
        if not rows:
            QMessageBox.information(self, "素材库为空",
                                    "素材库还没有片段：先去拆解详情页「✂ 切割入素材库」")
            return
        clips = [_clip_from_material(r) for r in rows]
        dlg = TemplateEditorDialog(self, clips)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            result = dlg.result_clips()
            if not result:
                return
            self.list.clear()
            for c in result:
                self._append_clip(c)
            self._append(f"模板拼装完成，共 {self.list.count()} 段。"
                         "可继续拖拽微调后导出。")

    def _open_auto_template(self):
        """AI 自动拼装：按 block_categories 序列自动从素材库取片，载入清单等待用户微调。"""
        from gui.dialogs_remix_template import AutoTemplateDialog
        rows = material_store.list_clips(limit=2000)
        if not rows:
            QMessageBox.information(self, "素材库为空",
                                    "素材库还没有片段：先去拆解详情页「✂ 切割入素材库」")
            return
        clips = [_clip_from_material(r) for r in rows]
        prods = material_store.distinct_products()
        dlg = AutoTemplateDialog(self, clips, prods)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            result = dlg.result_clips()
            if not result:
                return
            self.list.clear()
            for c in result:
                self._append_clip(c)
            self._append(f"AI 自动拼装完成，共 {self.list.count()} 段。"
                         "可微调后导出。")

    def _items(self):
        return [self.list.item(i) for i in range(self.list.count())]

    def _append_clip(self, clip):
        it = QListWidgetItem()
        it.setData(Qt.ItemDataRole.UserRole, clip)
        self._render(it)
        self.list.addItem(it)
        self._sync_count()

    def _render(self, it):
        c = it.data(Qt.ItemDataRole.UserRole)
        row = self.list.row(it) + 1
        it.setText(f"{row}. [{c['block_type']}] {c['label']}  {_fmt(c['duration'])}")

    def _sync_count(self):
        for i in range(self.list.count()):
            self._render(self.list.item(i))
        self.lbl_count.setText(f"{self.list.count()} 段")

    def list_clear(self):
        self.list.clear()
        self._sync_count()

    # ---------------- 排序 / 预览 ----------------
    def _clips(self):
        return [it.data(Qt.ItemDataRole.UserRole) for it in self._items()]

    def _menu(self, pos):
        it = self.list.itemAt(pos)
        if it is None:
            return
        self.list.setCurrentItem(it)
        menu = StyledMenu(self)
        menu.addAction("👁 预览", lambda: self._preview_item(it))
        menu.addAction("📁 打开所在文件夹", lambda: self._reveal(it))
        menu.addSeparator()
        menu.addAction("⬆ 上移", lambda: self._move(it, -1))
        menu.addAction("⬇ 下移", lambda: self._move(it, 1))
        menu.addAction("✕ 移出清单", lambda: self._remove(it))
        menu.exec(self.list.mapToGlobal(pos))

    def _preview_item(self, it):
        c = it.data(Qt.ItemDataRole.UserRole) if it else None
        if not c:
            return
        if not Path(c["path"]).exists():
            QMessageBox.information(self, "提示", "片段文件不存在或已被移动")
            return
        self._media = VideoPlayerDialog(self, c["path"])
        self._media.show()

    def _reveal(self, it):
        c = it.data(Qt.ItemDataRole.UserRole)
        if c and Path(c["path"]).exists():
            reveal_in_folder(c["path"])

    def _move(self, it, delta):
        row = self.list.row(it)
        dst = row + delta
        if dst < 0 or dst >= self.list.count():
            return
        self.list.takeItem(row)
        self.list.insertItem(dst, it)
        self.list.setCurrentItem(it)
        self._sync_count()

    def _remove(self, it):
        self.list.takeItem(self.list.row(it))
        self._sync_count()

    # ---------------- AI 顺序 / 导出 ----------------
    def _guard_busy(self):
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "忙", "上一个任务还在进行，请稍候…")
            return True
        return False

    def _ai_order(self):
        if self._guard_busy():
            return
        clips = self._clips()
        if len(clips) < 2:
            self._append("至少两段才能建议顺序。")
            return
        self._append("🧠 正在向豆包请求推荐顺序…")

        def fn(log, progress, should_stop):
            order = remixer.suggest_order(clips, log=log)
            return {"order": order}

        self._start(fn, self._on_ai_done)

    def _on_ai_done(self, res):
        if isinstance(res, Exception):
            self._append(f"✗ AI 顺序失败：{res}")
            return
        order = res.get("order") or []
        clips = self._clips()
        new = remixer.apply_order(clips, order)
        self.list.clear()
        for c in new:
            self._append_clip(c)
        self._append("✓ 已按建议顺序重排清单（不满意可继续拖拽微调）。")

    def _export(self):
        if self._guard_busy():
            return
        clips = self._clips()
        if len(clips) < 1:
            self._append("清单为空，先添加片段。")
            return
        if len(clips) < 2:
            self._append("⚠ 只有 1 段，导出等同复制（仍会拼）。")
        name = (self.ed_name.text() or "").strip()

        def fn(log, progress, should_stop):
            return remixer.run_remix(clips, name=name, log=log)

        self._start(fn, self._on_export_done)

    def _on_export_done(self, res):
        if isinstance(res, Exception):
            self._append(f"✗ 导出异常：{res}")
            return
        if res.get("ok"):
            self._append(f"✅ 混剪已导出：{res['dst']}（{res['count']} 段）")
            self._append("到「成品库」页即可看到这条成片。")
        else:
            self._append(f"⚠ 导出失败：{res.get('reason')}")

    # ---------------- 后台线程 ----------------
    def _start(self, fn, on_done):
        from gui.tool_panels import ToolWorker
        self._set_busy(True)
        self._worker = ToolWorker(fn, self)
        self._worker.log.connect(self._append)
        self._worker.done.connect(lambda r, cb=on_done: (cb(r), self._set_busy(False)))
        self._worker.start()

    def _set_busy(self, on):
        self.b_ai.setEnabled(not on)
        self.b_export.setEnabled(not on)

    def _append(self, text):
        self.log.append_line(str(text))

    def closeEvent(self, e):
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(2000)
        super().closeEvent(e)


class _LogBox(Card):
    def __init__(self, parent=None):
        super().__init__(parent, margins=(12, 10, 12, 10))
        from PySide6.QtWidgets import QPlainTextEdit
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumHeight(120)
        self.v.addWidget(QLabel("混剪日志"))
        self.v.addWidget(self.view)

    def append_line(self, text):
        self.view.appendPlainText(text)


def _wrap(layout):
    """把一排控件装进一张白卡并返回卡片（工具条不再裸摆灰底）。"""
    holder = Card(margins=(12, 8, 12, 8))
    holder.v.addLayout(layout)
    return holder


def _fmt(sec):
    s = int(max(0, float(sec or 0)))
    return f"{s // 60:02d}:{s % 60:02d}"
