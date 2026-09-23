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


def test_append_corpus_only_appends(tmp_path):
    """只追加、不覆写、不去重：提取一条就多一行，同一链接重复提取也各自留痕

    旧版按原文链接去重，同一条链接第二次提取“什么都没发生”，语料反而看着被覆盖了。"""
    import csv
    f = tmp_path / "文案样本库.csv"
    assert me.append_corpus(f, "标题A", "https://v.douyin.com/a", "文案正文A")
    assert me.append_corpus(f, "标题A", "https://v.douyin.com/a", "文案后来改了")
    assert me.append_corpus(f, "标题B", "https://weixin.qq.com/sph/b", "文案，含逗号")
    with open(f, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["原文链接"] for r in rows] == ["https://v.douyin.com/a",
                                             "https://v.douyin.com/a",
                                             "https://weixin.qq.com/sph/b"]
    assert [r["文案"] for r in rows[:2]] == ["文案正文A", "文案后来改了"]
    assert rows[2]["平台"] == "视频号"
    raw = f.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")        # utf-8-sig：Excel 双击不乱码
    assert raw.count(b"\xef\xbb\xbf") == 1        # 追加不在中间插 BOM（否则多出一列乱码）


def test_corpus_has_header_and_one_row_per_link(tmp_path):
    """表头必在第一行，且一行就是一条记录（字段内换行被折成空格）"""
    import csv
    f = tmp_path / "文案样本库.csv"
    me.append_corpus(f, "标题A", "https://v.douyin.com/a", "第一段\n换行也算一条")
    me.append_corpus(f, "标题B\r\n带回车", "https://v.douyin.com/b", "第二段\r\n正文")
    lines = f.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0] == "提取时间,平台,视频标题,原文链接,文案"
    assert len(lines) == 3                        # 表头 + 2 条，不被内嵌换行撑成4行
    with open(f, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["视频标题"] for r in rows] == ["标题A", "标题B 带回车"]
    assert rows[0]["文案"] == "第一段 换行也算一条"


def test_corpus_heals_missing_header(tmp_path):
    """遗留/手写的无表头文件：自动补上表头，老数据一行不丢

    没表头时 Excel 会把第一条语料当列名，整列错位；旧版只在“文件不存在”时写表头。"""
    import csv
    f = tmp_path / "文案样本库.csv"
    f.write_text("2026-09-21 14:44:00,视频号,老标题,https://v.douyin.com/old,老文案\n",
                 encoding="utf-8-sig", newline="")
    assert me.append_corpus(f, "新标题", "https://v.douyin.com/new", "新文案")
    with open(f, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["视频标题"] for r in rows] == ["老标题", "新标题"]


def test_corpus_appends_every_link_in_batch(monkeypatch, tmp_path):
    """批量粘多条链接：CSV 里每条一行（不合并、不遗漏），提取时间逐条记录"""
    import csv
    seq = {"n": 0}

    def fake_get(url, **k):
        if "params" not in k:
            return FakeResp()
        seq["n"] += 1
        return FakeResp({"code": 200, "data": {"title": f"作品{seq['n']}",
                                               "text": f"第{seq['n']}段口播"}})

    monkeypatch.setattr(me.requests, "get", fake_get)
    corpus = tmp_path / "文案样本库.csv"
    urls = ["https://v.douyin.com/a", "https://v.douyin.com/b", "https://v.douyin.com/c"]
    for u in urls:                                # 面板就是这么逐条调的
        me.extract_one(u, "b", "u", "k", tmp_path, set(), set(),
                       want_video=False, want_images=False, corpus_file=corpus)
    with open(corpus, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["原文链接"] for r in rows] == urls
    assert [r["文案"] for r in rows] == ["第1段口播", "第2段口播", "第3段口播"]


def test_corpus_tolerates_duplicate_links_in_one_paste(tmp_path):
    """一批里粘了重复链接：提链环节先去重（不重复下载），但 CSV 仍一行一条不丢

    两层口径得分清楚：同一批里的重复链接只提一次，跨批次重复提取则多一行留痕。"""
    urls = me.extract_share_urls("https://v.douyin.com/a\nhttps://v.douyin.com/a\n"
                                 "https://v.douyin.com/b")
    assert urls == ["https://v.douyin.com/a", "https://v.douyin.com/b"]
    f = tmp_path / "文案样本库.csv"
    for u in urls:
        assert me.append_corpus(f, "标题", u, "正文")
    assert f.read_text(encoding="utf-8-sig").splitlines()[0] == \
        "提取时间,平台,视频标题,原文链接,文案"
    assert len(f.read_text(encoding="utf-8-sig").splitlines()) == 3   # 表头 + 2 条


def test_extract_one_corpus_accumulates_across_runs(monkeypatch, tmp_path):
    """同一条链接跑两轮：CSV 两行都在，且样本库不计入「本次落盘文件」（统计不虚增）"""
    import csv

    def fake_get(url, **k):
        if "params" not in k:
            return FakeResp()
        data = ({"title": "同一标题", "video": ""} if k["params"]["type"] == "dsp"
                else {"title": "同一标题", "text": "口播正文"})
        return FakeResp({"code": 200, "data": data})

    monkeypatch.setattr(me.requests, "get", fake_get)
    corpus = tmp_path / "文案样本库.csv"
    url = "https://v.douyin.com/abc"
    products = []
    for _ in range(2):
        saved, _notes = me.extract_one(
            url, "b", "u", "k", tmp_path, set(), set(),
            want_video=False, want_images=False, corpus_file=corpus, log=lambda m: None)
        products += [Path(p).name for p in saved]
    # 单条 txt 靠 unique_path 错名：第二条不会截断覆写第一条
    assert sorted(products) == ["同一标题.txt", "同一标题_2.txt"]
    assert str(corpus) not in products
    with open(corpus, encoding="utf-8-sig", newline="") as fh:
        assert [r["文案"] for r in csv.DictReader(fh)] == ["口播正文", "口播正文"]
    # 两份 txt 内容都在（没被后一份截断写冲掉）
    assert (tmp_path / "同一标题.txt").read_text(encoding="utf-8").strip()
    assert (tmp_path / "同一标题_2.txt").read_text(encoding="utf-8").strip()


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
    # CSV 不在 saved 里：它是旁路语料库，混进产物列表会把“落盘 N 个文件”虚增
    assert str(corpus) not in saved and any(p.endswith(".txt") for p in saved)


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
