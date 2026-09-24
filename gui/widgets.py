"""
gui/widgets.py —— 无边框视频播放器 + 图片预览 + 提示词悬停预览浮层 + Toast
"""
import html
import re
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QPoint, QSize, QTimer, Signal, QKeyCombination
from PySide6.QtGui import (QGuiApplication, QPixmap, QPainter, QPen, QColor, QCursor,
                           QRegion, QPainterPath, QTransform, QKeySequence)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QScrollArea, QSlider,
                               QPushButton, QLabel, QWidget, QMessageBox,
                               QComboBox, QSizeGrip, QSizePolicy, QLineEdit, QInputDialog)

from core import translate
from gui.tool_panels import API_MAINTAINER_CODE, MAINTAINER_SHORTCUT, ToolWorker
from store import app_state
from utils.desktop_utils import open_path, reveal_in_folder


def _fmt_ms(ms):
    s = int(ms / 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


def _fmt_rate(r):
    """0.5 → 0.5；1.0 → 1（倍速列表里不想看到“1.0x”这种写法）"""
    return f"{float(r):g}"


# 倍速预设：审片时 1.5x/2x 快速过片、逐帧对口型时 0.5x
RATES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)

# 快退/快进步长：一条成品就 5~15 秒，写死 5 秒既不好对口型也不好跳读，
# 所以改成「1~5 秒」下拉 + 加减号，选过的步长记进 ui_state（下次还在）
STEP_SECONDS = (1, 2, 3, 4, 5)
STEP_DEFAULT = 5                        # 没记过时的默认（与旧版写死的 5 秒一致）
STEP_KEY = "player_step_sec"

# 深色播放器外壳。选择器全部挂在 #PlayerShell 下：应用级样式表里已经有
# QPushButton{...}，不拿 id 提高优先级的话按钮会被全局浅色样式盖掉。
# 也不用 WA_TranslucentBackground 做真圆角：透明窗口叠硬解视频在 Windows 上
# 会把画面区域变成一块黑，宁可角上直角。
_SHELL_QSS = """
#PlayerRoot { background:#15171C; }
#PlayerShell { background:#15171C; border:1px solid #2A2E37; border-radius:10px; }
#PlayerShell QLabel { color:#C9CFDA; background:transparent; font-size:12px; }
#PlayerShell QLabel#FileName { color:#F2F5FA; font-size:12px; font-weight:600; }
#PlayerShell QLabel#StatusTip { color:#8A93A3; font-size:11px; }
#PlayerShell QPushButton, #PlayerShell QToolButton {
    background:#252A33; color:#DCE1EA; border:0; border-radius:6px; padding:4px 9px; }
#PlayerShell QPushButton:hover, #PlayerShell QToolButton:hover { background:#323A47; }
#PlayerShell QPushButton:checked { background:#3370FF; color:#FFFFFF; }
#PlayerShell QPushButton#MarkOk:checked { background:#12A150; color:#FFFFFF; }
#PlayerShell QPushButton#MarkBad:checked { background:#D94A43; color:#FFFFFF; }
#PlayerShell QPushButton#CloseBtn:hover { background:#D94A43; color:#FFFFFF; }
#PlayerShell QComboBox { background:#252A33; color:#DCE1EA; border:0;
    border-radius:6px; padding:3px 6px; }
#PlayerShell QComboBox QAbstractItemView { background:#252A33; color:#DCE1EA;
    selection-background-color:#3370FF; selection-color:#FFFFFF;
    border:1px solid #3A4150; outline:none; }
/* 弹层列表的每个条目：默认深底浅字，鼠标划过/选中给蓝底白字。
   必须显式写 ::item，否则会被应用级浅色主题里的
   `QComboBox QAbstractItemView::item:hover/selected {background:#EAF1FF}` 顶掉——
   表现就是深黑列表上冒出一个几乎白色的选中块，看不清也反人类。 */
#PlayerShell QComboBox QAbstractItemView::item { color:#DCE1EA; background:#252A33;
    padding:4px 8px; min-height:22px; }
#PlayerShell QComboBox QAbstractItemView::item:hover,
#PlayerShell QComboBox QAbstractItemView::item:selected {
    background:#3370FF; color:#FFFFFF; }
#PlayerShell QSlider::groove:horizontal { height:6px; background:#31363F; border-radius:3px; }
#PlayerShell QSlider::sub-page:horizontal { background:#3370FF; border-radius:3px; }
#PlayerShell QSlider::handle:horizontal { width:16px; height:16px; margin:-5px 0;
    border-radius:8px; background:#EAF0FA; }
#PlayerShell QSlider::handle:horizontal:pressed { background:#FFFFFF; }
#PlayerShell QSizeGrip { background:transparent; }
"""


class _SeekSlider(QSlider):
    """进度条：除了拖着走，点哪儿就跳到哪儿

    默认 QSlider 点凹槽只是按 singleStep 挪一格（毫秒级的时长等于没反应），
    手柄又只有几像素宽，使用者就判定了「进度条拖不动」。
    这里自己把鼠标位置换算成进度，按下/拖动/抬起都直接定位。"""

    seeked = Signal(int)

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setFixedHeight(20)             # 抬高命中区，好点中手柄
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

    def _value_at(self, x):
        half = 8                            # 手柄半径（与上面 QSS 的 16px 对应）
        span = max(self.width() - 2 * half, 1)
        r = min(max((x - half) / span, 0.0), 1.0)
        return int(self.minimum() + r * (self.maximum() - self.minimum()))

    def _jump(self, x):
        if self.maximum() <= self.minimum():
            return                          # 时长还没回来：不拦着报错就行
        v = self._value_at(int(x))
        self.setValue(v)                    # setValue 不发 sliderMoved，自己补一个
        self.seeked.emit(v)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self._jump(e.position().x())
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self.isSliderDown():
            self._jump(e.position().x())
            e.accept()
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self.isSliderDown():
            self._jump(e.position().x())
            self.setSliderDown(False)
            e.accept()
        else:
            super().mouseReleaseEvent(e)


