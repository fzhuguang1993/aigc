"""
gui/window_frame.py —— 无边框圆角窗体通用装配器（Windows 10 专用）

背景：Windows 10 没有原生窗口圆角（那是 Win11 的 DWM 能力）。想要“整个软件圆角”，
只能去掉系统标题栏、自绘一条标题栏，并把窗口裁成圆角。这里把这套动作打包成一个
函数，主窗口和各个对话框都复用：

    apply_rounded(win, title=..., resizable=..., ...)

它负责三件事：
① 无边框 + 圆角（逐像素透明，抗锯齿无锯齿）：置 FramelessWindowHint +
   WA_TranslucentBackground，顶层窗口自身背景由 QSS 规则
   `QWidget[roundedTop="1"]` 钉成透明；真正可见的圆角卡片由底层子控件
   _RoundedBg 用 QPainter 抗锯齿绘制（卡片向外缩各侧 MARGIN_* 留透明带画软
   阴影——三边窄、底部宽；内容再向内缩 PAD 避开圆角弧——早先的 setMask
   方案是 1bit 掩码，圆角边缘天生带锯齿，已废弃）。
② 自绘标题栏：一条 TitleBar——拖动可移动窗口、双击切换最大化，右侧“最小化/
   最大化/关闭”按钮；插入到窗口顶层 QVBoxLayout 的第 0 行（主窗口由调用方自备）。
③ 拉大放小：需要缩放时在窗口四周的透明带（含阴影像素）盖 8 条透明“边缘把手”，
   各自设相应光标并接管拖拽改尺寸；固定尺寸对话框不装把手。最大化走手动
   （贴到屏幕可用区），因为无边框窗口 showMaximized() 会盖住任务栏。

约定：业务逻辑零改动，只是给窗口“换外壳”。
"""
from PySide6.QtCore import (Qt, QObject, QEvent, QRect, QRectF, QSize,
                            QPointF)
from PySide6.QtGui import (QColor, QPainter, QPen, QCursor, QPainterPath)
from PySide6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout, QSizeGrip)

# 透明带：窗口边缘往里这么多像素不画内容，留给阴影（也是边缘把手的拖拽热区）。
# 三边收窄、底部留宽：上/左/右贴边显得利落现代；阴影的视觉分量本来就在下方，
# 悬浮感全靠底部这条带，不能省
MARGIN_L = MARGIN_R = 6
MARGIN_T = 2            # 顶部极限收窄（48→40→34→32）。不能再减：alpha=0 的像素
                        # 会穿透鼠标事件，顶部边缘把手热区＝MARGIN_T+2，归零就拖不了
MARGIN_B = 10
# 卡片边缘再往里缩这么多像素放内容：保证内容的直角不探出圆角弧
# （条件：PAD ≥ RADIUS×(1−1/√2)≈0.29R，R=12 时 3.5 即可，取 4）
PAD = 4
# 窗口边缘到内容区的总缩进（分侧）
BAND_L = MARGIN_L + PAD
BAND_R = MARGIN_R + PAD
BAND_T = MARGIN_T + PAD
BAND_B = MARGIN_B + PAD
RADIUS = 12                     # 卡片圆角半径
TITLEBAR_H = 30                 # 自绘标题栏高度（与 TitleBar.setFixedHeight 对齐）；
                                # 顶部总高＝MARGIN_T+此值＝32px，与 Win11 原生标题栏齐平
