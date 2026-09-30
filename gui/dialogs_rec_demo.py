"""gui/dialogs_rec_demo.py —— 屏幕录制测试 Demo（极简版）

只想验证"能不能把屏幕录下来"：一键录全屏（多屏取并集），可选把系统声音
和画面混进同一个 MP4（音画合一），不做框选 / 指定窗口 / 设备检测 / 分轨那一套。
功能逻辑全在 video_text_tools.screen_recorder（纯逻辑层），本文件只做界面。

与正式「屏幕录制」工具（gui/dialogs_recorder.py）互不影响，各走各的产物。
"""
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QComboBox, QCheckBox, QMessageBox, QFileDialog,
                               QApplication)

from core.config import DOWNLOAD_DIR
from core.logger import log
from gui.header import page_header
from gui.tool_panels import BasePanel, _dep_missing_panel
from utils.desktop_utils import reveal_in_folder
from video_text_tools.ffmpeg_utils import get_ffmpeg_path
from video_text_tools.screen_recorder import (
    IS_WINDOWS, ERR_NOT_WINDOWS, Recorder, build_av_record_command,
    pick_system_audio, virtual_desktop)


def _screens_info():
    """所有屏的逻辑几何 + DPR，元组格式与 screen_recorder 约定一致：
    (x, y, w, h, dpr)"""
    out = []
    for s in QApplication.screens():
        g = s.geometry()
        out.append((g.x(), g.y(), g.width(), g.height(), s.devicePixelRatio()))
    return out


def _fmt_secs(sec):
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


class RecDemoPanel(BasePanel):
    """录屏测试 Demo：保存到目录 + 帧率 + 是否录系统声音 + 开始/停止 + 计时。"""

    def _build(self, outer):
        outer.addWidget(page_header(
            "屏幕录制 · 测试 Demo",
            "一键录全屏（多屏取并集），可选系统声音与画面混进同一个 MP4",
            icon="🎬"))
        if not IS_WINDOWS:
            outer.addWidget(_dep_missing_panel(ERR_NOT_WINDOWS))
            return

        self._rec = None            # Recorder 实例（录制中非 None）
        self._out = ""

        # ---- 保存目录 ----
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("保存到"))
        self.ed_out = QLineEdit(str(Path(DOWNLOAD_DIR) / "录屏"))
        self.b_dir = QPushButton("浏览…")
        self.b_dir.setObjectName("GhostBtn")
        self.b_dir.clicked.connect(self._pick_dir)
        row1.addWidget(self.ed_out, 1)
        row1.addWidget(self.b_dir)
        outer.addLayout(row1)

        # ---- 帧率 + 是否录系统声音 ----
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("帧率"))
        self.cb_fps = QComboBox()
        self.cb_fps.addItems(["15", "24", "30"])
        self.cb_fps.setCurrentText("30")
        self.cb_fps.setFixedWidth(80)
        self.ck_audio = QCheckBox("同时录系统声音（音画合一）")
        self.ck_audio.setChecked(True)
        self.ck_audio.setToolTip(
            "勾上则自动挑一个系统声音源（WASAPI 环回或立体声混音）与画面混进同一个 MP4；\n"
            "挑不到可用源就只录画面。取消勾选＝只录画面。")
        row2.addWidget(self.cb_fps)
        row2.addSpacing(18)
        row2.addWidget(self.ck_audio)
        row2.addStretch(1)
        outer.addLayout(row2)

        # ---- 开始/停止 + 计时 ----
        row3 = QHBoxLayout()
        self.b_run = QPushButton("⏺ 开始录制")
        self.b_run.setMinimumHeight(40)
        self.b_run.clicked.connect(self._toggle)
        self.lbl_time = QLabel("未录制")
        self.lbl_time.setObjectName("PageTip")
        row3.addWidget(self.b_run)
        row3.addSpacing(18)
        row3.addWidget(self.lbl_time)
        row3.addStretch(1)
        outer.addLayout(row3)

        # 进程存活轮询：刷新计时，并兜住 ffmpeg 意外退出
        self._tick = QTimer(self)
        self._tick.setInterval(500)
        self._tick.timeout.connect(self._on_tick)
        outer.addStretch(1)

    # ----------------------------------------------------------------
    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if d:
            self.ed_out.setText(d)

    def _toggle(self):
        if self._rec is None:
            self._start()
        else:
            self._stop()

    def _start(self):
        ffmpeg = get_ffmpeg_path()
        if not ffmpeg or not os.path.isfile(ffmpeg):
            QMessageBox.warning(self, "缺少 ffmpeg",
                                "没找到可用的 ffmpeg，无法录制。")
            return
        outdir = (self.ed_out.text().strip()
                  or str(Path(DOWNLOAD_DIR) / "录屏"))
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "目录不可用", f"创建保存目录失败：{e}")
            return
        name = "录屏Demo_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".mp4"
        self._out = os.path.join(outdir, name)
        audio = pick_system_audio(ffmpeg) if self.ck_audio.isChecked() else None
        physical = virtual_desktop(_screens_info())     # 多屏并集；单屏即主屏
        cmd, out = build_av_record_command(
            ffmpeg, self._out, fps=int(self.cb_fps.currentText()),
            audio=audio, physical=physical)
        self._rec = Recorder([(cmd, out)])
        err = self._rec.start()
        if err:
            self._rec = None
            QMessageBox.warning(self, "启动录制失败", err)
            return
        self.b_run.setText("⏹ 停止录制")
        self.lbl_time.setText("● 录制中 00:00")
        self._tick.start()
        log.info("[录屏Demo] 开始：%s（音频=%s，范围=%s）", out, audio, physical)

    def _stop(self):
        if self._rec is None:
            return
        self._tick.stop()
        rec = self._rec
        self._rec = None
        self.b_run.setEnabled(False)
        self.lbl_time.setText("正在收尾…")
        # 先让文案上屏，下一拍再阻塞式收尾（写 q 等 ffmpeg 补全容器索引）
        QTimer.singleShot(0, lambda: self._do_stop(rec))

    def _do_stop(self, rec):
        err = rec.stop()
        self.b_run.setEnabled(True)
        self.b_run.setText("⏺ 开始录制")
        if err:
            self.lbl_time.setText("录制失败")
            QMessageBox.warning(self, "录制失败", err)
            return
        self.lbl_time.setText("已保存")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("录制完成")
        box.setText(f"已保存到：\n{rec.out_path}")
        box.exec()
        reveal_in_folder(rec.out_path)

    def _on_tick(self):
        if self._rec is None:
            return
        if self._rec.alive():
            self.lbl_time.setText("● 录制中 " + _fmt_secs(self._rec.elapsed()))
            return
        # 进程中途退出（ffmpeg 报错/被别处杀掉）：当作已停止收场
        secs = self._rec.elapsed()
        self._rec = None
        self._tick.stop()
        self.b_run.setText("⏺ 开始录制")
        self.lbl_time.setText(f"录制中断（约 {_fmt_secs(secs)}，进程意外退出）")
