"""
video_text_tools/asr/transcribe.py —— 语音识别主体：faster-whisper 模型管理 + 词级转写 + SRT

「语音识别」这件事整个收在这一个模块里：模型是否就绪、怎么下、往哪加载、逐句转写、
渲染 SRT。爆款拆解、录屏字幕、实时字幕浮窗都从这里取识别能力，不再各指一处。

faster-whisper 是可选重依赖（会拉进 ctranslate2/av/tokenizers），一律延迟导入：
  没装  → DepMissing（消息给「pip install -r requirements-breakdown.txt」）；
  没模型 → ModelNotReady（GUI 据此弹下载引导，绝不静默跳过）。

模型只下不打包，落 core.config.MODELS_DIR（%APPDATA%\\AIGC视频助手\\models）。
转写步骤：ffmpeg 抽 16k 单声道 wav → WhisperModel(size, download_root, compute=int8)
→ word_timestamps=True 逐词时间戳 → TranscriptSegment 列表。

⚠ 模型管理与转写刻意放在同一模块（不拆 model.py）：单测常 monkeypatch model_ready /
  _model_dir_name / _MIN_WEIGHT_BYTES 这些名字，同模块内 ensure_model 现查全局才patch得动。
"""
import os
import re
from pathlib import Path

from .types import (TranscriptSegment, TranscriptWord,
                    DepMissing, ModelNotReady, TranscribeError)

# 可选模型（默认 medium，允许降档）
WHISPER_SIZES = ("medium", "small", "base", "tiny")
DEFAULT_SIZE = "medium"
# 国内镜像（下载慢/连不上官方 HF 时用）：设 HF_ENDPOINT 让 faster-whisper 走它
HF_MIRROR = "https://hf-mirror.com"
_HF_OFFICIAL = "https://huggingface.co"
_SIZE_RE = re.compile(r"faster-whisper-([a-z]+)")

# 推理设备：默认强制 CPU。WhisperModel 不传 device 时按 auto 自动选，
# 会在「有 N 卡但没装 CUDA 运行库」的普通用户机上直接崩（cublas64_12.dll not found）。
# 桌面分发面向未知机器，CPU + int8 最稳；有 CUDA 的用户想提速可自行改此常量。
_ASR_DEVICE = "cpu"


def _model_dir_name(size):
    return f"models--Systran--faster-whisper-{size}"


# blobs 里小于此的块都是 config/tokenizer；只有权重 model.bin 远大于它。
# 用它 + 「非 .incomplete」共同判定权重是否下完。
_MIN_WEIGHT_BYTES = 5_000_000


def _blobs_ready(size):
    """MODELS_DIR 的 HF 缓存布局下是否已有该 size「下完的权重」。

    只认 blobs 里一个「非 .incomplete 且 > 5MB」的文件（即权重 model.bin 落定）。
    下载中断会残留 .incomplete 大块，不能算就绪——否则界面误报「已就绪」，
    真去转写时按半成品加载会莫名失败。
    """
    from core.config import MODELS_DIR
    blobs = Path(MODELS_DIR) / _model_dir_name(size) / "blobs"
    if not blobs.is_dir():
        return False
    for f in blobs.iterdir():
        if not f.is_file() or f.name.endswith(".incomplete"):
            continue
        try:
            if f.stat().st_size > _MIN_WEIGHT_BYTES:
                return True
        except OSError:
            continue
    return False


def _plain_dir(size):
    """本地平铺目录（直链下载器/手动放置都走这个）：MODELS_DIR/faster-whisper-<size>。"""
    from core.config import MODELS_DIR
    return Path(MODELS_DIR) / f"faster-whisper-{size}"


def _plain_ready(size):
    """平铺目录里有「下完的 model.bin」即就绪（> 5MB 过滤掉半成品）。"""
    mb = _plain_dir(size) / "model.bin"
    try:
        return mb.is_file() and mb.stat().st_size > _MIN_WEIGHT_BYTES
    except OSError:
        return False


def model_ready(size=DEFAULT_SIZE):
    """该 size 模型是否可用：HF 缓存已下完，或本地平铺目录就绪（二者其一）。

    平铺目录这条同时支持「直链下载器」产物和「用户手动放置」——不必去凑 HF 的
    blobs/snapshots 那套复杂布局。
    """
    return _plain_ready(size) or _blobs_ready(size)


def resolve_model_path(size=DEFAULT_SIZE):
    """给 WhisperModel 的 model_size_or_path：平铺目录优先（直链/手动），否则用 HF 档名。

    WhisperModel 传目录路径时直接从那加载、download_root 被忽略；传“tiny/base…”则走 HF 缓存。
    """
    d = _plain_dir(size)
    if (d / "model.bin").is_file():
        return str(d)
    return size


