"""
tests/test_runtime_dir.py —— 运行时目录与凭证外泄回归测试

锁死两件事：
1. 数据落地目录绝不跟随 cwd（打包版＝exe 所在目录，开发版＝仓库根），
   且首次运行向导写 config.json 的位置必须和 config 读的是同一个；
2. 素材解析接口的真实凭证不许出现在会被推送的 core/config.py 里，
   只能写在 gitignored 的 core/config_local.py。
"""
import ast
import sys
from pathlib import Path

from core import config, paths, setup_wizard


def test_runtime_dir_does_not_follow_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert paths._runtime_dir() == Path(paths.__file__).resolve().parents[1]


def test_runtime_dir_follows_exe_when_frozen(monkeypatch, tmp_path):
    exe = tmp_path / "AIGC视频助手-GUI.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert paths._runtime_dir() == tmp_path.resolve()   # macOS 下 /var 会被 resolve 成 /private/var


def test_wizard_and_config_share_one_runtime_dir():
    # 两边各算一套曾经一个跟 cwd、一个跟 exe，导致向导写的配置读不到
    assert config.RUNTIME_DIR is paths.RUNTIME_DIR
    assert config.CONFIG_DIR is paths.CONFIG_DIR
    assert setup_wizard.CONFIG_JSON == config.CONFIG_JSON


def test_credential_configs_all_live_under_config_dir():
    """凭证类配置（主配置/界面状态/素材接口）都落在隐藏配置家，同一口径。"""
    from store import app_state
    assert config.CONFIG_JSON == paths.CONFIG_DIR / "config.json"
    assert app_state.STATE_FILE.parent == paths.CONFIG_DIR
    assert Path(config.API_TEXT_DIR) == paths.CONFIG_DIR / "api_text"


def test_material_api_defaults_are_empty():
    """core/config.py 里 MATERIAL_API_* 必须是空串：真值只放 config_local。"""
    tree = ast.parse(Path(config.__file__).read_text(encoding="utf-8"))
    assigned = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name) and t.id.startswith("MATERIAL_API_"):
                assigned[t.id] = node.value
    assert set(assigned) >= {"MATERIAL_API_BASE", "MATERIAL_API_UID",
                             "MATERIAL_API_KEY"}
    for name, value in assigned.items():
        assert isinstance(value, ast.Constant) and value.value == "", \
            f"{name} 被写上了真实凭证，应改放 core/config_local.py"
