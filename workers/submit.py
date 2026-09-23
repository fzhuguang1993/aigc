"""
workers/submit.py —— 提交链路（背压 + 负载均衡 + 带退避的换线重试）

设计约定（详见 docs/design-conventions.md）：
- 提交选项通过 SubmitOptions 由调用方（GUI/控制台）显式传入，
  worker 层不保留任何模块级可变全局状态；
- api_client 统一抛 ApiError，本层只决定"是否重试 / 是否换账号"；
- 重试采用指数退避，避免接口抖动时连续撞墙；
- **4xx 属于「请求本身写错了」，换一条线路也是同样的报文，一律不换线重试**
  （旧实现对 422 也逐条线退避重试，白烧参考图上传和时间，还让界面以为线路坏了）。

云端 payload 字段名以 `GET {base}/openapi.json` 里的 JobParameters 为准，
且 `additionalProperties: false`——多一个字段就整单被拒。详见
 docs/云端接口字段说明.md。
"""
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.config import (MODE, LORA_BY_MODE, ASSET_DIR, MAX_RETRY, DEFAULT_DURATION,
                         DEFAULT_STEPS)
from core.logger import Ctx, raw_info
from core.api_client import upload_asset, submit_job, ApiError
from processors.script_extractor import ensure_script_fields
from store import task_store, product_store
from store.task_store import (COL_RUNS, COL_STATUS, COL_ACCOUNT, COL_JOB_ID,
                              COL_DURATION)
from registry.manager import (REG, invalidate_load_cache, get_account, pick_account,
                              measure_load, pick_and_wait)
from workers.scan import mark_submitted

# 重试退避：第 n 次重试前 sleep min(BASE * 2^n, CAP) 秒
RETRY_BACKOFF_BASE = 2
RETRY_BACKOFF_CAP = 10


@dataclass(frozen=True)
class SubmitOptions:
    """一次提交的选项快照（表现层构建，随任务显式传递，不做全局态）"""
    duration: int = DEFAULT_DURATION
    steps: int = DEFAULT_STEPS          # AI 生成步数（1-50）
    kol: Optional[str] = None

    def __post_init__(self):
        # 跨入口（GUI/控制台/测试）统一兜底：步数限幅 1-50
        if not 1 <= self.steps <= 50:
            object.__setattr__(self, "steps", min(50, max(1, int(self.steps))))


def resolve_path(name):
    p = Path(name)
    return p if p.is_absolute() else (Path(ASSET_DIR) / p)


def build_payload(base, prompt, ctx, options, product=None):
    """组装提交 payload。参考图按 base 账号上传，换账号重试后需重新组装。"""
    loras = [LORA_BY_MODE.get(MODE, "")] if LORA_BY_MODE.get(MODE) else []
    # 参考图：产品中心该品名的图片优先，未建产品时为空（回退链见 docs/design-conventions.md）
    refs = product_store.images_for_product(product) if MODE == "r2v" else []

    # KOL 图片（产品中心登记优先，material/KOL 目录回退）
    if options.kol:
        kol_path = product_store.kol_image(options.kol)
        if kol_path:
            refs.append(kol_path)
            ctx.info(f"使用 KOL: {options.kol}")
        else:
            ctx.warning(f"KOL「{options.kol}」未找到形象图，已忽略")

    inputs = {"prompt": prompt}
    ref_ids = []
    for name in refs:
        p = resolve_path(name)
        if not p.exists():
            ctx.warning(f"参考图不存在：{p}")
            continue
        aid = upload_asset(base, p, "image")
        ref_ids.append(aid)
        # 上传日志降级为 DEBUG（避免冗余）
        ctx.debug(f"上传参考图 {p.name} -> {aid[:20]}...")
    if ref_ids:
        inputs["reference_images"] = ref_ids

    # 步数字段叫 inference_steps，不是 steps：云端 JobParameters 是
    # additionalProperties=False 的严格模型，多一个 steps 就整单 422
    # （实测报文：{"type":"extra_forbidden","loc":["body","parameters","steps"]}）
    params = {
        "width": 768, "height": 1376,
        "duration": options.duration, "seed": -1,
        "inference_steps": options.steps,      # AI 生成步数（1-50）
        "loras": [{"name": n} for n in loras],
    }
    return {"feature": "minimax-h3", "mode": MODE,
            "inputs": inputs, "parameters": params}


# 云端 422 报文里的字段拒绝类型：extra_forbidden=不认识的字段
_REJECT_TYPES = {"extra_forbidden", "unknown"}


def is_client_error(err):
    """4xx（除 429 限流）= 请求本身写错了，换线路/重试都无济于事"""
    code = getattr(err, "status_code", None)
    return bool(code) and 400 <= code < 500 and code != 429


