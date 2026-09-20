"""
checkers/spec_checker.py —— 口播脚本规范检测
两层设计：
  1. check_local()  本地规则，零依赖即用：
       禁用词命中（规范卡禁用词 + 风控中心生效政策的禁用词）
       价格口径比对（脚本出现的「N元/N块」是否都在规范卡价格口径内）
       必含话术缺失
  2. check_remote() 预留外部 AI 接口（OpenAI 兼容 chat/completions）：
       在 config.json / config_local.py 配置 SCRIPT_CHECK =
       {"enabled": true, "url": "https://.../v1/chat/completions", "api_key": "sk-...", "model": "..."}
       未配置时返回 (False, 提示)，界面按钮置灰引导去设置页。
"""
import json
import re

# 兜底禁用词：即使风控中心为空也生效（广告法极限词高频项）
BASE_BANNED = ["最好", "最佳", "第一", "顶级", "独家", "绝无仅有", "史无前例",
               "根治", "包治", "特效", "全效", "安全无副作用", "无任何副作用",
               "100%有效", "百分百有效", "永不复发", "零风险", "稳赚", "全网底价"]

_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块钱|块)")


def _words(s):
    return [w.strip() for w in re.split(r"[,，;；、\n]+", s or "") if w.strip()]


def check_local(text, spec=None, product_name=""):
    """返回问题列表 [{"level": "高/中/提示", "msg": "..."}]，空列表=通过"""
    spec = spec or {}
    issues = []
    if not (text or "").strip():
        return [{"level": "高", "msg": "没有可检测的口播文案（任务未生成口播或提取失败），"
                                      "检测的是提示词，建议先确认内容来源"}]

    # ---- 1. 禁用词：基础表 + 规范卡 + 风控中心 ----
    banned = list(BASE_BANNED) + _words(spec.get("禁用词"))
    try:
        from store import risk_store
        banned += risk_store.banned_words(product_name)
    except Exception:
        pass
    hit = sorted({w for w in banned if w and w in text})
    for w in hit:
        issues.append({"level": "高", "msg": f"命中禁用词「{w}」，必须改写"})

    # ---- 2. 价格口径 ----
    caliber = spec.get("价格口径") or ""
    if caliber:
        allowed = set(re.findall(r"\d+(?:\.\d+)?", caliber))
        for m in _PRICE_RE.finditer(text):
            if m.group(1) not in allowed:
                issues.append({"level": "高",
                               "msg": f"口播价格「{m.group(0)}」不在规范卡价格口径内（口径数字：{sorted(allowed)}）"})
    else:
        prices = _PRICE_RE.findall(text)
        if prices and spec:
            issues.append({"level": "中",
                           "msg": f"脚本出现价格 {sorted(set(prices))}，但规范卡未填「价格口径」，无法核对"})

    # ---- 3. 必含话术 ----
    for phrase in _words(spec.get("必含话术")):
        if phrase not in text:
            issues.append({"level": "中", "msg": f"缺少必含话术「{phrase}」"})

    # ---- 4. 活动提示 ----
    act_words = ["活动", "送", "赠", "优惠", "折扣", "半价", "秒杀", "拼团", "领券", "限时"]
    if (spec.get("活动口径") or "").strip() and any(w in text for w in act_words) \
            and "以页面" not in text and "详情" not in text:
        issues.append({"level": "提示",
                       "msg": "脚本涉及活动/赠品，建议加「具体以页面详情为准」类兜底话术"})
    return issues


# ---------------- 预留：外部 AI 检测 ----------------

def remote_configured():
    try:
        from core.config import SCRIPT_CHECK
        return bool(SCRIPT_CHECK.get("enabled") and SCRIPT_CHECK.get("url"))
    except Exception:
        return False


def build_prompt(text, spec=None, product_name=""):
    """把脚本 + 规范卡 + 适用风控政策拼成给大模型的审查提示"""
    lines = ["你是短视频带货口播脚本的合规审核员。请审查下面的脚本是否违反规范卡与风控政策，",
             "逐条输出问题（风险等级：高/中/提示 + 原因 + 修改建议），没有问题则输出「通过」。", ""]
    if product_name:
        lines.append(f"【产品】{product_name}")
    for k, v in (spec or {}).items():
        if (v or "").strip():
            lines.append(f"【规范卡-{k}】{v}")
    try:
        from store import risk_store
        for r in risk_store.rules_for(product_name):
            lines.append(f"【风控政策-{r['title']}】{(r['content'] or '')[:500]}")
    except Exception:
        pass
    lines += ["", "【待审脚本】", text]
    return "\n".join(lines)


def check_remote(text, spec=None, product_name=""):
    """调用外部 AI 接口做语义级检测。
    返回 (成功与否, 结论文本)。接口协议按 OpenAI Chat Completions 预留，
    如实际服务不是 OpenAI 兼容格式，只需要改本函数内的 payload/解析。"""
    if not remote_configured():
        return False, ("未配置 AI 检测接口。\n\n在「设置 → 脚本 AI 检测接口」填入服务地址/Key/模型后即可启用；"
                       "本地规则检测不受影响。")
    from core.config import SCRIPT_CHECK
    try:
        import requests
        payload = {
            "model": SCRIPT_CHECK.get("model") or "gpt-4o-mini",
            "messages": [{"role": "user",
                          "content": build_prompt(text, spec, product_name)}],
            "temperature": 0.2,
        }
        resp = requests.post(SCRIPT_CHECK["url"],
                             headers={"Authorization": f"Bearer {SCRIPT_CHECK.get('api_key', '')}"},
                             data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                             timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return True, data["choices"][0]["message"]["content"]
    except Exception as e:
        return False, f"AI 接口调用失败：{type(e).__name__}: {e}"