class VideoPlayerDialog(QDialog):
    """无边框播放器：播放/暂停、进度（可拖可点）、步长快进退、倍速、全屏、
    横竖屏自适应、审片标记

    无边框就得自己把窗口的事管完：顶部一条可拖动的标题栏（定位/全屏/最小化/关闭），
    右下角 QSizeGrip 拉大，Esc 关、双击画面全屏、单击画面播放/暂停。

    allow_mark：只有【成品视频】才给标记按钮。素材库里预览的是同事传上来的
    原材，误点一下就把素材改名了，所以默认不给。

    marked(new_path, mark)：标记完成（含改名）后发出来，父页面据此刷新列表。
    """

    marked = Signal(str, str)

    MIN_W, MIN_H = 360, 220             # 画面区最小尺寸（也是没报出视频尺寸时的兜底）
    # 自适应时画面可占屏幕可用区的比例。已在原 0.88 / 0.94 基础上整体缩小约 30%，
    # 作为新的默认基准；设置页「视频预览框大小」的百分比再在这个基准上乘系数。
    FILL_W, FILL_H = 0.62, 0.66
    FALLBACK_FILL = 0.35                # 后端还没报视频尺寸时的半屏兜底，同样缩过（原 0.5）
    REFIT_EPS = 0.015                   # 宽高比变化超过这个值才重摆（不跟用户抢尺寸）
    SHELL_RADIUS = 10                   # 外壳圆角半径（与 _SHELL_QSS 的 border-radius 对齐）
    # 预览框大小缩放（百分比）：存 ui_state，播放器每次打开时读取即时生效。
    SCALE_KEY = "player_preview_scale"
    SCALE_MIN, SCALE_MAX, SCALE_DEFAULT = 40, 160, 100

    def __init__(self, parent, file_path, allow_mark=False):
        super().__init__(parent)
        self._path = str(file_path or "")
        self._allow_mark = bool(allow_mark)
        self._drag_pos = None            # 无边框拖动：按下时的坐标偏移
        self._fitted = None              # 已按哪个宽高比摆过（None=还没摆）
        self._swapping = False           # 换文件途中屏蔽报错
        self._fullscreen = False
        self._last_size = None           # 视频真实宽高（退出全屏时重摆一次）

        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("PlayerRoot")        # 供 #PlayerRoot 规则给四角补上深色底，避免白角
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)   # 让 #PlayerRoot 背景真能绘出来
        self.setStyleSheet(_SHELL_QSS)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizeGripEnabled(True)

        shell = QWidget(objectName="PlayerShell")
        out = QVBoxLayout(self)
        out.setContentsMargins(0, 0, 0, 0)
        out.addWidget(shell)
        lay = QVBoxLayout(shell)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        # ---------- 顶部：拖动区 + 窗口按钮 ----------
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.lbl_name = QLabel(objectName="FileName")
        # 文件名动辄二三十字，不降到「可压缩」的话它会拿自己的最小宽度
        # 顶住整个窗口，竖屏成品就被带成两侧一大块黑边（完整名在 tooltip 里）
        self.lbl_name.setSizePolicy(QSizePolicy.Policy.Ignored,
                                    QSizePolicy.Policy.Preferred)
        bar.addWidget(self.lbl_name, 1)
        self.btn_reveal = QPushButton("📂 定位")
        self.btn_reveal.setToolTip("打开成品所在文件夹并选中这个文件（G）")
        self.btn_reveal.clicked.connect(self._reveal)
        self.btn_full = QPushButton("⛶ 全屏")
        self.btn_full.setCheckable(True)
        self.btn_full.setToolTip("全屏 / 退出全屏（F 或双击画面）")
        self.btn_full.toggled.connect(self._set_fullscreen)
        b_min = QPushButton("―")
        b_min.setToolTip("最小化")
        b_min.clicked.connect(self.showMinimized)
        b_close = QPushButton("✕")
        b_close.setObjectName("CloseBtn")
        b_close.setToolTip("关闭（Esc）")
        b_close.clicked.connect(self.close)
        for b in (self.btn_reveal, self.btn_full, b_min, b_close):
            bar.addWidget(b)
        lay.addLayout(bar)

        # ---------- 画面 ----------
        self.video = QVideoWidget()
        self.video.setMinimumSize(self.MIN_W, self.MIN_H)
        self.video.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.video.mousePressEvent = self._video_clicked
        lay.addWidget(self.video, 1)

        # ---------- 媒体（先建好，控制条要接它的信号） ----------
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)

        # ---------- 控制条（拆两行）----------
        # 为什么拆：竖屏成品要的是窄窗口，一行摆下 9 个控件的话
        # 布局算出来的最小宽度会把窗口顶宽，多出来的那截就成了画面两侧的黑边。
        ctrl = QHBoxLayout()
        ctrl.setSpacing(6)
        self.btn_play = QPushButton("▶")
        self.btn_play.setToolTip("播放 / 暂停（空格）")
        self.btn_play.setFixedWidth(40)
        self.btn_play.clicked.connect(self._toggle_play)
        b_back = QPushButton("－")        # 全角减号：比半角"-"好认也好点
        b_back.setObjectName("StepBack")
        b_back.setFixedWidth(34)
        b_back.setToolTip("按步长后退（←）")
        b_back.clicked.connect(lambda: self._step_seek(-1))
        self.cb_step = QComboBox()
        for s in STEP_SECONDS:
            self.cb_step.addItem(f"{s}秒", s)
        self._restore_step()
        self.cb_step.setToolTip("快退 / 快进的步长（1~5 秒）\n选过的下次开播放器还在")
        self.cb_step.currentIndexChanged.connect(self._step_changed)
        b_fwd = QPushButton("＋")
        b_fwd.setObjectName("StepFwd")
        b_fwd.setFixedWidth(34)
        b_fwd.setToolTip("按步长前进（→）")
        b_fwd.clicked.connect(lambda: self._step_seek(1))
        self.slider = _SeekSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setMinimumWidth(70)
        self.slider.sliderMoved.connect(self._seek_to)
        self.slider.seeked.connect(self._seek_to)
        self.lbl_time = QLabel("00:00 / 00:00")
        self.lbl_time.setToolTip("当前位置 / 总时长（进度条上点一下就能跳到那儿）")
        for w in (self.btn_play, b_back, self.cb_step, b_fwd,
                  self.slider, self.lbl_time):
            ctrl.addWidget(w)
        lay.addLayout(ctrl)
        self.btn_back, self.btn_fwd = b_back, b_fwd

        # ---------- 第二行：倍速 / 音量 / 循环 +（只给成品的）审片按钮 ----------
        mark_bar = QHBoxLayout()
        mark_bar.setSpacing(6)
        self.cb_rate = QComboBox()
        self.cb_rate.addItems([f"{_fmt_rate(r)}x" for r in RATES])
        self.cb_rate.setCurrentIndex(list(RATES).index(1.0))
        self.cb_rate.setToolTip("播放倍速（, 减速 / . 加速 / 斜杠回到 1x）\n"
                                "审片用 2x 过片、看口型用 0.5x")
        self.cb_rate.currentIndexChanged.connect(self._rate_changed)
        self.btn_mute = QPushButton("🔊")
        self.btn_mute.setCheckable(True)
        self.btn_mute.setToolTip("静音 / 取消静音（M）")
        self.btn_mute.toggled.connect(self._set_muted)
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(100)
        self.vol.setFixedWidth(64)
        self.vol.setToolTip("音量（↑ / ↓）")
        self.vol.valueChanged.connect(self._set_volume)
        self.btn_loop = QPushButton("🔁")
        self.btn_loop.setCheckable(True)
        self.btn_loop.setToolTip("循环播放：同一条反复看（L）")
        for w in (self.cb_rate, self.btn_mute, self.vol, self.btn_loop):
            mark_bar.addWidget(w)
        mark_bar.addStretch(1)
        self.lbl_mark = QLabel("审片：")
        self.btn_ok = QPushButton("👍 可用")
        self.btn_ok.setObjectName("MarkOk")
        self.btn_ok.setCheckable(True)
        self.btn_ok.setToolTip("标为可用：文件名上的「_不可用」记号会被去掉\n"
                               "（再点一次＝取消标记）")
        self.btn_bad = QPushButton("👎 不可用")
        self.btn_bad.setObjectName("MarkBad")
        self.btn_bad.setCheckable(True)
        self.btn_bad.setToolTip("标为不可用：文件名改成「…_不可用.mp4」，\n"
                                "回头用任务中心的「🧹 清理不可用」一次清掉（再点一次＝取消）")
        self.btn_ok.clicked.connect(lambda: self._on_mark("ok"))
        self.btn_bad.clicked.connect(lambda: self._on_mark("bad"))
        mark_bar.addWidget(self.lbl_mark)
        mark_bar.addWidget(self.btn_ok)
        mark_bar.addWidget(self.btn_bad)
        for w in (self.lbl_mark, self.btn_ok, self.btn_bad):
            w.setVisible(self._allow_mark)
        lay.addLayout(mark_bar)

        self.lbl_tip = QLabel(objectName="StatusTip")
        self.lbl_tip.setVisible(False)
        self.lbl_tip.setWordWrap(True)
        lay.addWidget(self.lbl_tip)

        self._grip = QSizeGrip(shell)          # 无边框时手动挂一个拖动块

        self.player.playbackStateChanged.connect(self._on_state)
        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(self._on_duration)
        self.player.errorOccurred.connect(self._on_err)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.playbackRateChanged.connect(self._on_rate)
        # 真实宽高比靠视频尺寸变化重排窗口（不依赖容器写没写 Resolution）
        self.video.videoSink().videoSizeChanged.connect(self._on_video_size)

        self._load(self._path, autoplay=True)
        self._sync_mark_buttons()

    # ---------------- 加载与换文件 ----------------

    def _load(self, path, autoplay=False):
        self._path = str(path)
        name = Path(self._path).name
        self.setWindowTitle(name)
        self.lbl_name.setText(name)
        self.lbl_name.setToolTip(self._path)
        if not Path(self._path).exists():
            self._say(f"文件不存在或已被移动：{self._path}", True)
            return
        self._swapping = True
        self.player.setSource(QUrl.fromLocalFile(self._path))
        self._swapping = False
        if autoplay:
            self.player.play()

    def _reload_after_rename(self, new_path, resume_ms, was_playing):
        """改名后重新指到新的路径，尽量接回原来的观看进度"""
        self._load(new_path, autoplay=False)
        if was_playing:
            self.player.play()
        if resume_ms:
            self.player.setPosition(resume_ms)

    # ---------------- 播放控制 ----------------

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _seek_rel(self, ms):
        self.player.setPosition(max(0, self.player.position() + int(ms)))

    def _step_sec(self):
        return int(self.cb_step.currentData() or STEP_DEFAULT)

    def _step_seek(self, direction):
        """±按钮与键盘 ←/→ 都走这里：步长由那个下拉说了算"""
        self._seek_rel(direction * self._step_sec() * 1000)

    def _restore_step(self):
        """步长接着上次的用（没记过才用默认）

        必须赶在接 currentIndexChanged 之前调，否则开一次窗口就写一次盘。"""
        want = app_state.get(STEP_KEY)
        for i in range(self.cb_step.count()):
            if self.cb_step.itemData(i) == want:
                self.cb_step.setCurrentIndex(i)
                return
        self.cb_step.setCurrentIndex(list(STEP_SECONDS).index(STEP_DEFAULT))

    def _step_changed(self, idx):
        sec = int(self.cb_step.itemData(idx) or STEP_DEFAULT)
        self.btn_back.setToolTip(f"后退 {sec} 秒（←）")
        self.btn_fwd.setToolTip(f"前进 {sec} 秒（→）")
        app_state.set_value(STEP_KEY, sec)

    def _seek_to(self, ms):
        """进度条定位：拖动、点哪儿跳哪儿都走这一条"""
        self.player.setPosition(max(0, int(ms)))
        self._show_pos(ms)          # 先本地回显，不等 positionChanged 绕一圈

    def _rate_changed(self, idx):
        try:
            self.player.setPlaybackRate(float(RATES[idx]))
        except (IndexError, ValueError):
            pass

    def _step_rate(self, delta):
        self.cb_rate.setCurrentIndex(min(max(self.cb_rate.currentIndex() + delta, 0),
                                         len(RATES) - 1))

    def _on_rate(self, rate):
        """把后端实际生效的倍速回写到下拉框

        解码器不支持时 setPlaybackRate 会静默失败，不回调回来就看不出来。"""
        for i, r in enumerate(RATES):
            if _fmt_rate(r) == _fmt_rate(rate):
                if self.cb_rate.currentIndex() != i:
                    self.cb_rate.setCurrentIndex(i)
                return

    def _set_volume(self, v):
        self.audio.setVolume(max(0.0, min(1.0, v / 100.0)))
        if v and self.btn_mute.isChecked():
            self.btn_mute.setChecked(False)

    def _set_muted(self, on):
        self.audio.setMuted(on)
        self.btn_mute.setText("🔇" if on else "🔊")

    def _on_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.btn_play.setText("❚❚" if playing else "▶")

    def _on_duration(self, ms):
        """时长回来才把进度条撑开

        范围还是 0~0 时滑块是拖不动的，而个别文件的时长比首帧晚到，
        不能只在创建时搭一次。"""
        self.slider.setRange(0, max(int(ms), 0))
        self._show_pos(self.player.position())

    def _on_pos(self, pos):
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        self._show_pos(pos)

    def _show_pos(self, ms):
        self.lbl_time.setText(f"{_fmt_ms(ms)} / {_fmt_ms(self.player.duration())}")

    def _on_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self.btn_loop.isChecked():
            self.player.setPosition(0)
            self.player.play()

    def _on_err(self, err, text):
        # 注意枚举名是 QMediaPlayer.Error（不是 ErrorType），写错会让这条
        # 槽在真正出错时抛 AttributeError，用户反而看不到任何失败提示
        if self._swapping or err == QMediaPlayer.Error.NoError:
            return
        self._say(f"无法播放：{text or '文件可能已删除或编码不支持'}", True)

    def _say(self, msg, warn=False):
        self.lbl_tip.setText(msg)
        self.lbl_tip.setStyleSheet(f"color:{'#F5A623' if warn else '#8A93A3'};")
        self.lbl_tip.setVisible(bool(msg))

    # ---------------- 窗口：全屏 / 横竖屏自适应 / 无边框拖动 ----------------

    def _set_fullscreen(self, on):
        self._fullscreen = bool(on)
        if on:
            self.showFullScreen()
        else:
            self.showNormal()
            if self._last_size:
                # 退出全屏：重新按视频比例摆一次（_fitted 清空才能再触发）
                self._fitted = None
                self._fit_to_video(QSize(*self._last_size))
        self.btn_full.blockSignals(True)
        self.btn_full.setChecked(on)
        self.btn_full.blockSignals(False)

    def _toggle_fullscreen(self):
        self._set_fullscreen(not self._fullscreen)

    def _avail(self):
        return (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()

    def _scale_factor(self):
        """预览框大小系数：读设置页写入的百分比（存 ui_state），限幅 40%~160%

        100% 即「整体缩小 30% 后的新基准」；每次开播放器时重读，改完下次打开即生效。"""
        try:
            v = int(app_state.get(self.SCALE_KEY) or self.SCALE_DEFAULT)
        except (TypeError, ValueError):
            v = self.SCALE_DEFAULT
        v = max(self.SCALE_MIN, min(self.SCALE_MAX, v))
        return v / 100.0

    def _apply_round_mask(self):
        """把无边框窗口沿圆角剪出一个真圆角（用遮罩而不是透明窗口）

        以前只给 #PlayerShell 做了 border-radius，外层 QDialog 仍是直角，
        壳子圆角外的四个小三角露出窗口默认白底——就是那个“白角突出”。
        不用 WA_TranslucentBackground（会把硬解视频变黑），改用 setMask 把窗口
        区域剪成圆角矩形：四角直接透明掉到桌面，客户区仍完全不透明，视频不受影响。"""
        if self._fullscreen:
            self.clearMask()
            return
        w, h, r = self.width(), self.height(), self.SHELL_RADIUS
        if w <= 0 or h <= 0:
            return
        path = QPainterPath()
        path.addRoundedRect(0, 0, w, h, r, r)
        # 本 PySide6 不支持 QRegion(QPainterPath, FillRule) 重载（会回退到
        # 要求 QPolygon/QRect 的重载报错、遮罩为空）。转成多边形再构 region。
        poly = path.toFillPolygon(QTransform()).toPolygon()
        self.setMask(QRegion(poly))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply_round_mask()

    def _place(self, w, h):
        """摆窗 + 居中（居中按可用区算，别被菜单栏/Dock 顶到屏幕外）"""
        avail = self._avail()
        self.resize(min(w, avail.width()), min(h, avail.height()))
        self.move(avail.x() + (avail.width() - self.width()) // 2,
                  avail.y() + (avail.height() - self.height()) // 2)

    def showEvent(self, e):
        super().showEvent(e)
        if self._fitted is None and not self._fullscreen:
            # 后端还没报出视频尺寸（容器没写 Resolution 或报得比首帧晚）：
            # 先按半屏给一个够大的窗，不要把播放器开成一个巴掌大的方块，
            # 等视频尺寸到了再按真实比例重摆一次。
            avail = self._avail()
            s = self._scale_factor()
            self._place(int(avail.width() * self.FALLBACK_FILL * s),
                        int(avail.height() * self.FALLBACK_FILL * s))

    def _on_video_size(self, *_):
        """视频尺寸变化的通知：尺寸要自己从 sink 上取

        PySide6 6.11 里这条信号到 Python 槽是不带参数的（签名就是
        videoSizeChanged()），当初写成接一个 QSize，槽每次都被 0 参数调用、
        报个 TypeError 被 Qt 吃掉——横竖屏自适应等于完全没做。
        所以这里签 (self, *_)：带不带尺寸都能接。"""
        self._fit_to_video(self.video.videoSink().videoSize())

    def _fit_to_video(self, size):
        """按视频真实宽高比重排窗口：横屏视频给横窗、竖屏视频给窄高窗

        判据是「比例变没变」而不是「只摆第一次」：后端可能先报一个占位尺寸、
        准确尺寸随后才到，只摆一次会停在错的比例上；而比例没变时不重摆，
        用户拖大拖小不会被后续回调顶回去。
        这里只能粗算（外壳占多少算不准），真正的贴比例交给 _reconcile。"""
        w, h = int(size.width()), int(size.height())
        if w <= 0 or h <= 0 or self._fullscreen:
            return
        self._last_size = (w, h)
        cur = w / h
        if self._fitted is not None and abs(cur - self._fitted) < self.REFIT_EPS:
            return
        self._fitted = cur
        avail = self._avail()
        s = self._scale_factor()
        scale = min(avail.width() * self.FILL_W * s / w,
                    avail.height() * self.FILL_H * s / h)
        self._place(max(int(w * scale), self.MIN_W + 60),
                    max(int(h * scale), self.MIN_H + 60))
        # 上面的尺寸是「整窗」而不是「画面」，布局会把画面区压成另一个比例；
        # 等一轮事件让布局落地后再按实测尺寸补差（_reconcile 里会继续约正）。
        QTimer.singleShot(0, self._reconcile)

    def _reconcile(self):
        """把窗口修到「画面区尺寸 == 视频宽高比」

        黑边的唯一来源就是画布比例与视频比例不等，而外壳占多少没法凭空算准
        （控件摆几行、字体大一号都会变），所以先摆一次再拿实测的画面尺寸补差：
        先调高（高的余量最便宜），顶到屏幕上下限了再反过来调宽。"""
        if self._fullscreen or not self._last_size or not self.isVisible():
            return
        vw, vh = self.video.width(), self.video.height()
        if vw <= 0 or vh <= 0:
            return
        w, h = self._last_size
        avail = self._avail()
        want_h = vw * h / w                       # 画面宽 vw 时该有多高
        old_h = self.height()
        new_h = min(max(int(old_h + want_h - vh), self.minimumSizeHint().height()),
                    avail.height())
        if new_h != old_h:
            self.resize(self.width(), new_h)
        # 高度真被改过就要等一轮布局把新的画面高报回来；没改（已顶到上下限）
        # 直接走宽度那一步，否则 self.height() 已是新值，比对永远相等
        if abs(new_h - old_h) <= 2:
            self._reconcile_width()
        else:
            QTimer.singleShot(0, self._reconcile_width)

    def _reconcile_width(self):
        """高度顶到屏幕上限补不开时，反过来把宽度收到贴视频比例"""
        if self._fullscreen or not self._last_size or not self.isVisible():
            return
        vw, vh = self.video.width(), self.video.height()
        if vw <= 0 or vh <= 0:
            return
        w, h = self._last_size
        avail = self._avail()
        want_w = vh * w / h                       # 画面高 vh 时该有多宽
        if abs(want_w - vw) <= 2:
            return
        new_w = min(max(int(self.width() + want_w - vw), self.minimumSizeHint().width()),
                    avail.width())
        self.resize(new_w, self.height())

    def _video_clicked(self, e):
        """单击画面＝播放/暂停，双击＝全屏（双击时两下点击会把播放状态又拨回来）"""
        if e.detail() == 2:
            self._toggle_fullscreen()
        else:
            self._toggle_play()
        e.accept()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        # 全屏时不拖；按在按钮/滑块上时事件被子控件吃掉，不会走到这里
        if self._drag_pos is not None and not self._fullscreen \
                and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key.Key_Escape:
            if self._fullscreen:
                self._set_fullscreen(False)
            else:
                self.close()
        elif k in (Qt.Key.Key_Space, Qt.Key.Key_K):
            self._toggle_play()
        elif k == Qt.Key.Key_Left:
            self._step_seek(-1)
        elif k == Qt.Key.Key_Right:
            self._step_seek(1)
        elif k == Qt.Key.Key_Up:
            self.vol.setValue(min(100, self.vol.value() + 5))
        elif k == Qt.Key.Key_Down:
            self.vol.setValue(max(0, self.vol.value() - 5))
        elif k in (Qt.Key.Key_F, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._toggle_fullscreen()
        elif k == Qt.Key.Key_M:
            self.btn_mute.click()
        elif k == Qt.Key.Key_L:
            self.btn_loop.click()
        elif k in (Qt.Key.Key_Comma, Qt.Key.Key_BracketLeft):
            self._step_rate(-1)
        elif k in (Qt.Key.Key_Period, Qt.Key.Key_BracketRight):
            self._step_rate(1)
        elif k == Qt.Key.Key_Slash:
            self.cb_rate.setCurrentIndex(list(RATES).index(1.0))
        elif k == Qt.Key.Key_G:
            self._reveal()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        self.player.stop()
        super().closeEvent(e)

    # ---------------- 审片标记 ----------------

    def _reveal(self):
        if Path(self._path).exists():
            reveal_in_folder(self._path)
        else:
            self._say("文件不存在或已被移动", True)

    def _current_mark(self):
        if not self._allow_mark:
            return ""
        from processors import output_mark
        return output_mark.mark_of(self._path)

    def _sync_mark_buttons(self):
        mark = self._current_mark()
        self.btn_ok.setChecked(mark == "ok")
        self.btn_bad.setChecked(mark == "bad")

    def _on_mark(self, want):
        """点当前已选中的那个按钮＝取消标记

        改名前必须先松手：Windows 上改不了「正被打开」的文件，而媒体后端
        此刻就是拿着它的那个。"""
        if not self._allow_mark:
            return
        from processors import output_mark
        mark = self._current_mark()
        target = "" if mark == want else want
        resume = self.player.position()
        was_playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self._swapping = True
        self.player.stop()
        self.player.setSource(QUrl())        # 把文件句柄放掉
        self._swapping = False
        ok, new_path, msg = output_mark.set_mark(self._path, target)
        if not ok:
            self._say(msg, True)
            self._load(self._path, autoplay=was_playing)
            self._sync_mark_buttons()
            return
        self._reload_after_rename(new_path, resume, was_playing)
        self._say(msg)
        self._sync_mark_buttons()
        self.marked.emit(new_path, target)


class ImagePreviewDialog(QDialog):
    """图片预览：等比缩放显示，超大图可滚动查看"""

    def __init__(self, parent, file_path):
        super().__init__(parent)
        self.setWindowTitle(f"图片预览 - {Path(file_path).name}")
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(int(screen.width() * 0.6), 1100),
                    min(int(screen.height() * 0.7), 800))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        pm = QPixmap(str(file_path))
        if pm.isNull():
            QMessageBox.warning(self, "无法打开", f"图片无法显示：\n{file_path}")
            self.reject()
            return
        area = QScrollArea()
        area.setWidgetResizable(True)
        lbl = QLabel()
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setPixmap(pm.scaled(area.size().width() - 20, area.size().height() - 20,
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation))
        area.setWidget(lbl)
        lay.addWidget(area)
        bar = QHBoxLayout()
        bar.addStretch(1)
        b_open = QPushButton("📂 用系统默认程序打开")
        b_open.setObjectName("GhostBtn")
        b_open.clicked.connect(lambda: open_path(file_path))
        bar.addWidget(b_open)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        bar.addWidget(b_close)
        lay.addLayout(bar)


# 中文（含中文标点）连续片段：悬停预览时整段高亮，与英文镜头描述区分开
_CJK_RUN = re.compile(r"([\u4e00-\u9fff\u3000-\u303f\uff01-\uff5e、。！？“”‘’…—·\u2014]+)")


def _highlight_cjk_html(text):
    """转义 HTML 后，把每段中文包成橙底色 span；保留原有换行"""
    lines = []
    for line in text.split("\n"):
        esc = html.escape(line)
        lines.append(_CJK_RUN.sub(
            r'<span style="color:#B54708; background-color:#FFF4C4;">\1</span>', esc))
    return "<br>".join(lines)


class HoverPreview(QScrollArea):
    """全文预览浮层：限宽自动换行、超长可滚动、保留原文换行排版。

    两种模式：
    - 钉住（show_pinned）：单击/空格唤起，Popup 模式抓取键盘，任意键关闭、点其它地方也关闭；
    - 悬停（show_at + hide_soon）：保留旧延时防抖机制，钉住模式下不生效。

    钉住模式里 Alt+W（macOS ⌘+W）是“任意键关闭”的例外：它不关窗，而是维护人的
    中文对照开关。提示词多是英文写的，正在读预览时最想知道中文，不该逼人到
    编辑弹窗里再翻一遍。第一次要口令，之后同一份内容在原文/对照间来回切。
    """

    WIDTH = 460
    MAX_H = 340
    HIDE_DELAY = 300        # ms：离开单元格后给移入浮层留的反应时间
    # 单独按下修饰键（比如先按住 Alt）不算“按了个键”：否则 Alt+W 的第一个键
    # 就把浮层关掉了，组合键永远按不出来
    MOD_KEYS = (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt,
                Qt.Key.Key_Meta, Qt.Key.Key_CapsLock)

    def __init__(self, parent=None, host=None):
        super().__init__(parent)
        # ToolTip 窗口标志：不抢焦点、永远浮在最上层
        self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowOpacity(0.98)
        self.setStyleSheet("""
            QScrollArea { background:#FFFFFF; border:1px solid #C6CFDD; border-radius:8px; }
            QLabel { background:transparent; color:#2B3441; font-size:12px; padding:10px; }
            QScrollBar:vertical { width:8px; background:transparent; }
            QScrollBar::handle:vertical { background:#C6CFDD; border-radius:4px; }
        """)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.label = QLabel()
        self.label.setWordWrap(True)                       # 按宽度自动换行
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setWidget(self.label)
        self.setFixedWidth(self.WIDTH)
        self.setMaximumHeight(self.MAX_H)
        self._keep_rect = None                             # 全局坐标：视为“未离开”的区域
        self._pinned = False                # 钉住模式（单击/空格唤起）中
        self._hide_timer = QTimer(self)                    # 延时隐藏检查
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(self.HIDE_DELAY)
        self._hide_timer.timeout.connect(self._check_hide)

        # ---- 维护人翻译（只在钉住模式下可用）----
        # 浮层本身是 Popup，收不到 QShortcut，所以在 keyPressEvent 里自己比对按键
        self._summon = QKeySequence(MAINTAINER_SHORTCUT)[0]
        self._host = host or parent       # 口令框要有宿主窗口，否则孤零零弹在屏幕正中
        self._pin_text = ""               # 当前钉住的那份原文
        self._pin_rich = True
        self._zh = ""                     # 当前内容的中文对照
        self._zh_src = ""                 # 对照对应的原文（内容换了就得重翻）
        self._showing_zh = False
        self._unlocked = False            # 口令验过一次就不再问（本会话内）
        self._busy = False
        self._tip = ""                    # 一次性提示（无需翻译之类）
        self._err = ""
        self._worker = None

    def set_keep_rect(self, rect):
        self._keep_rect = rect

    @property
    def pinned(self):
        return self._pinned

    def _to_html(self, text, highlight):
        if highlight:
            return _highlight_cjk_html(text)
        return "<br>".join(html.escape(line) for line in str(text or "").split("\n"))

    def _fit(self):
        self.label.adjustSize()
        h = min(self.label.sizeHint().height() + 24, self.MAX_H)
        self.setFixedHeight(h)

    def _render(self, text, rich_text):
        """悬停模式：只铺内容，不带翻译尾注（没解锁的人不该看见 Alt+W）"""
        self._hide_timer.stop()
        if rich_text:
            # 富文本：中文字高亮，换行用 <br> 还原
            self.label.setTextFormat(Qt.TextFormat.RichText)
            self.label.setText(self._to_html(text, True))
        else:
            self.label.setTextFormat(Qt.TextFormat.PlainText)
            self.label.setText(text.rstrip())
        self._fit()

    def _repaint(self):
        """钉住模式重绘：内容 + 一行灰色尾注，高度跟着尾注一起算"""
        body = self._zh if self._showing_zh else self._pin_text
        # 中文高亮是给“英文里夹中文”用的，整篇对照再刷黄底就成了满屏荧光笔
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setText(self._to_html(body, self._pin_rich and not self._showing_zh)
                           + self._foot())
        self._fit()

    def _foot(self):
        if not self._unlocked:
            return ""
        if self._busy:
            tip = "🌐 翻译中，稍等…"
        elif self._err:
            tip = "⚠ " + self._err
        elif self._tip:
            tip = self._tip
        elif self._showing_zh:
            tip = "中文对照（提交给云端的仍是原文）· 再按 Alt+W 回原文"
        else:
            tip = "按 Alt+W 看中文对照 · 其它键关闭"
        return '<br><span style="color:#8F959E;">{}</span>'.format(html.escape(tip))

    def show_pinned(self, text, pos, rich_text=True):
        """单击/空格唤起：钉住显示，任意键关闭（Popup 模式自带键盘抓取）"""
        if not text or not text.strip():
            self.hide()
            return
        self._pinned = True
        self._pin_text = text
        self._pin_rich = rich_text
        self._showing_zh = False      # 换了内容就先看原文，别拿上一段的对照糊弄
        self._tip = ""
        self._err = ""
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self._repaint()
        self.move(self.clamp_to_screen(pos))
        self.show()
        self.setFocus()                         # Popup 模式下确保键盘事件进浮层

    def show_at(self, text, pos, rich_text=False):
        if not text or not text.strip():
            self.hide()
            return
        if self._pinned:
            return                              # 已钉住：悬停不覆盖正在阅读的内容
        if self.windowFlags() & Qt.WindowType.Popup:
            # 上一次钉住关闭后恢复 ToolTip 标志（不抢焦点、不自动关闭）
            self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self._render(text, rich_text)
        self.move(self.clamp_to_screen(pos))
        self.show()

    def hide_soon(self):
        """不立即隐藏：给鼠标留出移入浮层的时间；钉住模式不打扰"""
        if self._pinned:
            return
        if self.isVisible():
            self._hide_timer.start()

    def _check_hide(self):
        pos = QCursor.pos()
        if self.geometry().contains(pos) or \
                (self._keep_rect and self._keep_rect.contains(pos)):
            self._hide_timer.start()       # 还在浮层/表格内，继续观察
        else:
            self.hide()

    def hide(self):
        self._hide_timer.stop()
        was_pinned = self._pinned
        self._pinned = False
        super().hide()
        if was_pinned:
            # 恢复 ToolTip 标志，不影响下次悬停模式
            self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)

    def keyPressEvent(self, e):
        if not self._pinned:
            super().keyPressEvent(e)
            return
        e.accept()
        if e.key() in self.MOD_KEYS:
            return                              # 只按下了修饰键：浮层留着等第二个键
        if QKeyCombination(e.modifiers(), Qt.Key(e.key())) == self._summon:
            self._summon_zh()
            return
        self.hide()              # 其余任意键关闭

    # ================= 维护人：就地看中文对照 =================
    def _summon_zh(self):
        if not self._unlocked:
            self._unlock()
        elif self._showing_zh:
            self._showing_zh = False
            self._repaint()
        elif self._zh and self._zh_src == self._pin_text:
            self._showing_zh = True             # 翻过了直接切，不再发请求
            self._repaint()
        else:
            self._translate()

    def _unlock(self):
        """口令框是模态窗，会把 Popup 的键盘抓取顶掉：先收窗再问，问完原样钉回来"""
        text, rich, pos = self._pin_text, self._pin_rich, self.pos()
        self.hide()
        host = self._host or self
        code, ok = QInputDialog.getText(host, "维护人验证", "请输入维护人口令：",
                                        QLineEdit.EchoMode.Password)
        if ok and code == API_MAINTAINER_CODE:
            self._unlocked = True
            self.show_pinned(text, pos, rich)
            self._translate()
            return
        self.show_pinned(text, pos, rich)       # 没通过也别把人正在看的内容弄丢
        if ok:
            QMessageBox.warning(host, "口令错误", "维护人口令不正确")

    def _translate(self):
        if self._busy:
            return
        self._err = ""
        self._tip = ""
        if not translate.has_latin(self._pin_text):
            self._tip = "没看到英文，无需翻译"
            self._repaint()
            return
        self._busy = True
        self._repaint()
        src = self._pin_text                    # 回来后比对：期间可能已经换看了另一条
        self._worker = ToolWorker(lambda log, progress, stop: (src, translate.translate_mixed(src)),
                                  self)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, res):
        self._worker = None
        self._busy = False
        self._tip = ""
        if isinstance(res, Exception):
            self._err = str(res) or type(res).__name__
        else:
            src, zh = res
            if src != self._pin_text:
                self._tip = "看的已经换了内容，这次结果不用了"
            else:
                self._zh = str(zh or "")
                self._zh_src = src
                self._showing_zh = bool(self._zh)
                if not self._zh:
                    self._tip = "没翻出内容"
        self._repaint()

    def clamp_to_screen(self, pos):
        """防止浮层超出屏幕右侧/底部"""
        screen = QGuiApplication.primaryScreen().availableGeometry()
        x = min(pos.x(), screen.right() - self.width() - 8)
        y = min(pos.y(), screen.bottom() - self.height() - 8)
        return QPoint(max(x, screen.left()), max(y, screen.top()))


