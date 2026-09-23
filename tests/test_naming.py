"""
tests/test_naming.py —— 输出文件命名规则回归测试

锁死三件事：
1. 默认规则的输出与历史写死那份**逐字节一致**（改名规则不能顺手改掉老交付习惯）；
2. 规则可以排列组合（字段顺序 / 分隔符 / 少几个字段），且「序号」续排、
   撞名唯一化这两种防覆盖手段都还在；
3. config.json 坏了/被手改错字段名时**回退默认**，绝不拼出空文件名
   （空名会让下载落到非法路径，报错还看不出是哪）。
"""
import json
from datetime import datetime

import pytest

from core import naming

CTX = {"num": 1, "product": "诺特兰德益生菌", "name": "雷亮",
       "when": datetime(2026, 9, 23, 10, 32)}


def _ctx(seq=1, **kw):
    return {**CTX, naming.SEQ: seq, **kw}


# ====================================================================
# 默认规则＝历史行为
# ====================================================================
def test_default_rules_match_legacy_format():
    assert naming.render(_ctx()) == "001_诺特兰德益生菌_0923_01_雷亮.mp4"


def test_default_num_and_seq_padding():
    assert naming.render(_ctx(num=42, seq=7), ["num", "seq"], "_") == "042_07.mp4"


def test_subdirname_is_by_day():
    assert naming.subdirname(datetime(2026, 9, 23)) == "0923"
    assert naming.subdirname().isdigit()          # 不传就是今天，别是空串


# ====================================================================
# 排列组合：顺序 / 分隔符 / 字段个数
# ====================================================================
def test_custom_order_and_separator():
    got = naming.render(_ctx(), ["date_full", "product", naming.SEQ, "num"], "-")
    assert got == "20260923-诺特兰德益生菌-01-001.mp4"


def test_no_separator_writes_fields_back_to_back():
    assert naming.render(_ctx(), ["num", naming.SEQ], "") == "00101.mp4"


def test_empty_segment_is_dropped_not_left_dangling():
    # 备注没填：不能留下一个孤零零的分隔符
    assert naming.render(_ctx(), ["remark", "num"], "_") == "001.mp4"
    assert naming.render({**CTX, "remark": "已过审"}, ["remark", "num"], "_") \
        == "已过审_001.mp4"


def test_illegal_chars_are_sanitized():
    assert naming.render(_ctx(product="a/b:c*?"), ["product"], "_") == "a_b_c__.mp4"


def test_describe_and_preview_for_settings_page():
    assert naming.describe(["num", "seq"], "_") == "编号_序号"
    assert naming.describe(["num", "seq"], "") == "编号 序号"      # 无分隔符也要读得通
    assert naming.preview(tokens=["product"], sep="-") == "诺特兰德益生菌.mp4"


# ====================================================================
# 坏规则要回退（设置页存的东西会被手改）
# ====================================================================
def test_known_tokens_keeps_only_recognized():
    assert naming.known_tokens(["bogus", "seq"]) == ["seq"]
    assert naming.known_tokens(["nope"]) == list(naming.DEFAULT_TOKENS)
    assert naming.known_tokens(None) == list(naming.DEFAULT_TOKENS)


# ====================================================================
# 序号续排 + 撞名唯一化
# ====================================================================
def test_next_seq_scans_only_the_same_prefix(tmp_path):
    d = tmp_path / "0923"
    d.mkdir()
    for name in ("001_诺特兰德益生菌_0923_01_雷亮.mp4",
                 "001_诺特兰德益生菌_0923_12_雷亮.mp4",
                 "002_别的品名_0923_99_雷亮.mp4"):
        (d / name).write_bytes(b"x")
    assert naming.next_seq(d, _ctx()) == 13
    # 另一批（编号+品名不同）不参与续排，从 1 开始
    assert naming.next_seq(d, _ctx(product="还没下载过的品名")) == 1


