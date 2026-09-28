"""
video_text_tools/asr/types.py —— 语音识别主体的数据结构与异常（纯定义、零重依赖）

这里是「语音识别」对外统一的口径：转写产出 TranscriptWord / TranscriptSegment，
识别异常 TranscribeError → DepMissing / ModelNotReady，语义纠错异常 FixError →
FixNotConfigured。爆款拆解（breakdown）、录屏字幕（subtitle）等消费者都复用这一份，
绝不各定义一份——异常尤其讲「同一个类对象」，否则调用方 except 抓不到识别侧抛的错。

无 Qt、无网络、无重依赖：纯 dataclass + 异常，可安全在任意层导入。
"""
from dataclasses import dataclass, field


# ---------------- 异常 ----------------
class AsrError(Exception):
    """语音识别相关异常基类：消息都要人能看懂，直接进界面提示 / stage_status。"""


class TranscribeError(AsrError):
    """转写失败（模型未就绪、依赖缺失、ffmpeg 抽音频失败等）。"""


class DepMissing(TranscribeError):
    """faster-whisper 等可选重依赖未安装：消息给「装什么」的可执行提示。"""


class ModelNotReady(TranscribeError):
    """Whisper 模型尚未下载到本地：GUI 据此弹下载引导，不静默跳过。"""


class FixError(AsrError):
    """语义纠错（大模型改同音字）调用失败：未配置之外的网络/返回/解析异常。"""


class FixNotConfigured(FixError):
    """纠错端点未配置（缺 api_key 或 model）：GUI 引导去接口管理页配置，不静默。"""


# ---------------- 数据结构 ----------------
@dataclass
class TranscriptWord:
    """词级时间戳（faster-whisper word_timestamps=True）。"""
    word: str
    start: float
    end: float


@dataclass
class TranscriptSegment:
    """一句/一段转写：起止时间 + 文本 + 词级明细。"""
    start: float
    end: float
    text: str
    words: list = field(default_factory=list)   # [TranscriptWord]
