"""
gui/pages_products.py —— 产品中心（树状素材库）
左侧树：分组 → 品名/KOL → 素材类型 → 文件，双击文件即可预览。
右侧详情：三类素材以相册式手风琴分区展示（默认展开参考图，展开一个自动收起其余）。
提交任务时按品名自动取参考图；KOL 按名称取形象图。
"""
import tempfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QPixmap, QColor, QIcon, QKeySequence
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QSplitter, QInputDialog,
                               QLineEdit, QMessageBox, QFileDialog, QMenu, QListWidget,
                               QListWidgetItem, QAbstractItemView, QApplication)

from store import product_store as ps
from utils.desktop_utils import open_path, reveal_in_folder
from gui.header import page_header, Card
from gui.widgets import VideoPlayerDialog, ImagePreviewDialog

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VID_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUD_EXT = {".mp3", ".wav", ".aac", ".m4a", ".flac"}
_KINDS = [("image", "🖼", "参考图片"), ("video", "🎬", "参考视频"), ("audio", "🔊", "音频")]
# 相册分区（手风琴）：收起时只留一行缩略宫格，展开时向下撑成更大宫格；同时只展开一个
ALBUM_COLLAPSED_H = 108
ALBUM_EXPANDED_H = 288
ALBUM_HEAD_QSS = (
    "QPushButton{background:transparent;border:none;font-size:13px;"
    "font-weight:700;color:#1F2329;text-align:left;padding:2px 0;}"
    "QPushButton:hover{color:#3370FF;}")
_KIND_FIELD = {"image": "images", "video": "videos", "audio": "audios"}
_KIND_FILTER = {"image": "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)",
                "video": "视频文件 (*.mp4 *.mov *.avi *.mkv *.webm)",
                "audio": "音频文件 (*.mp3 *.wav *.aac *.m4a *.flac)"}


def _files_of(p, kind):
    return [x for x in (p[_KIND_FIELD[kind]] or "").split(";") if x.strip()]


def _kind_by_ext(path):
    ext = Path(path).suffix.lower()
    if ext in IMG_EXT:
        return "image"
    if ext in VID_EXT:
        return "video"
    if ext in AUD_EXT:
        return "audio"
    return None


def _drop_paths(e):
    return [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]


def _media_tile(kind):
    """视频/音频相册格子的占位缩略：圆角色块 + 字形，
    不解码首帧（不依赖 ffmpeg/网络，offscreen 也不会崩）"""
    from PySide6.QtGui import QPainter, QFont
    from PySide6.QtCore import QRect
    colors = {"video": "#3370FF", "audio": "#7F3FBF"}
    glyphs = {"video": "\U0001F3AC", "audio": "\U0001F50A"}
    pm = QPixmap(96, 72)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(colors.get(kind, "#8F959E")))
    p.drawRoundedRect(0, 0, 96, 72, 8, 8)
    p.setPen(QColor("white"))
    f = QFont()
    f.setPointSize(26)
    p.setFont(f)
    p.drawText(QRect(0, 0, 96, 72), Qt.AlignmentFlag.AlignCenter,
               glyphs.get(kind, ""))
    p.end()
    return pm


class DragTree(QTreeWidget):
    """接受从资源管理器拖入的素材文件：拖到哪条链路就登记到哪个产品"""
    files_dropped = Signal(list, object)     # paths, role（可为 None）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        if not e.mimeData().hasUrls():
            return
        pos = e.position().toPoint() if hasattr(e, "position") else e.pos()
        it = self.itemAt(pos)
        role = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        e.acceptProposedAction()
        self.files_dropped.emit(_drop_paths(e), role)


