"""
installer/read_version.py —— 从 core/config.py 读 APP_VERSION（给打包脚本用）

为什么不 import core.config：那个模块一加载就要建数据目录、读 config.json、
按网关模式准备凭证，构建机上（可能没装全套依赖、没配接口地址）不该为了拿一个
版本字符串跑这些。这里只做纯文本匹配，源码里那一行就是唯一事实源。
"""
import io
import os
import re
import sys

#: 允许单引号/双引号、行首缩进；与 core/config.py 的写法保持一致
_PAT = re.compile(r"""^\s*APP_VERSION\s*=\s*['"]([^'"]+)['"]""", re.M)

_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "core", "config.py")


def read_version(path=None):
    """返回版本号串；找不到就抛 ValueError（构建脚本据此报错，不用 0.0.0 蒙混）"""
    text = io.open(path or _DEFAULT, encoding="utf-8").read()
    m = _PAT.search(text)
    if not m:
        raise ValueError("APP_VERSION not found in %s" % (path or _DEFAULT))
    return m.group(1).strip()


if __name__ == "__main__":
    try:
        sys.stdout.write(read_version())
    except (ValueError, OSError) as e:
        sys.stderr.write("%s\n" % e)
        sys.exit(1)
