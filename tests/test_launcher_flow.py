"""
tests/test_launcher_flow.py —— 唤出面板的「一排多个」入口卡：内容与键盘导航

这块改动的风险不在样式而在两件事，所以两条都钉住：
1. 卡片必须把「图标+名称+自己的全局快捷键」都画上，没配键的人不该看到一颗空胶囊
   （看着像控件坏了）；
2. 列数是 FlowLayout 排出来的、不固定，所以 ↑↓ 跨行只能按实际坐标算行。
   以前用「下标 ± 列数」，窗口一宽一行多塞一个，↑↓ 就跳到错的格子上。
   rows_of/locate 是纯函数，为的就是不真排版也能把这条算清楚（离屏测试里
   控件坐标全是 0，硬要排版反而测不到）。
3. 键盘路由不能只看“面板自己的 keyPressEvent”：卡片是 NoFocus，焦点恒在
   搜索框上，←→ 会被 QLineEdit 当光标键吃掉（用户报的“上下生效、左右不生效”）。
   这类“谁接走了键”只能靠 app.sendEvent + isAccepted 测，旧写法直接调
   dialog.keyPressEvent 是绕过了编辑框，那个洞根本测不到；
4. 暗色板要防 theme.tokenize：它会把种子 hex（#FFFFFF、#1F2329 那些）映回
   浅色令牌，踩中一个就在运行时静悄悄变回白底，肉眼看不出来。
"""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 早于任何 PySide6 导入

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QLabel

from gui import dialogs_launcher as dlg_mod
from gui import tools_registry as reg
from gui.dialogs_launcher import (LauncherDialog, key_display, locate, rows_of)
from store import app_state


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """每个用例一份临时偏好：app_state 每次读盘，所以换路径就是真隔离，
    用例必须自己把要用的键位配上，搭不到上一个用例的便车"""
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui_state.json")
    from gui import launcher_preview as _lp
    monkeypatch.setattr(_lp, "_SYNC_PREVIEW", True)   # 预览走同步：离屏等不到线程


# ---------------- 分行算法（纯函数，不靠排版） ----------------

@pytest.mark.parametrize("positions,expect", [
    ([(0, 0), (120, 0), (240, 0)], [[0, 1, 2]]),                 # 一行三个
    ([(0, 0), (120, 0), (0, 40)], [[0, 1], [2]]),                # 换行
    ([(240, 0), (120, 0), (0, 0)], [[2, 1, 0]]),                 # 行内按 x 升序
    ([(0, 0), (0, 60), (0, 120)], [[0], [1], [2]]),              # 退回一排一个
    ([], []),
    (None, []),
])
def test_rows_of_groups_by_y(positions, expect):
    assert rows_of(positions) == expect


def test_rows_of_tolerates_rounding_jitter():
    """同一行 y 差几像素（四舍五入/缩放）仍算一行，不能被拆成两排"""
    assert rows_of([(0, 0), (100, 3), (200, 1)]) == [[0, 1, 2]]


def test_unlaid_out_widgets_fall_into_one_row():
    """还没排版时坐标全是 0：宁可退化成一排，也不能算出越界下标"""
    assert rows_of([(0, 0)] * 4) == [[0, 1, 2, 3]]
    assert locate(rows_of([(0, 0)] * 4), 3) == (0, 3)


@pytest.mark.parametrize("index,expect", [(0, (0, 0)), (2, (1, 0)), (3, (1, 1))])
def test_locate_returns_row_and_col(index, expect):
    rows = [[0, 1], [2, 3]]
    assert locate(rows, index) == expect


def test_locate_unknown_index_is_harmless():
    assert locate([[0]], 9) == (0, 0)


def test_key_display_renames_meta_to_win():
    """Qt 把 Win 键叫 Meta，展示给用户要换回他嘴上说的那个名字"""
    assert key_display("Meta+Shift+D") == "Win+Shift+D"
    assert key_display("Ctrl+Alt+Space") == "Ctrl+Alt+Space"
    assert key_display("") == ""


# ---------------- 卡片内容：图标 + 名称 + 快捷键胶囊 ----------------

TOOLS = [("爆款拆解", "🔥", "拆分镜/口播"), ("语音识别", "🎙", "Whisper 转稿")]


@pytest.fixture
def panel(qapp, monkeypatch):
    d = LauncherDialog(lambda name: None, None)
    monkeypatch.setattr(d, "_load_tools", lambda: list(TOOLS))
    yield d
    d.close()


def _texts(widget):
    return [lb.text() for lb in widget.findChildren(QLabel)]


