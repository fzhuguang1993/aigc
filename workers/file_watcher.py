"""
workers/file_watcher.py —— 启动即后台建索引：全盘文件名增量扫 + 文档正文抽取

为什么是"软件启动时扫"而不是"第一次用搜索才扫"（用户点名要的）：首扫压在第一次
敲字上就等同"敲了没反应"；启动后趁没人用把它跑完，面板打开时索引已经在那儿了。

⚠ 节奏不是拍脑袋的 120 秒（这是本次修好的一个真 bug）：旧注释按"一轮 7.8 秒"
定的间隔，而本机实测**单轮要 65.5 秒**（光是 os.scandir 走访），加上逐目录提交
实际 100~200 秒——比 120 秒的间隔还长，于是这条线程**永远没有空闲**，4TB 叠瓦盘
常年顶满队列，整机 IO 排队。现在的规矩：

1. 间隔跟着上一轮的实际耗时走（next_interval），至少留出 1:1 的空闲；
2. 默认只索引固态盘（机械盘由 core.fileindex.local_drives 剔掉，见那边说明）；
3. 每轮都往日志写一行心跳（以前一个字都不打，卡死了也没证据可查）。

线程用裸 threading.Thread(daemon=True)，与 main_window._license_tick 同一个先例：
这条线程只管埋头跑，不往界面发信号，套 QThread 反而多背一层事件循环。
真要回主线程刷界面的地方（设置页状态行）在 gui 里，那边自己用
QTimer.singleShot(0, ...) 走项目既有惯例。
"""
from __future__ import annotations

import atexit
import threading
import time

#: 首扫之前等多久：窗口正在画第一帧、库正在建表，这时候整盘 scandir 会拖慢启动
STARTUP_DELAY = 3
#: 两轮之间的**下限**：一轮很快时也不必更密，刚存的文件 2 分钟内能搜到就够
ROUND_SEC = 120
#: 两轮之间的上限：机器再慢也不能让索引几天不动（15 分钟一轮是"还活着"的底线）
ROUND_MAX = 900
#: 每批目录之间睡多久（scan_once 每 200 个目录提交一次）：把 IO 摊平，
#: 别把整块盘的队列顶死。12 万个目录约 600 批，合计多花十几秒，换来的是
#: 用的时候不卡——这笔账在叠瓦盘上尤其划算。
SCAN_PACE = 0.02
#: 开机第一拍走“快速检查”（用户点名修的）：以前一起动就无条件把所有盘重扫一遍、
#: 把所有文档重抽一遍，于是“每次启动都在强制检查索引”，开机那一刻磁盘队列吃满。
#: 现在文件名只补真正到期的盘（fileindex.due_roots），正文够新就整段跳过。
#: 后面的轮次照旧走完整增量轮：一轮完整的差分本来也不算贵（签名没变的目录直接跳过）。
FIRST_ROUND_QUICK = True

_LOCK = threading.Lock()
_THREAD = None
_STOP = None
_KICK = None                  # 设置页按「立即重建索引」用它把下一轮提前
_STATS = {"rounds": 0, "scans": 0, "docs": 0, "last": "", "error": "",
          "last_sec": 0.0, "next_sec": ROUND_SEC}
_BUSY = False                 # 防重入：一轮没跑完就不再开第二轮
#: 定向重扫待办队列（索引设置页“立即重扫此盘/索引此盘文档”塞进来，loop 下一拍优先消费）：
#: 每项是 (kind, roots)，kind 为 "scan"（只重扫文件名）或 "docs"（只抽正文）；
#: 保持单写者纪律——不另起并发线程，交给常驻线程同一把 _BUSY 锁串行跑。
_PENDING = []
_PENDING_LOCK = threading.Lock()


def enabled():
    """总开关（设置页那个勾）：关掉就不该在后台扫盘"""
    from core import fileindex
    return fileindex.enabled()


def running():
    t = _THREAD
    return t is not None and t.is_alive()


def start():
    """装好后台线程；已经在跑 / 功能关了 / 开关关了 → 什么都不做。

    重复调用是安全的（main_window 与设置页都可能调），所以这里敢直接挂上。"""
    global _THREAD, _STOP, _KICK
    from core import fileindex
    if not fileindex.enabled():
        return False
    with _LOCK:
        if running():
            return False
        _STOP = threading.Event()
        _KICK = threading.Event()
        _THREAD = threading.Thread(target=_loop, args=(_STOP, _KICK),
                                   daemon=True, name="file-index")
        _THREAD.start()
    atexit.register(stop)          # 崩了/直接退出都尽量把线程停干净
    _log("本地文件索引：后台线程已启动，索引范围 %s"
         % ("、".join(str(r) for r in fileindex.search_roots()) or "空"))
    return True


def stop(timeout=2.0):
    """置位退出。不 join 死等：全盘扫描中途可能卡在一条慢盘上，
    用户要退出时不该被后台线程拖住（daemon 线程本来就会随进程走）。"""
    st = _STOP
    if st is not None:
        st.set()
    t = _THREAD
    if t is not None and t.is_alive():
        t.join(timeout)
    return True


