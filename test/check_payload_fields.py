"""
test/check_payload_fields.py —— 本地 payload 字段与云端真实 schema 对一遍

    python test/check_payload_fields.py            # 用 config.json 里的线路逐个核对

为什么需要它：云端 JobRequest/JobParameters/JobInputs 都是
`additionalProperties: false` 的严格模型，**多写一个字段就整单 HTTP 422**，
而且是每一条线路、每一个任务都失败（本项目真的踩过：parameters.steps
应为 parameters.inference_steps）。这类错误在本地怎么点界面都测不出来，
只能拿云端的 openapi.json 对。

只读接口（GET /openapi.json），不提交任务、不占云端额度。
"""
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import ACCOUNTS                        # noqa: E402
import workers.submit as sub                            # noqa: E402
from workers.submit import SubmitOptions                # noqa: E402


def our_payload():
    """按当前配置组装一份代表性 payload（只关心字段名，参考图上传换成假函数）"""
    class _Ctx:
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def debug(self, *a, **k): pass

    sub.upload_asset = lambda base, p, t="image": "a" * 32
    # steps/duration 取边界值：顺便把“取值范围”也对一遍
    return sub.build_payload("http://schema-check/api/v1", "字段核对用提示词",
                             _Ctx(), SubmitOptions(duration=15, steps=50), None)


def check_base(base):
    """返回 (ok, 说明)。ok=False 时 message 里写清楚是哪个字段对不上"""
    # schema 跟 app 同前缀：接口是 {base}/jobs 时，schema 在 {base}/openapi.json
    # （剔掉 /api/v1 会访问到前端页面，拿回来的是 HTML）
    url = base.rstrip("/") + "/openapi.json"
    try:
        schema = requests.get(url, timeout=20).json()
    except Exception as e:
        return None, f"取不到 schema（{url}）：{type(e).__name__}: {e}"

    schemas = schema.get("components", {}).get("schemas", {})

    def props(name):
        return set((schemas.get(name, {}).get("properties") or {}).keys())

    expect = {"JobRequest": props("JobRequest"),
              "JobInputs": props("JobInputs"),
              "JobParameters": props("JobParameters")}
    if not expect["JobParameters"]:
        return None, "schema 里没有 JobParameters，云端接口版本可能变了"

    payload = our_payload()
    actual = {"JobRequest": set(payload),
              "JobInputs": set(payload.get("inputs", {})),
              "JobParameters": set(payload.get("parameters", {}))}
    bad = []
    for model, keys in actual.items():
        extra = keys - expect[model]
        if extra:
            bad.append(f"{model} 多出 {sorted(extra)}")
    if bad:
        return False, "；".join(bad)
    # 顺带把可选的调质量字段列出来，方便决定要不要往界面上加
    unused = sorted(expect["JobParameters"] - actual["JobParameters"])
    return True, (f"字段全部被云端接受｜云端尚有未使用的参数：{' '.join(unused)}"
                  if unused else "字段全部被云端接受")


def main():
    bases = [a["base"] for a in ACCOUNTS] or ["请先在 config.json 配置线路"]
    seen = set()
    for base in bases:
        if base in seen:
            continue
        seen.add(base)
        ok, msg = check_base(base)
        mark = {True: "✅", False: "❌", None: "⚠"}[ok]
        print(f"{mark} {base}\n   {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
