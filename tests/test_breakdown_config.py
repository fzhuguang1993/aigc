"""
tests/test_breakdown_config.py —— 豆包接入点配置的加密往返与未配置判定

复用 material_extract 的 Fernet（盐=USER_NAME）：给了姓名 api_key 密文落盘、同名解得开；
占位值（<…>）一律视为未配置。测试在 conftest 隔离的临时 CONFIG_DIR 里跑，不碰真实配置。
"""
import json

import pytest

import core.config as cfg


@pytest.fixture(autouse=True)
def _isolate_config_json(tmp_path, monkeypatch):
    """把 config.json 重定向到用例临时文件并重置启动缓存：
    不碰共享配置家，更不会把 config.json 泄给依赖“无 config.json”前提的其它用例。"""
    monkeypatch.setattr(cfg, "CONFIG_JSON", tmp_path / "config.json")
    monkeypatch.setattr(cfg, "_JSON_CACHE", None)
    yield


def _clear_section():
    cfg.write_section("doubao_vision", {})


def test_unconfigured_by_default():
    _clear_section()
    c = cfg.doubao_vision_config()
    assert c["api_key"] == "" and c["endpoint"] == ""
    assert c["base_url"].endswith("/api/v3")
    assert cfg.doubao_vision_ready() is False


def test_save_read_roundtrip_plaintext_when_no_name(monkeypatch):
    _clear_section()
    monkeypatch.setattr(cfg, "USER_NAME", "")          # 无姓名 → 明文兜底
    cfg.save_doubao_vision("sk-abc", "ep-1234")
    c = cfg.doubao_vision_config()
    assert c["api_key"] == "sk-abc" and c["endpoint"] == "ep-1234"
    assert cfg.doubao_vision_ready() is True


def test_api_key_encrypted_at_rest_with_name(monkeypatch):
    _clear_section()
    monkeypatch.setattr(cfg, "USER_NAME", "张三")
    cfg.save_doubao_vision("sk-secret", "ep-9999")
    # 盘上必须是密文，绝不允许出现明文 key
    raw = json.loads(cfg.CONFIG_JSON.read_text(encoding="utf-8"))
    stored = raw["doubao_vision"]["api_key"]
    assert stored.startswith("enc:") and "sk-secret" not in stored
    # 同名才解得开
    assert cfg.doubao_vision_config()["api_key"] == "sk-secret"


def test_placeholder_treated_as_unconfigured(monkeypatch):
    _clear_section()
    monkeypatch.setattr(cfg, "USER_NAME", "")
    cfg.save_doubao_vision("<你的key>", "<ep-xxx>")
    c = cfg.doubao_vision_config()
    assert c["api_key"] == "" and c["endpoint"] == ""
    assert cfg.doubao_vision_ready() is False


def test_base_url_override(monkeypatch):
    _clear_section()
    monkeypatch.setattr(cfg, "USER_NAME", "")
    cfg.save_doubao_vision("k", "ep", base_url="https://ark.example.com/api/v3/")
    assert cfg.doubao_vision_config()["base_url"] == "https://ark.example.com/api/v3"


# ====================================================================
# 语音纠错（asr_fix）：加密往返 / 占位置空 / 就绪判定（与豆包同款规矩）
# ====================================================================
def _clear_asr_fix():
    cfg.write_section("asr_fix", {})


def test_asr_fix_unconfigured_by_default():
    _clear_asr_fix()
    c = cfg.asr_fix_config()
    assert c["api_key"] == ""
    assert c["model"] == "deepseek-chat"
    assert c["base_url"] == "https://api.deepseek.com/v1"
    assert cfg.asr_fix_ready() is False


def test_asr_fix_save_read_roundtrip_plaintext_when_no_name(monkeypatch):
    _clear_asr_fix()
    monkeypatch.setattr(cfg, "USER_NAME", "")          # 无姓名 → 明文兜底
    cfg.save_asr_fix("sk-ds", model="deepseek-chat")
    c = cfg.asr_fix_config()
    assert c["api_key"] == "sk-ds" and c["model"] == "deepseek-chat"
    assert cfg.asr_fix_ready() is True


def test_asr_fix_api_key_encrypted_at_rest_with_name(monkeypatch):
    _clear_asr_fix()
    monkeypatch.setattr(cfg, "USER_NAME", "张三")
    cfg.save_asr_fix("sk-secret-ds", model="deepseek-chat")
    raw = json.loads(cfg.CONFIG_JSON.read_text(encoding="utf-8"))
    stored = raw["asr_fix"]["api_key"]
    assert stored.startswith("enc:") and "sk-secret-ds" not in stored
    assert cfg.asr_fix_config()["api_key"] == "sk-secret-ds"


