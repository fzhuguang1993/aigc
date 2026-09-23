"""
tests/conftest.py —— 回归测试公共夹具

关键约定：
- 必须在导入 core.config 之前设置 AIGC_HOME，把运行时目录
  （SQLite / 日志 / 输出）隔离到临时目录，测试不碰真实数据。
- 项目根目录加入 sys.path，测试内可直接 import core/store/workers。
"""
import os
import sys
import tempfile
from pathlib import Path

# 必须早于任何 core.* 导入（RUNTIME_DIR / CONFIG_DIR 在 config 导入时固化）
_AIGC_TEST_HOME = tempfile.mkdtemp(prefix="aigc_test_")
os.environ["AIGC_HOME"] = _AIGC_TEST_HOME
# 凭证配置家也隔到临时目录，避免测试写到真实 %APPDATA%
os.environ["AIGC_CONFIG_DIR"] = str(Path(_AIGC_TEST_HOME) / "_config")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    """整场测试建一次表（库文件在 AIGC_HOME/data/ 下）"""
    from store import db
    db.init()


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后清空任务/运行记录/审片标记与运行时注册表，用例互不干扰"""
    from store import db
    from registry.manager import REG
    from core import naming

    def _reset():
        db.execute("DELETE FROM tasks")
        db.execute("DELETE FROM runs")
        db.execute("DELETE FROM file_marks")
        REG.tasks.clear()
        # 命名规则是模块级缓存：一个用例 set_rules 过就会泄给下一个，
        # 表现为“同一个文件名单独跑能过、整批跑不过”。每轮压回内置默认。
        naming.set_rules(list(naming.DEFAULT_TOKENS), naming.DEFAULT_SEP)

    _reset()
    yield
    _reset()
