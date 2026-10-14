"""
gui/screenshot.py —— 截图工具（区域/全屏/窗口 → 标注 → 钉住/复制/保存）

设计取向：
- 抓取先「冻结」整张虚拟桌面成一张 QPixmap 作底图：遮罩、工具条都是画在冻结图
  之上的我们的 UI，不会被截进结果里（截屏那一刻我们的窗口已先隐藏）。
- 选区 + 标注 + 钉住全程都在主线程用 QPainter 画，绝不进工作线程（项目线程纪律：
  后台线程不碰 QPainter/QFont）。
- 复用录屏已有的全屏遮罩底座：_MaskBase（盖满虚拟桌面 + Esc 取消）、WindowPicker
  （悬停吸附窗口）。区域框选手感自绘（在冻结图上选，不套用透出真实桌面的那套）。

标注工具（工具条下拉分组，不是一个按钮一个）：框线（矩形/圆角矩形/椭圆）、箭头（箭头/直线）、
画笔（P，自由手绘）、文字（T，字号与颜色可单独调）、马赛克（M）；线宽/颜色/字号均可选，Ctrl+Z 撤销。
选区阶段会悬停吸附前台窗口（高亮蓝框，单击即吸附整窗，按住拖动则自由框选）。
完成后钉住（置顶悬浮）、复制（写系统剪贴板，顺带进剪贴板历史）、保存（PNG 落盘）、或录屏（把选区
交给「屏幕录制」面板直接开录，可选内录/麦克风）。「🎞 GIF」把这块区域的实时画面录几秒、停录后自动
转成 GIF 并以“文件”形式复制进剪贴板（可直接粘贴到聊天，偷/录表情包；不录声音）。「🔤 取字」把选区里的屏幕文字本地 OCR 出来（无 token，
优先 RapidOCR、退回 Windows 内置 OCR，都没装则提示装 requirements-ocr.txt），可编辑修正后复制进剪贴板历史。
可在全局热键里配一键区域截图（默认 Ctrl+Alt+A）。
"""
import os
import time
from datetime import datetime

from PySide6.QtCore import Qt, QRect, QRectF, QPoint, QPointF, QTimer
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QKeySequence, QPainter,
                           QPen, QPixmap, QInputMethodEvent, QPolygonF)
from PySide6.QtWidgets import (QWidget, QLabel, QPushButton, QToolButton,
                               QHBoxLayout, QVBoxLayout, QColorDialog, QMenu,
                               QApplication, QFileDialog, QSizePolicy,
                               QKeySequenceEdit, QDialog, QCheckBox,
                               QPlainTextEdit, QMessageBox)

from core.config import DOWNLOAD_DIR
from gui import global_hotkey
from gui.dialogs_recorder import (_MaskBase, WindowPicker, MIN_DRAG,
                                  _phys_to_logical)
from gui.theme import tokenize
from gui.tools_registry import (SCREENSHOT_TOKEN, global_hotkey_enabled)

# 标注主色与默认线宽
RED = "#E53935"
SELECT_BORDER = "#3370FF"
DEFAULT_WIDTH = 3
MOSAIC_BLOCK = 12          # 马赛克像素块边长（逻辑像素）
# 拖一个矩形包围盒作图的框选类（矩形/圆角矩形/椭圆/马赛克共用同一段拖拽逻辑）
_BOX_TOOLS = ("rect", "roundrect", "ellipse", "mosaic")
# 两点成段的直线类（箭头 / 直线：按下点为起点，拖到终点）
_LINE_TOOLS = ("arrow", "line")
_TOOLS = ("rect", "roundrect", "ellipse", "arrow", "line", "pen", "text", "mosaic")


def _screens_box():
    """所有屏逻辑几何的并集（跨屏抓取/遮罩都按它铺满）。"""
    box = QRect()
    for s in QApplication.screens():
        box = box.united(s.geometry())
    return box


def freeze_desktop():
    """逐屏 grabWindow(0) 拼成整张虚拟桌面 QPixmap（逻辑坐标），返回 (pixmap, 桌面几何)。

    grabWindow(0) 抓的是该屏整幅；按各屏 devicePixelRatio 造带 DPR 的 QPixmap，再统一
    以「逻辑坐标 + 主屏 DPR」合成一张底图，遮罩直接按逻辑几何画即可（高 DPI 不糊）。
    """
    screens = QApplication.screens()
    if not screens:
        return None, QRect()
    primary_dpr = screens[0].devicePixelRatio()
    box = _screens_box()
    canvas = QPixmap(int(box.width() * primary_dpr), int(box.height() * primary_dpr))
    canvas.fill(Qt.GlobalColor.transparent)
    canvas.setDevicePixelRatio(primary_dpr)
    p = QPainter(canvas)
    p.setPen(Qt.PenStyle.NoPen)
    # canvas 的 painter 用逻辑坐标（画布逻辑尺寸＝box）：每块屏的截图 shot 由
    # grabWindow 自带该屏 DPR、逻辑尺寸即该屏几何，故按「逻辑目标矩形」1:1 贴回。
    # 以前乘了 primary_dpr 当位置——正是错位+放大的真凶：位置被推到 dpr 倍、
    # 且与鼠标/选区所在的逻辑坐标系对不上。
    for s in screens:
        g = s.geometry()
        shot = s.grabWindow(0)             # 物理像素截图（Qt 已按该屏 DPR 标注）
        target = QRect(g.x() - box.x(), g.y() - box.y(), g.width(), g.height())
        p.drawPixmap(target, shot)
    p.end()
    return canvas, box


def _primary_screen_box():
    s = QApplication.primaryScreen()
    return s.geometry() if s else QRect(0, 0, 1920, 1080)


def _window_rect_at(pt):
    """全局逻辑点下「肉眼可见的最靠前」应用窗口矩形（逻辑坐标）；没有给 None。

    复用录屏那套窗口枚举：list_windows(keep_order=True) 按 EnumWindows 的 z-order
    从最前到最后返回任务栏级应用窗，把每个窗的物理矩形换算成逻辑矩形后做命中测试，
    取第一个罩住该点的窗——即鼠标下真正看得见、压在别的窗之上那个。
    （旧写法取面积最小者，会抓到被其窗挡在后面、肉眼根本看不见的窗，故改。）
    最小化窗 gdigrab/吸附都无意义，跳过。"""
    try:
        from video_text_tools.screen_recorder import list_windows
    except Exception:
        return None
    try:
        wins = list_windows(exclude_self=True, keep_order=True)
    except Exception:
        return None
    for _hwnd, _title, phys, minimized in wins:   # 已按 z-order 从最前到后
        if minimized:
            continue
        r = _phys_to_logical(phys)
        if r.contains(pt):
            return r
    return None


