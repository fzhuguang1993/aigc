"""server/gateway.py —— 转发网关：/api/v1/*（与现有云端同名同形）

客户端只把 base 换成网关地址 + 带鉴权头，协议零改动。安全红线：

- 任何响应体不得出现上游 base / 真实 job_id / 真实 asset_id / 上游直链；
- job_id、asset_id 全部用网关自己的不透明 id，DB 映射回源；
- 产物直链改写为 /file 签名中转（流式），上游存储地址不外泄；
- 参考图先暂存网关（spool），提交时才转投选中的线路——上传与执行解耦。

未鉴权统一 401，授权过期 403（客户端据此弹激活窗）。
"""
import base64
import hashlib
import json
import os
import secrets
import uuid
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse

from server import config, store
from server.auth import require_license
from server.ratelimit import TOOL_MACHINE
from server import upstream, translate_api

router = APIRouter(prefix="/api/v1", tags=["gateway"])

MAX_UPLOAD_MB = 50

# 上游异常 → 网关对客户端的状态码映射（提交链路的换线重试靠 4xx/5xx 语义）
def _upstream_error(e):
    code = getattr(e, "status_code", None)
    if code and 400 <= code < 500 and code != 429:
        # 上游判我们报文错（理论不该发生，转发前已组装好）：原样透传 422 语义
        raise HTTPException(code, f"上游拒绝请求：{e}")
    raise HTTPException(502, f"上游线路异常：{e}")


# ---------------- 产物直链签名中转 ----------------

def _sign(u64):
    return hashlib.sha256((config.SECRET + "|" + u64).encode("utf-8")).hexdigest()[:16]


def wrap_url(real_url):
    """上游直链 → 网关中转 URL（真实地址只活在签名参数里，不可见不可改）"""
    p = base64.urlsafe_b64encode(real_url.encode("utf-8")).decode("ascii")
    return f"/api/v1/file?p={quote(p)}&s={_sign(p)}"


def unwrap_url(p, s):
    if not config.SECRET or s != _sign(p):
        raise HTTPException(403, "文件链接无效或已失效")
    try:
        return base64.urlsafe_b64decode(p.encode("ascii")).decode("utf-8")
    except Exception:
        raise HTTPException(400, "文件链接格式错误")


def _rewrite_urls(node):
    """递归把响应里的 http(s) 直链换成网关中转相对路径（客户端拼 base 后访问）"""
    if isinstance(node, dict):
        return {k: _rewrite_urls(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_rewrite_urls(v) for v in node]
    if isinstance(node, str) and node.startswith(("http://", "https://")):
        return wrap_url(node)
    return node


# ---------------- 探活与线路视图 ----------------

@router.get("/health")
def gw_health(principal=Depends(require_license)):
    lines = upstream.UPSTREAMS
    ready = sum(1 for u in lines if u.healthy)
    # 值全为字符串：客户端 main.py 的 `all(v == 'ready')` 汇总口径不被数字打破
    return {"status": "ready" if ready else "degraded",
            "state": "ready" if ready else "degraded",
            "upstreams": str(len(lines)),
            "ready": str(ready),
            "concurrency": str(upstream.total_concurrency())}


@router.get("/lines")
def gw_lines(principal=Depends(require_license)):
    """线路只读视图（供客户端「线路负载」页）：只有名字/健康/负载，没有地址"""
    cache = {}
    items = []
    for u in upstream.UPSTREAMS:
        info = u.info()
        load = upstream.measure_load(u, _cache=cache)
        info["load"] = load if load is not None else 0
        info["load_degraded"] = load is None
        items.append(info)
    return {"items": items}


# ---------------- 参考图暂存 ----------------

@router.post("/assets")
async def gw_upload(file: UploadFile = File(...), asset_type: str = Form("image"),
                    principal=Depends(require_license)):
    content = await file.read()
    if not content:
        raise HTTPException(400, "上传文件为空")
    if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD_MB}MB 上限")
    suffix = os.path.splitext(file.filename or "")[1][:10].lower() or ".bin"
    gw_id = "ast_" + uuid.uuid4().hex
    path = os.path.join(config.ASSET_SPOOL_DIR, gw_id + suffix)
    with open(path, "wb") as f:
        f.write(content)
    store.add_asset(gw_id, path, file.filename or gw_id, asset_type,
                    principal["machine_code"])
    return {"asset_id": gw_id}


