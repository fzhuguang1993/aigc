"""
tests/test_breakdown_vision.py —— 豆包 Vision 接入（mock requests.post，无网络）

校验：未配置抛 ConfigError；逐帧解析 JSON→FrameAnalysis；token/调用次数统计；
429 退避重试后成功；整批失败各自记 raw 不阻断；读超时快速失败不重试。
"""
import json

import requests

import video_text_tools.breakdown.vision as vs
from video_text_tools.breakdown.models import FrameShot, ConfigError, VisionError


class Resp:
    def __init__(self, status=200, payload=None, text="", headers=None):
        self.status_code, self._payload, self.text = status, payload, text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _content(shot_size="特写"):
    return json.dumps({"shot_size": shot_size, "camera": "推", "composition": "居中",
                       "transition": "无", "on_screen_text": "限时", "emotion": "紧迫"})


def _ok(payload_content):
    return lambda *a, **k: Resp(payload={
        "choices": [{"message": {"content": payload_content}}],
        "usage": {"total_tokens": 120}})


def test_no_config_raises():
    try:
        vs.DoubaoVision({"api_key": "", "endpoint": ""})
        assert False, "应抛 ConfigError"
    except ConfigError:
        pass


def test_analyze_frames_parses_and_costs(monkeypatch):
    monkeypatch.setattr(vs, "_encode_image", lambda p: "data:image/jpeg;base64,AA")
    monkeypatch.setattr(requests, "post", _ok(_content("远景")))
    v = vs.DoubaoVision({"api_key": "k", "endpoint": "ep-1",
                         "base_url": "https://x/api/v3"})
    frames = [FrameShot(1, 0.0, "a.jpg"), FrameShot(2, 3.0, "b.jpg")]
    out = v.analyze_frames(frames, "分析第 {idx} 帧", concurrency=3,
                           inter_frame_delay=0)
    assert [a.idx for a in out] == [1, 2]
    assert out[0].shot_size == "远景" and out[0].emotion == "紧迫"
    cost = v.cost()
    assert cost["vision_calls"] == 2 and cost["tokens"] == 240


def test_429_retry_then_ok(monkeypatch):
    monkeypatch.setattr(vs, "_encode_image", lambda p: "x")
    monkeypatch.setattr(vs.time, "sleep", lambda *_: None)      # 退避不真等
    seq = [Resp(status=429), Resp(status=429),
           Resp(payload={"choices": [{"message": {"content": _content("近景")}}],
                         "usage": {"total_tokens": 50}})]
    it = iter(seq)
    monkeypatch.setattr(requests, "post", lambda *a, **k: next(it))
    v = vs.DoubaoVision({"api_key": "k", "endpoint": "ep"})
    out = v.analyze_frames([FrameShot(1, 0.0, "a.jpg")], "p")
    assert out[0].shot_size == "近景"
    assert v.cost()["vision_calls"] == 1


def test_single_frame_failure_does_not_block_batch(monkeypatch):
    monkeypatch.setattr(vs, "_encode_image", lambda p: "x")

    def flaky(url, **k):
        # 第一帧请求体里 idx=1 → 500；idx=2 → 正常
        body = json.dumps(k.get("json", {}), ensure_ascii=False)
        if "第1帧" in body:
            return Resp(status=503)                             # 耗尽重试 → 失败
        return Resp(payload={"choices": [{"message": {"content": _content()}}],
                             "usage": {"total_tokens": 10}})

    monkeypatch.setattr(vs.time, "sleep", lambda *_: None)
    monkeypatch.setattr(requests, "post", flaky)
    v = vs.DoubaoVision({"api_key": "k", "endpoint": "ep"})
    frames = [FrameShot(1, 0.0, "a.jpg"), FrameShot(2, 3.0, "b.jpg")]
    out = v.analyze_frames(frames, "第{idx}帧", concurrency=1)
    assert out[0].raw.startswith("分析失败")                    # 该帧降级
    assert out[1].shot_size == "特写"                           # 另一帧正常


