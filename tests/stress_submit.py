"""
tests/stress_submit.py —— 提交链路压测（接口全 mock，不联网）

目的：一线员工反馈"像单线程"。本脚本驱动**真实的 workers.submit.do_submit**，
只把两处边界打桩：
  1) HTTP 网络层（submit_job / upload_asset）→ 换成带延迟、可失败、记录峰值并发的 mock；
  2) 数据库/落库副作用（task_store / product_store / ensure_script_fields / mark_submitted）
     → 换成计数用的假实现，避免压测污染 SQLite 也不依赖真实产品。
这样"选线 / 信号量 / 在途闸门 / 退避重试"这些真实调度逻辑原样参与，测出来的
并发表现才有说服力。

对比两种提交方式（同样 1000 条）：
  serial —— 复刻现状 gui/pages_tasks.SubmitWorker.run()：for 循环一条条串行提交；
  pool   —— 线程池并发提交（ proposed ）。
核心指标是「峰值网络并发」：串行=1，线程池=min(pool, Σ并发)。

运行：
    python tests/stress_submit.py                 # 默认 1000 条，两种模式对比
    python tests/stress_submit.py --tasks 1000 --pool 16 --latency 0.05
    python tests/stress_submit.py --gate          # 打开在途闸门，模拟云端在途上限
"""
import argparse
import logging
import random
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 允许 `python tests/stress_submit.py` 直接运行（把工程根加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import registry.manager as rm                      # noqa: E402
import workers.submit as submit                    # noqa: E402
from core.api_client import ApiError               # noqa: E402
from core import config as cfg                      # noqa: E402


