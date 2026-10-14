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

from PySide6.QtCore import Qt, QEvent, QThread, QObject, QSize, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
                               QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
                               QSizePolicy, QStackedWidget, QTextBrowser, QToolButton,
                               QVBoxLayout, QWidget)

#: 测试里置 True：预览同步计算，不等后台线程（离屏测试等不到线程回包）
_SYNC_PREVIEW = False

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv", ".mpg"}
DOC_EXTS = {".docx", ".xlsx", ".xls", ".pptx", ".pdf"}
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
#: 预览专用正文大小上限（远大于索引层的 20MB）：见 read_doc_text 说明。
_PREVIEW_DOC_MAX = 200 * 1024 * 1024
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
#: 渲染页样式：MD/HTML 用深色，docx 富文本用白底黑字（更像 Word）
_RENDER_DARK_QSS = ("QTextBrowser { background:rgba(255,255,255,0.04); border:none;"
                    " border-radius:8px; color:#E8EAED; }")
#: Word/Excel 富文本：白底黑字 + 左右留白（不撑到边就不像“放大过头”），字号略小于默认
_RENDER_DOC_QSS = ("QTextBrowser { background:#FFFFFF; border:none;"
                   " border-radius:8px; color:#1F2329; padding:14px 32px; font-size:12px; }")


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
    """Office 正文：走索引层现成抽取器（懒导入，抽不到返回 ""）。

    这里显式放宽大小上限（_PREVIEW_DOC_MAX）：索引层的 20MB 是给"几万个文件批量抽"
    兜底用的，而预览是用户点名看这一个文件。docx/pptx 的正文在 zip 里的 document.xml，
    体积几乎不随内嵌图片增长——一个几十 MB 的图文 docx，正文可能才几十 KB，卡在整包
    大小上判"太大无法预览"纯属误伤（图文 docx 与较大的 xlsx 都是栽在这）。"""
    try:
        from core import fileindex
        return fileindex.extract_text(path, max_bytes=_PREVIEW_DOC_MAX) or ""
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


def _fitz():
    """拿 PyMuPDF 模块（新版正规名 pymupdf，旧版只有 fitz）；没装返回 None。
    fitz 是重依赖，没装就优雅降级回文字抽取（跟爆款拆解可选依赖一个套路）。"""
    try:
        import pymupdf
        return pymupdf
    except Exception:
        try:
            import fitz
            return fitz
        except Exception:
            return None


def _pdf_page_count(path):
    """PDF 总页数（渲染器不可用/读不出返回 0）。"""
    fitz = _fitz()
    if fitz is None:
        return 0
    try:
        doc = fitz.open(str(path))
        try:
            return int(doc.page_count)
        finally:
            doc.close()
    except Exception:
        return 0


def _pdf_page_image(path, idx=0, max_px=_PDF_PREVIEW_MAX):
    """用 PyMuPDF（fitz）把 PDF 第 idx 页渲染成 QImage。没装/读不出给 null。

    为什么需要它：扫描/纯图片的 PDF 走 pypdf 抽文字是空的，预览只能显示
    “无可读正文”——那是能力缺失不是 bug。只有把页渲染成位图才看得见内容。
    逐页只渲染当前一页（不是整本铺开），几百页手册点开也只算一页，翻页才现算。"""
    fitz = _fitz()
    if fitz is None:
        return QImage()
    try:
        doc = fitz.open(str(path))
        try:
            n = doc.page_count
            if n <= 0:
                return QImage()
            idx = max(0, min(int(idx), n - 1))
            page = doc[idx]
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


def _pdf_first_page_image(path, max_px=_PDF_PREVIEW_MAX):
    """首页渲染（网格缩略图用）。"""
    return _pdf_page_image(path, 0, max_px)


