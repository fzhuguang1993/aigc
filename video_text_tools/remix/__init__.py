"""
video_text_tools/remix —— AI 混剪（MVP）

只做「选片段 + 排序 + ffmpeg concat 拼接导出」。'AI' 仅用于按板块/时长推荐片段顺序
（suggest_order 把各 clip 的 block_type/duration 交豆包出一个排列），**不生成画面**。
拼接复用 ffmpeg_utils.concat_clips；源规格不一致时自动走重编码兜底统一分辨率/帧率。
"""