def test_asr_fix_placeholder_key_treated_as_unconfigured(monkeypatch):
    _clear_asr_fix()
    monkeypatch.setattr(cfg, "USER_NAME", "")
    cfg.save_asr_fix("<你的key>", model="deepseek-chat")
    assert cfg.asr_fix_config()["api_key"] == ""
    assert cfg.asr_fix_ready() is False


def test_asr_fix_not_ready_without_model(monkeypatch):
    _clear_asr_fix()
    monkeypatch.setattr(cfg, "USER_NAME", "")
    cfg.write_section("asr_fix", {"api_key": "sk-only", "model": ""})   # 有 key 无 model
    assert cfg.asr_fix_config()["model"] == ""
    assert cfg.asr_fix_ready() is False


def test_asr_fix_base_url_override(monkeypatch):
    _clear_asr_fix()
    monkeypatch.setattr(cfg, "USER_NAME", "")
    cfg.save_asr_fix("k", model="m", base_url="https://ds.example.com/v1/")
    assert cfg.asr_fix_config()["base_url"] == "https://ds.example.com/v1"


# ====================================================================
# 领域词库（asr_glossary）：去重保序往返，合并写回只动该段
# ====================================================================
def test_glossary_roundtrip_dedup_and_order(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "USER_NAME", "")
    cfg.write_section("doubao_vision", {"api_key": "keep"})   # 另一段，验证不被冲掉
    out = cfg.asr_glossary_save([" 钙片 ", "骨密度", "钙片", "", "  ", "益生菌"])
    assert out == ["钙片", "骨密度", "益生菌"]                 # 去空/去重/保序/trim
    assert cfg.asr_glossary_load() == ["钙片", "骨密度", "益生菌"]
    # 合并写回：不得冲掉其它段
    raw = json.loads(cfg.CONFIG_JSON.read_text(encoding="utf-8"))
    assert raw["doubao_vision"]["api_key"] == "keep"


def test_glossary_empty_when_absent(tmp_path, monkeypatch):
    cfg.write_section("asr_glossary", [])
    assert cfg.asr_glossary_load() == []


def test_glossary_save_empty_clears(tmp_path, monkeypatch):
    cfg.asr_glossary_save(["钙片"])
    assert cfg.asr_glossary_save([]) == []
    assert cfg.asr_glossary_load() == []


# ====================================================================
# 词库文件导入/导出（Excel / CSV / JSON）：往返与清洗
# ====================================================================
def test_glossary_json_roundtrip(tmp_path):
    p = tmp_path / "g.json"
    out = cfg.glossary_to_file(p, [" 钙片 ", "骨密度", "钙片", "", "益生菌"])
    assert out == ["钙片", "骨密度", "益生菌"]          # 导出前已去空/去重/trim
    assert cfg.glossary_from_file(p) == ["钙片", "骨密度", "益生菌"]


def test_glossary_from_json_accepts_dict_and_object_list(tmp_path):
    p = tmp_path / "g2.json"
    p.write_text('{"terms": ["钙片", "骨密度"]}', encoding="utf-8")
    assert cfg.glossary_from_file(p) == ["钙片", "骨密度"]
    p.write_text('[{"词": "钙片"}, {"词": "骨质疏松"}]', encoding="utf-8")
    assert cfg.glossary_from_file(p) == ["钙片", "骨质疏松"]


def test_glossary_csv_roundtrip(tmp_path):
    p = tmp_path / "g.csv"
    cfg.glossary_to_file(p, ["钙片", "骨密度", "益生菌"])
    assert cfg.glossary_from_file(p) == ["钙片", "骨密度", "益生菌"]


def test_glossary_xlsx_roundtrip(tmp_path):
    p = tmp_path / "g.xlsx"
    cfg.glossary_to_file(p, ["钙片", "骨密度"])
    assert cfg.glossary_from_file(p) == ["钙片", "骨密度"]


def test_glossary_unsupported_ext_raises(tmp_path):
    p = tmp_path / "g.txt"
    p.write_text("钙片", encoding="utf-8")
    with pytest.raises(ValueError):
        cfg.glossary_from_file(p)
    with pytest.raises(ValueError):
        cfg.glossary_to_file(p, ["钙片"])
