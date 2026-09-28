"""
tests/test_breakdown_pipeline.py —— 总编排（mock 全链路外部 IO）

happy path 出完整 BreakdownResult；分别注入 解析/抽帧/转写/视觉 失败，断言
stage_status 明确标注、能降级的继续降级（转写失败仍出画面分析）、不静默。
"""
import json

from video_text_tools.breakdown import pipeline
from video_text_tools.breakdown import vision as vision_mod
from video_text_tools.breakdown.models import (
    AcquireError, FrameError, DepMissing, ConfigError, VisionError,
    FrameShot, FrameAnalysis, TranscriptSegment,
    STAGE_ACQUIRE, STAGE_FRAMES, STAGE_TRANSCRIBE,
    STAGE_VISION, STAGE_PROMPTS, STAGE_REWRITE)

FAKE_CFG = {"api_key": "k", "endpoint": "ep-1", "base_url": "https://x/api/v3"}


class FakeVision:
    """替身豆包：逐帧回固定 FrameAnalysis；文本按提示词种类回 JSON。"""

    def __init__(self, cfg):
        if not (cfg or {}).get("api_key"):
            raise ConfigError("未配置")
        self.calls = 0

    def analyze_frames(self, frames, prompt, concurrency=1, log=None,
                       progress=None, inter_frame_delay=0.0):
        return [FrameAnalysis(idx=f.idx, ts=f.ts, shot_size="特写",
                              camera="推", emotion="紧迫") for f in frames]

    def analyze_video(self, url, prompt=None, fps=0.5, timeout=600, log=None):
        """video_url 直连替身：收公网直链 url，回两个固定分镜。"""
        self.calls += 1
        return [FrameAnalysis(idx=1, ts=0.0, shot_size="近景", camera="手持"),
                FrameAnalysis(idx=2, ts=4.0, shot_size="特写", camera="推")]

    def chat_text(self, prompt, temperature=0.4, max_tokens=2048):
        self.calls += 1
        if "整体分析" in prompt:
            return json.dumps({"hook_desc": "前3秒抛痛点", "hook_score": "8",
                               "factors": "反差；悬念", "emotion_curve": "起伏",
                               "formula": "痛点+方案", "blueprint": "分步复刻"})
        if "规避平台查重" in prompt:                       # 改写去重
            return json.dumps([{"index": 1, "copy_prompt": "改写后文案"}])
        return json.dumps([{"index": 1, "time_range": "00:00-00:03",   # 三类提示词
                            "summary": "开场", "visual_prompt": "画面V",
                            "copy_prompt": "原文案", "shoot_prompt": "拍摄S"}])

    def cost(self):
        return {"vision_calls": self.calls, "tokens": 100}


def _ok_acquire(video="/tmp/v.mp4"):
    return (lambda text, out, hosts=None, log=None, api_cfg=None:
            (video, "标题T", ["备注"], "https://x/v.mp4"))


def _ok_frames(*a, **k):
    return [FrameShot(1, 0.0, "f1.jpg"), FrameShot(2, 3.0, "f2.jpg", True)]


def _ok_transcribe(*a, **k):
    return ([TranscriptSegment(0.0, 3.0, "每天两条", [])], "每天两条")


def _prime(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline.acquire, "resolve_and_download", _ok_acquire())
    monkeypatch.setattr(pipeline.frames_mod, "extract_frames", _ok_frames)
    monkeypatch.setattr(pipeline.transcribe_mod, "transcribe", _ok_transcribe)
    monkeypatch.setattr(vision_mod, "DoubaoVision", FakeVision)
    return {"doubao_cfg": FAKE_CFG, "out_dir": str(tmp_path),
            "vision_mode": "frames"}      # 钉住旧逐帧链路（默认已是 video_url）


def _opts(tmp_path):
    return {"doubao_cfg": FAKE_CFG, "out_dir": str(tmp_path), "model_size": "tiny",
            "vision_mode": "frames"}


