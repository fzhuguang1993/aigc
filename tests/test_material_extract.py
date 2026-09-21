"""
tests/test_material_extract.py —— 素材提取功能回归（全 mock，无网络）

覆盖：分享文案提链、直链域名白名单、解析接口应答处理、
extract_one 端到端（dsp+wenan 双接口、落盘产物）。
"""
from pathlib import Path

import pytest

import video_text_tools.material_extract as me


class FakeResp:
    def __init__(self, payload=None, text="", ok=True):
        self._payload, self.text = payload, text
        self.status_code = 200 if ok else 500

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def iter_content(self, _):
        yield b"fake-bytes"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------------- 分享链接提取 ----------------

def test_extract_share_urls_from_share_text():
    text = ("7.99 pQh:/ 复制打开抖音，看看【某人的作品】https://v.douyin.com/abc123/ "
            "复制此链接，打开Dou音搜索\n"
            "再来一条 https://www.kuaishou.com/f/xyz ；"
            "https://v.douyin.com/abc123/")
    urls = me.extract_share_urls(text)
    assert urls == ["https://v.douyin.com/abc123", "https://www.kuaishou.com/f/xyz"]
    assert me.extract_share_urls("") == []
    assert me.extract_share_urls("没有链接") == []


def test_extract_skips_api_doc_url():
    # 直接粘贴接口文档地址本身不算素材链接
    assert me.extract_share_urls(
        "https://api.zhuceka.cn/home/api?type=dsp&uid=1&key=k&url=") == []


# ---------------- 域名白名单 ----------------

def test_load_allowed_hosts(tmp_path):
    # 真实名单格式：一行一个域名，带 https:// 前缀（用户直接粘短链域也能用）
    f = tmp_path / "video.txt"
    f.write_text("https://v3-dy-o.zjcdn.com\n\nv23-3.kwaicdn.com/\n# 注释\n",
                 encoding="utf-8")
    assert me.load_allowed_hosts(f) == {"v3-dy-o.zjcdn.com", "v23-3.kwaicdn.com"}
    assert me.load_allowed_hosts(tmp_path / "missing.txt") == set()


def test_host_allowed():
    hosts = {"v3-dy-o.zjcdn.com", "douyinvod.com"}
    assert me.host_allowed("https://v3-dy-o.zjcdn.com/a/b.mp4", hosts)
    assert me.host_allowed("https://sub.douyinvod.com/x", hosts)
    assert not me.host_allowed("https://evil.com/douyinvod.com/x", hosts)
    assert not me.host_allowed("https://douyinvod.com.evil.cn/x", hosts)
    assert me.host_allowed("https://anything.com/x", set())   # 空白名单不限制


# ---------------- 解析接口应答 ----------------

def test_call_parse_api_ok(monkeypatch):
    monkeypatch.setattr(me.requests, "get",
                        lambda *a, **k: FakeResp({"code": 200, "data": {"title": "T"}}))
    assert me.call_parse_api("b", "u", "k", "dsp", "https://v.douyin.com/x") == {"title": "T"}


def test_call_parse_api_errors(monkeypatch):
    monkeypatch.setattr(me.requests, "get",
                        lambda *a, **k: FakeResp({"code": 400, "msg": "链接错误"}))
    with pytest.raises(ValueError, match="链接错误"):
        me.call_parse_api("b", "u", "k", "dsp", "x")
    monkeypatch.setattr(me.requests, "get", lambda *a, **k: FakeResp(None, text="<html>"))
    with pytest.raises(ValueError, match="JSON"):
        me.call_parse_api("b", "u", "k", "wenan", "x")


# ---------------- 接口配置运行时可换 ----------------

