"""server/tests/test_gate_auth.py —— 卡密激活与令牌体系

覆盖商用最在意的路径：一张卡只活在一台机器、续费叠加、试卡熔断、
封禁/作废即时生效、验签不可伪造。
"""
import time

from server import store
from helpers import activate, auth_headers, make_card


class TestCards:
    def test_normalize_accepts_loose_input(self):
        from server.cards import normalize
        key = make_card(30)[0]
        assert normalize(key) == key
        assert normalize(key.replace("-", "").lower()) == key
        assert normalize("  " + key + "  ") == key
        assert normalize("短") is None
        assert normalize("0OOO-1IL1-ABCD-EFGH") is None      # 剔除字符集外

    def test_generate_unique_and_persisted(self):
        keys = make_card(7, 25)
        assert len(set(keys)) == 25
        assert all(store.get_card(k) for k in keys)


class TestActivate:
    def test_fresh_card_binds_machine(self, client):
        key = make_card(30)[0]
        out = activate(client, key, "MACH-A")
        assert out["expire_at"] > time.time() + 29 * 86400
        card = store.get_card(key)
        assert card["status"] == "used" and card["machine_code"] == "MACH-A"
        assert out["days_remaining"] == 30

    def test_same_card_other_machine_rejected(self, client):
        key = make_card(30)[0]
        activate(client, key, "MACH-A")
        r = client.post("/auth/activate",
                        json={"card_key": key, "machine_code": "MACH-B"})
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "other_machine"

    def test_same_machine_reentry_is_idempotent(self, client):
        key = make_card(30)[0]
        first = activate(client, key, "MACH-A")
        again = activate(client, key, "MACH-A")
        assert again["expire_at"] == first["expire_at"]      # 不叠天数

    def test_renew_stacks_on_current_expiry(self, client):
        a, b = make_card(30, 2)
        first = activate(client, a, "MACH-A")
        second = activate(client, b, "MACH-A")               # 续费=再输一张新卡
        assert second["expire_at"] >= first["expire_at"] + 29 * 86400

    def test_unknown_card_404(self, client):
        r = client.post("/auth/activate",
                        json={"card_key": "ABCD-EFGH-JKMN-PQRS", "machine_code": "M"})
        assert r.status_code == 404

    def test_bad_format_rejected(self, client):
        r = client.post("/auth/activate",
                        json={"card_key": "xxx", "machine_code": "M"})
        assert r.status_code == 404

    def test_disabled_card_rejected(self, client):
        key = make_card(30)[0]
        store.set_card_status(key, "disabled")
        r = client.post("/auth/activate",
                        json={"card_key": key, "machine_code": "MACH-A"})
        assert r.status_code == 403

    def test_ip_rate_limit(self, client):
        """同 IP 每分钟超过阈值后 429——脚本批量试卡的第一道闸"""
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ"
        for i in range(10):          # 10 次都拿不存在的卡：每次只计 IP 额度
            r = client.post("/auth/activate",
                            json={"card_key": "AAAA-AAAA-AAAA-AAA" + chars[i],
                                  "machine_code": "MACH-X"})
            assert r.status_code == 404, i
        r = client.post("/auth/activate",
                        json={"card_key": "AAAA-AAAA-AAAA-AAA" + chars[10],
                              "machine_code": "MACH-X"})
        assert r.status_code == 429

    def test_card_fail_lockout(self, client):
        """同一张错卡敲错 5 次后锁定 1 小时（哪怕换了正确姿势也先吃 429）"""
        bad = "QQQQ-QQQQ-QQQQ-QQQQ"
        for i in range(5):
            r = client.post("/auth/activate",
                            json={"card_key": bad, "machine_code": "MACH-Y"})
            assert r.status_code == 404, i
        r = client.post("/auth/activate",
                        json={"card_key": bad, "machine_code": "MACH-Y"})
        assert r.status_code == 429


class TestVerify:
    def test_verify_roundtrip(self, client):
        key = make_card(30)[0]
        tok = activate(client, key)["token"]
        r = client.post("/auth/verify", json={"machine_code": "MACH-A", "token": tok})
        assert r.status_code == 200 and r.json()["valid"] is True

    def test_tampered_token_rejected(self, client):
        key = make_card(30)[0]
        tok = activate(client, key)["token"]
        raw, sig = tok.rsplit(".", 1)
        evil = raw + "." + ("0" * len(sig))
        r = client.post("/auth/verify", json={"machine_code": "MACH-A", "token": evil})
        assert r.status_code == 401

    def test_expired_card_403(self, client):
        key = make_card(30)[0]
        tok = activate(client, key)["token"]
        with store.conn() as c:      # 直接把到期时间拨到过去
            c.execute("UPDATE cards SET expire_at=? WHERE card_key=?",
                      (int(time.time()) - 1, key))
            c.commit()
        r = client.post("/auth/verify", json={"machine_code": "MACH-A", "token": tok})
        assert r.status_code == 403
        assert r.json()["detail"]["reason"] == "expired"

    def test_ban_takes_effect_immediately(self, client):
        """封禁后 verify 与网关请求都必须立刻 403——拉黑不等令牌自然过期"""
        key = make_card(30)[0]
        tok = activate(client, key)["token"]
        assert client.post("/auth/verify",
                           json={"machine_code": "MACH-A", "token": tok}).status_code == 200
        store.ban_machine("MACH-A", "盗卡")
        r = client.post("/auth/verify", json={"machine_code": "MACH-A", "token": tok})
        assert r.status_code == 403
        g = client.get("/api/v1/health", headers=auth_headers(tok, nonce="v1"))
        assert g.status_code == 403

    def test_disable_card_takes_effect_immediately(self, client):
        key = make_card(30)[0]
        tok = activate(client, key)["token"]
        store.set_card_status(key, "disabled")
        r = client.get("/api/v1/health", headers=auth_headers(tok, nonce="d1"))
        assert r.status_code == 401
