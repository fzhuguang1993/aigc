"""
backfill_run_gen_sec.py —— 给历史执行记录补齐「排队 / 生成」用时拆分

为什么要跑它：以前只存了一个「用时」（提交→完成，里面混着在云端排队等空位的时间），
现在拆成 `queued_sec` + `gen_sec` 两列，数据取自云端作业回报的
`submitted_at / started_at / completed_at`（见 docs/云端接口字段说明.md）。
新跑的任务会自动录，**只有改动之前那批老记录**需要这个脚本按 job_id 回云端查一次。

用法（在仓库根目录）：
    python backfill_run_gen_sec.py --dry-run     # 先看打算改成什么，不动库
    python backfill_run_gen_sec.py              # 真正写入
    python backfill_run_gen_sec.py --limit 50    # 只处理最近 50 条执行记录

云端已经清掉的作业（查不到）会被跳过并计数，不影响其余记录；
已经有拆分的行不会被覆盖，所以重复跑是安全的。
"""
import sys
import time

from store import db, task_store
from core.api_client import query_job
from registry.manager import get_account


def _pending_runs(limit):
    """还没拆分过、又有 job_id 的执行记录（list_runs 已按新→旧排）

    在跑的也收：云端已回报 started_at，能先把排队那段补上，
    跑完时 `record_run_end` 会把两个数一起刷成终态值。"""
    out = []
    for r in task_store.list_runs(limit=limit or 100000):
        if int(r.get("gen_sec") or 0) or int(r.get("queued_sec") or 0):
            continue                        # 已经拆过
        if not (r.get("job_id") or "").strip():
            continue                        # 没提交成功过，云端也没得查
        out.append(r)
    return out


def main(argv):
    dry = "--dry-run" in argv
    limit = 0
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except (IndexError, ValueError):
            print("--limit 需要一个数字")
            return 2

    db.init()
    rows = _pending_runs(limit)
    print(f"待补齐 {len(rows)} 条执行记录{'（--dry-run：不写库）' if dry else ''}")
    filled = missing = failed = 0
    for r in rows:
        acc = get_account(r["account"])
        if not acc:
            print(f"  跳过 job={r['job_id']}：本机没有线路「{r['account']}」的配置")
            missing += 1
            continue
        try:
            cloud = query_job(acc.base, r["job_id"])
        except Exception as e:
            # 云端清历史作业 / 换机器 / 网络不通，都只影响这一条
            print(f"  跳过 job={r['job_id']}：{type(e).__name__} {str(e)[:80]}")
            failed += 1
            continue
        queued, gen = task_store.split_from_cloud(cloud)
        if not (queued or gen):
            print(f"  跳过 job={r['job_id']}：云端没回报 started_at/completed_at")
            missing += 1
            continue
        label = (f"编号{r['num'] or '?'} {r['product'] or '未填品名'} "
                 f"[{r['status']}] 总用时 {r['duration']}秒")
        if dry:
            print(f"  将写入 {label} → 排队 {queued}秒 + 生成 {gen}秒")
        elif task_store.update_run_split(r["job_id"], cloud):
            print(f"  已写入 {label} → 排队 {queued}秒 + 生成 {gen}秒")
        else:
            print(f"  未写入 {label}（已有拆分或被并发抢先）")
            continue
        filled += 1
        time.sleep(0.2)                     # 别拿回填把云端问爆了

    print(f"完成：{'' if dry else '已'}补齐 {filled} 条"
          + (f"，云端查不到 {missing} 条" if missing else "")
          + (f"，查询失败 {failed} 条" if failed else ""))
    if not dry and filled:
        print("看板/任务中心的「生成用时」是现算的，重开页面就能看到")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
