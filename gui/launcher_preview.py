"""
gui/launcher_preview.py —— 唤出面板右侧的大面积预览窗 + 网格卡缩略图后台服务

设计要点：
- 预览随"当前选中项"更新：图片解码、视频抽首帧（复用 gui.thumb_cache 的 ffmpeg 管道）、
  代码/纯文本读文件、Office/PDF 用 core.fileindex.extract_text 抽正文，其余一律"无法预览"；
- 抽取一律放后台线程：95 万行库里点开一张大图 / 一个几百页 PDF 都不该把面板卡住。
  跨线程只走信号（普通 Python 线程没有 Qt 事件循环，QTimer.singleShot 在那儿永不触发，
  这是唤出面板搜索回程踩过的坑，这里绝不再犯）；
- 带序号守卫：连续切换选中项时，慢回包不能盖掉快回包（与搜索 _seq 同一套路）；
- 预览窗本身不解析 HTML / 脑图（P3 再挂页面），这里先把"文本 / 图片 / 无法预览"三态做稳。

⚠ 线程纪律（这条被踩过一次，崩得极难看，别再犯）：后台线程里**绝对不许**碰
QPainter / QFont / QFontMetrics —— Qt 的字体库只允许 GUI 线程构造，在工作线程
里 drawText 会直接 qFatal("Cannot use a QPainter ...")→ abort → 0xc0000409，
Python 层只留下一句 "Fatal Python error: segfault"，日志里查不到任何线索。
（本机 minidump 实测栈：Qt6Gui!QFontDatabasePrivate::ensureFontDatabase ←
  QPainter::drawText ← 工作线程，就是 render_doc_thumb 被丢进了 ImgWorker.run。）
QImage 的解码/构造是允许的（QPixmap 才是 GUI 线程专属），所以"抽正文"照旧在
后台做，"画纸样"以 DocCard 回抛主线程再画。QThread 同理见 ImgWorker 的说明。
"""
import os
import threading

from PySide6.QtCore import Qt, QThread, QObject, QSize, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
                               QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QSizePolicy, QStackedWidget, QTextBrowser, QToolButton,
                               QVBoxLayout, QWidget)

#: 测试里置 True：预览同步计算，不等后台线程（离屏测试等不到线程回包）
_SYNC_PREVIEW = False

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv", ".mpg"}
DOC_EXTS = {".docx", ".xlsx", ".pptx", ".pdf"}
HTML_EXTS = {".html", ".htm"}
TEXT_EXTS = {
    ".txt", ".md", ".log", ".rst", ".json", ".xml", ".csv", ".tsv",
    ".py", ".js", ".ts", ".css", ".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml",
    ".java", ".c", ".cpp", ".h", ".hpp", ".go", ".rs", ".rb", ".php", ".sh", ".bat",
    ".sql", ".kt", ".swift",
}
#: 代码类用等宽字体显示
MONO_EXTS = TEXT_EXTS - {".txt", ".md", ".log", ".csv", ".tsv"}
#: 文本预览截断：再大也只读这么多字符（预览"大致内容"，不是全文查看器）
_TEXT_CAP = 256 * 1024
#: PDF 首页渲染上限（长边像素）：预览给清晰一点，网格缩略图够看清即可。
#: 超上限就等比缩，既看得清又不至于把一本大开本手册撑成几十 MB 位图。
_PDF_PREVIEW_MAX = 1600
_PDF_THUMB_MAX = 512
#: Markdown 走"文本/渲染/脑图"三态；HTML 走"渲染/源码"两态。按扩展名分派。
MD_EXTS = {".md", ".markdown"}
#: 每种特殊类型给哪些模式按钮（key, 中文标签）；默认模式另存一份
_MODE_LABELS = {
    "md": (("text", "文本"), ("render", "渲染"), ("mindmap", "脑图")),
    "html": (("render", "渲染"), ("source", "源码")),
}
_MODE_DEFAULT = {"md": "render", "html": "render"}


