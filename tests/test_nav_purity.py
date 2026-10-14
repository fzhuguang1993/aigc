"""
tests/test_nav_purity.py —— 侧栏导航纯净度（新手永久下线 / 组织与UI库口令解锁）

钉住这次的入口改动：
1. 「新手入门」永久从导航去掉（页面还在栈里，索引不重排，零风险）；
2. 「组织结构」「UI 组件库」默认不进导航，只有维护人口令解锁（reveal_api_page）
   后才出现，和「接口管理」同进同出；
3. 悬浮球扇形（launcher_fan）不把这三页当双击目标漏出。

只读 _NAV_PAGES / _page_items 静态表，不实例化窗口，offscreen 即可。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _nav_index_set():
    import gui.main_window as mw
    return {i for _t, i in mw._NAV_PAGES}


def test_guide_page_never_in_nav():
    """新手入门（页 5）永久移出导航，标题也不许回潮。"""
    import gui.main_window as mw
    assert 5 not in _nav_index_set()
    assert not any("新手" in t for t, _i in mw._NAV_PAGES)


def test_org_and_uikit_hidden_by_default():
    """组织结构（8）与 UI 组件库（14）默认不进导航。"""
    idxes = _nav_index_set()
    assert 8 not in idxes
    assert 14 not in idxes


def test_nav_still_covers_everyday_pages():
    """常用页一个都不能少：看板/任务/产品/负载/工具/基建/拆解/设置。"""
    assert _nav_index_set() == {0, 1, 2, 3, 4, 6, 9, 13}


def test_fan_excludes_locked_pages():
    """悬浮球扇形双击目标不含 新手5 / 组织8 / UI库14。"""
    from gui.launcher_fan import _page_items
    ids = {idx for idx, _label, _icon in _page_items()}
    for hidden in (5, 8, 14):
        assert hidden not in ids


def test_reveal_flag_defaults_in_source():
    """三个解锁标志位初始必须为 False（出厂即隐藏）。"""
    import inspect
    import gui.main_window as mw
    src = inspect.getsource(mw.MainWindow.__init__)
    for flag in ("_api_revealed", "_org_revealed", "_uikit_revealed"):
        assert f"self.{flag} = False" in src