_CARD_BG = "#F2F3F5"            # 卡片底色：与 theme.py 的窗口背景色一致
_CARD_BORDER = "#D9DDE3"        # 卡片描边：白底上也勾得出轮廓（代替原来的 mask 硬切边）
# 各方向 -> (光标, (lx, wx, ty, hy))：新尺寸由 left+=lx*d、width+=wx*d、
# top+=ty*dy、height+=hy*dy 得出（拖左/上边时位移同时作用于位置与宽高）
_EDGES = {
    "l": (Qt.CursorShape.SizeHorCursor, (1, -1, 0, 0)),
    "r": (Qt.CursorShape.SizeHorCursor, (0, 1, 0, 0)),
    "t": (Qt.CursorShape.SizeVerCursor, (0, 0, 1, -1)),
    "b": (Qt.CursorShape.SizeVerCursor, (0, 0, 0, 1)),
    "tl": (Qt.CursorShape.SizeFDiagCursor, (1, -1, 1, -1)),
    "tr": (Qt.CursorShape.SizeBDiagCursor, (0, 1, 1, -1)),
    "bl": (Qt.CursorShape.SizeBDiagCursor, (1, -1, 0, 1)),
    "br": (Qt.CursorShape.SizeFDiagCursor, (0, 1, 0, 1)),
}


class _RoundedBg(QWidget):
    """最底层背景卡片：抗锯齿画圆角矩形 + 透明带内的软阴影。

    为什么这样不锯齿：逐像素 alpha 由合成器（DWM）混色，边缘是渐变而不是
    1bit 掩码的硬台阶。阴影不只是好看：透明带里像素 alpha>0，Windows 才会把
    鼠标事件交给窗口，边缘把手才抓得住（alpha=0 的像素会直接穿透到桌面）。

    card_bg / border 可自定义：不传就用两个常量，所以其它窗口一行代码不用改
    也与从前逐像素相同。唤出面板拿它画“深灰 + 半透明”（QColor 带 alpha 就能
    透，窗口本身已经是 WA_TranslucentBackground，逐像素 alpha 由 DWM 合成）。"""

    def __init__(self, window, radius=RADIUS, header_h=0,
                 card_bg=None, border=None):
        super().__init__(window)
        self._r = radius
        self._hh = header_h                   # 顶部白色标题带高（0＝无标题栏）
        self._bg = QColor(card_bg) if card_bg is not None else QColor(_CARD_BG)
        self._border = QColor(border) if border is not None else QColor(_CARD_BORDER)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setGeometry(window.rect())
        self.lower()                                # 压在兄弟控件最底下当背景

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        card = QRectF(self.rect()).adjusted(MARGIN_L, MARGIN_T,
                                            -MARGIN_R, -MARGIN_B)
        # 阴影：从卡片边缘向外一圈圈变淡变高（越靠窗口边越淡，但始终 >0）；
        # 各方向最多铺满自家透明带，顶部不衰减（保证把手最外圈像素可命中）
        for i in range(MARGIN_B, 0, -1):
            a = 6 + int(10 * (1 - i / MARGIN_B))
            p.setBrush(QColor(31, 35, 41, a))
            f = i / MARGIN_B
            p.drawRoundedRect(card.adjusted(-min(i, MARGIN_L), -min(i, MARGIN_T),
                                            min(i, MARGIN_R),
                                            min(i, MARGIN_B) * 0.75),
                              self._r + i * f, self._r + i * f)
        # 卡片本体 + 1px 描边（白底上也勾得出轮廓，代替原来的 mask 硬切边）
        # 颜色取自构造参数：带 alpha 时卡片以外的透底依旧透明，桌面从底下露出来
        p.setBrush(self._bg)
        p.setPen(QPen(self._border, 1))
        p.drawRoundedRect(card, self._r, self._r)
        if self._hh:
            # 顶部白色标题带：裁进卡片轮廓再铺白矩形——带顶两角天然是窗口
            # 同款圆弧，铺满卡片整宽，标题栏不再是“灰底上嵌一块白直角板”
            # （那种双层感就是“像 WinXP”的根源）；TitleBar 控件本体已改透明
            path = QPainterPath()
            path.addRoundedRect(card, self._r, self._r)
            p.setClipPath(path)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#FFFFFF"))
            p.drawRect(QRectF(card.left(), card.top(), card.width(), self._hh))
            p.setPen(QPen(QColor("#E5E7EB"), 1))     # 带底分隔线
            y = card.top() + self._hh
            p.drawLine(QPointF(card.left(), y), QPointF(card.right(), y))
            p.setClipping(False)
        p.end()


