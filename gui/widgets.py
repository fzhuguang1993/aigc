"""
gui/widgets.py —— 无边框视频播放器 + 图片预览 + 提示词悬停预览浮层 + Toast
"""
import html
import re
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QPoint, QSize, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPixmap, QPainter, QPen, QColor, QCursor
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QScrollArea, QSlider,
                               QPushButton, QLabel, QWidget, QMessageBox,
                               QComboBox, QSizeGrip)

from utils.desktop_utils import open_path, reveal_in_folder


def _fmt_ms(ms):
    s = int(ms / 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


def _fmt_rate(r):
    """0.5 → 0.5；1.0 → 1（倍速列表里不想看到“1.0x”这种写法）"""
    return f"{float(r):g}"


# 倍速预设：审片时 1.5x/2x 快速过片、逐帧对口型时 0.5x
RATES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)

# 深色播放器外壳。选择器全部挂在 #PlayerShell 下：应用级样式表里已经有
# QPushButton{...}，不拿 id 提高优先级的话按钮会被全局浅色样式盖掉。
# 也不用 WA_TranslucentBackground 做真圆角：透明窗口叠硬解视频在 Windows 上
# 会把画面区域变成一块黑，宁可角上直角。
_SHELL_QSS = """
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
    selection-background-color:#3370FF; border:1px solid #3A4150; }
#PlayerShell QSlider::groove:horizontal { height:4px; background:#31363F; border-radius:2px; }
#PlayerShell QSlider::sub-page:horizontal { background:#3370FF; border-radius:2px; }
#PlayerShell QSlider::handle:horizontal { width:12px; margin:-4px 0; border-radius:6px;
    background:#EAF0FA; }
#PlayerShell QSizeGrip { background:transparent; }
"""


