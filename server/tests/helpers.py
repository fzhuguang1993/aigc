"""server/tests/helpers.py —— 测试公共小工具（假响应/卡密/鉴权头）"""
import time
import uuid

from server import cards


class FakeResp:
    """顶替 httpx.Response：只要 status_code/json/text 三件套"""

    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self):
        return self._payload


def make_card(days=30, count=1):
    """造 count 张未激活卡，返回卡密列表"""
    keys, _ = cards.generate(days, count)
    return keys


def activate(client, card_key, machine="MACH-A"):
    r = client.post("/auth/activate",
                    json={"card_key": card_key, "machine_code": machine,
                          "app_version": "test"})
    assert r.status_code == 200, r.text
    return r.json()


def auth_headers(token, machine="MACH-A", nonce=None, ts=None):
    """默认 nonce 每次唯一（uuid）：要测重放就显式传固定值"""
    return {"X-License": token, "X-Machine": machine,
            "X-Ts": str(int(time.time()) if ts is None else ts),
            "X-Nonce": nonce or uuid.uuid4().hex}