def _docx_html(path):
    """docx → (html, 图片临时目录)：用 mammoth 转语义 HTML（标题/段落/加粗/列表/
    表格保留），内嵌图片落到临时目录，上层 QTextBrowser 靠 searchPaths 解析相对
    <img src>。无库/异常返回 None，调用方回退纯文本抽取。

    后台线程调用：只读 zip / 写临时文件 / 算哈希，不碰 QPainter。"""
    try:
        import mammoth
    except Exception:
        return None
    import tempfile
    import hashlib
    out_dir = tempfile.mkdtemp(prefix="ql_docx_")

    def _store(image):
        try:
            with image.open() as f:
                data = f.read()
            if not data:
                return {}
            ct = (getattr(image, "content_type", "") or "").split("/", 1)
            ext = "." + ct[1].split("+", 1)[0].split(";", 1)[0] if len(ct) > 1 else ".png"
            fn = hashlib.md5(data).hexdigest()[:16] + ext
            with open(os.path.join(out_dir, fn), "wb") as w:
                w.write(data)
            return {"src": fn}
        except Exception:
            return {}

    try:
        with open(str(path), "rb") as f:
            result = mammoth.convert_to_html(
                f, convert_image=mammoth.images.img_element(_store))
        html = result.value or ""
        return (html, out_dir) if html.strip() else None
    except Exception:
        return None


def _xls_text(path):
    """老式二进制 .xls（Excel 97-2003）抽正文：openpyxl 只读 .xlsx，.xls 得靠 xlrd。

    xlrd 2.0 反而只支持 .xls（-drop 了 xlsx），正好对上。逐 sheet 拼“表名 + 行（制表符分隔），
    整数去掉多余的 .0”。每个 sheet 只取前几行、总字符截断（预览看大概，不是全量）。"""
    try:
        import xlrd
    except Exception:
        return ""
    try:
        book = xlrd.open_workbook(str(path))
    except Exception:
        return ""
    out = []
    total = 0
    for sh in book.sheets():
        out.append("【%s】" % sh.name)
        total += len(out[-1])
        for r in range(min(sh.nrows, 500)):
            vals = []
            for c in range(sh.ncols):
                try:
                    v = sh.cell_value(r, c)
                except Exception:
                    v = ""
                if isinstance(v, float) and v.is_integer():
                    v = int(v)
                vals.append(str(v))
            line = "\t".join(vals).rstrip()
            if line:
                out.append(line)
                total += len(line)
                if total > _TEXT_CAP:
                    break
        if total > _TEXT_CAP:
            break
    return "\n".join(out)


def _html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


#: 表格预览只取前几张表 / 每表前若干行列：兼顾速度与内存（大表读全量会卡）
_SHEET_MAX_SHEETS = 3
_SHEET_MAX_ROWS = 200
_SHEET_MAX_COLS = 26


def _rows_to_html(title, rows):
    """行列表 → 一段带网格的 HTML 表（首行作表头）。border+border-collapse
    是 Qt 富文本引擎认得的写法，能画出格线；无有效列返回 ""。"""
    body = []
    ncol = 0
    for i, row in enumerate(rows):
        vals = ["" if v is None else str(v) for v in row]
        ncol = max(ncol, len(vals))
        tag = "th" if i == 0 else "td"
        tds = "".join("<%s>%s</%s>" % (tag, _html_escape(v), tag) for v in vals)
        body.append("<tr>%s</tr>" % tds)
    if not ncol:
        return ""
    return ("<h3>%s</h3><table border=\"1\" cellspacing=\"0\" cellpadding=\"3\" "
            "style=\"border-collapse:collapse\">%s</table>"
            % (_html_escape(title), "".join(body)))


def _sheet_html_xlsx(path):
    """.xlsx 流式读取（read_only）——比旧的全量抽取快很多，只取前几张表/若平行列。"""
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True, data_only=True)
    out = []
    try:
        for ws in wb.worksheets[:_SHEET_MAX_SHEETS]:
            rows = []
            for r, row in enumerate(ws.iter_rows(values_only=True)):
                rows.append(list(row[:_SHEET_MAX_COLS]))
                if r >= _SHEET_MAX_ROWS:
                    break
            if not rows:
                out.append("<h3>%s</h3><p>（空表）</p>" % _html_escape(ws.title))
                continue
            out.append(_rows_to_html(ws.title, rows))
    finally:
        wb.close()
    return "".join(out)


