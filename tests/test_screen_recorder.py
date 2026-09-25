"""
test_screen_recorder.py —— 屏幕录制功能层单元测试

不真实录屏：命令组装/坐标换算/设备解析用纯断言；Recorder 生命周期用
「读 stdin 一个字节的假脚本」模拟 ffmpeg 的 q 收尾协议。
"""
import os
import sys

import pytest

from video_text_tools.screen_recorder import (
    IS_WINDOWS, Recorder, audio_out_paths, build_record_command,
    escape_dshow_name, has_wasapi_loopback, is_mix_device,
    list_audio_devices, list_windows, parse_dshow_devices, rect_to_physical,
    screen_physical, virtual_desktop, _normalize_audio_list,
    _parse_demuxer_check)


# --------------------------------------------------------------------
# 坐标换算
# --------------------------------------------------------------------
def test_screen_physical_dpr():
    assert screen_physical((0, 0, 1920, 1080, 1.25)) == (0, 0, 2400, 1350)
    assert screen_physical((100, 50, 800, 600, 1.0)) == (100, 50, 800, 600)


def test_virtual_desktop_union_with_negative():
    # 主屏 1.25 缩放 + 左侧负坐标副屏：包围盒按物理像素并集
    screens = [(0, 0, 1920, 1080, 1.25), (-1920, 0, 1920, 1080, 1.0)]
    assert virtual_desktop(screens) == (-1920, 0, 4320, 1350)
    assert virtual_desktop([]) is None


def test_rect_to_physical_uses_dpr_of_containing_screen():
    screens = [(0, 0, 1920, 1080, 1.25), (1920, 0, 1920, 1080, 1.0)]
    # 左上角落在主屏 -> 按 1.25 换算（含原点偏移也乘 DPR）
    assert rect_to_physical((100, 50, 300, 200), screens) == (125, 62, 375, 250)
    # 落在副屏 -> 按 1.0 换算
    assert rect_to_physical((2000, 10, 300, 200), screens) == (2000, 10, 300, 200)
    # 不在任何屏内（脏数据）-> 退回主屏 DPR（10*1.25 取整到 12）
    assert rect_to_physical((0, 0, 10, 10), screens) == (0, 0, 12, 12)
    # 无屏可用 -> 原样透传
    assert rect_to_physical((7, 8, 9, 10), []) == (7, 8, 9, 10)


# --------------------------------------------------------------------
# dshow 设备解析与转义
# --------------------------------------------------------------------
DSHOW_SAMPLE = """
[dshow @ 0000] Direct video capture devices
[dshow @ 0000]   Alternative name "@device_pnp_{...}"
[dshow @ 0000] Direct audio capture devices
[dshow @ 0000]     "Microphone (Realtek Audio)"
[dshow @ 0000]     Alternative name "@device_cm_{...}"
[dshow @ 0000]     "立体声混音 (Realtek Audio)"
[dshow @ 0000] End of audio devices list
"""


def test_parse_dshow_devices_audio_only():
    assert parse_dshow_devices(DSHOW_SAMPLE) == [
        "Microphone (Realtek Audio)", "立体声混音 (Realtek Audio)"]
    assert parse_dshow_devices("") == []
    assert parse_dshow_devices(None) == []


def test_parse_dshow_devices_ffmpeg9_format():
    """ffmpeg 9+ 真机实测格式：无区段标题，行尾 (audio)/(none) 类型标记"""
    sample = (
        '[in#0 @ 0000] "OBS Virtual Camera" (none)\n'
        '[in#0 @ 0000]   Alternative name "@device_sw_{860BB310}"\n'
        '[in#0 @ 0000] "Analogue 1 + 2 (Focusrite USB Audio)" (audio)\n'
        '[in#0 @ 0000]   Alternative name "@device_cm_{8C607DC0}"\n'
        '[in#0 @ 0000] "\u7acb\u4f53\u58f0\u6df7\u97f3 (Realtek(R) Audio)" (audio)\n')
    assert parse_dshow_devices(sample) == [
        "Analogue 1 + 2 (Focusrite USB Audio)",
        "立体声混音 (Realtek(R) Audio)"]


