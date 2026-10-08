"""
tests/test_launcher_fan.py —— 扇形菜单：纯几何 + 候选池/持久化 + 离屏烟测

扇形的坑都集中在两处：① 几何——角度算错、贴屏幕边缘不会翻方向、命中测试偏一位就
点错功能；② 数据——候选池（工具 / 页面 / 搜索面板）与 app_state 里那份
launcher_fan_items 对不上，设置页改了扇形不跟着变。几何与数据都是纯函数，不需要
真的把窗口画出来就能验，所以这里大部分用例不碰 Qt 事件循环；最后一组用
offscreen 平台起一个真的 LauncherFan，确认构造/展开/收起这一路不炸。
"""
import math
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from gui import launcher_fan as lf
from store import app_state


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui.json")


# ---------------- 纯几何：角度 / 命中 / 朝屏内展开 ----------------

def test_arc_angles_single_item_lands_on_base():
    assert lf.arc_angles(1, 30, 120) == [30]


def test_arc_angles_two_items_symmetric_about_base():
    got = lf.arc_angles(2, 0, 120)
    assert got == pytest.approx([-60, 60])


def test_arc_angles_spreads_evenly_for_more_items():
    got = lf.arc_angles(3, 0, 120)
    assert got == pytest.approx([-60, 0, 60])
    assert len(lf.arc_angles(6, 0, 160)) == 6


def test_fan_positions_uses_upward_positive_angles():
    """0°=正右，90° 该往屏幕上方展开（y 变小）——y 轴朝下的坑就钉在这。"""
    (x, y), = lf.fan_positions(1, 100, 100, radius=50, base_deg=90)
    assert x == pytest.approx(100)
    assert y == pytest.approx(50)


def test_pick_at_hits_closest_button_within_radius():
    pts = [(0, 0), (100, 0), (200, 0)]
    assert lf.pick_at(pts, (4, 4), r=10) == 0
    assert lf.pick_at(pts, (96, 3), r=10) == 1
    assert lf.pick_at(pts, (150, 0), r=10) == -1     # 两颗都不在半径内


def test_base_toward_center_flips_when_ball_near_right_edge():
    """球贴屏幕右边：扇形该朝左（屏内）展开，不能甩出界。"""
    screen = (0, 0, 1920, 1080)
    leftward = lf.base_toward_center(1900, 540, screen)
    assert abs(abs(leftward) - 180) < 5               # ≈180°：朝左

    rightward = lf.base_toward_center(20, 540, screen)
    assert abs(rightward) < 5                        # ≈0°：朝右


def test_base_toward_center_points_up_when_ball_at_bottom():
    screen = (0, 0, 1920, 1080)
    up = lf.base_toward_center(960, 1060, screen)
    assert 85 < up < 95                              # ≈90°：朝上


# ---------------- 候选池 / 持久化 ----------------

def test_fan_candidates_covers_search_tools_pages():
    cands = lf.fan_candidates()
    kinds = {c["kind"] for c in cands}
    assert {"search", "tool", "page"} <= kinds
    assert any(c["kind"] == "search" for c in cands)
    page_refs = {c["ref"] for c in cands if c["kind"] == "page"}
    assert 1 in page_refs and 6 in page_refs          # 任务中心 / 设置 必须在候选里


def test_load_fan_items_defaults_when_unset():
    items = lf.load_fan_items()
    assert items                                     # 从没配过也要给一组实用默认，不能空
    assert {it["kind"] for it in items} == {"search", "page"}


def test_save_and_load_fan_items_roundtrip():
    cands = lf.fan_candidates()
    picked = [dict(cands[0]), dict(cands[1])]
    lf.save_fan_items(picked)
    assert lf.load_fan_items() == picked
    lf.save_fan_items([])                            # 清空后读回落到默认组，不是空列表
    assert lf.load_fan_items()


# ---------------- 离屏烟测：起一个真扇形，展开 / 命中 / 收起不炸 ----------------

def test_popup_positions_items_and_plus_button(qapp):
    class _FakeBall:
        def x(self): return 100
        def y(self): return 100
        def width(self): return 60
        def height(self): return 60

    picked = []
    fan = lf.LauncherFan(lambda it: picked.append(it), lambda: None)
    items = [{"kind": "search", "ref": "", "label": "搜索面板", "icon": "🔍"},
             {"kind": "page", "ref": 1, "label": "任务中心", "icon": "🎯"}]
    fan.popup(_FakeBall(), items)
    assert fan.isVisible()
    assert len(fan._points) == len(items) + 1         # 末尾还有一颗 "+"
    cx, cy = fan._points[0]
    assert lf.pick_at(fan._points, (cx, cy)) == 0
    fan.hide_menu()
    assert not fan.isVisible()
