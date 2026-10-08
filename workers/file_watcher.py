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

_LOCK = threading.Lock()
_THREAD = None
_STOP = None
_KICK = None                  # 设置页按「立即重建索引」用它把下一轮提前
_STATS = {"rounds": 0, "scans": 0, "docs": 0, "last": "", "error": "",
          "last_sec": 0.0, "next_sec": ROUND_SEC}
_BUSY = False                 # 防重入：一轮没跑完就不再开第二轮


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


def run_once(stop=None, scan=True, docs=True):
    """同步跑一轮（测试与「立即重建索引」用）；返回各步统计"""
    from core import fileindex
    out = {}
    if not scan and not docs:
        return out
    if scan:
        out["scan"] = fileindex.scan_once(stop=stop, pace=SCAN_PACE)
        _STATS["scans"] += 1
    if docs and fileindex.doc_enabled() and not (stop is not None and stop.is_set()):
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
    while not stop.is_set():
        _round(stop)
        # 等到下一轮，或者被 kick 提前叫醒（重建索引 / 改了范围）
        kick.wait(next_interval())
        kick.clear()


def _round(stop):
    """一轮扫描：重入直接跳过（宁可这轮不跑，也不能两个线程同时写同一份库）"""
    global _BUSY
    from core import fileindex
    if _BUSY:
        return
    if not fileindex.enabled():
        return
    _BUSY = True
    t0 = time.perf_counter()
    out = {}
    try:
        out = run_once(stop=stop)
        _STATS["error"] = ""
    except Exception as e:                      # 后台线程绝不能把进程带崩
        _STATS["error"] = "%s: %s" % (type(e).__name__, e)
        _log("本地文件索引：本轮失败 %s" % _STATS["error"], warning=True)
    finally:
        _BUSY = False
        _STATS["last_sec"] = round(time.perf_counter() - t0, 1)
    _heartbeat(out)


def _heartbeat(out):
    """每轮写一行心跳。以前一个字都不打：用户说"电脑卡死"，日志里连一条
    "索引跑了多久"都翻不出来，只能靠外部探针现场量。有了这一行，下次直接看日志。"""
    sc = out.get("scan") or {}
    dc = out.get("docs") or {}
    sec = _STATS["last_sec"]
    msg = ("本地文件索引：第 %d 轮 用时 %.1fs｜走访 %s 目录 收录 %s 文件"
           "（新增 %s 移除 %s）｜正文 %s 篇｜下轮 %ds 后"
           % (_STATS["rounds"], sec, _fmt(sc.get("dirs")), _fmt(sc.get("files")),
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
