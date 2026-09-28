"""
video_text_tools/asr —— 语音识别「主体」能力包

设计定位：语音识别是整个链路的主角，爆款拆解 / 录屏字幕 / 实时字幕浮窗都只是它的
消费者。于是把 faster-whisper 的模型管理、词级转写、SRT 渲染、以及识别后的 DeepSeek
语义纠错统一收在这里，任何功能都从这一处拿识别，不再各自伸手向 breakdown。

子模块：
  types       数据结构（TranscriptSegment/TranscriptWord）+ 异常（唯一来源，保类对象同一性）
  transcribe  模型就绪判定 / 下载（直链续传+hub 兜底）/ 逐句流式转写 / to_srt
  fixer       AsrFixer：整篇逐字稿送 OpenAI 兼容端点按语义改同音字，按编号回填不动时间戳

无 Qt、无自我 UI：对外都是「函数 + 回调（log/progress/should_stop）」，由 gui 层用
ToolWorker/QThread 调用。重依赖（faster_whisper）一律延迟导入，缺失明确抛错不静默。

导入约定：`transcribe` / `fixer` / `types` 一律以**子模块**形式暴露（沿用原
`from ..breakdown import transcribe as tr` 的用法，tr.model_ready / tr.transcribe /
tr.WHISPER_SIZES 等直接从模块取）。切勿在这里再 `from .transcribe import transcribe`
——那会用同名函数遮蔽子模块，令 `from ..asr import transcribe` 拿到函数而非模块。
"""
from . import types  # noqa: F401  子模块（异常/数据结构的唯一来源）
from . import transcribe  # noqa: F401  子模块：模型管理 + 转写 + to_srt
from . import fixer  # noqa: F401  子模块：AsrFixer
from .types import (  # noqa: F401  异常/数据类型同时挂到包顶层，方便 from ..asr import X
    AsrError, TranscribeError, DepMissing, ModelNotReady,
    FixError, FixNotConfigured,
    TranscriptWord, TranscriptSegment,
)
from .fixer import AsrFixer, apply_fix  # noqa: F401

__all__ = [
    "types", "transcribe", "fixer",
    "AsrError", "TranscribeError", "DepMissing", "ModelNotReady",
    "FixError", "FixNotConfigured", "TranscriptWord", "TranscriptSegment",
    "AsrFixer", "apply_fix",
]
