"""
tests/test_file_index.py —— 本地文件索引：全盘增量扫描 + 中文全文检索

这份索引的每一条口径都是拿真机测出来的，测试就照着那几个实测结论钉：
1. **排除清单**。只排 Windows/回收站是 172.6 万文件 / 74.7 秒，再排 AppData、
   node_modules 这些是 95.1 万 / 7.8 秒。少排一类就多十万个没人搜的文件，
   所以"排除目录整棵子树不进索引"是硬要求，不是美化。
2. **中文必须自己切二元组**。内置 FTS5 的 unicode61 把「关节不舒服」整串当
   一个词，`MATCH '关节'` 实测命中 0；trigram 分词器要求 ≥3 字符，两字词照样
   搜不到。所以「搜『关节』必须命中」这一条就是整个内容搜索的守门人。
3. **增量签名是「目录 mtime + 子项数」两条一起**：剪掉"没变的子树"会漏掉
   子目录里新增的文件，而只看 mtime 又会漏掉"同一秒里加一个又删一个"（实测
   removed 报 0）。两个条件都得对上才敢跳过差分。
4. **扫描被打断时绝不收尾清理**：本轮没走到 ≠ 已被删除，清了就是"一退出
   搜索框整个索引就空了"。

全部走 tmp_path 造假树，一块真盘都不碰。
"""
import os
import shutil
import sqlite3
import sys
import threading
import zipfile

import pytest

