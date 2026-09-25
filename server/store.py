"""server/store.py —— 服务端 SQLite 存储（卡密库 / 审计 / 任务映射 / 封禁）

口径与客户端 store/db.py 一致：标准库 sqlite3、WAL、进程内一把写锁。
初期单机几十 QPS 用不到真数据库；上量后同构迁 PostgreSQL。
"""
import os
import sqlite3
import threading
import time

from server import config

_LOCK = threading.Lock()
_CONN = None


def conn():
    """懒建连接（测试会把 config.DB_PATH 指到临时文件后调 reset()）"""
    global _CONN
    if _CONN is None:
        _CONN = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA journal_mode=WAL")
        _CONN.execute("PRAGMA busy_timeout=5000")
    return _CONN


def reset():
    """换库重建（仅测试/运维热切路径用）"""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
        _CONN = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    card_key     TEXT PRIMARY KEY,      -- 归一化后的 XXXX-XXXX-XXXX-XXXX
    days         INTEGER NOT NULL,      -- 卡密时长（天）
    status       TEXT NOT NULL DEFAULT 'unused',   -- unused/used/disabled
    machine_code TEXT NOT NULL DEFAULT '',
    activated_at INTEGER NOT NULL DEFAULT 0,
    expire_at    INTEGER NOT NULL DEFAULT 0,       -- 到期时间戳（续费叠加后移）
    created_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS activations_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    card_key     TEXT, machine_code TEXT, ip TEXT, app_version TEXT,
    result       TEXT,                                 -- ok / 拒绝原因
    ts           INTEGER
);
CREATE TABLE IF NOT EXISTS jobs (
    gw_job_id    TEXT PRIMARY KEY,      -- 网关对外 job_id，上游互不可见
    upstream     TEXT NOT NULL,
    real_job_id  TEXT NOT NULL,
    machine_code TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT '',
    terminal     INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_upstream ON jobs(upstream, terminal);
CREATE INDEX IF NOT EXISTS idx_jobs_real ON jobs(upstream, real_job_id);
CREATE TABLE IF NOT EXISTS assets (
    gw_asset_id  TEXT PRIMARY KEY,      -- 参考图先落网关暂存，提交时才转投线路
    path         TEXT NOT NULL,
    filename     TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT 'image',
    machine_code TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS banned_machines (
    machine_code TEXT PRIMARY KEY,
    reason       TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL
);
"""


def init_db():
    with _LOCK:
        c = conn()
        c.executescript(SCHEMA)
        c.commit()


def now():
    return int(time.time())


# ---------------- 卡密 ----------------

def insert_cards(rows):
    """rows: [(card_key, days)]，已存在的跳过（重复生成同一张卡不算错误）"""
    with _LOCK:
        c = conn()
        n = 0
        for key, days in rows:
            try:
                c.execute("INSERT INTO cards(card_key, days, created_at) VALUES(?,?,?)",
                          (key, int(days), now()))
                n += 1
            except sqlite3.IntegrityError:
                pass
        c.commit()
        return n


def get_card(card_key):
    row = conn().execute("SELECT * FROM cards WHERE card_key=?", (card_key,)).fetchone()
    return dict(row) if row else None


def use_card(card_key, machine_code, days, base_ts):
    """未用卡激活：绑机、起算到期。expire 从 base_ts 叠加（续费传旧到期时间）。"""
    ts = now()
    expire = base_ts + days * 86400
    with _LOCK:
        c = conn()
        cur = c.execute(
            "UPDATE cards SET status='used', machine_code=?, activated_at=?, expire_at=? "
            "WHERE card_key=? AND status='unused'",
            (machine_code, ts, expire, card_key))
        c.commit()
        if cur.rowcount != 1:
            return None                    # 并发下被别人先激活了
    return expire


def renew_card(card_key, days):
    """同机续费：到期时间叠加（已过期从当前时间起算），返回新 expire_at"""
    ts = now()
    with _LOCK:
        c = conn()
        row = get_card(card_key)
        if not row:
            return None
        base = max(row["expire_at"], ts)
        expire = base + days * 86400
        c.execute("UPDATE cards SET expire_at=? WHERE card_key=?", (expire, card_key))
        c.commit()
    return expire


def set_card_status(card_key, status):
    with _LOCK:
        c = conn()
        c.execute("UPDATE cards SET status=? WHERE card_key=?", (status, card_key))
        c.commit()


def list_cards(status=None, limit=500):
    sql, args = "SELECT * FROM cards", []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def count_cards(status=None):
    sql, args = "SELECT COUNT(*) c FROM cards", []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    return conn().execute(sql, args).fetchone()["c"]


def machine_expire(machine_code):
    """该机器当前最晚到期时间（续费时新卡在这上面叠加；无记录返回 0）"""
    row = conn().execute(
        "SELECT MAX(expire_at) m FROM cards WHERE machine_code=? AND status='used'",
        (machine_code,)).fetchone()
    return int(row["m"] or 0)


# ---------------- 审计 ----------------

def log_activation(card_key, machine_code, ip, app_version, result):
    with _LOCK:
        c = conn()
        c.execute("INSERT INTO activations_log(card_key, machine_code, ip, "
                  "app_version, result, ts) VALUES(?,?,?,?,?,?)",
                  (card_key, machine_code, ip, app_version, result, now()))
        c.commit()


def recent_failures(card_key, window):
    """该卡密最近 window 秒内被拒次数——暴力试卡的熔断依据（成功一次清零）。"""
    row = conn().execute(
        "SELECT COUNT(*) c FROM activations_log WHERE card_key=? AND result!='ok' "
        "AND ts>?", (card_key, now() - window)).fetchone()
    return row["c"]


# ---------------- 任务映射 ----------------

def add_job(gw_job_id, upstream, real_job_id, machine_code):
    with _LOCK:
        c = conn()
        c.execute("INSERT INTO jobs(gw_job_id, upstream, real_job_id, machine_code, "
                  "created_at) VALUES(?,?,?,?,?)",
                  (gw_job_id, upstream, real_job_id, machine_code, now()))
        c.commit()


def get_job(gw_job_id):
    row = conn().execute("SELECT * FROM jobs WHERE gw_job_id=?", (gw_job_id,)).fetchone()
    return dict(row) if row else None


def find_job_by_real(upstream, real_job_id):
    row = conn().execute("SELECT * FROM jobs WHERE upstream=? AND real_job_id=?",
                         (upstream, real_job_id)).fetchone()
    return dict(row) if row else None


def update_job(gw_job_id, status=None, terminal=None):
    sets, args = [], []
    if status is not None:
        sets.append("status=?")
        args.append(status)
    if terminal is not None:
        sets.append("terminal=?")
        args.append(1 if terminal else 0)
    if not sets:
        return
    args.append(gw_job_id)
    with _LOCK:
        c = conn()
        c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE gw_job_id=?", args)
        c.commit()


def jobs_today():
    row = conn().execute("SELECT COUNT(*) c FROM jobs WHERE created_at>?",
                         (now() - now() % 86400,)).fetchone()
    return row["c"]


# ---------------- 参考图暂存 ----------------

def add_asset(gw_asset_id, path, filename, content_type, machine_code):
    with _LOCK:
        c = conn()
        c.execute("INSERT INTO assets(gw_asset_id, path, filename, content_type, "
                  "machine_code, created_at) VALUES(?,?,?,?,?,?)",
                  (gw_asset_id, path, filename, content_type, machine_code, now()))
        c.commit()


def get_asset(gw_asset_id):
    row = conn().execute("SELECT * FROM assets WHERE gw_asset_id=?",
                         (gw_asset_id,)).fetchone()
    return dict(row) if row else None


def pop_expired_assets(ttl):
    """清理超时暂存文件，返回删除条数"""
    cutoff = now() - ttl
    rows = conn().execute("SELECT gw_asset_id, path FROM assets WHERE created_at<?",
                          (cutoff,)).fetchall()
    removed = 0
    with _LOCK:
        c = conn()
        for r in rows:
            try:
                os.remove(r["path"])
            except OSError:
                pass
            c.execute("DELETE FROM assets WHERE gw_asset_id=?", (r["gw_asset_id"],))
            removed += 1
        c.commit()
    return removed


# ---------------- 封禁 ----------------

def ban_machine(machine_code, reason=""):
    with _LOCK:
        c = conn()
        c.execute("INSERT OR REPLACE INTO banned_machines(machine_code, reason, "
                  "created_at) VALUES(?,?,?)", (machine_code, reason, now()))
        c.commit()


def unban_machine(machine_code):
    with _LOCK:
        c = conn()
        c.execute("DELETE FROM banned_machines WHERE machine_code=?", (machine_code,))
        c.commit()


def is_banned(machine_code):
    return conn().execute("SELECT 1 FROM banned_machines WHERE machine_code=?",
                          (machine_code,)).fetchone() is not None
