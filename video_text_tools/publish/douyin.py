"""
video_text_tools/publish/douyin.py —— 抖音发布适配器（一期参考实现，cookie 会话）

目标：拿创作者中心（creator.douyin.com）登录后的 cookie，走「申请上传 → PUT 直链
→ 创建视频」的序列，把成品发到抖音号。端点地址、UA、cookie→参数映射、反爬签名
参数名等**可变项全部从配置读**（core.config.publish_config() 的 douyin 段，可被
account.extra 覆盖），代码里不硬编码——平台改版时改配置即可，不必动这段逻辑。

⚠ 诚实说明：抖音 web 内部端点会随平台改版变动，且部分请求需要 a_bogus / msToken /
verifyFp 一类反爬签名参数。本实现把标准序列与可配置签名参数都铺好了，但**不承诺
任何时点都能无条件跑通**；若某端返回签名校验失败，请在配置的 sign_params /
headers 里补齐对应值。端点未配置（占位 <...>）时会明确抛「未配置」而不是假成功。
"""
import os

import requests

from .base import PlatformAdapter, register
from .models import (PublishRecord, AuthError, UploadError, RemoteRejectError,
                     NotConfiguredError, now_str, PLATFORM_DOUYIN, AUTH_COOKIE)

# 占位判定：配置里没填真端点（还是模板 <...>）就别假装能发
def _is_placeholder(v):
    s = str(v or "").strip()
    return not s or s.startswith("<")


def _douyin_cfg(acct):
    """合并默认发布配置里的 douyin 段 + 账号 extra（账号级覆盖优先）。"""
    cfg = {}
    try:
        from core.config import publish_config
        cfg = dict(publish_config().get("douyin") or {})
    except Exception:
        cfg = {}
    cfg.update(acct.extra or {})
    return cfg


class DouyinCookieAdapter(PlatformAdapter):
    key = PLATFORM_DOUYIN
    auth_type = AUTH_COOKIE
    display_name = "抖音·Cookie"

    # ---- 会话：cookie 串 → requests.Session ----
    def _session(self, acct, cfg):
        cookie = str((acct.secret or {}).get("cookie") or "").strip()
        if not cookie:
            raise AuthError("未配置抖音 cookie（请在账号里粘贴创作者中心登录后的 Cookie）")
        s = requests.Session()
        s.headers.update({
            "User-Agent": cfg.get("user_agent") or
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Referer": cfg.get("referer") or "https://creator.douyin.com/",
        })
        # 手工塞 cookie：比走 Set-Cookie 解析稳（登录 cookie 常带 SameSite/域差异）
        s.headers["Cookie"] = cookie
        return s

    def _params(self, cfg):
        """附加签名/追踪参数（a_bogus/msToken/verifyFp…），从配置透传，缺省为空。"""
        return dict(cfg.get("sign_params") or {})

    def _endpoint(self, cfg, name):
        url = cfg.get(name)
        if _is_placeholder(url):
            raise NotConfiguredError(f"抖音端点未配置：{name}")
        return url

    # ---- 登录态探测 ----
    def check_auth(self, acct, log=None):
        cfg = _douyin_cfg(acct)
        try:
            s = self._session(acct, cfg)
            r = s.get(self._endpoint(cfg, "user_info_url"),
                      params=self._params(cfg), timeout=20)
        except NotConfiguredError:
            # 没配探测端点不代表登录无效：只要 cookie 非空就回"未知"，不阻断发布
            return True, "已配置 cookie（未设 user_info_url，跳过在线校验）"
        except requests.RequestException as e:
            return False, f"网络错误：{e}"
        ok = r.status_code == 200
        return ok, "登录态有效" if ok else f"HTTP {r.status_code}（cookie 可能已过期）"

    # ---- 上传：申请直链 → PUT 文件 ----
    def upload(self, acct, item, log=None, progress=None, should_stop=None):
        if not os.path.exists(item.video_path):
            raise UploadError(f"视频文件不存在：{item.video_path}")
        cfg = _douyin_cfg(acct)
        s = self._session(acct, cfg)
        size = os.path.getsize(item.video_path)
        if progress:
            progress(0, 100, os.path.basename(item.video_path))

        # 1) 申请上传
        init = s.post(self._endpoint(cfg, "upload_init_url"),
                      params=self._params(cfg),
                      json={"source":"PC","video_size":size,
                            "filename": os.path.basename(item.video_path),
                            "title": item.title or ""},
                      timeout=30)
        body = _json(init)
        _ensure_ok(init, body, "申请上传失败")
        data = body.get("data") or {}
        put_url = data.get("upload_address") or data.get("upload_url")
        handle = {"video_uri": data.get("video_model") or data.get("video_uri") or "",
                  "upload_id": data.get("video_id") or data.get("upload_id") or ""}
        if not put_url:
            raise UploadError(f"申请上传未返回直链：{str(body)[:160]}")

        # 2) PUT 上传文件（分块读，避免整文件进内存）
        with open(item.video_path, "rb") as f:
            put = s.put(put_url, data=f, timeout=600)
        if put.status_code >= 400:
            raise UploadError(f"上传直链失败 HTTP {put.status_code}: {put.text[:160]}")
        if log:
            log(f"  ↑ 视频已上传（{size // 1024 // 1024}MB），等待平台转码")
        if progress:
            progress(90, 100, os.path.basename(item.video_path))
        return handle

    # ---- 创建发布 ----
    def publish(self, acct, item, handle, log=None):
        cfg = _douyin_cfg(acct)
        s = self._session(acct, cfg)
        payload = {
            "video_uri": handle.get("video_uri", ""),
            "video_id": handle.get("upload_id", ""),
            "title": item.title or "",
            "desc": item.desc or "",
            "tags": item.tags or [],
            "visibility": item.visibility or "public",
        }
        if item.publish_at:
            payload["publish_time"] = item.publish_at
        r = s.post(self._endpoint(cfg, "create_url"),
                   params=self._params(cfg), json=payload, timeout=60)
        body = _json(r)
        rec = PublishRecord(platform=self.key, account_label=acct.label,
                            title=item.display_title(), video_path=item.video_path)
        _ensure_ok(r, body, "发布失败")
        data = body.get("data") or {}
        aweme_id = str(data.get("aweme_id") or data.get("item_id") or "")
        base_url = cfg.get("post_url_base") or "https://www.douyin.com/video/"
        rec.ok = bool(aweme_id)
        rec.post_id = aweme_id
        rec.post_url = (base_url + aweme_id) if aweme_id else ""
        rec.published_at = now_str()
        if not aweme_id:
            rec.ok = False
            rec.message = f"发布回执无 aweme_id：{str(body)[:160]}"
        return rec


# ---------------- 小工具 ----------------
def _json(resp):
    try:
        return resp.json() or {}
    except ValueError:
        return {}


def _ensure_ok(resp, body, prefix):
    """抖音网关约定 {status_code/msg/data}：status_code!=0 或 HTTP>=400 视为拒绝。"""
    if resp.status_code >= 400:
        raise RemoteRejectError(f"{prefix} HTTP {resp.status_code}: {resp.text[:160]}")
    sc = body.get("status_code")
    if sc not in (0, None):
        raise RemoteRejectError(f"{prefix}：{body.get('status_msg') or body.get('msg') or sc}")


# 登记进注册表（模块被导入时生效）；直接调用装饰器函数，保持类本体干净
register(DouyinCookieAdapter)
