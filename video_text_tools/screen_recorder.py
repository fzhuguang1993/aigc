# video_text_tools/screen_recorder.py
"""屏幕录制功能层（纯逻辑、无 UI、无 Qt 依赖）

原理：Windows GDI 屏幕抓取走 ffmpeg 的 gdigrab 采集器——
  - 全屏/区域：`-f gdigrab -i desktop` 配 `-offset_x/-offset_y/-video_size`
    （物理像素；不传 offset/size 时 gdigrab 只截主屏，多屏要显式给包围盒）；
  - 窗口：`-i title=标题`，gdigrab 周期性跟随该窗口位置，
    本质仍是"抓窗口所在的屏幕区域"——被遮挡/最小化会穿帮（界面层负责提示）。
声音两条路：外部声音（麦克风）走 dshow 采集设备；内部声音（系统播放）
优先 wasapi_loopback（FFmpeg 7+，无需声卡特殊支持），老构建退而求其次
用 dshow 的「立体声混音」类设备（声卡没有就录不了，属尽力而为）。
两种声音可同时勾选：每个音源单独落一个无损 WAV（视频文件不含声轨）。
⚠ 每个产物一个 ffmpeg 子进程（控制层一起启停）：真机实测 ffmpeg 9.0
单命令多输出时，只要视频输出在场，音频在 q 停止时只剩第一个大包
（降负载/单音源都一样），拆进程后各自独立写入互不影响。
停止录制 = 向各子进程的 stdin 写 `q`，让它正常收尾
写完整容器索引（直接杀进程会损坏文件），超时再 terminate 兜底。

非 Windows 平台：本模块可安全导入，抓取相关入口返回空/报"不支持"。
"""
import ctypes
import os
import re
import subprocess
import sys
import time

IS_WINDOWS = sys.platform == "win32"

# 录制不可用时的统一错误文案（GUI 直接展示）
ERR_NOT_WINDOWS = "屏幕录制目前仅支持 Windows"


def _devnull():
    """Windows 下隐藏子进程控制台窗口"""
    return subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


# ====================================================================
# 坐标换算（逻辑像素 -> 物理像素）
# ====================================================================
# screens 统一用「逻辑」元组表示：(x, y, w, h, dpr)，来自
# QScreen.geometry() + devicePixelRatio()。gdigrab 只认物理像素，
# 所以所有喂给它的矩形都要过这里换算。
#
# 混合 DPI 多屏属长尾场景：换算按「矩形左上角所在屏」的 DPR 近似，
# 跨屏拉伸的窗口在副屏缩放率不同时可能偏移几个像素（界面层建议单屏录制）。

def _screen_of(x, y, screens):
    """找包含逻辑点 (x, y) 的屏；都不含就退回第一块（主屏）"""
    for s in screens:
        sx, sy, sw, sh, _d = s
        if sx <= x < sx + sw and sy <= y < sy + sh:
            return s
    return screens[0] if screens else None


def screen_physical(screen):
    """单块屏的物理矩形 (x, y, w, h)"""
    x, y, w, h, dpr = screen
    return (int(round(x * dpr)), int(round(y * dpr)),
            int(round(w * dpr)), int(round(h * dpr)))


def virtual_desktop(screens):
    """所有屏并集的物理包围盒（全屏录「所有屏幕」时的 gdigrab 参数）"""
    if not screens:
        return None
    boxes = [screen_physical(s) for s in screens]
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1 - x0, y1 - y0)


def rect_to_physical(rect, screens):
    """逻辑矩形 (x, y, w, h) -> 物理矩形；按左上角所在屏的 DPR 换算"""
    x, y, w, h = rect
    s = _screen_of(x, y, screens)
    if s is None:
        return (x, y, w, h)
    sx, sy, _sw, _sh, dpr = s
    return (int(round(x * dpr)), int(round(y * dpr)),
            int(round(w * dpr)), int(round(h * dpr)))


# ====================================================================
# 窗口枚举与点选（ctypes，无第三方依赖）
# ====================================================================
_HWND = ctypes.c_void_p        # 64 位下句柄必须按指针宽度传递，防截断


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_WINAPI_OK = None


