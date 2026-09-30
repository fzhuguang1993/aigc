"""
tests/test_breakdown_transcribe.py —— 词级转写（mock faster-whisper + ffmpeg，无重依赖）

校验：正常出 [TranscriptSegment] 含词级时间戳 + full_text，临时 wav 收尾清掉；
缺 faster-whisper → DepMissing；模型没下 → ModelNotReady。
"""
import sys
import os
import types

import video_text_tools.ffmpeg_utils as fu
from video_text_tools.asr import transcribe as tr
from video_text_tools.asr.types import DepMissing, ModelNotReady


class _W:
    def __init__(self, w, s, e):
        self.word, self.start, self.end = w, s, e


class _Seg:
    def __init__(self, s, e, t, ws):
        self.start, self.end, self.text, self.words = s, e, t, ws


class _Model:
    def __init__(self, *a, **k):
        pass

    def transcribe(self, path, **k):
        segs = [_Seg(0.0, 2.0, "每天两条", [_W("每天", 0.0, 1.0), _W("两条", 1.0, 2.0)]),
                _Seg(2.0, 4.0, "肠道通畅", [])]
        info = types.SimpleNamespace(language="zh", language_probability=0.99)
        # 真实 faster-whisper 返回 (segments 迭代器, info) 元组，不是带 .segments 的对象
        return segs, info


def _install_fake_fw(monkeypatch):
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = _Model
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)


def _patch_ffmpeg(monkeypatch, tmp_path):
    """抽音频的子进程：把 wav 建出来让 exists() 通过"""
    monkeypatch.setattr(fu, "get_ffmpeg_path", lambda: "ffmpeg")

    def run(cmd, **kw):
        open(cmd[-1], "wb").write(b"RIFFfake")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(fu, "_run_subprocess", run)
    monkeypatch.setattr(tr, "model_ready", lambda size: True)


def test_transcribe_words_and_cleanup(tmp_path, monkeypatch):
    _install_fake_fw(monkeypatch)
    _patch_ffmpeg(monkeypatch, tmp_path)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    segs, text = tr.transcribe(str(video), "tiny")
    assert [s.text for s in segs] == ["每天两条", "肠道通畅"]
    assert segs[0].words[0].word == "每天" and segs[0].words[0].end == 1.0
    assert text == "每天两条\n肠道通畅"
    assert not (tmp_path / "v.wav").exists()          # 临时 wav 收尾清掉


def test_transcribe_stream_emits_in_order(tmp_path, monkeypatch):
    """流式接口：每段定稿即按序回调 on_segment，且返回值与 transcribe 一致。"""
    _install_fake_fw(monkeypatch)
    _patch_ffmpeg(monkeypatch, tmp_path)
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x")
    seen = []
    segs, text = tr.transcribe_stream(str(v), "tiny", on_segment=lambda s: seen.append(s.text))
    assert seen == ["每天两条", "肠道通畅"]              # 逐句、按序上屏
    assert [s.text for s in segs] == seen
    assert text == "每天两条\n肠道通畅"
    assert not (tmp_path / "v.wav").exists()              # 临时 wav 照样收尾清掉


def test_transcribe_stream_on_segment_error_does_not_abort(tmp_path, monkeypatch):
    """显示端抛错绝不能打断识别（on_segment 异常被吞，句数不丢）。"""
    _install_fake_fw(monkeypatch)
    _patch_ffmpeg(monkeypatch, tmp_path)
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x")

    def bad(_s):
        raise RuntimeError("界面炸了")
    segs, text = tr.transcribe_stream(str(v), "tiny", on_segment=bad)
    assert len(segs) == 2 and text == "每天两条\n肠道通畅"


def test_transcribe_stream_passes_initial_prompt(tmp_path, monkeypatch):
    """词库喂 initial_prompt：非空时透传给 model.transcribe；None/空则不带该 kwarg。"""
    seen = {}

    class _Cap:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, path, **k):
            seen.update(k)
            segs = [_Seg(0.0, 2.0, "钙片", [_W("钙片", 0.0, 2.0)])]
            info = types.SimpleNamespace(language="zh", language_probability=0.99)
            return segs, info
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = _Cap
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    _patch_ffmpeg(monkeypatch, tmp_path)
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x")

    tr.transcribe_stream(str(v), "tiny", initial_prompt="钙片、骨密度")
    assert seen.get("initial_prompt") == "钙片、骨密度"

    seen.clear()
    tr.transcribe_stream(str(v), "tiny")               # 不传 → kwargs 里不该有该键
    assert "initial_prompt" not in seen

    seen.clear()
    tr.transcribe_stream(str(v), "tiny", initial_prompt="")   # 空串同样不传
    assert "initial_prompt" not in seen


def test_best_ready_size_prefer_then_highest_quality(tmp_path, monkeypatch):
    """先给 prefer（若就绪），否则按 medium→tiny 取第一个就绪的；都没下→None。"""
    ready = {"base"}
    monkeypatch.setattr(tr, "model_ready", lambda size: size in ready)
    assert tr.best_ready_size("medium") == "base"          # prefer 没下→降档取就绪的
    ready.add("small")
    assert tr.best_ready_size("medium") == "small"         # 多个就绪取质量更高的
    assert tr.best_ready_size("small") == "small"          # prefer 就绪则直接用
    ready.clear()
    assert tr.best_ready_size("medium") is None             # 一个都没下