def test_chip_shows_icon_name_and_hotkey(panel):
    app_state.set_value("tool_shortcuts", {"爆款拆解": "Ctrl+Alt+1"})
    panel._refill("")
    assert len(panel._chips) == 2
    top, chip = panel._chips[0], panel._chips[1]
    assert _texts(top)[0] == "🔥 爆款拆解"          # 图标和名称同一行
    assert "Ctrl+Alt+1" in _texts(top)              # 自己的全局键做成胶囊
    assert "Meta+" not in "".join(_texts(top))      # 展示口径统一成 Win


def test_chip_without_hotkey_keeps_the_row_occupied(panel):
    """没配键的卡片不能把胶囊那一行抽掉：FlowLayout 顶对齐，一排里混两种
    高度就是锯齿（探针实测：带键 46 高，不带键 29 高）"""
    app_state.set_value("tool_shortcuts", {"爆款拆解": "Ctrl+Alt+1"})
    panel._refill("")
    keyed, free = panel._chips[0], panel._chips[1]
    assert _texts(free)[0] == "🎙 语音识别"        # 不画蓝底，也不印字
    assert free.kb.text().strip() == ""
    assert keyed.kb.text() == "Ctrl+Alt+1"
    assert keyed.kb.sizeHint().height() == free.kb.sizeHint().height()
    assert free.kb.sizeHint().height() > 0          # 那一行确实占着


def test_refill_rebuilds_from_scratch(panel, monkeypatch):
    """过滤一次再清空：卡片不能残留，下标必须与 _chips 一一对应"""
    panel.edit.setText("爆拆")
    assert [c.name for c in panel._chips] == ["爆款拆解"]
    panel.edit.setText("")
    assert [c.name for c in panel._chips] == [n for n, _i, _d in TOOLS]


def test_enter_opens_current_chip(panel):
    """建好还没呼出的面板是空的（summon() 才排卡片），所以先手动排一次"""
    got = []
    panel._on_open = lambda name: got.append(name)
    panel._refill("")
    panel._set_cur(1)
    panel.keyPressEvent(_key("Return"))
    assert got == ["语音识别"]


def test_chip_click_opens_that_tool(panel):
    got = []
    panel._on_open = lambda name: got.append(name)
    panel._refill("")
    panel._on_chip(panel._chips[1])
    assert got == ["语音识别"]


def test_chips_never_take_keyboard_focus(panel):
    """焦点必须一直留在搜索框：给了卡片，打字和选卡就只能二选一"""
    from PySide6.QtCore import Qt
    panel._refill("")
    for c in panel._chips:
        assert c.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_host_reports_wrapped_height(panel):
    """不把这个高度报给 QScrollArea，最后一排会被裁掉半截"""
    assert panel.host.hasHeightForWidth() is True
    assert panel.host.heightForWidth(300) > 0


def test_grid_cards_do_not_overlap(qapp, panel):
    """固定尺寸卡片进 FlowLayout：旧代码按 sizeHint（<setFixedSize）排格子，
    卡片会彼此横向重叠——选中外框“重合”就是这个。修好后相邻卡不得相交。"""
    files = [{"path": "D:\\x\\a%d.pdf" % i, "name": "a%d.pdf" % i,
              "dir": "D:\\x", "size": 10, "mtime": 1.7e9, "snippet": ""}
             for i in range(9)]
    panel._show_view("medium")
    panel._seq = 1
    panel._apply_results(1, files, [], 100)
    panel.show()
    panel.resize(1000, 600)
    qapp.processEvents()
    rects = [(c.x(), c.y(), c.width(), c.height()) for c in panel._frows]
    assert len(rects) >= 2
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            ax, ay, aw, ah = rects[i]
            bx, by, bw, bh = rects[j]
            ox = min(ax + aw, bx + bw) - max(ax, bx)
            oy = min(ay + ah, by + bh) - max(ay, by)
            assert not (ox > 0 and oy > 0), \
                "卡片 %d%s 与 %d%s 重叠 %dx%d" % (i, rects[i], j, rects[j], ox, oy)


# ---------------- 方向键：行是排出来的，不是算出来的 ----------------

class _FakeChip:
    """只给 _step 需要的三件事：坐标、名字、选中态（真控件在离屏下没坐标）

    坐标一律走 mapTo：_step 吃的是“相对内容容器的绝对坐标”，chip 的 x/y
    只相对 _FlowHost，两段拼一起时必须用同一个坐标系。"""

    kind = "tool"

    def __init__(self, name, x, y):
        self.name, self._x, self._y = name, x, y
        self.current = False

    def x(self):
        return self._x

    def y(self):
        return self._y

    def mapTo(self, parent, dx=0, dy=0):
        return QPoint(self._x, self._y)

    def set_current(self, on):
        self.current = bool(on)


class _FakeRow:
    """假文件/文档行：坐标已经换算到内容容器（行在段标题下面，y 比卡片大）"""

    def __init__(self, path, x, y, kind="file"):
        self.path, self.name, self.kind = path, os.path.basename(path), kind
        self._x, self._y = x, y
        self.current = False

    def mapTo(self, parent, dx=0, dy=0):
        return QPoint(self._x, self._y)

    def set_current(self, on):
        self.current = bool(on)


