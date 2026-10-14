"""
tests/test_modules.py —— 功能模块开关的三层读取与页面可见性

钉住这次最容易出事的地方：
1. 启用集解析优先级：用户层 app_state > 安装层 ProgramData 清单 > 全开；
2. 首启采纳清单后必须 seed 进用户层，此后增减都以用户层为准；
3. 素材工坊三页被 AIGC / 基建 共享：任一启用即可见；
4. 常驻核心页（新手/设置…）不受裁剪，保证总回得到管理入口；
5. page_visible 默认全 True（无清单、用户层空），offscreen 构建零回归。

用 tmp_path 把 app_state 文件与 COMMON_DATA_DIR 都指到临时目录，逐用例隔离。
"""
import json

import pytest

from core import modules
from store import app_state


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """用户层写临时 ui_state，安装层写临时 ProgramData。"""
    monkeypatch.setattr(app_state, "STATE_FILE", tmp_path / "ui_state.json")
    common = tmp_path / "ProgramData"
    common.mkdir()
    monkeypatch.setattr(modules, "COMMON_DATA_DIR", common)
    return common


def _write_manifest(common, mods):
    common.mkdir(parents=True, exist_ok=True)
    (common / "install.json").write_text(
        json.dumps({"modules": mods}), encoding="utf-8")


# ---------------- 三层读取优先级 ----------------

def test_default_all_enabled_when_nothing_recorded():
    """开发 / 绿色 / CI：无用户层、无清单 → 全开，且不写盘。"""
    assert modules.enabled_modules() == set(modules.ALL)
    assert app_state.get("modules_enabled") is None       # 全开不该把开发机钉死


def test_manifest_seeds_user_layer_on_first_run(_isolate):
    """首启读到安装清单、用户层还空：采纳并 seed 进用户层。"""
    _write_manifest(_isolate, ["aigc", "tools"])
    assert modules.enabled_modules() == {"aigc", "tools"}
    assert app_state.get("modules_enabled") == ["aigc", "tools"]     # 已落用户层


def test_user_layer_overrides_manifest(_isolate):
    """用户层已有记录时，安装清单不再说话（个人增减为准）。"""
    _write_manifest(_isolate, ["aigc"])
    app_state.set_value("modules_enabled", ["tools"])
    assert modules.enabled_modules() == {"tools"}


def test_manifest_empty_list_treated_as_no_record(_isolate):
    """清单里的 modules 为空：当作没记录 → 全开（不空侧栏）。"""
    _write_manifest(_isolate, [])
    assert modules.enabled_modules() == set(modules.ALL)


def test_bad_manifest_treated_as_no_record(_isolate):
    """坏档（非 JSON / 含未知键）都不炸：忽略未知键，坏档退回全开。"""
    (_isolate / "install.json").write_text("{ not json", encoding="utf-8")
    assert modules.enabled_modules() == set(modules.ALL)
    _write_manifest(_isolate, ["aigc", "不存在的模块"])
    assert modules.enabled_modules() == {"aigc"}           # 未知键被丢弃


# ---------------- 页面可见性 / 工坊共享 ----------------

def test_page_visible_all_true_by_default():
    for idx in range(15):
        assert modules.page_visible(idx) is True


def test_disabled_module_hides_its_pages():
    app_state.set_value("modules_enabled", ["build"])
    assert modules.page_visible(0) is False        # 数据中台关
    assert modules.page_visible(1) is False        # 任务中心（AIGC）关
    assert modules.page_visible(13) is True        # 批量基建（build）开
    assert modules.page_visible(4) is False        # 工具中心关


def test_workshop_shared_by_aigc_or_build():
    """素材工坊 10/11/12：AIGC 或 基建 任一启用即可见。"""
    app_state.set_value("modules_enabled", ["aigc"])
    assert modules.page_visible(10) is True
    assert modules.workshop_visible() is True
    app_state.set_value("modules_enabled", ["build"])
    assert modules.page_visible(12) is True
    assert modules.workshop_visible() is True
    app_state.set_value("modules_enabled", ["tools"])
    assert modules.page_visible(10) is False
    assert modules.workshop_visible() is False


