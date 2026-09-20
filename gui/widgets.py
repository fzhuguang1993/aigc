"""
gui/widgets.py —— 视频/图片预览窗口 + 提示词悬停预览浮层（保留换行/限宽/可滚动）
"""
import os
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QPoint
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QScrollArea, QSlider,
                               QPushButton, QLabel, QWidget, QMessageBox, QStyle)


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
        b_open.clicked.connect(lambda: os.startfile(str(file_path)))
        bar.addWidget(b_open)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(self.accept)
        bar.addWidget(b_close)
        lay.addLayout(bar)


class HoverPreview(QScrollArea):
    """悬停预览浮层：限宽自动换行、超长可滚动、保留原文换行排版"""

    WIDTH = 460
    MAX_H = 340

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

    def show_at(self, text, pos):
        if not text or not text.strip():
            self.hide()
            return
        # PlainText：原文换行 \n 原样保留
        self.label.setText(text.rstrip())
        self.label.adjustSize()
        h = min(self.label.sizeHint().height() + 24, self.MAX_H)
        self.setFixedHeight(h)
        self.move(self.clamp_to_screen(pos))
        self.show()

    def clamp_to_screen(self, pos):
        """防止浮层超出屏幕右侧/底部"""
        screen = QGuiApplication.primaryScreen().availableGeometry()
        x = min(pos.x(), screen.right() - self.width() - 8)
        y = min(pos.y(), screen.bottom() - self.height() - 8)
        return QPoint(max(x, screen.left()), max(y, screen.top()))