def _sheet_html_xls(path):
    """.xls（Excel 97-2003）走 xlrd，同样只取前几张表/若干行列。"""
    import xlrd
    book = xlrd.open_workbook(str(path))
    out = []
    for sh in book.sheets()[:_SHEET_MAX_SHEETS]:
        rows = []
        for r in range(min(sh.nrows, _SHEET_MAX_ROWS + 1)):
            vals = []
            for c in range(min(sh.ncols, _SHEET_MAX_COLS)):
                try:
                    v = sh.cell_value(r, c)
                except Exception:
                    v = ""
                if isinstance(v, float) and v.is_integer():
                    v = int(v)
                vals.append(str(v))
            rows.append(vals)
        if not rows:
            out.append("<h3>%s</h3><p>（空表）</p>" % _html_escape(sh.name))
            continue
        out.append(_rows_to_html(sh.name, rows))
    return "".join(out)


def _sheet_html(path):
    """表格类预览：把 .xlsx/.xls 渲染成 HTML 表格（带网格），走 richtext 页显示。
    没装依赖/读不出返回 ""（上层退回纯文本）。"""
    try:
        if _ext(path) == ".xls":
            return _sheet_html_xls(path)
        return _sheet_html_xlsx(path)
    except Exception:
        return ""


def _doc_preview(path):
    """文档预览分派：PDF 交回页数（由 PreviewPane 逐页渲染、可翻页）；渲染器
    缺失才退回抽首页/抽文字；docx 优先 mammoth 转 HTML（带图带版式）；其余
    Office 一律走文字。返回 ("pdf", 页数) / ("image", QImage) / ("text", str)。"""
    if _ext(path) == ".pdf":
        n = _pdf_page_count(path)
        if n > 0:
            return "pdf", n
        img = _pdf_first_page_image(path, _PDF_PREVIEW_MAX)
        if not img.isNull():
            return "image", img
    if _ext(path) == ".docx":
        r = _docx_html(path)
        if r is not None:
            return "richtext", {"html": r[0], "base": r[1]}
    if _ext(path) in (".xlsx", ".xls"):
        html = _sheet_html(path)              # 表格→带网格 HTML（快、有单元格样子）
        if html.strip():
            return "richtext", {"html": html, "base": ""}
        if _ext(path) == ".xls":              # 解不出表格再退回纯文本
            return "text", _xls_text(path)
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
    wheel_page = Signal(int)               # PDF：不按 Ctrl 的滚轮→交上层翻上/下一页

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
        self.page_via_wheel = False          # True（PDF）：普通滚轮＝翻页，非平移
        self._press = None
        self._dragged = False

    # ---------- 对外 ----------
    def show_image(self, img, fit=True):
        pm = img if isinstance(img, QPixmap) else QPixmap.fromImage(img)
        self._item.setPixmap(pm)
        if pm.isNull():
            self._scene.setSceneRect(QGraphicsScene().sceneRect())
            return
        self._scene.setSceneRect(0, 0, pm.width(), pm.height())
        if fit:
            self._user_zoomed = False
            self._fit()
        else:
            # PDF 默认 100%（原大），高过一屏就靠拖动/滚轮翻页看，不再缩成“适应”
            self._user_zoomed = True
            self._apply(1.0, by_user=False)

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
        if self.page_via_wheel:              # PDF：不按 Ctrl 的滚轮＝上/下一页
            self.wheel_page.emit(-1 if e.angleDelta().y() > 0 else 1)
            e.accept()
            return
        super().wheelEvent(e)               # 普通图片：不按住 Ctrl 时滚动平移

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
    _pdf_ready = Signal(str, int, object)       # (token, 页下标, QImage)
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
        self._pdf_total, self._pdf_idx = 0, 0    # PDF 页数与当前页（逐页渲染）
        # 防滚轮翻页闪烁：当前是否已有一页图在显（有就不切“正在渲染”占位，
        # 旧页一直显到新页算好直接换上）；_pdf_pending + 定时器把快速滚轮的
        # 多格合成一次跳转，只渲染最终目标页，不再每格起一个线程刷白。
        self._pdf_has_img = False
        self._pdf_pending = None
        self._pdf_nav = QTimer(self)
        self._pdf_nav.setSingleShot(True)
        self._pdf_nav.setInterval(60)
        self._pdf_nav.timeout.connect(self._flush_pdf_nav)
        self._rt_base = ""                       # docx 图片临时目录（换项时清）
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
        for glyph, tip, fn in (("−", "缩小", self._zoom_out),
                               ("适应", "适应窗口", self._zoom_reset),
                               ("+", "放大", self._zoom_in)):
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
        # PDF 翻页条：仅多页 PDF 时出现（◀ 上一页 / 第 X 共 N 页 / 下一页 ▶）。
        # 单页 PDF 不挡道（没得可翻），但仍走逐页渲染同一张页。
        self._pdf_bar_lay = QHBoxLayout()
        self._pdf_bar_lay.setSpacing(6)
        self._pdf_bar_lay.setContentsMargins(0, 0, 0, 0)
        _pdf_btn_qss = (
            "QToolButton { color:#C9CED6; background:transparent;"
            " border:1px solid rgba(255,255,255,0.14); border-radius:5px;"
            " padding:1px 10px; font-size:12px; }"
            " QToolButton:hover { background:rgba(255,255,255,0.10); }"
            " QToolButton:disabled { color:#5A6068; }")
        self._pdf_prev = QToolButton(self)
        self._pdf_prev.setText("◀ 上一页")
        self._pdf_next = QToolButton(self)
        self._pdf_next.setText("下一页 ▶")
        for b in (self._pdf_prev, self._pdf_next):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_pdf_btn_qss)
        self._pdf_lbl = QLabel("")
        self._pdf_lbl.setStyleSheet(
            "color:#C9CED6; font-size:12px; background:transparent;")
        self._pdf_prev.clicked.connect(lambda: self._pdf_goto(-1))
        self._pdf_next.clicked.connect(lambda: self._pdf_goto(1))
        self._pdf_bar_lay.addWidget(self._pdf_prev)
        self._pdf_bar_lay.addWidget(self._pdf_lbl)
        self._pdf_bar_lay.addWidget(self._pdf_next)
        self._pdf_bar_lay.addStretch(1)
        self._pdf_bar = QWidget(self)
        self._pdf_bar.setLayout(self._pdf_bar_lay)
        self._pdf_bar.setVisible(False)
        lay.addWidget(self._pdf_bar)
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
        self._pdf_ready.connect(self._on_pdf_ready)
        self.page_image.wheel_page.connect(self._pdf_goto)   # PDF 滚轮翻页
        # 富文本（Word/Excel）页 Ctrl+滚轮缩放：装到 QTextBrowser 的视口上（滚轮事件走这儿）
        self._rt_zoom = 0
        self.page_render.viewport().installEventFilter(self)

    # ---------- 对外 ----------
    def _clear_rt(self):
        """释放上一个 docx 预览落地的图片临时目录（不看就不留，避免临时区堆积）。"""
        if self._rt_base:
            import shutil
            shutil.rmtree(self._rt_base, ignore_errors=True)
            self._rt_base = ""

    def show_item(self, item):
        """传入当前选中的结果项 dict（None/工具/空 → 复位占位）。"""
        item = item or {}
        path = str(item.get("path") or "")
        self._clear_rt()
        self._pdf_nav.stop()                    # 换项：取消未刷的翻页，旧 PDF 的中间态不留
        self._pdf_pending = None
        self._reset_rt_zoom()                   # 换项：富文本缩放回基准，别把上个文件的倍率带到新的
        if not path:
            self._seq += 1
            self._token = ""
            self._pv_kind, self._pv_data, self._pv_path = "none", None, ""
            self.lbl_title.setText("选择文件以预览")
            self.lbl_sub.setText("")
            self._show_toolbar(())
            self._set_zoom_bar(False)
            self._pdf_bar.setVisible(False)
            self._pdf_total, self._pdf_idx = 0, 0
            self.page_image.page_via_wheel = False
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

    def set_header_title(self, visible):
        """QuickLook 浮层顶部已有自己的文件名条，这里关掉 pane 自带的标题，
        免得上下两处都显示文件名（重复）。副标题（目录·大小·时间）与
        缩放/翻页/模式控件均不受影响。唤出面板不调它，仍照常显示标题。"""
        self.lbl_title.setVisible(bool(visible))

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
        if kind == "pdf":
            self._pdf_total, self._pdf_idx = int(payload or 0), 0
            self._pdf_has_img = False           # 新文档：首页仍给一次“正在渲染”占位
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
        self._set_zoom_bar(kind in ("image", "pdf", "richtext"))
        # PDF：普通滚轮＝翻页（图片保持平移）；其余一律关掉翻页通道
        self.page_image.page_via_wheel = (kind == "pdf")
        self._pdf_bar.setVisible(False)          # 只有 PDF 分支会再把它打开
        # 渲染页默认回到深色（docx 分支会自己改成白底黑字）
        self.page_render.setStyleSheet(_RENDER_DARK_QSS)
        if kind == "pdf":
            self._pdf_bar.setVisible(self._pdf_total > 1)
            self._show_pdf_page(self._pdf_idx)
            return
        if kind == "richtext":
            payload = data or {}
            html = str(payload.get("html") or "")
            base = str(payload.get("base") or "")
            if not html.strip():                 # 转不出 HTML → 退回纯文本抽取
                self._show_text(read_doc_text(self._pv_path))
                return
            self._rt_base = base
            self.page_render.setSearchPaths([base] if base else [])
            self.page_render.setStyleSheet(_RENDER_DOC_QSS)   # Word 文档：白底黑字
            self.page_render.setHtml(html)
            self.stack.setCurrentIndex(self._PAGE_RENDER)
            return
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
                    name = self.lbl_title.text()
                    # 文档名一并传下去：脑图自己不知道在预览哪个文件，导出时
                    # 提不了个像样的文件名（show_tree 里顺手把展开层级收到一级）
                    view.show_tree(parse_md_tree(text, name), name)
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
            # 真正的空文件（0 字节）单独提示“空文件”，不能和“抽不出正文”混为
            # 一谈——前者是文件确实没内容，后者是能打开但读不出（扫描/加密）。
            try:
                empty = os.path.isfile(self._pv_path) \
                    and os.path.getsize(self._pv_path) == 0
            except OSError:
                empty = False
            self._show_none("📄 空文件（0 字节）" if empty
                            else "📄 无可读正文（扫描件 / 加密 / 暂未支持的格式）")
            return
        self.page_text.setPlainText(text)
        self._apply_text_font()
        self.stack.setCurrentIndex(self._PAGE_TEXT)

    # ---------- PDF 逐页渲染 ----------
    def _show_pdf_page(self, idx):
        """渲染并展示第 idx 页（后台线程解码，token+页号双重守卫）。"""
        if self._pdf_total <= 0:
            self._show_none("📄 无法读取该 PDF")
            return
        idx = max(0, min(int(idx), self._pdf_total - 1))
        self._pdf_idx = idx
        self._pdf_lbl.setText("第 %d / %d 页" % (idx + 1, self._pdf_total))
        self._pdf_prev.setEnabled(idx > 0)
        self._pdf_next.setEnabled(idx < self._pdf_total - 1)
        path, token = self._pv_path, self._token
        if _SYNC_PREVIEW:
            self._on_pdf_ready(token, idx,
                               _pdf_page_image(path, idx, _PDF_PREVIEW_MAX))
            return
        # 只在“手里还没图”（首屏）时显一下占位；翻页时旧页留着，等新页算好直接换，
        # 不再回切占位页——这就是快速滚轮时“刷白/闪烁”的根源。
        if not self._pdf_has_img:
            self.page_none.setText("🔍 正在渲染第 %d 页…" % (idx + 1))
            self.stack.setCurrentIndex(self._PAGE_NONE)

        def work():
            img = _pdf_page_image(path, idx, _PDF_PREVIEW_MAX)
            self._pdf_ready.emit(token, idx, img)
        import threading
        threading.Thread(target=work, daemon=True, name="launcher-pdf").start()

    def _pdf_goto(self, delta):
        """滚轮/按钮翻页：不立即渲染，只累加目标页并重起防抖计时。
        快速滚一格只起一次渲染（合成到最终页），中间页不再逐页刷白。"""
        if self._pv_kind != "pdf" or self._pdf_total <= 0:
            return
        base = self._pdf_idx if self._pdf_pending is None else self._pdf_pending
        target = max(0, min(base + delta, self._pdf_total - 1))
        self._pdf_pending = target
        self._pdf_lbl.setText("第 %d / %d 页" % (target + 1, self._pdf_total))
        self._pdf_prev.setEnabled(target > 0)
        self._pdf_next.setEnabled(target < self._pdf_total - 1)
        self._pdf_nav.start()

    def _flush_pdf_nav(self):
        """防抖到期：只渲染最后停下的那一页。"""
        idx = self._pdf_pending
        self._pdf_pending = None
        if idx is None or self._pv_kind != "pdf":
            return
        if idx != self._pdf_idx:
            self._show_pdf_page(idx)

    def _on_pdf_ready(self, token, idx, img):
        """翻页回程：慢回包不能盖掉当前页（token 变了或页号不是正在看的都丢）。"""
        if token != self._token or self._pv_kind != "pdf" or idx != self._pdf_idx:
            return
        if not isinstance(img, QImage) or img.isNull():
            if not self._pdf_has_img:          # 首屏就失败才显错；翻页失败旧页还在，不刷白
                self._show_none("📄 第 %d 页渲染失败" % (idx + 1))
            return
        self._show_image(img, fit=False)
        self._pdf_has_img = True

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

    def _show_image(self, img, fit=True):
        """交给 ZoomImageView：图片按适应（fit=True），PDF 页按 100%（fit=False）。
        缩放/平移/滚轮行为由 ZoomImageView 自己管。"""
        self.page_image.show_image(img, fit=fit)
        self.stack.setCurrentIndex(self._PAGE_IMAGE)

    # ---------- 缩放派发：图片/PDF 走 ZoomImageView，富文本走 QTextBrowser ----------
    def _on_render_page(self):
        return self.stack.currentIndex() == self._PAGE_RENDER

    def _zoom_in(self):
        if self._on_render_page():
            self.page_render.zoomIn(1)
            self._rt_zoom += 1
        else:
            self.page_image.zoom_in()

    def _zoom_out(self):
        if self._on_render_page():
            self.page_render.zoomOut(1)
            self._rt_zoom -= 1
        else:
            self.page_image.zoom_out()

    def _zoom_reset(self):
        if self._on_render_page():
            self._reset_rt_zoom()
        else:
            self.page_image.reset_fit()

    def _reset_rt_zoom(self):
        """把 QTextBrowser 累计的缩放步数退回去（zoomIn/zoomOut 对称可逆）。"""
        if self._rt_zoom > 0:
            self.page_render.zoomOut(self._rt_zoom)
        elif self._rt_zoom < 0:
            self.page_render.zoomIn(-self._rt_zoom)
        self._rt_zoom = 0

    def eventFilter(self, obj, event):
        """富文本页 Ctrl+滚轮缩放（普通滚轮仍交 QTextBrowser 正常滚动）。"""
        if obj is self.page_render.viewport() \
                and event.type() == QEvent.Type.Wheel \
                and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            if event.angleDelta().y() > 0:
                self._zoom_in()
            else:
                self._zoom_out()
            event.accept()
            return True
        return super().eventFilter(obj, event)

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
