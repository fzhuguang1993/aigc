# video_text_tools/software_manager.py
"""本机软件检测 / 卸载 / 残留清理（纯功能层，无 Qt 依赖，回调式）

设计要点
--------
- 只依赖标准库 winreg / subprocess / ctypes / shutil，不引第三方。
- winreg 一律函数内延迟导入：既让非 Windows 环境导入本模块不炸，也方便单测
  用假对象顶掉 sys.modules["winreg"]（对齐 tests/test_tray.py 的做法），
  单测绝不碰真机注册表。
- 卸载是高危不可逆动作：本层只负责「取数 / 拼命令 / 执行 / 扫描候选」，
  是否删除、删哪些一律交给上层 UI 逐条确认；残留扫描只产候选、不自动删。
- 文件删除优先走回收站（SHFileOperationW + FOF_ALLOWUNDO，可恢复），
  失败才降级永久删除；注册表删除无回收站，只能靠「先预览再确认」兜底。
"""
import os
import re
import sys
import shutil
import subprocess
from pathlib import Path

#: 三处卸载登记根键（分别带 64/32 视图标志，来源标签供 UI 展示）
_UNINSTALL_SUBKEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

#: 明确是系统更新 / 组件的 DisplayName 前缀（连同 SystemComponent 一起剔除）
_UPDATE_PREFIXES = (
    "update for", "security update", "hotfix for",
    "definition update for", "driver update",
)

#: 驱动类关键词：name / publisher 命中即归「驱动程序」组（小写匹配）
_DRIVER_KWS = (
    "driver", "驱动", "graphics", "radeon", "geforce", "nvidia", "realtek",
    "chipset", "intel(r)", "wlan", "bluetooth", "audio console", "hi-inh",
    "processor graphics",
)

#: Windows 系统组件 / 自带应用关键词：命中即归「系统组件」组（小写匹配）
_SYSTEM_KWS = (
    "windows", "microsoft edge", "edge ", "visual c++", ".net",
    "net framework", "net core", "webview2", "directx", "media feature pack",
    "language experience", "power query", "universal crt", "microsoft store",
)


def supported():
    """软件卸载清理只在 Windows 上有意义。"""
    return sys.platform.startswith("win")


# ====================================================================
# 1. 枚举已安装软件
# ====================================================================
def _roots():
    """返回 [(winreg常量, 相对子键, 视图标志, 来源标签)]，供 list_installed 遍历。

    视图标志 KEY_WOW64_64KEY / KEY_WOW64_32KEY 显式指定，避免 32 位进程在 64 位
    系统上被重定向、漏读另一视图的软件。HKCU 无 WOW64 之分，标志传 0。"""
    import winreg
    return [
        (winreg.HKEY_LOCAL_MACHINE, _UNINSTALL_SUBKEY, winreg.KEY_WOW64_64KEY, "HKLM-64"),
        (winreg.HKEY_LOCAL_MACHINE, _UNINSTALL_SUBKEY, winreg.KEY_WOW64_32KEY, "HKLM-32"),
        (winreg.HKEY_CURRENT_USER, _UNINSTALL_SUBKEY, 0, "HKCU"),
    ]


def _read_values(key):
    """把打开的键的所有值读进 dict（EnumValue 逐个取，取不动的跳过）。"""
    import winreg
    out = {}
    i = 0
    while True:
        try:
            name, data, _typ = winreg.EnumValue(key, i)
        except OSError:
            break
        i += 1
        if name:
            out[name] = data
    return out


def _enum_subkeys(key):
    """列出键下所有子键名。"""
    import winreg
    names, i = [], 0
    while True:
        try:
            names.append(winreg.EnumKey(key, i))
        except OSError:
            break
        i += 1
    return names


def _should_skip(vals):
    """判定一条登记项是否应被剔除（系统组件 / 更新补丁 / 无名 / 无卸载命令）。"""
    name = (vals.get("DisplayName") or "").strip()
    if not name:
        return True
    if str(vals.get("SystemComponent", "")).strip() == "1":
        return True
    # Windows Update / 驱动补丁：带 ParentKeyName 或 ReleaseType，且没有常规卸载串
    if vals.get("ParentKeyName") or vals.get("ReleaseType"):
        return True
    low = name.lower()
    if any(low.startswith(p) for p in _UPDATE_PREFIXES):
        return True
    if not (vals.get("UninstallString") or vals.get("QuietUninstallString")):
        return True
    return False


