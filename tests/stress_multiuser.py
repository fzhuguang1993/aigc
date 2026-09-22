"""
tests/stress_multiuser.py —— 多人共用同一批线路的均衡压测（排队模型，接口 mock 不联网）

模型（与一线确认）：每条线路 concurrency=1，往忙线提交 = 进入服务端队列排队
（返回 job_id，永不 429）。所以不存在"超发/打爆"，唯一目标是**均衡各线总排队数**
（1 条/生成时长 匀速消化 ⇒ 排队长度就是等待时长）。多机信息隔离，只能靠 /jobs
查全局队列来指导分配。

对比三种口径（同一份初始快照下各跑一遍 n 条）：
  gated  —— 旧的串行+在途闸门：逐条 measure_load 选最空线。因 measure_load=max(cloud,local)，
            当某线 cloud(同事的队) ≫ 本机刚投数时，本机增量被遮罩 → 会把初始最空的线反复灌穿。
  balance—— 新注水式批量：BatchBalancer 在"全局快照 + 本批投影计数"上选最空线 → 摊平。
  ideal  —— plan_allocation 对初始快照算出的理论最优分配（均衡下界）。

指标：批末各线"真实总排队数"的极差(max-min)与离散度 CoV，越小越均衡。

运行：
    python tests/stress_multiuser.py
    python tests/stress_multiuser.py --tasks 20 --loads 10,25,15,5
    python tests/stress_multiuser.py --loads 5,5,5,5 --tasks 40      # 起手全空
"""
import argparse
import logging
import statistics
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import registry.manager as rm                                   # noqa: E402
import workers.submit as submit                                 # noqa: E402
# 复用单机压测里对真实链路的打桩（DB/产品/网络边界）
from stress_submit import install_mocks, _noop_sleep            # noqa: E402
from registry.manager import BatchBalancer, plan_allocation     # noqa: E402


class MockQueueCloud:
    """每条线一条无限深队列：提交即排队（永不拒），/jobs 反映有延迟 reflect。
    同事既有负载用 seed_peer 预置（本机看不到自己之外的增量，靠 cloud 体现）。"""

    def __init__(self, gen, reflect, latency):
        self.gen = gen            # 单条占用时长秒（取大 ⇒ 压测期间不消化，纯看分配）
        self.reflect = reflect    # /jobs 反映延迟秒（取大 ⇒ 放大遮罩效应）
        self.latency = latency    # 单次提交网络耗时
        self.lock = threading.Lock()
        self.queues = defaultdict(list)   # acc -> [{id, end, visible_at, owner}]
        self._n = 0

    @staticmethod
    def _acc(base):
        return base.rsplit("/mock/", 1)[-1]

    def submit_job(self, base, payload):
        time.sleep(self.latency)
        now = time.time()
        with self.lock:
            self._n += 1
            jid = f"u{self._n}"
            self.queues[self._acc(base)].append(
                {"id": jid, "end": now + self.gen,
                 "visible_at": now + self.reflect, "owner": "us"})
            return {"job_id": jid}

    def upload_asset(self, base, file_path, asset_type="image"):
        return "aid"

    def list_jobs(self, base, limit=100):
        now = time.time()
        with self.lock:
            acc = self._acc(base)
            # 只返回"已反映"的排队条目：本机刚投的在 reflect 窗口内对查询不可见
            return [{"status": "running"} for j in self.queues[acc]
                    if j["visible_at"] <= now]

    def seed_peer(self, acc, count):
        """预置同事在这条线的既有队列（立即可见，作为初始全局负载）。"""
        now = time.time()
        with self.lock:
            for _ in range(count):
                self._n += 1
                self.queues[acc].append(
                    {"id": f"p{self._n}", "end": now + self.gen,
                     "visible_at": now, "owner": "peer"})

    def total(self, acc):
        """该线真实总排队数（含未反映的本机任务）= 分配结果。"""
        with self.lock:
            return len(self.queues[acc])


def _fresh_cloud(names, loads, args):
    """按初始快照 loads 重建账号表 + 预置同事队列，返回 cloud。"""
    cloud = MockQueueCloud(gen=args.gen, reflect=args.reflect, latency=args.latency)
    install_mocks(cloud, [(n, 1) for n in names])   # concurrency=1
    for n, ld in zip(names, loads):
        cloud.seed_peer(n, ld)
    rm.REG.tasks.clear()
    rm.invalidate_load_cache()
    return cloud


def run_gated(cloud, names, n, options):
    """旧路径：串行逐条 do_submit（走 pick_and_wait 在途闸门）。"""
    dist = defaultdict(int)
    for i in range(1, n + 1):
        jid, err, acc = submit.do_submit(i, f"p{i % 3}", f"x{i}", options,
                                         _sleep=_noop_sleep)
        if acc:
            dist[acc] += 1
    return dist


def run_balanced(cloud, names, n, options, rescan_every):
    """新路径：批量注水分配器（绕开闸门）。"""
    dist = defaultdict(int)
    balancer = BatchBalancer(n, rescan_every=rescan_every)
    for i in range(1, n + 1):
        jid, err, acc = submit.do_submit(i, f"p{i % 3}", f"x{i}", options,
                                         balancer=balancer, _sleep=_noop_sleep)
        if acc:
            dist[acc] += 1
    return dist