class Toast(QWidget):
    """右下角轻量浮层提示：可带一个动作按钮（如「撤销」），自动消失、不抢焦点。

    用于删除可撤销、本批任务完成等“提醒一下就够、不值得弹框打断”的场景。
    调用方需持有引用防 GC；close 时发 finished 信号便于自行清理引用。"""
    finished = Signal()

    def __init__(self, text, action_text="", msec=5000, on_action=None,
                 anchor=None, color="#3370FF", parent=None):
        super().__init__(parent, Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._on_action = on_action
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        box = QLabel(text)
        box.setObjectName("ToastBox")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setStyleSheet(
            f"QLabel#ToastBox{{background:#FFFFFF;color:#1F2329;"
            f"border:1px solid {color};border-left:4px solid {color};"
            f"border-radius:8px;padding:10px 14px;}}")
        lay.addWidget(box)
        if action_text:
            btn = QPushButton(action_text)
            btn.setObjectName("ToastBtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton#ToastBtn{background:#FFFFFF;color:#3370FF;"
                "border:1px solid #DEE0E3;border-radius:8px;padding:8px 12px;}"
                "QPushButton#ToastBtn:hover{border-color:#3370FF;}")
            btn.clicked.connect(self._do_action)
            lay.addWidget(btn)
        self.adjustSize()
        self._place(anchor)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.close)
        self._timer.start(msec)
        self.finished.connect(self.deleteLater)

    def _do_action(self):
        try:
            if self._on_action:
                self._on_action()
        finally:
            self.close()

    def _place(self, anchor):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        m = 18
        if anchor is not None and anchor.isVisible():
            g = anchor.mapToGlobal(QPoint(0, 0))
            x = g.x() + anchor.width() - self.width() - m
            y = g.y() + anchor.height() - self.height() - m
        else:
            x = screen.right() - self.width() - m
            y = screen.bottom() - self.height() - m
        x = max(screen.left() + 4, min(x, screen.right() - self.width() - 4))
        y = max(screen.top() + 4, min(y, screen.bottom() - self.height() - 4))
        self.move(x, y)

    def mousePressEvent(self, e):
        self.close()          # 点一下即收起
        super().mousePressEvent(e)

    def closeEvent(self, e):
        self._timer.stop()
        self.finished.emit()
        super().closeEvent(e)


