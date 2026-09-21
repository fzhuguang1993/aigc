"""
test/load_balance_sim.py —— 负载均衡对照仿真（可重复运行，不联网）

    python test/load_balance_sim.py

场景：三台电脑（配置完全相同）各提交 20 条任务。云端模型为 7 条线路、每条
并发 1、排队中任务的状态是 `pending`、每 RUN_SECONDS 跑完队首一个任务。

对比三种情况的线路分布：
  改前  —— 排队态被漏计 + min 平局恒取配置第一条 + 没有在途闸门
  改后  —— 排除终态口径 + 平局随机 + 在途闸门（/jobs 可用，pick_and_wait）
  改后降级 —— /jobs 不可用时只剩本机视角，验证仍然不会全挤一条

顶部三个常量可改，用来试不同规模下的分布。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import registry.manager as mgr  # noqa: E402

LINES = [f"acc{i}" for i in range(1, 8)]
CLIENTS = ["A", "B", "C"]
PER_CLIENT = 20
RUN_SECONDS = 3


class Cloud:
    """假云端：每条线一个队列，队首在跑、后面排队（状态一律 pending）"""

    def __init__(self):
        self.q = {line: [] for line in LINES}   # line -> [{"status", "owner"}]
        self.clock = 0

    def tick(self, seconds=1):
        self.clock += seconds
        if self.clock % RUN_SECONDS == 0:
            for line in LINES:
                if self.q[line]:
                    self.q[line].pop(0)         # 队首跑完，离开队列

    def list_jobs(self, base, limit=100):
        line = base.split("//")[1].split(".")[0]
        return list(self.q[line])

    def submit(self, line, owner):
        self.q[line].append({"status": "pending", "owner": owner})

    def peak(self):
        return max(len(v) for v in self.q.values())


class FakeAcc:
    def __init__(self, name):
        self.name = name
        self.base = f"http://{name}.test/api/v1"
        self.concurrency = 1
        self.healthy = True
        self.fail_count = 0


def spread(picked):
    if not sum(picked.values()):
        return "-"
    lo, hi = min(picked.values()), max(picked.values())
    return f"{lo}~{hi} 条/线（极差 {hi - lo}）"


def run_current(broken_api=False):
    """跑现在的选线 + 闸门逻辑；broken_api=True 时 /jobs 一律抛异常"""
    cloud = Cloud()
    cur = {"client": CLIENTS[0]}
    stats = {"loops": 0, "blocked": 0}
    mgr.ACCOUNTS = [FakeAcc(line) for line in LINES]
    mgr.list_jobs = (
        lambda base, limit=100: (_ for _ in ()).throw(RuntimeError("HTTP 404"))
        if broken_api else cloud.list_jobs)
    # 每台电脑只看得见自己提交的任务（跨机共享视图要靠 NAS，尚未实现）
    mgr.REG.count_active_by_account = \
        lambda name: sum(1 for j in cloud.q[name] if j["owner"] == cur["client"])

    def fake_sleep(_seconds):
        cloud.tick(1)
        mgr.invalidate_load_cache()             # 等待期间要能看到云端变化
        stats["loops"] += 1

    picked = {line: 0 for line in LINES}
    for _step in range(PER_CLIENT):
        for client in CLIENTS:                  # 三台电脑交替提交
            cur["client"] = client
            mgr.invalidate_load_cache()
            before = stats["loops"]
            acc, _waited, _got = mgr.pick_and_wait(timeout=600, _sleep=fake_sleep)
            stats["blocked"] += 1 if stats["loops"] > before else 0
            picked[acc.name] += 1
            cloud.submit(acc.name, client)
    return picked, cloud.peak(), stats


def run_legacy():
    """旧算法复刻：pending 不计负载 + min 平局恒取配置第一条 + 没有闸门"""
    cloud = Cloud()
    picked = {line: 0 for line in LINES}
    for _ in range(PER_CLIENT * len(CLIENTS)):
        loads = {line: sum(1 for j in cloud.q[line]
                           if j["status"] in ("queued", "running", "starting"))
                 for line in LINES}
        line = min(loads, key=lambda k: loads[k])
        picked[line] += 1
        cloud.submit(line, "A")
        cloud.tick(0.2)                         # 提交很快，边提交边跑
    return picked, cloud.peak()


def show(title, picked, peak, stats=None):
    print(f"\n{title}")
    for line in LINES:
        print(f"  {line}: {'█' * picked[line]}{picked[line]}")
    text = f"  分布 {spread(picked)} · 单条线最大堆积 {peak} 条"
    if stats:
        total = PER_CLIENT * len(CLIENTS)
        text += (f" · 被闸门拦住并等待的提交 {stats['blocked']}/{total} 次"
                 f"（共轮询 {stats['loops']} 轮）")
    print(text)


if __name__ == "__main__":
    total = PER_CLIENT * len(CLIENTS)
    print(f"三台电脑各提交 {PER_CLIENT} 条，共 {total} 条任务；"
          f"一条线并发 1、单任务 {RUN_SECONDS} 秒")
    show("【改前】/jobs 可用，但排队态 pending 被漏计：", *run_legacy())
    show("【改后】/jobs 可用：pending 计入 + 平局随机 + 在途闸门：",
         *run_current(broken_api=False))
    show("【改后】/jobs 不可用：降级为仅本机视角，仍不会全挤一条：",
         *run_current(broken_api=True))
    print(f"\n参考：一条线并发 1、单任务 {RUN_SECONDS} 秒时，{total} 条任务"
          f"最快也要 {total * RUN_SECONDS / len(LINES):.0f} 秒，等待是必然的")