def test_escape_dshow_name():
    assert escape_dshow_name(r"a\b") == r"a\\b"
    assert escape_dshow_name("Mic: 阵列=1") == r"Mic\: 阵列\=1"
    assert escape_dshow_name(None) == ""


def test_list_audio_devices_silent_on_missing_exe():
    # ffmpeg 不存在/跑不动一律给空列表，不抛异常（面板降级为不录音）
    assert list_audio_devices("") == []
    assert list_audio_devices(r"Z:\no\such\ffmpeg.exe") == []


# --------------------------------------------------------------------
# 命令组装
# --------------------------------------------------------------------
def _seg(cmd):
    return " ".join(cmd)


def test_build_fullscreen_primary_default():
    plans = build_record_command("ffmpeg.exe", "fullscreen", "a.mp4")
    assert len(plans) == 1 and plans[0][1] == "a.mp4"
    cmd = plans[0][0]
    s = _seg(cmd)
    assert cmd[:5] == ["ffmpeg.exe", "-hide_banner", "-y", "-f", "gdigrab"]
    assert "-i desktop" in s
    assert "-offset_x" not in s          # 不给范围 = gdigrab 默认主屏
    assert "-an" in s and "dshow" not in s
    assert "-c:v libx264 -preset veryfast -b:v 4M" in s
    assert "-pix_fmt yuv420p" in s and "-movflags +faststart" in s
    assert cmd[-1] == "a.mp4"


def test_build_region_offset_and_even_size():
    cmd = build_record_command("ffmpeg.exe", "region", "a.mp4",
                               physical=(10, 20, 1001, 501), fps=15,
                               bitrate="8M")[0][0]
    s = _seg(cmd)
    # ⚠ 设备选项必须紧跟 -i 之前：放后面 ffmpeg 不报错但静默忽略，
    # 捕获变成整个桌面（历史 bug，真机 ffmpeg 9.0 实测复现）
    assert "-framerate 15 -offset_x 10 -offset_y 20 -video_size 1000x500 " \
           "-i desktop" in s
    assert "-b:v 8M" in s


def test_build_window_mode():
    cmd = build_record_command("ffmpeg.exe", "window", "a.mp4",
                               title="AIGC 工厂")[0][0]
    s = _seg(cmd)
    assert "-i title=AIGC 工厂" in s
    assert "-offset_x" not in s


def test_build_with_audio_maps_and_escapes():
    plans = build_record_command("ffmpeg.exe", "fullscreen", "a.mp4",
                                 audio=r"Mic : 1=2")
    assert len(plans) == 2                       # 视频 + 一条音轨两个子进程
    vs, acmd = plans[0][0], plans[1][0]
    assert "-an" in _seg(vs)                     # 视频不含声轨
    s = _seg(acmd)
    assert "-f dshow -i audio=Mic \\: 1\\=2" in s
    assert "-c:a pcm_s16le -ar 48000 -ac 2" in s
    assert plans[1][1] == "a_麦克风.wav" and acmd[-1] == "a_麦克风.wav"
    assert "gdigrab" not in s                    # 音轨进程不碰视频


def test_build_audio_str_shorthand_equals_mic_tuple():
    """旧写法（裸设备名字符串）与 ("mic", 名字) 完全同义，向后兼容"""
    a = build_record_command("f", "fullscreen", "a.mp4", audio="Mic X")
    b = build_record_command("f", "fullscreen", "a.mp4", audio=("mic", "Mic X"))
    assert a == b


def test_build_audio_system_wasapi_loopback():
    plans = build_record_command("f", "fullscreen", "a.mp4", audio=("system", None))
    acmd = plans[1][0]
    i = acmd.index("wasapi_loopback")
    assert acmd[i - 1] == "-f" and acmd[i + 1] == "-i" and acmd[i + 2] == ""
    assert plans[1][1] == "a_内部声音.wav"


def test_build_audio_system_dshow_mix_device():
    plans = build_record_command("f", "fullscreen", "a.mp4",
                                 audio=("system", "Stereo Mix (Realtek)"))
    assert "audio=Stereo Mix (Realtek)" in plans[1][0]


