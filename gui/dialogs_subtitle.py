"""
gui/dialogs_subtitle.py —— 录屏字幕：选项对话框 + Whisper 缺失引导 + 后台执行入口

被两处 UI 复用（屏幕录制面板、任务中心批量），本身不是独立页面：
  SubtitleOptionsDialog   收一次字幕处理的参数（模型/勾选/样式/输出目录）
  whisper_missing_guidance 按「没装 faster-whisper」vs「没下模型」分情形给动作
  run_subtitle            用 ToolWorker 后台跑 subtitle.run_batch，回来汇总 + 引导

纯 UI：一律延迟导入业务逻辑与 Qt 之外的重依赖，缺库也不崩。样式默认取
core.config.subtitle_config()，与逻辑层口径一致。
"""
from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                               QLabel, QPushButton, QLineEdit, QComboBox,
                               QCheckBox, QSpinBox, QColorDialog, QFileDialog,
                               QGroupBox, QMessageBox, QApplication)

from gui.tool_panels import ToolWorker

# faster-whisper 可选大件的安装命令（开发版直接可用；打包版随包内置或手动放置）
_INSTALL_CMD = "pip install -r requirements-breakdown.txt"
# pip 国内镜像源（官方 PyPI 慢/不通时一键安装用）
_PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
_SIZE_LABELS = [("medium（默认，最准）", "medium"), ("small", "small"),
                ("base", "base"), ("tiny（最快，较糙）", "tiny")]
_ALIGN_LABELS = [("底部", "bottom"), ("顶部", "top")]


def _swatch(btn, hex_color):
    """把颜色按钮涂成对应色块（前景色浅则配深边，保证白/黄也看得见）。"""
    c = QColor(hex_color)
    border = "#888888" if c.lightness() > 160 else c.darker(160).name()
    btn.setStyleSheet(
        f"background:{hex_color}; border:1px solid {border}; border-radius:4px;")


def _pick_color(parent, current):
    init = QColor(current if len(current) >= 7 else "#FFFFFF")
    got = QColorDialog.getColor(init, parent, "选择颜色")
    if not got.isValid():
        return current
    return got.name().upper()