def _app_dir():
    """本程序所在目录：打包态取 exe 目录，开发态取仓库根，用于自保护判定。"""
    if getattr(sys, "frozen", False):
        return os.path.normcase(os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.normcase(str(Path(__file__).resolve().parents[1]))


def _is_protected(vals):
    """命中本程序自身目录的项禁止卸载（防止把软件助手自己卸掉）。"""
    adir = _app_dir()
    for field in ("InstallLocation", "UninstallString"):
        v = (vals.get(field) or "").strip()
        if v and os.path.normcase(v).replace('"', "").startswith(adir):
            return True
    return False


def classify(record):
    """把一条记录归组："driver"（驱动）/ "system"（Windows 系统组件）/ "app"（常用软件）。

    只看名称 / 发布者里的关键词，先判驱动再判系统，都不命中归 app。保守口径：
    不按发布者一刀切，故 Visual Studio Code（发布者 Microsoft）这类正常第三方软件仍在主组。"""
    hay = ((record.get("name") or "") + " " + (record.get("publisher") or "")).lower()
    if any(k in hay for k in _DRIVER_KWS):
        return "driver"
    if any(k in hay for k in _SYSTEM_KWS):
        return "system"
    return "app"


def list_installed():
    """枚举本机可卸载软件，返回记录 list（按名称排序）。

    每条记录（dict）：
      name / version / publisher / install_location / uninstall_string /
      quiet_uninstall_string / install_date(YYYYMMDD) / estimated_size(KB) /
      is_msi / product_code(键名，MSI 时为 GUID) / source(HKLM-64/HKLM-32/HKCU) /
      hive / key_path(相对根的完整子键路径，供清理) / protected(bool)
    """
    import winreg
    records, seen = [], set()
    for root_const, base, flag, source in _roots():
        access = winreg.KEY_READ | flag
        try:
            rootkey = winreg.OpenKey(root_const, base, 0, access)
        except OSError:
            continue
        with rootkey:
            for sub in _enum_subkeys(rootkey):
                path = base + "\\" + sub
                try:
                    k = winreg.OpenKey(root_const, path, 0, access)
                except OSError:
                    continue
                with k:
                    vals = _read_values(k)
                if _should_skip(vals):
                    continue
                name = (vals.get("DisplayName") or "").strip()
                # 同一软件在两个视图都登记时（罕见）按 (名+版本+来源根) 去重
                dedup = (name.lower(), (vals.get("DisplayVersion") or "").strip(),
                         source[:4])
                if dedup in seen:
                    continue
                seen.add(dedup)
                is_msi = str(vals.get("WindowsInstaller", "")) == "1" or bool(
                    re.match(r"^\{[0-9A-Fa-f-]+\}$", sub))
                est = vals.get("EstimatedSize")
                rec = {
                    "name": name,
                    "version": (vals.get("DisplayVersion") or "").strip(),
                    "publisher": (vals.get("Publisher") or "").strip(),
                    "install_location": (vals.get("InstallLocation") or "").strip().strip('"'),
                    "uninstall_string": (vals.get("UninstallString") or "").strip(),
                    "quiet_uninstall_string": (vals.get("QuietUninstallString") or "").strip(),
                    "display_icon": (vals.get("DisplayIcon") or "").strip(),
                    "install_date": str(vals.get("InstallDate") or "").strip(),
                    "estimated_size": int(est) if isinstance(est, int) else 0,
                    "is_msi": is_msi,
                    "product_code": sub if is_msi else "",
                    "source": source,
                    "hive": root_const,
                    "key_path": path,
                    "protected": _is_protected(vals),
                }
                rec["category"] = classify(rec)
                records.append(rec)
    records.sort(key=lambda r: r["name"].lower())
    return records


# ====================================================================
# 2. 组装卸载命令
# ====================================================================
def build_uninstall_cmd(record, mode="gui"):
    """按模式拼卸载命令，返回 dict {exe, args, needs_gui, msg}。

    mode="gui"：原样用 UninstallString（官方卸载向导，交给用户点）；MSI 规整为
      msiexec /x {ProductCode}。
    mode="silent"：优先 QuietUninstallString；MSI 构造 /qn /norestart；exe 没有
      静默串时 needs_gui=True（不瞎猜 /S，让上层提示改用官方卸载器）。
    """
    name = record.get("name", "")
    uninstall = record.get("uninstall_string", "")
    quiet = record.get("quiet_uninstall_string", "")

    if record.get("is_msi") and record.get("product_code"):
        code = record["product_code"]
        if mode == "silent":
            return {"exe": "msiexec.exe", "args": ["/x", code, "/qn", "/norestart"],
                    "needs_gui": False, "msg": ""}
        return {"exe": "msiexec.exe", "args": ["/x", code],
                "needs_gui": False, "msg": ""}

    if mode == "silent":
        # 无 QuietUninstallString 时，按卸载器文件名推断静默参数（Inno/NSIS）
        src = quiet or uninstall
        exe, args = parse_uninstall_string(src)
        if not exe:
            return {"exe": "", "args": [], "needs_gui": True,
                    "msg": (f"「{name}」没有卸载命令，无法卸载。" if not uninstall
                            else f"「{name}」的卸载命令无法解析：{src}")}
        if not quiet:
            extra = _infer_silent_args(exe)
            if extra is None:
                return {"exe": "", "args": [], "needs_gui": True,
                        "msg": f"「{name}」没有静默卸载命令，建议改用「官方卸载器」手动完成。"}
            args = args + extra
        return {"exe": exe, "args": args, "needs_gui": False, "msg": ""}

    # gui 模式：原样用 UninstallString
    exe, args = parse_uninstall_string(uninstall)
    if not exe:
        return {"exe": "", "args": [], "needs_gui": True,
                "msg": f"「{name}」的卸载命令无法解析：{uninstall}"}
    return {"exe": exe, "args": args, "needs_gui": False, "msg": ""}


def _infer_silent_args(exe):
    """无静默串时按卸载器文件名推断静默参数；推不出返回 None。

    只认两类高置信特征（宁可不猜，也不拿错参数把卸载器跑挂）：
    Inno Setup 的 unins*.exe → /VERYSILENT；NSIS 默认名 uninstall.exe/uninst.exe → /S。"""
    base = os.path.basename(exe or "").lower()
    if base in ("uninstall.exe", "uninst.exe"):   # NSIS 默认名（须先判，否则 uninstall.exe 会被下面的 unins 前缀抢走）
        return ["/S"]
    if base.startswith("unins"):            # Inno Setup：unins000.exe / uninsBA…
        return ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"]
    return None


def silent_capable(record):
    """该记录能否静默卸载（不弹窗）：MSI / 有静默串 / 能推断出静默参数。"""
    return build_uninstall_cmd(record, "silent")["needs_gui"] is False


def parse_uninstall_string(s):
    """把 UninstallString 拆成 (exe, [args...])。

    UninstallString 常见两态：① "C:\\...\\unins000.exe" /SILENT（引号包 exe）；
    ② C:\\Program Files\\Foo Bar\\uninstall.exe /x（exe 带空格却不加引号）。

    ⚠ 第②态绝不能靠“逐段累加到磁盘存在就停”来定位 exe：`C:\\Program Files\\A B\\u.exe`
    里的中间目录 `C:\\Program Files\\A` 也可能真实存在，于是把目录当成了 exe——点开就
    是“弹出一个文件夹”而不是卸载向导。这里改成按可执行扩展名（.exe/.msi/.com/.bat/.cmd）
    的边界切分：第一个扩展名之前都是 exe 路径，之后是参数。解析不出返回 ("", [])。"""
    s = (s or "").strip()
    if not s:
        return "", []
    if s[0] == '"':
        end = s.find('"', 1)
        if end == -1:
            return s[1:].strip(), []
        exe = s[1:end].strip()
        rest = s[end + 1:].strip()
        return exe, _split_args(rest)
    # 未加引号：按可执行扩展名边界切（路径含空格也不怕）
    cut = _exe_ext_cut(s)
    if cut > 0:
        return s[:cut].strip(), _split_args(s[cut:].strip())
    # 没有可执行扩展名（如 msiexec /x {GUID}）：第一个 token 当命令名
    parts = s.split()
    return parts[0], _split_args(" ".join(parts[1:]))


def _split_args(rest):
    """把参数串按空格切、保留引号段（卸载参数一般无嵌套引号，够用）。"""
    rest = (rest or "").strip()
    if not rest:
        return []
    out, buf, q = [], "", False
    for ch in rest:
        if ch == '"':
            q = not q
        elif ch.isspace() and not q:
            if buf:
                out.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


# ====================================================================
# 3. 执行卸载
# ====================================================================
def is_admin():
    """当前进程是否已具备管理员权限。"""
    if not supported():
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _needs_elevation(record):
    """机器级（HKLM）软件且当前非管理员 -> 需要提权。"""
    import winreg
    return (record.get("hive") == winreg.HKEY_LOCAL_MACHINE) and not is_admin()


def _shell_execute_wait(exe, args, runas, timeout=1800):
    """ShellExecuteExW 启动并等退出，返回退出码（拿不到返回 None）。

    runas=True 触发 UAC 提权；这是 exe 卸载器 / msiexec 机器级卸载的正规姿势
    （子进程会脱离父进程，subprocess 等待拿不到真实结果，故统一走 ShellExecute）。"""
    import ctypes
    from ctypes import wintypes
    SEE_MASK_NOCLOSEPROCESS = 0x00000008
    SW_SHOWNORMAL = 1

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas" if runas else "open"
    sei.lpFile = exe
    sei.lpParameters = " ".join(args)
    sei.nShow = SW_SHOWNORMAL
    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
    if not ok:
        err = ctypes.GetLastError()
        if err == 1223:      # ERROR_CANCELLED：用户在 UAC 里点了“否”
            return None, "用户取消了权限请求（UAC）"
        return None, f"ShellExecuteExW 启动失败（错误码 {err}）"
    code = None
    if sei.hProcess:
        WAIT_OBJECT_0, WAIT_TIMEOUT = 0x00000000, 0x00000102
        kernel32 = ctypes.windll.kernel32
        r = kernel32.WaitForSingleObject(sei.hProcess, int(timeout * 1000))
        if r == WAIT_OBJECT_0:
            ec = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(ec)):
                code = ec.value
        kernel32.CloseHandle(sei.hProcess)
    return code, ""


