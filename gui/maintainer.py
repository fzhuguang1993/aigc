"""
gui/maintainer.py —— 维护人口令门槛：同一功能当天验证一次即可

以前每次唤出维护人入口（Alt+W）都要重新输口令、保存后立刻上锁，维护人一天
要重复输几十遍。现在按「功能」逐个记解锁日期（存 ui_state.json）：当天验过
一次就不再问，跨到第二天才要重新输。

功能键彼此独立（输出目录/接口配置/翻译对照各记各的），谁也不连带谁。
"""
from datetime import datetime

from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox

from store import app_state

STATE_KEY = "maintainer_unlocks"


def today():
    return datetime.now().strftime("%Y-%m-%d")


def app_has_unlocked(feature):
    """界面构建时同步解锁态用：某功能当天是否已验过口令"""
    return Gate(feature).is_open()


class Gate:
    """一个功能的按天门槛：is_open 查今天开没开，ask 负责问口令"""

    def __init__(self, feature):
        self.feature = feature

    def is_open(self):
        return (app_state.get(STATE_KEY) or {}).get(self.feature) == today()

    def unlock(self):
        marks = dict(app_state.get(STATE_KEY) or {})
        marks[self.feature] = today()
        app_state.set_value(STATE_KEY, marks)

    def ask(self, parent, title="维护人验证", label="请输入维护人口令：", force=False):
        """口令对 → 记当天解锁并 True；取消/错口令 → False（错口令弹提示）。
        API_MAINTAINER_CODE 懒导入：避开 tool_panels ↔ maintainer 的循环依赖

        force=True：忽略「当天已验过」缓存，无论如何都弹口令框——用于必须每次
        会话都重新验证才放出的入口（如接口管理），保证重启后 Alt+W 必定先弹框。"""
        if not force and self.is_open():
            return True                     # 当天已经验过：不再打扰
        from gui.tool_panels import API_MAINTAINER_CODE
        code, ok = QInputDialog.getText(parent, title, label,
                                        QLineEdit.EchoMode.Password)
        if not ok:
            return False
        if code != API_MAINTAINER_CODE:
            QMessageBox.warning(parent, "口令错误", "维护人口令不正确")
            return False
        self.unlock()
        return True
