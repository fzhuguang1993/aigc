"""
tests/test_multi_select_mode.py —— 成品/素材库「多选」交互口径（回归）

约定（用户反馈敲定）：
- 多选不是默认开的，也没有顶栏开关：空白处拖动框选即自动开启多选；
  右键空白处提供 多选开关 / 全选 / 反选；
- 未开多选：素材条目不可勾选、成品卡选框隐藏，点击照常预览；
- 开启多选后：点条目/卡片任意位置都切换勾选（触发区=整张卡，勾选框也能点），且绝不跳播放；
- 「操作」按钮：无多选无勾选叫「操作」，否则叫「批量操作」；文本不带 ▾（setMenu 自带箭头）。

卡片网格自 t3 起整体上收为共享组件 gui.media_grid.MediaGrid：多选开关/选框显隐/
勾选集合/橡皮筋框选都在组件里，页面通过 page.grid 暴露。本回归据此改走 grid API。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 早于任何 PySide6 导入

from pathlib import Path

import pytest

from PySide6.QtCore import Qt, QEvent, QPointF
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from store import db, material_store


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _no_asr_autorun():
    """后台口播识别默认开：测试机构造成品库页会触发 _maybe_asr。关掉它避免
    真有就绪模型时起线程去转写测试用的假视频（行为回归不该依赖 Whisper）。"""
    from store import app_state
    old = app_state.get("output_asr_autorun", True)
    app_state.set_value("output_asr_autorun", False)
    yield
    app_state.set_value("output_asr_autorun", old)


def _mk_clips(tmp_path, names):
    db.execute("DELETE FROM material_clips")
    for nm in names:
        f = tmp_path / nm
        f.write_bytes(b"x")
        material_store.add(str(f), block_type="钩子", product="骨胶原")


def _check_visible(page):
    """卡片选框是否可见（未 show 的窗体用 isVisibleTo 判显隐位，与旧实现同口径）。"""
    return [bool(c.chk.isVisibleTo(c)) for c in page.grid._cards]


def _menu_texts(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def _release(widget, x=10, y=10, button=Qt.MouseButton.LeftButton):
    """在局部坐标 (x,y) 合成一次鼠标释放，直接喂给 widget.mouseReleaseEvent。"""
    lp = QPointF(x, y)
    ev = QMouseEvent(QEvent.Type.MouseButtonRelease, lp, lp, button,
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    widget.mouseReleaseEvent(ev)


def test_material_multi_default_off_and_toggle(qapp, tmp_path):
    from gui.pages_material import MaterialPage
    _mk_clips(tmp_path, ("c1.mp4", "c2.mp4"))
    page = None
    try:
        page = MaterialPage()
        assert page.grid.count() == 2
        # 默认关：没有勾选框，按钮叫「操作」（且文本不带 ▾，防两颗箭头回锅）
        assert not any(_check_visible(page)), "默认关闭时选框不应可见"
        assert page.b_ops.text() == "⚙ 操作", page.b_ops.text()
        # 开启多选（框选/右键菜单走的是同一个入口）：卡片出选框，按钮改叫「批量操作」
        page.grid.set_multi(True)
        assert all(_check_visible(page)), "开启后选框应可见"
        assert page.b_ops.text() == "⚙ 批量操作", page.b_ops.text()
        assert page.grid.multi_on() is True
        # 右键菜单入口：全选 / 反选都会先自动开启多选
        page._act_select_all()
        assert page.grid.multi_on() and len(page.grid.selected()) == 2
        page._act_invert()
        assert len(page.grid.selected()) == 0, page.grid.selected()
        page._act_invert()
        assert len(page.grid.selected()) == 2
        # 关掉：选框收起、已选清空
        page.grid.set_multi(False)
        assert len(page.grid.selected()) == 0
        assert not any(_check_visible(page))
    finally:
        if page is not None:
            page.close()   # closeEvent 里停缩略图线程，不然解释器退出时 QThread 报错
        db.execute("DELETE FROM material_clips")


def test_material_check_syncs_selected(qapp, tmp_path):
    """勾选某张卡的选框：要回写组件的已选集合（等价旧「框选结果同步勾选」）。"""
    from gui.pages_material import MaterialPage
    _mk_clips(tmp_path, ("c1.mp4", "c2.mp4"))
    page = None
    try:
        page = MaterialPage()
        page.grid.set_multi(True)
        page.grid._cards[0].chk.setChecked(True)
        assert len(page.grid.selected()) == 1, page.grid.selected()
        assert page.grid._cards[0].chk.isChecked()
    finally:
        if page is not None:
            page.close()
        db.execute("DELETE FROM material_clips")


def test_output_card_click_no_play_in_multi(qapp, tmp_path):
    """多选开启时点击卡片：只切换勾选，绝不触发 on_open（跳播放）；关多选则照常预览。"""
    from gui.media_grid import MediaCard, _thumb_tile
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    row = {"path": str(f), "name": "v.mp4"}

    # 多选态：点整张卡 = 切勾选，不预览
    opened, checked = [], []
    card = MediaCard(row, {"name": "v"}, key=str(f),
                     on_open=lambda r: opened.append(r),
                     on_menu=lambda *a: None,
                     on_check=lambda k, c: checked.append((k, c)),
                     multi_on=lambda: True, tile=_thumb_tile)
    assert not card.chk.isVisibleTo(card), "默认多选关着，选框应隐藏"
    card.chk.setVisible(True)              # 模拟页面开启多选后的显隐
    _release(card)                          # 走的就是点击卡片时的那条分流
    assert opened == [] and checked and checked[0][1] is True, (opened, checked)

    # 多选关闭态：点击卡片照常走预览回调，不产生勾选
    opened2, checked2 = [], []
    card2 = MediaCard(row, {"name": "v"}, key=str(f),
                      on_open=lambda r: opened2.append(r),
                      on_menu=lambda *a: None,
                      on_check=lambda k, c: checked2.append((k, c)),
                      multi_on=lambda: False, tile=_thumb_tile)
    assert not card2.chk.isVisibleTo(card2)
    _release(card2)
    assert opened2 and not checked2, (opened2, checked2)


def test_material_empty_menu_and_sort(qapp, tmp_path):
    """空白右键菜单的多选开关严格跟随 grid.multi_on()：未开=开启多选，已开=退出多选。

    菜单项不得 setCheckable（windowsvista 会画原生框+钩，很丑），当前排序用「✓ 」前缀。"""
    from gui.pages_material import MaterialPage
    _mk_clips(tmp_path, ("b素材.mp4", "a素材.mp4"))   # 先 b 后 a：默认 id 倒序 a 在前
    page = None
    try:
        page = MaterialPage()
        texts = _menu_texts(page._empty_menu())
        assert texts[0] == "开启多选", texts
        assert "↕ 排序" in texts and "🌲 隐藏筛选树" in texts, texts
        acts = page._empty_menu().actions()
        assert not any(a.isCheckable() for a in acts if not a.isSeparator())
        sub = [a for a in acts if a.menu()][0].menu()
        sub_acts = sub.actions()
        assert not any(a.isCheckable() for a in sub_acts), "排序项也不能用原生勾选框"
        assert sub_acts[0].text().startswith("✓ "), sub_acts[0].text()
        assert all(not a.text().startswith("✓") for a in sub_acts[1:])
        # 已开启多选（哪怕一条没选）：顶上立即是「退出多选」，批量项同时列出
        page.grid.set_multi(True)
        texts = _menu_texts(page._empty_menu())
        assert texts[0] == "退出多选", texts
        for want in ("全选", "反选", "🏷 绑定产品…", "✏ 批量重命名…",
                     "🗑 删除片段（进回收站）", "↕ 排序"):
            assert want in texts, (want, texts)
        # 选满 2 条后：顶上仍是「退出多选」
        page._act_select_all()
        assert _menu_texts(page._empty_menu())[0] == "退出多选"
        # 默认排序：时间新→旧（后入库的 a 在前）；切名称排序后顺序跟着变
        assert [Path(r["path"]).name for r in page._rows] == ["a素材.mp4", "b素材.mp4"]
        page._pick_sort("name_desc")
        assert [Path(r["path"]).name for r in page._rows] == ["b素材.mp4", "a素材.mp4"]
        page._pick_sort("name_asc")
        assert [Path(r["path"]).name for r in page._rows] == ["a素材.mp4", "b素材.mp4"]
    finally:
        if page is not None:
            page.close()
        db.execute("DELETE FROM material_clips")


def test_output_empty_menu_and_sort(qapp, tmp_path, monkeypatch):
    """成品库：右键菜单同款结构；排序按生成日期/名称重排卡片。"""
    from store import output_store
    from gui.pages_output_lib import OutputLibPage
    rows = []
    for nm, day in (("b品.mp4", "2026-09-01 10:00"), ("a品.mp4", "2026-09-02 10:00")):
        f = tmp_path / nm
        f.write_bytes(b"x")
        rows.append({"path": str(f), "name": nm, "product": "", "mark": "",
                     "bound": False, "size": 1, "created_at": day, "_ts": 0,
                     "display": ""})
    monkeypatch.setattr(output_store, "list_outputs",
                        lambda product="", mark="", **kw: list(rows))
    page = OutputLibPage()
    try:
        texts = _menu_texts(page._empty_menu())
        # 未开多选：入口 + 名称显示切换（物理名/外显名）+ 筛选树显隐 + 排序
        assert texts == ["开启多选", "📄 显示物理名称（带后缀）", "🌲 隐藏筛选树",
                         "↕ 排序"], texts
        page.grid.set_multi(True)
        texts = _menu_texts(page._empty_menu())
        # 已开启多选（哪怕一条没选）：顶上立即是「退出多选」，批量项同时列出
        assert texts[0] == "退出多选" and "🏷 绑定产品…" in texts and texts[-1] == "↕ 排序", texts
        assert "🗑 移入回收站" in texts and "🏷 批量设置显示名称…" in texts, texts
        assert page.grid.multi_on() is True and page._sort == "time_desc"
        # 选满 2 条后：顶上仍是「退出多选」
        page._act_select_all()
        assert _menu_texts(page._empty_menu())[0] == "退出多选"
        # 默认时间新→旧：a品(09-02) 在前
        assert [c.row["name"] for c in page.grid._cards] == ["a品.mp4", "b品.mp4"]
        page._pick_sort("time_asc")
        assert [c.row["name"] for c in page.grid._cards] == ["b品.mp4", "a品.mp4"]
        page._pick_sort("name_asc")
        assert [c.row["name"] for c in page.grid._cards] == ["a品.mp4", "b品.mp4"]
    finally:
        page.close()


def test_empty_menu_toggle_never_noop(qapp, tmp_path):
    """回归「严重 bug」：多选开关文案必须恒等于 grid.multi_on()，且点顶部项必定切换。

    旧逻辑用「已选≥２」判断文案，一旦多选已开却已选<2（框选未框到/勾选又改回），
    菜单会显示「开启多选」→ 点下去因已开而直接 return（失效）→ 用户被困在多选态。
    修正后开关只看 multi_on，不存在 no-op。"""
    from gui.pages_material import MaterialPage
    _mk_clips(tmp_path, ("c1.mp4", "c2.mp4", "c3.mp4"))
    page = None
    try:
        page = MaterialPage()
        # 未开：顶部=开启多选，点后确实变开
        assert _menu_texts(page._empty_menu())[0] == "开启多选"
        page._empty_menu().actions()[0].trigger()
        assert page.grid.multi_on() is True
        # 多选态、一条未选（陷阱态）：顶部应是「退出多选」，点后确实退出
        assert _menu_texts(page._empty_menu())[0] == "退出多选"
        page._empty_menu().actions()[0].trigger()
        assert page.grid.multi_on() is False
        # 再开→勾 1 条（仍 <2）：顶部依旧「退出多选」，不是「开启多选」（旧 bug 核心）
        page.grid.set_multi(True)
        page.grid._cards[0].chk.setChecked(True)
        assert page.grid.multi_on() and len(page.grid.selected()) == 1
        top = page._empty_menu().actions()[0]
        assert top.text() == "退出多选", top.text()
        top.trigger()
        assert page.grid.multi_on() is False
    finally:
        if page is not None:
            page.close()
        db.execute("DELETE FROM material_clips")


def test_output_right_release_does_not_enable_multi(qapp, tmp_path, monkeypatch):
    """回归「右键直接开启多选」 bug：上一段框选残留的大 geometry 不得被右键释放误用。

    旧代码 Release 分支没判左键/手势是否进行中，QRubberBand 一旦建就常驻、
    geometry 是脏的，右键释放会拿旧大矩形落到 set_multi(True)。框选逻辑现居 MediaGrid。"""
    from PySide6.QtCore import QRect, QPoint
    from PySide6.QtWidgets import QRubberBand
    from store import output_store
    from gui.pages_output_lib import OutputLibPage
    rows = []
    for nm in ("a.mp4", "b.mp4"):
        f = tmp_path / nm
        f.write_bytes(b"x")
        rows.append({"path": str(f), "name": nm, "product": "", "mark": "",
                     "bound": False, "size": 1, "created_at": "2026-09-02 10:00",
                     "_ts": 0, "display": ""})
    monkeypatch.setattr(output_store, "list_outputs",
                        lambda product="", mark="", **kw: list(rows))
    page = OutputLibPage()
    grid = page.grid
    try:
        # 直接造「上一段真实框选遗留」的状态：rubber 已存、geometry 为大矩形，无进行中手势
        if grid._rubber is None:
            grid._rubber = QRubberBand(QRubberBand.Shape.Rectangle, grid._holder)
        grid._rubber.setGeometry(QRect(0, 0, 60, 60))
        grid._rub_anchor = None
        grid.set_multi(False)
        # 右键释放：不得误开多选
        grid.eventFilter(grid._holder,
                         _mouse_release(grid._holder, 30, 30, Qt.MouseButton.RightButton))
        assert grid.multi_on() is False, "右键释放误开启了多选"
        # 对照：左键释放 + 有进行中的手势（anchor 非空）仍正常框选开多选
        grid._rub_anchor = QPoint(0, 0)
        grid.eventFilter(grid._holder,
                         _mouse_release(grid._holder, 60, 60, Qt.MouseButton.LeftButton))
        assert grid.multi_on() is True, "正常的左键框选应能开启多选"
    finally:
        page.close()


def _mouse_release(widget, x, y, button):
    """给 eventFilter 造一个局部坐标在 (x,y) 的鼠标释放事件。"""
    lp = QPointF(x, y)
    buttons = button if button == Qt.MouseButton.LeftButton else Qt.MouseButton.NoButton
    return QMouseEvent(QEvent.Type.MouseButtonRelease, lp, lp, button,
                       buttons, Qt.KeyboardModifier.NoModifier)


def test_output_trash_view_and_names(qapp, tmp_path, monkeypatch):
    """回收站视图切换 + 卡片名称口径：默认外显名（去后缀），可切物理名（带后缀）。"""
    from store import output_store
    from gui.pages_output_lib import OutputLibPage
    f = tmp_path / "001_关节不舒服.mp4"
    f.write_bytes(b"x")
    lib_rows = [{"path": str(f), "name": f.name, "product": "", "mark": "",
                 "bound": False, "size": 1, "created_at": "2026-09-02 10:00",
                 "_ts": 0, "display": ""}]
    trash_rows = [{"path": str(f), "name": f.name, "origin_path": str(f),
                   "size": 1, "trashed_at": "2026-09-30 10:00:00",
                   "created_at": "2026-09-02 10:00", "display": ""}]
    monkeypatch.setattr(output_store, "list_outputs", lambda **kw: list(lib_rows))
    monkeypatch.setattr(output_store, "list_trash", lambda: list(trash_rows))
    page = OutputLibPage()
    try:
        # 默认外显名：去后缀
        assert page.grid._cards[0]._lbl_name._text == "001_关节不舒服"
        # 切物理名 → 带后缀；再切回
        page._toggle_physical()
        assert page._show_physical and page.grid._cards[0]._lbl_name._text == "001_关节不舒服.mp4"
        page._toggle_physical()
        assert not page._show_physical
        # 切到回收站视图：渲染回收站行、卡片标「已在回收站」
        page._set_view("trash")
        assert page._is_trash() and page.grid._cards[0].lbl_tags.text() == "🗑 已在回收站"
        # 回收站态空白菜单：未开多选只入口+排序；开多选后给还原/彻底删除/清空
        assert _menu_texts(page._empty_menu()) == ["开启多选", "↕ 排序"]
        page.grid.set_multi(True)
        ttexts = _menu_texts(page._empty_menu())
        assert "♻ 还原选中" in ttexts and "🗑 彻底删除选中" in ttexts
        assert "🧹 清空回收站" in ttexts and "🗑 移入回收站" not in ttexts
    finally:
        page.close()
