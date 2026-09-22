"""
tests/sim_shared_lines.py —— 多设备共享线路的负载均衡仿真（离散事件，不联网/不真等）

场景（与需求一致）：多台设备信息互不可见、共享固定数量线路；线路 concurrency=1、
多余任务在云端排队（排队模型）。用真实的 do_submit 会真等 500 秒跑不完，故这里把
调度要素建成事件仿真，只测"选线策略"对最终负载均衡的影响。

建模要素：
  - lines 条线路，每条并发 1，服务时长 ~ service（均匀抖动）；
  - tasks 条任务在 window 秒内随机到达（稳态流），每条归属一台 device（信息隔离）；
  - 设备选线时只能看 /jobs：某线的"可见在途" = 真实在途里 arrival+reflect<=now 的
    那部分（reflect = 反映延迟），且本地还有 cache 秒 TTL 的旧读数（不重扫）；
  - 三种策略对比：
      oracle  —— 完美信息（reflect=0,cache=0）的最短队列贪心，均衡上界；
      old     —— 旧贪心：逐条按"可见负载"argmin（受 reflect+cache 遮罩、多设备同刻会撞）；
      new     —— 新注水：base(非本机) + ours(本机累计)，等价线上 BatchBalancer。

指标：各线分到的任务数 / 批末积压 / 排队等待，跨线离散度 CoV（越小越均衡）。

运行：
    python tests/sim_shared_lines.py                 # 默认：10线/1万条/8h/500s
    python tests/sim_shared_lines.py --tasks 100000  # 加量看极端
    python tests/sim_shared_lines.py --cache 0 --reflect 0   # 无遮罩，三者应收敛
"""
import argparse
import math
import random
from bisect import bisect_right


class Line:
    """一条并发=1、可无限排队的线路。到达/可见/完成时间三列均随事件推进保持有序。"""
    __slots__ = ("arrivals", "visibles", "ends", "free_at", "durs")

    def __init__(self):
        self.arrivals = []     # 提交时刻（有序）
        self.visibles = []     # 对 /jobs 可见的时刻 = arrival+reflect（有序）
        self.ends = []         # 完成时刻（排队尾单调，故有序）
        self.free_at = 0.0     # 队列尾：全部排完的时刻
        self.durs = []

    def assign(self, now, dur, reflect):
        start = max(self.free_at, now)          # 前一条排完才轮到（concurrency=1）
        end = start + dur
        self.free_at = end
        self.arrivals.append(now)
        self.visibles.append(now + reflect)
        self.ends.append(end)
        self.durs.append(dur)
        return start, end

    def visible_in_system(self, t):
        """/jobs 此刻能看到的在途数：已可见且尚未完成。"""
        return bisect_right(self.visibles, t) - bisect_right(self.ends, t)

    def backlog(self, t):
        """真实在途（含未反映的），= 已到达 - 已完成。"""
        return bisect_right(self.arrivals, t) - bisect_right(self.ends, t)


def _cov(xs):
    if not xs:
        return 0.0
    m = sum(xs) / len(xs)
    if m == 0:
        return 0.0
    var = sum((x - m) ** 2 for x in xs) / len(xs)
    return math.sqrt(var) / m * 100


def simulate(lines_n, jobs, reflect, cache, strategy):
    """按到达时间顺序处理，返回 (每线分到数, 每线批末积压, 排队等待列表)。"""
    lines = [Line() for _ in range(lines_n)]
    n_dev = max((d for _, _, _, d in jobs), default=0) + 1
    # 每设备一套选线状态（信息隔离：设备之间不共享计数，只共享云端 /jobs）
    cache_state = [dict() for _ in range(n_dev)]        # old: line->(t,load)
    base = [dict() for _ in range(n_dev)]               # new: line->非本机基线
    ours = [dict() for _ in range(n_dev)]               # new: line->本机累计
    since = [0] * n_dev
    end = jobs[-1][0] if jobs else 0.0
    maxt = max(end, max((a for a, _, _, _ in jobs), default=0.0))

    for t, dur, _j, dev in jobs:                          # jobs 已按 t 升序
        if strategy == "oracle":
            load = [ln.backlog(t) for ln in lines]        # 完美信息=真实在途
            pick = _pick_min(load, lines_n)
        elif strategy == "old":
            load = []
            for i, ln in enumerate(lines):
                hit = cache_state[dev].get(i)
                if hit and t - hit[0] < cache:
                    load.append(hit[1])                   # 用旧读数（不重扫）
                else:
                    v = ln.visible_in_system(t)
                    cache_state[dev][i] = (t, v)
                    load.append(v)
            pick = _pick_min(load, lines_n)
        else:  # new：base + ours，rescan_every 用固定值
            if since[dev] >= 6:
                for i, ln in enumerate(lines):
                    est = max(0, ln.visible_in_system(t) - ours[dev].get(i, 0))
                    base[dev][i] = max(base[dev].get(i, est), est)
                since[dev] = 0
            eff = [base[dev].get(i, 0) + ours[dev].get(i, 0) for i in range(lines_n)]
            pick = _pick_min(eff, lines_n)
            ours[dev][pick] = ours[dev].get(pick, 0) + 1
            since[dev] += 1
        start, e = lines[pick].assign(t, dur, reflect)
        _ = start - t                                     # 排队等待（可在此统计）

    assigned = [len(ln.arrivals) for ln in lines]
    backlog = [ln.backlog(maxt + 1) for ln in lines]      # 仿真末刻各线积压
    return assigned, backlog


