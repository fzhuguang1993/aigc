"""
gui/pages_tasks.py —— 任务中心
分页表格 · 图形进度条 · 视频双击预览 · 右键打开位置 · 悬停预览浮层 ·
搜索/替换/日期筛选 · 扫描新任务 · 导入导出（含模板）
"""
from pathlib import Path
import time

from PySide6.QtCore import QThread, Signal, Qt, QDate, QPoint, QTimer
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QShortcut, QKeySequence
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
                               QMenu, QMessageBox, QFileDialog, QAbstractItemView,
                               QLineEdit, QDateEdit, QCheckBox, QInputDialog, QFrame)

from registry.manager import REG, ACCOUNTS, BatchBalancer, is_active
from core.config import DEFAULT_STEPS, SUBMIT_PACING, BALANCE_RESCAN_EVERY
from store import task_store, product_store, app_state
from utils.desktop_utils import reveal_in_folder
from workers.submit import (do_submit, cancel_one, SubmitOptions,
                            format_batch_distribution)
from gui.dialogs import (TaskDialog, FindReplaceDialog, ForceRerunDialog,
                         ScriptBindDialog)
from gui.delegates import ProgressDelegate, ProgressRole
from gui.formatting import secs
from gui.widgets import VideoPlayerDialog, HoverPreview, Toast
from gui.header import page_header
from gui.tablekit import (FieldManagerDialog, apply_field_layout, enable_drag_with_lock,
                          SecsItem)

STATUS_COLORS = {"completed": "#00A870", "failed": "#F54A45", "error": "#F54A45",
                 "cancelled": "#8F959E", "submitted": "#3370FF", "queued": "#3370FF",
                 "starting": "#FF8D19", "cancelling": "#FF8D19"}
RETRYABLE = {"failed", "error", "cancelled"}
# 状态图标（配颜色一起用，色弱/远看也能辨）：前缀到状态文字前
STATUS_ICONS = {"completed": "✓ ", "succeeded": "✓ ", "failed": "✕ ", "error": "✕ ",
                "timeout": "✕ ", "cancelled": "⊘ ", "canceled": "⊘ ",
                "running": "⏳ ", "starting": "⏳ ", "cancelling": "⏳ ",
                "submitted": "⏩ ", "queued": "⏩ "}
# 列表字段全量展示（含以前的隐藏技术字段），“字段管理”里逐个可勾可拖：
# 任务一多，使用者需要自己能控制看哪几列、列序怎么排
DATA_HEADERS = ["任务ID", "编号", "品名", "备注", "脚本", "提示词", "状态", "时长",
                "生成用时", "排队", "账号", "运行", "成功", "取消", "口播文案", "分镜数",
                "更新时间", "输出文件", "job_id", "URL"]
HEADERS = [""] + DATA_HEADERS          # 第 0 列：勾选框
CHECK_COL = 0
COL_PID, COL_NUM, COL_PRODUCT, COL_REMARK, COL_SCRIPT, COL_PROMPT, COL_STATUS, \
    COL_DUR, COL_GEN, COL_QUEUE, COL_ACCOUNT, COL_RUNS, COL_OK, COL_CANCEL, COL_VOICE, \
    COL_STORY, COL_UPDATED, COL_OUT, COL_JOB, COL_URL = range(1, len(DATA_HEADERS) + 1)
NUM_COLS = (COL_PID, COL_RUNS, COL_OK, COL_CANCEL, COL_DUR, COL_GEN, COL_QUEUE, COL_STORY)
# 两个时长列走 SecsItem：显示「4分58秒」而排序按秒数（按文本排会乱）
SECS_COLS = (COL_GEN, COL_QUEUE)
STRETCH_COLS = (COL_PROMPT, COL_OUT)
# 单击/空格弹出全文预览的三列（提示词/脚本/口播文案），值即 df 列名
PREVIEW_COLS = {COL_PROMPT: "提示词", COL_SCRIPT: "脚本", COL_VOICE: "口播文案"}
BAR_STATUS = ("running", "starting")   # 用图形进度条显示的状态
LEFT_COLS = {COL_PROMPT, COL_SCRIPT, COL_VOICE, COL_OUT, COL_REMARK,
             COL_JOB, COL_URL}   # 这几列左对齐，其余居中
# 技术字段：默认收起（不删列，去“字段管理”能勾出来），新手看到的还是原来的宽度
DEFAULT_HIDDEN = (COL_JOB, COL_URL)
# 状态下拉：“全部”+ 按使用者能读懂的口径分组（不暴露云端原始状态词）
STATUS_FILTERS = [("全部", None),
                  ("待执行", ("",)),
                  ("排队/提交", ("submitted", "queued")),
                  ("进行中", ("running", "starting", "cancelling")),
                  ("完成", ("completed", "succeeded")),
                  ("失败", ("failed", "error", "timeout")),
                  ("已取消", ("cancelled",))]
NO_TAG = "（无备注）"        # 备注下拉的虚拟选项：一筛就只看没标的
BLANK = "（未填）"


def _dur_tip(row, kind):
    """「生成用时 / 排队」两列的 tooltip：先说清这一列是什么，再把三段数字对上

    kind 就两个取值："gen" / "queue"。早期没存云端时间戳的记录显示的是
    含排队的总用时，不标出来就会被当成纯生成耗时拿去比。"""
    try:
        gen = int(row["生成用时"] or 0)
        q = int(row["排队用时"] or 0)
        unsplit = bool(row["用时含排队"])
    except (TypeError, ValueError, KeyError):
        return ""
    if not gen and not q:
        return "还没执行过：没得可统计"
    total = f"排队 {secs(q)} + 生成 {secs(gen)} = 提交到完成 {secs(gen + q)}"
    if unsplit:
        return (f"早期记录没拆出排队：这个数是提交到完成一共 {secs(gen)}"
                if kind == "gen" else "早期记录没拆出排队（可用回填脚本补）")
    if kind == "gen":
        return f"云端开始跑 → 出片：{secs(gen)}\n{total}"
    return f"提交 → 云端开始跑：{secs(q)}（单并发线路批量提交时这段最长）\n{total}"


def decide_rerun(items, force, inflight, choice):
    """把重跑弹窗的答案落到「提交清单 + 待取消清单」上（纯函数，便于回归）

    items    已经确定要提交的（新任务 + 改过提示词的迭代执行）
    force    需确认才能重跑的（已完成、提示词没改）
    inflight {任务ID: [还在跑的执行]}，由 REG 算出
    choice   None = 点「跳过」；否则 {"repeat": n, "cancel_first": bool}

    「跳过」不只跳过 force：正在跑的那几条也要退出本批——它们就是用户不想再
    加一份的那些，否则“跳过”了还会多出一个成品。返回 (items, to_cancel)。"""
    if choice is None:
        drop = {it[0] for it in force} | set(inflight)
        return [it for it in items if it[0] not in drop], []
    items = items + [it for it in force for _ in range(choice["repeat"])]
    to_cancel = ([at for ats in inflight.values() for at in ats]
                 if choice.get("cancel_first") else [])
    return items, to_cancel


def inflight_execs(tids):
    """{任务ID: [还在排队/还在跑的执行]}——只看本机本次会话提交过的

    只认 `REG` 不认数据库里的状态：重启后 REG 是空的，而库里的 running
    可能是上一辈子留下的（轮询线程只管本次会话提交的 job），拿它当
    “还在跑”会误报，对着一个早就跑完的 job 发取消请求。
    代价：软件重启后重跑同一任务不会提示“先取消”，那份旧 job 也已脱离跟踪，
    要去「📡 线路负载」页看云端真实队列。"""
    out = {}
    for tid in tids:
        ats = [at for at in REG.get_all_by_row(tid) if is_active(at["status"])]
        if ats:
            out[tid] = ats
    return out


def cancel_inflight(to_cancel, log=None):
    """提交前先把在跑的执行取消掉，返回 (成功数, 失败数)；log 回调用于播报

    抽出来写成函数而不是写死在 `SubmitWorker.run` 里：取消失败不阻断提交（发
    不出去就新旧两份同时在跑，得让人看见），但不能为了测它就得去跑一个线程。"""
    if not to_cancel:
        return 0, 0
    if log:
        log(f"⏹ 先取消 {len(to_cancel)} 个还在跑的执行…")
    ok = 0
    for at in to_cancel:
        if cancel_one(at):
            ok += 1
    if log:
        log(f"⏹ 已发送 {ok}/{len(to_cancel)} 个取消请求"
            + ("，失败的见日志" if ok < len(to_cancel) else "")
            + "；云端释放额度要几秒，单并发线路可能要等一下空位")
    return ok, len(to_cancel) - ok


class SubmitWorker(QThread):
    """后台提交线程，避免上传参考图时卡住界面"""
    log_msg = Signal(str)
    all_done = Signal(list)      # 参数：[(任务ID, 失败原因), …]，全部成功时为空列表

    def __init__(self, items, options, to_cancel=None):
        super().__init__()
        self.items = items
        self.options = options   # 提交瞬间的选项快照，避免全局态
        # 提交前先取消的旧执行（「⏹ 取消并重跑」选出来的）：取消也是 HTTP 请求，
        # 放本线程做而不是 _run 里，否则几十条选中任务能把界面卡住
        self.to_cancel = to_cancel or []
        self.distribution = ""   # 本批实际落线汇总，跑完后给 _on_submit_done 拼进提示

    def run(self):
        failed = []
        landed = []              # 每条提交成功的任务落在哪条线（换线重试后算最终那条）
        if self.to_cancel:
            # 先取消再提交：取消也是 HTTP 请求，放本线程做才不卡界面
            cancel_inflight(self.to_cancel, self.log_msg.emit)
        # 多条一起提交才启用注水分配器：按全局快照+本批投影均衡选线、绕开在途闸门；
        # 单条仍走原有闸门逻辑（balancer=None）
        balancer = (BatchBalancer(len(self.items), rescan_every=BALANCE_RESCAN_EVERY)
                    if len(self.items) > 1 else None)
        for i, (tid, product, prompt) in enumerate(self.items):
            if i:
                # 批量推送小间隔：排队模型下不再靠大抖动错峰，仅防连发
                time.sleep(SUBMIT_PACING)
            jid, err, acc = do_submit(tid, product, prompt, self.options,
                                      balancer=balancer)
            if jid:
                landed.append(acc)        # 只统计真正提交上去的，失败的另有弹窗留痕
                self.log_msg.emit(f"✓ 任务{tid} 已提交 [{acc}]")
            else:
                failed.append((tid, err))
                self.log_msg.emit(f"✗ 任务{tid} 提交失败: {err}")
        # 收尾报一句本批分布：均衡有没有失效，不能只靠事后翻日志（同事反馈现场）
        self.distribution = format_batch_distribution(landed, len(ACCOUNTS))
        if self.distribution:
            self.log_msg.emit(self.distribution)
        self.all_done.emit(failed)


