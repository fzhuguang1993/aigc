"""
store/task_store.py —— 任务与执行记录持久化 + 导入导出
上层（GUI / workers）统一走这里，不再直接读写 Excel。
Excel 仅作为导入/导出格式存在。
"""
from datetime import datetime
from pathlib import Path

import pandas as pd

from core.config import EXPORT_DIR as EXPORT_DIR_STR
from store import db

COL_ID = "编号"; COL_PRODUCT = "品名"; COL_PROMPT = "提示词"
COL_SCRIPT = "脚本"; COL_STORYBOARD = "分镜数"
COL_STATUS = "状态"; COL_ACCOUNT = "账号"; COL_JOB_ID = "job_id"
COL_OUTPUT = "输出"; COL_URL = "URL"; COL_DURATION = "时长"
COL_RUNS = "运行次数"; COL_SUCCESS = "成功次数"; COL_CANCEL = "取消次数"
COL_SCRIPT_TEXT = "口播文案"; COL_UPDATED = "更新时间"

_CN2DB = {"编号": "num", "品名": "product", "提示词": "prompt",
          "脚本": "script", "分镜数": "storyboard",
          "状态": "status", "账号": "account", "job_id": "job_id",
          "输出": "output", "URL": "url",
          "运行次数": "runs", "成功次数": "success", "取消次数": "cancels",
          "口播文案": "script_text", "更新时间": "updated_at", "时长": "duration",
          "备注": "remark"}

# 全字段导出（与导入模板列对齐，方便导出→修改→再导入闭环）
EXPORT_COLUMNS = ["编号", "品名", "提示词", "脚本", "备注", "口播文案", "分镜数",
                  "状态", "时长", "执行用时", "账号", "job_id",
                  "输出", "URL", "运行次数", "成功次数", "取消次数", "更新时间"]

EXPORT_DIR = Path(EXPORT_DIR_STR)   # 模板/导出目录：可在设置里改，默认运行目录 exports/


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now_precise():
    # 含微秒：用于「提示词变更 vs 上次执行」同秒级比较，以及用时计算
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


def _parse_ts(ts):
    try:
        return datetime.strptime(ts[:26], "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:                     # 兼容旧数据：秒精度无微秒尾巴
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")


# ---------------- 任务 CRUD ----------------

def list_tasks_df():
    """返回带中文列名的 DataFrame（供表格显示/导出），_id 列为数据库主键"""
    rows = db.query("SELECT * FROM tasks ORDER BY id")
    cols = ["id"] + list(_CN2DB.values())
    df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    for cn, en in _CN2DB.items():
        df[cn] = df[en]
    text_cols = [c for c in df.columns
                 if c not in ("id", "runs", "success", "cancels", "duration",
                              "storyboard", "_id", "分镜数")]
    if len(df):
        df[text_cols] = df[text_cols].fillna("").astype(str)
    df["_id"] = df["id"]
    # 派生列：执行用时（秒）——优先取最近一次成功执行，没有成功过再取最近终态
    durs = {}
    for sql in (
            "SELECT task_id, duration FROM runs WHERE status='completed' AND duration > 0 "
            "AND id IN (SELECT MAX(id) FROM runs WHERE status='completed' GROUP BY task_id)",
            "SELECT task_id, duration FROM runs WHERE duration > 0 AND status!='running' "
            "AND id IN (SELECT MAX(id) FROM runs WHERE status!='running' GROUP BY task_id)"):
        for r in db.query(sql):
            durs.setdefault(r["task_id"], int(r["duration"] or 0))
    df["执行用时"] = [int(durs.get(i, 0)) for i in df["_id"]] if len(df) \
        else pd.Series([], dtype=int)
    return df


def get_task(task_id):
    rows = db.query("SELECT * FROM tasks WHERE id=?", (task_id,))
    return rows[0] if rows else None


def filter_choices():
    """筛选下拉的候选值（任务多了光靠搜索拦不住）。"""
    def _distinct(col):
        vals = {(r[col] or "").strip() for r in
                db.query(f"SELECT DISTINCT {col} FROM tasks")}
        vals.discard("")
        return sorted(vals)
    return {"products": _distinct("product"), "scripts": _distinct("script"),
            "remarks": _distinct("remark")}


def add_task(num, product, prompt, script="", script_text="", storyboard=0, remark=""):
    """新建任务。品名/脚本允许为空（通版素材不关联任何产品/脚本）"""
    return db.execute(
        "INSERT INTO tasks(num, product, prompt, script, script_text, storyboard, "
        "remark, updated_at, prompt_changed_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(num), product, prompt, script, script_text, int(storyboard or 0),
         remark, _now(), _now_precise()))