def run_uninstall(record, mode="gui", log=None):
    """执行卸载，返回 {ok, code, msg}。UI 负责确认与提权提示，这里只管做。"""
    log = log or (lambda m: None)
    if record.get("protected"):
        return {"ok": False, "code": None, "msg": "本程序自身受保护，禁止卸载。"}
    cmd = build_uninstall_cmd(record, mode)
    if cmd["needs_gui"]:
        return {"ok": False, "code": None, "msg": cmd["msg"]}
    if not cmd["exe"]:
        return {"ok": False, "code": None, "msg": cmd.get("msg") or "无有效卸载命令。"}

    if not _launchable(cmd["exe"]):
        return {"ok": False, "code": None,
                "msg": f"卸载器路径无效（可能是目录或已丢失）：{cmd['exe']}"}

    runas = _needs_elevation(record)
    log(f"  ▶ 启动卸载器：{cmd['exe']} {' '.join(cmd['args'])}".rstrip()
        + ("（需管理员权限）" if runas else ""))
    code, err = _shell_execute_wait(cmd["exe"], cmd["args"], runas)
    if err:
        return {"ok": False, "code": None, "msg": err}
    # msiexec 3010 = 成功但需重启；0 = 完全成功
    if code in (0, 3010):
        msg = "卸载完成" + ("（建议重启电脑）" if code == 3010 else "")
        return {"ok": True, "code": code, "msg": msg}
    if code is None:
        return {"ok": True, "code": None,
                "msg": "卸载器已启动（未能确认退出码，请留意是否弹出重启提示）。"}
    return {"ok": False, "code": code,
            "msg": f"卸载器返回码 {code}（非 0：可能未完成或被取消）。"}


