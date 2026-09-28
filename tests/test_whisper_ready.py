"""
tests/test_whisper_ready.py —— 精简版不误报「模型已就绪」

根因：model_ready 过去只看 MODELS_DIR 里有没有权重文件，不看 faster_whisper 能否
导入。标准版 --exclude-module 掉了 faster_whisper，但维护机模型文件仍在，界面就谎报
「已就绪」。修好后：就绪 = 库可用 且 文件就绪。
"""
import builtins

from video_text_tools.asr import transcribe


def test_model_ready_false_without_library(monkeypatch):
    monkeypatch.setattr(transcribe, "whisper_available", lambda: False)
    monkeypatch.setattr(transcribe, "_plain_ready", lambda size: True)
    monkeypatch.setattr(transcribe, "_blobs_ready", lambda size: True)
    assert transcribe.model_ready("small") is False


def test_model_ready_true_with_library_and_files(monkeypatch):
    monkeypatch.setattr(transcribe, "whisper_available", lambda: True)
    monkeypatch.setattr(transcribe, "_plain_ready", lambda size: True)
    assert transcribe.model_ready("small") is True


def test_model_ready_false_with_library_but_no_files(monkeypatch):
    monkeypatch.setattr(transcribe, "whisper_available", lambda: True)
    monkeypatch.setattr(transcribe, "_plain_ready", lambda size: False)
    monkeypatch.setattr(transcribe, "_blobs_ready", lambda size: False)
    assert transcribe.model_ready("small") is False


def test_best_ready_size_none_without_library(monkeypatch):
    monkeypatch.setattr(transcribe, "whisper_available", lambda: False)
    monkeypatch.setattr(transcribe, "_blobs_ready", lambda size: True)
    assert transcribe.best_ready_size("small") is None


def test_whisper_available_false_when_import_fails(monkeypatch):
    """缓存归零 + 让 import faster_whisper 抛错 → whisper_available() 应返回 False。"""
    monkeypatch.setattr(transcribe, "_WHISPER_OK", None)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("simulated: 精简版未打包 faster_whisper")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert transcribe.whisper_available() is False