def _winapi():
    """取 user32/dwmapi 并一次性配好参数类型（懒初始化）；非 Windows 给 None"""
    global _WINAPI_OK
    if not IS_WINDOWS:
        return None
    if _WINAPI_OK is None:
        try:
            user32 = ctypes.windll.user32
            dwmapi = ctypes.windll.dwmapi
        except Exception:
            _WINAPI_OK = False
            return None
        for fn, args in (
                ("IsWindowVisible", [_HWND]),
                ("GetWindowTextLengthW", [_HWND]),
                ("GetWindowTextW", [_HWND, ctypes.c_wchar_p, ctypes.c_int]),
                ("GetWindowRect", [_HWND, ctypes.POINTER(_RECT)]),
                ("GetAncestor", [_HWND, ctypes.c_uint]),
                ("WindowFromPoint", [_POINT]),
                ("GetWindowThreadProcessId", [_HWND,
                                              ctypes.POINTER(ctypes.c_uint)]),
                ("IsIconic", [_HWND]),
                ("GetWindow", [_HWND, ctypes.c_uint]),
                ("GetWindowLongPtrW", [_HWND, ctypes.c_int]),
                ("GetClassNameW", [_HWND, ctypes.c_wchar_p, ctypes.c_int]),
        ):
            getattr(user32, fn).argtypes = args
        for fn in ("GetAncestor", "WindowFromPoint", "GetWindow"):
            getattr(user32, fn).restype = _HWND      # 返回句柄的必须按指针宽
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        dwmapi.DwmGetWindowAttribute.argtypes = [_HWND, ctypes.c_uint,
                                                 ctypes.c_void_p, ctypes.c_int]
        _WINAPI_OK = True
    return (ctypes.windll.user32, ctypes.windll.dwmapi)


def _query_window(hwnd):
    """窗口体检：只放行「任务栏里有按钮」的应用窗口（含最小化到任务栏的），
    其余（显卡/输入法之类的后台常驻、有主窗体的子面板、工具窗、
    被 DWM 藏起、无标题、展开时尺寸<64、桌面本身）一律剔除。
    通过给 (标题, 物理矩形, 是否最小化)，否则 None。

    最小化窗的矩形是系统摆在屏幕外的占位（约 158×26），没有意义，
    照原样带回由调用方忽略；gdigrab 抓不了最小化窗，选为录制目标时
    要先 restore_window 还原。"""
    u = _winapi()
    if u is None:
        return None
    user32, dwmapi = u
    try:
        if not user32.IsWindowVisible(hwnd):
            return None
        minimized = bool(user32.IsIconic(hwnd))
        # 被 DWM 藏起的（UWP 挂起/虚拟桌面切走的）窗口抓不到有效内容
        cloaked = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(hwnd, 14,           # DWMWA_CLOAKED
                                     ctypes.byref(cloaked),
                                     ctypes.sizeof(cloaked))
        if cloaked.value:
            return None
        # 任务栏按钮规则（Shell 同源）：显式 APPWINDOW 强制入选；否则
        # 排除工具窗（TOOLWINDOW）和一切有 owner 的窗（后台常驻面板大都有 owner）
        ex = user32.GetWindowLongPtrW(hwnd, -20)         # GWL_EXSTYLE
        if not (ex & 0x40000):                           # WS_EX_APPWINDOW
            if ex & 0x80 or user32.GetWindow(hwnd, 4):   # TOOLWINDOW / GW_OWNER
                return None
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value in ("Progman", "WorkerW"):          # 桌面本身不是应用窗
            return None
        n = user32.GetWindowTextLengthW(hwnd)
        if not n:
            return None
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title:
            return None
        r = _RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if minimized:      # 最小化窗的屏幕外占位矩形不参与尺寸判定
            return title, (r.left, r.top, w, h), True
        if w < 64 or h < 64:      # 过滤工具条/透明小部件
            return None
        return title, (r.left, r.top, w, h), False
    except Exception:
        return None


