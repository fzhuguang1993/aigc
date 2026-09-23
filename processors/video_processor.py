"""
video_processor.py —— 下载 + 按命名规则重命名（保存到运行目录下的 outputs/）

文件名怎么拼不在这里：字段清单与排列规则统一由 core/naming.py 管
（设置页「🏷 命名规则」可改，改完立即生效）。本模块只负责凑齐一个文件
的上下文（编号/品名/线路/备注…）交给命名模块，再把字节落盘。
"""
import time
from pathlib import Path
from datetime import datetime

from core.config import DOWNLOAD_DIR, USER_NAME
from core import naming
from core.api_client import download_to
from store import task_store
from utils.excel_utils import load_name_rule


def build_full_url(base, rel_url):
    host = base.replace("/api/v1", "").rstrip("/")
    if rel_url.startswith("http"):
        return rel_url
    if not rel_url.startswith("/"):
        rel_url = "/" + rel_url
    return host + rel_url


def download_with_retry(url, save_path, ctx):
    """下载文件，只在最终结果时输出日志"""
    for attempt in range(1, 4):
        try:
            download_to(url, save_path)
            size_mb = Path(save_path).stat().st_size / 1024 / 1024
            # 只记录第一次成功的情况（由调用方汇总）
            return True
        except Exception as e:
            if attempt < 3:
                ctx.debug(f"下载重试 (第{attempt}次): {e}")
                time.sleep(5)
            else:
                ctx.error(f"下载最终失败：{Path(save_path).name}")
    return False


def process_outputs(outs, base, row_idx, job_id, product, ctx, account=""):
    """处理视频输出，汇总日志（row_idx 现为数据库任务ID）

    account 只用来填命名规则里的「线路」字段，没传就不影响其它段。"""
    # 姓名：设置里填的优先，没填回退 Excel「命名规则」sheet（历史口径）
    name = USER_NAME or load_name_rule()
    complete_time = datetime.now()

    task = task_store.get_task(row_idx) or {}
    try:
        num = int(float(task.get("num") or 0))
    except Exception:
        num = 0
    if not num:
        num = row_idx

    items = outs.get("outputs", []) if isinstance(outs, dict) else []
    local_paths, full_urls = [], []

    for item in items:
        rel = item.get("url", "")
        if not rel:
            continue
        full_url = build_full_url(base, rel)
        full_urls.append(full_url)

        # 按天一个子目录（历史行为，与命名规则无关）
        save_dir = Path(DOWNLOAD_DIR) / naming.subdirname(complete_time)
        save_dir.mkdir(parents=True, exist_ok=True)
        # 强制重跑/抽卡时同一任务可能几乎同时完成多个 job，
        # 靠命名模块保证不撞名（有「序号」就续序号，没有就挂 (2) 尾巴）
        save_path = naming.resolve_save_path(save_dir, {
            "num": num, "product": product, "name": name, "when": complete_time,
            "account": account, "remark": task.get("remark") or "",
            "task_id": row_idx, "job_id": job_id})

        if download_with_retry(full_url, save_path, ctx):
            local_paths.append(str(save_path))

    # 汇总输出日志（由调用方 poll.py 统一显示）
    return local_paths, full_urls
