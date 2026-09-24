"""core/config_package.py —— 加密配置包单元测试（全部走 tmp_path，不碰真配置）"""
import json

import pytest

from core import config_package as cp


@pytest.fixture
def cfg_dir(tmp_path):
    """一份「本机配置」的样子：config.json + 界面偏好 + api_text 凭证/白名单"""
    (tmp_path / "api_text").mkdir()
    (tmp_path / "config.json").write_text(json.dumps(
        {"user_name": "雷亮",
         "accounts": [{"name": "acc1", "base": "http://1.2.3.4:7860/api/v1"}]},
        ensure_ascii=False), encoding="utf-8")
    (tmp_path / "ui_state.json").write_text('{"player_step_sec": 2}', encoding="utf-8")
    (tmp_path / "api_text" / "api_config.json").write_text(
        '{"base": "https://secret.example/api", "key": "SECRET-KEY"}', encoding="utf-8")
    (tmp_path / "api_text" / "image.txt").write_text("a.com\nb.com", encoding="utf-8")
    # 不该进包的东西：子目录 / 非白名单扩展名
    (tmp_path / "api_text" / "sub").mkdir()
    (tmp_path / "api_text" / "sub" / "x.json").write_text("{}", encoding="utf-8")
    (tmp_path / "api_text" / "note.md").write_text("hi", encoding="utf-8")
    return tmp_path


def _fake_package(tmp_path, files, name="evil.aigccfg"):
    """手工做一个任意内容的包（绕过 export 的白名单，验 import 侧防线）"""
    payload = {"app": "aigc", "files": files}
    blob = cp.encrypt_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    p = tmp_path / name
    p.write_bytes(blob)
    return str(p)


class TestRoundtrip:
    def test_export_then_import(self, cfg_dir, tmp_path):
        pkg = tmp_path / "out.aigccfg"
        n = cp.export_package(str(pkg), config_dir=cfg_dir)
        assert n == 4                      # config/ui_state/api_config/image
        dest = tmp_path / "machine_b"
        dest.mkdir()
        got = cp.import_package(str(pkg), config_dir=dest)
        assert got == 4
        assert json.loads((dest / "config.json").read_text(encoding="utf-8"))[
            "user_name"] == "雷亮"
        assert (dest / "api_text" / "image.txt").read_text(encoding="utf-8") == "a.com\nb.com"
        # 白名单外的东西不落盘
        assert not (dest / "api_text" / "note.md").exists()
        assert not (dest / "api_text" / "sub").exists()

    def test_no_plaintext_leak(self, cfg_dir, tmp_path):
        pkg = tmp_path / "out.aigccfg"
        cp.export_package(str(pkg), config_dir=cfg_dir)
        blob = pkg.read_bytes()
        for needle in (b"http://1.2.3.4", b"SECRET-KEY", b"whosyourdaddy",
                       b"\xe9\x9b\xb7\xe4\xba\xae"):      # "雷亮" 的 UTF-8
            assert needle not in blob, "密文里不该能扫出明文"

    def test_each_package_unique(self, cfg_dir, tmp_path):
        """同口令同内容两次导出密文不同（随机 salt），但都能解开"""
        a = tmp_path / "a.aigccfg"; b = tmp_path / "b.aigccfg"
        cp.export_package(str(a), config_dir=cfg_dir)
        cp.export_package(str(b), config_dir=cfg_dir)
        assert a.read_bytes() != b.read_bytes()
        assert cp.read_package(str(a)) == cp.read_package(str(b))


class TestGuards:
    def test_wrong_passphrase(self, cfg_dir, tmp_path):
        pkg = tmp_path / "out.aigccfg"
        cp.export_package(str(pkg), config_dir=cfg_dir)
        with pytest.raises(ValueError, match="口令"):
            cp.read_package(str(pkg), "not-the-password")

    def test_not_a_package(self, tmp_path):
        junk = tmp_path / "junk.aigccfg"
        junk.write_bytes(b"hello world, not a package at all")
        with pytest.raises(ValueError, match="文件头"):
            cp.read_package(str(junk))

    def test_truncated(self, cfg_dir, tmp_path):
        pkg = tmp_path / "out.aigccfg"
        cp.export_package(str(pkg), config_dir=cfg_dir)
        blob = pkg.read_bytes()
        pkg.write_bytes(blob[:len(blob) // 2])
        with pytest.raises(ValueError, match="口令|不完整"):
            cp.read_package(str(pkg))

    def test_path_traversal_rejected(self, cfg_dir, tmp_path):
        pkg = _fake_package(tmp_path, {"config.json": "{}", "../evil.txt": "x"})
        with pytest.raises(ValueError, match="不允许"):
            cp.read_package(pkg)
        # 就算绕过 read 直接喂 apply，也不落盘
        dest = tmp_path / "dst"; dest.mkdir()
        cp.apply_package({"config.json": "{}", "/etc/passwd": "x",
                          ".." + "/evil": "x"}, config_dir=dest)
        assert (dest / "config.json").exists()
        assert not (tmp_path / "evil").exists()
        assert list(dest.iterdir()) == [dest / "config.json"]

    def test_empty_package_rejected(self, tmp_path):
        pkg = _fake_package(tmp_path, {"ui_state.json": "{}"})
        with pytest.raises(ValueError, match="空"):
            cp.read_package(pkg)

    def test_export_without_config_raises(self, tmp_path):
        with pytest.raises(ValueError, match="config.json"):
            cp.export_package(str(tmp_path / "x.aigccfg"),
                              config_dir=tmp_path / "nowhere")


class TestBackup:
    def test_old_files_backed_up(self, cfg_dir, tmp_path):
        pkg = tmp_path / "out.aigccfg"
        cp.export_package(str(pkg), config_dir=cfg_dir)
        dest = tmp_path / "machine_b"; dest.mkdir()
        (dest / "config.json").write_text('{"user_name": "旧主人在这里"}',
                                          encoding="utf-8")
        cp.import_package(str(pkg), config_dir=dest)
        assert json.loads((dest / "config.json").read_text(encoding="utf-8"))[
            "user_name"] == "雷亮"
        backups = [p.name for p in dest.iterdir() if ".bak-" in p.name]
        assert len(backups) == 1 and backups[0].startswith("config.json.bak-")
        assert "旧主人在这里" in (dest / backups[0]).read_text(encoding="utf-8")
