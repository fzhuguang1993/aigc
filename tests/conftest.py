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

# 必须早于任何 core.* 导入（RUNTIME_DIR 在 config 导入时固化）
os.environ["AIGC_HOME"] = tempfile.mkdtemp(prefix="aigc_test_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    """整场测试建一次表（库文件在 AIGC_HOME/data/ 下）"""
    from store import db
    db.init()


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后清空任务/运行记录与运行时注册表，用例互不干扰"""
    from store import db
    from registry.manager import REG

    def _reset():
        db.execute("DELETE FROM tasks")
        db.execute("DELETE FROM runs")
        REG.tasks.clear()

    _reset()
    yield
    _reset()