def test_resolve_save_api_config(tmp_path):
    defaults = {"base": "https://api.default", "uid": "u0", "key": "k0"}
    # 无 json：用程序默认
    assert me.resolve_api_config(tmp_path, defaults) == defaults
    # 工具界面保存后：覆盖生效
    me.save_api_config(tmp_path, "https://api.new/", " u1 ", "k1")
    assert me.resolve_api_config(tmp_path, defaults) == {
        "base": "https://api.new", "uid": "u1", "key": "k1"}
    # json 损坏：默默回退默认，不阻断提取
    (tmp_path / "api_config.json").write_text("{broken", encoding="utf-8")
    assert me.resolve_api_config(tmp_path, defaults) == defaults


def test_save_api_config_encrypts_base_with_name(tmp_path):
    """给了使用人姓名：接口地址以密文落盘，同名才解得开，换名/无密钥回退默认"""
    import json
    defaults = {"base": "https://api.default", "uid": "u0", "key": "k0"}
    me.save_api_config(tmp_path, "https://api.secret/", "u1", "k1", secret="张三")
    raw = json.loads((tmp_path / "api_config.json").read_text(encoding="utf-8"))
    # 盘上是密文，绝不允许出现明文地址
    assert raw["base"].startswith("enc:") and "api.secret" not in raw["base"]
    # 姓名一致：解得开，末尾斜杠已归一化
    assert me.resolve_api_config(tmp_path, defaults, secret="张三")["base"] \
        == "https://api.secret"
    # 姓名不符 / 不给姓名：解不开 → 回退默认，不泄露
    assert me.resolve_api_config(tmp_path, defaults, secret="李四")["base"] \
        == "https://api.default"
    assert me.resolve_api_config(tmp_path, defaults)["base"] == "https://api.default"
    # uid/key 仍明文可读（本次只硬防护接口地址）
    cfg = me.resolve_api_config(tmp_path, defaults, secret="张三")
    assert cfg["uid"] == "u1" and cfg["key"] == "k1"


def test_encrypt_value_roundtrip_and_guards():
    """encrypt/decrypt 基础契约：无密钥＝明文；非密文原样返回；错密钥解为空串"""
    assert me.encrypt_value("https://a", "") == "https://a"          # 无姓名→不加密
    assert me.encrypt_value("", "张三") == ""                        # 空值→原样
    tok = me.encrypt_value("https://a", "张三")
    assert tok.startswith("enc:") and me.encrypt_value(tok, "张三") == tok  # 不双重加密
    assert me.decrypt_value(tok, "张三") == "https://a"
    assert me.decrypt_value(tok, "李四") == ""                       # 错密钥
    assert me.decrypt_value("https://plain", "张三") == "https://plain"  # 非密文透传


def test_call_parse_api_uses_configured_uid_key(monkeypatch):
    """接口参数来自生效配置（验证 uid/key 替换真的会透传到请求）"""
    seen = {}

    def fake_get(url, **k):
        seen["url"], seen["params"] = url, k["params"]
        return FakeResp({"code": 200, "data": {}})

    monkeypatch.setattr(me.requests, "get", fake_get)
    me.call_parse_api("https://api.new", "新UID", "新KEY", "dsp", "https://v.douyin.com/a")
    assert seen["url"] == "https://api.new"
    assert seen["params"]["uid"] == "新UID" and seen["params"]["key"] == "新KEY"


# ---------------- extract_one 端到端 ----------------

def test_extract_one_full(monkeypatch, tmp_path):
    dsp = {"code": 200, "data": {
        "title": "好物推荐：维生素B族",
        "cover": "https://p3-sign.douyinpic.com/c.jpg",
        "video": "https://v3-dy-o.zjcdn.com/v.mp4?sign=1",
        "images": ["https://p3-sign.douyinpic.com/i1.jpg"]}}
    wenan = {"code": 200, "data": {"text": "每天两条，肠道通畅"}}  # 实测结构：正文在 data.text
    calls = []

    def fake_get(url, **k):
        # 带 params 的是解析接口请求；不带的是成品直链下载
        if "params" not in k:
            return FakeResp()
        calls.append(k["params"]["type"])
        return FakeResp(dsp if k["params"]["type"] == "dsp" else wenan)

    monkeypatch.setattr(me.requests, "get", fake_get)
    saved, notes = me.extract_one(
        "https://v.douyin.com/abc", "b", "u", "k", tmp_path,
        hosts_video={"v3-dy-o.zjcdn.com"}, hosts_image={"p3-sign.douyinpic.com"},
        log=lambda m: None)
    assert calls == ["dsp", "wenan"]
    names = sorted(Path(p).name for p in saved)
    assert names == ["好物推荐：维生素B族.mp4", "好物推荐：维生素B族.txt",
                     "好物推荐：维生素B族_图1.jpg"]
    assert notes == []
    assert "每天两条，肠道通畅" in (tmp_path / "好物推荐：维生素B族.txt").read_text(encoding="utf-8")


