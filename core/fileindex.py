"""
core/fileindex.py —— 本地文件索引：全盘文件名 + 文档正文（Everything 式搜索的地基）

为什么要索引层：唤出面板要能像 Everything 那样敲两个字就出文件。直接 os.walk
现扫现答不现实（本机实测全盘 95 万文件），所以启动时在后台把目录树落进一份
独立 SQLite，搜索时只查库。

三个关键决定都有实测依据，别当理所当然：

1. **排除清单决定成败**。只排 Windows/回收站 → 172.6 万文件 / 74.7 秒；再排
   AppData、ProgramData、Temp、assembly 这些 → 95.1 万文件 / **7.8 秒**。差十倍
   全在 AppData 里那 21 万个 .py 和 16 万个 .pyc——没人会去搜它们。
2. **中文全文必须自己切词**。内置 SQLite 的 FTS5 有，但默认 unicode61 分词器
   把「关节不舒服」整串当一个词，`MATCH '关节'` 命中 0；trigram 分词器也搜不到
   两个字的词（它要求 ≥3 字符）。所以正文入库前先在 Python 里切成二元组，
   查询时同样切，再用短语匹配还原「连续子串」语义（实测全中）。
3. **增量不能靠"父目录没改就不进"**。子目录里新增文件不会刷新父目录的 mtime，
   照父目录签名剪掉整棵子树就会漏。所以全盘照走（7.8 秒本来就不贵），只对
   「签名变了」的目录做数据库差分。签名是 **目录自己的 mtime + 子项数**两条：
   只看 mtime 会漏掉"同一秒里加一个又删一个"（实测 removed 报 0）——子项数
   一跳，差分必然重跑，一个坏文件也不能把一轮扫描带偏。

不 import 任何 Qt：这层要能脱离界面单测，也要能在后台线程里裸跑。
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from core.config import RUNTIME_DIR

#: 索引库单独一个文件，绝不塞进 aigc.db：它随时可以整库删掉重扫，
#: 连累业务库（历史任务/看板）是绝对不能接受的。
DB_PATH = Path(RUNTIME_DIR) / "data" / "file_index.db"

#: 结构版本：改了表结构/切词口径就 +1，旧库自动整库重建（索引没有保留价值）
#: 1→2：dirs 的增量签名从「只看 mtime」改成「mtime + 子项数」，旧库的 mark 全是
#: 补出来的 0，留着会把正常目录误判成没变，索性整个作废重扫一遍。
INDEX_VERSION = "2"

#: 剪掉整棵子树的目录名（小写比对）。依据见模块头第 1 条。
EXCLUDE_DIRS = {
    "$recycle.bin", "system volume information", "windows", "winsxs",
    "programdata", "appdata", "assembly", "temp", "installer",
    "softwaredistribution", "recovery", "perflogs", "node_modules", ".git",
    ".venv", "venv", "__pycache__", "$windows.~bt", "config.msi",
    "windows.old", "microsoft", "searchplugins", "code store",
}

#: 会抽正文的扩展名。docx/pptx/xlsx 都是 zip+xml，纯 stdlib + 已有 openpyxl 就够；
#: pdf 走 pypdf（懒导入，没装就跳过 pdf，不硬失败）。老版二进制 Office
#: （.doc/.xls/.ppt）纯 stdlib 抽不出来，只进文件名索引。
DOC_EXTS = {".txt", ".md", ".docx", ".pptx", ".xlsx", ".pdf"}

#: 单文件超过这个大小就不抽正文（一个 500MB 的 PPT 里多半是图，抽了也拖死索引）
DOC_MAX_BYTES = 20 * 1024 * 1024
#: 正文最多存这么多字符。不封顶的话一个 100KB 的 txt 连切词能撑出 300KB 索引，
#: 两万个文档就是几个 GB——上限既是体积闸门也是"没人会读那么长"的实用取舍。
DOC_BODY_KEEP = 20_000
#: 文件名一次最多取多少候选再排序。LIKE 全扫没有质量可言，取够多才排得准：
#: 2000 候选实测仍是几十毫秒量级，而 400 个候选碰上热门字会排不出好结果。
FILE_CANDIDATES = 2000
#: PDF 最多抽前多少页（一本 300 页手册的正文没全存的必要，命中前 80 页够用）
PDF_MAX_PAGES = 80
#: PPT 最多抽前多少页（与上面同一取舍：一本 200 页的路演稿，正文都在头几十页）
PPTX_MAX_SLIDES = 80

# ---------- 偏好键（存在 app_state 里，与快捷键等设置同一份事实源） ----------
K_ENABLED = "filesearch_enabled"        # 文件名搜索总开关（默认开）
K_ROOTS = "filesearch_roots"            # 索引范围；空＝所有本地固定盘
K_DOC_ON = "filesearch_doc_enabled"     # 内容搜索开关（默认开）
K_DOC_ROOTS = "filesearch_doc_roots"    # 抽正文的目录；空＝默认文档目录
K_DOC_MAX_MB = "filesearch_doc_max_mb"  # 单文件上限（MB）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY,
  dir  TEXT NOT NULL,
  name TEXT NOT NULL,
  ext  TEXT DEFAULT '',
  size INTEGER DEFAULT 0,
  mtime REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_dir ON files(dir);
-- 每个目录自己的 mtime + 子项数 + 本轮是否见过：mtime+mark 用来跳过差分，
-- seen 用来清理「整个目录已被删掉」的残留行（这类目录本轮不会出现在走访里）。
-- 为什么除了 mtime 还要记子项数：Windows 上父目录的 mtime 粒度不够细，
-- 同一秒里“加一个又删一个”时间戳能一模一样，只看 mtime 就把这轮差分跳过了。
CREATE TABLE IF NOT EXISTS dirs(
  path TEXT PRIMARY KEY,
  mtime REAL DEFAULT 0,
  mark INTEGER DEFAULT 0,
  seen INTEGER DEFAULT 0
);
-- 文档登记表：id 显式当 FTS 的 rowid 用。为什么非要这层——FTS5 里按普通列
-- DELETE（WHERE path=?）是整表扫，重抽一批文档就是几十次全表扫；按 rowid 删
-- 才是 O(log n)。
CREATE TABLE IF NOT EXISTS docs_meta(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE, mtime REAL DEFAULT 0, size INTEGER DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
  toks, path UNINDEXED, body UNINDEXED
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT DEFAULT '');
"""

