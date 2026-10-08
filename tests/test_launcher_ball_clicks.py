"""
tests/test_launcher_ball_clicks.py —— 悬浮球：单击/双击/拖动 的判定逻辑

球同时挂"单击展开扇形"和"双击打开主程序"两个动作，最容易出的岔子是：
- 拖动也被当成一次点击 → 移动位置时误弹菜单；
- 双击判定不吞掉第一次按下挂起的"待定单击" → 双击变成了"扇形 + 主程序"两件事。

这两条都用真实鼠标事件（QTest）在 offscreen 平台上走一遍，而不是直接调私有方法：
判定逻辑本身写在 mousePressEvent/mouseReleaseEvent 里，绕过事件去调 `_fire_single()`
只能测到回调接线，测不到"要不要触发"这个真正的判断。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from gui import launcher_ball as lb
from store import app_state


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui.json")
    lb.hide_launcher_ball()
    yield
    lb.hide_launcher_ball()


@pytest.fixture
def ball(qapp):
    fired = {"single": 0, "double": 0}
    b = lb.LauncherBall(lambda: fired.__setitem__("single", fired["single"] + 1),
                        lambda: fired.__setitem__("double", fired["double"] + 1))
    b.resize(60, 60)
    b.show()
    QTest.qWaitForWindowExposed(b)
    return b, fired


def _wait_past_double_click_interval(qapp):
    QTest.qWait(qapp.doubleClickInterval() + 120)


def test_single_click_fires_only_single_after_interval(ball, qapp):
    b, fired = ball
    QTest.mouseClick(b, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     b.rect().center())
    # 抬手里挂起的"待定单击"定时器还没到点：这一刻两样都不该已经触发
    assert fired == {"single": 0, "double": 0}
    _wait_past_double_click_interval(qapp)
    assert fired == {"single": 1, "double": 0}


def test_double_click_cancels_pending_single_and_fires_double(ball, qapp):
    b, fired = ball
    center = b.rect().center()
    QTest.mouseClick(b, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     center)
    QTest.mouseClick(b, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     center)
    _wait_past_double_click_interval(qapp)
    assert fired == {"single": 0, "double": 1}, \
        "双击没吞掉第一次挂起的单击：扇形和主程序会同时被打开"


def test_drag_is_not_counted_as_click_and_saves_position(ball, qapp):
    b, fired = ball
    start = b.rect().center()
    QTest.mousePress(b, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(b, start + QPoint(30, 30))
    QTest.mouseRelease(b, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       start + QPoint(30, 30))
    _wait_past_double_click_interval(qapp)
    assert fired == {"single": 0, "double": 0}, "拖动不该触发单击/双击回调"
    assert app_state.get("launcher_ball_pos") == [b.x(), b.y()]


def test_fire_double_without_double_callback_falls_back_to_single(qapp):
    """没配 on_double 的老调用方式：双击退回执行单击，至少不能"点了没反应"。"""
    fired = []
    b = lb.LauncherBall(lambda: fired.append("single"))
    b._fire_double()
    assert fired == ["single"]
