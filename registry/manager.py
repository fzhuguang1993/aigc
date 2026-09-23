"""
registry.py —— 任务注册 + 账号状态 + 负载均衡

负载均衡的两个关键约定（踩过坑，改之前先读完）：

1. 状态一律用「排除终态」判断（`is_active`），不枚举活跃态。云端排队态叫什么
   并不由我们决定，旧实现只认 queued/running/starting，一旦云端返回
   pending/waiting/created：负载读数恒为 0（全挤一条线）、轮询线程还会永久
   失去对该任务的跟踪。
2. 负载读数取 max(云端队列, 本机在途)。云端从提交成功到出现在 /jobs 里有几秒
   延迟，只看云端会被自己连续灌穿。
3. `AccountState.healthy` 默认 True 只表示「还没测过，先当可用」（否则刚开机的
   几十秒里一条线都不能提交），**不等于检测通过**：展示层必须先问
   `first_check_done()`，没测过就写「检测中/待检测」，不能画绿灯。
"""
import random
import time
import threading

from core.config import (ACCOUNTS as CONFIG_ACCOUNTS, LOAD_CACHE_TTL, JOBS_LIMIT,
                         LOAD_TIE_BAND, SUBMIT_GATE, GATE_WAIT_TIMEOUT,
                         GATE_POLL_INTERVAL, BALANCE_RESCAN_EVERY,
                         HEALTH_FAIL_THRESHOLD, HEALTH_CHECK_INTERVAL)
from core.api_client import list_jobs, health
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

# 首轮真实探活是否已完成：`AccountState.healthy` 默认 True 只是「还没测过就先
# 当作可用」（否则刚开机一分钟都不能提交），但它绝不该被当成检测结果展示。
# 界面在事件置位前显示「检测中」，避免用户看到一排凭默认值亮起的绿灯。
FIRST_CHECK_DONE = threading.Event()


def first_check_done():
    return FIRST_CHECK_DONE.is_set()


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


def cloud_load(acc):
    """该线云端队列数（含所有人：同事 + 已反映的本机）；不可达返回 None。
    与 measure_load 的 max(cloud, local) 不同——这里要的是“全局裸读数”，供
    BatchBalancer 扣除本机已投数后得到“非本机基线”。"""
    try:
        items = list_jobs(acc.base, limit=JOBS_LIMIT)
        return sum(1 for j in items if is_active(j.get("status")))
    except Exception:
        return None


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


def plan_allocation(load, n):
    """对静态快照做注水式分配：把 n 条任务逐条投给「当前总队列最短」的线，
    返回每条线应分到的条数（与 load 同序）。只增不减——本就过载的线自动出局。
    与 BatchBalancer 的逐步 argmin 等价，供离线规划与单元直测。

    例：plan_allocation([10, 25, 15, 5], 20) -> [7, 0, 2, 11]
        （分配后总队列 [17, 25, 17, 16]，可控制的三条被拉平，过载线不分）
    """
    adds = [0] * len(load)
    eff = list(load)
    for _ in range(max(0, n)):
        if not eff:
            break
        i = min(range(len(eff)), key=lambda k: eff[k])   # 并列取靠前索引，保证确定性
        eff[i] += 1
        adds[i] += 1
    return adds


class BatchBalancer:
    """批量提交的注水式选线器（排队模型专用）。

    为什么要它：measure_load 取 max(cloud, local)，当某线 cloud 很大（同事排的队）
    而本机刚投几条时，max 会把本机增量遮罩掉、读数纹丝不动 → 逐条 argmin 会反复
    砸同一条最空的线。破解：把“非本机基线”与“本机已投数”分开维护——
        有效负载 = base(非本机) + ours(本机本批累计)，base 只随云端上调、不被 max 抹掉。

    与在途闸门相反：这里不阻塞等空位——排队模型下往忙线提交只是排队，不该等。
    每推 rescan_every 条重扫一次，仅用于并入同事新增的全局量（上调 base）。
    """

    def __init__(self, total, rescan_every=None):
        self.total = total
        self.rescan_every = (BALANCE_RESCAN_EVERY if rescan_every is None
                             else rescan_every)
        self.base = {}         # name -> 非本机负载估计（同事+已反映），重扫时只升不降
        self.ours = {}         # name -> 本机本批累计投了几条（精确，永不重置）
        self._since_rescan = 0
        self._rescan()

    def _lines(self):
        healthy = [a for a in ACCOUNTS if a.healthy]
        if healthy:
            return healthy
        fallback = sorted(ACCOUNTS, key=lambda a: a.fail_count)[:1]
        return fallback or ACCOUNTS

    def _rescan(self):
        """重扫全局：base = max(旧 base, 云端裸读数 - 本机已投)。
        只上调不下调：/jobs 反映延迟内云端可能还没含本机刚投的，下调会误丢同事基线。"""
        invalidate_load_cache()
        for a in ACCOUNTS:
            c = cloud_load(a)
            if c is None:                 # 降级：看不见全局，保留既有 base 估计
                continue
            est = max(0, c - self.ours.get(a.name, 0))
            self.base[a.name] = max(self.base.get(a.name, est), est)
        self._since_rescan = 0

    def _effective(self, acc):
        return self.base.get(acc.name, 0) + self.ours.get(acc.name, 0)

    def pick(self):
        """选一条「基线+本机累计」最小的线（并列按 LOAD_TIE_BAND 随机），本机计数 +1。"""
        if self.rescan_every and self._since_rescan >= self.rescan_every:
            self._rescan()
        lines = self._lines()
        best = min(self._effective(a) for a in lines)
        pool = [a for a in lines if self._effective(a) <= best + LOAD_TIE_BAND]
        acc = random.choice(pool)
        self.ours[acc.name] = self.ours.get(acc.name, 0) + 1
        self._since_rescan += 1
        return acc


# 一轮检测里的尝试次数：瞬时抖动（网络闪断、云端重启）不该一次就计失败
HEALTH_ATTEMPTS = 2


def check_all_accounts():
    """逐条线路真实探活一次，更新 healthy/fail_count。

    启动时必须立即跑一遍：旧实现先睡 30 秒才首检，AccountState 默认
    healthy=True，刚开软件的绿灯全是「缓存的默认值」而非检测结果。"""
    for acc in ACCOUNTS:
        fails = 0
        for _ in range(HEALTH_ATTEMPTS):        # 瞬时抖动重试一次，两次全败才计失败
            try:
                health(acc.base)
                fails = 0
                break
            except Exception:
                fails += 1
                if fails < HEALTH_ATTEMPTS:
                    time.sleep(0.5)
        if not fails:
            if not acc.healthy:
                raw_info(f"{acc.name} 恢复健康")
            acc.healthy = True
            acc.fail_count = 0
        else:
            acc.fail_count += 1
            if acc.fail_count >= HEALTH_FAIL_THRESHOLD and acc.healthy:
                acc.healthy = False
                raw_warning(f"{acc.name} 标记为不可用（连续失败 {acc.fail_count} 次）")
    FIRST_CHECK_DONE.set()


def health_monitor_worker():
    check_all_accounts()          # 先检后睡：界面首屏就是真实状态
    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)
        check_all_accounts()