def best_ready_size(prefer=DEFAULT_SIZE):
    """挑一个「已就绪」的模型来用：先给 prefer（若就绪），否则按质量档
    从高到低（WHISPER_SIZES 本就是 medium→tiny）取第一个就绪的；都没下 → None。

    给「点按钮就地看识别效果」这类场景用：不因默认档没下完而直接罢工，
    拿现有下得动的模型先跑起来。
    """
    if model_ready(prefer):
        return prefer
    for s in WHISPER_SIZES:
        if model_ready(s):
            return s
    return None


def ensure_model(size=DEFAULT_SIZE):
    """就绪则返回；否则抛 ModelNotReady，让 GUI 引导下载或手动放置"""
    if not model_ready(size):
        raise ModelNotReady(
            f"Whisper 模型「{size}」尚未下载。请在拆解面板点「⬇下载模型」，"
            "或手动把模型放到 %APPDATA%\\AIGC视频助手\\models 下（见 docs/爆款拆解.md）。")


def _set_endpoint(url):
    """把 huggingface_hub 指向 url。

    ENDPOINT 在导入时按 HF_ENDPOINT 定格，所以两管齐下：
      1. 首次 import 前：设/清 HF_ENDPOINT 环境变量；
      2. 已 import 过：直接改 constants.ENDPOINT（snapshot_download 调用时现读）。
    """
    if url and url != _HF_OFFICIAL:
        os.environ["HF_ENDPOINT"] = url
    else:
        os.environ.pop("HF_ENDPOINT", None)
    try:
        import huggingface_hub.constants as _c
    except Exception:
        return
    _c.ENDPOINT = url or _HF_OFFICIAL


def _apply_mirror(enable):
    """兼容旧调用：切国内镜像 / 官方。"""
    _set_endpoint(HF_MIRROR if enable else _HF_OFFICIAL)


def download_model(size=DEFAULT_SIZE, mirror=False, on_progress=None, log=None):
    """下载 faster-whisper 模型到 MODELS_DIR（首下即可用；已存在直接返回）。

    多端点兜底：勾选镜像→[hf-mirror, 官方] 依次试；否则 [官方, hf-mirror]。
    端点必须在 faster_whisper 首次导入前设好（见 _set_endpoint）；重试靠改常量切换。
    faster-whisper 无逐块回调，on_progress 只给「开始/结束」两刻度（不确定=-1）。
    """
    from core.config import MODELS_DIR

    def _log(msg):
        if log:
            log(msg)

    chain = [HF_MIRROR, _HF_OFFICIAL] if mirror else [_HF_OFFICIAL, HF_MIRROR]
    _set_endpoint(chain[0])                          # 首次导入前先把 env 设对（顺序关键）
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise DepMissing("缺少 faster-whisper，无法下载模型。请先执行：pip install -r requirements-breakdown.txt")
    Path(MODELS_DIR).mkdir(parents=True, exist_ok=True)
    if on_progress:
        on_progress(-1)                              # 开始：不确定进度
    errs = []
    for i, ep in enumerate(chain):
        if i > 0:
            _set_endpoint(ep)                        # 切换常量重试
        try:
            _log((f"从 {ep} 下载 Whisper「{size}」模型…") if i == 0
                 else f"↪ 该端点不通，换下一个重试：{ep}")
            WhisperModel(size, download_root=MODELS_DIR, device=_ASR_DEVICE,
                         compute_type="int8")
            if on_progress:
                on_progress(100)
            _log("✓ 模型下载完成")
            return True
        except Exception as e:
            errs.append(f"{ep}: {e}")
            _log(f"  ✗ {ep} 失败：{e}")
    raise TranscribeError(
        "模型下载失败（已依次尝试：" + "、".join(chain) + "）。"
        "常见原因：网络不通/DNS；可换网络时段重试，或手动放置模型到 "
        "%APPDATA%\\AIGC视频助手\\models（见 docs/录屏字幕.md）。"
        " 末次错误：" + (errs[-1] if errs else ""))


# 仓库里模型不需要的文件（README、.gitattributes），下载时滤掉
_SKIP_FILES = {"README.md", ".gitattributes"}


def _needed_files(siblings):
    """从仓库文件清单里挑出模型真正需要的（去掉 README/.gitattributes）。纯函数、可单测。"""
    return [f for f in siblings or [] if f not in _SKIP_FILES]


def _repo_of(size):
    return f"Systran/faster-whisper-{size}"


