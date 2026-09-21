"""
tests/test_config.py —— 账号 base 归一化回归测试

锁死约定：无论 config.json 是向导生成、设置页保存还是手写，
core.config 加载时一律补齐 /api/v1、去尾斜杠（否则 POST 打根路径 405）。
"""
from core.config import _normalize_account_base, _normalized_accounts


def test_appends_api_v1_when_missing():
    assert _normalize_account_base(
        "https://7860-cpod-xxx.pod.compshare.cn/"
    ) == "https://7860-cpod-xxx.pod.compshare.cn/api/v1"


def test_keeps_already_normalized_base():
    assert _normalize_account_base(
        "http://106.75.1.98:7860/api/v1"
    ) == "http://106.75.1.98:7860/api/v1"


def test_handles_plain_ip_without_port():
    assert _normalize_account_base("192.168.0.1") == "192.168.0.1/api/v1"


def test_accounts_normalized_without_mutating_source():
    src = [{"name": "acc1", "base": "http://a:7860/", "concurrency": 1},
           {"name": "acc2", "base": "http://b:7860/api/v1", "concurrency": 2}]
    out = _normalized_accounts(src)
    assert [a["base"] for a in out] == ["http://a:7860/api/v1",
                                        "http://b:7860/api/v1"]
    assert src[0]["base"] == "http://a:7860/"       # 不原地修改
    assert out[1]["concurrency"] == 2               # 其余字段保留