def _is_self_process(user32, hwnd):
    pid = ctypes.c_uint(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == ctypes.windll.kernel32.GetCurrentProcessId()


def list_windows(exclude_self=False):
    """任务栏里能看到的应用窗口（含最小化到任务栏的）：
    [(hwnd, 标题, 物理矩形, 是否最小化)]，按标题排序。非 Windows 给 []。
    后台常驻窗（显卡控制面板之类）、工具窗、有主窗体的面板不在范围内。

    exclude_self=True 时剔除本进程的窗口（窗口点选遮罩用——自家面板
    永远不该被选为点选目标）；下拉列表不剔除，用户可能就想录主窗口。
    """
    u = _winapi()
    if u is None:
        return []
    user32, _dwmapi = u
    out = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, _HWND, ctypes.c_void_p)

    def _cb(hwnd, _lparam):
        if exclude_self and _is_self_process(user32, hwnd):
            return True
        info = _query_window(hwnd)
        if info:
            out.append((hwnd, info[0], info[1], info[2]))
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(_cb), 0)
    except Exception:
        return []
    out.sort(key=lambda t: t[1].lower())
    return out


def get_window_rect(hwnd):
    """窗口当前物理矩形 (x, y, w, h)；窗口没了/不可见给 None（目标指示框跟随用）。"""
    u = _winapi()
    if u is None:
        return None
    user32, _dwmapi = u
    try:
        if not user32.IsWindowVisible(hwnd):
            return None
        r = _RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w <= 0 or h <= 0:
            return None
        return (r.left, r.top, w, h)
    except Exception:
        return None


def window_at_point(px, py):
    """点选：物理坐标 (px, py) 处的根窗口 (hwnd, 标题, 物理矩形)，没有给 None。

    自动跳过本进程的窗口（自己的面板/遮罩永远不会被选中）。"""
    u = _winapi()
    if u is None:
        return None
    user32, _dwmapi = u
    try:
        hwnd = user32.WindowFromPoint(_POINT(px, py))
        if not hwnd:
            return None
        hwnd = user32.GetAncestor(hwnd, 2)       # GA_ROOT：从子控件升到根窗口
        if not hwnd:
            return None
        if _is_self_process(user32, hwnd):
            return None                          # 排除自家窗口
        info = _query_window(hwnd)
        if not info or info[2]:                  # 最小化窗不在屏上，点不到
            return None
        return (hwnd, info[0], info[1])
    except Exception:
        return None


def restore_window(hwnd):
    """把最小化到任务栏的窗口还原并带到前台（gdigrab 抓不了最小化窗，
    选为录制目标时开录前要先还原）。句柄失效/非 Windows 静默略过。"""
    u = _winapi()
    if u is None:
        return
    user32, _dwmapi = u
    try:
        if not user32.IsWindow(hwnd):
            return
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)               # SW_RESTORE
        user32.BringWindowToTop(hwnd)
    except Exception:
        pass


# ====================================================================
# dshow 音频设备枚举
# ====================================================================
# 设备行真实形如 `[dshow @ 00007f]     "名字"`：前缀不定，直接抓第一对引号
_DEV_LINE_RE = re.compile(r'"([^"]+)"')
# ffmpeg 9+ 新格式：无区段标题，行尾用类型标记 `"名字" (audio)`
_DEV_AUDIO_RE = re.compile(r'"([^"]+)"\s*\(audio\)')


def parse_dshow_devices(text):
    """从 `ffmpeg -list_devices -f dshow` 的输出里抠出音频设备名列表。

    新旧两种真实格式都要兼容（都走 stderr）：
      · 旧版按区段分组：
            Direct audio capture devices
                "Microphone (Realtek Audio)"
                Alternative name "@device_cm_{...}"
            Direct video capture devices
      · ffmpeg 9+（真机 9.0.2 实测）没有区段标题，改行尾类型标记：
            [in#0 @ ...] "Analogue 1 + 2 (Focusrite USB Audio)" (audio)
            [in#0 @ ...]   Alternative name "@device_cm_{...}"
    解析不出任何设备返回 []（面板会降级为「不录音」，不算错误）。
    """
    out = []
    in_audio = False
    for line in (text or "").splitlines():
        low = line.lower()
        m = _DEV_AUDIO_RE.search(line)
        if m:                              # 新格式：行尾 (audio) 直接定性
            out.append(m.group(1))
            continue
        if "direct audio capture devices" in low:
            in_audio = True
            continue
        if "direct video capture devices" in low:
            in_audio = False
            continue
        if not in_audio or "alternative name" in low:
            continue
        m2 = _DEV_LINE_RE.search(line)
        if m2:
            out.append(m2.group(1))
    return out


