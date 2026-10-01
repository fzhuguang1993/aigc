"""
gui/pages_tasks.py —— 任务中心
分页表格 · 图形进度条 · 无边框视频预览（倍速/全屏/审片标记）·
快捷定位/复制成品 · 搜索/替换/日期筛选 · 扫描新任务 · 导入导出（含模板）
"""
from pathlib import Path
import time

from PySide6.QtCore import (QThread, Signal, Qt, QDate, QPoint, QSize, QTimer,
                            QPropertyAnimation, QEasingCurve)
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QShortcut, QKeySequence
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QHeaderView, QComboBox,
                               QMessageBox, QFileDialog, QAbstractItemView,
                               QLineEdit, QCheckBox, QInputDialog, QFrame,
                               QDialog, QDialogButtonBox, QFormLayout,
                               QGroupBox, QGraphicsOpacityEffect,
                               QListWidget, QListWidgetItem, QScrollArea)

from registry.manager import REG, ACCOUNTS, BatchBalancer, is_active
from core.config import DEFAULT_STEPS, SUBMIT_PACING, BALANCE_RESCAN_EVERY
from core import tags as tag_lib
from store import task_store, product_store, app_state
from utils.desktop_utils import reveal_in_folder, copy_paths_to_clipboard
from processors import output_mark, archiver
from workers.submit import (do_submit, cancel_one, SubmitOptions,
                            format_batch_distribution)
from gui.dialogs import (TaskDialog, FindReplaceDialog, ForceRerunDialog,
                         ScriptBindDialog)
from gui.delegates import ProgressDelegate, ProgressRole
from gui.formatting import secs
from gui.widgets import VideoPlayerDialog, HoverPreview, Toast, FlowLayout
from gui.header import page_header, Card, kpi_row
from gui.menus import StyledMenu
from gui.tablekit import (FieldManagerDialog, apply_field_layout,
                          enable_drag_with_lock, SecsItem)
from gui.kit import TableColumnKit, DateRangePicker
from gui.theme import tokenize

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
DATA_HEADERS = ["任务ID", "编号", "品名", "标签", "备注", "脚本", "提示词", "状态", "时长",
                "生成用时", "排队", "账号", "运行", "成功", "取消", "口播文案", "分镜数",
                "更新时间", "输出文件", "job_id", "URL"]
HEADERS = [""] + DATA_HEADERS          # 第 0 列：勾选框
CHECK_COL = 0
COL_PID, COL_NUM, COL_PRODUCT, COL_TAG, COL_REMARK, COL_SCRIPT, COL_PROMPT, COL_STATUS, \
    COL_DUR, COL_GEN, COL_QUEUE, COL_ACCOUNT, COL_RUNS, COL_OK, COL_CANCEL, COL_VOICE, \
    COL_STORY, COL_UPDATED, COL_OUT, COL_JOB, COL_URL = range(1, len(DATA_HEADERS) + 1)
SECS_COLS = (COL_GEN, COL_QUEUE)     # 两个时长列走 SecsItem：显示「4分58秒」而排序按秒数
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
NO_TAGGED = "（未打标）"     # 标签下拉的虚拟选项：一筛就只看没打标签的
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
        card.setStyleSheet(tokenize("QFrame#GuideCard{background:#FFFFFF;border:1px solid #3370FF;"
                           "border-radius:10px;}"))
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(8)
        head = QLabel(f"快速上手 · {idx}/{total}")
        head.setStyleSheet(tokenize("color:#3370FF;font-weight:bold;"))
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


def _empty_filters():
    """筛选的空条件集（各多选集合皆空 = 不限；日期关）。日期存 ISO 串，
    便于与行「更新时间」前 10 位直接字符串比较。status 存的是档位标签。"""
    return {"status": set(), "product": set(), "tag": set(),
            "remark": set(), "audit": set(),
            "date_on": False,
            "date_start": QDate.currentDate().addDays(-7).toString("yyyy-MM-dd"),
            "date_end": QDate.currentDate().toString("yyyy-MM-dd")}


class ParamsDialog(QDialog):
    """⚙ 参数管理：把时长 / 步数 / KOL 三个本批全局参数收进弹窗。

    三个下拉仍是 TasksPage 的控件（提交/持久化逻辑完全沿用），只是从
    工具栏搬进了这个对话框；对话框在 TasksPage 初始化时建好、常驻隐藏。"""
    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙ 参数管理")
        self.setMinimumWidth(430)
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(12)
        head = QLabel("本批执行参数")
        head.setObjectName("DialogTitle")
        v.addWidget(head)
        tip = QLabel("这里的时长 / 步数 / 数字人 KOL 是「执行选中 / 执行全部」整批统一使用的参数；\n"
                     "表格里的「时长」列只是上次提交的留痕，改这里才起作用。改完点「确定」保存，下次启动沿用。")
        tip.setObjectName("PageTip")
        tip.setWordWrap(True)
        v.addWidget(tip)
        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow("视频时长：", page.cb_duration)
        form.addRow("生成步数：", page.cb_steps)
        form.addRow("数字人 KOL：", page.cb_kol)
        v.addLayout(form)
        v.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


