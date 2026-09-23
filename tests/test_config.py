"""
tests/test_config.py —— 账号 base 归一化 + 内置默认线路回归测试

锁死两条约定：
1. 无论 config.json 是向导生成、设置页保存还是手写，core.config 加载时一律
   补齐 /api/v1、去尾斜杠（否则 POST 打根路径 405）；
2. exe 内置线路（config_local.ACCOUNTS）要能直接喂给 AccountState——缺字段
   得补齐、脏条目得剔除，否则首次配置就得让同事背地址。
"""
import sys
import types

import pytest

from core import config
from core.config import (_normalize_account_base, _normalized_accounts,
                         defaults_accounts)


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


# ====================================================================
# 内置默认线路：首次配置只填姓名的前提
# ====================================================================
@pytest.fixture()
def local_accounts(monkeypatch):
    """临时伪造一份 core/config_local.py（真实那份被 .gitignore 忽略）"""
    def _write(value):
        mod = types.ModuleType("core.config_local")
        if value is not _NO_ATTR:
            mod.ACCOUNTS = value
        monkeypatch.setitem(sys.modules, "core.config_local", mod)
        return mod
    return _write


_NO_ATTR = object()


def test_defaults_fill_missing_name_and_concurrency(local_accounts):
    """只写个地址也算一条内置线路：AccountState 是裸取键的，缺键就 import 失败"""
    local_accounts([{"base": "http://a:7860"}])
    assert defaults_accounts() == [{"name": "acc1", "base": "http://a:7860/api/v1",
                                    "concurrency": 1}]


def test_defaults_drop_blank_and_template_entries(local_accounts):
    local_accounts([{"name": "acc1", "base": "  "},
                    {"name": "tpl", "base": "https://<服务地址>:7860"},
                    {"name": "ok", "base": "http://b:7860/api/v1", "concurrency": 3}])
    out = defaults_accounts()
    assert [a["name"] for a in out] == ["ok"]         # 模板/空地址不占线路位
    assert out[0]["concurrency"] == 3


def test_defaults_survive_junk_shapes(local_accounts):
    """写成纯字符串列表、并发数填中文：都不能把软件卡在 import 阶段"""
    local_accounts(["http://a:7860", {"name": "b", "base": "http://b:7860",
                                      "concurrency": "x"}])
    out = defaults_accounts()
    assert [a["base"] for a in out] == ["http://a:7860/api/v1",
                                        "http://b:7860/api/v1"]
    assert all(a["concurrency"] >= 1 for a in out)


def test_defaults_empty_when_local_has_no_accounts(local_accounts):
    """config_local.py 存在但没定义 ACCOUNTS（只配了提取接口凭证）→ 回退到
    「自己填地址」，而不是在 import 阶段抛 AttributeError"""
    local_accounts(_NO_ATTR)
    assert defaults_accounts() == []


# ====================================================================
# 首配只存姓名：线路不能因为 config.json 里没 accounts 就变空
# ====================================================================
def test_name_only_config_still_gets_builtin_lines(tmp_path, monkeypatch, local_accounts):
    """开发机首配故意不把内置地址抄进 config.json（改了 config_local.py 要能
    立刻生效），所以“没 accounts”时必须回退内置默认，而不是零条线路"""
    cfg = tmp_path / "config.json"
    cfg.write_text('{"user_name": "张三"}', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_JSON", cfg)
    monkeypatch.setattr(config, "_JSON_CACHE", None)     # 清掉 import 时读的空缓存
    local_accounts([{"base": "http://a:7860"}])
    accounts, name = config._load_local_config()
    assert name == "张三"
    assert [a["base"] for a in accounts] == ["http://a:7860/api/v1"]


def test_config_json_accounts_win_over_builtin(tmp_path, monkeypatch, local_accounts):
    """同事在设置页改过线路（写进了 config.json），内置默认不得盖回他的选择"""
    cfg = tmp_path / "config.json"
    cfg.write_text('{"user_name": "李四", "accounts": '
                   '[{"name": "mine", "base": "http://mine:7860", "concurrency": 2}]}',
                   encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_JSON", cfg)
    monkeypatch.setattr(config, "_JSON_CACHE", None)
    local_accounts([{"base": "http://a:7860"}])
    accounts, _name = config._load_local_config()
    assert [a["name"] for a in accounts] == ["mine"]


def test_normalized_accounts_repairs_handwritten_entries():
    """手写 config.json / 抄错的内置项：缺 name/concurrency 、写成纯字符串
    都得归一化成 AccountState 能裸取的结构，而不是起不来"""
    out = _normalized_accounts(["http://a:7860", {"base": "http://b:7860/api/v1",
                                                  "concurrency": "2"},
                                {"name": "空行不算"}, {"name": "脏", "base": None}])
    assert [(a["name"], a["base"], a["concurrency"]) for a in out] == [
        ("acc1", "http://a:7860/api/v1", 1),
        ("acc2", "http://b:7860/api/v1", 2)]
