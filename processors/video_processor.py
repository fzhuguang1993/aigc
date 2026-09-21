"""
video_processor.py —— 下载 + 重命名（保存到运行目录下的 outputs/）
"""
import time
import re
from pathlib import Path
from datetime import datetime

from core.config import DOWNLOAD_DIR, DEFAULT_PRODUCT, USER_NAME
from utils.excel_utils import sanitize, build_filename
from core.api_client import download_to
from store import task_store


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


def process_outputs(outs, base, row_idx, job_id, product, ctx):
    """处理视频输出，汇总日志（row_idx 现为数据库任务ID）"""
    name = USER_NAME
    complete_time = datetime.now()
    date_str = complete_time.strftime("%m%d")

    task = task_store.get_task(row_idx) or {}
    try:
        num = int(float(task.get("num") or 0))
    except Exception:
        num = 0
    if not num:
        num = row_idx

    items = outs.get("outputs", []) if isinstance(outs, dict) else []
    local_paths, full_urls = [], []
    success_count = 0
    
    for item in items:
        rel = item.get("url", "")
        if not rel:
            continue
        full_url = build_full_url(base, rel)
        full_urls.append(full_url)

        prod = sanitize(product) or DEFAULT_PRODUCT
        prefix = f"{num:03d}_{prod}_{date_str}_"
        seq = next_seq(prefix)

        fname = build_filename(num, product, complete_time, name, seq)
        save_path = Path(DOWNLOAD_DIR) / date_str / fname
        save_path.parent.mkdir(parents=True, exist_ok=True)  # 运行目录下自动创建日期文件夹
        # 强制重跑/抽卡时同一任务可能几乎同时完成多个 job，避免不同轮次撞名互相覆盖
        while save_path.exists():
            seq += 1
            fname = build_filename(num, product, complete_time, name, seq)
            save_path = Path(DOWNLOAD_DIR) / date_str / fname

        if download_with_retry(full_url, save_path, ctx):
            local_paths.append(str(save_path))
            success_count += 1
    
    # 汇总输出日志（由调用方 poll.py 统一显示）
    return local_paths, full_urls


def next_seq(prefix):
    base = Path(DOWNLOAD_DIR)
    if not base.exists():
        return 1
    max_seq = 0
    pat = re.compile(re.escape(prefix) + r"(\d+)_")
    for p in base.rglob(f"{prefix}*"):
        m = pat.match(p.name)
        if m:
            max_seq = max(max_seq, int(m.group(1)))
    return max_seq + 1
