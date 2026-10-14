"""
test_screen_recorder.py —— 屏幕录制功能层单元测试

不真实录屏：命令组装/坐标换算/设备解析用纯断言；Recorder 生命周期用
「读 stdin 一个字节的假脚本」模拟 ffmpeg 的 q 收尾协议。
"""
import os
import sys
import types

import pytest

from video_text_tools.screen_recorder import (
    IS_WINDOWS, Recorder, build_record_command, build_mux_command,
    escape_dshow_name, has_wasapi_loopback, is_mix_device,
    list_audio_devices, list_windows, parse_dshow_devices, rect_to_physical,
    screen_physical, virtual_desktop, _normalize_audio_list,
    _parse_demuxer_check, parse_max_volume, probe_audio_level)


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
    assert cmd[:3] == ["ffmpeg.exe", "-hide_banner", "-y"]
    assert "-thread_queue_size 2048 -f gdigrab" in s
    assert "-i desktop" in s
    assert "-offset_x" not in s          # 不给范围 = gdigrab 默认主屏
    assert "-an" in s and "dshow" not in s
    assert "-c:v libx264 -preset veryfast -tune zerolatency -b:v 4M" in s
    assert "-vf" in s and "fps=30" in s        # 限幅+强制恒定帧率滤镜链
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


def test_build_with_audio_splits_video_and_mic_into_two_procs():
    """画面与麦克风分进程：plans[0] 视频(-an, gdigrab)，plans[1] 麦克风录裸 WAV"""
    plans = build_record_command("ffmpeg.exe", "fullscreen", "a.mp4",
                                 audio=r"Mic : 1=2")
    assert len(plans) == 2
    vcmd, vout = plans[0]
    s = _seg(vcmd)
    assert vout == "a.mp4"
    assert "gdigrab" in s and "-an" in s          # 视频进程绝不带音频输入
    assert "dshow" not in s                        # 音频不挤进视频进程
    assert "-c:v libx264 -preset veryfast -tune zerolatency -b:v 4M" in s
    assert "fps=30" in s and vcmd[-1] == "a.mp4"
    acmd, awav = plans[1]
    a = _seg(acmd)
    assert "-thread_queue_size 2048 -f dshow -i audio=Mic \\: 1\\=2" in a
    assert "-vn" in a and "-c:a pcm_s16le" in a
    assert awav == "a.mp4.audio0.wav" and acmd[-1] == awav


def test_build_audio_str_shorthand_equals_mic_tuple():
    """旧写法（裸设备名字符串）与 ("mic", 名字) 完全同义，向后兼容"""
    a = build_record_command("f", "fullscreen", "a.mp4", audio="Mic X")
    b = build_record_command("f", "fullscreen", "a.mp4", audio=("mic", "Mic X"))
    assert a == b


def test_build_audio_system_wasapi_loopback():
    plans = build_record_command("f", "fullscreen", "a.mp4", audio=("system", None))
    assert len(plans) == 2
    acmd = plans[1][0]
    i = acmd.index("wasapi_loopback")
    assert acmd[i - 3:i - 1] == ["-thread_queue_size", "2048"]  # 实时输入开读队列
    assert acmd[i - 1] == "-f" and acmd[i + 1] == "-i" and acmd[i + 2] == ""
    assert "-c:a pcm_s16le" in _seg(acmd)
    assert plans[1][1] == "a.mp4.audio0.wav"
    assert "-an" in _seg(plans[0][0])               # 视频进程仍是无声独立一条


def test_build_audio_system_dshow_mix_device():
    plans = build_record_command("f", "fullscreen", "a.mp4",
                                 audio=("system", "Stereo Mix (Realtek)"))
    assert len(plans) == 2
    assert "audio=Stereo Mix (Realtek)" in plans[1][0]
    assert "gdigrab" in plans[0][0]                 # 视频进程独立


def test_build_audio_junk_spec_falls_back_to_no_audio():
    for junk in (("mic", None), ("weird", "x"), ()):
        plans = build_record_command("f", "fullscreen", "a.mp4", audio=junk)
        assert len(plans) == 1                   # 没音源：不多余开音轨进程
        assert "-an" in _seg(plans[0][0])
        assert "dshow" not in _seg(plans[0][0]) and "wasapi" not in _seg(plans[0][0])


def test_build_dual_audio_two_wav_procs():
    """内部+外部同时勾：视频 1 条 + 两个音源各 1 条 WAV 进程（共 3 条）"""
    plans = build_record_command("f", "fullscreen", "a.mp4",
                                 audio=[("system", "Stereo Mix"), ("mic", "Mic X")])
    assert len(plans) == 3
    assert "-an" in _seg(plans[0][0])               # 视频不碰音频
    allc = " ".join(_seg(c) for c, _p in plans[1:])
    assert "audio=Stereo Mix" in allc and "audio=Mic X" in allc
    assert plans[1][1] == "a.mp4.audio0.wav"
    assert plans[2][1] == "a.mp4.audio1.wav"


