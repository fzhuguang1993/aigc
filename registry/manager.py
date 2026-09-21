"""
registry.py —— 任务注册 + 账号状态 + 负载均衡

负载均衡的两个关键约定（踩过坑，改之前先读完）：

1. 状态一律用「排除终态」判断（`is_active`），不枚举活跃态。云端排队态叫什么
   并不由我们决定，旧实现只认 queued/running/starting，一旦云端返回
   pending/waiting/created：负载读数恒为 0（全挤一条线）、轮询线程还会永久
   失去对该任务的跟踪。
2. 负载读数取 max(云端队列, 本机在途)。云端从提交成功到出现在 /jobs 里有几秒
   延迟，只看云端会被自己连续灌穿。
"""
import random
import time
import threading

from core.config import (ACCOUNTS as CONFIG_ACCOUNTS, LOAD_CACHE_TTL, JOBS_LIMIT,
                         LOAD_TIE_BAND, SUBMIT_GATE, GATE_WAIT_TIMEOUT,
                         GATE_POLL_INTERVAL)
from core.api_client import list_jobs
from core.logger import raw_info, raw_warning

# 云端任务终态：除了这几个，其余一律视为「还在排队或还在跑」
TERMINAL_STATUSES = {"completed", "succeeded", "success", "finished",
                     "failed", "error", "cancelled", "canceled",
                     "timeout", "expired"}


def is_active(status):
    """该状态的任务是否仍占着线路额度（非终态即算在途）"""
    return str(status or "").lower() not in TERMINAL_STATUSES


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

    def get_all_by_row(self, row_idx):
        """同一任务可能并发跑多个 job（强制重跑/抽卡），返回列表而非单个"""
        with self.lock:
            return [dict(t) for t in self.tasks.values() if t["row_idx"] == row_idx]

    def count_active_by_account(self, name):
        with self.lock:
            return sum(1 for t in self.tasks.values()
                       if t["account"] == name and is_active(t["status"]))

    def active(self):
        with self.lock:
            return [dict(t) for t in self.tasks.values() if is_active(t["status"])]

    def active_by_account(self, name):
        with self.lock:
            return [dict(t) for t in self.tasks.values()
                    if t["account"] == name and is_active(t["status"])]


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


_load_cache = {}      # name -> (时间戳, 负载, 来源 cloud/local)
_load_errors = {}     # name -> 最近一次云端查询异常（只在首次告警）
_cache_lock = threading.Lock()


def measure_load(acc, force=False):
    """返回 (负载, 来源)。来源 "cloud" = 云端真实队列（含同事提交的），
    "local" = 接口不可达，降级为只能看到本机任务。降级时其他机器塞了多少
    完全看不到，这正是需要共享盘补齐全局视图的原因。"""
    now = time.time()
    if not force:
        with _cache_lock:
            hit = _load_cache.get(acc.name)
            if hit and now - hit[0] < LOAD_CACHE_TTL:
                return hit[1], hit[2]

    err = None
    try:
        items = list_jobs(acc.base, limit=JOBS_LIMIT)
        cloud = sum(1 for j in items if is_active(j.get("status")))
        # 本机刚提交、云端还没反映出来的那几条也要占位
        load = max(cloud, REG.count_active_by_account(acc.name))
        src = "cloud"
    except Exception as e:
        load = REG.count_active_by_account(acc.name)
        src = "local"
        err = f"{type(e).__name__}: {e}"

    with _cache_lock:
        _load_cache[acc.name] = (now, load, src)
        if err:
            first = acc.name not in _load_errors
            _load_errors[acc.name] = err
            if first:
                raw_warning(f"⚠ {acc.name} 云端负载接口不可用（{err}）：已降级为仅本机视角，"
                            f"看不到同事往这条线塞了多少，负载均衡会失真")
        elif _load_errors.pop(acc.name, None):
            raw_info(f"✅ {acc.name} 云端负载接口已恢复")
    return load, src


def get_account_load(acc):
    return measure_load(acc)[0]


def load_source(name):
    """给 UI 用：这条线的负载读数是全局量（cloud）还是降级值（local）"""
    with _cache_lock:
        hit = _load_cache.get(name)
        return hit[2] if hit else None


def invalidate_load_cache(name=None):
    with _cache_lock:
        if name:
            _load_cache.pop(name, None)
        else:
            _load_cache.clear()


def ranked_accounts():
    """健康线路按负载升序：[(acc, load, src)]，全不健康时退回失败次数最少的"""
    healthy = [a for a in ACCOUNTS if a.healthy]
    if not healthy:
        raw_warning("所有账号不可用，选择 fail_count 最小的")
        healthy = sorted(ACCOUNTS, key=lambda a: a.fail_count)[:1]
    return sorted(((a, *measure_load(a)) for a in healthy), key=lambda x: x[1])


def pick_account(exclude=None):
    """
    选一条线路：低负载优先，负载相近的随机挑。exclude 用于换线重试。

    随机化不是装饰：三台电脑的配置、线路顺序、决策函数完全一样，旧实现用
    min() 平局恒取 ACCOUNTS 第一条，结果是大家一起把同一条线灌到 20+，
    其余线路全程空转。
    """
    ranked = ranked_accounts()
    if exclude is not None:
        ranked = [r for r in ranked if r[0] is not exclude] or ranked
    best = ranked[0][1]
    pool = [r for r in ranked if r[1] <= best + LOAD_TIE_BAND]
    return random.choice(pool)[0]


def pick_and_wait(timeout=None, _sleep=time.sleep, _log=None):
    """选线 + 在途闸门：选一条真有空位的线，全满就等到有人空出来。

    旧实现里 concurrency=1 只包住了「提交动作」那几百毫秒，POST 一成功就释放
    信号量，从没限制过一条线同时在跑几个任务——批量 30 条几秒就能全灌进去。

    不等“自己刚选中的那一条”而是等“任何一条有空位”：并发数配得不一样时
    （acc1 并发 1 已满、acc2 并发 3 还空着），死等一条会把能干的活耗在不能干的那条上。

    返回 (acc, 等待秒数, 是否真拿到空位)。第三项为 False 时是等超时了，
    调用方仍会提交（宁超发不丢任务）并留痕。闸门可由 config.json 的
    submit_gate=false 关掉（退回旧的“选一条直接发”行为）。
    """
    if not SUBMIT_GATE:
        return pick_account(), 0, True
    timeout = GATE_WAIT_TIMEOUT if timeout is None else timeout
    started = time.time()
    notified = False
    while True:
        ranked = ranked_accounts()
        free = [r for r in ranked if r[1] < r[0].concurrency]
        if free:
            best = free[0][1]
            pool = [r for r in free if r[1] <= best + LOAD_TIE_BAND]
            return random.choice(pool)[0], int(time.time() - started), True
        if time.time() - started >= timeout:
            return ranked[0][0], int(time.time() - started), False
        if _log and not notified:
            _log("全部线路已排满（"
                 + " ".join(f"{a.name}:{ld}" for a, ld, _s in ranked) + "），等空位…")
            notified = True
        _sleep(GATE_POLL_INTERVAL)
        # 等过一轮必须重查：不然 TTL 里的旧读数会把空位晚一轮发现
        invalidate_load_cache()


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
