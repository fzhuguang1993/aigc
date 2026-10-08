"""
gui/dialogs_launcher.py —— 总唤出面板：一个全局键弹搜索框，敲两下回车即开工具

一个键管所有工具，省得为每个工具抢一个系统热键（也避免与微信/QQ 的键位打架）。
形态参考 uTools/Spotlight：深灰半透明的小窗、屏幕居中偏上、输入即过滤、失焦自动收起。

要点：
- 面板必须能在**主窗口藏在托盘时**弹出来，所以不挂主窗口当 parent（挂了就跟着
  一起隐藏），改成顶层 Tool 窗口 + 置顶；
- 底色走“深灰 + 半透明”：窗口本来已是 WA_TranslucentBackground，所以给自绘
  背景卡片传一个带 alpha 的 QColor 就能透出桌面（不用改布局，也不影响其它窗口）；
- 失焦即收起：靠窗口的 ActivationChange 判断，不用 focusOutEvent——焦点在子控件
  之间移动（输入框→卡片）也会触发它，那样一点卡片面板就消失了；
- 匹配同时吃「前缀 / 包含 / 跳字(子序列) / 简介」四档并按档排序：记不全名字时
  输「爆拆」也要能找到「爆款拆解」，跳字匹配就是为这个准备的；
- 结果区是「一排多个」的入口卡（图标+名称在上，自己的全局快捷键胶囊在下），
  排满自动换行。列数不固定，所以 ↑↓ 跨行只能照实际排出来的坐标算行
  （见 rows_of），不能按“下标 ± 列数”算——那样列数一变就走错格；
- 四个方向键都在搜索框层面接管（见 eventFilter）：QLineEdit 会把 ←→ 当自己的
  光标键接住，事件根本不往下传，不接管就是“上下灵、左右不灵”；
- 面板高度跟着结果数收（见 _fit_height）：写死 420 高在只匹配到一颗卡时，
  下面拖一大块空板，看着像控件坏了；
- 结果分三段：工具卡在上，本地文件与文档内容在下（数据来自 core.fileindex
  的启动后台索引）。三段共用一套键盘导航，所以 _step 吃的是「相对同一个
  容器的绝对坐标」：chip 的 x/y 相对 _FlowHost、行相对 _body，直接拿控件自己
  的坐标比 y 会把两段的行算错；
- 查询走后台线程（95 万行 LIKE + 全文索引不该卡住敲字），输入停 120ms 才发
  一次，每发一次 _seq 递增、回来对不上号就丢弃：不丢弃就会被慢查询盖掉，
  表现为“打的字和结果不对应”。
"""
import html
import os
import threading
import time

from PySide6.QtCore import Qt, QEvent, QSize, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPalette, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QScrollArea, QSizePolicy, QSplitter,
                               QToolButton, QVBoxLayout, QWidget)

from core.fileindex import match_score
from gui.menus import StyledMenu
from gui.theme import tokenize
from gui.widgets import FlowLayout
from gui.window_foreground import raise_to_front
from gui.window_frame import apply_rounded
from gui.launcher_preview import DocCard, PreviewPane, img_worker
from store import app_state

#: 双栏面板尺寸：默认可观大且可自由拉伸（旧的固定 560x420 把结果框压成一条）
_DFT_W, _DFT_H = 1120, 680
_MIN_W, _MIN_H = 820, 520
#: 左列名义宽度：还没排版时 _FlowHost 问不到自身宽度，用它兜底换行
_LEFT_W = 520
#: 结果区最低高度：一颗都没匹配上时也不能收成一条缝（看着像窗口挂了）
_MIN_LIST_H = 80
#: 结果区最高高度：工具多到装不下就交给滚动条，别把面板长成一条顶天立地的柱子
_MAX_LIST_H = 320
#: 三态视图；_ICON 给非列表视图定 (缩略图px, 卡片宽)
#: 卡片宽在原基础上左右各缩 ~10%（配合 FlowLayout 修好的固定尺寸排位，选中外框不再挤在一起）
_VIEWS = ("list", "medium", "large")
_ICON = {"medium": (64, 132), "large": (120, 170)}
#: 判定“同一行”的 y 容差：FlowLayout 里同一行 y 相同，留点余量防四舍五入
_ROW_GAP = 8
#: 本地文件/文档内容各取多少行：面板已加高且交给滚动条浏览，可以多给一些，
#: 真要找特定的文件会继续打字缩小范围
_MAX_FILE_ROWS = 60
_MAX_DOC_ROWS = 24
#: 输入停多久才发一次查询：每敲一个字就扫 95 万行是把自己的输入卡住
_QUERY_DEBOUNCE_MS = 120
#: 测试里置 True：后台线程换成同步执行。异步本身不是这里要验的东西
#: （要验的是 seq 丢弃），但离屏测试等不到线程回来，结果永远贴不上去
_SYNC_SEARCH = False

# ---------- 深灰半透明配色 ----------
# 这些色值故意不碰 theme._SEED_TO_TOKEN 里的种子 hex，否则会被 tokenize 映回
# 浅色令牌（#FFFFFF→card、#1F2329→text 那些）；只有主色 #3370FF 故意用种子值，
# 让它跟着 ui_kit 的品牌色走。
_PANEL_BG = QColor(28, 32, 38, 232)             # 卡片底：~91% 不透明，透出桌面
_PANEL_BORDER = QColor(255, 255, 255, 30)       # 深底上用半透明白勾轮廓，白描边会扎眼
_TEXT = "#E8EAED"
_WEAK = "#9AA4B0"
_HAIRLINE = "rgba(255, 255, 255, 0.10)"

#: 搜索框里接管的四个方向键：(键, 横向步进, 纵向步进)
_STEP_KEYS = ((Qt.Key.Key_Left, -1, 0), (Qt.Key.Key_Right, 1, 0),
              (Qt.Key.Key_Up, 0, -1), (Qt.Key.Key_Down, 0, 1))

#: 入口卡外观。走 objectName 选择器，避免被应用级样式表里的 QPushButton/QLabel 盖掉；
#: 选中态用动态属性 cur，改属性后要 repolish 才会重新取值（Qt 样式表不监听属性变化）。
_CHIP_QSS = """
QFrame#LChip { background:rgba(255, 255, 255, 0.06);
               border:1px solid rgba(255, 255, 255, 0.12); border-radius:9px; }
QFrame#LChip:hover { background:rgba(255, 255, 255, 0.12);
               border-color:rgba(255, 255, 255, 0.22); }
QFrame#LChip[cur="1"] { border-color:#3370FF; background:rgba(51, 112, 255, 0.22); }
"""

#: 文件/文档行：整行宽、靠左对齐，选中态与入口卡一个色（三段得看上去是一套）
_ROW_QSS = """
QFrame#LRow { background:rgba(255, 255, 255, 0.04);
              border:1px solid transparent; border-radius:7px; }
QFrame#LRow:hover { background:rgba(255, 255, 255, 0.10); }
QFrame#LRow[cur="1"] { border-color:#3370FF; background:rgba(51, 112, 255, 0.18); }
"""

