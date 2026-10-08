"""
tests/test_launcher_preview.py —— 唤出面板右侧预览窗（分派 / 异步回程 / 空格折叠）

这块全是"按类型给内容、给不出就明确说无法预览"的分支，还有跨线程回包的序号守卫，
所以逐类钉死：图片解码、纯文本读文件（含 GBK 兜底）、代码归文本、未知类型给 none；
PreviewPane 同步路径下切项、旧回包不能盖新选中；面板空格折叠预览并落盘偏好。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 早于任何 PySide6 导入

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent
from PySide6.QtWidgets import QApplication

from gui import launcher_preview as lp
from gui.dialogs_launcher import LauncherDialog
from store import app_state


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # QPainter/QFont 要有 QGuiApplication 才不崩，故 autouse：本模块每个用例都在真 QApplication 下跑
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _sync(monkeypatch, tmp_path):
    """每例一份临时偏好 + 预览走同步：离屏等不到后台线程回包。"""
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui.json")
    monkeypatch.setattr(lp, "_SYNC_PREVIEW", True)


# ---------------- 分派与读取（纯函数） ----------------

def test_category_mapping():
    assert lp.category("a.PNG") == "image"
    assert lp.category("clip.mp4") == "video"
    assert lp.category("r.docx") == "doc"
    assert lp.category("p.html") == "html"
    assert lp.category("m.md") == "text"
    assert lp.category("x.bin") == "other"


def test_read_text_gbk_fallback(tmp_path):
    f = tmp_path / "gbk.txt"
    f.write_bytes("关节不舒服".encode("gbk"))
    assert "关节" in lp.read_text(str(f))


def test_build_payload_text_and_none(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("print(1)", encoding="utf-8")
    kind, payload = lp.build_payload(str(f))
    assert kind == "text" and "print(1)" in payload
    k2, _ = lp.build_payload(str(tmp_path / "z.bin"))
    assert k2 == "none"


def test_build_payload_image(tmp_path):
    img = QImage(40, 30, QImage.Format.Format_RGB32)
    img.fill(QColor("red"))
    p = tmp_path / "r.png"
    assert img.save(str(p))
    kind, payload = lp.build_payload(str(p))
    assert kind == "image" and not payload.isNull()


def test_render_doc_thumb_not_null():
    q = lp.render_doc_thumb("第一行\n第二行\n第三行")
    assert not q.isNull()
    assert q.width() > 0 and q.height() > 0


def test_doc_thumb_never_paints_off_the_gui_thread(monkeypatch, tmp_path):
    """缩略图服务跑在 QThread 里，而 QPainter/QFont 只允许 GUI 线程用：
    在工作线程里 drawText 会直接 qFatal → 0xc0000409（本机反复崩溃的真凶，
    minidump 实测栈：QFontDatabasePrivate::ensureFontDatabase ← QPainter::drawText）。
    所以后台只能把正文包成 DocCard 带回来，绘图这一步必须在主线程。"""
    monkeypatch.setattr(lp, "_pdf_first_page_image", lambda *a, **k: QImage())
    monkeypatch.setattr(lp, "read_doc_text", lambda *a, **k: "关节不舒服\n记住这三点")
    card = lp.preview_image(str(tmp_path / "x.docx"))
    assert isinstance(card, lp.DocCard)               # 后台给的绝不是已绘制的位图
    assert "关节" in card.text
    img = card.render()                              # 主线程才画
    assert isinstance(img, QImage) and not img.isNull()


# ---------------- PDF 首页渲染（扫描/纯图片 PDF） ----------------

def _png_bytes(w=20, h=30):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("blue"))
    from PySide6.QtCore import QBuffer, QIODevice
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def test_pdf_first_page_image_no_fitz_returns_null(monkeypatch):
    """没装 PyMuPDF（pymupdf/fitz 都导不进）时必须静默给 null，不能抛——上层靠这个回退抽文字。"""
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name in ("fitz", "pymupdf"):
            raise ImportError("no pymupdf")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert lp._pdf_first_page_image("x.pdf").isNull()


def test_pdf_first_page_image_renders_with_fake_fitz(monkeypatch, tmp_path):
    """注入假 fitz 验证渲染链路：首页→缩放→PNG→QImage，抽得出非 null 图。"""
    png = _png_bytes()

    class _Rect:
        width, height = 100.0, 200.0

    class _Pix:
        def tobytes(self, fmt):
            assert fmt == "png"
            return png

    class _Page:
        rect = _Rect()

        def get_pixmap(self, matrix=None):
            return _Pix()

    class _Doc(list):
        page_count = 1

        def close(self):
            pass

    class _Matrix:
        def __init__(self, *a):
            pass

    class _Fitz:
        Matrix = _Matrix

        @staticmethod
        def open(path):
            return _Doc([_Page()])

    monkeypatch.setitem(__import__("sys").modules, "pymupdf", _Fitz())
    img = lp._pdf_first_page_image(str(tmp_path / "s.pdf"))
    assert not img.isNull()
    assert img.width() == 20 and img.height() == 30


def test_doc_preview_pdf_prefers_image_then_text(monkeypatch, tmp_path):
    p = str(tmp_path / "scan.pdf")
    good = QImage(10, 10, QImage.Format.Format_RGB32)
    monkeypatch.setattr(lp, "_pdf_first_page_image", lambda *a, **k: good)
    monkeypatch.setattr(lp, "read_doc_text", lambda *a, **k: "正文")
    assert lp._doc_preview(p) == ("image", good)
    # 渲染不出（null）→ 回退抽文字
    monkeypatch.setattr(lp, "_pdf_first_page_image",
                        lambda *a, **k: QImage())
    assert lp._doc_preview(p) == ("text", "正文")


def test_doc_preview_non_pdf_is_text(monkeypatch, tmp_path):
    monkeypatch.setattr(lp, "read_doc_text", lambda *a, **k: "docx正文")
    called = {"n": 0}

    def no_render(*a, **k):
        called["n"] += 1
        return QImage()

    monkeypatch.setattr(lp, "_pdf_first_page_image", no_render)
    assert lp._doc_preview(str(tmp_path / "a.docx")) == ("text", "docx正文")
    assert called["n"] == 0               # 非 PDF 不该去碰渲染器


def test_doc_thumb_pdf_uses_rendered_image(monkeypatch, tmp_path):
    img = QImage(16, 16, QImage.Format.Format_RGB32)
    monkeypatch.setattr(lp, "_pdf_first_page_image", lambda *a, **k: img)
    assert lp._doc_thumb(str(tmp_path / "x.pdf")) is img
    # 非 PDF / 渲染不出→只带正文回 DocCard（纸样图由主线程 render() 画）
    monkeypatch.setattr(lp, "_pdf_first_page_image", lambda *a, **k: QImage())
    monkeypatch.setattr(lp, "read_doc_text", lambda *a, **k: "正文")
    card = lp._doc_thumb(str(tmp_path / "x.pdf"))
    assert isinstance(card, lp.DocCard) and card.text == "正文"
    assert not card.render().isNull()


# ---------------- PreviewPane 三态 ----------------

def test_preview_pane_text_none_and_clear(qapp, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello preview", encoding="utf-8")
    pane = lp.PreviewPane()
    pane.resize(400, 300)
    try:
        pane.show_item({"path": str(f), "name": "a.txt", "dir": str(tmp_path)})
        assert pane.stack.currentIndex() == pane._PAGE_TEXT
        assert "hello preview" in pane.page_text.toPlainText()

        pane.show_item({"path": str(tmp_path / "z.bin"), "name": "z.bin",
                        "dir": str(tmp_path)})
        assert pane.stack.currentIndex() == pane._PAGE_NONE
        assert "无法预览" in pane.page_none.text()

        pane.show_item(None)                    # 工具/空：复位占位
        assert pane.stack.currentIndex() == pane._PAGE_NONE
    finally:
        pane.close()


def test_seq_guard_drops_stale_reply(qapp, tmp_path):
    """切换选中后，更旧的 token 回包必须被丢掉，不能盖当前内容。"""
    f = tmp_path / "a.txt"
    f.write_text("current", encoding="utf-8")
    pane = lp.PreviewPane()
    pane.resize(400, 300)
    try:
        pane.show_item({"path": str(f), "name": "a.txt"})
        pane._on_loaded("0:stale", "text", "OLD")          # 旧包
        assert pane.page_text.toPlainText() == "current"   # 未被改写
        assert pane.stack.currentIndex() == pane._PAGE_TEXT
    finally:
        pane.close()


# ---------------- MD / HTML 多模式 ----------------

def test_md_three_modes_and_toolbar(qapp, tmp_path):
    """Markdown：默认进渲染；切文本看原文；切脑图挂 MindMapView。"""
    f = tmp_path / "n.md"
    f.write_text("# 标题一\n正文\n## 子标题\n- 甲\n- 乙\n", encoding="utf-8")
    pane = lp.PreviewPane()
    pane.resize(500, 400)
    try:
        pane.show_item({"path": str(f), "name": "n.md"})
        # 三态按钮齐备
        assert set(pane._mode_btns) == {"text", "render", "mindmap"}
        assert not pane._mode_bar.isHidden()
        # 默认渲染页（QTextBrowser.setMarkdown）
        assert pane.stack.currentIndex() == pane._PAGE_RENDER
        # 文本模式：看原始 markdown
        pane._set_mode("text")
        assert pane.stack.currentIndex() == pane._PAGE_TEXT
        assert "# 标题一" in pane.page_text.toPlainText()
        # 脑图模式：懒建 MindMapView 并进对应页
        pane._set_mode("mindmap")
        assert pane._mm is not None
        assert pane.stack.currentWidget() is pane._mm
    finally:
        pane.close()


def test_html_source_and_render_fallback(qapp, tmp_path, monkeypatch):
    """HTML：源码模式看原文；渲染模式在无 WebEngine（造不出）时回退 QTextBrowser，不崩。"""
    f = tmp_path / "p.html"
    f.write_text("<html><body><h1>Hi</h1></body></html>", encoding="utf-8")
    pane = lp.PreviewPane()
    pane.resize(500, 400)
    # 强制懒构造失败，走回退分支（离屏不去碰真正的 WebEngine）
    monkeypatch.setattr(pane, "_ensure_html", lambda: None)
    try:
        pane.show_item({"path": str(f), "name": "p.html"})
        assert set(pane._mode_btns) == {"render", "source"}
        # 默认渲染：回退到 page_render.setHtml
        assert pane.stack.currentIndex() == pane._PAGE_RENDER
        assert "Hi" in pane.page_render.toHtml()
        # 源码：原始 HTML 文本
        pane._set_mode("source")
        assert pane.stack.currentIndex() == pane._PAGE_TEXT
        assert "<h1>Hi</h1>" in pane.page_text.toPlainText()
    finally:
        pane.close()


# ---------------- 图片预览：可缩放 / 单击打开 / 缩放条显隐 ----------------

def _make_png(tmp_path):
    img = QImage(60, 40, QImage.Format.Format_RGB32)
    img.fill(QColor("green"))
    p = tmp_path / "r.png"
    assert img.save(str(p))
    return str(p)


def test_image_pane_is_zoomable(qapp, tmp_path):
    """图片项挂的是 ZoomImageView（不是旧的 QLabel）：能缩放且不会炸，缩放条亮起。"""
    pane = lp.PreviewPane()
    pane.resize(400, 300)
    try:
        pane.show_item({"path": _make_png(tmp_path), "name": "r.png"})
        assert isinstance(pane.page_image, lp.ZoomImageView)
        assert pane.stack.currentIndex() == pane._PAGE_IMAGE
        # 三个缩放按钮都亮（setVisible(True) 后 isHidden 为假）
        assert pane._zoom_btns and all(not b.isHidden() for b in pane._zoom_btns)
        pane.page_image.zoom_in()
        pane.page_image.zoom_out()
        pane.page_image.reset_fit()
    finally:
        pane.close()


def test_text_hides_image_zoom_bar(qapp, tmp_path):
    """非图片（文本）不该看见图片缩放条。"""
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    pane = lp.PreviewPane()
    pane.resize(400, 300)
    try:
        pane.show_item({"path": str(f), "name": "a.txt"})
        assert pane._zoom_btns and all(b.isHidden() for b in pane._zoom_btns)
    finally:
        pane.close()


def test_image_click_opens_existing_path(qapp, tmp_path):
    """单击预览图→openRequested 携真实存在的路径；路径不存在（删了）则不发。"""
    path = _make_png(tmp_path)
    pane = lp.PreviewPane()
    got = []
    pane.openRequested.connect(got.append)
    try:
        pane._pv_path = path
        pane._on_image_click()
        assert got == [path]
        got.clear()
        pane._pv_path = str(tmp_path / "gone.png")   # 不存在
        pane._on_image_click()
        assert got == []
    finally:
        pane.close()


# ---------------- 面板：预览单击打开 / 钉住不自动收起 ----------------

def test_dialog_open_path_only(qapp, monkeypatch):
    """面板接上 openRequested：_open_path_only 开文件并收起面板；不存在的
    路径直接忽略（不报错、不开）。"""
    import tempfile
    from utils import desktop_utils
    calls = []
    monkeypatch.setattr(desktop_utils, "open_path", lambda p: calls.append(p))
    d = LauncherDialog(lambda name: None, None)
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        d.show()
        d._open_path_only(path)
        assert calls == [path]
        assert d.isHidden()
        calls.clear()
        d._open_path_only("Q:\\nope.png")          # 不存在 → 忽略
        assert calls == []
    finally:
        d.close()
        try:
            os.unlink(path)
        except OSError:
            pass


def test_pin_blocks_autohide_and_persists(qapp, monkeypatch):
    """钉住：失焦（ActivationChange 且非活动窗）不自动收起；取消钉住则照旧收。"""
    d = LauncherDialog(lambda name: None, None)
    d.show()
    try:
        d._armed = True
        monkeypatch.setattr(d, "isActiveWindow", lambda: False)
        # 钉住
        d.btn_pin.setChecked(True)
        d._toggle_pin()
        assert d._pinned is True
        assert app_state.get("launcher_pinned") is True
        d.changeEvent(QEvent(QEvent.Type.ActivationChange))
        assert not d.isHidden()                    # 钉住不收
        # 取消钉住→失焦就收
        d.btn_pin.setChecked(False)
        d._toggle_pin()
        assert d._pinned is False
        d.show()
        d._armed = True
        d.changeEvent(QEvent(QEvent.Type.ActivationChange))
        assert d.isHidden()
    finally:
        d.close()


# ---------------- 缩略图线程生命周期（QThread 不能比它的线程先死） ----------------

def test_thumb_worker_is_process_level_not_dialog_child(qapp):
    """面板拿到的是进程级常驻单例，而且不是任何窗口的子对象。

    挂在对话框上时，退出会走 destroyQCoreApplication → QWidget::~QWidget →
    deleteChildren 析构一个还在跑的 QThread → qFatal("QThread: Destroyed while
    thread is still running")：本机那几起崩溃的 minidump 栈就是这个。"""
    d = LauncherDialog(lambda name: None, None)
    try:
        assert d._thumb is lp.img_worker()          # 同一个单例，不是每次新建
        assert d._thumb.parent() is None            # 不是对话框的子对象
        d._thumb.start()
        d.close()                                    # 关窗不能把还在跑的线程一起析构
        assert lp._IMG_WORKER is d._thumb
        assert d._thumb.isRunning() or d._thumb.isFinished()   # 对象仍然可用，没被提前销毁
    finally:
        lp.shutdown_img_worker()
    assert d._thumb.isFinished()


def test_img_worker_restarts_after_shutdown(qapp):
    """shutdown 之后还能拿到一条新线程（不能把已经停掉的单例继续发回去）。"""
    lp.shutdown_img_worker()
    w = lp.img_worker()
    assert w is not None and w.parent() is None
    lp.shutdown_img_worker()


# ---------------- 面板：空格折叠预览 ----------------

def test_space_toggles_preview_and_persists(qapp, monkeypatch):
    d = LauncherDialog(lambda name: None, None)
    monkeypatch.setattr(d, "_load_tools", lambda: [])
    d.show()
    try:
        assert not d._preview.isHidden()                  # 默认展开
        d.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space,
                                  Qt.KeyboardModifier.NoModifier))
        assert d._preview.isHidden()
        assert app_state.get("launcher_preview_open") is False
        d.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Space,
                                  Qt.KeyboardModifier.NoModifier))
        assert not d._preview.isHidden()
        assert app_state.get("launcher_preview_open") is True
    finally:
        d.close()
