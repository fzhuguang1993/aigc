"""
gui/dialogs_breakdown_detail.py —— 爆款拆解「任务详情」三屏联动弹窗

从任务库（store.breakdown_store）按 task_id 离线重建一条拆解结果，呈现手机三折叠式
三屏：左·图集 / 中·播放器 / 右·拆解文档，三者围绕「当前时间」双向联动——

  · 点分镜条 / 口播句的时间戳   → 视频跳到该秒 + 图集切图 + 该条高亮；
  · 视频播放推进（节流 200ms）  → 反算当前分镜/当前句 → 高亮 + 图集自动换图；
  · 点图集缩略图               → 同样跳到该时间点。

所有联动汇聚到唯一真源 `_apply_current(sec)`，用 `_mute` 挡住"程序跳转→positionChanged
→再跳转"的回环。数据/定位口径全部走纯逻辑 timeline.build_timeline / index_at，
本模块只做界面与信号接线。非模态，多个任务详情可同时开。
"""
import html
import re
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QTimer, Signal
from PySide6.QtGui import QPixmap, QIcon, QKeySequence, QShortcut
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QWidget, QScrollArea, QListWidget,
                               QListWidgetItem, QFrame, QSizePolicy, QStackedLayout,
                               QComboBox, QRadioButton, QButtonGroup, QDoubleSpinBox,
                               QLineEdit, QFormLayout, QDialogButtonBox, QPlainTextEdit,
                               QCheckBox)

from gui.header import page_header, FS_BLUE, FS_SUB, FS_WEAK
from gui.widgets import _SeekSlider
from utils.desktop_utils import open_path, reveal_in_folder
from gui.theme import tokenize

_OPEN = {}   # task_id -> 弹窗实例：防被 GC，重复打开同一任务只前置


def open_breakdown_detail(parent, task_id):
    """打开（或前置）某任务的三屏详情。task_id 无效/取不到结果时给个提示，不抛。"""
    tid = int(task_id or 0)
    dlg = _OPEN.get(tid)
    if dlg is not None and dlg.isVisible():
        dlg.raise_()
        dlg.activateWindow()
        return dlg
    dlg = BreakdownDetailDialog(tid, parent)
    _OPEN[tid] = dlg
    dlg.finished.connect(lambda *_: _OPEN.pop(tid, None))
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg


def _fmt_sec(sec):
    s = int(max(0, sec))
    return f"{s // 60:02d}:{s % 60:02d}"


def _fmt_dur(sec):
    if not sec:
        return "—"
    return _fmt_sec(sec)


def _score_num(v):
    """从 '8'/'8分'/'8.5' 里抽出整分值，抽不到给 -1。"""
    import re
    m = re.search(r"\d+(?:\.\d+)?", str(v or ""))
    return int(float(m.group())) if m else -1


class _PickItem(QFrame):
    """文档屏里一条可点、可高亮的条目（分镜 / 口播句共用）。

    只认一个 clicked(kind, idx, sec)；active 态换底色 + 蓝色左描边。"""
    clicked = Signal(str, int, float)

    def __init__(self, kind, idx, sec, parent=None):
        super().__init__(parent)
        self.kind, self.idx, self.sec = kind, idx, float(sec)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("active", False)
        self.setObjectName("PickItem")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(10, 7, 10, 7)
        self.v.setSpacing(2)

    def set_active(self, on):
        if self.property("active") == bool(on):
            return
        self.setProperty("active", bool(on))
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.kind, self.idx, self.sec)
            e.accept()
        else:
            super().mousePressEvent(e)


def _highlight_terms(text):
    """整体分析可读性：转义 HTML 后给「（…）/ N 分 / 箭头」等着色，突出重点。"""
    t = html.escape(str(text or ""))
    t = re.sub(r"（([^）]+)）", r"<span style='color:#8F959E;'>（\1）</span>", t)
    t = re.sub(r"(\d+\s*分)", r"<b style='color:#D83931;'>\1</b>", t)
    t = re.sub(r"(→|=>|->|—|＞|>)", r"<span style='color:#2F6BFF;'>\1</span>", t)
    return t


class ClickableCover(QLabel):
    """播放器上的封面遮罩：默认盖住黑屏，点一下即播放/暂停。"""
    clicked = Signal()

    def __init__(self, text=""):
        super().__init__(text)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            tokenize("background:#15171C; color:#8F959E; font-size:14px; border-radius:8px;"))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            e.accept()
        else:
            super().mousePressEvent(e)