def _ext(path):
    return os.path.splitext(str(path or ""))[1].lower()


def category(path):
    """按扩展名归类：image/video/doc/html/text/other。"""
    e = _ext(path)
    if e in IMAGE_EXTS:
        return "image"
    if e in VIDEO_EXTS:
        return "video"
    if e in DOC_EXTS:
        return "doc"
    if e in HTML_EXTS:
        return "html"
    if e in TEXT_EXTS:
        return "text"
    return "other"


def _pv_category(path):
    """预览语义归类：比 category 多把 Markdown 单拎成 "md"（html 已由 category 给出）。"""
    if _ext(path) in MD_EXTS:
        return "md"
    return category(path)


def gather_data(path, mtime=None, kind=None):
    """给 PreviewPane 取预览载荷：(kind, data)。image/video→QImage；
    text/doc/md/html→原始文本 str；其余→("none", None)。后台线程调用。"""
    kind = kind or _pv_category(path)
    if kind == "image":
        return "image", load_image(path)
    if kind == "video":
        return "image", load_video_thumb(path, mtime)
    if kind == "doc":
        return _doc_preview(path)
    if kind in ("text", "md", "html"):
        return kind, read_text(path)
    return "none", None


def read_text(path, cap=_TEXT_CAP):
    """读纯文本/代码：utf-8 → gbk → latin-1(errors=replace) 依次兜底，截断到 cap 字符。
    任何异常返回 ""（绝不因为一个坏文件把预览崩掉）。"""
    try:
        with open(str(path), "rb") as f:
            raw = f.read(cap + 4096)
    except OSError:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)[:cap]
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")[:cap]


def read_doc_text(path):
    """Office/PDF 正文：走索引层现成抽取器（懒导入，抽不到返回 ""）。"""
    try:
        from core import fileindex
        return fileindex.extract_text(path) or ""
    except Exception:
        return ""


def load_image(path):
    """同步解一张图成 QImage（供后台线程调用）。解不出返回 null QImage。"""
    img = QImage()
    try:
        img = QImage(str(path))
    except Exception:
        img = QImage()
    return img


def load_video_thumb(path, mtime=None):
    """视频首帧：复用 thumb_cache（按 路径+mtime 磁盘缓存，缺的现抽）。抽不到给 null。"""
    try:
        from gui import thumb_cache
        return thumb_cache.ensure_image(str(path), mtime)
    except Exception:
        return QImage()


