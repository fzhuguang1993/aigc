"""
video_text_tools/breakdown/vision.py —— 豆包 Vision（火山方舟 Ark，OpenAI 兼容端点）

接入方式：POST {base}/chat/completions，Authorization: Bearer <api_key>，model=<ep-xxxx>。
base 默认 https://ark.cn-beijing.volces.com/api/v3；提示词生成/整体分析/改写去重都复用
同一个 DoubaoVision（同一接入点，不引第二个模型服务）。

- 未配 api_key/endpoint → ConfigError（GUI 引导去配置）。
- analyze_frames：ThreadPoolExecutor(3) 逐帧并发，429/5xx 退避重试，累计 token 与调用次数。
- 一切走 requests 直连，测试里 mock requests.post 即可。
"""
import base64
import json
import mimetypes
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .models import FrameAnalysis, ConfigError, VisionError

_JSON_RE = re.compile(r"\{.*\}", re.S)
# video_url 返回可能是 JSON 数组或 {"shots":[...]} 对象，这里两者都容错抓取
_ARR_RE = re.compile(r"[\[\{].*[\}\]]", re.S)


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _extract_video_shots(text):
    """video_url 返回文本 → [FrameAnalysis]（按时间序）。

    容错：整块数组 / {"shots|items|segments":[...]}；时间字段 start|ts|begin；
    序号 index|idx。解不出任何条目返回 []（由调用方判失败回退，绝不静默出空）。"""
    m = _ARR_RE.search(text or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        data = (data.get("shots") or data.get("items")
                or data.get("segments") or [])
    if not isinstance(data, list):
        return []
    out = []
    for i, d in enumerate(data):
        if not isinstance(d, dict):
            continue
        out.append(FrameAnalysis(
            idx=int(_to_float(d.get("index", d.get("idx", i + 1)), i + 1)),
            ts=_to_float(d.get("start", d.get("ts", d.get("begin", 0.0)))),
            shot_size=str(d.get("shot_size", "")),
            camera=str(d.get("camera", "")),
            composition=str(d.get("composition", "")),
            transition=str(d.get("transition", "")),
            on_screen_text=str(d.get("on_screen_text", "")),
            emotion=str(d.get("emotion", "")),
            raw=json.dumps(d, ensure_ascii=False)))
    return out


# video_url 直连提示词：整条视频交模型内部抽帧+音画理解，产出与逐帧同字段分镜表
VIDEO_PROMPT = (
    "你是专业短视频分镜分析师。请按时间顺序拆解这条爆款短视频的分镜画面，输出一个 JSON 数组。\n"
    "每个分镜一段画面，字段：\n"
    '{"index":序号(从1),"start":起始秒(浮点),"end":结束秒(浮点),'
    '"shot_size":"景别(远/全/中/近/特写)","camera":"运镜(固定/推/拉/摇/移/跟/手持)",'
    '"composition":"构图要点","transition":"与上一分镜的转场(无则写\\"无\\")",'
    '"on_screen_text":"画面里出现的文字(无则空)","emotion":"画面传达的情绪"}\n'
    "只输出 JSON 数组，不要多余解释文字。")


def _encode_image(path):
    """本地图片 → data URL（方舟 image_url 直接吃 base64）"""
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        b = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b}"


def _extract_json(text):
    """从模型返回里抠出第一个 JSON 对象；抠不到返回 {}（调用方兜底 raw）"""
    m = _JSON_RE.search(text or "")
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except (ValueError, TypeError):
        return {}


