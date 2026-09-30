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
  prompt_zh TEXT DEFAULT '',
  demo INTEGER DEFAULT 0
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
-- 一键发布历史：一次「视频 × 平台账号」落一行（独立于任务/执行体系）。
-- ok=1 成功，post_url 是平台回执的作品链接；失败行 ok=0 且 message 记人话原因。
CREATE TABLE IF NOT EXISTS publishes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT DEFAULT '', account TEXT DEFAULT '', title TEXT DEFAULT '',
  video_path TEXT DEFAULT '', ok INTEGER DEFAULT 0,
  post_id TEXT DEFAULT '', post_url TEXT DEFAULT '', message TEXT DEFAULT '',
  published_at TEXT DEFAULT '', created_at TEXT DEFAULT ''
);
-- 爆款拆解任务库：一次拆解落一行（独立于生成任务/发布体系）。
-- payload 存 BreakdownResult.to_dict() 的 JSON，详情页三屏联动据此离线重建；
-- cover/gallery_dir/report 指向持久图库目录（BREAKDOWN_LIBRARY），删行不删图。
CREATE TABLE IF NOT EXISTS breakdown_tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT DEFAULT '', video_path TEXT DEFAULT '', link TEXT DEFAULT '',
  duration REAL DEFAULT 0, shot_count INTEGER DEFAULT 0,
  cover TEXT DEFAULT '', gallery_dir TEXT DEFAULT '', report TEXT DEFAULT '',
  status TEXT DEFAULT 'ok',         -- ok=完整 partial=半成品
  payload TEXT DEFAULT '',          -- BreakdownResult.to_dict() 的 JSON
  link_key TEXT DEFAULT '',         -- 规范化主链 sha1[:16]，同链接去重/断点命中用
  created_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
);
-- ⚠ link_key 的索引不在这里建：老库的 breakdown_tasks 没有 link_key 列，executescript
-- 会在 ALTER 之前先跑这里的 CREATE INDEX 而整段报错；索引改由 _MIGRATIONS（ALTER 补列
-- 之后）建，新库/老库都幂等安全。
-- 拆解任务 ↔ 产品 多对多关联（不同产品打法不同，按产品沉淀爆款拆解样本）。
-- 只存 id 引用，产品改名/删除不级联（关联自然失效，重开拆解不补）。
CREATE TABLE IF NOT EXISTS breakdown_task_products(
  task_id INTEGER NOT NULL,
  product_id INTEGER NOT NULL,
  PRIMARY KEY(task_id, product_id)
);
-- 素材库索引：一条拆解的板块（或手动区间）ffmpeg 切出的片段，一行一个。
-- path 存「存储侧绝对/可视路径」（本地即素材库根下真实路径）；backend 记录落盘后端
-- （本轮恒 local，SMB/OSS 落地后据此区分）。block_type/product 供分组筛选。
CREATE TABLE IF NOT EXISTS material_clips(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT DEFAULT '', backend TEXT DEFAULT 'local',
  block_type TEXT DEFAULT '', product TEXT DEFAULT '',
  source_task_id INTEGER DEFAULT 0,
  start REAL DEFAULT 0, end REAL DEFAULT 0, duration REAL DEFAULT 0,
  note TEXT DEFAULT '', created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_material_block_type ON material_clips(block_type);
CREATE INDEX IF NOT EXISTS idx_material_product ON material_clips(product);
-- 成品库 ↔ 产品 手动绑定：成品库是「扫盘视图」（不建物理表，永远与磁盘一致），
-- 但「右键绑定产品」这类人工归属必须落库。以归一化绝对路径为主键存一行，
-- 优先级高于自动 join（runs/tasks 里带出的产品）；解绑即删行。
-- product 冗余存名（产品改名/删除不级联，展示仍稳），product_id 仅作溯源快捷。
CREATE TABLE IF NOT EXISTS output_binds(
  path TEXT PRIMARY KEY,
  product TEXT DEFAULT '', product_id INTEGER DEFAULT 0,
  updated_at TEXT DEFAULT ''
);
-- 软件内回收站：成品「移入回收站」是把文件物理移到 scan_root 下的专用目录（不进系统回收站），
-- 一行记一条回收站里的文件：path=当前在回收站目录的真实路径，origin_path=移入前的完整原路径
-- （还原时移回 origin_path.parent，文件名撞车则重排）。彻底删除才真 unlink 并删这行。
CREATE TABLE IF NOT EXISTS output_trash(
  path TEXT PRIMARY KEY,
  origin_path TEXT DEFAULT '',
  name TEXT DEFAULT '',
  size INTEGER DEFAULT 0,
  trashed_at TEXT DEFAULT ''
);
-- 成品外显名称（别名）：卡片底部默认显示「文件名去后缀」，可批量自定义；只存这一层显示用的名字，
-- 绝不碰物理文件名。以归一化绝对路径为主键（与绑定/标记同口径，改名/还原随路径迁移）。
CREATE TABLE IF NOT EXISTS output_display(
  path TEXT PRIMARY KEY,
  display TEXT DEFAULT '',
  updated_at TEXT DEFAULT ''
);
-- 批量基建（抖音本地推搭计划）执行历史：一次「方案 × 账户」的搭建结果存一行。
-- 只存回执与原因，真实创建结果以平台为准；CREATE TABLE IF NOT EXISTS 对新老库
-- 都幂等，不进 _MIGRATIONS。
CREATE TABLE IF NOT EXISTS local_build_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT DEFAULT '', account TEXT DEFAULT '', advertiser_id TEXT DEFAULT '',
  plan_name TEXT DEFAULT '', ok INTEGER DEFAULT 0,
  project_id TEXT DEFAULT '', marketing_id TEXT DEFAULT '', asset_count INTEGER DEFAULT 0,
  message TEXT DEFAULT '', created_at TEXT DEFAULT '',
  customer_id INTEGER DEFAULT 0, license_id INTEGER DEFAULT 0, account_id INTEGER DEFAULT 0
);
-- 批量基建·三级组织：客户 → 执照/主体 → 本地推账户（严格树，一个账户只归一个执照）。
-- 每层都带 owner（负责成员名，对齐 org_store 的「成员名」值域）：账户为空则就近继承
-- 执照、再继承客户，实现「层级授权 + 逐个授权」混合。owner 空且 org 启用＝仅 admin 可见。
-- 凭证沿用 config 的 encrypt_value(盐=USER_NAME) 存 secret_enc，盘上不留明文。
CREATE TABLE IF NOT EXISTS local_customers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, owner TEXT DEFAULT '', remark TEXT DEFAULT '',
  created_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS local_licenses(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER DEFAULT 0, name TEXT NOT NULL,
  subject TEXT DEFAULT '',      -- 营业执照主体名/统一社会信用代码
  owner TEXT DEFAULT '', remark TEXT DEFAULT '',
  created_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS local_ad_accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  license_id INTEGER DEFAULT 0,
  platform TEXT DEFAULT 'douyin', label TEXT DEFAULT '',
  advertiser_id TEXT DEFAULT '', auth_type TEXT DEFAULT 'oauth',
  secret_enc TEXT DEFAULT '', owner TEXT DEFAULT '', extra TEXT DEFAULT '{}',
  created_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
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
    # 演示数据标记：打包给新用户播种的看板样例任务 demo=1，真实任务恒为 0；
    # 「清除演示数据」只删除 demo=1 的行，绝不碰真实数据。
    "ALTER TABLE tasks ADD COLUMN demo INTEGER DEFAULT 0",
    # 爆款拆解按链接去重：老库补 link_key 列 + 索引（重复执行安全）。
    "ALTER TABLE breakdown_tasks ADD COLUMN link_key TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_breakdown_link_key ON breakdown_tasks(link_key)",
    # 素材库片段索引表（新库 _SCHEMA 已建；老库补建，CREATE TABLE IF NOT EXISTS 幂等）。
    "CREATE INDEX IF NOT EXISTS idx_material_block_type ON material_clips(block_type)",
    "CREATE INDEX IF NOT EXISTS idx_material_product ON material_clips(product)",
    # 批量基建三级组织：老库的 local_build_runs 补三个组织维度列（新库 _SCHEMA 已带，
    # ALTER 重复执行安全）；三张组织表 CREATE TABLE IF NOT EXISTS 在 _SCHEMA 里幂等建。
    "ALTER TABLE local_build_runs ADD COLUMN customer_id INTEGER DEFAULT 0",
    "ALTER TABLE local_build_runs ADD COLUMN license_id INTEGER DEFAULT 0",
    "ALTER TABLE local_build_runs ADD COLUMN account_id INTEGER DEFAULT 0",
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
