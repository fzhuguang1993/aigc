"""
video_text_tools/breakdown/acquire.py —— 分享文案 → 去水印视频 → 落盘

复用素材提取那套聚客解析（material_extract），不引 Playwright：
  extract_share_urls → call_parse_api(type=dsp) → data.video 直链 → download_file。
只把「下载去水印视频」这一步抽出来给拆解用（图集/文案不是拆解对象）。

失败一律抛 AcquireError，交 pipeline 标「解析下载」阶段 fail:原因，不静默。
凭证默认从 core.config + api_text/api_config.json 解析（与 MaterialPanel 同口径）；
GUI 也可显式传 api_cfg 固化启动前快照。
"""
from pathlib import Path
from urllib.parse import urlparse

from .models import AcquireError


def resolve_api_cfg():
    """生效的解析接口配置：界面保存的 api_config.json 覆盖默认；base 密文用姓名解密"""
    from core.config import (MATERIAL_API_BASE, MATERIAL_API_UID,
                             MATERIAL_API_KEY, API_TEXT_DIR, USER_NAME)
    from video_text_tools.material_extract import resolve_api_config
    return resolve_api_config(API_TEXT_DIR, {"base": MATERIAL_API_BASE,
                                             "uid": MATERIAL_API_UID,
                                             "key": MATERIAL_API_KEY},
                              secret=USER_NAME)


def resolve_and_download(share_text, out_dir, hosts_video=None,
                         log=None, api_cfg=None):
    """从粘贴文本解析出第一条可下载的去水印视频，落到 out_dir。

    返回 (video_path, title, notes, video_url)。
    - share_text：粘贴的分享文案（可含多条链接，取第一条能出视频直链的）
    - hosts_video：直链域名白名单（空/None 不限制，与 material_extract 同规则）
    - video_url：解析接口返回的去水印公网直链（供豆包 video_url 直连模式一次传整条视频）
    无有效链接 / 接口没配 / 未返回直链 / 域名不在白名单 / 下载出错 → 抛 AcquireError。
    """
    def _log(msg):
        if log:
            log(msg)

    from video_text_tools import material_extract as me

    urls = me.extract_share_urls(share_text)
    if not urls:
        raise AcquireError("没找到有效的分享链接，请检查粘贴内容")

    cfg = api_cfg or resolve_api_cfg()
    # 网关(商用)模式下 call_parse_api 走服务端注入的 base/uid/key，本机凭证作废：
    # 此时不强求本机配齐（与素材提取同口径——它把本机值原样透传、由 call_parse_api 判定）。
    if (not me._gateway_base()
            and not (cfg.get("base") and cfg.get("uid") and cfg.get("key"))):
        raise AcquireError("解析接口未配置齐全（缺地址/UID/Key），请先在「素材提取」配好接口")

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    notes, last_err = [], None
    for u in urls:
        try:
            data = me.call_parse_api(cfg["base"], cfg["uid"], cfg["key"], "dsp", u)
        except Exception as e:                       # 单条解析失败：记下换下一条
            last_err = e
            notes.append(f"{u} 解析失败：{e}")
            continue

        title = me.safe_title(data.get("title"))
        video = (data.get("video") or "").strip()
        if not video:
            last_err = AcquireError("接口未返回视频直链（可能是图集内容）")
            notes.append(str(last_err))
            continue

        if not me.host_allowed(video, hosts_video or set()):
            raise AcquireError(
                f"视频直链域名不在白名单，已拒绝下载：{urlparse(video).hostname}"
                "（可在「素材提取」更新 video.txt 名单）")

        _log(f"  · 解析成功：{title}")
        try:
            path = me.download_file(video, me.unique_path(out_dir, title, ".mp4"))
        except Exception as e:
            raise AcquireError(f"视频下载失败：{e}")
        _log(f"  ✓ 视频已下载 → {Path(path).name}")
        return path, title, notes, video

    raise AcquireError(str(last_err) if last_err else "所有链接均未解析出可下载视频")
