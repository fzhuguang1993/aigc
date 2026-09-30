# ============================================================
# storage.py —— 素材/成品统一存储抽象
# ============================================================
"""把「素材库/成品库落哪儿」收敛成一层可插拔接口。

本轮只实现 LocalStorage（本地磁盘 pathlib 直连，零新依赖）；SmbStorage/OssStorage
仅定义类与后端选择位，任何能力调用都给统一「即将支持」提示，绝不进默认流程。
上层（拆分 / 归档 / 素材库 / 混剪）只跟 Storage 接口打交道——将来切 SMB/OSS
只换 get_storage 返回的实例，业务码一行不动。

约定：所有路径都用「相对根目录的相对路径（rel）」表达，屏蔽本地/远端差异；
LocalStorage 的 rel 解析做了穿越防护，`../` 逃逸出 root 一律拒绝。
"""
from __future__ import annotations

import shutil
from pathlib import Path


class Storage:
    """存储后端抽象：以 root 为家，一切读写都用相对 root 的 rel 路径。"""

    backend = ""

    def __init__(self, root: str = ""):
        self.root = Path(root) if root else None

    # ---- 子类须实现的能力（基类给出契约，调用即抛） ----
    def exists(self, rel) -> bool:
        raise NotImplementedError

    def list(self, subdir="") -> list:
        raise NotImplementedError

    def pull(self, rel, local) -> Path:
        """把存储里的 rel 取到本地路径 local，返回本地文件。"""
        raise NotImplementedError

    def push(self, local, rel) -> Path:
        """把本地文件 local 放进存储的 rel 位置，返回存储侧最终路径。"""
        raise NotImplementedError

    def local_view(self, rel):
        """给播放器 / 资源管理器看的可视路径：本地即真实路径，远端将来返回缓存副本。"""
        raise NotImplementedError


class LocalStorage(Storage):
    """本地磁盘实现：root 下一切用 pathlib 直连，含相对路径穿越防护。"""

    backend = "local"

    def _abs(self, rel) -> Path:
        """把 rel 安全解析到 root 之内；`../` 逃逸或绝对越界一律 ValueError。"""
        if self.root is None:
            raise ValueError("LocalStorage 未设置根目录")
        p = (self.root / str(rel).lstrip("/\\")).resolve()
        base = self.root.resolve()
        if base != p and base not in p.parents:
            raise ValueError(f"越界路径：{rel}")
        return p

    def exists(self, rel):
        return self._abs(rel).exists()

    def list(self, subdir=""):
        d = self._abs(subdir)
        if not d.is_dir():
            return []
        return sorted(d.iterdir())

    def pull(self, rel, local):
        src = self._abs(rel)
        dst = Path(local)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return dst

    def push(self, local, rel):
        src = Path(local)
        dst = self._abs(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return dst

    def local_view(self, rel):
        return self._abs(rel)


class _RemoteStub(Storage):
    """远端后端占位基类：本轮不落地，任何能力调用都给统一提示（消息由子类覆写）。"""

    _msg = "远端存储即将支持"

    def exists(self, rel):
        raise NotImplementedError(self._msg)

    def list(self, subdir=""):
        raise NotImplementedError(self._msg)

    def pull(self, rel, local):
        raise NotImplementedError(self._msg)

    def push(self, local, rel):
        raise NotImplementedError(self._msg)

    def local_view(self, rel):
        raise NotImplementedError(self._msg)


class SmbStorage(_RemoteStub):
    """SMB 共享盘后端占位：接口与本地一致，方法体一律抛「即将支持」。"""
    backend = "smb"
    _msg = "SMB 存储即将支持"


class OssStorage(_RemoteStub):
    """OSS 对象存储后端占位：接口与本地一致，方法体一律抛「即将支持」。"""
    backend = "oss"
    _msg = "OSS 存储即将支持"


#: 后端注册表：local 已落地；smb/oss 挂占位类，落地后替换实现类即可。
REGISTRY = {
    "local": LocalStorage,
    "smb": SmbStorage,
    "oss": OssStorage,
}


def get_storage(kind=None, root=None) -> Storage:
    """按 backend 取存储实例。

    kind 缺省读 config 的 storage.backend（默认 local）；root 缺省按 local 取素材库根。
    没登记的后端值（乱写/旧配置）回退本地，绝不让上层拿不到实例；显式要 smb/oss 则
    返回对应占位类（调用能力时抛「即将支持」），以便界面上如实反映未落地状态。
    """
    backend = str(kind or "").strip().lower()
    if not backend:
        from core.config import storage_config
        backend = str(storage_config().get("backend") or "local").strip().lower()
    if backend not in REGISTRY:
        backend = "local"
    if root is None:
        from core.config import storage_config
        root = storage_config()["material_root"]
    return REGISTRY[backend](root)