class _Spinner(QWidget):
    """旋转弧线加载动画（纯 QPainter 绘制，无额外依赖）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(26, 26)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._timer.start(60)

    def stop(self):
        self._timer.stop()

    def _tick(self):
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#3370FF"), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        r = self.rect().adjusted(3, 3, -3, -3)
        p.drawArc(r, (90 - self._angle) * 16, -270 * 16)
        p.end()


class LoadingOverlay(QWidget):
    """轻量加载遮罩：盖在页面上，首次重查询刷新时显示转圈提示。

    用法：页面 __init__ 里 self._loading = LoadingOverlay(self)；
    showEvent 首次触发时 show_overlay()，再 QTimer.singleShot 延迟执行
    重刷新，让遮罩先绘制一帧，完成后 hide_overlay()。
    """

    def __init__(self, host):
        super().__init__(host)
        self._host = host
        self.setStyleSheet("background:rgba(250,251,252,225);")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch(1)
        self.spinner = _Spinner(self)
        lay.addWidget(self.spinner)
        lbl = QLabel("加载中，请稍候…")
        lbl.setStyleSheet("color:#646A73; font-size:13px; background:transparent;")
        lay.addWidget(lbl)
        lay.addStretch(1)
        self.hide()

    def show_overlay(self):
        self.setGeometry(self._host.rect())
        self.spinner.start()
        self.show()
        self.raise_()

    def hide_overlay(self):
        self.spinner.stop()
        self.hide()