_LOCK = threading.RLock()          # 写库串行：扫描线程与查询线程共用一份库
_TLS = threading.local()           # 每线程一条连接（sqlite 连接不能跨线程用）


# ==================== 偏好 ====================
def _pref(key, default):
    """读偏好。懒 import：core 层平时不依赖 store，只有问偏好时才借一次。

    为什么不把偏好存进索引库的 meta 表：那会变成「设置页读 ui_state.json、
    索引读 file_index.db」两份事实源，而且「重建索引」顺手就把用户配置清了。"""
    try:
        from store import app_state
        v = app_state.get(key, default)
        return default if v is None else v
    except Exception:
        return default


def _set_pref(key, value):
    try:
        from store import app_state
        return app_state.set_value(key, value)
    except Exception:
        return False


def enabled():
    return bool(_pref(K_ENABLED, True))


def set_enabled(on):
    return _set_pref(K_ENABLED, bool(on))


def doc_enabled():
    return bool(_pref(K_DOC_ON, True))


def set_doc_enabled(on):
    return _set_pref(K_DOC_ON, bool(on))


def doc_max_bytes():
    try:
        mb = float(_pref(K_DOC_MAX_MB, DOC_MAX_BYTES / 1024 / 1024))
    except (TypeError, ValueError):
        mb = DOC_MAX_BYTES / 1024 / 1024
    return max(int(mb), 1) * 1024 * 1024


def set_doc_max_mb(mb):
    try:
        mb = int(float(mb))
    except (TypeError, ValueError):
        mb = 20
    return _set_pref(K_DOC_MAX_MB, max(mb, 1))


def _split_roots(raw):
    return [s.strip() for s in str(raw or "").split(os.pathsep) if s.strip()]


def custom_roots():
    """用户手动指定的索引范围（空＝用 local_drives() 的全盘口径）"""
    return _split_roots(_pref(K_ROOTS, ""))


def set_custom_roots(paths):
    return _set_pref(K_ROOTS, os.pathsep.join(str(p) for p in (paths or [])))


def default_doc_roots():
    """内容索引的默认范围：只吃「用户真会写文档的地方」。

    实测全盘 1.9 万个可抽文档里 1.5 万个是软件自带的 txt（Program Files 里），
    把它们全抽一遍既慢又搜不到有用东西，还白占几百 MB 库。"""
    home = Path.home()
    out = [home / "Documents", home / "Downloads", home / "Desktop"]
    for sub in ("material", "outputs", "exports"):
        out.append(Path(RUNTIME_DIR) / sub)
    repo = Path(__file__).resolve().parents[1]
    if str(repo) != str(RUNTIME_DIR):
        out.append(repo / "docs")
    return [str(p) for p in out if p.is_dir()]


def doc_roots():
    return _split_roots(_pref(K_DOC_ROOTS, "")) or default_doc_roots()


def set_doc_roots(paths):
    return _set_pref(K_DOC_ROOTS, os.pathsep.join(str(p) for p in (paths or [])))


def search_roots():
    """文件名索引要走的根：设置里指定了就用指定的，否则所有本地固定盘"""
    return custom_roots() or local_drives()


# ==================== 切词与匹配 ====================
def normalize(text):
    """剥空白 + 转小写：写入与查询必须走同一个函数，口径差一点就搜不到"""
    return "".join(str(text or "").split()).lower()


def bigram(text):
    """切成二元组（空格分隔）。少于 2 字原样返回（交给调用方兜底）。

    实测：内置分词器对中文「关节」这种两字词命中 0，切完二元组才能搜；
    单字留给 LIKE 兜底（切好的表里没有单字 token）。"""
    s = normalize(text)
    if len(s) < 2:
        return s
    return " ".join(s[i:i + 2] for i in range(len(s) - 1))


def match_score(query, name, desc):
    """返回排序分（越小越靠前），不匹配返回 None。

    0＝名称前缀　1＝名称包含　2＝名称跳字（子序列）　3＝简介/目录包含

    从 gui/dialogs_launcher.py 搬来：工具名和文件名要用同一套排序口径，
    两边各写一份迟早会漂（一边支持跳字一边不支持，用户就会问为什么）。"""
    q = (query or "").strip().lower()
    if not q:
        return 0
    n = (name or "").lower()
    if n.startswith(q):
        return 0
    if q in n:
        return 1
    it = iter(q)
    ch = next(it, None)
    for c in n:
        if c == ch:
            ch = next(it, None)
            if ch is None:
                return 2
    if q in (desc or "").lower():
        return 3
    return None


