"""核心模块

注意：不要在这里 import 子模块（如 config），
否则会在首次运行向导生成 config.json 之前就把配置缓存为空，
导致分发场景下配置读取失败。各模块请显式使用
`from core.config import ...` 方式导入。
"""
