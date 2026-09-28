"""
gui/pages_breakdown.py —— 「拆解任务」管理页（左侧导航项）

卡片式浏览爆款拆解任务库：每条一张卡（封面缩略图 / 标题 / 分镜数·时长·日期 /
状态角标），点卡片或「展开」打开三屏联动详情页；右键可删除或在资源管理器定位。
数据取自 store.breakdown_store（DB），图集/封面取自持久任务库目录。

主窗每 2 秒调 refresh()：靠「行签名」判是否真变了，没变就不重建卡片（防闪烁/防抖）。
"""
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QLineEdit, QScrollArea, QGridLayout,
                               QSizePolicy, QMenu, QMessageBox, QFrame,
                               QDialog, QListWidget, QListWidgetItem)

from gui.header import page_header, FS_BLUE, FS_SUB, FS_WEAK

CARD_W = 184
COVER_H = 328          # 手机竖屏比例（≈9:16）：封面不被裁成横条，一眼认出是哪个任务
GRID_GAP = 14


def _fmt_dur(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return "—"
    return f"{sec // 60:02d}:{sec % 60:02d}"


class _TaskCard(QFrame):
    """单张任务卡：竖排 封面图（真 setPixmap） + 标题/元信息/入口提示。

    整卡可点开详情（mouseRelease）；右键出菜单（详情/定位/删除）。用 QFrame
    容器 + 两个 QLabel：封面用 pixmap（本地 Windows 路径靠富文本 <img> 不可靠）。"""

    def __init__(self, row, on_open, on_delete, prods=None, on_tag=None, parent=None):
        super().__init__(parent)
        self.row = row
        self.task_id = int(row.get("id") or 0)
        self._on_open = on_open
        self._on_delete = on_delete
        self._on_tag = on_tag
        self._prods = list(prods or [])
        self.setObjectName("BdCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(CARD_W, COVER_H + 112)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.cover = QLabel()
        self.cover.setFixedSize(CARD_W, COVER_H)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setStyleSheet("background:#EDF0F5; color:#8F959E; font-size:22px;"
                                 "border-top-left-radius:8px; border-top-right-radius:8px;")
        self.cover.setText("🎬")
        v.addWidget(self.cover)
        r = row
        title = (r.get("title") or "（无标题）")
        if len(title) > 22:
            title = title[:22] + "…"
        badge = " ⚠未完成" if r.get("status") == "partial" else ""
        meta = (f"分镜 {r.get('shot_count', 0)} · {_fmt_dur(r.get('duration'))} · "
                f"{(r.get('created_at') or '')[5:16]}")
        tags = "🏷 " + "、".join(self._prods) if self._prods else "🏷 未关联产品"
        if len(tags) > 30:
            tags = tags[:30] + "…"
        info = QLabel(f"<div style='padding:6px 10px;'>"
                      f"<span style='font-size:13px; font-weight:600; color:#1F2329;'>"
                      f"{title}</span>"
                      f"<span style='color:#D83931;'>{badge}</span>"
                      f"<br><span style='font-size:11px; color:#8F959E;'>{meta}</span>"
                      f"<br><span style='font-size:11px; color:#7C5CFF;'>{tags}</span></div>")
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setContentsMargins(0, 0, 0, 0)
        v.addWidget(info, 1)
        self._load_cover()

    def _load_cover(self):
        cover = self.row.get("cover") or ""
        if cover and Path(cover).exists():
            pix = QPixmap(cover)
            if not pix.isNull():
                self.cover.setPixmap(pix.scaled(
                    CARD_W, COVER_H, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation))
                self.cover.setText("")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self._on_open(self.task_id)
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        m = QMenu(self)
        m.addAction("🔎 打开详情", lambda: self._on_open(self.task_id))
        if self._on_tag:
            m.addAction("🏷 关联产品…", lambda: self._on_tag(self.task_id, self.row))
        m.addAction("📂 定位封面", self._reveal)
        m.addSeparator()
        m.addAction("🗑 删除任务", lambda: self._on_delete(self.task_id, self.row))
        m.exec(e.globalPos())

    def _reveal(self):
        cover = self.row.get("cover") or ""
        if cover and Path(cover).exists():
            from utils.desktop_utils import reveal_in_folder
            reveal_in_folder(cover)


class BreakdownTasksPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(8)
        lay.addWidget(page_header(
            "拆解任务", "爆款拆解历史库 · 点卡片看图集/播放器/文档三屏联动 · 右键删除", icon="🔥"))

        bar = QHBoxLayout()
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索标题…")
        self.ed_search.setFixedWidth(240)
        self.ed_search.textChanged.connect(lambda _=None: self._render())
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("PageTip")
        b_ref = QPushButton("🔄 刷新")
        b_ref.setObjectName("GhostBtn")
        b_ref.clicked.connect(self._reload)
        b_dir = QPushButton("📂 任务库目录")
        b_dir.setObjectName("GhostBtn")
        b_dir.clicked.connect(self._open_lib)
        bar.addWidget(self.ed_search)
        bar.addWidget(self.lbl_count)
        bar.addStretch(1)
        bar.addWidget(b_ref)
        bar.addWidget(b_dir)
        lay.addLayout(bar)

        self._area = QScrollArea()
        self._area.setWidgetResizable(True)
        self._area.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        self._holder = QWidget()
        self._grid = QGridLayout(self._holder)
        self._grid.setSpacing(GRID_GAP)
        self._grid.setContentsMargins(10, 14, 10, 14)
        self._area.setWidget(self._holder)
        lay.addWidget(self._area, 1)

        self._lbl_empty = QLabel("还没有拆解任务：到「工具中心 → 爆款拆解」跑一条，完成即自动入库。")
        self._lbl_empty.setObjectName("PageTip")
        self._lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_empty.setWordWrap(True)
        self._lbl_empty.setVisible(False)
        lay.addWidget(self._lbl_empty)

        self._rows = []
        self._cards = []
        self._cols = 0
        self._sig = None
        self._prods = {}
        self._reload()
        QTimer.singleShot(0, self._relayout)

    # ---------------- 数据 ----------------
    def refresh(self):
        """主窗 2 秒定时刷：读库→只在签名变化时重建卡片（防闪烁/防抖动）。"""
        self._reload()

    def _reload(self):
        try:
            from store import breakdown_store
            rows = breakdown_store.list_tasks(limit=300, only_ok=True)
        except Exception as e:
            self.lbl_count.setText(f"⚠ 读取失败：{e}")
            return
        try:
            self._prods = breakdown_store.products_map([r.get("id") for r in rows])
        except Exception:
            self._prods = {}
        sig = tuple((r.get("id"), r.get("status"), r.get("cover")) for r in rows) \
            + tuple((k, tuple(v)) for k, v in self._prods.items())
        self._rows = rows
        if sig != self._sig:
            self._sig = sig
            self._render()

    def _render(self):
        kw = (self.ed_search.text() or "").strip().lower()
        for c in self._cards:
            c.deleteLater()
        self._cards = []
        while self._grid.count():
            self._grid.takeAt(0)
        shown = 0
        for r in self._rows:
            names = (self._prods or {}).get(int(r.get("id") or 0), [])
            if kw and kw not in (r.get("title") or "").lower() \
                    and not any(kw in n.lower() for n in names):
                continue
            card = _TaskCard(r, self._open, self._delete, prods=names,
                             on_tag=self._tag_products, parent=self._holder)
            card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            self._cards.append(card)
            shown += 1
        self.lbl_count.setText(f"共 {len(self._rows)} 条 · 显示 {shown} 条")
        self._lbl_empty.setVisible(shown == 0)
        self._relayout()

    def _relayout(self):
        w = self._area.viewport().width()
        cols = max(1, (w - 8 + GRID_GAP) // (CARD_W + GRID_GAP))
        if cols != self._cols:
            self._cols = cols
        while self._grid.count():
            self._grid.takeAt(0)
        for i, card in enumerate(self._cards):
            self._grid.addWidget(card, i // cols, i % cols,
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._grid.setRowStretch(self._grid.rowCount(), 1)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._relayout)

    # ---------------- 动作 ----------------
    def _open(self, task_id):
        from gui.dialogs_breakdown_detail import open_breakdown_detail
        open_breakdown_detail(self.window() or self, task_id)

    def _delete(self, task_id, row):
        if QMessageBox.question(
                self, "删除拆解任务",
                f"确定删除「{row.get('title') or '（无标题）'}」？\n"
                "（只删记录，已抽出的图集/封面文件保留在任务库目录）"
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            from store import breakdown_store
            breakdown_store.delete(task_id)
            self._sig = None
            self._reload()
        except Exception as e:
            QMessageBox.warning(self, "删除失败", str(e))

    def _tag_products(self, task_id, row):
        """右键「关联产品」：多选把任务归到产品名下，按产品沉淀爆款拆解样本。"""
        from store import breakdown_store
        try:
            current = set(breakdown_store.products_of(task_id))
        except Exception:
            current = set()
        ids = _pick_products(self, current)
        if ids is None:                       # 取消，不改
            return
        try:
            breakdown_store.set_products(task_id, ids)
            self._sig = None
            self._reload()
        except Exception as e:
            QMessageBox.warning(self, "关联产品失败", str(e))

    def _open_lib(self):
        try:
            from core.config import BREAKDOWN_LIBRARY
            Path(BREAKDOWN_LIBRARY).mkdir(parents=True, exist_ok=True)
            from utils.desktop_utils import open_path
            open_path(BREAKDOWN_LIBRARY)
        except Exception:
            pass


def _pick_products(parent, current_ids):
    """产品多选框：勾选=关联到该拆解任务。返回选中的 id 列表，取消返回 None。"""
    from store import product_store
    items = product_store.list_products(product_store.TYPE_PRODUCT)
    current_ids = set(current_ids or ())
    dlg = QDialog(parent)
    dlg.setWindowTitle("关联产品（可多选）")
    dlg.resize(320, 440)
    v = QVBoxLayout(dlg)
    tip = QLabel("勾选归属的产品：不同产品打法不同，按产品沉淀爆款拆解样本。")
    tip.setWordWrap(True)
    v.addWidget(tip)
    lst = QListWidget()
    lst.setSelectionMode(QListWidget.SelectionMode.NoSelection)
    for it in items:
        row = QListWidgetItem(it["name"])
        row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        row.setData(Qt.ItemDataRole.UserRole, int(it["id"]))
        row.setCheckState(Qt.CheckState.Checked if int(it["id"]) in current_ids
                          else Qt.CheckState.Unchecked)
        lst.addItem(row)
    if not items:
        lst.addItem("（产品中心还没有产品，请先到「产品中心」新增）")
    v.addWidget(lst, 1)
    bb = QHBoxLayout()
    bb.addStretch(1)
    b_ok = QPushButton("保存")
    b_ok.setDefault(True)
    b_ok.clicked.connect(dlg.accept)
    b_no = QPushButton("取消")
    b_no.setObjectName("GhostBtn")
    b_no.clicked.connect(dlg.reject)
    bb.addWidget(b_ok)
    bb.addWidget(b_no)
    v.addLayout(bb)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    picked = []
    for i in range(lst.count()):
        it = lst.item(i)
        if (it.flags() & Qt.ItemFlag.ItemIsUserCheckable
                and it.checkState() == Qt.CheckState.Checked):
            pid = int(it.data(Qt.ItemDataRole.UserRole) or 0)
            if pid:
                picked.append(pid)
    return picked
