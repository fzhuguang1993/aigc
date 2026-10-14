"""
gui/dialogs_rename.py —— 成品库/素材库「批量重命名 / 批量设置显示名称」弹窗（拖拽式拼名）

交互（用户敲定）：
- 顶部「字段池」：把想要的字段拖到下面（或双击）加入；
- 中间「命名顺序」：横排、可左右拖动排序，形如 日期_品名_编号；× 或拖回池里移除；
- 底部：分隔符（下划线/短横…）与起始序号仍像以前一样可选；
- 再下面：实时预览「新名称模板」+「原名 → 新名」示例。

字段渲染不再用 per-field lambda：统一 build_context(row, idx) 造一份取值字典，
命名顺序里每个 token 按 key 从字典取值，各自 sanitize、丢空段、用分隔符连接。
批量重命名确认后把 [(row, 新名主干，不含扩展名)] 放进 self.plan；调用方据此 rename / set_display。
"""
import json
from pathlib import Path

from PySide6.QtCore import Qt, QMimeData, QPoint, QUrl
from PySide6.QtGui import QDrag, QPainter, QPen, QColor
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QWidget, QComboBox, QSpinBox, QMessageBox, QFrame,
                               QApplication, QSizePolicy, QLineEdit)

from core import naming
from gui.window_frame import apply_rounded
from gui.widgets import FlowLayout
from gui import ui_kit
from gui.ui_kit import COLORS, rgba
from gui.theme import tokenize

# 字段登记：key →（中文名）。渲染的唯一入口是 build_context()，字段表里不再藏函数。
_FIELD_LABEL = {
    "seq": "编号",
    "stem": "原名",
    "product": "品名",
    "tag": "标签",
    "block_type": "板块类型",
    "date": "日期",
    "date_full": "完整日期",
    "time": "时刻",
    "task_id": "任务ID",
}

# 字段主题色：沿用「数据中台」KPI 软色块配色（主蓝/绿/紫/橙/青/红），一个字段一个色。
# 胶囊统一按此色做「浅底 + 圆角 + 同色描边」。色板真源已收敛到 ui_kit，这里只引用。
_FIELD_COLOR = ui_kit.FIELD_COLORS

# 各页面字段池可用的 key（顺序即展示顺序，常用在前）。日期只用「月日」，不收带年份的「完整日期」。
OUTPUT_FIELDS = ("seq", "stem", "product", "tag", "date", "time", "task_id")
MATERIAL_FIELDS = ("seq", "stem", "block_type", "product", "date", "time", "task_id")

DEFAULT_TOKENS = ("seq", "stem")        # 最不容易后悔的默认：原名前面加个批次序号
DEFAULT_SEP = "_"

_MIME = "application/x-aigc-rename-token"   # 拖拽载荷：{role, key, index}


def build_context(row, idx):
    """一行数据 + 当前序号 → 各字段取值字典（拼名唯一取值入口，取代旧版 lambda 表）。"""
    row = row or {}
    name = row.get("name") or row.get("path") or ""
    created = row.get("created_at") or ""
    return {
        "seq": f"{int(idx):03d}",                     # 编号补零到 3 位：001 / 002
        "stem": Path(name).stem,
        "product": row.get("product") or "",
        "tag": row.get("tag") or "",
        "block_type": row.get("block_type") or "",
        "date": created[5:7] + created[8:10],         # 月日 MMDD（不带年份，短、好观感）
        "date_full": created[:10].replace("-", ""),
        "time": created[11:16].replace(":", ""),
        "task_id": str(int(row.get("source_task_id") or 0)),
    }


def render_stem(tokens, sep, ctx):
    """按 tokens 顺序取值、各自 sanitize、丢掉空段、用 sep 连接（空字段那段不留孤立分隔符）。"""
    parts = [naming.sanitize(ctx.get(t, "")) for t in tokens]
    parts = [p for p in parts if p]
    return sep.join(parts)


