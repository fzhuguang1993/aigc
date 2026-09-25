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
  script_text TEXT DEFAULT '', updated_at TEXT DEFAULT '',
  duration INTEGER DEFAULT 0, prompt_changed_at TEXT DEFAULT '',
  script TEXT DEFAULT '', storyboard INTEGER DEFAULT 0,
  remark TEXT DEFAULT '',
  tag TEXT DEFAULT '',
  prompt_zh TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER, num TEXT, product TEXT, account TEXT, job_id TEXT,
  status TEXT, started_at TEXT, finished_at TEXT, output TEXT, error TEXT,
  duration INTEGER DEFAULT 0, gen_sec INTEGER DEFAULT 0, queued_sec INTEGER DEFAULT 0
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
-- 审片标记：以「成品文件当前路径」为键（一个任务可能抽卡多条，
-- 每条要能单独标可用/不可用，挂在 tasks/runs 上都会串位）。
-- 重命名时跟着改键（见 processors/output_mark.py）。
CREATE TABLE IF NOT EXISTS file_marks(
  path TEXT PRIMARY KEY,
  mark TEXT DEFAULT '',               -- bad=不可用 ok=可用 ''=没标
  marked_at TEXT DEFAULT ''
);
-- 组织结构（分部门分角色看数）：成员/部门/线路归属三张表。
-- 数据归属走“线路→成员”映射：runs/tasks 的 account 列就是线路名，
-- 历史数据零改动即可按权限切分；密码存 pbkdf2 盐哈希，不存明文。
CREATE TABLE IF NOT EXISTS org_members(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  pass_hash TEXT DEFAULT '',
  role TEXT DEFAULT 'member',          -- admin=看全部+管组织 manager=看本部门 member=看自己线路
  dept TEXT DEFAULT '',
  active INTEGER DEFAULT 1,
  created_at TEXT DEFAULT '', last_login TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS org_depts(
  name TEXT PRIMARY KEY, created_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS org_line_owners(
  account TEXT PRIMARY KEY,            -- 线路名（与 runs.account 同值域）
  owner TEXT DEFAULT '',               -- 归属成员名；空=未绑定（只 admin 可见）
  updated_at TEXT DEFAULT ''
);
"""

# 老库升级：列不存在时 ALTER 补上（重复执行安全）
_MIGRATIONS = (
    "ALTER TABLE products ADD COLUMN spec TEXT DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN duration INTEGER DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN prompt_changed_at TEXT DEFAULT ''",
    "ALTER TABLE runs ADD COLUMN duration INTEGER DEFAULT 0",
    # 总用时里混着云端排队：把「真生成」与「排队」拆成两列单独存（云端回报的时间戳算出）
    "ALTER TABLE runs ADD COLUMN gen_sec INTEGER DEFAULT 0",
    "ALTER TABLE runs ADD COLUMN queued_sec INTEGER DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN script TEXT DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN storyboard INTEGER DEFAULT 0",
    # 备注：使用者自己标的管理记号（“已过审”“待重拍”…），不参与业务逻辑
    "ALTER TABLE tasks ADD COLUMN remark TEXT DEFAULT ''",
    # 提示词中文对照：提示词很多是英文写的，机翻一份中文只给人看，
    # 不参与提交（提交永远用原文），所以单独开一列，不覆盖 prompt。
    "ALTER TABLE tasks ADD COLUMN prompt_zh TEXT DEFAULT ''",
    # 内容标签：给任务归一个内容类型（开场钩子/活动促销/情景剧…），
    # 词库在设置里维护；批量归档时就按它把成品归进「日期/产品/标签」子目录。
    "ALTER TABLE tasks ADD COLUMN tag TEXT DEFAULT ''",
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


def write_revision():
    """本连接的写入累计行数（sqlite3 内存计数，零查询成本）。

    UI 定时刷新用它感知「同秒内的多次写入」：时间戳类签名只能分辨到秒，
    同一秒里接连两次写入会被误判成「没变过」，界面就不刷新了。"""
    return _CONN.total_changes


def query(sql, args=()):
    with _LOCK:
        return [dict(r) for r in _CONN.execute(sql, args).fetchall()]


def execute(sql, args=()):
    with _LOCK:
        cur = _CONN.execute(sql, args)
        _CONN.commit()
        return cur.lastrowid
