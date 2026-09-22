"""
gui/widgets.py —— 视频/图片预览窗口 + 提示词悬停预览浮层（保留换行/限宽/可滚动）
"""
import html
import re
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QPoint, QTimer, QRect, Signal
from PySide6.QtGui import QGuiApplication, QPixmap, QPainter, QPen, QColor, QCursor
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QScrollArea, QSlider,
                               QPushButton, QLabel, QWidget, QMessageBox, QStyle)

from utils.desktop_utils import open_path


def _fmt_ms(ms):
    s = int(ms / 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


class VideoPlayerDialog(QDialog):
    """双击输出文件单元格弹出：播放该视频（含 播放/暂停/进度条/时间）"""

    def __init__(self, parent, file_path):
        super().__init__(parent)
        self.setWindowTitle(f"预览 - {Path(file_path).name}")
        self.resize(860, 520)

        lay = QVBoxLayout(self)
        self.video = QVideoWidget()
        self.video.setMinimumSize(640, 360)
        lay.addWidget(self.video, 1)

        ctrl = QHBoxLayout()
        self.btn_play = QPushButton()
        self.btn_play.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.btn_play.setFixedWidth(40)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.lbl_time = QLabel("00:00 / 00:00")
        ctrl.addWidget(self.btn_play)
        ctrl.addWidget(self.slider, 1)
        ctrl.addWidget(self.lbl_time)
        lay.addLayout(ctrl)

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)

        self.btn_play.clicked.connect(self.player.play)
        self.player.playbackStateChanged.connect(self._on_state)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(self.slider.setMaximum)
        self.player.errorOccurred.connect(self._on_err)

        if not Path(file_path).exists():
            QMessageBox.warning(self, "文件不存在", f"找不到视频文件：\n{file_path}")
        self.player.setSource(QUrl.fromLocalFile(str(file_path)))
        self.player.play()

    def _on_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        icon = QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay
        self.btn_play.setIcon(self.style().standardIcon(icon))

    def _on_pos(self, pos):
        self.slider.setValue(pos)
        self.lbl_time.setText(f"{_fmt_ms(pos)} / {_fmt_ms(self.player.duration())}")

    def _on_err(self, err, text):
        if text:
            QMessageBox.warning(self, "无法播放", f"{text}\n（文件可能已删除或编码不支持）")

    def closeEvent(self, e):
        self.player.stop()
        super().closeEvent(e)


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