class _FieldChip(QFrame):
    """字段胶囊。role=pool：单击追加到末尾 / 拖到命名顺序；role=order：左右拖排、× 移除。"""

    def __init__(self, key, role, dialog, index=0, parent=None):
        super().__init__(parent)
        self.key = key
        self.role = role
        self.dlg = dialog
        self.index = index
        self.setObjectName("RenChipPool" if role == "pool" else "RenChipOrder")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(11, 4, 11, 4)
        h.setSpacing(4)
        txt = QLabel(_FIELD_LABEL.get(key, key))        # 只显字段名（已去掉加号/把手图标）
        txt.setObjectName("RenChipText")
        h.addWidget(txt)
        if role == "order":
            b = QPushButton("×")
            b.setObjectName("RenChipX")
            b.setFixedSize(16, 16)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda: self.dlg.remove_token(self.index))
            h.addWidget(b)
        self._press = None
        self._apply_style()

    def _apply_style(self):
        """圆角 + 软色底（参考数据中台 KPI 徒牌）：按字段主题色做浅底/同色描边。
        取色走 ui_kit：主题色名 → COLORS 当前值，rgba 由 ui_kit.rgba 派生。"""
        accent = _FIELD_COLOR.get(self.key, COLORS["primary"])
        soft = rgba(accent, 0.14)
        hover = rgba(accent, 0.26)
        press = rgba(accent, 0.38)
        line = rgba(accent, 0.45)
        ink = COLORS["text"]
        if self.role == "pool":   # 字段池：实线描边，与 UI 组件库「扁平款胶囊」一致
            self.setStyleSheet(
                f"#RenChipPool{{background:{soft}; border:1px solid {line}; border-radius:14px;}}"
                f"#RenChipPool:hover{{background:{hover}; border:1px solid {accent};}}"
                f"#RenChipPool:pressed{{background:{press};}}"
                f"#RenChipPool #RenChipText{{color:{ink};}}")
        else:                     # 命名顺序：实线描边，暗示「已选中、可拖排」
            self.setStyleSheet(
                f"#RenChipOrder{{background:{soft}; border:1px solid {line}; border-radius:14px;}}"
                f"#RenChipOrder:hover{{background:{hover}; border:1px solid {accent};}}"
                f"#RenChipOrder:pressed{{background:{press};}}"
                f"#RenChipOrder #RenChipText{{color:{ink}; font-weight:600;}}")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        # 字段池：单击（未触发拖拽）= 追加到命名顺序末尾
        if (self.role == "pool" and e.button() == Qt.MouseButton.LeftButton
                and self._press is not None
                and (e.position().toPoint() - self._press).manhattanLength()
                < QApplication.startDragDistance()):
            self.dlg.add_token(self.key)
        self._press = None
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        super().mouseDoubleClickEvent(e)

    def mouseMoveEvent(self, e):
        if (self._press is not None and (e.buttons() & Qt.MouseButton.LeftButton)
                and (e.position().toPoint() - self._press).manhattanLength()
                >= QApplication.startDragDistance()):
            self._start_drag()
            self._press = None
        super().mouseMoveEvent(e)

    def _start_drag(self):
        drag = QDrag(self)
        data = QMimeData()
        data.setData(_MIME, json.dumps({"role": self.role, "key": self.key,
                                       "index": self.index}).encode("utf-8"))
        drag.setMimeData(data)
        # 拖拽跟随：把胶囊原样抓成图跟着光标走（从池加入 / 顺序内部重排都看得到实体）
        pm = self.grab()
        drag.setPixmap(pm)
        drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
        drag.exec(Qt.DropAction.CopyAction)