def _collect_asset_ids(node, out):
    if isinstance(node, dict):
        for v in node.values():
            _collect_asset_ids(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_asset_ids(v, out)
    elif isinstance(node, str) and node.startswith("ast_"):
        out.append(node)


# ---------------- 任务提交 / 查询 / 取消 / 产物 ----------------

@router.post("/jobs")
async def gw_submit(request: Request, principal=Depends(require_license)):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "请求体不是合法 JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")

    try:
        u = upstream.pick_upstream()
        # 1) 暂存参考图 → 转投到选中线路，建立 gw_id → 真实 id 映射
        ids = []
        _collect_asset_ids(payload, ids)
        real_map = {}
        for gw_id in dict.fromkeys(ids):
            row = store.get_asset(gw_id)
            if not row or not os.path.exists(row["path"]):
                raise HTTPException(400, f"参考图暂存已过期（{gw_id}），请重新提交")
            with open(row["path"], "rb") as f:
                content = f.read()
            real_map[gw_id] = upstream.upload_asset(
                u, row["filename"], content, row["content_type"])
        # 2) 换 id 后提交
        submitted = _swap_ids(payload, real_map)
        real_job_id = upstream.submit_job(u, submitted)
    except upstream.UpstreamError as e:
        _upstream_error(e)

    gw_job_id = "gwj_" + uuid.uuid4().hex[:16]
    store.add_job(gw_job_id, u.name, real_job_id, principal["machine_code"])
    return {"job_id": gw_job_id, "upstream": u.name}


def _swap_ids(node, real_map):
    if isinstance(node, dict):
        return {k: _swap_ids(v, real_map) for k, v in node.items()}
    if isinstance(node, list):
        return [_swap_ids(v, real_map) for v in node]
    if isinstance(node, str) and node in real_map:
        return real_map[node]
    return node


def _need_job(gw_job_id):
    hit = upstream.resolve_gw_job(gw_job_id)
    if not hit:
        raise HTTPException(404, "任务不存在（网关不认识的 job_id）")
    return hit


@router.get("/jobs")
def gw_list_jobs(limit: int = 100, principal=Depends(require_license)):
    """聚合各线路队列（客户端负载均衡读这一口）。无映射的第三方任务给伪 id。"""
    items = []
    for u in upstream.UPSTREAMS:
        try:
            raw = upstream.list_jobs(u, limit=limit)
        except Exception:
            continue                    # 单线读不通不影响聚合
        for j in raw:
            real = str(j.get("job_id") or j.get("id") or "")
            row = store.find_job_by_real(u.name, real) if real else None
            item = dict(j)
            item["job_id"] = row["gw_job_id"] if row else \
                "ext_" + hashlib.md5(f"{u.name}:{real}".encode()).hexdigest()[:12]
            items.append(item)
    return {"items": items}


@router.get("/jobs/{gw_job_id}")
def gw_query_job(gw_job_id: str, principal=Depends(require_license)):
    u, real = _need_job(gw_job_id)
    try:
        body = upstream.query_job(u, real)
    except upstream.UpstreamError as e:
        _upstream_error(e)
    if isinstance(body, dict):
        body = dict(body)
        body.pop("job_id", None)
        body.pop("jobId", None)
        body["job_id"] = gw_job_id      # 绝不回显上游真实 id
        st = str(body.get("status") or "")
        if upstream.is_active(st) is False:
            store.update_job(gw_job_id, status=st, terminal=True)
        else:
            store.update_job(gw_job_id, status=st)
    return body


@router.get("/jobs/{gw_job_id}/outputs")
def gw_outputs(gw_job_id: str, principal=Depends(require_license)):
    u, real = _need_job(gw_job_id)
    try:
        body = upstream.get_outputs(u, real)
    except upstream.UpstreamError as e:
        _upstream_error(e)
    # 直链全部改写成网关中转（相对路径，客户端拼 base 访问）
    return _rewrite_urls(body)


