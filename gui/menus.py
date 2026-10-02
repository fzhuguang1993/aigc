"""
gui/menus.py —— 全站统一的右键 / 下拉菜单模板（StyledMenu）

主题里给 QMenu 写了 border-radius，但 Windows 的菜单是独立顶层弹窗：圆角之外那一圈
仍被原生窗底 + 系统投影填成方角——这就是"做了圆角、最外延容器还是有个直角"的根因。
把修角动作收敛到这一个模板，全站所有右键菜单 / setMenu 下拉 / 子菜单都走 StyledMenu：

① 真圆角：置 FramelessWindowHint + NoDropShadowWindowHint + WA_TranslucentBackground，
   圆角外面真正变成透明，不再漏方底（属性须在窗口句柄建好前的 __init__ 里设）；
② 补投影：自绘一圈软阴影填进保留的 padding 带里，补回被关掉的系统投影，不显"平"；
③ 容器背景交给自绘，item 的悬停配色仍由 theme.py 的 app 级 QSS（QMenu::item）负责。

用法：把 gui 里所有 QMenu(...) 换成 StyledMenu(...) 即可（含子菜单），
构造参数与 QMenu 一致——可传 (parent) 或 (title, parent)。
"""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QMenu, QWidget, QHBoxLayout, QLabel,
                              QPushButton, QWidgetAction, QSizePolicy)

from gui.ui_kit import COLORS
from gui.ui_kit import RADIUS as TOK_RADIUS   # ui_kit 圆角令牌（本模块顶部 RADIUS=10 是菜单圆角，两者不同名）

RADIUS = 10                     # 卡片圆角半径（与 ui_kit.RADIUS["lg"] 对齐）
MARGIN = 12                     # 四周留白：既是 items 的 padding，也是自绘软阴影的活动带
_SHADOW_STEPS = 6               # 阴影分层数（越多越细腻）

# MenuCascade 用色：展开钮绿、收起钮红；分组标题与展开钮完全同款（success 实底 + 白字）
_EXPAND = {"bg": COLORS["success"], "hover": COLORS["line_ok"]}
_COLLAPSE = {"bg": COLORS["danger"], "hover": COLORS["danger_text"]}
_HEADER_BG = COLORS["success"]            # 分组标题底色：取 success 令牌（与默认展开钮同色）
_HEADER_COLOR = COLORS["on_primary"]      # 分组标题文字：主色底上白字


class StyledMenu(QMenu):
    """带真圆角 + 自绘软阴影的菜单；行为与 QMenu 完全一致，只是换了外壳。"""

    def __init__(self, *args):
        title, parent = "", None
        for a in args:
            if isinstance(a, QWidget):
                parent = a
            else:
                title = str(a)
        super().__init__(title, parent)
        # ① 去原生框 + 系统投影，逐像素透明：圆角外不再被方底填满
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # ② 容器背景透明、用 padding 腾出阴影带（items 随内容区一起内缩，不会盖住圆角）
        self.setStyleSheet(
            "QMenu { background: transparent; border: none;"
            f" padding: {MARGIN}px; }}")

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(self.rect()).adjusted(MARGIN, MARGIN, -MARGIN, -MARGIN)
        # 软阴影：从卡片边缘向外一圈圈变淡（画在 padding 带内，不会被窗口边界裁掉）
        # 阴影取正文色（text）的低位透明度，与全站墨色调校一致
        sc = QColor(COLORS["text"])
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(_SHADOW_STEPS, 0, -1):
            off = i * (MARGIN / _SHADOW_STEPS)
            alpha = 4 + int(16 * (1 - i / _SHADOW_STEPS))
            p.setBrush(QColor(sc.red(), sc.green(), sc.blue(), alpha))
            p.drawRoundedRect(card.adjusted(-off, -off, off, off),
                              RADIUS + off, RADIUS + off)
        # 卡片本体 + 1px 描边（白底上也勾得出轮廓）
        p.setBrush(QColor(COLORS["card"]))
        p.setPen(QPen(QColor(COLORS["border_popup"]), 1))
        p.drawRoundedRect(card, RADIUS, RADIUS)
        p.end()
        # 背景已透明，super 只会画各项（不会用方底覆盖上面的圆角卡片）
        super().paintEvent(e)


