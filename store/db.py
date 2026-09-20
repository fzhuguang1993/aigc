"""
store/db.py —— SQLite 底层访问（线程安全）
"""
import sqlite3
import threading
from pathlib import Path

from core.config import RUNTIME_DIR

DB_PATH = Path(RUNTIME_DIR) / "data" / "aigc.db"
_LOCK = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  num TEXT DEFAULT '', product TEXT DEFAULT '', prompt TEXT DEFAULT '',
  status TEXT DEFAULT '', account TEXT DEFAULT '', job_id TEXT DEFAULT '',
  output TEXT DEFAULT '', url TEXT DEFAULT '',
  runs INTEGER DEFAULT 0, success INTEGER DEFAULT 0, cancels INTEGER DEFAULT 0,
  script_text TEXT DEFAULT '', updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER, num TEXT, product TEXT, account TEXT, job_id TEXT,
  status TEXT, started_at TEXT, finished_at TEXT, output TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT DEFAULT 'product',
  name TEXT NOT NULL,
  images TEXT DEFAULT '', videos TEXT DEFAULT '', audios TEXT DEFAULT '',
  note TEXT DEFAULT '', spec TEXT DEFAULT '', updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS risk_rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT DEFAULT 'platform',      -- platform=平台风控 policy=产品风控
  title TEXT NOT NULL,
  applies TEXT DEFAULT '',            -- 适用产品（空=全部）
  content TEXT DEFAULT '',
  banned TEXT DEFAULT '',             -- 禁用词，逗号分隔
  active INTEGER DEFAULT 1,
  updated_at TEXT DEFAULT ''
);
"""

# 老库升级：列不存在时 ALTER 补上（重复执行安全）
_MIGRATIONS = (
    "ALTER TABLE products ADD COLUMN spec TEXT DEFAULT ''",
)


def _new_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # timeout=30：GUI/命令行两进程同时写库时等待而非直接报错
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


_CONN = _new_conn()


def init():
    with _LOCK:
        _CONN.executescript(_SCHEMA)
        for sql in _MIGRATIONS:
            try:
                _CONN.execute(sql)
            except sqlite3.OperationalError:
                pass    # 列已存在
        _CONN.commit()


def query(sql, args=()):
    with _LOCK:
        return [dict(r) for r in _CONN.execute(sql, args).fetchall()]


def execute(sql, args=()):
    with _LOCK:
        cur = _CONN.execute(sql, args)
        _CONN.commit()
        return cur.lastrowid
