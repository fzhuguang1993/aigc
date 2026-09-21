"""
worker_poll.py —— 每账号一个轮询线程
"""
import time
import threading
from pathlib import Path

from core.config import POLL_INTERVAL, EXTRACT_SCRIPT_ENABLED, MIN_DURATION_FOR_SCRIPT
from store import task_store
from store.task_store import (COL_STATUS, COL_OUTPUT, COL_URL, COL_SUCCESS,
                              COL_CANCEL, COL_SCRIPT_TEXT)
from core.api_client import query_job, get_outputs, extract_script
from core.logger import Ctx, raw_error
from registry.manager import REG, get_account
import processors.video_processor as processor


def poll_worker_for_account(acc_name):
    acc = get_account(acc_name)
    last_print = {}

    while True:
        tasks = REG.active_by_account(acc_name)
        if not tasks:
            time.sleep(POLL_INTERVAL)
            continue

        for t in tasks:
            ctx = Ctx(row=t["row_idx"], account=acc.name, job_id=t["job_id"])
            try:
                s = query_job(acc.base, t["job_id"])
            except Exception as e:
                # 静默处理查询异常，不刷屏
                if hasattr(ctx, '_last_error') and ctx._last_error == str(e):
                    pass  # 同一个错误不重复显示
                else:
                    # 只在首次出现时记录一次
                    ctx.debug(f"查询异常：{type(e).__name__}")
                    ctx._last_error = str(e)
                time.sleep(POLL_INTERVAL)  # 遇到错误跳过本次循环
                continue

            # 重置错误标记
            if hasattr(ctx, '_last_error'):
                del ctx._last_error
            
            st = s.get("status")
            prog = s.get("progress", 0)
            REG.update(t["job_id"], st, prog)

            # 不显示 running 状态，只显示最终结果
            key = (st, prog)
            if last_print.get(t["job_id"]) != key:
                last_print[t["job_id"]] = key

            if st in {"completed", "failed", "cancelled"}:
                # 任务终态：回写数据库 + 执行记录
                if st == "completed":
                    try:
                        outs = get_outputs(acc.base, t["job_id"])
                        local_paths, full_urls = processor.process_outputs(
                            outs, acc.base, t["row_idx"], t["job_id"],
                            t["product"], ctx)

                        task_store.update_row(t["row_idx"], **{
                            COL_STATUS: st,
                            COL_OUTPUT: "; ".join(local_paths),
                            COL_URL: "; ".join(full_urls)})
                        task_store.bump(t["row_idx"], COL_SUCCESS)
                        task_store.record_run_end(t["job_id"], st,
                                                  output="; ".join(local_paths))

                        # 时长达标才提取口播（duration 取自提交时的任务注册信息；失败静默）
                        try:
                            duration = t.get("duration") or 0
                            if EXTRACT_SCRIPT_ENABLED and \
                                    duration >= MIN_DURATION_FOR_SCRIPT:
                                script = extract_script(acc.base, t["prompt"])
                                if script:
                                    task_store.update_row(t["row_idx"],
                                                          **{COL_SCRIPT_TEXT: script})
                        except Exception:
                            pass

                        ctx.info(f"{st} | 下载{len(local_paths)}个 | 记录已回写")
                    except Exception as e:
                        task_store.update_row(t["row_idx"], **{COL_STATUS: "error"})
                        task_store.record_run_end(t["job_id"], "error", error=str(e))
                        ctx.error(f"下载/处理异常：{e}")

                elif st == "cancelled":
                    task_store.update_row(t["row_idx"], **{COL_STATUS: st})
                    task_store.bump(t["row_idx"], COL_CANCEL)
                    task_store.record_run_end(t["job_id"], st)
                    ctx.info(st)
                else:  # failed
                    task_store.update_row(t["row_idx"], **{COL_STATUS: st})
                    task_store.record_run_end(t["job_id"], st,
                                              error=str(s.get("error", "")))
                    ctx.info(st)

                REG.remove(t["job_id"])
                last_print.pop(t["job_id"], None)

        time.sleep(POLL_INTERVAL)


def start_poll_threads():
    from registry.manager import ACCOUNTS
    for acc in ACCOUNTS:
        threading.Thread(target=poll_worker_for_account, args=(acc.name,),
                         daemon=True, name=f"poll-{acc.name}").start()
