"""
script_extractor.py —— 口播文案 / 分镜数提取器
口播文案：从提示词里识别的对白/字幕文本（给剪辑快速定位是哪条提示词）；
脚本：使用软件的人自己填的创作文档（人物/场景等），两者独立字段，不要混淆。
"""
import re
from typing import Tuple, Optional
from core.logger import Ctx
from utils.excel_utils import update_row
from core.config import COL_SCRIPT, EXTRACT_SCRIPT_ENABLED

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
           "九": 9, "十": 10, "十一": 11, "十二": 12, "十三": 13, "十四": 14,
           "十五": 15, "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20}


def extract_script_from_prompt(prompt_text: str) -> str:
    """
    从提示词中提取中文对话
    
    Args:
        prompt_text: 完整的提示词文本
        
    Returns:
        提取出的中文对话，每行一个
    """
    # 方法 1: 匹配 <d>[Chinese] ... </d> 格式
    pattern1 = r'<d>\[Chinese\]\s*(.*?)\s*</d>'
    dialogs1 = re.findall(pattern1, prompt_text, re.DOTALL)
    
    # 方法 2: 匹配其他可能的对话格式（兼容全/半角冒号、直引号/中文引号/直角引号）
    pattern2 = r'说[：:]?\s*[“"『「](.+?)[”"』」]'
    dialogs2 = re.findall(pattern2, prompt_text, re.DOTALL)
    
    # 合并并去重
    all_dialogs = []
    for dialog in dialogs1 + dialogs2:
        # 清理文本
        dialog = re.sub(r'\n\s*\n', '\n', dialog).strip()
        if dialog and dialog not in all_dialogs:
            all_dialogs.append(dialog)
    
    return '\n'.join(all_dialogs)


def extract_storyboard_count(prompt_text: str) -> int:
    """从提示词识别分镜数（镜头1/第2个分镜/【镜头三】等编号取最大）；识别不出返回 0（留空）"""
    if not prompt_text or not prompt_text.strip():
        return 0
    found = []
    # 阿拉伯数字：镜头1 / 分镜 03 / 场景2 / 第4个镜头 / Shot 5 / SC06
    for m in re.finditer(r'(?:第\s*)?(?:分镜|镜头|镜位|场景|画面|Shot|SC)\s*[:：\-_\[【(]?\s*(\d{1,2})', prompt_text, re.IGNORECASE):
        found.append(int(m.group(1)))
    for m in re.finditer(r'第\s*(\d{1,2})\s*(?:个|条|组)?\s*(?:分镜|镜头|画面|场景)', prompt_text):
        found.append(int(m.group(1)))
    # 中文数字：第一个镜头 / 镜头三
    for m in re.finditer(r'第\s*([一二三四五六七八九十]{1,3})\s*(?:个|条|组)?\s*(?:分镜|镜头|画面|场景)', prompt_text):
        n = _CN_NUM.get(m.group(1))
        if n:
            found.append(n)
    for m in re.finditer(r'(?:分镜|镜头|场景)\s*[_\[【(]?\s*([一二三四五六七八九十]{1,3})', prompt_text):
        n = _CN_NUM.get(m.group(1))
        if n:
            found.append(n)
    # 只信合理范围内的编号（避免把时长“15秒”之类误计入）
    found = [n for n in found if 0 < n <= 40]
    return max(found) if found else 0


def ensure_script_fields(task_id: int, prompt: str, ctx=None) -> bool:
    """执行任务时补齐：口播文案/分镜数为空则从提示词自动识别（已有值不覆盖）。

    在提交链路调用，无论 5 秒还是 15 秒都识别；失败静默，不影响提交主流程。
    返回是否有字段被写入。
    """
    from store import task_store
    t = task_store.get_task(task_id)
    if not t or not EXTRACT_SCRIPT_ENABLED:
        return False
    upd = {}
    if not (t["script_text"] or "").strip():
        script = extract_script_from_prompt(prompt or "")
        if script:
            upd[task_store.COL_SCRIPT_TEXT] = script
    if not int(t["storyboard"] or 0):
        n = extract_storyboard_count(prompt or "")
        if n:
            upd[task_store.COL_STORYBOARD] = n
    if upd:
        task_store.update_row(task_id, **upd)
        if ctx:
            ctx.debug(f"自动识别补齐：{'+'.join(upd)}")
        return True
    return False


class ScriptExtractor:
    """口播文案提取器"""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.cache = {}

    def extract(self, prompt: str, ctx: Ctx) -> Tuple[Optional[str], Optional[str]]:
        """
        从提示词提取口播文案（本地提取，无需 API；不再按时长门槛跳过）
        """
        # 检查是否启用口播提取
        if not EXTRACT_SCRIPT_ENABLED:
            return None, None

        # 先查缓存
        if prompt in self.cache:
            ctx.debug("命中缓存")
            return self.cache[prompt], None

        try:
            # 本地提取，不调用 API
            script = extract_script_from_prompt(prompt)
            
            if not script:
                # 空镜任务没有口播，不输出警告（静默处理）
                return None, None

            # 缓存结果
            self.cache[prompt] = script
            ctx.info(f"提取成功：{len(script)} 字，{len(script.split(chr(10)))} 句")

            return script, None
        except Exception as e:
            error = f"提取过程异常：{e}"
            ctx.error(error)
            return None, error

    def extract_and_save(self, row_idx: int, prompt: str, account_base: str,
                         ctx: Ctx) -> bool:
        """
        提取并回写到 Excel
        """
        script, error = self.extract(prompt, ctx)

        if error:
            return False

        try:
            update_row(row_idx, **{COL_SCRIPT: script})
            ctx.info("已回写 Excel")
            return True
        except Exception as e:
            ctx.error(f"回写失败：{e}")
            return False
