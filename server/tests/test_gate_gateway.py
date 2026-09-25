"""server/tests/test_gate_gateway.py —— 转发网关：鉴权、防重放、映射回源、不外泄

商用安全红线全部钉在这里：
- 无令牌/篡改/过期/重放 → 401/403；
- 上游真实 base、真实 job_id/asset_id、产物直链，任何响应体都不许出现；
- 参考图暂存在网关，提交时才转投选中的线路。
"""
import time

import pytest

from server import gateway, store, translate_api, upstream
from helpers import FakeResp, activate, auth_headers, make_card


@pytest.fixture()
def lic(client):
    """激活一张 30 天卡，返回 token"""
    key = make_card(30)[0]
    return activate(client, key)["token"]


def h(token, **kw):
    return auth_headers(token, **kw)


class TestLicenseEnforcement:
    def test_no_headers_401(self, client):
        assert client.get("/api/v1/health").status_code == 401

    def test_tampered_signature_401(self, client, lic):
        raw, _sig = lic.rsplit(".", 1)
        r = client.get("/api/v1/health", headers=h(raw + "." + "0" * 32))
        assert r.status_code == 401

    def test_wrong_machine_401(self, client, lic):
        r = client.get("/api/v1/health", headers=h(lic, machine="OTHER"))
        assert r.status_code == 401

    def test_stale_timestamp_401(self, client, lic):
        r = client.get("/api/v1/health", headers=h(lic, ts=int(time.time()) - 9999))
        assert r.status_code == 401

    def test_nonce_replay_401(self, client, lic):
        hd = h(lic, nonce="fixed-nonce")
        assert client.get("/api/v1/health", headers=hd).status_code == 200
        assert client.get("/api/v1/health", headers=hd).status_code == 401

    def test_expired_token_403(self, client, lic):
        """到期后网关只收 403——客户端据此弹激活窗"""
        with store.conn() as c:
            c.execute("UPDATE cards SET expire_at=?", (int(time.time()) - 1,))
            c.commit()
        r = client.get("/api/v1/health", headers=h(lic))
        assert r.status_code == 403
        assert r.json()["detail"]["reason"] == "expired"


class TestReadOnlyEndpoints:
    def test_health_and_lines_no_upstream_leak(self, client, lic):
        r = client.get("/api/v1/health", headers=h(lic))
        assert r.status_code == 200 and r.json()["status"] == "ready"
        lines = client.get("/api/v1/lines", headers=h(lic))
        body = lines.text
        assert "acc1" in body and "acc2" in body
        assert "mock-upstream" not in body          # 上游地址绝不出网关

    def test_load_balancing_reads_cloud_queue(self, client, lic, cloud):
        cloud.on("GET", "http://mock-upstream/acc1",
                 FakeResp({"items": [{"job_id": "r1", "status": "running"}] * 2}))
        cloud.on("GET", "http://mock-upstream/acc2",
                 FakeResp({"items": [{"job_id": "r2", "status": "queued"}]}))
        r = client.get("/api/v1/lines", headers=h(lic))
        loads = {i["name"]: i["load"] for i in r.json()["items"]}
        assert loads == {"acc1": 2, "acc2": 1}


