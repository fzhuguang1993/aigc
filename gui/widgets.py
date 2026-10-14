"""
gui/widgets.py —— 无边框视频播放器 + 图片预览 + 提示词悬停预览浮层 + Toast
"""
import html
import re
from pathlib import Path

from PySide6.QtCore import (Qt, QEvent, QUrl, QPoint, QSize, QRect, QRectF, QTimer,
                            Signal, QKeyCombination)
from PySide6.QtGui import (QGuiApplication, QPixmap, QPainter, QPen, QColor, QCursor,
                           QKeySequence)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QScrollArea, QSlider,
                               QPushButton, QLabel, QWidget, QMessageBox,
                               QComboBox, QSizeGrip, QSizePolicy, QLineEdit,
                               QInputDialog, QLayout, QApplication,
                               QGraphicsView, QGraphicsScene, QFrame, QSpacerItem)

from core import translate
from gui.maintainer import Gate, app_has_unlocked
from gui.tool_panels import API_MAINTAINER_CODE, MAINTAINER_SHORTCUT, ToolWorker
from store import app_state
from utils.desktop_utils import open_path, reveal_in_folder
from gui.theme import tokenize
from gui.menus import StyledMenu
from gui.ui_kit import COLORS, RADIUS as TOK_RADIUS, draw_rounded_card


class FlowLayout(QLayout):
    """流式布局：控件从左往右排，一行放不下自动换行（胶囊标签组用）。

    只放 widget 项；支持 heightForWidth，父布局能问到正确高度，
    筛选面板展开动画靠这个算 sizeHint。"""

    def __init__(self, parent=None, hgap=6, vgap=6):
        super().__init__(parent)
        self._items = []
        self.hgap, self.vgap = hgap, vgap

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        m = self.contentsMargins()
        rect = QRect(m.left(), m.top(),
                     max(width - m.left() - m.right(), 0), 0)
        return self._do(rect, test=True) + m.top() + m.bottom()

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do(self.contentsRect(), test=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            w = it.widget()
            if w is not None:
                s = s.expandedTo(w.minimumSizeHint())
        m = self.contentsMargins()
        return s.boundedTo(QSize(1 << 20, 1 << 20)).expandedTo(
            QSize(s.width() + m.left() + m.right(),
                  s.height() + m.top() + m.bottom()))

    def _do(self, rect, test):
        """排一遍所有项；test=True 只算高度不动控件。返回内容底部相对 rect.top 的高度"""
        x, y, row_h = rect.x(), rect.y(), 0
        for it in self._items:
            w = it.widget()
            if w is None:
                continue
            sz = self._cell_size(w)
            if row_h and x + sz.width() > rect.right() + 1:
                x, y = rect.x(), y + row_h + self.vgap
                row_h = 0
            if not test:
                w.setGeometry(x, y, sz.width(), sz.height())
            x += sz.width() + self.hgap
            row_h = max(row_h, sz.height())
        return y + row_h - rect.y()

    @staticmethod
    def _cell_size(w):
        """子项占位尺寸：把 sizeHint 夹在 [minimumSize, maximumSize] 之间。

        为什么不能直接用 sizeHint：`setFixedSize` 把 min/max 钉成同一个值，但
        sizeHint 仍返回内容自然尺寸（比 fixed 小）。旧代码拿 sizeHint 排格子，
        固定尺寸的卡片就会彼此重叠（唤出面板网格卡的选中外框“重合”就是这么来的）。
        夹一段后：固定尺寸卡片按真实外框排位，普通胶囊仍按 sizeHint（min=0/max=∞）。"""
        sz = w.sizeHint()
        return sz.boundedTo(w.maximumSize()).expandedTo(w.minimumSize())


def _fmt_ms(ms):
    s = int(ms / 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


def _fmt_rate(r):
    """0.5 → 0.5；1.0 → 1（倍速列表里不想看到“1.0x”这种写法）"""
    return f"{float(r):g}"


# 倍速预设：审片时 1.5x/2x 快速过片、逐帧对口型时 0.5x
RATES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)

# 快退/快进步长：一条成品就 5~15 秒，写死 5 秒既不好对口型也不好跳读，
# 所以改成「1~5 秒」下拉 + 加减号，选过的步长记进 ui_state（下次还在）
STEP_SECONDS = (1, 2, 3, 4, 5)
STEP_DEFAULT = 5                        # 没记过时的默认（与旧版写死的 5 秒一致）
STEP_KEY = "player_step_sec"

# 播放器配色。窗口本体是 #PlayerRoot，后代选择器全部挂在它下面：两条控制条上的
# 控件（按钮/下拉/滑块/标签）都直接受用，不必再借一层壳子的 id 提优先级——
# 应用级样式表里已经有 QPushButton{...}，不拿 id 压一层的话按钮会被全局浅色样式盖掉。
# 顶部窗控条 #TopBar 仍是实体深底；底部播放条换成浮层控件（_ControlBar），
# 底色由它自己的 paintEvent 逐像素画 alpha，所以这里不再给它写 background。
# 窗口本体 #PlayerRoot 改纯黑：画面四周不再有留白/描边/圆角，视频自身的黑边与
# 底色同色，看不出“一圈边框”。
# 顶层窗仍然不设 WA_TranslucentBackground：透明窗口叠硬解视频在 Windows 上会把
# 画面区域变成一块黑。半透明只做在子控件这一层（画面走 QGraphicsVideoItem 的
# QPainter 合成，浮层的逐像素 alpha 才真能透出视频）。
_SHELL_QSS = """
#PlayerRoot { background:#000000; }
#TopBar { background:#15171C; border:0; }
#PlayerRoot QLabel { color:#C9CFDA; background:transparent; font-size:12px; }
#PlayerRoot QLabel#FileName { color:#F2F5FA; font-size:12px; font-weight:600; }
#PlayerRoot QLabel#StatusTip { color:#8F959E; font-size:11px; }
#PlayerRoot QPushButton, #PlayerRoot QToolButton {
    background:#252A33; color:#DCE1EA; border:0; border-radius:6px; padding:4px 9px; }
#PlayerRoot QPushButton:hover, #PlayerRoot QToolButton:hover { background:#323A47; }
#PlayerRoot QPushButton:checked { background:#3370FF; color:#FFFFFF; }
#PlayerRoot QPushButton#MarkOk:checked { background:#12A150; color:#FFFFFF; }
#PlayerRoot QPushButton#MarkBad:checked { background:#D94A43; color:#FFFFFF; }
#PlayerRoot QPushButton#CloseBtn:hover { background:#D94A43; color:#FFFFFF; }
#PlayerRoot QComboBox { background:#252A33; color:#DCE1EA; border:0;
    border-radius:6px; padding:3px 6px; }
#PlayerRoot QComboBox QAbstractItemView { background:#252A33; color:#DCE1EA;
    selection-background-color:#3370FF; selection-color:#FFFFFF;
    border:1px solid #3A4150; outline:none; }
/* 弹层列表的每个条目：默认深底浅字，鼠标划过/选中给蓝底白字。
   必须显式写 ::item，否则会被应用级浅色主题里的
   `QComboBox QAbstractItemView::item:hover/selected {background:#EAF1FF}` 顶掉——
   表现就是深黑列表上冒出一个几乎白色的选中块，看不清也反人类。 */
#PlayerRoot QComboBox QAbstractItemView::item { color:#DCE1EA; background:#252A33;
    padding:4px 8px; min-height:22px; }
#PlayerRoot QComboBox QAbstractItemView::item:hover,
#PlayerRoot QComboBox QAbstractItemView::item:selected {
    background:#3370FF; color:#FFFFFF; }
#PlayerRoot QSlider::groove:horizontal { height:6px; background:#31363F; border-radius:3px; }
#PlayerRoot QSlider::sub-page:horizontal { background:#3370FF; border-radius:3px; }
#PlayerRoot QSlider::handle:horizontal { width:16px; height:16px; margin:-5px 0;
    border-radius:8px; background:#EAF0FA; }
#PlayerRoot QSlider::handle:horizontal:pressed { background:#FFFFFF; }
#PlayerRoot QSizeGrip { background:transparent; }
"""


class _SeekSlider(QSlider):
    """进度条：除了拖着走，点哪儿就跳到哪儿

    默认 QSlider 点凹槽只是按 singleStep 挪一格（毫秒级的时长等于没反应），
    手柄又只有几像素宽，使用者就判定了「进度条拖不动」。
    这里自己把鼠标位置换算成进度，按下/拖动/抬起都直接定位。"""

    seeked = Signal(int)

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setFixedHeight(20)             # 抬高命中区，好点中手柄
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

    def _value_at(self, p):
        half = 8                            # 手柄半径（与上面 QSS 的 16px 对应）
        span = max(self.width() - 2 * half, 1)
        r = min(max((p - half) / span, 0.0), 1.0)
        return int(self.minimum() + r * (self.maximum() - self.minimum()))

    def _jump(self, p):
        if self.maximum() <= self.minimum():
            return                          # 时长还没回来：不拦着报错就行
        v = self._value_at(int(p))
        self.setValue(v)                    # setValue 不发 sliderMoved，自己补一个
        self.seeked.emit(v)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self._jump(int(e.position().x()))
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self.isSliderDown():
            self._jump(int(e.position().x()))
            e.accept()
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self.isSliderDown():
            self._jump(int(e.position().x()))
            self.setSliderDown(False)
            e.accept()
        else:
            super().mouseReleaseEvent(e)


class _ControlBar(QWidget):
    """播放条：非全屏是贴在窗口底的一条实体栏，全屏时换成压在画面上的浮层。

    底色走 paintEvent 逐像素画 alpha，而不是靠 QSS 的 rgba(...)：子控件的 QSS
    背景不参与合成，透明度画不出来（同 _FloatBar 那条已经验证过的路）。
    浮层能真半透明还有个前提：画面是 QGraphicsVideoItem（走 QPainter 合成进
    窗口缓冲），而不是 QVideoWidget 那块原生视频表面——同层的普通子控件画不到
    它上面去。顶层窗本身仍然不设 WA_TranslucentBackground（那会把硬解画面压黑）。

    它不进布局，位置由 VideoPlayerDialog._relocate 手动摆（非全屏贴下沿通栏，
    全屏按设置的下方/左侧/右侧摆在画面上，三档用的是同一套横排面板）。
    """

    RADIUS = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PlayerShell")     # 控件的配色靠 #PlayerRoot 后代选择器拿
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._alpha = 255
        self._rounded = False
        self._bg = QColor(21, 23, 28)         # 与 #TopBar 同色，两套样子看着是一家的

    def alpha(self):
        return self._alpha

    def set_style(self, alpha, rounded):
        """alpha：不透明度（0~255）；rounded：浮层给圆角、实体栏通栏方角"""
        alpha = max(0, min(255, int(alpha)))
        if alpha == self._alpha and rounded == self._rounded:
            return
        self._alpha, self._rounded = alpha, bool(rounded)
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, self._rounded)
        p.setPen(Qt.PenStyle.NoPen)
        self._bg.setAlpha(self._alpha)
        p.setBrush(self._bg)
        if self._rounded:
            r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            p.drawRoundedRect(r, self.RADIUS, self.RADIUS)
        else:
            p.drawRect(self.rect())
        p.end()