def list_audio_devices(ffmpeg_path):
    """枚举可录的 dshow 音频输入设备；ffmpeg 缺失/超时一律给 []"""
    if not ffmpeg_path:
        return []
    try:
        r = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-list_devices", "true",
             "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=_devnull())
        # 设备列表在 stderr；个别构建会打到 stdout，两边都喂给解析器
        return parse_dshow_devices((r.stderr or "") + "\n" + (r.stdout or ""))
    except Exception:
        return []


def escape_dshow_name(name):
    """dshow 设备名里的选项分隔符转义（`\\` 最先，防二次转义）"""
    return (name or "").replace("\\", "\\\\").replace(":", r"\:").replace("=", r"\=")


def is_mix_device(name):
    """启发式：dshow 设备是否为「立体声混音」一类系统声音设备（内部声音兑底用）"""
    low = (name or "").lower()
    return any(k in low for k in
               ("mix", "loopback", "what you hear", "what u hear", "混音"))


def _parse_demuxer_check(text):
    """`-h demuxer=wasapi_loopback` 的输出是否表示「支持」。

    ⚠ 不支持时真机 ffmpeg 9.0 实测报的是 "Unknown format '...'"（不是
    Unknown demuxer），两种措辞都要拦；支持时帮助正文会列出 demuxer 名字。
    """
    low = (text or "").lower()
    if not low.strip() or "unknown" in low:
        return False
    return "wasapi_loopback" in low


def has_wasapi_loopback(ffmpeg_path):
    """该 ffmpeg 是否支持 wasapi_loopback 解复用器（录系统内部声音）。

    注：主流发行版（含 gyan 9.0 essentials）基本都没编译这个解复用器，
    探测结果常态是 False，面板会自动退到「立体声混音」类 dshow 设备。
    """
    if not ffmpeg_path:
        return False
    try:
        r = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-h", "demuxer=wasapi_loopback"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, creationflags=_devnull())
        return _parse_demuxer_check((r.stdout or "") + (r.stderr or ""))
    except Exception:
        return False


# ====================================================================
# 录制命令组装
# ====================================================================
def _normalize_audio(audio):
    """音频规格归一化：str 或 (kind, name) -> ('mic'|'system', name|None)，无声给 None"""
    if not audio:
        return None
    if isinstance(audio, str):
        return ("mic", audio)
    kind, name = audio
    if kind == "system" and not name:
        return ("system", None)              # wasapi_loopback 默认输出设备
    if kind in ("mic", "system") and name:
        return (kind, name)                  # 都走 dshow 指定设备
    return None


def _normalize_audio_list(audio):
    """音频规格列表：收 None / 单规格 / 规格列表，按音源类型去重

    同一类型（mic/system）只保留第一个：WAV 产物按类型命名，
    两个同类型源会撞同一个文件名。"""
    if not audio:
        return []
    specs = audio if isinstance(audio, list) else [audio]
    out, seen = [], set()
    for s in specs:
        try:
            n = _normalize_audio(s)
        except (TypeError, ValueError):
            n = None
        if n and n[0] not in seen:
            seen.add(n[0])
            out.append(n)
    return out


# 音源类型 -> WAV 文件名后缀（分开存放，剪辑时一眼认出哪轨是什么）
AUDIO_SUFFIX = {"system": "内部声音", "mic": "麦克风"}


