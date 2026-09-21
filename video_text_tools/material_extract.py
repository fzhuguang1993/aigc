"""
material_extract.py —— 素材提取（抖音/快手分享链接 → 去水印视频/图集/文案）

第三方解析接口（聚客 API）：
  GET {base}?type=dsp&uid=..&key=..&url=<分享短链>    去水印：data.title/cover/video/images
  GET {base}?type=wenan&uid=..&key=..&url=<分享短链>  文案提取：data.text（实测正文在
      text 字段；接口文档写的 title 实为视频标题，只作兜底）
接口地址/uid/key 优先级：api_text/api_config.json（工具界面保存）
> 程序默认值（core.config，可 config_local 覆盖），换账号不必重新打包。
地址只存在维护人的 config_local.py（不随代码分发），界面上只让改 uid/key。

安全约定：解析接口返回的直链，只有命中 api_text/ 下的域名白名单
（video.txt=视频短链域、image.txt=图片短链域）才允许下载。
白名单随接口域名变化，由使用者**首次使用工具时导入**（不随代码分发），
运行时重读——换新域名只需重新导入，不需要动代码；缺失时不限制但日志告警。
"""
import csv
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

# 分享文案里提取链接：排除中英文标点/括号，避免把「复制打开抖音」等尾巴带进来
_URL_RE = re.compile(r"https?://[^\s、，；,;><\"'（）()\[\]]+")
_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\r\n\t]')

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def extract_share_urls(text):
    """从粘贴文本提取全部视频分享短链（去重保序）；过滤解析接口自身的域名"""
    out, seen = [], set()
    for m in _URL_RE.findall(text or ""):
        u = m.rstrip(".,;。、/")
        host = (urlparse(u).hostname or "").lower()
        if not host or "zhuceka" in host:      # 粘贴了接口文档地址本身，跳过
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def load_allowed_hosts(whitelist_file):
    """读取域名白名单文件（一行一个域名，允许带 http(s):// 前缀），返回小写集合；
    文件缺失/为空返回空集"""
    try:
        lines = Path(whitelist_file).read_text(encoding="utf-8",
                                               errors="ignore").splitlines()
    except OSError:
        return set()
    out = set()
    for ln in lines:
        ln = ln.strip().lower().lstrip(".")
        if not ln or ln.startswith("#"):
            continue
        ln = re.sub(r"^https?://", "", ln).rstrip("/")
        if ln:
            out.add(ln)
    return out


def host_allowed(link, allowed):
    """直链域名校验：命中白名单域名本身或其子域即放行；白名单为空不限制"""
    if not allowed:
        return True
    host = (urlparse(link).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in allowed)


def call_parse_api(base, uid, key, qtype, share_url, timeout=30):
    """调解析接口，成功返回 data dict；失败抛 ValueError。qtype: dsp/wenan"""
    r = requests.get(base, params={"type": qtype, "uid": uid, "key": key,
                                   "url": share_url},
                     timeout=timeout, headers=_UA)
    r.raise_for_status()
    try:
        j = r.json()
    except ValueError:
        raise ValueError(f"接口未返回 JSON（地址可能已变更）：{r.text[:120]}")
    if int(j.get("code", 0) or 0) != 200:
        raise ValueError(f"{j.get('msg') or '解析失败'}（code={j.get('code')}）")
    return j.get("data") or {}


def safe_title(name, fallback="素材"):
    """标题转安全文件名：去非法字符、限长 60"""
    name = _UNSAFE_RE.sub("", (name or "").strip())[:60].rstrip(".")
    return name or fallback


def unique_path(directory, stem, suffix):
    """目录内防重名：已存在则追加 _2/_3…"""
    directory = Path(directory)
    p = directory / f"{stem}{suffix}"
    n = 2
    while p.exists():
        p = directory / f"{stem}_{n}{suffix}"
        n += 1
    return str(p)


