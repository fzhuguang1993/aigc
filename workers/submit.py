"""
worker_submit.py —— 提交逻辑（带背压 + 负载均衡）
"""
from pathlib import Path

from core.config import MODE, LORA_BY_MODE, REFERENCE_IMAGES, ASSET_DIR, MAX_RETRY, ACCOUNTS as CONFIG_ACCOUNTS, DEFAULT_DURATION, KOL_DIR
from core.logger import Ctx
from core.api_client import upload_asset, submit_job
from utils.excel_utils import load_tasks, update_row, COL_RUNS, COL_STATUS, COL_ACCOUNT, COL_JOB_ID
from registry.manager import REG, invalidate_load_cache, get_account, pick_account
from workers.scan import mark_submitted


def resolve_path(name):
    p = Path(name)
    return p if p.is_absolute() else (Path(ASSET_DIR) / p)


def build_payload(base, prompt, ctx, kol=None):
    loras = [LORA_BY_MODE.get(MODE, "")] if LORA_BY_MODE.get(MODE) else []
    refs = REFERENCE_IMAGES if MODE == "r2v" else []
    
    # 添加 KOL 图片（如果选择了）
    if kol:
        kol_path = f"{KOL_DIR}/{kol}.png"
        kol_p = Path(kol_path)
        if kol_p.exists():
            refs.append(kol_path)
            ctx.info(f"使用 KOL: {kol}")

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
        "duration": DEFAULT_DURATION, "seed": -1,
        "loras": [{"name": n} for n in loras],
    }
    return {"feature": "minimax-h3", "mode": MODE,
            "inputs": inputs, "parameters": params}


def do_submit(row_idx, product, prompt):
    acc = pick_account()
    ctx = Ctx(row=row_idx, account=acc.name)

    # 从全局变量获取选中的 KOL
    from console.app import selected_kol
    kol = selected_kol

    ctx.debug(f"等待 {acc.name} 信号量（并发={acc.concurrency}）")
    acc.sem.acquire()
    try:
        try:
            payload = build_payload(acc.base, prompt, ctx, kol)
        except Exception as e:
            ctx.error(f"组装 payload 异常：{e}")
            return None, str(e), acc.name

        for attempt in range(MAX_RETRY + 1):
            try:
                resp, err = submit_job(acc.base, payload)
                if err is None:
                    job_id = resp["job_id"]
                    ctx.job_id = job_id
                    ctx.info("提交成功")
                    REG.add(job_id, row_idx, acc.name, prompt, product)
                    invalidate_load_cache(acc.name)
                    mark_submitted(row_idx)

                    df = load_tasks()
                    runs = int(df.at[row_idx, COL_RUNS] or 0) + 1
                    update_row(row_idx,
                               **{COL_RUNS: runs,
                                  COL_STATUS: "submitted",
                                  COL_ACCOUNT: acc.name,
                                  COL_JOB_ID: job_id})
                    return job_id, None, acc.name
                ctx.error(f"提交失败：{err}")
            except Exception as e:
                ctx.error(f"提交异常：{e}")

            if attempt < MAX_RETRY:
                next_acc = pick_account()
                if next_acc.name == acc.name:
                    next_acc = None
                    for a in CONFIG_ACCOUNTS:
                        if a["name"] != acc.name:
                            next_acc = get_account(a["name"])
                            break
                if next_acc:
                    ctx.info(f"换账号 {next_acc.name} 重试")
                    acc = next_acc
                    ctx.account = acc.name

        return None, "all retries failed", acc.name
    finally:
        acc.sem.release()


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
