# AIGC 视频工厂 · 桌面版

> 一个面向短视频量产团队的 **Windows 桌面套件**：以「Excel 填提示词 → 云端 GPU 批量生成视频」为生产主线，
> 围绕它长出了一整套**素材工坊、AI 混剪、爆款拆解、数据中台**，并顺手把**截图 / 录屏 / 剪贴板 /
> 空格预览 / 软件卸载**等一堆高频桌面效率工具收进同一个「工具中心」。商用授权、试用限时、崩溃留痕、
> 单实例托盘常驻一应俱全——既是给非技术同事「拿 exe 就能用」的成品，也是给开发者可扩展的框架。

- 🧭 **双入口**：`desktop.py`（PySide6 GUI）与 `main.py`（命令行控制台）共用同一套内核
- 🏭 **量产主线**：Excel 驱动 · 多账号负载均衡 · 故障自动转移 · 断点续跑 · 自动命名下载
- 🧰 **工具中心**：16 个可插拔小工具，全局热键一键呼出，最小化到托盘也能用
- 🔐 **商用就绪**：网关发卡激活 + 账号盐值加密 + 试用限时锁定 + 三级组织数据权限

---

## 一、项目亮点

### 1. 一条完整的 AIGC 短视频量产链路

```
Excel 任务表 ──扫描──▶ 空闲账号提交 ──负载均衡──▶ 云端 GPU 生成
      ▲                                                    │
      │                                                    ▼
  结果实时回写 ◀──下载+按规则重命名◀──────────────────── 轮询状态
```

- **多账号调度**：一个账号忙 / 挂掉自动换下一条线路，退避重试，无需人工干预
- **断点续跑**：进程重启后自动接续未完成的任务，绝不重复计费
- **自动命名归档**：`编号_品名_日期_序号_姓名.mp4` 落到按日期分层的 `outputs/`
- **口播提取**：成片时长达标时自动抽取口播文案回写 Excel

### 2. 素材工坊：从「爆款」到「成品」的闭环

| 模块 | 能力 |
|---|---|
| 🔥 **爆款拆解** | 粘贴爆款链接 → 拆分镜 / 口播 → 产出「画面 / 文案 / 复刻」三类提示词 + 整体分析，交付 Word 报告 |
| 🧲 **素材提取** | 抖音 / 快手分享链接 → 去水印视频、图集、文案一键入库 |
| 🎙 **语音识别** | Whisper 转口播逐字稿，导出 TXT / SRT，DeepSeek 纠错，可 ASS 烧录 |
| ✂️ **AI 混剪** | 双模式拼装（模板 / 自由），板块切割片段进素材库，成品库集中审阅 |
| 🗂 **素材库 / 成品库** | 扫盘 + 结构化标签 + 组织树 + 软件内回收站 + 多选批量（绑定 / 删除 / 重命名） |

### 3. 一个「装了就在系统里活着」的桌面效率层

