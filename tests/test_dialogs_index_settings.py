"""
tests/test_dialogs_index_settings.py —— 每盘符索引设置对话框：勾选持久化 + 重扫走异步

这个对话框最容易出的两件事：① 勾了哪几个盘要真的写进 custom_roots（不写就等于
每次打开都退回全盘口径，用户以为选了其实没选）；② 点"立即重扫"绝不能同步扫盘——
全盘一趟实测几十秒起步，同步跑在按钮点击所在的线程上就是"点完整个软件假死"。
这两条都靠 monkeypatch 顶掉真实的 local_drives()/file_watcher.reindex_roots()，
不需要真有一块盘、也不需要真起后台线程。
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from core import fileindex
from store import app_state
from workers import file_watcher
from gui.dialogs_index_settings import IndexSettingsDialog, _fmt_age, _fmt_n


@pytest.fixture(scope="module", autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui_state.json")
    fileindex.close()
    yield
    fileindex.close()


@pytest.fixture
def fake_drives(monkeypatch):
    monkeypatch.setattr(fileindex, "local_drives", lambda *a, **k: ["C:\\", "D:\\"])
    monkeypatch.setattr(fileindex, "root_status", lambda: [])


def test_fmt_age_boundaries():
    now = int(time.time())
    assert _fmt_age(0) == "未索引"
    assert _fmt_age(now - 30) == "刚刚更新"
    assert "分钟前" in _fmt_age(now - 600)
    assert "小时前" in _fmt_age(now - 7200)
    assert "天前" in _fmt_age(now - 3 * 86400)


def test_fmt_n():
    assert _fmt_n(1234) == "1,234"
    assert _fmt_n(None) == "-"


def test_dialog_builds_a_row_per_drive_and_defaults_to_all_checked(fake_drives):
    dlg = IndexSettingsDialog()
    assert set(dlg._rows.keys()) == {"C:\\", "D:\\"}
    for row in dlg._rows.values():
        assert row.ck.isChecked()          # 没配过 custom_roots＝全盘口径，都该勾上


def test_partial_toggle_persists_to_custom_roots(fake_drives):
    dlg = IndexSettingsDialog()
    dlg._rows["D:\\"].ck.setChecked(False)     # 触发 _on_toggle：只留 C 盘
    assert fileindex.custom_roots() == ["C:\\"]

    dlg._rows["D:\\"].ck.setChecked(True)      # 重新勾满＝回到全盘口径，写空列表
    assert fileindex.custom_roots() == []


def test_uncheck_all_falls_back_to_full_drive_scope(fake_drives):
    dlg = IndexSettingsDialog()
    dlg._rows["C:\\"].ck.setChecked(False)
    dlg._rows["D:\\"].ck.setChecked(False)
    assert fileindex.custom_roots() == [], \
        "全不勾也该写空（＝全盘口径），不能写成 [] 之外的脏值把范围卡死在'没有盘'"


def test_reindex_goes_through_async_queue_not_sync_scan(monkeypatch, fake_drives):
    """点重扫只该把请求塞进 file_watcher 的定向队列，绝不在这里同步扫盘。"""
    calls = []
    monkeypatch.setattr(file_watcher, "reindex_roots",
                        lambda roots: calls.append(list(roots)) or True)
    dlg = IndexSettingsDialog()
    ok = dlg._reindex_roots(["C:\\"])
    assert ok is True
    assert calls == [["C:\\"]]


def test_reindex_reports_when_feature_disabled(monkeypatch, fake_drives):
    monkeypatch.setattr(file_watcher, "reindex_roots", lambda roots: False)
    dlg = IndexSettingsDialog()
    ok = dlg._reindex_roots(["C:\\"])
    assert ok is False
    assert "启用" in dlg.lbl_status.text()
