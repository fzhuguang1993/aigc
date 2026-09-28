# tests/test_subprocess_encoding.py
"""静态检查：text 模式子进程必须显式指定 encoding。

背景：ffmpeg/ffprobe 输出是 UTF-8（含中文文件名/路径的字节）。若 text=True
不指定 encoding，Windows 会用 locale（GBK）解码，subprocess._readerthread 抛
UnicodeDecodeError——控制台刷几十条线程异常，且 stdout/stderr 变为 None。
已在 ffmpeg_utils / watermark 修过两处；本测试全仓库 AST 扫描防回归。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "build", "dist", ".git", ".pytest_cache", "__pycache__"}
TEXT_ENTRIES = {"run", "call", "check_call", "check_output", "Popen"}
TEXT_KW = {"text", "universal_newlines"}


def _subprocess_calls(tree):
    """产出所有 subprocess.xxx(...) 形式的调用节点"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in TEXT_ENTRIES:
            base = f.value
            if isinstance(base, ast.Name) and base.id == "subprocess":
                yield node


def test_text_mode_subprocess_must_set_encoding():
    offenders = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for call in _subprocess_calls(tree):
            kws = {k.arg for k in call.keywords if k.arg}
            text_on = any(
                k.arg in TEXT_KW
                and isinstance(k.value, ast.Constant)
                and k.value.value is True
                for k in call.keywords)
            if text_on and "encoding" not in kws:
                offenders.append(f"{rel}:{call.lineno}")
    assert not offenders, (
        "以下 text 模式子进程调用缺少 encoding=，Windows 中文路径下会以 GBK "
        "解码 UTF-8 输出并炸掉读取线程：\n  " + "\n  ".join(offenders))
