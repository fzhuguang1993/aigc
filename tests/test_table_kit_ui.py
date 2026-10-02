"""
tests/test_table_kit_ui.py —— 本轮「字段管理组件 + 表格交互升级」的回归用例

固化五块已在离屏冒烟验证过的行为（计划 G 节）：
- FieldManager：勾选 ↔ 顺序双向同步、set_all / reset、hidden 计算、拖拽落点插入；
- FieldManagerDialog 薄封装：(logical,标题)+[(分类,[标题])] → FieldManager 契约映射，
  accept() 仍产出 .order / .hidden；
- TableColumnKit 复制：选中区拼制表符文本写剪贴板 + 叠层行走虚线启停，Ctrl+C 命中 / Esc 停 /
  其它键放行（不破坏各表原键盘行为）；
- SortableTableHeader：首点新列＝降序、再点同列翻转、no_arrow 列不强制、绘制不崩；
- enable_sort_arrows：换表头后列宽 / resize 模式迁移（Stretch 列不硬设宽）；
- mount_header_checkbox：以表头为父的三态全选框、程序设态不回环、点框回调全选/清空。

全部离屏（QT_QPA_PLATFORM=offscreen）跑，clipboard 用桩避免平台差异。
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def fake_clip(monkeypatch):
    """把 QApplication.clipboard 换成桩， deterministic 断言写入的制表符文本。"""
    from PySide6.QtWidgets import QApplication

    class _Clip:
        def __init__(self):
            self.text = ""

        def setText(self, s):
            self.text = s

    inst = _Clip()
    monkeypatch.setattr(QApplication, "clipboard", lambda self, *a, **k: inst)
    return inst


# ---------------------------------------------------------------- FieldManager
def _sample_fields():
    # (id, label, category)：id 用逻辑列号语义，组件不解释
    return [
        (1, "任务ID", "标识"), (2, "状态", "标识"),
        (3, "标题", "内容"), (4, "正文", "内容"),
        (5, "耗时", "运行"), (6, "创建时间", "运行"),
    ]


def test_field_manager_order_hidden_and_label(qapp):
    from gui.kit import FieldManager
    fm = FieldManager(_sample_fields(), order=[3, 1, 4], hidden={2})
    # order 只保留未隐藏的给定顺序 id
    assert fm.order == [3, 1, 4]
    # hidden = 全字段 - order
    assert fm.hidden == {2, 5, 6}
    assert fm.label_of(3) == "标题"
    assert fm.label_of(999) == "999"     # 未知 id 回落成字符串


def test_field_manager_toggle_two_way_sync(qapp):
    from gui.kit import FieldManager
    fm = FieldManager(_sample_fields(), order=[1], hidden=set())
    hits = []
    fm.changed.connect(lambda: hits.append(1))
    # 勾选→追加到顺序末尾
    fm.toggle(4, True)
    assert fm.order == [1, 4]
    # 取消勾选→从顺序移除
    fm.toggle(1, False)
    assert fm.order == [4]
    # 重复勾选不重复插入
    fm.toggle(4, True)
    assert fm.order == [4]
    assert hits, "勾选/取消应触发 changed 信号"


def test_field_manager_remove_checks_back_pool(qapp):
    from gui.kit import FieldManager
    fm = FieldManager(_sample_fields(), order=[1, 3], hidden=set())
    box = fm._boxes[3]
    assert box.isChecked()
    fm.remove(3)
    assert 3 not in fm.order
    assert not box.isChecked()          # 移除后回勾左侧池勾选框


def test_field_manager_set_all_and_reset(qapp):
    from gui.kit import FieldManager
    ids = [f[0] for f in _sample_fields()]
    fm = FieldManager(_sample_fields(), order=[1], hidden=set())
    fm.set_all(True)
    assert fm.order == ids and fm.hidden == set()
    fm.set_all(False)
    assert fm.order == [] and fm.hidden == set(ids)
    # reset 回到给定默认（含 hidden 排除）
    fm.reset([3, 1], {1})
    assert fm.order == [3]              # 1 在 default_hidden 里，被剔除
    assert fm._boxes[3].isChecked() and not fm._boxes[1].isChecked()


def test_field_manager_categories_autogroup(qapp):
    from PySide6.QtWidgets import QGroupBox
    from gui.kit import FieldManager
    fm = FieldManager(_sample_fields(), order=[], hidden=set())
    titles = {b.title() for b in fm.findChildren(QGroupBox)}
    assert {"标识", "内容", "运行"} <= titles


def _fake_drop(src, fid, index=0):
    from gui.kit import _FM_MIME

    class _Mime:
        def data(self, fmt):
            return (json.dumps({"src": src, "id": fid, "index": index})
                    .encode("utf-8") if fmt == _FM_MIME else b"")

    class _Ev:
        def mimeData(self):
            return _Mime()

        def acceptProposedAction(self):
            pass

    return _Ev()


def test_field_manager_handle_drop_pool_inserts_and_checks(qapp):
    from PySide6.QtCore import QPoint
    from gui.kit import FieldManager
    fm = FieldManager(_sample_fields(), order=[1, 3], hidden=set())
    assert not fm._boxes[5].isChecked()
    # 从池拖入字段 5（未布局时 insert_index_at 落在末尾）
    fm.handle_drop(QPoint(9999, 9999), _fake_drop("pool", 5))
    assert 5 in fm.order
    assert fm._boxes[5].isChecked()     # 拖入等价于勾选


def test_field_manager_handle_drop_order_reorder(qapp):
    from PySide6.QtCore import QPoint
    from gui.kit import FieldManager
    fm = FieldManager(_sample_fields(), order=[1, 3, 4], hidden=set())
    # 把 index 2（字段 4）拖到最前（未布局、落点 x/y=0 → 插入位 0）
    fm.handle_drop(QPoint(0, 0), _fake_drop("order", 4, index=2))
    assert fm.order[0] == 4
    assert sorted(fm.order) == [1, 3, 4]


def test_field_manager_insert_index_at_empty(qapp):
    from PySide6.QtCore import QPoint
    from gui.kit import FieldManager
    fm = FieldManager(_sample_fields(), order=[], hidden=set())
    assert fm._bar.insert_index_at(QPoint(10, 10)) == 0


# ------------------------------------------------ 表头右键：MenuCascade 多级菜单 + 就地展开
def _kit_table(qapp):
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
    t = QTableWidget(3, 2)
    t.setHorizontalHeaderLabels(["甲", "乙"])
    for r, row in enumerate([["10", "x"], ["9", "y"], ["100", "z"]]):
        for c, v in enumerate(row):
            t.setItem(r, c, QTableWidgetItem(v))
    return t


def _leaf_texts(menu):
    """普通叶子菜单项文字（排除分隔符 / 子菜单入口 / QWidgetAction 里的钮与标题）。"""
    from PySide6.QtWidgets import QWidgetAction
    return [a.text() for a in menu.actions()
            if not a.isSeparator() and a.menu() is None
            and not isinstance(a, QWidgetAction) and a.text()]


def _button_texts(menu):
    """menu 各 QWidgetAction 里的 QPushButton 文字（展开/收起彩色钮）。"""
    from PySide6.QtWidgets import QWidgetAction, QPushButton
    out = []
    for a in menu.actions():
        if isinstance(a, QWidgetAction):
            w = a.defaultWidget()
            b = w.findChild(QPushButton) if w is not None else None
            if b is not None:
                out.append(b.text())
    return out


def _header_texts(menu):
    """menu 里做分组标题的 QLabel 文字（不含按钮行）。"""
    from PySide6.QtWidgets import QWidgetAction, QPushButton, QLabel
    out = []
    for a in menu.actions():
        if isinstance(a, QWidgetAction):
            w = a.defaultWidget()
            if w is not None and w.findChild(QPushButton) is None:
                lbl = w.findChild(QLabel)
                if lbl is not None:
                    out.append(lbl.text())
    return out


def test_header_menu_three_submenus_with_default_expand(qapp):
    from gui.kit import TableColumnKit
    kit = TableColumnKit(_kit_table(qapp))
    menu, handlers = kit._menu.build(0)
    subs = [a.menu() for a in menu.actions() if a.menu()]
    assert [m.title() for m in subs] == ["居中调整", "顺序调整", "字段格式"]
    assert {m.title(): _leaf_texts(m) for m in subs}["居中调整"] == [
        "本列靠左", "本列居中", "本列靠右", "本列垂直居中", "取消自动换行"]
    assert _leaf_texts(next(m for m in subs if m.title() == "顺序调整")) == ["升序排列", "降序排列"]
    assert _leaf_texts(next(m for m in subs if m.title() == "字段格式")) == ["文本", "数字", "日期"]
    # 每个弹层尾部一个绿钮「默认展开」；全菜单无任何对钩/勾选态
    for m in subs:
        assert _button_texts(m) == ["默认展开"]
        assert not any(a.isCheckable() for a in m.actions())
    assert all(callable(fn) for fn in handlers.values())


def test_default_expand_group_renders_inline(qapp):
    """展开的组：强调色分组标题(QLabel) + 子项就地平铺进一级菜单 + 尾部红钮「取消展开」；
    其余组仍是二级弹层。"""
    from gui.kit import TableColumnKit
    kit = TableColumnKit(_kit_table(qapp))
    kit._menu.set_expanded(["align"])
    menu, handlers = kit._menu.build(0)
    assert "居中调整" in _header_texts(menu)          # 强调色分组标题
    assert "本列靠左" in _leaf_texts(menu)             # 子项就地平铺进一级菜单
    assert _button_texts(menu) == ["取消展开"]         # 展开组尾部红钮
    assert not any(a.isCheckable() for a in menu.actions())   # 无对钩
    # 顺序调整 / 字段格式 仍折成二级弹层
    assert [a.menu().title() for a in menu.actions() if a.menu()] == ["顺序调整", "字段格式"]


def test_multiple_expand_groups_inline(qapp):
    """多组同时展开：align + order 都就地平铺，format 仍是弹层；各自独立、互不顶掉。"""
    from gui.kit import TableColumnKit
    kit = TableColumnKit(_kit_table(qapp))
    kit._menu.set_expanded(["align", "order"])
    menu, _ = kit._menu.build(0)
    assert set(_header_texts(menu)) >= {"居中调整", "顺序调整"}
    assert "本列靠左" in _leaf_texts(menu) and "升序排列" in _leaf_texts(menu)
    assert _button_texts(menu) == ["取消展开", "取消展开"]      # 两个展开组各一个红钮
    assert [a.menu().title() for a in menu.actions() if a.menu()] == ["字段格式"]


def test_header_menu_registration_and_flat_group(qapp):
    """注册机制：新组可多级；只挂一个平铺项的组直接成一级菜单项（没有多级不绕一层）；同 key 覆盖。"""
    from gui.kit import TableColumnKit
    kit = TableColumnKit(_kit_table(qapp))
    hits = []
    kit.add_header_menu("业务操作", [("刷新本列", lambda col: hits.append(col))])
    menu, handlers = kit._menu.build(2)
    assert _leaf_texts(menu) == ["刷新本列"]     # 直接挂一级，不建子菜单
    act = next(a for a in menu.actions() if a.text() == "刷新本列")
    handlers[act](2)
    assert hits == [2]
    # 同 key 覆盖
    kit.add_header_menu("业务操作改名", [("只读", lambda col: None)], key="业务操作")
    menu2, _h2 = kit._menu.build(0)
    assert _leaf_texts(menu2) == ["只读"]
    # 可嵌三级：带嵌套列表的组仍走子菜单，子项里再开一层
    kit.add_header_menu("导出", [("按行", [("CSV", lambda c: None),
                                    ("Excel", lambda c: None)]),
                          ("按列", lambda c: None)])
    menu3, _h3 = kit._menu.build(0)
    sub = next(a.menu() for a in menu3.actions()
               if a.menu() and a.menu().title() == "导出")
    # 按行是二级弹层（带 .menu()），按列是叶子；[a.text()] 保留全部入口
    assert [a.text() for a in sub.actions()][:2] == ["按行", "按列"]
    assert sub.actions()[0].menu() is not None          # 按行 又下钻一层


def test_header_menu_default_expand_toggle(qapp, monkeypatch):
    from gui.kit import TableColumnKit
    t = _kit_table(qapp)
    t.setObjectName("UTMenuTable")
    saved = {}
    from store import app_state
    monkeypatch.setattr(app_state, "get", lambda k, d=None: {})
    monkeypatch.setattr(app_state, "set_value", lambda k, v: saved.__setitem__(k, v))
    kit = TableColumnKit(t)
    kit._menu.toggle("align")
    assert kit._menu.expanded() == {"align"}
    assert saved["colkit::UTMenuTable"]["expand"] == ["align"]
    # 再点同一个：收起该组
    kit._menu.toggle("align")
    assert kit._menu.expanded() == set()
    # 多组同时展开：互不顶掉
    kit._menu.toggle("order")
    kit._menu.toggle("format")
    assert kit._menu.expanded() == {"order", "format"}
    assert saved["colkit::UTMenuTable"]["expand"] == ["format", "order"]


def test_expand_button_click_toggles_and_reopens(qapp):
    """绿钮 clicked → 该组进展开集合 + 置重开标记（show 循环据此同地重开）；非等下次。"""
    from PySide6.QtWidgets import QWidgetAction, QPushButton
    from gui.kit import TableColumnKit
    kit = TableColumnKit(_kit_table(qapp))
    cascade = kit._menu
    menu, _handlers = cascade.build(0)
    sub = next(a.menu() for a in menu.actions()
               if a.menu() and a.menu().title() == "顺序调整")
    btn = next(a.defaultWidget().findChild(QPushButton) for a in sub.actions()
               if isinstance(a, QWidgetAction)
               and a.defaultWidget().findChild(QPushButton) is not None)
    assert btn.text() == "默认展开"
    btn.click()                                   # 触发 clicked → _request_toggle("order")
    assert cascade.expanded() == {"order"}
    assert cascade._reopen is True                # 待 show 循环在同一位置重开


def test_apply_sort_number_moves_whole_rows(qapp):
    from PySide6.QtCore import Qt
    from gui.kit import TableColumnKit
    t = _kit_table(qapp)
    kit = TableColumnKit(t)
    kit._set_col_format(0, "number")
    kit._apply_sort(0, Qt.SortOrder.AscendingOrder)
    # 数字口径：9 < 10 < 100（文本口径会是 10,100,9）；整行随行迁移
    assert [t.item(r, 0).text() for r in range(3)] == ["9", "10", "100"]
    assert [t.item(r, 1).text() for r in range(3)] == ["y", "x", "z"]
    kit._apply_sort(0, Qt.SortOrder.DescendingOrder)
    assert [t.item(r, 0).text() for r in range(3)] == ["100", "10", "9"]
    # 菜单排序不残留激活箭头（-1＝无排序列）
    assert t.horizontalHeader().sortIndicatorSection() == -1


def test_fmt_key_number_and_date(qapp):
    from gui.kit import _fmt_key
    assert _fmt_key("1,280", "number") == 1280.0
    assert _fmt_key("—", "number") == float("-inf")
    # 中文年月日与 ISO 同轴；个位月日补零不串位
    assert _fmt_key("2026年9月9日", "date") < _fmt_key("2026-09-19", "date")
    assert _fmt_key("2026-09-09", "date") == _fmt_key("2026年9月9日", "date")
    assert _fmt_key("2026-09-19 12:30", "date") > _fmt_key("2026-09-19 09:59", "date")
    assert _fmt_key("任意", "text") == "任意"


# ------------------------------------------------------- FieldManagerDialog 薄封装
def test_dialog_to_fields_mapping(qapp):
    from gui.tablekit import FieldManagerDialog
    columns = [(1, "标题"), (2, "状态"), (3, "耗时")]
    categories = [("内容", ["标题"]), ("运行", ["耗时"])]
    fields, cats = FieldManagerDialog._to_fields(columns, categories)
    # 分类命中的进入给定组，未覆盖的列归“其他”
    assert (1, "标题", "内容") in fields
    assert (3, "耗时", "运行") in fields
    assert (2, "状态", "其他") in fields
    names = [g for g, _ in cats]
    assert "其他" in names


def test_dialog_to_fields_no_categories_flat(qapp):
    from gui.tablekit import FieldManagerDialog
    columns = [(1, "标题"), (2, "状态")]
    fields, cats = FieldManagerDialog._to_fields(columns, None)
    assert cats is None
    assert fields == [(1, "标题", "字段"), (2, "状态", "字段")]


def test_dialog_embeds_manager_and_accept_contract(qapp):
    from gui.kit import FieldManager
    from gui.tablekit import FieldManagerDialog
    columns = [(1, "标题"), (2, "状态"), (3, "耗时")]
    dlg = FieldManagerDialog(None, columns, order=[1, 2, 3], hidden=set())
    assert isinstance(dlg.fm, FieldManager)
    # 薄封装后仍经 fm 产出结果：交互改顺序 → accept() 写回 .order/.hidden
    dlg.fm.toggle(2, False)
    dlg.accept()
    assert dlg.order == [1, 3]
    assert dlg.hidden == {2}


# ----------------------------------------------------------- TableColumnKit 复制
def _build_table(qapp):
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem
    t = QTableWidget(2, 2)
    for r, row in enumerate([["a", "b"], ["c", "d"]]):
        for c, v in enumerate(row):
            t.setItem(r, c, QTableWidgetItem(v))
    return t


def test_colkit_copy_writes_clipboard_and_starts_marquee(qapp, fake_clip):
    from PySide6.QtWidgets import QTableWidgetSelectionRange
    from gui.kit import TableColumnKit
    t = _build_table(qapp)
    kit = TableColumnKit(t)
    t.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 1), True)
    kit._copy_selection()
    assert fake_clip.text == "a\tb\nc\td"
    assert kit._marquee._ranges, "复制后叠层应记录选区以画行走虚线"
    kit._marquee.stop()
    assert kit._marquee._ranges == []


def test_colkit_eventfilter_copy_esc_passthrough(qapp, fake_clip):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QTableWidgetSelectionRange
    from gui.kit import TableColumnKit
    t = _build_table(qapp)
    kit = TableColumnKit(t)
    t.setRangeSelected(QTableWidgetSelectionRange(0, 0, 0, 1), True)
    copy_ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C,
                        Qt.KeyboardModifier.ControlModifier)
    assert kit.eventFilter(t, copy_ev) is True     # Ctrl+C 被接管
    assert kit._marquee._ranges
    esc_ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                       Qt.KeyboardModifier.NoModifier)
    assert kit.eventFilter(t, esc_ev) is False     # Esc 停叠层但放行（不改返回值语义）
    assert kit._marquee._ranges == []
    other = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_X,
                      Qt.KeyboardModifier.NoModifier)
    assert kit.eventFilter(t, other) is False      # 其它键一律放行


def test_colkit_copy_no_selection_falls_back_current_item(qapp, fake_clip):
    from PySide6.QtWidgets import QTableWidgetSelectionRange
    from gui.kit import TableColumnKit
    t = _build_table(qapp)
    kit = TableColumnKit(t)
    t.setCurrentCell(1, 1)                            # 无拖拽选区，仅当前格
    # 清掉可能残留的 selectedRanges 之外，currentItem 分支应复制当前格文本
    t.setRangeSelected(QTableWidgetSelectionRange(0, 0, 0, 0), False)
    kit._copy_selection()
    assert fake_clip.text == "d"


# --------------------------------------------------------- SortableTableHeader
def test_sortable_header_first_click_descending_then_toggle(qapp):
    """首点新列＝降序，再点同列翻转升/升；换到另一列又回到降序。直接调
    handle_section_click（不依赖基类在点击时自动拨指示列——那张表头把
    setSortIndicatorShown 关了，靠的就是这份自持逻辑）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTableWidget
    from gui.kit import SortableTableHeader
    t = QTableWidget(3, 3)
    t.setHorizontalHeaderLabels(["甲", "乙", "丙"])
    hdr = SortableTableHeader()
    hdr.set_no_arrow_cols([0])
    t.setHorizontalHeader(hdr)
    qapp.processEvents()
    assert hdr.handle_section_click(1) == Qt.SortOrder.DescendingOrder   # 首点降序
    assert hdr.sortIndicatorSection() == 1
    assert hdr.handle_section_click(1) == Qt.SortOrder.AscendingOrder    # 同列再点→升
    assert hdr.handle_section_click(1) == Qt.SortOrder.DescendingOrder   # 再点→降
    assert hdr.handle_section_click(2) == Qt.SortOrder.DescendingOrder   # 换新列又降
    assert hdr.sortIndicatorSection() == 2


