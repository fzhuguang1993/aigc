"""
video_text_tools/subtitle/models.py —— 录屏字幕的数据结构与异常

纯数据 + 异常，无 Qt、无网络。转写口径与异常直接取自「语音识别主体」包
（video_text_tools.asr.types 的 TranscriptSegment / DepMissing / ModelNotReady /
TranscribeError），避免两处各定义一份；豆包配置异常 ConfigError 仍取自 breakdown
（卖点高亮 / 字幕检测都靠豆包 Vision）。SubtitleResult 是每个视频的最终交付物，
任一环节失败都不静默：落 ok=False + 人能看懂的 message（约束同拆解）。
"""
from dataclasses import dataclass, field

# 转写口径取自语音识别主体（单一来源，保类对象同一性）；豆包配置异常仍取 breakdown
from ..asr.types import (  # noqa: F401
    TranscriptSegment, TranscriptWord, DepMissing, ModelNotReady, TranscribeError,
)
from ..breakdown.models import ConfigError  # noqa: F401


class SubtitleError(Exception):
    """字幕链路通用异常基类：消息要人能看懂，直接进 SubtitleResult.message 与弹窗。"""


@dataclass
class SubtitleStyle:
    """ASS 样式（前景/描边/阴影、位置、卖点强调色、断句宽度）。"""
    font_size: int = 16
    primary: str = "#FFFFFF"        # 正文前景 #RRGGBB
    outline: str = "#000000"        # 描边色
    back: str = "#00000000"         # 阴影/背板色（可带末两位透明度）
    align: str = "bottom"           # bottom | top
    margin_v: int = 40              # 距边垂直留白
    highlight: str = "#FFD400"      # 卖点强调色
    max_chars: int = 18             # 单行超此字数按宽度断行（\N）

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        names = set(cls.__dataclass_fields__)
        kw = {k: v for k, v in d.items() if k in names and v is not None}
        return cls(**kw)


@dataclass
class SubtitleOptions:
    """一次字幕处理的参数集（由 SubtitleOptionsDialog / 语音识别工具组装）。"""
    model_size: str = "medium"
    detect: bool = False            # 抽帧检测是否已带烧录字幕
    gen_srt: bool = True            # 导出纯文本 .srt
    highlight: bool = False         # 卖点语义高亮（依赖豆包）
    burn: bool = False              # 烧录成片
    asr_fix: bool = False           # 识别后送 DeepSeek 语义纠错（未配置则自动跳过不抛）
    save_txt: bool = False          # 额外导出纯文本逐字稿 .txt
    style: SubtitleStyle = field(default_factory=SubtitleStyle)
    out_dir: str = ""               # 空 = 各视频同目录
    mirror: bool = False            # 下载模型走国内镜像


@dataclass
class SubtitleResult:
    """单个视频的字幕处理结果（run_one 的交付物）。"""
    video_path: str = ""
    ok: bool = False
    srt_path: str = ""
    ass_path: str = ""
    txt_path: str = ""
    burned_path: str = ""
    has_subtitle: object = None     # True=已带 / False=没有 / None=未检测或未知
    segments: list = field(default_factory=list)      # [TranscriptSegment]
    highlight_terms: list = field(default_factory=list)
    message: str = ""
    error: str = ""                 # 失败归类："DepMissing"/"ModelNotReady"/...（GUI 据此给引导）

    @property
    def name(self):
        import os
        return os.path.basename(self.video_path)

    @property
    def transcript_text(self):
        """逐字稿纯文本（按句拼行）：语音识别工具预览用这个。"""
        return "\n".join(getattr(s, "text", "") or "" for s in self.segments)
