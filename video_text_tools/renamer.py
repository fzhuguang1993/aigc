# video_text_tools/renamer.py
"""批量重命名引擎（从 RenameWorker 提取，去除 Qt 依赖，改为回调式）

规则列表 pattern 中每条规则是一个 dict：
    {'type': '数字',     'start_num': 1, 'padding': 2}   # 01, 02, 03...
    {'type': '大写字母', 'start_num': 1}                  # A, B, C... AA...
    {'type': '小写字母', 'start_num': 1}                  # a, b, c...
    {'type': '罗马数字', 'start_num': 1}                  # I, II, III...
    {'type': '希腊字母', 'start_num': 1}                  # α, β, γ...
    {'type': '文本',     'text': 'video_'}                # 固定文本
    {'type': '原文件名'}                                   # 保留原文件名
    {'type': '扩展名'}                                    # 占位，构建时忽略
"""
import os
from typing import List, Optional


class RenameEngine:
    """规则驱动的批量重命名引擎，支持预览（dry-run）"""

    def __init__(self, file_paths: List[str], pattern: List[dict]):
        self.file_paths = file_paths
        self.pattern = pattern
        self._is_running = True

    def stop(self):
        """请求中断执行"""
        self._is_running = False

    # ------------------------------------------------------------------
    # 编号格式转换
    # ------------------------------------------------------------------
    @staticmethod
    def _to_letter(n: int, upper: bool = True) -> str:
        result = ""
        while n > 0:
            n -= 1
            result = chr((n % 26) + (ord('A') if upper else ord('a'))) + result
            n //= 26
        return result

    @staticmethod
    def _to_roman(n: int) -> str:
        roman_map = [(1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),
                     (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),
                     (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')]
        result = ""
        num = n
        for value, symbol in roman_map:
            while num >= value:
                result += symbol
                num -= value
        return result

    @staticmethod
    def _to_greek(n: int) -> str:
        greek = ['α', 'β', 'γ', 'δ', 'ε', 'ζ', 'η', 'θ', 'ι', 'κ', 'λ', 'μ',
                 'ν', 'ξ', 'ο', 'π', 'ρ', 'σ', 'τ', 'υ', 'φ', 'χ', 'ψ', 'ω']
        if n <= len(greek):
            return greek[n - 1]
        result = ""
        while n > 0:
            n -= 1
            result = greek[n % 24] + result
            n //= 24
        return result

    def _get_next_value(self, rule: dict, index: int) -> str:
        if rule['type'] == '数字':
            return str(rule['start_num'] + index).zfill(rule['padding'])
        elif rule['type'] == '大写字母':
            return self._to_letter(rule['start_num'] + index, upper=True)
        elif rule['type'] == '小写字母':
            return self._to_letter(rule['start_num'] + index, upper=False)
        elif rule['type'] == '罗马数字':
            return self._to_roman(rule['start_num'] + index)
        elif rule['type'] == '希腊字母':
            return self._to_greek(rule['start_num'] + index)
        return ""

    def build_name(self, index: int, ext: str, original_name: str = "") -> str:
        """按规则列表构建新文件名"""
        parts = []
        for rule in self.pattern:
            if rule['type'] == '扩展名':
                continue
            elif rule['type'] == '原文件名':
                parts.append(original_name if original_name else "{原文件名}")
            elif rule['type'] in ['数字', '大写字母', '小写字母', '罗马数字', '希腊字母']:
                parts.append(self._get_next_value(rule, index))
            else:
                parts.append(rule['text'])
        return ''.join(parts) + ext

    # ------------------------------------------------------------------
    # 预览与执行
    # ------------------------------------------------------------------
    def preview(self) -> List[dict]:
        """预览重命名结果（不实际改文件）"""
        results = []
        for idx, file_path in enumerate(self.file_paths):
            ext = os.path.splitext(file_path)[1]
            original_base = os.path.splitext(os.path.basename(file_path))[0]
            new_name = self.build_name(idx, ext, original_base)
            results.append({
                "old_path": file_path,
                "new_path": os.path.join(os.path.dirname(file_path), new_name),
                "old_name": os.path.basename(file_path),
                "new_name": new_name,
            })
        return results

    def execute(self, progress_callback=None) -> dict:
        """执行批量重命名

        :param progress_callback: callable(current, total, message)
        :return: {"total": n, "renamed": n, "failed": n, "results": [...]}
        """
        total = len(self.file_paths)
        renamed = 0
        failed = 0
        results: List[dict] = []

        for idx, file_path in enumerate(self.file_paths, 1):
            if not self._is_running:
                break

            if progress_callback:
                progress_callback(idx, total, os.path.basename(file_path))

            ext = os.path.splitext(file_path)[1]
            original_base = os.path.splitext(os.path.basename(file_path))[0]
            new_name = self.build_name(idx - 1, ext, original_base)
            new_path = os.path.join(os.path.dirname(file_path), new_name)

            result = {
                "old_path": file_path,
                "new_path": new_path,
                "old_name": os.path.basename(file_path),
                "new_name": new_name,
                "status": "skipped",
                "reason": "",
            }

            if os.path.basename(file_path) == new_name:
                renamed += 1
                result["status"] = "skipped"
                result["reason"] = "文件名未变化"
                results.append(result)
                continue

            if os.path.exists(new_path):
                failed += 1
                result["status"] = "failed"
                result["reason"] = "目标文件已存在"
                results.append(result)
                continue

            try:
                os.rename(file_path, new_path)
                renamed += 1
                result["status"] = "success"
            except Exception as e:
                failed += 1
                result["status"] = "failed"
                result["reason"] = str(e)

            results.append(result)

        return {
            "total": total,
            "renamed": renamed,
            "failed": failed,
            "results": results,
            "pattern": self.pattern,
        }