#: 网格图标卡 + 视图切换按钮。同样走 objectName 选择器，躲开应用级样式。
_CARD_QSS = """
QFrame#LCard { background:rgba(255, 255, 255, 0.05);
               border:1px solid rgba(255, 255, 255, 0.10); border-radius:9px; }
QFrame#LCard:hover { background:rgba(255, 255, 255, 0.12); }
QFrame#LCard[cur="1"] { border-color:#3370FF; background:rgba(51, 112, 255, 0.22); }
QToolButton#LViewBtn { color:#9AA4B0; background:transparent; border:none;
               border-radius:5px; padding:2px 7px; font-size:14px; }
QToolButton#LViewBtn:checked { color:#8AB4FF; background:rgba(51, 112, 255, 0.24); }
QToolButton#LViewBtn:hover { background:rgba(255, 255, 255, 0.10); }
"""


def rows_of(positions, gap=_ROW_GAP):
    """按 y 坐标把入口分进行，返回 [[下标, ...], ...]（行内按 x 升序）。

    纯函数、只吃 (x, y) 串，是为了不排版也能测：真控件在离屏测试里没坐标。
    还没排版时坐标全是 0，会被归成一行，↑↓ 于是退化成 ←→——宁可不跨行，
    也不能算出负数下标或者越界撞到不存在的卡片。"""
    groups = []
    for idx, (_x, y) in enumerate(positions or []):
        if groups and abs(y - groups[-1][1]) <= gap:
            groups[-1][0].append(idx)
        else:
            groups.append([[idx], y])
    rows = [g[0] for g in groups]
    xs = dict(enumerate(p[0] for p in (positions or [])))
    return [sorted(r, key=lambda i: xs.get(i, 0)) for r in rows]


def locate(rows, index):
    """下标 -> (行号, 行内第几个)；找不到给 (0, 0)（空面板本来就不会走到这儿）"""
    for r, row in enumerate(rows):
        if index in row:
            return r, row.index(index)
    return 0, 0


def key_display(seq):
    """键位串照原样展示，只把 Qt 的 Meta 换回用户说的 Win"""
    return str(seq or "").replace("Meta+", "Win+")


def _repolish(widget):
    st = widget.style()
    st.unpolish(widget)
    st.polish(widget)


class _Chip(QFrame):
    """一个快捷入口：图标+名称在上，自己的全局快捷键做成胶囊在下。

    胶囊那行“没配键也要占着”（只是空着不画底色）：卡片高不齐时 FlowLayout
    是顶对齐的，一排里混着两种高度看上去就是锯齿（实测就是这样）。
    焦点一律不给——键盘焦点始终留在搜索框，否则方向键被卡片抢走，
    打字和选卡只能二选一。"""

    clicked = Signal(object)
    entered = Signal(object)
    kind = "tool"                         # _activate 据此分派（文件/文档行各自另说）

    def __init__(self, name, icon, desc, key, parent=None):
        super().__init__(parent)
        self.setObjectName("LChip")
        # QFrame 子类自己的 QSS 背景不渲染（不吃样式表里的 background），得显式开：
        # 不开就是“深底上贴了一圈看不见的白框”，选中态只剩描边在动
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.name = name
        self.setToolTip(desc or name)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(4)
        lb = QLabel("%s %s" % (icon, name))
        lb.setStyleSheet(tokenize(
            "color:%s; font-size:13px; background:transparent;" % _TEXT))
        lay.addWidget(lb)
        # 两种写法只差颜色，字号与 padding 一模一样：两颗卡自然等高。
        # 没键时给一个空格而不是空串：空串走另一条 sizeHint 路径，实测会高 2px
        self.kb = QLabel(key_display(key) or " ")
        self.kb.setObjectName("LChipKey")
        self.kb.setStyleSheet(tokenize(
            "color:%s; font-size:11px; font-weight:600;"
            " background:%s; border-radius:4px; padding:1px 5px;" % (
                ("#8AB4FF", "rgba(51, 112, 255, 0.24)")
                if key else ("transparent", "transparent"))))
        lay.addWidget(self.kb)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_current(self, on):
        self.setProperty("cur", "1" if on else "")
        _repolish(self)

    def mousePressEvent(self, e):
        # 只认左键按下：按下就发（不等 release），手感与点列表项一致；
        # 右键照常冒泡，别把面板的右键菜单机会吃掉
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self)
            e.accept()
            return
        super().mousePressEvent(e)

    def enterEvent(self, e):
        self.entered.emit(self)
        super().enterEvent(e)


#: 按扩展名给个图标：扫一眼“是什么类型”比读完一整条文件名快
_EXT_ICON = {
    ".txt": "📄", ".md": "📝", ".log": "🧾", ".rst": "📄",
    ".doc": "📄", ".docx": "📄", ".pdf": "📕",
    ".xls": "📊", ".xlsx": "📊", ".csv": "📊", ".ppt": "📐", ".pptx": "📐",
    ".zip": "🗜", ".rar": "🗜", ".7z": "🗜", ".gz": "🗜",
    ".mp4": "🎬", ".mov": "🎬", ".avi": "🎬", ".mkv": "🎬", ".webm": "🎬",
    ".mp3": "🎵", ".wav": "🎵", ".m4a": "🎵", ".aac": "🎵",
    ".png": "🖼", ".jpg": "🖼", ".jpeg": "🖼", ".gif": "🖼", ".webp": "🖼",
    ".py": "🐍", ".js": "📦", ".json": "📦", ".exe": "⚙",
}


def icon_for(path_or_name):
    return _EXT_ICON.get(os.path.splitext(str(path_or_name or ""))[1].lower(), "📄")


def human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d%s" if unit == "B" else "%.1f%s") % (n, unit)
        n /= 1024.0
    return ""


def human_time(ts):
    """结果右列的时间：今天的写“14:05”，更早的写日期——找刚存的文件靠这个定位"""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    st, now = time.localtime(ts), time.localtime()
    if st[:3] == now[:3]:
        return time.strftime("%H:%M", st)
    if st.tm_year == now.tm_year:
        return time.strftime("%m-%d", st)
    return time.strftime("%Y-%m-%d", st)


def _cut(text, limit):
    """截尾加省略号：路径与命中片段都可能在任意长度上撞墙，整行宽度就这么多"""
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + "…"


def highlight(text, query):
    """把命中的片段染成主色（富文本）。三段文本都得转义：文件名里带 & 是真会撞的，
    不转义就把整行 HTML 撑坏（变成满屏乱码而不是“少一个高亮”）。"""
    s = str(text or "")
    q = str(query or "").strip()
    low, ql = s.lower(), q.lower()
    pos = low.find(ql) if ql else -1
    if pos < 0:
        return html.escape(s)
    parts = [html.escape(s[:pos]),
             '<span style="color:#8AB4FF; font-weight:600;">%s</span>'
             % html.escape(s[pos:pos + len(q)]),
             html.escape(s[pos + len(q):])]
    return "".join(parts)


class _SectionTitle(QLabel):
    """段标题（📁 文件 / 📄 文档内容）：三段靠一行小字分开，不再加描边线"""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.setStyleSheet(tokenize(
            "color:%s; font-size:12px; font-weight:600; background:transparent;"
            " padding:2px 2px 0 2px;" % _WEAK))