def update_row(task_id, **fields):
    sets, args = [], []
    for k, v in fields.items():
        col = _CN2DB.get(k)
        if col:
            sets.append(f"{col}=?")
            args.append(v)
    # 提示词被修改：记下变更时间，供“迭代执行”判定
    if COL_PROMPT in fields:
        sets.append("prompt_changed_at=?")
        args.append(_now_precise())
    if not sets:
        return
    sets.append("updated_at=?")
    args += [_now(), task_id]
    db.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", args)


def iter_ready(task_id):
    """提示词在上次执行之后又改过 → 应视为“迭代执行”，允许重跑"""
    t = get_task(task_id)
    if not t:
        return False
    changed = t["prompt_changed_at"] or ""
    if not changed:
        return False                      # 存量老数据未记录变更时间，沿用旧行为
    last = db.query("SELECT started_at FROM runs WHERE task_id=? "
                    "ORDER BY id DESC LIMIT 1", (task_id,))
    if not last:
        return True                       # 改过提示词且从未执行过
    # 时间戳含微秒，可区分同秒内的先后顺序（定长字符串，可直接比较）
    return changed > (last[0]["started_at"] or "")


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
               (task_id, str(num), product, account, job_id, "running", _now_precise()))


def record_run_end(job_id, status, output="", error=""):
    now = _now_precise()
    # 执行用时（秒）：本次结束时间 - 开始时间
    dur = 0
    rows = db.query("SELECT started_at FROM runs WHERE job_id=? AND finished_at IS NULL "
                    "ORDER BY id DESC LIMIT 1", (job_id,))
    if rows and rows[0]["started_at"]:
        try:
            dur = max(0, int((_parse_ts(now) - _parse_ts(rows[0]["started_at"]
                                         )).total_seconds()))
        except ValueError:
            dur = 0
    db.execute("UPDATE runs SET status=?, finished_at=?, duration=?, output=?, error=? "
               "WHERE job_id=? AND finished_at IS NULL",
               (status, now, dur, output, error, job_id))


def attach_run_result(job_id, output=None, error=None):
    """终态打点之后补录产物/错误（如生成结束后才完成的下载），
    不改 finished_at/duration，保证“执行用时”不被后置动作污染。"""
    sets, args = [], []
    if output is not None:
        sets.append("output=?")
        args.append(output)
    if error is not None:
        sets.append("error=?")
        args.append(error)
    if not sets:
        return
    args.append(job_id)
    db.execute(f"UPDATE runs SET {','.join(sets)} WHERE job_id=?", args)


def list_runs(limit=1000):
    return db.query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))


# ---------------- 导入 / 导出 ----------------

def import_from_excel(path, sheet="Sheet"):
    """把任务 Excel 导入数据库（全字段：编号/品名/提示词/脚本/口播文案/分镜数）。

    品名/脚本允许留空（通版素材不关联产品/脚本）。
    按提示词去重：与库中已有任务、文件内部重复的提示词都不再追加。
    返回 (导入条数, 跳过重复条数, 本次文件涉及的任务ID列表)——第三项除新建任务外，
    也包含提示词已在库里而被跳过的旧任务，上传旧提示词时也能直接定位到那批任务。
    """
    df = pd.read_excel(path, sheet_name=sheet)
    by_prompt = {}
    for r in db.query("SELECT id, prompt FROM tasks"):
        by_prompt.setdefault((r["prompt"] or "").strip(), r["id"])
    seen = set()
    count = dup = 0
    hit_ids = []
    for _, row in df.iterrows():
        prompt = str(row.get("提示词", "") or "").strip()
        if not prompt or prompt in ("nan", "None"):
            continue
        if prompt in by_prompt or prompt in seen:
            dup += 1
            old = by_prompt.get(prompt)
            if old is not None and int(old) not in hit_ids:
                hit_ids.append(int(old))      # 旧提示词早就在库里：一样定位
            continue
        seen.add(prompt)

        def _s(col):
            v = str(row.get(col, "") or "").strip()
            return "" if v in ("nan", "None") else v

        try:
            story = int(float(row.get("分镜数", 0) or 0))
        except (TypeError, ValueError):
            story = 0
        tid = add_task(row.get("编号", ""), _s("品名"), prompt, script=_s("脚本"),
                       script_text=_s("口播文案"), storyboard=story,
                       remark=_s("备注"))
        if tid:
            hit_ids.append(int(tid))
        count += 1
    return count, dup, hit_ids


