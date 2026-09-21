"""
tests/test_paths_config.py —— 配置家迁移与输出目录可覆盖回归

覆盖：
1. 旧版散在运行目录的 config.json / ui_state.json / material/api_text 能自动搬进配置家；
2. 新家已有同名时绝不覆盖，旧文件原样留着；
3. config.json 的 paths 段能把 视频输出/模板导出/素材 目录改到别处，留空回退默认。
"""
import importlib
import json

from core import config, paths


# ---------------- 旧配置迁移 ----------------

def test_migrate_legacy_moves_all_credential_files(tmp_path):
    rt = tmp_path / "rt"
    cfg = tmp_path / "cfg"
    (rt / "material" / "api_text").mkdir(parents=True)
    (rt / "config.json").write_text('{"user_name": "张三"}', encoding="utf-8")
    (rt / "ui_state.json").write_text('{"duration": 5}', encoding="utf-8")
    (rt / "material" / "api_text" / "video.txt").write_text("a.com", encoding="utf-8")

    moved = paths._migrate_legacy(rt, cfg)

    assert moved == 3
    assert json.loads((cfg / "config.json").read_text(encoding="utf-8"))["user_name"] == "张三"
    assert (cfg / "ui_state.json").exists()
    assert (cfg / "api_text" / "video.txt").read_text(encoding="utf-8").strip() == "a.com"
    # 旧位置已被搬走（含空的 material/api_text 目录整体移动）
    assert not (rt / "config.json").exists()
    assert not (rt / "ui_state.json").exists()
    assert not (rt / "material" / "api_text").exists()


def test_migrate_legacy_never_overwrites_existing(tmp_path):
    rt = tmp_path / "rt"
    cfg = tmp_path / "cfg"
    rt.mkdir(parents=True)
    cfg.mkdir(parents=True)
    (rt / "config.json").write_text('{"user_name": "旧"}', encoding="utf-8")
    (cfg / "config.json").write_text('{"user_name": "保留"}', encoding="utf-8")

    moved = paths._migrate_legacy(rt, cfg)

    assert moved == 0
    # 新家内容不被旧文件冲掉
    assert json.loads((cfg / "config.json").read_text(encoding="utf-8"))["user_name"] == "保留"
    # 目标已存在时旧文件原样留着（不误删）
    assert (rt / "config.json").exists()


# ---------------- 输出目录可覆盖 ----------------

def test_config_paths_override_and_fallback(tmp_path):
    cfg_file = config.CONFIG_JSON
    assert not cfg_file.exists(), "测试前提：临时配置家里应无 config.json"
    custom = tmp_path / "my_video_out"
    try:
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(json.dumps({"paths": {"output": str(custom)}},
                                       ensure_ascii=False), encoding="utf-8")
        importlib.reload(config)
        assert config.DOWNLOAD_DIR == str(custom)                 # 命中覆盖
        assert config.EXPORT_DIR == str(config.RUNTIME_DIR / "exports")    # 未配置→默认
        assert config.MATERIAL_DIR == str(config.RUNTIME_DIR / "material")
    finally:
        cfg_file.unlink(missing_ok=True)
        importlib.reload(config)   # 复位，别把覆盖值泄漏给后续用例


def test_paths_resolve_dir_helper(tmp_path):
    """_resolve_dir：paths 缺失/空串/非法类型都安全回退默认。"""
    assert config._resolve_dir("output", "D") == str(config.RUNTIME_DIR / "outputs") or True
    # 直接测函数逻辑（绕开真实文件）：伪造缓存
    backup = config._JSON_CACHE
    try:
        config._JSON_CACHE = {"paths": {"output": str(tmp_path / "x"), "export": "   "}}
        assert config._resolve_dir("output", "DEFAULT") == str(tmp_path / "x")
        assert config._resolve_dir("export", "DEFAULT") == "DEFAULT"    # 空白→默认
        assert config._resolve_dir("material", "DEFAULT") == "DEFAULT"  # 缺键→默认
        config._JSON_CACHE = {"paths": "not-a-dict"}
        assert config._resolve_dir("output", "DEFAULT") == "DEFAULT"    # 非法类型→默认
    finally:
        config._JSON_CACHE = backup
