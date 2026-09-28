"""
gui/asr_live.py —— 播放器「🎤 识别字幕」的实时字幕浮窗

点播放器顶栏的识别按钮，弹一个无边框、总在最前的小浮窗：后台线程跑
faster-whisper 流式转写，**识别一句就往文本框尾部追加一句**（像 Word 往下打字），
自动滚到最新一行，让你当场看识别效果。

识别完成后标题栏的「✨ 纠错」按钮可把整篇逐字稿发给 DeepSeek（OpenAI 兼容）按
语义改同音字/错别字——只换字、保持每句与时间戳不变（重写后仍是 [mm:ss] 前缀）。

设计要点：
- 识别是重 CPU 活，放 QThread，绝不卡播放器界面；每句经 Signal 排队回主线程上屏；
- 模型自动挑「已就绪」档（best_ready_size），一个都没下就先给提示，不静默失败；
- 起识别时把领域词库喂 whisper initial_prompt，让实时这遍就少错同音字；
- 纠错另起后台线程，未配置 DeepSeek 只红字引导去「接口管理」页、绝不改坏原文；
- 关窗即请求 should_stop，让后台尽快收工，不留僵尸线程。
"""
import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QWidget, QLabel, QPushButton, QTextEdit, QVBoxLayout,
                               QHBoxLayout, QApplication, QSizePolicy)

from core.config import asr_glossary_load, asr_fix_config, asr_fix_ready
from video_text_tools.asr import transcribe as tr
from video_text_tools.asr.fixer import AsrFixer
from video_text_tools.asr.types import FixNotConfigured

# 深色外壳，跟播放器一个色系（#PlayerShell 的 #15171C/#252A33 家族）
_QSS = """
#AsrRoot { background:#15171C; border:1px solid #2A2E37; }
#AsrRoot QLabel { color:#C9CFDA; background:transparent; font-size:12px; }
#AsrRoot QLabel#AsrTitle { color:#F2F5FA; font-size:12px; font-weight:600; }
#AsrRoot QLabel#AsrStatus { color:#8F959E; font-size:11px; }
#AsrRoot QLabel#AsrStatus[err="1"] { color:#F54A45; }
#AsrRoot QPushButton { background:#252A33; color:#DCE1EA; border:0;
    border-radius:6px; padding:3px 8px; }
#AsrRoot QPushButton:hover { background:#323A47; }
#AsrRoot QPushButton:disabled { color:#5A6070; background:#1E222A; }
#AsrRoot QPushButton#AsrClose:hover { background:#D94A43; color:#FFFFFF; }
#AsrRoot QTextEdit { background:#1B1E25; color:#EAF0FA; border:1px solid #2A2E37;
    border-radius:8px; font-size:14px; }
"""

W, H = 440, 320

# 未配置 DeepSeek 时的统一提示（纠错按钮据此红字引导，不静默、不改坏原文）
_FIX_UNCONFIGURED = ("未配置语音纠错：请到「🔌 接口管理」页的「✨ 语音纠错」填好 "
                     "DeepSeek 的 api_key 与模型名后再点。")


def _fmt_ts(sec):
    sec = int(max(sec or 0, 0))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _glossary_prompt(gloss):
    """词库非空时拼成 whisper initial_prompt（顿号分隔），空则 None（不传）。"""
    return "、".join(gloss) if gloss else None


class _AsrWorker(QThread):
    """后台线程：流式转写，逐句 emit 回主线程。run 里只碰信号，不直接改控件。"""

    got = Signal(str, float, float)     # text, start, end
    done = Signal(int)                   # 段数
    failed = Signal(str)

    def __init__(self, path, size, parent=None, initial_prompt=None):
        super().__init__(parent)
        self._path = path
        self._size = size
        self._initial_prompt = initial_prompt
        self._stop = threading.Event()

    def request_stop(self):
        self._stop.set()

    def run(self):
        try:
            def on_seg(s):
                self.got.emit(s.text, s.start, s.end)
            segs, _ = tr.transcribe_stream(
                self._path, size=self._size, on_segment=on_seg,
                should_stop=self._stop.is_set, initial_prompt=self._initial_prompt)
            self.done.emit(len(segs))
        except tr.ModelNotReady as e:
            self.failed.emit(f"模型未就绪：{e}")
        except tr.DepMissing as e:
            self.failed.emit(f"缺少依赖：{e}")
        except tr.TranscribeError as e:
            self.failed.emit(f"识别失败：{e}")
        except Exception as e:                       # 兜底：任何异常都要显形，不静默
            self.failed.emit(f"识别异常：{type(e).__name__}: {e}")