def _spread(totals):
    lo, hi = min(totals), max(totals)
    mean = statistics.fmean(totals) if totals else 0
    cov = (statistics.pstdev(totals) / mean * 100) if totals and mean else 0
    return hi - lo, cov


def main():
    ap = argparse.ArgumentParser(description="多人共用线路的均衡压测（排队模型）")
    ap.add_argument("--tasks", type=int, default=20, help="本机提交条数（默认 20）")
    ap.add_argument("--loads", default="10,25,15,5",
                    help="各线初始快照负载，逗号分隔（默认 10,25,15,5）")
    ap.add_argument("--gen", type=float, default=600, help="单条占用秒（默认 600=不消化）")
    ap.add_argument("--reflect", type=float, default=30, help="/jobs 反映延迟秒（默认 30）")
    ap.add_argument("--latency", type=float, default=0.0, help="单次提交网络耗时秒")
    ap.add_argument("--cache", type=float, default=5.0, help="LOAD_CACHE_TTL 秒（默认 5=真实值）")
    ap.add_argument("--rescan", type=int, default=6, help="新路径每推几条重扫快照")
    ap.add_argument("--tie", type=int, default=0, help="LOAD_TIE_BAND（0=确定性，便于对齐 ideal）")
    ap.add_argument("--gate-timeout", type=float, default=0.3, help="旧闸门等待超时秒（缩小免拖慢）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.getLogger("aigc").setLevel(logging.INFO if args.verbose else logging.CRITICAL)

    loads = [int(x) for x in args.loads.split(",") if x.strip() != ""]
    names = [f"line{i + 1}" for i in range(len(loads))]

    # 全局调度：两条路径都用同一套云端参数，只是选线口径不同
    submit.MAX_RETRY = 0
    rm.LOAD_CACHE_TTL = args.cache

    def _run(mode):
        cloud = _fresh_cloud(names, loads, args)
        options = submit.SubmitOptions(duration=5, steps=8)
        rm.LOAD_TIE_BAND = args.tie               # 0=确定性 argmin，便于与 ideal 对齐
        t0 = time.perf_counter()
        if mode == "gated":
            rm.SUBMIT_GATE = True
            rm.GATE_WAIT_TIMEOUT = args.gate_timeout
            rm.GATE_POLL_INTERVAL = 0.05
            dist = run_gated(cloud, names, args.tasks, options)
        else:
            rm.SUBMIT_GATE = False                 # 新路径不依赖闸门（balancer 绕开）
            dist = run_balanced(cloud, names, args.tasks, options, args.rescan)
        wall = time.perf_counter() - t0
        totals = [cloud.total(nm) for nm in names]
        return dist, totals, wall

    print("=" * 72)
    print(f"多人共用压测（排队模型）：本机 {args.tasks} 条 ｜ 线路 {len(names)} 条 concurrency=1")
    print(f"初始快照负载：{dict(zip(names, loads))} ｜ 合计={sum(loads)}，"
          f"投后总量={sum(loads) + args.tasks}")
    print(f"反映延迟 reflect={args.reflect}s（放大遮罩效应）｜ tie_band={args.tie}")
    print("=" * 72)

    # 理想下界（对初始静态快照注水）
    ideal_adds = plan_allocation(loads, args.tasks)
    ideal_totals = [l + a for l, a in zip(loads, ideal_adds)]
    i_spread, i_cov = _spread(ideal_totals)

    g_dist, g_totals, g_wall = _run("gated")
    b_dist, b_totals, b_wall = _run("balance")
    g_spread, g_cov = _spread(g_totals)
    b_spread, b_cov = _spread(b_totals)

    def line(mode, dist, totals, spread, cov, wall):
        dist_row = {nm: dist.get(nm, 0) for nm in names}
        print(f"  [{mode:10}] 本机分布={dist_row}")
        print(f"              各线总排队={totals} ｜ 极差={spread} ｜ CoV={cov:.0f}% ｜ "
              f"推送耗时 {wall:.2f}s")

    line("gated(旧)", g_dist, g_totals, g_spread, g_cov, g_wall)
    line("balance(新)", b_dist, b_totals, b_spread, b_cov, b_wall)
    print(f"  [{' ' * 10}ideal] 本机分布={dict(zip(names, ideal_adds))}")
    print(f"              各线总排队={ideal_totals} ｜ 极差={i_spread} ｜ CoV={i_cov:.0f}%")
    print("-" * 72)
    print("解读：极差/CoV 越小=各线排队越均衡=大家等待越接近；推送耗时=旧法卡在闸门上的代价。")
    print("     balance(新)=紧贴 ideal 且不等空位；gated(旧) 因 measure_load=max(cloud,local)")
    print("     遮罩本机增量而把初始最空的线灌穿，还在每条上白等 gate-timeout。")
    print("=" * 72)


if __name__ == "__main__":
    main()
