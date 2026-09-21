"""
gui/tool_panels.py —— 工具中心各小工具的面板实现
纯 UI 层，业务逻辑全部复用 video_text_tools 功能包（回调式、无 Qt 依赖）。
耗时操作统一放 ToolWorker(QThread) 里跑，UI 只做信号转发。
可选依赖（pyautogui/pyperclip/smbclient/pymysql）一律延迟导入，缺失时给安装提示。
"""
import os
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QEvent
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QLineEdit, QSpinBox, QDoubleSpinBox,
                               QComboBox, QCheckBox, QListWidget, QTableWidget,
                               QTableWidgetItem, QHeaderView, QFileDialog,
                               QPlainTextEdit, QProgressBar, QFormLayout,
                               QGroupBox, QMessageBox, QAbstractItemView,
                               QApplication)

from core.config import DOWNLOAD_DIR, MATERIAL_DIR
from gui.header import page_header
from utils.desktop_utils import open_path

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv"}

# 维护人盲输口令：在素材提取页键盘直接输这一串（不显示、不输到任何框），
# 匹配上就弹出密文地址框。定位是“不主动暴露给同事”，不是强安全（口令会
# 存在于源码/exe 里，逆向可得）；真正的强安全靠打包时注入 config_local.py。
API_MAINTAINER_CODE = "leiliang3991"


# ====================================================================
# 通用：后台工作线程
# ====================================================================
class ToolWorker(QThread):
    """把阻塞的工具函数丢到后台：fn(log, progress, should_stop) -> 结果"""
    log = Signal(str)
    progress = Signal(int, int, str)      # current, total, name
    done = Signal(object)                 # 结果 dict 或 Exception

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._stop = False

    def stop(self):
        self._stop = True

    def _should_stop(self):
        return self._stop

    def run(self):
        try:
            self.done.emit(self._fn(self.log.emit, self.progress.emit,
                                    self._should_stop))
        except Exception as e:              # 统一回抛给面板处理
            self.done.emit(e)