def render_doc_thumb(text, w=220, h=280):
    """把文档正文渲染成一张"纸样"缩略图（内容派生的真实预览，不引外部渲染器）。
    白底 + 顶部几行正文 + 左侧蓝条；文字太少也照样出一张占位纸。

    ⚠ 只能在 GUI 线程调用：里面是 QPainter + QFont + drawText，工作线程调它 = qFatal。
    后台线程要"画纸样"请返回 DocCard(text)，由主线程收到信号后再调本函数。"""
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor("#FFFFFF"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.fillRect(0, 0, 4, h, QColor("#3370FF"))            # 左侧品牌蓝条
    lines = [ln for ln in (text or "").splitlines() if ln.strip()][:14]
    p.setPen(QColor("#1F2329"))
    f = QFont()
    f.setPixelSize(11)
    p.setFont(f)
    y = 18
    for ln in lines:
        p.drawText(14, y, w - 24, 16,
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   ln[:40])
        y += 17
        if y > h - 12:
            break
    p.end()
    return img


def _pdf_first_page_image(path, max_px=_PDF_PREVIEW_MAX):
    """用 PyMuPDF（fitz）把 PDF 首页渲染成 QImage。没装/读不出给 null。

    为什么需要它：扫描/纯图片的 PDF 走 pypdf 抽文字是空的，预览只能显示
    “无可读正文”——那是能力缺失不是 bug。只有把页渲染成位图才看得见内容。
    fitz 是重依赖，没装就优雅降级回文字抽取（跟爆款拆解可选依赖一个套路）。
    延迟导入：面板常驻也不背 PyMuPDF 的成本。"""
    try:
        import pymupdf as fitz                    # PyMuPDF 新版正规名
    except Exception:
        try:
            import fitz                           # 旧版只有 fitz 这个名字
        except Exception:
            return QImage()
    try:
        doc = fitz.open(str(path))
        try:
            if doc.page_count <= 0:
                return QImage()
            page = doc[0]
            rect = page.rect
            base = max(float(rect.width or 1), float(rect.height or 1))
            zoom = max(min(max_px / base, 4.0) if base else 1.0, 0.1)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            # 走 PNG 字节→QImage.fromData：避开 stride/格式直接构造的兼容性坑
            return QImage.fromData(pix.tobytes("png"))
        finally:
            doc.close()
    except Exception:
        return QImage()


def _doc_preview(path):
    """文档预览分派：PDF 先试渲染首页（扫描/图片 PDF 就靠这个），渲染不出才
    退回抽文字；其余 Office 一律走文字。返回 ("image", QImage) / ("text", str)。"""
    if _ext(path) == ".pdf":
        img = _pdf_first_page_image(path, _PDF_PREVIEW_MAX)
        if not img.isNull():
            return "image", img
    return "text", read_doc_text(path)


class DocCard:
    """"纸样"卡的**文字底**：后台线程只把正文捞回来，画图这一步交回 GUI 线程。

    为什么不干脆在后台把图画好：QImage 能在别的线程构造，但往 QImage 上
    QPainter::drawText 会触发 QFontDatabase 的"只能 GUI 线程"断言，直接 qFatal
    终止进程（本机就是这么崩了三四次的）。跨线程带一个纯 Python 对象最便宜。"""

    __slots__ = ("text",)

    def __init__(self, text=""):
        self.text = text or ""

    def render(self):
        """在主线程画成图（信号回程里调）。"""
        return render_doc_thumb(self.text)


def _doc_thumb(path):
    """网格缩略图：PDF 出真实首页图（QImage，可跨线程），抽不出/非 PDF 只带正文
    回 DocCard，留给主线程画"纸样"。返回 QImage 或 DocCard。"""
    if _ext(path) == ".pdf":
        img = _pdf_first_page_image(path, _PDF_THUMB_MAX)
        if not img.isNull():
            return img
    return DocCard(read_doc_text(path))


def build_payload(path, mtime=None, kind=None):
    """算出预览载荷：返回 ("image", QImage) / ("text", str) / ("none", None)。
    在后台线程调用（解码/抽帧/抽正文都在这里，主线程只做展示）。"""
    kind = kind or category(path)
    if kind == "image":
        return "image", load_image(path)
    if kind == "video":
        return "image", load_video_thumb(path, mtime)
    if kind == "text":
        return "text", read_text(path)
    if kind == "doc":
        return _doc_preview(path)
    return "none", None


def preview_image(path, mtime=None):
    """给网格卡用的缩略图（后台线程调用）：图片/视频/PDF 出 QImage，文档抽不出
    首页时回 DocCard(正文)——真正的绘制必须留在 GUI 线程，见模块头的线程纪律。
    其余类型返回 null QImage（卡上保留类型图标占位）。调用方（_on_thumb）负责
    把 DocCard 换成图再做 QPixmap.fromImage。"""
    kind = category(path)
    if kind == "image":
        return load_image(path)
    if kind == "video":
        return load_video_thumb(path, mtime)
    if kind == "doc":
        return _doc_thumb(path)
    return QImage()


class ImgWorker(QThread):
    """网格卡缩略图后台服务：submit 覆盖待办队列，逐张算 QImage/DocCard 后
    ready(path, obj) 回主线程上色。

    ⚠ 生命周期（同样是真崩溃，不是保险丝）：QThread 对象必须比它起来的线程活得久，
    否则 Qt 收尾时 deleteChildren 析构一个"还在跑"的 QThread 就是
    qFatal("QThread: Destroyed while thread is still running")。本机 minidump 实测栈：
    Py_FinalizeEx → destroyQCoreApplication → QWidget::~QWidget →
    deleteChildren → QThread::~QThread → int 29h。
    以前它是 LauncherDialog 的子对象 + closeEvent 里只 wait(300)（300ms 连一次
    PDF 渲染都不够），所以一退出就崩。现在走 img_worker() 进程级单例（parent=None，
    不当任何窗口的子对象），退出由 aboutToQuit → shutdown() 统一处理。"""

    ready = Signal(str, object)

    #: 一次任务可能就是 ffmpeg 抽帧 / 几百页 PDF 渲染，收尾得给够时间
    _STOP_WAIT_MS = 8000

    def __init__(self, parent=None):
        super().__init__(parent)
        from collections import deque
        self._q = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False

    def submit(self, jobs):
        """jobs: [(path, mtime), ...]。整队替换，避免连切视图时旧待办堆积。"""
        with self._lock:
            self._q.clear()
            for j in jobs:
                self._q.append(j)
        self._wake.set()

    def clear(self):
        """只清空待办，不停线程：面板收起了就别再为看不见的卡抽磁盘。"""
        with self._lock:
            self._q.clear()

    def request_stop(self):
        self._stop = True
        self._wake.set()

    def shutdown(self):
        """进程退出前调用：停得干净就行，等不到也不能把带着线程的 QThread 扔给 GC。"""
        self.request_stop()
        if self.isRunning():
            self.wait(self._STOP_WAIT_MS)

    def run(self):
        while not self._stop:
            with self._lock:
                job = self._q.popleft() if self._q else None
            if job is None:
                self._wake.wait(0.3)
                self._wake.clear()
                continue
            path, mtime = job
            if self._stop:
                break
            try:
                obj = preview_image(path, mtime)
            except Exception:                 # 一个坏文件不能把整条服务带倒
                continue
            if not self._stop:
                self.ready.emit(str(path), obj)


#: 进程级唯一缩略图线程（None = 还没人用过）。为什么不能每开一个面板建一条：
#: 面板常驻就不停，线程数随唤出次数增长；而作为对话框子对象又会在退出时被
#: deleteChildren 提前析构——就是上面那条 qFatal。
_IMG_WORKER = None


def img_worker():
    """拿到常驻的缩略图线程（懒建，并把自己接到进程退出流程上）。"""
    global _IMG_WORKER
    if _IMG_WORKER is not None and not _IMG_WORKER.isFinished():
        return _IMG_WORKER
    w = ImgWorker()                       # parent=None：不当任何 QObject 的子对象
    _IMG_WORKER = w
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(shutdown_img_worker)
    except Exception:
        pass
    return w


def shutdown_img_worker():
    """aboutToQuit 槽：把常驻线程收干净（主窗口退出链路与 Qt 收尾都靠它）。"""
    w = _IMG_WORKER
    if w is not None:
        w.shutdown()


class ZoomImageView(QGraphicsView):
    """图片/PDF 首页的可缩放预览：适应窗口默认占可视区 90%（左右各留 ~10%），
    Ctrl+滚轮或 +/− 缩放，拖拽平移，单击（没拖动）请求打开文件。

    为什么换掉旧的 QLabel：旧版把整张图一次性缩到窗宽、既看不清又不能放大拖动，
    “图片不清楚 / 没法放大缩小拖动 / 点预览打不开”都出在这儿。用 QGraphicsView
    自带 ScrollHandDrag 平移 + 变换缩放，放大时关掉平滑插值才不致糊。"""

    clicked_open = Signal()

    _MIN, _MAX = 0.05, 12.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._item = QGraphicsPixmapItem()
        self._scene.addItem(self._item)
        self.setScene(self._scene)
        self.setFrameStyle(0)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QColor(0, 0, 0, 0))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._zoom = 1.0
        self._user_zoomed = False           # 用户手动缩过就没随窗口重算“适应”
        self._press = None
        self._dragged = False

    # ---------- 对外 ----------
    def show_image(self, img):
        pm = img if isinstance(img, QPixmap) else QPixmap.fromImage(img)
        self._item.setPixmap(pm)
        if pm.isNull():
            self._scene.setSceneRect(QGraphicsScene().sceneRect())
            return
        self._scene.setSceneRect(0, 0, pm.width(), pm.height())
        self._user_zoomed = False
        self._fit()

    def zoom_in(self):
        self._apply(self._zoom * 1.25)

    def zoom_out(self):
        self._apply(self._zoom / 1.25)

    def reset_fit(self):
        self._user_zoomed = False
        self._fit()

    # ---------- 缩放核心 ----------
    def _fit_scale(self):
        pm = self._item.pixmap()
        vp = self.viewport().size()
        if pm.isNull() or vp.width() <= 0 or vp.height() <= 0:
            return 1.0
        aw = max(vp.width() * 0.9, 1.0)     # 左右/上下各留 10%
        ah = max(vp.height() * 0.9, 1.0)
        return min(aw / pm.width(), ah / pm.height())

    def _fit(self):
        self._apply(self._fit_scale(), by_user=False)

    def _apply(self, scale, by_user=True):
        self._zoom = min(max(scale, self._MIN), self._MAX)
        self._user_zoomed = self._user_zoomed or by_user
        self.resetTransform()
        self.scale(self._zoom, self._zoom)
        # 缩小用平滑（抗锯齿），放大到原图以上用快速（再平滑就是把像素糊开，更不清）
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,
                           self._zoom <= 1.0)

    # ---------- 交互 ----------
    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._apply(self._zoom * (1.25 if e.angleDelta().y() > 0 else 0.8))
            e.accept()
            return
        super().wheelEvent(e)               # 不按住 Ctrl：滚动平移

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.position().toPoint()
            self._dragged = False
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press is not None and \
                (e.position().toPoint() - self._press).manhattanLength() > 6:
            self._dragged = True
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and \
                self._press is not None and not self._dragged:
            self.clicked_open.emit()        # 按下-抬起没动＝单击：打开
        self._press = None
        super().mouseReleaseEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if not self._user_zoomed:           # 仍是“适应”模式：随窗宽重算
            self._fit()