from core import fileindex
from store import app_state
from workers import file_watcher


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每个用例一份独立库：DB_PATH 换到 tmp_path，连接是线程本地的，
    所以前后都得 close()，否则上一个用例的连接会指着已经删掉的库文件"""
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui_state.json")
    monkeypatch.setattr(fileindex, "DB_PATH", tmp_path / "idx" / "file_index.db")
    fileindex.close()
    yield
    fileindex.close()


def _mk(path, text="占位内容", enc="utf-8"):
    p = str(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    data = text.encode(enc) if isinstance(text, str) else bytes(text)
    with open(p, "wb") as f:
        f.write(data)
    return p


def _docx(path, text):
    """造一个能抽的 docx：真结构就是 zip 里一个 word/document.xml"""
    with zipfile.ZipFile(str(path), "w") as z:
        z.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        z.writestr("word/document.xml",
                   "<w:document><w:body><w:p><w:r><w:t>%s</w:t></w:r></w:p>"
                   "</w:body></w:document>" % text)
    return str(path)


def _pptx(path, slides):
    """多页 pptx：正文分散在 ppt/slides/slideN.xml，只读第 1 页会漏后面所有页"""
    with zipfile.ZipFile(str(path), "w") as z:
        for i, txt in enumerate(slides, 1):
            z.writestr("ppt/slides/slide%d.xml" % i,
                       "<p:sld><a:t>%s</a:t></p:sld>" % txt)
    return str(path)


@pytest.fixture
def tree(tmp_path):
    """一棵小树：普通文件 + 子目录 + 两个该被排除掉的目录"""
    root = tmp_path / "drive"
    _mk(root / "爆款拆解.txt", "关节不舒服的三种成因")
    _mk(root / "sub" / "骨胶原-9.27-142.md", "钩子：骨胶原")
    _mk(root / "AppData" / "leak.py", "print(1)")
    _mk(root / "node_modules" / "junk.txt", "junk")
    return root


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / "docs"
    _mk(root / "笔记.md", "关节不舒服的三种成因，骨胶原可以缓解")
    _mk(root / "子目录" / "报告.txt", "本期爆款拆解复盘：钩子很重要")
    _docx(root / "AI拆解.docx", "标题：骨胶原-9.27-142钩子：")
    return root


# ---------------- 切词与打分（纯函数，实测结论的落点） ----------------

@pytest.mark.parametrize("text,expect", [
    ("关节不舒服", "关节 节不 不舒 舒服"),
    ("ABC", "ab bc"),
    ("  A 中 ", "a中"),                    # 空白剥掉、转小写：写查两侧同一个口径
    ("只", "只"),                          # 单字切不出二元组，原样交给 LIKE 兜底
    ("", ""),
    (None, ""),
])
def test_bigram(text, expect):
    assert fileindex.bigram(text) == expect


def test_phrase_expr_keeps_substring_semantics():
    assert fileindex.phrase_expr("关节") == '"关节"'
    assert fileindex.phrase_expr("关节 不舒服") == '"关节" "不舒 舒服"'
    # 单字留进 FTS 只会让整个查询命中 0：丢掉，全丢光就退回 LIKE
    assert fileindex.phrase_expr("关") == ""
    # axbc 切成 ax/xb/bc，查 abc（ab+bc）不该被逐词 AND 误命中，短语才不命中
    assert fileindex.phrase_expr("abc").startswith('"ab bc"')


@pytest.mark.parametrize("q,name,desc,expect", [
    ("爆款", "爆款拆解", "", 0),
    ("款拆", "爆款拆解", "", 1),
    ("爆拆", "爆款拆解", "", 2),           # 记不全名字时的跳字
    ("文档", "随便.txt", os.path.join("C:\\x", "文档"), 3),
    ("zzz", "爆款拆解", "简介", None),
    ("", "任意", "", 0),                    # 空串＝全量列表，不是不匹配
])
def test_match_score_moves_here_and_unchanged(q, name, desc, expect):
    assert fileindex.match_score(q, name, desc) == expect


# ---------------- 扫描：首扫 / 增量 / 清理 ----------------

def test_first_scan_and_exclusions(tree):
    st = fileindex.scan_once(roots=[str(tree)])
    assert st["files"] == 2                       # AppData / node_modules 整棵子树不算
    assert st["added"] == 2
    hit = fileindex.search_files("爆款拆解")
    assert hit and hit[0]["name"] == "爆款拆解.txt"
    assert hit[0]["dir"] == str(tree)
    assert fileindex.search_files("leak") == []        # 排除目录里的文件不进索引
    assert fileindex.search_files("node_modules") == []


def test_rescan_only_touches_what_changed(tree):
    fileindex.scan_once(roots=[str(tree)])
    again = fileindex.scan_once(roots=[str(tree)])
    assert (again["added"], again["removed"]) == (0, 0)
    _mk(os.path.join(str(tree), "sub", "new_file.md"), "新增一篇")
    third = fileindex.scan_once(roots=[str(tree)])
    assert (third["added"], third["removed"]) == (1, 0)
    assert [h["name"] for h in fileindex.search_files("new_file")] == ["new_file.md"]
    os.remove(os.path.join(str(tree), "sub", "new_file.md"))
    fourth = fileindex.scan_once(roots=[str(tree)])
    assert (fourth["added"], fourth["removed"]) == (0, 1)
    assert fileindex.search_files("new_file") == []


def test_removal_caught_even_when_dir_mtime_is_too_coarse(tree):
    """Windows 上目录 mtime 粒度不够细：同一秒里"加一个又删一个"时间戳能一模一样。
    签名必须还看子项数，否则这条删除永远看不见（实测踩过：removed 报 0）。"""
    sub = os.path.join(str(tree), "sub")
    fileindex.scan_once(roots=[str(tree)])
    _mk(os.path.join(sub, "new_file.md"), "新增")
    fileindex.scan_once(roots=[str(tree)])
    os.remove(os.path.join(sub, "new_file.md"))
    conn = fileindex.connect()
    # 把库里的 mtime 写成磁盘此刻的值 = 模拟"mtime 一点没动"，只剩子项数变得了
    conn.execute("UPDATE dirs SET mtime=? WHERE path=?",
                 (fileindex._dir_mtime(sub), sub))
    conn.commit()
    st = fileindex.scan_once(roots=[str(tree)])
    assert st["removed"] == 1
    assert fileindex.search_files("new_file") == []


def test_old_db_gets_the_mark_column(tmp_path, monkeypatch):
    """老库（dirs 没 mark 列）必须 ALTER 补上：CREATE TABLE IF NOT EXISTS 对已存在
    的表一个列都不加，而 "no such column" 会被 scan_once 的 except sqlite3.Error
    吞掉 —— 不崩，是每个目录静默跳过差分，索引从此再也不动。"""
    p = tmp_path / "old.db"
    raw = sqlite3.connect(str(p))
    raw.executescript(
        "CREATE TABLE dirs(path TEXT PRIMARY KEY, mtime REAL DEFAULT 0,"
        "  seen INTEGER DEFAULT 0);\n"
        "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT DEFAULT '');\n")
    raw.execute("INSERT INTO meta(k,v) VALUES('version','1')")
    raw.commit()
    raw.close()
    monkeypatch.setattr(fileindex, "DB_PATH", p)
    fileindex.close()
    try:
        cols = {r["name"] for r in fileindex.connect().execute("PRAGMA table_info(dirs)")}
        assert "mark" in cols, "老库没补列，新签名的 SELECT 会当场报错"
    finally:
        fileindex.close()


def test_whole_directory_removed_is_purged(tree):
    fileindex.scan_once(roots=[str(tree)])
    assert fileindex.search_files("骨胶原")
    shutil.rmtree(os.path.join(str(tree), "sub"))
    st = fileindex.scan_once(roots=[str(tree)])
    assert st["removed"] == 1
    assert fileindex.search_files("骨胶原") == []


def test_interrupted_scan_does_not_wipe_the_index(tree):
    """stop 置位时提前收工：剩下的目录一个都没走到，此时清理＝整个索引被抹掉"""
    fileindex.scan_once(roots=[str(tree)])
    stop = threading.Event()
    stop.set()
    fileindex.scan_once(roots=[str(tree)], stop=stop)
    assert fileindex.search_files("爆款拆解"), "被打断的一轮把索引清没了"


def test_run_number_increments_each_round(tree):
    """轮次号必须是递增计数：拿时间戳当轮次，同一秒跑两轮就会把上轮 seen 当本轮"""
    fileindex.scan_once(roots=[str(tree)])
    conn = fileindex.connect()
    first = conn.execute("SELECT v FROM meta WHERE k='run'").fetchone()["v"]
    fileindex.scan_once(roots=[str(tree)])
    second = conn.execute("SELECT v FROM meta WHERE k='run'").fetchone()["v"]
    assert int(second) == int(first) + 1


def test_progress_callback_gets_totals(tree):
    seen = []
    fileindex.scan_once(roots=[str(tree)],
                        on_progress=lambda n, d: seen.append((n, d)))
    # 每 200 个目录才回调一次，小树不该被打扰（也证明没把 UI 线程淹了）
    assert seen == []


class _Fn:
    """伪 ctypes 函数：得是个能挂属性的对象（local_drives 会往上写 restype，
    绑到方法上写就 AttributeError，被局部 try 吞掉就走回退分支，测了个假）"""

    def __init__(self, fn):
        self._fn = fn
        self.restype = None

    def __call__(self, *args):
        return self._fn(*args)


class _Kernel32:
    def __init__(self, kinds, present):
        self.kinds, self.present = kinds, present
        self.GetDriveTypeW = _Fn(lambda root: self.kinds.get(str(root)[:1], 3))
        self.GetLogicalDrives = _Fn(self.mask)

    def mask(self):
        m = 0
        for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
            if ch in self.present:
                m |= 1 << i
        return m


class _Ctypes:
    c_uint = int

    def __init__(self, k32):
        class _W:
            kernel32 = k32
        self.windll = _W()


@pytest.mark.parametrize("kinds,expect", [
    ({"C": 3, "D": 3}, ["C:\\", "D:\\"]),
    ({"C": 3, "D": 2}, ["C:\\"]),          # 可移动盘（U 盘）不进默认口径
    ({"C": 3, "D": 4}, ["C:\\"]),          # 网络盘：拔了就每轮扫描卡在超时上
    ({"C": 3, "D": 5}, ["C:\\"]),          # 光驱
])
def test_local_drives_keeps_only_fixed(kinds, expect, monkeypatch):
    fake = _Ctypes(_Kernel32(kinds, set(kinds)))
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    monkeypatch.setattr(os, "name", "nt")
    assert fileindex.local_drives() == expect


def test_search_roots_follows_preference(tmp_path):
    assert fileindex.custom_roots() == []
    fileindex.set_custom_roots([tmp_path / "one", tmp_path / "two"])
    assert fileindex.custom_roots() == [str(tmp_path / "one"), str(tmp_path / "two")]
    assert fileindex.search_roots() == fileindex.custom_roots()
    fileindex.set_custom_roots([])
    assert fileindex.custom_roots() == []       # 清空＝回到全盘口径（这条不真扫盘）


# ---------------- 正文抽取 ----------------

def test_plain_text_handles_gbk(tmp_path):
    """Windows 下的 txt 常是 ANSI/GBK：只按 utf-8 读就整篇变乱码（项目踩过）"""
    a = _mk(tmp_path / "a.txt", "关节不舒服的三种成因", "utf-8")
    b = _mk(tmp_path / "b.txt", "骨胶原钩子很重要", "gbk")
    assert "关节" in fileindex.extract_text(a)
    assert "骨胶原" in fileindex.extract_text(b)


def test_office_formats(tmp_path):
    d = _docx(tmp_path / "AI拆解.docx", "标题：骨胶原-9.27-142钩子：")
    assert "骨胶原" in fileindex.extract_text(d)
    p = _pptx(tmp_path / "s.pptx", ["第一页文案", "第二页讲关节"])
    body = fileindex.extract_text(p)
    assert "第一页文案" in body and "第二页讲关节" in body


def test_docx_keeps_paragraph_layout(tmp_path):
    """docx 排版：每个标签都换成空格会把整篇 Word 拉成一整行（预览糊成坨的根因）。
    段落 </w:p> 必须变真换行、表格单元格 </w:tc> 变制表，正文才一块一块。"""
    import zipfile
    f = tmp_path / "段落.docx"
    with zipfile.ZipFile(str(f), "w") as z:
        z.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        z.writestr("word/document.xml",
                   "<w:document><w:body>"
                   "<w:p><w:r><w:t>第一段钩子</w:t></w:r></w:p>"
                   "<w:p><w:r><w:t>第二段骨胶原</w:t></w:r></w:p>"
                   "<w:tbl><w:tr>"
                   "<w:tc><w:p><w:r><w:t>品名</w:t></w:r></w:p></w:tc>"
                   "<w:tc><w:p><w:r><w:t>关节</w:t></w:r></w:p></w:tc>"
                   "</w:tr></w:tbl>"
                   "</w:body></w:document>")
    body = fileindex.extract_text(f)
    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert "第一段钩子" in lines               # 各自成行，不是黏成一整串
    assert "第二段骨胶原" in lines
    assert "品名\t关节" in body                # 同一行里两个单元格用制表分开
    assert body.count("\n") >= 2                 # 三个段落/行，至多两个行间隔


def test_xlsx(tmp_path):
    from openpyxl import Workbook
    f = tmp_path / "b.xlsx"
    wb = Workbook()
    wb.active.append(["品名", "骨胶原"])
    wb.save(str(f))
    assert "骨胶原" in fileindex.extract_text(f)


def test_lock_file_oversize_and_broken_return_empty(tmp_path):
    """三类坑货都得吞下：索引线程绝不能因为一个坏文件炸掉"""
    lock = _mk(tmp_path / "~$AI拆解.docx", b"PK\x03\x04notazip")
    assert fileindex.extract_text(lock) == ""        # Word 锁文件：假 zip
    big = _mk(tmp_path / "big.txt", "x" * 200)
    assert fileindex.extract_text(big, max_bytes=10) == ""
    broken = _mk(tmp_path / "broken.docx", "这不是 zip 文件".encode("utf-8"))
    assert fileindex.extract_text(broken) == ""
    assert fileindex.extract_text(tmp_path / "没这个.pdf") == ""
    assert fileindex.extract_text(tmp_path / "音.mp3") == ""   # 名单外不抽


def test_body_is_capped(tmp_path):
    """不封顶的话一个 100KB 的 txt 连切词能撑出 300KB 索引，两万个就是几个 GB"""
    long = _mk(tmp_path / "long.txt", "关节" * 30000)
    assert len(fileindex.extract_text(long)) == fileindex.DOC_BODY_KEEP


# ---------------- 全文索引与查询 ----------------

def test_index_docs_and_chinese_two_char_search(docs):
    """这条就是内容搜索的命门：内置分词器实测命中 0，切完二元组必须命中"""
    st = fileindex.index_docs(roots=[str(docs)], pace=0)
    assert st["indexed"] == 3
    got = fileindex.search_docs("关节")
    assert [g["name"] for g in got] == ["笔记.md"]
    assert "关节" in got[0]["snippet"]
    assert got[0]["path"].endswith("笔记.md")
    assert [g["name"] for g in fileindex.search_docs("爆款")] == ["报告.txt"]


def test_single_char_doc_query_falls_back_to_like(docs):
    fileindex.index_docs(roots=[str(docs)], pace=0)
    got = fileindex.search_docs("钩")
    assert got and "钩" in got[0]["snippet"]


def test_docs_are_not_reextracted(docs, monkeypatch):
    """(mtime,size) 没变就不重抽：每轮全盘重抽几千篇是把 CPU 白烧"""
    calls = []
    real = fileindex.extract_text

    def spy(path, max_bytes=None):
        calls.append(os.path.basename(str(path)))
        return real(path, max_bytes)

    monkeypatch.setattr(fileindex, "extract_text", spy)
    fileindex.index_docs(roots=[str(docs)], pace=0)
    assert sorted(calls) == ["AI拆解.docx", "报告.txt", "笔记.md"]
    calls.clear()
    fileindex.index_docs(roots=[str(docs)], pace=0)
    assert calls == []
    _mk(os.path.join(str(docs), "笔记.md"), "关节不舒服 这次改了内容")
    calls.clear()
    fileindex.index_docs(roots=[str(docs)], pace=0)
    assert calls == ["笔记.md"]


def test_dead_doc_links_are_pruned(docs):
    fileindex.index_docs(roots=[str(docs)], pace=0)
    assert {g["name"] for g in fileindex.search_docs("骨胶原")} == \
           {"AI拆解.docx", "笔记.md"}
    os.remove(os.path.join(str(docs), "AI拆解.docx"))
    fileindex.index_docs(roots=[str(docs)], pace=0)
    assert [g["name"] for g in fileindex.search_docs("骨胶原")] == ["笔记.md"]


def test_search_docs_pace_and_stop(docs):
    """stop 置位就收手：文档抽取是 CPU 大户，退出时不能拦着进程"""
    stop = threading.Event()
    stop.set()
    fileindex.index_docs(roots=[str(docs)], stop=stop, pace=0)
    assert fileindex.status()["docs"] == 0


# ---------------- 文件名搜索的排序 ----------------

def test_search_files_orders_prefix_then_contains_then_dir(tmp_path):
    root = tmp_path / "rank"
    _mk(root / "爆款拆解.txt")
    _mk(root / "我的爆款笔记.docx")
    _mk(root / "爆款" / "随便.txt")
    fileindex.scan_once(roots=[str(root)])
    names = [h["name"] for h in fileindex.search_files("爆款")]
    assert names[0] == "爆款拆解.txt"          # 前缀
    assert names[1] == "我的爆款笔记.docx"      # 名称包含
    assert set(names) == {"爆款拆解.txt", "我的爆款笔记.docx", "随便.txt"}
    assert fileindex.search_files("") == []
    assert fileindex.search_files("zzz不存在") == []


def test_file_rank_prefers_exact_then_shorter_name():
    """同一个粗档里还要细分：去后缀正好等于关键词的排第一，
    前缀相同但多了一堆字的往后排，中间命中沉底（名字与 mtime 无关）。"""
    r = fileindex._file_rank
    assert r("简历", "简历.pdf", "D:\\x") < r("简历", "简历正式版.pdf", "D:\\x")
    assert r("简历", "简历正式版.pdf", "D:\\x") < r("简历", "简历 (1).pdf", "D:\\x")
    assert r("简历", "开发简历.pdf", "D:\\x") > r("简历", "简历正式版.pdf", "D:\\x")
    assert r("简历", "随便.txt", "C:\\简历\\x") is not None   # 只在目录里命中也算（高档）
    assert r("zzz", "简历.pdf", "D:\\x") is None


def test_search_files_exact_name_ranks_above_longer_prefix(tmp_path):
    """一堆同档近义名时，“名字正好是关键词”的必须排最前，不能被最近修改的
    同名长文件压下去（实测踩过：搜“简历”，桌面 简历.pdf 被 简历正式版 挤到第17）。"""
    root = tmp_path / "jian"
    _mk(root / "简历正式版.pdf")
    _mk(root / "简历 (1).pdf")
    _mk(root / "简历.pdf")
    # 故意把“简历正式版”的 mtime 做得最新：旧口径靠 mtime 决胜就会把它顶到第一
    os.utime(root / "简历正式版.pdf", (9e8, 9e8))
    fileindex.scan_once(roots=[str(root)])
    names = [h["name"] for h in fileindex.search_files("简历")]
    assert names[0] == "简历.pdf"


def test_search_files_hides_office_lock_files(tmp_path):
    """~$ 打头的是 Office 编辑时产生的锁文件，扫进了索引但不能搜出来（纯噪声）"""
    root = tmp_path / "lock"
    _mk(root / "简历.pdf")
    _mk(root / "~$简历.pdf")
    fileindex.scan_once(roots=[str(root)])
    names = [h["name"] for h in fileindex.search_files("简历")]
    assert "简历.pdf" in names
    assert all(not n.startswith("~$") for n in names)


def test_like_metachars_are_escaped(tmp_path):
    """用户打的 % 和 _ 是字面量：不转义就会把"100%"搜成一堆无关命中"""
    root = tmp_path / "meta"
    _mk(root / "100%纯羊毛.txt")
    _mk(root / "a_b.txt")
    _mk(root / "xz.txt")
    fileindex.scan_once(roots=[str(root)])
    assert [h["name"] for h in fileindex.search_files("100%")] == ["100%纯羊毛.txt"]
    assert [h["name"] for h in fileindex.search_files("a_b")] == ["a_b.txt"]
    assert fileindex.search_files("a_b") and not fileindex.search_files("axbx")


# ---------------- 空库与状态 ----------------

def test_empty_index_answers_without_crashing():
    assert fileindex.search_files("任意") == []
    assert fileindex.search_docs("任意") == []
    st = fileindex.status()
    assert st == {"files": 0, "docs": 0, "last_scan": 0, "last_docs": 0}


def test_status_and_rebuild(tree, docs):
    fileindex.scan_once(roots=[str(tree)])
    fileindex.index_docs(roots=[str(docs)], pace=0)
    st = fileindex.status()
    assert st["files"] == 2 and st["docs"] == 3
    # 状态行要能展示"上次扫描时间"：存的必须是墙上时钟，不是单调时钟
    assert st["last_scan"] > 1_600_000_000
    fileindex.rebuild()
    assert fileindex.status() == {"files": 0, "docs": 0, "last_scan": 0, "last_docs": 0}
    assert fileindex.search_files("爆款拆解") == []
    fileindex.scan_once(roots=[str(tree)])        # 重建之后能接着扫回来
    assert fileindex.status()["files"] == 2


def test_wal_mode_is_on_for_concurrent_read_write(tree):
    """扫描线程写、UI 线程读：不是 WAL 就是一边写一边 SQLITE_BUSY"""
    fileindex.scan_once(roots=[str(tree)])
    assert fileindex.connect().execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# ---------------- 偏好 ----------------

def test_preferences_roundtrip():
    assert fileindex.enabled() is True and fileindex.doc_enabled() is True
    fileindex.set_enabled(False)
    fileindex.set_doc_enabled(False)
    assert fileindex.enabled() is False and fileindex.doc_enabled() is False
    fileindex.set_doc_max_mb(5)
    assert fileindex.doc_max_bytes() == 5 * 1024 * 1024
    fileindex.set_doc_max_mb(0)                   # 0 会让每个文件都超上限：夹到 1MB
    assert fileindex.doc_max_bytes() == 1024 * 1024
    fileindex.set_doc_max_mb("abc")
    assert fileindex.doc_max_bytes() == 20 * 1024 * 1024


def test_doc_roots_default_to_writable_places():
    got = fileindex.default_doc_roots()
    assert got and all(os.path.isdir(p) for p in got)
    assert fileindex.doc_roots() == got
    fileindex.set_doc_roots(["D:\\我的文档"])
    assert fileindex.doc_roots() == ["D:\\我的文档"]


# ---------------- 后台线程（workers/file_watcher） ----------------

def test_the_rhythm_is_the_one_we_shipped():
    """启动延迟 3 秒、间隔下限 120 秒是产品决定。写成断言是为了下次有人手滑
    改成 1 秒时先撞上测试，而不是先撞上用户的 CPU。

    ROUND_SEC 现在只是**下限**：本机实测单轮 65.5 秒（走访）~100-200 秒（带逐目录
    提交），固定 120 秒间隔等于让扫描线程一刻不停，那就是“电脑卡死”的根因之一。"""
    assert file_watcher.STARTUP_DELAY == 3
    assert file_watcher.ROUND_SEC == 120
    assert file_watcher.ROUND_MAX == 900          # 机器再慢也得每 15 分钟动一次
    assert 0 < file_watcher.SCAN_PACE < 1         # IO 摊平（但不能把一轮拖成十几分钟）
    assert fileindex.scan_once.__defaults__ is not None   # pace/commit_every 得是真参数


def test_next_interval_outruns_a_slow_round(monkeypatch):
    """单轮比间隔还长时必须把下一轮延后：按固定 120 秒催就是让磁盘永远没空闲"""
    monkeypatch.setattr(file_watcher, "_STATS", {"last_sec": 0.0})
    assert file_watcher.next_interval() == file_watcher.ROUND_SEC
    monkeypatch.setattr(file_watcher, "_STATS", {"last_sec": 340.0})
    assert file_watcher.next_interval() == 340     # 至少留出 1:1 的空闲
    monkeypatch.setattr(file_watcher, "_STATS", {"last_sec": 9999.0})
    assert file_watcher.next_interval() == file_watcher.ROUND_MAX   # 但也不能无限延后


def test_local_drives_drops_spinning_disks(monkeypatch):
    """默认只索引固态盘（本机实测：全盘走访一轮 65.5 秒，其中 F 盘 30.9 秒，
    F 就是那块 4TB SMR 叠瓦盘）。探不出盘类型时必须留着，不能静默不索引。"""
    kinds = {"C": 3, "D": 3, "F": 3}
    fake = _Ctypes(_Kernel32(kinds, set(kinds)))
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(fileindex, "_MEDIA_CACHE", {})
    monkeypatch.setattr(fileindex, "_HDD_NOTED", set())
    monkeypatch.setattr(fileindex, "is_solid_state", lambda r: {"C": True, "D": None,
                                                                "F": False}[r[:1]])
    assert fileindex.local_drives() == ["C:\\", "D:\\"]   # 探不出的 D 留着，确定是机械的 F 剔掉
    # 全是机械盘时退回全盘口径：宁可慢，也不能把功能变成“永远是 0 个文件”
    monkeypatch.setattr(fileindex, "_MEDIA_CACHE", {})
    monkeypatch.setattr(fileindex, "is_solid_state", lambda r: False)
    assert fileindex.local_drives() == ["C:\\", "D:\\", "F:\\"]
    # fast_only=False 是给“我就是要索引机械盘”留的口子
    assert fileindex.local_drives(fast_only=False) == ["C:\\", "D:\\", "F:\\"]


def test_start_refuses_when_the_feature_is_off(monkeypatch):
    monkeypatch.setattr(fileindex, "enabled", lambda: False)
    assert file_watcher.start() is False
    assert file_watcher.running() is False
    assert file_watcher.kick() is False           # 没线程可催，也不能谎报成功


def test_start_is_idempotent_and_stop_ends_it(monkeypatch):
    """真起一条线程，但 _loop 换成“只是等退出信号”：两条线程扫同一份库
    会把增量差分算乱（seen 轮次号互相踩），所以 start() 必须幂等。"""
    monkeypatch.setattr(file_watcher, "_loop", lambda stop, kick: stop.wait(5.0))
    try:
        assert file_watcher.start() is True
        assert file_watcher.running() is True
        assert file_watcher.start() is False
        assert file_watcher.kick() is True
    finally:
        file_watcher.stop()
    assert file_watcher.running() is False


def test_run_once_follows_the_two_switches(monkeypatch):
    calls = []
    monkeypatch.setattr(fileindex, "scan_once",
                        lambda **kw: calls.append("scan") or {"files": 3})
    monkeypatch.setattr(fileindex, "index_docs",
                        lambda **kw: calls.append("docs") or {"indexed": 1})
    monkeypatch.setattr(fileindex, "doc_enabled", lambda: False)
    file_watcher.run_once(docs=True)
    assert calls == ["scan"], "正文开关关了还去抽文档，就是白抽 55 秒"
    monkeypatch.setattr(fileindex, "doc_enabled", lambda: True)
    out = file_watcher.run_once(docs=True)
    assert calls == ["scan", "scan", "docs"]
    assert out["docs"]["indexed"] == 1
    assert "3文件" in file_watcher.stats()["last"]


def test_run_once_skips_docs_when_stopped_midway(monkeypatch):
    """扫到一半用户就退了：别再开第二段 55 秒的抽取"""
    calls = []
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(fileindex, "scan_once",
                        lambda **kw: calls.append("scan") or {})
    monkeypatch.setattr(fileindex, "index_docs",
                        lambda **kw: calls.append("docs") or {})
    file_watcher.run_once(stop=stop, docs=True)
    assert calls == ["scan"]


def test_a_failing_round_is_recorded_not_fatal(monkeypatch):
    """后台线程把进程带崩是最难看的一种 bug：异常只能进 stats 与日志"""
    def boom(**kw):
        raise RuntimeError("盘掉了")

    monkeypatch.setattr(fileindex, "scan_once", boom)
    file_watcher._round(threading.Event())            # 不抛
    assert "盘掉了" in file_watcher.stats()["error"]


def test_round_skips_while_one_is_running(monkeypatch):
    """重入保护：一轮没跑完不能再开第二轮（两个写入者同抢一份库）"""
    calls = []
    monkeypatch.setattr(fileindex, "scan_once", lambda **kw: calls.append(1))
    monkeypatch.setattr(file_watcher, "_BUSY", True)
    file_watcher._round(threading.Event())
    assert calls == []


def test_round_does_nothing_once_the_switch_is_off(monkeypatch):
    """运行中途关掉也要能停：不能“不勾了却还在扫盘”"""
    calls = []
    monkeypatch.setattr(fileindex, "enabled", lambda: False)
    monkeypatch.setattr(fileindex, "scan_once", lambda **kw: calls.append(1))
    file_watcher._round(threading.Event())
    assert calls == []


# ---------------- 连接是线程本地的 ----------------

def test_each_thread_gets_its_own_connection(tree):
    """sqlite 连接跨线程用直接抛：后台扫描线程与 UI 查询必须各拿一条"""
    fileindex.scan_once(roots=[str(tree)])
    main_conn = fileindex.connect()
    box = {}

    def work():
        c = fileindex.connect()
        box["same"] = c is main_conn
        box["hits"] = len(fileindex.search_files("爆款拆解"))
        fileindex.close()

    t = threading.Thread(target=work)
    t.start()
    t.join(5)
    assert box.get("same") is False, "两个线程拿到同一条连接，跨线程一用就抛"
    assert box.get("hits") == 1


# ---------------- 按盘归属：单独重扫一个盘，绝不误删另一个盘 ----------------
# 这是「每盘独立时间戳 + 单独立即重扫」能成立的地基：_purge 必须带 root 作用域，
# 不然扫 C 盘那一轮的收尾清理会把 D 盘还没走到的行当残留全删了。

@pytest.fixture
def two_roots(tmp_path):
    a = tmp_path / "driveA"
    b = tmp_path / "driveB"
    _mk(a / "a1.txt", "A 盘一号")
    _mk(a / "sub" / "a2.txt", "A 盘二号")
    _mk(b / "b1.txt", "B 盘一号")
    return a, b


def test_rescan_one_root_does_not_wipe_the_other(two_roots):
    a, b = two_roots
    fileindex.scan_once(roots=[str(a), str(b)])
    assert len(fileindex.search_files("b1")) == 1        # 两盘都先入索引

    # 单独重扫 A：本轮只有 A 的 seen 被刷新，B 的行 root 不匹配，不该被当成残留清掉
    fileindex.scan_once(roots=[str(a)])
    assert len(fileindex.search_files("a1")) == 1
    assert len(fileindex.search_files("b1")) == 1, \
        "重扫 A 盘把 B 盘的索引误删了：_purge 没有按盘作用域"


def test_root_status_records_each_drive_separately(two_roots):
    a, b = two_roots
    fileindex.scan_once(roots=[str(a), str(b)])
    st = {r["root"]: r for r in fileindex.root_status()}
    assert str(a) in st and str(b) in st, "两盘都该各自有一行状态"
    assert st[str(a)]["indexed"] and st[str(b)]["indexed"]
    assert st[str(a)]["files"] >= 2 and st[str(b)]["files"] >= 1
    assert st[str(a)]["last_scan"] > 0 and st[str(b)]["last_scan"] > 0

    # 单独重扫 A：只有 A 的时间戳被推到最新，B 保持不动
    b_last_before = st[str(b)]["last_scan"]
    import time as _time
    _time.sleep(1.1)
    fileindex.scan_once(roots=[str(a)])
    st2 = {r["root"]: r for r in fileindex.root_status()}
    assert st2[str(a)]["last_scan"] > b_last_before, "重扫 A 后 A 的时间戳该往前推"
    assert st2[str(b)]["last_scan"] == b_last_before, "B 没被重扫，时间戳不该变"


def test_interrupted_root_is_not_recorded(tmp_path):
    """被打断的那一盘不写 roots 表：半程数字会骗人（跟「全局时间戳」同一条纪律）。"""
    root = tmp_path / "driveX"
    _mk(root / "f1.txt", "占位")
    stop = threading.Event()
    stop.set()
    fileindex.scan_once(roots=[str(root)], stop=stop)
    assert all(r["root"] != str(root) for r in fileindex.root_status()), \
        "这一盘被提前打断，不该在 roots 表里留下一行成功记录"


# ---------------- 分隔符：Windows 盘符路径不能被 ':' 切开（历史红过的坑） ----------------

@pytest.mark.parametrize("paths", [
    ["D:\\我的文档"],
    ["C:\\Users", "D:\\资料", "E:\\备份"],
    ["/home/user/docs"],
    [],
])
def test_split_join_roots_roundtrip(paths):
    raw = fileindex._join_roots(paths)
    assert fileindex._split_roots(raw) == [str(p) for p in paths], \
        "盘符路径被切开/吞掉了：跨平台分隔符必须与 os.pathsep 无关"


def test_split_roots_reads_legacy_pathsep_string():
    """旧版用 os.pathsep 拼过串（Windows=';'，mac/Linux=':'）：读到非 JSON 的旧串
    也要按 ';' 切，且单段绝不按 ':' 拆——不然又踩回老 bug（mac 上把 'D:\\我的文档'
    斜成 ['D', '\\我的文档']）。"""
    legacy_win = "C:\\Users;D:\\资料"
    assert fileindex._split_roots(legacy_win) == ["C:\\Users", "D:\\资料"]
    legacy_mac_single = "D:\\我的文档"          # 没有 ';'：不当分隔符拆，整段留着
    assert fileindex._split_roots(legacy_mac_single) == ["D:\\我的文档"]
    assert fileindex._split_roots("") == []
    assert fileindex._split_roots(None) == []
