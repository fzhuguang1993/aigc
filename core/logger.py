"""
logger.py —— 日志系统
"""
import os
import logging
from logging.handlers import TimedRotatingFileHandler

from core.config import LOG_DIR, LOG_LEVEL_FILE, LOG_LEVEL_CONSOLE, LOG_RETENTION_DAYS


def _setup_logger():
    logger = logging.getLogger("aigc")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, "aigc.log")

    fh = TimedRotatingFileHandler(log_file, when="midnight",
                                  backupCount=LOG_RETENTION_DAYS, encoding="utf-8")
    fh.suffix = "%Y-%m-%d.log"
    fh.setLevel(getattr(logging, LOG_LEVEL_FILE))
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, LOG_LEVEL_CONSOLE))
    ch.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"))
    logger.addHandler(ch)

    return logger


log = _setup_logger()


def raw_info(msg):
    log.info(msg)


def raw_warning(msg):
    log.warning(msg)


def raw_error(msg):
    log.error(msg)


def raw_debug(msg):
    log.debug(msg)


class Ctx:
    def __init__(self, row=None, account=None, job_id=None):
        self.row = row
        self.account = account
        self.job_id = job_id

    def _prefix(self):
        parts = []
        if self.row is not None:
            parts.append(f"行{self.row + 1}")
        if self.account:
            parts.append(self.account)
        parts.append(self.job_id[:8] if self.job_id else "-")
        return "[" + "][".join(parts) + "]"

    def info(self, msg):
        log.info(f"{self._prefix()} {msg}")

    def warning(self, msg):
        log.warning(f"{self._prefix()} {msg}")

    def error(self, msg):
        log.error(f"{self._prefix()} {msg}")

    def debug(self, msg):
        log.debug(f"{self._prefix()} {msg}")