# ====================================================================
# 标注图元
# ====================================================================
class _Shape:
    """一条标注意图。kind ∈ rect/arrow/text/mosaic；坐标都是 overlay 全局逻辑坐标。"""

    def __init__(self, kind, **kw):
        self.kind = kind
        self.color = kw.get("color", RED)
        self.width = kw.get("width", DEFAULT_WIDTH)
        self.rect = kw.get("rect")                 # rect/mosaic 用
        self.p1 = kw.get("p1")                     # arrow/line 用
        self.p2 = kw.get("p2")
        self.points = kw.get("points")             # pen 用（自由手绘折线点列表）
        self.pos = kw.get("pos")                   # text 用（左上角）
        self.text = kw.get("text", "")
        self.font_px = kw.get("font_px", 18)


def _draw_shape(p, sh, mosaic_src, origin, sel_top):
    """把一条图元画到 painter 上。

    origin：全局坐标 -> 画布坐标的平移量（画布左上角对应的全局点）；
    sel_top：选区的全局左上角（马赛克源图就是选区裁片，靠它对齐）。
    mosaic_src：选区的像素化副本（QPixmap）。"""
    rel = lambda r: r.translated(-origin.x(), -origin.y()) if isinstance(r, QRect) else r
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if sh.kind in ("rect", "roundrect", "ellipse"):
        pen = QPen(QColor(sh.color)); pen.setWidth(sh.width)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        r = rel(sh.rect)
        if sh.kind == "rect":
            p.drawRect(r)
        elif sh.kind == "ellipse":
            p.drawEllipse(r)
        else:
            rad = max(6, min(r.width(), r.height()) // 6)
            p.drawRoundedRect(r, rad, rad)
    elif sh.kind == "mosaic":
        if mosaic_src is None or sh.rect is None:
            return
        r = rel(sh.rect)
        p.save()
        p.setClipRect(r)
        p.drawPixmap(sel_top.x() - origin.x(), sel_top.y() - origin.y(), mosaic_src)
        p.restore()
        # 补一圈中性灰细边框：贴完像素块后标出马赛克边界，拖拽/出图都能看清「框」
        # （此前只贴像素图不描边，画面均匀处几乎看不出选到了哪）
        border = QPen(QColor("#8F959E")); border.setWidth(1)
        p.setPen(border); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r)
    elif sh.kind in ("arrow", "line"):
        if sh.kind == "line":
            if sh.p1 is not None and sh.p2 is not None:
                pen = QPen(QColor(sh.color)); pen.setWidth(sh.width)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawLine(QPointF(sh.p1.x() - origin.x(), sh.p1.y() - origin.y()),
                           QPointF(sh.p2.x() - origin.x(), sh.p2.y() - origin.y()))
        else:
            _draw_arrow(p, sh.p1, sh.p2, QColor(sh.color), sh.width, origin)
    elif sh.kind == "pen":
        pts = sh.points or []
        if len(pts) >= 2:
            pen = QPen(QColor(sh.color)); pen.setWidth(sh.width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPolyline(QPolygonF([QPointF(pt.x() - origin.x(), pt.y() - origin.y())
                                      for pt in pts]))
    elif sh.kind == "text":
        p.setFont(QFont("Microsoft YaHei", 0, QFont.Weight.Bold))
        f = p.font(); f.setPixelSize(sh.font_px); p.setFont(f)
        p.setPen(QColor(sh.color))
        p.drawText(QPoint(sh.pos.x() - origin.x(), sh.pos.y() - origin.y()), sh.text)


def _draw_arrow(p, a, b, color, width, origin):
    """带三角头箭头：a→b 全局坐标，origin 为画布左上对应的全局点。"""
    if a is None or b is None:
        return
    ra = QPointF(a.x() - origin.x(), a.y() - origin.y())
    rb = QPointF(b.x() - origin.x(), b.y() - origin.y())
    pen = QPen(color); pen.setWidth(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawLine(ra, rb)
    # 箭头头部
    import math
    ang = math.atan2(rb.y() - ra.y(), rb.x() - ra.x())
    head = max(10, width * 4)
    for da in (math.radians(150), math.radians(-150)):
        hx = rb.x() + head * math.cos(ang + da)
        hy = rb.y() + head * math.sin(ang + da)
        p.drawLine(rb, QPointF(hx, hy))


def _pixelate(src: QPixmap, block=MOSAIC_BLOCK) -> QPixmap:
    """缩小再放大做马赛克（FastTransformation 像素感最足）。"""
    if src is None or src.isNull():
        return QPixmap()
    w, h = src.width(), src.height()
    small = src.scaled(max(1, w // block), max(1, h // block),
                       Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.FastTransformation)
    out = small.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.FastTransformation)
    # scaled 会把 devicePixelRatio 抹成 1：马赛克图若按 DPR=1 画进高 DPI 画布，
    # 会被放大 dpr 倍（「马赛克/选区默认放大」的来源）。把源图 DPR 还回去。
    out.setDevicePixelRatio(src.devicePixelRatio() or 1.0)
    return out


def _region_record_options(parent, region):
    """录屏联动前的声音选择小窗：确认可录这块区域 + 勾选内录/麦克风。
    返回 (内录on, 麦克风on)；取消返回 None（回到标注界面继续编辑）。"""
    dlg = QDialog(parent)
    dlg.setWindowTitle("录制这块区域")
    v = QVBoxLayout(dlg)
    v.setContentsMargins(18, 16, 18, 14); v.setSpacing(10)
    tip = QLabel("即将录制选区 %d×%d（屏幕坐标 %d, %d）。\n"
                 "录的是这块区域的实时画面（标注只作用于截图、不进视频）。声音可多选："
                 % (region.width(), region.height(), region.x(), region.y()))
    tip.setWordWrap(True)
    v.addWidget(tip)
    ck_sys = QCheckBox("🔊  内录（系统播放的声音）")
    ck_mic = QCheckBox("🎤  麦克风")
    v.addWidget(ck_sys)
    v.addWidget(ck_mic)
    row = QHBoxLayout()
    row.addStretch(1)
    b_no = QPushButton("取消"); b_no.clicked.connect(dlg.reject)
    b_go = QPushButton("开始录制"); b_go.setDefault(True); b_go.clicked.connect(dlg.accept)
    row.addWidget(b_no); row.addWidget(b_go)
    v.addLayout(row)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return ck_sys.isChecked(), ck_mic.isChecked()


def _region_gif_options(parent, region):
    """录 GIF（偷/录表情包）前的确认小窗：说明录的是实时画面、录完自动转 GIF
    并复制、建议录短一点。确定返回 True（开始录制），取消返回 False。"""
    dlg = QDialog(parent)
    dlg.setWindowTitle("录制这块区域为 GIF")
    v = QVBoxLayout(dlg)
    v.setContentsMargins(18, 16, 18, 14)
    v.setSpacing(10)
    tip = QLabel(
        "即将把选区 %d×%d 的实时画面录下来，停录后自动转成 GIF 并复制，"
        "可直接粘贴到聊天（不录声音）。\n建议只录几秒：越长 GIF 越大。"
        % (region.width(), region.height()))
    tip.setWordWrap(True)
    v.addWidget(tip)
    row = QHBoxLayout()
    row.addStretch(1)
    b_no = QPushButton("取消"); b_no.clicked.connect(dlg.reject)
    b_go = QPushButton("开始录制"); b_go.setDefault(True); b_go.clicked.connect(dlg.accept)
    row.addWidget(b_no); row.addWidget(b_go)
    v.addLayout(row)
    return dlg.exec() == QDialog.DialogCode.Accepted


def _review_ocr_text(parent, text):
    """OCR 结果预览/微编辑窗：识别错的词可以直接改了再复制。
    返回最终文本（可能为空串）；点取消返回 None（回到标注界面继续编辑）。"""
    dlg = QDialog(parent)
    dlg.setWindowTitle("识别结果（可修改后复制）")
    v = QVBoxLayout(dlg)
    v.setContentsMargins(16, 14, 16, 12); v.setSpacing(10)
    v.addWidget(QLabel("识别到以下文字，可直接编辑修正，再点「复制文字」："))
    ed = QPlainTextEdit()
    ed.setPlainText(text)
    f = ed.font(); f.setPointSize(12); ed.setFont(f)
    ed.setMinimumSize(480, 260)
    v.addWidget(ed, 1)
    row = QHBoxLayout(); row.addStretch(1)
    b_no = QPushButton("取消"); b_no.clicked.connect(dlg.reject)
    b_go = QPushButton("📋 复制文字"); b_go.setDefault(True); b_go.clicked.connect(dlg.accept)
    row.addWidget(b_no); row.addWidget(b_go)
    v.addLayout(row)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return ed.toPlainText().strip()


# ====================================================================
# 截图遮罩 + 标注编辑器
# ====================================================================
class ScreenshotOverlay(_MaskBase):
    """冻结桌面之上完成「选区 → 标注 → 出图」的全屏交互。

    两阶段：phase=select 拖拽定框；phase=edit 在框内画标注。
    initial_rect 给非空即跳过拖拽直接进 edit（全屏/窗口模式用）。
    """

    def __init__(self, frozen: QPixmap, box: QRect, parent=None,
                 initial_rect=None, result_holder=None):
        super().__init__(parent)
        self._frozen = frozen
        self._box = box                     # 桌面逻辑几何（overlay 与之重合）
        self._tl = box.topLeft()             # 全局↔控件本地换算锚点
        self._result = result_holder if result_holder is not None else {}
        self._sel = QRect()
        self._phase = "select"
        self._origin_pt = None
        self._hover_win = None          # 选区阶段悬停吸附到的窗口矩形（逻辑坐标）
        self.setMouseTracking(True)     # 选区阶段无按键也要收到 move 才能悬停高亮
        self._shapes = []
        self._redo = []
        self._tool = None
        self._drag_shape = None
        self._color = RED
        self._width = DEFAULT_WIDTH
        self._font_px = 18              # 文字字号（与线宽解耦，可在工具条单独调）
        self._mosaic_src = None
        # 就地文字编辑状态（无子控件版：遮罩自己持缓冲区 + IME 组合串 + 光标）
        self._editing = False
        self._text_buf = ""
        self._text_preedit = ""
        self._text_pos = QPoint()
        self._caret_on = True
        self._bar = None
        self._title_font = QFont("Microsoft YaHei", 9)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._caret_timer = QTimer(self)      # 光标闪烁：只驱动重绘，不做状态判断
        self._caret_timer.setInterval(530)
        self._caret_timer.timeout.connect(self._blink)
        if initial_rect is not None and not initial_rect.isNull():
            self._sel = initial_rect.intersected(box)
            self._enter_edit()

    # ---- 坐标换算（选区/图元统一存全局逻辑坐标；控件绘制/子件定位才转本地）----
    def _to_global(self, local_pt):
        return local_pt + self._tl

    def _to_local(self, global_pt):
        return global_pt - self._tl

    # ---- 生命周期 ----
    def run(self):
        self.exec()                       # 遮罩已按虚拟桌面并集定位（_MaskBase）
        return self._result.get("pixmap")

    def done(self, r):
        """关闭前先把未提交的字落定（“复制/保存/钉住”按钮直接 accept 也不丢字）。"""
        if self._editing:
            self._finish_text()
        super().done(r)

    def _blink(self):
        if self._editing:
            self._caret_on = not self._caret_on
            self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        tl = self._box.topLeft()
        # 1) 铺满冻结桌面底图（frozen 自带 DPR、逻辑尺寸＝box，与控件 1:1）
        p.drawPixmap(0, 0, self._frozen)
        # 2) 全幅压暗
        p.fillRect(self.rect(), QColor(0, 0, 0, 150))
        # 3) 选区内透出原图（高亮）：只用「裁剪 + 位置贴图」，避开 sourceRect 的
        #    DPR 坐标歧义——frozen 平铺在 (0,0)，裁到 local 就正好露出选区那块原图。
        if not self._sel.isNull():
            local = self._sel.translated(-tl.x(), -tl.y())
            p.save()
            p.setClipRect(local)
            p.drawPixmap(0, 0, self._frozen)
            p.restore()
            pen = QPen(QColor(SELECT_BORDER)); pen.setWidth(2)
            p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(local)
            if self._phase == "edit":
                origin = tl
                for sh in self._shapes:
                    _draw_shape(p, sh, self._mosaic_src, origin, self._sel.topLeft())
                if self._drag_shape is not None:      # 拖拽中的实时预览
                    _draw_shape(p, self._drag_shape, self._mosaic_src, origin,
                                self._sel.topLeft())
                if self._editing:                     # 就地输入中的文字 + 光标
                    self._draw_editing_text(p, origin)
            else:
                self._draw_hint(p, local, "拖动框选 · 松手定稿 · Esc 取消")
        # 选区阶段：悬停吸附到一个前台窗口时，透出原图 + 蓝框 + 提示（单击即吸附整窗）
        if (self._phase == "select" and self._sel.isNull()
                and self._hover_win is not None):
            hw = self._hover_win.translated(-tl.x(), -tl.y())
            p.save()
            p.setClipRect(hw)
            p.drawPixmap(0, 0, self._frozen)
            p.restore()
            pen = QPen(QColor(SELECT_BORDER)); pen.setWidth(2)
            p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(hw)
            self._draw_hint(p, hw, "单击吸附此窗口 · 按住拖动自由框选")
        p.end()

    def keyPressEvent(self, e):
        if self._editing and self._text_key(e):
            return
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        if self._phase == "edit":
            if e.key() == Qt.Key.Key_Z and e.modifiers() & Qt.ControlModifier:
                self._undo(); return
            if e.key() == Qt.Key.Key_Y and e.modifiers() & Qt.ControlModifier:
                self._redo_step(); return
        super().keyPressEvent(e)

    # ---- 就地文字编辑（无文本框：字/光标都画在遮罩上，预览与出图同一绘制路径）----
    def _text_key(self, e):
        k = e.key()
        if k in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt,
                 Qt.Key.Key_Meta, Qt.Key.Key_CapsLock):
            return True                       # 纯修饰键：吃掉，不走 Ctrl+Z 等分支
        mods = e.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._finish_text()
            return True
        if k == Qt.Key.Key_Escape:            # 编辑中：Esc 先取消这段字，不关整个遮罩
            self._cancel_text()
            return True
        if k == Qt.Key.Key_Backspace:
            if self._text_preedit:
                self._text_preedit = self._text_preedit[:-1]
            elif self._text_buf:
                self._text_buf = self._text_buf[:-1]
            self._restart_caret(); self.update()
            return True
        if not mods and e.text():
            ch = e.text()
            if not (ord(ch[0]) < 32 or ord(ch[0]) == 127):
                self._text_buf += ch          # 英文/数字等直路字符（IME 组合另走
                self._restart_caret(); self.update()  # inputMethodEvent）
            return True
        return False

    def inputMethodEvent(self, e):
        """中文等 IME 输入：组合串（带下划线的半成品）叠在已提交字后面显示，
        上屏即并进缓冲区——全程没有子控件，不存在“框在哪、字在哪”的两套坐标。"""
        if self._editing and isinstance(e, QInputMethodEvent):
            self._text_buf += e.commitString()
            self._text_preedit = e.preeditString()
            self._restart_caret()
            self.update()
            e.accept()
            return
        super().inputMethodEvent(e)

    def inputMethodQuery(self, query):
        """把候选窗领到光标处：编辑中报光标矩形，其余交基类。"""
        if (self._editing and query.parameter() ==
                Qt.InputMethodQuery.ImCursorRect):
            fr = self._text_font()
            fm = QFontMetrics(fr)
            pos = self._to_local(self._text_pos)
            return QRectF(pos.x() + fm.horizontalAdvance(self._full_text()),
                          pos.y() - fm.ascent(), 1, fm.height())
        return super().inputMethodQuery(query)

    def _full_text(self):
        return self._text_buf + self._text_preedit

    def _restart_caret(self):
        self._caret_on = True
        if not self._caret_timer.isActive():
            self._caret_timer.start()

    def _text_font(self):
        f = QFont("Microsoft YaHei", 0, QFont.Weight.Bold)
        f.setPixelSize(self._font_size())
        return f

    def _finish_text(self):
        """提交这段字（预览怎么画的，图元就怎么画，同一基线同一字体）。"""
        txt = self._full_text()
        self._editing = False
        self._caret_timer.stop()
        self._text_buf = self._text_preedit = ""
        if txt.strip():
            self._shapes.append(_Shape("text", pos=self._text_pos, text=txt,
                                       color=self._color, font_px=self._font_size()))
            self._redo.clear()
        self.update()

    def _cancel_text(self):
        self._editing = False
        self._caret_timer.stop()
        self._text_buf = self._text_preedit = ""
        self.update()

    def _draw_editing_text(self, p, origin):
        """把编辑中的文字 + 闪烁光标画在光标点上（与 _draw_shape 的 text 同基线）。"""
        fr = self._text_font()
        p.setFont(fr)
        p.setPen(QColor(self._color))
        pos = QPoint(self._text_pos.x() - origin.x(),
                     self._text_pos.y() - origin.y())
        txt = self._full_text()
        if txt:
            p.drawText(pos, txt)
        if self._caret_on:
            fm = QFontMetrics(fr)
            cx = pos.x() + fm.horizontalAdvance(txt)
            pen = QPen(QColor(self._color)); pen.setWidth(2)
            p.setPen(pen)
            p.drawLine(cx, pos.y() - fm.ascent(), cx, pos.y() + fm.descent())

    # ---- 选区阶段 ----
    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._phase == "select":
            self._origin_pt = self._to_global(e.position().toPoint())
            self._sel = QRect(self._origin_pt, self._origin_pt)
            self.update()
            return
        # edit 阶段：按当前工具起一个图元
        pt = self._to_global(e.position().toPoint())
        if not self._sel.contains(pt):
            return
        if self._editing:
            self._finish_text()
        if self._tool == "text":
            self._begin_text(pt)
        elif self._tool in _LINE_TOOLS:
            self._drag_shape = self._new_line(self._tool, pt)
            self.update()
        elif self._tool == "pen":
            self._drag_shape = self._new_pen(pt)
            self.update()
        elif self._tool in _BOX_TOOLS:
            self._drag_shape = self._new_box(self._tool, pt)
            self.update()

    def mouseMoveEvent(self, e):
        pt = self._to_global(e.position().toPoint())
        if self._phase == "select":
            if self._origin_pt is not None:          # 已按下：自由拖框
                self._sel = QRect(self._origin_pt, pt).normalized()
                if self._hover_win is not None:
                    self._hover_win = None
                self.update()
            else:                                    # 未按下：悬停探测可吸附的前台窗口
                hw = _window_rect_at(pt)
                if hw != self._hover_win:
                    self._hover_win = hw
                    self.update()
        elif self._drag_shape is not None:
            sh = self._drag_shape
            if sh.kind in _LINE_TOOLS:
                sh.p2 = pt
            elif sh.kind == "pen":
                sh.points.append(pt)
            else:
                sh.rect = QRect(sh._start, pt).normalized()
            self.update()        # 边拖边画：实时看到框/箭头/直线/画笔/马赛克的样子

    def mouseReleaseEvent(self, e):
        if self._phase == "select":
            self._origin_pt = None
            if self._sel.width() >= MIN_DRAG and self._sel.height() >= MIN_DRAG:
                self._enter_edit()
                return
            # 近乎零位移＝单击：吸附到指针下的窗口（没吸到则清空重来）
            click_pt = self._to_global(e.position().toPoint())
            hw = self._hover_win if self._hover_win is not None \
                else _window_rect_at(click_pt)
            if hw is not None and not hw.isNull():
                self._sel = hw.intersected(self._box)
                self._hover_win = None
                self._enter_edit()
            else:
                self._sel = QRect(); self.update()
            return
        if self._drag_shape is not None:
            self._commit_drag()
            self._drag_shape = None
            self.update()

    def _new_box(self, kind, pt):
        sh = _Shape(kind, color=self._color, width=self._width)
        sh.rect = QRect(pt, pt); sh._start = pt
        return sh

    def _new_line(self, kind, pt):
        """箭头/直线：两点成段（按下点为起点，拖到终点）。"""
        sh = _Shape(kind, color=self._color, width=self._width)
        sh.p1 = pt; sh.p2 = pt; sh._start = pt
        return sh

    def _new_pen(self, pt):
        """画笔：自由手绘折线，拖动逐点记录。"""
        sh = _Shape("pen", color=self._color, width=self._width)
        sh.points = [pt]; sh._start = pt
        return sh

    def _commit_drag(self):
        sh = self._drag_shape
        if sh is None:
            return
        if sh.kind in _LINE_TOOLS:
            if (sh.p2 - sh.p1).manhattanLength() < 4:
                return
        elif sh.kind == "pen":
            if len(sh.points or []) < 2:
                return
        else:
            if sh.rect is None:
                return
            sh.rect = sh.rect.normalized()
            if sh.rect.width() < 3 or sh.rect.height() < 3:
                return
        self._shapes.append(sh)
        self._redo.clear()

    def _crop(self, rect: QRect) -> QPixmap:
        """从冻结底图按全局逻辑矩形裁一块。

        只用「位置贴图」（drawPixmap 只给左上角，不给 sourceRect），规避
        sourceRect 在高 DPI 下的坐标系歧义：out 逻辑尺寸＝rect，把 frozen
        平移 ( _tl - rect ) 贴上，out 天然裁剪出 rect 那块。"""
        dpr = self._frozen.devicePixelRatio() or 1.0
        out = QPixmap(int(rect.width() * dpr), int(rect.height() * dpr))
        out.setDevicePixelRatio(dpr)
        out.fill(Qt.GlobalColor.transparent)
        p = QPainter(out)
        p.drawPixmap(QPoint(self._tl.x() - rect.x(), self._tl.y() - rect.y()),
                     self._frozen)
        p.end()
        return out

    # ---- 编辑阶段 ----
    def _enter_edit(self):
        self._phase = "edit"
        self._mosaic_src = _pixelate(self._crop(self._sel))
        self._build_toolbar()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def _reselect(self):
        """回到选区阶段重画一版。"""
        self._phase = "select"
        self._shapes.clear(); self._redo.clear()
        self._tool = None
        self._kill_toolbar()
        self.update()

    def _begin_text(self, pt):
        """点一下就在点上开始打字：没有子控件、没有框，只有光标和彩色字；
        落点即基线，预览怎么显、出图就怎么画（同一套坐标，不会再错位）。"""
        if self._editing:
            self._finish_text()
        self._editing = True
        self._text_buf = ""
        self._text_preedit = ""
        self._text_pos = pt
        self._restart_caret()
        self.setFocus(Qt.FocusReason.OtherFocusReason)   # 子控件(工具条)可能持有焦点，抢回来收键盘
        self.update()

    def _font_size(self):
        return self._font_px

    def _undo(self):
        if self._shapes:
            self._redo.append(self._shapes.pop())
            self.update()

    def _redo_step(self):
        if self._redo:
            self._shapes.append(self._redo.pop())
            self.update()

    # ---- 工具条 ----
    def _build_toolbar(self):
        self._kill_toolbar()
        bar = QWidget(self)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(6, 4, 6, 4); lay.setSpacing(4)

        # 各工具不再“一个按钮一个”：框线/箭头做成下拉分组，画笔/文字/马赛克保留单键
        self._tool_btns = {}        # 单按钮工具 kind -> QPushButton
        self._tool_actions = {}     # 下拉组里的工具 kind -> QAction
        self._groups = {}           # 组按钮 -> 该组包含的 kind 集合

        # 框线（下拉：矩形 / 圆角矩形 / 椭圆）
        self._grp(lay, "框线", (("rect", "▭ 矩形"),
                                ("roundrect", "▢ 圆角矩形"),
                                ("ellipse", "◯ 椭圆")))
        # 箭头（下拉：箭头 / 直线，都是两点成段）
        self._grp(lay, "箭头", (("arrow", "↗ 箭头"),
                                ("line", "╱ 直线")))
        # 画笔 / 文字 / 马赛克：各自一个可切换按钮（画笔=P、马赛克=M，鼠标悬停看汉字）
        for kind, glyph, tip in (("pen", "P", "画笔：按住拖动自由手绘"),
                                 ("text", "T", "文字：点一下在选区里直接打字"),
                                 ("mosaic", "M", "马赛克：拖一块覆盖并像素化")):
            b = QPushButton(glyph); b.setObjectName("ShotTool")
            b.setToolTip(tip); b.setCheckable(True); b.setFixedWidth(30)
            b.clicked.connect(lambda _c=False, k=kind: self._pick_tool(k))
            lay.addWidget(b)
            self._tool_btns[kind] = b

        lay.addSpacing(6)
        cb = QPushButton("🎨"); cb.setObjectName("ShotTool")
        cb.setToolTip("标注颜色（框线/箭头/直线/画笔/文字通用）")
        cb.setFixedWidth(30); cb.clicked.connect(self._pick_color)
        lay.addWidget(cb)
        # 粗细下拉（框线/箭头/直线/画笔的线宽）
        self._choice_btn(lay, "粗细", [(w, "%d px" % w) for w in (2, 3, 5, 8, 12)],
                         self._width, self._set_width, "width")
        # 字号下拉（仅文字用；与线宽解耦，可单独调）
        self._choice_btn(lay, "字号", [(s, "%d 号" % s) for s in (14, 18, 24, 32, 48)],
                         self._font_px, self._set_font_px, "font")

        lay.addSpacing(6)
        for text, slot in (("撤销", self._undo), ("重做", self._redo_step),
                           ("重选", self._reselect)):
            b = QPushButton(text); b.setObjectName("ShotTool"); b.clicked.connect(slot)
            lay.addWidget(b)

        lay.addStretch(1)
        for text, slot, obj in (
                ("✖ 取消", self.reject, "ShotCancel"),
                ("🎞 GIF", self._gif, "ShotGo"),
                ("🎬 录屏", self._record, "ShotGo"),
                ("🔤 取字", self._ocr, "ShotGo"),
                ("💾 保存", self._save, "ShotGo"),
                ("📌 钉住", self._pin, "ShotGo"),
                ("✅ 复制", self._copy, "ShotDone")):
            b = QPushButton(text); b.setObjectName(obj); b.clicked.connect(slot)
            lay.addWidget(b)

        bar.setStyleSheet(tokenize(_TOOLBAR_QSS))
        self._refresh_tool_ui()
        bar.adjustSize()
        self._place_bar(bar)
        bar.show()
        self._bar = bar

    def _grp(self, lay, title, items):
        """一个下拉分组按钮：点开菜单选具体样式，选中项打勾、按钮高亮表示该组正被使用。"""
        b = QToolButton(); b.setObjectName("ShotTool"); b.setText(title)
        b.setToolTip("%s：点右侧箭头选择具体样式" % title)
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        menu = QMenu(b)
        kinds = set()
        for kind, label in items:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.triggered.connect(lambda _c=False, k=kind: self._pick_tool(k))
            self._tool_actions[kind] = act
            kinds.add(kind)
        b.setMenu(menu)
        self._groups[b] = kinds
        lay.addWidget(b)
        return b

    def _choice_btn(self, lay, title, pairs, current, setter, which):
        """数值下拉（粗细/字号）：菜单里每项勾选跟随当前值，按钮标题带上现值。"""
        b = QToolButton(); b.setObjectName("ShotTool")
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        b.setToolTip("%s：点右侧箭头选择" % title)
        menu = QMenu(b)
        acts = {}
        for val, label in pairs:
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(val == current)
            act.triggered.connect(lambda _c=False, v=val, s=setter: s(v))
            acts[val] = act
        b.setMenu(menu)
        b.setText("%s %s" % (title, current))
        lay.addWidget(b)
        setattr(self, "_%s_btn" % which, b)
        setattr(self, "_%s_acts" % which, acts)
        setattr(self, "_%s_title" % which, title)
        return b

    def _refresh_tool_ui(self):
        """把当前工具反映到按钮/菜单：单键勾选、组内菜单项勾选、组按钮整体高亮。"""
        for k, btn in getattr(self, "_tool_btns", {}).items():
            btn.setChecked(k == self._tool)
        for k, act in getattr(self, "_tool_actions", {}).items():
            act.setChecked(k == self._tool)
        for btn, kinds in getattr(self, "_groups", {}).items():
            btn.setProperty("on", "1" if self._tool in kinds else "")
            st = btn.style(); st.unpolish(btn); st.polish(btn)

    def _place_bar(self, bar):
        """工具条尽量放选区下方，越界则放上方/贴边（全用控件本地坐标：bar 是子件）。"""
        sel = self._sel.translated(-self._tl.x(), -self._tl.y())
        rw, rh = self.width(), self.height()
        bw, bh = bar.width(), bar.height()
        x = sel.right() - bw
        y = sel.bottom() + 8
        if y + bh > rh:
            y = sel.bottom() - bh - 8
            if y < 0:
                y = rh - bh - 4
        x = max(0, min(x, rw - bw))
        bar.move(x, max(0, y))

    def _pick_tool(self, kind):
        if self._editing:
            self._finish_text()
        self._tool = None if self._tool == kind else kind
        self._refresh_tool_ui()
        self.setCursor(Qt.CursorShape.CrossCursor if self._tool
                       else Qt.CursorShape.ArrowCursor)

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self._color), self, "标注颜色")
        if c.isValid():
            self._color = c.name()
            self.update()

    def _set_width(self, v):
        self._width = v
        for w, act in self._width_acts.items():
            act.setChecked(w == v)
        self._width_btn.setText("%s %d" % (self._width_title, v))
        self.update()

    def _set_font_px(self, v):
        self._font_px = v
        for s, act in self._font_acts.items():
            act.setChecked(s == v)
        self._font_btn.setText("%s %d" % (self._font_title, v))
        self.update()

    def _kill_toolbar(self):
        if self._bar is not None:
            self._bar.deleteLater(); self._bar = None

    # ---- 出图 ----
    def render(self) -> QPixmap:
        """裁剪选区原图 + 叠加标注，返回结果图（逻辑尺寸，带 DPR）。"""
        crop = self._crop(self._sel)
        if self._shapes:
            mosaic = _pixelate(crop)
            p = QPainter(crop)
            tl = self._sel.topLeft()
            for sh in self._shapes:
                _draw_shape(p, sh, mosaic, tl, tl)
            p.end()
        return crop

    def _copy(self):
        pm = self.render()
        try:
            from gui import clipboard_hist
            clipboard_hist.manager.note_external_copy(pm)   # 先入库并置抑制，再写剪贴板
        except Exception:
            pass
        QApplication.clipboard().setPixmap(pm)
        self._result["pixmap"] = pm
        self._result["action"] = "copy"
        self.accept()

    def _save(self):
        pm = self.render()
        self._save_pixmap(pm)
        self._result["pixmap"] = pm
        self._result["action"] = "save"
        self.accept()

    def _save_pixmap(self, pm):
        name = "截图_%s.png" % datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(self, "保存截图",
                                              os.path.join(DOWNLOAD_DIR, name),
                                              "PNG 图片 (*.png)")
        if path:
            pm.save(path, "PNG")
            self._result["saved"] = path

    def _pin(self):
        pm = self.render()
        self._result["pixmap"] = pm
        self._result["action"] = "pin"
        self._result["pin_at"] = self._sel.topLeft()
        self.accept()

    def _record(self):
        """录屏联动：把当前选区交给「屏幕录制」面板，可选内录/麦克风，直接开录。
        录的是这块区域的实时画面（标注不进视频）；真正开录在 capture() 收尾、
        宿主窗恢复后派发（遮罩此刻还是模态，不能在里头上面板开录）。"""
        if self._editing:
            self._finish_text()
        opts = _region_record_options(self, self._sel)
        if opts is None:
            return                       # 取消：留在标注界面继续编辑
        sys_on, mic_on = opts
        self._result["action"] = "record"
        self._result["region"] = QRect(self._sel)
        self._result["sys_on"] = sys_on
        self._result["mic_on"] = mic_on
        self.accept()

    def _gif(self):
        """录成 GIF（偷/录表情包）：把当前选区交给屏幕录制面板，录这块区域的
        实时画面，录完自动转成 GIF 并复制进剪贴板（可直接粘贴到聊天）。GIF 不录声音。
        真正开录在 capture() 收尾、宿主窗恢复后派发（遮罩此刻还是模态）。"""
        if self._editing:
            self._finish_text()
        if not _region_gif_options(self, self._sel):
            return                       # 取消：留在标注界面继续编辑
        self._result["action"] = "record"
        self._result["gif"] = True
        self._result["region"] = QRect(self._sel)
        self._result["sys_on"] = False
        self._result["mic_on"] = False
        self.accept()

    def _ocr(self):
        """识图取字：对选区里的屏幕文字做本地 OCR（无 token），弹预览窗可
        改后复制进剪贴板历史。OCR 引擎未装/没识别到字都给明确提示，不静默失败。
        识别失败绝不能把遮罩带崩：引擎层已吞异常，这里只处理“没字/未装”。"""
        if self._editing:
            self._finish_text()
        from video_text_tools import ocr
        if not ocr.available():
            QMessageBox.information(
                self, "未安装 OCR 组件",
                "截图取字需要一个本地 OCR 引擎（二选一，装了即时生效，无需重启）：\n"
                "· Windows 内置 OCR：pip install winocr（几乎不增体积，中文效果中等，"
                "需系统装「中文 OCR 语言包」）；\n"
                "· RapidOCR：pip install rapidocr_onnxruntime（中文更准，但体积更大）。\n\n"
                "详见 requirements-ocr.txt。")
            return
        pm = self._crop(self._sel)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            text = ocr.recognize(pm)
        finally:
            QApplication.restoreOverrideCursor()
        if not text:
            QMessageBox.information(self, "识图取字", "这块区域没识别到文字（或字太糊）。")
            return
        final = _review_ocr_text(self, text)
        if not final:
            return                       # 取消/清空：留在标注界面继续编辑
        try:
            from gui import clipboard_hist
            clipboard_hist.manager.note_external_copy_text(final)   # 先入库再写剪贴板
        except Exception:
            pass
        QApplication.clipboard().setText(final)
        self._result["action"] = "copy_text"
        self._result["text"] = final
        self.accept()


