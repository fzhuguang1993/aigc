"""
video_text_tools/subtitle/srt.py —— 纯文本 SRT 字幕落盘

正文渲染直接复用「语音识别主体」的 asr.transcribe.to_srt（按句给起止、已有实现），这里只负责
落盘为 UTF-8。SRT 是「兜底可编辑」产物：拿去剪映/别的剪辑器二次精修用；带样式与
卖点上色走 ass.py 的 .ass（烧录用）。
"""
from pathlib import Path


def write_srt(segments, path):
    """把转写段落写成 .srt，返回落盘路径。"""
    from ..asr import transcribe as tr
    text = tr.to_srt(segments)
    Path(path).write_text(text, encoding="utf-8")
    return str(path)
