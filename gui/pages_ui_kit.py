"""
gui/pages_ui_kit.py —— 「UI 组件库」交互画廊：分类用 Tab 页，示例按钮带悬停/点击动效、点击弹窗说明

设计（按用户口径重做）：
- 分类用 Tab 页：按钮 / 输入框 / 选择框 / Form 表单 / 数字·日期 / 进度·滑块 / 表格·卡片 / 弹层 / 设计令牌，
  一类一个页签，一页看一类，互不干扰；后续逐类调样式也只动对应页签。
- 示例按钮 KitButton 自绘，带真实动效：鼠标移入背景渐亮、按下轻微回弹缩放；点一下弹窗告诉你
  「这是什么变体、干什么用、吃什么令牌」——不在按钮下方堆一行文字（那样阅读体验差）。
- 页里只吃 gui/ui_kit 的 COLORS/RADIUS/SPACE/FONT，改令牌回这里即见效；不吃业务数据、不接库。
入口先摆在侧栏（索引 14，便于整体核对），后续可改为口令/快捷键呼出。
"""
from PySide6.QtCore import (Qt, QDate, QTimer, QPoint, QRect, QSize, QEvent,
                            QRectF, QPointF, Property, QPropertyAnimation,
                            QEasingCurve, QSortFilterProxyModel, QObject)
from PySide6.QtGui import (QColor, QCursor, QPainter, QPen, QBrush,
                           QPainterPath, QFont, QPolygonF, QLinearGradient,
                           QStandardItemModel, QStandardItem, QKeySequence)
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QLineEdit, QPlainTextEdit, QComboBox,
                               QSpinBox, QDoubleSpinBox, QDateEdit, QCheckBox,
                               QRadioButton, QTabWidget, QProgressBar, QSlider,
                               QTableWidget, QTableWidgetItem, QHeaderView,
                               QScrollArea, QFrame, QButtonGroup, QSizePolicy,
                               QMessageBox, QFormLayout, QSplitter, QCalendarWidget,
                               QTreeView, QAbstractItemView, QStyle,
                               QStyleOptionSlider, QDialog, QApplication, QToolTip,
                               QTableWidgetSelectionRange, QStyledItemDelegate)

from gui import ui_kit
from gui.header import page_header, Card, KpiCard
from gui.widgets import FlowLayout
from gui.menus import StyledMenu

C = ui_kit.COLORS

# 复用 gui/kit.py 的共享组件底座（等价重构：画廊仍是「活文档」，视觉/交互完全不变）
from gui.kit import (_section_title, _sub_title, _hint, _contrast_text, _row,
                    _lighten, hover_tip, KitButton, TreeSelect, ModernDateEdit,
                    DateRangePicker, KitSlider, ResizableTextEdit, KitTable,
                    FieldManager, _FlowKpiCard, _round_dialog)