def _like(text):
    """转成 LIKE 的参数：转义 \\ % _，否则用户打的 _ 会变成通配符"""
    s = str(text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + s + "%"


def _like_prefix(text):
    """前缀 LIKE 参数（转义同 _like，只是开头不留 %）：候选池排序用"""
    s = str(text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return s + "%"


def _file_rank(query, name, dirpath):
    """文件排序分：越小越靠前，None=不匹配。

    在 match_score 的四个粗档（前缀/包含/跳字/目录）里再细分：名字几乎
    就等于关键词的（去后缀等于 q、或短前缀）必须压过“简历正式版”“简历(1)”
    这种加了一堆字的同档项——否则精确命中的 简历.pdf 会被同档近义名挤到
    十几位（用户搜“简历”看不到桌面上那个，就是这个）。"""
    base = match_score(query, name, dirpath)
    if base is None:
        return None
    q = str(query or "").strip().lower()
    stem = os.path.splitext(str(name or ""))[0].lower()
    if stem == q:
        tight = 0                            # 文件名正好是关键词
    elif stem.startswith(q):
        tight = 10 + (len(stem) - len(q))    # 前缀：多出来的字越少越靠前
    elif q in stem:
        tight = 200 + (len(stem) - len(q))   # 名字中间命中
    else:
        tight = 400                          # 跳字 / 只在目录里
    return base * 10000 + tight


def phrase_expr(query):
    """把查询词转成 FTS5 短语表达式（空格分开的多个词之间是 AND）。

    用短语而不是逐词 AND，是为了保住「连续子串」语义：文档里 axbc 切成
    ax/xb/bc，查 abc（切 ab/bc）不该命中，短语正好不命中。

    单字（含单字英文）切不出二元组，留进表达式里只会让整个查询命中 0，
    所以直接丢掉：全丢光就返回空串，调用方据此退回 LIKE。"""
    parts = []
    for term in str(query or "").split():
        if len(normalize(term)) < 2:
            continue
        parts.append('"' + bigram(term).replace('"', '""') + '"')
    return " ".join(parts)


# ==================== 库连接 ====================
def connect():
    """每线程一条连接（sqlite 连接跨线程用会直接抛）；第一次建库。"""
    c = getattr(_TLS, "conn", None)
    if c is not None and getattr(_TLS, "path", None) == str(DB_PATH):
        return c
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=30.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")     # 扫描线程写、UI 线程读，WAL 才不打架
    c.execute("PRAGMA synchronous=NORMAL")
    with _LOCK:
        c.executescript(_SCHEMA)
        _migrate(c)                 # 先补列再验版本：老库没得选，跳过就是静默失效
        ver = c.execute("SELECT v FROM meta WHERE k='version'").fetchone()
        if ver is None or ver[0] != INDEX_VERSION:
            _wipe(c)
            c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('version',?)",
                      (INDEX_VERSION,))
        c.commit()
    _TLS.conn = c
    _TLS.path = str(DB_PATH)
    return c


def _migrate(conn):
    """给已经存在的老库补新列。

    为什么必须单独写：CREATE TABLE IF NOT EXISTS 对已经存在的表一个列都不加，
    老库直接按新口径跑 "SELECT mtime,mark FROM dirs" 会抛 OperationalError，
    而那个异常正被 scan_once 的 except sqlite3.Error 吞掉 —— 结果不是崩，是
    **每个目录都跳过差分**，索引从此不再更新，用户完全看不出来。"""
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(dirs)")}
    except sqlite3.Error:
        return                      # 表都读不出来（多半是刚建/坏了），交给建库路径
    if "mark" not in cols:
        conn.execute("ALTER TABLE dirs ADD COLUMN mark INTEGER DEFAULT 0")
    conn.commit()


def _wipe(conn):
    for t in ("files", "dirs", "docs_meta", "docs_fts"):
        conn.execute("DELETE FROM %s" % t)


def close():
    """关掉本线程连接（测试换库路径 / 退出前用；忘了也不致命）"""
    c = getattr(_TLS, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
    _TLS.conn = None
    _TLS.path = None


# ==================== 盘与目录 ====================
#: IOCTL_STORAGE_QUERY_PROPERTY = CTL_CODE(0x2D, 0x500, METHOD_BUFFERED, FILE_ANY_ACCESS)
_IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
#: STORAGE_PROPERTY_ID.StorageDeviceSeekPenaltyProperty：寻道有无惩罚→区分固态/机械
_SEEK_PENALTY_PROPERTY = 7
#: 盘类型探测缓存：{"C:": True 固态 / False 机械 / None 探不出}
_MEDIA_CACHE = {}
#: 已经提醒过"跳过了机械盘"的盘（只日志一次，每轮都喷一遍就是刷屏）
_HDD_NOTED = set()


def _query_seek_penalty(root):
    """问一次"这个盘的寻道有没有惩罚"：FALSE=固态，TRUE=机械盘。

    为什么不用 GetDriveType / Win32_LogicalDisk：它们只告诉你"固定盘"，
    分不出 4TB 叠瓦机械盘与 NVMe。而 DeviceSeekPenalty 正是资源管理器里
    "媒体类型 SSD/HDD" 那一列的口径，拿卷句柄、access=0 就能问，
    **不需要管理员权限**（本机实测：C/D/E=SSD，F/G=HDD）。
    探不到返回 None，调用方按"不知道"处理（宁可多扫，不能不扫）。"""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                    ctypes.c_void_p]
        k32.DeviceIoControl.restype = ctypes.c_int
        k32.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.POINTER(ctypes.c_uint32),
                                        ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]

        class _Query(ctypes.Structure):
            _fields_ = [("PropertyId", ctypes.c_int32),
                        ("QueryType", ctypes.c_int32),
                        ("Extra", ctypes.c_byte * 4)]

        class _Seek(ctypes.Structure):
            _fields_ = [("Version", ctypes.c_uint32),
                        ("Size", ctypes.c_uint32),
                        ("IncursSeekPenalty", ctypes.c_ubyte)]

        letter = str(root)[:1]
        # 64 位下不钉住 restype=HANDLE，返回的句柄会被截成 32 位整数：
        # 每一步都"调用成功"，合起来一次也不生效（与 window_foreground 同一个坑）
        h = k32.CreateFileW("\\\\.\\%s" % letter, 0, 3, None, 3, 0x80, None)
        if h is None or int(h or 0) in (0, 0xFFFFFFFFFFFFFFFF, -1):
            return None
        try:
            q = _Query()
            q.PropertyId = _SEEK_PENALTY_PROPERTY
            q.QueryType = 0                     # PropertyStandardQuery
            out = _Seek()
            got = ctypes.c_uint32(0)
            ok = k32.DeviceIoControl(h, _IOCTL_STORAGE_QUERY_PROPERTY,
                                     ctypes.byref(q), ctypes.sizeof(q),
                                     ctypes.byref(out), ctypes.sizeof(out),
                                     ctypes.byref(got), None)
            if not ok or got.value < ctypes.sizeof(out):
                return None
            return not out.IncursSeekPenalty     # 无惩罚 → 固态
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None                             # 没 ctypes / 非 Windows / 驱动不让问