def test_core_pages_always_visible():
    """核心/维护页（新手5/设置6/接口7/组织8/UI库14）恒可见，保证回得到管理入口。"""
    app_state.set_value("modules_enabled", ["tools"])
    for idx in (5, 6, 7, 8, 14):
        assert modules.page_visible(idx) is True


def test_unregistered_page_defaults_visible():
    """没进归属表的页默认放行（宁可多显示也不误藏新页）。"""
    assert modules.page_visible(999) is True


# ---------------- 翻转与落盘 ----------------

def test_set_enabled_persists():
    app_state.set_value("modules_enabled", ["dashboard"])
    after = modules.set_enabled("tools", True)
    assert "tools" in after
    assert app_state.get("modules_enabled") == ["dashboard", "tools"]
    assert modules.is_enabled("tools") is True
    modules.set_enabled("dashboard", False)
    assert modules.is_enabled("dashboard") is False


def test_module_of_page():
    assert modules.module_of_page(0) == modules.MODULE_DASHBOARD
    assert modules.module_of_page(4) == modules.MODULE_TOOLS
    assert modules.module_of_page(10) is None        # 工坊页无单一归属


# ---------------- 安装期选的成品输出目录（output_dir 交接）----------------

def _write_manifest_full(common, data):
    common.mkdir(parents=True, exist_ok=True)
    (common / "install.json").write_text(json.dumps(data), encoding="utf-8")


def test_install_output_dir_reads_manifest(_isolate):
    """清单里没 output_dir → None；有则去空白后返回。"""
    assert modules.install_output_dir() is None
    _write_manifest_full(_isolate, {"modules": ["aigc"], "output_dir": r"D:\成品"})
    assert modules.install_output_dir() == r"D:\成品"
    _write_manifest_full(_isolate, {"modules": ["aigc"], "output_dir": "   "})
    assert modules.install_output_dir() is None      # 空白串当作没选


def _cfg_dir(tmp_path, monkeypatch):
    """seed_output_dir 在函数内 import core.paths.CONFIG_DIR：挨个模块打补丁即可。"""
    import core.paths
    d = tmp_path / "Roaming"
    d.mkdir()
    monkeypatch.setattr(core.paths, "CONFIG_DIR", d)
    return d


def test_seed_output_dir_writes_first_run_config(_isolate, tmp_path, monkeypatch):
    """首启采纳：安装期选的目录写进 config.json 的 paths.output，且不丢已有字段。"""
    cfg = _cfg_dir(tmp_path, monkeypatch)
    (cfg / "config.json").write_text(json.dumps({"user_name": "张三"}), encoding="utf-8")
    _write_manifest_full(_isolate, {"modules": ["aigc"], "output_dir": r"D:\成品"})
    assert modules.seed_output_dir() is True
    data = json.loads((cfg / "config.json").read_text(encoding="utf-8"))
    assert data["user_name"] == "张三"
    assert data["paths"]["output"] == r"D:\成品"


def test_seed_output_dir_keeps_user_choice(_isolate, tmp_path, monkeypatch):
    """用户已在设置里自定义过输出目录：安装值不覆盖。"""
    cfg = _cfg_dir(tmp_path, monkeypatch)
    (cfg / "config.json").write_text(
        json.dumps({"paths": {"output": r"E:\我的成品"}}), encoding="utf-8")
    _write_manifest_full(_isolate, {"modules": ["aigc"], "output_dir": r"D:\成品"})
    assert modules.seed_output_dir() is False
    data = json.loads((cfg / "config.json").read_text(encoding="utf-8"))
    assert data["paths"]["output"] == r"E:\我的成品"


def test_seed_output_dir_noop_without_manifest_or_config(_isolate, tmp_path, monkeypatch):
    """清单没写 output_dir，或 config.json 还没生成：都静默跳过不抛。"""
    cfg = _cfg_dir(tmp_path, monkeypatch)
    _write_manifest_full(_isolate, {"modules": ["aigc"]})
    (cfg / "config.json").write_text("{}", encoding="utf-8")
    assert modules.seed_output_dir() is False        # 清单没选
    _write_manifest_full(_isolate, {"modules": ["aigc"], "output_dir": r"D:\成品"})
    (cfg / "config.json").unlink()
    assert modules.seed_output_dir() is False        # 首配向导还没跑，不抢跑
