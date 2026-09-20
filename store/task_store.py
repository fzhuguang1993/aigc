"""
store/task_store.py —— 任务与执行记录持久化 + 导入导出
上层（GUI / workers）统一走这里，不再直接读写 Excel。
Excel 仅作为导入/导出格式存在。
"""
from datetime import datetime
from pathlib import Path

import pandas as pd

from core.config import RUNTIME_DIR
from store import db

COL_ID = "编号"; COL_PRODUCT = "品名"; COL_PROMPT = "提示词"
COL_STATUS = "状态"; COL_ACCOUNT = "账号"; COL_JOB_ID = "job_id"
COL_OUTPUT = "输出"; COL_URL = "URL"
COL_RUNS = "运行次数"; COL_SUCCESS = "成功次数"; COL_CANCEL = "取消次数"
COL_SCRIPT_TEXT = "口播文案"; COL_UPDATED = "更新时间"

_CN2DB = {"编号": "num", "品名": "product", "提示词": "prompt", "状态": "status",
          "账号": "account", "job_id": "job_id", "输出": "output", "URL": "url",
          "运行次数": "runs", "成功次数": "success", "取消次数": "cancels",
          "口播文案": "script_text", "更新时间": "updated_at"}

EXPORT_COLUMNS = ["编号", "品名", "提示词", "状态", "账号", "job_id", "输出", "URL",
                  "运行次数", "成功次数", "取消次数", "口播文案", "更新时间"]

EXPORT_DIR = RUNTIME_DIR / "exports"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 任务 CRUD ----------------

def list_tasks_df():
    """返回带中文列名的 DataFrame（供表格显示/导出），_id 列为数据库主键"""
    rows = db.query("SELECT * FROM tasks ORDER BY id")
    cols = ["id"] + list(_CN2DB.values())
    df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    for cn, en in _CN2DB.items():
        df[cn] = df[en]
    text_cols = [c for c in df.columns if c not in ("id", "runs", "success", "cancels", "_id")]
    if len(df):
        df[text_cols] = df[text_cols].fillna("").astype(str)
    df["_id"] = df["id"]
    return df


def get_task(task_id):
    rows = db.query("SELECT * FROM tasks WHERE id=?", (task_id,))
    return rows[0] if rows else None


def add_task(num, product, prompt):
    return db.execute("INSERT INTO tasks(num, product, prompt, updated_at) VALUES(?,?,?,?)",
                      (str(num), product, prompt, _now()))


def update_row(task_id, **fields):
    sets, args = [], []
    for k, v in fields.items():
        col = _CN2DB.get(k)
        if col:
            sets.append(f"{col}=?")
            args.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    args += [_now(), task_id]
    db.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", args)


def bump(task_id, cn_col):
    col = _CN2DB[cn_col]
    db.execute(f"UPDATE tasks SET {col}={col}+1, updated_at=? WHERE id=?", (_now(), task_id))


def delete_tasks(ids):
    for i in ids:
        db.execute("DELETE FROM tasks WHERE id=?", (i,))


def task_count():
    rows = db.query("SELECT COUNT(*) AS c FROM tasks")
    return rows[0]["c"] if rows else 0


def scan_new_rows():
    """返回 [(任务ID, {编号/品名/提示词}), ...]：提示词非空、状态为空、未运行过"""
    out = []
    for t in db.query("SELECT * FROM tasks ORDER BY id"):
        prompt = (t["prompt"] or "").strip()
        if prompt and not (t["status"] or "").strip() and int(t["runs"] or 0) == 0:
            out.append((t["id"], {"编号": t["num"], "品名": t["product"], "提示词": t["prompt"]}))
    return out


# ---------------- 执行记录 ----------------

def record_run_start(task_id, num, product, account, job_id):
    db.execute("INSERT INTO runs(task_id,num,product,account,job_id,status,started_at) "
               "VALUES(?,?,?,?,?,?,?)",
               (task_id, str(num), product, account, job_id, "running", _now()))


def record_run_end(job_id, status, output="", error=""):
    db.execute("UPDATE runs SET status=?, finished_at=?, output=?, error=? "
               "WHERE job_id=? AND finished_at IS NULL",
               (status, _now(), output, error, job_id))


def list_runs(limit=1000):
    return db.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))


# ---------------- 导入 / 导出 ----------------

def import_from_excel(path, sheet="Sheet"):
    """把任务 Excel 导入数据库，返回导入条数（列：编号/品名/提示词）"""
    df = pd.read_excel(path, sheet_name=sheet)
    count = 0
    for _, row in df.iterrows():
        prompt = str(row.get("提示词", "") or "").strip()
        if not prompt or prompt in ("nan", "None"):
            continue
        add_task(row.get("编号", ""), str(row.get("品名", "") or ""), prompt)
        count += 1
    return count


def write_import_template(path=None):
    """生成批量导入模板（带示例行，提示词支持单元格内换行），返回文件路径"""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    if not path:
        path = str(EXPORT_DIR / "导入模板.xlsx")
    df = pd.DataFrame([
        {"编号": 1, "品名": "示例-诺特兰德益生菌",
         "提示词": "第一行：场景描述（例：明亮客厅，男明星手持产品）\n"
                  "第二行：动作/口播内容（例：对镜头说：每天一条，肠道通畅）\n"
                  "第三行：镜头/字幕要求（例：产品logo特写，字幕：久坐党必备）"},
        {"编号": 2, "品名": "示例-品名写法自由",
         "提示词": "删掉示例行，一行一个提示词，直接从第三行开始填写即可。"},
    ])
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Sheet")
        ws = w.sheets["Sheet"]
        ws.column_dimensions["A"].width = 8
        ws.column_dimensions["B"].width = 24
        ws.column_dimensions["C"].width = 90
    return path