class _VideoView(QGraphicsView):
    """画面画布：无边框播放器里鼠标落在画面上的事都在这儿接。

    事件写在 view 而不是对话框上，是因为对话框本体被三行布局占满了，画面以外的
    地方点不到；而全屏时需要靠 MouseMove 把收起的浮层唤回来，只能挂在画面上。
    具体的点/拖/双击语义仍在对话框那边（_video_pressed 等），这里只负责递过来。
    """

    def __init__(self, scene, dlg, parent=None):
        super().__init__(scene, parent)
        self._dlg = dlg
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor("#000000"))    # 黑边：与 #PlayerRoot 同色
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMouseTracking(True)                   # 全屏时要知道鼠标到哪儿了

    def mousePressEvent(self, e):
        self._dlg._video_pressed(e)

    def mouseReleaseEvent(self, e):
        self._dlg._video_released(e)

    def mouseDoubleClickEvent(self, e):
        self._dlg._video_double(e)

    def mouseMoveEvent(self, e):
        self._dlg._video_move(e)

    def contextMenuEvent(self, e):
        self._dlg._show_bar_menu(e)

    def wheelEvent(self, e):
        # 画面上的滚轮拿去调音量（与 ↑/↓ 同一件事），不能让 QGraphicsView
        # 默认行为把场景滚走/缩放——那会让视频在窗里跑位
        self._dlg._video_wheel(e)