def test_sortable_header_no_arrow_col_is_skipped(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTableWidget
    from gui.kit import SortableTableHeader
    t = QTableWidget(3, 3)
    t.setHorizontalHeaderLabels(["选", "乙", "丙"])
    hdr = SortableTableHeader()
    hdr.set_no_arrow_cols([0])
    t.setHorizontalHeader(hdr)
    qapp.processEvents()
    # 勾选列：handle_section_click 直接返回 None，不记录为上次点过的排序列
    assert hdr.handle_section_click(0) is None
    assert hdr._last_col is None
    # 越界 / 负数同样安全
    assert hdr.handle_section_click(99) is None
    assert hdr.handle_section_click(-1) is None


def test_sortable_header_grab_paints_without_crash(qapp):
    from PySide6.QtWidgets import QTableWidget
    from gui.kit import SortableTableHeader
    t = QTableWidget(2, 3)
    t.setHorizontalHeaderLabels(["很宽的标题列内容", "乙", "丙"])
    hdr = SortableTableHeader()
    hdr.set_no_arrow_cols([0])
    t.setHorizontalHeader(hdr)
    t.resize(300, 140)
    t.show()
    qapp.processEvents()
    pm = hdr.grab()                # 触发 paintSection（含窄列预留 ARROW_W 计算）
    assert not pm.isNull()


def test_sortable_header_text_handles_str_headerdata(qapp):
    # PySide6 headerData 直接返回 str，_text 须能取到（曾经的 isValid() 崩点）
    from PySide6.QtWidgets import QTableWidget
    from gui.kit import SortableTableHeader
    t = QTableWidget(1, 2)
    t.setHorizontalHeaderLabels(["名称", "值"])
    hdr = SortableTableHeader()
    t.setHorizontalHeader(hdr)
    qapp.processEvents()
    assert hdr._text(0) == "名称"
    assert hdr._text(5) in ("", "5")   # 越界不崩


# ------------------------------------------------------------ enable_sort_arrows
def test_enable_sort_arrows_swaps_header_and_migrates(qapp):
    from PySide6.QtWidgets import QTableWidget
    from PySide6.QtWidgets import QHeaderView
    from gui.kit import SortableTableHeader, TableColumnKit
    t = QTableWidget(2, 3)
    t.setHorizontalHeaderLabels(["甲", "乙", "丙"])
    hh = t.horizontalHeader()
    hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
    hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    t.setColumnWidth(0, 120)
    t.setColumnWidth(1, 80)
    kit = TableColumnKit(t)
    new = kit.enable_sort_arrows(no_arrow_cols=(0,))
    assert isinstance(new, SortableTableHeader)
    assert t.horizontalHeader() is new
    assert 0 in new._no_arrow
    # 非 Stretch 列宽迁移
    assert t.columnWidth(0) == 120
    assert t.columnWidth(1) == 80
    # Stretch 列保持 Stretch 模式（未被硬设宽）
    assert new.sectionResizeMode(2) == QHeaderView.ResizeMode.Stretch


# ---------------------------------------------------------- mount_header_checkbox
def test_mount_header_checkbox_tri_state_and_toggle(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTableWidget, QHeaderView
    from gui.tablekit import mount_header_checkbox
    t = QTableWidget(3, 2)
    t.setHorizontalHeaderLabels(["选", "名"])
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    t.setColumnWidth(0, 40)
    t.resize(300, 160)
    t.show()
    qapp.processEvents()

    calls = []

    def on_toggle(state):
        calls.append(state)

    provider_state = {"v": Qt.CheckState.PartiallyChecked}

    def provider():
        return provider_state["v"]

    cb = mount_header_checkbox(t, 0, on_toggle, provider)
    assert cb.parentWidget() is t.horizontalHeader()

    cb.sync_state()                        # 程序设态：不回环触发 on_toggle
    assert cb.checkState() == Qt.CheckState.PartiallyChecked
    assert calls == []

    # 模拟用户点击：勾选框态改变应触发 on_toggle（guard 未持有）
    cb.setChecked(True)
    assert Qt.CheckState.Checked in calls
    # 宿主处理完回写真实态
    provider_state["v"] = Qt.CheckState.Checked
    cb.sync_state()
    assert cb.checkState() == Qt.CheckState.Checked


def test_mount_header_checkbox_repositions_on_resize(qapp):
    from PySide6.QtWidgets import QTableWidget, QHeaderView
    from gui.tablekit import mount_header_checkbox
    t = QTableWidget(2, 2)
    t.setHorizontalHeaderLabels(["选", "名"])
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    t.setColumnWidth(0, 40)
    t.resize(300, 160)
    t.show()
    qapp.processEvents()
    cb = mount_header_checkbox(t, 0, lambda s: None)
    before = cb.geometry().x()
    t.setColumnWidth(0, 120)               # 列变宽 → 框应随 sectionResized 重排
    qapp.processEvents()
    assert cb.geometry().x() != before
