"""
video_text_tools/local_push/douyin.py —— 抖音本地推搭建适配器（官方 OpenAPI / OAuth2.0）

走巨量引擎开放平台「本地推」投放能力（新版层级：项目 → 营销 → 素材）：
  授权校验 → 素材上传 → 创建项目 → 创建营销（绑定素材）→ 回执落 BuildRecord。
端点地址、附加请求头等**可变项全部从配置读**（core.config.local_push_config()
的 douyin 段，可被 account.extra 覆盖），代码里不硬编码——平台改版时改配置
即可，不必动这段逻辑。

⚠ 诚实说明：接口路径与字段名以官方《本地推投放能力》文档为准，本实现把标准
序列与可配置端点都铺好了，但**不承诺任何时点都能无条件跑通**；若某端路径或
字段与官方版本不符，请在账号 extra / config 里覆盖端点，或按文档微调 payload
映射。端点未配置（留空或占位 <...>）时会明确抛「未配置」，而不是假成功。
"""
import os

import requests

from .base import BuildAdapter, register
from .models import (BuildRecord, BuildAuthError, BuildRemoteError,
                     BuildNotConfiguredError, now_str,
                     PLATFORM_DOUYIN_LOCAL)


# 占位判定：配置里没填真端点（还是模板 <...>）就别假装能建
def _is_placeholder(v):
    s = str(v or "").strip()
    return not s or s.startswith("<")


def _douyin_cfg(acct):
    """合并默认配置里的 douyin 段 + 账号 extra（账号级覆盖优先）。"""
    cfg = {}
    try:
        from core.config import local_push_config
        cfg = dict(local_push_config().get("douyin") or {})
    except Exception:
        cfg = {}
    cfg.update(acct.extra or {})
    return cfg