def is_solid_state(root):
    """这个盘是不是固态（探不出给 None）。结果缓存：每轮都开一次句柄没意义。"""
    key = str(root or "")[:1].upper()
    if not key:
        return None
    if key in _MEDIA_CACHE:
        return _MEDIA_CACHE[key]
    _MEDIA_CACHE[key] = _query_seek_penalty("%s:" % key)
    return _MEDIA_CACHE[key]


def _note(msg):
    """往日志里留一句，任何毛病都吞掉：core 层不能因为日志把主流程带倒"""
    try:
        from core.logger import log
        log.info(msg)
    except Exception:
        pass


def local_drives(fast_only=True):
    """只认固定盘（DRIVE_FIXED），并且默认再剔掉机械盘。U 盘/网络盘要在设置里手动加。

    为什么默认不含可移动盘：拔掉的盘会让每轮扫描卡在超时重试上，表现成
    "索引再也不更新了"，而用户根本不知道是盘的事。

    为什么默认还要剔掉机械盘（本次实测的结论）：本机全盘走访一轮要 65.5 秒
    （C 17.8 / D 13.4 / E 0.9 / **F 30.9** / G 2.6），F 就是那块 4TB SMR 叠瓦盘。
    叠瓦盘随机元数据读极差，每 120 秒再来一整轮就把整台机器的磁盘队列顶满，
    系统看上去就是"卡死"。固态盘上同样的走访只有几秒。需要索引机械盘的，
    在设置里手动填进索引范围就行（custom_roots 不吃这个过滤）。

    探不出盘类型（None）时一律保留，宁可多扫不能不扫；全都被判成机械盘时
    退回全盘口径，不能让功能静默变成"永远是 0 个文件"。"""
    out = []
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.GetDriveTypeW.restype = ctypes.c_uint
            mask = kernel32.GetLogicalDrives()
            for i in range(26):
                if not (mask >> i) & 1:
                    continue
                root = "%s:\\" % chr(ord("A") + i)
                if kernel32.GetDriveTypeW(root) == 3:      # DRIVE_FIXED
                    out.append(root)
            if not out:
                return ["C:\\"]
            if not fast_only:
                return out
            keep = [r for r in out if is_solid_state(r) is not False]
            if not keep:                       # 全是机械盘（或全都探不出）：退回全盘口径
                return out
            for r in out:
                if r in keep or r in _HDD_NOTED:
                    continue
                _HDD_NOTED.add(r)
                _note("本地文件索引：默认跳过机械盘 %s（整轮走访实测几十秒，会把磁盘队列顶满；"
                      "要索引它就的去设置页手动加进索引范围）" % r)
            return keep
        except Exception:
            pass
    for letter in "CDEFGHIJKL":
        if os.path.exists(letter + ":\\") or os.path.exists(letter + ":/"):
            out.append(letter + ":/")
    return out or [str(Path.home())]


def excluded(name):
    return str(name or "").lower() in EXCLUDE_DIRS


