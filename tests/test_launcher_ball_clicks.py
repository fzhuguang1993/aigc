"""
tests/test_launcher_ball_clicks.py —— 悬浮球：双击/拖动 的判定逻辑（本版单击不担责）

球改成"移入展开"后，点击不再触发展开；剩下的点击职责只有"双击打开主程序"，最容易出的岔子：
- 拖动也被当成一次点击 → 移动位置时误判双击；
- 双击的第二下没被识别 → 只记第一下、永远等不到第二下（或反过来把两次都算双击）。

判定逻辑写在 mousePressEvent/mouseReleaseEvent 里，所以用真实鼠标事件（QTest）在
offscreen 平台走一遍，而不是直接调私有方法——绕过事件就测不到"要不要触发"。
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
    # on_hover 给个空回调：offscreen 下 QTest 的点击会顺带触一发 Enter，
    # 本用例只关心双击/拖动判定，不让 hover 计数干扰。
    fired = {"double": 0}
    b = lb.LauncherBall(lambda: None,
                        lambda: fired.__setitem__("double", fired["double"] + 1))
    b.resize(72, 72)
    b.show()
    QTest.qWaitForWindowExposed(b)
    yield b, fired
    b.stop()                    # 直接建的球没进 _ball_ref，_isolate 关不到，必须自收避免泄漏


def _wait_past_double_click_interval(qapp):
    QTest.qWait(qapp.doubleClickInterval() + 120)


def test_single_click_does_not_fire_double(ball, qapp):
    """本版单击不担责：一下点击不该触发双击。"""
    b, fired = ball
    QTest.mouseClick(b, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     b.rect().center())
    _wait_past_double_click_interval(qapp)
    assert fired == {"double": 0}


def test_double_click_fires_double_only(ball, qapp):
    b, fired = ball
    center = b.rect().center()
    QTest.mouseClick(b, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
    QTest.mouseClick(b, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
    _wait_past_double_click_interval(qapp)
    assert fired == {"double": 1}, "双击应只触发双击一次"


def test_drag_is_not_counted_as_click_and_saves_position(ball, qapp):
    b, fired = ball
    start = b.rect().center()
    QTest.mousePress(b, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(b, start + QPoint(30, 30))
    QTest.mouseRelease(b, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       start + QPoint(30, 30))
    _wait_past_double_click_interval(qapp)
    assert fired == {"double": 0}, "拖动不该触发双击回调"
    assert app_state.get("launcher_ball_pos") == [b.x(), b.y()]


def test_fire_double_without_double_callback_is_harmless(qapp):
    """没配 on_double 的老调用方式：双击什么也不做，不该炸。"""
    fired = []
    b = lb.LauncherBall(lambda: fired.append("hover"))
    b._fire_double()
    assert fired == []
