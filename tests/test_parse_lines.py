"""
tests/test_parse_lines.py —— 线路配置「一键复制 / 粘贴导入」的解析回归

粘贴入口接受三种来源（本软件复制的输出、纯地址数组、整份 config.json），
解析失败必须返回 None 而不是抛异常——GUI 据此保持表格原样。
"""
import json

from gui.pages_settings import parse_lines


def test_roundtrip_of_copy_output():
    """「📋 复制线路」的输出原样粘回来，字段一字不差"""
    rows = [{"name": "acc1", "base": "http://a.test/api/v1", "concurrency": 1},
            {"name": "acc2", "base": "http://b.test/api/v1", "concurrency": 3}]
    assert parse_lines(json.dumps({"accounts": rows}, ensure_ascii=False)) == rows


def test_plain_url_array_is_accepted():
    """手工粘贴的一串地址也要能用：名字自动补、缺省并发为 1"""
    out = parse_lines(json.dumps(["http://a.test", "http://b.test"]))
    assert [r["name"] for r in out] == ["acc1", "acc2"]
    assert all(r["concurrency"] == 1 for r in out)


def test_full_config_json_is_accepted_and_base_normalized():
    """整份 config.json 也能贴：取 accounts，并补上漏写的 /api/v1 后缀"""
    cfg = {"user_name": "张三",
           "accounts": [{"name": "acc1", "base": "https://x.test/", "concurrency": 2}]}
    out = parse_lines(json.dumps(cfg, ensure_ascii=False))
    assert out[0]["base"] == "https://x.test/api/v1"
    assert out[0]["concurrency"] == 2


def test_bad_concurrency_falls_back_to_one():
    out = parse_lines(json.dumps([{"base": "http://a.test", "concurrency": "很多"}]))
    assert out[0]["concurrency"] == 1
    out = parse_lines(json.dumps([{"base": "http://a.test", "concurrency": 0}]))
    assert out[0]["concurrency"] == 1


def test_entries_without_base_are_skipped():
    out = parse_lines(json.dumps([{"name": "空的"}, {"base": "http://a.test"}]))
    assert [r["base"] for r in out] == ["http://a.test/api/v1"]


def test_unparseable_input_returns_none():
    """返回 None 而不是抛异常，GUI 才能保持表格不动"""
    for bad in ["", "   ", "不是 JSON", "[1,2,3]", '{"foo": 1}', "[]"]:
        assert parse_lines(bad) is None, bad
