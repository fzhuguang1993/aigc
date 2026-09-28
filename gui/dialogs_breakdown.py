"""
gui/dialogs_breakdown.py —— 工具中心「爆款拆解」面板

纯 UI：链接输入 + 选项 + 首次引导（Whisper 模型 / 豆包凭证）+ 进度/阶段状态 +
结果预览 + 导出 Word 拆解文档。业务全在 video_text_tools.breakdown（回调式、无 Qt），
耗时操作放 ToolWorker 后台跑。独立成模块避开 tool_panels 的循环导入
（要在这里拿 BasePanel/ToolWorker，tool_panels 又要延迟导入本模块的 BreakdownPanel）。
"""
from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox,
                               QCheckBox, QPlainTextEdit, QGroupBox,
                               QFormLayout, QMessageBox, QWidget, QFileDialog,
                               QSizePolicy, QListWidget, QListWidgetItem, QFrame)

from gui.header import page_header
from gui.asr_widgets import FixToggle
from gui.maintainer import Gate, app_has_unlocked
from gui.tool_panels import BasePanel, ToolWorker
from utils.desktop_utils import open_path

# 豆包配置引导文案（火山方舟：新用户额度 / 协作奖励 / 选视觉模型 / 填 ep-id）
_DOUBAO_GUIDE = (
    "怎么用豆包 Vision 拆解画面（火山方舟 Ark）：\n"
    "1) 注册火山方舟，新用户送 500 万 token 免费额度；参加「协作奖励计划」每天调用可得免费额度；\n"
    "2) 在「开通管理」里选一个<b>视觉理解</b>模型（如 Doubao-Seed-1.6-Vision，名称里带 Vision 的才行）；\n"
    "3) 在「推理接入点」创建并复制接入点 ID（形如 <b>ep-xxxxxxxx</b>），填到下面「端点 ID」；\n"
    "4) 在「API Key 管理」生成 API Key，填到下面。api_key 以你的姓名加密存本地 config.json，不明文落盘；\n"
    "5) 画面分析默认走 <b>video_url 直连</b>：模型内部自动抽帧、整条视频一次调用（≤50MB 公网直链），"
    "比逐帧上传省请求、防限流；也可切回「逐帧抽取」传统模式。")