# =====================================================================
# MenuCascade —— 注册式「多级右键弹层」装配器（可复用弹层组件，与业务解耦）
#
# 为什么放这儿：多级右键菜单这套逻辑（注册分组 / 无多级降一级 / 分组就地展开 /
# 彩色分组标题 / 展开收起彩色按钮 / 切换后就地重开）历史上长在 TableColumnKit 里，
# 任何想加右键菜单的地方都得照抄。抽到弹层组件菜单里，表头 / 行右键 / 工具按钮下拉
# 都能复用：谁用谁 new 一个、注册自己的分组、给定 context（列号 / 行号 / 任意对象）。
#
# 分组规则：
# - 多个子项的组 → 二级弹层；只有一个平铺处理项的组 → 直接是一级菜单项（没多级不绕一层）；
#   子项里再挂列表 → 继续下钻（多级天然支持）。
# - 每个多子项组尾部带一个彩色文字按钮：收起态绿钮「默认展开」、展开态红钮「取消展开」。
#   点开后该组不再折成弹层，而是把「强调色分组标题 + 全部子项」就地平铺进一级菜单（跨平台稳：
#   macOS 下 QMenu 子菜单无法可靠程序化弹开）。可展开的组不唯一，多组各自独立、互不顶掉。
# =====================================================================
class MenuCascade:
    EXPAND_LABEL = "默认展开"      # 收起态：绿钮，点了就地展开该组
    COLLAPSE_LABEL = "取消展开"    # 已展开：红钮，点了收回该组

    def __init__(self, parent=None, on_changed=None):
        self._parent = parent
        self._groups = []            # [(key, title, items)]
        self._expanded = set()       # 当前展开的组键集合（可多组）
        self.on_changed = on_changed  # 展开集合变化回调（零参，供外部持久化）
        self._top = None             # 本轮 build 出的一级菜单（按钮点它才关得到整条链）
        self._reopen = False         # 切换展开置真：show 循环据此在同一位置重开

    # ---- 注册：同 key 覆盖 ----
    def add(self, title, items, key=None):
        k = key or title
        for i, (kk, _t, _it) in enumerate(self._groups):
            if kk == k:
                self._groups[i] = (k, title, list(items))
                return
        self._groups.append((k, title, list(items)))

    def group_keys(self):
        return [k for k, _t, _i in self._groups]

    def expanded(self):
        return set(self._expanded)

    def set_expanded(self, keys):
        valid = set(self.group_keys())
        self._expanded = {k for k in keys if k in valid}

    def toggle(self, key):
        # 各组独立开关：已在集合里就收起，否则展开——多组可同时展开，互不顶掉
        if key in self._expanded:
            self._expanded.discard(key)
        else:
            self._expanded.add(key)
        if self.on_changed is not None:
            self.on_changed()

    # ---- 构建：纯装配不 exec，返回 (menu, handlers)；handlers: 叶子 action→callable(context) ----
    def build(self, context):
        menu = StyledMenu(self._parent)
        self._top = menu
        handlers = {}
        for key, title, items in self._groups:
            if not items:
                continue
            if len(items) == 1 and callable(items[0][1]):
                # 没有多级：唯一子项直接挂成一级菜单项，不再多绕一层
                act = menu.addAction(items[0][0])
                handlers[act] = items[0][1]
                continue
            if key in self._expanded:
                # 已展开：强调色分组标题（不可点）+ 子项就地平铺进一级菜单 + 尾部红钮「取消展开」
                # QMenu 加自定义控件只能经 QWidgetAction+addAction（无 addWidget）
                menu.addAction(_group_header(menu, title))
                self._fill(menu, items, handlers)
                menu.addAction(self._expand_btn(self.COLLAPSE_LABEL, False, key))
                menu.addSeparator()
                continue
            sub = StyledMenu(title)
            self._fill(sub, items, handlers)
            sub.addSeparator()
            sub.addAction(self._expand_btn(self.EXPAND_LABEL, True, key))
            menu.addMenu(sub)
        return menu, handlers

    def _fill(self, menu, items, handlers):
        for label, payload in items:
            if callable(payload):
                handlers[menu.addAction(label)] = payload
            else:
                sub = StyledMenu(label)
                self._fill(sub, payload, handlers)
                menu.addMenu(sub)

    def _expand_btn(self, label, expandable, key):
        """生成一个带背景色的文字按钮 action：expandable=True 绿（可展开），False 红（可收起）；
        按钮横向铺满菜单内容宽度（与普通菜单项同宽）。"""
        palette = _EXPAND if expandable else _COLLAPSE
        bg, hover = palette["bg"], palette["hover"]
        btn = QPushButton(label)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setMinimumHeight(26)
        # Expanding：随行铺满，使按钮宽度与普通 item 同宽（不再只占文字宽）
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        btn.setStyleSheet(
            f"QPushButton {{ background:{bg}; color:#FFFFFF; border:none;"
            f" border-radius:{TOK_RADIUS['md']}px; padding:5px 14px; font-weight:600; }}"
            f" QPushButton:hover {{ background:{hover}; }}"
            f" QPushButton:pressed {{ background:{hover}; }}")
        btn.clicked.connect(lambda: self._request_toggle(key))
        wa = QWidgetAction(self._top)
        # 左右 0 内距：与分组标题同缘、铺满整个菜单宽（不留缝）
        wa.setDefaultWidget(_row(btn, margins=(0, 4, 0, 4), fill=True))
        wa.setData("default_expand")   # 标记：单测据此识别展开/收起开关
        return wa

    def _request_toggle(self, key):
        # 按钮点击：切展开态 → 置重开标记 → 关一级菜单（exec 随即返回，show 循环同地重开）
        self.toggle(key)
        self._reopen = True
        if self._top is not None:
            self._top.close()

    # ---- 弹出：context 透传给叶子 handler；切换展开后就地同位重开，其余项点选走 handlers ----
    def show(self, pos, context):
        chosen = None
        while True:
            self._reopen = False
            menu, handlers = self.build(context)
            chosen = menu.exec(pos)
            if self._reopen:
                continue
            break
        if chosen is None:
            return
        fn = handlers.get(chosen)
        if fn is not None:
            fn(context)


