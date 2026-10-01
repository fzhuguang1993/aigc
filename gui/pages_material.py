"""
gui/pages_material.py —— 素材库（板块切割片段索引浏览）

把爆款拆解切出的营销板块片段按「产品 / 板块类型」归档呈现：顶部标注当前存储后端
（本轮本地），可按类型/产品筛选、双击预览、右键定位/删除。数据取自 store.material_store
（material_clips 表），文件真实位置由 core.storage 决定（LocalStorage = 素材库根下路径）。

只做「看库、放片、清理索引」，不负责切割——切割在拆解详情页板块轴触发。
"""
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QEvent
from PySide6.QtGui import QPixmap, QIcon, QColor, QPainter, QFont
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QComboBox, QListWidget, QListWidgetItem,
                               QMessageBox, QAbstractItemView, QDialog)

from store import material_store
from utils.desktop_utils import open_path, reveal_in_folder
from gui.header import page_header, Card
from gui.menus import StyledMenu
from gui.widgets import VideoPlayerDialog

VID_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# 右键「排序」子菜单项：入库日期 / 名称，两个方向都可切（与成品库同口径）
_SORT_ITEMS = (("time_desc", "生成日期（新→旧）"), ("time_asc", "生成日期（旧→新）"),
               ("name_asc", "名称（A→Z）"), ("name_desc", "名称（Z→A）"))


def _fmt_dur(sec):
    s = int(max(0, float(sec or 0)))
    return f"{s // 60:02d}:{s % 60:02d}"


def _clip_tile():
    """片段格子占位缩略：圆角色块 + 摄影机字形（不解码首帧，offscreen 也不崩）。"""
    pm = QPixmap(72, 48)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#3370FF"))
    p.drawRoundedRect(0, 0, 72, 48, 6, 6)
    p.setPen(QColor("white"))
    f = QFont()
    f.setPointSize(18)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "\U0001F3AC")
    p.end()
    return pm


class MaterialPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 14, 24, 12)
        lay.setSpacing(8)
        lay.addWidget(page_header("素材库",
                                  "双击预览 · 空白处拖动框选即多选 · 右键空白：开启/退出多选、全选、反选、排序",
                                  icon="🎞"))

        bar = QHBoxLayout()
        bar.addWidget(QLabel("类型："))
        self.cb_type = QComboBox()
        self.cb_type.currentIndexChanged.connect(self._reload)
        bar.addWidget(self.cb_type)
        bar.addWidget(QLabel("产品："))
        self.cb_product = QComboBox()
        self.cb_product.currentIndexChanged.connect(self._reload)
        bar.addWidget(self.cb_product)
        bar.addStretch(1)
        # ---------- 多选入口不在顶栏：框选自动开启，右键空白处可开关多选/全选/反选 ----------
        self.lbl_sel = QLabel("已选 0 条")
        self.lbl_sel.setObjectName("PageTip")
        self.lbl_sel.setVisible(False)
        bar.addWidget(self.lbl_sel)
        # 操作入口：主题里默认 QPushButton 就是实心蓝（显眼）；setMenu 会自动画箭头，
        # 文本里别再手写 ▾，否则两颗箭头
        self.b_ops = QPushButton("⚙ 操作")
        self.b_ops.setMenu(self._build_ops_menu())
        bar.addWidget(self.b_ops)
        self.lbl_hint = QLabel("")
        self.lbl_hint.setObjectName("PageTip")
        bar.addWidget(self.lbl_hint)
        b_root = QPushButton("📂 打开素材库目录")
        b_root.setObjectName("GhostBtn")
        b_root.clicked.connect(self._open_root)
        bar.addWidget(b_root)
        ctrl = Card(margins=(14, 10, 14, 10))
        ctrl.v.addLayout(bar)
        lay.addWidget(ctrl)

        card = Card(margins=(10, 10, 10, 10))
        lay.addWidget(card, 1)
        self.list = QListWidget()
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setIconSize(QSize(72, 48))
        self.list.setGridSize(QSize(180, 96))
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setWordWrap(True)
        # ExtendedSelection：不先开多选也能橡皮筋框选；框选落地时自动切进多选
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        # 多选开启时点条目只切换勾选，不再跳预览（右键菜单仍可预览）
        self.list.itemDoubleClicked.connect(
            lambda it: None if self._multi else self._preview_item(it))
        # 勾选框 = 多选机制：勾选变化回调里按条目 id 维护已选集合
        self.list.itemChanged.connect(self._on_item_changed)
        # 框选/点选（MultiSelection）：以选中为准回写勾选
        self.list.itemSelectionChanged.connect(self._on_selection_changed)
        # 监听视口按下/松开：区分「框选拖动」与「单击」；未开多选时单击不留选中
        self._press_pos = None
        self.list.viewport().installEventFilter(self)
        card.v.addWidget(self.list)

        self._media = None
        self._backend = "local"
        # 多选：勾选条目对应的素材 id 集合 + 当前行（供全选/计数/重命序号）
        self._multi = False
        self._sort = "time_desc"   # 默认排序：生成日期新→旧
        self._checked = set()
        self._rows = []
        # 首帧缩略图：按 path 缓存已抽好的图，后台只补缺的（与成品库同一抽取器）
        self._thumbs = {}
        self._by_path = {}
        from gui.thumb_cache import ThumbWorker
        self._thumb_worker = ThumbWorker(self)
        self._thumb_worker.ready.connect(self._on_thumb)
        self._thumb_worker.start()
        self.rebuild_filters()
        self._reload()

    # ---------------- 数据 ----------------
    def current_backend(self):
        try:
            from core.config import storage_config
            return str(storage_config().get("backend") or "local")
        except Exception:
            return "local"

    def material_root(self):
        try:
            from core.config import storage_config
            return Path(storage_config()["material_root"])
        except Exception:
            return Path.home() / "Downloads" / "素材库"

    def rebuild_filters(self):
        """类型下拉 = 设置里维护的类别清单 ∪ 库里已出现的类型（并集，保序去重），
        新产品下拉取库里出现过的值；这样新加的类别未落片也能先筛、历史类别不会丢。"""
        try:
            from core import block_categories
            cats = block_categories.load()
        except Exception:
            cats = []
        merged, seen = [], set()
        for v in list(cats) + material_store.distinct_types():
            if v and v not in seen:
                seen.add(v)
                merged.append(v)
        self._fill(self.cb_type, merged)
        self._fill(self.cb_product, material_store.distinct_products())

    @staticmethod
    def _fill(combo, values):
        cur = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("全部")
        for v in values:
            combo.addItem(v)
        idx = combo.findText(cur)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _picked(self):
        return ("" if self.cb_type.currentIndex() <= 0 else self.cb_type.currentText(),
                "" if self.cb_product.currentIndex() <= 0 else self.cb_product.currentText())

    def refresh(self):
        """定时刷新：类型/产品/条数变了才重建（不闪、不丢选中）。"""
        rows = material_store.list_clips()
        sig = (len(rows), max([(r["id"]) for r in rows], default=0))
        if sig != getattr(self, "_sig", None):
            self._sig = sig
            self.rebuild_filters()
            self._reload()

    def _reload(self):
        btype, prod = self._picked()
        rows = self._sorted(material_store.list_clips(block_type=btype, product=prod))
        self._sig = (len(material_store.list_clips()),
                     max([r["id"] for r in material_store.list_clips()], default=0))
        self._rows = rows
        # 已选集合只留仍在库里的 id（换筛选/刷新后不会拿着已消失的行去操作）
        alive = {int(r["id"]) for r in rows}
        self._checked &= alive
        self.list.blockSignals(True)
        self.list.clear()
        self._by_path = {}
        pending = []
        self._backend = self.current_backend()
        for r in rows:
            path = r["path"]
            it = QListWidgetItem()
            it.setIcon(QIcon(_clip_tile()))
            label = (f"{r['block_type'] or '片段'}\n"
                     f"{Path(path).name[:18]}\n"
                     f"{_fmt_dur(r['duration'])}")
            it.setText(label)
            it.setToolTip(f"{path}\n产品：{r['product'] or '—'}  "
                          f"{r['created_at']}  [{self._backend}]")
            it.setData(Qt.ItemDataRole.UserRole, r)
            it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            # 选框只在多选开启后出现（QListWidgetItem 默认就带 UserCheckable，关闭态要显式摘掉）；
            # 程序态回填不触发 itemChanged（外层已 blockSignals）
            if self._multi:
                checked = int(r["id"]) in self._checked
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Checked if checked
                                 else Qt.CheckState.Unchecked)
                it.setSelected(checked)   # 选中高亮与勾选保持一致，框选随时接着用
            else:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self.list.addItem(it)
            self._by_path[path] = it
            img = self._thumbs.get(path)
            if img is not None:
                it.setIcon(self._icon_of(img))
            else:
                pending.append((path, None))
        self.list.blockSignals(False)
        self._thumb_worker.submit(pending)
        self.lbl_hint.setText(
            f"共 {len(rows)} 个片段 · 后端：{self._backend} · 根：{self.material_root()}")
        self._update_sel_label()

    def _icon_of(self, img):
        """QImage → 按图标尺寸等比缩放后的 QIcon（铺不进就留黑边，不致变形）。"""
        pm = QPixmap.fromImage(img).scaled(
            self.list.iconSize(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        return QIcon(pm)

    def _on_thumb(self, path, img):
        """后台抽好一帧：存缓存并给仍在列表里的对应条目换真实首帧。"""
        if img is None or img.isNull():
            return
        self._thumbs[path] = img
        it = self._by_path.get(path)
        if it is not None:
            it.setIcon(self._icon_of(img))

    def closeEvent(self, e):
        self._thumb_worker.request_stop()
        self._thumb_worker.wait(1500)
        super().closeEvent(e)

    # ---------------- 交互 ----------------
    def _row_of(self, item):
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _preview_item(self, it):
        r = self._row_of(it)
        if not r:
            return
        path = r["path"]
        if not Path(path).exists():
            QMessageBox.information(self, "提示", "片段文件不存在或已被移动")
            return
        if Path(path).suffix.lower() in VID_EXT:
            self._media = VideoPlayerDialog(self, path)
            self._media.show()
        else:
            open_path(path)

    def _menu(self, pos):
        it = self.list.itemAt(pos)
        if it is None:
            # 空白处右键：多选 / 全选 / 反选
            self._empty_menu().exec(self.list.viewport().mapToGlobal(pos))
            return
        r = self._row_of(it)
        menu = StyledMenu(self)
        menu.addAction("👁 预览", lambda: self._preview_item(it))
        menu.addAction("📁 打开所在文件夹",
                       lambda: reveal_in_folder(r["path"]) if Path(r["path"]).exists()
                       else QMessageBox.information(self, "提示", "文件不存在"))
        menu.addSeparator()
        menu.addAction("🗑 删除索引", lambda: self._delete(r))
        menu.exec(self.list.mapToGlobal(pos))

    def _delete(self, r):
        if QMessageBox.question(
                self, "删除索引",
                f"从素材库删除「{r['block_type'] or '片段'}」的索引行？\n\n"
                "仅删索引，磁盘上的片段文件不会被删除。") \
                != QMessageBox.StandardButton.Yes:
            return
        material_store.delete(r["id"])
        self.rebuild_filters()
        self._reload()

    def _open_root(self):
        root = self.material_root()
        root.mkdir(parents=True, exist_ok=True)
        open_path(str(root))

    # ---------------- 多选 / 批量操作 ----------------
    def eventFilter(self, obj, e):
        """区分「框选拖动」与「单击」：拖动即自动开启多选（不需要先点开关）；
        未开多选时单击条目不留选中高亮，双击预览照旧。"""
        if obj is self.list.viewport():
            t = e.type()
            if (t == QEvent.Type.MouseButtonPress
                    and e.button() == Qt.MouseButton.LeftButton):
                self._press_pos = e.position().toPoint()
            elif (t == QEvent.Type.MouseButtonRelease
                    and e.button() == Qt.MouseButton.LeftButton):
                p = e.position().toPoint()
                dragged = (self._press_pos is not None
                           and (p - self._press_pos).manhattanLength() >= 6)
                self._press_pos = None
                if dragged and not self._multi:
                    # 框选即多选：视图随后会把框到的条目置选，选择回调里回写勾选
                    self._set_multi(True)
                elif not dragged and not self._multi:
                    self.list.clearSelection()
                    return False
        return super().eventFilter(obj, e)

    def _empty_menu(self):
        """空白处右键：最顶是多选开关，严格跟随当前多选态 self._multi——
        未开显示「开启多选」，已在多选态显示「退出多选」（含已选 0/1 条的情况）。
        文案与实际状态一一对应，点击必定切换生效：不会出现「已开多选却仍显示开启多选、
        点了没反应」的失效，也不会「只显示开启却同时列出批量项」的自相矛盾。
        开启后才列 全选/反选/批量；排序常驻。

        注意：菜单项一律不用 setCheckable——windowsvista 会在项前画原生小圆框+对钩，
        很丑；当前状态靠文案表达（排序用「✓ 」文本前缀）。"""
        m = StyledMenu(self)
        if self._multi:
            m.addAction("退出多选", lambda: self._set_multi(False))
            m.addSeparator()
            m.addAction("全选", self._act_select_all)
            m.addAction("反选", self._act_invert)
            m.addSeparator()
            m.addAction("🏷 绑定产品…", self._act_bind)
            m.addAction("✏ 批量重命名…", self._act_rename)
            m.addAction("🗑 删除片段（进回收站）", self._act_delete)
        else:
            m.addAction("开启多选", lambda: self._set_multi(True))
        m.addSeparator()
        m.addMenu(self._build_sort_menu())
        return m

    def _build_sort_menu(self):
        """「↕ 排序」子菜单：悬停展开；当前方式前缀「✓」（纯文本，不用勾选框）。"""
        sm = StyledMenu("↕ 排序", self)
        for mode, label in _SORT_ITEMS:
            text = f"✓ {label}" if mode == self._sort else f"　 {label}"
            sm.addAction(text, lambda _=False, mo=mode: self._pick_sort(mo))
        return sm

    def _pick_sort(self, mode):
        self._sort = mode
        self._reload()

    def _sorted(self, rows):
        """展示层排序（不动数据层）：日期按 created_at 字符串（定长可字典序），
        名称忽略大小写；库里原始序就是 id 倒序≈时间新→旧，time_desc 直接沿用。"""
        if self._sort == "name_asc":
            return sorted(rows, key=lambda r: (Path(r["path"]).name or "").lower())
        if self._sort == "name_desc":
            return sorted(rows, key=lambda r: (Path(r["path"]).name or "").lower(),
                          reverse=True)
        if self._sort == "time_asc":
            return sorted(rows, key=lambda r: (r.get("created_at") or ""))
        if self._sort == "time_desc":
            return sorted(rows, key=lambda r: (r.get("created_at") or ""), reverse=True)
        return rows

    def _set_multi(self, on):
        """开/关多选：开 = 条目出勾选框、点条目任意位置即勾选（触发区=整个 item）；
        关 = 收起选框并清空。开启入口：鼠标框选自动开，或右键空白处手动开。"""
        if on == self._multi:
            return
        self._multi = on
        self.list.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection if on
            else QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            it = self.list.item(i)
            if on:
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            else:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Unchecked)
        self.list.blockSignals(False)
        if not on:
            self.list.clearSelection()
            self._checked.clear()
        self._update_sel_label()

    def _on_item_changed(self, it):
        r = self._row_of(it)
        if not r:
            return
        cid = int(r["id"])
        checked = it.checkState() == Qt.CheckState.Checked
        if checked:
            self._checked.add(cid)
        else:
            self._checked.discard(cid)
        # 点勾是另一个入口：把选中高亮同步过去（屏蔽选择信号防回调互搓）
        sm = self.list.selectionModel()
        if sm is not None:
            sm.blockSignals(True)
            it.setSelected(checked)
            sm.blockSignals(False)
        self._update_sel_label()

    def _on_selection_changed(self):
        """鼠标框选/点选：以选中为准回写勾选框与已选集合。"""
        if not self._multi:
            return
        self.list.blockSignals(True)
        ids = set()
        for i in range(self.list.count()):
            it = self.list.item(i)
            r = self._row_of(it)
            if not r:
                continue
            if it.isSelected():
                ids.add(int(r["id"]))
                it.setCheckState(Qt.CheckState.Checked)
            else:
                it.setCheckState(Qt.CheckState.Unchecked)
        self.list.blockSignals(False)
        self._checked = ids
        self._update_sel_label()

    def _update_sel_label(self):
        n = len(self._checked)
        self.lbl_sel.setText(f"已选 {n} 条")
        self.lbl_sel.setVisible(n > 0)
        # 多选/已勾选时，批量入口改名“批量操作”；setMenu 自带箭头，文本不写 ▾
        self.b_ops.setText("⚙ 批量操作" if (self._multi or n) else "⚙ 操作")

    def _act_select_all(self):
        self._set_multi(True)
        self._select_all()

    def _act_invert(self):
        """反选：已勾的去掉、没勾的勾上（多选未开时先自动开启）。"""
        self._set_multi(True)
        new = set()
        self.list.blockSignals(True)
        sm = self.list.selectionModel()
        if sm is not None:
            sm.blockSignals(True)
        for i in range(self.list.count()):
            it = self.list.item(i)
            r = self._row_of(it)
            if not r:
                continue
            cid = int(r["id"])
            on = cid not in self._checked
            it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            it.setSelected(on)
            if on:
                new.add(cid)
        if sm is not None:
            sm.blockSignals(False)
        self.list.blockSignals(False)
        self._checked = new
        self._update_sel_label()

    def _select_all(self):
        self.list.blockSignals(True)
        sm = self.list.selectionModel()
        if sm is not None:
            sm.blockSignals(True)
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setCheckState(Qt.CheckState.Checked)
            it.setSelected(True)
        if sm is not None:
            sm.blockSignals(False)
        self.list.blockSignals(False)
        for r in self._rows:
            self._checked.add(int(r["id"]))
        self._update_sel_label()

    def _clear_sel(self):
        self.list.blockSignals(True)
        sm = self.list.selectionModel()
        if sm is not None:
            sm.blockSignals(True)
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setCheckState(Qt.CheckState.Unchecked)
            it.setSelected(False)
        if sm is not None:
            sm.blockSignals(False)
        self.list.blockSignals(False)
        self._checked.clear()
        self._update_sel_label()

    def _checked_rows(self):
        """已勾选的行（按当前列表顺序）——重命序号按这个走。"""
        return [r for r in self._rows if int(r["id"]) in self._checked]

    def _build_ops_menu(self):
        m = StyledMenu(self)
        m.addAction("🏷 绑定产品…", self._act_bind)
        m.addSeparator()
        m.addAction("✏ 批量重命名…", self._act_rename)
        m.addSeparator()
        m.addAction("🗑 删除片段（进回收站）", self._act_delete)
        return m

    def _require_sel(self):
        rows = self._checked_rows()
        if not rows:
            QMessageBox.information(self, "先选几条",
                                    "先空白处拖动框选，或右键空白处→开启多选后点选要操作的片段。")
        return rows

    def _act_bind(self):
        rows = self._require_sel()
        if not rows:
            return
        from gui.dialogs_product import pick_product
        picked = pick_product(self)
        if not picked:
            return
        _pid, name = picked
        material_store.set_products([int(r["id"]) for r in rows], name)
        self._clear_sel()
        self.rebuild_filters()
        self._reload()
        self.lbl_hint.setText(f"已将 {len(rows)} 个片段归属到「{name}」")

    def _act_rename(self):
        rows = self._require_sel()
        if not rows:
            return
        from gui.dialogs_rename import BatchRenameDialog, MATERIAL_FIELDS
        dlg = BatchRenameDialog(self, rows, fields=MATERIAL_FIELDS, title="批量重命名素材")
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.plan:
            return
        done, failed = 0, []
        for row, stem in dlg.plan:
            ok, _new, msg = material_store.rename(row["id"], stem)
            if ok:
                done += 1
            else:
                failed.append(f"{Path(str(row['path'])).name}：{msg}")
        self._clear_sel()
        self.rebuild_filters()
        self._reload()
        if failed:
            QMessageBox.warning(self, "部分未改名",
                                f"成功 {done} 条，失败 {len(failed)} 条：\n"
                                + "\n".join(failed[:8])
                                + ("\n…" if len(failed) > 8 else ""))
        else:
            self.lbl_hint.setText(f"已重命名 {done} 个片段")

    def _act_delete(self):
        rows = self._require_sel()
        if not rows:
            return
        if QMessageBox.question(
                self, "确认删除",
                f"把选中的 {len(rows)} 个片段送进回收站并删除索引？\n\n"
                "（文件进回收站，误删可捞回；被占用删不掉的会保留索引行）") \
                != QMessageBox.StandardButton.Yes:
            return
        done, failed = material_store.delete_with_files([int(r["id"]) for r in rows])
        self._clear_sel()
        self.rebuild_filters()
        self._reload()
        if failed:
            QMessageBox.warning(self, "部分未删除",
                                f"已删 {len(done)} 条索引，失败 {len(failed)} 条：\n"
                                + "\n".join(f"{p}：{m}" for p, m in failed[:8])
                                + ("\n…" if len(failed) > 8 else ""))
        else:
            self.lbl_hint.setText(f"已将 {len(done)} 个片段送进回收站")
