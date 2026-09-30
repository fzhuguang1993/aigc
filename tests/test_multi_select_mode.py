"""
tests/test_multi_select_mode.py —— 成品/素材库「多选」交互口径（回归）

约定（用户反馈敲定）：
- 多选不是默认开的，也没有顶栏开关：空白处拖动框选即自动开启多选；
  右键空白处提供 多选开关 / 全选 / 反选；
- 未开多选：素材条目不可勾选、成品卡选框隐藏，点击/双击照常预览；
- 开启多选后：点条目/卡片任意位置都切换勾选（触发区=整个 item，勾选框也能点），
  且绝不跳播放；
- 「操作」按钮：无多选无勾选叫「操作」，否则叫「批量操作」；文本不带 ▾
  （setMenu 自带箭头，手写会变两颗）。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 早于任何 PySide6 导入

from pathlib import Path

import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from store import db, material_store


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _mk_clips(tmp_path, names):
    db.execute("DELETE FROM material_clips")
    for nm in names:
        f = tmp_path / nm
        f.write_bytes(b"x")
        material_store.add(str(f), block_type="钩子", product="骨胶原")


def _flags_checkable(page):
    return [bool(page.list.item(i).flags() & Qt.ItemFlag.ItemIsUserCheckable)
            for i in range(page.list.count())]


def test_material_multi_default_off_and_toggle(qapp, tmp_path):
    from gui.pages_material import MaterialPage
    _mk_clips(tmp_path, ("c1.mp4", "c2.mp4"))
    page = None
    try:
        page = MaterialPage()
        assert page.list.count() == 2
        # 默认关：没有勾选框，按钮叫「操作」（且文本不带 ▾，防两颗箭头回锅）
        assert not any(_flags_checkable(page)), "默认关闭时条目不应可勾选"
        assert page.b_ops.text() == "⚙ 操作", page.b_ops.text()
        # 开启多选（框选/右键菜单走的是同一个入口）：条目出勾选框，按钮改叫「批量操作」
        page._set_multi(True)
        assert all(_flags_checkable(page)), "开启后条目应可勾选"
        assert page.b_ops.text() == "⚙ 批量操作", page.b_ops.text()
        # 开启后框选机制就位：MultiSelection 才有点击切换 + 橡皮筋
        from PySide6.QtWidgets import QAbstractItemView
        assert page.list.selectionMode() == QAbstractItemView.SelectionMode.MultiSelection
        # 右键菜单入口：全选 / 反选都会先自动开启多选
        page._act_select_all()
        assert page._multi and len(page._checked) == 2
        page._act_invert()
        assert len(page._checked) == 0, page._checked
        page._act_invert()
        assert len(page._checked) == 2
        # 关掉：勾选框收起、已选清空
        page._set_multi(False)
        assert len(page._checked) == 0
        assert not any(_flags_checkable(page))
    finally:
        if page is not None:
            page.close()   # closeEvent 里停缩略图线程，不然解释器退出时 QThread 报错
        db.execute("DELETE FROM material_clips")


def test_material_selection_syncs_checks(qapp, tmp_path):
    """模拟鼠标框选的结果（selectionModel 变化）要回写勾选框与已选集合。"""
    from gui.pages_material import MaterialPage
    from PySide6.QtCore import QItemSelection, QItemSelectionModel
    _mk_clips(tmp_path, ("c1.mp4", "c2.mp4"))
    page = None
    try:
        page = MaterialPage()
        page._set_multi(True)
        sm = page.list.selectionModel()
        sel = QItemSelection(page.list.model().index(0, 0),
                             page.list.model().index(0, 0))
        sm.select(sel, QItemSelectionModel.SelectionFlag.Clear
                  | QItemSelectionModel.SelectionFlag.Select)
        assert len(page._checked) == 1, page._checked
        assert page.list.item(0).checkState() == Qt.CheckState.Checked
    finally:
        if page is not None:
            page.close()
        db.execute("DELETE FROM material_clips")


def test_output_card_click_no_play_in_multi(qapp, tmp_path):
    """多选开启时点击卡片：只切换勾选，绝不触发 on_open（跳播放）。"""
    from gui.pages_output_lib import _OutputCard
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    opened, checked = [], []
    card = _OutputCard({"path": str(f), "name": "v.mp4"},
                       on_open=lambda r: opened.append(r),
                       on_menu=lambda *a: None,
                       on_check=lambda p, c: checked.append((p, c)),
                       is_multi=lambda: True)
    # isVisibleTo：只看自身显隐位（卡片没 show，isVisible 永远是 False，断言不住）
    assert not card.chk.isVisibleTo(card), "默认多选关着，选框应隐藏"
    card.chk.setVisible(True)          # 模拟页面开启多选后的显隐
    card.chk.toggle()                  # 走的就是点击卡片时的那条切换路
    assert opened == [] and checked and checked[0][1] is True

    # 多选关闭态：点击卡片照常走预览回调
    opened2 = []
    card2 = _OutputCard({"path": str(f), "name": "v.mp4"},
                        on_open=lambda r: opened2.append(r),
                        on_menu=lambda *a: None,
                        on_check=lambda p, c: None,
                        is_multi=lambda: False)
    assert not card2.chk.isVisibleTo(card2)


def _menu_texts(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def test_material_empty_menu_and_sort(qapp, tmp_path):
    """空白右键菜单的多选开关严格跟随 self._multi：未开=开启多选，已开（哪怕只选了 0/1 条）=退出多选。

    菜单项不得 setCheckable（windowsvista 会画原生框+钩，很丑），当前排序用「✓ 」前缀。"""
    from gui.pages_material import MaterialPage
    _mk_clips(tmp_path, ("b素材.mp4", "a素材.mp4"))   # 先 b 后 a：默认 id 倒序 a 在前
    page = None
    try:
        page = MaterialPage()
        acts = page._empty_menu().actions()
        texts = [a.text() for a in acts if not a.isSeparator()]
        assert texts == ["开启多选", "↕ 排序"], texts
        assert not any(a.isCheckable() for a in acts if not a.isSeparator())
        sub = [a for a in acts if a.menu()][0].menu()
        sub_acts = sub.actions()
        assert not any(a.isCheckable() for a in sub_acts), "排序项也不能用原生勾选框"
        assert sub_acts[0].text().startswith("✓ "), sub_acts[0].text()
        assert all(not a.text().startswith("✓") for a in sub_acts[1:])
        # 已开启多选（哪怕一条没选）：顶上立即是「退出多选」，批量项同时列出
        # （开关文案严格跟随 self._multi，杜绝「已开却显示开启、点了没反应」的失效）
        page._set_multi(True)
        texts = _menu_texts(page._empty_menu())
        assert texts == ["退出多选", "全选", "反选",
                         "🏷 绑定产品…", "✏ 批量重命名…", "🗑 删除片段（进回收站）",
                         "↕ 排序"], texts
        # 选满 2 条后：顶上仍是「退出多选」
        page._select_all()
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
                     "bound": False, "size": 1, "created_at": day, "_ts": 0})
    monkeypatch.setattr(output_store, "list_outputs",
                        lambda product="", mark="", **kw: list(rows))
    page = OutputLibPage()
    try:
        texts = _menu_texts(page._empty_menu())
        # 未开多选：入口 + 名称显示切换（物理名/外显名）+ 排序
        assert texts == ["开启多选", "📄 显示物理名称（带后缀）", "↕ 排序"], texts
        page._set_multi(True)
        texts = _menu_texts(page._empty_menu())
        # 已开启多选（哪怕一条没选）：顶上立即是「退出多选」，批量项同时列出
        assert texts[0] == "退出多选" and "🏷 绑定产品…" in texts and texts[-1] == "↕ 排序", texts
        assert "🗑 移入回收站" in texts and "🏷 批量设置显示名称…" in texts, texts
        assert page._multi_on() is True and page._sort == "time_desc"
        # 选满 2 条后：顶上仍是「退出多选」
        page._select_all()
        assert _menu_texts(page._empty_menu())[0] == "退出多选"
        # 默认时间新→旧：a品(09-02) 在前
        assert [c.row["name"] for c in page._cards] == ["a品.mp4", "b品.mp4"]
        page._pick_sort("time_asc")
        assert [c.row["name"] for c in page._cards] == ["b品.mp4", "a品.mp4"]
        page._pick_sort("name_asc")
        assert [c.row["name"] for c in page._cards] == ["a品.mp4", "b品.mp4"]
    finally:
        page.close()


def test_empty_menu_toggle_never_noop(qapp, tmp_path):
    """回归「严重 bug」：多选开关文案必须恒等于 self._multi，且点顶部项必定切换。

    旧逻辑用「已选≥２」判断文案，一旦 _multi=True 却已选<2（框选未框到/
    勾选又改回），菜单会显示「开启多选」→ 点下去 _set_multi(True) 因已开而
    直接 return（失效）→ 用户被困在多选态。修正后开关只看 _multi，不存在 no-op。"""
    from gui.pages_material import MaterialPage
    _mk_clips(tmp_path, ("c1.mp4", "c2.mp4", "c3.mp4"))
    page = None
    try:
        page = MaterialPage()
        # 未开：顶部=开启多选，点后确实变开
        assert _menu_texts(page._empty_menu())[0] == "开启多选"
        page._empty_menu().actions()[0].trigger()
        assert page._multi is True
        # 多选态、一条未选（陷阱态）：顶部应是「退出多选」，点后确实退出
        assert _menu_texts(page._empty_menu())[0] == "退出多选"
        page._empty_menu().actions()[0].trigger()
        assert page._multi is False
        # 再开→勾 1 条（仍 <2）：顶部依旧「退出多选」，不是「开启多选」（旧 bug 核心）
        page._set_multi(True)
        page.list.item(0).setCheckState(Qt.CheckState.Checked)
        assert page._multi and len(page._checked) == 1
        top = page._empty_menu().actions()[0]
        assert top.text() == "退出多选", top.text()
        top.trigger()
        assert page._multi is False
    finally:
        if page is not None:
            page.close()
        db.execute("DELETE FROM material_clips")


def _mouse(t, x, y, button):
    """造一个局部坐标在 (x,y) 的鼠标事件（供直接调用 eventFilter）。"""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    lp = QPointF(x, y)
    buttons = button if t == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton
    return QMouseEvent(t, lp, lp, button, buttons, Qt.KeyboardModifier.NoModifier)


def test_output_right_release_does_not_enable_multi(qapp, tmp_path, monkeypatch):
    """回归「右键直接开启多选」 bug：上一段框选残留的大 geometry 不得被右键释放误用。

    旧代码 Release 分支没判左键/手势是否进行中，QRubberBand 一旦建就常驻、
    geometry 是脏的，右键释放会拿旧大矩形落到 _set_multi(True)。"""
    from PySide6.QtCore import QEvent, QRect, QPoint
    from PySide6.QtWidgets import QRubberBand
    from store import output_store
    from gui.pages_output_lib import OutputLibPage
    rows = []
    for nm in ("a.mp4", "b.mp4"):
        f = tmp_path / nm
        f.write_bytes(b"x")
        rows.append({"path": str(f), "name": nm, "product": "", "mark": "",
                     "bound": False, "size": 1, "created_at": "2026-09-02 10:00", "_ts": 0})
    monkeypatch.setattr(output_store, "list_outputs",
                        lambda product="", mark="", **kw: list(rows))
    page = OutputLibPage()
    try:
        # 直接造「上一段真实框选遗留」的状态：rubber 已存、geometry 为大矩形，无进行中手势
        if page._rubber is None:
            page._rubber = QRubberBand(QRubberBand.Shape.Rectangle, page._holder)
        page._rubber.setGeometry(QRect(0, 0, 60, 60))
        page._rub_anchor = None
        page._set_multi(False)
        # 右键释放：不得误开多选
        page.eventFilter(page._holder,
                         _mouse(QEvent.Type.MouseButtonRelease, 30, 30,
                                Qt.MouseButton.RightButton))
        assert page._multi is False, "右键释放误开启了多选"
        # 对照：左键释放 + 有进行中的手势（anchor 非空）仍正常框选开多选
        page._rub_anchor = QPoint(0, 0)
        page.eventFilter(page._holder,
                         _mouse(QEvent.Type.MouseButtonRelease, 60, 60,
                                Qt.MouseButton.LeftButton))
        assert page._multi is True, "正常的左键框选应能开启多选"
    finally:
        page.close()


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
        assert page._cards[0]._lbl_name._text == "001_关节不舒服"
        # 切物理名 → 带后缀；再切回
        page._toggle_physical()
        assert page._show_physical and page._cards[0]._lbl_name._text == "001_关节不舒服.mp4"
        page._toggle_physical()
        assert not page._show_physical
        # 切到回收站视图：渲染回收站行、卡片标「已在回收站」
        page._set_view("trash")
        assert page._is_trash() and page._cards[0].lbl_tags.text() == "🗑 已在回收站"
        # 回收站态空白菜单：未开多选只入口+排序；开多选后给还原/彻底删除/清空
        assert _menu_texts(page._empty_menu()) == ["开启多选", "↕ 排序"]
        page._set_multi(True)
        ttexts = _menu_texts(page._empty_menu())
        assert "♻ 还原选中" in ttexts and "🗑 彻底删除选中" in ttexts
        assert "🧹 清空回收站" in ttexts and "🗑 移入回收站" not in ttexts
    finally:
        page.close()
