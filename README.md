# AIGC 视频生成助手 —— 新手教程

一个 Excel 驱动的批量 AI 短视频生成工具：在 Excel 里填好提示词，程序自动提交到云端 GPU 服务生成视频，生成完自动下载、按规则重命名，保存在本地。支持多账号负载均衡、故障自动转移、断点续跑。

---

## 一、两种使用方式，对号入座

| 你是谁 | 看哪节 |
|---|---|
| 🧑‍💼 只使用（同事拿 exe 用） | [快速上手](#二快速上手5分钟) |
| 🧑‍💻 要改代码 / 自己打包 | [开发者指南](#五开发者指南) |

---

## 二、快速上手（5分钟）

### 1. 准备文件夹

拿到程序后，确认文件夹里有这三样：

```
你的文件夹/
├── AIGC视频助手.exe        ← 程序（或 main.py + 源码）
├── AIGC辅助excel.xlsx      ← 任务表（要填的就是它）
└── material/               ← 参考图片素材
```

### 2. 首次运行 → 配置向导

双击运行，第一次会进入配置向导：

```
1) 请输入你的姓名（用于生成视频文件命名）: 张三
2) 请输入 API 服务地址（可以有多个，逐个输入，输入空行结束）
   接口 1（空行完成）: http://106.75.1.98:7860
   接口 2（空行完成）: https://xxx.pod.compshare.cn
   接口 3（空行完成）: （直接回车）
✓ 配置完成！已保存: config.json
```

- 接口有几个填几个，**至少 1 个**；填完自动做连通检测
- 配置保存在运行目录的 `config.json`，**以后启动不再询问**
- 想重新配置：删除 `config.json` 再启动即可

### 3. 填写 Excel 任务表

打开 `AIGC辅助excel.xlsx` 的 `Sheet` 工作表，每行一个任务：

| 编号 | 品名 | 提示词 |
|---|---|---|
| 1 | 诺特兰德益生菌 | 一位男明星手持益生菌产品，对镜头口播推荐… |
| 2 | 诺特兰德VB | … |

- **编号**：数字，用于文件命名和指定运行
- **品名**：用于文件命名，同时对应 `material/品名.png` 参考图（有图则自动作为商品参考）
- **提示词**：填好后状态列留空，程序就把它视为"待执行任务"

### 4. 执行任务

进入交互界面后（日常只有两个命令）：

```
dd     ← 随时呼出菜单（推荐，全中文引导）
q      ← 退出
```

按 `dd` 后输入编号选择，常用的有：

- **批量执行**：输入范围如 `1-10`，排队跑完自动下载
- **run 命令**：直接输入 `run 1-10` 执行指定行
- **切换时长模式**：5 秒（快速测试）/ 15 秒（成片）

### 5. 看结果

视频自动保存在运行目录：

```
outputs/
└── 0920/                              ← 按日期
    └── 001_诺特兰德益生菌_0920_01_张三.mp4   ← 编号_品名_日期_序号_姓名
```

Excel 中的 `状态`、`输出`、`URL`、`成功次数` 等列会实时自动回写。

---

## 三、运行机制一览

```
Excel 扫描新任务 → 空闲账号自动提交（负载均衡）→ 云端生成
       ↑                                        ↓
  结果回写 Excel  ←  下载+重命名到 outputs/  ←  轮询状态
```

- **多账号**：一个账号忙/挂了自动换下一个，无需干预
- **断点续跑**：程序重启后自动接续未完成的任务
- **口播文案**：生成的视频时长 ≥10 秒时自动提取口播文案回写 Excel

## 四、常见问题

| 问题 | 解决 |
|---|---|
| 提示"至少需要配置一个接口地址" | 首运向导至少要填 1 个 API 地址 |
| 接口检测"暂时无法连通" | 检查服务是否已启动/网络，不影响保存配置，可稍后再试 |
| 任务不执行 | 确认"提示词"列非空、"状态"列为空、运行次数为 0 |
| 想换姓名 | 删除 `config.json` 重启，走一遍向导 |
| 双击 exe 闪退 | 用命令行启动看报错：`.\AIGC视频助手.exe` |

---

## 五、开发者指南

### 环境运行

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

开发模式下若无 `config.json`，会自动回退读取 `core/config_local.py`（从 `config_local.example.py` 复制并填写）。

### 目录结构

```
├── main.py              # 入口：首运向导 → 健康检查 → 轮询线程 → 交互控制台
├── core/                # config（路径/参数/账号）、api_client（统一 ApiError）、logger、setup_wizard（首运向导）
├── store/               # SQLite 持久化：db + task_store / product_store / risk_store
├── workers/             # scan 扫描 / submit 提交（SubmitOptions+退避换线重试）/ poll 轮询
├── processors/          # video_processor 下载重命名 / script_extractor 口播提取
├── registry/            # 任务注册 + 账号健康 + 负载均衡
├── utils/               # excel_utils 读写
├── console/             # app.py 交互控制台（dd 菜单）
├── gui/                 # PySide6 桌面界面（与 console 共用同一套内核）
├── tests/               # pytest 回归测试（主链路断言，改内核必跑）
└── docs/                # 设计文档（含 design-conventions.md 架构约定与回退规则）
```

### 关键约定

- 所有数据路径基于**运行目录**（可用 `AIGC_HOME` 环境变量覆盖，默认 `Path.cwd()`），打包分发零改路径
- `config.json` 加载优先级：运行目录 `config.json` → `core/config_local.py`
- `core/__init__.py` 保持为空导入（避免在向导生成配置前缓存空配置）
- 敏感信息（接口地址）只存在本地 `config.json` / `config_local.py`，均已 gitignore
- 提交选项通过 `SubmitOptions` 显式传参，worker 层禁止模块级可变全局态
- 全部回退（fallback）链路与错误处理约定见 [docs/design-conventions.md](docs/design-conventions.md)

### 跑回归测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q        # 改动提交/轮询/账号选择主链路后必跑，全绿再提交
```

### 打包成 exe（发给同事）

> PyInstaller 不支持跨平台：Windows 的 exe 必须在 Windows 上打。

```bat
:: Windows 上，项目根目录双击或命令行执行
build.bat
:: 产物：dist\AIGC视频助手.exe
```

分发清单：`exe + AIGC辅助excel.xlsx + material/`，同事双击运行即进入首次配置向导。
