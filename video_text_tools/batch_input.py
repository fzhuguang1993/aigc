# video_text_tools/batch_input.py
"""批量粘贴录入核心逻辑（从 BatchInputTool / BatchPasteWorker 提取，无 UI 依赖）

依赖: pyautogui、pyperclip
"""
import platform
import time

import pyautogui
import pyperclip


def get_mod_key() -> str:
    """获取系统修饰键：macOS=command，其他=ctrl"""
    return 'command' if platform.system() == "Darwin" else 'ctrl'


def read_clipboard_lines() -> list:
    """读取剪贴板并过滤空行，返回待录入文本列表"""
    raw_lines = pyperclip.paste().splitlines()
    return [s for s in (line.strip() for line in raw_lines) if s]


class BatchInput:
    """逐行自动粘贴录入：清空输入框 -> 粘贴 -> 回车"""

    DEFAULT_CONFIG = {
        "delay_select": 0.08,   # 全选后延迟
        "delay_delete": 0.06,   # 删除后延迟
        "delay_paste": 0.06,    # 粘贴后延迟
        "delay_enter": 0.09,    # 回车后延迟
        "clear_input": True,    # 每行录入前是否清空输入框
        "countdown_sec": 3,     # 启动前倒计时秒数（留给用户切换窗口）
    }

    def __init__(self, config: dict = None):
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self._is_running = True

    def stop(self):
        """请求中断录入"""
        self._is_running = False

    def countdown(self, on_tick=None):
        """阻塞式倒计时，on_tick(remaining) 每秒回调一次"""
        sec = self.config["countdown_sec"]
        for remaining in range(sec - 1, -1, -1):
            if not self._is_running:
                return False
            if on_tick:
                on_tick(remaining)
            time.sleep(1)
        return self._is_running

    def run(
        self,
        lines: list,
        progress_callback=None,
        should_stop=None,
    ) -> dict:
        """执行批量录入

        :param lines: 待录入的文本行列表
        :param progress_callback: callable(current, total, text)
        :param should_stop: callable() -> bool，额外中断条件
        :return: {"total": n, "input": n, "interrupted": bool}
        """
        stopped = should_stop or (lambda: False)
        mod = get_mod_key()
        total = len(lines)
        count = 0

        for idx, text in enumerate(lines, 1):
            if not self._is_running or stopped():
                return {"total": total, "input": count, "interrupted": True}

            if progress_callback:
                progress_callback(idx, total, text)

            # 清空输入框
            if self.config["clear_input"]:
                pyautogui.hotkey(mod, "a")
                time.sleep(self.config["delay_select"])
                pyautogui.press("delete")
                time.sleep(self.config["delay_delete"])

            # 备份剪贴板，避免原始内容丢失
            backup_clip = pyperclip.paste()
            pyperclip.copy(text)
            pyautogui.hotkey(mod, "v")
            pyperclip.copy(backup_clip)

            time.sleep(self.config["delay_paste"])
            pyautogui.press("enter")
            time.sleep(self.config["delay_enter"])

            count += 1

        return {"total": total, "input": count, "interrupted": False}
