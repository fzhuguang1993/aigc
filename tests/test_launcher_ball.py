"""
tests/test_launcher_ball.py —— 唤出悬浮球：移入唤起、移出请求收起、单例替换、位置落盘

本版交互改为"移入展开、移出收起"（单击不再承担展开），所以这里验的是：
① 鼠标移入确实把 hover 回调打出去、移出把 leave 回调打出去（收放由调用方裁定）；
② show_launcher_ball 先收旧球再上新球，同一时刻只留一颗；
③ 球会平滑启动/停止采样与动画计时器（不残留后台计时）。
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


def test_hover_enter_leave_fire_callbacks(qapp):
    fired = []
    ball = lb.show_launcher_ball(lambda: fired.append("enter"),
                                 on_leave=lambda: fired.append("leave"))
    assert isinstance(ball, lb.LauncherBall)
    ball._fire_hover()
    ball._fire_leave()
    assert fired == ["enter", "leave"]


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


def test_right_click_close_hands_the_cleanup_to_the_caller(qapp, monkeypatch):
    """关球必须把“收起已展开的扇形”这一步交回调用方：只 hide 球自己就是用户说的
    “关了悬浮球，展开的内容居然还没关闭”（扇形是另一个顶层窗，不归球管）。
    没传回调时退回“只收球”的老行为，不能抛。"""
    closed = []
    ball = lb.show_launcher_ball(lambda: None, on_close=lambda: closed.append(1))
    ball._hide_requested()
    assert closed == [1]
    assert lb._ball_ref is ball          # 收干净归回调管，球自己不动单例
    ball.stop()

    plain = lb.show_launcher_ball(lambda: None)
    hide = []
    monkeypatch.setattr(lb, "hide_launcher_ball", lambda: hide.append(1))
    plain._hide_requested()
    assert hide == [1] and app_state.get("launcher_ball_on") is False
    plain.stop()


def test_show_stops_timers_on_hide(qapp):
    ball = lb.show_launcher_ball(lambda: None)
    assert ball._sample_timer.isActive() and ball._anim_timer.isActive()
    lb.hide_launcher_ball()
    assert not ball._sample_timer.isActive() and not ball._anim_timer.isActive()


def test_net_speedometer_degrades_without_psutil():
    """没 psutil（取数函数为 None）时 poll 恒返 (0,0)，不能把装饰球带崩。"""
    m = lb.NetSpeedometer()
    m._io = None
    assert m.poll() == (0.0, 0.0)


def test_net_speedometer_computes_delta(monkeypatch):
    """两次采样差÷时间差 = 速率；首次给 0，回绕（变小）归 0。"""
    m = lb.NetSpeedometer()
    seq = iter([_IO(1000, 2000), _IO(3000, 6000), _IO(100, 100)])
    monkeypatch.setattr(m, "_io", lambda: next(seq))
    monkeypatch.setattr(lb.time, "monotonic", iter([0.0, 1.0, 2.0]).__next__)
    assert m.poll() == (0.0, 0.0)          # 第一次只建基线
    up, down = m.poll()                    # 第二帧：+2000 上行 / +4000 下行
    assert round(up) == 2000 and round(down) == 4000
    assert m.poll() == (0.0, 0.0)          # 回绕变小 → 归 0


class _IO:
    def __init__(self, sent, recv):
        self.bytes_sent, self.bytes_recv = sent, recv


def test_hover_glow_rises_then_falls(qapp):
    """移入→周圈发光缓动拉满；移出→回落到 0（发光与网速无关）。"""
    b = lb.LauncherBall(lambda: None)
    b._hovered = True
    for _ in range(60):
        b._animate()
    assert b._glow > 0.8, "移入后光晕应接近满"
    b._hovered = False
    for _ in range(120):
        b._animate()
    assert b._glow < 0.1, "移出后光晕应回落"
    b.stop()


def test_enter_leave_toggle_hovered(qapp):
    """enterEvent/leaveEvent 必须把 _hovered 置位/清位（驱动发光）。"""
    b = lb.LauncherBall(lambda: None)
    b._hovered = False
    b._set_hover(True)
    assert b._hovered is True
    b._set_hover(False)
    assert b._hovered is False
    b.stop()


def test_waveform_buffer_advances_with_traffic(qapp):
    """有流量时每帧向两条波形缓冲各推一个非零采样，无流量回平。"""
    b = lb.LauncherBall(lambda: None)
    b._down = 2 * 1024 * 1024      # 2 MB/s
    b._up = 1024 * 1024
    before = len(b._wave_down)
    b._animate()
    assert len(b._wave_down) == before + 1
    assert b._wave_down[-1] > 0.0 and b._wave_up[-1] > 0.0
    b._down = 0.0
    b._up = 0.0
    b._animate()
    assert b._wave_down[-1] == 0.0
    b.stop()