def _total_from(r, have):
    """从响应头估算文件总字节：优先 Content-Range（206），否则 Content-Length+已有。拿不到→None。"""
    cr = r.headers.get("Content-Range")                 # 形如 bytes 0-145000000/145000001
    if cr and "/" in cr:
        try:
            return int(cr.rsplit("/", 1)[1])
        except (ValueError, IndexError):
            pass
    cl = r.headers.get("Content-Length")
    if cl:
        try:
            return int(cl) + have
        except (ValueError, TypeError):
            pass
    return None


def _resume_get(requests, url, out, log=None, on_progress=None,
                read_timeout=20, attempts=80):
    """单文件：Range 断点续传 + 读超时。卡住/断线→下一轮从 .part 末尾接着拉；
    只有「确认下完（have>=total）」或「拿不到 total 且单轮流完整」才改名落定，
    否则保留 .part 不误判就绪。"""
    def _log(m):
        if log:
            log(m)

    if out.is_file():
        _log(f"  跳过 {out.name}（已在）")
        return
    part = out.with_name(out.name + ".part")
    total = None
    have = part.stat().st_size if part.exists() else 0
    for a in range(1, attempts + 1):
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with requests.get(url, headers=headers, stream=True,
                              timeout=(10, read_timeout)) as r:
                if r.status_code == 416:               # 区间越界：其实已下完
                    total = total or have
                    break
                r.raise_for_status()
                if total is None:
                    total = _total_from(r, have)
                with open(part, "ab" if have else "wb") as f:
                    for chunk in r.iter_content(65536):
                        if not chunk:
                            continue
                        f.write(chunk)
                        have += len(chunk)
                        if on_progress and total:
                            on_progress(min(int(have * 100 / total), 99))
        except Exception as e:
            _log(f"  {out.name} 第{a}次中断：{type(e).__name__}（已存 {have/1024/1024:.1f}MB，续传）")
            continue
        if total is None or have >= total:
            break
    if part.exists() and (total is None or have >= total):
        part.rename(out)
        _log(f"  ✓ {out.name} {out.stat().st_size/1024/1024:.1f}MB")
    elif part.exists():
        _log(f"  ✗ {out.name} 未完成 {have/1024/1024:.1f}/"
             f"{(total or 0)/1024/1024:.1f}MB，保留 .part 供下次续传")


def download_model_direct(size=DEFAULT_SIZE, mirror=True, on_progress=None, log=None,
                          read_timeout=20, attempts=80):
    """直链流式下载：requests + Range 断点续传 + 读超时，落到本地平铺目录。

    为何不用 huggingface_hub：面对 hf-mirror 这类镜像，hub 的请求会「连上不给数据」
    无限挂死（无读超时、不自动续传）；普通 Range 下载实测能正常吐字节（已验 206）。
    产物放 MODELS_DIR/faster-whisper-<size>/，配 resolve_model_path 直接加载。
    成功返回 True（权重就绪）；不完整则留 .part 下次续传、返回 False。
    """
    import requests

    def _log(m):
        if log:
            log(m)

    ep = HF_MIRROR if mirror else _HF_OFFICIAL
    repo = _repo_of(size)
    dest = _plain_dir(size)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        r = requests.get(f"{ep}/api/models/{repo}", timeout=(10, read_timeout))
        r.raise_for_status()
        files = _needed_files([s["rfilename"] for s in r.json().get("siblings", [])])
    except Exception as e:
        raise TranscribeError(f"列文件清单失败（{ep}）：{type(e).__name__}: {e}")
    if not files:
        raise TranscribeError(f"{ep} 上没找到 {repo} 的文件清单")
    _log(f"直链下载：{ep} / {repo} → {dest}")

    for fn in files:
        big = fn.endswith(".bin")
        _resume_get(requests, f"{ep}/{repo}/resolve/main/{fn}", dest / fn,
                    log=_log, on_progress=on_progress if big else None,
                    read_timeout=read_timeout, attempts=attempts)
        if big and on_progress and _plain_ready(size):
            on_progress(100)
    ok = _plain_ready(size)
    _log("✓ 直链下载完成" if ok else "✗ 下载后权重仍不完整（可重试续传）")
    return ok


