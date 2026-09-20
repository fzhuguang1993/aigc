"""
gui/log_sink.py —— 把 aigc 日志接入 GUI 日志窗
"""
import logging
import threading

_buf = []
_lock = threading.Lock()


class _GuiHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        with _lock:
            _buf.append(msg)
            if len(_buf) > 500:
                del _buf[:-500]


_installed = False


def install():
    global _installed
    if _installed:
        return
    h = _GuiHandler()
    h.setLevel(logging.INFO)
    h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logging.getLogger("aigc").addHandler(h)
    _installed = True


def drain():
    with _lock:
        out, _buf[:] = _buf[:], []
    return out