class DropList(QListWidget):
    """右侧素材区：拖入即登记到当前产品的对应类型"""
    files_dropped = Signal(list, object)

    def __init__(self, kind, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.files_dropped.emit(_drop_paths(e), self.kind)


class ProductsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 14, 24, 12)
        lay.setSpacing(8)

        lay.addWidget(page_header("产品中心",
                                  "树状管理素材 · 双击预览 · 拖拽入库 · 剪贴板 Ctrl+V 粘贴图片",
                                  icon="🧩"))

        bar = QHBoxLayout()
        b_p = QPushButton("＋ 新建产品")
        b_k = QPushButton("＋ 新建 KOL")
        b_del = QPushButton("🗑 删除当前条目")
        b_del.setObjectName("GhostBtn")
        bar.addWidget(b_p)
        bar.addWidget(b_k)
        bar.addWidget(b_del)
        bar.addStretch(1)
        self.lbl_hint = QLabel("")
        self.lbl_hint.setObjectName("PageTip")
        bar.addWidget(self.lbl_hint)
        # 工具条收进一张白卡（对标数据中台的分区卡），不再裸摆在灰底上
        ctrl = Card(margins=(14, 10, 14, 10))
        ctrl.v.addLayout(bar)
        lay.addWidget(ctrl)

        split = QSplitter()
        lay.addWidget(split, 1)

        # ---------- 左：素材树 ----------
        self.tree = DragTree()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.setMinimumWidth(260)
        self.tree.setIndentation(14)
        # macOS 默认会在选中项上画焦点虚框，关掉后统一用 QSS 圆角高亮
        self.tree.setAttribute(Qt.WidgetAttribute.WA_MacShowFocusRect, False)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.currentItemChanged.connect(self._on_pick)
        self.tree.itemDoubleClicked.connect(self._on_double)
        self.tree.files_dropped.connect(self._on_drop)
        # 素材树包进白卡：与右侧详情卡对齐，左右两栏都是白底圆角同一层次
        tree_card = Card(margins=(8, 8, 8, 8))
        tree_card.v.addWidget(self.tree)
        tree_card.setMinimumWidth(260)
        split.addWidget(tree_card)

        # ---------- 右：紧凑详情 ----------
        # 顶部内边距压到与左侧素材树首行齐平（两侧均在同一 QSplitter 里，
        # 外框顶部本就对齐；详情卡片默认上边距偏大会把首行文字顶下去，看着不齐）
        detail = Card(margins=(18, 8, 18, 14))
        split.addWidget(detail)
        self.detail = detail

        head = QHBoxLayout()
        self.lbl_name = QLabel("← 从左侧选择一个产品或 KOL")
        self.lbl_name.setStyleSheet("font-size:16px; font-weight:700; color:#1F2329; background:transparent;")
        self.lbl_type = QLabel("")
        head.addWidget(self.lbl_name)
        head.addWidget(self.lbl_type)
        head.addStretch(1)
        detail.v.addLayout(head)

        row = QHBoxLayout()
        t = QLabel("备注")
        t.setStyleSheet("font-size:12px; color:#646A73; font-weight:600; background:transparent;")
        row.addWidget(t)
        self.ed_note = QLineEdit()
        self.ed_note.setPlaceholderText("产品卖点 / 使用注意（可选，失焦自动保存）")
        self.ed_note.editingFinished.connect(self._save_note)
        row.addWidget(self.ed_note, 1)
        detail.v.addLayout(row)
        self.note_row = row

        # 三类素材：相册化手风琴分区（▸ 收起成一行缩略宫格，▾ 展开成更大宫格，同时只展开一个）
        self.sections = {}
        self.album = {}
        for kind, icon, label in _KINDS:
            sec = QVBoxLayout()
            sec.setSpacing(4)
            hs = QHBoxLayout()
            hs.setContentsMargins(0, 0, 0, 0)
            head = QPushButton()
            head.setObjectName("AlbumHead")
            head.setStyleSheet(ALBUM_HEAD_QSS)
            head.setCursor(Qt.CursorShape.PointingHandCursor)
            head.clicked.connect(lambda _=False, k=kind: self._toggle_album(k))
            hs.addWidget(head, 1)
            b_add = QPushButton("＋ 添加")
            b_add.setObjectName("GhostBtn")
            b_add.clicked.connect(lambda _=False, k=kind: self._add_files(k))
            hs.addWidget(b_add)
            if kind == "image":
                b_paste = QPushButton("📋 粘贴图片")
                b_paste.setObjectName("GhostBtn")
                b_paste.setToolTip("从剪贴板粘贴图片（截图/复制的图片文件都行），等同 Ctrl+V")
                b_paste.clicked.connect(lambda _=False: self.paste_image())
                hs.addWidget(b_paste)
            sec.addLayout(hs)
            w = DropList(kind)
            w.setViewMode(QListWidget.ViewMode.IconMode)
            w.setIconSize(QSize(96, 72))
            w.setGridSize(QSize(118, 108))
            w.setResizeMode(QListWidget.ResizeMode.Adjust)
            w.setMovement(QListWidget.Movement.Static)
            w.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            w.setWordWrap(True)
            w.setToolTip("可直接把文件拖到这里登记，图片区支持 Ctrl+V 粘贴剪贴板图片 · 点上方标题栏展开/收起")
            w.files_dropped.connect(lambda paths, k=kind: self._on_drop(paths, ("kind", self._pid, k)))
            w.itemDoubleClicked.connect(lambda it: self._preview_item(it))
            sec.addWidget(w)
            detail.v.addLayout(sec)
            self.sections[kind] = w
            self.album[kind] = {"head": head, "open": (kind == "image"), "n": 0,
                                "icon": icon, "label": label}
            self._apply_album_height(kind)
            self._album_head_text(kind, 0)
        detail.v.addStretch(1)
        self._pid = None
        self._media = None      # 预览窗口引用防 GC

        split.setSizes([280, 720])
        b_p.clicked.connect(lambda: self._create(ps.TYPE_PRODUCT))
        b_k.clicked.connect(lambda: self._create(ps.TYPE_KOL))
        b_del.clicked.connect(self._delete_current)
        # 页面本体也接受拖放：落在树/宫格以外的空白区时兜底登记到当前选中产品
        self.setAcceptDrops(True)
        self.rebuild()

    # ================= 树 =================
    def refresh(self):
        """定时刷新：只在素材库真变化时重建，避免每 2 秒收起展开态"""
        rows = ps.list_products()
        sig = (len(rows), max([(r["updated_at"] or "") for r in rows], default=""))
        if sig != getattr(self, "_sig", None):
            self._sig = sig
            self.rebuild(keep=self.current_id())

    def rebuild(self, keep=None):
        cur = keep if keep is not None else self.current_id()
        exp = self._expanded_roles()        # 记住重建前的展开态，重建后恢复
        self.tree.blockSignals(True)
        self.tree.clear()
        for ptype, glabel in [(ps.TYPE_PRODUCT, "📦 产品"), (ps.TYPE_KOL, "🎤 KOL 形象")]:
            items = ps.list_products(ptype)
            root = QTreeWidgetItem([f"{glabel}（{len(items)}）"])
            root.setData(0, Qt.ItemDataRole.UserRole, ("root", ptype))
            f = root.font(0); f.setBold(True); root.setFont(0, f)
            self.tree.addTopLevelItem(root)
            for p in items:
                node = QTreeWidgetItem([p["name"]])
                node.setData(0, Qt.ItemDataRole.UserRole, ("item", p["id"]))
                node.setToolTip(0, "双击展开/收起 · 右键添加 · 也可直接拖入素材文件")
                root.addChild(node)
                for kind, icon, label in _KINDS:
                    files = _files_of(p, kind)
                    kn = QTreeWidgetItem([f"{icon} {label}（{len(files)}）"])
                    kn.setData(0, Qt.ItemDataRole.UserRole, ("kind", p["id"], kind))
                    kn.setToolTip(0, "右键添加 · 支持拖入文件（按扩展名自动归类）")
                    node.addChild(kn)
                    for fpath in files:
                        fn = QTreeWidgetItem([f"　{Path(fpath).name}"])
                        fn.setData(0, Qt.ItemDataRole.UserRole, ("file", p["id"], kind, fpath))
                        fn.setToolTip(0, "双击预览 · 右键更多操作")
                        kn.addChild(fn)
                node.setExpanded(("item", p["id"]) in exp)
            # 首次构建无展开记录：产品少时默认展开方便浏览；
            # 有过记录则完全尊重用户状态（否则重建后树会“收缩且展不开”）
            if not exp:
                root.setExpanded(len(items) <= 6)
            else:
                root.setExpanded(("root", ptype) in exp)
        self.tree.blockSignals(False)
        # 恢复选中，并把选中节点的整条祖先链展开（定时重建后视图不“丢”）
        if cur is not None:
            node = self._find_item(cur)
            if node:
                self.tree.setCurrentItem(node)
                it = node.parent()              # 只展开祖先链；节点自身收起状态尊重用户
                while it is not None:
                    it.setExpanded(True)
                    it = it.parent()
                if self.tree.currentItem() is None:   # blockSignals 期间不触发 _on_pick
                    self._show_detail(ps.get_product(cur))
        elif self.tree.topLevelItemCount():
            self.tree.expandAll()

    def _expanded_roles(self):
        """收集当前树里处于展开态的节点 role（root/item），用于重建后恢复"""
        out = set()

        def walk(it):
            for i in range(it.childCount()):
                ch = it.child(i)
                if ch.isExpanded():
                    role = ch.data(0, Qt.ItemDataRole.UserRole)
                    if role and role[0] in ("root", "item"):
                        out.add(tuple(role))
                walk(ch)
        walk(self.tree.invisibleRootItem())
        return out

    def _find_item(self, pid):
        def walk(it):
            for i in range(it.childCount()):
                ch = it.child(i)
                role = ch.data(0, Qt.ItemDataRole.UserRole)
                if role[0] == "item" and role[1] == pid:
                    return ch
                got = walk(ch)
                if got:
                    return got
            return None
        return walk(self.tree.invisibleRootItem())

    def current_id(self):
        it = self.tree.currentItem()
        if it is None:
            return None
        role = it.data(0, Qt.ItemDataRole.UserRole)
        if not role:
            return None
        return role[1]           # item / kind / file 的第二元素都是 pid

    # ================= 详情 =================
    def _on_pick(self, cur, prev=None):
        role = cur.data(0, Qt.ItemDataRole.UserRole) if cur else None
        if not role or role[0] == "root":
            self._pid = None
            if role and role[0] == "root":
                self.tree.expandItem(cur)
            return
        pid = role[1]
        if role[0] == "file":
            pid, path = role[1], role[3]
            self._show_detail(ps.get_product(pid), focus=path)
            return
        self._show_detail(ps.get_product(pid))

    def _show_detail(self, p, focus=None):
        self._pid = p["id"] if p else None
        if not p:
            self.lbl_name.setText("← 从左侧选择一个产品或 KOL")
            self.lbl_type.setText("")
            self.ed_note.blockSignals(True); self.ed_note.clear(); self.ed_note.blockSignals(False)
            for kind, _, _ in _KINDS:
                self.sections[kind].clear()
                self.album[kind]["n"] = 0
                self.album[kind]["open"] = (kind == "image")
                self._apply_album_height(kind)
                self._album_head_text(kind, 0)
            return
        self.lbl_name.setText(p["name"])
        self.lbl_type.setText("产品" if p["type"] == ps.TYPE_PRODUCT else "KOL")
        self.lbl_type.setStyleSheet(
            "background:#EAF1FF; color:#3370FF; border-radius:9px; padding:2px 10px;"
            if p["type"] == ps.TYPE_PRODUCT else
            "background:#F1EAFE; color:#7F3FBF; border-radius:9px; padding:2px 10px;")
        self.ed_note.blockSignals(True)
        self.ed_note.setText(p["note"] or "")
        self.ed_note.blockSignals(False)
        for kind, _, _ in _KINDS:
            w = self.sections[kind]
            files = _files_of(p, kind)
            w.clear()
            for fpath in files:
                name = Path(fpath).name
                it = QListWidgetItem(name)
                it.setData(Qt.ItemDataRole.UserRole, fpath)
                it.setToolTip(fpath)
                it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
                if kind == "image" and Path(fpath).suffix.lower() in IMG_EXT and Path(fpath).exists():
                    pm = QPixmap(fpath).scaled(96, 72, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation)
                    it.setIcon(QIcon(pm))
                elif kind in ("video", "audio"):
                    it.setIcon(QIcon(_media_tile(kind)))
                if focus and fpath == focus:
                    w.setCurrentItem(it)
                w.addItem(it)
            if not files:
                empty = QListWidgetItem("（暂无，点右上角「＋ 添加」）")
                empty.setFlags(Qt.ItemFlag.NoItemFlags)
                empty.setForeground(QColor("#8F959E"))
                w.addItem(empty)
            self.album[kind]["n"] = len(files)
            # 选中新品回到默认手风琴态：参考图展开、其余收起
            self.album[kind]["open"] = (kind == "image")
            self._apply_album_height(kind)
            self._album_head_text(kind, len(files))

    def _save_note(self):
        if self._pid is not None:
            ps.set_note(self._pid, self.ed_note.text().strip())
            self.lbl_hint.setText("备注已保存")

    # ================= 相册分区：展开/收起 =================
    def _apply_album_height(self, kind):
        a = self.album[kind]
        self.sections[kind].setFixedHeight(
            ALBUM_COLLAPSED_H if not a["open"] else ALBUM_EXPANDED_H)

    def _album_head_text(self, kind, n):
        a = self.album[kind]
        a["head"].setText(("▾ " if a["open"] else "▸ ")
                          + f"{a['icon']} {a['label']}（{n}）")

    def _toggle_album(self, kind):
        # 手风琴：点亮当前分区就收起其余；再点已展开的那个则全收起
        a = self.album[kind]
        target = not a["open"]
        for k in self.album:
            self.album[k]["open"] = (k == kind and target)
            self._apply_album_height(k)
            self._album_head_text(k, self.album[k]["n"])

    # ================= 预览 / 文件操作 =================
    def _on_double(self, it, col):
        role = it.data(0, Qt.ItemDataRole.UserRole)
        if role and role[0] == "file":
            self._preview(role[3])
        elif role and role[0] == "item":
            it.setExpanded(not it.isExpanded())

    def _preview_item(self, it):
        path = it.data(Qt.ItemDataRole.UserRole)
        if path:
            self._preview(path)

    def _preview(self, path):
        p = Path(path)
        if not p.exists():
            QMessageBox.warning(self, "文件不存在", f"找不到文件：\n{path}")
            return
        if p.suffix.lower() in IMG_EXT:
            self._media = ImagePreviewDialog(self, str(p))
        else:
            self._media = VideoPlayerDialog(self, str(p))   # 视频/音频都走播放器
        self._media.show()

    def _open_system(self, path):
        if Path(path).exists():
            open_path(path)
        else:
            QMessageBox.information(self, "提示", "文件不存在或已被移动")

    def _open_location(self, path):
        if Path(path).exists():
            reveal_in_folder(path)
        else:
            QMessageBox.information(self, "提示", "文件不存在或已被移动")

    def _add_files(self, kind, pid=None):
        pid = pid if pid is not None else self.current_id()
        p = ps.get_product(pid) if pid else None
        if not p:
            QMessageBox.information(self, "提示", "请先在左侧选择一个产品/KOL")
            return
        paths, _ = QFileDialog.getOpenFileNames(self, f"添加{dict((k, l) for k, _, l in _KINDS)[kind]}",
                                                "", _KIND_FILTER[kind])
        if not paths:
            return
        saved = ps.add_files(pid, kind, paths)
        dup = len(paths) - len(saved)
        self.rebuild(keep=pid)
        self.lbl_hint.setText(f"已添加 {len(saved)} 个文件到「{p['name']}」"
                              + (f"（{dup} 个失败）" if dup else ""))

    def _remove_file(self, pid, kind, path):
        if QMessageBox.question(self, "移除登记",
                                f"从素材库移除「{Path(path).name}」？\n\n"
                                "仅移除登记，磁盘上的原文件不会被删除。") \
                != QMessageBox.StandardButton.Yes:
            return
        ps.remove_file(pid, kind, path)
        self.rebuild(keep=pid)

    # ================= 拖拽入库 =================
    def _on_drop(self, paths, role):
        """拖入文件：目标产品取自落点节点（file/kind/item），类型按扩展名自动判定；
        落点无法判断类型时才用拖放位置的类型兜底"""
        pid, fallback_kind = None, None
        if isinstance(role, tuple) and role:
            if role[0] == "file":
                pid, fallback_kind = role[1], role[2]
            elif role[0] == "kind":
                pid, fallback_kind = role[1], role[2]
            elif role[0] == "item":
                pid = role[1]
        elif isinstance(role, str):          # DropList 直接传 kind
            fallback_kind = role
        if pid is None:
            pid = self.current_id()
        p = ps.get_product(pid) if pid is not None else None
        if not p:
            QMessageBox.information(self, "提示",
                                    "请先在左侧选中一个产品/KOL，再把文件拖进来（或拖到它的节点上）")
            return
        by_kind = {"image": [], "video": [], "audio": []}
        unknown = 0
        for fpath in paths:
            if not fpath or not Path(fpath).is_file():
                continue
            kind = _kind_by_ext(fpath) or fallback_kind
            if kind:
                by_kind[kind].append(fpath)
            else:
                unknown += 1
        saved, failed = 0, 0
        for kind, plist in by_kind.items():
            if plist:
                n = len(ps.add_files(p["id"], kind, plist))
                saved += n
                failed += len(plist) - n
        self.rebuild(keep=p["id"])
        self._sig = None                    # 强制下次定时刷新重算签名
        msg = f"已拖入「{p['name']}」：登记 {saved} 个文件"
        if failed:
            msg += f"，{failed} 个复制失败"
        if unknown:
            msg += f"，{unknown} 个格式不支持已跳过（仅图片/视频/音频）"
        self.lbl_hint.setText(msg)

    # ================= 剪贴板粘贴图片 =================
    def paste_image(self, pid=None):
        """把剪贴板里的图片粘进当前产品的参考图：
        - 资源管理器里复制的图片文件 → 直接登记
        - 截图 / 图片软件复制的位图 → 落成 PNG 再入库"""
        pid = pid if pid is not None else self.current_id()
        p = ps.get_product(pid) if pid is not None else None
        if not p:
            QMessageBox.information(self, "提示", "请先在左侧选择一个产品/KOL，再粘贴图片")
            return
        cb = QApplication.clipboard()
        mime = cb.mimeData()
        img_paths = [u.toLocalFile() for u in mime.urls()
                     if u.isLocalFile() and _kind_by_ext(u.toLocalFile()) == "image"]
        if img_paths:
            saved = ps.add_files(p["id"], "image", img_paths)
        elif mime.hasImage():
            img = cb.image()
            if img.isNull():
                self.lbl_hint.setText("剪贴板里的图片是空的")
                return
            tmp = Path(tempfile.gettempdir()) / (
                f"aigc_paste_{datetime.now():%Y%m%d_%H%M%S_%f}.png")
            if not img.save(str(tmp), "PNG"):
                self.lbl_hint.setText("剪贴板图片保存失败")
                return
            saved = ps.add_files(p["id"], "image", [str(tmp)])
            try:
                tmp.unlink()
            except OSError:
                pass
        else:
            QMessageBox.information(
                self, "没有图片",
                "剪贴板里没有图片。\n\n先复制一张图片文件，或截屏（Win+Shift+S），再按 Ctrl+V。")
            return
        if not saved:
            self.lbl_hint.setText("粘贴失败：图片没能入库")
            return
        self.rebuild(keep=p["id"])
        self._sig = None
        self.lbl_hint.setText(f"已粘贴 {len(saved)} 张图片到「{p['name']}」")

    # ---- 页面级拖放兜底（子控件已各自处理，这里只接空白区） ----
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._on_drop(_drop_paths(e), None)

    def keyPressEvent(self, e):
        # Ctrl+V 粘贴图片：备注框等文本控件会把 Ctrl+V 吃下自己粘文本，
        # 不会传到这层；树/宫格/按钮不吃这个键，才会递上来触发图片粘贴
        if e.matches(QKeySequence.StandardKey.Paste):
            self.paste_image()
            return
        super().keyPressEvent(e)

    # ================= 右键菜单 =================
    def _tree_menu(self, pos):
        it = self.tree.itemAt(pos)
        menu = QMenu(self)
        role = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        if role and role[0] == "file":
            _, pid, kind, path = role
            menu.addAction("👁 预览", lambda: self._preview(path))
            menu.addAction("📂 打开文件", lambda: self._open_system(path))
            menu.addAction("📁 打开所在文件夹", lambda: self._open_location(path))
            menu.addSeparator()
            menu.addAction("✂ 从登记中移除", lambda: self._remove_file(pid, kind, path))
        elif role and role[0] == "kind":
            _, pid, kind = role
            menu.addAction("＋ 添加文件", lambda: self._add_files(kind, pid))
        elif role and role[0] == "item":
            pid = role[1]
            for kind, icon, label in _KINDS:
                menu.addAction(f"{icon} 添加{label}", lambda _=False, k=kind: self._add_files(k, pid))
            menu.addSeparator()
            menu.addAction("✏ 重命名", lambda: self._rename(pid))
            menu.addAction("🗑 删除该条目", lambda: self._delete(pid))
        elif role and role[0] == "root":
            menu.addAction("＋ 新建", lambda: self._create(role[1]))
        if menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ================= 新建 / 改名 / 删除 =================
    def _create(self, ptype):
        what = "产品" if ptype == ps.TYPE_PRODUCT else "KOL"
        name, ok = QInputDialog.getText(self, f"新建{what}", f"{what}名称：")
        name = name.strip()
        if not ok or not name:
            return
        if ps.get_by_name(name, ptype):
            QMessageBox.warning(self, "已存在", f"「{name}」已经存在了")
            return
        pid = ps.add_product(name, ptype)
        self.rebuild(keep=pid)
        self.lbl_hint.setText(f"已创建{what}「{name}」，右键它可添加素材")

    def _rename(self, pid):
        p = ps.get_product(pid)
        if not p:
            return
        what = "产品" if p["type"] == ps.TYPE_PRODUCT else "KOL"
        name, ok = QInputDialog.getText(self, f"重命名{what}",
                                        f"新名称（素材文件夹与相关任务的品名会一并更新）：",
                                        text=p["name"])
        name = name.strip()
        if not ok or not name:
            return
        try:
            changed = ps.rename_product(pid, name)
        except ValueError as e:
            QMessageBox.warning(self, "无法改名", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "改名失败", str(e))
            return
        self.rebuild(keep=pid if changed else None)
        self._sig = None
        if changed:
            self.lbl_hint.setText(f"已改名为「{name}」，素材文件与相关任务已同步")

    def _delete_current(self):
        pid = self.current_id()
        if pid is None:
            QMessageBox.information(self, "提示", "请先在左侧选择一个产品/KOL")
            return
        self._delete(pid)

    def _delete(self, pid):
        p = ps.get_product(pid)
        if not p:
            return
        if QMessageBox.question(self, "确认删除",
                                f"删除「{p['name']}」的素材登记？\n\n"
                                "只删除登记信息，material/products 里的文件不会被删除。") \
                != QMessageBox.StandardButton.Yes:
            return
        ps.delete_product(pid)
        self._show_detail(None)
        self.rebuild()