def _iter_dir(path):
    """列一个目录，返回 (子目录名, [(文件名, size, mtime)])；无权限就返回空。

    每个 entry 单独 try：一个坏文件（被占用/没权限/名字非法）不能让整个目录丢掉，
    全盘扫描一趟这种异常能碰上百次。"""
    dirs, files = [], []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if not excluded(e.name):
                            dirs.append(e.name)
                    else:
                        st = e.stat(follow_symlinks=False)
                        files.append((e.name, st.st_size, st.st_mtime))
                except OSError:
                    continue
    except OSError:
        return [], []
    return dirs, files


def _dir_mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


# ==================== 扫描 ====================
def scan_once(roots=None, stop=None, on_progress=None, pace=0.0, commit_every=200):
    """走一遍目录树，把变化落进 files 表。

    返回 {"files","dirs","added","removed","sec"}。增量口径见模块头第 3 条：
    全盘照走，只对「签名（mtime + 子项数）没变」的目录做数据库差分。

    ⚠ 只改内容、不增删文件的目录会被跳过（NTFS 上写文件不动父目录 mtime），
    所以 files.mtime 可能偏旧。正文索引不受影响——那边每篇都自己 os.stat。

    stop 置位时提前收工并**跳过清理**：本轮没走到的目录不等于被删了。

    pace / commit_every 是节流旋钮（以前没有它们，就是本机卡死的其中一条）：
    12.3 万个目录逐个 commit，每次 commit 都是一轮 WAL 落盘/fsync，机械盘上
    能把一轮拉到几百秒。改成一批 200 个目录提一次，中途被打断最多重跑 200 个
    目录的活（本来下一轮也是增量，不怕），换来的是磁盘队列不再顶满。
    pace 是每批之间睡多久（0 = 不睡，测试里就这样传）；测试用的小目录树
    进不了 200 个目录，永远不会碰到 sleep，不会被拖慢。"""
    t0 = time.perf_counter()
    conn = connect()
    run = _next_run(conn)               # 轮次号不能用时间戳：同一秒内跑两轮会让
    stack = [str(r) for r in (roots or search_roots())]   # _purge 把上轮的 seen 当本轮的，残留清不掉
    total = added = removed = ndirs = pending = 0
    cut = False                     # 中途被 stop 打断：本轮没走完，绝不能做收尾清理
    while stack:
        if stop is not None and stop.is_set():
            cut = True
            break
        d = stack.pop()
        names, files = _iter_dir(d)
        for sub in names:
            stack.append(os.path.join(d, sub))
        ndirs += 1
        total += len(files)
        mt = _dir_mtime(d)
        mark = len(names) + len(files)      # 签名的一半：mtime 在 Windows 上不够细
        try:
            row = conn.execute("SELECT mtime,mark FROM dirs WHERE path=?", (d,)).fetchone()
            if (row is not None and abs((row[0] or 0) - mt) < 1e-6
                    and int(row[1] if row[1] is not None else -1) == mark):
                conn.execute("UPDATE dirs SET seen=? WHERE path=?", (run, d))
            else:
                a, r = _sync_dir(conn, d, files, mt, run, mark)
                added += a
                removed += r
            pending += 1
            if pending >= commit_every:
                conn.commit()
                pending = 0
                if pace > 0:
                    time.sleep(pace)        # 把 IO 摊平：别把整块盘的队列顶死
        except sqlite3.Error:
            _safe_commit(conn)              # 已洗好的那批先进库，别让一个坏目录带走一整批
            pending = 0
            continue
        if on_progress and ndirs % 200 == 0:
            on_progress(total, d)
    if not cut:                     # 半程清理＝把还没走到的目录整个从索引里抹掉，
        try:                        # 表现为“一退出搜索就空了”；跳过的残留下一轮补上
            removed += _purge(conn, run)
        except sqlite3.Error:
            pass
    _safe_commit(conn)
    stats = {"files": total, "dirs": ndirs, "added": added, "removed": removed,
             "sec": round(time.perf_counter() - t0, 1)}
    _set_meta(conn, "last_scan", int(time.time()))    # 展示用，必须是墙上时钟
    _set_meta(conn, "last_scan_files", stats["files"])   # status() 靠它免掉 COUNT(*)
    _set_meta(conn, "last_scan_dirs", ndirs)
    _set_meta(conn, "last_scan_sec", stats["sec"])
    conn.commit()
    return stats


def _safe_commit(conn):
    """提交能提交的，提交不了就回滚：扫描循环里一个 sqlite 错误不能把整轮带倒"""
    try:
        conn.commit()
    except sqlite3.Error:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass


def _next_run(conn):
    """本轮扫描的唯一编号（存 meta 里递增）；seen 列靠它区分“这轮见没见过”"""
    row = conn.execute("SELECT v FROM meta WHERE k='run'").fetchone()
    try:
        nxt = int(row["v"]) + 1 if row else 1
    except (TypeError, ValueError):
        nxt = 1
    _set_meta(conn, "run", nxt)
    conn.commit()
    return nxt