class TestSubmitChain:
    def _mock_cloud(self, cloud):
        """假云端：/assets 回 a-real-1，/jobs 提交回 j-real-1，查询回 running→completed"""
        def handler(method, url, kw):
            if url.endswith("/assets"):
                return FakeResp({"asset_id": "a-real-1"})
            if url.endswith("/jobs") and method == "POST":
                return FakeResp({"job_id": "j-real-1"})
            if "/jobs/j-real-1/outputs" in url:
                return FakeResp({"outputs": [
                    {"url": "http://cdn.mock/v/1.mp4", "filename": "1.mp4"}]})
            if url.endswith("/jobs/j-real-1"):
                return FakeResp({"job_id": "j-real-1", "status": "completed",
                                 "progress": 100})
            if url.endswith("/jobs"):
                return FakeResp({"items": []})
            return FakeResp({})
        cloud.handler = handler
        return cloud

    def test_full_cycle_and_id_wrapping(self, client, lic, cloud):
        self._mock_cloud(cloud)
        # 1) 参考图先落网关暂存
        up = client.post("/api/v1/assets", headers=h(lic, nonce="up1"),
                         files={"file": ("ref.png", b"PNGDATA", "image/png")})
        assert up.status_code == 200
        ast = up.json()["asset_id"]
        assert ast.startswith("ast_") and "mock-upstream" not in up.text

        # 2) 提交：payload 引用 gw asset_id，落网关 gw job_id
        payload = {"feature": "minimax-h3", "mode": "r2v",
                   "inputs": {"prompt": "p", "reference_images": [ast]},
                   "parameters": {"duration": 5}}
        r = client.post("/api/v1/jobs", headers=h(lic, nonce="sub1"), json=payload)
        assert r.status_code == 200
        gw_job = r.json()["job_id"]
        assert gw_job.startswith("gwj_") and "j-real-1" not in r.text

        # 网关把暂存图转投到了被选线路，且提交报文里已是真实 asset_id：
        # 上传与提交必须落在同一条 base 上（参考图只存在于执行它的线路本地）
        uploads = [c for c in cloud.calls if c[1].endswith("/assets")]
        submits = [c for c in cloud.calls if c[1].endswith("/jobs") and c[0] == "POST"]
        assert len(uploads) == 1 and len(submits) == 1
        upload_base = uploads[0][1][: -len("/assets")]
        assert submits[0][1] == upload_base + "/jobs"
        assert submits[0][2]["json"]["inputs"]["reference_images"] == ["a-real-1"]

        # 3) 查询：回显 gw id，终态落库
        q = client.get(f"/api/v1/jobs/{gw_job}", headers=h(lic, nonce="q1"))
        assert q.json()["job_id"] == gw_job and q.json()["status"] == "completed"
        assert store.get_job(gw_job)["terminal"] == 1

        # 4) 产物：直链必须已被改写为网关中转
        o = client.get(f"/api/v1/jobs/{gw_job}/outputs", headers=h(lic, nonce="o1"))
        link = o.json()["outputs"][0]["url"]
        assert "cdn.mock" not in o.text and link.startswith("/api/v1/file?")

    def test_unknown_job_404(self, client, lic, cloud):
        r = client.get("/api/v1/jobs/gwj_nosuch", headers=h(lic, nonce="n1"))
        assert r.status_code == 404

    def test_asset_sweep_expired_400(self, client, lic, cloud):
        self._mock_cloud(cloud)
        payload = {"inputs": {"reference_images": ["ast_gone"]}}
        r = client.post("/api/v1/jobs", headers=h(lic, nonce="x1"), json=payload)
        assert r.status_code == 400

    def test_list_jobs_maps_real_ids(self, client, lic, cloud):
        store.add_job("gwj_mine", "acc1", "r-own", "MACH-A")
        cloud.on("GET", "http://mock-upstream/acc1",
                 FakeResp({"items": [{"job_id": "r-own", "status": "running"},
                                     {"job_id": "r-other", "status": "queued"}]}))
        cloud.on("GET", "http://mock-upstream/acc2", FakeResp({"items": []}))
        r = client.get("/api/v1/jobs", headers=h(lic, nonce="l1"))
        ids = [i["job_id"] for i in r.json()["items"]]
        assert ids[0] == "gwj_mine"              # 自家映射回 gw id
        assert ids[1].startswith("ext_")          # 无映射的第三方任务给伪 id
        assert "r-own" not in r.text

    def test_cancel_forwards_real_id(self, client, lic, cloud):
        self._mock_cloud(cloud)
        store.add_job("gwj_c1", "acc1", "j-cancel-me", "MACH-A")
        seen = {}

        def handler(method, url, kw):
            seen["url"] = url
            return FakeResp({"ok": True})
        cloud.handler = handler
        r = client.post("/api/v1/jobs/gwj_c1/cancel", headers=h(lic, nonce="c1"))
        assert r.status_code == 200
        assert url_tail_cancel(seen["url"]) and store.get_job("gwj_c1")["terminal"] == 1


def url_tail_cancel(url):
    return url.endswith("/jobs/j-cancel-me/cancel")


class TestFileProxy:
    def test_file_roundtrip(self, client, lic, monkeypatch):
        wrapped = gateway.wrap_url("http://cdn.mock/secret/path.mp4")
        assert "secret" not in wrapped and wrapped.startswith("/api/v1/file?")

        from urllib.parse import parse_qs
        qs = parse_qs(wrapped.split("?", 1)[1])

        class FakeStream:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_bytes(self, _n):
                yield b"VIDEO"

            def close(self):
                pass

            status_code = 200

        monkeypatch.setattr(gateway.httpx, "stream",
                            lambda *a, **k: FakeStream())
        r = client.get("/api/v1/file", headers=h(lic, nonce="f1"),
                       params={"p": qs["p"][0], "s": qs["s"][0]})
        assert r.status_code == 200 and r.content == b"VIDEO"

    def test_file_bad_signature_403(self, client, lic):
        r = client.get("/api/v1/file", headers=h(lic, nonce="f2"),
                       params={"p": "aGk", "s": "badsig"})
        assert r.status_code == 403

    def test_unbanned_leak_check(self, client, lic, cloud):
        """硬约束：拿一条真实线路 base 去搜所有网关响应——一处命中即泄密"""
        self_call = client.get("/api/v1/health", headers=h(lic, nonce="k1"))
        lines = client.get("/api/v1/lines", headers=h(lic, nonce="k2"))
        for resp in (self_call, lines):
            assert upstream.UPSTREAMS[0].base not in resp.text


