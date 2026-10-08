"""
core/crashdump.py —— 崩溃留痕：native 层一崩就把 Python 栈写进日志

为什么要有这个模块（这次排查的直接教训）：程序反复崩溃，Python 只丢下一句
"Fatal Python error: segfault"，自己的日志里连一行线索都没有——真凶（Qt 的
qFatal）只能去 %LOCALAPPDATA%\\CrashDumps 拿 cdb 解 minidump 才看得见。
两条手段把成本压成"下次不用再挖 dump"：

1. **faulthandler**：SIGSEGV / SIGABRT / SIGFPE / SIGILL / SIGBUS 一触发就把
   所有线程的 Python 栈打进 logs/crash.log。Qt 的 qFatal 走 abort() → SIGABRT，
   正好接得住（这次两个崩溃签名都是 qFatal，不是内存越界）。
2. **qInstallMessageHandler**：把 Qt 的告警与致命消息（"QThread: Destroyed while
   thread is still running"、"Cannot use a QPainter ..."）转写进自己的日志。
   qFatal 是**先发消息再 abort**，所以这行字比栈更早落盘，一眼能看出是哪条
   纪律被破了。

任何异常都吞掉：诊断设施把自己搞崩就是帮倒忙。core 层平时不碰 Qt，
所以 Qt 那一步是函数内懒导入，装不上就算了（faulthandler 照旧生效）。
"""
from __future__ import annotations

import os
import sys

#: 崩溃栈落到这个文件（与 aigc.log 同目录，翻日志时顺手就能找到）
CRASH_FILE = "crash.log"

_installed = False
_fh_file = None                  # 必须留住：句柄被 GC 掉 faulthandler 就写不出东西
_prev_hook = None
_qt_handler = None               # qInstallMessageHandler 传进去的回调要有引用才不被回收


def _log():
    try:
        from core.logger import log
        return log
    except Exception:
        return None


def _enable_faulthandler(log_dir):
    """把 native 信号的 Python 栈写到 logs/crash.log（追加，多次崩溃都留着）"""
    global _fh_file
    try:
        import faulthandler
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, CRASH_FILE)
        _fh_file = open(path, "a", encoding="utf-8", errors="replace")
        _fh_file.write("\n==== 进程启动 %s pid=%s ====\n"
                       % (_now(), os.getpid()))
        _fh_file.flush()
        faulthandler.enable(file=_fh_file, all_threads=True)
        return path
    except Exception:
        return ""


def _now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _excepthook(etype, value, tb):
    """未捕获的 Python 异常也进日志（原来只往 stderr 一喷，日志里查不到）。
    打完仍然转交上一个钩子，PySide 自己的行为不改。"""
    try:
        log = _log()
        if log is not None:
            log.error("未捕获异常", exc_info=(etype, value, tb))
    except Exception:
        pass
    try:
        if _prev_hook is not None:
            _prev_hook(etype, value, tb)
    except Exception:
        pass


def _install_qt_bridge():
    """Qt 的告警/致命消息转写进自己的日志。懒导入：core 层不硬依赖 PySide6。"""
    global _qt_handler
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        return False
    names = {
        QtMsgType.QtDebugMsg: "debug",
        QtMsgType.QtInfoMsg: "info",
        QtMsgType.QtWarningMsg: "warning",
        QtMsgType.QtCriticalMsg: "error",
        QtMsgType.QtFatalMsg: "critical",
    }

    def _handler(msg_type, context, message):
        try:
            log = _log()
            if log is None:
                return
            # qFatal 是"先发这条、再 abort"：致命消息必须当场落盘，
            # 否则进程一死缓冲区里的东西就没了，等于白装。
            sev = names.get(msg_type, "warning")
            getattr(log, sev, log.warning)("Qt[%s:%d %s] %s" % (
                os.path.basename(str(context.file or "?")),
                int(context.line or 0),
                str(context.function or "").split("(")[0], message))
            if msg_type in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                for h in list(log.handlers):
                    try:
                        h.flush()
                    except Exception:
                        pass
        except Exception:
            pass                        # 日志层的问题绝不能带崩 Qt

    _qt_handler = _handler
    try:
        qInstallMessageHandler(_handler)
        return True
    except Exception:
        return False


def enable(log_dir=None):
    """装上崩溃留痕，返回 crash.log 路径（装不上返回 ""）。重复调用只装一次。"""
    global _installed, _prev_hook
    if _installed:
        return ""
    _installed = True
    if not log_dir:
        try:
            from core.config import LOG_DIR as log_dir
        except Exception:
            log_dir = "logs"
    path = _enable_faulthandler(log_dir)
    _prev_hook = sys.excepthook
    try:
        sys.excepthook = _excepthook
    except Exception:
        pass
    _install_qt_bridge()
    return path
