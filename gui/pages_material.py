"""
gui/pages_material.py —— 素材库（板块切割片段索引浏览）

把爆款拆解切出的营销板块片段按「产品 / 板块类型」归档呈现：顶部标注当前存储后端
（本轮本地），可按类型/产品筛选、点卡片预览、右键定位/删除。数据取自 store.material_store
（material_clips 表），文件真实位置由 core.storage 决定（LocalStorage = 素材库根下路径）。

卡片网格复用共享组件 gui.media_grid.MediaGrid，排版与成品库完全一致（封面首帧 + 滚动标题 +
徽标=板块类型 + meta=时长·产品·日期 + 空白处框选即多选）。只做「看库、放片、清理索引」，
不负责切割——切割在拆解详情页板块轴触发。
"""
from pathlib import Path
import time

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QComboBox, QMessageBox, QDialog, QSplitter, QInputDialog,
                               QPlainTextEdit)

from store import material_store
from utils.desktop_utils import open_path, reveal_in_folder
from gui.header import page_header, Card
from gui.menus import StyledMenu
from gui.widgets import VideoPlayerDialog
from gui.media_grid import MediaGrid
from gui.tree_filter import TreeFilterPanel

VID_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# 右键「排序」子菜单项：入库日期 / 名称，两个方向都可切（与成品库同口径）
_SORT_ITEMS = (("time_desc", "生成日期（新→旧）"), ("time_asc", "生成日期（旧→新）"),
               ("name_asc", "名称（A→Z）"), ("name_desc", "名称（Z→A）"))


def _fmt_dur(sec):
    s = int(max(0, float(sec or 0)))
    return f"{s // 60:02d}:{s % 60:02d}"


class MaterialAsrWorker(QThread):
    """素材库口播增量转写：单后台线程串行跑 Whisper，与成品库 OutputAsrWorker 同构。

    只接「已标记为口播素材且尚未转写」的片段（与成品库同口径门控：ASR 有时把环境音
    误识别成口播，故必须人工标记才跑）。每完成一条用 done_one 信号回主线程（片段 id /
    正文 / 是否有口播 / 模型 / 耗时秒 / 错误）；单条失败带错误回主线程记入失败集，不写库→不重试。
    """

    done_one = Signal(int, str, int, str, float, str)   # clip_id, text, has_speech, model, 耗时, 错误

    def __init__(self, size, clips, parent=None):
        super().__init__(parent)
        self._size = size
        self._clips = [(int(cid), str(p)) for cid, p in (clips or [])
                       if str(p or "").strip()]
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        from video_text_tools.asr import transcribe as tr
        for cid, path in self._clips:
            if self._stop:
                break
            t0 = time.perf_counter()
            try:
                if not Path(path).exists():
                    continue
                segments, full_text = tr.transcribe(
                    path, size=self._size, should_stop=lambda: self._stop)
                text = (full_text or "").strip()
                has = 1 if (text and segments) else 0
                self.done_one.emit(cid, text, has, self._size,
                                   time.perf_counter() - t0, "")
            except Exception as e:
                self.done_one.emit(cid, "", 0, self._size,
                                   time.perf_counter() - t0, str(e))


class MaterialPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 14, 24, 12)
        lay.setSpacing(8)
        lay.addWidget(page_header("素材库",
                                  "点卡片预览 · 空白处拖动框选即多选 · 右键空白：开启/退出多选、全选、反选、排序",
                                  icon="🎞"))

        bar = QHBoxLayout()
        bar.addWidget(QLabel("类型："))
        self.cb_type = QComboBox()
        self.cb_type.currentIndexChanged.connect(lambda _=None: self._on_type_combo())
        bar.addWidget(self.cb_type)
        bar.addWidget(QLabel("产品："))
        self.cb_product = QComboBox()
        self.cb_product.currentIndexChanged.connect(lambda _=None: self._on_product_combo())
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

        # 卡片网格（与成品库同款排版）：本页只喂取值/回调
        self.grid = MediaGrid(key_of=lambda r: str(int(r["id"])),
                              path_of=lambda r: r["path"],
                              build_display=self._display,
                              on_open=self._preview,
                              on_menu=self._card_menu,
                              on_selection_changed=self._update_sel_label,
                              hover_preview=True,
                              on_badge=self._badge_hit)
        self.grid.set_empty_menu(self._empty_menu)

        # 左侧「标签树(板块类型) + 产品树」筛选面板（包白卡）
        self.panel = TreeFilterPanel(tag_title="标签（板块类型）", product_title="产品")
        self.panel.tag_picked.connect(self._on_tree_tag)
        self.panel.product_picked.connect(self._on_tree_product)
        self.left_card = Card(margins=(8, 8, 8, 8))
        self.left_card.v.addWidget(self.panel)
        self.left_card.setFixedWidth(220)   # 固定最小宽度 220，不可拖宽（之前用最小宽度→可被 splitter 拉宽）

        right = Card(margins=(10, 10, 10, 10))
        right.v.addWidget(self.grid, 1)

        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.addWidget(self.left_card)
        self.split.addWidget(right)
        self.split.setCollapsible(0, False)   # 左栏固定，禁止拖到 0
        self.split.setSizes([220, 780])
        lay.addWidget(self.split, 1)

        self._media = None
        self._backend = "local"
        self._sort = "time_desc"   # 默认排序：生成日期新→旧
        self._rows = []
        self._base = []            # 全量片段行（树计数 + 本地筛选的唯一数据源）
        self._tree_sig = None
        self._tree_sync = False     # 树驱动下拉时置真，避免下拉回调二次刷新
        from store import app_state
        self._tree_visible = bool(app_state.get("material_tree_visible", True))
        self._asr = None               # 后台口播转写 worker（单线程串行；None=空闲）
        self._asr_failed = set()       # 本会话已失败/跳过的片段 id（不每刷一次热重试）
        self._bubble = None            # 悬停脚本浮窗（单例复用）
        self.rebuild_filters()
        self._apply_tree_visible()
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
        self._fill(self.cb_type, merged + ["未分类"])
        self._fill(self.cb_product, material_store.distinct_products() + ["未分类"])

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
        base = material_store.list_clips()
        self._base = base
        self._sig = (len(base), max([r["id"] for r in base], default=0))
        rows = self._filtered(base)
        self._rows = rows
        # 已选集合只留仍在库里的 id（换筛选/刷新后不会拿着已消失的行去操作）
        alive = {str(int(r["id"])) for r in base}
        self.grid.discard_keys([k for k in self.grid.selected() if k not in alive])
        self._backend = self.current_backend()
        self.grid.set_rows(rows)
        self._refresh_trees()
        self.lbl_hint.setText(
            f"共 {len(rows)} 个片段 · 后端：{self._backend} · 根：{self.material_root()}")
        self._update_sel_label()
        # 库刷完顺手提交待识别队列（仅已标记为口播素材的片段；开关/模型就绪才跑）
        self._maybe_asr()

    # ---------------- 筛选（本地切 + 树双向同步） ----------------
    def _filtered(self, base):
        btype, prod = self._picked()
        out = []
        for r in base:
            if btype:
                rt = (r.get("block_type") or "").strip()
                if btype == "未分类":
                    if rt:
                        continue
                elif rt != btype:
                    continue
            if prod:
                rp = (r.get("product") or "").strip()
                if prod == "未分类":
                    if rp:
                        continue
                elif rp != prod:
                    continue
            out.append(r)
        return self._sorted(out)

    @staticmethod
    def _count_pairs(rows, field, unbound_label="未分类"):
        """按 field 统计取值→计数；空值归「未分类」且置底。回 [(显示值, 计数)]。"""
        cnt = {}
        for r in rows:
            v = (r.get(field) or "").strip()
            key = v or unbound_label
            cnt[key] = cnt.get(key, 0) + 1
        named = sorted((k, c) for k, c in cnt.items() if k != unbound_label)
        if unbound_label in cnt:
            named = named + [(unbound_label, cnt[unbound_label])]
        return named

    def _refresh_trees(self):
        """用 base 重算两棵树（只在签名变了才重建，否则只回选）。"""
        base = self._base
        pairs_tag = self._count_pairs(base, "block_type")
        pairs_prod = self._count_pairs(base, "product")
        tree_sig = (len(base), tuple(pairs_tag), tuple(pairs_prod))
        cur_tag = "" if self.cb_type.currentIndex() <= 0 else self.cb_type.currentText()
        cur_prod = "" if self.cb_product.currentIndex() <= 0 else self.cb_product.currentText()
        if tree_sig != self._tree_sig:
            self._tree_sig = tree_sig
            self.panel.set_tag(pairs_tag, len(base), current=cur_tag)
            self.panel.set_product(pairs_prod, len(base), current=cur_prod)
        else:
            self.panel.select_tag(cur_tag)
            self.panel.select_product(cur_prod)

    def _set_combo(self, combo, value):
        if not value:
            combo.setCurrentIndex(0)
        else:
            idx = combo.findText(value)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def _on_type_combo(self):
        if self._tree_sync:
            return
        self._reload()

    def _on_product_combo(self):
        if self._tree_sync:
            return
        self._reload()

    def _on_tree_tag(self, value):
        self._tree_sync = True
        try:
            self._set_combo(self.cb_type, value)
        finally:
            self._tree_sync = False
        self._reload()

    def _on_tree_product(self, value):
        self._tree_sync = True
        try:
            self._set_combo(self.cb_product, value)
        finally:
            self._tree_sync = False
        self._reload()

    def _apply_tree_visible(self):
        self.left_card.setVisible(self._tree_visible)

    def _toggle_tree(self):
        self._tree_visible = not self._tree_visible
        from store import app_state
        app_state.set_value("material_tree_visible", self._tree_visible)
        self._apply_tree_visible()

    def _display(self, r):
        """卡片文案：名称=文件名去后缀；徽标=板块类型（充当标签）+ 口播；meta=时长·产品·日期。"""
        path = r["path"]
        name = Path(path).stem
        bt = r.get("block_type") or "片段"
        tags = f"🎬 {bt}"
        meta = (f"{_fmt_dur(r.get('duration'))} · {r.get('product') or '未分类'} · "
                f"{(r.get('created_at') or '')[5:10]}")
        tooltip = (f"{path}\n产品：{r.get('product') or '—'}  "
                   f"{r.get('created_at')}  [{self._backend}]")
        badge = "🎙 口播" if r.get("has_speech") else ""
        return {"name": name, "tags": tags, "meta": meta, "tooltip": tooltip,
                "badge": badge}

    def closeEvent(self, e):
        self.grid.stop_worker()
        if self._asr is not None and self._asr.isRunning():
            self._asr.request_stop()
            self._asr.wait(2000)
        if self._bubble is not None:
            self._bubble.hide()
        super().closeEvent(e)

    # ---------------- 交互 ----------------
    def _preview(self, r):
        path = r["path"]
        if not Path(path).exists():
            QMessageBox.information(self, "提示", "片段文件不存在或已被移动")
            return
        if Path(path).suffix.lower() in VID_EXT:
            self._media = VideoPlayerDialog(self, path)
            self._media.show()
        else:
            open_path(path)

    def _card_menu(self, r, global_pos):
        menu = StyledMenu(self)
        menu.addAction("👁 预览", lambda: self._preview(r))
        menu.addAction("📁 打开所在文件夹",
                       lambda: reveal_in_folder(r["path"]) if Path(r["path"]).exists()
                       else QMessageBox.information(self, "提示", "文件不存在"))
        menu.addSeparator()
        menu.addAction("🏷 设置板块标签…", lambda: self._do_set_block_type([r]))
        menu.addMenu(self._build_voiceover_menu([r]))
        menu.addSeparator()
        menu.addAction("🗑 删除索引", lambda: self._delete(r))
        menu.exec(global_pos)

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

    def ingest_files(self, paths):
        """右键「入库到素材库」落地：按盘上路径建索引行（已存则跳过），刷一次库。返回新入库数。

        素材库以 material_clips 行为准（不拷文件），所以只 add 索引；同一路径不重复入库。"""
        added = 0
        for p in (paths or []):
            if not p or not Path(str(p)).is_file():
                continue
            if material_store.find_by_path(p):
                continue
            if material_store.add(str(p)):
                added += 1
        if added:
            self.rebuild_filters()
            self._reload()
        return added

    # ---------------- 多选 / 批量操作 ----------------
    def _empty_menu(self):
        """空白处右键：最顶是多选开关，严格跟随当前多选态——
        未开显示「开启多选」，已在多选态显示「退出多选」（含已选 0/1 条的情况）。
        文案与实际状态一一对应，点击必定切换生效：不会出现「已开多选却仍显示开启多选、
        点了没反应」的失效，也不会「只显示开启却同时列出批量项」的自相矛盾。
        开启后才列 全选/反选/批量；排序常驻。

        注意：菜单项一律不用 setCheckable——windowsvista 会在项前画原生小圆框+对钩，
        很丑；当前状态靠文案表达（排序用「✓ 」文本前缀）。"""
        m = StyledMenu(self)
        if self.grid.multi_on():
            m.addAction("退出多选", lambda: self.grid.set_multi(False))
            m.addSeparator()
            m.addAction("全选", self._act_select_all)
            m.addAction("反选", self._act_invert)
            m.addSeparator()
            m.addAction("🏷 绑定产品…", self._act_bind)
            m.addAction("🏷 批量设置板块标签…", self._act_set_block_type)
            m.addMenu(self._build_voiceover_batch_menu())
            m.addAction("✏ 批量重命名…", self._act_rename)
            m.addAction("🗑 删除片段（进回收站）", self._act_delete)
        else:
            m.addAction("开启多选", lambda: self.grid.set_multi(True))
        m.addSeparator()
        m.addAction("🌲 " + ("隐藏筛选树" if self._tree_visible else "显示筛选树"),
                    self._toggle_tree)
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

    def _update_sel_label(self):
        n = len(self.grid.selected())
        self.lbl_sel.setText(f"已选 {n} 条")
        self.lbl_sel.setVisible(n > 0)
        # 多选/已勾选时，批量入口改名“批量操作”；setMenu 自带箭头，文本不写 ▾
        self.b_ops.setText("⚙ 批量操作" if (self.grid.multi_on() or n) else "⚙ 操作")

    def _act_select_all(self):
        self.grid.set_multi(True)
        self.grid.select_all()

    def _act_invert(self):
        """反选：已勾的去掉、没勾的勾上（多选未开时先自动开启）。"""
        self.grid.set_multi(True)
        self.grid.invert()

    def _clear_sel(self):
        self.grid.clear_sel()

    def _build_ops_menu(self):
        m = StyledMenu(self)
        m.addAction("🏷 绑定产品…", self._act_bind)
        m.addAction("🏷 批量设置板块标签…", self._act_set_block_type)
        m.addMenu(self._build_voiceover_batch_menu())
        m.addSeparator()
        m.addAction("✏ 批量重命名…", self._act_rename)
        m.addSeparator()
        m.addAction("🗑 删除片段（进回收站）", self._act_delete)
        return m

    def _require_sel(self):
        rows = self.grid.selected_rows()
        if not rows:
            QMessageBox.information(self, "先选几条",
                                    "先空白处拖动框选，或右键空白处→开启多选后点选要操作的片段。")
        return rows

    def _act_set_block_type(self):
        rows = self._require_sel()
        if not rows:
            return
        self._do_set_block_type(rows)

    def _do_set_block_type(self, rows):
        """弹板块类型选择框（从 block_categories 取预设），写入选中行。"""
        from core import block_categories
        cats = block_categories.load()
        items = cats + ["（清空标签）"]
        choice, ok = QInputDialog.getItem(
            self, "设置板块标签",
            f"为 {len(rows)} 个片段选择板块标签：", items, 0, False)
        if not ok:
            return
        bt = "" if choice == "（清空标签）" else choice
        material_store.set_block_types([int(r["id"]) for r in rows], bt)
        self._clear_sel()
        self.rebuild_filters()
        self._reload()
        self.lbl_hint.setText(f"已将 {len(rows)} 个片段标签设为「{bt or '未分类'}」")

    # ---------------- 口播素材标记 / 逐字稿（多级右键） ----------------
    def _build_voiceover_menu(self, rows):
        """「🎙 口播 ▸」子菜单（单卡传 [r]，行已捕获）：标记/取消、清除稿、查看。"""
        m = StyledMenu("🎙 口播", self)
        marked = all(int(r.get("is_voiceover") or 0) == 1 for r in rows if r)
        if marked:
            m.addAction("取消口播标记", lambda: self._set_voiceover(rows, False))
        else:
            m.addAction("标记为口播素材", lambda: self._set_voiceover(rows, True))
        if any((r.get("transcript") or "").strip() or int(r.get("transcribed") or 0)
               for r in rows):
            m.addAction("清除口播稿", lambda: self._clear_script(rows))
        if len(rows) == 1 and (rows[0].get("transcript") or "").strip():
            m.addSeparator()
            m.addAction("🎙 查看/复制口播", lambda: self._view_script(rows[0]))
        return m

    def _build_voiceover_batch_menu(self):
        """批量口播子菜单（静态 ops 菜单用）：点击时才取当前选中行。"""
        m = StyledMenu("🎙 口播", self)
        m.addAction("批量标记为口播素材", self._act_mark_voiceover)
        m.addAction("批量取消口播标记", self._act_unmark_voiceover)
        m.addAction("批量清除口播稿", self._act_clear_voiceover)
        return m

    def _set_voiceover(self, rows, flag):
        """批量标记/取消口播素材；标记后下次库刷新会自动排队跑 ASR。"""
        ids = [int(r["id"]) for r in rows if r]
        if not ids:
            return
        material_store.set_voiceover(ids, flag)
        self._clear_sel()
        self._reload()
        self.lbl_hint.setText(f"已标记 {len(ids)} 条为口播素材" if flag
                              else f"已取消 {len(ids)} 条口播标记")

    def _clear_script(self, rows):
        """清除选中片段的口播逐字稿（保留口播标记，只清文字）。"""
        ids = [int(r["id"]) for r in rows if r]
        if not ids:
            return
        material_store.clear_clip_transcript(ids)
        self._clear_sel()
        self._reload()
        self.lbl_hint.setText(f"已清除 {len(ids)} 条口播稿")

    def _act_mark_voiceover(self):
        rows = self._require_sel()
        if rows:
            self._set_voiceover(rows, True)

    def _act_unmark_voiceover(self):
        rows = self._require_sel()
        if rows:
            self._set_voiceover(rows, False)

    def _act_clear_voiceover(self):
        rows = self._require_sel()
        if rows:
            self._clear_script(rows)

    def _view_script(self, row):
        """右键「查看/复制口播」：只读弹框展示逐字稿（可全选复制）。"""
        text = (row.get("transcript") or "").strip()
        if not text:
            QMessageBox.information(self, "无口播", "这条未识别到口播（或尚未转写）。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"口播逐字稿 · {Path(str(row['path'])).stem}")
        dlg.resize(560, 460)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"共 {len(text)} 字"))
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text)
        v.addWidget(view, 1)
        row_b = QHBoxLayout()
        b_copy = QPushButton("📋 复制全部")
        b_copy.setObjectName("GhostBtn")
        b_copy.clicked.connect(lambda: (view.selectAll(), view.copy()))
        b_close = QPushButton("关闭")
        b_close.setObjectName("GhostBtn")
        b_close.clicked.connect(dlg.accept)
        row_b.addWidget(b_copy)
        row_b.addStretch(1)
        row_b.addWidget(b_close)
        v.addLayout(row_b)
        dlg.exec()

    # ---------------- 口播识别（增量后台 Whisper，仅已标记片段） ----------------
    def _maybe_asr(self):
        """库刷完提交待识别队列：只队「已标记口播素材且尚未转写」的片段；无就绪模型静默跳过。"""
        if self._asr is not None and self._asr.isRunning():
            return
        try:
            from video_text_tools.asr import transcribe as tr
            size = tr.best_ready_size()
        except Exception:
            size = None
        if not size:
            return                       # 无就绪模型：静默不跑
        todo = []
        for r in self._base:
            cid = int(r["id"])
            if (int(r.get("is_voiceover") or 0) == 1
                    and not int(r.get("transcribed") or 0)
                    and cid not in self._asr_failed and r.get("path")):
                todo.append((cid, r["path"]))
        if not todo:
            return
        self._asr = MaterialAsrWorker(size, todo, self)
        self._asr.done_one.connect(self._on_asr_done)
        self._asr.start()

    def _on_asr_done(self, clip_id, text, has_speech, model, elapsed, error):
        """worker 回主线程：失败不写库（下次不重试）；成功写 material_store + 对应卡片补徽标。"""
        clip_id = int(clip_id)
        if error:
            self._asr_failed.add(clip_id)
            return
        text = text or ""
        material_store.set_clip_transcript(clip_id, text, has_speech, model)
        for r in self._base:
            if int(r["id"]) == clip_id:
                r["transcribed"] = 1
                r["has_speech"] = 1 if (has_speech and text.strip()) else 0
                r["transcript"] = text
                break
        if has_speech and text.strip():
            self.grid.set_badge(str(clip_id), "🎙 口播")

    # ---------------- 徒标交互：悬停/点击看口播脚本 ----------------
    def _badge_hit(self, row, kind, badge):
        """卡片右上角 🎙 徽标被移入/点击：有脚本才弹。悬停→轻量浮窗，点击→完整弹窗。"""
        text = (row.get("transcript") or "").strip()
        if not text:
            return
        self.grid.cancel_hover()        # 不叠悬停视频预览
        if kind == "click":
            self._view_script(row)
            return
        from PySide6.QtCore import QPoint
        tl = badge.mapToGlobal(badge.rect().topRight())
        if self._bubble is None:
            from gui.pages_output_lib import _ScriptBubble
            self._bubble = _ScriptBubble(self)
        title = Path(str(row.get("path") or "")).stem
        self._bubble.show_for(title, text, QPoint(tl.x() + 6, tl.y()), anchor=badge)

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

    @staticmethod
    def _plain_rename(path, stem):
        """拖入的外部文件（不在素材库索引里）：只改盘上名，撞名挂 (2)。返回 (ok, 一句话)。"""
        import re
        if not path:
            return False, "无路径"
        p = Path(str(path))
        if not p.exists():
            return False, "文件不存在或已被移动"
        safe = re.sub(r'[\\/:*?"<>|]', "_", str(stem or "").strip()) or p.stem
        target = p.with_name(f"{safe}{p.suffix}")
        i = 2
        while target.exists() and target != p:
            target = p.with_name(f"{safe}({i}){p.suffix}")
            i += 1
        if target == p:
            return True, "名称未变化"
        try:
            p.rename(target)
            return True, ""
        except OSError as e:
            return False, f"重命名失败：{e}"

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
            if "id" in row:
                ok, _new, msg = material_store.rename(row["id"], stem)
            else:
                # 拖入的外部文件（未入库）：直接改盘上文件名，不动索引
                ok, msg = self._plain_rename(row.get("path"), stem)
            if ok:
                done += 1
            else:
                failed.append(f"{Path(str(row.get('path') or '')).name}：{msg}")
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
