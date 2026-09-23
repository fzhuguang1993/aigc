"""
gui/formatting.py —— 展示层的小格式化工具（秒数这类），任务中心 / 执行记录 / 看板共用

放这里而不是各页各写一份：同一个「5分3秒」在三个页面写成三种样法，
使用者就会以为是三个不同口径的数。
"""


def secs(value):
    """秒数 → 展示文本：够 1 分钟折成「X分Y秒」；0 / 空 / 非法 → 「—」

    「—」而不是「0秒」：没跑过、和真的 0 秒，都不该显示成有数。"""
    try:
        d = int(value or 0)
    except (TypeError, ValueError):
        return "—"
    if d <= 0:
        return "—"
    return f"{d // 60}分{d % 60}秒" if d >= 60 else f"{d}秒"