_TOOLBAR_QSS = (
    "#ShotTool { background:transparent; color:#E6E8EB; border:none;"
    " border-radius:5px; padding:3px 6px; font-size:13px; }"
    "#ShotTool:hover { background:#3A404B; }"
    "#ShotTool:checked, #ShotTool[on=\"1\"] { background:#3370FF; color:#FFFFFF; }"
    "QToolButton#ShotTool::menu-indicator { width:10px; }"
    "#ShotCancel { background:transparent; color:#FF8A80; border:none;"
    " border-radius:5px; padding:3px 8px; }"
    "#ShotGo { background:#3A404B; color:#FFFFFF; border:none;"
    " border-radius:5px; padding:3px 10px; }"
    "#ShotDone { background:#3370FF; color:#FFFFFF; border:none;"
    " border-radius:5px; padding:3px 10px; }"
)


# ====================================================================
# 钉住悬浮窗
# ====================================================================
class PinWindow(QWidget):
    """置顶无边框悬浮图：拖动移动、滚轮缩放、右下角改大小、右键菜单、双击/Esc 关闭。"""

    _instances = []          # 持引用防 GC（钉住的图可能没父窗口）

    MIN_SCALE = 0.1
    MAX_SCALE = 6.0

    def __init__(self, pm: QPixmap, at: QPoint = None, parent=None):
        super().__init__(parent)
        self._pm = pm
        self._scale = 1.0
        self._drag_off = None
        self._resizing = False
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._label = QLabel(self)
        self._label.setFrameShape(QLabel.Shape.NoFrame)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._label)
        self._resize_display()
        if at is not None:
            self.move(at)
        self.show(); self.raise_()
        PinWindow._instances.append(self)
        self.destroyed.connect(lambda: self._drop_ref(self))

    @staticmethod
    def _drop_ref(win):
        if win in PinWindow._instances:
            PinWindow._instances.remove(win)

    def _dpr(self):
        return self._pm.devicePixelRatio() or 1.0

    def _base_size(self):
        d = self._dpr()
        return int(self._pm.width() / d), int(self._pm.height() / d)

    def _resize_display(self):
        bw, bh = self._base_size()
        w, h = max(24, int(bw * self._scale)), max(24, int(bh * self._scale))
        self.resize(w, h)
        dpr = self._dpr()
        self._label.setPixmap(self._pm.scaled(
            w * dpr, h * dpr, Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self._label.setScaledContents(True)

    def set_scale(self, s):
        self._scale = max(self.MIN_SCALE, min(self.MAX_SCALE, s))
        self._resize_display()

    # 交互
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            pos = e.position().toPoint()
            if self._near_grip(pos):
                self._resizing = True
            else:
                self._drag_off = pos
        elif e.button() == Qt.MouseButton.RightButton:
            self._menu(e)

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        if self._resizing:
            dpr = self._dpr()
            self.resize(max(24, pos.x()), max(24, pos.y()))
            self._label.setPixmap(self._pm.scaled(
                int(self.width() * dpr), int(self.height() * dpr),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            self._label.setScaledContents(True)
        elif self._drag_off is not None:
            self.move(e.globalPosition().toPoint() - self._drag_off)
        else:
            self.setCursor(Qt.CursorShape.SizeFDiagCursor
                           if self._near_grip(pos) else Qt.CursorShape.OpenHandCursor)

    def mouseReleaseEvent(self, e):
        self._drag_off = None; self._resizing = False

    def mouseDoubleClickEvent(self, e):
        self.close()

    def _near_grip(self, pos):
        return (pos.x() > self.width() - 16 and pos.y() > self.height() - 16)

    def wheelEvent(self, e):
        factor = 1.1 if e.angleDelta().y() > 0 else 1 / 1.1
        self.set_scale(self._scale * factor)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setPen(QPen(QColor("#3A404B"), 1))
        p.setBrush(QColor(0, 0, 0, 0))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def _menu(self, e):
        m = QMenu(self)
        m.addAction("复制到剪贴板", self._copy)
        m.addAction("保存为 PNG…", self._save)
        op = m.addMenu("透明度")
        for pct in (100, 85, 70, 50, 30):
            act = op.addAction("%d%%" % pct)
            act.triggered.connect(
                lambda _c=False, v=pct: self.setWindowOpacity(v / 100.0))
        m.addSeparator()
        m.addAction("关闭", self.close)
        m.exec(e.globalPosition().toPoint())

    def _copy(self):
        try:
            from gui import clipboard_hist
            clipboard_hist.manager.note_external_copy(self._pm)   # 先入库再写，避免重复
        except Exception:
            pass
        QApplication.clipboard().setPixmap(self._pm)

    def _save(self):
        name = "截图_%s.png" % datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(self, "保存截图",
                                              os.path.join(DOWNLOAD_DIR, name),
                                              "PNG 图片 (*.png)")
        if path:
            self._pm.save(path, "PNG")


# ====================================================================
# 顶层入口：三种抓取
# ====================================================================
def capture(mode, parent=None):
    """执行一次截图。mode ∈ region/full/window。返回结果 dict（含 pixmap/action）。

    先隐藏宿主窗口再冻结桌面，免得把自家面板截进底图；结束（含取消）后恢复。
    action==record 时，等宿主窗恢复可见后再把选区交给录屏面板（见 _dispatch_region_record）。
    """
    win = parent.window() if parent is not None else None
    restore = win is not None and win.isVisible()
    if win is not None:
        win.hide()
        QApplication.processEvents()
        time.sleep(0.12)                # 等窗体真的从屏幕上消失再抓
    holder = {}
    try:
        frozen, box = freeze_desktop()
        if frozen is not None and not frozen.isNull():
            ov = None
            if mode == "full":
                rect = _primary_screen_box()
                ov = ScreenshotOverlay(frozen, box, parent, initial_rect=rect,
                                       result_holder=holder)
            elif mode == "window":
                picked = WindowPicker.pick(parent)
                if picked is not None:
                    _hwnd, _title, logical = picked
                    ov = ScreenshotOverlay(frozen, box, parent, initial_rect=logical,
                                           result_holder=holder)
            else:
                ov = ScreenshotOverlay(frozen, box, parent, result_holder=holder)
            if ov is not None:
                pm = ov.run()
                if pm is not None and not pm.isNull() and holder.get("action") == "pin":
                    PinWindow(pm, holder.get("pin_at"))
    finally:
        if restore:
            win.show()
    # 录屏联动：宿主窗已恢复可见，这拍再把选区交给屏幕录制面板开录
    if holder.get("action") == "record":
        _dispatch_region_record(win, holder)
    return holder


def _dispatch_region_record(win, holder):
    """把截图选区交给主窗口→屏幕录制面板开录（主窗提供 record_region）。"""
    fn = getattr(win, "record_region", None)
    if fn is None:
        return
    try:
        fn(holder["region"], holder.get("sys_on", False), holder.get("mic_on", False),
           gif=holder.get("gif", False))
    except Exception:
        pass


# ====================================================================
# 工具卡片面板
# ====================================================================
class ScreenshotPanel(QWidget):
    """「截图」工具卡片：三个抓取按钮 + 说明。真正交互在遮罩里。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 8, 18, 14); root.setSpacing(12)
        from gui.header import page_header
        root.addWidget(page_header("截图",
                    "区域 / 全屏 / 窗口截图：框线·箭头·直线·画笔·文字·马赛克标注，可钉住悬浮"))
        row = QHBoxLayout()
        for text, mode in (("🔲 区域截图", "region"), ("🖥 全屏截图", "full"),
                           ("🪟 窗口截图", "window")):
            b = QPushButton(text)
            b.setObjectName("GhostBtn")
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.clicked.connect(lambda _c=False, m=mode: capture(m, self))
            row.addWidget(b)
        root.addLayout(row)

        # 全局快捷键：就在本面板里配（与「设置→托盘与全局快捷键」同一份存储，
        # 保存即通过 tools_registry 重注册，无需重启）
        from gui.tools_registry import screenshot_shortcut
        hr = QHBoxLayout()
        hr.setSpacing(8)
        hr.addWidget(QLabel("全局快捷键"))
        self.key_edit = QKeySequenceEdit(QKeySequence(screenshot_shortcut()))
        self.key_edit.setFixedWidth(220)
        hr.addWidget(self.key_edit)
        b_save = QPushButton("保存")
        b_save.setObjectName("GhostBtn")
        b_save.clicked.connect(self._save_hotkey)
        hr.addWidget(b_save)
        b_default = QPushButton("恢复默认")
        b_default.setObjectName("GhostBtn")
        b_default.clicked.connect(self._reset_hotkey)
        hr.addWidget(b_default)
        self.lbl_hotkey = QLabel("")
        self.lbl_hotkey.setObjectName("PageTip")
        hr.addWidget(self.lbl_hotkey, 1)
        root.addLayout(hr)
        self._refresh_hotkey_status()
        self.key_edit.keySequenceChanged.connect(
            lambda _s=None: self._set_hotkey_text("#8F959E", "点「保存」生效"))
        tip = QLabel("框选后拖动标注：工具条按类分组——「框线」下拉选矩形/圆角/椭圆，「箭头」下拉选箭头/直线，"
                     "P 画笔自由手绘，T 文字、M 马赛克（悬停显汉字）；🎨 调色、粗细下拉选线宽、字号下拉选文字大小（文字与线宽互不影响）；"
                     "选中后在选区里拖画/点击即实时预览，Ctrl+Z 撤销。完成可「钉住」置顶悬浮、「复制」进剪贴板、「保存」为 PNG、「🎞 GIF」把这块区域的实时画面录几秒、停录后自动转成 GIF 并复制（偷/录表情包，可直接粘贴到聊天）、「🎬 录屏」把这块区域直接录成视频（可选内录/麦克风），或「🔤 取字」把选区里的屏幕文字本地 OCR 出来（无 token，编辑后可复制）。全局快捷键在上方直接配，保存即生效。")
        tip.setObjectName("PageTip"); tip.setWordWrap(True)
        root.addWidget(tip)
        root.addStretch(1)

    # ---- 全局快捷键配置（存 ui_state，与设置页同源；保存即重注册）----
    def _save_hotkey(self):
        from gui.tools_registry import set_screenshot_shortcut
        seq = self.key_edit.keySequence().toString()
        if seq and global_hotkey.parse_sequence(seq) is None:
            self._set_hotkey_text("#FF8D19", "⚠ 组合键没按完（只有修饰键），旧键位不变")
            return
        set_screenshot_shortcut(seq)         # 空串＝停用一键截图（tools_registry 顺手释键）
        self._refresh_hotkey_status()

    def _reset_hotkey(self):
        from gui.tools_registry import set_screenshot_shortcut, SCREENSHOT_DEFAULT
        set_screenshot_shortcut(SCREENSHOT_DEFAULT)
        self.key_edit.setKeySequence(QKeySequence(SCREENSHOT_DEFAULT))
        self._refresh_hotkey_status()

    def _refresh_hotkey_status(self):
        from gui.tools_registry import screenshot_shortcut
        seq = screenshot_shortcut()
        if not seq:
            self._set_hotkey_text("#8F959E", "已停用一键截图；想恢复点「恢复默认」")
            return
        hk = global_hotkey.manager
        if not hk.available():
            self._set_hotkey_text("#FF8D19", "⚠ %s（非 Windows，只在窗口内生效）" % seq)
        elif global_hotkey_enabled() and SCREENSHOT_TOKEN in hk.bound_tokens():
            self._set_hotkey_text("#3370FF", "✓ %s 已注册为系统级：焦点在其它软件里也能一键截图" % seq)
        elif seq in dict(hk.failures):
            self._set_hotkey_text("#FF8D19", "⚠ %s 被其它软件占了（换微信/QQ 截图键外的一组）" % seq)
        else:
            self._set_hotkey_text("#8F959E", "%s（未开全局生效，只在本软件窗口内认）" % seq)

    def _set_hotkey_text(self, color, text):
        self.lbl_hotkey.setText(text)
        self.lbl_hotkey.setStyleSheet(tokenize(
            "color:%s; font-size:12px; background:transparent;" % color))
