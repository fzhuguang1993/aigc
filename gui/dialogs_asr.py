"""
gui/dialogs_asr.py —— 工具中心「🎙 语音识别」面板

语音识别是整个链路的主体：选一批视频 → faster-whisper 转口播逐字稿 →（可选）DeepSeek
按语义改同音字 → 保存逐字稿 .txt / 字幕 .srt，或直接把字幕烧录进视频。模型下载就地做
（ModelBar 自带 ⬇ 下载，进度走悬浮球），不逼用户去隐藏的维护页；DeepSeek 纠错只留开关，
配置仍在「接口管理」页，没配就置灰、绝不弹窗教路。

业务全部复用「语音识别主体」链路的现成编排 video_text_tools.subtitle.engine.run_batch
（解析音轨 → 转写 →（纠错）→ SRT/TXT/ASS → 烧录），本层只做界面 + 逐字稿预览。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QCheckBox, QPlainTextEdit, QGroupBox,
                               QFormLayout, QMessageBox, QFileDialog)

from gui.header import page_header
from gui.asr_widgets import ModelBar, FixToggle
from gui.tool_panels import BasePanel, FileListWidget
from utils.desktop_utils import open_path


class AsrPanel(BasePanel):
    """语音识别：选视频 → 转逐字稿 →（可纠错）→ 存 TXT/SRT 或烧录字幕。"""

    def _build(self, outer):
        outer.addWidget(page_header(
            "语音识别",
            "选视频：Whisper 转出口播逐字稿，可保存 TXT/SRT 字幕，也能直接烧录进画面",
            icon="🎙"))

        self.files = FileListWidget("视频文件（可批量）")
        outer.addWidget(self.files)

        # ---- 模型：选择 + 就绪状态 + 就地下载（不收去隐藏页） ----
        # 注意：属性名不能叫 self.bar —— BasePanel.make_run_row 会把它覆盖成进度条。
        self.model_bar = ModelBar(prefer=None)
        outer.addWidget(self.model_bar)

        # ---- 输出勾选 ----
        box = QGroupBox("输出")
        bv = QVBoxLayout(box)
        self.ck_txt = QCheckBox("保存逐字稿 .txt（一行一句，纯文字稿）")
        self.ck_txt.setChecked(True)
        self.ck_srt = QCheckBox("导出字幕 .srt（带时间戳，可导入剪映/播放器）")
        self.ck_srt.setChecked(True)
        self.ck_fix = FixToggle()
        self.ck_burn = QCheckBox("烧录字幕到画面（另存 _字幕.mp4，需 ffmpeg 带 libass）")
        for ck in (self.ck_txt, self.ck_srt, self.ck_fix, self.ck_burn):
            bv.addWidget(ck)
        outer.addWidget(box)

        # ---- 输出目录 ----
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.ed_out = QLineEdit()
        self.ed_out.setPlaceholderText("留空 = 各视频同目录")
        b_browse = QPushButton("浏览…")
        b_browse.setObjectName("GhostBtn")
        b_browse.clicked.connect(self._pick_out)
        r = QHBoxLayout()
        r.addWidget(self.ed_out, 1)
        r.addWidget(b_browse)
        form.addRow("输出目录：", r)
        outer.addLayout(form)

        # ---- 逐字稿预览 ----
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("识别完成后，第一条视频的逐字稿会显示在这里")
        self.preview.setFixedHeight(180)
        outer.addWidget(self.preview)

        self.make_log_box(outer, height=110)
        self.make_run_row(outer, "▶ 开始识别")
        b_open = QPushButton("📂 打开输出目录")
        b_open.setObjectName("GhostBtn")
        b_open.clicked.connect(lambda: open_path(self.ed_out.text().strip()))
        row2 = QHBoxLayout()
        row2.addWidget(b_open)
        row2.addStretch(1)
        outer.addLayout(row2)

    def _pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.ed_out.setText(d)

    def _task(self):
        paths = self.files.paths()
        if not paths:
            QMessageBox.information(self, "提示", "请先添加要识别的视频文件")
            return None
        if not (self.ck_txt.isChecked() or self.ck_srt.isChecked()
                or self.ck_burn.isChecked()):
            QMessageBox.information(self, "提示", "请至少勾选一种输出（逐字稿 / SRT / 烧录）")
            return None
        from video_text_tools.subtitle.models import SubtitleOptions
        opts = SubtitleOptions(
            model_size=self.model_bar.model_size(),
            detect=False, highlight=False,
            gen_srt=self.ck_srt.isChecked(),
            save_txt=self.ck_txt.isChecked(),
            asr_fix=self.ck_fix.isChecked(),
            burn=self.ck_burn.isChecked(),
            out_dir=self.ed_out.text().strip(),
            mirror=self.model_bar.mirror())

        def fn(log, progress, should_stop):
            from video_text_tools.subtitle import run_batch
            return run_batch(paths, opts, log=log, progress=progress,
                             should_stop=should_stop)
        return fn

    def on_result(self, res):
        results = res or []
        ok = [r for r in results if getattr(r, "ok", False)]
        miss = next((r for r in results
                     if getattr(r, "error", "") in ("DepMissing", "ModelNotReady")), None)
        if miss is not None:
            try:
                from gui.dialogs_subtitle import whisper_missing_guidance
                whisper_missing_guidance(self, miss.error, on_log=self._append_log)
            except Exception:
                self._append_log(f"⚠ 缺少依赖或模型：{miss.message}")
            return
        if ok:
            text = ok[0].transcript_text
            self.preview.setPlainText(text or "（未识别到语音内容）")
        done = sum(1 for r in ok if r.txt_path or r.srt_path or r.burned_path)
        self._append_log(f"🎉 识别完成：成功 {len(ok)} / 共 {len(results)} 条，"
                         f"已产出文件 {done} 条（逐字稿/SRT/烧录见输出目录）")