def test_build_audio_junk_spec_falls_back_to_no_audio():
    for junk in (("mic", None), ("weird", "x"), ()):
        plans = build_record_command("f", "fullscreen", "a.mp4", audio=junk)
        assert len(plans) == 1                   # 没音源：不多余开音轨进程
        assert "-an" in _seg(plans[0][0])
        assert "dshow" not in _seg(plans[0][0]) and "wasapi" not in _seg(plans[0][0])


def test_build_dual_audio_two_wav_commands():
    """内部+外部多选：一条视频命令 + 每源一条独立 WAV 命令"""
    plans = build_record_command("f", "fullscreen", "a.mp4",
                                 audio=[("system", "Stereo Mix"), ("mic", "Mic X")])
    assert len(plans) == 3
    assert [p for _c, p in plans] == ["a.mp4", "a_内部声音.wav", "a_麦克风.wav"]
    assert "audio=Stereo Mix" in _seg(plans[1][0])
    assert "audio=Mic X" in _seg(plans[2][0])
    assert "Mic X" not in _seg(plans[1][0]) and "Stereo Mix" not in _seg(plans[2][0])


def test_audio_out_paths_naming():
    assert audio_out_paths("d:/out/录屏_1.mp4",
                           [("system", None), ("mic", "M")]) == [
        (("system", None), "d:/out/录屏_1_内部声音.wav"),
        (("mic", "M"), "d:/out/录屏_1_麦克风.wav")]
    assert audio_out_paths("a.mp4", None) == []
    # 单规格（非列表）也收；同类型去重只留第一个（文件名会撞）
    assert audio_out_paths("a.mp4", ("mic", "A")) == [(("mic", "A"), "a_麦克风.wav")]
    assert len(audio_out_paths("a.mp4", [("mic", "A"), ("mic", "B")])) == 1


def test_normalize_audio_list_accepts_mixed_forms():
    assert _normalize_audio_list(None) == []
    assert _normalize_audio_list("Mic X") == [("mic", "Mic X")]
    assert _normalize_audio_list(["Mic X", ("system", None)]) == [
        ("mic", "Mic X"), ("system", None)]
    # 垃圾项静默丢弃，不炸列表
    assert _normalize_audio_list([("weird", "x"), ("mic", "M")]) == [("mic", "M")]


def test_is_mix_device():
    assert is_mix_device("Stereo Mix (Realtek Audio)")
    assert is_mix_device("立体声混音 (Realtek Audio)")
    assert is_mix_device("What U Hear")
    assert not is_mix_device("Microphone (Realtek Audio)")
    assert not is_mix_device(None)


def test_parse_demuxer_check():
    assert _parse_demuxer_check("wasapi_loopback demuxer\nAVOptions...")
    assert not _parse_demuxer_check("Unknown demuxer 'wasapi_loopback'.")
    # 真机 ffmpeg 9.0 实测：不支持时报的是 Unknown format（不是 Unknown demuxer）
    assert not _parse_demuxer_check("Unknown format 'wasapi_loopback'.\n")
    assert not _parse_demuxer_check("")
    assert not _parse_demuxer_check(None)


def test_has_wasapi_loopback_silent_on_missing_exe():
    assert has_wasapi_loopback("") is False
    assert has_wasapi_loopback(r"Z:\no\such\ffmpeg.exe") is False


def test_build_rejects_bad_input():
    with pytest.raises(ValueError):
        build_record_command("f", "nope", "a.mp4")
    with pytest.raises(ValueError):
        build_record_command("f", "window", "a.mp4")            # 缺标题
    with pytest.raises(ValueError):
        build_record_command("f", "window", "a.mp4", title="  ")
    with pytest.raises(ValueError):
        build_record_command("f", "region", "a.mp4")            # 缺范围


# --------------------------------------------------------------------
# 窗口枚举（平台守卫 + 结构约定）
# --------------------------------------------------------------------
def test_list_windows_structure():
    wins = list_windows()
    assert isinstance(wins, list)
    if not IS_WINDOWS:
        assert wins == []
        assert list_windows(exclude_self=True) == []
        return
    import ctypes
    user32 = ctypes.windll.user32
    for hwnd, title, rect, minimized in wins:
        assert isinstance(hwnd, int) and title.strip()
        assert len(rect) == 4
        # 列出的就是「任务栏有按钮」的窗：最小化标志必须和真机状态一致
        assert minimized == bool(user32.IsIconic(hwnd))
        if not minimized:
            # 展开态才有真矩形；最小化窗的矩形是屏幕外占位（约 158×26）
            assert rect[2] >= 64 and rect[3] >= 64
    # pytest 进程没有可见顶层窗：exclude_self 结果不应多于全量
    assert len(list_windows(exclude_self=True)) <= len(wins)