class BasePanel(QWidget):
    """带「日志区 + 进度条 + 启动/停止」骨架的工具面板"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 8, 18, 14)
        outer.setSpacing(10)
        self._build(outer)

    def _build(self, outer):
        raise NotImplementedError

    # ---- 骨架件（子类在 _build 里调用） ----
    def make_log_box(self, outer, height=140):
        self.log = QPlainTextEdit()
        self.log.setObjectName("LogBox")
        self.log.setReadOnly(True)
        self.log.setFixedHeight(height)
        outer.addWidget(self.log)

    def make_run_row(self, outer, run_text="▶ 开始执行"):
        row = QHBoxLayout()
        self.b_run = QPushButton(run_text)
        self.b_stop = QPushButton("⏹ 停止")
        self.b_stop.setObjectName("GhostBtn")
        self.b_stop.setEnabled(False)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        row.addWidget(self.b_run)
        row.addWidget(self.b_stop)
        row.addWidget(self.bar, 1)
        outer.addLayout(row)
        self.b_run.clicked.connect(self._on_run)
        self.b_stop.clicked.connect(self._on_stop)

    # ---- 运行控制 ----
    def _on_run(self):
        fn = self._task()                   # 子类返回闭包或 None（None=不启动）
        if fn is None:
            return
        self.b_run.setEnabled(False)
        self.b_stop.setEnabled(True)
        self.bar.setValue(0)
        self._worker = ToolWorker(fn, self)
        self._worker.log.connect(self._append_log)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_stop(self):
        if self._worker:
            self._worker.stop()
            self._append_log("⏹ 收到停止请求…")

    def _on_done(self, res):
        self.b_run.setEnabled(True)
        self.b_stop.setEnabled(False)
        self._worker = None
        if isinstance(res, Exception):
            self._append_log(f"❌ 执行异常：{res}")
            QMessageBox.warning(self, "执行失败", str(res))
            return
        self.on_result(res)

    def on_result(self, res):
        pass

    def _on_progress(self, cur, total, name):
        self.bar.setValue(int(cur / max(total, 1) * 100))

    def _append_log(self, msg):
        self.log.appendPlainText(str(msg))

    def _task(self):
        """子类返回 fn(log, progress, should_stop) 闭包；返回 None 表示不启动"""
        return None


# ====================================================================
# 通用：文件清单（添加文件 / 添加文件夹 / 清空）
# ====================================================================
class FileListWidget(QWidget):
    def __init__(self, title="待处理文件", video_only=True, parent=None):
        super().__init__(parent)
        self.video_only = video_only
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        head = QHBoxLayout()
        lab = QLabel(f"<b>{title}</b>")
        head.addWidget(lab)
        head.addStretch(1)
        self.lbl_count = QLabel("0 个")
        self.lbl_count.setObjectName("PageTip")
        head.addWidget(self.lbl_count)
        for text, slot in (("添加文件", self._add_files),
                           ("添加文件夹", self._add_dir),
                           ("清空", self._clear)):
            b = QPushButton(text)
            b.setObjectName("GhostBtn")
            b.clicked.connect(slot)
            head.addWidget(b)
        lay.addLayout(head)
        self.list = QListWidget()
        self.list.setFixedHeight(110)
        lay.addWidget(self.list)

    def paths(self):
        return [self.list.item(i).text() for i in range(self.list.count())]

    def _accept(self, p):
        return not self.video_only or Path(p).suffix.lower() in VIDEO_EXT

    def _add(self, paths):
        existing = set(self.paths())
        for p in paths:
            if self._accept(p) and p not in existing:
                self.list.addItem(p)
                existing.add(p)
        self.lbl_count.setText(f"{self.list.count()} 个")

    def _add_files(self):
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXT)) if self.video_only else "*"
        files, _ = QFileDialog.getOpenFileNames(self, "选择文件", "",
                                                f"视频文件 ({exts});;所有文件 (*)")
        self._add(files)

    def _add_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if d:
            try:
                self._add([os.path.join(d, f) for f in sorted(os.listdir(d))
                           if os.path.isfile(os.path.join(d, f))])
            except OSError as e:
                QMessageBox.warning(self, "读取失败", str(e))

    def _clear(self):
        self.list.clear()
        self.lbl_count.setText("0 个")


def _dep_missing_panel(hint: str) -> QWidget:
    """可选依赖缺失时的占位面板"""
    w = QWidget()
    lay = QVBoxLayout(w)
    lab = QLabel(hint)
    lab.setWordWrap(True)
    lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lab.setStyleSheet("font-size:13px; color:#8F959E; padding:40px;")
    lay.addWidget(lab)
    return w


# ====================================================================
# 1. 视频水印 / 格式化
# ====================================================================
class WatermarkPanel(BasePanel):
    def _build(self, outer):
        outer.addWidget(page_header("视频水印 / 格式化",
                                    "批量打水印（固定右下角 / 碰撞反弹）或统一压制分辨率码率",
                                    icon="💧"))
        self.files = FileListWidget("视频文件")
        outer.addWidget(self.files)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.ed_wm = QLineEdit()
        self.ed_wm.setPlaceholderText("可选：水印 PNG 图片；留空则只格式化不加水印")
        b = QPushButton("浏览…")
        b.setObjectName("GhostBtn")
        b.clicked.connect(self._pick_wm)
        r1 = QHBoxLayout()
        r1.addWidget(self.ed_wm, 1)
        r1.addWidget(b)
        form.addRow("水印图：", r1)

        self.cb_mode = QComboBox()
        self.cb_mode.addItems(["右下角固定", "碰撞反弹", "右下角 + 碰撞反弹"])
        form.addRow("水印位置：", self.cb_mode)

        self.ed_out = QLineEdit(str(Path(DOWNLOAD_DIR) / "水印输出"))
        b2 = QPushButton("浏览…")
        b2.setObjectName("GhostBtn")
        b2.clicked.connect(self._pick_out)
        r2 = QHBoxLayout()
        r2.addWidget(self.ed_out, 1)
        r2.addWidget(b2)
        form.addRow("输出目录：", r2)
        outer.addLayout(form)

        self.make_log_box(outer)
        self.make_run_row(outer, "▶ 开始处理")

    def _pick_wm(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择水印图", "",
                                           "图片 (*.png *.jpg *.jpeg *.webp)")
        if p:
            self.ed_wm.setText(p)

    def _pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.ed_out.setText(d)

    def _task(self):
        paths = self.files.paths()
        if not paths:
            QMessageBox.information(self, "提示", "请先添加要处理的视频文件")
            return None
        wm = self.ed_wm.text().strip()
        params = {"mode": self.cb_mode.currentIndex() + 1}
        out_dir = self.ed_out.text().strip()

        def fn(log, progress, should_stop):
            from video_text_tools.watermark import process_videos
            return process_videos(paths, watermark_path=wm, params=params,
                                  output_dir=out_dir, progress_callback=progress,
                                  log_callback=log, should_stop=should_stop)
        return fn

    def on_result(self, res):
        if isinstance(res, dict):
            self._append_log(f"🎉 完成：成功 {res.get('success', 0)}，"
                             f"跳过 {res.get('skipped', 0)}，失败 {res.get('failed', 0)}")


# ====================================================================
# 2. 批量改名
# ====================================================================
_RULE_TYPES = ["数字", "大写字母", "小写字母", "罗马数字", "希腊字母", "文本", "原文件名"]
_NUM_TYPES = _RULE_TYPES[:5]


class _RuleRow(QWidget):
    """单条改名规则：类型 + 起始号/补零/文本，按类型启停控件"""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        self.combo.addItems(_RULE_TYPES)
        self.combo.setFixedWidth(100)
        self.spin = QSpinBox()
        self.spin.setRange(1, 9999)
        self.spin.setFixedWidth(64)
        self.pad = QSpinBox()
        self.pad.setRange(1, 6)
        self.pad.setValue(2)
        self.pad.setFixedWidth(56)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("固定文本")
        for w in (self.combo, QLabel("起始"), self.spin, QLabel("补零"),
                  self.pad, self.edit):
            lay.addWidget(w)
        lay.addStretch(1)
        self.combo.currentTextChanged.connect(self._sync)
        self._sync(self.combo.currentText())

    def _sync(self, t):
        self.spin.setEnabled(t in _NUM_TYPES)
        self.pad.setEnabled(t == "数字")
        self.edit.setEnabled(t == "文本")

    def to_rule(self):
        t = self.combo.currentText()
        if t == "文本":
            return {"type": "文本", "text": self.edit.text()}
        if t == "原文件名":
            return {"type": "原文件名"}
        r = {"type": t, "start_num": self.spin.value()}
        if t == "数字":
            r["padding"] = self.pad.value()
        return r


class RenamePanel(BasePanel):
    def _build(self, outer):
        outer.addWidget(page_header("批量改名",
                                    "按规则拼接新文件名：编号 + 文本 + 原文件名，先预览再执行",
                                    icon="🏷"))
        self.files = FileListWidget("文件列表（按上下顺序套用编号）", video_only=False)
        outer.addWidget(self.files)

        box = QGroupBox("命名规则（从上到下拼接）")
        vbox = QVBoxLayout(box)
        self.rule_rows = QWidget()
        rl = QVBoxLayout(self.rule_rows)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        vbox.addWidget(self.rule_rows)
        br = QHBoxLayout()
        b_add = QPushButton("＋ 添加规则")
        b_add.setObjectName("GhostBtn")
        b_add.clicked.connect(self._add_rule)
        b_del = QPushButton("－ 删除末条")
        b_del.setObjectName("GhostBtn")
        b_del.clicked.connect(self._del_rule)
        br.addWidget(b_add)
        br.addWidget(b_del)
        br.addStretch(1)
        vbox.addLayout(br)
        outer.addWidget(box)
        self._add_rule()
        self._add_rule()

        row = QHBoxLayout()
        self.b_preview = QPushButton("👁 预览")
        self.b_preview.setObjectName("GhostBtn")
        self.b_preview.clicked.connect(self._preview)
        row.addWidget(self.b_preview)
        row.addStretch(1)
        outer.addLayout(row)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["原文件名", "新文件名"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setFixedHeight(150)
        outer.addWidget(self.table)

        self.make_log_box(outer, 70)
        self.make_run_row(outer, "▶ 执行改名")

    def _add_rule(self):
        rl = self.rule_rows.layout()
        rl.addWidget(_RuleRow())

    def _del_rule(self):
        rl = self.rule_rows.layout()
        if rl.count() > 1:
            w = rl.takeAt(rl.count() - 1).widget()
            if w:
                w.deleteLater()

    def _pattern(self):
        return [r.to_rule() for r in self.rule_rows.findChildren(_RuleRow)]

    def _engine(self):
        from video_text_tools.renamer import RenameEngine
        return RenameEngine(self.files.paths(), self._pattern())

    def _preview(self):
        paths = self.files.paths()
        if not paths:
            QMessageBox.information(self, "提示", "请先添加文件")
            return
        self._pending = self._engine().preview()
        self._fill_table(self._pending)

    def _fill_table(self, items):
        self.table.setRowCount(len(items))
        for i, it in enumerate(items):
            self.table.setItem(i, 0, QTableWidgetItem(it["old_name"]))
            self.table.setItem(i, 1, QTableWidgetItem(it["new_name"]))

    def _task(self):
        paths = self.files.paths()
        if not paths:
            QMessageBox.information(self, "提示", "请先添加文件")
            return None
        if QMessageBox.question(self, "确认改名",
                                f"即将对 {len(paths)} 个文件重命名，无法撤销，确认执行？"
                          ) != QMessageBox.StandardButton.Yes:
            return None
        engine = self._engine()

        def fn(log, progress, should_stop):
            res = engine.execute(progress_callback=progress)
            log(f"改名完成：成功 {res['renamed']}，失败 {res['failed']}")
            return res
        return fn

    def on_result(self, res):
        if isinstance(res, dict) and res.get("results"):
            self._fill_table(res["results"])


# ====================================================================
# 3. 封面提取 / 视频信息
# ====================================================================
class CoverPanel(BasePanel):
    def _build(self, outer):
        outer.addWidget(page_header("封面提取",
                                    "ffprobe 读取视频参数，抽取指定时间点画面生成封面图",
                                    icon="🖼"))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.ed_video = QLineEdit()
        b = QPushButton("浏览…")
        b.setObjectName("GhostBtn")
        b.clicked.connect(self._pick)
        r = QHBoxLayout()
        r.addWidget(self.ed_video, 1)
        r.addWidget(b)
        form.addRow("视频文件：", r)
        self.spin_t = QDoubleSpinBox()
        self.spin_t.setRange(0, 600)
        self.spin_t.setValue(1.0)
        self.spin_t.setSuffix(" 秒")
        form.addRow("取帧时间点：", self.spin_t)
        outer.addLayout(form)

        self.lbl_info = QLabel("选择文件后可先「读取信息」查看分辨率/码率/帧率/时长")
        self.lbl_info.setObjectName("PageTip")
        self.lbl_info.setWordWrap(True)
        outer.addWidget(self.lbl_info)

        r2 = QHBoxLayout()
        self.b_info = QPushButton("🔍 读取信息")
        self.b_info.setObjectName("GhostBtn")
        self.b_info.clicked.connect(self._read_info)
        r2.addWidget(self.b_info)
        r2.addStretch(1)
        outer.addLayout(r2)

        self.preview = QLabel()
        self.preview.setFixedSize(180, 320)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background:#F2F3F5; border-radius:8px;"
                                   "font-size:12px; color:#8F959E;")
        self.preview.setText("封面预览")
        outer.addWidget(self.preview)

        self.make_log_box(outer, 80)
        self.make_run_row(outer, "▶ 提取封面")

    def _pick(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择视频", "", "视频文件 (*.mp4 *.mov *.avi *.mkv)")
        if p:
            self.ed_video.setText(p)
            self._read_info()

    def _read_info(self):
        p = self.ed_video.text().strip()
        if not p or not os.path.exists(p):
            self.lbl_info.setText("请先选择有效的视频文件")
            return
        from video_text_tools.ffmpeg_utils import get_video_info
        info = get_video_info(p)
        if not info:
            self.lbl_info.setText("读取失败：请确认已安装 ffmpeg/ffprobe 并在 PATH 中")
            return
        self.lbl_info.setText(
            f"{info['width']}×{info['height']}（{info['orientation']}） · "
            f"{info['codec']} · {info['fps']} fps · {info['bitrate']} · "
            f"音频 {info['audio_bitrate']} · 时长 {info['duration']}")

    def _task(self):
        p = self.ed_video.text().strip()
        if not p or not os.path.exists(p):
            QMessageBox.information(self, "提示", "请先选择有效的视频文件")
            return None
        t = self.spin_t.value()

        def fn(log, progress, should_stop):
            from video_text_tools.ffmpeg_utils import get_video_thumbnail
            out = str(Path(p).with_name(Path(p).stem + "_封面.png"))
            progress(0, 1, Path(p).name)
            if get_video_thumbnail(p, out, time_pos=t):
                log(f"✅ 封面已保存：{out}")
                return {"ok": True, "path": out}
            log("❌ 提取失败：请确认 ffmpeg 可用（见输出目录说明）")
            return {"ok": False, "path": ""}
        return fn

    def on_result(self, res):
        if isinstance(res, dict) and res.get("ok"):
            self.preview.setPixmap(QPixmap(res["path"]).scaled(
                self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.bar.setValue(100)


# ====================================================================
# 4. 批量粘贴录入（可选依赖 pyautogui + pyperclip）
# ====================================================================
class BatchInputPanel(BasePanel):
    def _build(self, outer):
        outer.addWidget(page_header("批量粘贴录入",
                                    "把剪贴板里的多行文本逐行自动粘贴到目标输入框",
                                    icon="⌨"))
        tip = QLabel("用法：① 复制多行文本 → ② 点「开始录入」→ "
                     "③ 倒计时内把光标点到目标输入框 → ④ 工具自动逐行全选/粘贴/回车。"
                     "macOS 需在「系统设置 → 隐私与安全性 → 辅助功能」中授权本程序。")
        tip.setObjectName("PageTip")
        tip.setWordWrap(True)
        outer.addWidget(tip)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.spin_cd = QSpinBox()
        self.spin_cd.setRange(1, 10)
        self.spin_cd.setValue(3)
        self.spin_cd.setSuffix(" 秒")
        form.addRow("启动倒计时：", self.spin_cd)
        self.ck_clear = QCheckBox("每行录入前清空输入框")
        self.ck_clear.setChecked(True)
        form.addRow("", self.ck_clear)
        outer.addLayout(form)

        self.lbl_lines = QLabel("剪贴板当前：未读取")
        self.lbl_lines.setObjectName("PageTip")
        b_peek = QPushButton("📋 读取剪贴板预览")
        b_peek.setObjectName("GhostBtn")
        b_peek.clicked.connect(self._peek)
        r = QHBoxLayout()
        r.addWidget(self.lbl_lines, 1)
        r.addWidget(b_peek)
        outer.addLayout(r)

        self.make_log_box(outer)
        self.make_run_row(outer, "▶ 开始录入")

    def _batch(self):
        from video_text_tools.batch_input import BatchInput
        return BatchInput({"clear_input": self.ck_clear.isChecked(),
                           "countdown_sec": self.spin_cd.value()})

    def _peek(self):
        lines = self._read_lines()
        if lines is None:
            return
        self.lbl_lines.setText(f"剪贴板当前：{len(lines)} 行待录入"
                               + (f"（首行：{lines[0][:20]}…）" if lines else ""))

    def _read_lines(self):
        try:
            from video_text_tools.batch_input import read_clipboard_lines
        except ImportError:
            QMessageBox.warning(self, "缺少依赖",
                                "批量录入需要 pyautogui 与 pyperclip：\n\n"
                                "    pip install pyautogui pyperclip")
            return None
        lines = read_clipboard_lines()
        if not lines:
            QMessageBox.information(self, "提示", "剪贴板里没有文本内容")
            return None
        return lines

    def _task(self):
        lines = self._read_lines()
        if not lines:
            return None
        batch = self._batch()

        def fn(log, progress, should_stop):
            log(f"⏳ 倒计时 {batch.config['countdown_sec']} 秒，请把光标点到目标输入框…")
            if not batch.countdown(on_tick=lambda r: log(f"   {r + 1}…")):
                log("⏹ 已取消")
                return {"total": len(lines), "input": 0, "interrupted": True}
            res = batch.run(lines, progress_callback=progress,
                            should_stop=should_stop)
            log(f"录入完成：{res['input']}/{res['total']} 行"
                + ("（被中断）" if res["interrupted"] else ""))
            return res
        return fn


# ====================================================================
# 5. SMB 上传（可选依赖 smbclient）
# ====================================================================
class SmbPanel(BasePanel):
    def _build(self, outer):
        outer.addWidget(page_header("SMB 上传",
                                    "把成品视频批量上传到公司共享盘",
                                    icon="📤"))
        from video_text_tools.config import SMB_CONFIG
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.ed_host = QLineEdit(SMB_CONFIG.get("host", ""))
        self.ed_share = QLineEdit(SMB_CONFIG.get("share_name", ""))
        self.ed_user = QLineEdit(SMB_CONFIG.get("username", ""))
        self.ed_pass = QLineEdit(SMB_CONFIG.get("password", ""))
        self.ed_pass.setEchoMode(QLineEdit.Password)
        self.ed_remote = QLineEdit(SMB_CONFIG.get("remote_path", ""))
        self.ed_remote.setPlaceholderText("共享内的子目录，如：溯源视频")
        form.addRow("服务器地址：", self.ed_host)
        form.addRow("共享名：", self.ed_share)
        form.addRow("用户名：", self.ed_user)
        form.addRow("密码：", self.ed_pass)
        form.addRow("远程子目录：", self.ed_remote)
        outer.addLayout(form)

        self.files = FileListWidget("待上传文件")
        outer.addWidget(self.files)

        r = QHBoxLayout()
        self.b_test = QPushButton("🔌 测试连接")
        self.b_test.setObjectName("GhostBtn")
        self.b_test.clicked.connect(self._test)
        r.addWidget(self.b_test)
        r.addStretch(1)
        outer.addLayout(r)

        self.make_log_box(outer)
        self.make_run_row(outer, "▶ 上传")

    def _cfg(self):
        return {"host": self.ed_host.text().strip(),
                "share_name": self.ed_share.text().strip(),
                "username": self.ed_user.text().strip(),
                "password": self.ed_pass.text(),
                "remote_path": self.ed_remote.text().strip(),
                "domain": "", "port": 445}

    def _utils(self):
        try:
            from video_text_tools.smb_utils import SMBUtils
        except ImportError:
            QMessageBox.warning(self, "缺少依赖",
                                "SMB 上传需要 smbclient：\n\n    pip install smbclient")
            return None
        return SMBUtils(self._cfg())

    def _test(self):
        su = self._utils()
        if su:
            ok = su.check_connection()
            self._append_log("✅ 连接成功" if ok else "❌ 连接失败，请检查地址/账号")

    def _task(self):
        paths = self.files.paths()
        if not paths:
            QMessageBox.information(self, "提示", "请先添加要上传的文件")
            return None
        su = self._utils()
        if not su:
            return None

        def fn(log, progress, should_stop):
            results = su.upload_files(paths, log_callback=log)
            ok = sum(1 for r in results if r["success"])
            return {"success": ok, "failed": len(results) - ok}
        return fn


# ====================================================================
# 6. 视频溯源（可选依赖 pymysql，需内网 MySQL）
# ====================================================================
class TracePanel(BasePanel):
    def _build(self, outer):
        outer.addWidget(page_header("视频溯源",
                                    "从溯源码池取码，按「溯源码_日期_剪辑_运营」重命名并入库",
                                    icon="🔎"))
        from video_text_tools.config import DB_CFG
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.ed_host = QLineEdit(DB_CFG.get("host", ""))
        self.ed_db = QLineEdit(DB_CFG.get("database", ""))
        self.ed_user = QLineEdit(DB_CFG.get("user", ""))
        self.ed_pass = QLineEdit(DB_CFG.get("password", ""))
        self.ed_pass.setEchoMode(QLineEdit.Password)
        self.ed_name = QLineEdit("")
        self.ed_name.setPlaceholderText("剪辑人姓名（用于文件名首拼）")
        self.ed_op = QLineEdit("")
        self.ed_op.setPlaceholderText("运营姓名，留空则与剪辑人相同")
        self.spin_uid = QSpinBox()
        self.spin_uid.setRange(1, 999999)
        form.addRow("MySQL 地址：", self.ed_host)
        form.addRow("数据库：", self.ed_db)
        form.addRow("账号：", self.ed_user)
        form.addRow("密码：", self.ed_pass)
        form.addRow("剪辑人：", self.ed_name)
        form.addRow("运营：", self.ed_op)
        form.addRow("剪辑人 ID：", self.spin_uid)
        outer.addLayout(form)

        self.files = FileListWidget("待溯源视频")
        outer.addWidget(self.files)

        self.make_log_box(outer)
        self.make_run_row(outer, "▶ 开始溯源")

    def _db_cfg(self):
        from video_text_tools.config import DB_CFG
        return {**DB_CFG, "host": self.ed_host.text().strip() or DB_CFG["host"],
                "database": self.ed_db.text().strip() or DB_CFG["database"],
                "user": self.ed_user.text().strip() or DB_CFG["user"],
                "password": self.ed_pass.text()}

    def _task(self):
        paths = self.files.paths()
        name = self.ed_name.text().strip()
        if not paths:
            QMessageBox.information(self, "提示", "请先添加要溯源的视频")
            return None
        if not name:
            QMessageBox.information(self, "提示", "请填写剪辑人姓名")
            return None
        try:
            import pymysql  # noqa: F401  仅探测依赖
        except ImportError:
            QMessageBox.warning(self, "缺少依赖",
                                "溯源功能需要 pymysql：\n\n    pip install pymysql")
            return None
        from video_text_tools.trace_utils import TraceUtils
        tu = TraceUtils({"user_id": self.spin_uid.value(), "real_name": name,
                         "role": "editor"}, db_cfg=self._db_cfg())
        op = self.ed_op.text().strip() or None

        def fn(log, progress, should_stop):
            return tu.process_videos(paths, operator_name=op,
                                     progress_callback=lambda pct:
                                     progress(pct, 100, ""),
                                     log_callback=log)
        return fn

    def on_result(self, res):
        if isinstance(res, dict):
            ok = sum(1 for r in res.values() if r.get("success"))
            self._append_log(f"溯源完成：成功 {ok}/{len(res)}")


# ====================================================================
# 7. 素材提取（分享链接 → 去水印视频/图集/文案）
# ====================================================================
class MaterialPanel(BasePanel):
    """合并自原「文案提取/视频提取」两个预留接口：一条链接同时拿
    去水印成品与文案。直链域名白名单读 material/api_text/ 外部文件，
    每次执行重新加载——CDN 域名变了只需换 txt，不用改代码。"""

    def _build(self, outer):
        outer.addWidget(page_header("素材提取",
                                    "粘贴唞喑/筷手分享文案（可多行批量）：一键提取去水印视频、图集、文案",
                                    icon="🧲"))

        self.ed_input = QPlainTextEdit()
        self.ed_input.setPlaceholderText(
            "每行一条分享文案，链接会自动识别，例如：\n"
            "7.99 复制打开抖音，看看作品 https://v.douyin.com/xxxx/\n"
            "https://www.kuaishou.com/f/xxxx")
        self.ed_input.setFixedHeight(96)
        outer.addWidget(self.ed_input)

        row = QHBoxLayout()
        self.ck_video = QCheckBox("去水印视频")
        self.ck_video.setChecked(True)
        self.ck_text = QCheckBox("文案")
        self.ck_text.setChecked(True)
        self.ck_images = QCheckBox("图集")
        self.ck_images.setChecked(True)
        row.addWidget(self.ck_video)
        row.addWidget(self.ck_text)
        row.addWidget(self.ck_images)
        row.addStretch(1)
        b_open = QPushButton("📂 打开输出目录")
        b_open.setObjectName("GhostBtn")
        b_open.clicked.connect(lambda: open_path(self.ed_out.text().strip()))
        row.addWidget(b_open)
        outer.addLayout(row)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.ed_out = QLineEdit(str(Path(MATERIAL_DIR) / "素材提取"))
        b = QPushButton("浏览…")
        b.setObjectName("GhostBtn")
        b.clicked.connect(self._pick_out)
        r2 = QHBoxLayout()
        r2.addWidget(self.ed_out, 1)
        r2.addWidget(b)
        form.addRow("保存到：", r2)
        outer.addLayout(form)

        # ---- 域名白名单：由使用者首次使用时导入，不随代码分发 ----
        wl = QHBoxLayout()
        self.lbl_wl = QLabel()                      # 实时状态：已导入(条数)/未导入
        b_wl = QPushButton("📤 导入/更新域名名单")
        b_wl.setObjectName("GhostBtn")
        b_wl.clicked.connect(self._import_lists)
        wl.addWidget(self.lbl_wl, 1)
        wl.addWidget(b_wl)
        outer.addLayout(wl)

        # ---- 接口凭证：默认只改 uid/key，地址藏起来（维护人盲输口令才出现）----
        form2 = QFormLayout()
        form2.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.lbl_api_host = QLabel()          # 只显示“配了没”，不显示地址本身
        self.lbl_api_host.setStyleSheet("font-size:12px; color:#646A73;"
                                        " background:transparent;")
        self.ed_api_uid = QLineEdit()
        self.ed_api_key = QLineEdit()
        self.ed_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        b_save = QPushButton("💾 保存接口配置")
        b_save.setObjectName("GhostBtn")
        b_save.clicked.connect(self._save_api)
        r3 = QHBoxLayout()
        r3.addWidget(b_save)
        r3.addStretch(1)
        form2.addRow("接口地址：", self.lbl_api_host)
        form2.addRow("UID：", self.ed_api_uid)
        form2.addRow("Key：", r3)
        form2.addRow("", self.ed_api_key)
        outer.addLayout(form2)

        # 隐藏维护人区：口令没输对之前完全不存在（不占位、不可 Tab 到）
        self._base_unlocked = False
        self._key_buf = ""
        self.api_base_box = QGroupBox("🔓 维护人 · 接口地址")
        bl = QHBoxLayout(self.api_base_box)
        self.ed_api_base = QLineEdit()
        self.ed_api_base.setEchoMode(QLineEdit.EchoMode.Password)   # 密文显示
        self.ed_api_base.setPlaceholderText("接口地址（可 Ctrl+V 粘贴，显示为密文）")
        b_apply = QPushButton("应用地址")
        b_apply.setObjectName("GhostBtn")
        b_apply.clicked.connect(self._save_api)
        bl.addWidget(self.ed_api_base, 1)
        bl.addWidget(b_apply)
        self.api_base_box.setVisible(False)
        outer.addWidget(self.api_base_box)

        tip = QLabel("uid/key 与名单变更无需重新打包：改上方配置、重新导入名单即生效；"
                     "接口地址默认隐藏，需维护人在本页键盘盲输口令才会出现输入框；"
                     "文案成功后追加到同目录「文案样本库.csv」（积累样本，后期喂大模型学写脚本）")
        tip.setObjectName("InlineTip")
        outer.addWidget(tip)

        self._load_api()                            # 生效值 = api_config.json > 程序默认
        self._refresh_wl()

        self.make_log_box(outer, height=120)
        self.make_run_row(outer, "▶ 开始提取")

        # 全局监听键盘：只有本页可见时才累积字符匹配口令（见 eventFilter）
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # ---- 维护人口令：盲输命中后解锁地址框 ----
    def eventFilter(self, obj, e):
        if (not self._base_unlocked and self.isVisible()
                and e.type() == QEvent.Type.KeyPress):
            ch = e.text()
            if ch and len(ch) == 1 and ch.isprintable():
                self._key_buf = (self._key_buf + ch)[-len(API_MAINTAINER_CODE):]
                if self._key_buf == API_MAINTAINER_CODE:
                    self._key_buf = ""
                    self._unlock_api_base()
        return super().eventFilter(obj, e)

    def _unlock_api_base(self):
        """口令命中：把地址框显示出来并聚焦（地址不打印到日志，只标已解锁）"""
        self._base_unlocked = True
        self.api_base_box.setVisible(True)
        self.ed_api_base.setText(self._api_cfg().get("base") or "")
        self.ed_api_base.setFocus()
        self.ed_api_base.selectAll()
        self.log.appendPlainText("🔓 维护人模式已解锁：可填写/修改接口地址（输入为密文）")

    def _pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if d:
            self.ed_out.setText(d)

    # ---- 接口配置与白名单维护 ----
    def _api_cfg(self):
        """当前生效的接口配置（工具界面保存的 json 覆盖程序默认值）"""
        from core.config import (MATERIAL_API_BASE, MATERIAL_API_UID,
                                 MATERIAL_API_KEY, API_TEXT_DIR)
        from video_text_tools.material_extract import resolve_api_config
        return resolve_api_config(API_TEXT_DIR, {"base": MATERIAL_API_BASE,
                                                 "uid": MATERIAL_API_UID,
                                                 "key": MATERIAL_API_KEY})

    def _load_api(self):
        cfg = self._api_cfg()
        # 地址只报“配没配”，不把接口明文暴露给使用者
        self.lbl_api_host.setText(
            "✅ 已配置" if cfg.get("base")
            else "⚠ 未配置（维护人在本页键盘盲输口令可解锁输入框）")
        self.ed_api_uid.setText(cfg["uid"])
        self.ed_api_key.setText(cfg["key"])
        if self._base_unlocked:                       # 已解锁则同步真实地址到密文框
            self.ed_api_base.setText(cfg.get("base") or "")

    def _save_api(self):
        """保存 uid/key；维护人解锁后一并写地址。地址传 None＝保留现有值。"""
        uid = self.ed_api_uid.text().strip()
        key = self.ed_api_key.text().strip()
        if not uid or not key:
            QMessageBox.information(self, "提示", "UID 和 Key 都不能留空")
            return
        base = None
        if self._base_unlocked:
            t = self.ed_api_base.text().strip()
            base = t or None                          # 填了就写，留空则保留原值
        if base is None and not self._api_cfg().get("base"):
            QMessageBox.information(
                self, "提示", "还没配接口地址：请维护人在本页盲输口令解锁后填写地址")
            return
        from core.config import API_TEXT_DIR
        from video_text_tools.material_extract import save_api_config
        try:
            save_api_config(API_TEXT_DIR, base, uid, key)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self._load_api()
        self._append_log("✓ 接口配置已保存，下次提取立即生效"
                         + ("（含接口地址）" if base else ""))

    def _wl_files(self):
        from core.config import API_TEXT_DIR
        d = Path(API_TEXT_DIR)
        return d / "video.txt", d / "image.txt"

    def _refresh_wl(self):
        from video_text_tools.material_extract import load_allowed_hosts
        fv, fi = self._wl_files()
        nv = len(load_allowed_hosts(fv)) if fv.exists() else -1
        ni = len(load_allowed_hosts(fi)) if fi.exists() else -1
        def fmt(n):
            return "未导入" if n < 0 else ("空!" if n == 0 else f"{n} 条")
        ok = nv > 0 and ni > 0
        self.lbl_wl.setText(
            f"{'✅' if ok else '⚠️'} 直链域名白名单：视频 {fmt(nv)} · 图片 {fmt(ni)}"
            "（下载的直链只认名单内域名）")
        self.lbl_wl.setStyleSheet(
            "font-size:12px; color:#00A870; background:transparent;" if ok else
            "font-size:12px; color:#D83931; background:transparent;")

    def _import_lists(self):
        """依次导入两份名单（复制为规范文件名）；中途取消则保留已导入的"""
        import shutil
        fv, fi = self._wl_files()
        for dst, label in ((fv, "视频短链域名名单"), (fi, "图片短链域名名单")):
            src, _ = QFileDialog.getOpenFileName(
                self, f"选择{label} txt（一行一个域名，取消则跳过）",
                str(Path.home()), "文本文件 (*.txt);;所有文件 (*)")
            if not src:
                continue
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            except OSError as e:
                QMessageBox.warning(self, "导入失败", f"{label}：{e}")
                break
        self._refresh_wl()

    def _task(self):
        from video_text_tools import material_extract as me
        urls = me.extract_share_urls(self.ed_input.toPlainText())
        if not urls:
            QMessageBox.information(self, "提示", "没找到有效的分享链接，请检查粘贴内容")
            return None
        if not (self.ck_video.isChecked() or self.ck_text.isChecked()
                or self.ck_images.isChecked()):
            QMessageBox.information(self, "提示", "请至少勾选一种要提取的内容")
            return None
        out_dir = Path(self.ed_out.text().strip() or str(Path(MATERIAL_DIR) / "素材提取"))
        out_dir.mkdir(parents=True, exist_ok=True)
        corpus = out_dir / "文案样本库.csv"      # 追加式样本库，跨批次沉淀
        want = (self.ck_video.isChecked(), self.ck_text.isChecked(),
                self.ck_images.isChecked())

        # 首次使用引导：白名单缺失/为空时提醒导入（名单不随代码分发，由使用者上传）
        fv, fi = self._wl_files()
        if want[0] or want[2]:
            missing = [n for n, f in (("视频", fv), ("图片", fi))
                       if not me.load_allowed_hosts(f)]
            if missing:
                r = QMessageBox.question(
                    self, "导入直链域名白名单",
                    "尚未导入有效的域名白名单（" + "、".join(missing) + "）。\n"
                    "没有名单时下载不做域名限制，不安全也不推荐。\n\n现在导入吗？")
                if r == QMessageBox.StandardButton.Yes:
                    self._import_lists()
                    if [n for n, f in (("视频", fv), ("图片", fi))
                            if not me.load_allowed_hosts(f)]:
                        return None          # 仍未导入齐：回去补，不带着风险跑

        cfg = self._api_cfg()                # 启动前固化配置快照，透传功能层

        def fn(log, progress, should_stop):
            from core.config import API_TEXT_DIR
            # 每次执行重读白名单：重新导入后立即生效
            hosts_v = me.load_allowed_hosts(Path(API_TEXT_DIR) / "video.txt")
            hosts_i = me.load_allowed_hosts(Path(API_TEXT_DIR) / "image.txt")
            if not hosts_v:
                log("⚠ 视频域名名单为空：本次不做视频域名限制")
            saved, fails = [], 0
            for i, u in enumerate(urls):
                if should_stop():
                    log("⏹ 已停止")
                    break
                log(f"[{i + 1}/{len(urls)}] {u}")
                try:
                    files, notes = me.extract_one(
                        u, cfg["base"], cfg["uid"], cfg["key"],
                        out_dir, hosts_v, hosts_i,
                        want_video=want[0], want_text=want[1], want_images=want[2],
                        corpus_file=corpus if want[1] else None,
                        log=log, should_stop=should_stop)
                    saved += files
                    for n in notes:
                        log(f"  ⚠ {n}")
                except Exception as e:
                    fails += 1
                    log(f"  ✗ 失败：{e}")
                progress(i + 1, len(urls), "")
            return {"saved": saved, "fails": fails, "total": len(urls)}
        return fn

    def on_result(self, res):
        if isinstance(res, dict):
            self._append_log(f"提取完成：落盘 {len(res['saved'])} 个文件，"
                             f"失败 {res['fails']}/{res['total']} 条 → {self.ed_out.text()}")
            if res["saved"]:
                open_path(self.ed_out.text())


# ====================================================================
# 面板登记表（pages_tools 按名称取 factory）
# ====================================================================
def _safe_factory(builder):
    """factory(parent) -> QWidget；构建失败（缺依赖）时给占位提示而不是崩溃"""
    def factory(parent=None):
        try:
            return builder(parent)
        except ImportError as e:
            return _dep_missing_panel(f"该工具缺少依赖：{e}\n\n"
                                      f"安装后重新打开即可使用。")
    return factory


PANEL_FACTORIES = {
    "视频水印": _safe_factory(WatermarkPanel),
    "批量改名": _safe_factory(RenamePanel),
    "封面提取": _safe_factory(CoverPanel),
    "批量粘贴录入": _safe_factory(BatchInputPanel),
    "SMB 上传": _safe_factory(SmbPanel),
    "视频溯源": _safe_factory(TracePanel),
    "素材提取": _safe_factory(MaterialPanel),
}