class TitleBar(QWidget):
    """自绘标题栏：整条可拖动移动窗口，双击切最大化，右侧最小/最大/关闭。

    本体不铺背景：白色标题带与带底分隔线由 _RoundedBg 裁着窗口圆弧统一画，
    这里只摆文字和按钮（所以 QSS 里 #AppTitleBar 是透明背景）。"""

    def __init__(self, window, title="", show_min=True, show_max=True):
        super().__init__(window)
        self._win = window
        self._drag = None                                 # 拖动起点(窗口局部)相对光标
        self.setFixedHeight(TITLEBAR_H)
        self.setObjectName("AppTitleBar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 8, 0)
        lay.setSpacing(6)
        lbl = QLabel(title or window.windowTitle())
        lbl.setObjectName("AppTitleText")
        lay.addWidget(lbl, 1)                             # 左侧标题占满剩余宽度＝可拖区
        self.btn_min = self._btn("—", "最小化")
        self.btn_max = self._btn("□", "最大化 / 还原")
        self.btn_close = self._btn("✕", "关闭")
        self.btn_close.setObjectName("AppTitleClose")
        if show_min:
            lay.addWidget(self.btn_min)
        if show_max:
            lay.addWidget(self.btn_max)
        lay.addWidget(self.btn_close)
        self.btn_min.clicked.connect(self._minimize)
        self.btn_max.clicked.connect(self.toggle_max)
        self.btn_close.clicked.connect(self._close)
        self.show_max_btn(show_max)

    def _btn(self, text, tip):
        b = QPushButton(text)
        b.setObjectName("AppTitleBtn")
        b.setToolTip(tip)
        b.setFixedSize(32, 24)   # 悬停底色块跟着标题带高度收一档，否则显高
        b.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        return b

    def show_max_btn(self, on):
        self.btn_max.setVisible(bool(on))

    def _minimize(self):
        """最小化：主窗口可以把这一动作改道收进托盘（设置里的开关）。

        鸭子类型问一句窗口自己要不要，而不是在这里 import gui.tray：
        工具小窗、对话框没有这个钩子，照常最小化；标题栏也就不依赖业务层。"""
        win = self._win
        hook = getattr(win, "try_minimize_to_tray", None)
        if callable(hook) and hook():
            return
        win.showMinimized()

    def _close(self):
        win = self._win
        # 对话框走 reject（取消语义）；主窗口等没 exec 的直接 close
        if callable(getattr(win, "exec", None)) and callable(getattr(win, "reject", None)):
            win.reject()
        else:
            win.close()

    def toggle_max(self):
        win = self._win
        norm = win.property("_norm_geo")
        if isinstance(norm, QRect) and not norm.isNull():
            win.setGeometry(norm)                         # 还原
            win.setProperty("_norm_geo", None)
            self.btn_max.setText("□")
        else:
            win.setProperty("_norm_geo", win.geometry())
            scr = win.screen() or _primary_screen()
            win.setGeometry(scr.availableGeometry())      # 贴满工作区（不盖任务栏）
            self.btn_max.setText("❐")

    # ------- 拖动移动 -------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self._win.move(e.globalPosition().toPoint() - self._drag)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        self.toggle_max()


class _Edge(QWidget):
    """一条透明边缘把手：悬停出缩放光标，按住拖动改窗口尺寸。"""

    def __init__(self, window, key):
        super().__init__(window)
        self._win = window
        self._key = key
        self._start = None                               # (原始 geometry, 原始全局点)
        cur, _ = _EDGES[key]
        self.setCursor(QCursor(cur))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.show()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._start = (self._win.geometry(), e.globalPosition().toPoint())
            e.accept()

    def mouseReleaseEvent(self, e):
        self._start = None

    def mouseMoveEvent(self, e):
        if not self._start or not (e.buttons() & Qt.MouseButton.LeftButton):
            return
        geo, press = self._start
        dx = e.globalPosition().toPoint().x() - press.x()
        dy = e.globalPosition().toPoint().y() - press.y()
        win = self._win
        min_w = win.minimumWidth() or 200
        min_h = win.minimumHeight() or 140
        lx, wx, ty, hy = _EDGES[self._key][1]
        left = geo.left() + lx * dx
        width = geo.width() + wx * dx
        top = geo.top() + ty * dy
        height = geo.height() + hy * dy
        if width < min_w:
            if lx:                    # 拖的是左/右边：钉住对侧，不让它压穿
                left = geo.right() - min_w + 1
            width = min_w
        if height < min_h:
            if ty:
                top = geo.bottom() - min_h + 1
            height = min_h
        win.setGeometry(left, top, width, height)
        e.accept()


class _FrameFilter(QObject):
    """装在顶层窗口上：Show/Resize 时同步背景卡片尺寸，并摆好 8 条边缘把手。"""

    def __init__(self, window, edges, bg):
        super().__init__(window)
        self._edges = edges                               # {key: _Edge}
        self._bg = bg
        window.installEventFilter(self)

    def _layout(self, win):
        w, h = win.width(), win.height()
        # 把手厚度：盖满各侧透明带、再探入卡片边缘 2px（摸得到）
        gl, gt = MARGIN_L + 2, MARGIN_T + 2
        gr, gb = MARGIN_R + 2, MARGIN_B + 2
        for key, ed in self._edges.items():
            if key == "l":
                ed.setGeometry(0, gt, gl, h - gt - gb)
            elif key == "r":
                ed.setGeometry(w - gr, gt, gr, h - gt - gb)
            elif key == "t":
                ed.setGeometry(gl, 0, w - gl - gr, gt)
            elif key == "b":
                ed.setGeometry(gl, h - gb, w - gl - gr, gb)
            elif key == "tl":
                ed.setGeometry(0, 0, gl, gt)
            elif key == "tr":
                ed.setGeometry(w - gr, 0, gr, gt)
            elif key == "bl":
                ed.setGeometry(0, h - gb, gl, gb)
            elif key == "br":
                ed.setGeometry(w - gr, h - gb, gr, gb)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.Show, QEvent.Resize):
            if self._bg is not None:
                self._bg.setGeometry(obj.rect())
                self._bg.update()
            if self._edges:
                self._layout(obj)
        return False


