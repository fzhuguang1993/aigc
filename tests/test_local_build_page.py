"""
tests/test_local_build_page.py —— 批量基建独立页的导航接线与页栈装配

offscreen 构建（不弹窗），验证：
- 「🏗  批量基建」在 _NAV_PAGES 且页栈索引为 13；导航索引无重复、新页在末尾就位；
- LocalBuildPage 可离屏实例化，四个分层 Tab（客户/执照/账户/搭建）齐全、刷新不抛；
- 旧工具卡片入口已下线（TOOLS 与 PANEL_FACTORIES 不再含「批量基建」）。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_nav_entry_and_index():
    import gui.main_window as mw
    assert ("🏗  批量基建", 13) in mw._NAV_PAGES
    idxes = [i for _t, i in mw._NAV_PAGES]
    assert 13 in idxes
    assert len(idxes) == len(set(idxes))          # 无重复索引
    # 页栈装配数：新页追加末尾，0..14 共 15 页（含索引 14 的「🎛 UI 组件库」）；
    # 导航未列的 7/8/10/11/12 由代码常驻
    assert sorted(set(idxes) | {7, 8, 10, 11, 12}) == list(range(15))


def test_local_build_page_four_tabs(qapp):
    from gui.pages_local_build import LocalBuildPage
    page = LocalBuildPage()
    titles = [page.tabs.tabText(i) for i in range(page.tabs.count())]
    assert page.tabs.count() == 4
    assert any("客户" in t for t in titles)
    assert any("执照" in t for t in titles)
    assert any("账户" in t for t in titles)
    assert any("搭建" in t for t in titles)
    page.refresh()          # 离屏刷新不抛


def test_tool_entry_removed():
    from gui.pages_tools import TOOLS
    from gui.tool_panels import PANEL_FACTORIES
    names = {n for n, _i, _d, _f in TOOLS}
    assert "批量基建" not in names
    assert "批量基建" not in PANEL_FACTORIES