class PreviewPane(QWidget):
    """右侧预览窗：随传入的选中项显示内容。三态堆栈——占位/无法预览、文本、图片。
    P3 会再挂 HTML 与脑图页，这里先把地基与异步回程做稳。"""

    _loaded = Signal(str, str, object)          # (token, kind, payload)
    openRequested = Signal(str)                 # 单击预览图→打开当前文件（携路径）

    #: 堆栈页下标（前三个固定；render 也常驻；html/脑图 懒构造后动态追加）
    _PAGE_NONE, _PAGE_TEXT, _PAGE_IMAGE = 0, 1, 2
    _PAGE_RENDER = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._seq = 0
        self._token = ""
        self._cur_img = QImage()                # 当前图片原图，resize 时按窗宽重算
        # 当前预览项的类型/数据/模式（模式切换不重新读盘，直接拿缓存重绘）
        self._pv_path, self._pv_mtime = "", None
        self._pv_kind, self._pv_data, self._pv_mode = "none", None, None
        self._mode_btns = {}
        self._page_html = None                  # QWebEngineView：首次预览 HTML 才造
        self._mm = None                         # MindMapView：首次预览 MD 脑图才造
        self.setMinimumWidth(260)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.lbl_title = QLabel("选择文件以预览")
        self.lbl_title.setStyleSheet(
            "color:#E8EAED; font-size:14px; font-weight:600; background:transparent;")
        head.addWidget(self.lbl_title, 1)
        # 图片缩放条：仅图片/PDF 时亮（− 适应 +），平时隐藏
        self._zoom_btns = []
        for glyph, tip, fn in (("−", "缩小", lambda: self.page_image.zoom_out()),
                               ("适应", "适应窗口", lambda: self.page_image.reset_fit()),
                               ("+", "放大", lambda: self.page_image.zoom_in())):
            b = QToolButton(self)
            b.setText(glyph)
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                "QToolButton { color:#9AA4B0; background:transparent;"
                " border:1px solid rgba(255,255,255,0.14); border-radius:5px;"
                " padding:1px 8px; font-size:13px; }"
                " QToolButton:hover { background:rgba(255,255,255,0.10); }")
            b.clicked.connect(lambda _=False, f=fn: f())
            b.setVisible(False)
            head.addWidget(b)
            self._zoom_btns.append(b)
        lay.addLayout(head)
        # 模式切换条：仅 Markdown / HTML 时出现（文本/渲染/脑图 或 渲染/源码）
        self._bar = QHBoxLayout()
        self._bar.setSpacing(6)
        self._mode_bar = QWidget(self)
        self._mode_bar.setLayout(self._bar)
        self._mode_bar.setVisible(False)
        lay.addWidget(self._mode_bar)
        self.lbl_sub = QLabel("")
        self.lbl_sub.setStyleSheet(
            "color:#9AA4B0; font-size:11px; background:transparent;")
        self.lbl_sub.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lbl_sub)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background:transparent;")

        # 占位/无法预览页
        self.page_none = QLabel("")
        self.page_none.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_none.setWordWrap(True)
        self.page_none.setStyleSheet(
            "color:#9AA4B0; font-size:13px; background:rgba(255,255,255,0.03);"
            " border-radius:8px;")

        # 文本页
        self.page_text = QPlainTextEdit()
        self.page_text.setReadOnly(True)
        self.page_text.setStyleSheet(
            "QPlainTextEdit { background:rgba(255,255,255,0.04); border:none;"
            " border-radius:8px; color:#E8EAED; }")

        # 图片页：换成 ZoomImageView（可缩放/拖动/单击打开）。旧 QLabel 只会把整
        # 张图缩到窗宽，既看不清又动不了——图片不清楚/没法放大拖动/点着打不开都在这。
        self.page_image = ZoomImageView()
        self.page_image.setStyleSheet("background:transparent;")
        self.page_image.clicked_open.connect(self._on_image_click)

        # 渲染页（MD 富文本 / HTML 无 WebEngine 时的回退）：QTextBrowser 常驻且轻量
        self.page_render = QTextBrowser()
        self.page_render.setOpenExternalLinks(False)
        self.page_render.setStyleSheet(
            "QTextBrowser { background:rgba(255,255,255,0.04); border:none;"
            " border-radius:8px; color:#E8EAED; }")

        self.stack.addWidget(self.page_none)
        self.stack.addWidget(self.page_text)
        self.stack.addWidget(self.page_image)
        self.stack.addWidget(self.page_render)
        self.stack.setCurrentIndex(self._PAGE_NONE)
        lay.addWidget(self.stack, 1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._loaded.connect(self._on_loaded)

    # ---------- 对外 ----------
    def show_item(self, item):
        """传入当前选中的结果项 dict（None/工具/空 → 复位占位）。"""
        item = item or {}
        path = str(item.get("path") or "")
        if not path:
            self._seq += 1
            self._token = ""
            self._pv_kind, self._pv_data, self._pv_path = "none", None, ""
            self.lbl_title.setText("选择文件以预览")
            self.lbl_sub.setText("")
            self._show_toolbar(())
            self._set_zoom_bar(False)
            self.stack.setCurrentIndex(self._PAGE_NONE)
            self.page_none.setText("选中一个文件或文档结果，这里就预览它")
            return
        self.lbl_title.setText(str(item.get("name") or os.path.basename(path)))
        self.lbl_sub.setText(self._subtitle(item))
        self._seq += 1
        seq = self._seq
        self._token = "%d:%s" % (seq, path)
        mtime = item.get("mtime")
        self._pv_path, self._pv_mtime = path, mtime
        kind = _pv_category(path)
        if _SYNC_PREVIEW:
            self._on_loaded(self._token, *gather_data(path, mtime, kind))
            return
        self._show_toolbar(())
        self._set_zoom_bar(False)
        self.stack.setCurrentIndex(self._PAGE_NONE)
        self.page_none.setText("🔍 正在预览…")

        def work():
            payload = gather_data(path, mtime, kind)
            self._loaded.emit(self._token, *payload)
        import threading
        threading.Thread(target=work, daemon=True, name="launcher-preview").start()

    def _subtitle(self, item):
        parts = [str(item.get("dir") or "")]
        try:
            from gui.dialogs_launcher import human_size, human_time
            sz = human_size(item.get("size"))
            tm = human_time(item.get("mtime"))
            if sz:
                parts.append(sz)
            if tm:
                parts.append(tm)
        except Exception:
            pass
        return "  ·  ".join([p for p in parts if p])

    # ---------- 回程 ----------
    def _on_loaded(self, token, kind, payload):
        if token != self._token:                 # 慢回包盖快选中：丢
            return
        self._pv_kind, self._pv_data = kind, payload
        specs = _MODE_LABELS.get(kind, ())
        self._show_toolbar(specs)
        mode = _MODE_DEFAULT.get(kind) or (specs[0][0] if specs else None)
        self._pv_mode = mode
        if mode and mode in self._mode_btns:
            self._mode_btns[mode].setChecked(True)
        self._render_mode(mode)

    def _render_mode(self, mode):
        """按当前类型 + 模式把缓存数据铺到对应页；模式切换不重新读盘。"""
        kind, data = self._pv_kind, self._pv_data
        self._set_zoom_bar(kind == "image")
        if kind == "image":
            img = data if isinstance(data, QImage) else QImage()
            if img.isNull():
                self._show_none("🖼 无法预览该图片/视频")
                return
            self._cur_img = img
            self._show_image(img)
            return
        if kind == "text":
            self._show_text(data)
            return
        if kind == "md":
            text = str(data or "")
            if mode == "render":
                self.page_render.setMarkdown(text)
                self.stack.setCurrentIndex(self._PAGE_RENDER)
                return
            if mode == "mindmap":
                view = self._ensure_mindmap()
                if view is not None:
                    from gui.launcher_mindmap import parse_md_tree
                    view.show_tree(parse_md_tree(text, self.lbl_title.text()))
                    self.stack.setCurrentIndex(self.stack.indexOf(view))
                    return
                self.page_render.setMarkdown(text)     # 造不出脑图退回渲染
                self.stack.setCurrentIndex(self._PAGE_RENDER)
                return
            self.page_text.setPlainText(text)          # text
            self.stack.setCurrentIndex(self._PAGE_TEXT)
            return
        if kind == "html":
            if mode == "source":
                self._show_text(data)
                return
            view = self._ensure_html()
            if view is not None:
                from PySide6.QtCore import QUrl
                view.load(QUrl.fromLocalFile(os.path.abspath(self._pv_path)))
                self.stack.setCurrentIndex(self.stack.indexOf(view))
                return
            self.page_render.setHtml(str(data or ""))  # 无 WebEngine → QTextBrowser 回退
            self.stack.setCurrentIndex(self._PAGE_RENDER)
            return
        self._show_none("🚫 无法预览此文件类型")

    def _show_text(self, data):
        text = str(data or "")
        if not text.strip():
            self._show_none("📄 无可读正文（可能是扫描件或超出大小上限）")
            return
        self.page_text.setPlainText(text)
        self._apply_text_font()
        self.stack.setCurrentIndex(self._PAGE_TEXT)

    # ---------- 模式工具条 ----------
    def _show_toolbar(self, specs):
        while self._bar.count():
            it = self._bar.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._mode_btns = {}
        if not specs:
            self._mode_bar.setVisible(False)
            return
        for key, label in specs:
            b = QPushButton(label, self._mode_bar)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            # QPushButton 的 :checked 伪态会随 isChecked 自动重绘，不必手动换样式
            b.setStyleSheet(
                "QPushButton { color:#9AA4B0; background:transparent;"
                " border:1px solid rgba(255,255,255,0.14); border-radius:6px;"
                " padding:2px 12px; }"
                " QPushButton:checked { color:#8AB4FF;"
                " background:rgba(51,112,255,0.24); border-color:#3370FF; }")
            b.clicked.connect(lambda _=False, k=key: self._set_mode(k))
            self._bar.addWidget(b)
            self._mode_btns[key] = b
        self._bar.addStretch(1)
        self._mode_bar.setVisible(True)

    def _set_mode(self, mode):
        self._pv_mode = mode
        for k, b in self._mode_btns.items():
            b.setChecked(k == mode)
        self._render_mode(mode)

    # ---------- 懒构造：重依赖只在首次用到时建 ----------
    def _ensure_html(self):
        """QWebEngineView 懒导入 + 懒构造：面板常驻也不背 WebEngine 的成本。
        已知代价：在半透明无边框 Tool 窗里会画成不透明实底（预览区本就实底，可接受）；
        首次初始化异步→头一帧前显示空白。导入/构造失败返回 None，调用方回退 QTextBrowser。"""
        if self._page_html is not None:
            return self._page_html
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            v = QWebEngineView(self)
            v.setStyleSheet("background:#FFFFFF;")
            self.stack.addWidget(v)
            self._page_html = v
            return v
        except Exception:
            return None

    def _ensure_mindmap(self):
        if self._mm is not None:
            return self._mm
        try:
            from gui.launcher_mindmap import MindMapView
            v = MindMapView(self)
            self.stack.addWidget(v)
            self._mm = v
            return v
        except Exception:
            return None

    def _show_none(self, msg):
        self._cur_img = QImage()
        self.page_none.setText(msg)
        self.stack.setCurrentIndex(self._PAGE_NONE)

    def _show_image(self, img):
        """交给 ZoomImageView：按可视区 90% 适应，缩放/平移它自己管。"""
        self.page_image.show_image(img)
        self.stack.setCurrentIndex(self._PAGE_IMAGE)

    def _apply_text_font(self):
        # 代码类走等宽字体，普通文本用默认比例字体
        path = self._token.split(":", 1)[1] if ":" in self._token else ""
        e = _ext(path)
        fam = "Consolas" if e in MONO_EXTS else ""
        f = self.page_text.font()
        if fam:
            f.setFamily(fam)
            f.setStyleHint(QFont.StyleHint.TypeWriter)
        self.page_text.setFont(f)

    def _img_box(self):
        w = max(self.stack.width() - 24, 160)
        h = max(self.stack.height() - 24, 160)
        from PySide6.QtCore import QSize
        return QSize(w, h)

    def resizeEvent(self, e):
        # ZoomImageView 自己按可视区重算适应；这里不能再调 _show_image，
        # 否则会把用户手动放大的倍率重置掉。
        super().resizeEvent(e)

    def _set_zoom_bar(self, on):
        """图片缩放条（减 适应 加）只在图片/PDF 首页那页出现，其余类型隐藏。"""
        for b in self._zoom_btns:
            b.setVisible(bool(on))

    def _on_image_click(self):
        """单击预览图 → 请求打开当前文件。ZoomImageView 已用“没拖动”滤掉平移，
        这儿再确认一次路径仍在（文件可能已被删）。"""
        path = str(self._pv_path or "")
        if path and os.path.exists(path):
            self.openRequested.emit(path)
