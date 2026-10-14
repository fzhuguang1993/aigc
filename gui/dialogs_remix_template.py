"""
gui/dialogs_remix_template.py —— AI 混剪双模式：AI 自动拼装 + 自定义模板拼装

AI 自动拼装（AutoTemplateDialog）：按 block_categories 默认序列自动从素材池分组取 clip，
每类最多 N 条（duration 降序），把有序清单交给调用方。拼装信号 = material_clips.block_type
（拆解切割时 AI 已分配，无需新 ASR）。

自定义模板拼装（TemplateEditorDialog）：用户编排 block_type 段落序列，再逐槽从素材库
挑一个 clip。确认后按段顺序组装 clip 清单，载入 RemixPage 等待用户微调导出。

两个 Dialog 均以 clip dict 列表作数据源（调用方传入），不直接读库。
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QComboBox, QSpinBox, QListWidget,
                               QListWidgetItem, QToolButton, QInputDialog,
                               QDialogButtonBox, QWidget)


def _fmt_dur(sec):
    s = int(max(0, float(sec or 0)))
    return f"{s // 60:02d}:{s % 60:02d}"


def _clip_label(clip):
    if not clip:
        return ""
    return (clip.get("label") or Path(clip.get("path") or "").name)


# ============================================================
# AI 自动拼装
# ============================================================

class AutoTemplateDialog(QDialog):
    """AI 自动拼装：按 block_categories.load() 序列分组取素材，每类最多 N 条。"""

    def __init__(self, parent, all_clips, products=None):
        super().__init__(parent)
        self.setWindowTitle("AI 自动拼装")
        self.resize(420, 220)
        self._all_clips = list(all_clips)
        self._result = []
        from core import block_categories
        self._seq = block_categories.load()
        if products is None:
            products = sorted({(c.get("product") or "").strip()
                               for c in all_clips if (c.get("product") or "").strip()})

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(10)

        # 产品筛选
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("产品筛选："))
        self.cb_prod = QComboBox()
        self.cb_prod.addItems(["全部"] + list(products))
        self.cb_prod.currentIndexChanged.connect(self._update_preview)
        row1.addWidget(self.cb_prod, 1)
        v.addLayout(row1)

        # 每段最多取几条
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("每段最多选："))
        self._spin = QSpinBox()
        self._spin.setRange(1, 5)
        self._spin.setValue(2)
        self._spin.setSuffix(" 条")
        self._spin.valueChanged.connect(self._update_preview)
        row2.addWidget(self._spin)
        row2.addStretch(1)
        v.addLayout(row2)

        # 预览序列 + 总数
        self._lbl_preview = QLabel("")
        self._lbl_preview.setWordWrap(True)
        self._lbl_preview.setObjectName("PageTip")
        v.addWidget(self._lbl_preview)
        self._lbl_total = QLabel("")
        v.addWidget(self._lbl_total)

        # 确认 / 取消
        self._btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_btn = self._btns.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn:
            ok_btn.setText("确定 → 载入清单")
        self._btns.accepted.connect(self._do_accept)
        self._btns.rejected.connect(self.reject)
        v.addWidget(self._btns)

        self._update_preview()

    # ---- 内部分组 ----
    def _grouped(self, prod=None):
        g = {}
        for c in self._all_clips:
            bt = (c.get("block_type") or "").strip()
            if not bt:
                continue
            if prod and prod != "全部" and (c.get("product") or "").strip() != prod:
                continue
            g.setdefault(bt, []).append(c)
        return g

    def _update_preview(self):
        prod = self.cb_prod.currentText()
        max_n = self._spin.value()
        grouped = self._grouped(prod)
        parts, total = [], 0
        for bt in self._seq:
            n = min(len(grouped.get(bt, [])), max_n)
            if n:
                parts.append(f"{bt}×{n}")
                total += n
        self._lbl_preview.setText(" · ".join(parts) if parts else "该范围内没有可用素材")
        self._lbl_total.setText(f"将生成 {total} 段混剪清单")
        ok = self._btns.button(QDialogButtonBox.StandardButton.Ok)
        if ok:
            ok.setEnabled(total > 0)

    def _compute_result(self):
        """按 seq 顺序，每类取 duration 最长的前 N 条，返回有序 clip 列表。"""
        prod = self.cb_prod.currentText()
        max_n = self._spin.value()
        grouped = self._grouped(prod)
        result = []
        for bt in self._seq:
            clips = sorted(grouped.get(bt, []),
                           key=lambda c: float(c.get("duration") or 0), reverse=True)
            result.extend(clips[:max_n])
        self._result = result
        return result

    def _do_accept(self):
        self._compute_result()
        self.accept()

    def result_clips(self):
        return self._result


# ============================================================
# 自定义模板拼装
# ============================================================

class TemplateEditorDialog(QDialog):
    """自定义模板拼装：左列段落序列，右列当前槽候选素材，点选绑定。"""

    def __init__(self, parent, all_clips):
        super().__init__(parent)
        self.setWindowTitle("模板混剪（自定义拼装）")
        self.resize(860, 520)
        self._all_clips = list(all_clips)
        self._slots = []        # [{"block_type": str, "clip": dict|None}]
        self._current_idx = -1
        from core import block_categories
        self._available_types = block_categories.load()
        prods = sorted({(c.get("product") or "").strip()
                        for c in all_clips if (c.get("product") or "").strip()})

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        # 顶部工具行：产品筛选 + 清空选择
        bar = QHBoxLayout()
        bar.addWidget(QLabel("产品筛选："))
        self.cb_prod = QComboBox()
        self.cb_prod.addItems(["全部"] + prods)
        self.cb_prod.currentIndexChanged.connect(self._fill_candidates)
        bar.addWidget(self.cb_prod)
        bar.addStretch(1)
        b_clear = QPushButton("清空全部选择")
        b_clear.setObjectName("GhostBtn")
        b_clear.clicked.connect(self._clear_selections)
        bar.addWidget(b_clear)
        v.addLayout(bar)

        # 主体：左段列表 + 右候选列表
        body = QHBoxLayout()
        body.setSpacing(10)

        # 左：段落序列
        left_w = QWidget()
        left_v = QVBoxLayout(left_w)
        left_v.setContentsMargins(0, 0, 0, 0)
        left_v.addWidget(QLabel("模板段落（单击选中，右侧点素材绑定）："))
        self._slot_list = QListWidget()
        self._slot_list.setMaximumWidth(280)
        self._slot_list.currentRowChanged.connect(self._on_slot_changed)
        left_v.addWidget(self._slot_list, 1)

        slot_btns = QHBoxLayout()
        b_add = QPushButton("＋ 添加段落")
        b_add.setObjectName("GhostBtn")
        b_add.clicked.connect(self._add_slot)
        slot_btns.addWidget(b_add)
        slot_btns.addStretch(1)
        for txt, fn in (("\u25b2", lambda: self._move_slot(-1)),
                        ("\u25bc", lambda: self._move_slot(1)),
                        ("\u2715", lambda: self._remove_slot(self._current_idx))):
            b = QToolButton()
            b.setText(txt)
            b.setAutoRaise(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(fn)
            slot_btns.addWidget(b)
        left_v.addLayout(slot_btns)
        body.addWidget(left_w, 2)

        # 右：当前段候选
        right_w = QWidget()
        right_v = QVBoxLayout(right_w)
        right_v.setContentsMargins(0, 0, 0, 0)
        self._lbl_right = QLabel("候选素材（请先在左列选择一个段落）")
        self._lbl_right.setObjectName("PageTip")
        right_v.addWidget(self._lbl_right)
        self._cand_list = QListWidget()
        self._cand_list.itemClicked.connect(self._on_candidate_clicked)
        right_v.addWidget(self._cand_list, 1)
        body.addWidget(right_w, 3)
        v.addLayout(body, 1)

        # 底部：摘要 + 载入/取消
        bot = QHBoxLayout()
        self._lbl_summary = QLabel("模板为空")
        self._lbl_summary.setObjectName("PageTip")
        bot.addWidget(self._lbl_summary, 1)
        self._b_load = QPushButton("\u2713 载入混剪清单")
        self._b_load.setObjectName("PrimaryBtn")
        self._b_load.clicked.connect(self._do_accept)
        self._b_load.setEnabled(False)
        bot.addWidget(self._b_load)
        b_cancel = QPushButton("取消")
        b_cancel.setObjectName("GhostBtn")
        b_cancel.clicked.connect(self.reject)
        bot.addWidget(b_cancel)
        v.addLayout(bot)

        self._load_default_template()

    # ---- 初始化默认模板 ----
    def _load_default_template(self):
        """优先从 app_state 恢复上次模板，否则用 block_categories 前 6 项。"""
        from store import app_state
        try:
            saved = app_state.get("remix_template_last", [])
        except Exception:
            saved = []
        if isinstance(saved, list) and saved:
            self._slots = [{"block_type": str(bt), "clip": None}
                           for bt in saved if str(bt).strip()]
        else:
            self._slots = [{"block_type": bt, "clip": None}
                           for bt in self._available_types[:6]]
        self._rebuild_slot_list()

    # ---- 段列表 ----
    def _rebuild_slot_list(self):
        """重建左列（保留当前选中行），随后刷新候选面板。"""
        cur = self._slot_list.currentRow()
        self._slot_list.blockSignals(True)
        self._slot_list.clear()
        for i, slot in enumerate(self._slots):
            bt = slot["block_type"]
            clip = slot.get("clip")
            if clip:
                txt = f"{i + 1}. {bt}  \u2190  {_clip_label(clip)}"
            else:
                txt = f"{i + 1}. {bt}  （未选）"
            self._slot_list.addItem(QListWidgetItem(txt))
        new_row = cur if 0 <= cur < len(self._slots) else (0 if self._slots else -1)
        self._current_idx = new_row
        if new_row >= 0:
            self._slot_list.setCurrentRow(new_row)
        self._slot_list.blockSignals(False)
        self._fill_candidates()
        self._update_summary()

    def _on_slot_changed(self, row):
        self._current_idx = row
        self._fill_candidates()

    def _add_slot(self):
        choice, ok = QInputDialog.getItem(
            self, "添加段落", "选择板块类型：",
            self._available_types, 0, False)
        if not ok or not choice:
            return
        self._slots.append({"block_type": choice, "clip": None})
        idx = len(self._slots) - 1
        self._slot_list.blockSignals(True)
        self._slot_list.addItem(
            QListWidgetItem(f"{idx + 1}. {choice}  （未选）"))
        self._slot_list.blockSignals(False)
        self._slot_list.setCurrentRow(idx)   # 触发 _on_slot_changed → _fill_candidates
        self._update_summary()

    def _remove_slot(self, idx):
        if 0 <= idx < len(self._slots):
            self._slots.pop(idx)
            self._rebuild_slot_list()

    def _move_slot(self, delta):
        idx = self._current_idx
        new = idx + delta
        if 0 <= idx < len(self._slots) and 0 <= new < len(self._slots):
            self._slots[idx], self._slots[new] = self._slots[new], self._slots[idx]
            self._rebuild_slot_list()
            self._slot_list.setCurrentRow(new)

    def _clear_selections(self):
        for s in self._slots:
            s["clip"] = None
        self._rebuild_slot_list()

    # ---- 候选面板 ----
    def _fill_candidates(self):
        """按当前槽 block_type + 产品筛选重建右侧候选列表；当前已选 clip 前显示 ●。"""
        if not hasattr(self, "_cand_list"):
            return   # __init__ 安全期（_rebuild_slot_list 先于 _cand_list 创建时触发）
        if self._current_idx < 0 or self._current_idx >= len(self._slots):
            self._cand_list.clear()
            self._lbl_right.setText("候选素材（请先在左列选择一个段落）")
            return
        bt = self._slots[self._current_idx]["block_type"]
        prod = self.cb_prod.currentText()
        tip = f"候选素材（{bt}"
        if prod != "全部":
            tip += f" · {prod}"
        tip += "）："
        self._lbl_right.setText(tip)
        candidates = [c for c in self._all_clips
                      if (c.get("block_type") or "").strip() == bt
                      and (prod == "全部" or (c.get("product") or "").strip() == prod)]
        cur_clip = self._slots[self._current_idx].get("clip")
        cur_path = (cur_clip.get("path") or "") if cur_clip else ""
        self._cand_list.blockSignals(True)
        self._cand_list.clear()
        for c in candidates:
            dur = _fmt_dur(c.get("duration") or 0)
            marker = "\u25cf " if (cur_path and c.get("path") == cur_path) else "   "
            it = QListWidgetItem(f"{marker}{_clip_label(c)}  \u00b7  {dur}")
            it.setData(Qt.ItemDataRole.UserRole, c)
            it.setToolTip(c.get("path") or "")
            self._cand_list.addItem(it)
        self._cand_list.blockSignals(False)
        if not candidates:
            self._cand_list.addItem(QListWidgetItem("（该板块类型暂无素材）"))

    def _on_candidate_clicked(self, item):
        """单击右侧候选 → 绑定到当前槽，更新左列文字 + 候选选中标记。"""
        if self._current_idx < 0 or self._current_idx >= len(self._slots):
            return
        clip = item.data(Qt.ItemDataRole.UserRole)
        if clip is None:
            return   # "（暂无素材）"占位行，忽略
        self._slots[self._current_idx]["clip"] = clip
        # 只更新当前行文字，避免全量重建左列（保留选中滚动位置）
        bt = self._slots[self._current_idx]["block_type"]
        row_item = self._slot_list.item(self._current_idx)
        if row_item:
            row_item.setText(f"{self._current_idx + 1}. {bt}  \u2190  {_clip_label(clip)}")
        # 刷新候选标记（当前 clip 前加 ●）
        self._fill_candidates()
        self._update_summary()

    def _update_summary(self):
        n_filled = sum(1 for s in self._slots if s.get("clip"))
        n_total = len(self._slots)
        if n_total:
            self._lbl_summary.setText(f"共 {n_total} 段 · 已选 {n_filled} 段")
        else:
            self._lbl_summary.setText("模板为空")
        self._b_load.setEnabled(n_filled > 0)

    def _do_accept(self):
        """保存模板供下次默认恢复，接受对话框。"""
        from store import app_state
        try:
            app_state.set_value("remix_template_last",
                                [s["block_type"] for s in self._slots])
        except Exception:
            pass
        self.accept()

    def result_clips(self):
        """按段顺序返回已选 clip 列表（跳过未选段）。"""
        return [s["clip"] for s in self._slots if s.get("clip")]