class _FakeArea:
    def ensureWidgetVisible(self, widget, *a):
        pass


@pytest.fixture
def navigating(panel):
    """两行：0 1 2 / 3 4 5，每行内 x 依次 0/120/240"""
    chips = [_FakeChip("t%d" % i, (i % 3) * 120, (i // 3) * 40) for i in range(6)]
    panel._chips = chips
    panel._area = _FakeArea()
    panel._cur = 0
    return panel


def _cur(d):
    return [i for i, c in enumerate(d._chips) if c.current]


def test_left_right_walks_flat_across_rows(navigating):
    d = navigating
    d._step(dx=1)
    assert _cur(d) == [1]
    d._set_cur(2)
    d._step(dx=1)                          # 行尾向右：接着走下一行第一个
    assert _cur(d) == [3]
    d._set_cur(0)
    d._step(dx=-1)                         # 首位向左：回到末尾，不报错
    assert _cur(d) == [5]


def test_up_down_jumps_to_nearest_column(navigating):
    """第 2 行第 3 个（下标 5，x=240）按 ↑ 应落在下标 2，不是下标 3"""
    d = navigating
    d._set_cur(5)
    d._step(dy=-1)
    assert _cur(d) == [2]
    d._step(dy=1)
    assert _cur(d) == [5]


def test_up_down_wraps_and_keeps_column(navigating):
    d = navigating
    d._set_cur(1)                          # 第一行中间
    d._step(dy=1)
    assert _cur(d) == [4]                  # 第二行中间
    d._step(dy=1)                          # 最后一行再向下：绕回第一行
    assert _cur(d) == [1]


def test_step_on_empty_panel_is_silent(navigating):
    d = navigating
    d._chips = []
    d._cur = -1
    d._step(dy=1)                          # 不该 IndexError
    d._step(dx=1)
    assert d._cur == -1


# ---------------- 方向键：焦点坐在搜索框里，四个键都得递到面板 ----------------

def _press(qapp, widget, key):
    """按真实投递路径发一个键，返回事件本身（要看 isAccepted）

    不能用 QTest.keyClick：它不走“未 accept 则向父链传播”那一段，
    四个方向键看起来全部失效，与用户实际看到的现象对不上。
    真实现场是 app.sendEvent(编辑框, QKeyEvent(...))：↑↓ 漏到面板、←→ 被
    QLineEdit 吃掉——“上下生效、左右不生效”就是这么来的。"""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    ev = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    qapp.sendEvent(widget, ev)
    return ev


def test_four_arrow_keys_all_move_selection(qapp, panel):
    """回归用例：←→ 被 QLineEdit 当自己的光标键 accept 掉，面板永远收不到

    卡片是 NoFocus，键盘焦点恒在搜索框上；四个键必须表现一致，
    不能只修好↑↓。"""
    from PySide6.QtCore import Qt
    panel._refill("")                       # 真卡片，两颗
    steps = []
    panel._step = lambda dx=0, dy=0: steps.append((dx, dy))
    for key in (Qt.Key.Key_Left, Qt.Key.Key_Right,
                Qt.Key.Key_Up, Qt.Key.Key_Down):
        ev = _press(qapp, panel.edit, key)
        assert ev.isAccepted(), "%s 被编辑框吃掉了" % key
    assert steps == [(-1, 0), (1, 0), (0, -1), (0, 1)]


def test_arrow_keys_no_longer_move_text_cursor(qapp, panel):
    """把代价钉进测试：搜索框里用 ←→ 挪光标这个功能没了（改用 Home/End/退格）

    不断言这一条的话，下个人接手时会把它当成 bug 改回去。"""
    panel._refill("")
    panel._step()                           # 真卡片在离屏下没坐标，拿真 _step 跑一遍不报错
    panel.edit.setText("爆拆")
    panel.edit.setCursorPosition(2)
    from PySide6.QtCore import Qt
    for key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
        _press(qapp, panel.edit, key)
    assert panel.edit.cursorPosition() == 2
    assert panel.edit.text() == "爆拆"       # 也不能顺手把字删了


def test_step_table_covers_exactly_four_arrows():
    """接管的键只能有这四个：多一个就会把 Home/End 这类常用编辑键误伤"""
    from PySide6.QtCore import Qt
    from gui.dialogs_launcher import _STEP_KEYS
    assert {(k,) for k, _dx, _dy in _STEP_KEYS} == {(k,) for k in (
        Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down)}
    assert [(dx, dy) for _k, dx, dy in _STEP_KEYS] == [(-1, 0), (1, 0), (0, -1), (0, 1)]


# ---------------- 深灰半透明外壳 ----------------

def _shell(widget):
    from gui.window_frame import _RoundedBg
    shells = [w for w in widget.findChildren(_RoundedBg)]
    assert len(shells) == 1, "自绘外壳应当只有一个，多了就是装了两遍"
    return shells[0]


def test_default_shell_keeps_the_light_card(qapp):
    """window_frame 新开的 card_bg / border 是加性的：不传就得与从前逐像素相同

    其它十几个窗口一行代码都没改，默认值一动就连坐。"""
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QDialog
    from gui.window_frame import _CARD_BG, _CARD_BORDER, apply_rounded
    dlg = QDialog()
    apply_rounded(dlg, show_min=False, show_max=False)
    shell = _shell(dlg)
    assert shell._bg == QColor(_CARD_BG)
    assert shell._border == QColor(_CARD_BORDER)
    assert shell._bg.alpha() == 255            # 普通窗口仍是不透明的
    dlg.close()


def test_launcher_shell_is_dark_and_translucent(panel):
    from PySide6.QtGui import QColor
    from gui.dialogs_launcher import _PANEL_BG, _PANEL_BORDER
    shell = _shell(panel)
    assert shell._bg == QColor(_PANEL_BG)
    assert shell._bg.alpha() < 255, "不透明就是一块死板，用户点名的问题"
    assert shell._bg.red() < 60 and shell._bg.green() < 60, "深灰，不是死黑"
    assert shell._border.alpha() < 255, "深底上用半透明白勾边，白描边会扎眼"


def test_dark_palette_survives_tokenize():
    """theme.tokenize 会把种子 hex 映成浅色令牌（#FFFFFF→card、#1F2329→text）

    暗色板只要踩到一个种子 hex，运行时就被换回浅色，深底配浅字当场失明。
    这条不测，下次有人“顺手美化”一下就会碎，而且碎得很隐蔽。"""
    from gui.dialogs_launcher import _CHIP_QSS, _TEXT, _WEAK
    from gui.theme import tokenize
    css = tokenize(_CHIP_QSS)
    for light in ("#FFFFFF", "#F5F8FF", "#E5E7EB", "#FAFBFD", "#C6D4F0"):
        assert light not in css.upper()
    assert tokenize("color:%s;" % _TEXT).upper().startswith("COLOR:#E8EAED")
    assert tokenize("color:%s;" % _WEAK).upper().startswith("COLOR:#9AA4B0")


def test_search_box_palette_is_dark_aware(panel):
    """光标与占位符是按 palette 取色的：只改样式表会留白底默认的浅灰光标"""
    from PySide6.QtGui import QPalette
    pal = panel.edit.palette()
    assert pal.color(QPalette.ColorRole.Text).lightness() > 150
    assert pal.color(QPalette.ColorRole.PlaceholderText).lightness() > 100


# ---------------- 双栏可拉伸：结果区吃满左列，超出交给滚动 ----------------

MANY = [("工具%d" % i, "🔧", "简介%d" % i) for i in range(8)]


@pytest.fixture
def shown(qapp, monkeypatch):
    """走真入口 summon()：几何刷新发生在它 show() 之后那一步，
    自己手拼 show()+_refill() 会漏掉 layout().activate()，量不到列表可视高
    （实际踩过）。"""
    d = LauncherDialog(lambda name: None, None)
    monkeypatch.setattr(d, "_load_tools", lambda: list(MANY))
    d.summon()
    qapp.processEvents()
    yield d
    d.close()


def test_panel_is_resizable_dual_pane(shown):
    """可拉伸 + 左右分栏是 P0 的地基：有下限尺寸、右侧预览窗存在"""
    from gui.dialogs_launcher import _MIN_W, _MIN_H
    d = shown
    assert hasattr(d, "_split") and hasattr(d, "_preview")
    assert d.minimumWidth() == _MIN_W and d.minimumHeight() == _MIN_H


def test_fit_height_no_longer_shrinks_the_window(qapp, shown):
    """_fit_height 只刷几何、不改整窗高（旧实现把窗收缩，正是“结果框太短”的来源）"""
    d = shown
    d.resize(1000, 600)
    qapp.processEvents()
    before = d.height()
    d._content_height = lambda: 40               # 只有一颗卡的需求高
    d._fit_height()
    assert d.height() == before, "不该再把整窗收高"


def test_empty_result_does_not_collapse_the_list(shown):
    """一颗都没匹配上时列表可视高仍留着，不能收成一条缝（看着像窗口挂了）"""
    from gui.dialogs_launcher import _MIN_LIST_H
    d = shown
    d._refill("zzz不存在")
    assert d._chips == []
    assert d._area.height() >= _MIN_LIST_H


# ---------------- 面板里的快捷键要跟设置页一致 ----------------

def test_capsule_follows_latest_binding(panel):
    reg.set_tool_shortcut("语音识别", "Ctrl+Alt+2")
    panel._refill("")
    assert panel._chips[1].kb.text() == "Ctrl+Alt+2"
    reg.set_tool_shortcut("语音识别", "")
    panel._refill("")
    assert panel._chips[1].kb.text().strip() == ""


# ---------------- 小工具 ----------------

class _key:
    """冒充 QKeyEvent：只让 keyPressEvent 问那两句 key() / modifiers()"""

    _MAP = {"Return": 0x01000004, "Escape": 0x01000000, "Up": 0x01000013,
            "Down": 0x01000014, "Left": 0x01000010, "Right": 0x01000011,
            "Tab": 0x01000001}

    def __init__(self, name, ctrl=False):
        from PySide6.QtCore import Qt
        self._k = self._MAP[name]
        self._m = (Qt.KeyboardModifier.ControlModifier if ctrl
                   else Qt.KeyboardModifier.NoModifier)

    def key(self):
        return self._k

    def modifiers(self):
        return self._m


# ---------------- 三段结果：工具 → 本地文件 → 文档内容 ----------------

FILES = [{"path": "D:\\素材\\关节不舒服.docx", "name": "关节不舒服.docx",
          "dir": "D:\\素材", "size": 12345, "mtime": 1791386970},
         {"path": "D:\\素材\\骨胶蓜.txt", "name": "骨胶蓜.txt",
          "dir": "D:\\素材", "size": 20, "mtime": 1791386970}]
DOCS = [{"path": "D:\\素材\\稿子.md", "name": "稿子.md", "dir": "D:\\素材",
         "size": 99, "mtime": 1791386970, "snippet": "……关节不适人群的日常痛点"}]


def _ask(d, text):
    """把“打字 → 停 120ms → 查”压成一步：离屏测试里等不到 QTimer 到点，
    手动 stop + 直调，剩下的链路（节流后的那一段）与真打字完全一致。"""
    d.edit.setText(text)
    d._debounce.stop()
    d._search_now()


@pytest.fixture
def indexed(monkeypatch):
    """把索引层整个换成假的：这一组要验的是面板接线，不是 fileindex。
    真 fileindex 会去读用户机器上那份 95 万行的库（实测 700 多 MB），
    测试里绝不能碰，所以连 search_* 都不让它真进 SQL。"""
    from core import fileindex
    monkeypatch.setattr(fileindex, "enabled", lambda: True)
    monkeypatch.setattr(fileindex, "doc_enabled", lambda: True)
    monkeypatch.setattr(fileindex, "status",
                        lambda: {"files": 1234, "docs": 5})
    monkeypatch.setattr(fileindex, "search_files",
                        lambda q, limit=50: list(FILES)[:limit])
    monkeypatch.setattr(fileindex, "search_docs",
                        lambda q, limit=50: list(DOCS)[:limit])
    # 异步本身不是这里要验的东西（要验的是 seq 丢弃），但离屏等不到线程回来
    monkeypatch.setattr(dlg_mod, "_SYNC_SEARCH", True)
    return fileindex


def test_three_sections_keep_the_navigation_order(panel, indexed):
    """_rows() 就是键盘上下走的顺序：卡片在上、文件在中、正文在下"""
    panel._refill("")
    assert len(panel._chips) == 2            # 两颗工具卡
    _ask(panel, "爆")                        # “爆”同时命中工具名与文件段
    kinds = [getattr(w, "kind", "?") for w in panel._rows()]
    assert kinds[:1] == ["tool"]
    assert kinds[-3:] == ["file", "file", "doc"]
    assert not panel.lbl_files.isHidden() and not panel.lbl_docs.isHidden()


def test_empty_section_title_stays_hidden(panel, indexed):
    """那一段没结果就不要把标题孤零零挂在那儿（看着像坏了）"""
    from core import fileindex
    index = indexed
    monkeypatched = index.search_docs
    index.search_docs = lambda q, limit=50: []
    panel._refill("")
    _ask(panel, "关节")
    assert not panel.lbl_files.isHidden()
    assert panel.lbl_docs.isHidden()
    index.search_docs = monkeypatched


def test_step_moves_across_containers_by_absolute_y(panel, indexed):
    """↑↓ 吃的是“相对同一个容器”的坐标：chip 的 y 只相对 _FlowHost，
    行相对 _body——拿控件自己的坐标混着比，两段会被当成同一行算错"""
    panel._refill("")
    panel._chips = [_FakeChip("t0", 0, 0), _FakeChip("t1", 120, 0)]
    panel._frows = [_FakeRow(FILES[0]["path"], 0, 200),
                    _FakeRow(FILES[1]["path"], 0, 240)]
    panel._drows = [_FakeRow(DOCS[0]["path"], 0, 300, "doc")]
    panel._area = _FakeArea()
    panel._set_cur(0)
    panel._step(dy=1)
    assert panel._cur == 2                 # 卡片行 → 文件段第一行
    panel._step(dy=1)
    assert panel._cur == 3
    panel._step(dy=1)
    assert panel._cur == 4                 # 最后落到正文段
    panel._step(dy=-1)
    assert panel._cur == 3


def test_right_arrow_walks_from_tool_into_files(panel, indexed):
    """←→ 走的是拼平的三段：卡片走到头要接着进文件行，不能只在卡片里回绕"""
    panel._refill("")
    _ask(panel, "爆")
    tools = sum(1 for w in panel._rows() if getattr(w, "kind", "tool") == "tool")
    assert tools >= 1
    panel._set_cur(tools - 1)
    panel._step(dx=1)
    assert panel._cur == tools
    assert getattr(panel._rows()[panel._cur], "kind", "") == "file"


def test_enter_opens_the_current_file(panel, indexed, monkeypatch):
    got = []
    monkeypatch.setattr("utils.desktop_utils.open_path", lambda p: got.append(p))
    panel._refill("")
    _ask(panel, "关节")
    panel._set_cur(0)
    panel.keyPressEvent(_key("Return"))
    assert got == [FILES[0]["path"]]


def test_ctrl_enter_reveals_the_folder(panel, indexed, monkeypatch):
    got = []
    monkeypatch.setattr("utils.desktop_utils.reveal_in_folder",
                        lambda p: got.append(p))
    panel._refill("")
    _ask(panel, "关节")
    panel._set_cur(0)
    panel.keyPressEvent(_key("Return", ctrl=True))
    assert got == [FILES[0]["path"]]


def test_ctrl_enter_on_a_tool_is_just_enter(panel, indexed, monkeypatch):
    """工具没有“所在目录”这个动作，Ctrl+Enter 等同 Enter，不能默默吞掉"""
    opened, revealed = [], []
    monkeypatch.setattr("utils.desktop_utils.reveal_in_folder",
                        lambda p: revealed.append(p))
    panel._on_open = lambda name: opened.append(name)
    panel._refill("")
    panel._set_cur(0)
    panel.keyPressEvent(_key("Return", ctrl=True))
    assert opened and opened[0] in ("爆款拆解", "语音识别")
    assert revealed == []


def test_click_selects_but_double_click_opens(panel, indexed, monkeypatch):
    """P4 语义：单击只选中（不开），双击才打开。"""
    got = []
    monkeypatch.setattr("utils.desktop_utils.open_path", lambda p: got.append(p))
    panel._refill("")
    _ask(panel, "关节")
    row = panel._frows[1]
    panel._on_row(row)                        # 单击
    assert got == []                          # 没打开
    assert panel._rows().index(row) == panel._cur   # 但选中了指针
    panel._on_row_open(row)                   # 双击
    assert got == [FILES[1]["path"]]          # 才真打开


def test_double_click_mouse_event_opens(panel, indexed, monkeypatch):
    """鼠标双击事件经 mouseDoubleClickEvent → doubleClicked 信号 → 打开。"""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    got = []
    monkeypatch.setattr("utils.desktop_utils.open_path", lambda p: got.append(p))
    panel._refill("")
    _ask(panel, "关节")
    row = panel._frows[0]
    ev = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(5, 5), QPointF(5, 5),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    row.mouseDoubleClickEvent(ev)
    assert got == [FILES[0]["path"]]


def test_tab_key_toggles_focus(panel, monkeypatch):
    """Tab 不再当"打开"，而是走 _toggle_focus（在列表/预览之间倒焦点）。"""
    hit = []
    monkeypatch.setattr(panel, "_toggle_focus", lambda: hit.append(1))
    panel.keyPressEvent(_key("Tab"))
    assert hit == [1]


def test_wheel_scroll_moves_the_scrollbar(panel):
    """滚轮换算：下滚值增、上滚值减、无竖直分量不管。"""
    class Bar:
        def __init__(self):
            self._v, self._ss = 100, 40
        def singleStep(self):
            return self._ss
        def value(self):
            return self._v
        def setValue(self, v):
            self._v = v

    class Delta:
        def __init__(self, y):
            self._y = y
        def y(self):
            return self._y

    class Ev:
        def __init__(self, y):
            self._d = Delta(y)
        def angleDelta(self):
            return self._d

    class Area:
        def __init__(self, b):
            self._b = b
        def verticalScrollBar(self):
            return self._b

    bar = Bar()
    panel._area = Area(bar)
    assert panel._wheel_scroll(Ev(-120)) is True    # 下滚
    assert bar.value() > 100
    prev = bar.value()
    assert panel._wheel_scroll(Ev(120)) is True     # 上滚
    assert bar.value() < prev
    assert panel._wheel_scroll(Ev(0)) is False      # 没有竖直分量


def test_stale_reply_cannot_overwrite_the_latest_one(panel, indexed):
    """seq 对不上号就丢：不丢就是“慢查询盖掉快查询”，
    表现为用户打的字和屏幕上的结果不对应"""
    panel._refill("")
    panel._seq = 41
    panel._apply_results(40, FILES, DOCS, 9)      # 上一轮的迟到回包
    assert panel._frows == [] and panel._drows == []
    panel._apply_results(41, FILES, DOCS, 9)
    assert len(panel._frows) == len(FILES)


def test_typing_a_new_word_bumps_the_round(panel, indexed):
    panel._refill("")
    _ask(panel, "关节")
    first = panel._seq
    _ask(panel, "骨胶蓜")
    assert panel._seq > first


def test_summon_drops_the_previous_results(qapp, monkeypatch, indexed):
    """重开面板不能拖上一次搜的词与行（_seq 也要进位，否则旧回包能贴回来）"""
    d = LauncherDialog(lambda name: None, None)
    monkeypatch.setattr(d, "_load_tools", lambda: list(TOOLS))
    d._refill("")
    _ask(d, "关节")
    assert d._frows
    seq = d._seq
    d.summon()
    assert d._frows == [] and d._drows == []
    assert d._seq > seq
    d.close()


def test_empty_index_says_it_is_building(panel, monkeypatch):
    """首扫还没跑完（冷盘实测 45 秒）时不能一脸空白：那看着像功能坏了"""
    from core import fileindex
    monkeypatch.setattr(fileindex, "enabled", lambda: True)
    monkeypatch.setattr(fileindex, "doc_enabled", lambda: False)
    monkeypatch.setattr(fileindex, "status", lambda: {"files": 0})
    monkeypatch.setattr(fileindex, "search_files", lambda q, limit=50: [])
    monkeypatch.setattr(fileindex, "search_docs", lambda q, limit=50: [])
    monkeypatch.setattr(dlg_mod, "_SYNC_SEARCH", True)
    panel._refill("")
    _ask(panel, "关节")
    assert not panel.lbl_hint.isHidden()
    assert "索引建立中" in panel.lbl_hint.text()


def test_disabled_switches_do_not_query(panel, monkeypatch):
    """设置里关掉就不再查：两段都该安炒，也不能报“在建”"""
    from core import fileindex
    monkeypatch.setattr(fileindex, "enabled", lambda: False)
    monkeypatch.setattr(fileindex, "doc_enabled", lambda: False)
    monkeypatch.setattr(fileindex, "status", lambda: {"files": 10})
    monkeypatch.setattr(dlg_mod, "_SYNC_SEARCH", True)
    panel._refill("")
    _ask(panel, "关节")
    assert panel._frows == [] and panel._drows == []
    assert panel.lbl_hint.isHidden()


def test_ready_index_shows_status_on_empty_query(panel, indexed):
    """一打开面板（空查询）就要看见索引就绪，不能对着空板子猜有没有建好"""
    panel._refill("")
    assert not panel.lbl_hint.isHidden()
    assert "就绪" in panel.lbl_hint.text()


def test_building_index_shows_progress_on_empty_query(panel, monkeypatch):
    """首扫进行中打开面板：得显“正在建立”和已收录数，让用户看得见在动"""
    from core import fileindex
    from workers import file_watcher
    monkeypatch.setattr(fileindex, "enabled", lambda: True)
    monkeypatch.setattr(fileindex, "status", lambda: {"files": 5123, "docs": 0})
    monkeypatch.setattr(file_watcher, "running", lambda: True)
    monkeypatch.setattr(file_watcher, "stats", lambda: {"scans": 0})
    panel._refill("")
    assert not panel.lbl_hint.isHidden()
    assert "正在建立本地索引" in panel.lbl_hint.text()
    assert "5,123" in panel.lbl_hint.text()


def test_first_results_auto_select_row_zero_for_enter(panel, indexed):
    """打字命中文件、但没工具命中时 _cur 是 -1；结果出来必须亮第 0 行，
    否则用户看着结果按回车却没反应（“不知道在不在 work”的主因）"""
    panel._refill("")
    _ask(panel, "关节")                       # “关节”不命中任何工具名
    assert panel._chips == []
    assert panel._frows
    assert panel._cur == 0
    assert getattr(panel._rows()[panel._cur], "kind", "") == "file"


def test_async_threaded_search_lands_via_signal(qapp, panel, indexed, monkeypatch):
    """真机走的是后台线程那条回程：普通线程里 QTimer.singleShot 不触发，
    必须靠信号 queued 回主线程。关掉 _SYNC_SEARCH 逼它真起线程、真发信号，
    再 pump 事件循环等结果贴上来（"卡在搜索中"这个坑同步路径测不到）。"""
    monkeypatch.setattr(dlg_mod, "_SYNC_SEARCH", False)
    panel._refill("")
    panel.edit.setText("关节")                  # textChanged→_refill→起 debounce
    panel._debounce.stop()                       # 不等 120ms，直接发
    panel._search_now()
    assert "搜索中" in panel.lbl_hint.text()      # 结果回来前先有可见反馈
    deadline = time.time() + 3.0
    while time.time() < deadline and not panel._frows:
        qapp.processEvents()
        time.sleep(0.005)
    assert panel._frows, "后台线程查完了，结果却没贴上来（回程没走通）"
    assert "搜索中" not in panel.lbl_hint.text()


def test_view_toggle_switches_widget_type(panel, indexed):
    """列表→网格切换：同样的结果重建为 _IconCard，且只亮对应容器"""
    panel._refill("")
    _ask(panel, "关节")
    assert panel._frows and all(type(w).__name__ == "_FileRow" for w in panel._frows)
    panel._show_view("medium")
    assert all(type(w).__name__ == "_IconCard" for w in panel._frows)
    assert not panel._file_grid.isHidden() and panel._file_list.isHidden()
    panel._show_view("large")
    assert panel._view == "large"
    panel._show_view("list")
    assert all(type(w).__name__ == "_FileRow" for w in panel._frows)


def test_view_choice_persists(panel, indexed):
    from store import app_state
    _ask(panel, "关节")
    panel._show_view("medium", persist=True)
    assert app_state.get("launcher_view") == "medium"


def test_default_view_applies_on_new_panel(qapp, monkeypatch, indexed):
    """新建面板时起始视图取“默认视图”偏好"""
    from store import app_state
    app_state.set_value("launcher_view_default", "large")
    d = LauncherDialog(lambda name: None, None)
    monkeypatch.setattr(d, "_load_tools", lambda: [])
    try:
        assert d._view == "large"
    finally:
        d.close()


def test_a_broken_index_never_breaks_the_panel(panel, monkeypatch):
    """索引层抛异常（库坏了 / 盘掉了）不能把面板带崩：顶多那两段没结果"""
    from core import fileindex

    def boom(*a, **k):
        raise RuntimeError("库坏了")

    monkeypatch.setattr(fileindex, "enabled", boom)
    monkeypatch.setattr(dlg_mod, "_SYNC_SEARCH", True)
    panel._refill("")
    _ask(panel, "关节")
    assert panel._frows == []


# ---------------- 行控件本身：内容、高亮、换算 ----------------

def test_file_row_shows_name_dir_and_size():
    from gui.dialogs_launcher import _FileRow
    txt = " ".join(_texts(_FileRow(FILES[0], "关节")))
    assert "不舒服.docx" in txt              # 命中段被包进 <span>，所以验后半
    assert "D:\\素材" in txt                 # “在哪个文件夹”是这一段最有用的信息
    assert "KB" in txt


def test_doc_row_adds_the_snippet_line():
    """“在哪提到的”才是正文搜索的全部意义，没这行就退化成按文件名搜"""
    from gui.dialogs_launcher import _DocRow
    r = _DocRow(DOCS[0], "关节")
    assert r.kind == "doc"
    assert any("日常痛点" in t for t in _texts(r))


def test_rows_never_take_keyboard_focus():
    """与卡片同理：焦点给了行，打字和选行就只能二选一"""
    from PySide6.QtCore import Qt
    from gui.dialogs_launcher import _DocRow, _FileRow
    for r in (_FileRow(FILES[0]), _DocRow(DOCS[0])):
        assert r.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_highlight_escapes_html_in_names():
    """文件名里带 & < 是真会撞的：不转义就把整行 HTML 撑坏"""
    from gui.dialogs_launcher import highlight
    s = highlight("a<b>&c 关节.docx", "关节")
    assert "&lt;" in s and "&amp;" in s
    assert "#8AB4FF" in s and "关节" in s


def test_highlight_without_a_hit_is_plain_escaped_text():
    from gui.dialogs_launcher import highlight
    assert highlight("a&b.txt", "") == "a&amp;b.txt"
    assert highlight("a&b.txt", "zzz") == "a&amp;b.txt"


def test_human_size_and_time_read_the_way_people_say_it():
    from gui.dialogs_launcher import human_size, human_time
    assert human_size(20) == "20B"
    assert human_size(12345) == "12.1KB"
    assert human_size(None) == "" and human_size("x") == ""
    assert human_time(0) == ""
    assert human_time(time.mktime(time.localtime()))  # 今天：写时分


def test_icon_for_falls_back_to_a_plain_page():
    from gui.dialogs_launcher import icon_for
    assert icon_for("a.pdf") == "📕"
    assert icon_for("a.nope") == "📄"
    assert icon_for("") == "📄"