def _row(inner, margins=(14, 3, 14, 3), fill=False):
    """把控件包进一行，作为 QWidgetAction 的默认控件；菜单行高与左右内距跟普通 item 对齐。
    fill=False：左对齐 + 右弹性（标题 / 内容宽控件）；fill=True：不给弹性，交由 inner 自身
    Expanding 策略铺满行宽（按钮需与菜单同宽时用）。"""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(*margins)
    lay.setSpacing(0)
    lay.addWidget(inner)
    if not fill:
        lay.addStretch(1)
    return w


def _group_header(menu, title):
    """就地展开时的分组标题：与展开钮完全同款——success 实底 + 白字圆角胶囊，整条横铺（与按钮同缘同宽、不留缝），
    不可点。经 QWidgetAction 挂进菜单。"""
    lbl = QLabel(title)
    lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    lbl.setStyleSheet(
        f"color:{_HEADER_COLOR}; background:{_HEADER_BG}; font-weight:600; font-size:13px;"
        f" border:none; border-radius:{TOK_RADIUS['md']}px; padding:5px 14px;")
    wa = QWidgetAction(menu)
    # 左右 0 内距：与按钮同宽同缘线、铺满整个菜单（不留缝）
    wa.setDefaultWidget(_row(lbl, margins=(0, 4, 0, 4), fill=True))
    return wa
