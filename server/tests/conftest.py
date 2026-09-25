"""server/tests/conftest.py —— 网关服务端测试夹具

约定：
- 必须早于任何 server.* 导入设置 AIGC_GATE_* 环境变量（config 在 import 时固化）；
- 每个用例新库 + 空限流表 + 假线路池，绝不碰真实数据；
- 所有上游调用经 upstream._http / gateway 里的 httpx 假实现，不起真服务、不外联。
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="gate_test_")
os.environ["AIGC_GATE_DATA"] = _TMP
os.environ["AIGC_GATE_SECRET"] = "test-secret-0123456789abcdef"
os.environ["AIGC_GATE_ADMIN_TOKEN"] = "admin-test-token"
os.environ["AIGC_GATE_CONFIG"] = str(Path(_TMP) / "nonexistent.json")
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))      # 仓库根：import server.*
sys.path.insert(0, str(_HERE))                  # 本目录：import helpers

import pytest  # noqa: E402

from server import config, store, upstream  # noqa: E402
from server import ratelimit  # noqa: E402
from helpers import FakeResp  # noqa: E402  （本目录已入 sys.path）


@pytest.fixture(autouse=True)
def _fresh_state(tmp_path, monkeypatch):
    """每用例：新库、清空限流/nonce、装两条假线路、关掉探活与启动重载"""
    config.DB_PATH = str(tmp_path / "gate.db")
    store.reset()
    store.init_db()
    for w in (ratelimit.ACTIVATE_IP, ratelimit.CARD_FAIL, ratelimit.TOOL_MACHINE):
        w._hits.clear()
    ratelimit.NONCES._seen.clear()

    upstream.UPSTREAMS = [
        upstream.Upstream({"name": "acc1", "base": "http://mock-upstream/acc1/api/v1",
                           "concurrency": 2}),
        upstream.Upstream({"name": "acc2", "base": "http://mock-upstream/acc2/api/v1",
                           "concurrency": 3}),
    ]
    upstream.MATERIAL_API = {"base": "http://mock-upstream/juke",
                             "uid": "u1", "key": "k1"}
    upstream.TRANSLATE_API = {"ak": "test-ak", "sk": "test-sk"}
    # 探活与 startup 重载都会踩掉上面的假池：一并钉死
    monkeypatch.setattr(upstream, "check_all", lambda: None)
    monkeypatch.setattr(upstream, "load_upstreams", lambda path=None: upstream.UPSTREAMS)
    yield


@pytest.fixture()
def client(_fresh_state):
    """TestClient：with 语句触发 startup（load/probe 已被 _fresh_state 钉死）"""
    from fastapi.testclient import TestClient
    from server.app import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def cloud(monkeypatch):
    """假上游集合：upstream._http 的全部出入都落这里，可断言可编排。

    用法：cloud.on("POST", "/url 前缀", FakeResp(...) 或 fn)；
    复杂编排直接换 cloud.handler = fn(method, url, kw) -> FakeResp（可抛 UpstreamError）。
    """
    class Cloud:
        def __init__(self):
            self.calls = []           # (method, url, kwargs)
            self.responses = []       # (method, 前缀匹配, resp 或 fn)
            self.handler = None

        def on(self, method, url_prefix, resp):
            self.responses.append((method, url_prefix, resp))

        def http(self, method, url, **kw):
            self.calls.append((method, url, kw))
            if self.handler:
                return self.handler(method, url, kw)
            for m, prefix, resp in reversed(self.responses):
                if m == method and url.startswith(prefix):
                    return resp() if callable(resp) else resp
            return FakeResp({})

    c = Cloud()
    monkeypatch.setattr(upstream, "_http", c.http)
    return c