def test_read_timeout_fails_fast_no_retry(monkeypatch):
    """大文本生成读超时：只发 1 次、直接抛 VisionError——不许白重试。

    非流式大请求（几千 token）读超时后重试必然再超，4 次重试≈十几分钟静默，
    用户看到的就是「卡死」。超时必须快速失败、把原因带回上层日志。
    """
    hits = {"n": 0}

    def timeout(*a, **k):
        hits["n"] += 1
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(requests, "post", timeout)
    v = vs.DoubaoVision({"api_key": "k", "endpoint": "ep"})
    try:
        v.chat_text("写一长篇文案", timeout=180)
        assert False, "应抛 VisionError"
    except VisionError as e:
        assert "超时" in str(e)
    assert hits["n"] == 1                                       # 不重试


def test_thinking_disabled_by_default(monkeypatch):
    """seed-2.x 默认开思考会把大生成时长翻倍（实测 322s vs 161s），请求体必须
    默认带 thinking:disabled；cfg 可覆盖。"""
    seen = []
    monkeypatch.setattr(requests, "post", lambda url, **k: (
        seen.append(k["json"]) or
        Resp(payload={"choices": [{"message": {"content": "收到"}}],
                      "usage": {"total_tokens": 5}})))
    v = vs.DoubaoVision({"api_key": "k", "endpoint": "ep"})
    v.chat_text("hi")
    assert seen[0]["thinking"] == {"type": "disabled"}

    v2 = vs.DoubaoVision({"api_key": "k", "endpoint": "ep", "thinking": "enabled"})
    v2.chat_text("hi")
    assert seen[1]["thinking"] == {"type": "enabled"}


def test_thinking_400_falls_back_and_resends(monkeypatch):
    """老端点不认 thinking 参数报 400：自动去参重发一次就成功；二次仍 400 才报错。"""
    seen = []

    def only_old(url, **k):
        # 存快照拷贝：_post 回退时会 body.pop 原地改同一个 dict，存引用录不到首发状态
        seen.append(dict(k["json"]))
        if "thinking" in seen[-1]:
            return Resp(status=400, text="Invalid parameter: thinking")
        return Resp(payload={"choices": [{"message": {"content": "好"}}],
                             "usage": {"total_tokens": 1}})

    monkeypatch.setattr(requests, "post", only_old)
    v = vs.DoubaoVision({"api_key": "k", "endpoint": "ep"})
    assert v.chat_text("hi") == "好"
    assert "thinking" in seen[0] and "thinking" not in seen[1]   # 首发带去、回退去参
    assert v.thinking is None                                    # 后续请求不再带


def test_429_sets_shared_cooldown_not_short_backoff(monkeypatch):
    """429 必须走分钟级全局冷却（实测旧 1~8s 退避对Ark限流无效、成批连坐）：
    吃 429 后设冷却并等待后重发；后续新请求也要先过冷却闸门。"""
    sleeps = []
    monkeypatch.setattr(vs.time, "sleep", lambda s=None: sleeps.append(s))
    ok = Resp(payload={"choices": [{"message": {"content": "好"}}],
                       "usage": {"total_tokens": 1}})
    seq = iter([Resp(status=429), ok, ok])
    monkeypatch.setattr(requests, "post", lambda *a, **k: next(seq))
    v = vs.DoubaoVision({"api_key": "k", "endpoint": "ep"})
    assert v.chat_text("hi") == "好"
    assert any(s and s >= 25 for s in sleeps)          # 冷却 ~30s，不是旧的 1~8s
    # 新请求仍先等冷却（全局共享，逐帧 3 并发同理）
    before = len(sleeps)
    monkeypatch.setattr(requests, "post", lambda *a, **k: ok)
    v.chat_text("again")
    assert len(sleeps) > before


def test_429_respects_retry_after_header(monkeypatch):
    """服务端给了 Retry-After 就听它的，不用默认 30s（5s 下限钳防碎片重试）。"""
    sleeps = []
    monkeypatch.setattr(vs.time, "sleep", lambda s=None: sleeps.append(s))
    seq = iter([Resp(status=429, headers={"Retry-After": "8"}),
                Resp(payload={"choices": [{"message": {"content": "ok"}}],
                              "usage": {"total_tokens": 1}})])
    monkeypatch.setattr(requests, "post", lambda *a, **k: next(seq))
    v = vs.DoubaoVision({"api_key": "k", "endpoint": "ep"})
    v.chat_text("hi")
    assert any(s and 7.5 <= s <= 9 for s in sleeps)    # 精确等了 Retry-After 的 8s