def _pick_min(vals, n):
    best = min(vals)
    pool = [i for i in range(n) if vals[i] == best]       # 并列随机（多机去相关）
    return random.choice(pool)


def _report(name, assigned, backlog, tasks, lines_n):
    print(f"  [{name:7}] 每线分到数={assigned}")
    print(f"           均衡度 CoV(分到的数)={_cov(assigned):.1f}%  ｜ "
          f"批末积压CoV={_cov(backlog):.1f}%  ｜ 末刻总积压={sum(backlog)}")


def main():
    ap = argparse.ArgumentParser(description="多设备共享线路负载均衡仿真")
    ap.add_argument("--lines", type=int, default=10)
    ap.add_argument("--tasks", type=int, default=10000)
    ap.add_argument("--window", type=float, default=8 * 3600, help="到达时间窗秒（默认8h）")
    ap.add_argument("--service", type=float, default=500, help="单条服务时长秒")
    ap.add_argument("--jitter", type=float, default=0.1, help="服务时长相对抖动 ±比例")
    ap.add_argument("--devices", type=int, default=10, help="并发提交的设备数")
    ap.add_argument("--reflect", type=float, default=3, help="/jobs 反映延迟秒")
    ap.add_argument("--cache", type=float, default=5, help="设备本地负载缓存TTL秒")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    random.seed(args.seed)

    # 生成任务：到达时刻均匀散布在窗口内；服务时长带抖动；随机归属设备
    jobs = []
    for _ in range(args.tasks):
        t = rng.uniform(0, args.window)
        dur = args.service * rng.uniform(1 - args.jitter, 1 + args.jitter)
        dev = rng.randrange(args.devices)
        jobs.append((t, dur, len(jobs), dev))
    jobs.sort(key=lambda x: x[0])

    # 可行性硬账：总服务需求 vs 总供给
    total_work = sum(j[1] for j in jobs)
    capacity = args.lines * args.window                        # 线·秒
    lam = args.tasks / args.window
    mu = args.lines / args.service                             # 系统每秒可完成条数
    rho = lam / mu

    def _hr(sec):
        return f"{sec / 3600:.1f}h" if sec >= 3600 else f"{sec / 60:.1f}min"

    print("=" * 74)
    print(f"仿真：{args.lines} 条线 concurrency=1 ｜ {args.tasks} 条任务 "
          f"{_hr(args.window)} 内随机到达 ｜ 单条≈{args.service:.0f}s")
    print(f"多设备：{args.devices} 台，信息隔离，只能靠 /jobs（反映延迟 {args.reflect}s，"
          f"本地缓存 {args.cache}s）")
    print("-" * 74)
    print(f"可行性：需要 {total_work:,.0f} 线·秒，窗口内只有 {capacity:,.0f} 线·秒供给 → "
          f"缺口 {total_work - capacity:,.0f}（利用率 ρ={rho:.1f}）")
    print(f"        窗口内能完成 ≈ {capacity / args.service:.0f} 条，"
          f"其余 {max(0, args.tasks - capacity / args.service):.0f} 条积压到窗口之后")
    print("=" * 74)

    for name, strat in (("oracle", "oracle"), ("old", "old"), ("new", "new")):
        assigned, backlog = simulate(args.lines, jobs, args.reflect, args.cache, strat)
        _report(name, assigned, backlog, args.tasks, args.lines)

    print("-" * 74)
    print("解读：CoV 越小=各线越均衡。稳态随机到达下三者通常都能拉平（旧贪心也不差）；")
    print("     把 --cache/--reflect 调大、或让任务集中在短时间到达（突发提交），")
    print("     old 的遮罩失衡才会显式暴露、由 new 兜住。")
    print("=" * 74)


if __name__ == "__main__":
    main()
