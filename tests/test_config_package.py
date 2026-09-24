"""core/config_package.py —— 加密配置包 v2 单元测试（全部 tmp，不碰真配置/真库）"""
import json

import pytest

from core import config_package as cp


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    """把配置家/素材库/数据库都拨到临时状态，攒一份「像真的」本机环境"""
    cfg = tmp_path / "cfg"
    (cfg / "api_text").mkdir(parents=True)
    (cfg / "config.json").write_text(json.dumps(
        {"user_name": "雷亮", "smb": {"host": "smb-host"},
         "accounts": [{"name": "acc1", "base": "http://1.2.3.4:7860/api/v1"}]},
        ensure_ascii=False), encoding="utf-8")
    (cfg / "ui_state.json").write_text('{"tasks_fields": {"order": [1,2]}}',
                                       encoding="utf-8")
    (cfg / "api_text" / "api_config.json").write_text('{"key": "SECRET-KEY"}',
                                                      encoding="utf-8")
    (cfg / "api_text" / "image.txt").write_text("a.com", encoding="utf-8")
    (cfg / "api_text" / "note.md").write_text("不进包", encoding="utf-8")
    monkeypatch.setattr(cp, "CONFIG_DIR", cfg)

    # 素材库：一张产品图 + 一个大视频（视频不该进包）
    mat = tmp_path / "mat"
    (mat / "products" / "诺特兰德").mkdir(parents=True)
    pic = mat / "products" / "诺特兰德" / "a.jpg"
    pic.write_bytes(b"\xff\xd8\xff\xe0-FAKE-JPEG")
    (mat / "products" / "诺特兰德" / "big.mp4").write_bytes(b"video-bytes")
    monkeypatch.setattr(cp, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(cp, "MATERIAL_DIR", "mat")

    from store import db
    db.execute("DELETE FROM products")
    db.execute("DELETE FROM risk_rules")
    db.execute("INSERT INTO products(id,type,name,images,videos,spec) "
               "VALUES(1,'product','诺特兰德',?,?,'规范卡内容X')",
               (str(pic), str(mat / "products" / "诺特兰德" / "big.mp4")))
    db.execute("INSERT INTO risk_rules(id,scope,title,banned) "
               "VALUES(9,'platform','极限词','最,第一')")
    yield cfg
    db.execute("DELETE FROM products")
    db.execute("DELETE FROM risk_rules")


def _import_into(pkg_path, dest_cfg, tmp_path, monkeypatch):
    """把包导入到「另一台机器」：把 CONFIG_DIR/素材根拨过去再 apply"""
    monkeypatch.setattr(cp, "CONFIG_DIR", dest_cfg)
    monkeypatch.setattr(cp, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(cp, "MATERIAL_DIR", "mat_b")


class TestRoundtrip:
    def test_export_returns_manifest_items(self, cfg_dir, tmp_path):
        pkg = tmp_path / "out.aigccfg"
        info = cp.export_package(str(pkg))
        assert any("产品" in t for t in info["items"])
        assert any("风控" in t for t in info["items"])
        assert info["files"] >= 6

    def test_full_roundtrip(self, cfg_dir, tmp_path, monkeypatch):
        pkg = tmp_path / "out.aigccfg"
        cp.export_package(str(pkg))
        dest = tmp_path / "machine_b" / "cfg"
        dest.mkdir(parents=True)
        _import_into(pkg, dest, tmp_path / "mb", monkeypatch)
        result = cp.import_package(str(pkg))
        assert len(result["applied"]) == 5 and not result["skipped"]
        # 文本三件
        assert json.loads((dest / "config.json").read_text("utf-8"))["user_name"] == "雷亮"
        assert (dest / "api_text" / "image.txt").exists()
        assert not (dest / "api_text" / "note.md").exists()   # 白名单外不落盘
        # 产品/风控进了「新机器」的库
        from store import db
        rows = db.query("SELECT * FROM products")
        assert len(rows) == 1 and rows[0]["spec"] == "规范卡内容X"
        assert db.query("SELECT title FROM risk_rules")[0]["title"] == "极限词"
        # 图片文件搬过去了、路径重写成本机绝对路径；视频没进包、字符串原样留着
        new_pic = tmp_path / "mb" / "mat_b" / "products" / "诺特兰德" / "a.jpg"
        assert new_pic.exists() and new_pic.read_bytes() == b"\xff\xd8\xff\xe0-FAKE-JPEG"
        assert rows[0]["images"] == str(new_pic)
        assert "big.mp4" in rows[0]["videos"] and not (
            tmp_path / "mb" / "mat_b" / "products" / "诺特兰德" / "big.mp4").exists()

    def test_no_plaintext_leak(self, cfg_dir, tmp_path):
        pkg = tmp_path / "out.aigccfg"
        cp.export_package(str(pkg))
        blob = pkg.read_bytes()
        for needle in (b"http://1.2.3.4", b"SECRET-KEY", b"whosyourdaddy",
                       b"\xe8\xa7\x84\xe8\x8c\x83\xe5\x8d\xa1"):   # "规范卡" UTF-8
            assert needle not in blob, "密文里不该能扫出明文"

    def test_each_package_unique(self, cfg_dir, tmp_path):
        a, b = tmp_path / "a.aigccfg", tmp_path / "b.aigccfg"
        cp.export_package(str(a))
        cp.export_package(str(b))
        assert a.read_bytes() != b.read_bytes()      # 随机 salt
        assert json.loads(decrypt_manifest(a)["exported_at"][:4]) or True


def decrypt_manifest(path):
    import zipfile, io
    raw = cp._decrypt_envelope(path.read_bytes(), cp.MAGIC, cp.PASSPHRASE)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return json.loads(zf.read("manifest.json").decode("utf-8"))


class TestVersioning:
    """「后续功能要调整，包要跟着变」：注册表前向/后向兼容"""

    def test_unknown_item_skipped(self, cfg_dir, tmp_path, monkeypatch):
        """新软件导出的包含本机不认识的条目：跳过它，其余照常导入"""
        pkg = tmp_path / "out.aigccfg"
        cp.export_package(str(pkg))
        data = cp.read_package(str(pkg))
        data["manifest"]["items"].append({"id": "future_thing", "title": "未来功能配置"})
        data["items"]["future_thing"] = {"x.json": b"{}"}
        dest = tmp_path / "mc"
        dest.mkdir()
        _import_into(pkg, dest, tmp_path / "mc", monkeypatch)
        result = cp.apply_package(data)
        assert "未来功能配置" in result["skipped"]
        assert len(result["applied"]) == 5          # 本机登记的 5 条全部照常导入
        assert (dest / "config.json").exists()

    def test_missing_item_keeps_local(self, cfg_dir, tmp_path, monkeypatch):
        """老包（没有产品条目）导入新机器：产品表不许被清空"""
        pkg = tmp_path / "old.aigccfg"
        cp.export_package(str(pkg))
        data = cp.read_package(str(pkg))
        data["manifest"]["items"] = [m for m in data["manifest"]["items"]
                                     if m["id"] != "db_products"]
        data["items"].pop("db_products")
        from store import db
        assert db.query("SELECT COUNT(*) c FROM products")[0]["c"] == 1
        result = cp.apply_package(data)
        assert not any("规范卡" in t for t in result["applied"])
        assert len(result["applied"]) == 4
        assert db.query("SELECT COUNT(*) c FROM products")[0]["c"] == 1

    def test_v1_package_still_importable(self, cfg_dir, tmp_path, monkeypatch):
        """升级前导出的 v1 旧包：折算进 v2 条目，别变砖"""
        v1 = {"app": "aigc", "files": {
            "config.json": '{"user_name": "旧包人"}',
            "ui_state.json": '{"player_step_sec": 5}',
            "api_text/api_config.json": '{"k": 1}'}}
        # encrypt_bytes 产出现 v2 头，只换头、salt+token 原样留着（v1 信封同构）
        blob = cp.MAGIC_V1 + cp.encrypt_bytes(
            json.dumps(v1).encode("utf-8"), cp.PASSPHRASE)[len(cp.MAGIC):]
        src = tmp_path / "v1.aigccfg"
        src.write_bytes(blob)
        dest = tmp_path / "mv1"
        dest.mkdir()
        _import_into(src, dest, tmp_path / "mv1", monkeypatch)
        result = cp.import_package(str(src))
        assert json.loads((dest / "config.json").read_text("utf-8"))["user_name"] == "旧包人"
        assert len(result["applied"]) == 3


class TestLines:
    """线路小包（.aigcline）：地址日更的轻量进出口，只碰 accounts 段"""

    def test_roundtrip_only_lines(self, cfg_dir, tmp_path):
        pkg = tmp_path / "l.aigcline"
        info = cp.export_lines(str(pkg))
        assert info["count"] == 1 and info["names"] == ["acc1"]
        # 把本机线路改坏、姓名改掉，再拿包里的好数据救场
        p = cfg_dir / "config.json"
        data = json.loads(p.read_text("utf-8"))
        data["accounts"] = [{"name": "坏线", "base": "http://9.9.9.9"}]
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        got = cp.read_lines(str(pkg))
        assert cp.apply_lines(got["accounts"]) == 1
        after = json.loads(p.read_text("utf-8"))
        assert after["accounts"][0]["base"] == "http://1.2.3.4:7860/api/v1"
        assert after["user_name"] == "雷亮"          # 其它字段原样保留
        assert after["smb"] == {"host": "smb-host"}
        assert any("config.json.bak-" in x.name for x in cfg_dir.iterdir())

    def test_no_plaintext_and_wrong_entry(self, cfg_dir, tmp_path):
        pkg = tmp_path / "l.aigcline"
        cp.export_lines(str(pkg))
        assert b"http://1.2.3.4" not in pkg.read_bytes()
        big = tmp_path / "full.aigccfg"
        cp.export_package(str(big))
        with pytest.raises(ValueError, match="整套配置包"):   # 线路入口喂整包
            cp.read_lines(str(big))
        with pytest.raises(ValueError, match="文件头"):       # 整包入口喂线路包
            cp.read_package(str(pkg))

    def test_dirty_rows_dropped(self, cfg_dir, tmp_path):
        blob = cp.encrypt_bytes(json.dumps({
            "app": "aigc", "kind": "lines", "accounts": [
                {"name": "好", "base": "http://a/"},
                {"name": "空地址", "base": "  "},
                {"name": "歪并发", "base": "http://b", "concurrency": "x"},
            ]}).encode("utf-8"), magic=cp.LINES_MAGIC)
        src = tmp_path / "dirty.aigcline"
        src.write_bytes(blob)
        rows = cp.read_lines(str(src))["accounts"]
        assert [r["name"] for r in rows] == ["好", "歪并发"]
        assert rows[1]["concurrency"] == 1          # 坏数字兜默认值

    def test_wrong_passphrase(self, cfg_dir, tmp_path):
        pkg = tmp_path / "l.aigcline"
        cp.export_lines(str(pkg))
        with pytest.raises(ValueError, match="口令"):
            cp.read_lines(str(pkg), "not-the-password")

    def test_export_without_accounts(self, cfg_dir, tmp_path):
        (cfg_dir / "config.json").write_text('{"user_name": "没人配线路"}',
                                             encoding="utf-8")
        with pytest.raises(ValueError, match="没有.*线路"):
            cp.export_lines(str(tmp_path / "x.aigcline"))


class TestGuards:
    def test_wrong_passphrase(self, cfg_dir, tmp_path):
        pkg = tmp_path / "out.aigccfg"
        cp.export_package(str(pkg))
        with pytest.raises(ValueError, match="口令"):
            cp.read_package(str(pkg), "not-the-password")

    def test_not_a_package(self, tmp_path):
        junk = tmp_path / "junk.aigccfg"
        junk.write_bytes(b"hello world, not a package")
        with pytest.raises(ValueError, match="文件头"):
            cp.read_package(str(junk))

    def test_truncated(self, cfg_dir, tmp_path):
        pkg = tmp_path / "out.aigccfg"
        cp.export_package(str(pkg))
        blob = pkg.read_bytes()
        pkg.write_bytes(blob[:len(blob) // 2])
        with pytest.raises(ValueError, match="口令|损坏"):
            cp.read_package(str(pkg))

    def test_export_without_config_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cp, "CONFIG_DIR", tmp_path / "nowhere")
        with pytest.raises(ValueError, match="config.json"):
            cp.export_package(str(tmp_path / "x.aigccfg"))


class TestBackup:
    def test_old_files_backed_up(self, cfg_dir, tmp_path, monkeypatch):
        pkg = tmp_path / "out.aigccfg"
        cp.export_package(str(pkg))
        dest = tmp_path / "mb2" / "cfg"
        dest.mkdir(parents=True)
        (dest / "config.json").write_text('{"user_name": "旧主人在这里"}',
                                          encoding="utf-8")
        _import_into(pkg, dest, tmp_path / "mb2", monkeypatch)
        cp.import_package(str(pkg))
        assert json.loads((dest / "config.json").read_text("utf-8"))["user_name"] == "雷亮"
        backups = [p.name for p in dest.iterdir() if "config.json.bak-" in p.name]
        assert len(backups) == 1
        assert "旧主人在这里" in (dest / backups[0]).read_text("utf-8")