class _GuideBubble(QWidget):
    """任务中心首次上手的引导气泡：无边框小卡片，指向下一步操作，可跳过"""
    advanced = Signal()
    closed = Signal()

    def __init__(self, parent, text, idx, total):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("GuideCard")
        card.setStyleSheet("QFrame#GuideCard{background:#FFFFFF;border:1px solid #3370FF;"
                           "border-radius:10px;}")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(8)
        head = QLabel(f"快速上手 · {idx}/{total}")
        head.setStyleSheet("color:#3370FF;font-weight:bold;")
        cl.addWidget(head)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setMinimumWidth(300)
        body.setMaximumWidth(340)
        cl.addWidget(body)
        row = QHBoxLayout()
        row.addStretch(1)
        b_skip = QPushButton("跳过")
        b_skip.setObjectName("GhostBtn")
        b_next = QPushButton("下一步" if idx < total else "开始使用")
        b_skip.clicked.connect(self.closed.emit)
        b_next.clicked.connect(self.advanced.emit)
        row.addWidget(b_skip)
        row.addWidget(b_next)
        cl.addLayout(row)
        lay.addWidget(card)


class TasksPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(6)

        lay.addWidget(page_header("任务中心", "单击输出列播放视频 · 单击/空格弹出全文 · 任意键关闭 · 右键更多", icon="📋"))

        # ---------- 工具栏 ----------
        bar = QHBoxLayout()
        b_new = QPushButton("＋ 新建任务")
        b_run = QPushButton("▶ 执行选中")
        b_runall = QPushButton("⏩ 执行全部待办")
        b_scan = QPushButton("🔍 扫描新任务")
        b_cancel = QPushButton("⏹ 取消选中")
        b_del = QPushButton("🗑 删除")
        b_io = QPushButton("📁 导入/导出")
        # 注意：QPushButton.setMenu 配合全局样式表会导致点击无反应，改为手动弹出菜单
        self._io_menu = self._build_io_menu()
        b_io.clicked.connect(
            lambda: self._io_menu.exec(b_io.mapToGlobal(QPoint(0, b_io.height()))))
        b_fields = QPushButton("⚟ 字段管理")
        b_fields.setObjectName("GhostBtn")
        b_fields.setToolTip("控制显示哪些列，拖动表头可直接调整列顺序")
        for b in (b_cancel, b_del, b_io, b_fields):
            b.setObjectName("GhostBtn")
        for b in (b_new, b_run, b_runall, b_scan, b_cancel, b_del, b_io, b_fields):
            bar.addWidget(b)
        # 留存几个引导/快捷键目标（首次上手气泡、顶部待办数量都要用）
        self.b_new, self.b_run, self.b_io = b_new, b_run, b_io
        self.b_runall, self.b_fields = b_runall, b_fields
        bar.addStretch(1)
        bar.addWidget(QLabel("时长"))
        self.cb_duration = QComboBox()
        self.cb_duration.addItems([f"{i}秒" for i in range(2, 16)])   # 2-15 秒可选
        self.cb_duration.setCurrentIndex(3)                           # 默认 5 秒
        bar.addWidget(self.cb_duration)
        bar.addWidget(QLabel("步数"))
        self.cb_steps = QComboBox()                                   # AI 生成步数（1-50）
        self.cb_steps.addItems([str(i) for i in range(1, 51)])
        self.cb_steps.setCurrentIndex(DEFAULT_STEPS - 1)
        self.cb_steps.setToolTip("生成步数：越大细节越好、耗时更长（1-50）\n"
                                 "作为 parameters.inference_steps 传给云端，下次启动沿用本次值")
        bar.addWidget(self.cb_steps)
        bar.addWidget(QLabel("KOL"))
        self.cb_kol = QComboBox()
        self.cb_kol.addItems(["不使用"] + product_store.kol_names())
        bar.addWidget(self.cb_kol)
        # 常驻“本次默认参数”：避免“改了时长/步数没生效”的经典困惑
        bar.addSpacing(8)
        self.lbl_params = QLabel("")
        self.lbl_params.setObjectName("PageTip")
        self.lbl_params.setToolTip(
            "工具栏当前值＝下一次「执行选中/执行全部待办」会用到的参数；\n"
            "表格里的「时长」列只是上次提交的留痕，改不动也不起作用")
        bar.addWidget(self.lbl_params)
        lay.addLayout(bar)
        self._restore_exec_params()          # 沿用上次用过的时长/步数/KOL

        # ---------- 筛选栏 ----------
        fbar = QHBoxLayout()
        fbar.addWidget(QLabel("🔍"))
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索品名/编号/脚本/提示词/口播文案，回车过滤")
        self.ed_search.setFixedWidth(240)
        self.ed_search.returnPressed.connect(lambda: (self._goto_first_page(), self.refresh()))
        fbar.addWidget(self.ed_search)
        b_replace = QPushButton("🔁 查找/替换")
        b_replace.setObjectName("GhostBtn")
        b_replace.setToolTip("默认只查找：按关键词挑一批任务勾选置顶；点「替换 »」展开后可批量改写提示词")
        b_replace.clicked.connect(self._find_replace)
        fbar.addWidget(b_replace)
        self.b_replace_btn = b_replace
        fbar.addSpacing(12)
        # 下拉筛选：任务上百条后光靠搜索框拦不住，要能按品名/状态/备注直接分档
        self.cb_f_product = QComboBox()
        self.cb_f_status = QComboBox()
        self.cb_f_remark = QComboBox()
        for lbl, cb in (("品名", self.cb_f_product),
                        ("状态", self.cb_f_status),
                        ("备注", self.cb_f_remark)):
            cb.setMinimumContentsLength(8)
            cb.setToolTip(f"按{lbl}筛选任务（与搜索框/日期是“并且”关系）")
            cb.currentIndexChanged.connect(lambda *_a: (self._goto_first_page(),
                                                        self.refresh()))
            fbar.addWidget(QLabel(lbl))
            fbar.addWidget(cb)
        fbar.addSpacing(16)
        self.cb_date = QCheckBox("按日期筛选")
        self.cb_date.toggled.connect(self._toggle_date_filter)
        fbar.addWidget(self.cb_date)
        self.de_start = QDateEdit(QDate.currentDate().addDays(-7))
        self.de_end = QDateEdit(QDate.currentDate())
        for de in (self.de_start, self.de_end):
            de.setCalendarPopup(True)
            de.setDisplayFormat("yyyy-MM-dd")
            de.setEnabled(False)
            de.dateChanged.connect(self.refresh)
        fbar.addWidget(self.de_start)
        fbar.addWidget(QLabel("至"))
        fbar.addWidget(self.de_end)
        b_clear = QPushButton("✕ 清除筛选")
        b_clear.setObjectName("GhostBtn")
        b_clear.setToolTip("清空搜索/日期条件，并取消「置顶聚集」（回到默认的最新在前）")
        b_clear.clicked.connect(self._clear_filters)
        fbar.addWidget(b_clear)
        fbar.addStretch(1)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("PageTip")
        fbar.addWidget(self.lbl_count)
        lay.addLayout(fbar)

        # ---------- 快捷筛选标签（A1）：一键设好状态/日期常用组合 ----------
        chbar = QHBoxLayout()
        chbar.setSpacing(6)
        chbar.addWidget(QLabel("快捷筛选："))
        for label, kind in (("待执行", "todo"), ("进行中", "running"),
                            ("失败/可重试", "retry"), ("今日更新", "today"),
                            ("全部放开", "all")):
            b = QPushButton(label)
            b.setObjectName("ChipBtn")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c=False, k=kind: self._quick_filter(k))
            chbar.addWidget(b)
        chbar.addStretch(1)
        lay.addLayout(chbar)

        # ---------- 表格 ----------
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # 不再 NoSelection：启用 Qt 自带的鼠标按住拖框选（橡皮筋），框到的行
        # 同步勾上勾选框（见 _on_rubber_band），与“跨页勾选”共用同一集合
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setMouseTracking(True)
        # 框选靠「按下→拖动→松开」自己识别（见 eventFilter）：不用
        # itemSelectionChanged，否则点一下勾选框取消时被连带选中的行会反手又勾上
        self.table.setItemDelegate(ProgressDelegate(self.table))
        # 勾选列：固定宽度、不参与排序
        self.table.setColumnWidth(CHECK_COL, 36)
        self.table.horizontalHeader().setSortIndicatorShown(False)
        enable_drag_with_lock(self.table, lock_count=1)
        for c in STRETCH_COLS:
            self.table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch)
        widths = {COL_PID: 60, COL_NUM: 50, COL_PRODUCT: 110, COL_REMARK: 110,
                  COL_SCRIPT: 90, COL_STATUS: 130, COL_DUR: 52, COL_GEN: 80,
                  COL_QUEUE: 62,
                  COL_ACCOUNT: 70, COL_RUNS: 50, COL_OK: 50, COL_CANCEL: 50,
                  COL_VOICE: 90, COL_STORY: 56, COL_UPDATED: 95,
                  COL_JOB: 110, COL_URL: 160}
        for c, w in widths.items():
            self.table.setColumnWidth(c, w)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellEntered.connect(self._on_cell_entered)
        self.table.cellClicked.connect(self._on_cell_click)
        self.table.cellDoubleClicked.connect(self._on_cell_double)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self.table.viewport().installEventFilter(self)
        self.table.installEventFilter(self)      # 空格键弹全文预览用
        lay.addWidget(self.table)
        self._build_empty_state()        # 空表时盖在表格上的引导层（导入/新建 或 清除筛选）

        # ---------- 分页栏 ----------
        pbar = QHBoxLayout()
        self.lbl_sel = QLabel("已选 0 个")
        self.lbl_sel.setObjectName("PageTip")
        pbar.addWidget(self.lbl_sel)
        b_clearsel = QPushButton("清除选择")
        b_clearsel.setObjectName("GhostBtn")
        b_clearsel.clicked.connect(self._clear_selection)
        pbar.addWidget(b_clearsel)
        self.lbl_run = QLabel("")           # E2：进行中/排队总览，有活才显示
        self.lbl_run.setObjectName("InlineTip")
        pbar.addWidget(self.lbl_run)
        pbar.addStretch(1)
        self.b_prev = QPushButton("‹ 上一页")
        self.b_next = QPushButton("下一页 ›")
        self.b_prev.setObjectName("GhostBtn")
        self.b_next.setObjectName("GhostBtn")
        self.lbl_page = QLabel("第 1 / 1 页")
        self.cb_psize = QComboBox()
        self.cb_psize.addItems(["20 条/页", "50 条/页", "100 条/页", "全部"])
        self.cb_psize.setCurrentIndex(0)
        self.b_prev.clicked.connect(lambda: self._page_step(-1))
        self.b_next.clicked.connect(lambda: self._page_step(1))
        self.cb_psize.currentIndexChanged.connect(lambda: (self._goto_first_page(), self.refresh()))
        pbar.addWidget(self.b_prev)
        pbar.addWidget(self.lbl_page)
        pbar.addWidget(self.b_next)
        pbar.addSpacing(12)
        pbar.addWidget(QLabel("每页"))
        pbar.addWidget(self.cb_psize)
        lay.addLayout(pbar)

        self.lbl_tip = QLabel("")
        self.lbl_tip.setObjectName("InlineTip")
        lay.addWidget(self.lbl_tip)

        # ---------- 状态 ----------
        self.worker = None
        self._filtered = []        # [(tid, row), ...] 筛选后的全量
        self._selected_ids = set() # 跨页保留的选中集合
        self._drag_anchor = None   # 鼠标框选的按下起点（区分单击与拖选）
        self._gather_ids = set()   # 上一批需要置顶聚集的任务（替换命中/新导入）
        self._user_sort_col = None # 用户点过的排序列；None=未排序，保留聚集顺序
        self._page = 1
        self._syncing = False      # 恢复选中时屏蔽 selectionChanged
        self._player = None        # 视频窗口引用，防 GC
        self._tour_bubble = None   # 首次上手引导气泡引用，防 GC
        self._tour_started = False # 引导只在首次显示时跑一遍
        self.hover = HoverPreview()  # 全文预览浮层（单击/空格唤起，任意键关闭）
        self._hover_cell = None      # 鼠标最后所在的 (row, col)，空格键弹整列用
        self._toasts = []            # 右下角浮层引用，防 GC
        self._failed_ids = set()     # 本次提交失败的任务：标红+置顶，便于改后重跑
        self._prev_active = 0        # 上次刷新的在途数，用于“跑完”边沿检测
        self._expect_done = False    # 提交后等待完成通知

        b_new.clicked.connect(self._new_task)
        b_del.clicked.connect(self._del_selected)
        b_cancel.clicked.connect(self._cancel_selected)
        b_run.clicked.connect(lambda: self._run(True))
        b_runall.clicked.connect(lambda: self._run(False))
        b_scan.clicked.connect(self._scan_new)
        b_fields.clicked.connect(self._manage_fields)
        self._restore_field_layout()      # 沿用上次列宽/列序/显隐
        self._setup_shortcuts()           # A4：常用键盘快捷键
        self._arm_first_hints()           # C2：关键按钮首次悬停多讲一句
        self._update_params_label()       # E1：初始化“本次默认参数”显示

    # ============ 导入/导出菜单 ============
    def _build_io_menu(self):
        m = QMenu(self)
        m.addAction("📥 从 Excel 导入任务", self._import_excel)
        m.addAction("📄 下载导入模板", self._download_template)
        m.addSeparator()
        m.addAction("导出任务为 Excel", lambda: self._do_export("excel"))
        m.addAction("导出任务为 CSV", lambda: self._do_export("csv"))
        m.addAction("导出任务为 JSON", lambda: self._do_export("json"))
        m.addSeparator()
        m.addAction("导出执行记录", self._export_runs)
        return m

    def _download_template(self):
        try:
            path = task_store.write_import_template()
            if QMessageBox.question(self, "模板已生成",
                                    f"{path}\n\n是否打开所在文件夹？") \
                    == QMessageBox.StandardButton.Yes:
                reveal_in_folder(path)
        except Exception as e:
            QMessageBox.critical(self, "生成失败", str(e))

    def _export_runs(self):
        try:
            self.lbl_tip.setText(f"已导出执行记录：{task_store.export_runs('excel')}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    # ============ 扫描新任务 ============
    def _scan_new(self):
        news = [tid for tid, _ in task_store.scan_new_rows()]
        if not news:
            self.lbl_tip.setText("扫描完成：没有新任务（新任务=有提示词且从未运行过）")
            return
        if QMessageBox.question(
                self, "扫描到新任务",
                f"发现 {len(news)} 个新任务（有提示词、未运行过）。\n是否帮你勾选它们？") \
                != QMessageBox.StandardButton.Yes:
            return
        # 新任务可能零散分布在多页：只勾这批并置顶聚集（旧勾选不混进来，
        # 避免“执行选中”误跑到用户之前随手勾的任务），分页档位自动抬升
        self._selected_ids = set()
        n = self._gather_matches(news)
        self.lbl_tip.setText(f"已勾选并置顶 {n} 个新任务，可直接点「执行选中」")

    # ============ 分页 ============
    def _page_size(self):
        return [20, 50, 100, 10 ** 9][self.cb_psize.currentIndex()]

    _SIZES = (20, 50, 100)

    def _grow_page_size(self, need):
        """把每页显示档位抬到能装下 need 条的最小档；超过 100 条直接切「全部」"""
        target = 3 if need > self._SIZES[-1] else next(
            (i for i, s in enumerate(self._SIZES) if s >= need), 3)
        if self.cb_psize.currentIndex() < target:
            self.cb_psize.setCurrentIndex(target)

    def _page_count(self):
        import math
        return max(1, math.ceil(len(self._filtered) / self._page_size()))

    def _goto_first_page(self):
        self._page = 1

    def _page_step(self, d):
        self._page = max(1, min(self._page + d, self._page_count()))
        self.hover.hide()
        self.refresh()

    # ============ 筛选 ============
    def _toggle_date_filter(self, on):
        self.de_start.setEnabled(on)
        self.de_end.setEnabled(on)
        self._goto_first_page()
        self.refresh()

    def _clear_filters(self):
        self._gather_ids = set()
        self._user_sort_col = None
        self.ed_search.clear()
        self.cb_date.setChecked(False)   # 触发 toggled→refresh
        for cb in (self.cb_f_product, self.cb_f_status, self.cb_f_remark):
            cb.blockSignals(True)        # 不逐个触发 refresh
            cb.setCurrentIndex(0)
            cb.blockSignals(False)
        self._goto_first_page()
        self.refresh()

    def _match_filter(self, row):
        return (self._match_kw(row) and self._match_choice(row)
                and self._match_date(row))

    def _sync_filter_choices(self):
        """筛选候选跟着数据走：新建/导入后不必重启就能筛到新产品。

        重建期间必屏蔽信号：否则 addItem 会再触发一次 refresh（自调递归）。"""
        ch = task_store.filter_choices()
        for cb, items in ((self.cb_f_product,
                           [("", "全部"), (BLANK, BLANK)] + [(p, p) for p in ch["products"]]),
                          (self.cb_f_remark,
                           [("", "全部"), (NO_TAG, NO_TAG)] + [(t, t) for t in ch["remarks"]])):
            keep = cb.currentData()
            cb.blockSignals(True)
            cb.clear()
            for data, label in items:
                cb.addItem(label, data)
            idx = cb.findData(keep)
            cb.setCurrentIndex(idx if idx >= 0 else 0)
            cb.blockSignals(False)
        if self.cb_f_status.count() != len(STATUS_FILTERS):   # 固定档位，只填一次
            self.cb_f_status.blockSignals(True)
            self.cb_f_status.clear()
            for label, group in STATUS_FILTERS:
                self.cb_f_status.addItem(label, group)
            self.cb_f_status.blockSignals(False)

    def _match_choice(self, row):
        """品名/状态/备注三个下拉：选了具体值就必须命中（选“全部”＝不限）"""
        for cb, col, blank in ((self.cb_f_product, "品名", BLANK),
                               (self.cb_f_remark, "备注", NO_TAG)):
            want = cb.currentData()
            if not want:
                continue
            got = str(row[col]).strip()
            if want == blank:
                if got:
                    return False
            elif got != want:
                return False
        group = self.cb_f_status.currentData()
        if group and str(row["状态"]).strip() not in group:
            return False
        return True

    def _match_kw(self, row):
        """搜索关键词命中（品名/编号/脚本/提示词/口播文案/备注）；未填关键词视为命中"""
        kw = self.ed_search.text().strip().lower()
        if not kw:
            return True
        hay = " ".join([str(row["品名"]), str(row["编号"]), str(row["脚本"]),
                        str(row["提示词"]), str(row["口播文案"]),
                        str(row["备注"])]).lower()
        return kw in hay

    def _match_date(self, row):
        if self.cb_date.isChecked():
            d = str(row["更新时间"])[:10]
            if not d:
                return False
            s = self.de_start.date().toString("yyyy-MM-dd")
            e = self.de_end.date().toString("yyyy-MM-dd")
            if not (s <= d <= e):
                return False
        return True

    def _gather_matches(self, tids):
        """把一批任务挑出来：全部勾选 + 置顶聚集 + 分页档位抬到装得下。

        搜索替换命中、扫描新任务、Excel 导入（上传旧提示词）共用——任务
        可能零散分布在各页，必须聚成一排且处于勾选态，才能直接「执行选中」。
        只抬档位不降级（尊重用户选的档位）；点「✖ 清除筛选」或手点列头则回到默认序。"""
        tids = {int(t) for t in tids if t is not None}
        if not tids:
            return 0
        self._selected_ids |= tids
        self._gather_ids = tids
        self._user_sort_col = None    # 聚集优先于上一次表头排序，否则置顶会被打回
        self._grow_page_size(len(tids))
        self._goto_first_page()
        self.refresh()
        return len(tids)

    # ============ 刷新（筛选→分页→填充）============
    def refresh(self):
        if not self.hover.pinned:            # 定时刷新不打扰正在阅读的钉住浮层
            self.hover.hide()
        # KOL 列表跟随产品中心变化（保留当前选择）
        cur_kol = self.cb_kol.currentText()
        self.cb_kol.blockSignals(True)
        self.cb_kol.clear()
        self.cb_kol.addItems(["不使用"] + product_store.kol_names())
        idx = self.cb_kol.findText(cur_kol)
        self.cb_kol.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_kol.blockSignals(False)
        df = task_store.list_tasks_df()
        self._sync_filter_choices()
        self._filtered = [(int(row["_id"]), row)
                          for _, row in df.iterrows() if self._match_filter(row)]
        # 默认「最新在前」：按更新时间倒序（同秒再按任务ID倒序）。商家日常关心的是
        # 刚跑过/刚导入的任务，不该从最早编号翻起；点列头排序或清除筛选后会回到这个默认序
        self._filtered.sort(key=lambda x: (str(x[1]["更新时间"]), x[0]), reverse=True)
        # 刚挑出的那一批置顶聚集（稳定排序，其余保持原序）——否则新导入的
        # 任务排在最后，与已排队的旧任务混在一起根本找不着
        if self._gather_ids:
            self._filtered.sort(key=lambda x: x[0] not in self._gather_ids)
        self._page = max(1, min(self._page, self._page_count()))
        size = self._page_size()
        start = (self._page - 1) * size
        page_rows = self._filtered[start:start + size]

        actives = REG.active()
        active_map = {}
        for t in actives:                 # 同一任务可能并发多个 job（强制重跑/抽卡）
            active_map.setdefault(t["row_idx"], []).append(t)
        header = self.table.horizontalHeader()
        # 表头排序只在用户真点过列头时生效：否则 setSortingEnabled(True) 会按默认
        # 指示列（任务ID升序）重排，刚置顶聚集的那批（ID最大）会被换回页底
        sort_col = self._user_sort_col
        sort_order = header.sortIndicatorOrder()
        if sort_col is None:
            header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        elif sort_col == CHECK_COL:
            sort_col = COL_PID
        self.table.setSortingEnabled(False)
        self._syncing = True
        self.table.setRowCount(0)
        has_bar = False
        for tid, row in page_rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            status = str(row["状态"]).strip()
            ats = active_map.get(tid)
            out_path = str(row["输出"]).split(";")[0].strip()
            for c in range(len(HEADERS)):
                item = self._make_item(tid, row, r, c, ats, status, out_path)
                self.table.setItem(r, c, item)
            if ats and any(t["status"] in BAR_STATUS for t in ats):
                self.table.setRowHeight(r, 34)
                has_bar = True
        self.table.setSortingEnabled(True)
        if sort_col is not None and sort_col >= 0:
            self.table.sortItems(sort_col, sort_order)
        self._syncing = False
        # 有进度条行才开移动画定时器，平时完全静默
        self.table.itemDelegate().set_anim_enabled(has_bar)

        self.lbl_count.setText(f"显示 {len(self._filtered)}/{len(df)} 条")
        self._update_empty(len(df), len(self._filtered))   # 无任务/无命中时给引导，别让界面一片空白
        self.lbl_page.setText(f"第 {self._page} / {self._page_count()} 页")
        self.b_prev.setEnabled(self._page > 1)
        self.b_next.setEnabled(self._page < self._page_count())
        self._update_sel_label()
        self._update_run_summary(actives)

    def _make_item(self, tid, row, r, c, ats, status, out_path):
        item = SecsItem(0) if c in SECS_COLS else QTableWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, tid)     # 行→任务ID，排序后仍可靠
        pct = None
        if c == CHECK_COL:
            item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            item.setCheckState(Qt.CheckState.Checked if tid in self._selected_ids
                               else Qt.CheckState.Unchecked)
            item.setToolTip("勾选后可跨页保留，支持批量执行/删除；"
                            "按住鼠标在表上拖出框，框到的行会一次全部勾上")
        elif c == COL_PID:
            item.setData(Qt.ItemDataRole.DisplayRole, tid)
        elif c == COL_NUM:
            item.setText(str(row["编号"]))
        elif c == COL_PRODUCT:
            item.setText(str(row["品名"]))
        elif c == COL_REMARK:
            rk = str(row["备注"])
            item.setText(rk[:24])
            item.setToolTip((rk or "（双击可写备注，支持先勾选再批量写）")
                            if not rk or len(rk) <= 24 else rk)
        elif c == COL_JOB:
            item.setText(str(row["job_id"]))
            item.setToolTip(str(row["job_id"]))
        elif c == COL_URL:
            u = str(row["URL"])
            item.setText(u[:40])
            item.setToolTip(u)
        elif c == COL_SCRIPT:
            s = str(row["脚本"])
            item.setText(s[:30])
            item.setToolTip("")                          # 单击/空格看全文
        elif c == COL_PROMPT:
            p = str(row["提示词"])
            item.setText(p[:40])
            item.setToolTip("")                          # 用悬停浮层替代默认气泡
        elif c == COL_STATUS:
            show = status or "待执行"
            color_key = status
            if ats:
                if len(ats) == 1:
                    at0 = ats[0]
                    show, color_key = at0["status"], at0["status"]
                    if at0["status"] in BAR_STATUS:
                        pct = int(at0["progress"] or 0)
                        show = ""                        # 由 delegate 画进度条
                else:
                    n = len(ats)
                    color_key = "running"
                    show = f"执行中×{n}"
                    bars = [int(t["progress"] or 0) for t in ats
                            if t["status"] in BAR_STATUS]
                    if bars:
                        pct = min(bars)                  # 多路并发时以最慢一条代表整体
                        show = ""
                    item.setToolTip(f"{n} 个执行正在进行（抽卡/重跑），进度条显示最慢的一条")
            if show:
                show = STATUS_ICONS.get(color_key, "") + show
            item.setText(show)
            item.setForeground(QColor(STATUS_COLORS.get(color_key, "#8A94A6")))
            if tid in self._failed_ids:
                # 本次提交失败：整格标红置顶，提醒“这条要改提示词重跑”
                item.setBackground(QColor("#FFECE8"))
                item.setForeground(QColor("#F54A45"))
        elif c == COL_DUR:
            try:
                d = int(row["时长"] or 0)
            except (TypeError, ValueError):
                d = 0
            if d:
                item.setData(Qt.ItemDataRole.DisplayRole, d)   # 数值排序
            item.setText(f"{d}秒" if d else "—")
            item.setToolTip("本任务最后一次提交选的视频时长（执行后自动登记）")
        elif c == COL_GEN:
            try:
                d = int(row["生成用时"] or 0)
            except (TypeError, ValueError):
                d = 0
            item.set_secs(d)
            item.setToolTip(_dur_tip(row, "gen"))
        elif c == COL_QUEUE:
            try:
                d = int(row["排队用时"] or 0)
            except (TypeError, ValueError):
                d = 0
            item.set_secs(d)
            item.setToolTip(_dur_tip(row, "queue"))
        elif c == COL_ACCOUNT:
            item.setText(str(row["账号"]))
        elif c == COL_RUNS:
            item.setData(Qt.ItemDataRole.DisplayRole, int(row["运行次数"] or 0))
        elif c == COL_OK:
            item.setData(Qt.ItemDataRole.DisplayRole, int(row["成功次数"] or 0))
        elif c == COL_CANCEL:
            item.setData(Qt.ItemDataRole.DisplayRole, int(row["取消次数"] or 0))
        elif c == COL_VOICE:
            item.setText(str(row["口播文案"])[:30])
            item.setToolTip("")
        elif c == COL_STORY:
            try:
                n = int(row["分镜数"] or 0)
            except (TypeError, ValueError):
                n = 0
            if n:
                item.setData(Qt.ItemDataRole.DisplayRole, n)   # 数值排序
                item.setText(str(n))
            else:
                item.setText("")                          # 识别不出就留空
            item.setToolTip("从提示词自动识别（镜头/分镜编号），识别不出留空")
        elif c == COL_UPDATED:
            item.setText(str(row["更新时间"])[5:16])
        elif c == COL_OUT:
            item.setText(Path(out_path).name if out_path else "")
            item.setData(Qt.ItemDataRole.UserRole + 2, out_path)   # 完整路径
            item.setToolTip(out_path)
        if pct is not None:
            item.setData(ProgressRole, pct)
        if c in LEFT_COLS:
            item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        else:
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    # ============ 选中管理（勾选框，跨页保留） ============
    def _row_tid(self, r):
        item = self.table.item(r, CHECK_COL)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_item_changed(self, item):
        if self._syncing or item.column() != CHECK_COL:
            return
        tid = item.data(Qt.ItemDataRole.UserRole)
        if tid is None:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._selected_ids.add(tid)
        else:
            self._selected_ids.discard(tid)
        self._update_sel_label()

    def _on_rubber_band(self):
        """把当前选中的行勾上（只增不减，取消请点勾或「清除选择」）。

        由 eventFilter 在确认「真的拖动过鼠标」后调用，不挂 itemSelectionChanged：
        点勾选框取消勾选时该行也会被视为选中，会被反手又勾回去。
        完事清掉 Qt 高亮：用户真正依赖的是勾选框（跨页保留、驱动批量操作）。"""
        if self._syncing:
            return
        rows = {idx.row() for idx in self.table.selectedIndexes()}
        self._syncing = True
        for r in rows:
            it = self.table.item(r, CHECK_COL)
            if it is None or it.checkState() == Qt.CheckState.Checked:
                continue
            it.setCheckState(Qt.CheckState.Checked)
            tid = it.data(Qt.ItemDataRole.UserRole)
            if tid is not None:
                self._selected_ids.add(tid)
        self.table.clearSelection()
        self._syncing = False
        self._update_sel_label()

    def _on_header_clicked(self, logical):
        """点列头：记下用户排序意图（聚集排序让位于手动排序）；
        点勾选列表头 = 全选/取消本页"""
        if logical != CHECK_COL:
            self._user_sort_col = logical
            self._gather_ids = set()
            return
        if self.table.rowCount() == 0:
            return
        items = [self.table.item(r, CHECK_COL) for r in range(self.table.rowCount())]
        all_on = all(i.checkState() == Qt.CheckState.Checked for i in items)
        self._syncing = True
        for i in items:
            i.setCheckState(Qt.CheckState.Unchecked if all_on
                            else Qt.CheckState.Checked)
        self._syncing = False
        page_ids = {i.data(Qt.ItemDataRole.UserRole) for i in items}
        if all_on:
            self._selected_ids -= page_ids
        else:
            self._selected_ids |= page_ids
        self._update_sel_label()

    def _clear_selection(self):
        self._selected_ids.clear()
        self._syncing = True
        for r in range(self.table.rowCount()):
            self.table.item(r, CHECK_COL).setCheckState(Qt.CheckState.Unchecked)
        self._syncing = False
        self._update_sel_label()

    def _update_sel_label(self):
        self.lbl_sel.setText(f"已选 {len(self._selected_ids)} 个")

    # ============ 易用性增强：参数常驻 / 运行总览 / 快捷筛选 / 快捷键 / 首次提示 ============
    _QUICK_GROUPS = {"todo": ("",),
                     "running": ("running", "starting", "cancelling"),
                     "retry": ("failed", "error", "timeout")}

    def _update_params_label(self):
        """E1：把工具栏当前值常驻显示，改哪个下拉立刻更新"""
        dur = 2 + self.cb_duration.currentIndex()
        steps = self.cb_steps.currentText()
        kol = "不使用KOL" if self.cb_kol.currentIndex() == 0 else self.cb_kol.currentText()
        self.lbl_params.setText(f"本次默认：{dur}秒 · {steps}步 · {kol}")

    def _update_run_summary(self, actives):
        # A2：把「执行全部待办」的口径写成实时数量（当前筛选下填了提示词的）
        todo = sum(1 for _, row in self._filtered if str(row["提示词"]).strip())
        self.b_runall.setText(f"⏩ 执行全部待办（{todo}）")
        # E2：有在途任务才显示「进行中 / 排队」总览
        running = sum(1 for t in actives if t["status"] in BAR_STATUS)
        queued = len(actives) - running
        if actives:
            self.lbl_run.setText(f"⏳ 进行中 {running} · 排队 {queued}")
            self.lbl_run.show()
        else:
            self.lbl_run.hide()
        # E3：从「有在途」翻到「全清空」且本批在等完成 → 右下角轻提示（不弹框打断）
        if self._expect_done and self._prev_active > 0 and not actives:
            self._expect_done = False
            self._show_toast("✅ 本批任务已全部完成，视频已下载到 outputs/",
                             color="#00A870", msec=6000)
        self._prev_active = len(actives)

    def _set_status_group(self, group):
        idx = self.cb_f_status.findData(group)
        if idx >= 0:
            self.cb_f_status.setCurrentIndex(idx)   # currentIndexChanged 会 refresh

    def _quick_filter(self, kind):
        """A1 快捷标签：一键设好常用组合，先放开其它条件避免“并且”后互相抵消"""
        if kind == "all":
            self._clear_filters()
            return
        self.ed_search.blockSignals(True); self.ed_search.clear(); self.ed_search.blockSignals(False)
        for cb in (self.cb_f_product, self.cb_f_remark):
            cb.blockSignals(True); cb.setCurrentIndex(0); cb.blockSignals(False)
        self._goto_first_page()
        if kind == "today":
            self.cb_f_status.blockSignals(True); self.cb_f_status.setCurrentIndex(0)
            self.cb_f_status.blockSignals(False)
            self.de_start.setDate(QDate.currentDate())
            self.de_end.setDate(QDate.currentDate())
            self.cb_date.setChecked(True)          # 触发 _toggle_date_filter → 启用日期并 refresh
            self.refresh()
            return
        self.cb_date.blockSignals(True); self.cb_date.setChecked(False); self.cb_date.blockSignals(False)
        self.de_start.setEnabled(False); self.de_end.setEnabled(False)
        self._set_status_group(self._QUICK_GROUPS.get(kind))   # 触发 refresh
        self.refresh()

    def _in_lineedit(self):
        w = self.window().focusWidget() if self.window() else None
        return isinstance(w, QLineEdit)

    def _visible_only(self, fn):
        """快捷键守卫：只有任务中心当前可见时才响应（避免在其它页面误触发）"""
        return lambda: fn() if self.isVisible() else None

    def _select_all_page(self):
        if self.table.rowCount() == 0:
            return
        self._syncing = True
        ids = set()
        for r in range(self.table.rowCount()):
            it = self.table.item(r, CHECK_COL)
            it.setCheckState(Qt.CheckState.Checked)
            ids.add(it.data(Qt.ItemDataRole.UserRole))
        self._syncing = False
        self._selected_ids |= ids
        self._update_sel_label()

    def _setup_shortcuts(self):
        self._shortcuts = []
        for seq, fn in (
                ("Ctrl+F", lambda: (self.ed_search.setFocus(), self.ed_search.selectAll())),
                ("Ctrl+A", lambda: None if self._in_lineedit() else self._select_all_page()),
                ("Delete", lambda: None if self._in_lineedit() else self._del_selected()),
                ("F5", self.refresh),
                ("Ctrl+Return", lambda: self._run(True)),
        ):
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(Qt.ShortcutContext.WindowShortcut)
            s.activated.connect(self._visible_only(fn))
            self._shortcuts.append(s)

    def _arm_first_hints(self):
        """C2：关键按钮首次悬停多讲一句白话解释，看过一次就不再打扰"""
        self._hint_widgets = {}
        for w, key, text in (
            (self.b_runall, "hint_runall",
             "⏩ 执行全部待办＝跑当前筛选下所有「填了提示词、还没成功跑过」的任务，会先弹确认"),
            (self.b_replace_btn, "hint_find",
             "🔁 查找/替换：默认只查找并把命中的一批勾选置顶；点弹窗里的「替换 »」才批量改写"),
            (self.b_fields, "hint_fields",
             "⚟ 字段管理：控制显示哪些列；也可直接拖动表头调列序，设置会被记住"),
        ):
            if w is not None and not app_state.get(key):
                w.installEventFilter(self)
                self._hint_widgets[w] = (key, text)

    def _on_first_hint(self, w):
        key, text = self._hint_widgets.pop(w, (None, None))
        if not key:
            return
        self.lbl_tip.setText(text)
        app_state.set_value(key, True)
        w.removeEventFilter(self)

    def _show_toast(self, text, action_text="", msec=5000, on_action=None, color="#3370FF"):
        t = Toast(text, action_text=action_text, msec=msec, on_action=on_action,
                  anchor=self, color=color, parent=self.window())
        self._toasts.append(t)
        t.finished.connect(lambda _t=t: self._toasts.remove(_t) if _t in self._toasts else None)
        t.show()

    def _undo_delete(self, rows):
        n = task_store.restore_tasks(rows)
        self.refresh()
        self.lbl_tip.setText(f"↶ 已撤销，恢复 {n} 个任务")

    # ============ 空状态引导（表格无任务 / 无命中时盖在视口上）============
    def _build_empty_state(self):
        self._empty = QWidget(self.table.viewport())
        self._empty.setObjectName("EmptyState")
        self._empty.setStyleSheet("QWidget#EmptyState{background:transparent;}")
        v = QVBoxLayout(self._empty)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.setSpacing(6)
        self._empty_icon = QLabel("📭")
        self._empty_icon.setStyleSheet("font-size:46px;")
        self._empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._empty_icon)
        self._empty_title = QLabel("")
        self._empty_title.setStyleSheet("font-size:16px;font-weight:bold;color:#1F2329;")
        self._empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self._empty_title)
        self._empty_sub = QLabel("")
        self._empty_sub.setObjectName("PageTip")
        self._empty_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_sub.setWordWrap(True)
        v.addWidget(self._empty_sub)
        v.addSpacing(8)
        btns = QHBoxLayout()
        btns.setSpacing(10)
        self._empty_btn_import = QPushButton("📥 导入 Excel 任务")
        self._empty_btn_import.clicked.connect(self._import_excel)
        self._empty_btn_new = QPushButton("＋ 新建任务")
        self._empty_btn_new.setObjectName("GhostBtn")
        self._empty_btn_new.clicked.connect(self._new_task)
        self._empty_btn_clear = QPushButton("✕ 清除筛选")
        self._empty_btn_clear.setObjectName("GhostBtn")
        self._empty_btn_clear.clicked.connect(self._clear_filters)
        for b in (self._empty_btn_import, self._empty_btn_new, self._empty_btn_clear):
            btns.addWidget(b)
        holder = QHBoxLayout()
        holder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        holder.addLayout(btns)
        v.addLayout(holder)
        self._empty.hide()

    def _update_empty(self, total, matched):
        if total == 0:
            self._empty_icon.setText("📭")
            self._empty_title.setText("还没有任务")
            self._empty_sub.setText("点下方『导入 Excel 任务』批量建，或『新建任务』手写一条提示词；\n"
                                    "也可用工具栏『🔍 扫描新任务』拾取外部新增的提示词")
            self._empty_btn_import.show()
            self._empty_btn_new.show()
            self._empty_btn_clear.hide()
        elif matched == 0:
            self._empty_icon.setText("🔍")
            self._empty_title.setText("没有匹配的任务")
            self._empty_sub.setText("当前搜索 / 筛选条件太严，一条都没筛中；点『清除筛选』回到全部任务")
            self._empty_btn_import.hide()
            self._empty_btn_new.hide()
            self._empty_btn_clear.show()
        else:
            self._empty.hide()
            return
        vp = self.table.viewport()
        self._empty.setGeometry(vp.rect())
        self._empty.raise_()
        self._empty.show()

    # ============ 首次上手：四步引导（只在第一次进入任务中心时弹，可跳过）============
    def showEvent(self, e):
        super().showEvent(e)
        if self._tour_started or app_state.get("tasks_tour_seen"):
            return
        self._tour_started = True
        QTimer.singleShot(400, self._start_tour)

    def _start_tour(self):
        steps = [
            (self.b_io, "第 1 步 · 建任务：点这里【导入/导出】批量导入 Excel，"
                        "或用工具栏最左【＋ 新建任务】手写提示词。"),
            (self.b_run, "第 2 步 · 开跑：先在表格最左列勾选任务（支持跨页保留），"
                         "再点【▶ 执行选中】；赶时间可点【⏩ 执行全部待办】。"),
            (self.table, "第 3 步 · 看进度：『状态』列实时显示进度条，跑完视频自动下载到 "
                         "outputs/，单击『输出文件』列即可播放，右键有更多操作。"),
            (self.b_fields, "第 4 步 · 提效率：上方『快捷筛选』一键只看待办/进行中/失败；"
                            "Ctrl+F 搜索、Ctrl+A 全选本页、Delete 删除、F5 刷新；"
                            "『字段管理』控制显示哪些列。"),
        ]
        self._show_tour_step(steps, 0)

    def _show_tour_step(self, steps, i):
        if self._tour_bubble is not None:
            self._tour_bubble.close()
            self._tour_bubble = None
        if i >= len(steps):
            app_state.set_value("tasks_tour_seen", True)
            return
        target, text = steps[i]
        tip = _GuideBubble(self.window(), text, i + 1, len(steps))
        self._tour_bubble = tip
        tip.advanced.connect(lambda: self._show_tour_step(steps, i + 1))
        tip.closed.connect(self._finish_tour)
        tip.adjustSize()
        anchor = (target.mapToGlobal(QPoint(60, 60)) if target is self.table
                  else target.mapToGlobal(QPoint(6, target.height() + 8)))
        scr = QGuiApplication.primaryScreen().availableGeometry()
        x = max(scr.left() + 8, min(anchor.x(), scr.right() - tip.width() - 8))
        y = max(scr.top() + 8, min(anchor.y(), scr.bottom() - tip.height() - 8))
        tip.move(x, y)
        tip.show()
        tip.raise_()

    def _finish_tour(self):
        if self._tour_bubble is not None:
            self._tour_bubble.close()
            self._tour_bubble = None
        app_state.set_value("tasks_tour_seen", True)

    # ============ 全文预览浮层：单击或空格弹出，任意键关闭 ============
    def _on_cell_entered(self, r, c):
        # 不再悬停自动弹（会挡鼠标移入浮层的路），只记录光标位置供空格键使用
        self._hover_cell = (r, c)
        # D3：可操作单元格（看全文/播视频）显示手型光标，提示“这里能点”
        it = self.table.item(r, c)
        clickable = (c in PREVIEW_COLS and it is not None and bool(it.text())) \
            or (c == COL_OUT and it is not None
                and bool(it.data(Qt.ItemDataRole.UserRole + 2)))
        self.table.viewport().setCursor(Qt.CursorShape.PointingHandCursor if clickable
                                          else Qt.CursorShape.ArrowCursor)

    def _on_cell_click(self, r, c):
        if c in PREVIEW_COLS:
            self._popup_preview(r, c)
        elif c == COL_OUT:
            self._play_video(r)          # A3：单击输出列即播放，不用等双击

    def _popup_preview(self, r, c):
        if c not in PREVIEW_COLS or self.table.item(r, c) is None:
            return
        text = self._filtered_full_text(r, c)
        if not text.strip():
            self.hover.hide()
            return
        pos = QCursor.pos()
        self.hover.show_pinned(text, QPoint(pos.x() + 12, pos.y() + 12), rich_text=True)

    def _filtered_full_text(self, r, c):
        col_name = PREVIEW_COLS.get(c)
        if not col_name:
            return ""
        tid = self._row_tid(r)
        for t, row in self._filtered:
            if t == tid:
                return str(row[col_name])
        return ""

    def eventFilter(self, obj, e):
        from PySide6.QtCore import QEvent
        # 框选识别：在表视口上按住拖动超 16px 后的松开 = 一次框选，把框到的行勾上
        # （位移不够当普通单击处理，不碰勾选态——否则点勾选框会连带选中互相打架）
        if obj is self.table.viewport():
            if e.type() == QEvent.Type.MouseButtonPress \
                    and e.button() == Qt.MouseButton.LeftButton:
                self._drag_anchor = e.position().toPoint()
            elif e.type() == QEvent.Type.MouseButtonRelease \
                    and e.button() == Qt.MouseButton.LeftButton:
                anchor = self._drag_anchor
                self._drag_anchor = None
                if anchor is not None and \
                        (e.position().toPoint() - anchor).manhattanLength() > 16:
                    self._on_rubber_band()
                else:
                    # 单击不框选：顺手清掉 Qt 的行高亮。启用框选后单击也会高亮
                    # 整行，而这里的“真选中”是勾选框（跨页保留、驱动批量操作），
                    # 留着高亮会让人误以为这行被选上了。
                    self.table.clearSelection()
        if obj is self.table and e.type() == QEvent.Type.KeyPress \
                and e.key() == Qt.Key.Key_Space and self._hover_cell:
            self._popup_preview(*self._hover_cell)
            return True                     # 消费掉，避免表格另行处理空格
        # C2：关键按钮首次悬停时多讲一句（看过一次就摘掉过滤器，不再弹）
        if getattr(self, "_hint_widgets", None) and obj in self._hint_widgets \
                and e.type() == QEvent.Type.Enter:
            self._on_first_hint(obj)
        if obj is self.table.viewport() and e.type() == QEvent.Type.Resize \
                and getattr(self, "_empty", None) is not None and self._empty.isVisible():
            self._empty.setGeometry(self.table.viewport().rect())   # 空状态引导随表格一起缩放居中
        if obj is self.table.viewport() and e.type() == QEvent.Type.Leave:
            self.hover.hide_soon()          # 钉住模式不受影响（浮层自行判断）
        return super().eventFilter(obj, e)

    # ============ 双击 / 右键 ============
    def _on_cell_double(self, r, c):
        if c == COL_OUT:
            return                     # 单击已播放，双击不再重复、也不进编辑窗
        elif c == COL_REMARK:
            self._edit_remark(r)         # 备注要的是“随手一笔”，不弹整个任务窗
        elif c != CHECK_COL:
            self._edit_current(r)

    def _edit_remark(self, r):
        """写备注：先勾选多个就一次批量写，没勾选只改当前行；留空保存＝清除。

        备注不参与执行/迭代/重跑判定，纯粹给使用者自己分组用。"""
        tids = sorted(self._selected_ids) or [self._row_tid(r)]
        tids = [t for t in tids if t is not None]
        if not tids:
            return
        single = len(tids) == 1
        cur = ((task_store.get_task(tids[0]) or {}).get("remark") or "") if single else ""
        text, ok = QInputDialog.getText(
            self, "写备注" if single else f"写备注（{len(tids)} 个任务）",
            f"任务{tids[0]} 备注：" if single
            else f"一次写入 {len(tids)} 个已勾选任务（留空保存＝清除备注）：",
            QLineEdit.EchoMode.Normal, cur)
        if not ok:
            return
        for tid in tids:
            task_store.update_row(tid, **{"备注": text.strip()})
        self.refresh()

    def _play_video(self, r):
        item = self.table.item(r, COL_OUT)
        path = item.data(Qt.ItemDataRole.UserRole + 2) if item else ""
        if not path:
            QMessageBox.information(self, "提示", "该任务还没有输出视频")
            return
        if not Path(path).exists():
            QMessageBox.warning(self, "文件不存在", f"找不到视频文件：\n{path}")
            return
        self._player = VideoPlayerDialog(self, path)
        self._player.show()

    def _open_location(self, r):
        item = self.table.item(r, COL_OUT)
        path = item.data(Qt.ItemDataRole.UserRole + 2) if item else ""
        if path and Path(path).exists():
            reveal_in_folder(path)
        else:
            QMessageBox.information(self, "提示", "输出文件不存在或已被移动")

    def _on_context_menu(self, pos):
        """表格右键菜单：播放/打开位置/编辑/删除"""
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        r = idx.row()
        item_out = self.table.item(r, COL_OUT)
        path = item_out.data(Qt.ItemDataRole.UserRole + 2) if item_out else ""
        menu = QMenu(self)
        ats = REG.get_all_by_row(self._row_tid(r))
        if ats:
            if len(ats) == 1:
                menu.addAction("⏹ 取消执行（云端）", lambda: self._cancel(ats[0]))
            else:
                menu.addAction(f"⏹ 取消全部执行 ×{len(ats)}（云端）",
                               lambda: self._cancel_many(ats))
            menu.addSeparator()
        if path:
            menu.addAction("▶ 播放预览", lambda: self._play_video(r))
            menu.addAction("📂 打开存放位置", lambda: self._open_location(r))
            menu.addSeparator()
        menu.addAction("✎ 编辑任务", lambda: self._edit_current(r))
        n_sel = len(self._selected_ids)
        menu.addAction(("🏷 写备注（已选 " + str(n_sel) + " 个）") if n_sel
                       else "🏷 写备注（当前任务）",
                       lambda: self._edit_remark(r))
        menu.addAction(f"📝 批量绑定脚本（{'已选 ' + str(n_sel) + ' 个' if n_sel else '当前 1 个'}）",
                       lambda: self._bind_script(r))
        menu.addAction("🧐 口播规范检测", lambda: self._check_script(r))
        menu.addAction("🗑 删除任务",
                       lambda: self._del_tasks({self._row_tid(r)}))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ============ 取消云端任务 ============
    def _cancel(self, at):
        """向云端发送取消请求；终态由轮询线程回写"""
        tid = at["row_idx"]
        if QMessageBox.question(
                self, "取消任务",
                f"任务{tid} 正在{at['status']}（{int(at['progress'] or 0)}%），"
                "确认取消？") != QMessageBox.StandardButton.Yes:
            return
        self._do_cancel(at)

    def _cancel_many(self, ats):
        """同一任务并发多路执行（抽卡/重跑）时，一次确认全部取消"""
        tid = ats[0]["row_idx"]
        if QMessageBox.question(
                self, "取消任务",
                f"任务{tid} 当前有 {len(ats)} 个执行正在进行（抽卡/重跑），"
                "确认全部取消？") != QMessageBox.StandardButton.Yes:
            return
        ok = fail = 0
        for at in ats:
            if cancel_one(at):
                ok += 1
            else:
                fail += 1
        if fail:
            QMessageBox.warning(self, "部分取消失败",
                                f"{ok} 个已发送取消请求，{fail} 个失败，详见日志")
        else:
            self.lbl_tip.setText(f"⏹ 任务{tid} 的 {ok} 个执行均已发送取消请求，等待云端确认…")
        self.refresh()

    def _do_cancel(self, at):
        tid = at["row_idx"]
        if cancel_one(at):
            self.lbl_tip.setText(f"⏹ 任务{tid} 已发送取消请求，等待云端确认…")
        else:
            QMessageBox.warning(self, "取消失败",
                                f"任务{tid} 取消请求发送失败，详见日志")
        self.refresh()

    def _cancel_selected(self):
        targets = []
        for tid in sorted(self._selected_ids):
            targets.extend(REG.get_all_by_row(tid))
        if not targets:
            self.lbl_tip.setText("没有可取消的任务：勾选的任务均未在执行")
            return
        if len(targets) == 1:
            self._cancel(targets[0])          # 只有一个执行：沿用单条确认文案
            return
        # 多个执行只弹一次确认，不再每条弹一个窗口
        n_task = len({at["row_idx"] for at in targets})
        if QMessageBox.question(
                self, "批量取消任务",
                f"选中的 {n_task} 个任务共有 {len(targets)} 个执行正在进行，"
                "确认全部取消？") != QMessageBox.StandardButton.Yes:
            return
        ok = fail = 0
        for at in targets:
            if cancel_one(at):
                ok += 1
            else:
                fail += 1
        if fail:
            QMessageBox.warning(self, "部分取消失败",
                                f"{ok} 个已发送取消请求，{fail} 个失败，详见日志")
        else:
            self.lbl_tip.setText(f"⏹ {n_task} 个任务的 {ok} 个执行均已发送取消请求，等待云端确认…")
        self.refresh()

    def _check_script(self, r):
        """按产品规范卡 + 风控政策检测该任务的口播/提示词合规性"""
        tid = self._row_tid(r)
        t = task_store.get_task(tid)
        if not t:
            return
        has_script = bool((t["script_text"] or "").strip())
        text = t["script_text"] if has_script else (t["prompt"] or "")
        src = "口播文案" if has_script else "提示词（口播未提取，退而检提示词）"
        pname = t["product"] or ""
        spec = {}
        p = product_store.get_by_name(pname) if pname else None
        if p:
            spec = product_store.get_spec(p["id"])
        from gui.dialogs_spec import SpecCheckDialog
        title = (f"任务{t['num'] or tid} · {pname or '未填品名'} · 检测来源：{src}"
                 + ("" if has_script or (t["prompt"] or "").strip() else " · 无内容可检"))
        self._spec_dlg = SpecCheckDialog(self, title, text, spec, pname)
        self._spec_dlg.show()

    def _manage_fields(self):
        """字段管理：所有列表字段都在此显示/隐藏；顺序可在此拖，也可直接拖表头"""
        h = self.table.horizontalHeader()
        order = [h.logicalIndex(v) for v in range(1, self.table.columnCount())]
        hidden = {c for c in order if self.table.isColumnHidden(c)}
        cols = [(i, DATA_HEADERS[i - 1]) for i in range(1, len(HEADERS))]
        d = FieldManagerDialog(self, cols, order, hidden)
        if d.exec():
            apply_field_layout(self.table, d.order, d.hidden, first_locked=1)
            # 不存就会踩坑：每次开软件都要重新藏一遍 job_id/URL、重新拖一次列序
            app_state.set_value("tasks_fields",
                                {"order": list(d.order),
                                 "hidden": sorted(d.hidden)})

    def _restore_field_layout(self):
        """启动时恢复列布局；没存过时把技术字段（job_id/URL）默认收起。

        版本升级会增减列：只接受落在当前表头范围内的逻辑号，新列接在末尾。"""
        n = len(DATA_HEADERS)
        valid = set(range(1, n + 1))
        saved = app_state.get("tasks_fields") or {}
        known = [c for c in (saved.get("order") or [])
                 if isinstance(c, int) and c in valid]
        order = known + [c for c in range(1, n + 1) if c not in known]
        hidden = {c for c in (saved.get("hidden") or [])
                  if isinstance(c, int) and c in valid}
        if not known:                       # 首次使用（或升级后新增的技术列）
            hidden |= set(DEFAULT_HIDDEN)
        else:
            hidden |= {c for c in DEFAULT_HIDDEN if c not in known}
        apply_field_layout(self.table, order, hidden, first_locked=1)

    def _edit_current(self, r=None):
        if r is None:
            return
        tid = self._row_tid(r)
        t = task_store.get_task(tid)
        if not t:
            return
        data = TaskDialog.ask(self, {"num": t["num"], "product": t["product"],
                                     "script": t["script"], "prompt": t["prompt"],
                                     "remark": t["remark"]})
        if data:
            upd = {"编号": data["num"], "品名": data["product"],
                   "脚本": data["script"], "备注": data["remark"]}
            if data["prompt"] != (t["prompt"] or ""):
                # 改了提示词才重置执行态；只改脚本不影响迭代/重跑判定
                upd.update({"提示词": data["prompt"], "状态": "", "运行次数": 0})
            task_store.update_row(tid, **upd)
            self.refresh()

    def _bind_script(self, r):
        """单脚本多提示词：把一份脚本批量绑到勾选的任务（无勾选时仅当前行）；
        留空保存 = 清除关联（工厂流水线片段等不绑定任何脚本）"""
        tids = sorted(self._selected_ids) or [self._row_tid(r)]
        tids = [t for t in tids if t is not None]
        if not tids:
            return
        first = task_store.get_task(tids[0])
        current = (first or {}).get("script", "") or ""
        text = ScriptBindDialog.ask(self, count=len(tids), current=current)
        if text is None:
            return
        for tid in tids:
            task_store.update_row(tid, **{"脚本": text})
        self.lbl_tip.setText(
            f"已{'清除' if not text else '绑定'} {len(tids)} 个任务的脚本"
            + ("（留空=不关联任何脚本）" if not text else ""))
        self.refresh()

    # ============ 操作 ============
    def _restore_exec_params(self):
        """沿用上次执行用的时长/步数/KOL。

        不沿用就会踩坑：跑一批 10 秒任务后重启软件，下拉默默回到 5 秒，
        使用者以为“改了没生效”“怎么又变成原来的了”。"""
        d = app_state.get("exec_params") or {}
        dur = d.get("duration")
        if isinstance(dur, int) and 2 <= dur <= 15:
            self.cb_duration.setCurrentIndex(dur - 2)
        steps = d.get("steps")
        if isinstance(steps, int) and 1 <= steps <= 50:
            self.cb_steps.setCurrentIndex(steps - 1)
        kol = d.get("kol")
        if kol:
            idx = self.cb_kol.findText(kol)
            if idx >= 0:
                self.cb_kol.setCurrentIndex(idx)
        for cb in (self.cb_duration, self.cb_steps, self.cb_kol):
            cb.currentIndexChanged.connect(self._save_exec_params)

    def _save_exec_params(self, *_):
        app_state.set_value("exec_params", {
            "duration": 2 + self.cb_duration.currentIndex(),
            "steps": int(self.cb_steps.currentText()),
            "kol": None if self.cb_kol.currentIndex() == 0 else self.cb_kol.currentText()})
        self._update_params_label()

    def _current_options(self):
        """从控件读取当前时长/步数/KOL，生成提交选项快照。

        这三个是「本批任务怎么跑」的全局参数，不是任务自带字段：
        列表里的「时长」列只是上次提交的留痕，改参数要改这里。
        """
        return SubmitOptions(
            duration=2 + self.cb_duration.currentIndex(),
            steps=int(self.cb_steps.currentText()),
            kol=None if self.cb_kol.currentIndex() == 0 else self.cb_kol.currentText())

    def _new_task(self):
        data = TaskDialog.ask(self)
        if data and data["prompt"]:
            task_store.add_task(data["num"] or (task_store.task_count() + 1),
                                data["product"], data["prompt"],
                                script=data.get("script", ""),
                                remark=data.get("remark", ""))
            self.refresh()

    def _del_selected(self):
        if not self._selected_ids:
            self.lbl_tip.setText("没有可删除的任务：请先在表格左侧勾选")
            return
        self._del_tasks(set(self._selected_ids))

    def _del_tasks(self, tids):
        tids = {t for t in tids if t is not None}
        if not tids:
            return
        if QMessageBox.question(self, "确认",
                                f"删除选中的 {len(tids)} 个任务？（不影响已生成的视频）") \
                == QMessageBox.StandardButton.Yes:
            rows = [task_store.get_task(t) for t in tids]      # 删前抓整行，供 5 秒内撤销
            rows = [r for r in rows if r]
            task_store.delete_tasks(list(tids))
            self._selected_ids -= set(tids)
            self.refresh()
            self._show_toast(f"🗑 已删除 {len(rows)} 个任务", action_text="↶ 撤销",
                             on_action=lambda rs=rows: self._undo_delete(rs),
                             color="#F54A45", msec=6000)

    def _find_replace(self):
        sel = list(self._selected_ids)
        d = FindReplaceDialog.ask(self, selected_count=len(sel))
        if not d:
            return
        find, repl = d["find"], d["replace"]
        locate_only = d.get("locate_only")      # 只找不改：按关键词挑一批任务出来
        if d["only_selected"] and sel:
            tids = sel
        else:
            df = task_store.list_tasks_df()
            tids = [int(r["_id"]) for _, r in df.iterrows()]
        count = 0
        hit_ids = []                       # 含旧提示词的任务：处理后选中并置顶
        hits = []                          # [(tid, 旧片段, 新片段)]，供替换前预览
        for tid in tids:
            t = task_store.get_task(tid)
            prompt = t["prompt"] or ""
            if find in prompt:
                hit_ids.append(tid)
                count += 1
                if not locate_only:
                    sample = prompt.replace(find, repl)
                    hits.append((tid, prompt[:36], sample[:36]))
        if not count:
            self.lbl_tip.setText(f"未找到包含「{find}」的提示词")
            self.refresh()
            return
        # B2：改写前先给一眼“会动哪些、改成什么样”，避免一失手把上百条提示词改错
        if not locate_only:
            preview = "\n".join(f"  任务{tid}：{old} → {new}" for tid, old, new in hits[:6])
            more = f"\n  …共 {count} 个任务" if count > 6 else ""
            if QMessageBox.question(
                    self, "确认替换",
                    f"将把 {count} 个任务的提示词中的「{find}」全部替换为「{repl}」：\n"
                    f"{preview}{more}\n\n替换后可用「查找/定位」重新核对，确认执行？") \
                    != QMessageBox.StandardButton.Yes:
                self.lbl_tip.setText("已取消替换（未改动任何提示词）")
                return
            for tid, _o, _n in hits:
                t = task_store.get_task(tid)
                task_store.update_row(tid, **{"提示词": (t["prompt"] or "").replace(find, repl)})
        n = self._gather_matches(hit_ids)
        self.lbl_tip.setText(
            f"已按「{find}」勾选并置顶 {n} 个任务，可直接「执行选中」（提示词未改动）"
            if locate_only else
            f"替换完成：{n} 个任务的提示词已更新，已勾选并置顶集中显示，可直接「执行选中」")

    def _run(self, selected_only):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, "提示", "还有任务正在提交，请稍候…")
            return
        if selected_only:
            tids = sorted(self._selected_ids)
            if not tids:
                self.lbl_tip.setText("先在表格里勾选任务（第 1 列复选框，支持跨页保留），再点「执行选中」")
                return
        else:
            tids = [tid for tid, _ in self._filtered]
        # 先查哪些任务还有执行在跑（抽卡没结束、跑一半又点了一次「执行选中」）：
        # 分类口径要靠它——旧实现不查，给还在跑的任务念“已经执行成功过”这种错文案，
        # 确认后又不取消那份在跑的直接再提交，新旧两份同时占并发、各出一个成品
        inflight = inflight_execs(tids)
        items = []
        iterated = 0
        force = []                       # 已完成但提示词没改过，需确认是否强制重跑
        for tid in tids:
            t = task_store.get_task(tid)
            if not t:
                continue
            prompt = (t["prompt"] or "").strip()
            if not prompt:
                continue
            if tid in inflight:
                # 还在跑的不归“强制重跑”那档：归到提交清单，由同一个弹窗问要不要先取消
                items.append((tid, t["product"], prompt))
                continue
            if t["status"] and t["status"] not in RETRYABLE and int(t["runs"] or 0) > 0:
                # 已完成过：若提示词在上次执行后又改过 → 自动识别为“迭代执行”；
                # 没改过 → 收集到 force，交给用户确认是否强制重跑（而不是静默跳过）
                if not task_store.iter_ready(tid):
                    force.append((tid, t["product"], prompt))
                    continue
                iterated += 1
            # 已取消/失败的任务不提前清状态：提交成功时 _register_success 会写 submitted。
            # 旧实现先写空串再提交，一旦提交失败，行就永远停在“待执行”，
            # 看不出到底发没发出去（内测现场：422 全失败后任务看起来像排队中）
            items.append((tid, t["product"], prompt))

        repeated = 1
        options = self._current_options()      # 一次点击一个快照，弹窗与提交用的是同一组参数
        to_cancel = []
        skipped = False
        if force or inflight:
            single = len(force) == 1 and not items
            choice = ForceRerunDialog.ask(
                self, task_id=force[0][0] if single else None,
                count=len(force), allow_repeat=single,
                params_note=f"{options.duration}秒 / {options.steps} 步",
                running={tid: len(ats) for tid, ats in inflight.items()})
            items, to_cancel = decide_rerun(items, force, inflight, choice)
            if choice is None:
                force = []          # 跳过的这批不能再用“含 N 个强制重跑”的口径报
                skipped = True
            else:
                repeated = choice["repeat"]

        if not items:
            self.lbl_tip.setText(
                "已按「跳过」处理：没重跑任何任务，在跑的那几条保持原样" if skipped
                else "没有可执行的任务：请确认已填写提示词；改过提示词的已完成任务会自动按迭代重跑")
            return
        self.lbl_tip.setText(f"正在提交 {len(items)} 个任务"
                             f"（本次：{options.duration}秒 / {options.steps} 步）"
                             + (f"，含 {iterated} 个提示词迭代" if iterated else "")
                             + (f"；任务{force[0][0]} 抽 {repeated} 次卡"
                                if repeated > 1 and len(force) == 1
                                else f"；含 {len(force)} 个强制重跑" if force else "")
                             + (f"；先取消 {len(to_cancel)} 个在跑的执行" if to_cancel else "")
                             + "…")
        self.worker = SubmitWorker(items, options, to_cancel)
        self._failed_ids = set()      # B3：新一批提交，清空上次的失败标红
        self._expect_done = True       # E3：本批在等完成，跑完后轻提醒
        # 每条进度都带上本批参数：否则一行「✓ 任务6 已提交」就把上面那句
        # 「本次：10秒 / 20 步」冲掉，使用者无从判断这次到底用的什么参数
        snap = f"（本次：{options.duration}秒 / {options.steps} 步）"
        self.worker.log_msg.connect(
            lambda m, s=snap: self.lbl_tip.setText(f"{m} {s}"))
        self.worker.all_done.connect(self._on_submit_done)
        self.worker.start()

    def _on_submit_done(self, failed):
        """提交结果必留痕：失败只写一行灰色小字会被看成“改了没生效”
        （云端拒绝参数时就是这样：一行提示转瞬即逝，表格里的旧视频、旧时长还在）"""
        dist = getattr(self.worker, "distribution", "")
        # 本批落线汇总挂在收尾提示里：一句“只跑了一个线路”能不能当场对出来
        tail = ("　· " + dist) if dist else ""
        # B3：本次提交失败的任务标红并置顶，改好提示词直接重跑，不用翻找
        self._failed_ids = {tid for tid, _ in failed}
        if failed:
            self._gather_matches([tid for tid, _ in failed])   # 内部会 refresh
        else:
            self.refresh()
        if not failed:
            self.lbl_tip.setText("提交完成，云端生成中…进度条将实时更新，完成后自动下载到 outputs/"
                                 + tail)
            return
        reasons = "\n".join(f"  任务{tid}：{why}" for tid, why in failed[:8])
        more = f"\n  …共 {len(failed)} 条" if len(failed) > 8 else ""
        QMessageBox.warning(
            self, "部分任务未提交成功",
            f"{len(failed)} 个任务没提交上去，云端队列里根本不会有它们，\n"
            f"列表里看到的仍是上次跑出来的视频和参数。\n\n{reasons}{more}\n\n"
            "具体报文已写入日志。修正原因后重新【执行选中】即可。")
        self.lbl_tip.setText(f"⚠ {len(failed)} 个任务提交失败（见弹窗与日志），"
                             f"未提交到云端" + tail)

    def _import_excel(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择任务 Excel", "", "Excel 文件 (*.xlsx)")
        if not path:
            return
        try:
            n, dup, hit_ids = task_store.import_from_excel(path)
            msg = f"已导入 {n} 条任务"
            if dup:
                msg += f"；跳过重复 {dup} 条（提示词已存在）"
            if hit_ids:
                # 这份文件涉及的任务（新建的 + 提示词已存在的旧任务）勾选并置顶聚集
                self._gather_matches(hit_ids)
                msg += f"；已选中并置顶这 {len(hit_ids)} 条任务"
            self.lbl_tip.setText(msg)
        except Exception as e:
            QMessageBox.critical(self, "导入失败", str(e))

    def _do_export(self, fmt):
        try:
            self.lbl_tip.setText(f"已导出：{task_store.export_tasks(fmt)}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))
