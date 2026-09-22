"""
gui/theme.py —— 全局样式（飞书风浅色卡片）

QSS 管不到的两处下拉弹层顽疾，由本文件的 _ComboTweak 在运行时统一修：
- 选中项左侧的「对钩」及其占位（样式绘制，QSS 屏蔽不了）；
- 弹层宽度不跟随内容，长文字被截一半。
"""
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QStyledItemDelegate,
                               QStyleOptionViewItem)


class _NoCheckDelegate(QStyledItemDelegate):
    """去掉弹层条目的装饰（对钩/单选圆点），文字不再被挤占宽度"""

    def paint(self, painter, option, index):
        option.features &= ~QStyleOptionViewItem.ViewItemFeature.HasDecoration
        super().paint(painter, option, index)


class _ComboTweak(QObject):
    """全局事件过滤器：任意 QComboBox 弹出前，自动换无对钩 delegate、
    禁文本省略，并把弹层加宽到能放下最长条目（含动态刷新后的内容）。"""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Show and isinstance(obj, QComboBox):
            self._arm(obj)                       # 首次显示时挂上弹层视图的监听
        elif ev.type() == QEvent.Type.Show and isinstance(obj, QAbstractItemView):
            combo = obj.property("aigcOwnerCombo")
            if isinstance(combo, QComboBox):
                self._tweak(combo, obj)
        return False

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

QPushButton { background: #3370FF; color: white; border: none; border-radius: 8px; padding: 7px 14px; }
QPushButton:hover { background: #5A8BFF; }
QPushButton:pressed { background: #2457D9; }
QPushButton#GhostBtn { background: #FFFFFF; color: #37445A; border: 1px solid #DEE0E3; }
QPushButton#GhostBtn:hover { border-color: #3370FF; color: #3370FF; }
/* 快捷筛选标签：淡蓝胶囊，区别于普通按钮，一眼看出是“一键筛选” */
QPushButton#ChipBtn { background: #F2F6FF; color: #3370FF; border: 1px solid #D6E4FF;
    border-radius: 12px; padding: 3px 12px; font-size: 12px; }
QPushButton#ChipBtn:hover { background: #E1ECFF; border-color: #3370FF; }

QLineEdit, QPlainTextEdit {
    background: #FFFFFF; border: 1px solid #DEE0E3; border-radius: 8px; padding: 6px 8px; }
QLineEdit:focus, QPlainTextEdit:focus { border-color: #3370FF; }

/* 下拉框：飞书风——白底圆角、悬停/展开蓝描边、右侧自绘小箭头，
   弹出列表与 QMenu 同源样式（不能依赖原生样式，否则丑成文本框+小方块） */
QComboBox {
    background: #FFFFFF; border: 1px solid #DEE0E3; border-radius: 8px;
    padding: 5px 8px; min-height: 20px; color: #1F2329; }
QComboBox:hover, QComboBox:focus, QComboBox:on { border-color: #3370FF; }
QComboBox:disabled { background: #F5F6F7; color: #8F959E; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right;
    width: 24px; border: none; background: transparent; }
/* 全局样式表会干掉原生下拉箭头，用 border 三角自绘一个，悬停/展开变蓝 */
QComboBox::down-arrow { width: 0; height: 0; margin-right: 8px;
    border-left: 5px solid transparent; border-right: 5px solid transparent;
    border-top: 6px solid #646A73; }
QComboBox:hover::down-arrow, QComboBox:focus::down-arrow { border-top-color: #3370FF; }
QComboBox QAbstractItemView {
    background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 8px;
    padding: 0; outline: none;
    selection-background-color: #EAF1FF; selection-color: #3370FF; }
QComboBox QAbstractItemView::item {
    min-height: 30px; padding: 2px 12px;
    background: transparent; color: #1F2329; }
QComboBox QAbstractItemView::item:hover,
QComboBox QAbstractItemView::item:selected { background: #EAF1FF; color: #3370FF; }

QTableWidget { background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 10px;
    gridline-color: transparent; }
QTableWidget::item { padding: 5px 8px; border-bottom: 1px solid #EFF0F1; }
QTableWidget::item:selected { background: #EAF1FF; color: #1F2329; }
QHeaderView::section { background: #F5F6F7; border: none; border-bottom: 1px solid #E5E7EB;
    padding: 8px; font-weight: bold; color: #475569; }
QTableCornerButton::section { background: #F5F6F7; border: none; }

#PageTitle { font-size: 20px; font-weight: bold; }
#PageTip { color: #8F959E; }
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
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }

QPushButton#SideBtn { background: #243144; color: #D5E0F0; border: 1px solid #33455E;
    border-radius: 8px; padding: 7px 10px; margin: 4px 8px; }
QPushButton#SideBtn:hover { background: #3370FF; color: #FFFFFF; }

QToolTip { background: #FFFFFF; color: #1F2329; border: 1px solid #DEE0E3;
    padding: 8px; font-size: 12px; }

QWidget#Card { background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 10px; }
QWidget#Sidebar QPushButton { font-size: 12px; }
QWidget#ToolCard { background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 12px; }
QWidget#ToolCard:hover { border-color: #3370FF; }

QTreeWidget, QListWidget { background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 10px;
    outline: 0; }
QTreeWidget::item { height: 30px; padding: 4px 6px; margin: 1px 6px; border-radius: 8px; }
QTreeWidget::item:hover { background: #F2F6FF; }
QTreeWidget::item:selected { background: #3370FF; color: #FFFFFF; }
QListWidget::item { padding: 4px 2px; border-radius: 6px; }
QListWidget::item:selected { background: #EAF1FF; color: #1F2329; }
QListWidget { outline: 0; }
QTreeWidget::branch { background: transparent; }
"""


def apply_theme(app):
    app.setStyleSheet(QSS)
    app.installEventFilter(_ComboTweak(app))   # 全局修下拉弹层：无对钩、宽度自适应
