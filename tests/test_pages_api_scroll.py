"""
tests/test_pages_api_scroll.py —— 接口管理页两处修复的冒烟（offscreen，不弹窗）

用户反馈两个问题：
  1. 页面长度固定死、内容变长不跟着长（卡片被 QStackedWidget 裁掉，下面的够不着）
     —— 现在整页套 QScrollArea，页面随内容变长可滚动；
  2. 语音识别词库要能用 Excel/CSV/JSON 维护 —— 词库卡加了「导入/导出」入口。
这里只验证装配到位（滚动区存在、词库框与导入/导出方法在），不点真实文件对话框。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_api_page_is_scrolled(qapp):
    from PySide6.QtWidgets import QScrollArea
    from gui.pages_api import ApiManagerPage
    page = ApiManagerPage()
    # 整页应被包在一个 QScrollArea 里（内容超屏时可滚，不再被裁）
    assert page.findChild(QScrollArea) is not None, "接口管理页未套 QScrollArea"


def test_api_page_glossary_file_io_wired(qapp):
    from gui.pages_api import ApiManagerPage
    page = ApiManagerPage()
    assert hasattr(page, "ed_gloss")                 # 词库文本框仍在
    assert callable(page._import_glossary)           # Excel/CSV/JSON 导入入口
    assert callable(page._export_glossary)           # 导出为文件入口
