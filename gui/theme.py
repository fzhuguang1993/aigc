"""
gui/theme.py —— 全局样式（飞书风浅色卡片）

QSS 管不到的三处下拉框顽疾，由本文件的 _ComboTweak 在运行时统一修：
- Windows 原生风格下 QSS 的 ::down-arrow 压不住原生箭头，会出现两个箭头：
  这里把样式表箭头彻底掉光（零尺寸+无图），改用每个 combo 上挂一个小
  覆盖层子控件自绘单箭头，结构上不存在“第二个”；
- 选中项左侧的「对钩」及其占位（样式绘制，QSS 屏蔽不了）；
- 弹层宽度不跟随内容，长文字被截一半。

原生 tooltip 黑底顽疾由 _TipPolish 统一兜底（详见类注释）。

复选框对钩/单选圆点/数字框箭头在 windowsvista 下画不了纯 QSS（会被
原生样式接管），由 _icon_assets() 启动时在临时目录生成几张透明小图，
QSS 里的 @CHK@/@DOT@/@UP@/@DOWN@ 占位符在 apply_theme 时替换成真实路径；
生成失败只是装饰图消失，其余样式不受影响。
"""
import os
import tempfile

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QStyledItemDelegate,
                               QStyleOptionViewItem, QWidget)


class _ArrowOverlay(QWidget):
    """下拉框右缘的单箭头覆盖层：QSS 在 windowsvista 下压不住原生箭头
    （项见多起：设了 ::down-arrow image 仍画两个），干脆把样式表箭头零掉，
    由这个小控件把灰色三角画在 ::drop-down 子控件区正中。
    鼠标事件穿透：QComboBox 按坐标判断点击箭头区开弹层，体验不变。"""
    W = 24                              # 与 QSS ::drop-down 宽度一致

    def __init__(self, combo):
        super().__init__(combo)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFixedSize(self.W, self.W)
        self._combo = combo
        self.show()                         # 子控件默认隐藏，首建即显

    def _place(self):
        # 与 QSS ::drop-down 一致：靠右、竖直居中、宽 24px（不必走 subControlRect）
        r = self._combo.rect()
        self.setGeometry(r.width() - self.W,
                         (r.height() - self.height()) // 2,
                         self.W, self.W)

    def paintEvent(self, e):
        from PySide6.QtGui import QPainter
        combo = self._combo
        view = combo.view()
        opened = view is not None and view.isVisible()
        arrow = _arrow_pixmap("#3370FF" if opened else "#646A73")
        p = QPainter(self)
        p.drawPixmap((self.width() - arrow.width()) // 2,
                     (self.height() - arrow.height()) // 2, arrow)
        p.end()


class _NoCheckDelegate(QStyledItemDelegate):
    """去掉弹层条目的装饰（对钩/单选圆点），文字不再被挤占宽度"""

    def paint(self, painter, option, index):
        option.features &= ~QStyleOptionViewItem.ViewItemFeature.HasDecoration
        super().paint(painter, option, index)


class _ComboTweak(QObject):
    """全局事件过滤器：任意 QComboBox 弹出前，自动换无对钩 delegate、
    禁文本省略，并把弹层加宽到能放下最长条目（含动态刷新后的内容）。"""

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Type.Show and isinstance(obj, QComboBox):
            self._arm(obj)                       # 首次显示时挂上弹层视图的监听
            self._attach_arrow(obj)              # 顺便挂上单箭头覆盖层
            obj._arrow_overlay and obj._arrow_overlay._place()
        elif t == QEvent.Type.Resize and isinstance(obj, QComboBox):
            ov = getattr(obj, "_arrow_overlay", None)
            if ov is not None:
                ov._place()                      # 宽度变了跟着重定位箭头区
        elif t == QEvent.Type.Show and isinstance(obj, QAbstractItemView):
            combo = obj.property("aigcOwnerCombo")
            if isinstance(combo, QComboBox):
                self._tweak(combo, obj)
                if getattr(combo, "_arrow_overlay", None) is not None:
                    combo._arrow_overlay.update()
        elif t == QEvent.Type.Hide and isinstance(obj, QAbstractItemView):
            combo = obj.property("aigcOwnerCombo")
            if isinstance(combo, QComboBox) and getattr(combo, "_arrow_overlay", None) is not None:
                combo._arrow_overlay.update()    # 弹层收起：箭头回到灰色
        return False

    def _attach_arrow(self, combo):
        if getattr(combo, "_arrow_overlay", None) is not None:
            return
        combo._arrow_overlay = _ArrowOverlay(combo)

    def _arm(self, combo):
        view = combo.view()
        if view is None or view.property("aigcOwnerCombo") is not None:
            return
        view.setProperty("aigcOwnerCombo", combo)   # 弹层视图→归属 combo
        view.installEventFilter(self)

    def _tweak(self, combo, view):
        if not isinstance(view.itemDelegate(), _NoCheckDelegate):
            view.setItemDelegate(_NoCheckDelegate(view))
        view.setTextElideMode(Qt.TextElideMode.ElideNone)
        fm = combo.fontMetrics()
        widest = max((fm.horizontalAdvance(combo.itemText(i))
                      for i in range(combo.count())), default=0)
        # 左右内边距 + 滚动条/边框余量；不小于 combo 本体宽度
        view.setMinimumWidth(max(widest + 40, combo.width()))
        self._polish_popup(view)

    # 弹层本体级兜底：Windows 深色模式下，app 级 QSS 的 background(-color) 常被
    # 原生弹层样式盖成黑底（与 QToolTip 同一个坑，见下方 QToolTip 注释）。趁 view
    # 的 Show 事件给它本体钉一层控件级浅色样式 + 局部 palette——控件自己的样式表
    # 优先级最高，原生盖不掉，全站下拉弹层一次性拉回浅色。
    _VIEW_QSS = (
        "QAbstractItemView{background-color:#FFFFFF; color:#1F2329;"
        " border:1px solid #E5E7EB; border-radius:8px; outline:none;"
        " selection-background-color:#EAF1FF; selection-color:#3370FF;}"
        "QAbstractItemView::item{background-color:transparent; color:#1F2329;"
        " min-height:30px; padding:2px 12px;}"
        "QAbstractItemView::item:hover,QAbstractItemView::item:selected"
        "{background-color:#EAF1FF; color:#3370FF;}")

    def _polish_popup(self, view):
        if getattr(view, "_aigc_polished", False):
            return
        view._aigc_polished = True
        view.setStyleSheet(self._VIEW_QSS)
        from PySide6.QtGui import QPalette, QColor
        pal = view.palette()
        for role, col in ((QPalette.ColorRole.Base, "#FFFFFF"),
                          (QPalette.ColorRole.Text, "#1F2329"),
                          (QPalette.ColorRole.Window, "#FFFFFF"),
                          (QPalette.ColorRole.WindowText, "#1F2329"),
                          (QPalette.ColorRole.Highlight, "#3370FF"),
                          (QPalette.ColorRole.HighlightedText, "#FFFFFF")):
            pal.setColor(role, QColor(col))
        view.setPalette(pal)
        vp = view.viewport()
        if vp is not None:
            vp.setPalette(pal)
        # 弹层容器（QComboBoxPrivateContainer）是独立顶层窗口：view 没铺满的
        # 边缘/圆角外一圈会按容器自己的 palette 画——一并钉成浅色，免得漏黑边
        container = view.window()
        if container is not None and container is not view:
            cpal = container.palette()
            for role, col in ((QPalette.ColorRole.Window, "#FFFFFF"),
                              (QPalette.ColorRole.Base, "#FFFFFF"),
                              (QPalette.ColorRole.WindowText, "#1F2329")):
                cpal.setColor(role, QColor(col))
            container.setPalette(cpal)


class _TipPolish(QObject):
    """原生 tooltip 保底美化：实测 Win10 上部分环境里，app 级 QSS 的 QToolTip
    规则与 app palette 两条渠道都管不住原生 tooltip 窗口（QTipLabel），弹出来
    黑底白字几乎不可读——这就是“图表上那个黑窗口”的根因（它来自控件级
    setToolTip，不走图表自绘的 _ChartTip）。
    解法：趁它即将显示的 Show 事件里，给这个窗口本体直接挂控件级样式表 +
    局部 palette——控件自己的样式表优先级最高，原生样式盖不掉。全站所有
    残留的原生 tooltip（按钮/表格单元格/标题栏…）一并被拉回浅色。"""

    TIP_QSS = ("background-color:#FFFFFF; color:#1F2329;"
               " border:1px solid #DEE0E3; padding:6px 9px; font-size:12px;")

    def eventFilter(self, obj, ev):
        if (ev.type() == QEvent.Show and isinstance(obj, QWidget)
                and obj.metaObject().className() == "QTipLabel"):
            obj.setStyleSheet(self.TIP_QSS)
            from PySide6.QtGui import QPalette, QColor
            pal = obj.palette()
            pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#FFFFFF"))
            pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#1F2329"))
            obj.setPalette(pal)
        return False


QSS = """
QWidget { font-family: "Microsoft YaHei UI", "Microsoft YaHei"; font-size: 13px; color: #1F2329; }
QMainWindow, QDialog { background: #F2F3F5; }

#Sidebar { background: #17212F; min-width: 170px; }
#Logo { color: #FFFFFF; font-size: 17px; font-weight: bold; padding: 8px 10px 18px 12px; }
#SideStatus { color: #8FA3BF; font-size: 12px; }
QListWidget#NavList { background: transparent; border: none; color: #D5E0F0; outline: none; }
QListWidget#NavList::item { height: 42px; border-radius: 10px; margin: 2px 8px; padding-left: 12px; font-size: 14px; }
QListWidget#NavList::item:selected { background: #3370FF; color: #FFFFFF; }
QListWidget#NavList::item:hover:!selected { background: #243144; }

QPushButton { background: #3370FF; color: white; border: none; border-radius: 8px;
    padding: 7px 14px; font-weight: 500; }
QPushButton:hover { background: #5A8BFF; }
QPushButton:pressed { background: #2457D9; }
QPushButton:disabled { background: #F5F6F7; color: #C9CDD4; }
QPushButton#GhostBtn { background: #FFFFFF; color: #37445A; border: 1px solid #DEE0E3; }
QPushButton#GhostBtn:hover { border-color: #3370FF; color: #3370FF; }
QPushButton#GhostBtn:pressed { background: #F2F6FF; border-color: #2457D9; color: #2457D9; }
QPushButton#GhostBtn:disabled { background: #F7F8FA; color: #C9CDD4; border-color: #E9EBEF; }
/* 危险动作变体：平时淡红描边不扎眼，悬停才实心提醒（删除/重置类入口用） */
QPushButton#DangerBtn { background: #FFF1F0; color: #D83931; border: 1px solid #FFCCC7; }
QPushButton#DangerBtn:hover { background: #F54A45; color: #FFFFFF; border-color: #F54A45; }
QPushButton#DangerBtn:pressed { background: #D83931; color: #FFFFFF; }
/* 快捷筛选标签：淡蓝胶囊，区别于普通按钮，一眼看出是“一键筛选” */
QPushButton#ChipBtn { background: #F2F6FF; color: #3370FF; border: 1px solid #D6E4FF;
    border-radius: 12px; padding: 3px 12px; font-size: 12px; font-weight: 400; }
QPushButton#ChipBtn:hover { background: #E1ECFF; border-color: #3370FF; }
QPushButton#ChipBtn:checked { background: #3370FF; color: #FFFFFF; border-color: #3370FF; }
/* 描边变体：与快捷筛选同款胶囊，只把圈深一号（「⚟ 更多筛选」入口、面板里的可点选项） */
QPushButton#ChipBtn[accent="1"] { border-color: #3370FF; }
/* 折叠编辑栏标题：整行可点，读起来像一行说明而不是一颗按钮 */
QPushButton#AccHead { background: #FFFFFF; border: 1px solid #DEE0E3; border-radius: 8px;
    padding: 9px 12px; text-align: left; color: #1F2329; font-weight: 600; }
QPushButton#AccHead:hover { border-color: #3370FF; color: #3370FF; }
QLineEdit, QPlainTextEdit {
    background: #FFFFFF; border: 1px solid #DEE0E3; border-radius: 8px; padding: 6px 8px; }
QLineEdit:focus, QPlainTextEdit:focus { border-color: #3370FF; }
QLineEdit:disabled, QPlainTextEdit:disabled { background: #F5F6F7; color: #8F959E; }

/* 下拉框：飞书风——白底圆角、悬停/展开蓝描边，
   弹出列表与 QMenu 同源样式（不能依赖原生样式，否则丑成文本框+小方块）。
   ⚠ ::down-arrow 零尺寸＋无图＝把原生/样式箭头彻底关干净；可见的单箭头由
   _ArrowOverlay 覆盖层画（windowsvista 下 QSS image 压不住原生箭头，会两个） */
QComboBox {
    background: #FFFFFF; border: 1px solid #DEE0E3; border-radius: 8px;
    padding: 5px 8px; min-height: 20px; color: #1F2329; }
QComboBox:hover, QComboBox:focus, QComboBox:on { border-color: #3370FF; }
QComboBox:disabled { background: #F5F6F7; color: #8F959E; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right;
    width: 24px; border: none; background: transparent; }
QComboBox::down-arrow { image: none; width: 0; height: 0; border: none; }
QComboBox QAbstractItemView {
    background-color: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 8px;
    padding: 0; outline: none; color: #1F2329;
    selection-background-color: #EAF1FF; selection-color: #3370FF; }
QComboBox QAbstractItemView::item {
    min-height: 30px; padding: 2px 12px;
    background-color: transparent; color: #1F2329; }
QComboBox QAbstractItemView::item:hover,
QComboBox QAbstractItemView::item:selected { background-color: #EAF1FF; color: #3370FF; }

/* 数字/日期编辑：与 QLineEdit 同款外观；原生的方形箭头按钮换成
   无边框小按钮 + 矢量小箭头（image 由 @UP@/@DOWN@ 注入）。
   选择器用 QAbstractSpinBox，把 QDateEdit 一并拉进同一套皮 */
QAbstractSpinBox { background: #FFFFFF; border: 1px solid #DEE0E3; border-radius: 8px;
    padding: 5px 8px; min-height: 20px; color: #1F2329; }
QAbstractSpinBox:hover, QAbstractSpinBox:focus { border-color: #3370FF; }
QAbstractSpinBox:disabled { background: #F5F6F7; color: #8F959E; }
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {
    subcontrol-origin: border; width: 20px; border: none; background: transparent; }
QAbstractSpinBox::up-button { subcontrol-position: top right; border-top-right-radius: 8px; }
QAbstractSpinBox::down-button { subcontrol-position: bottom right; border-bottom-right-radius: 8px; }
QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover { background: #F2F6FF; }
QAbstractSpinBox::up-arrow { image: url(@UP@); width: 10px; height: 6px; }
QAbstractSpinBox::down-arrow { image: url(@DOWN@); width: 10px; height: 6px; }

/* 复选/单选：原生小方块换成飞书风圆角框（选中蓝底白勾/蓝心圆点，
   图由 @CHK@/@DOT@ 注入；悬停文字变蓝是“可点”的轻量反馈） */
QCheckBox, QRadioButton { background: transparent; spacing: 8px; color: #1F2329; }
QCheckBox:hover, QRadioButton:hover { color: #3370FF; }
QCheckBox:disabled, QRadioButton:disabled { color: #C9CDD4; }
QCheckBox::indicator, QRadioButton::indicator { width: 18px; height: 18px; }
QCheckBox::indicator { background: #FFFFFF; border: 1px solid #C9CDD4; border-radius: 5px; }
QCheckBox::indicator:hover { border-color: #3370FF; }
QCheckBox::indicator:checked { background: #3370FF; border-color: #3370FF; image: url(@CHK@); }
QRadioButton::indicator { background: #FFFFFF; border: 1px solid #C9CDD4; border-radius: 10px; }
QRadioButton::indicator:hover { border-color: #3370FF; }
QRadioButton::indicator:checked { border-color: #3370FF; image: url(@DOT@); }

/* 通用页签：与数据中台 DashTabs 同款的墨线式（替掉原生方块页签）；
   drawBase=0 去掉页签栏下那条原生白线 */
QTabWidget::pane { border: none; background: transparent; }
QTabBar { qproperty-drawBase: 0; background: transparent; }
QTabBar::tab { background: transparent; color: #646A73; border: none;
    padding: 8px 14px; margin-right: 4px; font-size: 13px; }
QTabBar::tab:hover { color: #1F2329; }
QTabBar::tab:selected { color: #3370FF; font-weight: 600; border-bottom: 2px solid #3370FF; }

/* 进度条：14px 胶囊，淡灰轨道 + 主蓝填充 */
QProgressBar { background: #EFF1F5; border: none; border-radius: 7px;
    height: 14px; text-align: center; color: #646A73; font-size: 11px; }
QProgressBar::chunk { background: #3370FF; border-radius: 7px; }

/* 滑块：细轨道 + 白圆钮（播放器壳内的 #PlayerShell 局部样式优先级更高，不互扰） */
QSlider::groove:horizontal { height: 4px; background: #E5E7EB; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #3370FF; border-radius: 2px; }
QSlider::handle:horizontal { width: 14px; height: 14px; margin: -5px 0;
    background: #FFFFFF; border: 1px solid #C9CDD4; border-radius: 7px; }
QSlider::handle:horizontal:hover { border-color: #3370FF; }

QTableWidget { background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 12px;
    gridline-color: transparent; }
QTableWidget::item { padding: 5px 8px; border-bottom: 1px solid #EFF0F1; }
QTableWidget::item:hover { background: #F7F8FA; }
QTableWidget::item:selected { background: #EAF1FF; color: #1F2329; }
QHeaderView::section { background: #F5F6F7; border: none; border-bottom: 1px solid #E5E7EB;
    padding: 10px 8px; font-size: 12px; font-weight: 600; color: #475569; }
QTableCornerButton::section { background: #F5F6F7; border: none; }

#PageTitle { font-size: 20px; font-weight: bold; }
#PageTip { color: #8F959E; }
#PageWarn { color: #D83931; font-weight: bold; }
#InlineTip { color: #3370FF; }
#DialogTitle { font-size: 18px; font-weight: bold; }

QPlainTextEdit#LogBox { background: #10151C; color: #B7C4D6; border: none; border-radius: 10px;
    font-family: Consolas, monospace; font-size: 12px; }
QStatusBar { background: #FFFFFF; color: #8F959E; border-top: 1px solid #E5E7EB; }
QMenu { background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 8px; padding: 6px; }
QMenu::item { padding: 6px 22px; border-radius: 6px; }
QMenu::item:selected { background: #EAF1FF; color: #3370FF; }

QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #C9CDD4; border-radius: 5px; min-height: 30px; }
QScrollBar:horizontal { background: transparent; height: 10px; }
QScrollBar::handle:horizontal { background: #C9CDD4; border-radius: 5px; min-width: 30px; }
QScrollBar::handle:hover { background: #A9AFB8; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }

QPushButton#SideBtn { background: #243144; color: #D5E0F0; border: 1px solid #33455E;
    border-radius: 8px; padding: 7px 10px; margin: 4px 8px; }
QPushButton#SideBtn:hover { background: #3370FF; color: #FFFFFF; }

/* 悬停提示：① 必须用 background-color— Windows 深色模式下写 background 简写
   会被原生样式盖成黑底；② 刻意不写 border-radius——Windows 10 的 QToolTip 是
   矩形弹窗、没有逐像素透明，圆角外的四个角会被填成黑色（“黑角”到处都是）。
   注意：这两条+app palette 在部分机器上仍压不住黑底，真正的兜底是
   apply_theme 里挂的 _TipPolish（在 tooltip 窗口 Show 时直接给它本体钉样式） */
QToolTip { background-color: #FFFFFF; color: #1F2329; border: 1px solid #DEE0E3;
    padding: 6px 9px; font-size: 12px; }

QWidget#Card { background: #FFFFFF; border: 1px solid #ECEEF1; border-radius: 12px; }
/* 无边框圆角窗口（gui/window_frame.py）：顶层窗口自身不再铺实体背景，
   可见的卡片底+阴影由 _RoundedBg 逐像素 alpha 抗锯齿绘制——属性选择器只匹配
   设了 roundedTop 的顶层窗口，不波及子控件；缺了这条，上面的
   QMainWindow/QDialog 背景会把方角矩形重新画满，圆角直接失效 */
QWidget[roundedTop="1"] { background: transparent; }
/* 无边框圆角窗口的自绘标题栏：本体透明——白色标题带和带底分隔线由
   _RoundedBg 裁着窗口圆弧从卡片顶缘一路画下来，标题栏不再自带白直角板，
   消除“灰底嵌白块”的双层边框感（WinXP 味根源）；按钮默认透明、
   悬停淡蓝，关闭悬停红（与普通按钮的实蓝底区分开，不像“可提交”的动作） */
QWidget#AppTitleBar { background: transparent; }
QLabel#AppTitleText { font-size: 13px; font-weight: 700; color: #1F2329; background: transparent; }
QPushButton#AppTitleBtn { background: transparent; color: #646A73; border: none;
    border-radius: 6px; padding: 0; font-size: 13px; }
QPushButton#AppTitleBtn:hover { background: #EAF1FF; color: #3370FF; }
QPushButton#AppTitleClose:hover { background: #F54A45; color: #FFFFFF; }
/* 设置页底部动作栏：钉在窗口底的白底栏，顶边分隔线与内容区断开 */
QWidget#SettingsFoot { background: #FFFFFF; border-top: 1px solid #E5E7EB; }
QWidget#Sidebar QPushButton { font-size: 12px; }
QWidget#ToolCard { background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 12px; }
QWidget#ToolCard[canopen="1"]:hover { border: 1px solid #3370FF; }
QWidget#ToolCard[canopen="0"] { background: #FAFBFC; color: #8F959E; }

/* 线路负载概览条：线路卡片自身在 paintEvent 里绘背景/描边/状态条，
   这里只需给顶部概览大条一个白底圆角容器 */
QWidget#SummaryCard { background: #FFFFFF; border: 1px solid #ECEEF1; border-radius: 12px; }

QTreeWidget, QListWidget { background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 12px;
    outline: 0; }
QTreeWidget::item { height: 30px; padding: 4px 6px; margin: 1px 6px; border-radius: 8px; }
QTreeWidget::item:hover { background: #F2F6FF; }
QTreeWidget::item:selected { background: #3370FF; color: #FFFFFF; }
QListWidget::item { padding: 4px 2px; border-radius: 6px; }
QListWidget::item:selected { background: #EAF1FF; color: #1F2329; }
QListWidget { outline: 0; }
QTreeWidget::branch { background: transparent; }
"""

_ARROW_CACHE = {}

# --------------------------------------------------------------------
# QSS 小图资产：复选对钩/单选蓝点/数字框箭头在 windowsvista 下必须
# 走 image: url()（纯 QSS 画不过去）。启动时在临时目录生成几张透明
# PNG，apply_theme 把 QSS 里的 @CHK@/@DOT@/@UP@/@DOWN@ 换成真实路径；
# 任何一步失败都给空路径——只是装饰图缺失，不拉爆整体样式。
# --------------------------------------------------------------------
_ICON_ASSETS = None


def _asset_dir():
    d = os.path.join(tempfile.gettempdir(), "aigc_ui_assets")
    os.makedirs(d, exist_ok=True)
    return d


def _icon_assets():
    global _ICON_ASSETS
    if _ICON_ASSETS is not None:
        return _ICON_ASSETS
    assets = {}
    try:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QImage, QPainter, QPen, QColor, QPolygonF

        def _make(name, w, h, draw):
            img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            draw(p, w, h)
            p.end()
            path = os.path.join(_asset_dir(), name)
            if img.save(path):
                assets[name] = path.replace("\\", "/")

        def _chk(p, w, h):                 # 12px 白对钩（垫在蓝底上）
            pen = QPen(QColor("#FFFFFF"))
            pen.setWidthF(2.2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPolyline(QPolygonF([QPointF(2.4, 6.4), QPointF(4.9, 8.9),
                                      QPointF(9.6, 3.3)]))

        def _dot(p, w, h):                 # 16px 单选蓝心
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#3370FF"))
            p.drawEllipse(QPointF(w / 2.0, h / 2.0), 4.0, 4.0)

        def _up(p, w, h):                  # 10x6 实心三角（上下各一张）
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#646A73"))
            p.drawPolygon(QPolygonF([QPointF(1, h - 1), QPointF(w - 1, h - 1),
                                     QPointF(w / 2.0, 1)]))

        def _down(p, w, h):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#646A73"))
            p.drawPolygon(QPolygonF([QPointF(1, 1), QPointF(w - 1, 1),
                                     QPointF(w / 2.0, h - 1)]))

        _make("check.png", 12, 12, _chk)
        _make("radio_dot.png", 16, 16, _dot)
        _make("spin_up.png", 10, 6, _up)
        _make("spin_down.png", 10, 6, _down)
    except Exception:
        assets = {}
    _ICON_ASSETS = assets
    return assets


def build_qss():
    """把 QSS 模板里的 @xxx@ 占位符换成小图真实路径（无图时为空串）"""
    a = _icon_assets()
    return (QSS.replace("@CHK@", a.get("check.png", ""))
              .replace("@DOT@", a.get("radio_dot.png", ""))
              .replace("@UP@", a.get("spin_up.png", ""))
              .replace("@DOWN@", a.get("spin_down.png", "")))


def _arrow_pixmap(color: str):
    """画一张向下的实心三角（内存缓存，不落盘）：
    旧方案把 PNG 存临时目录再 url() 贴进 QSS，但 windowsvista 会在图旁边
    再画一个原生箭头（双箭头顽疾）；现在箭头只由 _ArrowOverlay 画这一处。"""
    pm = _ARROW_CACHE.get(color)
    if pm is not None:
        return pm
    from PySide6.QtGui import QPixmap, QPainter, QColor, QPolygonF
    from PySide6.QtCore import QPointF
    pm = QPixmap(12, 8)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawPolygon(QPolygonF([QPointF(1, 1.5), QPointF(11, 1.5), QPointF(6, 7)]))
    p.end()
    _ARROW_CACHE[color] = pm
    return pm


def apply_theme(app):
    app.setStyleSheet(build_qss())
    # tooltip 调色板兜底：部分 Windows 环境 QSS 管不住原生 tooltip（黑底白字），
    # palette 把底色钉成浅色后两条渠道都是白底；仍不够——再挂 _TipPolish
    # 在 tooltip 窗口弹出的瞬间直接给它本体钉上浅色样式（最终兜底）
    from PySide6.QtGui import QPalette, QColor
    pal = app.palette()
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#FFFFFF"))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#1F2329"))
    # 输入框占位文字统一弱文灰（QSS 无 placeholder 伪元素，只能走 palette）
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#8F959E"))
    app.setPalette(pal)
    app.installEventFilter(_TipPolish(app))
    app.installEventFilter(_ComboTweak(app))   # 全局修下拉：单箭头覆盖层、无对钩、宽度自适应
