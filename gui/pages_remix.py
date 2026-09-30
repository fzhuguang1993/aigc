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
                               QLineEdit, QAbstractItemView, QDialog, QDialogButtonBox)

from store import material_store, output_store
from utils.desktop_utils import open_path, reveal_in_folder
from gui.header import page_header, Card
from gui.widgets import VideoPlayerDialog
from gui.menus import StyledMenu

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


class _PickerDialog(QDialog):
    """通用多选挑选：items=[(label, clip)]；确认后回吐勾选的 clip 列表。"""

    def __init__(self, parent, title, items):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 460)
        v = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for label, clip in items:
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, clip)
            self.list.addItem(it)
        v.addWidget(self.list)
        tip = QLabel("Ctrl/Shift 多选 · 双击预览")
        v.addWidget(tip)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)
        self._picked = []

    def _accept(self):
        self._picked = [it.data(Qt.ItemDataRole.UserRole)
                        for it in self.list.selectedItems()]
        self.accept()

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
        b_export = QPushButton("▶ 导出混剪")
        b_export.setObjectName("PrimaryBtn")
        b_export.clicked.connect(self._export)
        b_clear = QPushButton("🗑 清空清单")
        b_clear.setObjectName("GhostBtn")
        b_clear.clicked.connect(self.list_clear)
        ops.addWidget(b_ai)
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