class _FileRow(QFrame):
    """一行文件结果：图标 + 文件名（命中段高亮）+ 灰色所在目录 + 右列大小/时间。

    为什么不跟工具卡一起进网格：路径动辄七八十个字符，排成一格就每颗都截断，
    而“它在哪个文件夹”恰恰是这一段最有用的信息。
    焦点依旧不给（NoFocus）、信号与 _Chip 同形：三段得共用一套键盘导航。"""

    clicked = Signal(object)
    doubleClicked = Signal(object)              # 双击才真打开（单击只选中）
    entered = Signal(object)
    menu_requested = Signal(object, object)       # (行, 全局坐标)

    kind = "file"

    def __init__(self, item, query="", parent=None):
        super().__init__(parent)
        self.item = dict(item or {})
        self.path = str(self.item.get("path") or "")
        self.name = str(self.item.get("name") or os.path.basename(self.path))
        self.dir = str(self.item.get("dir") or os.path.dirname(self.path))
        self.query = str(query or "")
        self.setObjectName("LRow")
        # QFrame 子类不开这个属性不吃 QSS 背景（入口卡踩过同一个坑）
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        tip = self.path
        if self.item.get("snippet"):
            tip = "%s\n%s" % (self.path, self.item["snippet"])
        self.setToolTip(tip)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(9, 5, 9, 5)
        lay.setSpacing(8)
        # 图标列必须钉死宽度：emoji 走彩色字体，字形实际比 14px 字号宽，
        # 若沿用 _label 的 Ignored 水平策略会被压到 0 宽、字形糊进右边文件名一列
        # （就是列表里“图标和文字重叠”的成因），所以这里给固定宽 + Fixed 策略。
        lay.addWidget(self._icon_label(icon_for(self.path)))
        mid = QVBoxLayout()
        mid.setSpacing(1)
        mid.addWidget(self._label(highlight(self.name, query),
                                  "font-size:13px;", rich=True))
        for extra in self._extra_lines():
            mid.addWidget(extra)
        mid.addWidget(self._label(_cut(self.dir, 78), "font-size:11px;"))
        lay.addLayout(mid, 1)
        right = QVBoxLayout()
        right.setSpacing(1)
        right.addWidget(self._label(human_size(self.item.get("size")),
                                    "font-size:11px;", right=True))
        right.addWidget(self._label(human_time(self.item.get("mtime")),
                                    "font-size:11px;", right=True))
        lay.addLayout(right)

    def _icon_label(self, icon):
        lb = QLabel(icon, self)
        lb.setStyleSheet(tokenize("color:%s; font-size:14px; background:transparent;"
                                  % _TEXT))
        lb.setFixedWidth(22)
        lb.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return lb

    def _label(self, text, css, rich=False, right=False):
        lb = QLabel(text, self)
        lb.setStyleSheet(tokenize("color:%s; background:transparent; %s"
                                  % (_TEXT if rich else _WEAK, css)))
        if rich:
            lb.setTextFormat(Qt.TextFormat.RichText)
        # Ignored：允许被压到 sizeHint 以下，否则长路径会把右列大小/时间顶出面板
        lb.setSizePolicy(QSizePolicy.Policy.Ignored,
                         QSizePolicy.Policy.Preferred)
        lb.setAlignment(
            (Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft)
            | Qt.AlignmentFlag.AlignVCenter)
        return lb

    def _extra_lines(self):
        return []

    def set_current(self, on):
        self.setProperty("cur", "1" if on else "")
        _repolish(self)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self)              # 单击：选中 + 更新预览，不打开
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit(self)        # 双击：打开
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def enterEvent(self, e):
        self.entered.emit(self)
        super().enterEvent(e)

    def _menu(self, pos):
        self.menu_requested.emit(self, self.mapToGlobal(pos))


class _DocRow(_FileRow):
    """文档内容行：比文件行多一行命中片段——“在哪提到的”才是正文搜索的全部意义"""

    kind = "doc"

    def _extra_lines(self):
        snip = _cut(self.item.get("snippet") or "", 96)
        if not snip:
            return []
        return [self._label(highlight(snip, self.query),
                            "font-size:12px;", rich=True)]


