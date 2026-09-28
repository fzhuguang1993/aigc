"""
video_text_tools/subtitle —— 录屏字幕生成与烧录（纯逻辑层，无 Qt）

无自我 UI：对外都是「函数 + 回调（log/progress/should_stop）」，由 GUI 通过 ToolWorker
调用。口径统一收在 models.py，SubtitleResult 记录每个视频的结果，任一失败不静默。

子模块：
  models     数据结构（SubtitleStyle/Options/Result）+ 复用语音识别主体的异常
  audio      音轨解析（旁路 WAV 复制成临时副本，规避覆写源音轨的陷阱）
  srt        纯文本 .srt 落盘（复用 asr.transcribe.to_srt）
  ass        带样式/卖点上色的 .ass 渲染
  burn       ffmpeg 烧录命令构建 + libass 能力探测
  detect     抽帧 + 豆包判断"画面是否已带字幕"
  highlight  豆包挑卖点词（供 ass 逐词上色）
  engine     run_one / run_batch 总编排

重依赖（faster-whisper）与豆包凭证都延迟读取；缺库/缺模型/缺豆包各自降级，不崩。
"""
from .models import (  # noqa: F401
    SubtitleStyle, SubtitleOptions, SubtitleResult, SubtitleError,
    TranscriptSegment, DepMissing, ModelNotReady,
)
from .audio import find_sidecar, resolve_audio_source  # noqa: F401
from .engine import run_one, run_batch  # noqa: F401