class UiKitPage(QWidget):
    """UI 组件库交互画廊：分类 Tab，示例按钮带动效 + 点击弹窗说明。"""

    _KPI_W = 330   # KPI 卡片固定宽度（不随窗口拉伸）：使默认内容区实宽（约 1470~1630）恰好一行 4 张，最大化再容纳 6 张
    _KPI_H = 80    # KPI 卡片最小高度（内容装不下时卡片自动长高）
    _KPI_GAP = 18  # KPI 卡片行/列间距

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 14, 24, 24)
        lay.setSpacing(12)
        lay.addWidget(page_header(
            "UI 组件库",
            "分类 Tab 页：示例按钮有悬停/点击动效，点一下弹窗告诉你它是什么。"
            "改 gui/ui_kit.py 令牌后回这里核对", icon="🎛"))
        tabs = QTabWidget()
        self.tabs = tabs
        tabs.addTab(self._tab_buttons(), "🔘 按钮")
        tabs.addTab(self._tab_inputs(), "⌨️ 输入框")
        tabs.addTab(self._tab_selection(), "☑️ 选择框")
        tabs.addTab(self._tab_form(), "📝 Form 表单")
        tabs.addTab(self._tab_number_date(), "🔢 数字·日期")
        tabs.addTab(self._tab_progress(), "📊 进度·滑块")
        tabs.addTab(self._tab_data(), "🗂️ 表格·卡片")
        tabs.addTab(self._tab_overlay(), "🪟 弹层")
        tabs.addTab(self._tab_tokens(), "🎨 设计令牌")
        lay.addWidget(tabs, 1)

    # 每个 Tab 一个自带滚动的内容壳
    def _tab_shell(self, hint):
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        inner = QWidget()
        sa.setWidget(inner)
        v = QVBoxLayout(inner)
        v.setContentsMargins(8, 16, 8, 16)
        v.setSpacing(14)
        if hint:
            v.addWidget(_hint(hint))
        return sa, v

    # ================= Tab：按钮 =================
    def _tab_buttons(self):
        sa, v = self._tab_shell(
            "每颗按钮：默认颜色深、鼠标移入变浅、按下轻微回弹缩放；点一下弹窗说明它是什么变体、"
            "干什么用、吃什么令牌。")
        v.addWidget(_sub_title("主色 / 变体"))
        r1 = FlowLayout()
        r1.addWidget(KitButton("主按钮 Primary", kind="primary",
                               desc="主按钮：一个界面里的首要动作（提交 / 保存 / 确定）。\n"
                                    "默认深蓝 primary，悬停变浅 primary_hover，按下 primary_press。"))
        r1.addWidget(KitButton("Ghost 幽灵", kind="ghost", obj_name="GhostBtn",
                               desc="幽灵按钮：次要动作（取消 / 返回）。\n"
                                    "平时白底 + 灰描边，悬停描边与文字转主蓝。"))
        r1.addWidget(KitButton("Danger 危险", kind="danger", obj_name="DangerBtn",
                               desc="危险按钮：不可逆 / 破坏性动作（删除 / 重置 / 移入回收站）。\n"
                                    "统一口径后：默认实心红（深），悬停变浅红。"))
        v.addLayout(r1)

        v.addWidget(_sub_title("胶囊 Chip（可点选，选中填主蓝）"))
        r2 = FlowLayout()
        chip = KitButton("Chip 筛选", kind="chip", obj_name="ChipBtn",
                         desc="筛选胶囊：一键筛选标签。默认 primary_soft 略深、悬停变浅，"
                              "可 checkable，选中填充主蓝。")
        chip.setCheckable(True)
        chip.setChecked(True)
        r2.addWidget(chip)
        v.addLayout(r2)

        v.addWidget(_sub_title("折叠栏 AccHead（整行标题可点，展开 / 收起下方内容）"))
        v.addWidget(self._accordion_demo())

        v.addWidget(_sub_title("侧栏按钮 SideBtn（深色导航里用，悬停转亮蓝）"))
        r2b = FlowLayout()
        r2b.addWidget(KitButton("SideBtn 侧栏按钮", kind="sidebar", obj_name="SideBtn",
                                desc="侧栏深色按钮：无边框深色导航里用，默认深底、悬停转亮蓝。"))
        v.addLayout(r2b)

        v.addWidget(_sub_title("禁用态（不可点，仅供对比配色）"))
        r3 = FlowLayout()
        d1 = KitButton("Disabled 禁用", kind="primary", desc="禁用态：淡灰底弱文字。")
        d1.setEnabled(False)
        d2 = KitButton("Ghost 禁用", kind="ghost", obj_name="GhostBtn", desc="幽灵禁用态：更淡的灰底灰描边。")
        d2.setEnabled(False)
        r3.addWidget(d1)
        r3.addWidget(d2)
        v.addLayout(r3)
        v.addStretch(1)
        return sa

    def _accordion_demo(self):
        """折叠栏真实例：点整行标题→展开 / 收起下方内容区，箭头随之 ▸/▾ 翻转。"""
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        text = " 更多筛选（点标题展开 / 收起）"
        head = KitButton("▸" + text, kind="ghost", obj_name="AccHead", explain=False)
        head.setMinimumWidth(360)
        body = QLabel("展开后的内容区：这里可以放子控件 / 说明文字。\n再点一下标题即可收起。")
        body.setWordWrap(True)
        body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        body.setStyleSheet(
            f"background:{C['bg_field']};color:{C['sub']};"
            f"border:1px solid {C['border_popup']};border-top:none;"
            f"border-bottom-left-radius:{ui_kit.RADIUS['md']}px;"
            f"border-bottom-right-radius:{ui_kit.RADIUS['md']}px;"
            "padding:10px 12px;")
        body.hide()

        def _toggle():
            show = not body.isVisible()
            body.setVisible(show)
            head.setText(("▾" if show else "▸") + text)

        head.clicked.connect(_toggle)
        lay.addWidget(head)
        lay.addWidget(body)
        return host

    # ================= Tab：输入框 =================
    def _tab_inputs(self):
        sa, v = self._tab_shell("输入类：QLineEdit / 多行 QPlainTextEdit / 只读态。聚焦描边转主蓝。")
        v.addWidget(_sub_title("单行 QLineEdit"))
        e1 = QLineEdit()
        e1.setPlaceholderText("空态：占位文字走 palette=weak")
        e1.setMinimumWidth(240)
        e2 = QLineEdit("聚焦试试：边框会变主蓝")
        e2.setMinimumWidth(240)
        e3 = QLineEdit("只读态")
        e3.setReadOnly(True)
        e3.setStyleSheet(f"background:{C['bg_field']};color:{C['weak']};")
        e3.setMinimumWidth(200)
        v.addLayout(_row(e1, e2, e3))
        v.addWidget(_sub_title("多行文本（拖右下角斜纹把手，上下左右自由拉伸；有最小显示区域）"))
        rtext = ResizableTextEdit(min_w=220, min_h=90,
                                  placeholder="把鼠标移到右下角，出现斜纹把手后按住拖动…")
        # 用对齐方式加入：布局不把它拉满，拖把手才能自由改尺寸
        v.addWidget(rtext, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        v.addStretch(1)
        return sa

    # ================= Tab：选择框 =================
    def _tab_selection(self):
        sa, v = self._tab_shell("选择类：下拉 QComboBox / 复选 QCheckBox / 单选 QRadioButton。")
        v.addWidget(_sub_title("下拉 QComboBox"))
        combo = QComboBox()
        combo.addItems(["QComboBox 请选择…", "长一点的选项自动加宽弹层", "由 _ComboTweak 统一修"])
        combo.setMinimumWidth(260)
        v.addLayout(_row(combo))
        v.addWidget(_sub_title("复选 QCheckBox"))
        c2 = QCheckBox("已选中")
        c2.setChecked(True)
        c3 = QCheckBox("禁用")
        c3.setEnabled(False)
        v.addLayout(_row(QCheckBox("默认"), c2, c3))
        v.addWidget(_sub_title("单选 QRadioButton"))
        rg = QButtonGroup(self)
        r1 = QRadioButton("单选 A")
        r2 = QRadioButton("单选 B")
        r2.setChecked(True)
        rg.addButton(r1)
        rg.addButton(r2)
        v.addLayout(_row(r1, r2))
        v.addWidget(_sub_title("多级下拉（一个下拉里带层级树：顶部可搜索，默认收起、点一级展开下一级）"))
        v.addWidget(TreeSelect())
        v.addStretch(1)
        return sa

    # ================= Tab：Form 表单 =================
    def _tab_form(self):
        sa, v = self._tab_shell("Form 表单：QFormLayout 把标签与字段成对排列，底部动作按钮右对齐。")
        card = Card()
        card.v.addWidget(_section_title("示例表单"))
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("品名", QLineEdit("骨胶原维D钙"))
        cat = QComboBox()
        cat.addItems(["保健食品", "医疗器械", "日化"])
        form.addRow("分类", cat)
        form.addRow("标签", QLineEdit("关节, 好物分享"))
        form.addRow("所在地区", TreeSelect())
        note = QPlainTextEdit()
        note.setPlaceholderText("备注（多行）")
        note.setFixedHeight(64)
        form.addRow("备注", note)
        card.v.addLayout(form)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(KitButton("取消", kind="ghost", obj_name="GhostBtn",
                                 desc="取消：关闭表单不提交（次要动作，幽灵钮）。"))
        btns.addWidget(KitButton("保存", kind="primary",
                                 desc="保存：提交表单（首要动作，主按钮）。点它就是在演示这颗按钮。"))
        card.v.addLayout(btns)
        v.addWidget(card)
        v.addStretch(1)
        return sa

    # ================= Tab：数字·日期 =================
    def _tab_number_date(self):
        sa, v = self._tab_shell(
            "数字与日期。日期两种：① 区间选择器（点近7天=7天前~今天一个范围）；"
            "② 单选日期（不带快捷）。")
        v.addWidget(_sub_title("数字 QSpinBox / QDoubleSpinBox"))
        spin = QSpinBox()
        spin.setRange(0, 999)
        spin.setValue(28)
        dsp = QDoubleSpinBox()
        dsp.setRange(0, 10)
        dsp.setValue(3.5)
        v.addLayout(_row(spin, dsp))
        v.addWidget(_sub_title("样式①：日期区间（字段式按钮弹日历，左侧近 N 天快捷选一段范围）"))
        v.addLayout(_row(DateRangePicker()))
        v.addWidget(_sub_title("样式②：单选日期（原生 QDateEdit 换皮，保留键盘输入 + 右侧日历弹层）"))
        v.addLayout(_row(ModernDateEdit(QDate(2026, 9, 30))))
        v.addStretch(1)
        return sa

    # ================= Tab：进度·滑块 =================
    def _tab_progress(self):
        sa, v = self._tab_shell("进度与滑块：QProgressBar / QSlider。点「走一遍进度」看填充动画。")
        self._pb = QProgressBar()
        self._pb.setRange(0, 100)
        self._pb.setValue(60)
        run = KitButton("▶ 走一遍进度", kind="primary",
                        desc="演示：驱动进度条从 0 走到 100，展示 QProgressBar 填充动效。",
                        explain=False)
        run.clicked.connect(self._run_progress)
        v.addLayout(_row(self._pb, run))
        v.addWidget(_sub_title("滑块 QSlider（拖动时浮现百分比气泡：水平在拇指上方，垂直在拇指右侧，松手即隐）"))
        hrow = QHBoxLayout()
        hrow.setSpacing(24)
        sl = KitSlider(Qt.Orientation.Horizontal)
        sl.setRange(0, 100)
        sl.setValue(40)
        hrow.addWidget(sl, 1)
        sv = KitSlider(Qt.Orientation.Vertical)
        sv.setRange(0, 100)
        sv.setValue(65)
        hrow.addWidget(sv, 0, Qt.AlignmentFlag.AlignHCenter)
        v.addLayout(hrow)
        v.addStretch(1)
        return sa

    def _run_progress(self):
        self._pb.setValue(0)
        if not hasattr(self, "_timer"):
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
        self._timer.start(24)

    def _tick(self):
        val = self._pb.value() + 2
        self._pb.setValue(val)
        if val >= 100:
            self._timer.stop()

    # ================= Tab：表格·卡片 =================
    def _tab_data(self):
        sa, v = self._tab_shell("数据展示：表格 QTableWidget / KPI 卡片 KpiCard / 字段胶囊。")
        v.addWidget(_sub_title("表格（列标题右键→多级菜单：居中调整 / 顺序调整 / 字段格式，二级菜单里带「默认展开 / 取消展开」文字开关（无对钩）：点开就把该组内容直接平铺展开（右键即见全部子项、不用再悬停），可多组同时展开、互不顶掉；拖表头边改列宽；双击在单元格内直接编辑；选中 Ctrl+C 复制后出现 Excel 式行走虚线框、按 Esc 取消；默认每格一行，超出用 … 省略、悬停看完整内容，开自动换行则整格展开）"))
        tb = KitTable(3, 3, ["品名", "状态", "数值"])
        data = [("骨胶原维D钙·关节好物分享长文本换行示例", "成功", "128"),
                ("关节贴", "运行中", "32"), ("钙片", "失败", "—")]
        A = Qt.AlignmentFlag
        for r, row in enumerate(data):
            for c, val in enumerate(row):
                it = QTableWidgetItem(val)
                it.setToolTip(val)          # 默认一行时悬停看完整内容
                if c == 1:
                    it.setForeground(QColor(ui_kit.STATUS_COLORS.get(val, C["text"])))
                    it.setTextAlignment(A.AlignCenter)
                elif c == 2:
                    it.setTextAlignment(A.AlignRight | A.AlignVCenter)
                tb.setItem(r, c, it)
        tb.setMinimumHeight(180)
        v.addWidget(tb)

        v.addWidget(_sub_title("字段管理 FieldManager（固定尺寸；左侧按分类勾选即加入右侧；右侧流式排列，"
                               "拖动胶囊调序，落点会闪烁光标提示插入位，点 × 移除；字段多时滚轮上下看全；"
                               ".order/.hidden 实时可取）"))
        v.addWidget(self._field_manager_demo())

        v.addWidget(_sub_title("KPI 卡片 KpiCard（吃 KPI_PALETTE；右侧空白放小折线 sparkline）"))
        # 定宽 KPI 带：每卡固定 _KPI_W 宽、不拉伸；普通窗口一行 4 张，最大化行宽能容纳
        # 6 张则一行 6 张，卡片数超过一行自动折到下一行（FlowLayout 按实际可用宽换行）。
        # 后期要做“上板/下板”直接再调一次 _kpi_band(...) 即可。
        kpi_specs = (
            ("总执行", "1,280", True, [3, 5, 4, 6, 8, 7, 9, 12]),
            ("成功", "1,150", True, [2, 4, 3, 5, 6, 6, 8, 9]),
            ("失败", "96", False, [5, 4, 6, 3, 2, 3, 1, 1]),
            ("成功率", "89.8%", True, [70, 74, 72, 80, 83, 85, 88, 90]),
            ("视频素材", "348", True, [12, 18, 15, 22, 19, 25, 24, 30]),
            ("今日发布", "27", True, [1, 2, 1, 3, 2, 4, 3, 5]),
        )
        v.addWidget(self._kpi_band(kpi_specs))

        v.addWidget(_sub_title("字段胶囊 · 扁平款（吃 FIELD_COLORS，圆角）"))
        pairs = (("seq", "编号"), ("stem", "原名"), ("product", "品名"),
                 ("tag", "标签"), ("date", "日期"), ("time", "时刻"))
        flat_host = QWidget()
        fflow = FlowLayout(flat_host, hgap=8, vgap=8)
        fflow.setContentsMargins(0, 0, 0, 0)
        for key, label in pairs:
            fflow.addWidget(self._token_chip(key, label, "flat"))
        v.addWidget(flat_host)
        v.addWidget(_sub_title("字段胶囊 · 渐变实物款（同批色，上下渐变带一点立体感，也圆角）"))
        grad_host = QWidget()
        gflow = FlowLayout(grad_host, hgap=8, vgap=8)
        gflow.setContentsMargins(0, 0, 0, 0)
        for key, label in pairs:
            gflow.addWidget(self._token_chip(key, label, "grad"))
        v.addWidget(grad_host)
        v.addStretch(1)
        return sa

    def _kpi_band(self, specs):
        """一排定宽 KPI 卡片（供上板/下板等多条复用）。
        卡片固定宽 _KPI_W、不随窗口拉伸；用 FlowLayout 按实际可用宽排：一行能塞几张就塞几张，
        放不下自动折到下一行——普通窗口一行 4 张、最大化后行宽能容纳 6 张则一行 6 张。
        specs = [(名称, 数值, 是否上涨 up, spark 序列), ...]。"""
        host = QWidget()
        flow = FlowLayout(host, hgap=self._KPI_GAP, vgap=self._KPI_GAP)
        flow.setContentsMargins(0, 0, 0, 0)
        for i, (name, val, up, spark) in enumerate(specs):
            k = _FlowKpiCard(self._KPI_W, self._KPI_H, name, "📈",
                             ui_kit.KPI_PALETTE[i % len(ui_kit.KPI_PALETTE)])
            k.set_value(val, "较昨日 +3.2%", up=up)
            k.set_sparkline(spark)
            # 不再把 spark 的最小宽压成 0：那是卡片很窄（~240）时才需要的撑宽手段。
            # 卡片定宽到 330 已装得下 图标+文字+96px 折线；若再压 0，文字列会把空间全占走、
            # 把折线挤成 0 宽（就是“默认折线没了”的根因）。
            flow.addWidget(k)
        return host

    def _token_chip(self, key, label, style="flat"):
        acc = ui_kit.FIELD_COLORS.get(key, C["primary"])
        chip = QLabel(label)
        chip.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        r = ui_kit.RADIUS["pill"]           # 胶囊圆角：两款都圆
        chip.setMinimumHeight(26)
        if style == "grad":
            top = _lighten(acc, 0.24)
            chip.setStyleSheet(
                f"background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
                f"stop:0 {top},stop:1 {acc});"
                f"color:{C['on_primary']};border:1px solid {acc};"
                f"border-radius:{r}px;padding:5px 14px;")
            chip.setToolTip(f"字段 {key}｜渐变实物款｜{acc}")
        else:
            chip.setStyleSheet(
                f"background:{ui_kit.rgba(acc, 0.14)};"
                f"border:1px solid {ui_kit.rgba(acc, 0.45)};color:{C['text']};"
                f"border-radius:{r}px;padding:5px 14px;")
            chip.setToolTip(f"字段 {key}｜扁平款｜主题色 {acc}")
        return chip

    def _field_manager_demo(self):
        """字段管理组件演示：直接嵌一个 FieldManager，下方实时回显当前 .order/.hidden。
        数据用与任务中心同构的样例字段（分类 + 逻辑号），只读展示、不回写任何表。"""
        host = Card()
        # 字段放足 20 个、右侧默认选中一多半：直观验证「右侧很多时滚轮能否看全」
        fields = [
            (1, "任务ID", "标识"), (2, "编号", "标识"), (3, "账号", "标识"),
            (4, "KOL", "标识"), (5, "批次号", "标识"),
            (6, "品名", "内容"), (7, "标签", "内容"), (8, "备注", "内容"),
            (9, "提示词", "内容"), (10, "脚本", "内容"), (11, "口播文案", "内容"),
            (12, "状态", "运行"), (13, "开始时间", "运行"), (14, "生成用时", "运行"),
            (15, "重试次数", "运行"), (16, "错误信息", "运行"),
            (17, "输出路径", "交付"), (18, "分辨率", "交付"), (19, "时长", "交付"),
            (20, "发布时间", "交付"),
        ]
        cats = [("标识", [1, 2, 3, 4, 5]), ("内容", [6, 7, 8, 9, 10, 11]),
                ("运行", [12, 13, 14, 15, 16]), ("交付", [17, 18, 19, 20])]
        fm = FieldManager(fields, order=[1, 4, 6, 9, 10, 12, 13, 14, 17, 19, 20],
                          hidden=set(range(1, 21)) - {1, 4, 6, 9, 10, 12, 13, 14, 17, 19, 20},
                          categories=cats)
        status = QLabel("")
        status.setObjectName("PageTip")
        status.setWordWrap(True)

        def _refresh(*_a):
            labels = [fm.label_of(i) for i in fm.order]
            hid = [fm.label_of(i) for i in sorted(fm.hidden)]
            status.setText("当前顺序：" + " · ".join(labels)
                           + ("\n已隐藏：" + " · ".join(hid) if hid else "\n已隐藏：无"))
        fm.changed.connect(_refresh)
        _refresh()
        host.v.addWidget(fm, 1)
        host.v.addWidget(status)
        return host

    # ================= Tab：弹层 =================
    def _tab_overlay(self):
        sa, v = self._tab_shell(
            "浮层：右键菜单 StyledMenu / 悬停 Tooltip / 对话框 QMessageBox。这几颗点了就是真的弹，"
            "所见即所得；tooltip 全站接管成单例圆角白卡（淡灰软阴影），同一时刻只会弹一个。")
        m = KitButton("🖱 点开右键菜单", kind="ghost", obj_name="GhostBtn",
                      desc="演示：弹出全站统一的 StyledMenu 右键菜单（真圆角 + 自绘软阴影）。",
                      explain=False)
        m.clicked.connect(self._show_menu_demo)
        tipb = KitButton("☝ 悬停看 Tooltip", kind="ghost", obj_name="GhostBtn",
                         desc="演示：悬停弹出圆角 tooltip 气泡。", explain=False)
        # 只留一份文案：hover_tip 350ms 即弹，内容就用 widget 自己的 toolTip；
        # 原生 tooltip 已被 _TipRouter 拦截转投同一颗气泡，不会再“第二个盖第一个”
        tipb.setToolTip("多行 tooltip 示例：\n第一行说明，第二行补充；\n"
                        "白底深字、真圆角、淡灰软阴影。")
        hover_tip(tipb)
        dlg = KitButton("🗨 弹出对话框", kind="primary",
                        desc="演示：弹出一颗真圆角 QMessageBox（顶层去框 + 逐像素透明）。", explain=False)
        dlg.clicked.connect(self._demo_dialog)
        v.addLayout(_row(m, tipb, dlg))
        v.addStretch(1)
        return sa

    def _show_menu_demo(self):
        # 对标飞书：菜单项走“纯文字 + 行高拉开 + 悬停淡蓝”，不再用一堆彩色 emoji
        # （emoji 字形跨平台不一致、风格显旧）。破坏性项放分隔线下方、置灰红字。
        menu = StyledMenu(self)
        act = menu.addAction("移入回收站")
        act.setToolTip("移入回收站：软件内回收站，可随时还原")
        menu.addAction("设置显示名")
        menu.addAction("批量重命名")
        menu.addSeparator()
        undo = menu.addAction("撤销上一步")
        undo.setEnabled(False)          # 置灰项：对比一下禁用态配色
        menu.exec(QCursor.pos())

    def _demo_dialog(self):
        box = QMessageBox(self.window())
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("组件库 · 对话框")
        box.setText("这是一颗真圆角 QMessageBox：顶层去框 + 逐像素透明，圆角外不再漏方底。")
        _round_dialog(box)
        box.exec()

    # ================= Tab：设计令牌 =================
    def _tab_tokens(self):
        sa, v = self._tab_shell(
            "设计令牌：都来自 gui/ui_kit.py 的 COLORS / FONT / SPACE / RADIUS。改一处令牌，全站生效。")
        v.addWidget(_sub_title("色板 Swatches（COLORS 单一真源）"))
        for group, items in ui_kit.swatches():
            v.addWidget(QLabel(group))
            host = QWidget()
            flow = FlowLayout(host, hgap=8, vgap=8)
            flow.setContentsMargins(0, 0, 0, 0)
            for name, hexv in items:
                flow.addWidget(self._swatch(name, hexv))
            v.addWidget(host)

        v.addWidget(_sub_title("字阶 Font"))
        for name in ("xs", "sm", "base", "md", "title", "page_title"):
            px = ui_kit.FONT[name]
            lbl = QLabel(f"{name} · {px}px —— AIGC 工厂 0123")
            lbl.setStyleSheet(f"font-size:{px}px;color:{C['text']};background:transparent;")
            v.addWidget(lbl)

        v.addWidget(_sub_title("间距 Space（色条宽＝对应 px）"))
        for name in ("xs", "sm", "md", "lg", "xl", "2xl", "3xl"):
            px = ui_kit.SPACE[name]
            bar = QWidget()
            bar.setFixedSize(max(px, 4) * 4, 12)
            bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            bar.setStyleSheet(f"background:{ui_kit.rgba(C['primary'], 0.5)};"
                              f"border-radius:{ui_kit.RADIUS['sm']}px;")
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(bar)
            row.addWidget(QLabel(f"{name} = {px}px"))
            row.addStretch(1)
            v.addLayout(row)

        v.addWidget(_sub_title("圆角 Radius"))
        host = QWidget()
        flow = FlowLayout(host, hgap=10, vgap=10)
        flow.setContentsMargins(0, 0, 0, 0)
        for name, px in ui_kit.RADIUS.items():
            box = QLabel(f"{name}\n{px}")
            box.setFixedSize(92, 52)
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            box.setStyleSheet(
                f"background:{C['bg_field']};border:1px solid {C['primary']};"
                f"border-radius:{px}px;color:{C['sub']};font-size:12px;")
            box.setToolTip(f"RADIUS['{name}'] = {px}px")
            flow.addWidget(box)
        v.addWidget(host)
        v.addStretch(1)
        return sa

    def _swatch(self, name, hexv):
        box = QLabel(name)
        box.setFixedSize(120, 44)
        box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        box.setStyleSheet(
            f"background:{hexv};color:{_contrast_text(hexv)};"
            f"border:1px solid {C['border_popup']};border-radius:{ui_kit.RADIUS['md']}px;"
            "font-size:12px;")
        box.setToolTip(f"{name}\n{hexv}")
        return box