class _FixWorker(QThread):
    """后台线程：整篇逐字稿送 DeepSeek 语义纠错，按编号回填后 emit 等长文本列表。

    只吐文本，时间戳由主线程用 self._lines 原样配对——纠错绝不改变句数/顺序。
    未配置（ConfigError）或任何失败都走 failed 信号，让界面红字显形，不静默。
    """

    done = Signal(list)                  # 与入参等长的纠错文本列表
    failed = Signal(str)

    def __init__(self, texts, glossary, cfg, parent=None):
        super().__init__(parent)
        self._texts = list(texts)
        self._glossary = list(glossary or [])
        self._cfg = cfg

    def run(self):
        try:
            fixed = AsrFixer(self._cfg).correct_segments(
                self._texts, glossary=self._glossary)
            self.done.emit(list(fixed))
        except FixNotConfigured as e:
            self.failed.emit(str(e))
        except Exception as e:                       # 网络/解析异常：显形，不假装成功
            self.failed.emit(f"纠错失败：{type(e).__name__}: {e}")


class AsrLiveWindow(QWidget):
    """实时字幕浮窗本体。start() 起后台识别；识别完成后可 ✨ 纠错；关窗收线程。"""

    def __init__(self, parent, path, size):
        super().__init__(parent)
        self._path = str(path)
        self._size = size
        self._drag = None
        self._worker = None
        self._fix_worker = None
        self._lines = []                                   # [(start, text)]，纠错后按序回填
        self._glossary = asr_glossary_load()               # 词库：喂 initial_prompt + 纠错

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setObjectName("AsrRoot")
        self.setStyleSheet(_QSS)
        self.resize(W, H)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(6)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        from pathlib import Path as _P
        self.lbl_title = QLabel(f"🎤 实时字幕 · {_P(self._path).name}", objectName="AsrTitle")
        self.lbl_title.setSizePolicy(QSizePolicy.Policy.Ignored,
                                     QSizePolicy.Policy.Preferred)
        self.lbl_status = QLabel("", objectName="AsrStatus")
        self.btn_fix = QPushButton("✨ 纠错", objectName="AsrFix")
        self.btn_fix.setToolTip("识别完成后用 DeepSeek 按语义改同音字/错别字"
                                "（需在「🔌 接口管理」页配置；未配置只提示不改坏原文）")
        self.btn_fix.setEnabled(False)
        self.btn_fix.clicked.connect(self._do_fix)
        b_close = QPushButton("✕", objectName="AsrClose")
        b_close.setToolTip("关闭（并停止识别）")
        b_close.clicked.connect(self.close_win)
        bar.addWidget(self.lbl_title, 1)
        bar.addWidget(self.lbl_status)
        bar.addWidget(self.btn_fix)
        bar.addWidget(b_close)
        root.addLayout(bar)

        self.edit = QTextEdit()
        self.edit.setReadOnly(True)
        self.edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        root.addWidget(self.edit, 1)

        self._append_meta()

    # ---- 对外 ----
    def start(self):
        if self._size is None:
            self._set_status("未检测到已就绪的模型：请先在「🔌 接口管理」页的「🎙 语音模型」"
                             "下载（tiny 最小、最容易下成功），再来点识别。", err=True)
            return
        prompt = _glossary_prompt(self._glossary)
        tip = f"（已带 {len(self._glossary)} 个词库词）" if self._glossary else ""
        self._set_status(f"识别中（模型 {self._size}）…{tip}")
        self._worker = _AsrWorker(self._path, self._size, self, initial_prompt=prompt)
        self._worker.got.connect(self._on_segment)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def close_win(self):
        if self._worker is not None:
            self._worker.request_stop()
            self._worker.quit()
            self._worker.wait(3000)
        if self._fix_worker is not None:
            self._fix_worker.quit()
            self._fix_worker.wait(3000)
        hide_asr_live()
        self.close()
        self.deleteLater()

    # ---- 槽（主线程） ----
    def _on_segment(self, text, start, end):
        # Word 式尾部追加：定稿一行就落一行，随后滚到底跟随最新
        self._lines.append((start, text))
        self.edit.append(f"[{_fmt_ts(start)}] {text}")
        sb = self.edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_done(self, n):
        self._set_status(f"完成：共 {n} 句" if n else "完成：未识别到内容")
        self.btn_fix.setEnabled(bool(n) and bool(self._lines))

    def _on_failed(self, msg):
        self._set_status(msg, err=True)
        self.edit.append(f"⚠ {msg}")

    # ---- ✨ 纠错 ----
    def _do_fix(self):
        if not self._lines:
            return
        if not asr_fix_ready():                       # 未配置：只红字引导，不发请求、不改原文
            self._set_status(_FIX_UNCONFIGURED, err=True)
            return
        self.btn_fix.setEnabled(False)
        self._set_status("✨ 纠错中（送 DeepSeek 按语义改同音字）…")
        texts = [t for _s, t in self._lines]
        self._fix_worker = _FixWorker(texts, self._glossary, asr_fix_config(), self)
        self._fix_worker.done.connect(self._on_fixed)
        self._fix_worker.failed.connect(self._on_fix_failed)
        self._fix_worker.finished.connect(self._fix_worker.deleteLater)
        self._fix_worker.start()

    def _on_fixed(self, fixed):
        # 行数与 self._lines 严格一致（correct_segments 保证）：按原时间戳重写，只换字
        self.edit.clear()
        self.edit.append(f"# 模型 {self._size}（CPU 推理） · ✨ 已用 DeepSeek 语义纠错\n")
        changed = 0
        for (start, orig), new in zip(self._lines, fixed):
            new = (new or "").strip() or orig         # 某行没纠正到→保留原文，绝不落空
            if new != orig:
                changed += 1
            self.edit.append(f"[{_fmt_ts(start)}] {new}")
        sb = self.edit.verticalScrollBar()
        sb.setValue(sb.maximum())
        self._set_status(f"✨ 纠错完成：改了 {changed} 句（共 {len(self._lines)} 句）")
        self.btn_fix.setEnabled(True)

    def _on_fix_failed(self, msg):
        self._set_status(msg, err=True)
        self.btn_fix.setEnabled(True)                 # 失败可重试，原文一个字没被改坏

    # ---- 内部 ----
    def _append_meta(self):
        if self._size:
            self.edit.append(f"# 模型 {self._size}（CPU 推理） · 逐句实时追加，识别中…\n")

    def _set_status(self, text, err=False):
        self.lbl_status.setText(text)
        self.lbl_status.setProperty("err", "1" if err else "0")
        self.lbl_status.style().unpolish(self.lbl_status)
        self.lbl_status.style().polish(self.lbl_status)

    # ---- 无边框拖拽：标题条区域按下可拖动整个浮窗 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.position().toPoint()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(self.pos() + e.position().toPoint() - self._drag)
            self._drag = e.position().toPoint()

    def mouseReleaseEvent(self, e):
        self._drag = None


_win_ref = None


def show_asr_live(parent, path):
    """弹出（并启动）实时字幕浮窗；再次调用会先收掉上一个，保持单实例。"""
    global _win_ref
    if _win_ref is not None:
        try:
            _win_ref.close_win()
        except Exception:
            _win_ref = None
    win = AsrLiveWindow(parent, path, tr.best_ready_size())
    _win_ref = win
    screen = QApplication.primaryScreen()
    if screen:
        geo = screen.availableGeometry()
        win.move(geo.right() - W - 40, geo.top() + 80)
    win.show()
    win.start()
    return win


def hide_asr_live():
    """收掉当前浮窗（若有）。"""
    global _win_ref
    _win_ref = None
