"""
gui/pages_products.py —— 产品中心（树状素材库）
左侧树：分组 → 品名/KOL → 素材类型 → 文件，双击文件即可预览。
右侧详情：紧凑分组展示，支持添加/移除登记。
提交任务时按品名自动取参考图；KOL 按名称取形象图。
"""
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QPixmap, QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QSplitter, QInputDialog,
                               QLineEdit, QMessageBox, QFileDialog, QMenu, QListWidget,
                               QListWidgetItem, QAbstractItemView)

from store import product_store as ps
from gui.header import page_header, Card
from gui.widgets import VideoPlayerDialog, ImagePreviewDialog
from gui.dialogs_spec import SpecCardDialog

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VID_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
AUD_EXT = {".mp3", ".wav", ".aac", ".m4a", ".flac"}
_KINDS = [("image", "🖼", "参考图片"), ("video", "🎬", "参考视频"), ("audio", "🔊", "音频")]
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
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(8)

        lay.addWidget(page_header("产品中心",
                                  "树状管理素材 · 双击预览 · 支持从资源管理器拖拽文件入库 · 规范卡约束口播口径",
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
        lay.addLayout(bar)

        split = QSplitter()
        lay.addWidget(split, 1)

        # ---------- 左：素材树 ----------
        self.tree = DragTree()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.setMinimumWidth(260)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.currentItemChanged.connect(self._on_pick)
        self.tree.itemDoubleClicked.connect(self._on_double)
        self.tree.files_dropped.connect(self._on_drop)
        split.addWidget(self.tree)

        # ---------- 右：紧凑详情 ----------
        detail = Card(margins=(18, 14, 18, 14))
        split.addWidget(detail)
        self.detail = detail

        head = QHBoxLayout()
        self.lbl_name = QLabel("← 从左侧选择一个产品或 KOL")
        self.lbl_name.setStyleSheet("font-size:16px; font-weight:700; color:#1F2329; background:transparent;")
        self.lbl_type = QLabel("")
        head.addWidget(self.lbl_name)
        head.addWidget(self.lbl_type)
        self.b_spec = QPushButton("📐 规范卡")
        self.b_spec.setObjectName("GhostBtn")
        self.b_spec.setToolTip("定义这个品的套餐/价格口径/活动口径/禁用词；任务中心可据此检测口播脚本")
        self.b_spec.setEnabled(False)
        self.b_spec.clicked.connect(self._open_spec)
        head.addWidget(self.b_spec)
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

        # 三类素材分区（纵向堆叠，图片用缩略图横排，视频/音频用紧凑列表）
        self.sections = {}
        for kind, icon, label in _KINDS:
            sec = QVBoxLayout()
            sec.setSpacing(4)
            sh = QHBoxLayout()
            ttl = QLabel(f"{icon} {label}")
            ttl.setStyleSheet("font-size:13px; font-weight:700; color:#1F2329; background:transparent;")
            b_add = QPushButton("＋ 添加")
            b_add.setObjectName("GhostBtn")
            b_add.clicked.connect(lambda _=False, k=kind: self._add_files(k))
            sh.addWidget(ttl)
            sh.addStretch(1)
            sh.addWidget(b_add)
            sec.addLayout(sh)
            if kind == "image":
                w = DropList(kind)
                w.setViewMode(QListWidget.ViewMode.IconMode)
                w.setIconSize(QSize(84, 60))
                w.setGridSize(QSize(100, 88))
                w.setResizeMode(QListWidget.ResizeMode.Adjust)
                w.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
                w.setFixedHeight(112)
                w.setWordWrap(True)
            else:
                w = DropList(kind)
                w.setFixedHeight(84)
            w.setToolTip("可直接把文件拖到这里登记")
            w.files_dropped.connect(lambda paths, k=kind: self._on_drop(paths, ("kind", self._pid, k)))
            w.itemDoubleClicked.connect(lambda it: self._preview_item(it))
            sec.addWidget(w)
            detail.v.addLayout(sec)
            self.sections[kind] = w
        detail.v.addStretch(1)
        self._pid = None
        self._media = None      # 预览窗口引用防 GC

        split.setSizes([280, 720])
        b_p.clicked.connect(lambda: self._create(ps.TYPE_PRODUCT))
        b_k.clicked.connect(lambda: self._create(ps.TYPE_KOL))
        b_del.clicked.connect(self._delete_current)
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
            root.setExpanded(len(items) <= 6)
        self.tree.blockSignals(False)
        # 恢复选中
        if cur is not None:
            node = self._find_item(cur)
            if node:
                self.tree.setCurrentItem(node)
                if self.tree.currentItem() is None:   # blockSignals 期间不触发 _on_pick
                    self._show_detail(ps.get_product(cur))
        elif self.tree.topLevelItemCount():
            self.tree.expandAll()

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
        self.b_spec.setEnabled(p is not None)
        if not p:
            self.lbl_name.setText("← 从左侧选择一个产品或 KOL")
            self.lbl_type.setText("")
            self.ed_note.blockSignals(True); self.ed_note.clear(); self.ed_note.blockSignals(False)
            for kind, _, _ in _KINDS:
                self.sections[kind].clear()
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
                if kind == "image" and Path(fpath).suffix.lower() in IMG_EXT and Path(fpath).exists():
                    pm = QPixmap(fpath).scaled(84, 60, Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation)
                    it.setIcon(QPixmap(pm))
                    it.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
                if focus and fpath == focus:
                    w.setCurrentItem(it)
                w.addItem(it)
            if not files:
                empty = QListWidgetItem("（暂无，点右上角「＋ 添加」）")
                empty.setFlags(Qt.ItemFlag.NoItemFlags)
                empty.setForeground(QColor("#8F959E"))
                w.addItem(empty)

    def _save_note(self):
        if self._pid is not None:
            ps.set_note(self._pid, self.ed_note.text().strip())
            self.lbl_hint.setText("备注已保存")

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
            os.startfile(str(path))
        else:
            QMessageBox.information(self, "提示", "文件不存在或已被移动")

    def _open_location(self, path):
        if Path(path).exists():
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
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

    # ================= 拖拽入库 / 规范卡 =================
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

    def _open_spec(self):
        p = ps.get_product(self._pid) if self._pid is not None else None
        if not p:
            return
        SpecCardDialog(self, p).exec()
        self._sig = None
        self.lbl_hint.setText(f"「{p['name']}」规范卡已更新，任务中心右键可检测口播")

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
            menu.addAction("🗑 删除该条目", lambda: self._delete(pid))
        elif role and role[0] == "root":
            menu.addAction("＋ 新建", lambda: self._create(role[1]))
        if menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ================= 新建 / 删除 =================
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