def download_model_auto(size=DEFAULT_SIZE, mirror=True, on_progress=None, log=None):
    """下载首选：先走直链续传下载器（抗镜像挂死、可断点续传、有真进度），
    下不动或报错再退回 huggingface_hub 老路（面向镜像很给力的用户）。

    两条产物目录不同（直链→平铺目录，hub→HF 缓存），但 model_ready/resolve_model_path
    都认，所以谁先成都一样能用。
    """
    def _log(m):
        if log:
            log(m)

    try:
        if download_model_direct(size, mirror=mirror, on_progress=on_progress, log=log):
            return True
        _log("直链下载未完成（权重不完整），改用 huggingface_hub 重试…")
    except Exception as e:
        _log(f"直链下载失败（{type(e).__name__}），改用 huggingface_hub 重试…")
    return download_model(size, mirror=mirror, on_progress=on_progress, log=log)


def _extract_audio(video, wav_path, run, get_ffmpeg_path):
    """ffmpeg 抽 16k 单声道 wav（whisper 的标准输入格式）"""
    cmd = [get_ffmpeg_path(), "-i", video, "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", "-y", str(wav_path)]
    r = run(cmd, timeout=600)
    if getattr(r, "returncode", 1) != 0 or not Path(wav_path).exists():
        err = ((getattr(r, "stderr", "") or "")[-200:]).strip()
        raise TranscribeError(f"音频分离失败：{err or 'ffmpeg 返回非零'}")


def transcribe_stream(video, size=DEFAULT_SIZE, on_segment=None, should_stop=None, log=None,
                      initial_prompt=None):
    """流式转写：模型边解码，边把每个定稿段实时回调给 on_segment(TranscriptSegment)。

    faster-whisper 的 seg_iter 是惰性生成器，逐段吐出，所以「识别一句、吐一句」
    天然成立，适合实时上屏的浮窗。跑完仍返回 (segments, full_text) 与 transcribe 一致。
    on_segment 抛错只吞掉，绝不因显示端把识别打断。
    initial_prompt：非空时喂给 whisper 作解码引导（领域词库少写同音字）；None 则不传。
    """
    from core.config import MODELS_DIR
    from video_text_tools.ffmpeg_utils import get_ffmpeg_path, _run_subprocess

    def _log(msg):
        if log:
            log(msg)

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise DepMissing("缺少 faster-whisper，无法转写口播。可选装：pip install -r requirements-breakdown.txt"
                         "（不装也能拆画面/提示词，只是没有逐字稿）")
    ensure_model(size)

    work = Path(video).with_suffix(".wav")
    _extract_audio(video, work, _run_subprocess, get_ffmpeg_path)
    try:
        model = WhisperModel(resolve_model_path(size), download_root=MODELS_DIR,
                             device=_ASR_DEVICE, compute_type="int8")
        # faster-whisper 的 transcribe() 返回 (segments 迭代器, info)，不是带 .segments 的对象
        kwargs = {"word_timestamps": True, "vad_filter": True}
        if initial_prompt:
            kwargs["initial_prompt"] = initial_prompt
        seg_iter, info = model.transcribe(str(work), **kwargs)
        segments, texts = [], []
        try:
            _log(f"  识别语言≈{info.language}（{info.language_probability:.0%}）")
        except Exception:
            pass
        for seg in seg_iter:
            if should_stop and should_stop():
                break
            words = [TranscriptWord(w.word.strip(), float(w.start), float(w.end))
                     for w in (seg.words or []) if (w.word or "").strip()]
            text = (seg.text or "").strip()
            if not text:
                continue
            s = TranscriptSegment(float(seg.start), float(seg.end), text, words)
            segments.append(s)
            texts.append(text)
            if on_segment:
                try:
                    on_segment(s)
                except Exception:
                    pass
        full_text = "\n".join(texts)
        _log(f"  ✓ 转写 {len(segments)} 句")
        return segments, full_text
    finally:
        work.unlink(missing_ok=True)                 # 临时 wav 用完就清


def transcribe(video, size=DEFAULT_SIZE, log=None, should_stop=None, initial_prompt=None):
    """转写视频，返回 (segments:[TranscriptSegment], full_text:str)。

    缺依赖 → DepMissing；模型没下 → ModelNotReady（都由调用方标「转写」阶段并降级继续）。
    不关流式回调的薄封装，行为与历史完全一致。
    """
    return transcribe_stream(video, size=size, should_stop=should_stop, log=log,
                             initial_prompt=initial_prompt)


def to_srt(segments):
    """把转写段渲染成 SRT 文本（供导出/预览；不含毫秒精度需求，按句给起止）"""
    def ts(sec):
        h = int(sec // 3600)
        m = int(sec % 3600 // 60)
        s = int(sec % 60)
        ms = int(round((sec - int(sec)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, seg in enumerate(segments, start=1):
        lines.append(f"{i}\n{ts(seg.start)} --> {ts(seg.end)}\n{seg.text}\n")
    return "\n".join(lines)