def kick():
    """让下一轮提前开始（重建索引 / 改了目录范围后用），不用等那个间隔"""
    k = _KICK
    if k is not None:
        k.set()
        return True
    return False


def _requeue(req):
    """把定向请求放回待办（去重）：本轮因正忙/未开没跑成时不能丢。"""
    with _PENDING_LOCK:
        for item in req:
            if item not in _PENDING:
                _PENDING.append(item)


def _take_pending():
    """取走并清空当前待办（一次取干净，避免同一轮反复碰队列）。"""
    with _PENDING_LOCK:
        req = list(_PENDING)
        del _PENDING[:]
        return req


def has_pending():
    with _PENDING_LOCK:
        return bool(_PENDING)


def busy():
    """后台正在跑一轮（无论全局还是定向）：索引设置页据此显“进度/在跑”。"""
    return _BUSY


def _submit(kind, roots):
    """定向请求入队的公共路子：全程异步（塞队列 + 踢醒后台线程），**不阻塞调用线程**。

    返回后台线程是否接手（False 多半是总开关关了，调用方可据此提示“请先启用
    本地文件搜索”）。roots 没点名的时候两档不同：文件名请求当非法调用处理（设置页
    总是带盘符过来，空手就等于“什么都没选”），正文请求按 doc_roots() 全集跑。"""
    from core import fileindex
    req = [str(r) for r in (roots or []) if str(r).strip()]
    if not req and kind == "docs":
        req = [str(r) for r in fileindex.doc_roots()]
    if not req or not fileindex.enabled():
        return False
    _requeue([(kind, tuple(req))])
    if not running():
        start()
    return kick()


def reindex_roots(roots):
    """请求只重扫指定盘/根的文件名（“立即重扫此盘/所选盘”）。"""
    return _submit("scan", roots)


def reindex_docs(roots=None):
    """请求只重抽指定盘/根的文档正文（“索引此盘文档”）。

    以前根本没这个入口：定向重扫那一档 run_once 直接跳过正文，所以“按盘扫文档”
    点了没反应；而不定向的那一档一抽就是 doc_roots() 全集，抽 C 盘顺手把 D 盘的
    正文索引清掉了（见 core.fileindex._prune_docs）。"""
    return _submit("docs", roots)


def stats():
    """给设置页状态行与日志用：跑过几轮、上一轮收了什么、下一轮等多久"""
    return dict(_STATS)


def next_interval():
    """下一轮该等多久：至少 ROUND_SEC，但**必须容得下上一轮真的跑了多久**。

    单轮比间隔长还按固定间隔催，就等于让扫描线程一刻不停——本机就是这么把
    一块 4TB 叠瓦盘顶满的（间隔 120 秒、单轮实测 100~200 秒）。这里按 1:1
    占空比留空闲，并夹在 [ROUND_SEC, ROUND_MAX] 里：机器再慢也保证索引
    每 15 分钟一定动一次。"""
    last = float(_STATS.get("last_sec") or 0)
    want = max(ROUND_SEC, round(last))
    _STATS["next_sec"] = min(want, ROUND_MAX)
    return _STATS["next_sec"]


def run_once(stop=None, scan=True, docs=True, roots=None, quick=False):
    """同步跑一轮（测试与“立即重建索引”用）；返回各步统计。

    quick=True 是启动首轮的快速检查档（用户点名修的“每次启动都在强制检查索引”）：
    文件名只补 fileindex.due_roots() 里真到期的盘，一个都没到期就不扫；正文只在
    距上次抽取超过 fileindex.DOCS_DUE_SEC 时才动。默认 False，跟老行为一致。

    roots 不为空就是定向重扫：只扫这几个盘，且这一档不顺手抽正文（“重扫此盘”
    不该变成“抽干整盘文档”，抽正文有自己的入口 reindex_docs）。反过来 quick 只收窄
    文件名那一档，正文照旧按 doc_roots() 自己的名单走，免得“只扫了几个盘”被当成
    “只抽这几个盘的正文”，把另几个盘的新文档始终落在索引外。"""
    from core import fileindex
    out = {}
    scan_roots = roots                  # 文件名这一档走哪些根
    if quick and roots is None:
        scan_roots = fileindex.due_roots()
        out["due"] = len(scan_roots)
        if not scan_roots:
            scan = False                # 一个盘都没到期：这一拍连目录都不用走
        if docs and not fileindex.docs_due():
            docs = False
    if not scan and not docs:
        return out
    if scan:
        if scan_roots is not None:
            out["scan"] = fileindex.scan_once(roots=scan_roots, stop=stop, pace=SCAN_PACE)
        else:
            out["scan"] = fileindex.scan_once(stop=stop, pace=SCAN_PACE)
        _STATS["scans"] += 1
    if docs and fileindex.doc_enabled() \
            and not (stop is not None and stop.is_set()):
        # 以前这里还带一句 `roots is None`：定向重扫那一档永远不抽正文，
        # “索引此盘文档”点了就是没反应（这是按盘文档扫描坏掉的第一半）
        if roots is not None:
            out["docs"] = fileindex.index_docs(roots=roots, stop=stop)
        else:
            out["docs"] = fileindex.index_docs(stop=stop)
        _STATS["docs"] += 1
    _STATS["rounds"] += 1
    _STATS["last"] = ("%s文件 %s文档"
                      % ((out.get("scan") or {}).get("files", "-"),
                         (out.get("docs") or {}).get("indexed", "-")))
    return out