class TestToolEndpoints:
    def test_material_parse_forwards_credentials(self, client, lic, monkeypatch):
        captured = {}

        class R:
            status_code = 200

            @staticmethod
            def json():
                return {"code": 200, "msg": "ok",
                        "data": {"title": "t", "text": "hello"}}

        def fake_get(url, params=None, **kw):
            captured["url"] = url
            captured["params"] = params
            return R()
        monkeypatch.setattr(gateway.httpx, "get", fake_get)
        r = client.get("/api/v1/material/parse", headers=h(lic, nonce="m1"),
                       params={"type": "dsp", "url": "https://v.douyin.com/x/"})
        assert r.status_code == 200 and r.json()["data"]["title"] == "t"
        assert captured["url"] == "http://mock-upstream/juke"       # 真实凭证在网关侧注入
        assert captured["params"]["uid"] == "u1"

    def test_material_rejects_bad_type(self, client, lic):
        r = client.get("/api/v1/material/parse", headers=h(lic, nonce="m2"),
                       params={"type": "xxx", "url": "http://a"})
        assert r.status_code == 400

    def test_tool_rate_limit_per_machine(self, client, lic, monkeypatch):
        class R:
            status_code = 200

            def json(self):
                return {"code": 200, "data": {}}
        monkeypatch.setattr(gateway.httpx, "get", lambda *a, **k: R())
        codes = [client.get("/api/v1/material/parse",
                            headers=h(lic, nonce=f"r{i}"),
                            params={"type": "dsp", "url": "http://a"}).status_code
                 for i in range(upstream.config.TOOL_RATE_PER_MIN + 1)]
        assert codes[-1] == 429 and codes[0] == 200

    def test_translate_roundtrip(self, client, lic, monkeypatch):
        monkeypatch.setattr(translate_api, "translate_texts",
                            lambda texts, source, target: ["译:" + t for t in texts])
        r = client.post("/api/v1/translate", headers=h(lic, nonce="t1"),
                        json={"texts": ["close up", "medium shot"],
                              "source": "auto", "target": "zh"})
        assert r.json()["translations"] == ["译:close up", "译:medium shot"]

    def test_scripts_extract_forwards(self, client, lic, cloud):
        seen = {}

        def handler(method, url, kw):
            seen["url"] = url
            seen["prompt"] = kw["json"]["prompt"]
            return FakeResp({"script": "口播文案"})
        cloud.handler = handler
        r = client.post("/api/v1/scripts/extract", headers=h(lic, nonce="s1"),
                        json={"prompt": "p"})
        assert r.status_code == 200 and r.json() == {"script": "口播文案"}
        assert seen["url"].endswith("/scripts/extract")
        assert seen["prompt"] == "p"
        assert "mock-upstream" not in r.text          # 响应不回显上游地址

    def test_scripts_extract_empty_prompt_400(self, client, lic):
        r = client.post("/api/v1/scripts/extract", headers=h(lic, nonce="s2"),
                        json={"prompt": "  "})
        assert r.status_code == 400


class TestAdmin:
    def test_admin_requires_token(self, client):
        assert client.get("/admin/status").status_code == 401      # 没带头：401
        r = client.get("/admin/status", headers={"X-Admin-Token": "wrong"})
        assert r.status_code == 401

    def test_gen_and_disable_cards_via_admin(self, client):
        hd = {"X-Admin-Token": "admin-test-token"}
        r = client.post("/admin/cards", headers=hd, json={"days": 7, "count": 3})
        assert r.status_code == 200 and r.json()["generated"] == 3
        key = r.json()["card_keys"][0]
        assert store.get_card(key)["days"] == 7
        assert client.post("/admin/cards/disable", headers=hd,
                           json={"card_key": key}).status_code == 200
        assert store.get_card(key)["status"] == "disabled"

    def test_ban_unban(self, client):
        hd = {"X-Admin-Token": "admin-test-token"}
        client.post("/admin/ban", headers=hd,
                    json={"machine_code": "mach-b", "reason": "试卡"})
        assert store.is_banned("MACH-B")
        client.post("/admin/unban", headers=hd, json={"machine_code": "MACH-B"})
        assert not store.is_banned("MACH-B")
