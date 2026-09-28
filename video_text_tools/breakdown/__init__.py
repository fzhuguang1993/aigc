"""
video_text_tools/breakdown —— 爆款拆解纯逻辑层

无 Qt、无自我 UI：每个模块只做一件事，对外都是「函数 + 回调（log/progress/should_stop）」，
由 gui.dialogs_breakdown.BreakdownPanel 通过 ToolWorker 调用。口径统一收在 models.py：
BreakdownResult 是最终交付物，stage_status 记录每阶段 ok / fail:原因，任一失败不静默。

子模块：
  acquire     分享文案 → 去水印视频直链 → 下载（复用 material_extract 聚客解析）
  frames      ffmpeg 每 N 秒抽帧 + 场景切换检测，按时间戳去重
  vision      豆包 Vision 逐帧分析（方舟 OpenAI 兼容端点，3 并发 + 退避重试）
  prompts     3 类提示词生成 + 整体分析 + 改写去重（复用同一豆包接入点）
  pipeline    串起以上阶段的总编排（可降级继续、汇总成本、收尾清理）
  report      拆解结果导出 Word（.docx）报告（取代旧的 Excel 回写，视频同目录同名）
  gallery     按分镜时间戳抽「图集 + 封面」，供任务库详情页三屏联动（持久化）
  timeline    详情页时间轴纯逻辑：分镜/口播行归一 + 播放位置→当前条目定位

注：语音识别（faster-whisper 模型管理/转写/to_srt）与识别后语义纠错（AsrFixer）已上收
到「语音识别主体」包 video_text_tools.asr，拆解只是它的消费者之一；转写类型/异常经
models.py 从 asr.types re-export，保持类对象同一性。
"""
from .models import (  # noqa: F401  对外统一从这里取数据结构与异常
    STAGES, STAGE_ACQUIRE, STAGE_FRAMES, STAGE_TRANSCRIBE,
    STAGE_VISION, STAGE_PROMPTS, STAGE_REWRITE,
    BreakdownError, AcquireError, FrameError, TranscribeError,
    DepMissing, ModelNotReady, VisionError, ConfigError,
    FrameShot, FrameAnalysis, TranscriptWord, TranscriptSegment,
    SegmentPrompts, OverallAnalysis, BreakdownResult,
)
from .pipeline import run  # noqa: F401
from .report import write_report  # noqa: F401  docx 延迟导入，包加载不依赖 python-docx
from .gallery import build_gallery  # noqa: F401  ffmpeg 延迟导入（函数内），包加载不依赖
from .timeline import build_timeline, index_at  # noqa: F401  纯逻辑，无 Qt