def parse_rejected(err_text):
    """从 422 报文里取出「云端不认识的字段路径」，如 [["body","parameters","steps"]]。

    兼容两种 FastAPI 包装：{error:{details:{errors:[...]}}} 与顶层 {detail:[...]}。
    解析不出来返回 []（调用方据此放弃自愈、直接报错）。
    """
    m = re.search(r"\{.*\}", err_text or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        errors = (data.get("error", {}).get("details") or {}).get("errors") \
            or data.get("details", {}).get("errors") or data.get("detail")
    except (ValueError, AttributeError):
        return []                      # 报文解析不了（返回的是 HTML 等）：不猜，直接报错
    if not isinstance(errors, list):
        return []
    return [e["loc"] for e in errors
            if isinstance(e, dict) and e.get("type") in _REJECT_TYPES
            and isinstance(e.get("loc"), list) and len(e["loc"]) > 1]


def drop_rejected(payload, locs):
    """把云端拒绝的字段从 payload 里摘掉，返回被摘掉的字段名列表。

    自愈用的：只要字段名对不上，整批提交会 100% 失败（本次事故即如此）；
    摘掉被拒字段后同一条线路立即重试，最多损失一个参数而不是全部任务。
    """
    dropped = []
    for loc in locs:
        path = [str(x) for x in loc if x != "body"]     # ["body","parameters","steps"]
        node = payload
        for key in path[:-1]:
            if not isinstance(node, dict) or key not in node:
                node = None
                break
            node = node[key]
        if isinstance(node, dict) and path and path[-1] in node:
            node.pop(path[-1])
            dropped.append(".".join(path))
    return dropped


def client_error_brief(err):
    """把 4xx 报文压成一句人话，供界面直接展示，如
    「HTTP 422 云端拒绝参数：parameters.steps」"""
    text = str(err)
    fields = parse_rejected(text)
    if fields:
        names = "、".join(".".join(str(x) for x in f if x != "body") for f in fields)
        return f"{text.split(':')[0]} 云端拒绝参数：{names}"
    return text.split("}")[0][:160] or "提交失败"


def _next_account(current):
    """换线重试：仍按负载选一条不是 current 的健康线。
    （旧实现取配置顺序的第一条健康账号，等价于「不管怎么换都换到 acc1」）"""
    return pick_account(exclude=current)


def _register_success(acc, row_idx, job_id, prompt, product, ctx, duration):
    """提交成功后的登记：运行时注册表 + 任务表回写 + 执行记录。"""
    ctx.job_id = job_id
    REG.add(job_id, row_idx, acc.name, prompt, product, duration=duration)
    invalidate_load_cache(acc.name)
    mark_submitted(row_idx)

    task = task_store.get_task(row_idx) or {}
    runs = int(task.get("runs") or 0) + 1
    task_store.update_row(row_idx,
                          **{COL_RUNS: runs,
                             COL_STATUS: "submitted",
                             COL_ACCOUNT: acc.name,
                             COL_JOB_ID: job_id,
                             COL_DURATION: int(duration or 0)})   # 任务表留痕本次提交时长
    task_store.record_run_start(row_idx, task.get("num", ""),
                                product, acc.name, job_id)


def do_submit(row_idx, product, prompt, options=None, *, balancer=None, _sleep=time.sleep):
    """
    提交单个任务：选线（带在途闸门）→ 组装 → 提交；失败则退避重试并换线。

    返回 (job_id, err_msg, account_name)；成功时 err_msg 为 None，
    失败时 err_msg 是一句能直接给人看的原因（不再只回 "all retries failed"）。

    balancer：批量提交时传入 BatchBalancer，走「快照+投影」注水选线并**绕开在途
    闸门**（排队模型下往忙线提交只是排队，不该阻塞等空位）；不传则为单条手动提交，
    保持原有 pick_and_wait 闸门行为不变。
    _sleep 仅供测试注入，业务代码勿传。
    """
    options = options or SubmitOptions()
    # 执行时自动补齐：口播文案/分镜数为空则从提示词识别（不限时长，失败静默）
    try:
        ensure_script_fields(row_idx, prompt)
    except Exception:
        pass

    # 选线：批量模式走注水分配器（绕闸门），单条手动提交走在途闸门
    if balancer is not None:
        acc = balancer.pick()
        ctx = Ctx(row=row_idx, account=acc.name)
        load, src = measure_load(acc)
        ctx.debug(f"批量选线 {acc.name}｜投影在途 {load}｜"
                  f"{'云端全量' if src == 'cloud' else '仅本机视角⚠'}")
    else:
        # 选线 + 在途闸门：挑一条真有空位的线，全满则等到有人空出来
        # （旧实现只锁住提交动作那几百毫秒，一条线能被无限灌）
        acc, waited, got_slot = pick_and_wait(_log=raw_info)
        ctx = Ctx(row=row_idx, account=acc.name)
        if not got_slot:
            ctx.warning(f"{acc.name} 等空位超过 {waited}s 仍全满，按现状提交")
        elif waited:
            ctx.info(f"等 {waited}s 后拿到 {acc.name} 空位")
        else:
            load, src = measure_load(acc)
            ctx.debug(f"选线 {acc.name}｜在途 {load}/{acc.concurrency}｜"
                      f"{'云端全量' if src == 'cloud' else '仅本机视角⚠'}")

    ctx.debug(f"等待 {acc.name} 信号量（并发={acc.concurrency}）")
    held = acc  # 当前实际持有信号量的账号（修复：旧实现换账号后释放错了信号量）
    held.sem.acquire()
    payload = None        # 参考图按账号上传，只在换线后重建，同线重试复用
    built_for = None
    last_err = ""
    try:
        for attempt in range(MAX_RETRY + 1):
            if payload is None or built_for != acc.base:
                payload = build_payload(acc.base, prompt, ctx, options, product)
                built_for = acc.base
            try:
                # submit_job 已在出口处取好 job_id（响应格式异常抛 ApiError
                # 并回显原文），这里绝不裸取键：KeyError 会被下面当成线路故障
                job_id = submit_job(acc.base, payload)
                _register_success(acc, row_idx, job_id, prompt, product,
                                  ctx, options.duration)
                ctx.info("提交成功")
                return job_id, None, acc.name
            except ApiError as e:
                last_err = client_error_brief(e)
                if is_client_error(e):
                    # 参数名/取值不对：换线路毫无意义，先尝试摘掉被拒字段原线重试
                    dropped = drop_rejected(payload, parse_rejected(str(e)))
                    if dropped and attempt < MAX_RETRY:
                        ctx.warning(f"云端不接受参数「{'、'.join(dropped)}」，"
                                    f"已自动去除后重试（该参数本次不生效）")
                        continue
                    ctx.error(f"提交失败（参数被云端拒绝，不再换线重试）：{e}")
                    return None, last_err, acc.name
                ctx.error(f"提交失败：{e}")
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                ctx.error(f"提交异常：{e}")

            if attempt < MAX_RETRY:
                _sleep(min(RETRY_BACKOFF_BASE * (2 ** attempt), RETRY_BACKOFF_CAP))
                next_acc = _next_account(acc)
                if next_acc is not acc:
                    ctx.info(f"换账号 {next_acc.name} 重试")
                    held.sem.release()
                    next_acc.sem.acquire()
                    held = next_acc
                    acc = next_acc
                    ctx.account = acc.name

        return None, last_err or "所有重试均失败", acc.name
    finally:
        held.sem.release()


def format_batch_distribution(landed, total_lines):
    """把本批提交成功的任务落到哪些线路汇成一句人话：「本批 10 条分布：acc1×4 acc2×3 …」

    只传提交成功的账号名（失败的自有弹窗留痕），否则“分布”会把根本没占线路的失败也算一条。

    为何要专门留这一行：选线是后台做的，“多选强制重跑只跑了一个线路”这种
    问题以前只能翻 DEBUG 日志复盘，同事反馈时根本说不清当时到底怎么分的。
    全摊到一条线上（线路不止一条时）额外给 ⚠，把“均衡失效”从口头描述变成现
    场留痕。
    """
    names = [n for n in landed if n]
    if not names:
        return ""
    counter = Counter(names)
    parts = " ".join(f"{n}×{c}" for n, c in
                     sorted(counter.items(), key=lambda x: (-x[1], x[0])))
    note = f"本批 {len(names)} 条分布：{parts}"
    if len(counter) == 1 and total_lines > 1:
        note += (f"　⚠ {total_lines} 条线路只用上了 {names[0]}："
                 "其余线路探活未通过或已排满，去「📡 线路负载」看一眼状态列")
    return note


def cancel_one(task):
    acc = get_account(task["account"])
    if not acc:
        return False
    ctx = Ctx(row=task["row_idx"], account=acc.name, job_id=task["job_id"])
    try:
        from core.api_client import cancel_job
        cancel_job(acc.base, task["job_id"])
        ctx.info("已发送取消请求")
        REG.update(task["job_id"], "cancelling", task["progress"])
        return True
    except Exception as e:
        ctx.error(f"取消失败：{e}")
        return False