def test_429_set_limit_exceeded_fails_fast_with_guidance(monkeypatch):
    """安全体验模式用量打满：秒回 429 SetLimitExceeded（非分钟级限流，冷却重试
    永远等不回来）——必须快速失败、不冷却、把控制台自助恢复路径回给用户。"""
    sleeps = []
    monkeypatch.setattr(vs.time, "sleep", lambda s=None: sleeps.append(s))
    hits = {"n": 0}
    body = ('{"error":{"code":"SetLimitExceeded","message":"Your account has '
            'reached the set usage limit for the [doubao-seed-2-1-pro] model, '
            'and the model service has been paused."}}')

    def paused(*a, **k):
        hits["n"] += 1
        return Resp(status=429, text=body)
    monkeypatch.setattr(requests, "post", paused)
    v = vs.DoubaoVision({"api_key": "k", "endpoint": "ep"})
    try:
        v.chat_text("hi")
        assert False, "应抛 VisionError"
    except VisionError as e:
        assert "安全体验模式" in str(e)                 # 给出可执行的恢复指引
    assert hits["n"] == 1                              # 不重试
    assert not sleeps                                  # 不冷却（冷却也等不回来）


# ---------------- video_url 直连：整条视频一次调用 ----------------

def _video_shots_json():
    return json.dumps([
        {"index": 1, "start": 0.0, "end": 3.0, "shot_size": "近景", "camera": "手持",
         "composition": "居中", "transition": "无", "on_screen_text": "", "emotion": "轻松"},
        {"index": 2, "start": 3.0, "end": 8.0, "shot_size": "特写", "camera": "推",
         "composition": "三分", "transition": "硬切", "on_screen_text": "限时", "emotion": "紧迫"}])


def test_analyze_video_body_format_and_parse(monkeypatch):
    """报文：fps 在 video_url 对象内（官方文档）；返回数组→[FrameAnalysis]；计 1 次调用。"""
    seen = []
    monkeypatch.setattr(requests, "post", lambda url, **k: (
        seen.append(dict(k["json"])) or
        Resp(payload={"choices": [{"message": {"content": _video_shots_json()}}],
                      "usage": {"total_tokens": 900}})))
    v = vs.DoubaoVision({"api_key": "k", "endpoint": "ep"})
    out = v.analyze_video("https://x/v.mp4", fps=0.5)
    seg = seen[0]["messages"][0]["content"]
    assert seg[0]["type"] == "video_url"
    assert seg[0]["video_url"] == {"url": "https://x/v.mp4", "fps": 0.5}
    assert seg[1]["type"] == "text" and "JSON 数组" in seg[1]["text"]
    assert [a.idx for a in out] == [1, 2]
    assert out[1].ts == 3.0 and out[1].shot_size == "特写"
    assert out[1].on_screen_text == "限时" and out[1].emotion == "紧迫"
    assert v.cost() == {"vision_calls": 1, "tokens": 900}      # 整条视频仅一次


def test_analyze_video_accepts_shots_wrapper(monkeypatch):
    """返回 {"shots":[…]} 包裹形式也容错；缺 index 按顺序补，时间字段兼容 ts。"""
    payload = json.dumps({"shots": [{"ts": 1.5, "shot_size": "全景"}]})
    monkeypatch.setattr(requests, "post", _ok(payload))
    v = vs.DoubaoVision({"api_key": "k", "endpoint": "ep"})
    out = v.analyze_video("https://x/v.mp4")
    assert len(out) == 1 and out[0].idx == 1 and out[0].ts == 1.5


def test_analyze_video_unparseable_raises(monkeypatch):
    """解不出分镜绝不静默出空：抛 VisionError 带返回片段，交调用方回退逐帧。"""
    monkeypatch.setattr(requests, "post", _ok("抱歉，该视频无法访问"))
    v = vs.DoubaoVision({"api_key": "k", "endpoint": "ep"})
    try:
        v.analyze_video("https://x/v.mp4")
        assert False, "应抛 VisionError"
    except VisionError as e:
        assert "解析不出分镜" in str(e)


def test_analyze_video_requires_url():
    """无 url 不得静默发起空请求。"""
    v = vs.DoubaoVision({"api_key": "k", "endpoint": "ep"})
    try:
        v.analyze_video(None)
        assert False, "应抛 VisionError"
    except VisionError as e:
        assert "url" in str(e)
