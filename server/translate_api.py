"""server/translate_api.py —— 火山引擎「文本翻译」服务端封装

逻辑与客户端 core/translate.py 的接口层一致（分批 + 报文解析 + 友好报错），
区别只有两点：
1. ak/sk 来自 upstreams.json 的 translate 段——密钥不再随 exe 分发；
2. 只做批量翻译一个动作（语种切段、中英混排等"人话加工"留在客户端做）。
"""
import json

from server import upstream

# 火山「文本翻译」官方限制：一次请求 TextList 不超 16 条、总长不超 5000 字符
BATCH_SIZE = 16
BATCH_CHARS = 4500

_CLIENT = None


class TranslateError(Exception):
    """翻译不可用/调用失败：消息原样带给网关响应，客户端界面直接展示"""


def _client():
    """只借 volcengine SDK 的 V4 签名，服务类自己搭（口径同客户端 _client）"""
    global _CLIENT
    cfg = upstream.TRANSLATE_API
    ak, sk = str(cfg.get("ak") or ""), str(cfg.get("sk") or "")
    if not ak or not sk:
        raise TranslateError("网关未配置翻译密钥（upstreams.json 的 translate 段）")
    if _CLIENT is not None:
        return _CLIENT
    try:
        from volcengine.ApiInfo import ApiInfo
        from volcengine.Credentials import Credentials
        from volcengine.ServiceInfo import ServiceInfo
        from volcengine.base.Service import Service
    except ImportError:
        raise TranslateError("网关缺少翻译依赖：pip install volcengine")
    host = "translate.volcengineapi.com"
    service, action, version = "translate", "TranslateText", "2020-06-01"
    region = str(cfg.get("region") or "cn-north-1")
    info = ServiceInfo(host, {}, Credentials("", "", service, region), 10, 10)
    apis = {action: ApiInfo("POST", "/", {"Action": action, "Version": version}, {}, {})}
    cli = Service(info, apis)
    cli.set_ak(ak)
    cli.set_sk(sk)
    _CLIENT = cli
    return cli


def reset_client():
    """upstreams.json 热重载后换密钥用（测试也从这清缓存）"""
    global _CLIENT
    _CLIENT = None


def _item_text(item):
    if isinstance(item, dict):
        for key in ("Translation", "translation", "dst", "text"):
            if item.get(key) is not None:
                return str(item[key])
        return ""
    return str(item if item is not None else "")


def _extract(resp, n):
    if not isinstance(resp, dict):
        raise TranslateError(f"翻译接口返回无法识别：{str(resp)[:200]}")
    meta = resp.get("ResponseMetadata")
    err = ((meta or {}).get("Error") or {}) if isinstance(meta, dict) else {}
    if err:
        raise TranslateError("翻译接口报错：{} {}".format(
            err.get("Code") or err.get("CodeN") or "", err.get("Message") or ""))
    items = resp.get("TranslationList") or resp.get("translation_list") or []
    if isinstance(items, list) and len(items) == n:
        return [_item_text(x) for x in items]
    raise TranslateError(f"翻译接口返回条数对不上：{json.dumps(resp, ensure_ascii=False)[:200]}")


def _batches(texts):
    cur, total = [], 0
    for t in texts:
        if cur and (len(cur) >= BATCH_SIZE or total + len(t) > BATCH_CHARS):
            yield cur
            cur, total = [], 0
        cur.append(t)
        total += len(t)
    if cur:
        yield cur


def translate_texts(texts, source="auto", target="zh"):
    """批量翻译（自动分批），返回与入参等长的译文列表；失败抛 TranslateError"""
    texts = [str(t) for t in texts]
    if not texts:
        return []
    cli = _client()
    project = str(upstream.TRANSLATE_API.get("project") or "default")
    out = []
    for batch in _batches(texts):
        body = {"TargetLanguage": target, "ProjectName": project, "TextList": batch}
        if source and source != "auto":
            body["SourceLanguage"] = source
        try:
            resp = json.loads(cli.json("TranslateText", {}, json.dumps(body)))
        except Exception as e:
            raise TranslateError(f"翻译请求失败：{str(e)[:200]}")
        out.extend(_extract(resp, len(batch)))
    return out