def audio_out_paths(out_path, audio):
    """每个音源的独立 WAV 落盘路径：[(规格, 路径)]，与视频同目录同基名

    build_record_command 内部用同一套命名规则，两边不会漂移。"""
    base, _ = os.path.splitext(out_path)
    return [(spec, f"{base}_{AUDIO_SUFFIX[spec[0]]}.wav")
            for spec in _normalize_audio_list(audio)]


def build_record_command(ffmpeg_path, mode, out_path, *,
                         fps=30, bitrate="4M",
                         physical=None, title=None,
                         audio=None, audio_bitrate="128k"):
    """拼 ffmpeg 录屏命令，返回 [(命令, 产物路径)]：第一个是主视频，
    其后每个音源一条独立 WAV 命令。

    为什么拆多条命令（一个产物一个子进程）：真机实测 ffmpeg 9.0，
    单命令多输出时只要视频输出在场，音频在 q 停止时会丢尾巴——
    WAV 只剩第一个大包（1.0s/0.5s），降低视频负载也救不回来。

    mode: "fullscreen" | "region" | "window"
      - fullscreen：physical=None 录主屏；给物理包围盒则录该范围（多屏并集
        或指定某块屏——gdigrab 不给 size 时只截主屏，这是它的默认行为）
      - region：physical 必填（屏幕选择/框选结果换算成的物理矩形）
      - window：title 必填（gdigrab 只有子串匹配，标题取整窗口全标题）
    audio：None/空 = 不录音；可传单个规格或规格列表（内部+外部多选）。
      str = dshow 设备名（兼容写法，按麦克风）；("mic", 名字) = dshow
      外部声音；("system", None) = wasapi_loopback 内部声音（需构建内含
      该解复用器，探测不过时面板不会给出这一选项）；("system", 名字) =
      立体声混音类 dshow 设备录内部声音。
      ⚠ 音轨不再混进 MP4（视频一律 -an）：每源一个 WAV（pcm_s16le/
      48k/双声道），命名见 audio_out_paths。
    audio_bitrate：保留签名兼容旧调用，分开存 WAV 后不再使用。
    """
    if mode not in ("fullscreen", "region", "window"):
        raise ValueError(f"未知录制模式: {mode}")
    if mode == "window" and not (title or "").strip():
        raise ValueError("窗口录制缺少标题")
    if mode == "region" and not physical:
        raise ValueError("区域录制缺少范围")
    aspecs = _normalize_audio_list(audio)

    cmd = [ffmpeg_path, "-hide_banner", "-y",
           "-f", "gdigrab", "-framerate", str(int(fps))]
    if mode == "window":
        cmd += ["-i", f"title={title}"]
    else:
        # ⚠ gdigrab 的设备选项必须排在 -i 之前：放在后面 ffmpeg 不报错
        # （退出码 0）但会静默忽略，捕获直接变成整个虚拟桌面
        if physical:
            x, y, w, h = physical
            # 奇数宽高会让 yuv420p 报错，统一向下取偶（gdigrab 也要求偶数）
            cmd += ["-offset_x", str(int(x)), "-offset_y", str(int(y)),
                    "-video_size", f"{int(w) & ~1}x{int(h) & ~1}"]
        cmd += ["-i", "desktop"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-b:v", str(bitrate),
            "-pix_fmt", "yuv420p", "-an",
            "-movflags", "+faststart", out_path]
    plans = [(cmd, out_path)]
    # 每个音源一条独立进程：dshow/wasapi 设备不同，可同时采集互不抢占
    for spec, wav in audio_out_paths(out_path, aspecs):
        acmd = [ffmpeg_path, "-hide_banner", "-y"]
        if spec == ("system", None):
            acmd += ["-f", "wasapi_loopback", "-i", ""]
        else:
            acmd += ["-f", "dshow", "-i", f"audio={escape_dshow_name(spec[1])}"]
        acmd += ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", wav]
        plans.append((acmd, wav))
    return plans