# --------------------------------------------------------------------
# Recorder 生命周期（假 ffmpeg：等 stdin 的 q，再落盘）
# --------------------------------------------------------------------
_FAKE_FFMPEG = """
import sys
data = sys.stdin.buffer.read(1)      # 等录制控制层写 'q'
if data:
    open(sys.argv[1], "w").write("ok")
"""

_FAKE_EARLY_EXIT = """
import sys
sys.exit(3)                          # 不产出任何文件就退（模拟启动即崩）
"""


def _fake_exe(tmp_path, body):
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(body, encoding="utf-8")
    return [sys.executable, str(script)]


def test_recorder_stop_sends_q_and_cleans_up(tmp_path):
    out = str(tmp_path / "rec.mp4")
    rec = Recorder([(_fake_exe(tmp_path, _FAKE_FFMPEG) + [out], out)])
    assert rec.start() is None
    assert rec.alive()
    assert rec.start() == "录制已在进行中"      # 防重复启动
    assert rec.stop() is None
    assert not rec.alive()
    assert os.path.isfile(out)                  # q 协议：子进程收到后才写文件
    assert not os.path.exists(rec.err_path)     # 成功收尾删旁路日志


def test_recorder_zero_byte_output_is_failure(tmp_path):
    out = str(tmp_path / "rec.mp4")
    rec = Recorder([(_fake_exe(tmp_path, _FAKE_EARLY_EXIT) + [out], out)])
    assert rec.start() is None
    err = rec.stop()
    assert err and "退出码 3" in err            # 没产出文件一律判失败


_FAKE_FFMPEG_MULTI = """
import sys
data = sys.stdin.buffer.read(1)      # 等录制控制层写 'q'
if data:
    for p in sys.argv[1:]:           # 视频 + 旁路 WAV 依次落盘
        open(p, "w").write("" if "empty" in p else "ok")
"""


def test_recorder_cleans_zero_byte_audio(tmp_path):
    """分开存放的音轨：正常 WAV 保留，0 字节的（设备抽风）收尾清掉"""
    out = str(tmp_path / "rec.mp4")
    wav_ok = str(tmp_path / "rec_麦克风.wav")
    wav_empty = str(tmp_path / "rec_empty.wav")
    exe = _fake_exe(tmp_path, _FAKE_FFMPEG_MULTI)
    rec = Recorder([(exe + [out], out), (exe + [wav_ok], wav_ok),
                    (exe + [wav_empty], wav_empty)])
    assert rec.start() is None
    assert rec.stop() is None
    assert os.path.getsize(wav_ok) > 0
    assert not os.path.exists(wav_empty)


def test_recorder_start_failure_no_orphan(tmp_path):
    """任一条 spawn 不了就整批不收，不留孤儿 ffmpeg 进程"""
    out = str(tmp_path / "rec.mp4")
    wav = str(tmp_path / "rec_麦克风.wav")
    exe = _fake_exe(tmp_path, _FAKE_FFMPEG)
    rec = Recorder([(exe + [out], out),
                    ([str(tmp_path / "nope" / "ffmpeg.exe"), wav], wav)])
    err = rec.start()
    assert err and "找不到 ffmpeg" in err
    assert not rec.alive()


def test_recorder_stop_without_start():
    rec = Recorder([(["ffmpeg.exe"], "x.mp4")])
    assert rec.stop() == "没有在录制的任务"


def test_recorder_start_missing_exe(tmp_path):
    out = str(tmp_path / "rec.mp4")
    rec = Recorder([([str(tmp_path / "nope" / "ffmpeg.exe"), out], out)])
    err = rec.start()
    assert err and "找不到 ffmpeg" in err       # 路径型 exe 先验存在性，不盲 spawn
