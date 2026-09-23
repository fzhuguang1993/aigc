"""
test_run_gen_sec.py —— 单条视频的「生成用时」与「排队用时」拆分

钉住的是那个口径问题：批量提交时同一条线串行跑，「提交→完成」里一大半是
在云端等空位。拿它当生成耗时，越靠后的视频显得越慢，看板平均下来的也不是
「一条视频要跑多久」。数据源是云端作业回报的三个时间戳（实测都有）：
`submitted_at`（提交）→ `started_at`（开始跑）→ `completed_at`（出片）。
"""
from datetime import datetime

from store import db, task_store as ts

# 从真实回报里抄的一段：排队 629 秒（39.40s→09.38s 不足整分，向下取整）、生成 297 秒
CLOUD = {"job_id": "j-1",
         "submitted_at": "2026-09-23T07:32:39.399710+00:00",
         "started_at": "2026-09-23T07:43:09.377000+00:00",
         "completed_at": "2026-09-23T07:48:06.952000+00:00"}


def _row(job_id):
    return db.query("SELECT * FROM runs WHERE job_id=?", (job_id,))[0]


def _insert_run(tid=1, status="completed", duration=1294, gen=0, queued=0, job_id=None):
    """直接写一行执行记录：起点用当天，保证落在看板「近 7 天」窗口里"""
    today = datetime.now().strftime("%Y-%m-%d")
    jid = job_id or f"j-{tid}-{gen}-{queued}"
    db.execute("INSERT INTO runs(task_id,num,product,account,job_id,status,"
               "started_at,finished_at,duration,gen_sec,queued_sec) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
               (tid, str(tid), "品名", "acc1", jid, status,
                f"{today} 10:00:00", f"{today} 10:21:34", duration, gen, queued))
    return jid


# ---------------- 拆分怎么算 ----------------

def test_record_run_end_splits_queue_from_generation():
    ts.record_run_start(1, "1", "品名", "acc1", "j-1")
    ts.record_run_end("j-1", "completed", cloud=CLOUD)
    r = _row("j-1")
    assert r["queued_sec"] == 629, "提交→开始跑＝在云端等空位"
    assert r["gen_sec"] == 297, "开始跑→出片才是这条视频真正的生成耗时"


def test_envelope_and_z_suffix_still_split():
    """云端换写法不能让拆分静默失效"""
    ts.record_run_start(1, "1", "品名", "acc1", "j-2")
    ts.record_run_end("j-2", "completed", cloud={"data": dict(CLOUD, submitted_at=None)})
    assert _row("j-2")["gen_sec"] == 297
    ts.record_run_start(1, "1", "品名", "acc1", "j-3")
    ts.record_run_end("j-3", "completed",
                      cloud=dict(CLOUD,
                                 started_at="2026-09-23T07:43:09Z",
                                 completed_at="2026-09-23T07:48:09Z"))
    assert _row("j-3")["gen_sec"] == 300


def test_no_timestamps_leaves_split_empty_rather_than_guessed():
    """拿不到云端时间戳就留 0：宁可没数，也不拿轮询时刻猜一个「生成耗时」"""
    ts.record_run_start(1, "1", "品名", "acc1", "j-4")
    ts.record_run_end("j-4", "completed", cloud={"status": "completed"})
    r = _row("j-4")
    assert (r["gen_sec"], r["queued_sec"]) == (0, 0)
    assert r["status"] == "completed", "拆不出来不影响终态照常刻录"


def test_backfill_does_not_overwrite_a_real_split():
    """回填要能重复跑：第二次不动已有值，否则刚采到的新数会被老回报刷掉"""
    jid = _insert_run(gen=297, queued=630, job_id="j-5")
    assert ts.update_run_split(jid, CLOUD) is False
    assert _row("j-5")["gen_sec"] == 297

    legacy = _insert_run(job_id="j-6", gen=0, queued=0)
    assert ts.update_run_split(legacy, CLOUD) is True
    r = _row("j-6")
    assert (r["gen_sec"], r["queued_sec"]) == (297, 629)
    assert ts.update_run_split(legacy, CLOUD) is False, "第二次跑就是幂等的"


# ---------------- 任务中心那一列 ----------------

def test_task_row_shows_generation_time_with_queue_beside_it():
    tid = ts.add_task("1", "品名甲", "提示词甲")
    _insert_run(tid=tid, duration=928, gen=298, queued=630)
    row = ts.list_tasks_df().iloc[0]
    assert int(row["生成用时"]) == 298
    assert int(row["排队用时"]) == 630
    assert not bool(row["用时含排队"]), "拆得开的记录不该标「含排队」"


def test_legacy_row_falls_back_to_total_and_says_so():
    """早期记录只有「提交→完成」：给那个数可以，但必须标出来含排队"""
    tid = ts.add_task("2", "品名乙", "提示词乙")
    _insert_run(tid=tid, duration=928, gen=0, queued=0)
    row = ts.list_tasks_df().iloc[0]
    assert int(row["生成用时"]) == 928, "没有拆分可显示时显示总用时，别显示 0"
    assert bool(row["用时含排队"]) is True


def test_latest_successful_run_wins():
    """重跑过几条就显示最近成功那条的生成耗时，不累加也不取平均"""
    tid = ts.add_task("3", "品名丙", "提示词丙")
    _insert_run(tid=tid, duration=928, gen=600, queued=300)
    _insert_run(tid=tid, duration=500, gen=300, queued=200)
    row = ts.list_tasks_df().iloc[0]
    assert int(row["生成用时"]) == 300 and int(row["排队用时"]) == 200


# ---------------- 看板平均 ----------------

def test_dashboard_average_excludes_queue():
    """「平均生成时长」只平均生成段：排队 630 秒不该把一条 5 分钟的视频说成 15 分钟"""
    _insert_run(duration=928, gen=298, queued=630)
    _insert_run(tid=2, duration=790, gen=457, queued=333)
    cur = ts.range_stats(7)["cur"]
    assert cur["avg_dur"] == (298 + 457) / 2
    assert cur["avg_queued"] == (630 + 333) / 2, "排队另给一个数，别塞进生成里"


def test_legacy_rows_still_count_in_average_via_total():
    """没拆分的老记录回退用总用时参与平均：把它们从平均值里隐掉，
    看上去反而像「只统计了最近那几条」"""
    _insert_run(duration=900, gen=0, queued=0)
    assert ts.range_stats(7)["cur"]["avg_dur"] == 900


def test_running_row_is_not_averaged():
    _insert_run(status="running", duration=0, gen=0, queued=0)
    assert ts.range_stats(7)["cur"]["avg_dur"] == 0


# ---------------- 展示格式 ----------------

def test_secs_display_is_one_wording_everywhere():
    from gui.formatting import secs
    assert secs(298) == "4分58秒" and secs(45) == "45秒"
    assert secs(0) == "—" and secs(None) == "—", "没跑过不能显示成「0秒」"
