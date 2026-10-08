"""
tests/test_settings_filesearch_card.py —— 「本地文件搜索」设置卡接线自检

这张卡没有一个「保存」按钮：勾一下、加一个目录、点一次重建都得当场落盘并催后台
线程。最容易出的事故依旧是 handler 名字写错/漏连——界面照常画出来，用户一点就
AttributeError（与「托盘与全局快捷键」卡同一个坑）。这里把每个入口都点一遍，
另外盯死两条测试纪律：
① 索引库路径必须顶到临时目录：本机那份真索引是 95 万行 / 700 多 MB（实测），
   测试碰它一次就等于在自己的 CI 上埋一颗慢弹；
② 后台线程一律不许真起来：start/stop/kick 全换成计数器。起线程的测试不受控，
   它什么时候写库、写哪份库都没人知道。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 早于任何 PySide6 导入

import pytest
from PySide6.QtWidgets import QApplication

from core import fileindex
from gui.pages_settings import SettingsPage
from store import app_state
from workers import file_watcher


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _worker(tmp_path, monkeypatch):
    """偏好与索引库都指到临时目录，后台线程整条腿顶掉（只记谁调用过）"""
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui_state.json")
    monkeypatch.setattr(fileindex, "DB_PATH", tmp_path / "idx" / "file_index.db")
    fileindex.close()
    hits = {"start": 0, "stop": 0, "kick": 0}
    monkeypatch.setattr(file_watcher, "start",
                        lambda: hits.__setitem__("start", hits["start"] + 1) or True)
    monkeypatch.setattr(file_watcher, "stop",
                        lambda *a: hits.__setitem__("stop", hits["stop"] + 1) or True)
    monkeypatch.setattr(file_watcher, "kick",
                        lambda: hits.__setitem__("kick", hits["kick"] + 1) or True)
    monkeypatch.setattr(file_watcher, "running", lambda: False)
    yield hits
    fileindex.close()


@pytest.fixture
def page(qapp):
    p = SettingsPage()
    yield p
    p.deleteLater()


# ---------------- 卡片本身 ----------------

def test_card_has_every_control(page):
    for name in ("ck_filesearch", "combo_fs_scope", "fs_roots_box", "lst_fs_roots",
                 "ck_docsearch", "fs_doc_box", "lst_doc_roots", "spin_doc_mb",
                 "combo_fs_view", "lbl_filesearch"):
        assert hasattr(page, name), "本地文件搜索卡缺控件：%s" % name


def test_defaults_are_on_full_disk_docs_20mb(page):
    """产品口径（用户定的四条里"启动就扫全盘"那条）：默认开、默认全盘、默认搜正文"""
    assert page.ck_filesearch.isChecked() is True
    assert page.combo_fs_scope.currentIndex() == 0
    assert page.ck_docsearch.isChecked() is True
    assert page.spin_doc_mb.value() == 20
    # 选全盘时不该还摊着一个空目录清单（用户会以为"什么都没选"）
    assert page.fs_roots_box.isHidden()


def test_status_line_is_filled_on_build(page):
    t = page.lbl_filesearch.text()
    assert "已收录" in t and "还没扫过" in t, "状态行得说得出当前进度：" + t


# ---------------- 开关：点一下就得停/起后台线程 ----------------

def test_unchecking_stops_the_worker(page, _worker):
    page.ck_filesearch.setChecked(False)
    assert fileindex.enabled() is False
    assert _worker["stop"] == 1
    for w in (page.combo_fs_scope, page.ck_docsearch, page.spin_doc_mb):
        assert w.isEnabled() is False


def test_rechecking_starts_and_kicks(page, _worker):
    page.ck_filesearch.setChecked(False)
    page.ck_filesearch.setChecked(True)
    assert fileindex.enabled() is True
    assert _worker["start"] >= 1 and _worker["kick"] >= 1


def test_doc_toggle_hides_its_detail_box(page, _worker):
    """关掉正文搜索就把目录与上限收起来：留着只会让人以为还在抽"""
    page.ck_docsearch.setChecked(False)
    assert fileindex.doc_enabled() is False
    assert page.fs_doc_box.isHidden()
    page.ck_docsearch.setChecked(True)
    assert not page.fs_doc_box.isHidden()
    assert _worker["kick"] >= 2


def test_max_mb_roundtrips_into_the_indexer(page):
    page.spin_doc_mb.setValue(50)
    assert fileindex.doc_max_bytes() == 50 * 1024 * 1024


# ---------------- 唤出面板默认视图 ----------------

def test_default_view_defaults_to_list(page):
    """没设过偏好时下拉回到“列表”（index 0）。"""
    assert page.combo_fs_view.currentIndex() == 0


def test_default_view_reads_saved_pref(qapp, tmp_path, monkeypatch):
    """先写 launcher_view_default=large，再建页：初值得回填到对应项。"""
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui_state.json")
    app_state.set_value("launcher_view_default", "large")
    p = SettingsPage()
    try:
        assert p.combo_fs_view.currentIndex() == 2
    finally:
        p.deleteLater()


def test_changing_default_view_persists(page):
    """改下拉即时写 launcher_view_default（无保存按钮）。"""
    page.combo_fs_view.setCurrentIndex(1)
    assert app_state.get("launcher_view_default") == "medium"
    page.combo_fs_view.setCurrentIndex(2)
    assert app_state.get("launcher_view_default") == "large"


# ---------------- 目录清单 ----------------

def test_scope_switch_then_back_clears_the_list(page, _worker):
    page.combo_fs_scope.setCurrentIndex(1)
    assert not page.fs_roots_box.isHidden()
    page.lst_fs_roots.addItem("D:\\项目")
    page._fs_save_roots(page._dir_paths(page.lst_fs_roots))
    assert fileindex.custom_roots() == ["D:\\项目"]
    assert fileindex.search_roots() == ["D:\\项目"]      # 指定目录真的改了扫描口径
    assert _worker["kick"] >= 1
    page.combo_fs_scope.setCurrentIndex(0)               # 切回全盘：清单清空并落盘
    assert fileindex.custom_roots() == []
    assert page.lst_fs_roots.count() == 0
    assert fileindex.search_roots() == fileindex.local_drives()


def test_emptying_the_list_does_not_fall_back_to_nothing(page):
    """列表空着时不写字面：空清单本身就等于"全盘"，写下去等于改了个寂寞"""
    page.combo_fs_scope.setCurrentIndex(1)
    page._fs_save_roots([])
    assert fileindex.custom_roots() == []


def test_doc_roots_default_when_cleared(page):
    page._doc_save_roots([])
    assert fileindex.doc_roots() == fileindex.default_doc_roots()


def test_remove_without_a_selection_only_says_so(page):
    page.lst_fs_roots.clear()
    page.lbl_filesearch.setText("")
    page._dir_del(page.lst_fs_roots, page._fs_save_roots)   # 不抛
    assert "选中" in page.lbl_filesearch.text()


# ---------------- 重建索引 ----------------

def test_rebuild_button_is_wired(page):
    """没接线就是“看着能点、点了没反应”：直接查信号有没有连上 handler"""
    from PySide6.QtWidgets import QPushButton
    hits = [x for x in page.findChildren(QPushButton)
            if "重建索引" in x.text()]
    assert len(hits) == 1, "重建按钮应当有且只有一个"
    # 真去点一下会把模态确认框弹出来（离屏测试就挂在那儿），所以只问接没接。
    # “2clicked()” 是 Qt 的归一化签名串：clicked 有重载（带不带 checked），
    # 光写 "clicked" / "clicked()" 都认不出这个信号，receivers 永远返 0。
    assert hits[0].receivers("2clicked()") >= 1, "重建按钮没接线：点了不会有任何反应"


def test_rebuild_worker_rebuilds_then_kicks(page, _worker, monkeypatch):
    got = []
    monkeypatch.setattr(fileindex, "rebuild", lambda: got.append("rebuild") or True)
    page._fs_rebuild_worker()
    assert got == ["rebuild"]
    assert _worker["start"] >= 1 and _worker["kick"] >= 1


def test_rebuild_failure_is_reported_not_raised(page, qapp, monkeypatch):
    """库被占用/磁盘只读时 rebuild 会抛：不能让后台线程把整个软件带崩"""
    def boom():
        raise OSError("库被占用")

    monkeypatch.setattr(fileindex, "rebuild", boom)
    page._fs_rebuild_worker()                             # 不抛
    qapp.processEvents()                                  # 送回主线程那条 singleShot
    assert "重建失败" in page.lbl_filesearch.text()
    assert str(fileindex.DB_PATH) in page.lbl_filesearch.text(), "得告诉他库在哪儿才能手动删"


# ---------------- 状态行的取数节奏 ----------------

def test_status_query_is_throttled(page, monkeypatch):
    """本页是 2 秒一刷，而 COUNT(*) 在百万行表上要扫全表：不夹住就是常驻负载"""
    calls = []

    def fake():
        calls.append(1)
        return {"files": 1, "docs": 0, "last_scan": 0, "last_docs": 0}

    monkeypatch.setattr(fileindex, "status", fake)
    page._fs_stat_at = 0
    for _ in range(3):
        page._refresh_filesearch_status()
    assert len(calls) == 1


def test_status_survives_a_broken_db(page, monkeypatch):
    def boom():
        raise RuntimeError("库坏了")

    monkeypatch.setattr(fileindex, "status", boom)
    page._fs_stat_at = 0
    page._refresh_filesearch_status()                     # 不抛
    assert "暂不可用" in page.lbl_filesearch.text()