def test_extract_one_blocks_offlist_domain(monkeypatch, tmp_path):
    """直链域名不在白名单：不下载并给出换名单提示"""
    monkeypatch.setattr(me.requests, "get", lambda *a, **k: FakeResp({"code": 200, "data": {
        "title": "T", "video": "https://evil.example.com/v.mp4"}}))
    saved, notes = me.extract_one(
        "https://v.douyin.com/abc", "b", "u", "k", tmp_path,
        hosts_video={"zjcdn.com"}, hosts_image=set(),
        want_text=False, log=None)
    assert saved == [] and any("白名单" in n for n in notes)


# ---------------- 文案样本库 ----------------

def test_guess_platform():
    assert me.guess_platform("https://v.douyin.com/abc/") == "抖音"
    assert me.guess_platform("https://www.kuaishou.com/f/x") == "快手"
    assert me.guess_platform("https://weixin.qq.com/sph/A") == "视频号"
    assert me.guess_platform("https://xhslink.com/x") == "其他"


def test_append_corpus(tmp_path):
    import csv
    f = tmp_path / "文案样本库.csv"
    assert me.append_corpus(f, "标题A", "https://v.douyin.com/a", "文案正文A")
    # 同一链接重复提取：不重复写入
    assert not me.append_corpus(f, "标题A", "https://v.douyin.com/a", "文案正文A")
    assert me.append_corpus(f, "标题B", "https://weixin.qq.com/sph/b", "文案，含逗号\n换行")
    with open(f, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["原文链接"] for r in rows] == ["https://v.douyin.com/a",
                                             "https://weixin.qq.com/sph/b"]
    assert rows[0]["平台"] == "抖音" and rows[1]["文案"] == "文案，含逗号\n换行"
    # utf-8-sig：Excel 双击打开不乱码
    assert f.read_bytes().startswith(b"\xef\xbb\xbf")


def test_extract_one_writes_corpus(monkeypatch, tmp_path):
    """extract_one 链到样本库：文案同时落 txt 与追加 CSV"""
    def fake_get(url, **k):
        if "params" not in k:
            return FakeResp()
        return FakeResp({"code": 200, "data": {"text": "每天两条，肠道通畅"}})
    monkeypatch.setattr(me.requests, "get", fake_get)
    corpus = tmp_path / "样本.csv"
    saved, _ = me.extract_one("https://v.douyin.com/z", "b", "u", "k", tmp_path,
                              set(), set(), want_video=False, want_images=False,
                              corpus_file=corpus)
    assert corpus.exists() and "每天两条" in corpus.read_text(encoding="utf-8-sig")
    assert str(corpus) in saved


def test_extract_one_wenan_title_fallback(monkeypatch, tmp_path):
    """文案接口只返回 title（无 text）时仍能落盘，不致于拿到空正文"""
    def fake_get(url, **k):
        if "params" not in k:
            return FakeResp()
        return FakeResp({"code": 200, "data": {"title": "兜底标题"}})
    monkeypatch.setattr(me.requests, "get", fake_get)
    saved, notes = me.extract_one("https://v.douyin.com/x", "b", "u", "k", tmp_path,
                                  set(), set(), want_video=False, want_images=False)
    assert any(p.endswith(".txt") for p in saved) and notes == []
