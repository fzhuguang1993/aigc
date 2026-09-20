"""
test/seed_demo.py —— 灌入演示数据（任务 + 执行记录），供界面预览排序/筛选/悬停效果
用法：python test/seed_demo.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store import db
db.init()
from store import task_store as ts
from store.db import query as _q, execute as _e


def query_tasks(sql, args=()):
    return _q(sql, args)


ts.query_tasks = query_tasks
ts.execute = _e

DEMO = [
    # (编号, 品名, 提示词, 状态, 账号, 运行, 成功, 取消, 口播文案, 距今天数)
    ("1", "诺特兰德益生菌", "一位男明星手持诺特兰德益生菌产品，站在明亮的客厅里，对镜头口播推荐："
     "肠道通畅全靠它，每天一条，益生菌+益生元双搭配。要求口型同步，画面干净，节奏轻快。",
     "completed", "acc1", 1, 1, 0,
     "肠道通畅全靠它，每天一条，益生菌加益生元双搭配，久坐外卖党真的可以冲。", 0),
    ("2", "诺特兰德VB", "职场女性清晨在办公桌前，拿起诺特兰德维生素B泡腾片丢进杯中，气泡特写，"
     "对镜头说：加班熬夜脸发黄，就靠它救急。光线柔和，产品logo清晰可见。",
     "completed", "acc1", 2, 1, 0,
     "加班熬夜脸发黄，就靠它救急，一片顶三天，办公室抽屉常备。", 1),
    ("3", "诺特兰德钙片", "健身教练在健身房拿起诺特兰德钙片，肌肉线条特写，口播：练了三年，"
     "骨骼健康才知道重要，钙+VD+K2三合一，练后一粒刚刚好。",
     "", "", 0, 0, 0, "", 0),
    ("4", "诺特兰德叶黄素", "戴眼镜的程序员揉眼睛，镜头推近桌上的诺特兰德叶黄素酯软糖，"
     "拆开吃两粒，屏幕蓝光在镜片上反光。字幕：盯屏10小时的救命软糖。",
     "failed", "acc1", 1, 0, 0, "", 2),
    ("5", "诺特兰德蛋白粉", "清晨厨房，女主将诺特兰德蛋白粉舀入摇摇杯，牛奶倒入慢镜头，"
     "蛋白粉挂壁细腻，对镜头微笑：早餐3分钟，蛋白质拉满。",
     "", "", 0, 0, 0, "", 0),
    ("6", "同仁堂枸杞原浆", "国潮风背景，汉服少女手持小袋枸杞原浆，撕开直接喝，"
     "琥珀色液体特写，口播：以前提气色就输在起跑线。色调暖红，转场利落。",
     "cancelled", "acc1", 1, 0, 1, "", 3),
    ("7", "汤臣倍健鱼油", "书房场景，中年男士对着镜头展示汤臣倍健鱼油胶囊，"
     "剪开胶囊滴在青菜上类比：每天两粒，给血管洗个澡。",
     "", "", 0, 0, 0, "", 1),
    ("8", "Swisse葡萄籽", "美妆博主风，女生梳妆台前手持Swisse葡萄籽，"
     "素颜怼脸镜头抗氧化，口播：晒后修复的黄金72小时别忘了它。",
     "completed", "acc1", 1, 1, 0, "", 4),
]

count = 0
existing = ts.query_tasks("SELECT COUNT(*) AS c FROM tasks WHERE product LIKE '%诺特兰德益生菌%'")
if existing and existing[0]["c"] > 0:
    print("演示数据已存在，跳过（如需重新灌入请先清空 data/aigc.db）")
    sys.exit(0)

for num, product, prompt, status, account, runs, success, cancels, script, days_ago in DEMO:
    tid = ts.add_task(num, product, prompt)
    updated = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    ts.execute("UPDATE tasks SET status=?, account=?, runs=?, success=?, cancels=?, "
               "script_text=?, updated_at=? WHERE id=?",
               (status, account, runs, success, cancels, script, updated, tid))
    # 同步造执行记录
    for i in range(runs):
        started = (datetime.now() - timedelta(days=days_ago, hours=i + 1))
        run_status = {"completed": "completed", "failed": "failed",
                      "cancelled": "cancelled", "": "running"}[status]
        ts.execute(
            "INSERT INTO runs(task_id,num,product,account,job_id,status,started_at,"
            "finished_at,output,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (tid, num, product, account or "acc1", f"demo-{tid}-{i}", run_status,
             started.strftime("%Y-%m-%d %H:%M:%S"),
             "" if run_status == "running" else (started + timedelta(minutes=6)).strftime("%Y-%m-%d %H:%M:%S"),
             f"outputs/{started:%m%d}/00{num}_{product}_demo_{i + 1:02d}.mp4"
             if run_status == "completed" else "",
             "云端返回 failed：显存不足" if run_status == "failed" else ""))
    count += 1

print(f"已灌入 {count} 个演示任务 + 执行记录（可重复执行，会追加）")
print("启动界面查看：python desktop.py")