def test_to_srt(tmp_path, monkeypatch):
    _install_fake_fw(monkeypatch)
    _patch_ffmpeg(monkeypatch, tmp_path)
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x")
    segs, _ = tr.transcribe(str(v), "tiny")
    srt = tr.to_srt(segs)
    assert "00:00:00,000 --> 00:00:02,000" in srt
    assert "每天两条" in srt


def test_missing_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)   # import 视为不可用
    try:
        tr.transcribe("v.mp4", "tiny")
        assert False, "应抛 DepMissing"
    except DepMissing as e:
        assert "requirements-breakdown" in str(e)


def test_model_not_ready(monkeypatch):
    _install_fake_fw(monkeypatch)
    monkeypatch.setattr(tr, "model_ready", lambda size: False)
    try:
        tr.transcribe("v.mp4", "medium")
        assert False, "应抛 ModelNotReady"
    except ModelNotReady:
        pass


def test_model_ready_checks_completed_weight(tmp_path, monkeypatch):
    """只认「非 .incomplete 且足够大的权重块」才算就绪；只有小 config 不算。

    本用例只验「权重文件探测」这一段逻辑，与 faster_whisper 是否安装无关，
    故把 whisper_available 钉成 True 隔离掉可选重依赖（没装库的开发机也能跑）。"""
    import core.config as cfg
    from pathlib import Path
    monkeypatch.setattr(cfg, "MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(tr, "whisper_available", lambda: True)
    root = Path(cfg.MODELS_DIR) / tr._model_dir_name("tiny")
    (root / "blobs").mkdir(parents=True, exist_ok=True)
    (root / "blobs" / "cfg.json").write_bytes(b"{}")       # 只有小文件
    assert tr.model_ready("tiny") is False
    (root / "blobs" / "model.bin").write_bytes(b"x" * (tr._MIN_WEIGHT_BYTES + 1))
    assert tr.model_ready("tiny") is True
    assert tr.model_ready("medium") is False                # 别的 size 不受影响


def test_model_ready_ignores_incomplete(tmp_path, monkeypatch):
    """半成品回归：大块还带 .incomplete（下载中断残留），绝不能误判为就绪。"""
    import core.config as cfg
    from pathlib import Path
    monkeypatch.setattr(cfg, "MODELS_DIR", str(tmp_path / "models"))
    blobs = Path(cfg.MODELS_DIR) / tr._model_dir_name("medium") / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    (blobs / "9b45e100.deadbeef.incomplete").write_bytes(b"x" * (tr._MIN_WEIGHT_BYTES + 1))
    assert tr.model_ready("medium") is False


def test_mirror_sets_env_and_constants(monkeypatch):
    """钉陷阱：huggingface_hub 已导入过时，勾镜像必须同时改 constants.ENDPOINT。"""
    hfc = types.ModuleType("huggingface_hub.constants")
    hfc.ENDPOINT = "https://huggingface.co"
    hub = types.ModuleType("huggingface_hub")
    hub.constants = hfc
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setitem(sys.modules, "huggingface_hub.constants", hfc)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)

    tr._apply_mirror(True)
    assert hfc.ENDPOINT == tr.HF_MIRROR
    assert os.environ["HF_ENDPOINT"] == tr.HF_MIRROR

    tr._apply_mirror(False)
    assert hfc.ENDPOINT == "https://huggingface.co"
    assert "HF_ENDPOINT" not in os.environ


def test_download_model_mirror_first(tmp_path, monkeypatch):
    """勾选镜像：端点链首位是镜像（先切端点再导入，顺序错了镜像不生效）。"""
    import core.config as cfg
    _install_fake_fw(monkeypatch)
    monkeypatch.setattr(cfg, "MODELS_DIR", str(tmp_path / "models"))
    tried = []
    monkeypatch.setattr(tr, "_set_endpoint", lambda url: tried.append(url))
    assert tr.download_model("tiny", mirror=True) is True
    assert tried[0] == tr.HF_MIRROR                # 镜像优先


def test_download_model_falls_back_to_next_endpoint(tmp_path, monkeypatch):
    """首个端点连不上→自动换下一个重试（多镜像兜底）。"""
    import core.config as cfg
    monkeypatch.setattr(cfg, "MODELS_DIR", str(tmp_path / "models"))
    n = {"c": 0}

    def boom(*a, **k):
        n["c"] += 1
        if n["c"] == 1:
            raise OSError("connectError")           # 第一个端点失败
        return object()                              # 第二个成功
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = boom
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    tried = []
    monkeypatch.setattr(tr, "_set_endpoint", lambda url: tried.append(url))
    assert tr.download_model("tiny", mirror=True) is True
    assert tried == [tr.HF_MIRROR, tr._HF_OFFICIAL]   # 镜像不通→官方兜底