def test_build_mux_dual_audio_amix_into_one_track():
    """合流：两路 WAV 先 amix 混成一条，视频流 copy，产出带声 MP4"""
    cmd = build_mux_command("f", "v.mp4", ["v.a0.wav", "v.a1.wav"], "out.mp4",
                            channel_mode="center")
    s = _seg(cmd)
    assert "-i v.mp4" in s and "-i v.a0.wav" in s and "-i v.a1.wav" in s
    assert "-c:v copy" in s                         # 视频不重编码
    assert "[1:a][2:a]amix=inputs=2" in s and "normalize=0" in s
    assert "-map 0:v:0" in s and "-map [aout]" in s
    assert "-c:a aac" in s
    assert cmd[-1] == "out.mp4"


def test_build_mux_single_audio_and_empty():
    cmd = build_mux_command("f", "v.mp4", ["v.a0.wav"], "out.mp4",
                            channel_mode="center")
    s = _seg(cmd)
    assert "-map 0:v:0 -map 1:a:0 -c:v copy" in s
    assert "pan=stereo|c0=0.5*c0+0.5*c1" in s       # 默认人声居中
    # 无音轨 → 不需要合流
    assert build_mux_command("f", "v.mp4", [], "out.mp4") is None


def test_build_command_uses_nvenc_when_probe_succeeds():
    """探测到 NVENC 可用 → 编码器切到 h264_nvenc p5/hq（画质优先，不吃 CPU）"""
    from video_text_tools.screen_recorder import _HW_ENC_CACHE
    # 直接塞缓存，绕过实际 subprocess 探测（测试环境无 ffmpeg）
    _HW_ENC_CACHE["ffmpeg.exe"] = "h264_nvenc"
    try:
        plans = build_record_command("ffmpeg.exe", "fullscreen", "a.mp4",
                                     fps=30, bitrate="4M")
        s = _seg(plans[0][0])
        assert "-c:v h264_nvenc -preset p5 -tune hq -b:v 4M" in s
        assert "-pix_fmt nv12" in s
        assert "libx264" not in s and "zerolatency" not in s
        # 硬编保留到 ≤1440p（2K 屏不降采样），而非软编的 1080p 限幅
        assert "min(2560,iw)" in s and "min(1440,ih)" in s
    finally:
        _HW_ENC_CACHE.pop("ffmpeg.exe", None)


def test_resolution_source_means_no_downscale():
    """resolution='source'/源屏 → 只取偶不缩放（所见即所得），不受软硬编上限约束"""
    for r in ("source", "源屏", "原生"):
        s = _seg(build_record_command("f", "fullscreen", "a.mp4", resolution=r)[0][0])
        assert "scale=trunc(iw/2)*2:trunc(ih/2)*2" in s
        assert "min(1920,iw)" not in s and "min(2560,iw)" not in s


def test_resolution_preset_and_custom_cap():
    """预设档按上限缩；自定义 宽x高 亦可；认不出的值退回自动口径"""
    s = _seg(build_record_command("f", "fullscreen", "a.mp4", resolution="1080p")[0][0])
    assert "min(1920,iw)" in s and "min(1080,ih)" in s
    s = _seg(build_record_command("f", "fullscreen", "a.mp4", resolution="2560x1440")[0][0])
    assert "min(2560,iw)" in s and "min(1440,ih)" in s
    # 非法值 → 退回自动（无 source 滤镜、也不是自定义上限）
    s = _seg(build_record_command("f", "fullscreen", "a.mp4", resolution="乱填")[0][0])
    assert "scale=" in s and "trunc(iw/2)*2" not in s


def test_build_command_falls_back_to_libx264_when_probe_says_no():
    """探测结果为不可用（缓存 False）→ 回落 libx264、不影响启动"""
    from video_text_tools.screen_recorder import _HW_ENC_CACHE
    _HW_ENC_CACHE["ffmpeg.exe"] = False
    try:
        plans = build_record_command("ffmpeg.exe", "fullscreen", "a.mp4")
        s = _seg(plans[0][0])
        assert "-c:v libx264 -preset veryfast -tune zerolatency" in s
        assert "h264_nvenc" not in s
        # 软编走 1080p 限幅（保流畅），不放到 1440p
        assert "min(1920,iw)" in s and "min(2560,iw)" not in s
    finally:
        _HW_ENC_CACHE.pop("ffmpeg.exe", None)


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


