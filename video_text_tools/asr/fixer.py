"""
video_text_tools/asr/fixer.py —— 识别后用大模型按语义纠错（DeepSeek 等）

faster-whisper 是"听音写字"，同音字（钙/盖、骨密度/股民度）它无从分辨；本模块把
逐字稿整篇发给一个 OpenAI 兼容的 chat 端点（默认 DeepSeek），让大模型按上下文只改
错别字/同音字/明显断句，**保持每行编号与行数不变**——于是句子时间戳原样保留，纠错后
仍能拿去烧字幕。

接入方式与豆包 Vision 完全一致（POST {base}/chat/completions、Bearer、退避重试），
配置读 core.config.asr_fix_config()。未配 api_key/model → FixNotConfigured（GUI 引导
去接口管理页配置），绝不静默返回原文当"成功"。作为「语音识别主体」的一环，只依赖
本包的 asr.types，不回依赖 breakdown。
"""
import re
import time

from .types import FixError, FixNotConfigured

_SYSTEM = (
    "你是中文口播字幕的纠错助手。给定一段按行编号的字幕，其中可能有同音字、错别字、"
    "明显的断句错误。你的任务：只纠正这些错误、让文字通顺，"
    "不得翻译、不得改写语气风格、不得合并或拆分句子、不得增删行数。"
    "严格保持每行的编号与行数与输入完全一致，每行只输出纠正后的文本本身，"
    "不要输出任何解释、标题或代码块标记。"
)

# 行首编号：允许 "1\t文本" / "1. 文本" / "1、文本" / "1 文本"
_LINE_RE = re.compile(r"^\s*(\d+)\s*[\t.、:：]?\s?(.*)$")


def _parse_numbered(content, n):
    """把模型返回按行首编号解析成 {编号(1..n): 文本}；解析不到的编号不入选。

    容忍代码围栏、空行、行首多余符号。只认落在 1..n 的编号，越界丢弃。
    """
    out = {}
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        try:
            idx = int(m.group(1))
        except ValueError:
            continue
        if 1 <= idx <= n:
            out[idx] = m.group(2).strip()
    return out


class AsrFixer:
    """纠错端点封装：一次 correct_segments 会把逐字稿分块送模型、按编号回填。"""

    def __init__(self, cfg):
        cfg = cfg or {}
        self.api_key = (cfg.get("api_key") or "").strip()
        self.model = (cfg.get("model") or "").strip()
        self.base_url = (cfg.get("base_url") or "").strip().rstrip("/") \
            or "https://api.deepseek.com/v1"
        if not self.api_key or not self.model:
            raise FixNotConfigured(
                "语音纠错未配置：需要 api_key 与 model。请在「接口管理」页的"
                "「✨ 语音纠错」里填写（如 DeepSeek 的 key 与 deepseek-chat）。")
        self.calls = 0
        self.tokens = 0

    # ---- 底层一次请求：带 429/5xx 退避重试 + token 统计（与豆包 Vision 同款）----
    def _post(self, messages, temperature=0.2, max_tokens=2048, retries=4):
        import requests
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        body = {"model": self.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens}
        last = None
        for attempt in range(retries):
            try:
                r = requests.post(url, headers=headers, json=body, timeout=60)
            except Exception as e:
                last = f"请求异常：{e}"
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code in (429, 500, 502, 503):
                last = f"HTTP {r.status_code}"
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code >= 400:
                raise FixError(f"纠错模型调用失败 HTTP {r.status_code}：{r.text[:200]}")
            try:
                j = r.json()
            except ValueError:
                raise FixError(f"纠错模型返回非 JSON：{r.text[:120]}")
            usage = j.get("usage") or {}
            self.tokens += int(usage.get("total_tokens",
                                         usage.get("prompt_tokens", 0)
                                         + usage.get("completion_tokens", 0)) or 0)
            self.calls += 1
            choices = j.get("choices") or []
            content = ((choices[0].get("message") if choices else {}) or {}).get("content", "")
            if not content:
                raise FixError(f"纠错模型返回空内容：{str(j)[:160]}")
            return content
        raise FixError(f"纠错模型限流重试 {retries} 次仍失败：{last}")

    def cost(self):
        return {"fix_calls": self.calls, "tokens": self.tokens}

    def _correct_block(self, lines, gloss):
        """纠正一块（≤chunk 行）：返回等长列表，元素为纠正文本或 None（该行未拿到）。"""
        sys = _SYSTEM
        if gloss:
            sys += "\n\n以下词语是正确写法，遇到同音/形近错误请改成它们：\n" + "、".join(gloss)
        body = "\n".join(f"{i + 1}\t{t}" for i, t in enumerate(lines))
        content = self._post([{"role": "system", "content": sys},
                              {"role": "user", "content": body}])
        parsed = _parse_numbered(content, len(lines))
        return [parsed.get(i + 1) or None for i in range(len(lines))]

    def correct_segments(self, segments, glossary=None, chunk=40):
        """对 [TranscriptSegment]（或带 .text 的对象/纯字符串）逐块纠错。

        返回与入参等长的字符串列表：拿到大模型纠正的行用纠正文本，
        任何一行解析不到/该块整体失败 → 回退原文，绝不吞段或错位。
        """
        texts = [(getattr(s, "text", None) if s is not None else "") or str(s or "")
                 for s in segments]
        out = list(texts)
        gloss = [str(g).strip() for g in (glossary or []) if str(g).strip()]
        for start in range(0, len(texts), max(1, chunk)):
            block = texts[start:start + chunk]
            try:
                fixed = self._correct_block(block, gloss)
            except FixNotConfigured:
                raise
            except Exception:
                fixed = [None] * len(block)          # 该块失败：整块保留原文
            for j, val in enumerate(fixed):
                if val:
                    out[start + j] = val
        return out


def apply_fix(segments, glossary=None, cfg=None, chunk=40):
    """「识别后语义纠错」的统一入口：就地按语义改 [TranscriptSegment].text（时间戳不动）。

    任何消费者（爆款拆解 pipeline、字幕 engine、语音识别工具）都调这一个函数，纠错行为
    只此一处。**绝不抛**：未配置(FixNotConfigured)/初始化失败/某块失败，都保留原始识别、
    只把原因写进返回的 note，由调用方决定怎么提示（记日志/加说明，不弹窗、不 fail）。

    返回 (changed, cost, note)：
      changed  实际被改动的句数；
      cost     {"fix_calls", "fix_tokens"}（未真正调用时为空 dict）；
      note     人话说明（成功=已改几句；未配置/失败=原因），供调用方展示。
    """
    if not segments:
        return 0, {}, ""
    try:
        if cfg is None:
            from core.config import asr_fix_config
            cfg = asr_fix_config()
        fixer = AsrFixer(cfg)
    except FixNotConfigured as e:
        return 0, {}, str(e)
    except Exception as e:
        return 0, {}, f"纠错初始化失败：{e}"
    try:
        fixed = fixer.correct_segments([s.text for s in segments],
                                       glossary=glossary, chunk=chunk)
    except Exception as e:
        return 0, {}, f"语义纠错失败（保留原始识别）：{e}"
    changed = 0
    for seg, new in zip(segments, fixed):
        new = (new or "").strip()
        if new and new != seg.text:
            seg.text = new
            changed += 1
    c = fixer.cost()
    cost = {"fix_calls": c.get("fix_calls", 0), "fix_tokens": c.get("tokens", 0)}
    return changed, cost, f"已 DeepSeek 语义纠错：改 {changed}/{len(segments)} 句"
