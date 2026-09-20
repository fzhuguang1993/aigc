"""
worker_poll.py —— 每账号一个轮询线程
"""
import time
import threading
from pathlib import Path

from core.config import POLL_INTERVAL
from utils.excel_utils import load_tasks, save_tasks, COL_STATUS, COL_OUTPUT, COL_URL, COL_SUCCESS, COL_CANCEL
from core.api_client import query_job, get_outputs
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
                # 任务完成，汇总处理
                local_paths, full_urls = [], []
                summary_parts = [f"{st}"]
                
                if st == "completed":
                    try:
                        outs = get_outputs(acc.base, t["job_id"])
                        local_paths, full_urls = processor.process_outputs(
                            outs, acc.base, t["row_idx"], t["job_id"],
                            t["product"], ctx)
                        
                        # 汇总下载信息
                        download_count = len(local_paths)
                        
                        # 回写 Excel
                        df = load_tasks()
                        i = t["row_idx"]
                        df.at[i, COL_STATUS] = str(st)
                        df.at[i, COL_OUTPUT] = "; ".join(local_paths)
                        df.at[i, COL_URL] = "; ".join(full_urls)
                        df.at[i, COL_SUCCESS] = int(df.at[i, COL_SUCCESS] or 0) + 1
                        save_tasks(df)
                        
                        # 一条日志汇总所有信息
                        summary_parts = [f"{st}"]
                        summary_parts.append(f"下载{download_count}个")
                        summary_parts.append("Excel 已回写")
                        ctx.info(" | ".join(summary_parts))
                        
                    except Exception as e:
                        ctx.error(f"下载/处理异常：{e}")
                        summary_parts.append(f"失败：{e}")
                        ctx.info(" | ".join(summary_parts))

                elif st == "cancelled":
                    # 取消任务
                    df = load_tasks()
                    i = t["row_idx"]
                    df.at[i, COL_STATUS] = str(st)
                    df.at[i, COL_CANCEL] = int(df.at[i, COL_CANCEL] or 0) + 1
                    save_tasks(df)
                    summary_parts.append("Excel 已回写")
                    ctx.info(" | ".join(summary_parts))
                else:  # failed
                    df = load_tasks()
                    i = t["row_idx"]
                    df.at[i, COL_STATUS] = str(st)
                    save_tasks(df)
                    ctx.info(" | ".join(summary_parts))

                REG.remove(t["job_id"])
                last_print.pop(t["job_id"], None)

        time.sleep(POLL_INTERVAL)


def start_poll_threads():
    from registry.manager import ACCOUNTS
    for acc in ACCOUNTS:
        threading.Thread(target=poll_worker_for_account, args=(acc.name,),
                         daemon=True, name=f"poll-{acc.name}").start()