def _fake_mux_run(monkeypatch):
    """把 screen_recorder.subprocess.run 替成『合流成功』的假体：把命令最后一个
    参数（输出文件）写成非空，模拟 ffmpeg 把视频+WAV mux 成新 mp4。"""
    import video_text_tools.screen_recorder as sr

    def _run(cmd, **kw):
        open(cmd[-1], "w").write("muxed")
        return types.SimpleNamespace(returncode=0, stderr=b"", stdout=b"")
    monkeypatch.setattr(sr.subprocess, "run", _run)


def test_recorder_merges_audio_wavs_and_cleans_temps(tmp_path, monkeypatch):
    """分进程产物停止后合流：有效 WAV 并进 mp4 后删除，0 字节 WAV 丢弃"""
    _fake_mux_run(monkeypatch)
    out = str(tmp_path / "rec.mp4")
    wav_ok = str(tmp_path / "rec.audio0.wav")
    wav_empty = str(tmp_path / "rec_empty.audio1.wav")   # 名字含 empty→假体写 0 字节
    exe = _fake_exe(tmp_path, _FAKE_FFMPEG_MULTI)
    rec = Recorder([(exe + [out], out), (exe + [wav_ok], wav_ok),
                    (exe + [wav_empty], wav_empty)])
    assert rec.start() is None
    assert rec.stop() is None
    assert os.path.isfile(out) and os.path.getsize(out) > 0   # 合流后的成片
    assert not os.path.exists(wav_ok)                          # 旁路 WAV 已并入并删
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


# --------------------------------------------------------------------
# 设备检测（试录测峰值电平）
# --------------------------------------------------------------------
def test_parse_max_volume():
    assert parse_max_volume("max_volume: -12.3 dB") == -12.3
    assert parse_max_volume("[x] max_volume: -inf dB") == -200.0
    assert parse_max_volume("nothing here") is None
    assert parse_max_volume("") is None
    assert parse_max_volume(None) is None


def _fake_probe_run(monkeypatch, stderr, holder=None):
    """把 screen_recorder.subprocess.run 替成返回给定 stderr 的假体，
    顺便把拼好的命令存进 holder 便于断言。"""
    import video_text_tools.screen_recorder as sr

    def _run(cmd, **kw):
        if holder is not None:
            holder["cmd"] = cmd
        return types.SimpleNamespace(stderr=stderr, stdout="")
    monkeypatch.setattr(sr.subprocess, "run", _run)


def test_probe_audio_level_signal(monkeypatch):
    got = {}
    _fake_probe_run(monkeypatch,
                    "[Parsed_volumedetect_0 @ 0x1] max_volume: -18.7 dB\n", got)
    db, ok, msg = probe_audio_level("ffmpeg.exe", ("mic", "My Mic"))
    assert db == -18.7 and ok is True and "收到声音" in msg
    cmd = " ".join(got["cmd"])
    # 采集输入与录制同一口径：dshow 指定设备 + volumedetect + null 输出
    assert "-f dshow -i audio=My Mic" in cmd
    assert "-af volumedetect -f null" in cmd and "-t 2" in cmd


def test_probe_audio_level_silence(monkeypatch):
    _fake_probe_run(monkeypatch, "max_volume: -inf dB\n")
    db, ok, msg = probe_audio_level("ffmpeg.exe", ("mic", "Quiet Mic"))
    assert db == -200.0 and ok is False and "几乎没声音" in msg


def test_probe_audio_level_device_busy_hint(monkeypatch):
    _fake_probe_run(monkeypatch,
                    "[dshow @ 0x1] Could not run the "
                    "graph: Audio device in use\n")
    db, ok, msg = probe_audio_level("ffmpeg.exe", ("mic", "Busy"))
    assert db is None and ok is False and "in use" in msg


def test_probe_audio_level_no_ffmpeg_or_spec():
    # 不跑子进程的早退分支
    assert probe_audio_level("", ("mic", "M"))[1] is False
    assert probe_audio_level("ffmpeg.exe", None)[1] is False


def test_probe_system_without_wasapi_short_circuits(monkeypatch):
    """('system', None) 且 ffmpeg 无 WASAPI 环回：直接给引导，不跑子进程"""
    import video_text_tools.screen_recorder as sr
    monkeypatch.setattr(sr, "has_wasapi_loopback", lambda p: False)
    called = {}
    _fake_probe_run(monkeypatch, "", called)
    db, ok, msg = probe_audio_level("ffmpeg.exe", ("system", None))
    assert ok is False and "WASAPI" in msg and "cmd" not in called