class _IconCard(QFrame):
    """网格图标卡：缩略图/类型图标在上，两行文件名在下。

    与 _FileRow 同一套接口（kind/path/name/set_current/clicked/entered/menu_requested），
    这样 _rows()/_step/_set_cur/右键菜单都无需区分“列表还是网格”。缩略图先放
    类型 emoji 占位，真实图片/视频/文档图由 P2 异步回填（set_pixmap）。"""

    clicked = Signal(object)
    doubleClicked = Signal(object)
    entered = Signal(object)
    menu_requested = Signal(object, object)

    def __init__(self, item, query="", icon_px=64, card_w=148, kind="file", parent=None):
        super().__init__(parent)
        self.kind = kind
        self.item = dict(item or {})
        self.path = str(self.item.get("path") or "")
        self.name = str(self.item.get("name") or os.path.basename(self.path))
        self.dir = str(self.item.get("dir") or os.path.dirname(self.path))
        self.query = str(query or "")
        self._icon_px = int(icon_px)
        self.setObjectName("LCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.setFixedSize(int(card_w), self._icon_px + 62)
        self.setToolTip(self.path)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 6)
        lay.setSpacing(4)
        self.thumb = QLabel()
        self.thumb.setFixedSize(self._icon_px, self._icon_px)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setStyleSheet("font-size:%dpx; background:transparent;"
                                 % int(self._icon_px * 0.55))
        self.thumb.setText(icon_for(self.path))
        lay.addWidget(self.thumb, 0, Qt.AlignmentFlag.AlignHCenter)
        lb = QLabel(highlight(_cut(self.name, 22), self.query))
        lb.setWordWrap(True)
        lb.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        lb.setTextFormat(Qt.TextFormat.RichText)
        lb.setStyleSheet(tokenize(
            "color:%s; font-size:12px; background:transparent;" % _TEXT))
        lay.addWidget(lb)

    def set_pixmap(self, pm):
        """真实缩略图回填（P2）：null/空不覆盖，保留类型图标占位。"""
        if pm is None or pm.isNull():
            return
        self.thumb.setPixmap(pm.scaled(
            self._icon_px, self._icon_px, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self.thumb.setText("")

    def set_current(self, on):
        self.setProperty("cur", "1" if on else "")
        _repolish(self)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self)              # 单击：选中 + 更新预览，不打开
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit(self)        # 双击：打开
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def enterEvent(self, e):
        self.entered.emit(self)
        super().enterEvent(e)

    def _menu(self, pos):
        self.menu_requested.emit(self, self.mapToGlobal(pos))


class _FlowHost(QWidget):
    """入口卡的容器：把 FlowLayout 的 heightForWidth 报给外层。

    不报这一层，QScrollArea 问不到“换行之后究竟多高”，最后一排会被裁掉半截
    （项目里 FlowLayout 踩过同一个坑：换行失效三坑之一就是 sizeHint 定位）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.flow = FlowLayout(self, hgap=8, vgap=8)
        self.flow.setContentsMargins(2, 2, 2, 2)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.flow.heightForWidth(max(int(width), 1))

    def sizeHint(self):
        # 离屏/还没排版时宽度问不到，给面板净宽兜底，别算出 0 高
        w = self.width() or (_LEFT_W - 44)
        return QSize(w, max(self.flow.heightForWidth(w), 80))


class LauncherDialog(QDialog):
    """工具 + 本地文件 + 文档内容的唤出面板。on_open(name) 由主窗口传入
    （与卡片点击共用 open_tool）；文件/文档两个段直接走 utils.desktop_utils。"""

    # 后台查询线程回主线程贴结果用的信号：普通线程没有 Qt 事件循环，
    # 不能靠 QTimer.singleShot（定时器不触发），只能靠信号 queued 投递。
    searchDone = Signal(int, object, object, int)

    def __init__(self, on_open, parent=None):
        # WindowStaysOnTopHint 不是可选的美化：热键是在"别的软件有焦点"时触发的，
        # 而这条面板又要在主窗口藏在托盘时单独浮出来——不置顶，它就会弹到用户
        # 正看着的窗口底下，观感等同于"快捷键没生效"（本机实测就是这个现象）。
        super().__init__(parent, Qt.WindowType.Tool |
                         Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("🔍 唤出工具")
        self._on_open = on_open
        self._items = []              # [(name, icon, desc)] 可打开的工具
        self._armed = False           # 是否已"站稳"，见类注释的失焦收起
        self._chips = []              # 当前展示的入口卡（下标就是键盘选中的坐标）
        self._frows = []              # 文件行（与 _chips 拼成同一条导航链）
        self._drows = []              # 文档内容行
        self._cur = -1                # 键盘选中的那一颗/那一行（下标在 _rows() 里）
        self._seq = 0                 # 查询轮号：对不上号的回包一律丢
        self._view = "list"           # 当前视图：list / medium / large（P1 用）
        self._last = None             # 上一次结果 (files, docs, indexed)：切视图不重查
        self._pinned = bool(app_state.get("launcher_pinned", False))  # 钉住：失焦不自动收起
        self.setStyleSheet(tokenize(_CHIP_QSS + _ROW_QSS + _CARD_QSS))

        # 双栏：左＝搜索+工具+结果，右＝预览窗。外层用 QVBoxLayout 兜住 splitter，
        # 因为 apply_rounded 只认顶层 QVBoxLayout 来加圆角边距；分栏体本身进 QSplitter。
        _outer = QVBoxLayout(self)
        _outer.setContentsMargins(18, 18, 18, 14)
        _outer.setSpacing(10)
        self._split = QSplitter(Qt.Orientation.Horizontal)
        self._left = QWidget()
        lay = QVBoxLayout(self._left)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(
            "搜工具、本地文件、文档内容（支持跳字：爆拆 → 爆款拆解）")
        self.edit.setStyleSheet(tokenize(
            "QLineEdit { border:none; background:transparent; font-size:18px; "
            "color:%s; padding:4px 0; }" % _TEXT))
        # 深底上 palette 也得钉一遍：光标与占位符都是按 palette 取色的，
        # 只改样式表的 color 会留下白底默认的浅灰光标，在深底上几乎看不见
        pal = self.edit.palette()
        for role, col in ((QPalette.ColorRole.Text, _TEXT),
                          (QPalette.ColorRole.PlaceholderText, _WEAK),
                          (QPalette.ColorRole.Base, "transparent"),
                          (QPalette.ColorRole.Highlight, "#3370FF"),
                          (QPalette.ColorRole.HighlightedText, _TEXT)):
            pal.setColor(role, QColor(col))
        self.edit.setPalette(pal)
        lay.addWidget(self.edit)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sep.setStyleSheet(tokenize("background:%s;" % _HAIRLINE))
        lay.addWidget(sep)

        self.host = _FlowHost()
        self.flow = self.host.flow
        # 三段共用一个内容容器：QScrollArea 只认一个 widget，而“工具 → 文件 →
        # 文档内容”这个先后顺序就是键盘上下走的顺序，必须写死在布局里
        self._body = QWidget()
        bl = QVBoxLayout(self._body)
        bl.setContentsMargins(2, 2, 2, 2)
        bl.setSpacing(4)
        bl.addWidget(self.host)
        # 状态行常驻在工具段下方：索引建立进度 / 搜索中 / 无结果都写这一行。
        # 用户第一次打开面板就能看见"本地索引到底建到哪了"，而不是对着空板子
        # 以为功能坏了（原实现只在"一个文件都没收录且没结果"时才亮，索引建好后
        # 反而永远不显示，等于没进度）。
        self.lbl_hint = QLabel("")
        self.lbl_hint.setVisible(False)
        self.lbl_hint.setStyleSheet(tokenize(
            "color:%s; font-size:12px; background:transparent;" % _WEAK))
        bl.addWidget(self.lbl_hint)
        # 视图切换条（列表/中图标/大图标）：常驻在工具段与文件段之间，有结果才显
        bl.addWidget(self._build_view_bar())
        self.lbl_files = _SectionTitle("📁 文件")
        self.lbl_docs = _SectionTitle("📄 文档内容")
        # 每段两套容器：list 用 QWidget+QVBoxLayout，grid 用 _FlowHost。按视图显隐——
        # 用 QWidget 而非裸布局，是因为 QLayout 不能可靠 setVisible 切换。
        self._file_list = QWidget()
        self._file_lay = QVBoxLayout(self._file_list)
        self._file_lay.setContentsMargins(0, 0, 0, 0)
        self._file_lay.setSpacing(3)
        self._file_grid = _FlowHost()
        self._doc_list = QWidget()
        self._doc_lay = QVBoxLayout(self._doc_list)
        self._doc_lay.setContentsMargins(0, 0, 0, 0)
        self._doc_lay.setSpacing(3)
        self._doc_grid = _FlowHost()
        for w in (self.lbl_files, self._file_list, self._file_grid,
                  self.lbl_docs, self._doc_list, self._doc_grid):
            bl.addWidget(w)
        bl.addStretch(1)
        self.lbl_files.setVisible(False)
        self.lbl_docs.setVisible(False)
        self._file_grid.setVisible(False)
        self._doc_grid.setVisible(False)

        area = QScrollArea()
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidgetResizable(True)
        area.setStyleSheet(tokenize(
            "QScrollArea { background:transparent; border:none; }"
            "QScrollArea > QWidget > QWidget { background:transparent; }"))
        area.setWidget(self._body)
        lay.addWidget(area, 1)
        self._area = area
        # 滚轮转发：鼠标停在行/卡上也能滚（行本身不吃 wheel，会冒到 viewport）
        self._area_viewport = area.viewport()
        self._area_viewport.installEventFilter(self)

        tip = QHBoxLayout()
        tip.setSpacing(8)
        for k, v in (("Enter", "打开"), ("Ctrl+Enter", "开目录"),
                     ("↑↓←→", "选择"), ("Esc", "关闭")):
            kb = QLabel(k)
            kb.setStyleSheet(tokenize(
                "color:#8AB4FF; font-size:12px; font-weight:600;"
                " background:rgba(51, 112, 255, 0.24);"
                " border-radius:4px; padding:1px 6px;"))
            tb = QLabel(v)
            tb.setStyleSheet(tokenize(
                "color:%s; font-size:12px; background:transparent;" % _WEAK))
            tip.addWidget(kb)
            tip.addWidget(tb, 0, Qt.AlignmentFlag.AlignVCenter)
        tip.addStretch(1)
        # 钉住📌：给不想每次按键呼出的人——钉上后面板失焦也不收，直到自己 Esc/关掉。
        self.btn_pin = QToolButton()
        self.btn_pin.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_pin.setCheckable(True)
        self.btn_pin.setChecked(self._pinned)
        self.btn_pin.setToolTip("钉住：面板失焦不自动收起（再点取消）")
        self.btn_pin.setText("📌 已钉住" if self._pinned else "📌 钉住")
        self.btn_pin.setStyleSheet(tokenize(
            "QToolButton { color:%s; background:transparent; border:none;"
            " border-radius:5px; padding:2px 8px; font-size:12px; }"
            " QToolButton:checked { color:#8AB4FF;"
            " background:rgba(51, 112, 255, 0.24); }" % _WEAK))
        self.btn_pin.clicked.connect(self._toggle_pin)
        tip.addWidget(self.btn_pin)
        lay.addLayout(tip)

        # 右侧预览窗：随选中项实时预览（文本/图片/视频/文档；其余“无法预览”）
        self._preview = PreviewPane(self)
        # 单击预览图 → 打开那个文件（ZoomImageView 的 clicked_open 已在 pane 内转成 openRequested）
        self._preview.openRequested.connect(self._open_path_only)
        self._split.addWidget(self._left)
        self._split.addWidget(self._preview)
        self._split.setStretchFactor(0, 0)
        self._split.setStretchFactor(1, 1)
        self._split.setCollapsible(1, True)
        sp = app_state.get("launcher_split")
        if isinstance(sp, (list, tuple)) and len(sp) == 2 and sum(int(x) for x in sp) > 0:
            self._split.setSizes([int(sp[0]), int(sp[1])])
        else:
            self._split.setSizes([520, 600])
        _outer.addWidget(self._split, 1)

        # 尺寸：默认可观大且可拉伸，优先恢复上次关窗时的尺寸
        self.setMinimumSize(_MIN_W, _MIN_H)
        sz = app_state.get("launcher_size")
        if isinstance(sz, (list, tuple)) and len(sz) == 2:
            self.resize(max(int(sz[0]), _MIN_W), max(int(sz[1]), _MIN_H))
        else:
            self.resize(_DFT_W, _DFT_H)

        self.edit.textChanged.connect(self._refill)
        # 后台线程查完靠这个信号回主线程贴结果（emit 自动 queued 到主事件循环）
        self.searchDone.connect(self._apply_results)
        # 四个方向键在搜索框层面就接走（否则 ←→ 被 QLineEdit 当光标键吃掉）
        self.edit.installEventFilter(self)
        # 节流：敲字时每停 120ms 才发一次查询（每敲一个字母就扫 95 万行是卡自己）
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_QUERY_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._search_now)
        # 面板可见期间每 1 秒刷一次索引状态：首扫要几十秒，只有让进度自己动，
        # 用户才看得见"在建立"。status() 现在只读 meta 里的缓存计数（O(1)，不碰
        # files 表），所以逐秒刷也不心疼；就绪后依旧主动停表，不留常驻定时器。
        self._poll = QTimer(self)
        self._poll.setInterval(1000)
        self._poll.timeout.connect(self._refresh_status)
        # 起始视图取"默认视图"偏好（缺省列表）
        self._show_view(app_state.get("launcher_view_default")
                        or app_state.get("launcher_view") or "list")
        # 网格卡缩略图后台服务：用进程级常驻单例，**不能当本对话框的子对象**。
        # 挂在窗口上时，退出（或关窗）会走 deleteChildren 析构一个还在跑的 QThread →
        # qFatal("QThread: Destroyed while thread is still running")，Python 层只留下
        # 一句 segfault（本机 minidump 实测栈已验证）。懒启动依旧在 _apply_results。
        self._thumb = img_worker()
        self._thumb.ready.connect(self._on_thumb)
        # 预览窗折叠状态沿用上次（空格可切）
        self._preview_open = bool(app_state.get("launcher_preview_open", True))
        if not self._preview_open:
            self._preview.setVisible(False)
        apply_rounded(self, resizable=True, corner_grip=True,
                      show_min=False, show_max=False, add_titlebar=False,
                      card_bg=_PANEL_BG, border=_PANEL_BORDER)

    # ---------- 数据 ----------
    def _load_tools(self):
        """可打开的工具（下架/规划中的不进面板），固定过的排前面省得打字"""
        from gui.tool_panels import PANEL_FACTORIES
        from gui.tools_registry import TOOLS, load_pinned
        out = [(n, ico, d) for n, ico, d, fac in TOOLS
               if fac and fac in PANEL_FACTORIES]
        pinned = set(load_pinned())
        out.sort(key=lambda t: 0 if t[0] in pinned else 1)
        return out

    def _refill(self, text=""):
        """按过滤结果重排入口卡：先拆干净再建，卡片与 _chips 下标必须一一对应"""
        if not self._items:
            self._items = self._load_tools()
        for c in self._chips:
            self.flow.removeWidget(c)
            c.setParent(None)
            c.deleteLater()
        self._chips = []
        from gui.tools_registry import tool_shortcuts
        keys = tool_shortcuts() or {}
        scored = []
        for name, icon, desc in self._items:
            s = match_score(text, name, desc)
            if s is not None:
                scored.append((s, name, icon, desc))
        scored.sort(key=lambda x: (x[0], x[1]))
        for _s, name, icon, desc in scored:
            chip = _Chip(name, icon, desc, keys.get(name) or "")
            chip.clicked.connect(self._on_chip)
            chip.entered.connect(self._on_enter)
            self.flow.addWidget(chip)
            self._chips.append(chip)
        self._clear_rows()
        self._set_cur(0 if self._chips else -1)
        self.host.updateGeometry()          # 行数变了，让外层重新问一次高度
        self._debounce.stop()               # 上一个词的等待作废，从头再计 120ms
        if str(text or "").strip():
            self._debounce.start()
        else:
            self._seq += 1                  # 作废在途回包：清空后不能再贴上次结果
            self._refresh_status()          # 空查询：把"索引就绪/建立中"摊给用户看
        self._fit_height()

    def _fit_height(self):
        """双栏可拉伸后不再把整窗收缩到刚好（那既跟用户拉伸打架，也正是“结果框太短”
        的来源）：这里只通知各层几何变了，结果区由 QScrollArea 吃满左列剩余高度、
        超出交给滚动条。"""
        for w in (getattr(self, "_body", None), getattr(self, "host", None),
                  getattr(self, "_left", None)):
            try:
                if w is not None:
                    w.updateGeometry()
            except RuntimeError:                # 控件已在事件循环里被销毁
                pass

    # ---------- 本地文件 / 文档内容 ----------
    def _clear_rows(self):
        """拆掉下面两段（连段标题与灰字）：不清就会和新一批结果并排贴在一起"""
        for w in (self.lbl_files, self.lbl_docs, self.lbl_hint):
            w.setVisible(False)
        for store in (self._frows, self._drows):
            for w in store:
                # setParent(None) 就会把它从子布局里摘掉；deleteLater 交给事件循环
                w.setParent(None)
                w.deleteLater()
            store.clear()

    # ---------- 视图切换（列表 / 中图标 / 大图标） ----------
    def _build_view_bar(self):
        w = QWidget()
        bar = QHBoxLayout(w)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(2)
        bar.addStretch(1)
        group = QButtonGroup(self)
        group.setExclusive(True)
        self._view_btns = {}
        for view, glyph, tip in (("list", "☰", "列表"),
                                 ("medium", "▦", "中图标"),
                                 ("large", "▩", "大图标")):
            b = QToolButton(w)
            b.setText(glyph)
            b.setToolTip(tip)
            b.setCheckable(True)
            b.setObjectName("LViewBtn")
            b.clicked.connect(lambda _=False, v=view: self._show_view(v, persist=True))
            group.addButton(b)
            bar.addWidget(b)
            self._view_btns[view] = b
        self._bar_widget = w
        w.setVisible(False)
        return w

    def _show_view(self, view, persist=False):
        """切视图：记住选择、点亮按钮，然后用上一次结果就地重建（不重新查库）。"""
        if view not in _VIEWS:
            view = "list"
        self._view = view
        if persist:
            app_state.set_value("launcher_view", view)
        for key, b in self._view_btns.items():
            b.setChecked(key == view)
        if self._last is not None:
            self._apply_results(self._seq, *self._last)
        else:
            self._fit_height()

    # ---------- 状态行 ----------
    def _set_hint(self, text, visible=True):
        """把状态行写成 text（空串即隐藏）。改完重算高度，别把它挤出行。"""
        self.lbl_hint.setText(text or "")
        self.lbl_hint.setVisible(bool(visible and text))
        self._body.updateGeometry()
        self.host.updateGeometry()

    def _index_state(self):
        """(启用?, 在建?, 文件数, 文档数)：全走只读查询，任何异常退回中性值，
        绝不因为索引层的小毛病把面板带崩。"""
        try:
            from core import fileindex
            from workers import file_watcher
            if not fileindex.enabled():
                return False, False, 0, 0
            st = fileindex.status()
            files = int(st.get("files") or 0)
            docs = int(st.get("docs") or 0)
            scans = int(file_watcher.stats().get("scans") or 0)
            # 首轮没扫完（或一个都没收录）= 还在建，值得实时给进度；
            # 线程没在跑（比如没开后台扫）时靠 files==0 兜底，也不会误报"就绪 0"。
            building = files == 0 or (file_watcher.running() and scans < 1)
            return True, building, files, docs
        except Exception:
            return True, False, 0, 0

    def _refresh_status(self):
        """空查询时把索引状态摊开；一旦就绪就停掉轮询（进度已经到手，不必再刷）。"""
        if str(self.edit.text() or "").strip():
            return                          # 打字/搜索中/结果由 _search_now、_apply_results 管
        on, building, files, docs = self._index_state()
        if not on:
            self._set_hint("🔍 本地文件搜索已在设置里关闭", True)
        elif building:
            self._set_hint("🔍 正在建立本地索引：已收录 %s 个文件 · %s 篇文档"
                           "（首次约需 1 分钟，可先搜工具）"
                           % ("{:,}".format(files), "{:,}".format(docs)), True)
        else:
            self._set_hint("🔍 本地索引就绪：%s 个文件 · %s 篇文档，输入即可搜索"
                           % ("{:,}".format(files), "{:,}".format(docs)), True)
            self._poll.stop()               # 就绪后不用再逐秒刷
        self._fit_height()

    def _query(self, q):
        """两段查询 + 已收录文件数。索引层任何毛病都不能把面板带崩：
        用户看到的顶多是“这一段没结果”，而不是报错弹窗。"""
        try:
            from core import fileindex
            files = (fileindex.search_files(q, _MAX_FILE_ROWS)
                     if fileindex.enabled() else [])
            docs = (fileindex.search_docs(q, _MAX_DOC_ROWS)
                    if fileindex.doc_enabled() else [])
            return files, docs, int(fileindex.status().get("files") or 0)
        except Exception:
            return [], [], 0

    def _search_now(self):
        """节流定时器到点：发一次查询。真查的时候已经在后台线程里了，
        这里只负责“把当前输入拓下来 + 占一个轮号”。"""
        q = str(self.edit.text() or "").strip()
        if not q:
            self._clear_rows()
            self._fit_height()
            return
        self._seq += 1
        seq = self._seq
        if _SYNC_SEARCH:                    # 测试路径：离屏等不到线程回来
            self._apply_results(seq, *self._query(q))
            return

        def work():
            files, docs, indexed = self._query(q)
            # 跨线程只能走信号：普通 Python 线程没有 Qt 事件循环，在这儿调
            # QTimer.singleShot 的定时器永远不会触发（真机实测：卡在"搜索中"、
            # 结果永远贴不上来；测试走 _SYNC_SEARCH 同步路径恰好绕开了它）。
            # 信号 emit 到活在主线程的接收者 → Qt 自动 queued 投递到主循环执行。
            self.searchDone.emit(seq, files, docs, indexed)

        self._set_hint("🔍 搜索中…", True)   # 95 万行 LIKE 约 0.3 秒：不留痕就等于"按了没反应"
        threading.Thread(target=work, daemon=True, name="launcher-search").start()

    def _apply_results(self, seq, files, docs, indexed=0):
        """贴结果。seq 对不上号一律丢：后台回来的先后不保证，慢查询盖掉快查询
        就是“打的字和结果不对应”（节流反而制造新 bug，必须有这一道）。"""
        if seq != self._seq:
            return
        self._clear_rows()
        self._last = (files, docs, indexed)
        q = str(self.edit.text() or "").strip()
        grid = self._view != "list"
        if grid:
            px, cw = _ICON.get(self._view, _ICON["medium"])
            for it in files or []:
                self._add_row(self._file_grid.flow, self._frows,
                              _IconCard(it, q, px, cw, "file"))
            for it in docs or []:
                self._add_row(self._doc_grid.flow, self._drows,
                              _IconCard(it, q, px, cw, "doc"))
        else:
            for it in files or []:
                self._add_row(self._file_lay, self._frows, _FileRow(it, q))
            for it in docs or []:
                self._add_row(self._doc_lay, self._drows, _DocRow(it, q))
        self.lbl_files.setVisible(bool(self._frows))
        self.lbl_docs.setVisible(bool(self._drows))
        self._file_list.setVisible((not grid) and bool(self._frows))
        self._file_grid.setVisible(grid and bool(self._frows))
        self._doc_list.setVisible((not grid) and bool(self._drows))
        self._doc_grid.setVisible(grid and bool(self._drows))
        self._bar_widget.setVisible(bool(self._frows or self._drows))
        if grid and (self._frows or self._drows):
            if not self._thumb.isRunning():
                self._thumb.start()
            self._thumb.submit([(w.path, w.item.get("mtime"))
                                for w in self._frows + self._drows])
        if self._frows or self._drows or self._chips:
            self._set_hint("", False)         # 有命中就别聒噪，行/卡自己会说话
        else:
            try:
                from core import fileindex
                on = fileindex.enabled()
            except Exception:
                on = False
            if not on:
                self._set_hint("", False)     # 设置里关了就不报"没结果"，那是自找的空白
            elif indexed <= 0:
                # 首扫还没跑完（冷盘实测几十秒）：说清楚"在建"，别让人以为功能坏了
                self._set_hint("🔍 本地索引建立中：已收录 %s 个文件，稍等再搜"
                               % "{:,}".format(max(indexed, 0)), True)
            else:
                self._set_hint("没有找到匹配的文件或文档内容（本地已索引 %s 个文件）"
                               % "{:,}".format(max(indexed, 0)), True)
        self._body.updateGeometry()
        rows = self._rows()
        # 保住已选中的项；从没选过（第一次出结果）就亮第 0 行，否则 _cur 停在 -1，
        # 用户打完字看着结果出来了按回车却没反应——这正是"不知道在不在 work"的主因。
        if not rows:
            self._set_cur(-1, scroll=False)
        elif self._cur < 0 or self._cur >= len(rows):
            self._set_cur(0, scroll=False)
        else:
            self._set_cur(self._cur, scroll=False)
        self._fit_height()

    def _add_row(self, lay, store, row):
        lay.addWidget(row)
        row.clicked.connect(self._on_row)            # 单击：选中
        row.doubleClicked.connect(self._on_row_open)  # 双击：打开
        row.entered.connect(self._on_row_enter)
        row.menu_requested.connect(self._on_row_menu)
        store.append(row)
        return row

    def _rows(self):
        """三段拼成同一条导航链：顺序就是布局里的先后（工具 → 文件 → 文档内容）。
        ↑↓←→ 与 Enter 只认这个下标，所以段序一改键盘顺序跟着改，不用另维护。"""
        return list(self._chips) + list(self._frows) + list(self._drows)

    def _pos(self, w):
        """导航比的是“相对同一个容器的坐标”：chip 的 x/y 相对 _FlowHost、
        行相对 _body，直接拿控件自己的坐标混着比 y，两段的行就会算错。"""
        p = w.mapTo(self._body, 0, 0)
        return p.x(), p.y()

    def _content_height(self):
        """三段加起来要多高：_FlowHost 只报工具那一段，加了下两段还只量它，
        文件行就会被裁掉半截（滚动区问不到真实高度）。"""
        h = 0
        try:
            lay = self._body.layout()
            lay.activate()
            h = int(lay.sizeHint().height())
        except (AttributeError, RuntimeError, TypeError):
            h = 0
        if h <= 0:
            h = self.host.heightForWidth(self.host.width() or (_LEFT_W - 44))
        return h

    # ---------- 选中与打开 ----------
    def _on_chip(self, chip):
        self._activate(chip)

    def _on_enter(self, chip):
        rows = self._rows()
        idx = rows.index(chip) if chip in rows else -1
        if idx >= 0:
            self._set_cur(idx, scroll=False)

    def _on_row(self, row):
        """单击：选中这行（并跟上预览），不打开。打开交给双击/回车。"""
        self._on_row_enter(row)

    def _on_row_open(self, row):
        """双击：真打开。"""
        self._activate(row)

    def _on_row_enter(self, row):
        rows = self._rows()
        if row in rows:
            self._set_cur(rows.index(row), scroll=False)

    def _set_cur(self, index, scroll=True):
        self._cur = index
        rows = self._rows()
        for i, w in enumerate(rows):
            w.set_current(i == index)
        if scroll and 0 <= index < len(rows):
            self._area.ensureWidgetVisible(rows[index], 0, 8)
        self._sync_preview(rows, index)

    def _sync_preview(self, rows, index):
        """选中项是文件/文档 → 预览它；工具/越界 → 复位占位。"""
        w = rows[index] if 0 <= index < len(rows) else None
        if w is not None and getattr(w, "kind", "tool") in ("file", "doc"):
            self._preview.show_item(getattr(w, "item", None))
        else:
            self._preview.show_item(None)

    def _on_thumb(self, path, obj):
        """后台缩略图回来：按 path 找到对应网格卡上色（列表行没图，忽略）。

        文档抽不出首页时后台回的是 DocCard(正文)：“纸样”那张图必须在这儿（主线程）
        画——QPainter/QFont 进了工作线程就是 qFatal，见 launcher_preview 模块头。"""
        if isinstance(obj, DocCard):
            obj = obj.render()
        if not isinstance(obj, QImage) or obj.isNull():
            return
        for w in self._frows + self._drows:
            if isinstance(w, _IconCard) and w.path == path:
                w.set_pixmap(QPixmap.fromImage(obj))

    def _toggle_preview(self):
        """空格：折叠/展开右侧预览窗（记住状态，下次召唤沿用）。
        用存储的 _preview_open 而非 isVisible()：离屏/父窗未显时 isVisible 恒 False，
        拿它判折叠会“永远展不开”。"""
        self._preview_open = not self._preview_open
        self._preview.setVisible(self._preview_open)
        app_state.set_value("launcher_preview_open", self._preview_open)
        if self._preview_open:
            self._split.setSizes([520, 600])
            self._sync_preview(self._rows(), self._cur)
        else:
            self._split.setSizes([self.width(), 0])
        self._fit_height()

    def _toggle_focus(self):
        """Tab：在结果列表与预览窗之间倒焦点。焦点一旦进了 WebEngine/脑图，
        方向键会被它们吃掉，Tab 跳回搜索框才能继续用键盘选文件。"""
        from PySide6.QtWidgets import QApplication
        cur = QApplication.focusWidget()
        if cur is not None and (cur is self._preview or self._preview.isAncestorOf(cur)):
            self.edit.setFocus()
            return
        w = self._preview.stack.currentWidget()
        (w or self._preview).setFocus(Qt.FocusReason.TabFocusReason)

    def _wheel_scroll(self, e):
        """把滚轮量换算成竖直滞条新值（上滚为正→内容上移、值变小）。"""
        bar = self._area.verticalScrollBar()
        dy = e.angleDelta().y()
        if dy == 0:
            return False
        steps = -dy / 120.0
        step = max(int(bar.singleStep()), 40)
        bar.setValue(int(bar.value() + steps * step * 3))
        return True

    def _step(self, dx=0, dy=0):
        """←→ 走扁平顺序（跨行也连着走），↑↓ 按坐标换行并贴最近的列。

        列数不固定，所以“下标 ± 列数”这种算法一律不用：行是排出来才知道的。"""
        all_rows = self._rows()
        n = len(all_rows)
        if not n or self._cur < 0:
            return
        if not dy:
            self._set_cur((self._cur + dx) % n)
            return
        pos = [self._pos(w) for w in all_rows]
        rows = rows_of(pos)
        r, _c = locate(rows, self._cur)
        cand = rows[(r + dy) % len(rows)]
        x = pos[self._cur][0]
        self._set_cur(min(cand, key=lambda i: abs(pos[i][0] - x)))

    def _open(self, name):
        self.hide()
        self._on_open(str(name))

    def _activate(self, w, ctrl=False):
        """回车/点击的统一派发：工具→唤出面板；文件与文档→打开它；
        Ctrl+Enter→打开所在目录。工具没有“所在目录”这个概念，所以 ctrl 等同回车。"""
        if getattr(w, "kind", "tool") == "tool":
            self._open(getattr(w, "name", ""))
            return
        path = str(getattr(w, "path", "") or "")
        if not path:
            return
        from utils.desktop_utils import open_path, reveal_in_folder
        self.hide()
        if ctrl:
            reveal_in_folder(path)
        else:
            open_path(path)

    def _open_path_only(self, path):
        """预览图单击打开：只认当前这一条路径，不动选中行下标。"""
        path = str(path or "")
        if not path or not os.path.exists(path):
            return
        from utils.desktop_utils import open_path
        self.hide()
        open_path(path)

    # ---------- 右键 ----------
    def _on_row_menu(self, row, global_pos):
        """行上的右键菜单：打开 / 打开所在目录 / 复制路径 / 复制文件。
        “复制文件”走 copy_paths_to_clipboard（Windows 上写 CF_HDROP，粘到
        微信就是一个附件，而不是一串文字）；“复制路径”才是纯文本。"""
        from utils.desktop_utils import copy_paths_to_clipboard, reveal_in_folder
        path = str(getattr(row, "path", "") or "")
        menu = StyledMenu(self)
        act_open = menu.addAction("🚀 打开")
        act_dir = menu.addAction("📁 打开所在目录")
        act_text = menu.addAction("📋 复制路径")
        act_file = menu.addAction("📎 复制文件")
        if not path:
            for a in (act_dir, act_text, act_file):
                a.setEnabled(False)
        chosen = menu.exec(global_pos)
        if chosen is act_open:
            self._activate(row)
        elif chosen is act_dir:
            self.hide()
            reveal_in_folder(path)
        elif chosen is act_text:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(path)
        elif chosen is act_file:
            copy_paths_to_clipboard([path])

    # ---------- 显隐 ----------
    def summon(self):
        """弹出并居中偏上（全局热键与托盘菜单共用这一入口）"""
        if self.isVisible() and self.isActiveWindow() and not self._pinned:
            self.hide()                     # 再按一次键＝收起（钉住时不收，只当刷新）
            return
        self._items = self._load_tools()
        self.edit.blockSignals(True)        # clear() 会触发 textChanged，别白排一次
        self.edit.clear()
        self.edit.blockSignals(False)
        self._debounce.stop()               # 上一次残留的待发布查询不能漏进新面板
        self._show_view(app_state.get("launcher_view_default")
                        or app_state.get("launcher_view") or "list")
        self._refill("")                    # 空查询会调 _refresh_status 摆出索引状态
        self._poll.start()                  # 面板可见期间实时刷新"建立中"进度
        self._center_on_screen()
        self.show()
        # show() 只是把排版请求排进队列，不手动 activate 就问不到列表可视高，
        # _fit_height 会退回“量不出、什么都不做”——实测面板就死停在 420 高，
        # 三排卡下面拖一大块空板（用户说的“丑”里就有一条）
        self.layout().activate()
        self._fit_height()
        self.raise_()
        self.activateWindow()
        # activateWindow() 走的是 SetForegroundWindow，会被 Windows 前台锁拒掉；
        # 补一次"挂到前台线程再抢"，面板才真能拿到键盘焦点（否则打字不进门）
        raise_to_front(self)
        self.edit.setFocus()
        # 显示当帧的激活变化不算"用户切走了"，延后一点再开始监
        self._armed = False
        QTimer.singleShot(150, self._arm)

    def _arm(self):
        self._armed = True

    def _center_on_screen(self):
        from PySide6.QtWidgets import QApplication
        scr = QApplication.primaryScreen()
        geo = scr.availableGeometry() if scr is not None else None
        if geo is None:
            return
        self.move(geo.x() + (geo.width() - self.width()) // 2,
                  geo.y() + int(geo.height() * 0.18))     # 偏上：视线自然落点

    def changeEvent(self, e):
        """失焦收起：只在“站稳”之后、且窗口确实不再是活动窗口、且没钉住时才关。

        钉住（📌）是给“不想每次按键呼出”的人：面板一直开着，直到他自己 Esc / 关掉。"""
        if (e.type() == QEvent.Type.ActivationChange and self._armed
                and not self._pinned and not self.isActiveWindow()):
            self.hide()
            return
        super().changeEvent(e)

    def _toggle_pin(self):
        """📌 开关：记住偏好，下次唤出沿用；钉住时 changeEvent 不再因失焦收起。"""
        self._pinned = bool(self.btn_pin.isChecked())
        self.btn_pin.setText("📌 已钉住" if self._pinned else "📌 钉住")
        app_state.set_value("launcher_pinned", self._pinned)

    def hideEvent(self, e):
        # 面板不在眼前就停表：定时器不该在背后一直跑（取数虽已是 O(1)，白刷也没道理）
        self._poll.stop()
        # 看不见的卡不值得抽盘：清掉待办（线程留着空闲，下次唤出直接用）
        th = getattr(self, "_thumb", None)
        if th is not None:
            th.clear()
        # 记住用户调好的整窗尺寸与左右分栏比例，下次召唤直接回到他习惯的样子
        if hasattr(self, "_split"):
            app_state.set_value("launcher_size", [self.width(), self.height()])
            app_state.set_value("launcher_split", list(self._split.sizes()))
        super().hideEvent(e)

    def closeEvent(self, e):
        """关窗：只把手上的活停下、把信号接掉。

        线程本体不归本窗口管（它是进程级单例，退出由 aboutToQuit 统一收）：
        在这儿 wait(300) 然后让对话框带着 QThread 一起被析构，就是
        “QThread: Destroyed while thread is still running” 的 qFatal。"""
        th = getattr(self, "_thumb", None)
        if th is not None:
            th.clear()
            try:
                th.ready.disconnect(self._on_thumb)
            except (RuntimeError, TypeError):
                pass                        # 未连接 / 接收者已亡：没什么可解的
        super().closeEvent(e)

    # ---------- 按键 ----------
    def eventFilter(self, obj, e):
        """四个方向键由面板接管，不归文本编辑。

        卡片焦点一律不给（NoFocus），键盘焦点始终坐在 QLineEdit 上；而
        QLineEdit 认为 ←→ 是它自己的光标键，接住就 accept，事件根本不会冒到面板的
        keyPressEvent（↑↓ 它“单行文本里不管”才漏下来）。实测（逐键 sendEvent 看
        isAccepted）：←→ 选中不动、光标在走；↑↓ 选中会走——这就是
        “上下生效、左右不生效”的全部原因。

        代价写在明面上：搜索框里不能再用 ←→ 挪光标。打字时游标本来就在末尾，
        改错了用 Backspace/Home/End 或鼠标，换回四个键一致值得。"""
        if (e.type() == QEvent.Type.Wheel
                and obj is getattr(self, "_area_viewport", None)):
            if self._wheel_scroll(e):
                e.accept()
                return True
        if obj is self.edit and e.type() == QEvent.Type.KeyPress:
            if e.key() == Qt.Key.Key_Space and not e.modifiers():
                # 空格给预览折叠（模糊匹配不吃空格，代价：搜索框里打不出空格）
                self._toggle_preview()
                e.accept()
                return True
            for key, dx, dy in _STEP_KEYS:
                if e.key() == key:
                    self._step(dx, dy)
                    e.accept()
                    return True
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key.Key_Tab:
            # Tab 在结果列表与预览窗之间切焦点：一旦焦点落在 WebEngine/脑图里，
            # 方向键会被它们吃掉，用 Tab 跳回搜索框才能继续用键盘选文件
            self._toggle_focus()
            return
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            rows = self._rows()
            w = rows[self._cur] if 0 <= self._cur < len(rows) else None
            if w is not None:
                # Ctrl+Enter：文件/文档行→打开所在目录；工具行等同 Enter
                self._activate(
                    w, bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier))
            return
        if k == Qt.Key.Key_Escape:
            self.hide()
            return
        if k == Qt.Key.Key_Space:
            self._toggle_preview()
            return
        if k == Qt.Key.Key_Right:
            self._step(dx=1)
            return
        if k == Qt.Key.Key_Left:
            self._step(dx=-1)
            return
        if k == Qt.Key.Key_Down:
            self._step(dy=1)
            return
        if k == Qt.Key.Key_Up:
            self._step(dy=-1)
            return
        super().keyPressEvent(e)
