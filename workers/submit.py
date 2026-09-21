"""
workers/submit.py —— 提交链路（背压 + 负载均衡 + 带退避的换线重试）

设计约定（详见 docs/design-conventions.md）：
- 提交选项通过 SubmitOptions 由调用方（GUI/控制台）显式传入，
  worker 层不保留任何模块级可变全局状态；
- api_client 统一抛 ApiError，本层只决定"是否重试 / 是否换账号"；
- 重试采用指数退避，避免接口抖动时连续撞墙。
"""
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.config import (MODE, LORA_BY_MODE, ASSET_DIR, MAX_RETRY, DEFAULT_DURATION,
                         ACCOUNTS as CONFIG_ACCOUNTS)
from core.logger import Ctx
from core.api_client import upload_asset, submit_job, ApiError
from store import task_store, product_store
from store.task_store import COL_RUNS, COL_STATUS, COL_ACCOUNT, COL_JOB_ID
from registry.manager import REG, invalidate_load_cache, get_account, pick_account
from workers.scan import mark_submitted

# 重试退避：第 n 次重试前 sleep min(BASE * 2^n, CAP) 秒
RETRY_BACKOFF_BASE = 2
RETRY_BACKOFF_CAP = 10


@dataclass(frozen=True)
class SubmitOptions:
    """一次提交的选项快照（表现层构建，随任务显式传递，不做全局态）"""
    duration: int = DEFAULT_DURATION
    kol: Optional[str] = None


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

    params = {
        "width": 768, "height": 1376,
        "duration": options.duration, "seed": -1,
        "loras": [{"name": n} for n in loras],
    }
    return {"feature": "minimax-h3", "mode": MODE,
            "inputs": inputs, "parameters": params}


def _next_account(current):
    """换线重试：选一个非当前的健康账号；没有则原账号重试。"""
    for cfg in CONFIG_ACCOUNTS:
        if cfg["name"] == current.name:
            continue
        cand = get_account(cfg["name"])
        if cand and cand.healthy:
            return cand
    return current


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
                             COL_JOB_ID: job_id})
    task_store.record_run_start(row_idx, task.get("num", ""),
                                product, acc.name, job_id)


def do_submit(row_idx, product, prompt, options=None, *, _sleep=time.sleep):
    """
    提交单个任务：选账号 → 组装 → 提交；失败则退避重试并优先换账号。
    全程持有账号信号量（背压），换账号时旧信号量立即释放。

    返回 (job_id, err_msg, account_name)，成功时 err_msg 为 None。
    _sleep 仅供测试注入，业务代码勿传。
    """
    options = options or SubmitOptions()
    acc = pick_account()
    ctx = Ctx(row=row_idx, account=acc.name)

    ctx.debug(f"等待 {acc.name} 信号量（并发={acc.concurrency}）")
    held = acc  # 当前实际持有信号量的账号（修复：旧实现换账号后释放错了信号量）
    held.sem.acquire()
    try:
        for attempt in range(MAX_RETRY + 1):
            try:
                payload = build_payload(acc.base, prompt, ctx, options, product)
                job_id = submit_job(acc.base, payload)["job_id"]
                _register_success(acc, row_idx, job_id, prompt, product,
                                  ctx, options.duration)
                ctx.info("提交成功")
                return job_id, None, acc.name
            except ApiError as e:
                ctx.error(f"提交失败：{e}")
            except Exception as e:
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

        return None, "all retries failed", acc.name
    finally:
        held.sem.release()


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