# ====================================================================
# 4. 残留扫描（只产候选，绝不自动删）
# ====================================================================
def _name_tokens(record):
    """从名称 / 发布者生成用于匹配目录 / 注册表键名的候选 token（小写去符号）。"""
    raws = []
    for field in ("name", "publisher"):
        v = (record.get(field) or "").strip()
        if v:
            raws.append(v)
    toks = set()
    for r in raws:
        norm = re.sub(r"[^\w]+", "", r).lower()
        if len(norm) >= 3:
            toks.add(norm)
    return toks


def _dir_size_kb(path):
    """粗估目录大小（KB），只读不改；无权限的条目静默跳过。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total // 1024


def _env_dirs():
    """文件残留候选的公共父目录（存在才纳入）。"""
    cands = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData",
                "LOCALAPPDATA", "APPDATA"):
        d = os.environ.get(var)
        if d and os.path.isdir(d):
            cands.append(d)
    return cands


def _match_token(dirname, toks):
    """目录名是否与某个 token 匹配：规范化后相等或以 token 开头（保守，不含模糊包含）。"""
    norm = re.sub(r"[^\w]+", "", dirname).lower()
    if not norm:
        return False
    for t in toks:
        if norm == t or norm.startswith(t):
            return True
    return False


def _reg_key_exists(root, path):
    import winreg
    try:
        k = winreg.OpenKey(root, path, 0, winreg.KEY_READ)
        k.Close()
        return True
    except OSError:
        return False


def scan_residue(record):
    """卸载后扫描候选残留，返回 ResidueItem 列表（每项 dict，含 exists）。

    文件/目录：InstallLocation + 各公共父目录下与名称/发布者同名的一级目录。
    注册表：Uninstall 下的原键（仍在=陈旧项）+ Software\\<token> 同名键。
    只列“确实存在”的候选，绝不自动删除。"""
    import winreg
    items = []
    toks = _name_tokens(record)

    # ---- 文件 / 目录 ----
    loc = record.get("install_location", "")
    if loc and os.path.isdir(loc):
        items.append(_file_item("dir", loc, "high"))   # 软件自身安装目录 = 高置信
    seen_dirs = {os.path.normcase(loc)} if loc else set()
    for base in _env_dirs():
        try:
            subs = os.listdir(base)
        except OSError:
            continue
        for sub in subs:
            full = os.path.join(base, sub)
            if not os.path.isdir(full):
                continue
            if not _match_token(sub, toks):
                continue
            nk = os.path.normcase(full)
            if nk in seen_dirs:
                continue
            seen_dirs.add(nk)
            items.append(_file_item("dir", full))       # 公共目录下同名 = 低置信（可能是共享/别家）

    # ---- 注册表 ----
    # 1) Uninstall 下的原键（卸载没删干净=陈旧项）
    if record.get("key_path") and _reg_key_exists(record["hive"], record["key_path"]):
        items.append({
            "kind": "reg", "path": _reg_label(record["hive"], record["key_path"]),
            "size": 0, "exists": True, "confidence": "high",
            "root": record["hive"], "rel": record["key_path"],
            "label": "卸载登记项（陈旧）",
        })
    # 2) Software\<token> 同名键（三视图）
    for root_const, flag in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                             (winreg.HKEY_CURRENT_USER, 0)):
        for t in toks:
            for disp in _token_display_names(record, t):
                rel = f"SOFTWARE\\{disp}"
                if _reg_key_exists(root_const, rel):
                    items.append({
                        "kind": "reg", "path": _reg_label(root_const, rel),
                        "size": 0, "exists": True, "confidence": "low",
                        "root": root_const, "rel": rel, "flag": flag,
                        "label": "Software 配置键",
                    })
    # 去重（同一标签路径可能因多 token 命中重复）
    uniq, out = set(), []
    for it in items:
        k = (it["kind"], it["path"].lower())
        if k in uniq:
            continue
        uniq.add(k)
        out.append(it)
    return out


def _file_item(kind, path, confidence="low"):
    size = _dir_size_kb(path) if kind == "dir" else (
        os.path.getsize(path) // 1024 if os.path.isfile(path) else 0)
    return {"kind": kind, "path": path, "size": size, "exists": os.path.exists(path),
            "confidence": confidence,
            "label": "程序目录" if kind == "dir" else "文件"}


def split_residue(items):
    """按置信度拆分残留：(high, low)。high=有把握属于该软件（自身目录/自身 Uninstall 键）。"""
    high = [it for it in items if it.get("confidence") == "high"]
    low = [it for it in items if it.get("confidence") != "high"]
    return high, low


def _token_display_names(record, token):
    """给 token 找回可能的注册表键显示名：原样名称 / 发布者（去尾部空格）。"""
    names = []
    for field in ("name", "publisher"):
        v = (record.get(field) or "").strip()
        if v and re.sub(r"[^\w]+", "", v).lower() == token and v not in names:
            names.append(v)
    if not names:
        names.append(record.get("name", "").strip())
    return [n for n in names if n]


def _reg_label(root_const, rel):
    import winreg
    names = {winreg.HKEY_LOCAL_MACHINE: "HKEY_LOCAL_MACHINE",
             winreg.HKEY_CURRENT_USER: "HKEY_CURRENT_USER",
             winreg.HKEY_CLASSES_ROOT: "HKEY_CLASSES_ROOT"}
    return names.get(root_const, "?") + "\\" + rel


# ====================================================================
# 5. 删除残留（上层已确认，逐条执行并回报）
# ====================================================================
def _recycle_path(path):
    """SHFileOperationW 把文件/目录移入回收站（可恢复）。返回 (ok, err)。"""
    import ctypes
    from ctypes import wintypes

    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT = 0x3, 0x40, 0x4, 0x4
    # pFrom 必须以双 \0 结尾
    buf = path + "\0\0"

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = buf
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
    try:
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception as e:
        return False, str(e)
    if res == 0:
        return True, ""
    return False, f"回收站删除失败（错误码 {res}）"


def _delete_forever(path):
    """降级永久删除：目录 rmtree，文件 remove。返回 (ok, err)。"""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return True, ""
    except OSError as e:
        return False, str(e)


def _reg_delete_recursive(root_const, rel, flag=0):
    """递归删除注册表键：先删最深子键，再删本键的值，最后删键（DeleteTree 已弃用）。"""
    import winreg
    access = winreg.KEY_READ | flag
    try:
        k = winreg.OpenKey(root_const, rel, 0, access)
    except FileNotFoundError:
        return True, ""          # 本就不存在＝已是目标态
    except OSError as e:
        return False, str(e)
    with k:
        subs = _enum_subkeys(k)
    for sub in subs:
        ok, err = _reg_delete_recursive(root_const, rel + "\\" + sub, flag)
        if not ok:
            return ok, err
    # 删本键的值
    try:
        k2 = winreg.OpenKey(root_const, rel, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE | flag)
    except OSError as e:
        return False, str(e)
    with k2:
        vals, i = [], 0
        while True:
            try:
                vals.append(winreg.EnumValue(k2, i)[0])
            except OSError:
                break
            i += 1
        for v in vals:
            try:
                winreg.DeleteValue(k2, v)
            except OSError:
                pass
    try:
        winreg.DeleteKey(root_const, rel)
        return True, ""
    except FileNotFoundError:
        return True, ""
    except OSError as e:
        return False, str(e)


def remove_residue(items, log=None):
    """逐条删除已确认的残留项，返回每条结果 [{path, ok, msg}]。

    文件/目录：优先回收站，失败降级永久删除；注册表：递归删除，非管理员导致
    删不掉的项如实标记「需提权」，不假装成功。"""
    log = log or (lambda m: None)
    results = []
    for it in items:
        path = it.get("path", "")
        if it["kind"] == "reg":
            if it.get("root") is not None and it.get("rel"):
                ok, err = _reg_delete_recursive(it["root"], it["rel"], it.get("flag", 0))
                if not ok and "Access is denied" in err.lower():
                    err += "（需以管理员身份重试清理）"
            else:
                ok, err = False, "注册表项缺少定位信息，已跳过"
            log(("  ✅ 删除注册表 " if ok else "  ❌ 注册表未删 ") + path
                + (f"：{err}" if err else ""))
            results.append({"path": path, "ok": ok, "msg": err})
            continue
        # 文件 / 目录
        if not os.path.exists(path):
            results.append({"path": path, "ok": True, "msg": "已不存在"})
            continue
        ok, err = _recycle_path(path)
        how = "移入回收站"
        if not ok:
            ok, err = _delete_forever(path)
            how = "永久删除"
        log(("  ✅ " + how + " " if ok else "  ❌ 删除失败 ") + path
            + (f"：{err}" if err else ""))
        results.append({"path": path, "ok": ok, "msg": err})
    return results


# ====================================================================
# 5.5 批量自动卸载 + 保守清理（纯逻辑、回调式、无 Qt）
# ====================================================================
def auto_uninstall_and_clean(records, log=None, progress=None, should_stop=None):
    """逐条静默卸载并只自动删高置信残留；无静默口/受保护项跳过不弹窗。

    返回每条 report dict：{name, status, cleaned[], kept_low[], msg}。
    status ∈ uninstalled / skipped_protected / skipped_nosilent / failed。
    整批不抛异常（逐条 try），should_stop() 为真时中断。"""
    log = log or (lambda m: None)
    progress = progress or (lambda *a: None)
    should_stop = should_stop or (lambda: False)
    total = len(records)
    reports = []
    for i, rec in enumerate(records):
        if should_stop():
            log("⏹ 收到停止请求，已中断批量。")
            break
        name = rec.get("name", "")
        progress(i, total, name)
        rep = {"name": name, "status": "", "cleaned": [], "kept_low": [], "msg": ""}
        try:
            if rec.get("protected"):
                rep["status"] = "skipped_protected"
                log(f" 跳过（受保护）：{name}")
            elif not silent_capable(rec):
                rep["status"] = "skipped_nosilent"
                log(f"⏭ 跳过（无静默卸载口，需手动）：{name}")
            else:
                res = run_uninstall(rec, "silent", log=log)
                if not res.get("ok"):
                    rep["status"] = "failed"
                    rep["msg"] = res.get("msg", "")
                    log(f"❌ 卸载失败：{name} — {rep['msg']}")
                else:
                    high, low = split_residue(scan_residue(rec))
                    if high:
                        rm = remove_residue(high, log=log)
                        rep["cleaned"] = [r["path"] for r in rm if r["ok"]]
                    rep["kept_low"] = [it["path"] for it in low]
                    rep["status"] = "uninstalled"
                    rep["msg"] = res.get("msg", "")
                    log(f"✅ 已卸载：{name}（自动清理 {len(rep['cleaned'])} 项残留"
                        + (f"，{len(rep['kept_low'])} 项低置信未动）" if rep["kept_low"] else "）"))
        except Exception as e:
            rep["status"] = "failed"
            rep["msg"] = str(e)
            log(f"❌ 处理异常：{name} — {e}")
        reports.append(rep)
    progress(total, total, "完成")
    return reports


# ====================================================================
# 展示辅助
# ====================================================================
def human_size(kb):
    """把 KB 数转成人话字符串。"""
    if not kb or kb <= 0:
        return ""
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


def human_date(yyyymmdd):
    """把 InstallDate（YYYYMMDD）转成 YYYY-MM-DD，非法值原样返回。"""
    s = str(yyyymmdd or "")
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return ""


# ====================================================================
# 卸载串解析 / 图标 / 校验辅助
# ====================================================================
_EXE_EXT = ("exe", "msi", "com", "bat", "cmd")


def _exe_ext_cut(s):
    r"""找未加引号卸载串里 exe 路径的结束位置（第一个「合法边界的可执行扩展名」之后）。

    不靠 \b（历史教训：写进源码时 \b 易被吞成退格符）：手动找 .exe/.msi/... 并校验
    其后一个是 空格/结尾/引号/斜杠，避免把 `C:\Program`（截断的中间目录）误当成 exe。
    找不到返回 -1。"""
    low = s.lower()
    best = -1
    for e in _EXE_EXT:
        tok = "." + e
        idx = low.find(tok)
        while idx != -1:
            j = idx + len(tok)
            nxt = s[j:j + 1]
            if nxt == "" or nxt.isspace() or nxt in ('"', "'", "/"):
                if best == -1 or idx < best:
                    best = idx
                break
            idx = low.find(tok, idx + 1)
    if best < 0:
        return -1
    return best + 4          # .exe/.msi/.com/.bat/.cmd 都是 4 字符（含点）


def _launchable(exe):
    """exe 是否可作为程序启动：带路径分隔符必须是磁盘上的真实文件（挡住目录）。"""
    if not exe:
        return False
    if ("\\" in exe) or ("/" in exe):
        return os.path.isfile(exe)
    return True              # 裸命令名（msiexec.exe / rundll32）交给系统解析


def _clean_icon_path(s):
    """规范化 DisplayIcon：去引号、去前导 -、去尾部 ,index、展开环境变量。"""
    s = (s or "").strip().strip('"').strip()
    if s.startswith("-"):
        s = s[1:].strip().strip('"').strip()
    m = re.search(r",\s*-?\d+\s*$", s)
    if m:
        s = s[:m.start()]
    return os.path.expandvars(s.strip())


def icon_source_path(record):
    """给记录找一个能取到图标的真实文件路径；取不到返回 ""。

    优先 DisplayIcon（多数程序会指向自己的主 exe / ico），其次卸载器 exe。
    InstallLocation 是目录、主程序名难猜，不纳入（避免取到无关 exe）。"""
    di = _clean_icon_path(record.get("display_icon", ""))
    if di and os.path.isfile(di):
        return di
    for field in ("uninstall_string", "quiet_uninstall_string"):
        exe, _ = parse_uninstall_string(record.get(field, ""))
        if exe and os.path.isfile(exe):
            return exe
    return ""
