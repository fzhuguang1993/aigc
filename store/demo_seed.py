"""
store/demo_seed.py —— 任务中心/数据中台的看板演示数据（一次性播种 + 一次性清除）

为什么需要：新用户拿到打包 exe，任务中心与数据中台空空如也，看板/图表看不出名堂。
首次运行按样例铺一批「看起来在跑」的任务与执行记录，把 KPI、逐日曲线、线路/产品分布、
时长占比、失败原因等都填出形状，用户一眼看懂各屏在讲什么。

安全边界（关键）：
- 演示任务一律 demo=1，真实任务恒为 demo=0，两套数据用这一列干净切开；
- 只在「没有任何真实任务」时才播种——开发机/老用户库里已有真实数据，直接跳过，绝不污染；
- 已播过（库里已有 demo=1）也不重复播；
- 「清除演示数据」只删 demo=1 的 tasks 及其名下的 runs，绝不碰真实数据。
"""
import random
from datetime import datetime, timedelta

from store import db

# 品名/标签/提示词样例：够杂，产品分布图、标签筛选、时长分桶才好看
_PRODUCTS = ["诺特兰德益生菌", "抖音同款保温杯", "便携榨汁杯", "控油洗面奶",
             "儿童学习桌", "无线蓝牙耳机", "除螨喷雾", "衣物收纳箱",
             "筋膜枪", "维C泡腾片", "护眼台灯", "懒人拖把"]
_TAGS = ["开场钩子", "活动促销", "情景剧", "产品展示", "口播种草", "痛点引入", ""]
_ACCOUNTS = ["线路A", "线路B", "线路C", "线路D"]
# 状态配比（越靠前越多）：完成为主，掺进行中/失败/待执行，看板才有起伏
_STATUSES = (["completed"] * 10 + ["failed"] * 3 + ["running"] * 3
             + ["pending"] * 5)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _has_real_tasks():
    r = db.query("SELECT COUNT(*) c FROM tasks WHERE COALESCE(demo,0)=0")
    return (r[0]["c"] if r else 0) > 0


def _has_demo():
    r = db.query("SELECT COUNT(*) c FROM tasks WHERE COALESCE(demo,0)=1")
    return (r[0]["c"] if r else 0) > 0


def demo_present():
    """当前是否存在演示数据（供「清除演示数据」按钮决定是否显示）。"""
    return _has_demo()


def seed_demo(count=40):
    """铺一批演示任务 + 对应执行记录，仅当库中无真实任务、且尚未播过时执行。

    返回本次插入的任务条数；不满足播种条件返回 0。"""
    if _has_real_tasks() or _has_demo():
        return 0
    now = datetime.now()
    inserted = 0
    for i in range(count):
        product = random.choice(_PRODUCTS)
        status = random.choice(_STATUSES)
        account = random.choice(_ACCOUNTS)
        tag = random.choice(_TAGS)
        vdur = random.choice([5, 6, 8, 10, 12, 15])
        # 近 14 天内散布，逐日曲线与环比才有内容
        started = now - timedelta(days=random.randint(0, 13),
                                  hours=random.randint(9, 21),
                                  minutes=random.randint(0, 59))
        finished = started + timedelta(seconds=random.randint(60, 600))
        runs = 1 if status != "pending" else 0
        success = 1 if status == "completed" else 0
        task_updated = finished.strftime("%Y-%m-%d %H:%M:%S") if runs else \
            started.strftime("%Y-%m-%d %H:%M:%S")
        tid = db.execute(
            "INSERT INTO tasks(num, product, prompt, status, account, job_id, "
            "output, url, runs, success, cancels, script_text, updated_at, "
            "duration, script, storyboard, remark, tag, prompt_zh, demo) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (f"D{i + 1:03d}", product,
             f"{product}：明亮场景手持产品，对镜头口播卖点，字幕同步，产品logo特写",
             ("" if status == "pending" else status), account,
             (f"demo-job-{i + 1}" if runs else ""),
             (rf"outputs\demo\{product}_{i + 1}.mp4" if status == "completed" else ""),
             "", runs, success, 0, "每天一条，坚持见效", task_updated,
             vdur, product, random.randint(0, 6),
             random.choice(["已过审", "待重拍", "", ""]), tag, "", ))
        inserted += 1
        # 非待执行的补一条 runs（看板 range_stats 读的是 runs 表）
        if runs:
            gen = random.randint(30, 200)
            queued = random.randint(5, 120)
            db.execute(
                "INSERT INTO runs(task_id, num, product, account, job_id, status, "
                "started_at, finished_at, output, error, duration, gen_sec, queued_sec) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, f"D{i + 1:03d}", product, account, f"demo-job-{i + 1}",
                 status,
                 started.strftime("%Y-%m-%d %H:%M:%S.%f"),
                 (finished.strftime("%Y-%m-%d %H:%M:%S.%f")
                  if status in ("completed", "failed") else None),
                 (rf"outputs\demo\{product}_{i + 1}.mp4"
                  if status == "completed" else ""),
                 (random.choice(["显存不足", "云端排队超时", "内容审核未通过"])
                  if status == "failed" else ""),
                 int((finished - started).total_seconds()), gen, queued))
    return inserted


def clear_demo():
    """删除全部演示数据（demo=1 的 tasks 及其名下 runs），返回删除的任务条数。

    真实任务（demo=0）永不触碰。"""
    if not _has_demo():
        return 0
    n = db.query("SELECT COUNT(*) c FROM tasks WHERE COALESCE(demo,0)=1")[0]["c"]
    db.execute("DELETE FROM runs WHERE task_id IN "
               "(SELECT id FROM tasks WHERE COALESCE(demo,0)=1)")
    db.execute("DELETE FROM tasks WHERE COALESCE(demo,0)=1")
    return int(n or 0)
