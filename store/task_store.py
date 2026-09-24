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
                  "状态", "时长", "生成用时", "排队用时", "账号", "job_id",
                  "输出", "审核", "URL", "运行次数", "成功次数", "取消次数", "更新时间"]

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


def _iso_to_dt(ts):
    """云端时间戳（`2026-09-23T07:32:39.399710+00:00`）→ datetime；解不开返回 None

    只做差值用，不换算本地时区：两端都来自云端，带的是同一个 UTC 偏移。"""
    s = str(ts or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _span_seconds(start, end):
    """两个云端时间戳相差的秒数（向下取整）；缺任一端/解不开/负数 → 0"""
    a, b = _iso_to_dt(start), _iso_to_dt(end)
    if not a or not b:
        return 0
    try:
        gap = b - a
    except TypeError:                     # 一端带时区一端不带（云端换了写法）
        gap = b.replace(tzinfo=None) - a.replace(tzinfo=None)
    return max(0, int(gap.total_seconds()))


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
    # 派生列：单条视频的「生成用时」与「排队用时」（秒）
    # 优先取最近一次成功执行，没成功过再取最近终态；老记录没拆分（gen_sec=0）
    # 则回退用总用时，至少不给个 0 让人以为生成只用了 0 秒
    durs = {}
    for sql in (
            "SELECT task_id, duration, gen_sec, queued_sec FROM runs "
            "WHERE status='completed' AND duration > 0 "
            "AND id IN (SELECT MAX(id) FROM runs WHERE status='completed' GROUP BY task_id)",
            "SELECT task_id, duration, gen_sec, queued_sec FROM runs "
            "WHERE duration > 0 AND status!='running' "
            "AND id IN (SELECT MAX(id) FROM runs WHERE status!='running' GROUP BY task_id)"):
        for r in db.query(sql):
            g = int(r["gen_sec"] or 0)
            durs.setdefault(r["task_id"], (g or int(r["duration"] or 0),
                                           int(r["queued_sec"] or 0), bool(g)))
    triples = [durs.get(i, (0, 0, False)) for i in df["_id"]]
    df["生成用时"] = [int(t[0]) for t in triples] if len(df) else pd.Series([], dtype=int)
    df["排队用时"] = [int(t[1]) for t in triples] if len(df) else pd.Series([], dtype=int)
    # 辅助列：这条的「生成用时」其实是未拆分的总用时（早期没存云端时间戳）
    # 不标出来就会拿着含排队的数当生成耗时比
    df["用时含排队"] = [(not t[2]) and t[0] > 0 for t in triples] if len(df) \
        else pd.Series([], dtype=bool)
    # 派生列：审片标记。看的是「输出文件」里第一个成品——跟任务中心
    # 那一格显示/播放的是同一个文件，抽卡多条时只标得了当前展示的这条
    marks = marks_by_path()

    def _mark_of(out):
        first = _mark_key(str(out or "").split(";")[0])
        return MARK_LABEL.get(marks.get(first, ""), "")

    df["审核"] = [_mark_of(o) for o in df["输出"]] if len(df) \
        else pd.Series([], dtype=str)
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


def set_prompt_zh(task_id, text):
    """只写「提示词中文对照」

    不进 _CN2DB、也刻意不走 update_row：对照译文只是给人看的附属品，
    跟着 update_row 会把 updated_at / prompt_changed_at 一起刷掉，
    在「迭代执行」判定里就成了“提示词又改了、这条要重跑”。"""
    db.execute("UPDATE tasks SET prompt_zh=? WHERE id=?", (str(text or ""), task_id))


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


# 撤销删除时按原主键回插的完整列（与 tasks 表字段一致）
_RESTORE_COLS = ["id", "num", "product", "prompt", "status", "account", "job_id",
                 "output", "url", "runs", "success", "cancels", "script_text",
                 "updated_at", "duration", "prompt_changed_at", "script",
                 "storyboard", "remark", "prompt_zh"]


def restore_tasks(rows):
    """把删除前抓取的整行按原 id 回插（INSERT OR REPLACE）：主键不变，
    关联的 runs 记录也不会错位。rows 为 get_task() 返回的字典列表。"""
    for r in rows:
        if not r or r.get("id") is None:
            continue
        cols = [c for c in _RESTORE_COLS if c in r]
        db.execute(
            "INSERT OR REPLACE INTO tasks(%s) VALUES(%s)"
            % (",".join(cols), ",".join("?" * len(cols))),
            [r[c] for c in cols])
    return len(rows)


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


def record_run_end(job_id, status, output="", error="", cloud=None):
    """终态刻录：`duration` 是提交→完成的总用时（含云端排队），
    `gen_sec`/`queued_sec` 拆成「真生成」与「排队」两段——数据来自云端回报的
    `submitted_at / started_at / completed_at`（实测三个都有，见 docs/云端接口字段说明.md），
    拿本地轮询时刻估会差一个 POLL_INTERVAL，而单条视频生成本身才 5 分钟量级。"""
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
    # 排队/生成拆分：拿不到云端时间戳就留 0（展示层回退用总用时，不拿轮询时刻猜）
    queued, gen = split_from_cloud(cloud)
    db.execute("UPDATE runs SET status=?, finished_at=?, duration=?, gen_sec=?, "
               "queued_sec=?, output=?, error=? WHERE job_id=? AND finished_at IS NULL",
               (status, now, dur, gen, queued, output, error, job_id))


def split_from_cloud(cloud):
    """云端作业回报 → (排队秒, 生成秒)；字段缺任一端就给 0（当作“不知道”）

    兼容 `{data:{...}}` 包裹（云端接口见过这两种写法，不让拆分因套一层就静默失效）。"""
    c = cloud if isinstance(cloud, dict) else {}
    _TS = ("submitted_at", "created_at", "started_at", "start_time",
           "completed_at", "finished_at", "end_time")
    if not any(k in c for k in _TS):
        for key in ("data", "job", "task"):
            if isinstance(c.get(key), dict):
                c = c[key]
                break
    started = c.get("started_at") or c.get("start_time")
    finished = c.get("completed_at") or c.get("finished_at") or c.get("end_time")
    return (_span_seconds(c.get("submitted_at") or c.get("created_at"), started),
            _span_seconds(started, finished))


def update_run_split(job_id, cloud, only_if_empty=True):
    """用云端回报回填某条执行的「排队/生成」拆分（历史数据回填入口用）

    only_if_empty：已经有数就不覆盖——回填脚本重复跑不会把刚采到的新值刷掉。
    返回是否真的写了这一行（`db.execute` 回的是 lastrowid，所以自己先查再定）。"""
    queued, gen = split_from_cloud(cloud)
    if not (queued or gen):
        return False
    rows = db.query("SELECT gen_sec, queued_sec FROM runs WHERE job_id=?", (job_id,))
    if not rows:
        return False
    if only_if_empty and any(int(r["gen_sec"] or 0) or int(r["queued_sec"] or 0)
                             for r in rows):
        return False
    db.execute("UPDATE runs SET gen_sec=?, queued_sec=? WHERE job_id=?",
               (gen, queued, job_id))
    return True


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


# ---------------- 审片标记（可用 / 不可用） ----------------

MARK_BAD = "bad"
MARK_OK = "ok"
MARK_LABEL = {MARK_BAD: "不可用", MARK_OK: "可用"}
# 标记落在成品文件的【当前路径】上：一个任务可能抽卡出好几条，每条要能
# 单独判，挂在 tasks/runs 上都会串位。重命名时跟着改键。
BAD_SUFFIX = "不可用"          # 文件名上的记号：不动库的人也能一眼认出


def _mark_key(path):
    """标记以绝对路径为键：同一个文件用相对/绝对两种写法不该记两条"""
    s = str(path or "").strip()
    if not s:
        return ""
    try:
        return str(Path(s).resolve())
    except OSError:                     # 路径不存在/含非法字符：退化成原值
        return s


def marks_by_path():
    """{绝对路径: 标记}，给列表派生用（一次查完，不逐行回库）"""
    return {r["path"]: r["mark"]
            for r in db.query("SELECT path, mark FROM file_marks")}


def get_file_mark(path):
    rows = db.query("SELECT mark FROM file_marks WHERE path=?", (_mark_key(path),))
    return rows[0]["mark"] if rows else ""


def set_file_mark(path, mark):
    """标一条成品；mark 传空＝取消标记（整行删掉，库里的空记号没人读）"""
    key = _mark_key(path)
    if not key:
        return False
    if not mark:
        db.execute("DELETE FROM file_marks WHERE path=?", (key,))
        return True
    db.execute("INSERT OR REPLACE INTO file_marks(path, mark, marked_at) VALUES(?,?,?)",
               (key, mark, _now()))
    return True


def move_file_mark(old, new):
    """文件被重命名：标记跟着走

    不跟的话刚标完就变孤儿——列表上看不到了，“一键清理”也找不到它。"""
    o, n = _mark_key(old), _mark_key(new)
    if not o or o == n:
        return
    rows = db.query("SELECT mark, marked_at FROM file_marks WHERE path=?", (o,))
    db.execute("DELETE FROM file_marks WHERE path=?", (o,))
    for r in rows:
        db.execute("INSERT OR REPLACE INTO file_marks(path, mark, marked_at) VALUES(?,?,?)",
                   (n, r["mark"], r["marked_at"]))


def list_file_marks(mark=None):
    if mark:
        return db.query("SELECT path, mark, marked_at FROM file_marks WHERE mark=?"
                        " ORDER BY marked_at DESC", (mark,))
    return db.query("SELECT path, mark, marked_at FROM file_marks ORDER BY marked_at DESC")


def _split_outputs(s):
    """tasks.output / runs.output 存的是「; 」拼接的多产物路径"""
    return [p.strip() for p in str(s or "").split(";") if p.strip()]


def _is_same_file(a, b):
    """同一个文件的两种写法（相对/绝对、斜杠方向）也算同一个"""
    x, y = _mark_key(a), _mark_key(b)
    return bool(x) and x == y


def replace_output_path(old, new):
    """文件重命名后同步【任务表与执行记录】里的路径，不然双击就报「文件不存在」

    逐段比对而不是整串字符串替换：一个路径是另一个的前缀时（_01 与 _012）
    整串替换会把不相干的那条也改坏。返回改了多少行。"""
    hits = 0
    for table in ("tasks", "runs"):
        for r in db.query(f"SELECT id, output FROM {table}"
                          f" WHERE output IS NOT NULL AND output != ''"):
            segs = _split_outputs(r["output"])
            if not any(_is_same_file(s, old) for s in segs):
                continue
            segs = [new if _is_same_file(s, old) else s for s in segs]
            db.execute(f"UPDATE {table} SET output=? WHERE id=?",
                       ("; ".join(segs), r["id"]))
            hits += 1
    return hits


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
          # 平均生成时长只算 `gen_sec`；没拆分的老记录回退用总用时（偶尔偏大一点，
          # 也不要把它们从平均值里隐掉——那样看上去反而像“只跑了新的几条”）
          " AVG(CASE WHEN status='completed' THEN COALESCE(NULLIF(gen_sec,0), duration) END) dur,"
          " AVG(CASE WHEN status='completed' THEN NULLIF(queued_sec,0) END) qdur"
          " FROM runs WHERE substr(started_at,1,10)")