def write_import_template(path=None):
    """生成批量导入模板（全字段，带示例行，提示词支持单元格内换行），返回文件路径"""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    if not path:
        path = str(EXPORT_DIR / "导入模板.xlsx")
    df = pd.DataFrame([
        {"编号": 1, "品名": "示例-诺特兰德益生菌",
         "提示词": "第一行：场景描述（例：明亮客厅，男明星手持产品）\n"
                  "第二行：动作/口播内容（例：对镜头说：每天一条，肠道通畅）\n"
                  "第三行：镜头/字幕要求（例：产品logo特写，字幕：久坐党必备）",
         "脚本": "自定义脚本（人物/场景/分镜拆解等，自己填）\n"
                "口播文案不用写：执行时自动从提示词识别",
         "备注": "任务多了随手标一句：已过审 / 待重拍 / 只发视频号…（可导入可导出）",
         "口播文案": "", "分镜数": ""},
        {"编号": 2, "品名": "",   # 品名可留空：通版素材不关联产品
         "提示词": "工厂流水线空镜，传送带特写，无产品。品名/脚本都可留空。",
         "脚本": "", "备注": "", "口播文案": "", "分镜数": ""},
        {"编号": 3, "品名": "示例-手写口播",
         "提示词": "对镜头说：\"换季免疫力，每天两粒\"，字幕同步。",
         "脚本": "一个脚本可绑多条提示词：导入后用任务中心「批量绑定脚本」统一关联",
         "备注": "", "口播文案": "换季免疫力，每天两粒", "分镜数": 3},
    ])
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Sheet")
        ws = w.sheets["Sheet"]
        for col, width in (("A", 8), ("B", 22), ("C", 60), ("D", 36),
                           ("E", 24), ("F", 24), ("G", 8)):
            ws.column_dimensions[col].width = width
    return path


# ---------------- 看板统计 ----------------

_RANGE = ("SELECT COUNT(*) total, SUM(status='completed') ok,"
          " SUM(status IN ('failed','error')) fail, SUM(status='cancelled') cancel,"
          " AVG(CASE WHEN status='completed' THEN duration END) dur"
          " FROM runs WHERE substr(started_at,1,10)")


def _sum_row(rows):
    r = rows[0] if rows else {}
    out = {k: int(r.get(k) or 0) for k in ("total", "ok", "fail", "cancel")}
    out["avg_dur"] = round(float(r.get("dur") or 0), 1)   # 成功执行平均用时（秒）
    return out