def _loop(stop, kick):
    if stop.wait(STARTUP_DELAY):
        return
    quick = bool(FIRST_ROUND_QUICK)     # 开机第一拍走快速检查，之后恢复完整增量轮
    while not stop.is_set():
        req = _take_pending()
        if req:
            # 定向重扫优先：拿不到写锁/还没开就放回队列，不能把用户请求丢了
            _dispatch(req, stop)
        else:
            _round(stop, quick=quick)
            quick = False
        # 等到下一轮，或者被 kick 提前叫醒（重建索引 / 改了范围 / 定向重扫）
        kick.wait(next_interval())
        kick.clear()


def _dispatch(req, stop):
    """按提交顺序串行消费一批定向请求；有一项没跑成（正忙/开关关了）就把剩下
    的连同它一起放回队列——单写者纪律不变，也不会把用户点过的请求吞掉。"""
    for i, (kind, roots) in enumerate(req):
        if not _round(stop, roots=list(roots), kind=kind):
            _requeue(req[i:])
            return False
    return True


def _round(stop, roots=None, kind="all", quick=False):
    """一轮扫描：重入直接跳过（宁可这轮不跑，也不能两个线程同写一份库）。

    返回是否真的跑了（False=因正忙/已开关而跳过），供上层决定要不要把定向
    请求放回队列。roots 不为空就是定向请求；kind 分“只扫文件名”与“只抽正文”
    两档（定向请求都按档走，免得一按“重扫”就顺手把整盘文档抽一遍）；quick 是
    启动首轮那一下。"""
    global _BUSY
    from core import fileindex
    if _BUSY:
        return False
    if not fileindex.enabled():
        return False
    _BUSY = True
    t0 = time.perf_counter()
    out = {}
    try:
        if kind == "docs":
            out = run_once(stop=stop, scan=False, docs=True, roots=roots)
        elif roots is not None:
            out = run_once(stop=stop, scan=True, docs=False, roots=roots)
        else:
            out = run_once(stop=stop, quick=quick)
        _STATS["error"] = ""
    except Exception as e:                      # 后台线程绝不能把进程带崩
        _STATS["error"] = "%s: %s" % (type(e).__name__, e)
        _log("本地文件索引：本轮失败 %s" % _STATS["error"], warning=True)
    finally:
        _BUSY = False
        _STATS["last_sec"] = round(time.perf_counter() - t0, 1)
    _heartbeat(out, roots=roots, kind=kind)
    return True


def _heartbeat(out, roots=None, kind="all"):
    """每轮写一行心跳。以前一个字都不打：用户说"电脑卡死"，日志里连一条
    "索引跑了多久"都翻不出来，只能靠外部探针现场量。有了这一行，下次直接看日志。"""
    sc = out.get("scan") or {}
    dc = out.get("docs") or {}
    sec = _STATS["last_sec"]
    if kind == "docs":
        tag = "文档定向抽取"
    elif roots is not None:
        tag = "定向重扫"
    elif out.get("due") is not None:
        tag = "快速检查（到期 %d 个盘）" % out["due"]
    else:
        tag = "第 %d 轮" % _STATS["rounds"]
    msg = ("本地文件索引：%s 用时 %.1fs｜走访 %s 目录 收录 %s 文件"
           "（新增 %s 移除 %s）｜正文 %s 篇｜下轮 %ds 后"
           % (tag, sec, _fmt(sc.get("dirs")), _fmt(sc.get("files")),
              _fmt(sc.get("added")), _fmt(sc.get("removed")),
              _fmt(dc.get("indexed")), next_interval()))
    # 一轮顶到间隔以上就是把机器顶满了，值得单独喊一句（设置页也看得到这一行）
    _log(msg, warning=sec > ROUND_SEC)
    if sec > ROUND_SEC:
        _log("本地文件索引：单轮 %.1fs 已超过间隔下限，已把下一轮延后（后台扫描"
             "不该把磁盘队列顶满）；若一直这样，去设置页缩小索引范围" % sec,
             warning=True)


def _fmt(v):
    try:
        return "{:,}".format(int(v))
    except (TypeError, ValueError):
        return "-"


def _log(msg, warning=False):
    try:
        from core.logger import log
        (log.warning if warning else log.info)(msg)
    except Exception:
        pass                        # 日志层的问题绝不能带崩后台线程