def test_download_model_all_endpoints_fail_raises(tmp_path, monkeypatch):
    """两个端点都不通 → 抛 TranscribeError（不静默）。"""
    from video_text_tools.asr.types import TranscribeError
    import core.config as cfg
    monkeypatch.setattr(cfg, "MODELS_DIR", str(tmp_path / "models"))

    def boom(*a, **k):
        raise OSError("connectError")
    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = boom
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    monkeypatch.setattr(tr, "_set_endpoint", lambda url: None)
    try:
        tr.download_model("tiny", mirror=True)
        assert False, "应抛 TranscribeError"
    except TranscribeError as e:
        assert "connectError" in str(e)


# ---------------- 直链下载器（requests 依赖注入，不碰网络） ----------------
class _Resp:
    def __init__(self, body, headers, status=200):
        self._body, self.headers, self.status_code = body, headers, status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f"http {self.status_code}")

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]


class _FakeReq:
    """按 Range 切片返回 full；total 可谎报以模拟「没下完」。"""

    def __init__(self, full, total=None):
        self.full = full
        self.total = total if total is not None else len(full)
        self.ranges = []

    def get(self, url, headers=None, stream=False, timeout=None):
        have = 0
        rng = (headers or {}).get("Range")
        if rng:
            have = int(rng.split("=")[1].split("-")[0])
        self.ranges.append(have)
        body = self.full[have:]
        if have:
            hd = {"Content-Range": f"bytes {have}-{self.total - 1}/{self.total}"}
            return _Resp(body, hd, 206)
        return _Resp(body, {"Content-Length": str(self.total)})


def test_needed_files_filters_readme():
    got = tr._needed_files(["README.md", ".gitattributes", "config.json",
                            "model.bin", "tokenizer.json", "vocabulary.txt"])
    assert got == ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]


def test_total_from_prefers_content_range():
    r = types.SimpleNamespace(headers={"Content-Range": "bytes 100-999/1000"})
    assert tr._total_from(r, 100) == 1000
    r = types.SimpleNamespace(headers={"Content-Length": "500"})
    assert tr._total_from(r, 100) == 600
    assert tr._total_from(types.SimpleNamespace(headers={}), 0) is None


def test_resume_get_writes_and_renames(tmp_path):
    out = tmp_path / "model.bin"
    tr._resume_get(_FakeReq(b"x" * 200), "u", out)
    assert out.is_file() and out.stat().st_size == 200
    assert not (tmp_path / "model.bin.part").exists()          # 下完改名不留 .part


def test_resume_get_resumes_from_existing_part(tmp_path):
    out = tmp_path / "model.bin"
    (tmp_path / "model.bin.part").write_bytes(b"y" * 80)       # 上轮残留
    req = _FakeReq(b"x" * 200)
    tr._resume_get(req, "u", out)
    assert req.ranges[0] == 80                                   # 从 80 续传
    assert out.is_file() and out.stat().st_size == 200


def test_resume_get_keeps_part_when_incomplete(tmp_path):
    """total 谎报得比实际大：永远下不满 → 保留 .part、绝不落成 model.bin 误判就绪。"""
    out = tmp_path / "model.bin"
    tr._resume_get(_FakeReq(b"x" * 50, total=9999), "u", out, attempts=3)
    assert not out.exists()
    assert (tmp_path / "model.bin.part").is_file()


def test_plain_dir_model_ready_and_resolve(tmp_path, monkeypatch):
    """平铺目录里有下完的 model.bin → 就绪，且 resolve 指回该目录；没下完→用 HF 档名。

    同 weight 用例：只验文件探测，whisper_available 钉 True 隔离可选依赖。"""
    import core.config as cfg
    monkeypatch.setattr(cfg, "MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(tr, "whisper_available", lambda: True)
    d = tr._plain_dir("small")
    d.mkdir(parents=True)
    assert tr.model_ready("small") is False
    assert tr.resolve_model_path("small") == "small"           # 未就绪→回退档名
    (d / "model.bin").write_bytes(b"x" * (tr._MIN_WEIGHT_BYTES + 1))
    assert tr.model_ready("small") is True
    assert tr.resolve_model_path("small") == str(d)             # 就绪→平铺目录


def test_download_model_auto_prefers_direct(monkeypatch):
    """直链成功就不必再走 huggingface_hub。"""
    calls = []
    monkeypatch.setattr(tr, "download_model_direct",
                        lambda size, **k: (calls.append("direct"), True)[1])
    monkeypatch.setattr(tr, "download_model", lambda *a, **k: (calls.append("hf"), True)[1])
    assert tr.download_model_auto("tiny", mirror=True) is True
    assert calls == ["direct"]


def test_download_model_auto_falls_back_to_hf(monkeypatch):
    """直链报错→退回 huggingface_hub 老路（不静默失败）。"""
    calls = []

    def boom(size, **k):
        calls.append("direct")
        raise RuntimeError("断网")
    monkeypatch.setattr(tr, "download_model_direct", boom)
    monkeypatch.setattr(tr, "download_model", lambda *a, **k: (calls.append("hf"), True)[1])
    assert tr.download_model_auto("tiny", mirror=True) is True
    assert calls == ["direct", "hf"]
