"""
tests/test_launcher_ball.py —— 唤出悬浮球：点球唤出、单例替换、位置落盘

只验这条小球最要命的两件事：①没拖动的一次点击确实把唤出回调打出去（点了没反应＝白做）；
②show_launcher_ball 先收旧球再上新球，同一时刻只留一颗（否则开开关关攒一堆球）。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from gui import launcher_ball as lb
from store import app_state


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _iso(monkeypatch, tmp_path):
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui.json")
    lb.hide_launcher_ball()
    yield
    lb.hide_launcher_ball()


def test_click_fires_callback(qapp):
    fired = []
    ball = lb.show_launcher_ball(lambda: fired.append(1))
    assert isinstance(ball, lb.LauncherBall)
    ball._fire_single()
    assert fired == [1]


def test_show_replaces_previous(qapp):
    first = lb.show_launcher_ball(lambda: None)
    second = lb.show_launcher_ball(lambda: None)
    assert second is not first
    assert lb._ball_ref is second
    lb.hide_launcher_ball()
    assert lb._ball_ref is None


def test_hide_is_idempotent(qapp):
    lb.hide_launcher_ball()          # 没有球时收起不该炸
    lb.show_launcher_ball(lambda: None)
    lb.hide_launcher_ball()
    lb.hide_launcher_ball()
    assert lb._ball_ref is None