def _sum_row(rows):
    r = rows[0] if rows else {}
    out = {k: int(r.get(k) or 0) for k in ("total", "ok", "fail", "cancel")}
    out["avg_dur"] = round(float(r.get("dur") or 0), 1)     # 成功执行平均生成用时（秒）
    out["avg_queued"] = round(float(r.get("qdur") or 0), 1)  # 平均排队（秒），老记录多未采
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


def daily_report_stats(now_hm=None):
    """日汇报（今日截至目前 vs 昨日同时段）：总量/成功 + 分产品 + 视频时长分桶 + 审片标记。

    环比按「昨日同时段」：只统计昨天开钟点不晚于现在这一刻的执行。
    不然 14:01 生成的汇报拿今日半天数据去比昨天一整天，必然误报“运行 ▼73%”——
    人家只是还没跑到下午。测试/回放可显式传 now_hm="HH:MM" 钉死截止点。

    时长桶只计成功（status=completed）：失败的跑次根本没有成品，
    拿它们的任务时长进桶会虚增条数。时长用任务的 tasks.duration（视频秒数）；
    runs.duration 是执行耗时，不能用。时长未知（<=0）只计总量、不落进两个时长桶。
    占比分母＝长+短（有明确时长且成功的那些）。
    """
    from collections import defaultdict
    from datetime import date, datetime, timedelta
    today = date.today().isoformat()
    yest = (date.today() - timedelta(days=1)).isoformat()
    if now_hm is None:
        now_hm = datetime.now().strftime("%H:%M")
    rows = db.query(
        "SELECT substr(r.started_at,1,10) d, substr(r.started_at,12,5) hm,"
        " r.product product, r.status status,"
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
        if r["d"] == yest and str(r["hm"] or "") > now_hm:
            continue        # 昨日同时段口径：现在还没跑到的钟点，昨天那截不算
        a["total"] += 1
        ok = r["status"] == "completed"
        if ok:
            a["ok"] += 1
        try:
            vdur = int(r["vdur"] or 0)
        except (TypeError, ValueError):
            vdur = 0
        if ok:                          # 失败的没成品可看，不进时长桶
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
    # 审片「可用」是落在成品文件上的累计标记（不随日汇报归零）：
    # 今天能交差多少条，看的是这个数而不是成功数
    avail = db.query("SELECT COUNT(*) c FROM file_marks WHERE mark=?",
                     (MARK_OK,))[0]["c"]
    return {
        "date": today,
        "total": cur["total"], "ok": cur["ok"],
        "y_total": prev["total"], "y_ok": prev["ok"],
        "long": cur["long"], "short": cur["short"],
        "y_long": prev["long"], "y_short": prev["short"],
        "products": products,
        "avail": int(avail or 0),
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
            "finished_at", "duration", "gen_sec", "queued_sec", "output", "error"]
    df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    for c in ("started_at", "finished_at"):     # 展示时去掉微秒尾巴
        if c in df.columns and len(df):
            df[c] = df[c].fillna("").astype(str).str[:19]
    df = df.rename(columns={"started_at": "开始时间", "num": "编号", "product": "品名",
                            "account": "账号", "status": "状态", "finished_at": "结束时间",
                            "duration": "总用时(秒)", "gen_sec": "生成用时(秒)",
                            "queued_sec": "排队用时(秒)",
                            "output": "输出文件", "error": "错误信息"})
    return df[["开始时间", "编号", "品名", "账号", "状态", "结束时间",
               "总用时(秒)", "生成用时(秒)", "排队用时(秒)", "输出文件", "错误信息"]]


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