# ---------------------------------------------------------------- 打桩：DB / 产品
class FakeTaskStore:
    """只计数，不碰 SQLite。字段名/签名对齐 do_submit 里用到的调用。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.updated = 0
        self.run_started = 0

    def get_task(self, row_idx):
        return {"runs": 0, "num": f"N{row_idx}"}

    def update_row(self, task_id, **fields):
        with self.lock:
            self.updated += 1

    def record_run_start(self, *a, **k):
        with self.lock:
            self.run_started += 1

    def bump(self, *a, **k):
        pass


class FakeProductStore:
    def images_for_product(self, product):
        return []                       # 未建产品：无参考图（真实默认也是空）

    def kol_image(self, kol):
        return None


# ------------------------------------------------------- 打桩：HTTP 网络层（mock 云）
class MockCloud:
    """模拟云端：submit_job 带延迟/随机失败，并记录"同时在飞的请求数"峰值。
    可选后台"完成器"让在途任务到期终态，供在途闸门释放槽位（--gate 用）。"""

    def __init__(self, latency=0.05, jitter=0.02, fail_rate=0.0,
                 gen_s=3.0, enable_completer=False, complete_poll=0.05):
        self.latency = latency
        self.jitter = jitter
        self.fail_rate = fail_rate
        self.gen_s = gen_s
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.calls = 0
        self.fail4xx = 0
        self.fail5xx = 0
        self.by_account = defaultdict(int)
        self._active = {}               # job_id -> 提交时刻（供完成器）
        self.completed = 0
        self._enable = enable_completer
        self._complete_poll = complete_poll
        self._stop = threading.Event()
        self._thread = None

    # 供 submit_job 解析账号名（base 里带 /mock/{name}）
    def _acc_of(self, base):
        return base.rsplit("/mock/", 1)[-1]

    def submit_job(self, base, payload):
        with self.lock:
            self.calls += 1
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        try:
            time.sleep(max(0.0, self.latency + random.uniform(-self.jitter, self.jitter)))
            r = random.random()
            if r < self.fail_rate / 2:
                with self.lock:
                    self.fail5xx += 1
                raise ApiError("HTTP 500: mock server error", 500)   # 可重试→换线
            if r < self.fail_rate:
                with self.lock:
                    self.fail4xx += 1
                raise ApiError('HTTP 422: {"detail":[{"type":"extra_forbidden",'
                               '"loc":["body","parameters","steps"]}]}', 422)  # 不换线
            job_id = f"job{self.calls}"
            acc = self._acc_of(base)
            with self.lock:
                self.by_account[acc] += 1
                if self._enable:
                    self._active[job_id] = time.time()
            return job_id          # submit_job 契约：直接返回 job_id 字符串
        finally:
            with self.lock:
                self.inflight -= 1

    def upload_asset(self, base, file_path, asset_type="image"):
        time.sleep(0.01)
        return "aid_mock"

    def list_jobs(self, base, limit=100):
        # 云端"看不见"本机刚提交的（返回空）→ measure_load 回退到本机在途计数
        return []

    # ---- 完成器：模拟"生成结束"，把到期的在途任务从 REG 移除以释放闸门槽位
    def start(self):
        if self._enable:
            self._stop.clear()           # 关键：上一轮 stop() 置位了共享 Event，不清除会让本轮完成器秒退
            self._thread = threading.Thread(target=self._completer, daemon=True)
            self._thread.start()

    def reset(self):
        """每轮测前复位：清在途/完成计数（peak/calls 由 measure 清）。"""
        with self.lock:
            self._active.clear()
            self.completed = 0
            self.by_account.clear()
            self.fail5xx = 0
            self.fail4xx = 0

    def stop(self):
        self._stop.set()

    def _completer(self):
        while not self._stop.is_set():
            now = time.time()
            due = []
            with self.lock:
                for jid, t0 in list(self._active.items()):
                    if now - t0 >= self.gen_s:
                        due.append(jid)
                        self._active.pop(jid, None)
                        self.completed += 1
            for jid in due:
                rm.REG.update(jid, "completed", 100)
                rm.REG.remove(jid)
            self._stop.wait(self._complete_poll)


# ------------------------------------------------------------------ 环境装配
def install_mocks(cloud, accounts_spec):
    """把真实 do_submit 依赖的边界换成 mock，并按 accounts_spec 重建线路表。"""
    submit.task_store = FakeTaskStore()
    submit.product_store = FakeProductStore()
    submit.ensure_script_fields = lambda *a, **k: None
    submit.mark_submitted = lambda *a, **k: None
    submit.submit_job = cloud.submit_job
    submit.upload_asset = cloud.upload_asset

    # 重建账号（AccountState 会各自建 Semaphore(concurrency)）
    rm.ACCOUNTS = [rm.AccountState({"name": n, "base": f"http://mock/{n}",
                                     "concurrency": c})
                   for n, c in accounts_spec]
    rm.list_jobs = cloud.list_jobs
    rm.invalidate_load_cache()


def _noop_sleep(_s):
    """注入给 do_submit 的退避 sleep，压测里不让它真等（否则退避 2/4/…s 拖慢）。"""
    return None


# ------------------------------------------------------------------ 两种提交方式
def run_serial(n, options, jitter):
    """复刻 SubmitWorker.run()：串行，一条之间 sleep(uniform(*jitter))。"""
    items = [(i, f"product_{i % 3}", f"prompt_{i}") for i in range(1, n + 1)]
    ok = fail = 0
    dist = defaultdict(int)
    t0 = time.perf_counter()
    for i, (tid, product, prompt) in enumerate(items):
        if i and jitter:
            time.sleep(random.uniform(*jitter))
        jid, err, acc = submit.do_submit(tid, product, prompt, options, _sleep=_noop_sleep)
        if jid:
            ok += 1
            dist[acc] += 1
        else:
            fail += 1
    return {"mode": "serial", "ok": ok, "fail": fail, "dist": dict(dist),
            "wall": time.perf_counter() - t0}


def run_pool(n, options, pool, jitter):
    """线程池并发提交：每条任务丢进池里，do_submit 内部的信号量天然限流。"""
    items = [(i, f"product_{i % 3}", f"prompt_{i}") for i in range(1, n + 1)]
    lock = threading.Lock()
    ok = fail = 0
    dist = defaultdict(int)

    def one(item):
        nonlocal ok, fail
        tid, product, prompt = item
        if jitter:
            time.sleep(random.uniform(*jitter))
        jid, err, acc = submit.do_submit(tid, product, prompt, options, _sleep=_noop_sleep)
        with lock:
            if jid:
                ok += 1
                dist[acc] += 1
            else:
                fail += 1

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=pool) as ex:
        list(ex.map(one, items))
    return {"mode": "pool", "ok": ok, "fail": fail, "dist": dict(dist),
            "wall": time.perf_counter() - t0}


# ------------------------------------------------------------------ 单模式一次跑
def measure(mode, n, options, pool, jitter, cloud, accounts_spec, gate, gen_s):
    cloud.peak = 0
    cloud.calls = 0
    cloud.reset()
    rm.REG.tasks.clear()
    rm.invalidate_load_cache()
    cloud.start()
    try:
        rep = run_pool(n, options, pool, jitter) if mode == "pool" \
            else run_serial(n, options, jitter)
    finally:
        cloud.stop()
        if gate:
            time.sleep(0.2)                 # 让完成器收尾
            rm.REG.tasks.clear()
    cap = sum(c for _, c in accounts_spec)
    rep["peak"] = cloud.peak
    rep["http_calls"] = cloud.calls
    rep["fail5xx"] = cloud.fail5xx
    rep["fail4xx"] = cloud.fail4xx
    rep["throughput"] = round(rep["ok"] / rep["wall"], 1) if rep["wall"] else 0
    rep["cap"] = cap
    return rep


def _print(rep, gate, gen_s, jitter_range):
    n = rep["ok"] + rep["fail"]
    print(f"\n[{rep['mode'].upper():6}] 峰值网络并发={rep['peak']:>3}  "
          f"（账号并发上限Σ={rep['cap']}）")
    print(f"        用时 {rep['wall']:.2f}s ｜ 吞吐 {rep['throughput']} 条/s ｜ "
          f"成功 {rep['ok']}/{n} ｜ 失败 {rep['fail']}（5xx {rep['fail5xx']} / 4xx {rep['fail4xx']}）")
    print(f"        分账号：{rep['dist']}")
    if gate:
        print(f"        （在途闸门开：云端每线在途≤并发，生成模拟 {gen_s}s/条 → "
              f"最终耗时受账号×并发×生成时长约束）")
    if jitter_range:
        avg = sum(jitter_range) / 2
        proj = avg * n
        print(f"        ⚠ 生产抖动 {jitter_range[0]}~{jitter_range[1]}s/条：串行≈ +{proj:.0f}s"
              f"（{proj/60:.1f} 分钟），且抖动期间几乎零并发")


def main():
    ap = argparse.ArgumentParser(description="提交链路压测（mock 接口）")
    ap.add_argument("--tasks", type=int, default=1000, help="任务条数（默认 1000）")
    ap.add_argument("--pool", type=int, default=16, help="线程池大小（默认 16）")
    ap.add_argument("--accounts", type=int, default=3, help="mock 账号数（默认 3）")
    ap.add_argument("--concurrency", type=int, default=3, help="每账号并发上限（默认 3）")
    ap.add_argument("--conc-list", default="",
                    help="异构并发，逗号分隔（如 1,3,5）；给了它就忽略 --accounts/--concurrency")
    ap.add_argument("--latency", type=float, default=0.05, help="单次提交 HTTP 延迟秒（默认 0.05）")
    ap.add_argument("--fail-rate", type=float, default=0.0, help="随机失败率 0~1（默认 0）")
    ap.add_argument("--gate", action="store_true", help="打开在途闸门（模拟云端在途上限）")
    ap.add_argument("--gen-time", type=float, default=3.0, help="闸门模式：单条生成时长秒（默认 3）")
    ap.add_argument("--jitter", action="store_true",
                    help="是否应用生产 SUBMIT_JITTER（串行会因此非常慢，慎用大 tasks）")
    ap.add_argument("-v", "--verbose", action="store_true", help="保留提交链路的逐条日志")
    args = ap.parse_args()

    # 默认静音提交链路的逐条 INFO 日志（否则上千条刷屏看不到结论）
    logging.getLogger("aigc").setLevel(logging.INFO if args.verbose else logging.ERROR)

    # 生产 SUBMIT_JITTER 默认不真跑（1000 条要 13 分钟），仅在报告里做投影
    jitter_range = tuple(cfg.SUBMIT_JITTER) if args.jitter else 0

    # 全局调度参数按模式调整（只影响本次进程内的 rm 模块全局）
    rm.SUBMIT_GATE = bool(args.gate)
    rm.LOAD_CACHE_TTL = 0.2              # 压测用亚秒级缓存：既实时又避免高频重扫 REG 全表
    if args.gate:
        rm.LOAD_CACHE_TTL = 0.0          # 闸门下实时读数，看两模式是否向"云端在途上限"收敛
        rm.GATE_POLL_INTERVAL = 0.2      # 真实值 5s；压测缩到 0.2s
        rm.GATE_WAIT_TIMEOUT = 30

    if args.conc_list:
        concs = [int(x) for x in args.conc_list.split(",") if x.strip()]
        accounts_spec = [(f"acc{i + 1}", c) for i, c in enumerate(concs)]
    else:
        accounts_spec = [(f"acc{i + 1}", args.concurrency) for i in range(args.accounts)]
    spec_note = "×".join(f"{n}:{c}" for n, c in accounts_spec)
    cloud = MockCloud(latency=args.latency, fail_rate=args.fail_rate,
                      gen_s=args.gen_time, enable_completer=args.gate,
                      complete_poll=0.2)
    install_mocks(cloud, accounts_spec)
    options = submit.SubmitOptions(duration=5, steps=8)

    print("=" * 68)
    print(f"压测：{args.tasks} 条 ｜ mock 账号[{spec_note}]"
          f" ｜ 单次提交延迟 {args.latency}s ｜ 失败率 {args.fail_rate}"
          f" ｜ 闸门 {'开' if args.gate else '关'}")
    print("=" * 68)

    s = measure("serial", args.tasks, options, args.pool, jitter_range, cloud,
                accounts_spec, args.gate, args.gen_time)
    _print(s, args.gate, args.gen_time, jitter_range)

    p = measure("pool", args.tasks, options, args.pool, jitter_range, cloud,
                accounts_spec, args.gate, args.gen_time)
    _print(p, args.gate, args.gen_time, jitter_range)

    print("\n" + "-" * 68)
    speedup = (s["wall"] / p["wall"]) if p["wall"] else 0
    print(f"结论：线程池相对串行 提速 ≈ {speedup:.1f}x；"
          f"峰值网络并发 串行 {s['peak']} → 池 {p['peak']}（受 Σ并发={p['cap']} 与池={args.pool} 共同封顶）")
    print("说明：当前 GUI 批量提交走的是 serial 路径（SubmitWorker 里 for 循环 + "
          "SUBMIT_JITTER），所以一线体感是单线程。")
    if args.gate:
        print("⚠ 闸门模式提醒：在途闸门 pick_and_wait 是“先查后做”的非原子预留，"
              "并发提交会在同一缓存窗口一起判定有空位而超发（故池吞吐可＞Σ并发）。")
        print("   账号信号量(sem)是原子的、能限住“瞬时提交并发”，限不住“同时生成的 job 数”；"
              "真要把提交改成并发，闸门需换成带预留的信号量式。")
    print("=" * 68)


if __name__ == "__main__":
    main()