def _sync_dir(conn, d, files, mt, run, mark):
    """把一个目录的内容对齐到库里，返回 (新增/更新行数, 删除行数)。

    先查这个目录已有的行、再与磁盘列表比对，而不是无脑 REPLACE 全量写：
    稳态下每个目录只发一条 SELECT 和零条写入，95 万文件的重扫才不会变成
    95 万次 UPSERT。"""
    have = {r["name"]: (r["size"], r["mtime"]) for r in conn.execute(
        "SELECT name,size,mtime FROM files WHERE dir=?", (d,))}
    now = {n: (sz, fmt) for n, sz, fmt in files}
    ups = []
    for n, (sz, fmt) in now.items():
        if have.get(n) != (sz, fmt):
            ups.append((os.path.join(d, n), d, n,
                        os.path.splitext(n)[1].lower(), sz, fmt))
    gone = [os.path.join(d, n) for n in have if n not in now]
    if ups:
        conn.executemany("INSERT OR REPLACE INTO files"
                         "(path,dir,name,ext,size,mtime) VALUES(?,?,?,?,?,?)", ups)
    if gone:
        conn.executemany("DELETE FROM files WHERE path=?", [(p,) for p in gone])
    conn.execute("INSERT OR REPLACE INTO dirs(path,mtime,mark,seen) VALUES(?,?,?,?)",
                 (d, mt, mark, run))
    return len(ups), len(gone)


def _purge(conn, run):
    """清掉本轮没见到的目录及其下所有文件行（整个文件夹被删的情况）。

    返回删掉的行数：整目录被删不走进 _sync_dir，不计这一笔的话
    stats["removed"] 就永远是 0，报不了“这一轮带走了多少文件”。"""
    cur = conn.execute("DELETE FROM files WHERE dir IN"
                       "(SELECT path FROM dirs WHERE seen<>?)", (run,))
    conn.execute("DELETE FROM dirs WHERE seen<>?", (run,))
    return max(cur.rowcount, 0)


def _set_meta(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))


# ==================== 正文抽取 ====================
def _xml_text(blob):
    """去掉 XML 标签拿正文：docx/pptx 就是 zip 里几个 xml，不需要任何依赖"""
    return re.sub(rb"<[^>]*>", b" ", blob).decode("utf-8", "ignore")


def _docx_text(path):
    import zipfile
    with zipfile.ZipFile(str(path)) as z:
        names = set(z.namelist())
        out = [_docx_xml_text(z.read(m)) for m in
               ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml")
               if m in names]
    return "\n".join(s for s in out if s)


def _docx_xml_text(blob):
    """按 WordprocessingML 结构抽正文并保住排版（纯 stdlib，不引 python-docx）。

    旧实现对每个标签一律换成空格，`</w:p>`（段落结束）也被吞，整篇文档被拉成
    一整行 → 预览糊成一坨（就是“doc 排版有很大问题”的根因）。这里先把结构锚点
    换成真换行/制表：行内换行 <w:br/> 与段落 </w:p> 换行、<w:tab/> 制表；表格
    里单元格自己的段落结束紧跟 </w:tc>，得折叠成制表符才能让一行各列并排。
    最后去标签、收敛多余空行。"""
    blob = re.sub(rb"<w:tab(?:\s[^>]*)?/>", b"\t", blob)
    blob = re.sub(rb"<w:br(?:\s[^>]*)?/>", b"\n", blob)
    # 单元格内段落结束紧接 </w:tc>：表格一行应左右并排，不能因段落而拆行
    blob = re.sub(rb"</w:p></w:tc>", b"\t", blob)
    blob = re.sub(rb"</w:tc>", b"\t", blob)
    blob = re.sub(rb"</w:tr>", b"\n", blob)
    blob = re.sub(rb"</w:p>", b"\n", blob)
    text = re.sub(rb"<[^>]+>", b"", blob).decode("utf-8", "ignore")
    lines = []
    blank = 0
    for ln in text.splitlines():
        ln = ln.strip()
        if ln:
            lines.append(ln)
            blank = 0
        else:
            blank += 1
            if blank == 1 and lines:      # 连续空行最多留一个，且不在开头堆空行
                lines.append("")
    return "\n".join(lines)


