"""
tests/test_ui_kit.py —— 设计令牌单一真源 + theme.tokenize 接线 + UI 画廊页可构建

覆盖三件事：
1) 令牌表自洽：_SEED_TO_TOKEN 引用的每个语义名都真实存在于 COLORS（防止改表时打错 key
   让 tokenize 运行时 KeyError 崩掉整站样式）；
2) tokenize 语义：令牌未改时对 QSS 是恒等（种子==当前值），改一个主色即把全站该 hex 换掉，
   且不会「二次替换」（一个令牌的新值恰好等于另一个种子时不被连环改写）；
3) 画廊页 gui/pages_ui_kit.py 能离屏构建、分节齐全，作为「统一 UI 池」的可视化入口。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_seed_tokens_all_resolvable():
    """theme 的每个种子都必须映射到 ui_kit.COLORS 里真实存在的语义名。"""
    from gui import theme, ui_kit
    for seed, key in theme._SEED_TO_TOKEN.items():
        assert key in ui_kit.COLORS, f"种子 {seed} 指向不存在的令牌 {key!r}"
        # 约定：未改令牌时种子 hex == 该令牌的当前值（tokenize 对 QSS 才是恒等）
        assert seed.upper() == ui_kit.COLORS[key].upper(), \
            f"种子 {seed} 与 COLORS[{key!r}]={ui_kit.COLORS[key]} 不一致，tokenize 会误改 QSS"


def test_tokenize_identity_on_qss():
    """没改令牌时，tokenize 不应改动 QSS 一个字符（纯恒等，零转录风险）。"""
    from gui import theme
    assert theme.tokenize(theme.QSS) == theme.QSS


def test_tokenize_propagates_token_change():
    """改一个主色，所有该 hex 都应跟着变成新值；且不被连环二次替换。"""
    from gui import theme, ui_kit
    orig = dict(ui_kit.COLORS)
    try:
        ui_kit.COLORS["primary"] = "#AA0000"
        out = theme.tokenize("a{color:#3370FF;background:#3370FF;}")
        assert out == "a{color:#AA0000;background:#AA0000;}"
        # 边界：primary 新值恰好等于另一个种子（#EAF1FF=primary_soft）时，
        # 单遍替换只按原文命中一次，不会把刚生成的 #EAF1FF 再当 primary_soft 改一遍
        ui_kit.COLORS["primary"] = ui_kit.COLORS["primary_soft"]  # 令 primary→#EAF1FF
        out2 = theme.tokenize("x{color:#3370FF;}   /* primary 变成 soft 的旧值 */")
        assert "#EAF1FF" in out2 and "#3370FF" not in out2
    finally:
        ui_kit.COLORS.clear()
        ui_kit.COLORS.update(orig)


def test_rgba_and_palettes():
    from gui import ui_kit
    assert ui_kit.rgba("#3370FF", 0.14) == "rgba(51,112,255,0.14)"
    # 非颜色（QColor 无法解析）原样返回，不炸
    assert ui_kit.rgba("not-a-real-color", 0.2) == "not-a-real-color"
    # 语义色板引用的都是 COLORS 里的真值
    for name in ("成功", "失败", "取消", "运行中"):
        assert name in ui_kit.STATUS_COLORS
    assert ui_kit.FIELD_COLORS["seq"] == ui_kit.COLORS["primary"]


def test_tooltip_takeover_single_rounded_bubble(qapp):
    """tooltip 全站接管：ToolTip 事件被 _TipRouter 拦下、文案从控件侧解析后转投单例
    TipBubble——原生 QTipLabel（矩形窗、直角套圆角的根源）不再露面；再弹第二份内容
    是同一颗气泡换文案，结构上不可能出现「第二个弹层盖掉第一个」。
    注：Qt 真实发出的 QTipEvent 在 PySide6 没有绑定类（到 Python 侧无 text()），
    所以用例发同类型普通 QEvent，验证的正是 router「不读事件、只解析控件」的链。"""
    from PySide6.QtCore import QPoint, Qt, QEvent
    from PySide6.QtWidgets import QLabel
    from gui.theme import apply_theme
    from gui.kit import TipBubble

    def tip_event():
        return QEvent(QEvent.Type.ToolTip)

    apply_theme(qapp)                       # 幂等：install_tip_bubble 挂在其内
    w = QLabel("x")
    w.setToolTip("第一段内容")
    qapp.sendEvent(w, tip_event())
    b = TipBubble.instance()
    assert b.isVisible() and b._lbl.text() == "第一段内容", "ToolTip 应转投圆角气泡"
    # 单例复用：换内容不新建窗口
    w.setToolTip("第二段内容")
    qapp.sendEvent(w, tip_event())
    assert TipBubble.instance() is b and b._lbl.text() == "第二段内容"
    # 原生通道没被触发：不该有任何可见的 QTipLabel 顶层窗
    labels = [t for t in qapp.topLevelWidgets()
              if t.metaObject().className() == "QTipLabel"]
    assert all(not t.isVisible() for t in labels), "原生 tooltip 仍被弹了出来"
    # 按下即隐（与原生 tooltip 行为一致）
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent
    qapp.sendEvent(w, QMouseEvent(QEvent.Type.MouseButtonPress,
                                  QPointF(1, 1), w.mapToGlobal(QPoint(1, 1)),
                                  Qt.MouseButton.LeftButton,
                                  Qt.MouseButton.LeftButton,
                                  Qt.KeyboardModifier.NoModifier))
    assert not b.isVisible(), "鼠标按下后气泡应收起"
    b.hide()


def test_gallery_page_builds(qapp):
    """UI 画廊页离屏构建：Tab 分类 + 自绘动效按钮 + 单框多级树/日期/滑块/表格/胶囊等新组件都在。"""
    from gui.theme import apply_theme
    from gui.pages_ui_kit import (UiKitPage, KitButton, TreeSelect,
                                  DateRangePicker, KitSlider, KitTable,
                                  ResizableTextEdit)
    from gui.header import Sparkline
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTabWidget, QTreeView, QLineEdit
    apply_theme(qapp)
    page = UiKitPage()
    page.resize(1000, 800)
    page.show()
    qapp.processEvents()
    tabs = page.findChild(QTabWidget)
    assert tabs is not None, "画廊页没有 Tab 容器"
    labels = "|".join(tabs.tabText(i) for i in range(tabs.count()))
    for kw in ("按钮", "输入框", "选择框", "Form", "数字", "进度", "表格", "弹层", "设计令牌"):
        assert kw in labels, f"画廊缺少 Tab：{kw}"
    kits = page.findChildren(KitButton)
    assert len(kits) >= 6, f"KitButton 数量异常：{len(kits)}"
    assert any(b._explain for b in kits), "应有默认点击弹窗说明的示例按钮"
    assert any(b._kind == "danger" for b in kits), "应有危险按钮（带确认）"
    # 多级下拉：单个下拉内的层级树（非多框联动），树里 广东省→深圳市/广州市
    ts = page.findChild(TreeSelect)
    assert ts is not None, "缺少单框多级树下拉"
    assert not hasattr(ts, "_combos"), "不应该是多个下拉框联动"
    pop = ts._build_pop()
    tree = pop.findChild(QTreeView)
    assert pop.findChild(QLineEdit) is not None, "多级下拉弹层顶部应有搜索框"
    model = tree.model().sourceModel()   # 树现在挂在 _TreeFilter 代理上，读源模型
    root = model.invisibleRootItem()
    tops = [root.child(i).text() for i in range(root.rowCount())]
    assert "广东省" in tops, tops
    gd = [root.child(i) for i in range(root.rowCount())
          if root.child(i).text() == "广东省"][0]
    cities = [gd.child(i).text() for i in range(gd.rowCount())]
    assert "深圳市" in cities and "广州市" in cities, cities
    pop.deleteLater()
    # 日期：样式①为区间选择器；内嵌日历卡片（旧样式③）已从画廊撤下
    assert page.findChild(DateRangePicker) is not None, "缺少日期区间选择器"
    assert page.findChild(ResizableTextEdit) is not None, "缺少可自由拉伸的多行文本"
    # 表格：列对齐/换行仅表头右键可设
    assert page.findChild(KitTable) is not None, "缺少交互表格"
    # KPI 卡迷你折线
    assert page.findChildren(Sparkline), "KPI 卡右侧应有迷你折线图"
    # 滑块：水平 + 垂直至少各一个
    sliders = page.findChildren(KitSlider)
    assert len(sliders) >= 2, f"应有水平+垂直两个滑块，实得 {len(sliders)}"
    assert any(s.orientation() == Qt.Orientation.Vertical for s in sliders), "缺垂直滑块"
    # 滑块气泡：拖动显、松手隐
    ks = sliders[0]
    ks._show_bubble(True)
    qapp.processEvents()
    assert ks._bubble.isVisibleTo(ks) and "%" in ks._bubble.text(), "拖动时未浮现百分比气泡"
    ks._show_bubble(False)
    assert not ks._bubble.isVisibleTo(ks), "松手后气泡应隐藏"
