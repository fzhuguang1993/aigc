"""
gui/asr_widgets.py —— 语音识别相关、被多个界面复用的小控件

把「选 Whisper 模型 + 看就绪状态 + 就地下载」这一整套从各面板里抽出来，凡是
要用语音识别的地方（语音识别工具、字幕选项框……）都共用同一个 ModelBar，不必
每处各写一遍下载/镜像/状态刷新，也就不用再逼用户去隐藏的维护页才能下模型。

重依赖（transcribe / ToolWorker / speed_ball）一律延迟导入；任一失败不静默：
下载失败会原样抛给 whisper_missing_guidance 分类给动作。
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                               QPushButton, QComboBox, QCheckBox)

# 展示名 ↔ 模型档；顺序即下拉顺序（默认最优档在前）
SIZE_LABELS = [("medium（默认，最准）", "medium"), ("small", "small"),
               ("base", "base"), ("tiny（最快，较糙）", "tiny")]
_CB_INDEX = {"medium": 0, "small": 1, "base": 2, "tiny": 3}


def best_default_size():
    """当前本机可用作默认档：优先 medium，否则取已就绪里质量最高的；都没下→medium。"""
    try:
        from video_text_tools.asr import transcribe as tr
        return tr.best_ready_size() or "medium"
    except Exception:
        return "medium"


class ModelBar(QWidget):
    """一行式 Whisper 模型选择器：下拉 + 就绪状态 + ⬇下载 + 国内镜像。
    
      由调用方在真正识别前用 best_ready_size 兑底）。刻意不叫 size()——那是 QWidget 自带的几何方法。
    - mirror()：是否走 hf-mirror。
    - download_requested：点「⬇ 下载模型」时，本控件自己起后台下（进度走悬浮球），
      下完刷新状态并发出该信号（携带 size）。
    """
    download_requested = Signal(str)

    def __init__(self, parent=None, prefer=None):
        super().__init__(parent)
        self._worker = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Whisper 模型："))
        self.cb_size = QComboBox()
        for label, val in SIZE_LABELS:
            self.cb_size.addItem(label, val)
        self.cb_size.setCurrentIndex(_CB_INDEX.get(prefer or best_default_size(), 0))
        self.cb_size.currentIndexChanged.connect(self._refresh_status)
        row.addWidget(self.cb_size)
        row.addStretch(1)
        self.ck_mirror = QCheckBox("用国内镜像")
        row.addWidget(self.ck_mirror)
        self.b_dl = QPushButton("⬇ 下载模型")
        self.b_dl.setObjectName("GhostBtn")
        self.b_dl.clicked.connect(self._download)
        row.addWidget(self.b_dl)
        lay.addLayout(row)

        self.lbl_status = QLabel("…")
        self.lbl_status.setObjectName("PageTip")
        self.lbl_status.setWordWrap(True)
        lay.addWidget(self.lbl_status)
        self._refresh_status()

    # ---- 取值 ----
    def model_size(self):
        return self.cb_size.currentData() or "medium"

    def mirror(self):
        return self.ck_mirror.isChecked()

    # ---- 就绪状态 ----
    def _refresh_status(self, *_):
        size = self.model_size()
        try:
            from video_text_tools.asr import transcribe as tr
            ready = tr.model_ready(size)
            best = tr.best_ready_size()
        except Exception:
            ready, best = False, None
        if ready:
            self.lbl_status.setText(f"✅ 「{size}」模型已就绪")
            self.lbl_status.setStyleSheet(
                "font-size:12px; color:#00A870; background:transparent;")
            self.b_dl.setEnabled(False)
        else:
            hint = f"（已就绪的最佳档：{best}，可直接改选）" if best else "（尚无就绪模型）"
            self.lbl_status.setText(
                f"⚠ 「{size}」未下载{hint}；点右侧「⬇ 下载模型」就地下载，无需去别处配置")
            self.lbl_status.setStyleSheet(
                "font-size:12px; color:#D83931; background:transparent;")
            self.b_dl.setEnabled(True)

    # ---- 就地下载（后台 ToolWorker，进度走悬浮球）----
    def _download(self, *_):
        from core.config import MODELS_DIR
        from gui.speed_ball import show_speed_ball
        from gui.tool_panels import ToolWorker
        if self._worker is not None and self._worker.isRunning():
            return
        size, mirror = self.model_size(), self.mirror()
        self.lbl_status.setText("⏳ 正在下载模型…（首次较慢，请稍候）")
        self.b_dl.setEnabled(False)
        show_speed_ball(self, MODELS_DIR, f"下载「{size}」模型")

        def fn(log, progress, should_stop):
            from video_text_tools.asr import transcribe as tr
            tr.download_model_auto(size, mirror=mirror, log=log,
                                   on_progress=lambda p: progress(max(p, 0), 100, size))
            return size

        self._worker = ToolWorker(fn, self)
        self._worker.log.connect(
            lambda m: self.lbl_status.setText("⏳ " + (m or "")[:48]))
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, res):
        from gui.speed_ball import hide_speed_ball
        hide_speed_ball()
        self._worker = None
        self.b_dl.setEnabled(True)
        if isinstance(res, Exception):
            # 缺库/缺模型走统一引导（打包态会说“请用创作版”，不甩 pip）
            try:
                from gui.dialogs_subtitle import whisper_missing_guidance
                whisper_missing_guidance(self.window(), res)
            except Exception:
                self.lbl_status.setText(f"✗ 下载失败：{res}")
            self._refresh_status()
            return
        self._refresh_status()
        self.download_requested.emit(self.model_size())


class FixToggle(QCheckBox):
    """「DeepSeek 语义纠错」勾选：只留开关本身，配置仍收在接口管理页。

    未配置（asr_fix_ready() 为假）时不弹窗、不教路——只是把选项置灰并给一句
    tooltip 说明去哪儿配置；配置好回来就自动可勾。默认勾选取决于是否已配置。
    """

    def __init__(self, text="✨ DeepSeek 语义纠错（改同音字，需已配置）", parent=None):
        super().__init__(text, parent)
        self.refresh()

    def refresh(self):
        try:
            from core.config import asr_fix_ready
            ok = asr_fix_ready()
        except Exception:
            ok = False
        self.setEnabled(ok)
        if ok:
            self.setToolTip("识别完成后把逐字稿送 DeepSeek 按语义纠正同音字/错别字，只改字不动时间戳。")
            self.setChecked(self.isChecked() or True)
        else:
            self.setChecked(False)
            self.setToolTip("尚未配置 DeepSeek 纠错端点，故此选项暂不可用；"
                            "在「🔌 接口管理」页填好 key/model 后回来即可勾选。")
        return ok