def _pptx_text(path):
    """逐页拿：只读 slide1 的话，第二页往后写的内容永远搜不到（初稿就踩了）"""
    import zipfile
    out = []
    with zipfile.ZipFile(str(path)) as z:
        slides = sorted(n for n in z.namelist()
                        if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
        for n in slides[:PPTX_MAX_SLIDES]:
            out.append(_xml_text(z.read(n)))
    return "\n".join(out)


def _xlsx_text(path):
    from openpyxl import load_workbook
    wb = load_workbook(str(path), read_only=True, data_only=True)
    out = []
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    out.append(" ".join(cells))
    finally:
        wb.close()
    return "\n".join(out)


def _pdf_text(path):
    try:
        from pypdf import PdfReader        # 懒导入：没装 pypdf 就只跳过 pdf
    except Exception:
        return ""
    r = PdfReader(str(path))
    out = []
    for pg in r.pages[:PDF_MAX_PAGES]:
        try:
            out.append(pg.extract_text() or "")
        except Exception:
            continue                       # 单页坏了不影响整本
    return "\n".join(out)


def _plain_text(path):
    """txt/md 多编码试：项目踩过 GBK 坑（Windows 下的 txt 常是 ANSI/GBK）"""
    raw = Path(str(path)).read_bytes()
    for enc in ("utf-8", "utf-8-sig", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def extract_text(path, max_bytes=None):
    """抽文档正文；任何失败都返回 ""（绝不让索引线程因为一个坏文件炸掉）

    `~$xxx.docx` 是 Word 的锁文件（实测 162 字节的假 zip，喂进 zipfile 就
    BadZipFile），所以按前缀直接跳。"""
    p = Path(str(path))
    if p.name.startswith("~$"):
        return ""
    ext = p.suffix.lower()
    if ext not in DOC_EXTS:
        return ""
    try:
        if max_bytes is None:
            max_bytes = doc_max_bytes()
        if not p.is_file() or p.stat().st_size > max_bytes:
            return ""
        if ext in (".txt", ".md"):
            text = _plain_text(p)
        elif ext == ".docx":
            text = _docx_text(p)
        elif ext == ".pptx":
            text = _pptx_text(p)
        elif ext == ".xlsx":
            text = _xlsx_text(p)
        else:
            text = _pdf_text(p)
    except Exception:
        return ""
    return (text or "")[:DOC_BODY_KEEP]


# ==================== 文档索引 ====================
def index_docs(roots=None, stop=None, on_progress=None, force=False, pace=0.2):
    """给文档抽正文建全文索引；只重抽 (mtime,size) 变了或没抽过的。

    pace（每 50 个 sleep 一次）不是洁癖：一次抽几千个文档会把 CPU 吃满，
    用户正在剪视频时后台这么跑就会被当成"软件抢性能"。测试里传 0 关掉。"""
    t0 = time.perf_counter()
    conn = connect()
    roots = [str(r) for r in (roots or doc_roots())]
    done = skipped = 0
    batch = []

    def flush(rows):
        if not rows:
            return
        with _LOCK:
            for path, mtime, size, toks, body in rows:
                cur = conn.execute("SELECT id FROM docs_meta WHERE path=?", (path,))
                row = cur.fetchone()
                if row is None:
                    cur2 = conn.execute(
                        "INSERT INTO docs_meta(path,mtime,size) VALUES(?,?,?)",
                        (path, mtime, size))
                    rid = cur2.lastrowid
                else:
                    rid = row["id"]
                    conn.execute(
                        "UPDATE docs_meta SET mtime=?,size=? WHERE id=?",
                        (mtime, size, rid))
                    conn.execute("DELETE FROM docs_fts WHERE rowid=?", (rid,))
                conn.execute(
                    "INSERT INTO docs_fts(rowid,toks,path,body) VALUES(?,?,?,?)",
                    (rid, toks, path, body))
            conn.commit()

    for root in roots:
        if stop is not None and stop.is_set():
            break
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [x for x in dirnames if not excluded(x)]
            for fn in filenames:
                if stop is not None and stop.is_set():
                    break
                if not _doc_worth(fn):
                    skipped += 1
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if not force:
                    old = conn.execute(
                        "SELECT mtime,size FROM docs_meta WHERE path=?",
                        (full,)).fetchone()
                    if old is not None and abs(old["mtime"] - st.st_mtime) < 1e-6 \
                            and old["size"] == st.st_size:
                        continue
                body = extract_text(full)
                batch.append((full, st.st_mtime, st.st_size,
                              ("%s %s" % (bigram(fn), bigram(body))).strip(), body))
                done += 1
                if len(batch) >= 50:
                    flush(batch)
                    batch = []
                    if on_progress:
                        on_progress(done, full)
                    if pace > 0:
                        time.sleep(pace)
    flush(batch)
    _prune_docs(conn, roots)
    _set_meta(conn, "last_docs", int(time.time()))
    _set_meta(conn, "last_docs_done", done)
    conn.commit()
    return {"indexed": done, "skipped": skipped,
            "sec": round(time.perf_counter() - t0, 1)}


def _doc_worth(fn):
    """值不值得抽：扩展名在名单里、不是 Word 锁文件、大小没超上限"""
    if fn.startswith("~$"):
        return False
    ext = os.path.splitext(fn)[1].lower()
    return ext in DOC_EXTS


def _prune_docs(conn, roots):
    """文档已经不在了（或移出了文档目录）就删索引行，不然会搜到死链"""
    keep = tuple(str(r) for r in roots)
    if not keep:
        return
    stale = [r["id"] for r in conn.execute(
        "SELECT id,path FROM docs_meta").fetchall()
        if not r["path"].startswith(keep) or not os.path.isfile(r["path"])]
    for rid in stale:
        conn.execute("DELETE FROM docs_fts WHERE rowid=?", (rid,))
        conn.execute("DELETE FROM docs_meta WHERE id=?", (rid,))


# ==================== 查询 ====================
def search_files(query, limit=50):
    """按文件名/所在目录搜：LIKE 取候选 → _file_rank 细排（与工具搜索同口径）。

    候选 SQL 带一层 ORDER BY：把名字命中（尤其前缀）排在目录命中之前入池，
    否则热门词 LIMIT 2000 会先抓到一批按 rowid 乱序的目录命中，把真正按名字
    命中的好结果截在池外（实测踩过：搜 sk 共 4.5 万条，不排序时前缀命中根本没进候选）。"""
    q = str(query or "").strip()
    if not q:
        return []
    conn = connect()
    like = _like(q)
    try:
        rows = conn.execute(
            "SELECT path,dir,name,ext,size,mtime FROM files"
            " WHERE name LIKE ? ESCAPE '\\' OR dir LIKE ? ESCAPE '\\'"
            " ORDER BY CASE WHEN name LIKE ? ESCAPE '\\' THEN 0"
            "          WHEN name LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END"
            " LIMIT ?", (like, like, _like_prefix(q), like, FILE_CANDIDATES)).fetchall()
    except sqlite3.Error:
        return []
    scored = []
    for r in rows:
        if str(r["name"] or "").startswith("~$"):
            continue                    # Office 锁文件（~$xxx）是垃圾，不进结果
        rk = _file_rank(q, r["name"], r["dir"])
        if rk is not None:
            scored.append((rk, -float(r["mtime"] or 0), r))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [_row_to_item(r) for _rk, _m, r in scored[:limit]]


def search_docs(query, limit=20):
    """按正文搜：二元组短语匹配；单字退回 LIKE（切好的表里没有单字 token）"""
    q = str(query or "").strip()
    if not q:
        return []
    conn = connect()
    expr = phrase_expr(q)
    sql = ("SELECT m.path AS path, f.body AS body, m.mtime AS mtime,"
           " m.size AS size FROM docs_fts f JOIN docs_meta m ON m.id=f.rowid WHERE ")
    try:
        if expr:
            cur = conn.execute(sql + "docs_fts MATCH ? LIMIT ?",
                               (expr, FILE_CANDIDATES))
        else:
            cur = conn.execute(sql + "f.body LIKE ? ESCAPE '\\' LIMIT ?",
                               (_like(q), FILE_CANDIDATES))
        out = []
        for row in cur.fetchall():
            out.append({"path": row["path"],
                        "name": os.path.basename(row["path"]),
                        "dir": os.path.dirname(row["path"]),
                        "size": int(row["size"] or 0),
                        "mtime": float(row["mtime"] or 0),
                        "snippet": make_snippet(row["body"] or "", q)})
            if len(out) >= limit:
                return out
        return out
    except sqlite3.Error:
        return []


def make_snippet(body, query, span=40):
    """命中位置前后各取一截，让结果一眼看出"在哪提到的" """
    if not body:
        return ""
    low = body.lower()
    pos = 0
    for t in str(query or "").split():
        got = low.find(t.lower())
        if got >= 0:
            pos = got
            break
    a = max(0, pos - span)
    text = re.sub(r"\s+", " ", body[a:pos + span * 2]).strip()
    return ("…" if a > 0 else "") + text + ("…" if a + len(text) < len(body) else "")


def _row_to_item(r):
    return {"path": r["path"], "name": r["name"], "dir": r["dir"],
            "ext": r["ext"], "size": int(r["size"] or 0),
            "mtime": float(r["mtime"] or 0), "snippet": ""}


# ==================== 状态与维护 ====================
def _meta_get(conn, key):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    if row is None or row[0] in (None, ""):
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def status():
    """给设置页与面板提示行用：索引到什么程度了（表还没建也不能抛）。

    ⚠ 文件数读 meta 里的缓存，绝不 COUNT(*)：本机索引库 729MB / 95 万行，
    COUNT(files) 热缓存 0.10 秒、**冷缓存 4.62 秒**（实测）。而唤出面板每秒刷一次
    状态行、设置页每 5 秒刷一次，全跑在 Qt 主线程上；而全局低级键盘钩子就挂
    在同一条主线程里——主线程一被占住，整个系统的键盘都得等它返回，用户的说法
    就是"电脑卡死"。扫描收尾时已经把总数写进 last_scan_files（见 scan_once），
    读它就是一条主键 SELECT。
    没这个键就报 0，**不退回 COUNT**：库里的行只可能由 scan_once 写进去，而它一定
    会顺手把计数补上（启动 3 秒后的首轮），所以"没键"只会出现在刚建库/整库被删
    时。为一个几乎走不到的分支留一次 4.6 秒的主线程卡顿，就是拿所有人换一个人。
    文档数照旧 COUNT：docs_meta 只装"用户真会写文档的目录"，本机才 1500 行。"""
    conn = connect()
    try:
        nf = _meta_get(conn, "last_scan_files") or 0
        nd = conn.execute("SELECT COUNT(*) AS c FROM docs_meta").fetchone()["c"]
        last = conn.execute("SELECT v FROM meta WHERE k='last_scan'").fetchone()
        lastd = conn.execute("SELECT v FROM meta WHERE k='last_docs'").fetchone()
    except sqlite3.Error:
        return {"files": 0, "docs": 0, "last_scan": 0, "last_docs": 0}
    return {"files": nf, "docs": nd,
            "last_scan": int(last["v"]) if last and last["v"] else 0,
            "last_docs": int(lastd["v"]) if lastd and lastd["v"] else 0}


def rebuild():
    """整库清空（偏好不动）：库坏了、口径改了、用户想重来都用这个。

    meta 里的进度也得一并抹掉（只留 version/run）：“已收录 95 万文件 / 上次
    扫描 10:32”在重建后要是还挂着，就是给用户报假账。"""
    conn = connect()
    with _LOCK:
        _wipe(conn)
        conn.execute("DELETE FROM meta WHERE k NOT IN ('version','run')")
        conn.commit()
    return True


def vacuum():
    """大批文件消失后回收磁盘（VACUUM 不能在事务里跑，所以单独一个函数）"""
    conn = connect()
    with _LOCK:
        conn.commit()
        conn.execute("VACUUM")
    return True
