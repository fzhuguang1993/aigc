"""server/ratelimit.py —— 进程内滑动窗口限流（试卡熔断 + 防重放 nonce 表）

单进程 uvicorn 足够：多 worker 部署时限流是"每 worker 各算各的"，
精度下降但方向不变；真正的硬防线是卡密状态机本身（一张卡只能激活一台机器）。
"""
import threading
import time
from collections import defaultdict, deque

_LOCK = threading.Lock()


class SlidingWindow:
    """按 key 计数的滑动窗口：hit() 返回窗口内累计次数（含本次）。"""

    def __init__(self, window):
        self.window = window
        self._hits = defaultdict(deque)

    def hit(self, key):
        now = time.time()
        with _LOCK:
            q = self._hits[key]
            q.append(now)
            cutoff = now - self.window
            while q and q[0] <= cutoff:
                q.popleft()
            return len(q)

    def count(self, key):
        now = time.time()
        with _LOCK:
            q = self._hits[key]
            cutoff = now - self.window
            while q and q[0] <= cutoff:
                q.popleft()
            return len(q)

    def reset(self, key):
        with _LOCK:
            self._hits.pop(key, None)


class NonceCache:
    """已用 nonce 记忆（带过期）：重放检测用，命中即拒绝。

    只存内存：重启后旧 nonce 被"遗忘"，但重启同时刷新了时间窗判定，
    攻击者可重放的窗口 ≤ REPLAY_WINDOW（300 秒），可接受。
    """

    def __init__(self, ttl):
        self.ttl = ttl
        self._seen = {}
        self._last_gc = time.time()

    def seen_or_add(self, nonce):
        """返回 True = 重放（已见过）；False = 首次（已登记）"""
        now = time.time()
        with _LOCK:
            if now - self._last_gc > self.ttl:
                self._last_gc = now
                cutoff = now - self.ttl
                for k in [k for k, v in self._seen.items() if v <= cutoff]:
                    self._seen.pop(k, None)
            if nonce in self._seen:
                return True
            self._seen[nonce] = now
            return False


#: 单 IP 激活尝试限流（每分钟）
ACTIVATE_IP = SlidingWindow(60)
#: 单卡密失败限流（窗口默认 1 小时，配合 activations_log 做持久熔断）
CARD_FAIL = SlidingWindow(3600)
#: 工具接口（提取/翻译）按机器码每分钟限流
TOOL_MACHINE = SlidingWindow(60)
#: 防重放 nonce 表
NONCES = NonceCache(600)
