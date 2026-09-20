"""
main.py —— 入口
"""
import threading
from datetime import datetime

from core.config import EXCEL_PATH, ACCOUNTS, DOWNLOAD_DIR, DEFAULT_DURATION
from core.logger import raw_info, raw_warning
from registry.manager import health_monitor_worker
from workers.poll import start_poll_threads
from console.app import interactive_loop

# 当前视频时长（秒）
current_duration = DEFAULT_DURATION


def main():
    raw_info(f"启动 {datetime.now().isoformat()}")
    
    # 汇总启动信息为极简格式
    info_parts = [
        f"Excel={EXCEL_PATH.split('/')[-1]}",
        f"账号={'/'.join([a['name'] for a in ACCOUNTS])}",
        f"下载={DOWNLOAD_DIR.split('/')[-1]}",
        f"时长={current_duration}秒"
    ]
    raw_info(" | ".join(info_parts))
    
    # 账号健康状态汇总
    health_status = []
    for a in ACCOUNTS:
        try:
            from core.api_client import health
            h = health(a["base"])
            status = "OK" if all(v == 'ready' for v in h.values()) else "FAIL"
            device = h.get('device', 'unknown').split()[0]
            health_status.append(f"{a['name']}={status} [{device}]")
        except Exception as e:
            health_status.append(f"{a['name']}=ERR")
    
    raw_info("服务：" + "; ".join(health_status))

    threading.Thread(target=health_monitor_worker, daemon=True,
                     name="health").start()
    raw_info("健康检查线程已启动")

    start_poll_threads()
    raw_info("轮询线程已启动")

    interactive_loop()


if __name__ == "__main__":
    main()
