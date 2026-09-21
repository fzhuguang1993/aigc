"""
registry.py —— 任务注册 + 账号状态 + 负载均衡
"""
import time
import threading

from core.config import ACCOUNTS as CONFIG_ACCOUNTS, LOAD_CACHE_TTL, JOBS_LIMIT
from core.api_client import list_jobs
from core.logger import raw_info, raw_warning


class Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.tasks = {}

    def add(self, job_id, row_idx, account, prompt, product, duration=None):
        with self.lock:
            self.tasks[job_id] = {
                "job_id": job_id, "row_idx": row_idx,
                "account": account, "prompt": prompt, "product": product,
                "duration": duration,
                "status": "queued", "progress": 0,
            }

    def update(self, job_id, status, progress):
        with self.lock:
            if job_id in self.tasks:
                self.tasks[job_id]["status"] = status
                self.tasks[job_id]["progress"] = progress

    def remove(self, job_id):
        with self.lock:
            self.tasks.pop(job_id, None)

    def get_by_row(self, row_idx):
        with self.lock:
            for t in self.tasks.values():
                if t["row_idx"] == row_idx:
                    return dict(t)
        return None

    def count_active_by_account(self, name):
        with self.lock:
            return sum(1 for t in self.tasks.values()
                       if t["account"] == name
                       and t["status"] in {"queued", "running", "starting"})

    def active(self):
        with self.lock:
            return [dict(t) for t in self.tasks.values()
                    if t["status"] in {"queued", "running", "starting", "cancelling"}]

    def active_by_account(self, name):
        with self.lock:
            return [dict(t) for t in self.tasks.values()
                    if t["account"] == name
                    and t["status"] in {"queued", "running", "starting", "cancelling"}]


REG = Registry()


class AccountState:
    def __init__(self, cfg):
        self.name = cfg["name"]
        self.base = cfg["base"]
        self.concurrency = cfg["concurrency"]
        self.sem = threading.Semaphore(cfg["concurrency"])
        self.healthy = True
        self.fail_count = 0

ACCOUNTS = [AccountState(c) for c in CONFIG_ACCOUNTS]


def get_account(name):
    for a in ACCOUNTS:
        if a.name == name:
            return a
    return None


_load_cache = {}
_cache_lock = threading.Lock()


def get_account_load(acc):
    now = time.time()
    with _cache_lock:
        if acc.name in _load_cache:
            ts, load = _load_cache[acc.name]
            if now - ts < LOAD_CACHE_TTL:
                return load

    try:
        items = list_jobs(acc.base, limit=JOBS_LIMIT)
        active = [j for j in items
                  if j.get("status") in ("queued", "running", "starting")]
        load = len(active)
    except Exception:
        load = REG.count_active_by_account(acc.name)

    with _cache_lock:
        _load_cache[acc.name] = (now, load)
    return load


def invalidate_load_cache(name=None):
    with _cache_lock:
        if name:
            _load_cache.pop(name, None)
        else:
            _load_cache.clear()


def pick_account():
    healthy = [a for a in ACCOUNTS if a.healthy]
    if not healthy:
        raw_warning("所有账号不可用，选择 fail_count 最小的")
        healthy = sorted(ACCOUNTS, key=lambda a: a.fail_count)[:1]
    return min(healthy, key=get_account_load)


def health_monitor_worker():
    while True:
        for acc in ACCOUNTS:
            try:
                from core.api_client import health
                health(acc.base)
                if not acc.healthy:
                    raw_info(f"{acc.name} 恢复健康")
                acc.healthy = True
                acc.fail_count = 0
            except Exception as e:
                acc.fail_count += 1
                if acc.fail_count >= 3 and acc.healthy:
                    acc.healthy = False
                    raw_warning(f"{acc.name} 标记为不可用（连续失败 {acc.fail_count} 次）")
        time.sleep(30)
