# AIGC 视频生成辅助系统 - 模块化版本

## 📁 项目结构

```
aigc/
├── core/                    # 核心模块
│   ├── __init__.py         # 核心模块导出
│   ├── config.py           # 全局配置（Excel、账号、参数等）
│   ├── api_client.py       # API 客户端封装
│   └── logger.py           # 日志系统
├── workers/                 # 工作线程
│   ├── __init__.py         # 工作线程导出
│   ├── submit.py           # 任务提交（负载均衡 + 背压）
│   ├── poll.py             # 状态轮询（每账号独立线程）
│   └── scan.py             # 任务扫描
├── processors/              # 处理器
│   ├── __init__.py         # 处理器导出
│   ├── video_processor.py  # 视频下载 + 重命名 + NAS 拷贝
│   └── script_extractor.py # ⭐ 口播文案提取器
├── utils/                   # 工具函数
│   ├── __init__.py         # 工具函数导出
│   └── excel_utils.py      # Excel 读写操作
├── registry/                # 注册中心
│   ├── __init__.py         # 注册中心导出
│   └── manager.py          # 任务注册 + 账号状态 + 负载均衡
├── console/                 # 控制台
│   ├── __init__.py         # 控制台导出
│   └── app.py              # 交互式命令行界面
├── outputs/                 # 输出目录（保持不变）
├── logs/                    # 日志目录（保持不变）
├── main.py                  # 程序入口
├── AIGC 辅助 excel.xlsx       # 任务表（保持不变）
└── *.png                    # 参考素材（保持不变）
```

## ✨ 新增功能

### 1. **口播文案提取**
- 在 Excel 中新增 `口播文案` 列
- 提交任务后自动从提示词提取口播文案
- 支持手动触发提取

### 2. **模块化架构**
- **core**: 核心功能（配置、API、日志）
- **workers**: 后台工作线程（提交、轮询、扫描）
- **processors**: 业务处理器（视频处理、脚本提取）
- **utils**: 通用工具（Excel 操作）
- **registry**: 服务注册与发现（任务管理、负载均衡）
- **console**: 用户交互界面

## 🎮 使用方法

### 启动程序
```bash
python main.py
```

### 控制台命令
```
> s / status        查看活跃任务
> h / health        查看账号健康 + 负载
> n / new / scan    扫描新项目（只列出）
> c / cancel        按序号取消任务
> run 4             跑第 4 行
> run 4,5,6         跑多行
> run new           扫描并跑所有新项目
> q / quit          退出
```

## 📊 Excel 列说明

| 列名 | 说明 | 来源 |
|------|------|------|
| 编号 | 任务序号 | 用户填写 |
| 品名 | 产品名称 | 用户填写 |
| 提示词 | AI 视频生成提示词 | 用户填写 |
| 脚本 | 备用脚本字段 | 用户填写 |
| 状态 | submitted/running/completed/failed | 脚本回写 |
| 账号 | acc1/acc2/acc3 | 脚本回写 |
| job_id | API 任务 ID | 脚本回写 |
| 输出 | 本地视频路径 | 脚本回写 |
| URL | 在线视频链接 | 脚本回写 |
| 运行次数 | 提交次数统计 | 脚本回写 |
| 成功次数 | 完成次数统计 | 脚本回写 |
| 取消次数 | 取消次数统计 | 脚本回写 |
| **口播文案** | **⭐从提示词提取的口播内容** | **脚本自动提取** |

## 🔧 配置说明

主要配置在 `core/config.py` 中：

- **视频参数**: MODE, DURATION, WIDTH, HEIGHT, SEED
- **账号配置**: ACCOUNTS (3 个并发账号)
- **NAS 路径**: NAS_ENABLED, NAS_DIR
- **提取 API**: 在 `core/api_client.py` 的 `extract_script()` 中配置

## 🚀 扩展建议

### 实现口播文案提取 API
在 `core/api_client.py` 中修改 `extract_script()` 函数：

```python
def extract_script(base, prompt):
    """调用 LLM API 或专门的脚本生成接口"""
    # 方案 1: 调用 Minimax 文本生成 API
    # 方案 2: 调用内部脚本生成服务
    # 方案 3: 使用其他大模型 API
    
    try:
        # 示例：调用自定义 API
        r = requests.post(f"{base}/scripts/extract",
                         json={"prompt": prompt},
                         timeout=60)
        r.raise_for_status()
        result = r.json()
        return result.get("script", ""), result.get("error")
    except Exception as e:
        return None, str(e)
```

### 添加更多处理器
在 `processors/` 目录下创建新的处理器类，例如：
- `thumbnail_generator.py` - 视频缩略图生成
- `video_editor.py` - 视频剪辑处理
- `metadata_extractor.py` - 视频元数据提取

## 📝 更新日志

### v2.0 (模块化重构)
- ✅ 代码模块化拆分
- ✅ 新增口播文案提取功能
- ✅ 优化导入结构和依赖关系
- ✅ 保持向后兼容

### v1.0 (初始版本)
- ✅ 基础任务提交功能
- ✅ 状态轮询机制
- ✅ 视频下载和重命名
- ✅ Excel 集成管理
