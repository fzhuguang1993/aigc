"""core/storage.py —— 存储抽象单元测试（全部 tmp，不碰真配置/真盘）"""
import pytest

from core import storage as st


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "lib"
    r.mkdir()
    return r


class TestLocalStorage:
    def test_push_pull_roundtrip(self, root, tmp_path):
        s = st.LocalStorage(str(root))
        src = tmp_path / "a.mp4"
        src.write_bytes(b"video-bytes")
        # push：本地文件 → 素材库相对路径
        dst = s.push(str(src), "钩子/ clip1 .mp4")
        assert dst.exists() and dst.read_bytes() == b"video-bytes"
        assert s.exists("钩子/ clip1 .mp4")
        # pull：素材库 → 另一个本地路径
        back = tmp_path / "back.mp4"
        got = s.pull("钩子/ clip1 .mp4", str(back))
        assert got.exists() and got.read_bytes() == b"video-bytes"

    def test_list_dir(self, root):
        s = st.LocalStorage(str(root))
        (root / "产品A").mkdir()
        (root / "产品A" / "x.mp4").write_bytes(b"x")
        (root / "产品A" / "y.mp4").write_bytes(b"y")
        got = s.list("产品A")
        assert [p.name for p in got] == ["x.mp4", "y.mp4"]
        assert s.list("不存在") == []          # 目录不存在 → 空列表，不抛

    def test_traversal_guard(self, root):
        s = st.LocalStorage(str(root))
        with pytest.raises(ValueError, match="越界"):
            s.exists("../escape.txt")
        with pytest.raises(ValueError, match="越界"):
            s.push(str(root / "ok.txt"), "..//escape.txt")

    def test_local_view_is_real_path(self, root):
        s = st.LocalStorage(str(root))
        assert s.local_view("a/b.mp4") == (root / "a/b.mp4").resolve()


class TestBackendSelection:
    def test_explicit_local(self, root):
        assert isinstance(st.get_storage("local", str(root)), st.LocalStorage)

    def test_unknown_backend_falls_back_local(self, root):
        # 旧配置/乱写的后端值 → 本地兜底，绝不拿不到实例
        assert isinstance(st.get_storage("ftp", str(root)), st.LocalStorage)

    def test_smb_stub_raises(self):
        s = st.get_storage("smb", "/whatever")
        assert isinstance(s, st.SmbStorage)
        with pytest.raises(NotImplementedError, match="SMB"):
            s.exists("x")

    def test_oss_stub_raises(self):
        s = st.get_storage("oss", "/whatever")
        assert isinstance(s, st.OssStorage)
        with pytest.raises(NotImplementedError, match="OSS"):
            s.list()

    def test_default_reads_config(self, root, monkeypatch):
        from core import config
        monkeypatch.setattr(config, "storage_config",
                            lambda: {"backend": "local",
                                     "material_root": str(root),
                                     "output_root": str(root)})
        s = st.get_storage()                       # 不传 kind → 走 config
        assert isinstance(s, st.LocalStorage)
        assert s.root == root


class TestStorageConfig:
    def test_defaults_and_clamp(self, monkeypatch):
        from core import config
        monkeypatch.setattr(config, "read_section",
                            lambda k, d=None: {"backend": "smb-ftp",
                                               "material_root": "", "output_root": ""})
        cfg = config.storage_config()
        assert cfg["backend"] == "local"           # 非法值 → local
        assert cfg["material_root"].endswith("素材库")
        assert cfg["output_root"].endswith("成品库")

    def test_explicit_roots_win(self, monkeypatch):
        from core import config
        monkeypatch.setattr(config, "read_section",
                            lambda k, d=None: {"backend": "oss",
                                               "material_root": "D:/m", "output_root": "D:/o"})
        cfg = config.storage_config()
        assert cfg["backend"] == "oss" and cfg["material_root"] == "D:/m" \
            and cfg["output_root"] == "D:/o"
