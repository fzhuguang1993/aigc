"""
test_task_store.py —— 任务仓储层回归（导入定位 / 备注字段 / 筛选候选 / 累计口径）
"""
from store import task_store as ts


def test_import_returns_involved_ids(tmp_path):
    tpl = ts.write_import_template(str(tmp_path / "tpl.xlsx"))
    n, dup, hit_ids = ts.import_from_excel(tpl)
    assert n == 3 and dup == 0
    assert len(hit_ids) == 3 and len(set(hit_ids)) == 3
    in_db = {int(r["_id"]) for _, r in ts.list_tasks_df().iterrows()}
    assert set(hit_ids) <= in_db, "返回的 ID 必须真能在表里查到"

    # 重复导入：一条不新增，但仍返回库里已有的那 3 个任务——
    # 上传旧提示词时界面才能把“就是这几条”定位出来
    n2, dup2, hit_ids2 = ts.import_from_excel(tpl)
    assert n2 == 0 and dup2 == 3
    assert sorted(hit_ids2) == sorted(hit_ids)


def test_import_mixed_new_and_old_prompts(tmp_path):
    """一半新提示词 + 一半旧提示词：ID 列表要把两边都带回来"""
    import pandas as pd
    old = str(tmp_path / "old.xlsx")
    pd.DataFrame({"提示词": ["已存在的甲", "已存在的乙"]}).to_excel(
        old, index=False, sheet_name="Sheet")
    _, _, old_ids = ts.import_from_excel(old)
    assert len(old_ids) == 2

    mixed = str(tmp_path / "mixed.xlsx")
    pd.DataFrame({"提示词": ["已存在的甲", "全新的丙"]}).to_excel(
        mixed, index=False, sheet_name="Sheet")
    n, dup, ids = ts.import_from_excel(mixed)
    assert (n, dup) == (1, 1)
    assert old_ids[0] in ids, "旧任务也要被定位到"
    assert len(ids) == 2


def test_import_keeps_id_order_matching_table_order(tmp_path):
    """导入顺序 = 列表顺序（聚集置顶依赖稳定顺序，不能乱）"""
    path = str(tmp_path / "t.xlsx")
    import pandas as pd
    pd.DataFrame({"提示词": ["提示词甲", "提示词乙", "提示词丙"]}).to_excel(
        path, index=False, sheet_name="Sheet")
    _, _, ids = ts.import_from_excel(path)
    df = ts.list_tasks_df()
    assert [int(i) for i in df["_id"]] == ids
    assert [str(p) for p in df["提示词"]] == ["提示词甲", "提示词乙", "提示词丙"]


# ---------------- 备注（使用者自己标的管理记号）----------------

def test_remark_roundtrip_and_exported():
    tid = ts.add_task("1", "诺特兰德", "提示词甲", remark="已过审")
    assert ts.get_task(tid)["remark"] == "已过审"
    ts.update_row(tid, **{"备注": "待重拍"})
    assert ts.get_task(tid)["remark"] == "待重拍"
    assert str(ts.list_tasks_df().iloc[0]["备注"]) == "待重拍"
    assert "备注" in ts.EXPORT_COLUMNS, "导出必须带备注，否则交接/换机器就丢了"


def test_remark_change_does_not_reset_execution_state():
    """写备注不能动执行态，也不能被当成“改过提示词”——
    否则备注一写，已完成的任务全被推动重跑判定"""
    tid = ts.add_task("1", "P", "提示词")
    ts.update_row(tid, **{"状态": "completed", "运行次数": 1})
    before = ts.get_task(tid)
    ts.update_row(tid, **{"备注": "复查过"})
    t = ts.get_task(tid)
    assert (t["status"], int(t["runs"])) == ("completed", 1)
    assert t["prompt_changed_at"] == before["prompt_changed_at"], \
        "只有提示词变更才该推 prompt_changed_at"


def test_import_excel_carries_remark(tmp_path):
    import pandas as pd
    path = str(tmp_path / "r.xlsx")
    pd.DataFrame({"提示词": ["甲", "乙"], "备注": ["已过审", ""]}).to_excel(
        path, index=False, sheet_name="Sheet")
    n, dup, ids = ts.import_from_excel(path)
    assert (n, dup) == (2, 0)
    assert ts.get_task(ids[0])["remark"] == "已过审"
    assert ts.get_task(ids[1])["remark"] == ""


def test_import_template_has_remark_column(tmp_path):
    """模板里没有这一列，使用者就只会用软件改备注， Excel 闭环断掉"""
    import pandas as pd
    path = ts.write_import_template(str(tmp_path / "tpl.xlsx"))
    assert "备注" in pd.read_excel(path, sheet_name="Sheet").columns


# ---------------- 筛选候选 ----------------

def test_filter_choices_lists_distinct_values():
    ts.add_task("1", "甲品", "提示词甲", remark="已过审")
    ts.add_task("2", "乙品", "提示词乙", script="同一脚本", remark="已过审")
    ts.add_task("3", "", "提示词丙")            # 空值不进候选（界面走「（未填）」虚档）
    ch = ts.filter_choices()
    assert set(ch["products"]) == {"甲品", "乙品"}
    assert ch["products"] == sorted(ch["products"]), "候选要有序，下拉里不能乱排"
    assert "" not in ch["products"], "空品名不进候选（界面另给「（未填）」虚档）"
    assert ch["remarks"] == ["已过审"]
    assert ch["scripts"] == ["同一脚本"]


# ---------------- 看板「全部」累计口径 ----------------

def _add_run(status, day):
    from store import db
    db.execute(
        "INSERT INTO runs(task_id,num,product,account,job_id,status,"
        "started_at,finished_at,duration) VALUES(?,?,?,?,?,?,?,?,?)",
        (1, "1", "P", "acc1", f"j-{day}-{status}", status,
         f"{day} 10:00:00", f"{day} 10:00:10", 10))


def test_range_stats_none_covers_all_history():
    """days=None＝从第一条记录累计至今，换天/换周都不会“归零”"""
    from datetime import date, timedelta
    old = (date.today() - timedelta(days=400)).isoformat()    # 超出任何近 N 天窗口
    _add_run("completed", old)
    _add_run("failed", date.today().isoformat())
    assert ts.range_stats(7)["cur"]["total"] == 1
    st = ts.range_stats(None)
    assert st["cur"]["total"] == 2 and st["cur"]["fail"] == 1
    assert st["prev"]["total"] == 0, "累计没有上一周期，环比必须是 0（界面据此隐掉环比）"
    assert [d["d"] for d in st["daily"]] == sorted(
        [d["d"] for d in st["daily"]]), "逐日按日期升序"
    assert len(st["daily"]) == 2, "累计口径只列有记录的天，不补几年空白 0"
    assert st["days"] is None