def _primary_screen():
    from PySide6.QtWidgets import QApplication
    return QApplication.primaryScreen()


def _first_row_titlebar(window):
    """窗口内容第一行是不是 TitleBar：主窗口这种“调用方自备标题栏”的
    也要能识别出来，才能把白色头部带画对齐。"""
    lay = window.layout()
    if isinstance(window, QMainWindow):
        cw = window.centralWidget()
        lay = cw.layout() if cw is not None else None
    if lay is None or lay.count() == 0:
        return False
    it = lay.itemAt(0)
    return isinstance(it.widget() if it is not None else None, TitleBar)


def apply_rounded(window, title="", radius=RADIUS, resizable=True,
                  show_min=True, show_max=True, add_titlebar=True,
                  corner_grip=False, card_bg=None, border=None):
    """把任意顶层窗口装配成无边框圆角窗（逐像素透明，无锯齿）。

    window      ：QMainWindow / QDialog / 顶层 QWidget。
    add_titlebar：True 时自动把 TitleBar 插进窗口顶层 QVBoxLayout 第 0 行；
                  主窗口这种特殊布局由调用方自己插好标题栏后传 False。
    corner_grip ：为 True 时右下角额外放一个原生 QSizeGrip（与边缘把手并存，
                  给“拉大放小”一个明显的斜角抓手）。
    card_bg     ：卡片底色（可带 alpha，需要半透明就传带 alpha 的 QColor）；
                  不传＝用默认浅底，与其它窗口逐像素相同。
    border      ：卡片 1px 描边色，口径同上。
    返回创建的 TitleBar（未加标题栏时返回 None）。

    尺寸口径：内容区不因为换外壳被挤压——上/左/右缩进 BAND_T/L/R、底部
    缩进 BAND_B（宽一点托阴影），固定尺寸弹窗自动把这部分连同标题栏高度
    一起补出去（_chrome_h 记录补了多少，TaskDialog 撑开翻译条那类动态
    改高的地方直接叠用）。
    """
    # ① 无边框 + 逐像素透明
    flags = window.windowFlags()
    if Qt.WindowType.FramelessWindowHint not in flags:
        window.setWindowFlags(flags | Qt.WindowType.FramelessWindowHint)
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    # 窗口自身背景交给 theme.py 的 `QWidget[roundedTop="1"]{background:transparent}`
    # 规则钉掉：否则 QSS 的 QMainWindow/QDialog 背景会把方角矩形重新画满
    window.setProperty("roundedTop", "1")

    # 先判断是否固定尺寸（min==max）：决定要不要边缘把手、要不要给标题栏腾高
    is_fixed = (window.minimumSize() == window.maximumSize()
                and window.minimumSize().isValid() and not window.minimumSize().isEmpty())

    titlebar = None
    window._chrome_h = 0
    if isinstance(window, QMainWindow):
        # 主窗口：窗口级边距同时内缩中心区与状态栏（实测 QMainWindowLayout 吃这套）；
        # 带标题栏时顶部只用 MARGIN_T——标题带贴卡片顶缘画，圆弧由裁剪接手，
        # 不再留 PAD 灰间隙（“上边框太高”的第二刀）
        top = MARGIN_T if _first_row_titlebar(window) else BAND_T
        window.setContentsMargins(BAND_L, top, BAND_R, BAND_B)
    else:
        lay = window.layout()
        if isinstance(lay, QVBoxLayout):
            m = lay.contentsMargins()
            nl, nr = max(m.left(), BAND_L), max(m.right(), BAND_R)
            nb = max(m.bottom(), BAND_B)
            nt = MARGIN_T if add_titlebar else max(m.top(), BAND_T)
            dw = (nl - m.left()) + (nr - m.right())
            dh = (nt - m.top()) + (nb - m.bottom())
            lay.setContentsMargins(nl, nt, nr, nb)
            if add_titlebar:
                titlebar = TitleBar(window, title, show_min, show_max)
                lay.insertWidget(0, titlebar)
                dh += TITLEBAR_H - PAD            # 标题栏占位要顶掉原顶边距里的 PAD 段（已不含）
                window._chrome_h = dh
            if is_fixed:
                # 固定尺寸弹窗：把标题栏 + 透明带的高宽补出去，别挤压内容
                window.setFixedSize(window.width() + dw, window.height() + dh)
            elif dw or dh:
                window.resize(window.width() + dw, window.height() + dh)

    # ② 背景卡片（在所有内容之下，跟着窗口尺寸走）：带标题栏时顺手把
    #    顶部白色标题带画出来——标题栏与卡片顶缘齐平，带高就是栏高
    header_h = 0
    if titlebar is not None or _first_row_titlebar(window):
        header_h = TITLEBAR_H
    bg = _RoundedBg(window, radius, header_h, card_bg, border)

    # ③ 边缘把手（仅非固定尺寸且允许缩放时）
    edges = {}
    if resizable and not is_fixed:
        for key in _EDGES:
            edges[key] = _Edge(window, key)
        window.setMouseTracking(True)

    if corner_grip and resizable and not is_fixed:
        grip = QSizeGrip(window)
        grip.lower()

    _FrameFilter(window, edges, bg)
    return titlebar
