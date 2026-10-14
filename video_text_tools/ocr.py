"""
video_text_tools/ocr.py —— 截图「识图取字」的 OCR 引擎层（纯离线，无接口 token）

两套后端按可用性自动选，调用方只管 recognize(pixmap) 拿文字：
- RapidOCR（rapidocr_onnxruntime）：Paddle PP-OCR 的 onnxruntime 版，中文最准，
  但带 onnxruntime + opencv（几十 MB），作为「装了才用」的可选增强，优先命中；
- Windows 内置 OCR（winocr 调系统 Windows.Media.Ocr）：几乎 0 体积、离线、无 token，
  中文效果中等，且依赖本机装了「中文 OCR 语言包」——没装 winsdk/winocr 会自动跳过。

两者都没装 → available() 返回 False，上层给明确「装哪个」的提示。
纪律：一律懒加载（没装就在 import 处失败并被吞掉，绝不连累截图主链路）；
      任何异常都吞掉、返回空串/None，识别失败不能把遮罩带崩。
识别时 RapidOCR 吃临时 PNG 文件（它能直接读路径），Windows 内置 OCR 直接喂 QImage 的 RGBA 字节。
"""
import os
import re
import tempfile

# 进程内缓存：None=未探测 / False=不可用 / 其它=可用的引擎实例或标志
_RAPID = None
_WIN = None


def _tmp_png(pm):
    """把 QPixmap 落一张临时 PNG，返回路径；失败返回空串。调用方负责删。"""
    if pm is None or pm.isNull():
        return ""
    fd, path = tempfile.mkstemp(suffix=".png", prefix="ocr_")
    os.close(fd)
    try:
        if not pm.save(path, "PNG"):
            _safe_remove(path)
            return ""
        return path
    except Exception:
        _safe_remove(path)
        return ""


def _safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _rapidocr():
    """懒加载并缓存 RapidOCR 实例；没装/初始化失败返回 None。"""
    global _RAPID
    if _RAPID is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _RAPID = RapidOCR()
        except Exception:
            _RAPID = False
    return _RAPID or None


def _win_ready():
    """懒探测 Windows 内置 OCR（winocr）是否可用；结果缓存。"""
    global _WIN
    if _WIN is None:
        try:
            import winocr  # noqa: F401  依赖 winsdk，import 失败即视为不可用
            _WIN = True
        except Exception:
            _WIN = False
    return bool(_WIN)


def _run_rapidocr(pm):
    """B：RapidOCR。返回 None=不可用 / ''=没字 / 非空=命中。"""
    engine = _rapidocr()
    if engine is None:
        return None
    path = _tmp_png(pm)
    if not path:
        return None
    try:
        result, _ = engine(path)
        if not result:
            return ""
        # result 每项形如 [四点框, 文本, 置信度]
        return "\n".join(str(line[1]) for line in result)
    except Exception:
        return ""
    finally:
        _safe_remove(path)


def _run_winocr(pm):
    """A：Windows 内置 OCR（winrt.windows.media.ocr）。返回 None=不可用 / ''=没字或语言包缺 / 非空=命中。

    直接喂 QImage 的 RGBA8888 字节给 SoftwareBitmap，不绕 PIL/临时图（多一个 Pillow 依赖不如不加，
    且打包体积优先）。语言跟随系统用户配置文件（中文系统即用中文 OCR 引擎）。"""
    if not _win_ready():
        return None
    if pm is None or pm.isNull():
        return None
    try:
        import asyncio
        from PySide6.QtGui import QImage
        from winrt.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter
    except Exception:
        return ""
    try:
        qimg = pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        w, h = qimg.width(), qimg.height()
        if w <= 0 or h <= 0:
            return ""
        # PySide6 的 constBits() 直接返回整块缓冲区 memoryview（RGBA8888 无行填充，
        # 长度就是 w*h*4）；旧写法调 .setsize 在新版会变 memoryview 报错。
        data = bytes(qimg.constBits())
        writer = DataWriter()
        writer.write_bytes(data)
        sb = SoftwareBitmap.create_copy_from_buffer(
            writer.detach_buffer(), BitmapPixelFormat.RGBA8, w, h)
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            return ""
        return asyncio.run(_ocr_lines(engine.recognize_async(sb)))
    except Exception:
        return ""


async def _ocr_lines(awaitable):
    """把 WinRT 异步识别结果拼成行文本。

    Windows OCR 会把每个 CJK 字符当个“词”，字间插空格（你好→你 好）：把两个
    汉字之间的空格去掉，复制出来才是正常中文（西文/数字间空格保留）。"""
    result = await awaitable
    text = "\n".join(line.text for line in result.lines)
    cjk = r"\u4e00-\u9fff\u3400-\u4dbf\uff00-\uffef"
    text = re.sub(r"(?<=[%s])\s+(?=[%s])" % (cjk, cjk), "", text)
    return text.strip()


def available():
    """是否至少装了一套 OCR 后端。"""
    return _rapidocr() is not None or _win_ready()


def recognize(pm):
    """识别图上的文字，返回拼好的纯文本（无字/无引擎给空串）。

    优先 RapidOCR（中文更准），没装或这次没出字再退 Windows 内置 OCR。"""
    for fn in (_run_rapidocr, _run_winocr):
        text = fn(pm)
        if text:                       # 有字就直接用
            return text
    return ""
