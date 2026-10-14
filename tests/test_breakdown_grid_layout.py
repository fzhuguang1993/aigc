"""
tests/test_breakdown_grid_layout.py —— 拆解任务列表排版：卡片少也要靠左紧挨着

钉住用户报的那一条：宽屏只有 2 张卡时两卡隔得太开。原因是 QGridLayout 会把
视口的富余宽度平摊给“已经用到的每一列”——2 张卡就是 2 列，每列各分到半个屏幕，
卡片被顶得老远（卡片铺满一行时看不出来，所以只在条目少时抽风）。
修法是尾巴上留一列“吃余量”的空列，这里就按这一条断言。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 早于任何 PySide6 导入

import pytest                     # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget      # noqa: E402
from gui.pages_breakdown import BreakdownTasksPage, CARD_W, GRID_GAP   # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _shown(qapp, w=1900):
    pg = BreakdownTasksPage()
    pg.resize(w, 900)
    pg.show()
    qapp.processEvents()
    return pg


def _place(pg, qapp, n):
    """塞 n 张与真卡同宽的假卡（不动数据库），重排后返回它们在 holder 里的左上角 x"""
    for c in pg._cards:
        c.deleteLater()
    cards = []
    for _ in range(n):
        c = QWidget(pg._holder)
        c.setFixedSize(CARD_W, 260)
        cards.append(c)
    pg._cards = cards
    pg._relayout()
    qapp.processEvents()
    return [c.x() for c in cards]


def test_two_cards_stay_tight_on_wide_viewport(qapp):
    """两张卡：间距只许是一个卡片宽+一个缝，不许是半个屏幕"""
    pg = _shown(qapp)
    xs = _place(pg, qapp, 2)
    assert xs[0] <= 20, "第一张卡要贴着左边缘，不该被挤到中间"
    assert xs[1] - xs[0] == CARD_W + GRID_GAP, \
        "两卡之间只许隔一条缝：%d ≠ %d（富余宽度被平摊给两列了）" % (
            xs[1] - xs[0], CARD_W + GRID_GAP)
    pg.deleteLater()


def test_one_card_sits_at_the_left_edge(qapp):
    """一张卡：更不该出现“唯一一列独占整屏宽、卡片跑到屏幕当中”"""
    pg = _shown(qapp)
    xs = _place(pg, qapp, 1)
    assert xs[0] <= 20
    pg.deleteLater()


def test_columns_shrink_leaves_no_ghost_stretch(qapp):
    """窗口先宽后窄（列数变小）：旧的“余量列”不能留在右边继续挖坑"""
    pg = _shown(qapp)
    _place(pg, qapp, 12)                      # 先按宽屏铺满一行
    pg.resize(1000, 900)
    qapp.processEvents()                      # 视口变窄 → 列数跟着变小
    xs = _place(pg, qapp, 2)
    assert xs[1] - xs[0] == CARD_W + GRID_GAP
    pg.deleteLater()
