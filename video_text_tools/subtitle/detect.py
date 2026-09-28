"""
video_text_tools/subtitle/detect.py —— 抽帧 + 豆包判断"画面是否已烧录字幕"

批量入口对已处理过的视频不重复烧：先抽几张均匀帧，问豆包"这帧里有没有成行的烧录字幕"，
多数投票。这是尽力而为的启发式——视觉模型判定非 100% 可靠，所以：

  判定不了（未配豆包 / 抽帧失败 / 无有效回包）一律返回 verdict=None，
  由 engine **保守当作"无字幕"继续补生成**，绝不因检测失败而漏做字幕。

复用 breakdown：frames.extract_frames 抽帧、vision.DoubaoVision 走 _post + _encode_image
做"带图的自定义问答"（analyze_frames 会把结构裁成景别等固定字段，装不下我们的
has_subtitle，故这里直接吃底层 _post）。
"""
import json
import re
import shutil
from pathlib import Path

_PROMPT = ("你在检查一张视频截图。只判断：画面里（尤其底部或顶部）是否已经存在"
           "烧录进去的字幕条（成行的对白/口播字幕）。忽略水印、台标 logo、点赞关注"
           '按钮、弹幕等。只回 JSON：{"has_subtitle": true 或 false}')

_JSON_RE = re.compile(r"\{.*?\}", re.S)


def _parse_yesno(content):
    """从模型返回里抠 has_subtitle；抠不到返回 None。"""
    m = _JSON_RE.search(content or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    v = d.get("has_subtitle")
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "是")
    return None


def has_burned_subtitle(video, doubao_cfg, work_dir, log=None, should_stop=None):
    """返回 (verdict, reason)：True=已带字幕 / False=没有 / None=无法判定。

    None 由上层保守当作无字幕处理（见模块头）。
    """
    from ..breakdown import frames as frames_mod
    from ..breakdown.vision import DoubaoVision, _encode_image
    from ..breakdown.models import ConfigError

    def _log(m):
        if log:
            log(m)

    work = Path(work_dir) / "detect"
    try:
        vision = DoubaoVision(doubao_cfg or {})
    except ConfigError as e:
        return None, f"未配置豆包，跳过检测（{e}）"
    except Exception as e:
        return None, f"检测初始化失败：{e}"

    try:
        shots = frames_mod.extract_frames(video, str(work), interval=6, log=log)
        step = max(1, len(shots) // 5)
        sample = shots[::step][:5] or shots[:3]
        votes = []
        for fs in sample:
            if should_stop and should_stop():
                break
            content = vision._post([{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": _encode_image(fs.path)}},
                {"type": "text", "text": _PROMPT}]}])
            v = _parse_yesno(content)
            if v is not None:
                votes.append(v)
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        return None, f"检测失败：{e}"
    shutil.rmtree(work, ignore_errors=True)

    if not votes:
        return None, "检测无有效回包，按无字幕处理"
    return (sum(votes) > len(votes) / 2), f"抽样 {len(votes)} 帧判定"