- 🪄 **悬浮球唤出**：移入展开 / 移出收起，球面实时网速，扇形放大工具浮窗
- ⌨️ **全局热键**（`RegisterHotKey` 系统级，焦点在别的软件里也能一键弹工具）：
  录屏 `Ctrl+Alt+R` · 截图 `Ctrl+Alt+A` · 剪贴板快贴 `` Ctrl+` `` · 总唤出面板 `Ctrl+Alt+Space` · 切换窗口大小 `Ctrl+Alt+E`
- 👁 **QuickLook 空格预览**：在资源管理器 / 桌面选中文件按空格即预览（docx / pdf / xls / 图片 / 视频首帧）
- 🖱 **右键入库**：资源管理器菜单直接把素材 / 成品送进程序（跨进程单实例接管队列）
- ✂️ **截图标注**：区域 / 全屏 / 窗口抓图 + 框线·箭头·画笔·文字·马赛克，可钉住悬浮 / 复制 / 保存
- 📋 **剪贴板历史**：后台自动记录文字与图片，分类回看、一键回贴
- 🧹 **软件卸载清理**：检测已装软件 → 官方 / 静默卸载 → 扫描并清理注册表与文件残留（逐条确认、进回收站、可恢复），支持**批量保守自动卸载**

### 4. 数据中台（BI 看板）

- 多维度统计（执行次数 / 用时 / 成功率 / 账号负载），现代仪表盘配色
- 下钻明细改为**弹窗展示**：点段看局部、点空白看全部
- 支持从 Excel 导入任务、向 Excel / CSV / JSON 导出全部记录

### 5. 商用化与工程健壮性

- 🔑 **双运行模式**：开发直连 / 商用网关（可插拔适配器、发卡激活、账号密码登录 + 盐值加密）
- ⏳ **试用限时锁定**：首次运行起 N 小时自然日历，`trial.json` + 注册表双锚点防篡改，改系统时间也不能续命
- 🏢 **三级组织与数据权限**：客户 / 执照 / 账户，按角色圈定可见范围
- 🛡 **崩溃留痕**：native 层崩溃也能在 `logs/crash.log` 留下 Python 栈与 Qt 原话
-  **统一视觉工程**：无边框圆角窗体逐像素透明绘制、全站 UI 令牌单点收口、QMessageBox 圆角统一
- 📦 **三产物打包**：精简版 / 创作版 / 命令行（PyInstaller）+ Inno Setup 安装器 + 分模块可选安装

---

## 二、技术架构

### 分层总览

```
┌─────────────────────────────────────────────────────────────┐
│  入口层    desktop.py (GUI)          main.py (命令行控制台)   │
│            ├ 单实例守护 · 崩溃留痕 · EULA · 激活/试用校验      │
├─────────────────────────────────────────────────────────────┤
│  界面层    gui/  PySide6                                    │
│            main_window(导航/页栈) · pages_* · dialogs_*       │
│            tool_panels(PANEL_FACTORIES) · tools_registry     │
│            系统级：quicklook / screenshot / clipboard /       │
│                    launcher_ball / global_hotkey             │
│            视觉：theme · ui_kit(令牌) · kit(组件库) · 圆角窗体 │
├─────────────────────────────────────────────────────────────┤
│  内核层    workers(扫描/提交/轮询) · processors(下载/口播) ·   │
│            registry(账号健康 + 负载均衡) · video_text_tools    │
│            (ocr / screen_recorder / software_manager …)       │
├─────────────────────────────────────────────────────────────┤
│  数据层    store/  SQLite 持久化                              │
│            db · task_store · product_store · output_store ·   │
│            material_store · clipboard_store · app_state …     │
├─────────────────────────────────────────────────────────────┤
│  基础层    core/  config · paths · crashdump · trial ·        │
│            single_instance · shell_integration · modules      │
│            utils(excel) · console · server(商用网关 ASGI)      │
└─────────────────────────────────────────────────────────────┘
```

### 目录结构

```
├── desktop.py            # GUI 入口：单实例→主题→EULA→激活→建库→起轮询线程→主窗
├── main.py               # 命令行入口：首运向导→健康检查→轮询→交互控制台
├── core/                 # 配置/路径/崩溃留痕/试用锁/单实例/右键入库/分模块安装
├── workers/              # scan 扫描 · submit 提交(SubmitOptions+退避换线) · poll 轮询
├── processors/           # video_processor 下载重命名 · script_extractor 口播提取
├── registry/             # 任务注册 + 账号健康 + 负载均衡调度
├── store/                # SQLite 持久化（db + 各 store + app_state）
├── gui/                  # PySide6 桌面界面（pages_* / dialogs_* / 系统级工具 / 视觉层）
├── video_text_tools/     # 无 Qt 的功能内核：ocr · screen_recorder · software_manager …
├── console/              # 交互控制台（dd 菜单）
├── server/               # 商用网关 ASGI 入口与本地商用模式
├── utils/                # excel 读写等通用工具
├── installer/            # Inno Setup 脚本（.iss）
├── tests/                # pytest 回归测试（改内核主链路必跑）
└── docs/                 # 设计文档与新用户指南
```

### 关键设计约定

- **GUI 与内核解耦**：功能逻辑（卸载、录制、OCR、提交/轮询）沉到无 Qt 的
  `video_text_tools/` 与 `workers/`，界面只做装配与线程调度，两侧可分别测试。
- **耗时/高危动作走 `ToolWorker(QThread)`**：UI 只收 `log/progress/done` 信号，绝不卡界面。
- **工具中心两步接入**：① 在 `tool_panels.PANEL_FACTORIES` 登记工厂；② 在 `tools_registry.TOOLS` 加一条卡片。
  纯数据层 `tools_registry` 被「工具中心」与「设置页」共用，从根上消除模块级循环依赖。
- **所有数据路径基于「运行目录」**：打包版＝exe 所在目录，开发版＝仓库根（可用 `AIGC_HOME` 覆盖）；
  故意不用 `Path.cwd()`，避免换目录启动另建空库。
- **敏感信息只落本地**：接口地址、密钥存 `config.json` / `core/config_local.py`，均已 gitignore。
- **提交选项通过 `SubmitOptions` 显式传参**，worker 层禁止模块级可变全局态。
- 全部回退（fallback）链路与错误处理约定见 [docs/design-conventions.md](docs/design-conventions.md)。

---

## 三、技术栈

| 领域 | 选型 |
|---|---|
| 语言 / 运行时 | Python 3.9+（开发用 3.12） |
| GUI | PySide6（Qt 6） |
| 数据 | SQLite（持久化）· pandas / openpyxl / xlrd（Excel） |
| 网络 / AI | requests · volcengine（豆包 / 方舟）· Chat Completions |
| 多媒体 | FFmpeg（内置二进制：录制 / gdigrab / 转码 / ASS 烧录）· Whisper（ASR） |
| 文档预览 | pypdf · mammoth（docx→HTML）· QTextBrowser 富文本页 |
| OCR | RapidOCR（可选，见 `requirements-ocr.txt`） |
| Windows 集成 | ctypes（ShellExecute / 热键 / 回收站）· comtypes（右键菜单 / 资源管理器） |
| 安全 | cryptography（盐值加密）· psutil |
| 打包 | PyInstaller（三产物）· Inno Setup（安装器） |

---

## 四、快速开始

### 🧑‍💼 只使用（拿 exe）

1. 双击 `AIGC视频助手.exe`，首次进入配置向导（打包机已内置接口线路，通常只需填姓名）
2. 填 `AIGC辅助excel.xlsx`：每行一个任务（编号 / 品名 / 提示词）
3. 交互界面输入 `dd` 呼出全中文菜单，选「批量执行」并给范围（如 `1-10`）
4. 视频自动下载到 `outputs/<日期>/`，Excel 状态列实时回写

> 配置存于隐藏配置家 `%APPDATA%\AIGC视频助手\config.json`；想重配删掉它再启动即可。

### 🧑‍💻 开发 / 自己打包

```bash
# 1) 环境
python -m venv .venv
# Windows: .venv\Scripts\activate     Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
# 可选：OCR 能力   pip install -r requirements-ocr.txt
# 可选：爆款拆解   pip install -r requirements-breakdown.txt

