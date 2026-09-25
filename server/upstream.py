"""server/upstream.py —— 上游线路池：负载均衡、探活、转发

把客户端 registry/manager.py 打磨过的口径搬到大服务端（商用后线路地址只活在
这台机器上，客户 exe 里再也翻不出真实接口）：

1. 任务是否占额度一律按「排除终态」判断（is_active），不枚举活跃态；
2. 探活拿到任何 HTTP 响应（含 404/405）只算「探活路径没实现」（alive≠故障），
   连接层异常与 5xx 才计失败；
3. 全线路不健康时退到 fail_count 最小的一档平摊，不死赌一条；
4. 负载相近（LOAD_TIE_BAND）随机挑——多用户同时提交时避免一起灌同一条线。

与客户端不同的调度决策：商用场景排队模型下网关**不等空位直接投最空闲线**
（等价旧的 BatchBalancer 行为）；上游队列满只会排队，不该阻塞用户请求。
参考图先暂存在网关（assets 表 + spool 目录），提交时才转投选中的线路，
由此彻底解耦"上传时选哪条线"与"执行时选哪条线"。
"""
import json
import random
import threading
import time

import httpx

from server import config, store

# 云端任务终态：除了这几个，其余一律视为「还在排队或还在跑」（与客户端同口径）
TERMINAL_STATUSES = {"completed", "succeeded", "success", "finished",
                     "failed", "error", "cancelled", "canceled",
                     "timeout", "expired"}

LOAD_TIE_BAND = 1


def is_active(status):
    return str(status or "").lower() not in TERMINAL_STATUSES


class UpstreamError(Exception):
    """上游调用失败。status_code：HTTP 状态码（连接层异常时 None）——
    classify_probe 靠它区分「服务有话回」与「真不通」。"""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class Upstream:
    def __init__(self, cfg):
        self.name = str(cfg.get("name") or "acc")
        self.base = str(cfg.get("base") or "").rstrip("/")
        self.api_key = str(cfg.get("api_key") or "")
        try:
            self.concurrency = max(int(cfg.get("concurrency") or 1), 1)
        except (TypeError, ValueError):
            self.concurrency = 1
        self.healthy = True
        self.fail_count = 0
        self.probe_state = None      # None 没测过 / ok / alive / down（同客户端语义）

    def info(self):
        """给 /lines 与 /health 的只读视图——绝不含 base，上游地址不出网关"""
        return {"name": self.name, "concurrency": self.concurrency,
                "healthy": self.healthy, "probe_state": self.probe_state,
                "fail_count": self.fail_count}


UPSTREAMS = []
MATERIAL_API = {}      # {"base","uid","key"}：聚客提取凭证，只存服务端
TRANSLATE_API = {}     # {"ak","sk","region","project"}：火山翻译凭证

_load_lock = threading.Lock()


def load_upstreams(path=None):
    """读取 upstreams.json；文件缺失=没配线路（网关会拒绝提交但服务可起）"""
    global UPSTREAMS, MATERIAL_API, TRANSLATE_API
    path = path or config.UPSTREAMS_FILE
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError):
        data = {}
    with _load_lock:
        UPSTREAMS = [Upstream(c) for c in (data.get("upstreams") or [])
                     if str(c.get("base") or "").strip()]
        MATERIAL_API = dict(data.get("material_api") or {})
        TRANSLATE_API = dict(data.get("translate") or {})
    return UPSTREAMS


def total_concurrency():
    return sum(u.concurrency for u in UPSTREAMS)


# ---------------- HTTP 出口（单点，测试整体替换） ----------------

def _http(method, url, **kw):
    """所有上游请求的统一出口：返回 httpx.Response，非 2xx 转 UpstreamError。

    tests 通过 monkeypatch 本函数模拟云端，不必起真服务。
    """
    kw.setdefault("timeout", 30)
    try:
        r = httpx.request(method, url, **kw)
    except httpx.HTTPError as e:
        raise UpstreamError(f"{type(e).__name__}: {e}") from e
    if r.status_code >= 400:
        raise UpstreamError(f"HTTP {r.status_code}: {r.text[:300]}", r.status_code)
    return r


def _headers(u, extra=None):
    h = dict(extra or {})
    if u is not None and u.api_key:
        h["Authorization"] = f"Bearer {u.api_key}"
    return h


# 云端不同接口把载荷包在外层对象里的写法（与客户端 api_client 同口径）
_ENVELOPES = ("data", "job", "task")


def _field(resp_json, *names):
    node = resp_json if isinstance(resp_json, dict) else {}
    for key in _ENVELOPES:
        inner = node.get(key)
        if isinstance(inner, dict) and any(n in inner for n in names):
            node = inner
            break
    for n in names:
        value = node.get(n)
        if value:
            return value
    raise UpstreamError(f"上游响应格式异常：未返回 {'、'.join(names)}：{str(resp_json)[:200]}")


# ---------------- 负载与选线 ----------------

