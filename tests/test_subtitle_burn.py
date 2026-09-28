"""
tests/test_subtitle_burn.py —— 烧录命令构建 + ASS 滤镜路径转义（不跑真 ffmpeg）

钉两类命令：
- 录屏无声 mp4（audio_wav 非空）→ 两路输入、-map 1:a、AAC 重编音轨；
- 视频自带音轨（audio_wav 为空）→ -map 0:a?、-c:a copy 不动音频；
- Windows 路径转义：反斜杠→/、冒号→\\:，避免盘符被当滤镜选项分隔。
"""
from video_text_tools.subtitle.burn import _ass_filter, build_subtitle_command


def test_ass_filter_windows_escape():
    assert _ass_filter("C:\\a b\\x.ass") == "ass=C\\:/a b/x.ass"


def test_command_with_sidecar_audio():
    cmd = build_subtitle_command("ffmpeg", "v.mp4", "C:\\s\\v.ass", "v_内部声音.wav", "out.mp4")
    assert cmd.count("-i") == 2
    assert "v_内部声音.wav" in cmd
    assert "-map" in cmd and "1:a:0" in cmd            # 把旁路音轨接回
    assert "aac" in cmd                                 # 音轨重编码
    assert any(x.startswith("ass=") for x in cmd)      # 滤镜
    assert "out.mp4" == cmd[-1]


def test_command_without_sidecar_audio():
    cmd = build_subtitle_command("ffmpeg", "v.mp4", "v.ass", None, "out.mp4")
    assert cmd.count("-i") == 1
    assert "0:a?" in cmd                                # 有音轨则带上、无也不报错
    assert "copy" in cmd                                # 音频直通不二次压
    assert "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "copy"


def test_command_video_codec_common():
    cmd = build_subtitle_command("ffmpeg", "v.mp4", "v.ass", "a.wav", "out.mp4")
    for token in ("libx264", "yuv420p", "+faststart"):
        assert token in cmd
