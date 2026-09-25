"""
tests/test_license.py —— 商用激活体系回归

覆盖：机器码稳定性、license.json 原子读写、激活/复核各状态机、
离线宽限判定、网关鉴权头四件套。全部走 monkeypatch 假服务器，
不碰真网络，也不留缓存串台（autouse 夹具每用例隔离）。
"""
import re
import time

import pytest

import core.license as lic


GATE = "http://gate.test"


class FakePost:
    """替 requests.post：按预设 (status_code, payload) 回应，并记录请求"""

    def __init__(self, status_code=200, payload=None, exc=None):
        self.status_code = status_code
        self.payload = payload or {}
        self.exc = exc
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc

        outer = self

        class R:
            status_code = outer.status_code

            def json(self_inner):
                return outer.payload
        return R()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """license.json 挂临时目录 + 默认网关模式 + 清进程缓存"""
    monkeypatch.setattr(lic, "LICENSE_FILE", tmp_path / "license.json")
    monkeypatch.setattr(lic, "_gateway_base", lambda: GATE)
    monkeypatch.setattr(lic, "_MACHINE", "M" * 32)
    lic.clear_cache()
    yield
    lic.clear_cache()


# ---------------- 机器码 ----------------

def test_machine_code_shape_and_stability(monkeypatch):
    monkeypatch.setattr(lic, "_MACHINE", None)
    code = lic.machine_code()
    assert re.fullmatch(r"[0-9A-F]{32}", code)
    assert lic.machine_code() == code            # 进程内恒定


# ---------------- 网关地址口径 ----------------

def test_gateway_root_strips_api_suffix(monkeypatch):
    monkeypatch.setattr(lic, "_gateway_base", lambda: GATE + "/api/v1")
    assert lic.gateway_root() == GATE
    assert lic.gateway_api_base() == GATE + "/api/v1"
    assert lic.gateway_mode() is True


def test_no_gateway_means_dev_passthrough(monkeypatch):
    monkeypatch.setattr(lic, "_gateway_base", lambda: "")
    assert lic.gateway_mode() is False
    ok, info = lic.check_on_startup()
    assert ok and info.get("mode") == "dev"
    assert lic.request_headers() == {}           # 直连不多带一个头


# ---------------- 激活 ----------------

def test_activate_success_saves_cache(monkeypatch):
    exp = int(time.time()) + 30 * 86400
    fake = FakePost(200, {"token": "tk.sig", "card_key": "ABCD",
                          "expire_at": exp, "days_remaining": 30,
                          "offline_grace_days": 3})
    monkeypatch.setattr("requests.post", fake)
    ok, info = lic.activate("  abcd  ")
    assert ok and info["days_remaining"] == 30
    cached = lic._load()
    assert cached["token"] == "tk.sig" and cached["expire_at"] == exp
    assert cached["machine"] == "M" * 32
    assert cached["offline_grace"] == 3 * 86400
    url, kw = fake.calls[0]
    assert url == f"{GATE}/auth/activate"
    assert kw["json"]["card_key"] == "abcd"       # 只去首尾空白，其余原样交服务端归一
    assert lic.local_valid() is True


def test_activate_surfaces_server_message(monkeypatch):
    fake = FakePost(409, {"detail": {"reason": "machine_mismatch",
                                     "message": "该卡密已在其他电脑使用"}})
    monkeypatch.setattr("requests.post", fake)
    ok, msg = lic.activate("ABCD")
    assert ok is False and msg == "该卡密已在其他电脑使用"
    assert not lic.local_valid()


def test_activate_network_error_is_message_not_crash(monkeypatch):
    import requests
    fake = FakePost(exc=requests.ConnectionError("refused"))
    monkeypatch.setattr("requests.post", fake)
    ok, msg = lic.activate("ABCD")
    assert ok is False and "无法连接激活服务器" in msg


# ---------------- 联网复核状态机 ----------------

def _seed(expire_at, grace=3 * 86400):
    lic._save({"card_key": "C", "machine": "M" * 32, "token": "tk.sig",
               "expire_at": expire_at, "last_verify": 0,
               "offline_grace": grace})


def test_verify_ok_refreshes_cache(monkeypatch):
    _seed(int(time.time()) - 10)                  # 本地看似已过期
    new_exp = int(time.time()) + 7 * 86400        # 服务端其实续过了
    monkeypatch.setattr("requests.post",
                        FakePost(200, {"expire_at": new_exp, "days_remaining": 7}))
    state, info = lic.verify_online()
    assert state == "ok"
    assert lic._load()["expire_at"] == new_exp    # 以服务端为准回写
    assert lic.needs_recheck() is False


def test_verify_expired_and_banned(monkeypatch):
    _seed(int(time.time()) + 86400)
    monkeypatch.setattr("requests.post", FakePost(
        403, {"detail": {"reason": "expired", "message": "授权已到期"}}))
    assert lic.verify_online()[0] == "expired"
    monkeypatch.setattr("requests.post", FakePost(
        403, {"detail": {"reason": "banned", "message": "机器码已被封禁"}}))
    assert lic.verify_online()[0] == "invalid"


def test_verify_network_down_returns_net_error(monkeypatch):
    import requests
    _seed(int(time.time()) + 86400)
    monkeypatch.setattr("requests.post",
                        FakePost(exc=requests.Timeout("t")))
    assert lic.verify_online()[0] == lic.NET_ERROR


# ---------------- 离线宽限 ----------------

def test_offline_grace_window():
    now = int(time.time())
    _seed(now - 86400, grace=3 * 86400)           # 过期 1 天，宽限 3 天
    assert lic.offline_ok() is True
    _seed(now - 5 * 86400, grace=3 * 86400)       # 超出宽限
    assert lic.offline_ok() is False
    lic.clear_cache()
    assert lic.offline_ok() is False              # 压根没激活过


def test_check_on_startup_offline_paths(monkeypatch):
    now = int(time.time())
    _seed(now - 86400, grace=3 * 86400)
    monkeypatch.setattr("requests.post",
                        FakePost(exc=__import__("requests").ConnectionError("x")))
    ok, info = lic.check_on_startup()
    assert ok and info["mode"] == "offline"
    _seed(now - 10 * 86400, grace=3 * 86400)
    ok, info = lic.check_on_startup()
    assert not ok and "宽限" in info


def test_check_on_startup_requires_activation(monkeypatch):
    ok, info = lic.check_on_startup()             # 网关模式但从未激活
    assert not ok and "未激活" in info


# ---------------- 鉴权头 ----------------

def test_request_headers_four_pieces_unique_nonce(monkeypatch):
    _seed(int(time.time()) + 86400)
    h1 = lic.request_headers()
    h2 = lic.request_headers()
    assert set(h1) == {"X-License", "X-Machine", "X-Ts", "X-Nonce"}
    assert h1["X-License"] == "tk.sig" and h1["X-Machine"] == "M" * 32
    assert int(h1["X-Ts"]) > 0
    assert h1["X-Nonce"] != h2["X-Nonce"]         # 每次新 nonce，防重放


def test_request_headers_empty_without_token():
    assert lic.request_headers() == {}            # 未激活：不带残缺头


# ---------------- 到期显示 ----------------

def test_days_remaining_rounds_up():
    _seed(int(time.time()) + 86400 * 3 + 60)
    assert lic.days_remaining() == 4              # 3 天零 1 分钟＝“还能用 4 个自然日”
    _seed(int(time.time()) - 60)
    assert lic.days_remaining() == 0