# ---------------- 文案样本库（追加式 CSV，供后期喂大模型学写脚本） ----------------
CORPUS_FIELDS = ["提取时间", "平台", "视频标题", "原文链接", "文案"]

_PLATFORMS = (("douyin", "抖音"), ("iesdouyin", "抖音"), ("kuaishou", "快手"),
              ("gifshow", "快手"), ("weixin.qq", "视频号"), ("channels", "视频号"),
              ("xiaohongshu", "小红书"), ("xhscdn", "小红书"), ("bilibili", "B站"))


def guess_platform(url):
    """从分享链接域名推断平台（本地样本用标准名，方便后续分析/喂模型）"""
    host = (urlparse(url).hostname or "").lower()
    for key, name in _PLATFORMS:
        if key in host:
            return name
    return "其他"


# ---------------- 接口配置（地址/uid/key 运行时可换） ----------------
# 接口地址密文存储：用「使用人姓名」(config.USER_NAME) 作盐，PBKDF2-HMAC-SHA256
# 派生 Fernet 密钥加密 base 后再写盘，api_config.json 里不再躺着明文接口地址。
# 只有本机 config.json 姓名一致才解得开——拷这份 json 到别的机器/换名即用不了。
# secret 为空时退回明文（未配置姓名的兜底），旧明文配置也仍可正常读取。
_ENC_PREFIX = "enc:v1:"
_KDF_SALT = b"aigc.material.api.v1"
_KDF_ITERATIONS = 200_000