class VideoPlayerDialog(QDialog):
    """无边框播放器：播放/暂停、进度、倍速、全屏、横竖屏自适应、审片标记

    无边框就得自己把窗口的事管完：顶部一条可拖动的标题栏（定位/全屏/最小化/关闭），
    右下角 QSizeGrip 拉大，Esc 关、双击画面全屏、单击画面播放/暂停。

    allow_mark：只有【成品视频】才给标记按钮。素材库里预览的是同事传上来的
    原材，误点一下就把素材改名了，所以默认不给。

    marked(new_path, mark)：标记完成（含改名）后发出来，父页面据此刷新列表。
    """

    marked = Signal(str, str)

    MIN_W, MIN_H = 360, 220
    FILL_W, FILL_H = 0.72, 0.72          # 自适应时占屏幕可用区的比例
    BAR_H = 96                          # 标题条 + 控制条大致占高

    def __init__(self, parent, file_path, allow_mark=False):
        super().__init__(parent)
        self._path = str(file_path or "")
        self._allow_mark = bool(allow_mark)
        self._drag_pos = None            # 无边框拖动：按下时的坐标偏移
        self._fitted = None              # 已按哪个视频尺寸自适应过（不跟用户抢尺寸）
        self._swapping = False           # 换文件途中屏蔽报错
        self._fullscreen = False
        self._last_size = None           # 视频真实宽高（退出全屏时重摆一次）

        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
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

        # ---------- 控制条 ----------
        ctrl = QHBoxLayout()
        ctrl.setSpacing(6)
        self.btn_play = QPushButton("▶")
        self.btn_play.setToolTip("播放 / 暂停（空格）")
        self.btn_play.setFixedWidth(40)
        self.btn_play.clicked.connect(self._toggle_play)
        b_back = QPushButton("-5s")
        b_back.setToolTip("后退 5 秒（←）")
        b_back.clicked.connect(lambda: self._seek_rel(-5000))
        b_fwd = QPushButton("+5s")
        b_fwd.setToolTip("前进 5 秒（→）")
        b_fwd.clicked.connect(lambda: self._seek_rel(5000))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.lbl_time = QLabel("00:00 / 00:00")
        self.lbl_time.setToolTip("当前位置 / 总时长")
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
        self.vol.setFixedWidth(72)
        self.vol.setToolTip("音量（↑ / ↓）")
        self.vol.valueChanged.connect(self._set_volume)
        self.btn_loop = QPushButton("🔁 循环")
        self.btn_loop.setCheckable(True)
        self.btn_loop.setToolTip("循环播放：同一条反复看（L）")
        for w in (self.btn_play, b_back, b_fwd, self.slider, self.lbl_time,
                  self.cb_rate, self.btn_mute, self.vol, self.btn_loop):
            ctrl.addWidget(w)
        lay.addLayout(ctrl)

        # ---------- 审片区（只给成品视频） ----------
        mark_bar = QHBoxLayout()
        mark_bar.setSpacing(6)
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
        mark_bar.addStretch(1)
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
        self.player.durationChanged.connect(self.slider.setMaximum)
        self.player.errorOccurred.connect(self._on_err)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.playbackRateChanged.connect(self._on_rate)
        # 真实宽高比元数据靠谱：不依赖容器写没写 Resolution
        self.video.videoSink().videoSizeChanged.connect(self._fit_to_video)

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
        self.player.setPosition(max(0, self.player.position() + ms))

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

    def _on_pos(self, pos):
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        self.lbl_time.setText(f"{_fmt_ms(pos)} / {_fmt_ms(self.player.duration())}")

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

    def _fit_to_video(self, size):
        """按视频真实宽高比重排窗口：竖屏视频不再两侧各一大块黑

        只在本条视频第一次报出尺寸时动手（_fitted 记下那个尺寸），
        否则用户拖大拖小会被后续的尺寸回调顶回去。"""
        w, h = int(size.width()), int(size.height())
        if w <= 0 or h <= 0:
            return
        self._last_size = (w, h)
        if self._fitted == (w, h) or self._fullscreen:
            return
        self._fitted = (w, h)
        avail = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        max_w = max(int(avail.width() * self.FILL_W), self.MIN_W)
        max_h = max(int(avail.height() * self.FILL_H), self.MIN_H)
        scale = min(max_w / w, max_h / h)
        self.resize(max(int(w * scale), self.MIN_W),
                    max(int(h * scale) + self.BAR_H, self.MIN_H))
        self.move(avail.x() + (avail.width() - self.width()) // 2,
                  avail.y() + (avail.height() - self.height()) // 2)

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
            self._seek_rel(-5000)
        elif k == Qt.Key.Key_Right:
            self._seek_rel(5000)
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
    """

    WIDTH = 460
    MAX_H = 340
    HIDE_DELAY = 300        # ms：离开单元格后给移入浮层留的反应时间

    def __init__(self, parent=None):
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

    def set_keep_rect(self, rect):
        self._keep_rect = rect

    @property
    def pinned(self):
        return self._pinned

    def _render(self, text, rich_text):
        self._hide_timer.stop()
        if rich_text:
            # 富文本：中文字高亮，换行用 <br> 还原
            self.label.setTextFormat(Qt.TextFormat.RichText)
            self.label.setText(_highlight_cjk_html(text))
        else:
            self.label.setTextFormat(Qt.TextFormat.PlainText)
            self.label.setText(text.rstrip())
        self.label.adjustSize()
        h = min(self.label.sizeHint().height() + 24, self.MAX_H)
        self.setFixedHeight(h)

    def show_pinned(self, text, pos, rich_text=True):
        """单击/空格唤起：钉住显示，任意键关闭（Popup 模式自带键盘抓取）"""
        if not text or not text.strip():
            self.hide()
            return
        self._pinned = True
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self._render(text, rich_text)
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
        if self._pinned:
            e.accept()
            self.hide()          # 钉住模式下：再按任意键关闭
            return
        super().keyPressEvent(e)

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