class VideoPlayerDialog(QDialog):
    """无边框播放器：播放/暂停、进度（可拖可点）、步长快进退、倍速、全屏、
    横竖屏自适应、审片标记

    无边框就得自己把窗口的事管完：顶部一条可拖动的标题栏（全屏/最小化/关闭），
    右下角 QSizeGrip 拉大，Esc 关、双击画面全屏、单击画面播放/暂停；
    识别字幕 / 定位源文件收到画面右键菜单里（不占顶栏）。

    画面就是窗口本体：四周不留留白、不描边、不剪圆角。按 ⛶ 全屏时顶部窗控条
    收掉，画面铺满整块屏（含任务栏那一条），播放条换成压在画面上的半透明浮层：
    默认 alpha 150（能透出视频），鼠标移到栏上变不透明，鼠标只要动一下栏就回来、
    真静止满 2.5 秒才收起；浮层的位置没有选项——就是屏幕底部居中那一条，
    进度条恒在它最下面一行横着（贴左右边的版本试过，进度条落在屏幕中部，
    不符合使用习惯，按用户要求收回底部）。
    顶栏收起后窗控改由浮层末尾的 ―/✕ 承担，其余快捷键不变。

    allow_mark：只有【成品视频】才给标记按钮。素材库里预览的是同事传上来的
    原材，误点一下就把素材改名了，所以默认不给。

    marked(new_path, mark)：标记完成（含改名）后发出来，父页面据此刷新列表。
    """

    marked = Signal(str, str)

    MIN_W, MIN_H = 360, 220             # 还没报出视频尺寸时的兜底（尺寸到了改成手机量级）
    # 自适应时画面可占屏幕可用区的比例。先在原 0.88 / 0.94 基础上整体缩小约 30%，
    # 又按用户反馈（整窗看着小了一圈、播放条里的按钮被挤）上调约 5%；
    # 设置页「视频预览框大小」的百分比再在这个基准上乘系数。
    FILL_W, FILL_H = 0.65, 0.69
    FALLBACK_FILL = 0.37                # 后端还没报视频尺寸时的半屏兜底，跟着上面一起调过
    REFIT_EPS = 0.015                   # 宽高比变化超过这个值才重摆（不跟用户抢尺寸）
    # 预览框大小缩放（百分比）：存 ui_state，播放器每次打开时读取即时生效。
    SCALE_KEY = "player_preview_scale"
    SCALE_MIN, SCALE_MAX, SCALE_DEFAULT = 40, 160, 100
    # 播放区域最小尺寸：按 iPhone 17 的屏幕给（6.3"，物理 1206×2622，
    # 逻辑点 402×874），方向跟着视频转：竖版画面至少 402×874，横版至少 874×402。
    # 拖窗、横竖屏自适应都拆不开这一对（小过一个手机屏幕就没法审片）。
    PHONE_SHORT, PHONE_LONG = 402, 874
    # 全屏浮层：栏最小高 / 鼠标静止多久收起 / 半透明与不透明的 alpha
    # / 浮层与屏幕边之间的空隙。栏宽不写死：横排面板要多少宽听布局的。
    BAR_H = 56
    IDLE_MS = 2500
    ALPHA_IDLE, ALPHA_HOT = 150, 255
    GAP = 16                              # 浮层与屏幕边之间的空隙

    def __init__(self, parent, file_path, allow_mark=False):
        super().__init__(parent)
        self._path = str(file_path or "")
        self._allow_mark = bool(allow_mark)
        self._drag_pos = None            # 无边框拖动：按下时的坐标偏移
        self._fitted = None              # 已按哪个宽高比摆过（None=还没摆）
        self._swapping = False           # 换文件途中屏蔽报错
        self._fullscreen = False
        self._last_size = None           # 视频真实宽高（退出全屏时重摆一次）
        self._press = None               # 画面上的按下位置：判“是点还是拖”用（见 release）
        self._click1_toggled = False     # 单击画面拨过一下播放态：双击时要拨回去
        self._deck_h = self.BAR_H        # 非全屏时播放条那一行占多高（spacer 跟着它走）
        self._bar_timer = QTimer(self)   # 鼠标静止计时：到点收起浮层（仅全屏启用）
        self._bar_hidden = False         # 浮层是否已收起
        self._bar_hold = False           #  true=不许收（报错时置上，等下一次鼠标静止）

        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("PlayerRoot")        # 供 #PlayerRoot 规则铺黑底
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)   # 让 #PlayerRoot 背景真能绘出来
        self.setStyleSheet(_SHELL_QSS)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # 三行紧贴：顶部窗控条 / 画面 / 播放条占位。播放条本体不进布局（见 _deck），
        # 只用一条 spacer 把那一行的高度占住，这样同一个控件在“贴下沿的实体条”
        # 与“压在画面上的浮层”之间切换时不必重排任何子控件。
        out = QVBoxLayout(self)
        out.setContentsMargins(0, 0, 0, 0)
        out.setSpacing(0)

        # ---------- 顶部：拖动区 + 窗口按钮（全屏时收掉）----------
        topbar = QWidget(objectName="TopBar")
        self._topbar = topbar
        out.addWidget(topbar)
        bar = QHBoxLayout(topbar)
        bar.setContentsMargins(10, 4, 8, 4)
        bar.setSpacing(6)
        self.lbl_name = QLabel(objectName="FileName")
        # 文件名动辄二三十字，不降到「可压缩」的话它会拿自己的最小宽度
        # 顶住整个窗口，竖屏成品就被带成两侧一大块黑边（完整名在 tooltip 里）
        self.lbl_name.setSizePolicy(QSizePolicy.Policy.Ignored,
                                    QSizePolicy.Policy.Preferred)
        bar.addWidget(self.lbl_name, 1)
        self.btn_full = QPushButton("⛶ 全屏")
        self.btn_full.setCheckable(True)
        self.btn_full.setToolTip("全屏 / 退出全屏（F 或双击画面）")
        self.btn_full.toggled.connect(self._set_fullscreen)
        b_min = QPushButton("―")
        b_min.setToolTip("最小化")
        b_min.clicked.connect(self.showMinimized)
        b_close = QPushButton("✕")
        b_close.setObjectName("CloseBtn")
        b_close.setToolTip("关闭（Esc）")
        b_close.clicked.connect(self.close)
        # 识别字幕 / 定位源文件不再占顶栏一颗按钮（显得拥挤），改收进画面右键菜单（见
        # contextMenuEvent）：需要时右键画面即取，标题栏只留全屏/最小化/关闭。
        for b in (self.btn_full, b_min, b_close):
            bar.addWidget(b)

        # ---------- 画面：顶到窗口的左、右、上三边 ----------
        # 用 QGraphicsView + QGraphicsVideoItem 而不是 QVideoWidget：后者是一块
        # 原生视频表面，同层的普通子控件画不到它上面去，全屏的半透明浮层就压不上去；
        # 前者把帧走 QPainter 合成进窗口缓冲，逐像素 alpha 能真透出视频。
        self.scene = QGraphicsScene(self)
        self.item = QGraphicsVideoItem()
        self.item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.scene.addItem(self.item)
        self.view = _VideoView(self.scene, self)
        self.view.setMinimumSize(self.MIN_W, self.MIN_H)
        out.addWidget(self.view, 1)

        # ---------- 底部：播放条（浮层控件，手动摆位，只把 spacer 放进布局）----------
        deck = _ControlBar(self)
        self._deck = deck
        deck.setMouseTracking(True)          # 浮层要认“鼠标在不在栏上”
        deck.installEventFilter(self)        # Enter/Leave/Move 走 eventFilter 改 alpha
        lay = QVBoxLayout(deck)
        lay.setContentsMargins(10, 4, 8, 4)
        lay.setSpacing(4)
        self._deck_gap = QSpacerItem(0, self._deck_h,
                                     QSizePolicy.Policy.Ignored,
                                     QSizePolicy.Policy.Fixed)
        out.addItem(self._deck_gap)

        # ---------- 媒体（先建好，控制条要接它的信号） ----------
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        # 画面接到 item 自己的 sink 上（QMediaPlayer 那条 setVideoOutput(QGraphicsVideoItem)
        # 内部也是转手到这个 sink，直接给 sink 少一层依赖）
        self.player.setVideoSink(self.item.videoSink())

        # ---------- 控制条（横排两行：进度那一行摆最下面）----------
        # 为什么拆两行：竖屏成品要的是窄窗口，一行摆下 9 个控件的话
        # 布局算出来的最小宽度会把窗口顶宽，多出来的那截就成了画面两侧的黑边。
        # 全屏时换成浮层、非全屏时贴在窗口底，用的都是这一套横排面板：
        # 进度条永远横着摆在最下面一行（用户口径：换位置不能换使用习惯）。
        self.btn_play = QPushButton("▶")
        self.btn_play.setToolTip("播放 / 暂停（空格）")
        self.btn_play.setFixedWidth(40)
        self.btn_play.clicked.connect(self._toggle_play)
        b_back = QPushButton("－")        # 全角减号：比半角"-"好认也好点
        b_back.setObjectName("StepBack")
        b_back.setFixedWidth(34)
        b_back.setToolTip("按步长后退（←）")
        b_back.clicked.connect(lambda: self._step_seek(-1))
        self.cb_step = QComboBox()
        for s in STEP_SECONDS:
            self.cb_step.addItem(f"{s}秒", s)
        self._restore_step()
        self.cb_step.setToolTip("快退 / 快进的步长（1~5 秒）\n选过的下次开播放器还在")
        self.cb_step.currentIndexChanged.connect(self._step_changed)
        b_fwd = QPushButton("＋")
        b_fwd.setObjectName("StepFwd")
        b_fwd.setFixedWidth(34)
        b_fwd.setToolTip("按步长前进（→）")
        b_fwd.clicked.connect(lambda: self._step_seek(1))
        self.slider = _SeekSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setMinimumWidth(70)
        self.slider.sliderMoved.connect(self._seek_to)
        self.slider.seeked.connect(self._seek_to)
        self.lbl_time = QLabel("00:00 / 00:00")
        self.lbl_time.setToolTip("当前位置 / 总时长（进度条上点一下就能跳到那儿）")
        # 进度行的成员（这一行永远横着，且在栏里排最下面）
        self._row1_widgets = (self.btn_play, b_back, self.cb_step, b_fwd,
                               self.slider, self.lbl_time)
        self.btn_back, self.btn_fwd = b_back, b_fwd

        # ---------- 第二行：倍速 / 音量 / 循环 +（只给成品的）审片按钮 ----------
        self.cb_rate = QComboBox()
        self.cb_rate.addItems([f"{_fmt_rate(r)}x" for r in RATES])
        self.cb_rate.setCurrentIndex(list(RATES).index(1.0))
        self.cb_rate.setToolTip("播放倍速（, 减速 / . 加速 / 斜杠回到 1x）\n"
                                "审片用 2x 过片、看口型用 0.5x")
        self.cb_rate.currentIndexChanged.connect(self._rate_changed)
        self.btn_mute = QPushButton("🔊")
        self.btn_mute.setCheckable(True)
        self.btn_mute.setToolTip("静音 / 取消静音（M）")
        self.btn_mute.toggled.connect(self._set_muted)
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(100)
        self.vol.setFixedWidth(64)
        self.vol.setToolTip("音量（↑ / ↓）")
        self.vol.valueChanged.connect(self._set_volume)
        self.btn_loop = QPushButton("🔁")
        self.btn_loop.setCheckable(True)
        self.btn_loop.setToolTip("循环播放：同一条反复看（L）")
        self.lbl_mark = QLabel("审片：")
        self.btn_ok = QPushButton("👍 可用")
        self.btn_ok.setObjectName("MarkOk")
        self.btn_ok.setCheckable(True)
        self.btn_ok.setToolTip("标为可用：文件名上的「_不可用」记号会被去掉\n"
                               "（再点一次＝取消标记）")
        self.btn_bad = QPushButton("👎 不可用")
        self.btn_bad.setObjectName("MarkBad")
        self.btn_bad.setCheckable(True)
        self.btn_bad.setToolTip("标为不可用：文件名改成「…_不可用.mp4」，\n"
                                "回头用任务中心的「🧹 清理不可用」一次清掉（再点一次＝取消）")
        self.btn_ok.clicked.connect(lambda: self._on_mark("ok"))
        self.btn_bad.clicked.connect(lambda: self._on_mark("bad"))
        for w in (self.lbl_mark, self.btn_ok, self.btn_bad):
            w.setVisible(self._allow_mark)

        # 全屏时顶部窗控条是收起来的，―/✕ 搬到浮层末尾接着能最小化、关窗；
        # 非全屏顶栏就在，这两颗隐着（隐藏的控件不占布局位置）
        self.btn_min_fs = QPushButton("―")
        self.btn_min_fs.setToolTip("最小化")
        self.btn_min_fs.clicked.connect(self.showMinimized)
        self.btn_min_fs.setVisible(False)
        self.btn_close_fs = QPushButton("✕")
        self.btn_close_fs.setObjectName("CloseBtn")
        self.btn_close_fs.setToolTip("关闭（Esc）")
        self.btn_close_fs.clicked.connect(self.close)
        self.btn_close_fs.setVisible(False)

        self.lbl_tip = QLabel(objectName="StatusTip")
        self.lbl_tip.setVisible(False)
        self.lbl_tip.setWordWrap(True)

        # 上行：倍速 / 静音 / 音量 / 循环 /（只给成品的）审片 + 窗控；
        # 下行：播放 / 快退 / 步长 / 进度 / 时间。进度条必须在最下面一行。
        self._row_top = QHBoxLayout()
        self._row_top.setSpacing(6)
        for w in (self.cb_rate, self.btn_mute, self.vol, self.btn_loop,
                  self.lbl_mark, self.btn_ok, self.btn_bad):
            self._row_top.addWidget(w)
        self._row_top.addStretch(1)             # 窗控贴右，不被拉伸带跑
        self._row_top.addWidget(self.btn_min_fs)
        self._row_top.addWidget(self.btn_close_fs)
        self._row_seek = QHBoxLayout()
        self._row_seek.setSpacing(6)
        for w in self._row1_widgets:
            self._row_seek.addWidget(w)
        self._rows = QVBoxLayout()
        self._rows.setSpacing(4)
        self._rows.addLayout(self._row_top)
        self._rows.addLayout(self._row_seek)
        lay.addLayout(self._rows)
        lay.addWidget(self.lbl_tip)

        self._grip = QSizeGrip(deck)            # 无边框时手动挂一个拖动块（挂播放条上）

        self.player.playbackStateChanged.connect(self._on_state)
        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(self._on_duration)
        self.player.errorOccurred.connect(self._on_err)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.playbackRateChanged.connect(self._on_rate)
        # 真实宽高比靠视频尺寸变化重排窗口（不依赖容器写没写 Resolution）。
        # item 上是 nativeSizeChanged（QVideoWidget 那套是 sink 上的 videoSizeChanged）
        self.item.nativeSizeChanged.connect(self._on_video_size)

        self._bar_timer.setSingleShot(True)
        self._bar_timer.setInterval(self.IDLE_MS)
        self._bar_timer.timeout.connect(self._on_bar_idle)

        self._load(self._path, autoplay=True)
        self._sync_mark_buttons()

    # ---------------- 加载与换文件 ----------------

    def _load(self, path, autoplay=False):
        self._path = str(path)
        name = Path(self._path).name
        self.setWindowTitle(name)
        self.lbl_name.setText(name)
        self.lbl_name.setToolTip(self._path)
        if not Path(self._path).exists():
            self._say(f"文件不存在或已被移动：{self._path}", True)
            return
        self._swapping = True
        self.player.setSource(QUrl.fromLocalFile(self._path))
        self._swapping = False
        if autoplay:
            self.player.play()

    def _reload_after_rename(self, new_path, resume_ms, was_playing):
        """改名后重新指到新的路径，尽量接回原来的观看进度"""
        self._load(new_path, autoplay=False)
        if was_playing:
            self.player.play()
        if resume_ms:
            self.player.setPosition(resume_ms)

    # ---------------- 播放控制 ----------------

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _do_asr(self):
        """🎤 识别字幕：弹实时字幕浮窗，后台流式转写当前视频。"""
        p = self._path
        if not p or not Path(p).exists():
            self._say("当前文件不存在或已被移动，无法识别", True)
            return
        from gui.asr_live import show_asr_live
        show_asr_live(self, p)

    def _seek_rel(self, ms):
        self.player.setPosition(max(0, self.player.position() + int(ms)))

    def _step_sec(self):
        return int(self.cb_step.currentData() or STEP_DEFAULT)

    def _step_seek(self, direction):
        """±按钮与键盘 ←/→ 都走这里：步长由那个下拉说了算"""
        self._seek_rel(direction * self._step_sec() * 1000)

    def _restore_step(self):
        """步长接着上次的用（没记过才用默认）

        必须赶在接 currentIndexChanged 之前调，否则开一次窗口就写一次盘。"""
        want = app_state.get(STEP_KEY)
        for i in range(self.cb_step.count()):
            if self.cb_step.itemData(i) == want:
                self.cb_step.setCurrentIndex(i)
                return
        self.cb_step.setCurrentIndex(list(STEP_SECONDS).index(STEP_DEFAULT))

    def _step_changed(self, idx):
        sec = int(self.cb_step.itemData(idx) or STEP_DEFAULT)
        self.btn_back.setToolTip(f"后退 {sec} 秒（←）")
        self.btn_fwd.setToolTip(f"前进 {sec} 秒（→）")
        app_state.set_value(STEP_KEY, sec)

    def _seek_to(self, ms):
        """进度条定位：拖动、点哪儿跳哪儿都走这一条"""
        self.player.setPosition(max(0, int(ms)))
        self._show_pos(ms)          # 先本地回显，不等 positionChanged 绕一圈

    def _rate_changed(self, idx):
        try:
            self.player.setPlaybackRate(float(RATES[idx]))
        except (IndexError, ValueError):
            pass

    def _step_rate(self, delta):
        self.cb_rate.setCurrentIndex(min(max(self.cb_rate.currentIndex() + delta, 0),
                                         len(RATES) - 1))

    def _on_rate(self, rate):
        """把后端实际生效的倍速回写到下拉框

        解码器不支持时 setPlaybackRate 会静默失败，不回调回来就看不出来。"""
        for i, r in enumerate(RATES):
            if _fmt_rate(r) == _fmt_rate(rate):
                if self.cb_rate.currentIndex() != i:
                    self.cb_rate.setCurrentIndex(i)
                return

    def _set_volume(self, v):
        self.audio.setVolume(max(0.0, min(1.0, v / 100.0)))
        if v and self.btn_mute.isChecked():
            self.btn_mute.setChecked(False)

    def _set_muted(self, on):
        self.audio.setMuted(on)
        self.btn_mute.setText("🔇" if on else "🔊")

    def _on_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.btn_play.setText("❚❚" if playing else "▶")

    def _on_duration(self, ms):
        """时长回来才把进度条撑开

        范围还是 0~0 时滑块是拖不动的，而个别文件的时长比首帧晚到，
        不能只在创建时搭一次。"""
        self.slider.setRange(0, max(int(ms), 0))
        self._show_pos(self.player.position())

    def _on_pos(self, pos):
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        self._show_pos(pos)

    def _show_pos(self, ms):
        self.lbl_time.setText(f"{_fmt_ms(ms)} / {_fmt_ms(self.player.duration())}")

    def _on_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self.btn_loop.isChecked():
            self.player.setPosition(0)
            self.player.play()

    def _on_err(self, err, text):
        # 注意枚举名是 QMediaPlayer.Error（不是 ErrorType），写错会让这条
        # 槽在真正出错时抛 AttributeError，用户反而看不到任何失败提示
        if self._swapping or err == QMediaPlayer.Error.NoError:
            return
        self._say(f"无法播放：{text or '文件可能已删除或编码不支持'}", True)

    def _say(self, msg, warn=False):
        self.lbl_tip.setText(msg)
        self.lbl_tip.setStyleSheet(f"color:{'#FF8D19' if warn else '#8F959E'};")
        self.lbl_tip.setVisible(bool(msg))
        if warn and msg:
            # 报错时不许收起浮层：全屏下顶栏是收起来的，栏再一收，
            # 用户就看不到“为什么没画面”了
            self._bar_hold = True
            self._wake_bar()
        self._sync_deck_gap()      # 提示行的有/无会改播放条高度，spacer 与摆位都得跟上

    # ---------------- 全屏浮层：摆位 / 静止收起 ----------------

    def _bar_rect(self):
        """浮层该占的那块矩形（dialog 坐标）：永远底部居中

        取消左/右侧边那两档：用户要的是“进度条就在屏幕下面横着”，
        贴左右边时它落在屏幕中部，反而不符合使用习惯。"""
        w = min(max(self._deck.sizeHint().width(), 320),
                max(self.width() - 2 * self.GAP, 200))
        h = max(self._deck.sizeHint().height(), self.BAR_H)
        return QRect(int((self.width() - w) // 2), int(self.height() - h - self.GAP),
                     int(w), int(h))

    def _relocate(self):
        """按当前态摆播放条：非全屏贴下沿通栏、不透明方角；全屏做底部居中的浮层"""
        if not hasattr(self, "_deck") or self._deck is None:
            return
        if self._fullscreen:
            self._deck.setGeometry(self._bar_rect())
        else:
            self._deck.setGeometry(0, max(self.height() - self._deck_h, 0),
                                   self.width(), self._deck_h)
        self._update_bar_state()
        self._deck.raise_()
        if hasattr(self, "_grip") and self._grip is not None:
            # 播放条不进布局了，QSizeGrip 也没人替它摆（它不属任何布局，默认停在
            # 左上角一块 100×30，恰好压在播放/快退按钮上拦点击）：自己抬到右下角。
            # 全屏时收起来：浮层靠逐像素 alpha 透视频，native 子窗口会把那块戳个坑。
            self._grip.setVisible(not self._fullscreen)
            if not self._fullscreen:
                gs = self._grip.sizeHint().boundedTo(self._deck.size())
                self._grip.resize(gs)
                self._grip.move(max(self._deck.width() - gs.width(), 0),
                                max(self._deck.height() - gs.height(), 0))
                self._grip.raise_()

    def _update_bar_state(self):
        """把浮层的显/隐与不透明度算一遍（非全屏永远实体不透明）"""
        if not self._fullscreen:
            self._deck.show()
            self._deck.set_style(self.ALPHA_HOT, False)
            return
        if self._bar_hidden:
            self._deck.hide()
            return
        self._deck.show()
        hot = self._bar_hold or self._hover_bar()
        self._deck.set_style(self.ALPHA_HOT if hot else self.ALPHA_IDLE, True)

    def _hover_bar(self):
        """鼠标是否落在浮层矩形里

        用全局坐标算，不用 Enter/Leave：栏里的按钮/下拉会各自吃掉事件，
        指针从播放条空白移到按钮上时栏自己收到的是 Leave，拿它判会误报“移开了”，
        表现就是按住倍速下拉时横栏突然变透。"""
        if self._deck.isHidden():
            return False
        return self._deck.rect().contains(self._deck.mapFromGlobal(QCursor.pos()))

    def _wake_bar(self):
        """浮层现身（或保持在身）并把静止计时归零"""
        if not self._fullscreen:
            return
        self._bar_hidden = False
        self._update_bar_state()
        self._bar_timer.start(self.IDLE_MS)

    def _on_bar_idle(self):
        """鼠标静止到点：先验一眼“是不是正搭在栏上/正报错”，都不是才收"""
        if not self._fullscreen:
            return
        if self._bar_hold or self._hover_bar():
            self._update_bar_state()
            self._bar_timer.start(self.IDLE_MS)
            return
        self._bar_hidden = True
        self._update_bar_state()

    def _apply_shell_min(self):
        """窗口最小宽度的底线：既不能窄过播放条要的宽，也不能窄过画面下限

        播放条不进布局，就没人替它顶住窗宽：窗一窄，栏里的控件先被压扁
        （实测栏要 436，窗只有 402 时时间标签就被切成 140）。"""
        if self._fullscreen or not hasattr(self, "_deck"):
            return
        need = max(self.view.minimumWidth(), self._deck.minimumSizeHint().width())
        if self.minimumWidth() != need:
            self.setMinimumWidth(need)

    def _sync_deck_gap(self):
        """非全屏时把 spacer 高度对到播放条实测高（报错提示行一出，条就长一截）

        不靠“_deck_h 变没变”判要不要刷 spacer：退出全屏那一路 spacer 是被收到 0 的，
        而高度值本身可能跟全屏里一样，靠旧值比就会漏刷，画面底下永远少一条播放条。"""
        if self._fullscreen or not hasattr(self, "_deck_gap"):
            return
        h = max(self._deck.sizeHint().height(), 48)
        self._deck_h = h
        self._deck_gap.changeSize(0, h, QSizePolicy.Policy.Ignored,
                                  QSizePolicy.Policy.Fixed)
        self._apply_shell_min()
        self.layout().activate()
        if self.height() < self.minimumSizeHint().height():
            # 窗高是早先按“没提示行”的播放条算出来的，提示行一冒出来就不够放：
            # 不补这一刀，画面（有 MIN_H 顶着不肯让）会与播放条叠在一起
            self.resize(self.width(), self.minimumSizeHint().height())
        self._relocate()

    def _refit(self):
        """把视频按原比例塞进可见区：多出来的地方由 view 的纯黑背景当黑边

        必须在每次窗口尺寸变化后重跑：fitInView 设的是一次性的缩放矩阵，
        窗口后来被 _place/_reconcile 改大改小，缩放不会自己跟着变，
        画面就会停在旧尺寸上（看着比播放器大一号或小一号）。"""
        if not hasattr(self, "item") or self.item.boundingRect().isEmpty():
            return
        self.view.fitInView(self.item, Qt.AspectRatioMode.KeepAspectRatio)

    @property
    def _deck_alpha(self):
        """当前播放条的不透明度（测试与调试用）"""
        return self._deck.alpha()

    def _video_move(self, e):
        """画面上的鼠标移动：非全屏时拿它拖着窗走（无边框，画面就是唯一抓手）；
        全屏时不拖窗，只负责把浮层顶回来。

        这里不再区分“有没有动到栏那条边缘”：鼠标动一下就算续期/唤回，
        真的静止满 IDLE_MS 才收起（用户反馈过：往栏那边动的时候它不该收得
        那么快，那会儿计时根本没归零）。"""
        if self._fullscreen:
            self._wake_bar()
            return
        if self._drag_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def _video_wheel(self, e):
        """画面上的滚轮调音量（与 ↑/↓ 同一件事），不让 view 拿它去滚动场景"""
        dy = e.angleDelta().y()
        if dy == 0:
            e.ignore()
            return
        step = 5 if dy > 0 else -5
        self.vol.setValue(max(0, min(100, self.vol.value() + step)))
        e.accept()

    def eventFilter(self, obj, e):
        """播放条自己的鼠标事件：进了栏就变不透明并重新计时，离开了恢复半透明"""
        if obj is self._deck and self._fullscreen:
            t = e.type()
            if t == QEvent.Type.MouseMove:
                self._bar_hidden = False
                self._update_bar_state()
                self._bar_timer.start(self.IDLE_MS)
            elif t in (QEvent.Type.Enter, QEvent.Type.Leave):
                self._update_bar_state()      # 悬停态靠 _hover_bar 实算，这里只刷新 alpha
        return super().eventFilter(obj, e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refit()           # 窗一变画面必须重按比例铺（缩放不会自己跟着变）
        self._relocate()          # 窗宽一变，通栏/浮层都得重新摆

    # ---------------- 窗口：全屏 / 横竖屏自适应 / 无边框拖动 ----------------

    def _set_fullscreen(self, on):
        """全屏＝画面铺满整块屏 + 播放条换成压在画面上的半透明浮层

        showFullScreen() 用的是屏幕 geometry（不是 availableGeometry），本来就该
        盖住任务栏；以前看着“不真全屏”，是外面那一层壳子还留着 10×8 留白 + 描边
        + 顶栏，画面根本没顶到屏幕边。
        进全屏：顶栏收掉、播放条那一行的 spacer 收到 0（画面吃满整屏）、按设置的
        位置把横排面板摆成浮层、重新起“鼠标静止就收起”的计时。
        退出反过来走一遍：窗控条回来、播放条恢复实体通栏，再按视频比例重摆一次。"""
        self._fullscreen = bool(on)
        # 顶栏收起后窗控改由浮层末尾的 ―/✕ 承担
        for b in (self.btn_min_fs, self.btn_close_fs):
            b.setVisible(on)
        if on:
            self._topbar.hide()
            self._deck_gap.changeSize(0, 0, QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Fixed)
            self.layout().activate()
            self.showFullScreen()
            self._bar_hold = False
            self._bar_hidden = False
            self._refit()
            self._relocate()
            self._wake_bar()          # 刚进全屏先亮一遍，静止够久才收
        else:
            self.showNormal()
            self._topbar.show()
            self._bar_timer.stop()
            self._bar_hidden = False
            self._bar_hold = False
            self._sync_deck_gap()     # spacer 要回那一行高度，画面不能再盖到栏下面
            if self._last_size:
                # 退出全屏：重新按视频比例摆一次（_fitted 清空才能再触发）
                self._fitted = None
                self._fit_to_video(QSize(*self._last_size))
        self.btn_full.blockSignals(True)
        self.btn_full.setChecked(on)
        self.btn_full.blockSignals(False)

    def _toggle_fullscreen(self):
        self._set_fullscreen(not self._fullscreen)

    def _avail(self):
        return (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()

    def _scale_factor(self):
        """预览框大小系数：读设置页写入的百分比（存 ui_state），限幅 40%~160%

        100% 即「整体缩小 30% 后的新基准」；每次开播放器时重读，改完下次打开即生效。"""
        try:
            v = int(app_state.get(self.SCALE_KEY) or self.SCALE_DEFAULT)
        except (TypeError, ValueError):
            v = self.SCALE_DEFAULT
        v = max(self.SCALE_MIN, min(self.SCALE_MAX, v))
        return v / 100.0

    def _place(self, w, h):
        """摆窗 + 居中（居中按可用区算，别被菜单栏/Dock 顶到屏幕外）"""
        avail = self._avail()
        self.resize(min(w, avail.width()), min(h, avail.height()))
        self.move(avail.x() + (avail.width() - self.width()) // 2,
                  avail.y() + (avail.height() - self.height()) // 2)

    def showEvent(self, e):
        super().showEvent(e)
        self._relocate()
        if self._fitted is None and not self._fullscreen:
            # 后端还没报出视频尺寸（容器没写 Resolution 或报得比首帧晚）：
            # 先按半屏给一个够大的窗，不要把播放器开成一个巴掌大的方块，
            # 等视频尺寸到了再按真实比例重摆一次。
            avail = self._avail()
            s = self._scale_factor()
            self._place(int(avail.width() * self.FALLBACK_FILL * s),
                        int(avail.height() * self.FALLBACK_FILL * s))

    def _on_video_size(self, *_):
        """视频尺寸变化的通知：尺寸要自己从 item 上取

        PySide6 6.11 里这条信号到 Python 槽是不带参数的（签名就是
        nativeSizeChanged()），当初写成接一个 QSize，槽每次都被 0 参数调用、
        报个 TypeError 被 Qt 吃掉——横竖屏自适应等于完全没做。
        所以这里签 (self, *_)：带不带尺寸都能接。"""
        ns = self.item.nativeSize()
        if ns.width() > 0 and ns.height() > 0:
            # 把场景尺寸钉成视频像素（1:1）：nativeSizeChanged 只改 nativeSize，
            # item 自己的 size 不跟着变，不补这一刀 fitInView 就是按 320×240 放
            self.item.setSize(ns)
        self._refit()
        self._fit_to_video(ns)

    def _fit_to_video(self, size):
        """按视频真实宽高比重排窗口：横屏视频给横窗、竖屏视频给窄高窗

        判据是「比例变没变」而不是「只摆第一次」：后端可能先报一个占位尺寸、
        准确尺寸随后才到，只摆一次会停在错的比例上；而比例没变时不重摆，
        用户拖大拖小不会被后续回调顶回去。
        这里只能粗算（外壳占多少算不准），真正的贴比例交给 _reconcile。"""
        w, h = int(size.width()), int(size.height())
        if w <= 0 or h <= 0 or self._fullscreen:
            return
        self._last_size = (w, h)
        # 画面下限按手机屏幕给，方向跟着视频转（竖版 402×874、横版 874×402）：
        # 小过一个手机屏幕就没法审片，拖窗也拖不到那么小。
        pw, ph = ((self.PHONE_SHORT, self.PHONE_LONG) if h > w
                  else (self.PHONE_LONG, self.PHONE_SHORT))
        if (self.view.minimumWidth(), self.view.minimumHeight()) != (pw, ph):
            self.view.setMinimumSize(pw, ph)
            self._apply_shell_min()
        cur = w / h
        if self._fitted is not None and abs(cur - self._fitted) < self.REFIT_EPS:
            return
        self._fitted = cur
        avail = self._avail()
        s = self._scale_factor()
        scale = min(avail.width() * self.FILL_W * s / w,
                    avail.height() * self.FILL_H * s / h)
        # 宽的底线还要给播放条留出富余：播放条不进布局，没人替它顶住窗宽，
        # 窗比栏要的宽度还窄时，栏里的时间标签先被切成一截（按钮也挤成一团）
        bar_w = self._deck.minimumSizeHint().width() if hasattr(self, "_deck") else 0
        self._place(max(int(w * scale), pw + 60, bar_w + 2 * self.GAP),
                    max(int(h * scale), ph + 60))
        # 上面的尺寸是「整窗」而不是「画面」，布局会把画面区压成另一个比例；
        # 等一轮事件让布局落地后再按实测尺寸补差（_reconcile 里会继续约正）。
        QTimer.singleShot(0, self._reconcile)

    def _reconcile(self):
        """把窗口修到「画面区尺寸 == 视频宽高比」

        黑边的唯一来源就是画布比例与视频比例不等，而外壳占多少没法凭空算准
        （控件摆几行、字体大一号都会变），所以先摆一次再拿实测的画面尺寸补差：
        先调高（高的余量最便宜），顶到屏幕上下限了再反过来调宽。"""
        if self._fullscreen or not self._last_size or not self.isVisible():
            return
        vw, vh = self.view.viewport().width(), self.view.viewport().height()
        if vw <= 0 or vh <= 0:
            return
        w, h = self._last_size
        avail = self._avail()
        want_h = vw * h / w                       # 画面宽 vw 时该有多高
        old_h = self.height()
        new_h = min(max(int(old_h + want_h - vh), self.minimumSizeHint().height()),
                    avail.height())
        if new_h != old_h:
            self.resize(self.width(), new_h)
        # 高度真被改过就要等一轮布局把新的画面高报回来；没改（已顶到上下限）
        # 直接走宽度那一步，否则 self.height() 已是新值，比对永远相等
        if abs(new_h - old_h) <= 2:
            self._reconcile_width()
        else:
            QTimer.singleShot(0, self._reconcile_width)

    def _reconcile_width(self):
        """高度顶到屏幕上限补不开时，反过来把宽度收到贴视频比例"""
        if self._fullscreen or not self._last_size or not self.isVisible():
            return
        vw, vh = self.view.viewport().width(), self.view.viewport().height()
        if vw <= 0 or vh <= 0:
            return
        w, h = self._last_size
        avail = self._avail()
        want_w = vh * w / h                       # 画面高 vh 时该有多宽
        if abs(want_w - vw) <= 2:
            return
        new_w = min(max(int(self.width() + want_w - vw), self.minimumSizeHint().width()),
                    avail.width())
        self.resize(new_w, self.height())

    def _video_pressed(self, e):
        """按下画面先不得动手：可能是要点（播放/暂停），也可能是想把窗拖走——
        无边框之后四周再没有留白，画面就是唯一能抓的地方，所以按下只记下位置，
        等松手看位移再判（同思维导图那套）。
        右键不在这里管（交给 contextMenuEvent），ignore 掉免得顺带拨一下播放态。"""
        self._press = None
        if e.button() != Qt.MouseButton.LeftButton:
            e.ignore()
            return
        self._press = e.globalPosition().toPoint()
        self._drag_pos = self._press - self.frameGeometry().topLeft()
        e.accept()                # 收下按下：后续 move 才回到本控件（再往上传给窗口拖动）

    def _video_released(self, e):
        p, self._press = self._press, None
        self._drag_pos = None
        if (p is not None and e.button() == Qt.MouseButton.LeftButton
                and (e.globalPosition().toPoint() - p).manhattanLength() <= self._click_tol()):
            self._toggle_play()   # 原地按住松开＝点；拖出去了＝只挪窗，不拨播放态
            self._click1_toggled = True
        e.accept()

    def _video_double(self, e):
        """双击画面＝全屏。Qt6 的 QMouseEvent 没 detail()（那是 Qt5 的），双击走
        mouseDoubleClickEvent，不是带 detail()==2 的按下——上一版这里靠 detail() 判双击，
        每次按画面都直接抛异常，所以单击暂停与双击全屏其实一直没生效。
        双击的第一个按下已经把播放态拨了一下，这里再拨回去：不要“进全屏顺手把片停了”。"""
        self._press = None
        self._toggle_fullscreen()
        if self._click1_toggled:
            self._toggle_play()
            self._click1_toggled = False
        e.accept()

    @staticmethod
    def _click_tol():
        """允许的位移判据：跟着系统拖动阈值走，最少给 4px（否则手抖一下就算拖拽）。"""
        return max(int(QApplication.startDragDistance()), 4)

    def _show_bar_menu(self, e):
        """播放界面右键菜单：把从标题栏收起的「识别字幕 / 定位源文件」收在这里。
        事件从 _VideoView 递过来（画面是窗口的主体，右键能落到的就那一块）。"""
        m = StyledMenu(self)
        m.addAction("🎤 识别字幕", self._do_asr)
        m.addAction("📂 定位源文件", self._reveal)
        m.exec(e.globalPos())
        e.accept()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        # 全屏时不拖；按在按钮/滑块上时事件被子控件吃掉，不会走到这里
        if self._drag_pos is not None and not self._fullscreen \
                and e.buttons() & Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key.Key_Escape:
            if self._fullscreen:
                self._set_fullscreen(False)
            else:
                self.close()
        elif k in (Qt.Key.Key_Space, Qt.Key.Key_K):
            self._toggle_play()
        elif k == Qt.Key.Key_Left:
            self._step_seek(-1)
        elif k == Qt.Key.Key_Right:
            self._step_seek(1)
        elif k == Qt.Key.Key_Up:
            self.vol.setValue(min(100, self.vol.value() + 5))
        elif k == Qt.Key.Key_Down:
            self.vol.setValue(max(0, self.vol.value() - 5))
        elif k in (Qt.Key.Key_F, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._toggle_fullscreen()
        elif k == Qt.Key.Key_M:
            self.btn_mute.click()
        elif k == Qt.Key.Key_L:
            self.btn_loop.click()
        elif k in (Qt.Key.Key_Comma, Qt.Key.Key_BracketLeft):
            self._step_rate(-1)
        elif k in (Qt.Key.Key_Period, Qt.Key.Key_BracketRight):
            self._step_rate(1)
        elif k == Qt.Key.Key_Slash:
            self.cb_rate.setCurrentIndex(list(RATES).index(1.0))
        elif k == Qt.Key.Key_G:
            self._reveal()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        self.player.stop()
        super().closeEvent(e)

    # ---------------- 审片标记 ----------------

    def _reveal(self):
        if Path(self._path).exists():
            reveal_in_folder(self._path)
        else:
            self._say("文件不存在或已被移动", True)

    def _current_mark(self):
        if not self._allow_mark:
            return ""
        from processors import output_mark
        return output_mark.mark_of(self._path)

    def _sync_mark_buttons(self):
        mark = self._current_mark()
        self.btn_ok.setChecked(mark == "ok")
        self.btn_bad.setChecked(mark == "bad")

    def _on_mark(self, want):
        """点当前已选中的那个按钮＝取消标记

        改名前必须先松手：Windows 上改不了「正被打开」的文件，而媒体后端
        此刻就是拿着它的那个。"""
        if not self._allow_mark:
            return
        from processors import output_mark
        mark = self._current_mark()
        target = "" if mark == want else want
        resume = self.player.position()
        was_playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self._swapping = True
        self.player.stop()
        self.player.setSource(QUrl())        # 把文件句柄放掉
        self._swapping = False
        ok, new_path, msg = output_mark.set_mark(self._path, target)
        if not ok:
            self._say(msg, True)
            self._load(self._path, autoplay=was_playing)
            self._sync_mark_buttons()
            return
        self._reload_after_rename(new_path, resume, was_playing)
        self._say(msg)
        self._sync_mark_buttons()
        self.marked.emit(new_path, target)


class _ImageView(QGraphicsView):
    """可缩放图片画布：滚轮以光标为锚点缩放，左键按住拖拽平移，默认 100% 原图。

    用 QGraphicsView 而不是旧的 QScrollArea+缩放 QLabel：后者只能整块重
    绘，做不到“围着鼠标位置缩放”与平滑大图平移；Scene/View 自带滚动条与
    ScrollHandDrag 拖拽，缩放用 QTransform 累积、不重新采样原图。
    """
    scaleChanged = Signal(float)

    MIN_SCALE = 0.02
    MAX_SCALE = 40.0
    STEP = 1.25

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self._scale = 1.0
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setMouseTracking(True)

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if dy == 0:
            e.ignore()
            return
        self.zoom(self.STEP if dy > 0 else 1 / self.STEP)
        e.accept()

    def zoom(self, factor):
        target = max(self.MIN_SCALE, min(self.MAX_SCALE, self._scale * factor))
        if abs(target - self._scale) < 1e-6:
            return
        self.scale(target / self._scale, target / self._scale)
        self._scale = target
        self.scaleChanged.emit(self._scale)

    def to_original(self):
        """100%：按原始像素显示（变换矩阵归一）"""
        self.resetTransform()
        self._scale = 1.0
        self.scaleChanged.emit(1.0)

    def fit_window(self):
        r = self.scene().sceneRect()
        if r.isEmpty():
            return
        self.fitInView(r, Qt.AspectRatioMode.KeepAspectRatio)
        self._scale = float(self.transform().m11())
        self.scaleChanged.emit(self._scale)


class _FloatBar(QWidget):
    """图片预览的半透明浮动控制条：直接压在图片之上，圆角深底 + 白色图标按钮。

    无边框预览窗没有标题栏，这条浮具承担全部操作；按住它的空白处（信息文字 /
    按钮之间）可拖动整个窗口。用 paintEvent 画带 alpha 的圆角矩形而不是走 QSS
    背景，保证半透明能真正透出下面的图片（子控件走逐像素 alpha 合成）。
    """
    RADIUS = 14

    def __init__(self, dlg):
        super().__init__(dlg)
        self._dlg = dlg
        self._drag = None
        self.setObjectName("ImgFloatBar")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(tokenize("""
            #ImgFloatBar QPushButton { background:transparent; color:#E6EAF2;
                border:0; border-radius:7px; padding:3px 9px; font-size:12px; }
            #ImgFloatBar QPushButton:hover { background:#3A4250; color:#FFFFFF; }
            #ImgFloatBar QPushButton:checked { background:#3370FF; color:#FFFFFF; }
            #ImgFloatBar QPushButton#ImgClose:hover { background:#D94A43; color:#FFFFFF; }
            #ImgFloatBar QLabel { color:#C9CFDA; background:transparent; font-size:12px; }
        """))
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(12, 6, 12, 6)
        self.lay.setSpacing(4)

    def add(self, widget):
        self.lay.addWidget(widget)

    def _btn(self, text, tip, slot, obj=""):
        b = QPushButton(text)
        b.setToolTip(tip)
        b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        if obj:
            b.setObjectName(obj)
        b.clicked.connect(slot)
        return b

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.setBrush(QColor(21, 23, 28, 170))
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawRoundedRect(r, self.RADIUS, self.RADIUS)
        p.end()

    # 按住浮条空白处拖动窗口（按钮 / 标签之外的区域）
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self._dlg.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.MouseButton.LeftButton:
            self._dlg.move(e.globalPosition().toPoint() - self._drag)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag = None


class ImagePreviewDialog(QDialog):
    """无边框图片预览：图片铺满整窗，滚轮缩放、左键拖拽平移；操作按钮做成
    压在图片上的半透明浮条（内置），不再有一圈粗边框 / 标题栏。

    产品中心双击参考图唤起（也供其它页面复用）。Esc 或浮条 ✕ 关闭。
    """

    def __init__(self, parent, file_path):
        super().__init__(parent)
        self._path = str(file_path)
        self.setWindowTitle(f"图片预览 - {Path(file_path).name}")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(int(screen.width() * 0.8), 1400),
                    min(int(screen.height() * 0.86), 980))
        self._pm = QPixmap(self._path)
        if self._pm.isNull():
            QMessageBox.warning(self, "无法打开", f"图片无法显示：\n{self._path}")
            self.reject()
            return

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._scene = QGraphicsScene(self)
        self._scene.addPixmap(self._pm)
        self.view = _ImageView(self._scene, self)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setBackgroundBrush(QColor("#15171C"))
        # 关掉滚动条：无边框铺满靠拖拽平移看局部，留滚动条会破坏“无边框”观感
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.scaleChanged.connect(self._on_scale)
        lay.addWidget(self.view)

        # ---------- 半透明浮动控制条（内置在图片之上）----------
        self.bar = _FloatBar(self)
        self.lbl_info = QLabel(f"{self._pm.width()} × {self._pm.height()} px")
        self.bar.add(self.lbl_info)
        self.lbl_zoom = QLabel("100%")
        self.lbl_zoom.setMinimumWidth(46)
        self.lbl_zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bar.add(self.lbl_zoom)
        self.bar.add(self.bar._btn("－", "缩小（滚轮向下 / 键盘 -）",
                                   lambda: self.view.zoom(1 / _ImageView.STEP)))
        self.bar.add(self.bar._btn("＋", "放大（滚轮向上 / 键盘 +）",
                                   lambda: self.view.zoom(_ImageView.STEP)))
        self.bar.add(self.bar._btn("100%", "按原图像素显示（键盘 0）", self.view.to_original))
        self.bar.add(self.bar._btn("适应", "缩放到刚好铺满窗口（键盘 F）", self.view.fit_window))
        self.bar.add(self.bar._btn("📋", "把原图复制到剪贴板", self._copy))
        self.bar.add(self.bar._btn("🖨", "打印原图（按页面等比缩放居中）", self._print))
        self.bar.add(self.bar._btn("📂", "用系统默认程序打开", lambda: open_path(self._path)))
        self.bar.add(self.bar._btn("✕", "关闭（Esc）", self.reject, "ImgClose"))

        # 右下角缩放把手（无边框也能拉大拉小）
        self._grip = QSizeGrip(self)
        self._grip.setFixedSize(14, 14)

        # 默认贴合原图：100% 像素铺上，图比窗大就拖拽看，小就居中
        self.view.to_original()
        self.view.centerOn(self._scene.sceneRect().center())

    def _relocate(self):
        """把浮条底部居中、右下角放缩放把手（窗口尺寸一变就要重新定位）"""
        if not hasattr(self, "bar"):
            return
        self.bar.adjustSize()
        bw = min(self.bar.sizeHint().width(), max(self.width() - 16, 0))
        bh = self.bar.sizeHint().height()
        self.bar.resize(max(bw, 1), bh)
        self.bar.move(max(8, (self.width() - bw) // 2),
                      max(8, self.height() - bh - 16))
        self.bar.raise_()
        self._grip.move(self.width() - 16, self.height() - 16)
        self._grip.raise_()

    def showEvent(self, e):
        super().showEvent(e)
        self._relocate()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relocate()

    def _on_scale(self, s):
        self.lbl_zoom.setText(f"{round(s * 100)}%")

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key.Key_Plus, Qt.Key.Key_Add, Qt.Key.Key_Equal):
            self.view.zoom(_ImageView.STEP)
        elif k in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self.view.zoom(1 / _ImageView.STEP)
        elif k == Qt.Key.Key_0:
            self.view.to_original()
        elif k == Qt.Key.Key_F:
            self.view.fit_window()
        else:
            super().keyPressEvent(e)

    def _copy(self):
        QApplication.clipboard().setPixmap(self._pm)
        self.lbl_info.setText("已复制原图到剪贴板")

    def _print(self):
        try:
            from PySide6.QtPrintSupport import QPrinter, QPrintDialog
        except Exception:
            QMessageBox.information(self, "打印不可用", "当前环境缺少打印组件")
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle("打印图片")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        p = QPainter(printer)
        rect = p.viewport()
        pm = self._pm.scaled(rect.size(), Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        p.drawPixmap((rect.width() - pm.width()) // 2,
                     (rect.height() - pm.height()) // 2, pm)
        p.end()
        self.lbl_info.setText("已发送到打印机")


# 中文（含中文标点）连续片段：悬停预览时整段高亮，与英文镜头描述区分开
_CJK_RUN = re.compile(r"([\u4e00-\u9fff\u3000-\u303f\uff01-\uff5e、。！？“”‘’…—·\u2014]+)")

# 高亮取设计令牌：文字用 warning 橙（与全站强调色一致）；软黄底是暖色，
# 令牌里的 *_soft 全是冷调（蓝/红）压不住橙字，故单独留一档暖黄
_HL_COLOR = COLORS["warning"]
_HL_BG = "#FFF4C4"


def _highlight_cjk_html(text):
    """转义 HTML 后，把每段中文包成橙底色 span；保留原有换行"""
    lines = []
    span = f'<span style="color:{_HL_COLOR}; background-color:{_HL_BG};">\\1</span>'
    for line in text.split("\n"):
        esc = html.escape(line)
        lines.append(_CJK_RUN.sub(span, esc))
    return "<br>".join(lines)


class HoverPreview(QWidget):
    """全文预览浮层：限宽自动换行、超长可滚动、保留原文换行排版。

    两种模式：
    - 钉住（show_pinned）：单击/空格唤起，Popup 模式抓取键盘，任意键关闭、点其它地方也关闭；
    - 悬停（show_at + hide_soon）：保留旧延时防抖机制，钉住模式下不生效。

    钉住模式里 Alt+W（macOS ⌘+W）是“任意键关闭”的例外：它不关窗，而是维护人的
    中文对照开关。提示词多是英文写的，正在读预览时最想知道中文，不该逼人到
    编辑弹窗里再翻一遍。第一次要口令，之后同一份内容在原文/对照间来回切。
    """

    WIDTH = 460           # 白卡可见宽（不含四周阴影带）
    MAX_H = 340           # 白卡可见最大高
    SHADOW = 14           # 四周软阴影活动带（与 _RoundPopup 同款：透明边带里逐层画阴影）
    PAD = 12              # 卡片内文字留白（也保证文字不压到圆角）
    HIDE_DELAY = 300        # ms：离开单元格后给移入浮层留的反应时间
    # 单独按下修饰键（比如先按住 Alt）不算“按了个键”：否则 Alt+W 的第一个键
    # 就把浮层关掉了，组合键永远按不出来
    MOD_KEYS = (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt,
                Qt.Key.Key_Meta, Qt.Key.Key_CapsLock)

    def __init__(self, parent=None, host=None):
        super().__init__(parent)
        # ToolTip 窗口标志：不抢焦点、永远浮在最上层；NoDropShadow + 逐像素透明，
        # 四角交给 paintEvent 自绘圆角卡——与 StyledMenu / TipBubble 同一套弹层家族，
        # 告别过去「QSS border-radius 在方窗里画圆角（直角套圆角）」和脱离调色板的硬编码色。
        self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # 外层留出阴影带，里面放一个透明的 QScrollArea 承载正文（超长可滚动）
        outer = QVBoxLayout(self)
        outer.setContentsMargins(self.SHADOW, self.SHADOW, self.SHADOW, self.SHADOW)
        outer.setSpacing(0)
        self._scroll = QScrollArea()
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.label = QLabel()
        self.label.setWordWrap(True)                       # 按宽度自动换行
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._scroll.setWidget(self.label)
        outer.addWidget(self._scroll)
        # 内容层保持透明，让自绘白卡透出；文字 / 滚动条一律取设计令牌（tokenize 映射回 COLORS）
        self.setStyleSheet(tokenize(f"""
            QScrollArea {{ background:transparent; border:none; }}
            QScrollArea > QWidget > QWidget {{ background:transparent; }}
            QLabel {{ background:transparent; color:#1F2329; font-size:12px;
                      padding:{self.PAD}px; }}
            QScrollBar:vertical {{ width:8px; background:transparent; margin:0; }}
            QScrollBar::handle:vertical {{ background:#C9CDD4; border-radius:4px;
                                           min-height:24px; }}
            QScrollBar::handle:vertical:hover {{ background:#A9AFB8; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background:transparent; }}
        """))
        self.setFixedWidth(self.WIDTH + 2 * self.SHADOW)
        self.setMaximumHeight(self.MAX_H + 2 * self.SHADOW)
        self._keep_rect = None                             # 全局坐标：视为“未离开”的区域
        self._pinned = False                # 钉住模式（单击/空格唤起）中
        self._hide_timer = QTimer(self)                    # 延时隐藏检查
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(self.HIDE_DELAY)
        self._hide_timer.timeout.connect(self._check_hide)

        # ---- 维护人翻译（只在钉住模式下可用）----
        # 浮层本身是 Popup，收不到 QShortcut，所以在 keyPressEvent 里自己比对按键
        self._summon = QKeySequence(MAINTAINER_SHORTCUT)[0]
        self._host = host or parent       # 口令框要有宿主窗口，否则孤零零弹在屏幕正中
        self._pin_text = ""               # 当前钉住的那份原文
        self._pin_rich = True
        self._zh = ""                     # 当前内容的中文对照
        self._zh_src = ""                 # 对照对应的原文（内容换了就得重翻）
        self._showing_zh = False
        self._gate_pin = Gate("prompt_translate")
        self._unlocked = app_has_unlocked("prompt_translate")   # 当天验过就不过问
        self._busy = False
        self._tip = ""                    # 一次性提示（无需翻译之类）
        self._err = ""
        self._worker = None

    def set_keep_rect(self, rect):
        self._keep_rect = rect

    @property
    def pinned(self):
        return self._pinned

    def _to_html(self, text, highlight):
        if highlight:
            return _highlight_cjk_html(text)
        return "<br>".join(html.escape(line) for line in str(text or "").split("\n"))

    def paintEvent(self, e):
        # 透明顶层上自绘一张圆角白卡 + 一圈软阴影（与全站弹层家族同款）；
        # 内容层保持透明，卡片底色从四角透出——修「方窗套圆角」。
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw_rounded_card(p, self.rect(), TOK_RADIUS["lg"], self.SHADOW)
        p.end()
        super().paintEvent(e)

    def _fit(self):
        self.label.adjustSize()
        card_h = min(self.label.sizeHint().height() + 2 * self.PAD, self.MAX_H)
        self.setFixedHeight(card_h + 2 * self.SHADOW)

    def _render(self, text, rich_text):
        """悬停模式：只铺内容，不带翻译尾注（没解锁的人不该看见 Alt+W）"""
        self._hide_timer.stop()
        if rich_text:
            # 富文本：中文字高亮，换行用 <br> 还原
            self.label.setTextFormat(Qt.TextFormat.RichText)
            self.label.setText(self._to_html(text, True))
        else:
            self.label.setTextFormat(Qt.TextFormat.PlainText)
            self.label.setText(text.rstrip())
        self._fit()

    def _repaint(self):
        """钉住模式重绘：内容 + 一行灰色尾注，高度跟着尾注一起算"""
        body = self._zh if self._showing_zh else self._pin_text
        # 中文高亮是给“英文里夹中文”用的，整篇对照再刷黄底就成了满屏荧光笔
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setText(self._to_html(body, self._pin_rich and not self._showing_zh)
                           + self._foot())
        self._fit()

    def _foot(self):
        if not self._unlocked:
            return ""
        if self._busy:
            tip = "🌐 翻译中，稍等…"
        elif self._err:
            tip = "⚠ " + self._err
        elif self._tip:
            tip = self._tip
        elif self._showing_zh:
            tip = "中文对照（提交给云端的仍是原文）· 再按 Alt+W 回原文"
        else:
            tip = "按 Alt+W 看中文对照 · 其它键关闭"
        return '<br><span style="color:{};">{}</span>'.format(
            COLORS["weak"], html.escape(tip))

    def show_pinned(self, text, pos, rich_text=True):
        """单击/空格唤起：钉住显示，任意键关闭（Popup 模式自带键盘抓取）"""
        if not text or not text.strip():
            self.hide()
            return
        self._pinned = True
        self._pin_text = text
        self._pin_rich = rich_text
        self._showing_zh = False      # 换了内容就先看原文，别拿上一段的对照糊弄
        self._tip = ""
        self._err = ""
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._repaint()
        self.move(self.clamp_to_screen(pos))
        self.show()
        self.setFocus()                         # Popup 模式下确保键盘事件进浮层

    def show_at(self, text, pos, rich_text=False):
        if not text or not text.strip():
            self.hide()
            return
        if self._pinned:
            return                              # 已钉住：悬停不覆盖正在阅读的内容
        if self.windowFlags() & Qt.WindowType.Popup:
            # 上一次钉住关闭后恢复 ToolTip 标志（不抢焦点、不自动关闭）
            self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.NoDropShadowWindowHint)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._render(text, rich_text)
        self.move(self.clamp_to_screen(pos))
        self.show()

    def hide_soon(self):
        """不立即隐藏：给鼠标留出移入浮层的时间；钉住模式不打扰"""
        if self._pinned:
            return
        if self.isVisible():
            self._hide_timer.start()

    def _check_hide(self):
        pos = QCursor.pos()
        if self.geometry().contains(pos) or \
                (self._keep_rect and self._keep_rect.contains(pos)):
            self._hide_timer.start()       # 还在浮层/表格内，继续观察
        else:
            self.hide()

    def hide(self):
        self._hide_timer.stop()
        was_pinned = self._pinned
        self._pinned = False
        super().hide()
        if was_pinned:
            # 恢复 ToolTip 标志，不影响下次悬停模式
            self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.NoDropShadowWindowHint)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def keyPressEvent(self, e):
        if not self._pinned:
            super().keyPressEvent(e)
            return
        e.accept()
        if e.key() in self.MOD_KEYS:
            return                              # 只按下了修饰键：浮层留着等第二个键
        if QKeyCombination(e.modifiers(), Qt.Key(e.key())) == self._summon:
            self._summon_zh()
            return
        self.hide()              # 其余任意键关闭

    # ================= 维护人：就地看中文对照 =================
    def _summon_zh(self):
        if not self._unlocked:
            self._unlock()
        elif self._showing_zh:
            self._showing_zh = False
            self._repaint()
        elif self._zh and self._zh_src == self._pin_text:
            self._showing_zh = True             # 翻过了直接切，不再发请求
            self._repaint()
        else:
            self._translate()

    def _unlock(self):
        """口令框是模态窗，会把 Popup 的键盘抓取顶掉：先收窗再问，问完原样钉回来；
        同一功能当天只验一次（跨天才重新问）"""
        text, rich, pos = self._pin_text, self._pin_rich, self.pos()
        self.hide()
        if self._gate_pin.ask(self._host or self):
            self._unlocked = True
            self.show_pinned(text, pos, rich)
            self._translate()
            return
        self.show_pinned(text, pos, rich)       # 没通过也别把人正在看的内容弄丢

    def _translate(self):
        if self._busy:
            return
        self._err = ""
        self._tip = ""
        if not translate.has_latin(self._pin_text):
            self._tip = "没看到英文，无需翻译"
            self._repaint()
            return
        self._busy = True
        self._repaint()
        src = self._pin_text                    # 回来后比对：期间可能已经换看了另一条
        self._worker = ToolWorker(lambda log, progress, stop: (src, translate.translate_mixed(src)),
                                  self)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, res):
        self._worker = None
        self._busy = False
        self._tip = ""
        if isinstance(res, Exception):
            self._err = str(res) or type(res).__name__
        else:
            src, zh = res
            if src != self._pin_text:
                self._tip = "看的已经换了内容，这次结果不用了"
            else:
                self._zh = str(zh or "")
                self._zh_src = src
                self._showing_zh = bool(self._zh)
                if not self._zh:
                    self._tip = "没翻出内容"
        self._repaint()

    def clamp_to_screen(self, pos):
        """防止浮层超出屏幕右侧/底部"""
        screen = QGuiApplication.primaryScreen().availableGeometry()
        x = min(pos.x(), screen.right() - self.width() - 8)
        y = min(pos.y(), screen.bottom() - self.height() - 8)
        return QPoint(max(x, screen.left()), max(y, screen.top()))


class Toast(QWidget):
    """右下角轻量浮层提示：可带一个动作按钮（如「撤销」），自动消失、不抢焦点。

    用于删除可撤销、本批任务完成等“提醒一下就够、不值得弹框打断”的场景。
    调用方需持有引用防 GC；close 时发 finished 信号便于自行清理引用。"""
    finished = Signal()

    def __init__(self, text, action_text="", msec=5000, on_action=None,
                 anchor=None, color="#3370FF", parent=None):
        super().__init__(parent, Qt.WindowType.Tool
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._on_action = on_action
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        box = QLabel(text)
        box.setObjectName("ToastBox")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setStyleSheet(
            tokenize(f"QLabel#ToastBox{{background:#FFFFFF;color:#1F2329;"
            f"border:1px solid {color};border-left:4px solid {color};"
            f"border-radius:8px;padding:10px 14px;}}"))
        lay.addWidget(box)
        if action_text:
            btn = QPushButton(action_text)
            btn.setObjectName("ToastBtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                tokenize("QPushButton#ToastBtn{background:#FFFFFF;color:#3370FF;"
                "border:1px solid #DEE0E3;border-radius:8px;padding:8px 12px;}"
                "QPushButton#ToastBtn:hover{border-color:#3370FF;}"))
            btn.clicked.connect(self._do_action)
            lay.addWidget(btn)
        self.adjustSize()
        self._place(anchor)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.close)
        self._timer.start(msec)
        self.finished.connect(self.deleteLater)

    def _do_action(self):
        try:
            if self._on_action:
                self._on_action()
        finally:
            self.close()

    def _place(self, anchor):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        m = 18
        if anchor is not None and anchor.isVisible():
            g = anchor.mapToGlobal(QPoint(0, 0))
            x = g.x() + anchor.width() - self.width() - m
            y = g.y() + anchor.height() - self.height() - m
        else:
            x = screen.right() - self.width() - m
            y = screen.bottom() - self.height() - m
        x = max(screen.left() + 4, min(x, screen.right() - self.width() - 4))
        y = max(screen.top() + 4, min(y, screen.bottom() - self.height() - 4))
        self.move(x, y)

    def mousePressEvent(self, e):
        self.close()          # 点一下即收起
        super().mousePressEvent(e)

    def closeEvent(self, e):
        self._timer.stop()
        self.finished.emit()
        super().closeEvent(e)


class _Spinner(QWidget):
    """旋转弧线加载动画（纯 QPainter 绘制，无额外依赖）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(26, 26)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self):
        self._timer.start(60)

    def stop(self):
        self._timer.stop()

    def _tick(self):
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#3370FF"), 4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        r = self.rect().adjusted(3, 3, -3, -3)
        p.drawArc(r, (90 - self._angle) * 16, -270 * 16)
        p.end()


class LoadingOverlay(QWidget):
    """轻量加载遮罩：盖在页面上，首次重查询刷新时显示转圈提示。

    用法：页面 __init__ 里 self._loading = LoadingOverlay(self)；
    showEvent 首次触发时 show_overlay()，再 QTimer.singleShot 延迟执行
    重刷新，让遮罩先绘制一帧，完成后 hide_overlay()。
    """

    def __init__(self, host):
        super().__init__(host)
        self._host = host
        self.setStyleSheet("background:rgba(250,251,252,225);")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addStretch(1)
        self.spinner = _Spinner(self)
        lay.addWidget(self.spinner)
        lbl = QLabel("加载中，请稍候…")
        lbl.setStyleSheet(tokenize("color:#646A73; font-size:13px; background:transparent;"))
        lay.addWidget(lbl)
        lay.addStretch(1)
        self.hide()

    def show_overlay(self):
        self.setGeometry(self._host.rect())
        self.spinner.start()
        self.show()
        self.raise_()

    def hide_overlay(self):
        self.spinner.stop()
        self.hide()
