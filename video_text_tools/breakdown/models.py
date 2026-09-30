"""
video_text_tools/breakdown/models.py —— 爆款拆解的数据结构与异常

纯数据 + 异常，无 Qt、无网络。各阶段（解析/抽帧/转写/Vision/提示词）共用这里的
口径：BreakdownResult 是最终交付物，也是 Excel 回写与 GUI 预览的唯一数据源；
stage_status 记录每个阶段 ok / fail:原因，任一失败都不静默（约束7）。

⚠ 转写相关的数据结构与异常（TranscriptSegment/TranscriptWord 与 TranscribeError/
DepMissing/ModelNotReady）已上收到「语音识别主体」包 video_text_tools.asr.types，
这里只做 re-export——既让老调用方 `from .models import …` 照常可用，又保证异常是
「同一个类对象」（否则 pipeline 的 except 抓不到 asr 抛的错）。
"""
import dataclasses
from dataclasses import dataclass, field

from ..asr.types import (  # noqa: F401  转写类型/异常统一取自语音识别主体包
    TranscribeError, DepMissing, ModelNotReady,
    TranscriptWord, TranscriptSegment,
)


def _slim(cls, d):
    """只取 cls 认识的字段：缺的走默认、多的丢弃，容忍旧/新 payload 往返。"""
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in (d or {}).items() if k in names}


def _segment_from_dict(d):
    """重建 TranscriptSegment（含词级明细），TranscriptSegment 定义在 asr 包里。"""
    d = d or {}
    words = [TranscriptWord(**_slim(TranscriptWord, w)) for w in (d.get("words") or [])]
    kw = _slim(TranscriptSegment, d)
    kw["words"] = words
    return TranscriptSegment(**kw)


# ---------------- 阶段名（stage_status 的键，GUI/Excel 共用同一份常量） ----------------
STAGE_ACQUIRE = "解析下载"
STAGE_FRAMES = "抽帧"
STAGE_TRANSCRIBE = "转写"
STAGE_VISION = "视觉分析"
STAGE_BLOCKS = "板块拆分"
STAGE_PROMPTS = "提示词生成"
STAGE_REWRITE = "改写去重"
STAGES = [STAGE_ACQUIRE, STAGE_FRAMES, STAGE_TRANSCRIBE,
          STAGE_VISION, STAGE_BLOCKS, STAGE_PROMPTS, STAGE_REWRITE]


class BreakdownError(Exception):
    """拆解链路通用异常基类：消息都要人能看懂，直接进 stage_status 与弹窗。"""


class AcquireError(BreakdownError):
    """解析/下载失败（无有效链接、接口未返回直链、域名不在白名单、下载出错）。"""


class FrameError(BreakdownError):
    """抽帧失败（ffmpeg 缺失或执行报错）。"""


class VisionError(BreakdownError):
    """豆包 Vision 调用失败（未配置、限流重试耗尽、返回结构异常）。"""


class ConfigError(VisionError):
    """豆包接入点未配置（缺 api_key 或 endpoint）。"""


@dataclass
class FrameShot:
    """一张关键帧：序号 / 时间点(秒) / 落盘路径 / 是否场景切换帧。"""
    idx: int
    ts: float
    path: str
    is_scene: bool = False

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**_slim(cls, d))


@dataclass
class FrameAnalysis:
    """单帧的视觉分析结果（豆包 Vision 逐帧产出）。"""
    idx: int
    ts: float
    shot_size: str = ""      # 景别
    camera: str = ""         # 运镜
    composition: str = ""    # 构图
    transition: str = ""     # 转场
    on_screen_text: str = ""  # 画面文字
    emotion: str = ""        # 情绪
    raw: str = ""            # 原始返回文本（兜底/排查用）

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**_slim(cls, d))


@dataclass
class SegmentPrompts:
    """一个分镜段落的 3 类提示词 + 该段概要。"""
    index: int
    time_range: str          # 如 "00:00-00:03"
    visual_prompt: str = ""  # 画面生成
    copy_prompt: str = ""    # 文案改写
    shoot_prompt: str = ""   # 复刻拍摄
    summary: str = ""

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**_slim(cls, d))