def _fernet(secret):
    """由使用人姓名派生对称密钥（PBKDF2-HMAC-SHA256 → Fernet）"""
    import base64
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_KDF_SALT,
                     iterations=_KDF_ITERATIONS).derive(str(secret).encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_value(plain, secret):
    """加密单个值；secret 缺失或值已加密则原样返回"""
    plain = (plain or "").strip()
    if not secret or not plain or plain.startswith(_ENC_PREFIX):
        return plain
    return _ENC_PREFIX + _fernet(secret).encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_value(token, secret):
    """解密单个值；非密文原样返回；姓名不符/损坏返回空串（按未配置处理）"""
    token = (token or "").strip()
    if not token.startswith(_ENC_PREFIX):
        return token
    if not secret:
        return ""
    try:
        return _fernet(secret).decrypt(
            token[len(_ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def resolve_api_config(api_dir, defaults, secret=None):
    """生效配置 = api_config.json（工具界面保存）覆盖程序默认值；
    文件不存在/损坏时原样返回 defaults，不阻断提取。
    base 若为密文，用 secret（使用人姓名）解密；解不开就当未配置（保留默认）。"""
    cfg = dict(defaults)
    try:
        j = json.loads((Path(api_dir) / "api_config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(j, dict):
        return cfg
    base = str(j.get("base") or "").strip()
    if base:
        base = decrypt_value(base, secret) if base.startswith(_ENC_PREFIX) else base
        if base:
            cfg["base"] = base
    for k in ("uid", "key"):
        v = str(j.get(k) or "").strip()
        if v:
            cfg[k] = v
    return cfg


def save_api_config(api_dir, base, uid, key, secret=None):
    """把接口凭证写入 api_config.json（下次提取立即生效）。

    base 传 None ＝保留文件里已有的接口地址：界面上不再让人改地址，
    但旧版本存过的地址不能被这次保存冲掉。
    给了 secret（使用人姓名）时接口地址以密文落盘；无 secret 则明文兜底。"""
    d = Path(api_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / "api_config.json"
    cfg = {}
    try:
        old = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        if isinstance(old, dict):
            cfg = old
    except ValueError:
        pass                                  # 文件写坏了：不拦保存，直接重建
    cfg["uid"] = (uid or "").strip()
    cfg["key"] = (key or "").strip()
    if base is not None:
        cfg["base"] = encrypt_value(base.strip().rstrip("/"), secret)
    f.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def append_corpus(csv_path, title, share_url, text, log=None):
    """向样本库 CSV 追加一条文案（utf-8-sig，Excel 双击不乱码）。

    同一原文链接不重复写入；写失败不阻断主流程，只记提醒。返回是否新写入。
    """
    try:
        csv_path = Path(csv_path)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        exists = csv_path.exists()
        if exists:   # 防重：同一链接只收一次
            with open(csv_path, encoding="utf-8-sig", newline="") as f:
                if any(row.get("原文链接") == share_url
                       for row in csv.DictReader(f)):
                    if log:
                        log("  · 文案已在样本库（链接重复，不追加）")
                    return False
        new = not exists
        with open(csv_path, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CORPUS_FIELDS)
            if new:
                w.writeheader()
            w.writerow({"提取时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "平台": guess_platform(share_url),
                        "视频标题": (title or "").strip(),
                        "原文链接": share_url,
                        "文案": text.strip()})
        if log:
            log(f"  ✓ 样本库追加 1 条 → {csv_path.name}")
        return True
    except OSError as e:
        if log:
            log(f"  ⚠ 样本库写入失败（不影响本次提取）：{e}")
        return False


def download_file(link, dest, timeout=120):
    """流式下载直链到目标路径（.part 临时文件防半截），返回路径"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(link, stream=True, timeout=timeout, headers=_UA) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if chunk:
                        f.write(chunk)
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)     # 异常时清掉半截文件
    return str(dest)


def extract_one(share_url, base, uid, key, out_dir,
                hosts_video, hosts_image,
                want_video=True, want_text=True, want_images=True,
                corpus_file=None, log=None, should_stop=None):
    """处理一条分享链接：去水印视频/图集（dsp）+ 文案（wenan）。

    corpus_file 不为空时，提取到的文案同步追加进样本库 CSV。
    返回 (落盘文件列表, 警告说明列表)；接口失败抛异常由调用方按条记录。
    """
    def _log(msg):
        if log:
            log(msg)

    saved, notes = [], []
    stem = ""

    if want_video or want_images:
        data = call_parse_api(base, uid, key, "dsp", share_url)
        if should_stop and should_stop():
            return saved, notes
        stem = safe_title(data.get("title"))

        video = (data.get("video") or "").strip()
        if want_video and video:
            if host_allowed(video, hosts_video):
                p = download_file(video, unique_path(out_dir, stem, ".mp4"))
                saved.append(p)
                _log(f"  ✓ 视频 → {Path(p).name}")
            else:
                notes.append(f"视频直链域名不在白名单，已跳过（可更新 api_text/video.txt）："
                             f"{urlparse(video).hostname}")
        elif want_video:
            notes.append("接口未返回视频直链（可能是图集内容）")

        if want_images:
            for i, img in enumerate(data.get("images") or []):
                img = (img or "").strip()
                if not img:
                    continue
                if not host_allowed(img, hosts_image):
                    notes.append(f"图集第{i + 1}张域名不在白名单，已跳过："
                                 f"{urlparse(img).hostname}")
                    continue
                ext = ".jpg"
                p = download_file(img, unique_path(out_dir, f"{stem}_图{i + 1}", ext))
                saved.append(p)
                _log(f"  ✓ 图集{i + 1} → {Path(p).name}")

    if want_text:
        w = call_parse_api(base, uid, key, "wenan", share_url)
        if should_stop and should_stop():
            return saved, notes
        # 实测文案正文在 data.text；title 是视频标题，仅作兜底
        text = (w.get("text") or w.get("title") or "").strip()
        stem = stem or safe_title(w.get("title") or text[:20])
        if text:
            p = Path(unique_path(out_dir, stem, ".txt"))
            p.write_text(f"{text}\n\n来源：{share_url}\n", encoding="utf-8")
            saved.append(str(p))
            _log(f"  ✓ 文案 → {Path(p).name}")
            if corpus_file:
                if append_corpus(corpus_file, w.get("title") or stem, share_url,
                                 text, log=log):
                    saved.append(str(corpus_file))
        else:
            notes.append("文案接口未返回内容")

    return saved, notes