def range_stats(days=7):
    """BI 看板数据：近 N 天汇总 + 环比上 N 天 + 逐日明细（缺失日补 0）+ 线路分布

    days=None 代表「全部」：从第一条记录累计至今（永不清零）。
    没有上一个等长周期可比，prev 全 0，看板据此隐掉环比。"""
    from datetime import date, timedelta
    today = date.today().isoformat()
    t0 = "0000-01-01"
    if days is None:
        cur_start, prev_start = t0, None
    else:
        cur_start = (date.today() - timedelta(days=days - 1)).isoformat()
        prev_start = (date.today() - timedelta(days=2 * days - 1)).isoformat()

    cur = _sum_row(db.query(_RANGE + " >= ? AND substr(started_at,1,10) <= ?",
                            (cur_start, today)))
    prev = (_sum_row(db.query(_RANGE + " >= ? AND substr(started_at,1,10) < ?",
                             (prev_start, cur_start))) if prev_start
            else {"total": 0, "ok": 0, "fail": 0, "cancel": 0, "avg_dur": 0})
    running = db.query("SELECT COUNT(*) c FROM runs WHERE status='running'")[0]["c"]

    daily_rows = db.query(
        "SELECT substr(started_at,1,10) d, COUNT(*) total,"
        " SUM(status='completed') ok, SUM(status IN ('failed','error')) fail,"
        " SUM(status='cancelled') cancel"
        " FROM runs WHERE substr(started_at,1,10) >= ? GROUP BY d", (cur_start,))
    by_day = {r["d"]: r for r in daily_rows}
    daily = []
    if days is None:
        # 累计口径下把每一天都补零会拉出一条长空白：只列有记录的那些天
        # （图轴取最近 60 个有记录日，再多就不好看也不需要）
        for d in sorted(by_day)[-60:]:
            r = by_day[d]
            daily.append({"d": d, "total": int(r.get("total") or 0),
                          "ok": int(r.get("ok") or 0),
                          "fail": int(r.get("fail") or 0),
                          "cancel": int(r.get("cancel") or 0)})
    else:
        for i in range(days - 1, -1, -1):
            d = (date.today() - timedelta(days=i)).isoformat()
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
    """进度汇报用：区间内执行总量/成功数 + 每条任务执行次数分布 + 品名占比

    days=None ＝全部历史累计（汇报口径变成“累计”）。"""
    from collections import Counter
    from datetime import date, timedelta
    end = date.today().isoformat()
    start = "0000-01-01" if days is None else \
        (date.today() - timedelta(days=days - 1)).isoformat()
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


def daily_report_stats():
    """日汇报（今日截至目前 vs 昨日）：总量/成功 + 分产品 + 视频时长分桶。

    时长桶用任务的 tasks.duration（视频秒数）；runs.duration 是执行耗时，不能用。
    时长未知（<=0）只计总量、不落进两个时长桶。占比分母＝长+短（有明确时长的）。
    """
    from collections import defaultdict
    from datetime import date, timedelta
    today = date.today().isoformat()
    yest = (date.today() - timedelta(days=1)).isoformat()
    rows = db.query(
        "SELECT substr(r.started_at,1,10) d, r.product product, r.status status,"
        " COALESCE(t.duration, 0) vdur"
        " FROM runs r LEFT JOIN tasks t ON t.id = r.task_id"
        " WHERE substr(r.started_at,1,10) IN (?, ?)", (today, yest))

    def _blank():
        return {"total": 0, "ok": 0, "long": 0, "short": 0,
                "products": defaultdict(lambda: [0, 0])}
    agg = {today: _blank(), yest: _blank()}
    for r in rows:
        a = agg.get(r["d"])
        if a is None:
            continue
        a["total"] += 1
        ok = r["status"] == "completed"
        if ok:
            a["ok"] += 1
        try:
            vdur = int(r["vdur"] or 0)
        except (TypeError, ValueError):
            vdur = 0
        if vdur >= 10:
            a["long"] += 1
        elif vdur > 0:
            a["short"] += 1
        p = (r["product"] or "").strip() or "未填品名"
        slot = a["products"][p]
        slot[0] += 1
        if ok:
            slot[1] += 1

    cur, prev = agg[today], agg[yest]
    # 分产品：按 运行次数降序 → 成功数降序 → 品名，次数多的排前
    products = sorted(((p, n[0], n[1]) for p, n in cur["products"].items()),
                      key=lambda x: (-x[1], -x[2], x[0]))
    return {
        "date": today,
        "total": cur["total"], "ok": cur["ok"],
        "y_total": prev["total"], "y_ok": prev["ok"],
        "long": cur["long"], "short": cur["short"],
        "y_long": prev["long"], "y_short": prev["short"],
        "products": products,
    }


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
            "finished_at", "duration", "output", "error"]
    df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    for c in ("started_at", "finished_at"):     # 展示时去掉微秒尾巴
        if c in df.columns and len(df):
            df[c] = df[c].fillna("").astype(str).str[:19]
    df = df.rename(columns={"started_at": "开始时间", "num": "编号", "product": "品名",
                            "account": "账号", "status": "状态", "finished_at": "结束时间",
                            "duration": "用时(秒)",
                            "output": "输出文件", "error": "错误信息"})
    return df[["开始时间", "编号", "品名", "账号", "状态", "结束时间",
               "用时(秒)", "输出文件", "错误信息"]]


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