def measure_load(u, _cache=None):
    """该线云端队列里的在途数；不可达返回 None。

    网关看不到"同事"——所有用户都从这里过，这一读数就是全局真值，
    不再需要客户端那套 max(cloud, local) 补视角。
    `_cache`：同一请求内的多次读数缓存（dict 传入），省掉重复 /jobs。
    """
    if _cache is not None and u.name in _cache:
        return _cache[u.name]
    try:
        items = _list_jobs_raw(u)
        load = sum(1 for j in items if is_active(j.get("status")))
    except Exception:
        load = None
    if _cache is not None:
        _cache[u.name] = load
    return load


def _list_jobs_raw(u, limit=None):
    r = _http("GET", f"{u.base}/jobs", params={"limit": limit or config.JOBS_LIMIT},
              headers=_headers(u))
    return r.json().get("items", [])


def schedulable():
    """健康线优先；全不健康时取 fail_count 最小的一档（平摊，不死赌一条）"""
    if not UPSTREAMS:
        return []
    healthy = [u for u in UPSTREAMS if u.healthy]
    if healthy:
        return healthy
    least = min(u.fail_count for u in UPSTREAMS)
    return [u for u in UPSTREAMS if u.fail_count == least] or list(UPSTREAMS)


def pick_upstream(cache=None):
    """注水口径：候选里挑负载最小的，相近的随机（多用户错峰）。

    所有线读数都拿不到时按"0 负载"处理——宁可乐观提交也不能判死整池
    （/jobs 读不通不代表提交不通，客户端同款教训）。
    """
    pool = schedulable()
    if not pool:
        raise UpstreamError("网关未配置任何上游线路（server/upstreams.json）")
    loads = {u.name: (measure_load(u, _cache=cache) if cache is not None
                       else measure_load(u)) for u in pool}
    ranked = sorted(pool, key=lambda u: loads[u.name] if loads[u.name] is not None else 0)
    best = loads[ranked[0].name] or 0
    ties = [u for u in ranked
            if (loads[u.name] or 0) <= best + LOAD_TIE_BAND]
    return random.choice(ties)


# ---------------- 上游动作 ----------------

def upload_asset(u, filename, content, asset_type="image"):
    """把网关暂存的参考图转投到选中线路，返回上游真实 asset_id。"""
    r = _http("POST", f"{u.base}/assets", timeout=(10, 300),
              params={"asset_type": asset_type},
              files={"file": (filename, content)},
              headers=_headers(u))
    return _field(r.json(), "asset_id", "id")


def submit_job(u, payload):
    r = _http("POST", f"{u.base}/jobs", timeout=(10, 60), json=payload,
              headers=_headers(u))
    return _field(r.json(), "job_id", "jobId", "id")


def query_job(u, real_job_id):
    return _http("GET", f"{u.base}/jobs/{real_job_id}", headers=_headers(u)).json()


def get_outputs(u, real_job_id):
    return _http("GET", f"{u.base}/jobs/{real_job_id}/outputs",
                 headers=_headers(u)).json()


def cancel_job(u, real_job_id):
    return _http("POST", f"{u.base}/jobs/{real_job_id}/cancel",
                 headers=_headers(u)).json()


def list_jobs(u, limit=None):
    return _list_jobs_raw(u, limit)


def health(u):
    return _http("GET", f"{u.base}/health", timeout=15, headers=_headers(u)).json()


def extract_script(u, prompt):
    """云端口播文案提取（无状态短请求）；失败抛 UpstreamError 由路由层换线。"""
    r = _http("POST", f"{u.base}/scripts/extract", timeout=(10, 60),
              json={"prompt": prompt}, headers=_headers(u))
    return r.json().get("script", "")


# ---------------- 探活 ----------------

def classify_probe(err):
    """与客户端 registry.manager.classify_probe 同口径：有 HTTP 状态码且 <500
    = 机器活着只是路径不对（alive）；连不上/5xx = down。"""
    code = getattr(err, "status_code", None)
    return "alive" if (code and code < 500) else "down"


def check_all():
    """逐条真实探活一轮（后台线程定时调，口径同客户端 health_monitor_worker）"""
    for u in UPSTREAMS:
        verdict = "down"
        for i in range(2):                      # 瞬时抖动重试一次
            try:
                health(u)
                verdict = "ok"
                break
            except Exception as e:
                verdict = classify_probe(e)
                if verdict == "alive":
                    break
                time.sleep(0.5)
        u.probe_state = verdict
        if verdict != "down":
            u.healthy = True
            u.fail_count = 0
        else:
            u.fail_count += 1
            if u.fail_count >= config.HEALTH_FAIL_THRESHOLD:
                u.healthy = False


def probe_loop(stop_event=None):
    stop_event = stop_event or threading.Event()
    check_all()
    while not stop_event.wait(config.HEALTH_CHECK_INTERVAL):
        check_all()


def get_by_name(name):
    for u in UPSTREAMS:
        if u.name == name:
            return u
    return None


# ---------------- 任务回源 ----------------

def resolve_gw_job(gw_job_id):
    """gw job_id → (Upstream, real_job_id)；无映射抛 KeyError 语义的 404 由路由层做"""
    row = store.get_job(gw_job_id)
    if not row:
        return None
    u = get_by_name(row["upstream"])
    return (u, row["real_job_id"]) if u else None
