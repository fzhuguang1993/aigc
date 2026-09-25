"""
test/seed_dashboard_demo.py —— 给「数据中台」灌一批演示数据，让六张图都有内容

覆盖到的图：
- 每日执行趋势（堆叠柱）：近 30 天，每天都有跑次
- 状态占比 / 产品分布 / 视频时长占比（环形）：状态、产品、时长都有分布
- 各线路执行分布（横条）：线路A/B/C 三条
- 今日执行节奏（折线）：★关键★ 今天往“已经过去的每一个整点”都灌了跑次，
  折线图这才有点可画——否则全是 0，看上去就像“没有折线图”

注意：折线图按设计只画“0 点 ~ 当前小时”（还没跑到的钟点画出来没意义）。
所以早上打开时线段会短一些，属于正常；下午/晚上打开能看到完整的一天曲线。

可重复执行：每次运行会先清空之前生成的 DEMO- 数据再重灌，不会越堆越多。

用法：
    python test/seed_dashboard_demo.py
然后启动界面：
    python desktop.py   →   左侧「📊 数据中台」
"""
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store import db

db.init()

random.seed(20260925)     # 固定随机种子：可重复执行时形状稳定

# 演示产品 / 线路 / 视频时长（秒）：时长刻意跨四个桶（≤5 / 6~10 / 11~15 / >15）
PRODUCTS = ["诺特兰德益生菌", "诺特兰德VB", "钙尔奇D钙片",
            "Swisse葡萄籽", "汤臣倍健鱼油", "同仁堂枸杞原浆"]
ACCOUNTS = ["线路A", "线路B", "线路C"]
DURATIONS = [5, 8, 12, 20]

# 每个整点的“当日节奏”权重（模拟真实作息：夜里少、白天高、晚间达峰）
HOUR_WEIGHT = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 1, 7: 2, 8: 4, 9: 6,
               10: 8, 11: 7, 12: 3, 13: 5, 14: 7, 15: 8, 16: 7, 17: 5,
               18: 6, 19: 8, 20: 9, 21: 7, 22: 4, 23: 1}


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _pick_status(running_allowed=False):
    """加权状态：绝大多数成功，少量失败/取消；running 仅今天最后小时点缀。"""
    r = random.random()
    if r < 0.82:
        return "completed"
    if r < 0.92:
        return "failed"
    if r < 0.97:
        return "cancelled"
    return "running" if running_allowed else "completed"


def build_task_pool():
    """造一批 DEMO 任务：每个产品 × 每种时长一条，返回 [(id, num, product, duration)]"""
    pool = []
    idx = 0
    for product in PRODUCTS:
        for dur in DURATIONS:
            idx += 1
            num = f"DEMO-{idx:02d}"
            prompt = f"{product} 口播种草：明亮的居家场景，博主手持{product}，" \
                     f"对镜头自然推荐，口型同步，画面干净，节奏轻快。"
            tid = db.execute(
                "INSERT INTO tasks(num, product, prompt, duration, status, "
                "updated_at, prompt_changed_at) VALUES(?,?,?,?,?,?,?)",
                (num, product, prompt, dur, "completed", _fmt(datetime.now()),
                 _fmt(datetime.now())))
            pool.append((tid, num, product, dur))
    return pool


def add_run(task, day_dt, status):
    """写一条执行记录：task=(id,num,product,dur)；day_dt=开始时间；回填成品/用时"""
    tid, num, product, dur = task
    account = random.choice(ACCOUNTS)
    finished = ""
    output = ""
    error = ""
    gen_sec = queued_sec = 0
    if status == "completed":
        gen_sec = random.randint(120, 480)
        queued_sec = random.randint(30, 300)
        finished = day_dt + timedelta(seconds=gen_sec + queued_sec)
        output = f"outputs/{day_dt:%m%d}/{num}_{product}_demo.mp4"
    elif status in ("failed", "cancelled"):
        finished = day_dt + timedelta(minutes=random.randint(1, 6))
        if status == "failed":
            error = "云端返回 failed：显存不足 / 触发风控"
    db.execute(
        "INSERT INTO runs(task_id,num,product,account,job_id,status,started_at,"
        "finished_at,output,error,duration,gen_sec,queued_sec)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, num, product, account, f"{num}-{int(day_dt.timestamp())}", status,
         _fmt(day_dt), _fmt(finished) if finished else "", output, error,
         dur, gen_sec, queued_sec))


def seed_past_days(pool, days=30):
    """近 days 天（不含今天）：每天随机跑一批，制造趋势起伏 + 少量空白日"""
    n = 0
    for off in range(1, days + 1):
        day = date.today() - timedelta(days=off)
        # 周末略低；偶发“停跑日”让趋势有起伏
        base = random.randint(8, 20)
        if day.weekday() >= 5:
            base = int(base * 0.5)
        if random.random() < 0.08:
            base = random.randint(0, 2)
        for _ in range(base):
            hour = random.randint(8, 22)
            minute = random.randint(0, 59)
            start = datetime(day.year, day.month, day.day, hour, minute,
                             random.randint(0, 59))
            add_run(random.choice(pool), start, _pick_status())
            n += 1
    return n


def seed_today(pool):
    """★今天：按“已过整点”的节奏权重灌跑次，折线图靠这个才有曲线★"""
    now = datetime.now()
    cur_h = now.hour
    n = 0
    for h in range(0, cur_h + 1):
        cnt = max(0, int(round(HOUR_WEIGHT[h] * random.uniform(0.5, 1.3))))
        for _ in range(cnt):
            minute = random.randint(0, 59)
            start = now.replace(hour=h, minute=minute,
                                second=random.randint(0, 59), microsecond=0)
            # 当前小时的最后几条留作“正在运行”，点亮 KPI「正在运行」
            running = (h == cur_h and random.random() < 0.25)
            status = _pick_status(running_allowed=running)
            add_run(random.choice(pool), start, status)
            n += 1
    return n


def reset():
    """清空历史 DEMO 数据（按 num 前缀识别），保证可重复执行不堆积"""
    db.execute("DELETE FROM runs WHERE num LIKE 'DEMO-%'")
    db.execute("DELETE FROM tasks WHERE num LIKE 'DEMO-%'")


def main():
    reset()
    pool = build_task_pool()
    past = seed_past_days(pool)
    today = seed_today(pool)
    print(f"已灌入演示数据：任务 {len(pool)} 条 + 执行记录 {past + today} 条")
    print(f"  · 近 30 天：{past} 条（趋势 / 环形 / 线路分布）")
    print(f"  · 今日 0~{datetime.now().hour:02d} 点：{today} 条（今日节奏折线）")
    print("\n查看：python desktop.py → 左侧「📊 数据中台」")
    print("提示：折线图只画到“当前小时”，早上打开线段偏短属正常。")


if __name__ == "__main__":
    main()