def test_happy_path(tmp_path, monkeypatch):
    opts = _prime(monkeypatch, tmp_path)
    res = pipeline.run("https://v.douyin.com/x", opts)
    assert not res.is_partial()
    assert res.title == "标题T"
    assert res.stage_ok(STAGE_TRANSCRIBE) and res.stage_ok(STAGE_VISION)
    assert res.stage_ok(STAGE_PROMPTS) and res.stage_ok(STAGE_REWRITE)
    assert res.segments and res.segments[0].visual_prompt == "画面V"
    assert res.segments[0].copy_prompt == "改写后文案"       # 改写去重生效
    assert res.overall.hook_score == "8"
    assert res.cost["tokens"] >= 100


def test_acquire_failure(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AcquireError("接口没返回视频")
    monkeypatch.setattr(pipeline.acquire, "resolve_and_download", boom)
    monkeypatch.setattr(vision_mod, "DoubaoVision", FakeVision)
    res = pipeline.run("bad", _opts(tmp_path))
    assert str(res.stage_status[STAGE_ACQUIRE]).startswith("fail:")
    assert "接口没返回视频" in res.stage_status[STAGE_ACQUIRE]
    assert res.is_partial()
    assert STAGE_FRAMES not in res.stage_status              # 提前返回，未执行


def test_frames_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.acquire, "resolve_and_download", _ok_acquire())

    def boom(*a, **k):
        raise FrameError("ffmpeg 缺失")
    monkeypatch.setattr(pipeline.frames_mod, "extract_frames", boom)
    res = pipeline.run("l", _opts(tmp_path))
    assert res.stage_status[STAGE_FRAMES] == "fail:ffmpeg 缺失"
    assert res.is_partial()


def test_transcribe_degrade_continues(tmp_path, monkeypatch):
    opts = _prime(monkeypatch, tmp_path)

    def dep(*a, **k):
        raise DepMissing("没装 faster-whisper")
    monkeypatch.setattr(pipeline.transcribe_mod, "transcribe", dep)
    res = pipeline.run("l", opts)
    assert res.stage_status[STAGE_TRANSCRIBE] == "fail:没装 faster-whisper"
    assert res.stage_ok(STAGE_VISION)                         # 画面拆解照常
    assert res.stage_ok(STAGE_PROMPTS)
    # 降级仍算半成品（转写失败）：不写盘，但画面结果已产出
    assert res.is_partial() and res.frame_analyses


def test_vision_config_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline.acquire, "resolve_and_download", _ok_acquire())
    monkeypatch.setattr(pipeline.frames_mod, "extract_frames", _ok_frames)
    monkeypatch.setattr(pipeline.transcribe_mod, "transcribe", _ok_transcribe)

    class NoCfg:
        def __init__(self, cfg):
            raise ConfigError("豆包未配置")
    monkeypatch.setattr(vision_mod, "DoubaoVision", NoCfg)
    res = pipeline.run("l", _opts(tmp_path))
    assert res.stage_status[STAGE_VISION].startswith("fail:")
    assert STAGE_PROMPTS not in res.stage_status              # 视觉失败即返回


def test_asr_fix_opt_in_applied(tmp_path, monkeypatch):
    """显式开 asr_fix：转写后调 apply_fix 改逐字稿、成本并入、note 记录。"""
    opts = _prime(monkeypatch, tmp_path)
    hits = {"n": 0}

    def fake_apply(segments, glossary=None, cfg=None):
        hits["n"] += 1
        segments[0].text = "每天两条（纠错）"
        return 1, {"fix_calls": 1, "fix_tokens": 5}, "已 DeepSeek 语义纠错：改 1/1 句"
    monkeypatch.setattr(pipeline, "apply_fix", fake_apply)
    opts["asr_fix"] = True
    res = pipeline.run("l", opts)
    assert hits["n"] == 1
    assert res.transcript_text == "每天两条（纠错）"
    assert res.cost.get("fix_calls") == 1
    assert any("纠错" in n for n in res.notes)