@router.post("/jobs/{gw_job_id}/cancel")
def gw_cancel(gw_job_id: str, principal=Depends(require_license)):
    u, real = _need_job(gw_job_id)
    try:
        body = upstream.cancel_job(u, real)
    except upstream.UpstreamError as e:
        _upstream_error(e)
    store.update_job(gw_job_id, status="cancelled", terminal=True)
    return body if isinstance(body, (dict, list)) else {"ok": True}


@router.get("/file")
def gw_file(p: str, s: str, principal=Depends(require_license)):
    real_url = unwrap_url(p, s)
    try:
        upstream_r = httpx.stream("GET", real_url, timeout=600, follow_redirects=True)
        resp = upstream_r.__enter__()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"文件中转失败：{e}")
    if resp.status_code >= 400:
        resp.close()
        raise HTTPException(502, f"文件下载失败 HTTP {resp.status_code}")

    def iter_():
        try:
            for chunk in resp.iter_bytes(1 << 16):
                yield chunk
        finally:
            resp.close()

    return StreamingResponse(iter_(), headers={
        "Content-Disposition": f"attachment; filename=f{secrets.token_hex(4)}.mp4"})


# ---------------- 工具类接口（素材提取 / 翻译） ----------------

@router.post("/scripts/extract")
async def gw_extract(request: Request, principal=Depends(require_license)):
    """口播文案提取透传（云端线路自带能力）：{prompt} → {script}

    无状态短请求不进 job 映射表；健在线路轮流尝试，单线失败换下一条。"""
    _tool_rate(principal)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "请求体不是合法 JSON")
    prompt = str((body or {}).get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")
    pool = upstream.schedulable()
    if not pool:
        raise HTTPException(503, "网关未配置任何上游线路")
    last = None
    for u in pool:
        try:
            script = upstream.extract_script(u, prompt)
        except upstream.UpstreamError as e:
            last = e
            continue
        return {"script": script}
    _upstream_error(last)


def _tool_rate(principal):
    """按机器码每分钟限流：时长卡期内不限次，但要防单用户脚本刷爆"""
    if TOOL_MACHINE.hit(principal["machine_code"]) > config.TOOL_RATE_PER_MIN:
        raise HTTPException(429, "工具接口请求太频繁，歇一分钟再试")


@router.get("/material/parse")
def gw_material(type: str, url: str, principal=Depends(require_license)):
    """聚客解析转发：真实 base/uid/key 只在服务端 upstreams.json。

    保持 {code,msg,data} 原形返回；data 里的直链域名走客户端白名单校验，
    这里不 rewrite（直链是公开 CDN，客户端只下载不回调聚客）。
    """
    _tool_rate(principal)
    cfg = upstream.MATERIAL_API
    if not cfg.get("base"):
        raise HTTPException(503, "网关未配置素材提取接口")
    if type not in ("dsp", "wenan"):
        raise HTTPException(400, "type 仅支持 dsp/wenan")
    try:
        r = httpx.get(cfg["base"], params={"type": type, "uid": cfg.get("uid", ""),
                                           "key": cfg.get("key", ""), "url": url},
                      timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"提取接口不可达：{e}")
    try:
        return r.json()
    except ValueError:
        raise HTTPException(502, f"提取接口未返回 JSON：{r.text[:120]}")


@router.post("/translate")
async def gw_translate(request: Request, principal=Depends(require_license)):
    """火山文本翻译转发：ak/sk 只在服务端；请求 {texts,source,target} → {translations}"""
    _tool_rate(principal)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "请求体不是合法 JSON")
    texts = body.get("texts")
    if not isinstance(texts, list) or not texts:
        raise HTTPException(400, "texts 必须是非空数组")
    if not upstream.TRANSLATE_API.get("ak"):
        raise HTTPException(503, "网关未配置翻译密钥")
    try:
        out = translate_api.translate_texts(
            [str(t) for t in texts],
            source=str(body.get("source") or "auto"),
            target=str(body.get("target") or "zh"))
    except translate_api.TranslateError as e:
        raise HTTPException(502, str(e))
    return {"translations": out}