# ====================================================================
# 录制进程控制
# ====================================================================
class Recorder:
    """一组录屏子进程（1 视频 + N 音轨）的生命周期：start / stop / 轮询 / 判定。

    收 build_record_command 的 plans=[(命令, 产物路径)]，第一个是主视频；
    成败只看主产物，旁路 WAV 全 0 字节/缺失都不判失败（设备抽风时
    至少保住画面）。stderr 每进程重定向到各自旁路日志（管道塞满会
    反压 ffmpeg 卡死录制）：成功收尾后删，出错时读尾部作诊断信息。
    """

    def __init__(self, plans, out_path=None):
        self.plans = [(list(c), p) for c, p in plans]
        if not self.plans:
            raise ValueError("Recorder 需要至少一条 (命令, 产物) 计划")
        self.out_path = out_path if out_path is not None else self.plans[0][1]
        self.audio_paths = [p for _c, p in self.plans[1:]]
        self.err_paths = [p + ".err.log" for _c, p in self.plans]
        self.err_path = self.err_paths[0]        # 主产物日志（测试/诊断入口）
        self._procs = []                          # [(Popen, err_path)]
        self._started_at = 0.0

    def start(self):
        """启动全部子进程（视频在前，音轨紧随）。None=成功，str=错误原因。

        某一条 spawn 失败就把已起的全部收掉，不留孤儿进程。"""
        if not IS_WINDOWS:
            return ERR_NOT_WINDOWS
        if self.alive():
            return "录制已在进行中"
        procs = []
        try:
            for (cmd, _p), errp in zip(self.plans, self.err_paths):
                exe = cmd[0]
                # 裸命令名（交给 PATH 解析）之外的情形，先验文件真实存在
                if (os.sep in exe or os.altsep in exe or exe.endswith(".exe")) \
                        and not os.path.isfile(exe):
                    raise FileNotFoundError(f"找不到 ffmpeg 可执行文件：{exe}")
                errf = open(errp, "wb")
                try:
                    proc = subprocess.Popen(
                        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=errf, creationflags=_devnull())
                finally:
                    errf.close()      # 子进程已持有句柄，父进程这边关掉
                procs.append((proc, errp))
        except OSError as e:
            for proc, _e in procs:
                try:
                    proc.terminate()
                except OSError:
                    pass
            self._clean_logs([p for _c, p in procs])
            return f"启动 ffmpeg 失败：{e}"
        self._procs = procs
        self._started_at = time.time()
        return None

    def alive(self):
        """任一子进程还活着算录制中（音轨先退不中断录制，视频退了就停）"""
        return any(p.poll() is None for p, _e in self._procs)

    def elapsed(self):
        """已录秒数（供界面计时；未启动/已停止也有值，用于回显）"""
        return max(0.0, time.time() - self._started_at) if self._started_at else 0.0

    def _tail_err(self, path, limit=600):
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - limit))
                return f.read().decode("utf-8", "replace").strip()
        except OSError:
            return ""

    @staticmethod
    def _clean_logs(paths):
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass

    def stop(self, timeout=10):
        """停止全部子进程。返回 None=成功，str=失败原因（带 ffmpeg 报错尾部）。

        逐进程写 `q` 让它们正常收尾（补全容器索引），超时才 terminate；
        主产物为 0 字节一律判失败；旁路 WAV 为 0 字节的清掉（设备抽风
        时别留空文件让用户以为录到了声音）。"""
        if not self._procs:
            return "没有在录制的任务"
        procs, self._procs = self._procs, []
        for proc, _e in procs:
            try:
                if proc.poll() is None:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                    proc.stdin.close()
            except OSError:
                pass                   # 进程可能刚好自己退了
        for proc, _e in procs:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        code = procs[0][0].returncode
        size = os.path.getsize(self.out_path) \
            if os.path.isfile(self.out_path) else 0
        if size == 0:
            tail = self._tail_err(self.err_paths[0])
            return (f"录制失败（ffmpeg 退出码 {code}）"
                    + (f"\n{tail}" if tail else ""))
        for p in self.audio_paths:
            try:
                if os.path.isfile(p) and os.path.getsize(p) == 0:
                    os.remove(p)
            except OSError:
                pass
        self._clean_logs(self.err_paths)
        return None
