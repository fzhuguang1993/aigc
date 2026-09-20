"""
script_extractor.py —— 口播文案提取器
"""
import re
from typing import Tuple, Optional
from core.logger import Ctx
from utils.excel_utils import update_row
from core.config import COL_SCRIPT, EXTRACT_SCRIPT_ENABLED, MIN_DURATION_FOR_SCRIPT, DEFAULT_DURATION


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
    
    # 方法 2: 匹配其他可能的对话格式
    pattern2 = r'说.*?:\s*["\'](.+?)["\']'
    dialogs2 = re.findall(pattern2, prompt_text, re.DOTALL)
    
    # 合并并去重
    all_dialogs = []
    for dialog in dialogs1 + dialogs2:
        # 清理文本
        dialog = re.sub(r'\n\s*\n', '\n', dialog).strip()
        if dialog and dialog not in all_dialogs:
            all_dialogs.append(dialog)
    
    return '\n'.join(all_dialogs)


class ScriptExtractor:
    """口播文案提取器"""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.cache = {}

    def extract(self, prompt: str, ctx: Ctx) -> Tuple[Optional[str], Optional[str]]:
        """
        从提示词提取口播文案（本地提取，无需 API）
        """
        # 检查是否启用口播提取
        if not EXTRACT_SCRIPT_ENABLED:
            return None, None
        
        # 检查视频时长是否足够长（使用默认值，因为无法访问 main 的 current_duration）
        if DEFAULT_DURATION < MIN_DURATION_FOR_SCRIPT:
            # 5 秒模式不提取口播
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