class BreakdownDetailDialog(QDialog):
    def __init__(self, task_id, parent=None):
        super().__init__(parent)
        self.task_id = int(task_id)
        self._result = None
        self._shots, self._lines, self._gal = [], [], []
        self._blocks = []
        self._shot_items, self._line_items, self._block_items = [], [], []
        self._cur_shot = self._cur_line = self._cur_gal = self._cur_block = -1
        self._mute = False
        self._last_pos_ms = 0

        self.setWindowTitle("拆解详情")
        self.resize(1400, 840)
        self.setStyleSheet(_DETAIL_QSS)
        # 先给 self 建好外层布局，apply_rounded 才会把自绘标题栏插进第 0 行
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(0)

        host = QWidget(objectName="DetailShell")
        host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        hv = QVBoxLayout(host)
        hv.setContentsMargins(14, 12, 14, 12)
        hv.setSpacing(10)
        self._build_body(hv)
        outer.addWidget(host, 1)

        from gui.window_frame import apply_rounded
        apply_rounded(self, title="🔥 拆解详情")

        self._load()

    # ------------------------------------------------------------------
    def _build_body(self, hv):
        head = QHBoxLayout()
        self.lbl_title = QLabel("拆解详情")
        self.lbl_title.setStyleSheet(
            f"font-size:16px; font-weight:700; color:{FS_BLUE}; background:transparent;")
        head.addWidget(self.lbl_title, 1)
        for text, slot in (("📂 打开视频目录", self._open_video_dir),
                           ("📄 打开 Word 报告", self._open_report),
                           ("✕ 关闭", self.close)):
            b = QPushButton(text)
            b.setObjectName("GhostBtn")
            b.clicked.connect(slot)
            head.addWidget(b)
        hv.addLayout(head)

        cols = QHBoxLayout()
        cols.setSpacing(10)
        cols.addWidget(self._build_gallery(), 1)
        cols.addWidget(self._build_player(), 1)
        cols.addWidget(self._build_doc(), 1)
        hv.addLayout(cols, 1)

    # ---------------- 左：图集 ----------------
    def _build_gallery(self):
        box = QFrame(objectName="Panel")
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)
        cap = QLabel("🖼 图集（分镜帧）")
        cap.setObjectName("CapTitle")
        v.addWidget(cap)
        self.big = QLabel("（无图集）")
        self.big.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.big.setMinimumHeight(280)
        self.big.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.big.setStyleSheet(tokenize("color:#8F959E; background:#F2F3F5; border-radius:8px;"))
        v.addWidget(self.big, 3)
        self.thumbs = QListWidget()
        self.thumbs.setViewMode(QListWidget.ViewMode.IconMode)
        self.thumbs.setIconSize(self.thumbs.iconSize())
        self.thumbs.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.thumbs.setMovement(QListWidget.Movement.Static)
        self.thumbs.setSpacing(6)
        self.thumbs.itemClicked.connect(self._on_thumb)
        v.addWidget(self.thumbs, 2)
        return box

    # ---------------- 中：播放器 ----------------
    def _build_player(self):
        box = QFrame(objectName="Panel")
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)
        cap = QLabel("🎬 完整视频")
        cap.setObjectName("CapTitle")
        v.addWidget(cap)

        stage_box = QWidget()
        stage = QStackedLayout(stage_box)
        stage.setContentsMargins(0, 0, 0, 0)
        self.video = QVideoWidget()
        self.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.video.setMinimumHeight(360)
        self.video.setStyleSheet("background:#15171C; border-radius:8px;")
        self.video_cover = ClickableCover("🎬 点击播放")   # 封面遮罩，避免默认黑屏
        self.video_cover.setMinimumHeight(360)
        stage.addWidget(self.video)         # index 0：视频画面
        stage.addWidget(self.video_cover)   # index 1：封面（默认显示）
        stage.setCurrentIndex(1)
        self._stage = stage
        self._stage_box = stage_box
        v.addWidget(stage_box, 1)
        self.video_placeholder = QLabel("视频文件不存在或已被移动，无法播放")
        self.video_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_placeholder.setStyleSheet(
            tokenize("color:#8F959E; background:#15171C; border-radius:8px;"))
        self.video_placeholder.setMinimumHeight(360)
        self.video_placeholder.setVisible(False)
        v.addWidget(self.video_placeholder)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)

        bar = QHBoxLayout()
        self.btn_play = QPushButton("▶")
        self.btn_play.setFixedWidth(40)
        self.btn_play.clicked.connect(self._toggle_play)
        self.slider = _SeekSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(self._seek_ms)
        self.slider.seeked.connect(self._seek_ms)
        self.lbl_time = QLabel("00:00 / 00:00")
        bar.addWidget(self.btn_play)
        bar.addWidget(self.slider, 1)
        bar.addWidget(self.lbl_time)
        v.addLayout(bar)

        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self._tick = QTimer(self)
        self._tick.setSingleShot(True)
        self._tick.setInterval(200)
        self._tick.timeout.connect(self._flush_pos)
        self.video_cover.clicked.connect(self._toggle_play)
        sc = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        sc.setContext(Qt.ShortcutContext.WindowShortcut)
        sc.activated.connect(self._toggle_play)
        return box

    # ---------------- 右：拆解文档 ----------------
    def _build_doc(self):
        box = QFrame(objectName="Panel")
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)
        cap = QLabel("📝 拆解文档（点时间戳跳转）")
        cap.setObjectName("CapTitle")
        v.addWidget(cap)
        self.doc_scroll = QScrollArea()
        self.doc_scroll.setWidgetResizable(True)
        self.doc_scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        inner = QWidget()
        self.doc_v = QVBoxLayout(inner)
        self.doc_v.setContentsMargins(2, 2, 6, 2)
        self.doc_v.setSpacing(6)
        self.doc_v.addWidget(self._section("整体分析"))
        self._overall_host = QWidget()
        self._overall_v = QVBoxLayout(self._overall_host)
        self._overall_v.setContentsMargins(0, 0, 0, 0)
        self.doc_v.addWidget(self._overall_host)
        self.doc_v.addWidget(self._section("分镜画面 · 三类提示词"))
        self._shots_host = QWidget()
        self._shots_v = QVBoxLayout(self._shots_host)
        self._shots_v.setContentsMargins(0, 0, 0, 0)
        self._shots_v.setSpacing(6)
        self.doc_v.addWidget(self._shots_host)
        self.doc_v.addWidget(self._section("口播逐字稿"))
        self._lines_host = QWidget()
        self._lines_v = QVBoxLayout(self._lines_host)
        self._lines_v.setContentsMargins(0, 0, 0, 0)
        self._lines_v.setSpacing(4)
        self.doc_v.addWidget(self._lines_host)
        # 板块标题行右侧挂「✂ 切割入素材库」：把板块/手选区间切成片段归档
        blk_head = QHBoxLayout()
        blk_head.setContentsMargins(0, 0, 0, 0)
        blk_head.addWidget(self._section("营销板块（点跳板块起点）"))
        blk_head.addStretch(1)
        self.btn_cut = QPushButton("✂ 切割入素材库")
        self.btn_cut.setObjectName("GhostBtn")
        self.btn_cut.setToolTip("按板块（自动）或手选区间切成片段，归档进素材库，为 AI 混剪备料")
        self.btn_cut.clicked.connect(self._open_cut)
        blk_head.addWidget(self.btn_cut)
        self.doc_v.addLayout(blk_head)
        self._blocks_host = QWidget()
        self._blocks_v = QVBoxLayout(self._blocks_host)
        self._blocks_v.setContentsMargins(0, 0, 0, 0)
        self._blocks_v.setSpacing(4)
        self.doc_v.addWidget(self._blocks_host)
        self.doc_v.addStretch(1)
        self.doc_scroll.setWidget(inner)
        v.addWidget(self.doc_scroll, 1)
        return box

    @staticmethod
    def _section(title):
        lbl = QLabel(title)
        lbl.setStyleSheet(
            f"font-size:13px; font-weight:700; color:{FS_SUB}; background:transparent;"
            "margin-top:6px;")
        return lbl

    # ------------------------------------------------------------------
    # 装载数据（离线从库重建）
    # ------------------------------------------------------------------
    def _load(self):
        from store import breakdown_store
        from video_text_tools.breakdown.timeline import build_timeline
        res = breakdown_store.load_result(self.task_id)
        if res is None:
            self.lbl_title.setText("⚠ 任务不存在或数据已损坏")
            return
        self._result = res
        tl = build_timeline(res)
        self._shots, self._lines = tl["shots"], tl["lines"]
        self._blocks = tl.get("blocks", [])
        self._gal = list(res.gallery or [])

        tag = "（未完成）" if res.is_partial() else ""
        cost = res.cost or {}
        self.lbl_title.setText(
            f"🔥 {res.title or '（无标题）'}{tag} · 分镜 {res.shot_count} · "
            f"时长 {_fmt_dur(res.duration)} · 豆包 {cost.get('vision_calls', 0)} 次")

        self._fill_overall()
        self._fill_shots()
        self._fill_lines()
        self._fill_blocks()
        self._fill_thumbs()
        self._show_first_frame()
        self._set_source(res.video_path)

    _OVR_ACCENT = ("#F5722C", "#7C5CFF", "#00A870", "#2F6BFF", "#D83931")

    def _fill_overall(self):
        o = self._result.overall
        kv = [("钩子", f"{o.hook_desc}（{o.hook_score}分）"), ("爆点因素", o.factors),
              ("情绪曲线", o.emotion_curve), ("内容公式", o.formula), ("复刻蓝图", o.blueprint)]
        n = 0
        for k, val in kv:
            if not (val or "").strip():
                continue
            self._overall_v.addWidget(
                self._kv_card(k, val, self._OVR_ACCENT[n % len(self._OVR_ACCENT)]))
            n += 1

    def _kv_card(self, key, val, accent):
        """整体分析：每条一张带彩色左描边 + 关键词色块的卡片，重点一眼可辨。"""
        card = QFrame()
        card.setStyleSheet(
            tokenize("QFrame{background:#FFFFFF; border:1px solid #E5E6EB;"
            f"border-left:3px solid {accent}; border-radius:8px;}}"))
        h = QHBoxLayout(card)
        h.setContentsMargins(10, 8, 12, 8)
        h.setSpacing(10)
        pill = QLabel(key)
        pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pill.setStyleSheet(
            tokenize(f"background:{accent}; color:#FFFFFF; font-size:12px; font-weight:700;"
            "border-radius:9px; padding:3px 9px;"))
        h.addWidget(pill, 0, Qt.AlignmentFlag.AlignTop)
        body = QLabel(_highlight_terms(val))
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setStyleSheet(tokenize("background:transparent; color:#1F2329; font-size:13px;"))
        h.addWidget(body, 1)
        return card

    def _show_first_frame(self):
        """第一屏默认显示首帧图集（别空着），并给播放器铺一张封面遮罩（别黑屏）。"""
        cover = ""
        if self._gal:
            cover = self._gal[0].get("path", "")
        if not cover:
            cover = getattr(self._result, "cover_path", "") or ""
        if not (cover and Path(cover).exists()):
            return
        self._set_big(cover)
        pix = QPixmap(cover)
        if not pix.isNull():
            self.video_cover.setPixmap(pix.scaled(
                460, 620, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def _plain(self, html_text):
        lbl = QLabel(html_text)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setStyleSheet(tokenize("background:transparent; color:#1F2329; font-size:12px;"))
        return lbl

    def _fill_shots(self):
        if not self._shots:
            self._shots_v.addWidget(self._plain("（无分镜数据）"))
            return
        for i, s in enumerate(self._shots):
            item = _PickItem("shot", i, s["ts"])
            head = QLabel(f"#{s['idx']}　{_fmt_sec(s['ts'])}　"
                          f"{s.get('time_range','')}　"
                          f"{s.get('shot_size','')} {s.get('camera','')}".strip())
            head.setObjectName("ItemHead")
            item.v.addWidget(head)
            bits = [b for b in (s.get("composition"), s.get("transition"),
                                s.get("on_screen_text"), s.get("emotion")) if b]
            if bits:
                item.v.addWidget(self._muted(" / ".join(bits)))
            for lab, key in (("画面", "visual_prompt"), ("文案", "copy_prompt"),
                             ("复刻", "shoot_prompt")):
                val = (s.get(key) or "").strip()
                if val:
                    item.v.addWidget(self._body(f"<b>{lab}：</b>{val}"))
            item.clicked.connect(self._on_pick)
            self._shot_items.append(item)
            self._shots_v.addWidget(item)

    def _fill_lines(self):
        if not self._lines:
            self._lines_v.addWidget(self._plain("（无口播逐字稿）"))
            return
        for i, ln in enumerate(self._lines):
            item = _PickItem("line", i, ln["start"])
            lab = QLabel(f"{_fmt_sec(ln['start'])}　{ln['text']}")
            lab.setWordWrap(True)
            lab.setTextFormat(Qt.TextFormat.RichText)
            lab.setObjectName("LineText")
            item.v.addWidget(lab)
            item.clicked.connect(self._on_pick)
            self._line_items.append(item)
            self._lines_v.addWidget(item)

    # 板块类型 → 配色（按类型名哈希取色，同类型同色，一眼可辨）
    _BLOCK_COLORS = ("#F5722C", "#7C5CFF", "#00A870", "#2F6BFF", "#D83931",
                     "#0FC6C2", "#EB5DA0", "#FAAD14")

    def _block_color(self, btype):
        idx = sum(ord(c) for c in (btype or "")) % len(self._BLOCK_COLORS)
        return self._BLOCK_COLORS[idx]

    def _fill_blocks(self):
        if not self._blocks:
            self._blocks_v.addWidget(self._plain("（无板块拆分数据）"))
            return
        for i, b in enumerate(self._blocks):
            item = _PickItem("block", i, b["start"])
            color = self._block_color(b.get("type", ""))
            head = QLabel(
                f"<span style='color:{color}; font-weight:700;'>●</span>　"
                f"{html.escape(b.get('type', '') or '板块')}　"
                f"{_fmt_sec(b['start'])}–{_fmt_sec(b['end'])}")
            head.setTextFormat(Qt.TextFormat.RichText)
            head.setObjectName("ItemHead")
            item.v.addWidget(head)
            if (b.get("summary") or "").strip():
                item.v.addWidget(self._muted(b["summary"].strip()))
            item.clicked.connect(self._on_pick)
            self._block_items.append(item)
            self._blocks_v.addWidget(item)

    def _muted(self, t):
        lbl = QLabel(t)
        lbl.setWordWrap(True)
        lbl.setObjectName("Muted")
        lbl.setStyleSheet(f"color:{FS_WEAK}; font-size:11px; background:transparent;")
        return lbl

    def _body(self, html_text):
        lbl = QLabel(html_text)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setStyleSheet(tokenize("color:#1F2329; font-size:12px; background:transparent;"))
        return lbl

    def _fill_thumbs(self):
        self.thumbs.clear()
        for g in self._gal:
            it = QListWidgetItem()
            pix = QPixmap(g.get("path", ""))
            if not pix.isNull():
                it.setIcon(QIcon(pix.scaled(
                    96, 64, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)))
            it.setData(Qt.ItemDataRole.UserRole, float(g.get("ts", 0)))
            it.setToolTip(_fmt_sec(g.get("ts", 0)))
            self.thumbs.addItem(it)

    def _set_source(self, video_path):
        self._video_path = video_path or ""
        if not self._video_path or not Path(self._video_path).exists():
            self._stage_box.setVisible(False)
            self.video_placeholder.setVisible(True)
            self.btn_play.setEnabled(False)
            return
        self._stage_box.setVisible(True)
        self.video_placeholder.setVisible(False)
        self.player.setSource(QUrl.fromLocalFile(self._video_path))

    # ------------------------------------------------------------------
    # 播放器控制
    # ------------------------------------------------------------------
    def _toggle_play(self):
        if not self._video_ok():
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self._stage.setCurrentIndex(0)   # 露出视频画面、收起封面遮罩
            self.player.play()

    def _seek_ms(self, ms):
        self.player.setPosition(max(0, int(ms)))
        self._show_time(ms)

    def _on_duration(self, dur):
        self.slider.setRange(0, max(0, int(dur)))

    def _on_state(self, state):
        self.btn_play.setText(
            "⏸" if state == QMediaPlayer.PlaybackState.PlayingState else "▶")

    def _show_time(self, ms):
        dur = self.player.duration() or 0
        self.lbl_time.setText(f"{_fmt_sec(ms / 1000)} / {_fmt_sec(dur / 1000)}")

    def _on_pos(self, ms):
        self._last_pos_ms = ms
        if not self.slider.isSliderDown():
            self.slider.setValue(int(ms))
        self._show_time(ms)
        if not self._tick.isActive():
            self._tick.start()

    def _flush_pos(self):
        self._apply_current(self._last_pos_ms / 1000.0, from_user=False)

    # ------------------------------------------------------------------
    # 联动核心（唯一真源）
    # ------------------------------------------------------------------
    def _on_pick(self, kind, idx, sec):
        if self._video_ok():
            self._mute = True
            self._stage.setCurrentIndex(0)   # 点时间戳也露出画面
            self.player.setPosition(int(sec * 1000))
            self.slider.setValue(int(sec * 1000))
            QTimer.singleShot(120, lambda: setattr(self, "_mute", False))
        self._apply_current(sec, from_user=True)

    def _on_thumb(self, item):
        sec = float(item.data(Qt.ItemDataRole.UserRole) or 0)
        self._on_pick("gallery", -1, sec)

    def _video_ok(self):
        return bool(getattr(self, "_video_path", "")) and Path(self._video_path).exists()

    def _apply_current(self, sec, from_user=False):
        from video_text_tools.breakdown.timeline import index_at
        shots_ts = [s["ts"] for s in self._shots]
        lines_ts = [ln["start"] for ln in self._lines]
        gal_ts = [g.get("ts", 0) for g in self._gal]
        # 板块轴：按 start 升序定位当前段（落在 [start,end) 内的那一块）
        block_starts = [b["start"] for b in self._blocks]

        si = index_at(shots_ts, sec)
        li = index_at(lines_ts, sec)
        gi = index_at(gal_ts, sec)
        bi = index_at(block_starts, sec)
        if bi >= 0 and sec >= self._blocks[bi]["end"]:
            bi = -1                       # 超出当前板块尾→不点亮（空隙不亮）
        if gi >= 0:
            self._set_big(self._gal[gi].get("path", ""))
        if si != self._cur_shot:
            self._highlight(self._shot_items, si)
            if si >= 0 and (from_user or self._cur_shot >= 0):
                self._scroll_to(self._shot_items[si])
            self._cur_shot = si
        if li != self._cur_line:
            self._highlight(self._line_items, li)
            if li >= 0 and (from_user or self._cur_line >= 0):
                self._scroll_to(self._line_items[li])
            self._cur_line = li
        if bi != self._cur_block:
            self._highlight(self._block_items, bi)
            if bi >= 0 and (from_user or self._cur_block >= 0):
                self._scroll_to(self._block_items[bi])
            self._cur_block = bi
        if from_user and self._gal:
            self._select_thumb(max(gi, 0))

    def _set_big(self, path):
        if not path or not Path(path).exists():
            return
        pix = QPixmap(path)
        if pix.isNull():
            return
        self.big.setText("")
        self.big.setPixmap(pix.scaled(self.big.size(),
                                      Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))

    @staticmethod
    def _highlight(items, active_idx):
        for i, it in enumerate(items):
            it.set_active(i == active_idx)

    def _scroll_to(self, widget):
        try:
            self.doc_scroll.widget().ensureWidgetVisible(widget, 0, 40)
        except Exception:
            pass

    def _select_thumb(self, row):
        self.thumbs.blockSignals(True)
        if 0 <= row < self.thumbs.count():
            self.thumbs.setCurrentRow(row)
        self.thumbs.blockSignals(False)

    # ------------------------------------------------------------------
    def _stop_play(self):
        """停播（幂等）：任何隐藏路径都调它，杜绝关窗后音频/视频仍在后台跑。"""
        try:
            self.player.stop()
        except Exception:
            pass

    def closeEvent(self, e):
        """close() 路径（顶部「✕ 关闭」）：关窗即停播。"""
        self._stop_play()
        super().closeEvent(e)

    def hideEvent(self, e):
        """标题栏右上角「✕」对 QDialog 走的是 reject()→done()→hide()，
        不触发 closeEvent；这里补一刀，保证那条路径也停播。stop() 幂等，
        与 closeEvent 并存不会重复出问题。"""
        self._stop_play()
        super().hideEvent(e)

    def _open_video_dir(self):
        vp = getattr(self, "_video_path", "")
        if vp and Path(vp).exists():
            reveal_in_folder(vp)
        elif self._result and self._result.gallery:
            open_path(str(Path(self._result.gallery[0]["path"]).parent.parent))

    def _open_report(self):
        rep = getattr(self._result, "report_path", "") if self._result else ""
        if rep and Path(rep).exists():
            open_path(rep)

    def _open_cut(self):
        """弹出「切割入素材库」：选自动/手动 + 归属产品 → 后台 ToolWorker 跑 run_cuts。"""
        if self._result is None:
            return
        if not (self._result.video_path and Path(self._result.video_path).exists()):
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "提示", "源视频不存在或已被移动，无法切割")
            return
        dlg = _CutDialog(self, self._result)
        dlg.exec()


# ==================================================================
# 板块/手选区间 → 切割 → 归档素材库
class _CutDialog(QDialog):
    """从拆解详情弹出的切割归档面板。

    自动：取 result.blocks 逐块；手动：在轴上点选区间（也可手填起止）。
    切割在后台线程跑（不卡界面），实时写日志；完成后素材库页即可看到片段。"""

    def __init__(self, host, result, parent=None):
        super().__init__(parent or host)
        self._host = host
        self._result = result
        self._manual = []          # [{type,start,end,summary}]
        self._worker = None
        self.setWindowTitle("✂ 切割入素材库")
        self.resize(560, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # 归属产品（可下拉选已有，也可直接输入新产品名）
        prow = QHBoxLayout()
        prow.addWidget(QLabel("归属产品："))
        self.cb_product = QComboBox()
        self.cb_product.setEditable(True)
        try:
            from store import product_store as ps
            self.cb_product.addItems([""] + ps.product_names())
        except Exception:
            self.cb_product.addItem("")
        self.cb_product.setCurrentText("")
        prow.addWidget(self.cb_product, 1)
        root.addLayout(prow)

        # 模式：自动 / 手动
        mode = QHBoxLayout()
        self.rb_auto = QRadioButton("自动（全部板块）")
        self.rb_manual = QRadioButton("手动（自选区间）")
        self.rb_auto.setChecked(bool(getattr(result, "blocks", None)))
        if not getattr(result, "blocks", None):
            self.rb_manual.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_auto)
        grp.addButton(self.rb_manual)
        self.rb_auto.toggled.connect(self._sync_manual_enabled)
        mode.addWidget(self.rb_auto)
        mode.addWidget(self.rb_manual)
        mode.addStretch(1)
        root.addLayout(mode)

        n = len(getattr(result, "blocks", None) or [])
        self.lbl_blocks = QLabel(f"识别到 {n} 个板块 · 自动将逐块切片段")
        self.lbl_blocks.setStyleSheet(f"color:{FS_WEAK}; font-size:12px;")
        root.addWidget(self.lbl_blocks)

        # 手动区间编辑器
        self.manual_box = QWidget()
        mv = QVBoxLayout(self.manual_box)
        mv.setContentsMargins(0, 0, 0, 0)
        mv.setSpacing(6)
        editor = QHBoxLayout()
        self.cb_type = QComboBox()
        self.cb_type.setEditable(True)
        self.cb_type.addItems(self._block_types())
        self.sp_start = QDoubleSpinBox()
        self.sp_start.setRange(0, 99999)
        self.sp_start.setDecimals(1)
        self.sp_start.setSuffix(" 秒")
        self.sp_end = QDoubleSpinBox()
        self.sp_end.setRange(0, 99999)
        self.sp_end.setDecimals(1)
        self.sp_end.setSuffix(" 秒")
        b_use_pos = QPushButton("⏱ 取当前位置")
        b_use_pos.setObjectName("GhostBtn")
        b_use_pos.setToolTip("把播放器当前时间填入起点（再手动调终点）")
        b_use_pos.clicked.connect(self._use_playhead)
        b_add = QPushButton("＋ 加入清单")
        b_add.clicked.connect(self._add_manual)
        for w in (self.cb_type, self.sp_start, self.sp_end, b_use_pos, b_add):
            editor.addWidget(w)
        mv.addLayout(editor)
        self.list_manual = QListWidget()
        self.list_manual.setMinimumHeight(96)
        mv.addWidget(self.list_manual)
        b_del = QPushButton("－ 移除选中")
        b_del.setObjectName("GhostBtn")
        b_del.clicked.connect(self._del_manual)
        mv.addWidget(b_del, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self.manual_box)

        # 精确度：快切(copy) / 重编码（边界更准但慢）
        prec = QHBoxLayout()
        self.ck_reencode = QCheckBox("精确切割（重编码，帧准但较慢；默认按关键帧快切）")
        prec.addWidget(self.ck_reencode)
        prec.addStretch(1)
        root.addLayout(prec)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("font-size:12px;")
        root.addWidget(self.log, 1)

        btns = QDialogButtonBox()
        self.b_run = btns.addButton("✂ 开始切割", QDialogButtonBox.ButtonRole.AcceptRole)
        self.b_close = btns.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        self.b_run.clicked.connect(self._run)
        btns.rejected.connect(self.close)
        root.addWidget(btns)

        self._sync_manual_enabled()

    # ---------- 手动清单 ----------
    @staticmethod
    def _block_types():
        try:
            from core import block_categories
            return block_categories.load()
        except Exception:
            return []

    def _sync_manual_enabled(self):
        self.manual_box.setVisible(self.rb_manual.isChecked())
        self.lbl_blocks.setVisible(self.rb_auto.isChecked())

    def _use_playhead(self):
        sec = getattr(self._host, "_last_pos_ms", 0) / 1000.0
        self.sp_start.setValue(sec)
        if self.sp_end.value() <= sec:
            self.sp_end.setValue(sec + 3.0)

    def _add_manual(self):
        btype = (self.cb_type.currentText() or "").strip() or "片段"
        start, end = float(self.sp_start.value()), float(self.sp_end.value())
        if end <= start:
            self._append_log("⚠ 终点需大于起点")
            return
        self._manual.append({"type": btype, "start": start, "end": end})
        self.list_manual.addItem(f"{btype}  {start:.1f}–{end:.1f} 秒")

    def _del_manual(self):
        row = self.list_manual.currentRow()
        if row < 0:
            return
        self.list_manual.takeItem(row)
        del self._manual[row]

    def _append_log(self, text):
        self.log.appendPlainText(str(text))

    # ---------- 执行 ----------
    def _run(self):
        if self._worker is not None and self._worker.isRunning():
            return
        from video_text_tools.breakdown import splitter
        mode = "manual" if self.rb_manual.isChecked() else "auto"
        product = (self.cb_product.currentText() or "").strip()
        if mode == "manual" and not self._manual:
            self._append_log("⚠ 手动模式先至少加入一段区间")
            return
        reencode = self.ck_reencode.isChecked()
        items = splitter.plan_cuts(self._result, mode=mode, manual=self._manual,
                                   product=product, reencode=reencode)
        if not items:
            self._append_log("⚠ 无可切割区间（板块为空或源视频不可用）")
            return
        self._append_log(f"计划切割 {len(items)} 段 → 素材库/{product or '未归类'}")
        self.b_run.setEnabled(False)
        self.b_run.setText("切割中…")

        def fn(log, progress, should_stop):
            return splitter.run_cuts(items, log=log)

        from gui.tool_panels import ToolWorker
        self._worker = ToolWorker(fn, self)
        self._worker.log.connect(self._append_log)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, res):
        self.b_run.setEnabled(True)
        self.b_run.setText("✂ 开始切割")
        self._worker = None
        if isinstance(res, Exception):
            self._append_log(f"✗ 切割异常：{res}")
            return
        self._append_log(
            f"✓ 完成：成功 {len(res.get('cut', []))}、失败 {len(res.get('failed', []))}")

    def closeEvent(self, e):
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(2000)
        super().closeEvent(e)


_DETAIL_QSS = f"""
#DetailShell {{ background:#FFFFFF; border:1px solid #E5E6EB; border-radius:10px; }}
#Panel {{ background:#F7F8FA; border:1px solid #E5E6EB; border-radius:8px; }}
#Panel QLabel#CapTitle {{ font-size:13px; font-weight:700; color:{FS_SUB};
    background:transparent; }}
#PickItem {{ background:#FFFFFF; border:1px solid #E5E6EB; border-left:3px solid transparent;
    border-radius:6px; }}
#PickItem:hover {{ border-left:3px solid #C9D6FF; }}
#PickItem[active="true"] {{ background:#EAF1FF; border-left:3px solid {FS_BLUE}; }}
#PickItem QLabel {{ background:transparent; }}
#PickItem QLabel#ItemHead {{ font-size:12px; font-weight:600; color:#1F2329; }}
#ListWidget {{ background:#FFFFFF; border:1px solid #E5E6EB; border-radius:6px; }}
"""