class BreakdownPanel(BasePanel):
    """爆款拆解：粘贴链接 → 解析下载 →（直连模式：本地转写 ‖ 豆包解析并行）→ 3类提示词+整体分析 → 导出 Word 文档。"""

    def _build(self, outer):
        # 外层拆左右两栏：左＝原拆解表单/日志（全部塞进 body），右＝任务库列。
        # 重绑 outer 后，下面原有那一串 outer.* 全部落进左栏，右栏另建。
        split = QHBoxLayout()
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(12)
        body = QVBoxLayout()
        body.setSpacing(10)
        bodyw = QWidget()
        bodyw.setLayout(body)
        bodyw.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        split.addWidget(bodyw, 1)
        split.addWidget(self._build_task_col(), 0)
        outer.addLayout(split, 1)
        outer = body

        outer.addWidget(page_header(
            "爆款拆解",
            "粘贴抖音/快手分享链接：整条视频一次传豆包直连分析（与本地 Whisper 转写并行），"
            "产出画面/文案/复刻 3 类提示词与整体分析，完成后自动导出 Word 拆解文档",
            icon="🔥"))

        self.ed_input = QPlainTextEdit()
        self.ed_input.setPlaceholderText(
            "粘贴分享文案或链接（一次拆一条，取第一条能解析出视频的）：\n"
            "7.99 复制打开抖音，看看作品 https://v.douyin.com/xxxx/")
        self.ed_input.setFixedHeight(72)
        outer.addWidget(self.ed_input)

        # ---- 选项行 ----
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.cb_model = QComboBox()
        self.cb_model.addItems(["medium", "small", "base", "tiny"])
        try:
            from video_text_tools.asr import transcribe as tr
            _def = tr.best_ready_size()
        except Exception:
            _def = None
        self.cb_model.setCurrentText(_def or "medium")
        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(1, 15)
        self.sp_interval.setValue(3)
        self.sp_interval.setSuffix(" 秒")
        self.sp_conc = QSpinBox()
        self.sp_conc.setRange(1, 3)                  # 逐帧并发硬顶 3；默认 1（每帧含图上传，高并发秒撞 429）
        self.sp_conc.setValue(1)
        # 视觉分析方式：video_url 直连（默认，整条视频一次调用，免逐帧上传规避 429）
        # / frames 逐帧抽取（传统兜底）；初值从 config.json 的 breakdown 段读。
        from core.config import breakdown_config
        _bd = breakdown_config()
        self.cb_vmode = QComboBox()
        self.cb_vmode.addItem("video_url 直连（公网直链一次传豆包，免逐帧，防限流）",
                              "video_url")
        self.cb_vmode.addItem("逐帧抽取（本地抽帧+逐帧上传，传统兜底）", "frames")
        if _bd["vision_mode"] == "frames":
            self.cb_vmode.setCurrentIndex(1)
        self.dsb_fps = QDoubleSpinBox()
        self.dsb_fps.setRange(0.2, 5.0)
        self.dsb_fps.setSingleStep(0.1)
        self.dsb_fps.setDecimals(1)
        self.dsb_fps.setValue(_bd["fps"])
        self.dsb_fps.setSuffix(" fps")
        self.dsb_fps.setToolTip("模型内部抽帧频率：越高越细致但 token 越贵；短视频拆解推荐 0.3~1.0")
        self.ed_out = QLineEdit(str(Path.home() / "Downloads" / "爆款拆解"))
        b_pick = QPushButton("浏览…")
        b_pick.setObjectName("GhostBtn")
        b_pick.clicked.connect(self._pick_out)
        r_out = QHBoxLayout()
        r_out.addWidget(self.ed_out, 1)
        r_out.addWidget(b_pick)
        form.addRow("Whisper 模型：", self.cb_model)
        form.addRow("视觉方式：", self.cb_vmode)
        form.addRow("抽帧密度：", self.dsb_fps)
        form.addRow("抽帧间隔：", self.sp_interval)
        form.addRow("视觉并发：", self.sp_conc)

        def _sync_vmode(*_):
            """直连模式：fps 生效、抽帧间隔/并发置灰；逐帧模式反之。"""
            direct = self.cb_vmode.currentData() == "video_url"
            self.dsb_fps.setEnabled(direct)
            self.sp_interval.setEnabled(not direct)
            self.sp_conc.setEnabled(not direct)
        self.cb_vmode.currentIndexChanged.connect(_sync_vmode)
        _sync_vmode()
        self.ck_fix = FixToggle("✨ DeepSeek 语义纠错（先改同音字再送豆包拆解，需已配置）")
        form.addRow("识别后：", self.ck_fix)
        form.addRow("视频存到：", r_out)
        outer.addLayout(form)

        # ---- 首次引导：Whisper + 豆包 ----
        outer.addWidget(self._build_guide())

        # ---- 阶段状态 + 结果预览 ----
        mid = QHBoxLayout()
        self.stage_box = QPlainTextEdit()
        self.stage_box.setReadOnly(True)
        self.stage_box.setPlaceholderText("各阶段状态会显示在这里（✅ 成功 / ❌ 失败原因）")
        self.stage_box.setFixedWidth(260)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("拆解结果预览：分镜表 / 3 类提示词 / 整体分析")
        mid.addWidget(self.stage_box)
        mid.addWidget(self.preview, 1)
        midw = QWidget()
        midw.setLayout(mid)
        midw.setFixedHeight(200)
        outer.addWidget(midw)

        self.make_log_box(outer, height=110)
        self._refresh_status()

        self.make_run_row(outer, "▶ 开始拆解")
        self.b_report = QPushButton("🔎 打开详情页")
        self.b_report.setObjectName("GhostBtn")
        self.b_report.setToolTip("拆解完成后可打开「图集 | 播放器 | 拆解文档」三屏联动详情")
        self.b_report.setEnabled(False)
        self.b_report.clicked.connect(self._open_detail)
        b_open = QPushButton("📂 打开视频目录")
        b_open.setObjectName("GhostBtn")
        b_open.clicked.connect(lambda: open_path(self.ed_out.text().strip()))
        row2 = QHBoxLayout()
        row2.addWidget(self.b_report)
        row2.addWidget(b_open)
        row2.addStretch(1)
        outer.addLayout(row2)

    # ------------------------------------------------------------------
    # 首次引导区
    # ------------------------------------------------------------------
    def _build_guide(self):
        box = QGroupBox("首次使用引导")
        v = QVBoxLayout(box)
        # 解析下载接口状态：爆款拆解复用「素材提取」的聚客接口（同一份
        # api_config.json，不是独立配置）——直接把生效的 uid/key 显示出来，避免“改了没同步”的误判。
        self.lbl_api = QLabel()
        self.lbl_api.setToolTip(
            "解析下载复用工具中心「素材提取」的接口凭证（api_text/api_config.json），"
            "改 uid/key 请到「素材提取」面板保存，两边即时同步生效。")
        ar = QHBoxLayout()
        ar.addWidget(self.lbl_api, 1)
        v.addLayout(ar)
        # Whisper 状态 + 跳转（模型下载已收到接口管理页统一做）
        self.lbl_whisper = QLabel()
        wr = QHBoxLayout()
        wr.addWidget(self.lbl_whisper, 1)
        self.b_whisper_cfg = QPushButton("🔌 去下载模型")
        self.b_whisper_cfg.setObjectName("GhostBtn")
        self.b_whisper_cfg.clicked.connect(self._goto_api_page)
        wr.addWidget(self.b_whisper_cfg)
        v.addLayout(wr)
        # 豆包状态 + 跳转（编辑已收到接口管理页）
        self.lbl_doubao = QLabel()
        dr = QHBoxLayout()
        dr.addWidget(self.lbl_doubao, 1)
        self.b_doubao_cfg = QPushButton("🔌 去配置豆包")
        self.b_doubao_cfg.setObjectName("GhostBtn")
        self.b_doubao_cfg.clicked.connect(self._goto_api_page)
        dr.addWidget(self.b_doubao_cfg)
        v.addLayout(dr)
        tip = QLabel("模型下载与豆包/DeepSeek 接入点、领域词库都集中在「🔌 接口管理」页维护，保存即生效；"
                     "解析下载则直接复用工具中心「素材提取」的接口（非独立配置）。")
        tip.setObjectName("InlineTip")
        tip.setWordWrap(True)
        v.addWidget(tip)
        return box

    def _refresh_status(self):
        """刷新 Whisper / 豆包 两个状态标签（引导区一眼看清缺什么）"""
        from video_text_tools.asr import transcribe as tr
        from core.config import doubao_vision_ready
        size = self.cb_model.currentText()
        if tr.model_ready(size):
            self.lbl_whisper.setText(f"✅ Whisper「{size}」模型已就绪")
            self.lbl_whisper.setStyleSheet(_OK_QSS)
        else:
            best = tr.best_ready_size()
            hint = f"（已就绪的最佳档：{best}，可改选）" if best else ""
            self.lbl_whisper.setText(f"⚠ Whisper「{size}」未下载{hint}；下载请到接口管理页")
            self.lbl_whisper.setStyleSheet(_WARN_QSS)
        if doubao_vision_ready():
            self.lbl_doubao.setText("✅ 豆包 Vision 已配置")
            self.lbl_doubao.setStyleSheet(_OK_QSS)
        else:
            self.lbl_doubao.setText("⚠ 豆包 Vision 未配置（拆解画面分析必需，请到接口管理页配置）")
            self.lbl_doubao.setStyleSheet(_WARN_QSS)
        self._refresh_parse_api()

    def _refresh_parse_api(self):
        """刷新解析下载接口状态（复用「素材提取」的聚客接口，非独立配置）。"""
        try:
            from video_text_tools.breakdown.acquire import resolve_api_cfg
            from video_text_tools.material_extract import _gateway_base
            cfg = resolve_api_cfg()
            if _gateway_base():
                self.lbl_api.setText("🔗 解析下载：走网关（服务端注入凭证，本机 key 忽略）")
                self.lbl_api.setStyleSheet(_OK_QSS)
            elif cfg.get("base") and cfg.get("uid") and cfg.get("key"):
                uid, k = str(cfg["uid"]), str(cfg["key"])
                masked = f"{k[:2]}…{k[-2:]}" if len(k) > 4 else "已填"
                self.lbl_api.setText(
                    f"✅ 解析下载：复用「素材提取」接口（uid={uid} · key={masked}）")
                self.lbl_api.setStyleSheet(_OK_QSS)
            else:
                self.lbl_api.setText("⚠ 解析接口未配齐：请到 工具中心→素材提取 填地址/UID/Key 并保存")
                self.lbl_api.setStyleSheet(_WARN_QSS)
        except Exception as e:
            self.lbl_api.setText(f"⚠ 解析接口状态读取失败：{e}")
            self.lbl_api.setStyleSheet(_WARN_QSS)

    # ------------------------------------------------------------------
    # 去「接口管理」页统一维护（模型下载 / 豆包 / DeepSeek / 词库）
    # ------------------------------------------------------------------
    def _goto_api_page(self):
        """直接跳到主窗口的「接口管理」页（不需任何口令/快捷键提示）。"""
        w = self.window()
        if hasattr(w, "reveal_api_page"):
            w.reveal_api_page()

    # ------------------------------------------------------------------
    # 输出目录
    # ------------------------------------------------------------------
    def _pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "选择视频保存目录")
        if d:
            self.ed_out.setText(d)

    # ------------------------------------------------------------------
    # 主任务：交给后台线程跑 pipeline.run
    # ------------------------------------------------------------------
    def _task(self):
        text = self.ed_input.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "提示", "请先粘贴要拆解的分享链接")
            return None
        self._refresh_parse_api()   # 开始前刷新一遍，确保显示的 uid/key 是当前生效值
        from core.config import doubao_vision_config
        from video_text_tools.material_extract import load_allowed_hosts
        from core.config import API_TEXT_DIR
        opts = {
            "model_size": self.cb_model.currentText(),
            "vision_mode": self.cb_vmode.currentData(),
            "fps": self.dsb_fps.value(),
            "interval": self.sp_interval.value(),
            "scene_thresh": 0.3,
            "concurrency": self.sp_conc.value(),
            "out_dir": self.ed_out.text().strip(),
            "doubao_cfg": doubao_vision_config(),
            "hosts_video": load_allowed_hosts(Path(API_TEXT_DIR) / "video.txt"),
            "asr_fix": self.ck_fix.isChecked(),
        }

        def fn(log, progress, should_stop):
            from video_text_tools.breakdown import run as bd_run
            res = bd_run(text, opts, log=log, progress=progress,
                         should_stop=should_stop)
            # 拆解交付的后处理（全在后台线程，不卡 UI）：导出 Word → 抽图集/封面 → 入库。
            self._post_process(res, opts.get("out_dir", ""), log)
            return res
        return fn

    def _post_process(self, res, out_dir, log):
        """拆解跑完的落盘三步（worker 线程里跑）：只碰文件与库，不碰任何控件。
        只有完整成功（非半成品）才导 Word/抽图集/入库；失败的任务不进列表，只记日志。"""
        if res.is_partial():
            fails = "、".join(res.failed_stages()) or "解析未成功"
            log(f"❌ 半成品（失败阶段：{fails}）：不入库、不进拆解任务列表（仅当前会话预览）。")
            return
        import time
        from video_text_tools.breakdown.models import DepMissing
        # 1) Word（落在视频目录）
        try:
            from video_text_tools.breakdown import write_report
            path = write_report(res, out_dir=(out_dir or None), log=log)
            res.report_path = path or ""
        except DepMissing as e:
            log(f"⚠ 未导出 Word 文档：{e}")
        except Exception as e:
            log(f"✗ 导出 Word 文档失败：{e}")
        # 2) 图集 + 封面（落持久任务库目录，按时间戳命名避开 id 先有鸡还是先有蛋）
        gal_dir = ""
        try:
            from core.config import BREAKDOWN_LIBRARY
            from video_text_tools.breakdown import build_gallery
            folder = f"{time.strftime('%m%d-%H%M%S')}_{_lib_slug(res)}"
            gal_dir = str(Path(BREAKDOWN_LIBRARY) / folder)
            build_gallery(res, gal_dir, log=log)
        except Exception as e:
            log(f"⚠ 图集抽取跳过：{e}")
            gal_dir = ""
        # 3) 入库（拿到 task_id，回填 res.task_id）
        try:
            from store import breakdown_store
            breakdown_store.save(res, gallery_dir=gal_dir, log=log)
        except Exception as e:
            log(f"✗ 拆解任务入库失败：{e}")

    def on_result(self, res):
        self._last_result = res
        self._last_task_id = int(getattr(res, "task_id", 0) or 0)
        self.b_report.setEnabled(self._last_task_id > 0)
        self.stage_box.setPlainText(self._render_stages(res))
        self.preview.setPlainText(self._render_preview(res))
        self._reload_task_list()
        if res.is_partial():
            return                     # 具体原因已由 _post_process 日志说明（未入库）
        cost = res.cost or {}
        self._append_log(
            f"✅ 拆解完成：{res.title}（分镜 {res.shot_count}，"
            f"豆包调用 {cost.get('vision_calls', 0)} 次 / {cost.get('tokens', 0)} token）。"
            + ("已入库。" if self._last_task_id else ""))

    def _render_stages(self, res):
        from video_text_tools.breakdown.models import STAGES
        lines = []
        for s in STAGES:
            v = res.stage_status.get(s)
            if v is None:
                lines.append(f"⏸ {s}：未执行")
            elif v == "ok":
                lines.append(f"✅ {s}")
            else:
                lines.append(f"❌ {s}：{str(v)[5:] if str(v).startswith('fail:') else v}")
        return "\n".join(lines)

    def _render_preview(self, res):
        parts = [f"标题：{res.title}", f"钩子：{res.overall.hook_desc}"
                 f"（{res.overall.hook_score}分）", "", "【分镜画面】"]
        for a in res.frame_analyses[:40]:
            bits = [b for b in (a.shot_size, a.camera, a.composition,
                                a.transition, a.on_screen_text, a.emotion) if b]
            parts.append(f"  {a.ts:.0f}s " + " / ".join(bits))
        parts += ["", "【3 类提示词】"]
        for s in res.segments[:40]:
            parts.append(f"  [{s.time_range}] 画面：{s.visual_prompt}")
            parts.append(f"           文案：{s.copy_prompt}")
            parts.append(f"           复刻：{s.shoot_prompt}")
        parts += ["", "【整体分析】",
                  f"  爆点因素：{res.overall.factors}",
                  f"  情绪曲线：{res.overall.emotion_curve}",
                  f"  内容公式：{res.overall.formula}",
                  f"  复刻蓝图：{res.overall.blueprint}"]
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 右栏：拆解任务列（封面+标题，双击进三屏详情；顶部可跳管理页）
    # ------------------------------------------------------------------
    def _build_task_col(self):
        col = QFrame()
        col.setObjectName("TaskCol")
        col.setFixedWidth(240)
        v = QVBoxLayout(col)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)
        head = QHBoxLayout()
        t = QLabel("拆解任务")
        t.setStyleSheet("font-size:13px; font-weight:700; color:#1F2329; background:transparent;")
        head.addWidget(t)
        head.addStretch(1)
        b_mgr = QPushButton("🗂 管理")
        b_mgr.setObjectName("GhostBtn")
        b_mgr.setToolTip("打开「拆解任务」管理页（卡片式浏览全部历史）")
        b_mgr.clicked.connect(self._goto_manage)
        head.addWidget(b_mgr)
        v.addLayout(head)
        self.task_list = QListWidget()
        self.task_list.setObjectName("TaskList")
        self.task_list.setViewMode(QListWidget.ViewMode.ListMode)
        self.task_list.setIconSize(QSize(54, 96))
        self.task_list.setSpacing(4)
        self.task_list.setUniformItemSizes(True)
        self.task_list.itemDoubleClicked.connect(self._on_task_activated)
        self.task_list.itemActivated.connect(self._on_task_activated)
        v.addWidget(self.task_list, 1)
        tip = QLabel("双击任一条 → 三屏联动详情")
        tip.setObjectName("PageTip")
        v.addWidget(tip)
        self._reload_task_list()
        return col

    def _reload_task_list(self):
        """重读任务库填右列（最新在前），保留当前选中。"""
        try:
            from store import breakdown_store
            rows = breakdown_store.list_tasks(limit=40, only_ok=True)
        except Exception as e:
            self.task_list.clear()
            self.task_list.addItem(f"⚠ 任务库读取失败：{e}")
            return
        self.task_list.clear()
        for r in rows:
            it = QListWidgetItem()
            cover = r.get("cover") or ""
            if cover and Path(cover).exists():
                it.setIcon(QIcon(QPixmap(cover).scaled(
                    54, 96, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)))
            mark = " ⚠" if r.get("status") == "partial" else ""
            it.setText(f"{r.get('title') or '（无标题）'}{mark}")
            it.setToolTip(f"{r.get('created_at','')}  分镜 {r.get('shot_count',0)}{mark}")
            it.setData(Qt.ItemDataRole.UserRole, int(r.get("id") or 0))
            self.task_list.addItem(it)

    def _on_task_activated(self, item):
        tid = int(item.data(Qt.ItemDataRole.UserRole) or 0) if item else 0
        if tid:
            from gui.dialogs_breakdown_detail import open_breakdown_detail
            open_breakdown_detail(self.window() or self, tid)

    def _open_detail(self):
        tid = int(getattr(self, "_last_task_id", 0) or 0)
        if tid:
            from gui.dialogs_breakdown_detail import open_breakdown_detail
            open_breakdown_detail(self.window() or self, tid)

    def _goto_manage(self):
        """跳主窗口的「拆解任务」页（没开管理页时退回打开任务库目录）。"""
        w = self.window()
        page = getattr(w, "page_breakdown", None)
        if page is not None and hasattr(w, "pages"):
            w.pages.setCurrentWidget(page)
            if hasattr(w, "_sync_nav_to_page"):
                w._sync_nav_to_page(page)
            return
        try:
            from core.config import BREAKDOWN_LIBRARY
            Path(BREAKDOWN_LIBRARY).mkdir(parents=True, exist_ok=True)
            open_path(BREAKDOWN_LIBRARY)
        except Exception:
            pass


# 任务库目录名净化：取视频名/标题，剔非法字符，保底“拆解”。
_ILLEGAL = '\\/:*?"<>|'


def _lib_slug(res):
    stem = ""
    vp = getattr(res, "video_path", "") or ""
    if vp:
        stem = Path(vp).stem
    if not stem:
        stem = (getattr(res, "title", "") or "").strip()
    cleaned = "".join("_" if c in _ILLEGAL else c for c in stem).strip(" ._")
    return (cleaned or "拆解")[:40]


_OK_QSS = "font-size:12px; color:#00A870; background:transparent;"
_WARN_QSS = "font-size:12px; color:#D83931; background:transparent;"
