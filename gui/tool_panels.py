"""
gui/tool_panels.py —— 工具中心各小工具的面板实现
纯 UI 层，业务逻辑全部复用 video_text_tools 功能包（回调式、无 Qt 依赖）。
耗时操作统一放 ToolWorker(QThread) 里跑，UI 只做信号转发。
可选依赖（pyautogui/pyperclip/smbclient/pymysql）一律延迟导入，缺失时给安装提示。
"""
import os
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QLineEdit, QSpinBox, QDoubleSpinBox,
                               QComboBox, QCheckBox, QListWidget, QTableWidget,
                               QTableWidgetItem, QHeaderView, QFileDialog,
                               QPlainTextEdit, QProgressBar, QFormLayout,
                               QGroupBox, QMessageBox, QAbstractItemView)

from core.config import DOWNLOAD_DIR
from gui.header import page_header

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv"}


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
}