class DouyinLocalAdapter(BuildAdapter):
    key = PLATFORM_DOUYIN_LOCAL
    display_name = "抖音本地推·OpenAPI"

    # ---- 端点/请求组装 ----
    def _endpoint(self, cfg, name):
        url = str(cfg.get(name) or "").strip()
        if _is_placeholder(url):
            raise BuildNotConfiguredError(f"抖音本地推端点未配置：{name}")
        base = str(cfg.get("api_base") or "").strip()
        # 允许配相对路径（拼 api_base），也允许整段绝对地址
        if url.startswith("http") or not base:
            return url
        return base.rstrip("/") + "/" + url.lstrip("/")

    def _params(self, acct, cfg):
        """巨量引擎 OpenAPI v3.0：access_token 走 query 参数。"""
        return {"access_token": str((acct.secret or {}).get("access_token") or "").strip()}

    def _headers(self, acct, cfg):
        h = {"Content-Type": "application/json"}
        h.update(cfg.get("headers") or {})
        return h

    # ---- 授权校验 ----
    def check_auth(self, acct, log=None):
        if not acct.has_credential():
            return False, "未配置 access_token（请在账号里粘贴开放平台授权 Token）"
        cfg = _douyin_cfg(acct)
        try:
            r = requests.get(self._endpoint(cfg, "advertiser_info_url"),
                             params=self._params(acct, cfg),
                             headers=self._headers(acct, cfg), timeout=20)
        except BuildNotConfiguredError:
            # 没配探测端点不代表授权无效：只要凭证非空就回"未知"，不阻断搭建
            return True, "已配置凭证（未设 advertiser_info_url，跳过在线校验）"
        except requests.RequestException as e:
            return False, f"网络错误：{e}"
        try:
            body = r.json() or {}
        except ValueError:
            body = {}
        ok = r.status_code < 400 and body.get("code") in (0, None)
        if ok:
            return True, "授权有效"
        return False, f"HTTP {r.status_code} / {body.get('message') or '授权被拒'}"

    # ---- 批量搭建：素材上传 → 创建项目 → 创建营销 ----
    def build_one(self, acct, plan, log=None, progress=None, should_stop=None):
        cfg = _douyin_cfg(acct)
        rec = BuildRecord(platform=self.key, account_label=acct.label,
                          advertiser_id=acct.advertiser_id,
                          plan_name=plan.display_name())
        if not str(acct.advertiser_id or "").strip():
            raise BuildAuthError("账户未填本地推账户 ID（advertiser_id）")

        def _log(msg):
            if log:
                log(msg)

        videos = [str(p).strip() for p in (plan.videos or []) if str(p).strip()]
        if not videos:
            raise BuildRemoteError("方案未配置素材视频（本地推短视频投放至少需要 1 条）")
        total = len(videos) + 2               # 素材逐条 + 项目 + 营销
        done = 0

        # 1) 素材上传（逐条视频 → video_id）
        asset_ids = []
        for vp in videos:
            if should_stop and should_stop():
                rec.message = "已取消"
                return rec
            if not os.path.exists(vp):
                raise BuildRemoteError(f"素材文件不存在：{vp}")
            asset_ids.append(self._upload_asset(acct, cfg, vp))
            done += 1
            if progress:
                progress(done, total, os.path.basename(vp))
        rec.asset_count = len(asset_ids)
        _log(f"  ↑ 素材上传完成（{len(asset_ids)} 条）")

        # 2) 创建项目（预算/出价/定向）
        project_id = self._create_project(acct, cfg, plan)
        done += 1
        if progress:
            progress(done, total, "创建项目")
        _log(f"  ▶ 项目已创建：{project_id}")

        # 3) 创建营销（绑定素材/门店或商品）
        marketing_id = self._create_marketing(acct, cfg, plan, project_id, asset_ids)
        done += 1
        if progress:
            progress(done, total, "创建营销")
        _log(f"  ▶ 营销已创建：{marketing_id}")

        rec.ok = bool(project_id and marketing_id)
        rec.project_id = project_id
        rec.marketing_id = marketing_id
        rec.created_at = now_str()
        if not rec.ok:
            rec.message = "创建回执缺少 ID（项目/营销）"
        return rec

    # ---- 分步实现 ----
    def _upload_asset(self, acct, cfg, video_path):
        """上传一条素材视频，返回平台 video_id。"""
        url = self._endpoint(cfg, "asset_upload_url")
        try:
            with open(video_path, "rb") as f:
                r = requests.post(
                    url, params=self._params(acct, cfg),
                    files={"video_file": (os.path.basename(video_path), f, "video/mp4")},
                    headers=self._headers(acct, cfg), timeout=600)
        except requests.RequestException as e:
            raise BuildRemoteError(
                f"素材上传网络错误（{os.path.basename(video_path)}）：{e}") from e
        body = _json(r)
        _ensure_ok(r, body, f"素材上传失败（{os.path.basename(video_path)}）")
        data = body.get("data") or {}
        vid = str(data.get("video_id") or data.get("material_id") or "")
        if not vid:
            raise BuildRemoteError(f"素材上传回执无 video_id：{str(body)[:160]}")
        return vid

    def _create_project(self, acct, cfg, plan):
        """创建项目（项目层参数：预算/出价/定向），返回 project_id。"""
        payload = {
            "advertiser_id": acct.advertiser_id,
            "project_name": plan.display_name(),
            "marketing_goal": plan.goal,
            "promotion_type": plan.promo_type,
            "budget": plan.budget,
            "bid": plan.bid,
            "audience": {
                "region": _split_region(plan.region),
                "age": plan.age,
                "gender": plan.gender,
                "delivery_hours": plan.hours,
            },
        }
        r = requests.post(self._endpoint(cfg, "project_create_url"),
                          params=self._params(acct, cfg),
                          headers=self._headers(acct, cfg),
                          json=payload, timeout=60)
        body = _json(r)
        _ensure_ok(r, body, "创建项目失败")
        data = body.get("data") or {}
        pid = str(data.get("project_id") or data.get("id") or "")
        if not pid:
            raise BuildRemoteError(f"创建项目回执无 project_id：{str(body)[:160]}")
        return pid

    def _create_marketing(self, acct, cfg, plan, project_id, asset_ids):
        """创建营销（绑定素材/门店或商品），返回营销 ID。"""
        payload = {
            "advertiser_id": acct.advertiser_id,
            "project_id": project_id,
            "marketing_name": f"{plan.display_name()}-营销",
            "target_id": plan.target_id,
            "douyin_uid": plan.douyin_uid,
            "video_ids": list(asset_ids),
            "titles": list(plan.titles or []),
        }
        r = requests.post(self._endpoint(cfg, "marketing_create_url"),
                          params=self._params(acct, cfg),
                          headers=self._headers(acct, cfg),
                          json=payload, timeout=60)
        body = _json(r)
        _ensure_ok(r, body, "创建营销失败")
        data = body.get("data") or {}
        mid = str(data.get("promotion_id") or data.get("marketing_id")
                  or data.get("id") or "")
        if not mid:
            raise BuildRemoteError(f"创建营销回执无营销 ID：{str(body)[:160]}")
        return mid


# ---------------- 小工具 ----------------
def _json(resp):
    try:
        return resp.json() or {}
    except ValueError:
        return {}


def _ensure_ok(resp, body, prefix):
    """巨量引擎 OpenAPI 约定 {code, message, data}：HTTP>=400 或 code!=0 视为拒绝。"""
    if resp.status_code >= 400:
        raise BuildRemoteError(f"{prefix} HTTP {resp.status_code}: {resp.text[:160]}")
    code = body.get("code")
    if code not in (0, None):
        raise BuildRemoteError(f"{prefix}：{body.get('message') or code}")


def _split_region(region):
    """逗号（含中文逗号）分隔的地域串 → 列表（去空）。"""
    s = str(region or "").replace("，", ",")
    return [x.strip() for x in s.split(",") if x.strip()]


# 登记进注册表（模块被导入时生效）；直接调用装饰器函数，保持类本体干净
register(DouyinLocalAdapter)