class _ChipBar(QWidget):
    """装胶囊的横排（流式）容器：接受从另一条 bar 拖来的胶囊，落点交给弹窗算插位。"""

    def __init__(self, role, dialog):
        super().__init__()
        self.role = role
        self.dlg = dialog
        self.setObjectName("RenBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self._flow = FlowLayout(self, hgap=6, vgap=6)
        self._flow.setContentsMargins(8, 6, 8, 6)
        self._ind = None          # 命名顺序拖动时的落点竖线 x（仅 order 条用）

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(_MIME):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(_MIME):
            if self.role == "order":     # 实时把落点画成竖线，让重排「看得到要去哪」
                self._ind = int(e.position().toPoint().x())
                self.update()
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        if self._ind is not None:
            self._ind = None
            self.update()

    def dropEvent(self, e):
        if e.mimeData().hasFormat(_MIME):
            self._ind = None
            self.dlg.handle_drop(self.role, e)
            e.acceptProposedAction()
            self.update()

    def paintEvent(self, e):
        super().paintEvent(e)          # 先让 QSS 把底色画出来
        if self._ind is None:
            return
        p = QPainter(self)
        pen = QPen(QColor("#3A6EE8"))
        pen.setWidth(2)
        p.setPen(pen)
        p.drawLine(self._ind, 6, self._ind, self.height() - 6)
        p.end()


class BatchRenameDialog(QDialog):
    """拖拽式拼名弹窗；确认后把 [(row, 新名主干，不含扩展名)] 放进 self.plan。"""

    def __init__(self, parent, rows, fields=None, default_tokens=None, title=None,
                 ok_text=None):
        super().__init__(parent)
        self.rows = list(rows or [])
        self.plan = []
        self._keys = tuple(fields or OUTPUT_FIELDS)
        picked = [t for t in (default_tokens or DEFAULT_TOKENS) if t in self._keys]
        self._tokens = picked or list(self._keys[:1])
        self.setWindowTitle(title or "批量重命名")
        self.resize(680, 560)
        self.setAcceptDrops(True)      # 支持从资源管理器拖入文件/文件夹重建待改列表

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)

        tip = QLabel(f"将处理选中的 <b>{len(self.rows)}</b> 条。从字段池把字段拖到下面排好顺序，"
                     "下方实时预览「新名称模板」与示例；空字段那一段会自动省略，不留孤立分隔符。")
        tip.setObjectName("PageTip")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self._drop_tip = QLabel("💡 可把文件/文件夹从资源管理器直接拖进本窗，用它们重建待改列表（含文件夹内视频，递归收集）。")
        self._drop_tip.setObjectName("PageTip")
        self._drop_tip.setWordWrap(True)
        lay.addWidget(self._drop_tip)

        lay.addWidget(QLabel("① 字段池（点一下追加到末尾，也可拖到下面插入）"))
        self.pool = _ChipBar("pool", self)
        lay.addWidget(self.pool)

        lay.addWidget(QLabel("② 命名顺序（左右拖动排序，× 或拖回上面移除）"))
        self.order = _ChipBar("order", self)
        lay.addWidget(self.order)

        foot = QHBoxLayout()
        foot.addWidget(QLabel("字段之间用："))
        self.cb_sep = QComboBox()
        for key, label in naming.SEP_CHOICES.items():
            self.cb_sep.addItem(label, key)
        self.cb_sep.currentIndexChanged.connect(self._refresh)
        foot.addWidget(self.cb_sep)
        foot.addSpacing(16)
        foot.addWidget(QLabel("序号从"))
        self.spin = QSpinBox()
        self.spin.setRange(0, 9999)
        self.spin.setValue(1)
        self.spin.valueChanged.connect(self._refresh)
        foot.addWidget(self.spin)
        foot.addWidget(QLabel("开始"))
        foot.addStretch(1)
        lay.addLayout(foot)

        # 🔁 文件名搜索替换：在拼好的新名里把「查找」子串换成「替换为」（空=不处理）。
        # 默认命名顺序含「原名」，勾上它就等于对原文件名做查找替换。
        rep = QHBoxLayout()
        rep.setSpacing(6)
        rep.addWidget(QLabel("🔁 文件名替换：把"))
        self.rep_find = QLineEdit()
        self.rep_find.setPlaceholderText("要查找的字（留空=不替换）")
        self.rep_find.setFixedWidth(160)
        self.rep_find.textChanged.connect(lambda _=None: self._refresh())
        rep.addWidget(self.rep_find)
        rep.addWidget(QLabel("换成"))
        self.rep_repl = QLineEdit()
        self.rep_repl.setPlaceholderText("替换为（留空=删除）")
        self.rep_repl.setFixedWidth(160)
        self.rep_repl.textChanged.connect(lambda _=None: self._refresh())
        rep.addWidget(self.rep_repl)
        rep.addStretch(1)
        lay.addLayout(rep)

        self.lbl_preview = QLabel()
        self.lbl_preview.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_preview.setWordWrap(True)
        self.lbl_preview.setStyleSheet(
            tokenize("background:#F7F8FA; border:1px dashed #C9CDD4; border-radius:6px;"
            "padding:10px 12px; color:#1F2329;"))
        lay.addWidget(self.lbl_preview)

        bb = QHBoxLayout()
        b_reset = QPushButton("↩ 用默认")
        b_reset.setObjectName("GhostBtn")
        b_reset.clicked.connect(self._reset_defaults)
        bb.addWidget(b_reset)
        b_scan = QPushButton("🔎 识别命名结构")
        b_scan.setObjectName("PrimaryBtn")
        b_scan.setToolTip("扫描这批文件名的现有格式，自动识别分隔符（_ - . / 驼峰）与字段顺序并回填；不自动跑，只随本钮")
        b_scan.clicked.connect(self._prescan)
        bb.addWidget(b_scan)
        bb.addStretch(1)
        b_cancel = QPushButton("取消")
        b_cancel.setObjectName("GhostBtn")
        b_cancel.clicked.connect(self.reject)
        b_ok = QPushButton(ok_text or "✏ 应用重命名")
        b_ok.clicked.connect(self._accept)
        bb.addWidget(b_cancel)
        bb.addWidget(b_ok)
        lay.addLayout(bb)

        self.setStyleSheet(
            tokenize("#RenBar{background:#FAFBFC; border:1px solid #E5E6EB; border-radius:8px;}"
            "#RenChipX{background:transparent;border:none;color:#8F959E;"
            "font-weight:700;padding:0;}"
            "#RenChipX:hover{color:#F54A45;}"))

        self._rebuild_bars()
        apply_rounded(self, show_min=False, show_max=False)

    # ---------- 数据 -> 名称 ----------
    def _sep(self):
        return self.cb_sep.currentData()

    def _apply_replace(self, stem):
        """在拼好的新名主干上做“查找→替换”；查找框为空则原样返回。"""
        find = self.rep_find.text()
        if not find:
            return stem
        return stem.replace(find, self.rep_repl.text())

    def _build_plan(self):
        plan = []
        for i, row in enumerate(self.rows, start=self.spin.value()):
            ctx = build_context(row, i)
            stem = render_stem(self._tokens, self._sep(), ctx)
            if not stem:
                name = row.get("name") or row.get("path") or ""
                stem = Path(name).stem
            stem = self._apply_replace(stem)
            plan.append((row, stem))
        return plan

    # ---------- 增删排序 ----------
    def add_token(self, key):
        if key in self._keys and key not in self._tokens:
            self._tokens.append(key)
            self._rebuild_bars()
            self._refresh()

    def remove_token(self, index):
        if 0 <= index < len(self._tokens):
            self._tokens.pop(index)
            self._rebuild_bars()
            self._refresh()

    def _reset_defaults(self):
        self._tokens = [t for t in DEFAULT_TOKENS if t in self._keys] or list(self._keys[:1])
        self._rebuild_bars()
        self._refresh()

    def _prescan(self):
        """手动预读命名规律：扫描选中行文件名主干→回填分隔符与字段顺序。
        绝不自动触发；识别不到就提示保持手动。"""
        if not self.rows:
            self.lbl_preview.setText("<span style='color:#D83931'>没有可预读的行。</span>")
            return
        stems = [Path(r.get("name") or r.get("path") or "").stem for r in self.rows]
        info = naming.detect_pattern(stems, rows=self.rows, context_fn=build_context)
        applied = []
        style = info.get("style") or "none"
        sep = info.get("sep")
        # 只有真识别到规律（有分隔符或驼峰）才改分隔符，避免把无名规律的文件“连写”误导
        if sep is not None and (style != "none"):
            idx = self.cb_sep.findData(sep)
            if idx >= 0 and idx != self.cb_sep.currentIndex():
                self.cb_sep.setCurrentIndex(idx)
            applied.append("分隔符「%s」" % (sep or "无（连着写/%s）" % style))
        toks = info.get("tokens")
        if toks:
            picked = [t for t in toks if t in self._keys]
            if picked:
                self._tokens = picked
                applied.append("字段顺序 " + (self._sep() or "·").join(
                    _FIELD_LABEL.get(t, t) for t in picked))
        self._rebuild_bars()
        self._refresh()
        if not applied:
            self.lbl_preview.setText(
                "<span style='color:#D83931'>未识别出稳定命名规律，已保持手动。</span>"
                "<div style='color:#8F959E'>（可仍手动拖字段、选分隔符）</div>")
        else:
            self.lbl_preview.setText(
                "<div style='color:#00A870'>🔎 已按现有文件名预读：" + "　·　".join(applied) +
                "（不满意可继续手动调，确认无误再点应用）</div>" + self.lbl_preview.text())

    def handle_drop(self, role, event):
        try:
            payload = json.loads(bytes(event.mimeData().data(_MIME)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        src, key, idx = payload.get("role"), payload.get("key"), int(payload.get("index", 0))
        x = event.position().toPoint().x()
        if role == "order" and key in self._keys:
            if src == "pool":
                self._tokens.insert(self._order_index_at(x), key)
            else:   # 命名顺序内部重排
                if 0 <= idx < len(self._tokens):
                    self._tokens.pop(idx)
                    pos = self._order_index_at(x)
                    if pos > idx:
                        pos -= 1
                    self._tokens.insert(pos, key)
            self._rebuild_bars()
            self._refresh()
        elif role == "pool" and src == "order" and key in self._tokens:
            self._tokens.remove(key)
            self._rebuild_bars()
            self._refresh()

    def _order_index_at(self, x):
        """按落点 x 在命名顺序里算插入位（在哪个胶囊左边就插到它前面）。"""
        chips = [self.order._flow.itemAt(i).widget() for i in range(self.order._flow.count())]
        chips = [c for c in chips if isinstance(c, _FieldChip)]
        for i, c in enumerate(chips):
            if x < c.geometry().center().x():
                return i
        return len(chips)

    # ---------- 装载 ----------
    def _clear_bar(self, bar):
        while bar._flow.count():
            it = bar._flow.takeAt(0)
            w = it.widget() if it is not None else None
            if w is not None:
                w.deleteLater()

    def _rebuild_bars(self):
        self._clear_bar(self.pool)
        for k in self._keys:
            if k not in self._tokens:
                self.pool._flow.addWidget(_FieldChip(k, "pool", self))
        self._clear_bar(self.order)
        sep = self._sep()
        for i, k in enumerate(self._tokens):
            if i:
                tag = QLabel(sep)
                tag.setStyleSheet(tokenize("color:#8F959E; background:transparent; border:none;"))
                self.order._flow.addWidget(tag)
            self.order._flow.addWidget(_FieldChip(k, "order", self, index=i))

    def _refresh(self):
        # 分隔符变了要重画顺序条里的分隔标签
        self._rebuild_bars()
        if not self._tokens:
            self.lbl_preview.setText("<span style='color:#F54A45'>至少要选一个字段</span>")
            return
        sep = self._sep()
        template = sep.join(_FIELD_LABEL.get(t, t) for t in self._tokens)
        lines = [f"<div style='color:#4E5969'>模板：<b style='color:#1F2329'>{template}</b></div>"]
        for i, row in enumerate(self.rows[:6], start=self.spin.value()):
            ctx = build_context(row, i)
            stem = self._apply_replace(render_stem(self._tokens, sep, ctx) or Path(
                row.get("name") or row.get("path") or "").stem)
            old = Path(row.get("name") or row.get("path") or "").name
            suf = Path(row.get("path") or "").suffix
            lines.append(f"<div><span style='color:#8F959E'>{old}</span>"
                         f"  →  <b style='color:#1F2329'>{stem}{suf}</b></div>")
        more = (f"<div style='color:#8F959E'>… 其余 {len(self.rows) - 6} 条同理</div>"
                if len(self.rows) > 6 else "")
        self.lbl_preview.setText("".join(lines) + more)

    # ---------- 拖入文件/文件夹重建待改列表 ----------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        if not md.hasUrls():
            return
        try:
            from store.output_store import VID_EXT
        except Exception:
            VID_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
        files, seen = [], set()

        def add(f):
            k = str(f).lower()
            if k not in seen:
                seen.add(k)
                files.append(f)

        for url in md.urls():
            p = url.toLocalFile()
            if not p:
                continue
            path = Path(p)
            try:
                if path.is_dir():
                    for f in sorted(path.rglob("*")):
                        if f.is_file() and f.suffix.lower() in VID_EXT:
                            add(f)
                elif path.is_file() and path.suffix.lower() in VID_EXT:
                    add(path)
            except OSError:
                continue
        if not files:
            QMessageBox.information(
                self, "没收集到视频",
                "拖入的内容里没有视频文件（支持 " + " ".join(sorted(VID_EXT)) + "）。")
            return
        self.rows = [self._row_from_file(f) for f in files]
        self._drop_tip.setText(
            f"✅ 已拖入并重建待改列表：共 <b>{len(self.rows)}</b> 个视频（来路：拖入的文件/文件夹）。")
        self._refresh()
        e.acceptProposedAction()

    @staticmethod
    def _row_from_file(f):
        """一个磁盘文件→一行最小记录（path/name/created_at）；供 build_context 取值。"""
        import datetime
        import os
        f = Path(f)
        try:
            m = datetime.datetime.fromtimestamp(os.path.getmtime(f))
        except OSError:
            m = datetime.datetime.now()
        return {"name": f.name, "path": str(f),
                "created_at": m.strftime("%Y-%m-%d %H:%M:%S")}

    # ---------- 确认 ----------
    def _accept(self):
        if not self._tokens:
            QMessageBox.warning(self, "还差一步", "命名顺序不能是空的")
            return
        self.plan = self._build_plan()
        self.accept()