@dataclass
class BlockSegment:
    """一个营销板块（钩子/痛点/行动号召…）在时间轴上的一段。

    start/end 为秒（钳在 [0,duration] 且区间互不重叠，见 prompts.build_blocks）；
    type 限定在配置的板块类型白名单内。summary 是该板块的一句话说明。"""
    index: int
    type: str
    start: float
    end: float
    summary: str = ""

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**_slim(cls, d))


@dataclass
class OverallAnalysis:
    """整条视频的可复用分析。"""
    hook_desc: str = ""       # 前 3 秒钩子描述
    hook_score: str = ""      # 0-10
    factors: str = ""         # 爆点因素（分点）
    emotion_curve: str = ""   # 情绪曲线
    formula: str = ""         # 内容公式
    blueprint: str = ""       # 复刻蓝图

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**_slim(cls, d))


@dataclass
class BreakdownResult:
    """一条链接的完整拆解结果——Excel 回写与 GUI 预览的唯一数据源。"""
    link: str = ""
    title: str = ""
    video_path: str = ""
    duration: float = 0.0
    frames: list = field(default_factory=list)            # [FrameShot]
    frame_analyses: list = field(default_factory=list)    # [FrameAnalysis]
    transcript: list = field(default_factory=list)        # [TranscriptSegment]
    transcript_text: str = ""                             # 逐字稿纯文本
    segments: list = field(default_factory=list)          # [SegmentPrompts]
    blocks: list = field(default_factory=list)            # [BlockSegment] 营销板块时间轴
    overall: OverallAnalysis = field(default_factory=OverallAnalysis)
    shot_count: int = 0
    shot_durations: str = ""                              # 每段时长串
    # 每阶段状态：值 ∈ {"ok"} 或 "fail:原因"；缺键＝该阶段未执行
    stage_status: dict = field(default_factory=dict)
    cost: dict = field(default_factory=lambda: {"vision_calls": 0, "tokens": 0})
    notes: list = field(default_factory=list)             # 非致命提醒
    # —— 任务库 / 详情三屏联动扩展字段（默认空，不影响现有构造与测试）——
    report_path: str = ""                                 # 导出的 Word 文档路径
    cover_path: str = ""                                  # 封面（首帧）图路径
    gallery: list = field(default_factory=list)           # [{"idx","ts","path"}] 图集
    task_id: int = 0                                      # 入库后回填 breakdown_tasks.id
    source_kind: str = "url"                              # 来源类型（预留）

    def to_dict(self):
        """整条结果转纯 dict（递归含嵌套 dataclass），供 JSON 落库。"""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d):
        """从 payload dict 重建 Breakdown（逐字段回填、容忍缺/多字段）。"""
        d = d or {}
        kw = _slim(cls, d)
        kw["frames"] = [FrameShot.from_dict(x) for x in (d.get("frames") or [])]
        kw["frame_analyses"] = [FrameAnalysis.from_dict(x)
                                for x in (d.get("frame_analyses") or [])]
        kw["transcript"] = [_segment_from_dict(x) for x in (d.get("transcript") or [])]
        kw["segments"] = [SegmentPrompts.from_dict(x) for x in (d.get("segments") or [])]
        kw["blocks"] = [BlockSegment.from_dict(x) for x in (d.get("blocks") or [])]
        kw["overall"] = OverallAnalysis.from_dict(d.get("overall") or {})
        kw["gallery"] = list(d.get("gallery") or [])
        return cls(**kw)

    def stage_ok(self, stage):
        return self.stage_status.get(stage) == "ok"

    def mark(self, stage, ok=True, reason=""):
        """记一个阶段结果：ok→"ok"，否则 "fail:原因"（原因缺省给个通用词）。"""
        if ok:
            self.stage_status[stage] = "ok"
        else:
            self.stage_status[stage] = f"fail:{reason or '未提供原因'}"

    def failed_stages(self):
        return [s for s in STAGES
                if str(self.stage_status.get(s, "")).startswith("fail:")]

    def is_partial(self):
        """半成品：任一已执行阶段失败，或压根没解析出视频——不写盘，只提示。"""
        return bool(self.failed_stages()) or not self.stage_ok(STAGE_ACQUIRE)