class FilterPanel(QWidget):
    """⚟ 更多筛选：快捷筛选条下方就地展开的内嵌面板（取代旧模态弹窗）。

    选项是胶囊标签：点一下变色＝选中并即时过滤，再点一下取消；面板与
    快捷标签共用同一 _filters 状态、双向同步。同类多选＝任一命中（OR），
    类间＝并且（AND）；全部取消＝不限（取消最后一项就该放开）。
    选项跟着数据走：每次展开按当前 task_store.filter_choices + 词库重建。"""
    _TITLES = {"status": "状态", "audit": "审片标记", "product": "品名",
               "tag": "标签", "remark": "备注"}

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self._page = page
        self._syncing = False       # 程序化重勾时屏蔽点击/变更回写，防风暴
        self.setObjectName("Card")
        # 子类不自开 WA_StyledBackground，不补这行 QSS 白底画不出来
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 12)
        v.setSpacing(8)
        trow = QHBoxLayout()
        tip = QLabel("点标签即过滤（再点取消）· 同类多选＝任一命中 · 类间＝并且 · 全不选＝该类不限")
        tip.setObjectName("PageTip")
        trow.addWidget(tip)
        trow.addStretch(1)
        b_clear = QPushButton("✕ 清除全部")
        b_clear.setObjectName("GhostBtn")
        b_clear.setToolTip("一次放开所有筛选条件（等价于快捷条「全部放开」）")
        b_clear.clicked.connect(self._clear_all)
        trow.addWidget(b_clear)
        v.addLayout(trow)
        grid = QGridLayout()
        grid.setSpacing(10)
        # 三列强制平分：不拉伸的话日期组（两个日期框+箭头）会把列宽抢走，
        # 胶囊组被压成一列竖排，换行布局白做
        for c in range(3):
            grid.setColumnStretch(c, 1)
        v.addLayout(grid)
        self._flows = {}        # key -> FlowLayout（选项每次 reload 按数据重建）
        self._chips = {}        # key -> [QPushButton]
        specs = (("status", 0, 0), ("audit", 0, 1), ("date", 0, 2),
                 ("product", 1, 0), ("tag", 1, 1), ("remark", 1, 2))
        for key, r, c in specs:
            if key == "date":
                grid.addWidget(self._build_date(), r, c)
                continue
            box = QGroupBox(self._TITLES[key])
            bv = QVBoxLayout(box)
            bv.setContentsMargins(8, 6, 8, 8)
            flow = FlowLayout(hgap=6, vgap=6)
            bv.addLayout(flow)
            grid.addWidget(box, r, c)
            self._flows[key] = flow
            self._chips[key] = []

    def _build_date(self):
        gb = QGroupBox("按更新时间")
        gb.setCheckable(True)
        self.gb_date = gb
        gb.toggled.connect(self._on_changed)
        gv = QVBoxLayout(gb)
        gv.setContentsMargins(8, 4, 8, 8)
        # 统一用组件库里的日期区间选择器（一颗按钮弹日历、带近 N 天快捷），
        # 不再摆两个裸 QDateEdit——与全站其它日期区间口径一致
        self.dr_date = DateRangePicker()
        self.dr_date.changed.connect(self._on_changed)
        gv.addWidget(self.dr_date)
        gv.addStretch(1)
        return gb

    def reload(self):
        """按当前数据重建胶囊 + 按页面 _filters 回勾（展开时、快捷标签改动后调用）"""
        f = self._page._filters
        ch = task_store.filter_choices()
        tag_opts = list(tag_lib.load())
        for t in ch["tags"]:
            if t not in tag_opts:
                tag_opts.append(t)
        opts = {"status": [lbl for lbl, grp in STATUS_FILTERS[1:]],
                "audit": ["可用", "不可用"],
                "product": [BLANK] + list(ch["products"]),
                "tag": [NO_TAGGED] + tag_opts,
                "remark": [NO_TAG] + list(ch["remarks"])}
        self._syncing = True
        for key, flow in self._flows.items():
            while flow.count():
                it = flow.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
            chips = []
            for opt in opts[key]:
                b = QPushButton(str(opt))
                b.setObjectName("ChipBtn")
                b.setCheckable(True)
                b.setChecked(opt in f[key])
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(self._on_changed)   # 点一下就地过滤，无「应用」按钮
                flow.addWidget(b)
                chips.append(b)
            self._chips[key] = chips
        self.gb_date.setChecked(f["date_on"])
        self.dr_date.set_range(QDate.fromString(f["date_start"], "yyyy-MM-dd"),
                               QDate.fromString(f["date_end"], "yyyy-MM-dd"))
        self._syncing = False
        # 勾过的值可能已不在候选里（产品被删等）：回推一次，让失效条件静默退场
        self._push()

    def sizeHint(self):
        """面板高度：FlowLayout 的 sizeHint 只反映「一行」，展开动画的终点高度
        得按列宽估算各组换行后的高度再取两行合计（收起态 width 还没定，
        按假定宽估；估多了收尾会自动收敛，估少了展开末尾补一下）"""
        m = self.layout().contentsMargins()
        total_w = max(self.width(), 1000) - m.left() - m.right()
        col_w = max((total_w - 2 * 10) // 3 - 16, 180)     # 3 列减组间距/内边距
        row0 = max(self._flows["status"].heightForWidth(col_w),
                   self._flows["audit"].heightForWidth(col_w),
                   self.gb_date.sizeHint().height() - 14)
        row1 = max(self._flows[k].heightForWidth(col_w)
                   for k in ("product", "tag", "remark"))
        h = (row0 + 44) + (row1 + 44) + 10 + 36 + m.top() + m.bottom()
        return QSize(0, h)

    def collect(self):
        f = {k: {b.text() for b in chips if b.isChecked()}
             for k, chips in self._chips.items()}
        f["date_on"] = self.gb_date.isChecked()
        s, e = self.dr_date.get_range()
        f["date_start"] = s.toString("yyyy-MM-dd")
        f["date_end"] = e.toString("yyyy-MM-dd")
        return f

    def _on_changed(self, *_a):
        if not self._syncing:
            self._push()

    def _clear_all(self):
        """面板内一键放开：清页面条件后 reload 重勾（reload 尾部会把空条件推回去）"""
        self._page._filters = _empty_filters()
        self.reload()

    def _push(self):
        """面板勾选态 → 页面筛选态：就地套用（_syncing 期间不触发，不会成环）"""
        self._page._filters = self.collect()
        self._page._apply_filter_summary()
        self._page._goto_first_page()
        self._page.refresh()


# 字段管理弹窗的列分组（标题须与 DATA_HEADERS 一一对应，覆盖全部可管理列）
_FIELD_CATEGORIES = [
    ("基础信息", ["任务ID", "编号", "品名", "标签", "备注"]),
    ("文案素材", ["脚本", "提示词", "口播文案", "分镜数"]),
    ("执行状态", ["状态", "时长", "生成用时", "排队", "账号", "运行", "成功", "取消"]),
    ("时间输出", ["更新时间", "输出文件"]),
    ("技术字段", ["job_id", "URL"]),
]


class TasksPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 14, 24, 12)
        lay.setSpacing(12)

        # 对标数据中台的视觉层次：页头 → 一排 KPI 概览 → 白卡分组的「控制区
        # + 表格内容区」，而不是把工具条/筛选/表格裸摆在灰底上。
        hdr = page_header("任务中心", icon="📋")
        lay.addWidget(hdr)

        # ---------- 顶部 KPI 概览（总任务/待执行/进行中/成功/失败，随 refresh 更新）----------
        self._kpi_lay, self._kpi_cards = kpi_row([
            ("总任务", "📦", "#3370FF"), ("待执行", "🗒", "#8F959E"),
            ("进行中", "⏳", "#FF8D19"), ("成功", "✓", "#00B96B"),
            ("失败", "✕", "#F54A45"),
        ], spacing=12, min_width=118)
        self.k_total, self.k_pending, self.k_run, self.k_ok, self.k_fail = self._kpi_cards
        lay.addLayout(self._kpi_lay)

        # ---------- 控制区卡片：工具栏 / 搜索 / 快捷筛选 / 内嵌筛选面板收进一张白卡 ----------
        ctrl = Card(margins=(14, 12, 14, 12))
        ctrl.v.setSpacing(10)

        # ---------- 工具栏 ----------
        bar = QHBoxLayout()
        b_new = QPushButton("＋ 新建任务")
        b_run = QPushButton("▶ 执行选中")
        b_runall = QPushButton("⏩ 执行全部")
        b_scan = QPushButton("🔍 扫描新任务")
        b_cancel = QPushButton("⏹ 取消选中")
        b_del = QPushButton("🗑 删除")
        b_io = QPushButton("📁 导入/导出")
        # 注意：QPushButton.setMenu 配合全局样式表会导致点击无反应，改为手动弹出菜单
        self._io_menu = self._build_io_menu()
        b_io.clicked.connect(
            lambda: self._io_menu.exec(b_io.mapToGlobal(QPoint(0, b_io.height()))))
        # 成品整理（清理不可用 / 清理未批准 / 批量归档）合成一个下拉菜单按钮：
        # 同样避开 setMenu+全局样式的坑，用 clicked 手动 exec
        b_tidy = QPushButton("🧹 成品整理")
        self._tidy_menu = self._build_tidy_menu()
        b_tidy.clicked.connect(
            lambda: self._tidy_menu.exec(b_tidy.mapToGlobal(QPoint(0, b_tidy.height()))))
        # 字幕处理：对勾选成品批量走录屏字幕模块（生成/检测/高亮/烧录）
        b_sub = QPushButton("🔤 字幕处理")
        b_sub.setToolTip("给勾选的成品视频批量生成字幕 / 烧录（复用录屏字幕模块）")
        b_sub.clicked.connect(self._subtitle_batch)
        for b in (b_cancel, b_del, b_io, b_tidy, b_sub):
            b.setObjectName("GhostBtn")
        for b in (b_new, b_run, b_runall, b_scan, b_cancel, b_del,
                  b_io, b_tidy, b_sub):
            bar.addWidget(b)
        # 留存几个引导/快捷键目标（首次上手气泡、顶部待办数量都要用）
        self.b_new, self.b_run, self.b_io = b_new, b_run, b_io
        self.b_runall = b_runall
        # 一次性「清除演示数据」：仅当库里存在演示任务且尚未清除过时才显示
        self.b_demo = QPushButton("🧹 清除演示数据")
        self.b_demo.setObjectName("GhostBtn")
        self.b_demo.setToolTip("打包自带的看板演示任务；确认后清除，本按钮不再出现，真实任务不受影响")
        self.b_demo.clicked.connect(self._clear_demo_tasks)
        bar.addWidget(self.b_demo)
        self._update_demo_btn()
        bar.addStretch(1)
        # 时长/步数/KOL 收进「⚙ 参数管理」弹窗：工具栏只留入口 + 常驻概要；
        # 三个下拉仍是本页控件（提交/持久化逻辑沿用），只是被 ParamsDialog 收养
        self.cb_duration = QComboBox()
        self.cb_duration.addItems([f"{i}秒" for i in range(2, 16)])   # 2-15 秒可选
        self.cb_duration.setCurrentIndex(3)                           # 默认 5 秒
        self.cb_steps = QComboBox()                                   # AI 生成步数（1-50）
        self.cb_steps.addItems([str(i) for i in range(1, 51)])
        self.cb_steps.setCurrentIndex(DEFAULT_STEPS - 1)
        self.cb_steps.setToolTip("生成步数：越大细节越好、耗时更长（1-50）\n"
                                 "作为 parameters.inference_steps 传给云端，下次启动沿用本次值")
        self.cb_kol = QComboBox()
        self._kol_items = ["不使用"] + product_store.kol_names()   # KOL 下拉内容指纹
        self.cb_kol.addItems(self._kol_items)
        self._params_dlg = ParamsDialog(self, self)
        b_params = QPushButton("⚙ 参数管理")
        b_params.setObjectName("GhostBtn")
        b_params.setToolTip("设置本批执行的时长 / 步数 / 数字人 KOL")
        b_params.clicked.connect(self._open_params)
        bar.addWidget(b_params)
        self.b_params = b_params
        # 「本次默认参数」挪到页头右侧、淡红常驻：时长/步数/KOL 是整批共用的，
        # 藏在工具栏末端容易被当成「改了没生效」，放最显眼的地方盯着它
        self.lbl_params = QLabel("")
        self.lbl_params.setStyleSheet("font-size:12px;font-weight:600;color:#D45C5C;"
                                      "background:transparent;")
        self.lbl_params.setToolTip(
            "「参数管理」当前值＝下一次「执行选中/执行全部」会用到的参数；\n"
            "表格里的「时长」列只是上次提交的留痕，改不动也不起作用")
        hdr.layout().addWidget(self.lbl_params)
        # 值一变就淡入脉冲：这个参数最容易弄错，动效把视线拉过来
        self._params_fx = QGraphicsOpacityEffect(self.lbl_params)
        self.lbl_params.setGraphicsEffect(self._params_fx)
        self._params_an = QPropertyAnimation(self._params_fx, b"opacity", self)
        self._params_an.setDuration(450)
        self._params_an.setStartValue(0.25)
        self._params_an.setEndValue(1.0)
        self._params_an.setEasingCurve(QEasingCurve.Type.OutCubic)
        ctrl.v.addLayout(bar)
        self._restore_exec_params()          # 沿用上次用过的时长/步数/KOL

        # ---------- 搜索 + 已生效筛选概要 ----------
        fbar = QHBoxLayout()
        fbar.addWidget(QLabel("🔍"))
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("搜索品名/编号/标签/脚本/提示词/口播文案，回车过滤")
        self.ed_search.setFixedWidth(260)
        self.ed_search.returnPressed.connect(lambda: (self._goto_first_page(), self.refresh()))
        fbar.addWidget(self.ed_search)
        b_replace = QPushButton("🔁 查找/替换")
        b_replace.setObjectName("GhostBtn")
        b_replace.setToolTip("默认只查找：按关键词挑一批任务勾选置顶；点「替换 »」展开后可批量改写提示词")
        b_replace.clicked.connect(self._find_replace)
        fbar.addWidget(b_replace)
        self.b_replace_btn = b_replace
        fbar.addStretch(1)
        # 多条件筛选入口挪到了下方快捷筛选条末尾的「⚟ 更多筛选」（内嵌展开）；
        # 这里只留已生效条件的概要
        self.lbl_filter = QLabel("未筛选")
        self.lbl_filter.setObjectName("PageTip")
        fbar.addWidget(self.lbl_filter)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("PageTip")
        fbar.addWidget(self.lbl_count)
        ctrl.v.addLayout(fbar)

        # ---------- 快捷筛选标签 + 「更多筛选」展开入口 ----------
        chbar = QHBoxLayout()
        lbl_ch = QLabel("快捷筛选：")
        lbl_ch.setObjectName("PageTitle")
        chbar.addWidget(lbl_ch)
        for text, kind in self._QUICK_GROUPS:
            btn = QPushButton(text)
            btn.setObjectName("ChipBtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=kind: self._quick_filter(k))
            chbar.addWidget(btn)
        chbar.addSpacing(6)
        # 与快捷筛选同款胶囊（ChipBtn），只把描边深一号标识它是入口
        self.b_more = QPushButton("⚟ 更多筛选 ▾")
        self.b_more.setObjectName("ChipBtn")
        self.b_more.setProperty("accent", "1")
        self.b_more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.b_more.setToolTip("在下方就地展开筛选面板：状态/品名/标签/备注/审片/日期<br>"
                               "多选组合，<b>点选即过滤</b>；与快捷标签共用同一套条件，双向同步")
        self.b_more.clicked.connect(lambda: self._toggle_filter_panel())
        chbar.addWidget(self.b_more)
        chbar.addStretch(1)
        # 「⚟ 字段管理」入口：点开放大式弹窗（推广后台同款：左分类勾选 / 右拖动排序）
        self.b_fields = QPushButton("⚟ 字段管理")
        self.b_fields.setObjectName("ChipBtn")
        self.b_fields.setProperty("accent", "1")
        self.b_fields.setCursor(Qt.CursorShape.PointingHandCursor)
        self.b_fields.setToolTip("弹窗管理列显示/隐藏与顺序：左侧按分类勾选，右侧拖动调序（设置会被记住）")
        self.b_fields.clicked.connect(self._manage_fields)
        chbar.addWidget(self.b_fields)
        ctrl.v.addLayout(chbar)

        # ---------- 内嵌筛选面板：默认收起，展开时把表格整体下推 ----------
        self.filter_panel = FilterPanel(self)
        self.filter_panel.setVisible(False)
        self.filter_panel.setMaximumHeight(0)
        ctrl.v.addWidget(self.filter_panel)
        self._panel_an = QPropertyAnimation(self.filter_panel, b"maximumHeight", self)
        self._panel_an.setDuration(180)
        self._panel_an.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._panel_an.finished.connect(self._panel_an_done)

        # 字段管理改为弹窗（见 _manage_fields），不再内嵌下推面板
        lay.addWidget(ctrl)

        # ---------- 内容区卡片：表格 + 分页栏收进一张白卡 ----------
        content = Card(margins=(12, 10, 12, 10))
        content.v.setSpacing(8)

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
        widths = {COL_PID: 60, COL_NUM: 50, COL_PRODUCT: 110, COL_TAG: 84, COL_REMARK: 110,
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
        self._tag_editor = None                  # 当前内联标签下拉（非 None 时暂停重建表）
        content.v.addWidget(self.table, 1)
        # 统一列交互：表头右键可设本列靠左/居中/靠右 / 垂直居中 / 自动换行。
        # 复用 gui.kit.TableColumnKit：只接管「表头右键」这一个入口，不动本页的
        # 进度条委托 / 框选勾选 / 排序 / 字段管理 / 单元格业务菜单等既有行为。
        TableColumnKit(self.table)
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
        content.v.addLayout(pbar)
        lay.addWidget(content, 1)

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
        self.hover = HoverPreview(host=self)  # 全文预览浮层（单击/空格唤起，任意键关闭，Alt+W 除外）
        self._hover_cell = None      # 鼠标最后所在的 (row, col)，空格键弹整列用
        self._toasts = []            # 右下角浮层引用，防 GC
        self._failed_ids = set()     # 本次提交失败的任务：标红+置顶，便于改后重跑
        self._prev_active = 0        # 上次刷新的在途数，用于“跑完”边沿检测
        self._expect_done = False    # 提交后等待完成通知
        self._filters = _empty_filters()   # 筛选条件集（多条件×多选），空=不限
        self._panel_open = False       # 「⚟ 更多筛选」内嵌面板是否展开
        self._last_view_sig = None   # 上次刷新的视图指纹（未变则跳过重建）
        self._last_data_sig = None   # 上次刷新的数据指纹（库写入 + 内存在途态）

        b_new.clicked.connect(self._new_task)
        b_del.clicked.connect(self._del_selected)
        b_cancel.clicked.connect(self._cancel_selected)
        b_run.clicked.connect(lambda: self._run(True))
        b_runall.clicked.connect(lambda: self._run(False))
        b_scan.clicked.connect(self._scan_new)
        self._restore_field_layout()      # 沿用上次列宽/列序/显隐
        self._setup_shortcuts()           # A4：常用键盘快捷键
        self._arm_first_hints()           # C2：关键按钮首次悬停多讲一句
        self._update_params_label()       # E1：初始化“本次默认参数”显示
        self._apply_filter_summary()      # 初始化筛选概要（刚进来＝未筛选）

    # ============ 导入/导出菜单 ============
    def _build_io_menu(self):
        m = StyledMenu(self)
        m.addAction("📥 从 Excel 导入任务", self._import_excel)
        m.addAction("📄 下载导入模板", self._download_template)
        m.addSeparator()
        m.addAction("导出任务为 Excel", lambda: self._do_export("excel"))
        m.addAction("导出任务为 CSV", lambda: self._do_export("csv"))
        m.addAction("导出任务为 JSON", lambda: self._do_export("json"))
        m.addSeparator()
        m.addAction("导出执行记录", self._export_runs)
        return m

    def _build_tidy_menu(self):
        """成品整理下拉：把原来三个工具栏按钮收进一处（行尾提示同旧版）"""
        m = StyledMenu(self)
        a_bad = m.addAction("🧹 清理不可用", self._cleanup_bad)
        a_bad.setToolTip("把所有标了「👎 不可用」的成品一次移到回收站（能搜回来）；\n"
                         "只删视频与它们的标记，不删任务、也不删执行记录")
        a_purge = m.addAction("🧹 清理未批准", self._purge_unapproved)
        a_purge.setToolTip("反向清理：只保留标了「👍 可用」的成品，其余没标可用的\n"
                           "成品一次移到回收站；清完没有可用成品的任务行一并删（可撤销）；\n"
                           "在跑/排队任务不受影响，需输入「确认删除」才执行")
        m.addSeparator()
        a_arc = m.addAction("📦 批量归档", self._archive)
        a_arc.setToolTip("把生成文件夹里散着的成品，按【日期 / 产品 / 标签】归进子文件夹；\n"
                         "以任务表为准逐条搬（不丢审片标记、输出路径同步更新），只搬位置不改文件名")
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

    # ============ 参数管理 / 更多筛选（内嵌展开面板） ============
    def _open_params(self):
        """弹「参数管理」：取消要能反悔，故打开前快照、被拒时回滚（下拉改动会即时存）"""
        snap = (self.cb_duration.currentIndex(), self.cb_steps.currentIndex(),
                self.cb_kol.currentIndex())
        if self._params_dlg.exec() != QDialog.DialogCode.Accepted:
            for cb, idx in zip((self.cb_duration, self.cb_steps, self.cb_kol), snap):
                cb.blockSignals(True)
                cb.setCurrentIndex(idx)
                cb.blockSignals(False)
        self._save_exec_params()
        self._update_params_label()

    def _toggle_filter_panel(self, show=None):
        """「⚟ 更多筛选」：面板在快捷条下方高度补间展开/收起，把表格整体下推。
        旧版是模态弹窗：勾完就关、丢了上下文；内嵌面板勾选即过滤、所见即所得。"""
        p = self.filter_panel
        on = (not self._panel_open) if show is None else show
        if on and self._panel_open:
            return
        if not on and not self._panel_open:
            return
        self._panel_open = on
        if on:
            p.reload()               # 展开前按当前条件回勾（快捷标签改过也对得上）
            p.setVisible(True)
        self._panel_an.stop()
        self._panel_an.setStartValue(p.height() if p.maximumHeight() == 16777215
                                     else p.maximumHeight())
        self._panel_an.setEndValue(max(p.sizeHint().height(), 160) if on else 0)
        self._panel_an.start()
        self._apply_filter_summary()   # 刷新按钮文字里的箭头/计数

    def _panel_an_done(self):
        """展开动画收尾：放开高度上限，让面板跟随内容自适应"""
        if self._panel_open:
            self.filter_panel.setMaximumHeight(16777215)
        else:
            self.filter_panel.setVisible(False)

    def _sync_filter_panel(self):
        """快捷标签/清除改了筛选态后，面板重勾回显（仅展开时）"""
        p = getattr(self, "filter_panel", None)
        if p is not None and self._panel_open and p.isVisible():
            p.reload()

    def _manage_fields(self):
        """「⚟ 字段管理」弹窗（推广后台式）：左分类勾选、右拖动排序，确定后应用并持久化。"""
        table = self.table
        h = table.horizontalHeader()
        n = table.columnCount()
        vis = [c for c in (h.logicalIndex(v) for v in range(1, n))
               if not table.isColumnHidden(c)]
        hidden = {c for c in range(1, n) if table.isColumnHidden(c)}
        cols = [(i + 1, DATA_HEADERS[i]) for i in range(len(DATA_HEADERS))]
        dlg = FieldManagerDialog(
            self, cols, vis, hidden, categories=_FIELD_CATEGORIES,
            default_order=list(range(1, len(DATA_HEADERS) + 1)),
            default_hidden=set(DEFAULT_HIDDEN), title="字段管理 · 任务中心")
        if dlg.exec():
            apply_field_layout(table, dlg.order, dlg.hidden, first_locked=1)
            app_state.set_value("tasks_fields",
                                {"order": list(dlg.order),
                                 "hidden": sorted(dlg.hidden)})

    # 快捷标签：一键套用常用条件（与「⚟ 更多筛选」面板共用同一 _filters 状态）
    _QUICK_GROUPS = [("待执行", "pending"), ("进行中", "running"),
                     ("失败/可重试", "failed"), ("今日更新", "today"),
                     ("可用", "ok"), ("不可用", "bad"), ("全部放开", "clear")]

    def _has_filter(self):
        f = self._filters
        return bool(f["status"] or f["product"] or f["tag"]
                    or f["remark"] or f["audit"] or f["date_on"])

    def _apply_filter_summary(self):
        """把已生效条件写成概要 + 刷新「更多筛选」按钮的计数/箭头；无筛选则“未筛选”"""
        arrow = "▴" if getattr(self, "_panel_open", False) else "▾"
        if not self._has_filter():
            self.lbl_filter.setText("未筛选")
            self.lbl_filter.setToolTip("")
            self.b_more.setText(f"⚟ 更多筛选 {arrow}")
            return
        f = self._filters
        n = sum(len(f[k]) for k in ("status", "product", "tag", "remark", "audit"))
        if f["date_on"]:
            n += 1
        self.b_more.setText(f"⚟ 更多筛选（{n}）{arrow}")
        parts = []
        if f["status"]:
            parts.append("状态：" + "、".join(sorted(f["status"])))
        if f["product"]:
            parts.append("品名：" + "、".join(sorted(f["product"])))
        if f["tag"]:
            parts.append("标签：" + "、".join(sorted(f["tag"])))
        if f["remark"]:
            parts.append("备注：" + "、".join(sorted(f["remark"])))
        if f["audit"]:
            parts.append("审片：" + "、".join(sorted(f["audit"])))
        if f["date_on"]:
            parts.append(f"日期：{f['date_start']}~{f['date_end']}")
        txt = "　".join(parts)
        self.lbl_filter.setText(txt if len(txt) <= 40 else txt[:38] + "…")
        self.lbl_filter.setToolTip(txt + "\n（点「⚟ 更多筛选」修改；快捷条「全部放开」一键清除）")

    def _clear_filters(self):
        self._gather_ids = set()
        self._user_sort_col = None
        self._filters = _empty_filters()
        self.ed_search.clear()
        self._apply_filter_summary()
        self._sync_filter_panel()
        self._goto_first_page()
        self.refresh()

    def _quick_filter(self, kind):
        """快捷标签：一键套用常用条件（其余清空，避免多条件 AND 互相抵消）；
        可用/不可用再点一次取消。「全部放开」等价于清除全部筛选。"""
        f = _empty_filters()
        today = QDate.currentDate().toString("yyyy-MM-dd")
        cur = self._filters
        only_audit = not (cur["status"] or cur["product"] or cur["tag"]
                          or cur["remark"] or cur["date_on"])
        if kind == "pending":
            f["status"] = {"待执行"}
        elif kind == "running":
            f["status"] = {"进行中"}
        elif kind == "failed":
            f["status"] = {"失败"}
        elif kind == "today":
            f["date_on"] = True
            f["date_start"] = today
            f["date_end"] = today
        elif kind == "ok":
            if not (cur["audit"] == {"可用"} and only_audit):
                f["audit"] = {"可用"}      # 否则视为取消：f 保持空
        elif kind == "bad":
            if not (cur["audit"] == {"不可用"} and only_audit):
                f["audit"] = {"不可用"}
        # kind == "clear" 或 取消情形：f 保持空 = 全部放开
        if kind == "clear":
            self.ed_search.clear()
        self._gather_ids = set()
        self._user_sort_col = None
        self._filters = f
        self._apply_filter_summary()
        self._sync_filter_panel()
        self._goto_first_page()
        self.refresh()

    def _match_filter(self, row):
        return (self._match_kw(row) and self._match_choice(row)
                and self._match_date(row) and self._match_audit(row))

    @staticmethod
    def _in_choices(sel, got, blank):
        """多选集合命中判定：got 非空且落在 sel 里；got 为空时当 sel 含该字段
        的“空”哨兵（BLANK/NO_TAGGED/NO_TAG）才命中"""
        if got:
            return got in sel
        return blank in sel

    def _match_choice(self, row):
        """品名/标签/备注/状态：各自多选集合内任一命中即可；未选＝不限"""
        f = self._filters
        if f["product"] and not self._in_choices(f["product"], str(row["品名"]).strip(), BLANK):
            return False
        if f["tag"] and not self._in_choices(f["tag"], str(row["标签"]).strip(), NO_TAGGED):
            return False
        if f["remark"] and not self._in_choices(f["remark"], str(row["备注"]).strip(), NO_TAG):
            return False
        if f["status"]:
            st = str(row["状态"]).strip()
            gmap = dict(STATUS_FILTERS)
            if not any(st in gmap[lbl] for lbl in f["status"] if lbl in gmap):
                return False
        return True

    def _match_kw(self, row):
        """搜索关键词命中（品名/编号/脚本/提示词/口播文案/备注）；未填关键词视为命中"""
        kw = self.ed_search.text().strip().lower()
        if not kw:
            return True
        hay = " ".join([str(row["品名"]), str(row["编号"]), str(row["标签"]),
                        str(row["脚本"]),
                        str(row["提示词"]), str(row["口播文案"]),
                        str(row["备注"])]).lower()
        return kw in hay

    def _match_date(self, row):
        f = self._filters
        if f["date_on"]:
            d = str(row["更新时间"])[:10]
            if not d:
                return False
            if not (f["date_start"] <= d <= f["date_end"]):
                return False
        return True

    def _match_audit(self, row):
        """审片多选：只看「可用/不可用」任一命中的任务（未选＝不限）"""
        sel = self._filters["audit"]
        if not sel:
            return True
        return str(row.get("审核") or "").strip() in sel

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
    def _sync_kol_combo(self):
        """KOL 下拉跟随产品中心变化：内容没变就不动（每 2 秒 clear 会冲掉正
        展开的弹层，也会白白重建）；重建时保留当前选择"""
        names = ["不使用"] + product_store.kol_names()
        if names == self._kol_items:
            return
        self._kol_items = names
        cur_kol = self.cb_kol.currentText()
        self.cb_kol.blockSignals(True)
        self.cb_kol.clear()
        self.cb_kol.addItems(names)
        idx = self.cb_kol.findText(cur_kol)
        self.cb_kol.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_kol.blockSignals(False)

    def _view_signature(self):
        """视图状态指纹：页码/每页/排序/搜索/筛选/聚集/失败标红——这些变了
        才需要整表重建。与数据指纹分开：用它区分「数据刷新」与「换了一份列表」"""
        f = self._filters
        filt = tuple(sorted(
            (k, tuple(sorted(v)) if isinstance(v, set) else v)
            for k, v in f.items()))
        order = (self.table.horizontalHeader().sortIndicatorOrder()
                 if self._user_sort_col is not None else None)
        return (self._page, self._page_size(), self._user_sort_col, order,
                self.ed_search.text(), filt,
                tuple(sorted(self._gather_ids)),
                tuple(sorted(self._failed_ids)))

    def _update_demo_btn(self):
        """「清除演示数据」按钮可见性：仅当库里真有演示任务、且尚未清除过时显示。"""
        try:
            from store import demo_seed
            show = bool(demo_seed.demo_present()) and not app_state.get("demo_cleared")
        except Exception:
            show = False
        self.b_demo.setVisible(show)

    def _clear_demo_tasks(self):
        """确认后删除全部演示任务（demo=1）；置 demo_cleared 标、隐藏按钮，以后不再出现。"""
        if QMessageBox.question(
                self, "清除演示数据",
                "将删除打包自带的全部演示任务（真实任务不受影响）。\n"
                "清除后本按钮不再出现，确定继续吗？"
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            from store import demo_seed
            n = demo_seed.clear_demo()
        except Exception as e:
            QMessageBox.warning(self, "清除失败", str(e))
            return
        app_state.set_value("demo_cleared", True)
        self.b_demo.setVisible(False)
        self.refresh(force=True)
        QMessageBox.information(self, "已清除", f"已删除 {n} 条演示任务。")

    def refresh(self, force=False):
        """刷新表格（定时器每 2 秒调用；force=True 是 F5 手动强制）

        数据与视图都没变就整段跳过：不清表、不重建、不动滚动条——
        2 秒定时刷不过是「看看有没有新变化」，没变化时连重绘都不该发生。"""
        if getattr(self, "_tag_editor", None) is not None:
            return                # 内联标签下拉开着：先别重建表格，否则会冲掉编辑器
        if not self.hover.pinned:            # 定时刷新不打扰正在阅读的钉住浮层
            self.hover.hide()
        self._sync_kol_combo()
        actives = REG.active()
        view_sig = self._view_signature()
        data_sig = (task_store.ui_signature(),
                    tuple(sorted((t["job_id"], t["status"], int(t["progress"] or 0))
                                 for t in actives)))
        if not force and view_sig == self._last_view_sig \
                and data_sig == self._last_data_sig:
            # 库与内存在途态都没变：表格原样保留，只把「执行全部（n）」
            # 与在途概要这类轻量文字刷一遍（含“跑完”的完成提示检测）
            self._update_run_summary(actives)
            return
        view_changed = view_sig != self._last_view_sig
        self._last_view_sig = view_sig
        self._last_data_sig = data_sig
        # 数据刷新（轮询落库）先记下滚动位置、重建完还回去（清行会把滚动条
        # 归零）；翻页/改筛选/排序是新的一份列表，从顶部看起
        scroll_top = 0 if view_changed else self.table.verticalScrollBar().value()

        df = task_store.list_tasks_df()
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
        has_bar = False
        self.table.setUpdatesEnabled(False)   # 重建期挂起重绘：几百个格子一次画完
        try:
            self.table.setRowCount(0)
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
        finally:
            self.table.setUpdatesEnabled(True)
        self.table.setSortingEnabled(True)
        if sort_col is not None and sort_col >= 0:
            self.table.sortItems(sort_col, sort_order)
        self._syncing = False
        # 滚动位置显式还原：同步重建不会天然归零（实测同行数重建后 value 不变），
        # 数据刷新时原样还回；列表变短时 Qt 会把 value 往回压（clamp），这里再钉一次。
        # 必须无条件执行——否则翻页/筛选时新列表会停在上一页的滚动位置。
        self.table.verticalScrollBar().setValue(scroll_top)
        # 有进度条行才开移动画定时器，平时完全静默
        self.table.itemDelegate().set_anim_enabled(has_bar)

        self.lbl_count.setText(f"显示 {len(self._filtered)}/{len(df)} 条")
        self._update_kpis(df, actives)                  # 顶部 KPI 概览跟全量任务走（不受筛选影响）
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
                            "按住鼠标在表上拖出框，框到的行会取反：没勾的勾上、已勾的取消")
        elif c == COL_PID:
            item.setData(Qt.ItemDataRole.DisplayRole, tid)
        elif c == COL_NUM:
            item.setText(str(row["编号"]))
        elif c == COL_PRODUCT:
            item.setText(str(row["品名"]))
        elif c == COL_TAG:
            tg = str(row["标签"])
            item.setText(tg)
            item.setToolTip((tg or "（双击就地下拉选标签，归档时按它分文件夹；可先勾选多个再批量打）")
                            + "\n标签词库在「设置 → 🏷 内容标签」里维护")
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
            # 标记要在列表里看得见：不能要求使用者自己去认文件名后缀
            # 加个图标 + 底色，一眼就知道哪些已经在待清理名单里
            audit = str(row.get("审核") or "")
            if audit == "不可用":
                item.setText("⛔ " + item.text())
                item.setBackground(QColor("#FFF1F0"))
                item.setForeground(QColor("#D94A43"))
                item.setToolTip(out_path + "\n审片：已标不可用（右键可取消标记，"
                                          "或点工具栏「🧹 清理不可用」）")
            elif audit == "可用":
                item.setText("✅ " + item.text())
                item.setForeground(QColor("#00A870"))
                item.setToolTip(out_path + "\n审片：已标可用（文件名上不另外加记号）")
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
        """把框到的行【取反】：没勾的勾上，已经勾上的取消勾选。

        由 eventFilter 在确认「真的拖动过鼠标」后调用，不挂 itemSelectionChanged：
        点勾选框取消勾选时该行也会被视为选中，会被反手又勾回去。
        只增不减的旧写法逼着用户「框错了只能一行行点回去」或去按「清除选择」，
        而框选本身就像 Excel 的拖选——再框一次应当是把这批撤掉。
        完事清掉 Qt 高亮：用户真正依赖的是勾选框（跨页保留、驱动批量操作）。"""
        if self._syncing:
            return
        rows = {idx.row() for idx in self.table.selectedIndexes()}
        self._syncing = True
        for r in rows:
            it = self.table.item(r, CHECK_COL)
            if it is None:
                continue
            tid = it.data(Qt.ItemDataRole.UserRole)
            if it.checkState() == Qt.CheckState.Checked:
                it.setCheckState(Qt.CheckState.Unchecked)
                if tid is not None:
                    self._selected_ids.discard(tid)
            else:
                it.setCheckState(Qt.CheckState.Checked)
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

    # ============ 易用性增强：参数常驻 / 运行总览 / 快捷键 / 首次提示 ============
    def _update_params_label(self):
        """E1：把工具栏当前值常驻显示，改哪个下拉立刻更新"""
        dur = 2 + self.cb_duration.currentIndex()
        steps = self.cb_steps.currentText()
        kol = "不使用KOL" if self.cb_kol.currentIndex() == 0 else self.cb_kol.currentText()
        self.lbl_params.setText(f"本次默认：{dur}秒 · {steps}步 · {kol}")
        self._params_an.stop()
        self._params_an.start()      # 淡入脉冲：参数一变就把视线拉过来

    def _update_kpis(self, df, actives):
        """顶部指标带：总任务/待执行/进行中/成功/失败，口径与表格一致。

        吃全量 df（不按当前筛选过滤），否则一筛就“只剩几条”会让人误以
        为任务变少了；「进行中」用内存 REG 的实时在途数，与状态列改写同口径。"""
        n_ok = n_fail = n_pending = 0
        for _, row in df.iterrows():
            s = str(row["状态"]).strip()
            if s in ("completed", "succeeded"):
                n_ok += 1
            elif s in ("failed", "error", "timeout"):
                n_fail += 1
            elif not s:
                n_pending += 1
        self.k_total.set_value(len(df))
        self.k_pending.set_value(n_pending)
        self.k_run.set_value(len(actives))
        self.k_ok.set_value(n_ok)
        self.k_fail.set_value(n_fail)

    def _update_run_summary(self, actives):
        # A2：把「执行全部」的口径写成实时数量（当前筛选下填了提示词的）
        todo = sum(1 for _, row in self._filtered if str(row["提示词"]).strip())
        self.b_runall.setText(f"⏩ 执行全部（{todo}）")
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
                ("F5", lambda: self.refresh(force=True)),
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
             "⏩ 执行全部＝跑当前筛选下所有「填了提示词、还没成功跑过」的任务，会先弹确认"),
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
        self._empty_title.setStyleSheet(tokenize("font-size:16px;font-weight:bold;color:#1F2329;"))
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
                         "再点【▶ 执行选中】；赶时间可点【⏩ 执行全部】。"),
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
        # 内联标签下拉：焦点离开就收起（正在看下拉弹层里的选项不算离开）
        if getattr(self, "_tag_editor", None) is not None and obj is self._tag_editor \
                and e.type() == QEvent.Type.FocusOut:
            QTimer.singleShot(0, self._maybe_close_tag_editor)
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
            # 空格与单击同路由：文本列弹全文预览，输出列播视频（鼠标停哪敲哪，
            # 不用绕到键盘去点鼠标）；其余列本来就不响应，保持一致
            self._on_cell_click(*self._hover_cell)
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
        elif c == COL_TAG:
            self._start_tag_editor(r)  # 双击就地弹下拉选标签，不另开对话框
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

    def _edit_tag(self, r):
        """选标签：只能从词库挑（保证归档目录名规范）；先勾选多个就一次批量打。

        选「（未打标）」＝清除标签。词库不够用去「设置 → 🏷 内容标签」新增。
        标签不参与执行/迭代/重跑判定，只用于筛选与归档分组。"""
        tids = sorted(self._selected_ids) or [self._row_tid(r)]
        tids = [t for t in tids if t is not None]
        if not tids:
            return
        single = len(tids) == 1
        cur = ((task_store.get_task(tids[0]) or {}).get("tag") or "") if single else ""
        choices = [NO_TAGGED] + tag_lib.load()
        if cur and cur not in choices:            # 库里有但词库已删的旧标签：仍列出
            choices.insert(1, cur)
        start = choices.index(cur) if cur in choices else 0
        text, ok = QInputDialog.getItem(
            self, "选标签" if single else f"选标签（{len(tids)} 个任务）",
            f"任务{tids[0]} 标签：" if single
            else f"一次写入 {len(tids)} 个已勾选任务（选「{NO_TAGGED}」＝清除）：",
            choices, start, False)
        if not ok:
            return
        new = "" if text == NO_TAGGED else text
        for tid in tids:
            task_store.update_row(tid, **{"标签": new})
        self.refresh()

    def _start_tag_editor(self, r):
        """双击标签格：就地摆一个下拉（不另开对话框），从词库选；先勾了多个就一次批量打。"""
        if self._tag_editor is not None:
            self._close_tag_editor()
        item = self.table.item(r, COL_TAG)
        if item is None:
            return
        tid = item.data(Qt.ItemDataRole.UserRole)
        cur = (task_store.get_task(tid) or {}).get("tag") or "" if tid else ""
        choices = [NO_TAGGED] + tag_lib.load()
        if cur and cur not in choices:              # 旧标签虽已出词库仍列出，别悄悄丢
            choices.insert(1, cur)
        combo = QComboBox(self.table)
        combo.addItems(choices)
        combo.setCurrentIndex(choices.index(cur) if cur in choices else 0)
        combo._row = r
        combo.setToolTip("选中即写入标签（先勾选多行＝批量打同一标签）；点别处取消")
        combo.activated.connect(lambda _i, cb=combo: self._commit_tag(cb))
        combo.installEventFilter(self)
        self.table.setCellWidget(r, COL_TAG, combo)
        self._tag_editor = combo
        combo.setFocus()
        QTimer.singleShot(0, combo.showPopup)       # 双击后就地把列表摊开，省一次点击

    def _commit_tag(self, combo):
        r = combo._row
        new = "" if combo.currentText() == NO_TAGGED else combo.currentText()
        item = self.table.item(r, COL_TAG)
        base = item.data(Qt.ItemDataRole.UserRole) if item else None
        tids = sorted(self._selected_ids) or ([base] if base else [])
        tids = [t for t in tids if t is not None]
        self._tag_editor = None
        self.table.removeCellWidget(r, COL_TAG)
        for tid in tids:
            task_store.update_row(tid, **{"标签": new})
        self.refresh()

    def _close_tag_editor(self):
        combo = self._tag_editor
        self._tag_editor = None
        if combo is not None:
            self.table.removeCellWidget(combo._row, COL_TAG)   # 撤下编辑器，露出原标签格

    def _maybe_close_tag_editor(self):
        combo = self._tag_editor
        if combo is None or combo.hasFocus() or combo.view().isVisible():
            return                       # 焦点回到自身或正在看下拉弹层：不算离开
        self._close_tag_editor()

    def _cell_path(self, r):
        """这一行正在展示/播放的那个成品文件（多产物只取第一个）"""
        item = self.table.item(r, COL_OUT)
        return item.data(Qt.ItemDataRole.UserRole + 2) if item else ""

    def _play_video(self, r):
        path = self._cell_path(r)
        if not path:
            QMessageBox.information(self, "提示", "该任务还没有输出视频")
            return
        if not Path(path).exists():
            QMessageBox.warning(self, "文件不存在", f"找不到视频文件：\n{path}")
            return
        # allow_mark：成品才给审片按钮（素材库预览不给，不误改素材名）
        self._player = VideoPlayerDialog(self, path, allow_mark=True)
        # 标不可用会改名，路径变了不刷新就还会指着老名字
        self._player.marked.connect(lambda *_: self.refresh())
        self._player.show()

    def _open_location(self, r):
        path = self._cell_path(r)
        if path and Path(path).exists():
            reveal_in_folder(path)
        else:
            QMessageBox.information(self, "提示", "输出文件不存在或已被移动")

    def _copy_video(self, r):
        """把成品文件本身放进系统剪贴板（粘到微信/钉钉就是发附件）"""
        path = self._cell_path(r)
        if not path:
            QMessageBox.information(self, "提示", "该任务还没有输出视频")
            return
        ok, msg = copy_paths_to_clipboard([path])
        if ok:
            self.lbl_tip.setText(msg)
        else:
            QMessageBox.warning(self, "复制失败", msg)

    def _mark(self, r, mark):
        """在列表上直接改标记（不用先打开播放器）

        正在播放器里看这条时去改名，Windows 上会因为文件被占用而失败，
        改名重试三次都不行就把原因原话告诉使用者。"""
        path = self._cell_path(r)
        if not path:
            QMessageBox.information(self, "提示", "该任务还没有输出视频")
            return
        ok, new_path, msg = output_mark.set_mark(path, mark)
        if not ok:
            QMessageBox.warning(self, "标记失败", msg)
            return
        self.lbl_tip.setText(msg)
        self.refresh()

    def _cleanup_bad(self):
        """一键清理：把所有「不可用」的成品移到回收站（不真删）"""
        items = output_mark.collect_bad()
        alive = [i for i in items if i["exists"]]
        gone = len(items) - len(alive)
        if not items:
            QMessageBox.information(
                self, "没有待清理的成品",
                "播放视频时点「👎 不可用」就会进待清理名单")
            return
        mb = sum(i["size"] for i in alive) / 1024 / 1024
        names = "\n".join("  · " + Path(i["path"]).name for i in alive[:8])
        if len(alive) > 8:
            names += f"\n  …… 另外 {len(alive) - 8} 个"
        text = (f"共 {len(alive)} 个「不可用」成品，合计 {mb:.0f} MB：\n{names}"
                if alive else "没有现存的文件（都已经被手工删过了）")
        if gone:
            text += f"\n\n另有 {gone} 条标记的文件已经不在了，顺手清掉标记"
        text += "\n\n会移到【回收站】，误清了还能从回收站找回来；\n"
        text += "任务与执行记录不会被删。确定清理？"
        if QMessageBox.question(self, "清理不可用成品", text) \
                != QMessageBox.StandardButton.Yes:
            return
        st = output_mark.cleanup()
        msg = f"已移到回收站 {len(st['trashed'])} 个"
        if st["missing"]:
            msg += f"，顺便清掉 {len(st['missing'])} 条空标记"
        if st["failed"]:
            msg += f"；{len(st['failed'])} 个失败：{st['failed'][0][1]}"
        self.lbl_tip.setText(msg)
        self.refresh()

    def _purge_unapproved(self):
        """一键清理未批准：只保留标了「可用」的成品，其余连文件带任务一起删。

        与「清理不可用」相反、也更狠：没标可用的成品文件一律进回收站，
        清完没有可用成品残留的任务行也一并删（任务行可撤销，文件从回收站可捞回）。
        因影响大，需手动输入“确认删除”四个字才执行。"""
        plan = task_store.purge_unapproved_plan()
        if not plan:
            QMessageBox.information(
                self, "没有待清理的成品",
                "现存成品都已标「可用」（或还没有已出片的成品）。\n"
                "在视频上点「👍 可用」保留，其余的会在这里被清掉。")
            return
        n_files = sum(len(it["drop"]) for it in plan)
        n_tasks = sum(1 for it in plan if it["drop_task"])
        mb = sum(it["size"] for it in plan) / 1024 / 1024
        preview = "\n".join(
            f"  · {it['product']}：删 {len(it['drop'])} 个"
            + ("（连任务一起删）" if it["drop_task"] else "（保留任务）")
            for it in plan[:10])
        if len(plan) > 10:
            preview += f"\n  …… 另外 {len(plan) - 10} 个任务"
        text = (f"将删除 {n_files} 个没标「可用」的成品（约 {mb:.0f} MB），"
                f"其中 {n_tasks} 个任务清完没有可用成品，任务行一并删除。\n"
                f"{preview}\n\n"
                "· 成品文件移到【回收站】，误清可从回收站捞回；\n"
                "· 被删的任务行可在提示条 6 秒内点「撤销」恢复；\n"
                "· 没出片的任务（在跑/失败）不受影响；今日成品同样会清。\n\n"
                "此操作影响较大，请输入【确认删除】四个字以继续：")
        word, ok = QInputDialog.getText(self, "一键清理未批准成品", text)
        if not ok:
            return
        if word.strip() != "确认删除":
            self.lbl_tip.setText("已取消：未输入「确认删除」")
            return
        # 删前抓快照（只快照会被整条删的任务），供 6 秒内撤销
        rows = [task_store.get_task(it["id"]) for it in plan if it["drop_task"]]
        rows = [r for r in rows if r]
        st = task_store.purge_unapproved(plan)
        msg = f"🧹 已清理 {len(st['trashed'])} 个未批准成品（移到回收站）"
        if st["tasks_deleted"]:
            msg += f"，删除 {len(st['tasks_deleted'])} 个任务"
        if st["tasks_kept"]:
            msg += f"，{st['tasks_kept']} 个任务保留可用成品"
        self.refresh()
        if st["failed"]:
            QMessageBox.warning(
                self, "部分清理失败",
                msg + f"；{len(st['failed'])} 个文件没删成：{st['failed'][0][1]}\n"
                      "（删除失败的成品对应任务已保守保留，可重试）")
        elif st["tasks_deleted"]:
            self._show_toast(msg, action_text="↶ 撤销任务",
                             on_action=lambda rs=rows: self._undo_delete(rs),
                             color="#F54A45", msec=6000)
        else:
            self.lbl_tip.setText(msg)

    def _archive(self):
        """批量归档：把审片标了【可用】的成品搬进【成品库 / 素材库】。

        先算清单（只收可用、且还在生成目录里没进过库的那些），弹目的地二选一，
        再按【产品 / 标签】两级子文件夹落进所选库；搬完同步输出路径与审片标记，
        归档进素材库的额外登记一行 material_clips（见 processors.archiver）。"""
        items = archiver.plan()
        if not items:
            QMessageBox.information(
                self, "没有可归档的成品",
                "归档只收【标了「可用」】且还在生成目录里的成品。\n"
                "请先在任务列表把满意的作品标「👍 可用」，或这些可用成品已进过库。")
            return
        # 目的地二选一：成品库（可直接发布）/ 素材库（混剪取料的片段）
        dest, ok = QInputDialog.getItem(
            self, "归档目的地",
            f"将把 {len(items)} 个【可用】成品归档到哪个库？\n"
            "（按 产品 / 标签 两级子文件夹归类）",
            [archiver.DEST_LABEL[archiver.DEST_OUTPUT],
             archiver.DEST_LABEL[archiver.DEST_MATERIAL]], 0, False)
        if not ok:
            return
        dest_key = (archiver.DEST_MATERIAL if dest == archiver.DEST_LABEL[archiver.DEST_MATERIAL]
                    else archiver.DEST_OUTPUT)
        summ = archiver.summary(items)
        lines = "\n".join(f"　{p} ／ {t}：{n} 条" for p, t, n in summ[:12])
        if len(summ) > 12:
            lines += f"\n　…… 另外 {len(summ) - 12} 组"
        if QMessageBox.question(
                self, "批量归档",
                f"将把 {len(items)} 个【可用】成品归档到【{dest}】，按【产品 / 标签】分子文件夹：\n"
                f"{lines}\n\n只搬位置、不改文件名；任务输出路径与审片标记会同步更新。\n"
                "确定归档？") != QMessageBox.StandardButton.Yes:
            return
        st = archiver.run(items, dest=dest_key)
        msg = f"📦 已归档 {len(st['moved'])} 个可用成品到【{dest} / 产品 / 标签】"
        if st["failed"]:
            msg += f"；{len(st['failed'])} 个失败：{st['failed'][0][1]}"
            QMessageBox.warning(self, "部分归档失败", msg)
        else:
            self.lbl_tip.setText(msg)
        self.refresh()

    def _subtitle_batch(self):
        """字幕处理：对勾选的成品视频批量生成字幕 / 抽帧检测 / 高亮 / 烧录。

        取当前表里勾选行且有成品文件的；先弹字幕选项收参数（默认勾选“检测已有字
        幕”），再后台 run_subtitle 跑一批，结束汇总“已生成/已跳过/失败”（不静默）。"""
        rows = [r for r in range(self.table.rowCount())
                if self.table.item(r, CHECK_COL)
                and self.table.item(r, CHECK_COL).checkState() == Qt.CheckState.Checked]
        paths = [self._cell_path(r) for r in rows]
        paths = [p for p in paths if p and Path(p).is_file()]
        if not paths:
            QMessageBox.information(
                self, "提示", "请先勾选含成品文件的任务行（成品需已下载到本地）")
            return
        from gui.dialogs_subtitle import ask_subtitle_options, run_subtitle
        from video_text_tools.subtitle.models import SubtitleOptions
        from core.logger import log
        opts = ask_subtitle_options(self, options=SubtitleOptions(detect=True))
        if opts is None:
            return
        self.lbl_tip.setText(f"🔤 字幕处理中：{len(paths)} 个视频…")

        def done(results):
            skip = sum(1 for r in results if r.ok and "已含字幕" in str(r.message))
            fail = [r for r in results if not r.ok]
            made = sum(1 for r in results if r.ok) - skip
            msg = (f"🔤 字幕：已生成 {made} / 跳过(已含字幕) {skip} / "
                   f"失败 {len(fail)}，共 {len(results)}")
            self.lbl_tip.setText(msg)
            self.refresh()
            if fail:
                detail = "\n".join(f"  · {r.name}：{r.message}" for r in fail[:8])
                if len(fail) > 8:
                    detail += f"\n  …… 另外 {len(fail) - 8} 个"
                QMessageBox.warning(self, "部分字幕处理失败",
                                    f"{msg}\n\n失败明细：\n{detail}")

        run_subtitle(self, paths, opts, on_done=done,
                     on_log=lambda m: log.info("[字幕] %s", m))

    def _on_context_menu(self, pos):
        """表格右键菜单：播放/定位/复制/标记 · 编辑/备注/检测/删除"""
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        r = idx.row()
        path = self._cell_path(r)
        menu = StyledMenu(self)
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
            menu.addAction("📍 快捷定位（打开目录并选中）",
                           lambda: self._open_location(r))
            menu.addAction("📋 复制视频文件（可直接粘贴发送）",
                           lambda: self._copy_video(r))
            menu.addSeparator()
            mark = output_mark.mark_of(path)
            if mark == task_store.MARK_BAD:
                menu.addAction("↩ 取消「不可用」标记（文件名改回去）",
                               lambda: self._mark(r, ""))
            elif mark == task_store.MARK_OK:
                menu.addAction("👎 标记不可用（改名 + 待清理）",
                               lambda: self._mark(r, task_store.MARK_BAD))
                menu.addAction("↩ 取消「可用」标记", lambda: self._mark(r, ""))
            else:
                menu.addAction("👎 标记不可用（改名 + 待清理）",
                               lambda: self._mark(r, task_store.MARK_BAD))
                menu.addAction("👍 标记可用", lambda: self._mark(r, task_store.MARK_OK))
            menu.addSeparator()
        menu.addAction("✎ 编辑任务", lambda: self._edit_current(r))
        n_sel = len(self._selected_ids)
        menu.addAction(("🏷 写备注（已选 " + str(n_sel) + " 个）") if n_sel
                       else "🏷 写备注（当前任务）",
                       lambda: self._edit_remark(r))
        menu.addAction(("🏷 选标签（已选 " + str(n_sel) + " 个）") if n_sel
                       else "🏷 选标签（当前任务）",
                       lambda: self._edit_tag(r))
        menu.addAction(f"📝 批量绑定脚本（{'已选 ' + str(n_sel) + ' 个' if n_sel else '当前 1 个'}）",
                       lambda: self._bind_script(r))
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
        data = TaskDialog.ask(self, {"id": tid, "num": t["num"], "product": t["product"],
                                     "script": t["script"], "prompt": t["prompt"],
                                     "remark": t["remark"],
                                     "prompt_zh": t.get("prompt_zh") or ""})
        if data:
            # 编号不写回：弹窗里已经拿掉输入框，编号建好就不给改（文件命名锚）
            upd = {"品名": data["product"],
                   "脚本": data["script"], "备注": data["remark"]}
            if data["prompt"] != (t["prompt"] or ""):
                # 改了提示词才重置执行态；只改脚本不影响迭代/重跑判定
                upd.update({"提示词": data["prompt"], "状态": "", "运行次数": 0})
            task_store.update_row(tid, **upd)
            # 中文对照：翻完又改了原文的，那份译文就对不上了，清掉比留着骗人强
            task_store.set_prompt_zh(tid, data["prompt_zh"] if data["zh_valid"] else "")
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
            tid = task_store.add_task(data["num"] or (task_store.task_count() + 1),
                                      data["product"], data["prompt"],
                                      script=data.get("script", ""),
                                      remark=data.get("remark", ""))
            # 新建时翻的对照：任务 id 到手才能补写进库
            if tid and data.get("zh_valid"):
                task_store.set_prompt_zh(tid, data["prompt_zh"])
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
                # 原文被批量改了，旧的中文对照就对不上了，同步清掉（留着会骗人）
                task_store.set_prompt_zh(tid, "")
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