# 2) 运行（二选一）
python desktop.py     # GUI 桌面版
python main.py        # 命令行控制台

# 3) 回归测试（改内核主链路后必跑，全绿再提交）
pip install -r requirements-dev.txt
python -m pytest -q
```

开发模式下若无 `config.json`，会自动回退读取 `core/config_local.py`
（从 `config_local.example.py` 复制并填写）。

### 📦 打包成 exe（发给同事）

> PyInstaller 不支持跨平台：Windows 的 exe 必须在 Windows 上打。

```bat
:: 项目根目录执行，产出 dist\ 下的精简版 / 创作版 / 命令行三产物
build.bat
```

分发清单：`exe + AIGC辅助excel.xlsx + material/`，同事双击即进入首次配置向导。

---

## 五、常见问题

| 问题 | 解决 |
|---|---|
| 提示「至少需要配置一个接口地址」 | 首运向导至少填 1 个 API 地址 |
| 日志报 `port=80` 超时 / `Max retries` | 接口地址漏端口，改为 `http://<IP>:7860` 后重启 |
| 任务不执行 | 确认「提示词」列非空、「状态」列为空、运行次数为 0 |
| 双击 exe 闪退 | 用命令行启动看报错：`.\AIGC视频助手.exe` |
| 想换姓名 | 删除 `config.json` 重启走一遍向导 |

更多上手细节见 [docs/新用户指南.md](docs/新用户指南.md)。

---

<p align="center">如果这个项目帮到了你，欢迎 ⭐ Star 支持。</p>