def test_asr_fix_skipped_by_default(tmp_path, monkeypatch):
    """不传 asr_fix：绝不碰 apply_fix（pipeline 不擅自联网）。"""
    opts = _prime(monkeypatch, tmp_path)
    monkeypatch.setattr(pipeline, "apply_fix",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调纠错")))
    res = pipeline.run("l", opts)
    assert res.stage_ok(STAGE_TRANSCRIBE)


def test_prompt_stage_logs_heartbeat(tmp_path, monkeypatch):
    """阶段5/6 三次大请求（提示词/整体分析/改写）必须打开始+完成日志——

    每步 1~3 分钟无字节，不心跳就会被当成「46/46 帧后卡死」。"""
    opts = _prime(monkeypatch, tmp_path)
    logs = []
    res = pipeline.run("l", opts, log=logs.append)
    assert res.stage_ok(STAGE_PROMPTS) and res.stage_ok(STAGE_REWRITE)
    joined = "\n".join(logs)
    assert "生成分镜提示词" in joined and "分镜提示词完成" in joined
    assert "生成整体分析" in joined and "整体分析完成" in joined
    assert "改写去重" in joined and "改写去重完成" in joined


def test_prompt_stage_failure_logs_reason(tmp_path, monkeypatch):
    """大请求超时：降级仍出结果，但三条 ⚠ 日志必须带原因——绝不静默。"""
    opts = _prime(monkeypatch, tmp_path)

    class TimeoutVision(FakeVision):
        def chat_text(self, prompt, temperature=0.4, max_tokens=2048, **kw):
            self.calls += 1
            raise ValueError("豆包生成超时（180s 无响应）")

    monkeypatch.setattr(vision_mod, "DoubaoVision", TimeoutVision)
    logs = []
    res = pipeline.run("l", opts, log=logs.append)
    joined = "\n".join(logs)
    assert "⚠ 分镜提示词生成失败" in joined            # 降级为字段直拼但告知原因
    assert "⚠ 整体分析失败" in joined
    assert "⚠ 改写去重失败" in joined
    assert res.segments                                # 仍产出可用结果


# ---------------- video_url 直连模式（默认）：跳过抽帧 + 两条自动回退 ----------------

def _video_file(tmp_path, size=1000):
    p = tmp_path / "v.mp4"
    p.write_bytes(b"x" * size)
    return str(p)


def _direct_opts(tmp_path):
    return {"doubao_cfg": FAKE_CFG, "out_dir": str(tmp_path), "model_size": "tiny"}


def test_video_url_direct_skips_frames(tmp_path, monkeypatch):
    """默认 video_url：公网直链一次传豆包、不抽帧、分镜来自直连。"""
    video = _video_file(tmp_path)
    monkeypatch.setattr(pipeline.acquire, "resolve_and_download", _ok_acquire(video))

    def no_frames(*a, **k):
        raise AssertionError("直连模式不该抽帧")
    monkeypatch.setattr(pipeline.frames_mod, "extract_frames", no_frames)
    monkeypatch.setattr(pipeline.transcribe_mod, "transcribe", _ok_transcribe)
    seen = {}

    class RecVision(FakeVision):
        def analyze_video(self, url, prompt=None, **k):
            seen["url"] = url
            return super().analyze_video(url)
    monkeypatch.setattr(vision_mod, "DoubaoVision", RecVision)
    res = pipeline.run("l", _direct_opts(tmp_path))
    assert seen["url"] == "https://x/v.mp4"       # 直连用的就是解析出的公网直链
    assert STAGE_FRAMES not in res.stage_status
    assert res.stage_ok(STAGE_VISION) and not res.is_partial()
    assert [a.ts for a in res.frame_analyses] == [0.0, 4.0]
    assert res.shot_count == 2 and res.duration == 4.0


def test_video_url_too_large_falls_back_to_frames(tmp_path, monkeypatch):
    """视频超上限（钳小常量模拟 >50MB）：未执行前就改走逐帧，记 note+日志。"""
    opts = _prime(monkeypatch, tmp_path)
    opts["vision_mode"] = "video_url"
    video = _video_file(tmp_path)
    monkeypatch.setattr(pipeline.acquire, "resolve_and_download", _ok_acquire(video))
    monkeypatch.setattr(pipeline, "_VIDEO_URL_MAX_BYTES", 100)
    logs = []
    res = pipeline.run("l", opts, log=logs.append)
    assert "回退逐帧" in "\n".join(logs)
    assert res.stage_ok(STAGE_FRAMES) and res.stage_ok(STAGE_VISION)
    assert not res.is_partial()
    assert any("video_url 直连不可用" in n and "50MB" in n for n in res.notes)


def test_video_url_error_falls_back_to_frames(tmp_path, monkeypatch):
    """直连调用抛 VisionError：绝不静默，自动补跑「抽帧+逐帧」整段并记原因。"""
    opts = _prime(monkeypatch, tmp_path)
    opts["vision_mode"] = "video_url"
    video = _video_file(tmp_path)
    monkeypatch.setattr(pipeline.acquire, "resolve_and_download", _ok_acquire(video))

    class BoomDirect(FakeVision):
        def analyze_video(self, url, prompt=None, fps=0.5, timeout=600, log=None):
            raise VisionError("豆包取不到该直链或超时：HTTP 429")
    monkeypatch.setattr(vision_mod, "DoubaoVision", BoomDirect)
    logs = []
    res = pipeline.run("l", opts, log=logs.append)
    joined = "\n".join(logs)
    assert "已回退逐帧" in joined and "HTTP 429" in joined   # 原因不吞
    assert res.stage_ok(STAGE_FRAMES) and res.stage_ok(STAGE_VISION)
    assert not res.is_partial()
    assert res.frame_analyses[0].shot_size == "特写"          # 逐帧替身产物
    assert any("自动回退逐帧抽取" in n for n in res.notes)


def test_direct_transcribe_and_vision_run_in_parallel(tmp_path, monkeypatch):
    """直连模式：本地转写与豆包解析并发——两路都产出、日志出现「并行」阶段、进度只前进。"""
    video = _video_file(tmp_path)
    monkeypatch.setattr(pipeline.acquire, "resolve_and_download", _ok_acquire(video))
    monkeypatch.setattr(pipeline.frames_mod, "extract_frames",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("直连不抽帧")))
    monkeypatch.setattr(pipeline.transcribe_mod, "transcribe", _ok_transcribe)
    monkeypatch.setattr(vision_mod, "DoubaoVision", FakeVision)
    logs, seen, labels = [], [], []

    def prog(pct, tot=100, lab=""):
        seen.append(pct)
        labels.append(lab)
    res = pipeline.run("l", _direct_opts(tmp_path), log=logs.append, progress=prog)
    assert "并行" in " ".join(labels)                       # 进度标签带出并行阶段
    assert res.stage_ok(STAGE_TRANSCRIBE) and res.stage_ok(STAGE_VISION)
    assert res.transcript and res.frame_analyses           # 两路都到位
    assert seen == sorted(seen)                            # 并行不拉回进度条


def test_direct_parallel_transcribe_degrade_keeps_vision(tmp_path, monkeypatch):
    """直连并行：转写降级不拖累豆包解析（两分支并发、互不依赖）。"""
    video = _video_file(tmp_path)
    monkeypatch.setattr(pipeline.acquire, "resolve_and_download", _ok_acquire(video))
    monkeypatch.setattr(pipeline.frames_mod, "extract_frames",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("直连不抽帧")))

    def dep(*a, **k):
        raise DepMissing("没装 faster-whisper")
    monkeypatch.setattr(pipeline.transcribe_mod, "transcribe", dep)
    monkeypatch.setattr(vision_mod, "DoubaoVision", FakeVision)
    res = pipeline.run("l", _direct_opts(tmp_path))
    assert res.stage_status[STAGE_TRANSCRIBE].startswith("fail:")
    assert res.stage_ok(STAGE_VISION) and res.frame_analyses  # 画面拆解照常
