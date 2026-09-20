"""
gui/theme.py —— 全局样式（飞书风浅色卡片）
"""
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

QLineEdit, QPlainTextEdit, QComboBox {
    background: #FFFFFF; border: 1px solid #DEE0E3; border-radius: 8px; padding: 6px 8px; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border-color: #3370FF; }
QComboBox::drop-down { border: none; width: 22px; }

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

QTreeWidget, QListWidget { background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 10px; }
QTreeWidget::item, QListWidget::item { padding: 4px 2px; border-radius: 6px; }
QTreeWidget::item:selected, QListWidget::item:selected { background: #EAF1FF; color: #1F2329; }
QListWidget { outline: 0; }
"""


def apply_theme(app):
    app.setStyleSheet(QSS)