class DoubaoVision:
    """豆包 Vision 接入点封装：既是逐帧分析器，也是 prompts 的文本生成器。"""

    def __init__(self, cfg):
        cfg = cfg or {}
        self.api_key = (cfg.get("api_key") or "").strip()
        self.model = (cfg.get("endpoint") or "").strip()
        self.base_url = (cfg.get("base_url") or "").strip().rstrip("/") \
            or "https://ark.cn-beijing.volces.com/api/v3"
        if not self.api_key or not self.model:
            raise ConfigError(
                "豆包 Vision 未配置：需要 api_key 与端点 ID（ep-xxxx）。"
                "请在拆解面板「⚙配置」填写（先在火山方舟创建视觉理解模型的推理接入点）。")
        # 思考开关：seed-2.x 系列默认开思考，实测 40 项大生成 322s（含 257s 静默
        # 思考）→ 关掉后 161s；结构化 JSON 任务不需要深想。老端点不认该参数会 400，
        # _post 里自动去参重发一次兼容。cfg["thinking"] 可覆盖为 "enabled"。
        self.thinking = (cfg.get("thinking") or "disabled").strip() or None
        # 全局限流闸门：方舟 RPM/TPM 是分钟级滑动窗口，旧版 1~8s 短退避对 429
        # 几乎无效（实测：一帧撞 429 后其余帧与后续大请求连坐全挂）。任一请求
        # 吃 429 就设冷却截止时刻，全部线程（含逐帧 3 并发与阶段 5/6 大请求）
        # 发请求前统一等过窗口，避免重试雪崩。
        self._gate = threading.Lock()
        self._cooldown_until = 0.0
        self.calls = 0
        self.tokens = 0

    def _retry_after(self, r):
        """429 冷却秒数：优先听服务端 Retry-After，没有则 30s+抖动（半个限流窗口）"""
        try:
            return max(5.0, float((getattr(r, "headers", None) or {})
                                  .get("Retry-After")))
        except (TypeError, ValueError):
            return 30.0 + random.uniform(0, 5)

    # ---- 底层一次请求：带 429/5xx 退避重试 + token 统计 ----
    def _post(self, messages, temperature=0.3, max_tokens=1024, retries=4,
              timeout=60):
        """timeout 为读超时秒数：逐帧小输出 60s 够用；大文本生成走
        chat_text(timeout=300)——读超时不重试（长生成重试必然再超，白等且静默）。
        429 走全局冷却重试（见 _retry_after），5xx 指数退避。"""
        import requests
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        body = {"model": self.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens}
        if self.thinking:
            body["thinking"] = {"type": self.thinking}
        last = None
        for attempt in range(retries):
            with self._gate:
                wait = self._cooldown_until - time.time()
            if wait > 0:
                time.sleep(wait)                    # 全局冷却：别的线程撞过 429，停下等窗口
            try:
                r = requests.post(url, headers=headers, json=body,
                                  timeout=timeout)
            except requests.exceptions.ReadTimeout as e:
                # 模型生成太慢（非流式全程无字节）：重试同样会超，直接失败
                raise VisionError(f"豆包生成超时（{timeout}s 无响应）：{e}")
            except Exception as e:                    # 网络抖动也退避重试
                last = f"请求异常：{e}"
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code == 429:
                detail = (r.text or "")[:200]
                if ("SetLimitExceeded" in detail or "QuotaExceeded" in detail
                        or "has been paused" in detail):
                    # 用量上限打满/模型服务被暂停：分钟级冷却永远等不回来，快速失败
                    # 并给出自助恢复路径（实测安全体验模式下秒回此错，重试白等）。
                    raise VisionError(
                        "方舟用量上限已打满，模型服务被暂停：请到火山方舟控制台"
                        "「模型开通/限额管理」调高该模型用量上限或关闭「安全体验模式」"
                        f"后重跑。原始信息：{detail}")
                last = f"HTTP 429：{detail}"          # 真限流：设全局冷却后重发
                with self._gate:
                    self._cooldown_until = max(self._cooldown_until,
                                               time.time() + self._retry_after(r))
                continue
            if r.status_code in (500, 502, 503):     # 上游忙：指数退避
                last = f"HTTP {r.status_code}"
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code == 400 and "thinking" in body:
                # 老端点不认 thinking 参数：去掉后原样重发一次（此时 body 已无
                # thinking，再 400 就按普通错误报出，不会无限回退）
                self.thinking = None
                body.pop("thinking", None)
                continue
            if r.status_code >= 400:
                raise VisionError(f"豆包调用失败 HTTP {r.status_code}：{r.text[:200]}")
            try:
                j = r.json()
            except ValueError:
                raise VisionError(f"豆包返回非 JSON：{r.text[:120]}")
            usage = j.get("usage") or {}
            self.tokens += int(usage.get("total_tokens",
                                         usage.get("prompt_tokens", 0)
                                         + usage.get("completion_tokens", 0)) or 0)
            self.calls += 1
            choices = j.get("choices") or []
            content = ((choices[0].get("message") if choices else {}) or {}).get("content", "")
            if not content:
                raise VisionError(f"豆包返回空内容：{json.dumps(j, ensure_ascii=False)[:160]}")
            return content
        raise VisionError(f"豆包限流重试 {retries} 次仍失败：{last}")

    def cost(self):
        return {"vision_calls": self.calls, "tokens": self.tokens}

    # ---- 纯文本一次对话（提示词生成/整体分析/改写都走它）----
    def chat_text(self, prompt, temperature=0.4, max_tokens=2048, timeout=300):
        """大文本生成：实测 40 分镜量级关思考后仍需 ~160s，真实 46 分镜更大——
        300s 留余量；超时不重试（见 _post），失败由 prompts 降级并打日志。"""
        return self._post([{"role": "user", "content": prompt}],
                          temperature=temperature, max_tokens=max_tokens,
                          timeout=timeout)

    # ---- video_url 直连：整条视频一次调用（免本地抽帧/逐帧上传，规避 429）----
    def analyze_video(self, url, prompt=None, fps=0.5, timeout=600, log=None):
        """公网直链整条 mp4（≤50MB）一次传豆包，模型内部抽帧+音画理解，回 [FrameAnalysis]。

        报文与官方示例逐字一致：{"type":"video_url","video_url":{"url":..,"fps":..}}；
        fps 在 video_url 对象内（[0.2,5]）。单次请求非流式（全程零字节）：timeout 给足
        600s；ReadTimeout 快失败/429 全局冷却均复用 _post。解不出分镜抛 VisionError
        （绝不静默出空视频拆解），由调用方决定回退逐帧。"""
        if not url:
            raise VisionError("analyze_video 需要公网直链 url")
        messages = [{"role": "user", "content": [
            {"type": "video_url", "video_url": {"url": url, "fps": fps}},
            {"type": "text", "text": prompt or VIDEO_PROMPT},
        ]}]
        if log:
            log(f"  ⏳ video_url 直连解析整条视频（模型内部 fps={fps} 抽帧，"
                "单次请求，约 1~5 分钟）…")
        content = self._post(messages, max_tokens=6000, timeout=timeout)
        shots = _extract_video_shots(content)
        if not shots:
            raise VisionError(f"video_url 返回解析不出分镜：{content[:120]}")
        if log:
            log(f"  ✓ video_url 直连完成：共 {len(shots)} 个分镜")
        return shots

    # ---- 逐帧视觉分析（默认串行）----
    def analyze_frames(self, frames, prompt_tpl, concurrency=1, log=None,
                       progress=None, inter_frame_delay=1.5):
        """对每张 FrameShot 出景别/运镜/构图/转场/画面文字/情绪，返回 [FrameAnalysis]（按 idx 排序）。

        prompt_tpl 里的 {idx} 会替换成帧序号；图片以 data URL 随消息发送。
        单帧失败不阻断整批：该帧 analysis.raw 记错误，其余照常。
        每帧完成都打一条心跳日志并回调 progress(done, total)——成功不打日志时
        整批静默几分钟会被当成卡死（推理模型单帧就可能数十秒）。

        并发默认降到 1 + 帧间 sleep 1.5s：每帧含 base64 图片上传，大并发实测很
        快打满方舟免费账号 QPS 连续 429（回退路径就因此全挂）；宁慢不崩。"""
        total = len(frames)
        workers = max(1, min(concurrency, 3))
        lock = threading.Lock()
        done = [0]

        def _tick():
            with lock:
                done[0] += 1
                cur = done[0]
            if log:
                log(f"  · 视觉分析 {cur}/{total} 帧完成")
            if progress:
                try:
                    progress(cur, total)
                except Exception:
                    pass

        def _one(fs):
            text = prompt_tpl.replace("{idx}", str(fs.idx)) if prompt_tpl else \
                "请分析这一帧的画面语言。"
            messages = [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": _encode_image(fs.path)}},
                {"type": "text", "text": text},
            ]}]
            try:
                content = self._post(messages, retries=6)   # 逐帧小请求：限流冷却后多给几次机会
                d = _extract_json(content)
                return FrameAnalysis(
                    idx=fs.idx, ts=fs.ts,
                    shot_size=str(d.get("shot_size", "")),
                    camera=str(d.get("camera", "")),
                    composition=str(d.get("composition", "")),
                    transition=str(d.get("transition", "")),
                    on_screen_text=str(d.get("on_screen_text", "")),
                    emotion=str(d.get("emotion", "")),
                    raw=content if not d else "")
            except Exception as e:
                if log:
                    log(f"  ⚠ 第{fs.idx}帧分析失败：{e}")
                return FrameAnalysis(idx=fs.idx, ts=fs.ts, raw=f"分析失败：{e}")
            finally:
                _tick()
                if inter_frame_delay > 0:
                    time.sleep(inter_frame_delay)   # 帧间限速：给方舟 QPS 窗口喘口

        if log and frames:
            log(f"  逐帧视觉分析：共 {total} 帧、{workers} 并发（单帧含图片上传，"
                "每帧数秒~数十秒，完成一帧报一条）")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_one, frames))
        results.sort(key=lambda a: a.idx)
        fails = sum(1 for a in results if str(a.raw).startswith("分析失败"))
        if fails and log:
            log(f"  ⚠ {fails}/{total} 帧分析失败（失败帧置空占位，成功帧照常；"
                "若成批失败多为方舟限流，稍等重试或降并发）")
        return results