def test_next_seq_on_empty_or_missing_dir(tmp_path):
    assert naming.next_seq(tmp_path, _ctx()) == 1
    assert naming.next_seq(tmp_path / "没有这个目录", _ctx()) == 1


def test_stem_before_seq():
    assert naming.stem_before_seq(_ctx(), ["num"], "_") is None       # 没排序号
    assert naming.stem_before_seq(_ctx(), ["num", naming.SEQ], "_") == "001_"
    assert naming.stem_before_seq(_ctx(), [naming.SEQ], "_") == ""    # 序号打头


def test_resolve_save_path_keeps_incrementing_seq(tmp_path):
    for i in range(1, 5):
        p = naming.resolve_save_path(tmp_path, _ctx())
        assert p.name == f"001_诺特兰德益生菌_0923_{i:02d}_雷亮.mp4"
        p.write_bytes(b"x")                 # 真落一个文件，模拟下载完成


def test_resolve_save_path_without_seq_uses_dup_suffix(tmp_path):
    tokens = ["num", "product", "name"]
    first = naming.resolve_save_path(tmp_path, _ctx(), tokens, "_")
    assert first.name == "001_诺特兰德益生菌_雷亮.mp4"
    first.write_bytes(b"x")
    second = naming.resolve_save_path(tmp_path, _ctx(), tokens, "_")
    assert second.name == "001_诺特兰德益生菌_雷亮(2).mp4"


# ====================================================================
# 落盘：保存即生效 + 合并写回不丢别的字段
# ====================================================================
@pytest.fixture()
def cfg_file(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(naming, "CONFIG_JSON", p)
    naming.set_rules(list(naming.DEFAULT_TOKENS), naming.DEFAULT_SEP)
    yield p
    naming.set_rules(list(naming.DEFAULT_TOKENS), naming.DEFAULT_SEP)


def test_save_rules_merges_into_existing_config(cfg_file):
    cfg_file.write_text(json.dumps({"user_name": "雷亮",
                                    "accounts": [{"name": "acc1",
                                                  "base": "http://a:7860/api/v1"}]}),
                        encoding="utf-8")
    naming.save_rules(["date_full", "product", naming.SEQ], "-")
    assert naming.rules() == (["date_full", "product", naming.SEQ], "-")
    # 立刻生效：不重启、不重新 import，拼名就已经按新规则走
    assert naming.render(_ctx()) == "20260923-诺特兰德益生菌-01.mp4"
    data = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert data["user_name"] == "雷亮" and len(data["accounts"]) == 1
    assert data["filename"] == {"tokens": ["date_full", "product", "seq"], "sep": "-"}


def test_save_rules_drops_unknown_tokens(cfg_file):
    naming.save_rules(["bogus", "num"], "_")
    assert naming.rules() == (["num"], "_")


def test_load_falls_back_to_default(cfg_file):
    # 没有 config.json（全新机器）
    assert naming.load(force=True) == (list(naming.DEFAULT_TOKENS), naming.DEFAULT_SEP)
    # JSON 语法坏
    cfg_file.write_text("{不是JSON", encoding="utf-8")
    assert naming.load(force=True)[0] == list(naming.DEFAULT_TOKENS)
    # filename 段写成别的东西
    cfg_file.write_text('{"filename": "一段字符串"}', encoding="utf-8")
    assert naming.load(force=True)[0] == list(naming.DEFAULT_TOKENS)
    # 字段名全是手敲打错的
    cfg_file.write_text('{"filename": {"tokens": ["bugus"], "sep": "_"}}', encoding="utf-8")
    assert naming.load(force=True)[0] == list(naming.DEFAULT_TOKENS)


def test_load_is_cached_until_forced(cfg_file):
    naming.load(force=True)
    cfg_file.write_text('{"filename": {"tokens": ["num"], "sep": "."}}', encoding="utf-8")
    assert naming.load()[0] == list(naming.DEFAULT_TOKENS)      # 还在用缓存
    assert naming.load(force=True)[0] == ["num"]                # 重读才看到新规则