# ====================================================================
# 选项对话框
# ====================================================================
class SubtitleOptionsDialog(QDialog):
    """收一次字幕处理的参数。构造时可带初始 SubtitleOptions 覆盖默认（如任务页预选）。"""

    def __init__(self, parent=None, options=None):
        super().__init__(parent)
        self._load_defaults()
        self.setWindowTitle("字幕选项")
        self.resize(460, 460)
        v = QVBoxLayout(self)

        tip = QLabel("无音轨的录屏会自动取旁录音（内部声/麦克风）；烧录会另存新文件，"
                     "不覆盖原片。卖点高亮与「检测已有字幕」都依赖豆包，未配置会自动跳过。")
        tip.setWordWrap(True)
        tip.setObjectName("PageTip")
        v.addWidget(tip)

        # ---- 模型 + 状态 + 下载 ----
        form = QFormLayout()
        self.cb_size = QComboBox()
        for label, val in _SIZE_LABELS:
            self.cb_size.addItem(label, val)
        self.cb_size.currentIndexChanged.connect(self._refresh_model_status)
        form.addRow("转写模型：", self.cb_size)
        v.addLayout(form)

        mrow = QHBoxLayout()
        self.lbl_model = QLabel("…")
        self.lbl_model.setObjectName("PageTip")
        b_dl = QPushButton("⬇ 下载模型")
        b_dl.setObjectName("GhostBtn")
        b_dl.clicked.connect(self._download_model)
        self.ck_mirror = QCheckBox("用国内镜像")
        mrow.addWidget(self.lbl_model, 1)
        mrow.addWidget(self.ck_mirror)
        mrow.addWidget(b_dl)
        v.addLayout(mrow)

        # ---- 勾选 ----
        box = QGroupBox("处理")
        bv = QVBoxLayout(box)
        self.ck_detect = QCheckBox("先抽帧检测是否已有字幕（已带则跳过）")
        self.ck_srt = QCheckBox("导出纯文本 .srt（兜底 / 供剪映二次精修）")
        self.ck_srt.setChecked(True)
        self.ck_highlight = QCheckBox("卖点语义高亮（需豆包，命中词用强调色）")
        self.ck_burn = QCheckBox("烧录字幕到画面（需 ffmpeg 带 libass）")
        for ck in (self.ck_detect, self.ck_srt, self.ck_highlight, self.ck_burn):
            bv.addWidget(ck)
        v.addWidget(box)

        # ---- 样式 ----
        sbox = QGroupBox("字幕样式（写进 .ass）")
        sv = QVBoxLayout(sbox)
        r1 = QHBoxLayout()
        self.sp_size = QSpinBox()
        self.sp_size.setRange(8, 72)
        self.sp_size.setValue(self._style["font_size"])
        self.cb_align = QComboBox()
        for label, val in _ALIGN_LABELS:
            self.cb_align.addItem(label, val)
        self.cb_align.setCurrentIndex(
            0 if self._style["align"] == "bottom" else 1)
        r1.addWidget(QLabel("字号"))
        r1.addWidget(self.sp_size)
        r1.addSpacing(16)
        r1.addWidget(QLabel("位置"))
        r1.addWidget(self.cb_align)
        r1.addStretch(1)
        sv.addLayout(r1)

        r2 = QHBoxLayout()
        self.btn_primary = QPushButton()
        self.btn_outline = QPushButton()
        self.btn_highlight = QPushButton()
        for b in (self.btn_primary, self.btn_outline, self.btn_highlight):
            b.setFixedSize(34, 22)
        self._c_primary = self._style["primary"]
        self._c_outline = self._style["outline"]
        self._c_highlight = self._style["highlight"]
        self.btn_primary.clicked.connect(
            lambda: self._set_color("primary"))
        self.btn_outline.clicked.connect(
            lambda: self._set_color("outline"))
        self.btn_highlight.clicked.connect(
            lambda: self._set_color("highlight"))
        for lab, b in (("前景", self.btn_primary), ("描边", self.btn_outline),
                       ("高亮", self.btn_highlight)):
            r2.addWidget(QLabel(lab))
            r2.addWidget(b)
        r2.addStretch(1)
        sv.addLayout(r2)
        self._repaint_swatches()
        v.addWidget(sbox)

        # ---- 输出目录 ----
        orow = QHBoxLayout()
        self.ed_out = QLineEdit()
        self.ed_out.setPlaceholderText("留空 = 各视频同目录")
        b_browse = QPushButton("浏览…")
        b_browse.setObjectName("GhostBtn")
        b_browse.clicked.connect(self._browse_out)
        orow.addWidget(QLabel("输出目录"))
        orow.addWidget(self.ed_out, 1)
        orow.addWidget(b_browse)
        v.addLayout(orow)

        v.addStretch(1)
        bb = QHBoxLayout()
        bb.addStretch(1)
        b_ok = QPushButton("确定")
        b_ok.clicked.connect(self.accept)
        b_no = QPushButton("取消")
        b_no.setObjectName("GhostBtn")
        b_no.clicked.connect(self.reject)
        bb.addWidget(b_ok)
        bb.addWidget(b_no)
        v.addLayout(bb)

        # 默认/初始选项都等控件建好后再套（_apply_options 依赖这些 widget）
        _dsize = self._default_size
        try:
            from video_text_tools.asr import transcribe as tr
            if not tr.model_ready(_dsize):        # 默认档（常为 medium）没下 → 自动挑已就绪里最好的
                _dsize = tr.best_ready_size() or _dsize
        except Exception:
            pass
        self.cb_size.setCurrentIndex(self.cb_index(_dsize))
        if options is not None:
            self._apply_options(options)
        self._refresh_model_status()

    # ---- 默认值（config 段，不碰控件） ----
    def _load_defaults(self):
        from core.config import subtitle_config
        c = subtitle_config()
        self._default_size = c.get("model_size", "medium")
        self._style = dict(c.get("default_style") or {})
        self._style.setdefault("font_size", 16)
        self._style.setdefault("align", "bottom")
        self._style.setdefault("primary", "#FFFFFF")
        self._style.setdefault("outline", "#000000")
        self._style.setdefault("highlight", "#FFD400")

    @staticmethod
    def cb_index(size):
        return {"medium": 0, "small": 1, "base": 2, "tiny": 3}.get(size, 0)

    def _apply_options(self, o):
        self.cb_size.setCurrentIndex(self.cb_index(o.model_size))
        self.ck_detect.setChecked(bool(o.detect))
        self.ck_srt.setChecked(bool(o.gen_srt))
        self.ck_highlight.setChecked(bool(o.highlight))
        self.ck_burn.setChecked(bool(o.burn))
        self.ck_mirror.setChecked(bool(o.mirror))
        st = o.style
        self.sp_size.setValue(st.font_size)
        self.cb_align.setCurrentIndex(0 if st.align == "bottom" else 1)
        self._c_primary, self._c_outline, self._c_highlight = \
            st.primary, st.outline, st.highlight
        self._repaint_swatches()
        self.ed_out.setText(o.out_dir or "")

    def _set_color(self, which):
        cur = getattr(self, f"_c_{which}")
        setattr(self, f"_c_{which}", _pick_color(self, cur))
        self._repaint_swatches()

    def _repaint_swatches(self):
        _swatch(self.btn_primary, self._c_primary)
        _swatch(self.btn_outline, self._c_outline)
        _swatch(self.btn_highlight, self._c_highlight)

    def _browse_out(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.ed_out.setText(d)

    def _size(self):
        return self.cb_size.currentData() or "medium"

    def _refresh_model_status(self, *_):
        try:
            from video_text_tools.asr import transcribe as tr
            ready = tr.model_ready(self._size())
        except Exception:
            ready = False
        self.lbl_model.setText(
            f"✓ 「{self._size()}」模型已就绪" if ready
            else f"⚠ 「{self._size()}」模型未下载，点右侧下载")

    def _download_model(self, *_):
        from core.config import MODELS_DIR
        from gui.speed_ball import show_speed_ball
        size, mirror = self._size(), self.ck_mirror.isChecked()
        self.lbl_model.setText("⏳ 正在下载模型…（首次较慢，请稍候）")
        ck = self.cb_index(size)
        show_speed_ball(self, MODELS_DIR, f"下载「{size}」模型")

        def fn(log, progress, should_stop):
            from video_text_tools.asr import transcribe as tr
            tr.download_model_auto(size, mirror=mirror, log=log,
                                   on_progress=lambda p: progress(max(p, 0), 100, size))
            return True

        self._dl = ToolWorker(fn, self)
        # 逐端点状态映到标签（本框无日志区，只显示当前正在从哪个端点下）
        self._dl.log.connect(lambda m: self.lbl_model.setText("⏳ " + (m or "")[:48]))
        self._dl.done.connect(lambda res, _i=ck: self._on_dl_done(res))
        self._dl.start()

    def _on_dl_done(self, res):
        from gui.speed_ball import hide_speed_ball
        hide_speed_ball()
        self._dl = None
        if isinstance(res, Exception):
            # 缺库/缺模型走统一引导（打包态会说“请用创作版”，不展 pip）
            if type(res).__name__ in ("DepMissing", "ModelNotReady"):
                whisper_missing_guidance(self, res)
            self.lbl_model.setText(f"✗ 下载失败：{res}")
        else:
            self.lbl_model.setText(f"✓ 「{self._size()}」模型已就绪")

    def result_options(self):
        from video_text_tools.subtitle.models import (SubtitleOptions,
                                                      SubtitleStyle)
        style = SubtitleStyle(
            font_size=self.sp_size.value(), primary=self._c_primary,
            outline=self._c_outline, back=self._style.get("back", "#00000000"),
            align=self.cb_align.currentData() or "bottom",
            margin_v=self._style.get("margin_v", 40),
            highlight=self._c_highlight,
            max_chars=self._style.get("max_chars", 18))
        return SubtitleOptions(
            model_size=self._size(), detect=self.ck_detect.isChecked(),
            gen_srt=self.ck_srt.isChecked(), highlight=self.ck_highlight.isChecked(),
            burn=self.ck_burn.isChecked(), style=style,
            out_dir=self.ed_out.text().strip(), mirror=self.ck_mirror.isChecked())


def ask_subtitle_options(parent, options=None):
    """弹一次选项框，确定返回 SubtitleOptions，取消返回 None。"""
    dlg = SubtitleOptionsDialog(parent, options=options)
    return dlg.result_options() if dlg.exec() else None


# ====================================================================
# Whisper 缺失引导：分「没装库」/「没下模型」两类给动作
# ====================================================================
def _packaged():
    """是否跑在 PyInstaller 打包后的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def _requirements_path():
    """项目根下的 requirements-breakdown.txt（gui/ 的上一级）；不存在返回 None。"""
    p = Path(__file__).resolve().parents[1] / "requirements-breakdown.txt"
    return p if p.is_file() else None


def install_breakdown_deps(parent, on_log=None, on_done=None, mirror=False):
    """开发版一键装 faster-whisper：用当前解释器的 pip（sys.executable），
    自动锁定程序实际跑的那个 .venv，用户无需开 cmd / 无需懂路径。

    pip 输出逐行经 on_log 实时回显（让你看得见“真在装”）；完成后回调 on_done。
    仅适用于开发版；打包 exe 里没 pip，不走这条路。worker 挂在 parent 防 GC。
    """
    req = _requirements_path()
    cmd = [sys.executable, "-m", "pip", "install"]
    if mirror:
        cmd += ["-i", _PIP_MIRROR]
    cmd += (["-r", str(req)] if req else ["faster-whisper>=1.0.0"])

    def fn(log, progress, should_stop):
        log("▶ 正在自动安装 faster-whisper 依赖（首次较慢，可能几分钟，请稍候）…")
        log("  命令：" + " ".join(cmd))
        progress(0, 100, "pip install…")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", bufsize=1)
        try:
            for line in iter(p.stdout.readline, ""):
                if should_stop and should_stop():
                    p.terminate()
                    log("⏹ 已取消安装")
                    return False
                s = (line or "").rstrip()
                if s:
                    log("   " + s)
        finally:
            rc = p.wait()
        if rc != 0:
            raise RuntimeError(
                f"pip 安装失败（返回码 {rc}）。可手动执行上面那条命令；"
                "网络不通就改用国内镜像：\n  " + " ".join(
                    [sys.executable, "-m", "pip", "install", "-i", _PIP_MIRROR,
                     "-r", str(req) if req else "faster-whisper"]))
        log("✅ 依赖安装完成。重新点「⬇ 下载模型」即可开始下载模型。")
        progress(100, 100, "完成")
        return True

    worker = ToolWorker(fn, parent)
    if on_log:
        worker.log.connect(on_log)

    def _d(res):
        parent._pip_worker = None
        if isinstance(res, Exception):
            QMessageBox.warning(parent, "安装失败", str(res))
        elif on_done:
            on_done()

    worker.done.connect(_d)
    parent._pip_worker = worker
    worker.start()
    return worker


def whisper_missing_guidance(parent, err, on_log=None):
    """err：DepMissing / ModelNotReady 实例，或其 error 字符串。
    on_log：可选日志回调（有则开发版“一键安装”可实时回显 pip 输出）。"""
    kind = err if isinstance(err, str) else type(err).__name__
    msg = "" if isinstance(err, str) else str(err)

    if kind == "DepMissing":
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("缺少 faster-whisper")
        if _packaged():
            # 打包精简版：whisper 被有意剪掉（守体积），用户没有 Python，别甩 pip。
            box.setText(
                "生成字幕需要 faster-whisper（语音转文字），"
                "但你当前用的是【精简版】，它不含这个组件。\n\n"
                "请改用【AIGC视频助手-创作版.exe】——它已内置 faster-whisper，"
                "打开后点「⬇ 下载模型」即可自动配好，不需要装 Python。")
            box.setInformativeText(
                "找不到创作版？说明维护人打包时本机未装 faster-whisper，"
                "请向维护人索取创作版安装包（详见 docs/录屏字幕.md）。")
            box.addButton(QMessageBox.StandardButton.Ok)
            box.exec()
            return
        # 开发版（源码运行，本就有 Python）：给安装命令 + 一键自动装
        box.setText(
            "生成字幕需要 faster-whisper（语音转文字），当前解释器尚未安装。\n\n"
            "可点「一键安装依赖」让程序自动装进当前 Python（无需手动开 cmd），\n"
            "或点「复制安装命令」自己执行：\n    " + _INSTALL_CMD)
        box.setInformativeText("安装较慢且会拉入 ctranslate2/av 等大件；官方源不通可选国内镜像。")
        b_copy = box.addButton("复制安装命令", QMessageBox.ButtonRole.ActionRole)
        b_inst = box.addButton("🔧 一键安装依赖", QMessageBox.ButtonRole.ActionRole)
        b_mirr = box.addButton("🔧 用国内镜像安装", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_copy:
            QApplication.clipboard().setText(_INSTALL_CMD)
        elif clicked is b_inst:
            install_breakdown_deps(parent, on_log=on_log, mirror=False)
        elif clicked is b_mirr:
            install_breakdown_deps(parent, on_log=on_log, mirror=True)
        return

    # ModelNotReady（及其兜底）：指向下载入口
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("Whisper 模型未下载")
    box.setText(
        "faster-whisper 已就绪，但对应模型还没下载到本机。\n\n"
        "打开字幕选项，点「⬇ 下载模型」（首次较慢，连不上官方源就勾「用国内镜像」）。")
    if msg:
        box.setInformativeText(msg)
    box.exec()


# ====================================================================
# 后台执行：跑一批字幕，回来汇总 + 缺失引导
# ====================================================================
def run_subtitle(parent, videos, options, on_done=None, on_log=None):
    """用 ToolWorker 后台跑 subtitle.run_batch。

    结束回调 on_done(list[SubtitleResult])；若因「没装库 / 没下模型」整体失败，
    先弹 whisper_missing_guidance 再回调。worker 引用挂在 parent 上防被 GC。
    videos 为空直接返回。
    """
    videos = [v for v in (videos or []) if str(v).strip()]
    if not videos:
        return

    def fn(log, progress, should_stop):
        from video_text_tools.subtitle import run_batch
        return run_batch(videos, options, log=log, progress=progress,
                         should_stop=should_stop)

    worker = ToolWorker(fn, parent)
    if on_log:
        worker.log.connect(on_log)

    def _done(res):
        parent._subtitle_worker = None
        if isinstance(res, Exception):
            results = []
            QMessageBox.warning(parent, "字幕处理失败", str(res))
        else:
            results = res or []
            miss = next((r for r in results
                         if getattr(r, "error", "") in ("DepMissing", "ModelNotReady")),
                        None)
            if miss is not None:
                whisper_missing_guidance(parent, miss.error, on_log=on_log)
        if on_done:
            on_done(results)

    worker.done.connect(_done)
    parent._subtitle_worker = worker
    worker.start()
    return worker