# ---------------- 看板统计 ----------------

_RANGE = ("SELECT COUNT(*) total, SUM(status='completed') ok,"
          " SUM(status IN ('failed','error')) fail, SUM(status='cancelled') cancel"
          " FROM runs WHERE substr(started_at,1,10)")


def _sum_row(rows):
    r = rows[0] if rows else {}
    return {k: int(r.get(k) or 0) for k in ("total", "ok", "fail", "cancel")}


def range_stats(days=7):
    """BI 看板数据：近 N 天汇总 + 环比上 N 天 + 逐日明细（缺失日补 0）+ 线路分布"""
    from datetime import date, timedelta
    today = date.today()
    cur_start = (today - timedelta(days=days - 1)).isoformat()
    prev_start = (today - timedelta(days=2 * days - 1)).isoformat()

    cur = _sum_row(db.query(_RANGE + " >= ? AND substr(started_at,1,10) <= ?",
                            (cur_start, today.isoformat())))
    prev = _sum_row(db.query(_RANGE + " >= ? AND substr(started_at,1,10) < ?",
                             (prev_start, cur_start)))
    running = db.query("SELECT COUNT(*) c FROM runs WHERE status='running'")[0]["c"]

    daily_rows = db.query(
        "SELECT substr(started_at,1,10) d, COUNT(*) total,"
        " SUM(status='completed') ok, SUM(status IN ('failed','error')) fail,"
        " SUM(status='cancelled') cancel"
        " FROM runs WHERE substr(started_at,1,10) >= ? GROUP BY d", (cur_start,))
    by_day = {r["d"]: r for r in daily_rows}
    daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        r = by_day.get(d, {})
        daily.append({"d": d, "total": int(r.get("total") or 0),
                      "ok": int(r.get("ok") or 0), "fail": int(r.get("fail") or 0),
                      "cancel": int(r.get("cancel") or 0)})

    accounts = db.query(
        "SELECT account, COUNT(*) total, SUM(status='completed') ok"
        " FROM runs WHERE substr(started_at,1,10) >= ?"
        " GROUP BY account ORDER BY total DESC", (cur_start,))

    cur["rate"] = round(cur["ok"] / cur["total"] * 100, 1) if cur["total"] else 0.0
    prev["rate"] = round(prev["ok"] / prev["total"] * 100, 1) if prev["total"] else 0.0
    return {"cur": cur, "prev": prev, "running": int(running or 0),
            "daily": daily, "accounts": accounts, "days": days}


def report_stats(days=1):
    """进度汇报用：区间内执行总量/成功数 + 每条任务执行次数分布 + 品名占比"""
    from collections import Counter
    from datetime import date, timedelta
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = db.query(
        "SELECT COALESCE(NULLIF(num, ''), 'task:' || task_id) k,"
        " product, status FROM runs"
        " WHERE substr(started_at,1,10) >= ? AND substr(started_at,1,10) <= ?",
        (start, end))
    total = len(rows)
    ok = sum(1 for r in rows if r["status"] == "completed")
    per_task = Counter(r["k"] for r in rows)
    # {执行次数: 条数}，只看跑了 2 次及以上的，次数多的排前
    dist = sorted(Counter(per_task.values()).items(), reverse=True)
    prods = Counter((r["product"] or "未填品名").strip() or "未填品名" for r in rows)
    return {"total": total, "ok": ok, "multi": dist,
            "products": prods.most_common(), "days": days}


def export_tasks(fmt="excel", path=None):
    df = list_tasks_df()
    return _dump(df[EXPORT_COLUMNS], fmt, path, f"任务表_{_stamp()}")


def export_runs(fmt="excel", path=None):
    return _dump(_runs_df(list_runs()), fmt, path, f"执行记录_{_stamp()}")


def export_runs_rows(rows, fmt="excel", path=None):
    """导出指定的记录子集（执行记录页「导出选中」用）"""
    return _dump(_runs_df(rows), fmt, path, f"执行记录_选中_{_stamp()}")


def _runs_df(rows):
    cols = ["started_at", "num", "product", "account", "status",
            "finished_at", "output", "error"]
    df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    df = df.rename(columns={"started_at": "开始时间", "num": "编号", "product": "品名",
                            "account": "账号", "status": "状态", "finished_at": "结束时间",
                            "output": "输出文件", "error": "错误信息"})
    return df[["开始时间", "编号", "品名", "账号", "状态", "结束时间", "输出文件", "错误信息"]]


def _stamp():
    return datetime.now().strftime("%m%d_%H%M")


def _dump(df, fmt, path, default_name):
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    if not path:
        ext = {"excel": ".xlsx", "csv": ".csv", "json": ".json"}[fmt]
        path = str(EXPORT_DIR / (default_name + ext))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if fmt == "excel":
        df.to_excel(path, index=False, engine="openpyxl")
    elif fmt == "csv":
        df.to_csv(path, index=False, encoding="utf-8-sig")
    else:
        df.to_json(path, orient="records", force_ascii=False, indent=2)
    return path
