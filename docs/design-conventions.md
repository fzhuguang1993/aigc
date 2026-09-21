# 架构约定与回退（Fallback）规则

> 本文档是改动 `core / store / workers / registry / gui / console` 前的必读约定。
> 所有"优先 A、失败退到 B"的隐式回退链在此集中登记，代码中新增回退必须同步更新本文档。

## 1. 分层与依赖方向

```
core(配置/日志/HTTP) → store(SQLite 持久化) → workers(提交/轮询/扫描)
                                  ↘ registry(运行时任务表/账号状态)
                                                ↘ gui / console(两套表现层)
```

- 依赖只能单向向下，表现层（gui/console）之间互不引用，共用同一套 workers/store 内核。
- `core/api_client.py` 保持纯 HTTP 薄封装，不掺业务逻辑。

## 2. 错误处理约定（api_client）

- 所有公开函数**成功返回解析后的结果**（dict / list / str），
  **失败统一抛 `ApiError`**（携带 `status_code`，网络层异常为 None）。
- 禁止再引入"返回 (resp, err) 元组"的第二套风格。
- 调用方策略：
  - 提交链路（`workers.submit`）：捕获 `ApiError` → 决定退避/换线重试；
  - 轮询链路（`workers.poll`）：查询异常静默降级（下个周期重试），下载/处理异常回写 `error` 状态；
  - 表现层：给用户可见的报错提示。

## 3. 提交选项传递（无全局可变状态）

- 时长 / KOL 等提交选项一律封装为 `workers.submit.SubmitOptions` **不可变快照**，
  由表现层在**提交瞬间**构建并显式传给 `do_submit()`。
- worker 层（workers/registry/store）禁止新增模块级可变全局变量跨层传参。
- 提交时的 `duration` 会随任务写入 `registry.REG`，轮询侧从任务记录读取
  （而不是读任何"当前全局模式"），保证先发后改互串不影响在跑任务。
- `do_submit(..., _sleep=...)` 的 `_sleep` 仅供测试注入退避节奏，业务代码勿传。

## 4. 回退（Fallback）规则总表

| # | 场景 | 优先 | 回退 | 实现位置 |
|---|------|------|------|----------|
| 1 | 运行时目录 | 环境变量 `AIGC_HOME` | 当前工作目录 `Path.cwd()` | `core/config.py: RUNTIME_DIR` |
| 2 | 用户配置 | 运行目录 `config.json`（首运向导生成） | `core/config_local.py`（开发环境） | `core/config.py: _load_local_config` |
| 3 | 文件命名姓名 | `config.json` 的 `user_name` | Excel「命名规则」sheet | `utils/excel_utils.py: load_name_rule` |
| 4 | 参考图 | 产品中心该品名登记图片 | 无（为空则不带参考图提交） | `workers/submit.py: build_payload` |
| 5 | KOL 形象图 | 产品中心登记 | `material/KOL` 目录 → 都找不到则忽略并告警 | `workers/submit.py: build_payload` + `store/product_store.py: kol_image` |
| 6 | 账号负载统计 | 云端 `list_jobs` 实时数 | 本地 `REG.count_active_by_account`（接口不可达时） | `registry/manager.py: get_account_load` |
| 7 | 账号选择 | 健康账号中负载最低 | 全部不可用时选 `fail_count` 最小的 | `registry/manager.py: pick_account` |
| 8 | 提交重试换线 | 其他健康账号（换线 + 指数退避 2/4/8s，封顶 10s） | 无备选账号时原账号退避重试 | `workers/submit.py: _next_account / do_submit` |
| 9 | 口播文案提取 | 任务时长 ≥ `MIN_DURATION_FOR_SCRIPT` 才提取 | 提取失败静默（不影响主流程） | `workers/poll.py` |
| 10 | 账号健康标记 | `health` 连续成功 | 连续失败 ≥3 次标记不可用，监控线程自动恢复 | `registry/manager.py: health_monitor_worker` |

## 5. 并发与资源约定

- 每个账号一个 `Semaphore(concurrency)` 做提交背压；`do_submit` 全程持有，
  **换账号时先释放旧信号量再获取新信号量**（历史 bug：曾释放错账号信号量，已由
  `tests/test_submit.py::test_switches_account_with_backoff` 锁死）。
- 轮询线程每账号一个（daemon），终态（completed/failed/cancelled）必须 `REG.remove`。
- SQLite 写入统一走 `store/db.py`（RLock + timeout=30），支持 GUI/控制台双进程并发写。

## 6. 测试约定

- 回归测试放 `tests/`（pytest），批量手测脚本留在 `test/`（不计入回归）。
- `tests/conftest.py` 通过 `AIGC_HOME` 把 SQLite/日志/输出隔离到临时目录；
  涉及网络的依赖一律 monkeypatch `workers.submit` / `core.api_client` 命名空间。
- 改动提交/轮询/账号选择主链路后，必须跑 `python -m pytest -q` 全绿再提交。
- 新增主链路行为时同步补断言，不写"只打印不 assert"的回归用例。
