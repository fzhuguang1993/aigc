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
- **payload 字段名以云端 `GET {base}/openapi.json` 为准**，且请求体是
  `additionalProperties: false` 的严格模型——多一个字段不是被忽略，而是整单 422。
  字段清单与核对脚本见 `docs/云端接口字段说明.md` + `test/check_payload_fields.py`。
- `do_submit(..., _sleep=...)` 的 `_sleep` 仅供测试注入退避节奏，业务代码勿传。

## 4. 回退（Fallback）规则总表

| # | 场景 | 优先 | 回退 | 实现位置 |
|---|------|------|------|----------|
| 1 | 运行时目录（DB/日志/输出/ui_state 落地处） | 环境变量 `AIGC_HOME` | 打包版＝**exe 所在目录**，开发版＝**仓库根**（`_runtime_dir()`）。⚠ 绝不能用 `Path.cwd()`：cwd 是「从哪个目录启动」而不是「软件在哪」，换目录启动会另建一份空 `data/aigc.db`，看板从零开始（内测现场就是被这个误判成「统计不持久化」）。（口径单实在 `core/paths.py`，因为首次运行向导要赶在 config 加载前算出目录，不能 `from core.config import`） | `core/paths.py: RUNTIME_DIR` → `core/config.py` |
| 2 | 用户配置 | 配置家 `config.json`（首运向导/首配弹窗生成） | **exe 内置线路**（维护机 `core/config_local.py` 的 `ACCOUNTS`，打包时编进去）→ 首次只问姓名；两者都没有才回退出“自己粘地址”。⚠ `config.json` 里 **没 `accounts` 键也算这种回退**（开发机首配只存姓名，改 `config_local.py` 要能立刻生效） | `core/config.py: _load_local_config/defaults_accounts` |
| 2b | 账号 base 后缀 | 已是 `…/api/v1` 原样使用 | 缺失时加载端自动补齐（手写 config.json 漏后缀会 POST 打根路径返 405） | `core/config.py: _normalize_account_base` |
| 3 | 文件命名姓名 | `config.json` 的 `user_name` | Excel「命名规则」sheet | `utils/excel_utils.py: load_name_rule` |
| 4 | 参考图 | 产品中心该品名登记图片 | 无（为空则不带参考图提交） | `workers/submit.py: build_payload` |
| 5 | KOL 形象图 | 产品中心登记 | `material/KOL` 目录 → 都找不到则忽略并告警 | `workers/submit.py: build_payload` + `store/product_store.py: kol_image` |
| 6 | 账号负载统计 | 云端 `list_jobs` 实时数（取 `max(云端, 本机在途)`，防提交到入队几秒延迟期内被自己灌穿） | 接口不可达时降级为 `REG.count_active_by_account`，**首次降级必告警**，UI 标「⚠仅本机」 | `registry/manager.py: measure_load` |
| 7 | 账号选择 | 健康线路中负载最低，且**负载相近（差 ≤`LOAD_TIE_BAND`）的随机挑一条** | 全部不可用时在 `fail_count` **最小的那一整档**之间摊开（`schedulable_accounts()`，见第 34 行）；绝不能只留一条 | `registry/manager.py: pick_account/schedulable_accounts` |
| 8 | 提交重试换线 | 其他健康账号中**负载最低的**（`pick_account(exclude=当前)`，换线 + 指数退避 2/4/8s，封顶 10s） | 无备选账号时原账号退避重试 | `workers/submit.py: _next_account / do_submit` |
| 9 | 口播文案/分镜数自动识别 | 提交链路（执行时）字段为空且能从提示词识别 → 自动补齐（不限时长；口播已有/分镜已识别则不覆盖） | 识别不出留空，规范检测退而用提示词；失败静默不影响提交 | `workers/submit.py: do_submit` + `processors/script_extractor.py: ensure_script_fields` |
| 10 | 账号健康标记 | **启动立即真实探活一轮**（`check_all_accounts` 先检后睡），一轮内 `HEALTH_ATTEMPTS=2` 次全败才计一次失败 | 连续失败 ≥`HEALTH_FAIL_THRESHOLD(3)` 轮才标不可用（防网络闪断误杀），监控线程自动恢复并清零；`healthy` 默认 True 只是“未测先当可用”，展示层靠 `first_check_done()` 先显示「检测中/⚪ 待检测」而不是绿灯；侧栏灯可点击手动重测 | `registry/manager.py: check_all_accounts/health_monitor_worker` + `gui/main_window.py: _recheck_health` |
| 11 | 下载文件名防撞 | `next_seq()` 扫描已有文件取最大序号 +1 | 序号仍撞名（强制重跑/抽卡时同任务并发多 job）则线性探测下一个可用序号 | `processors/video_processor.py: process_outputs` |
| 12 | 已完成任务重跑 | **先查还有没有执行在跑**（`inflight_execs`，见第 36 行）：在跑的不归“强制重跑”那档；没在跑的若提示词在上次执行后改过 → 自动识别为「迭代执行」，无需确认 | 没改过 → 弹窗确认是否「强制重跑」，单条时可选抽卡次数（同提示词并发多 job） | `store/task_store.py: iter_ready` + `gui/dialogs.py: ForceRerunDialog` + `gui/pages_tasks.py: _run` |
| 13 | 任务编辑重置执行态 | 提示词变了 → 重置状态/运行次数（否则永远进不了待办） | 只改品名/脚本 → 不动执行态，不影响迭代/重跑判定 | `gui/pages_tasks.py: _edit_current` |
| 14 | 生成步数（1-50） | GUI 工具栏「步数」下拉取值，随 SubmitOptions 快照透传 payload.**inference_steps**（写成 `steps` 会被云端整单拒绝） | 跨入口越界值自动限幅到 1-50，缺省 `DEFAULT_STEPS=8`；批量取消多任务只弹一次确认 | `workers/submit.py: SubmitOptions/build_payload` + `gui/pages_tasks.py: _cancel_selected` |
| 15 | 执行用时口径 | 三个数各存各的：`duration`＝提交→完成（含排队）、`queued_sec`＝提交→云端开始跑、`gen_sec`＝开始跑→出片；终态检测时先刻录 finished_at | 下载成品/补录产物排在打点之后，不污染用时；任务表列优先最近一次 completed，无则取最近终态 | `workers/poll.py: _finalize_completed` + `store/task_store.py: record_run_end/attach_run_result` |
| 16 | 素材提取直链安全与配置 | 接口返回的直链只认 `api_text/{video,image}.txt` 域名白名单；白名单**不随代码分发**，使用者首次用工具时导入（缺失弹引导）；接口 **UID/Key 界面可改**（存 `api_text/api_config.json` 覆盖程序默认），**接口地址默认不暴露**：内置值只存维护人的 `core/config_local.py`（不入库），打包版若没内置，**维护人在本页键盘盲输口令（`API_MAINTAINER_CODE`）唤出密文地址框**现场填写（无需重打包，保存后地址框自动收起、下次须重新输口令）；`save_api_config` 地址传 None 保留旧值 | 仍未导入齐则不启动执行；json 损坏默默回退默认；每次执行重读名单 | `video_text_tools/material_extract.py` + `gui/tool_panels.py: MaterialPanel` |
| 17 | 文案样本库 | 提取到文案时除单条 txt 外，追加一行到同目录 `文案样本库.csv`（提取时间/平台/标题/链接/文案），供后期喂大模型学写脚本 | 同链接不重复入库（幂等）；utf-8-sig 保证 Excel 不乱码；写失败（文件被占）只告警不阻断本次提取 | `video_text_tools/material_extract.py: append_corpus` |
| 18 | 一批任务的「聚集定位」 | 搜索替换命中（或只定位不替换）/ Excel 导入（含提示词已存在被跳过的旧任务）/ 扫描新任务 → 统一走 `_gather_matches`：全部勾选 + 置顶聚集 + 分页档位自动抬到装得下 | 只抬档位不降级；`_user_sort_col is None` 时不调 `sortItems`（否则默认按ID升序会把刚置顶的新一批换回页底）；手动勾选不改行序；点列头/「清除筛选」让位 | `gui/pages_tasks.py: _gather_matches` + `store/task_store.py: import_from_excel` |
| 19 | 任务状态是否「还在占用线路」 | **排除终态**（`is_active`：completed/succeeded/failed/error/cancelled/timeout…），云端排队态叫什么都能算进去 | 状态为空/未知时保守当作仍占用（宁可多跟不丢跟踪）。绝不能枚举活跃态：旧实现只认 queued/running/starting，云端返回 pending 时负载恒为 0，且轮询永久失去该任务 | `registry/manager.py: is_active` |
| 20 | 多条线同样空时选哪条 | 负载差 ≤`LOAD_TIE_BAND` 的线归为同一池，**随机挑一条** | 池只有一个候选时就选它；平局取首（旧 `min()` 行为）已禁用——多台配置相同的电脑会一起把一条线灌爆 | `registry/manager.py: pick_account` |
| 21 | 线路额度已满时是否继续提交 | 选线与闸门合在 `pick_and_wait`：选一条**真有空位**的线（不等“刚选中的那条”，并发数配得不一样时死等会把活耗在满载线上）；全满则每 `GATE_POLL_INTERVAL` 秒重查（等完一轮必 `invalidate_load_cache`，否则 TTL 旧读数会晚一轮发现空位），上限 `GATE_WAIT_TIMEOUT` | 等超时仍要提交（宁超发不丢任务）并留告警；`config.json` 写 `"submit_gate": false` 可整体关掉闸门 | `registry/manager.py: pick_and_wait` |
| 22 | 云端返回 4xx（参数被拒） | 先解析 422 报文里的 `extra_forbidden` 字段路径，**摘掉被拒字段后在同一条线重试**（同线不重传参考图）；摘不掉就立即失败 | 绝不逐条线退避换线重试（参数写错时换线无济于事，还会把 7 条线全烧一遍）；`type` 只认 `extra_forbidden`，取值非法（duration=99）不能摘掉字段 | `workers/submit.py: is_client_error/parse_rejected/drop_rejected` |
| 23 | 提交失败怎么让使用者知道 | 每条任务的失败原因（人话，含被拒字段名）汇总后**弹窗告警** + 写日志；行状态保持原值 | 旧实现先清空状态再提交，失败后行永远停在“待执行”假象里（现不预清）；仅一行灰色 tip 会被看成“改了没生效” | `gui/pages_tasks.py: _run/_on_submit_done` |
| 24 | 工具栏时长/步数/KOL | 上次用过的值跨重启沿用（`RUNTIME_DIR/ui_state.json`，临时文件 + `os.replace` 原子写） | 文件缺失/损坏一律当“没有记录”，回到默认 5秒/8步；写失败只影响沿用，不阻断操作。不存 `config.json`（那是使用者手填的接口配置） | `store/app_state.py` + `gui/pages_tasks.py: _restore_exec_params` |
| 25 | 备注字段 `remark` | 任务表「备注」列：右键「🏷 写备注」/ 双击该列就地改；已勾选则**批量写同一句**，留空＝清除 | 备注**不参与任何业务判定**（不进 payload、不影响 `prompt_changed_at`/迭代/重跑判定），只走 `update_row`；老库靠 `_MIGRATIONS` 的 `ALTER TABLE` 平滑升级 | `store/db.py` + `store/task_store.py` + `gui/pages_tasks.py: _edit_remark` |
| 26 | 任务表列布局与筛选条件 | 「⚙ 字段管理」可增删排序**全部 19 个字段**，布局存 `ui_state.json: tasks_fields`；品名/状态/备注三个下拉筛选与搜索框是 **AND** 关系 | 首次无记录时默认收起 `job_id`/`URL` 两列（技术字段，排障时在字段管理里勾出）；下拉候选值来自 `filter_choices()`（DISTINCT），空档用虚值 `（未填）`/`（无备注）`；重建下拉必须 `blockSignals`，否则 `currentIndexChanged→refresh` 自调递归 | `gui/pages_tasks.py: _restore_field_layout/_sync_filter_choices` + `store/task_store.py: filter_choices` |
| 27 | 看板统计口径 | 时间范围可选「今日/近7天/近14天/近30天/近90天/**全部（累计）**」，选中的范围跨重启沿用（`ui_state.json: dash_range`） | 「全部」＝`days=None`，从第一条记录累计至今、**永不清零**；没有等长的上一周期可比，故 `prev` 全 0，界面据此把环比改成「累计至今」字样；按天明细只列有记录的天（截最近 60 天） | `store/task_store.py: range_stats/report_stats` + `gui/pages_dashboard.py` |
| 28 | 云端响应必需字段在 api_client 出口取 | `submit_job` 直接返回 job_id 字符串、`upload_asset` 返 asset_id：都走 `_field()`（容忍 `jobId`/顶层 `id`/`{data:{...}}` 包裹），取不到抛 **ApiError 并回显响应原文** | 绝不在调用方裸取 `resp["job_id"]`：KeyError 到提交层被 `except Exception` 当成「线路故障」，健康线路被接连标不可用，日志只剩一句 `'job_id'`（线上真实踩过，批量提交全线停摊）；万一真冒出意外异常，提交层报 `类型: 消息`（如 `KeyError: 'job_id'`）不留裸一词 | `core/api_client.py: _field` + `workers/submit.py: do_submit` |
| 29 | 内置线路归一化 | `_normalized_accounts()` 是唯一口径：补 `/api/v1`、补 `name/concurrency`、容许写成纯字符串、丢掉空 base；`defaults_accounts()` 额外剔除模板占位（`<服务地址>`） | 缺文件（ImportError）与有文件但没定义 ACCOUNTS（AttributeError）一律返回 []，回到“自己粘地址”；绝不能在 import 阶段抛异常把软件卡死（`AccountState` 是裸取 `cfg["concurrency"]` 的）。首配写盘走 `setup_wizard.save_first_config()`（弹窗与向导共用）；`_freeze_accounts()`：打包版把内置地址固化进 config.json，开发机只存姓名。⚠ 向导不能 `from core.config import`，`core/setup_wizard.py: _builtin_defaults` 与 config 那份要同步改 | `core/config.py: _normalized_accounts/defaults_accounts` + `core/setup_wizard.py` |
| 30 | 表格里的“选中”算哪个 | **任务中心：勾选框是唯一口径**（跨页保留、驱动执行/取消/删除/写备注）；鼠标按住拖框（位移 >16px 才算框选）只把框到的行**勾上**并立即清掉 Qt 高亮 | 不用 `itemSelectionChanged`（点勾选框取消时该行也会被当作选中，会反手又勾回去）；单击也清高亮，避免“看着选中了其实没勾”；只增不减，取消靠点勾或「清除选择」 | `gui/pages_tasks.py: _on_rubber_band/eventFilter` |
| 31 | 多行批量删除的确认次数 | 设置页线路表：整行选中（SelectRows）+ 连续多选（ExtendedSelection），**一次确认删完全部选中行**（从大到小 `removeRow` 避免行号位移） | 未选中只提示不误删；删后仍需点「💾 保存设置」才写盘（不默默改配置）。旧实现无确认且只能删当前一行，多选时弹 N 次窗 | `gui/pages_settings.py: _del_rows` |
| 32 | 文案样本库只能追加 | 素材提取的 `文案样本库.csv` **只追加、不覆写、不去重**：提取到一条文案就多一行，同一链接重复提取也各自留痕（靠「提取时间」区分）；**一行一条记录**（字段内换行折成空格，`wc -l` 才等于记录数）；**首行必是表头**，遗留无表头文件自动前置补上；路径固定在 `material/素材提取/`，**不跟着“保存到”漂** | 旧版三宗罪都被当成“前面的记录被覆盖了”：按原文链接去重（重复提取什么都没写）、样本库建在输出目录下（换目录＝换库）、只在文件不存在时写表头。CSV 被 Excel/WPS 占用时追加报 PermissionError → 只记日志不阻断，并提醒别在 Excel 里保存旧副本（那才是真覆盖） | `video_text_tools/material_extract.py: append_corpus/_ensure_header/_one_line` + `gui/tool_panels.py: MaterialPanel._task` |
| 33 | 表格行内按钮（操作列） | `setCellWidget` 放按钮时，点击回调**只记按钮对象**，行号与单元格值都在点击时现查（`cellWidget(i, COL) is btn` 反查行）；按钮 `NoFocus` 不抢 Tab；列号用模块常量（`COL_NAME/COL_BASE/COL_CONC/COL_OPEN`）而不是魔法数字 | 不能 `lambda row=r, base=base` 捕获创建时的值：行会被删（行号位移）、地址会被双击改，两种都是“点开上一行的地址”；删除后也不重绑按钮（反查天然正确）。行高 45px（`ROW_H`）+ 本表 `item{padding:3px}`：雅黑 13px 行距已接近 26px，默认行高会把地址文字上下切掉。加了 widget 列之后 `selectedRows()` 只在选区铺满全部列时才计数（末列没 item），批删要再并上 `selectedIndexes()` 的行号 | `gui/pages_settings.py: _set_open_btn/_open_api/_del_rows` |
| 34 | 探活异常怎么归类（决定降不降灯） | 看 `ApiError.status_code` **有没有值**：有值且 <500（404/405/401…）＝服务有话回 → `probe_state="alive"`，**不记失败、不降灯**，UI 画 🟡「在线（探活路径未实现）」；没值（拒连/超时/DNS）或 ≥500＝真不通 → `"down"`，照旧计失败 | 旧实现把探活异常一律当故障：云端没实现 `GET {base}/health`（返 HTML 404「页面未找到」）→ 3 轮后**全线标红** → `[:1]` 兜底只剩一条 → **整批灌一条线**（多选强制重跑现场，同事反馈）。配套三处：候选池取最小那一整档（`schedulable_accounts`，全红只告警一次）、`BatchBalancer._lines()` 沿用同一口径、批量收尾必报「本批 N 条分布」，只用上一条线时带 ⚠（GUI 与命令行同一句 `format_batch_distribution`） | `registry/manager.py: classify_probe/schedulable_accounts` + `gui/header.py: line_light` + `workers/submit.py: format_batch_distribution` |
| 35 | 一次用浏览器打开多条线路 | 设置页底部「🌐 打开选中」：按表格顺序去重后逐条开标签，超过 `OPEN_MAX=8` 条先确认；没选中任何行时**只问一句**「要打开全部 N 条吗」，答否就一个都不开 | 空地址行先滤掉再交给 `_browser_url`（否则拼出光杆 `http://`），全空则提示「还没填接口地址」；`openUrl` 返回 False 的行汇总成「部分没打开」告警而不是静默失败。逐行点行末按钮在 5 条以上就太累 | `gui/pages_settings.py: _open_selected/dedupe_browser_urls/_selected_rows` |
| 36 | 重跑时该任务还有执行在跑 | 「▶ 执行选中」先过 `inflight_execs(tids)`（只看 `REG` 且 `is_active`）：有在跑的执行 → 同一个弹窗多给一个默认项「⏹ 取消并重跑」（另有「🔁 直接重跑」与「跳过」），取消排在提交**之前**且在后台线程里做 | 旧实现根本不查：再提交一份 → 新旧两份同时占并发、各下一个成品（**同事反馈现场**），还给正在跑的任务念“已经执行成功过”这种错文案（单条时更给出抽卡，1 份变 4 份）。不能拿数据库的 `status=running` 当“还在跑”：重启后 `REG` 空了而库里那句是上一辈子留下的（轮询只管本次会话的 job），拿它发取消等于对着早就跑完的 job 发消息。「跳过」必须连在跑的那几条一起退出本批，否则“跳过”了还会多一个成品。命令行 `run` 不拦不弹确认，但提一句「还有 N 个没跑完，这次会再提交一份」 | `gui/pages_tasks.py: inflight_execs/decide_rerun/cancel_inflight/SubmitWorker` + `gui/dialogs.py: ForceRerunDialog` + `console/app.py: _submit_by_ids` |
| 37 | 「这条视频生成用了多久」怎么算 | `gen_sec` 用**云端回报的三个时间戳**算（`submitted_at`→`started_at`→`completed_at`）：任务中心分「生成用时」+「排队」两列，看板「平均生成时长」只平均 `gen_sec` | 旧口径把提交→完成整个当生成用时：批量提交时同一条线串行跑，**越靠后的视频数字越大**（实测 928 秒里 629 秒在排队、真生成只 297 秒），看板的平均也就不是“一条视频要多久”。宁可没数也不猜：拿不到时间戳时 `gen_sec=0`，执行记录显示「—」，任务中心回退显示总用时并标「早期记录没拆出排队」；不拿本地轮询时刻估（差一个 POLL_INTERVAL，而单条生成本身才 5 分钟量级）。老数据跑 `backfill_run_gen_sec.py` 按 job_id 回云端补（幂等，不覆盖已有拆分）。展示与排序：三个时长列共用 `gui.formatting.secs`（写三份就会出现“10分30秒”排在“4分58秒”前面）与 `tablekit.SecsItem`（重载 `<` ：显示折分秒、排序比秒数） | `store/task_store.py: split_from_cloud/record_run_end/update_run_split/list_tasks_df/_RANGE` + `workers/poll.py` + `gui/pages_tasks.py: _dur_tip` + `gui/tablekit.py: SecsItem` + `backfill_run_gen_sec.py` |

### 4.1 三个文本字段的分工（勿混淆）

| 字段 | 来源 | 用途 |
|------|------|------|
| 脚本 `script` | 使用软件的人自己填（人物/场景等创作文档） | 单脚本多提示词：右键「批量绑定脚本」统一关联，留空=不关联 |
| 口播文案 `script_text` | 系统从提示词自动识别（也可导入时手填） | 剪辑快速定位是哪条提示词 |
| 分镜数 `storyboard` | 系统从提示词的镜头/分镜编号识别，识别不出留空 | 剪辑排期参考 |

## 5. 并发与资源约定

- 每个账号一个 `Semaphore(concurrency)`，但它**只能锁住「提交动作」的并发**（POST
  一成功就 release，管不到云端还在跑的任务）；一条线路真正的在途
  上限靠 `pick_and_wait` 在提交前查负载拦下来，两者不可互相替代。
- `do_submit` 全程持有信号量，**换账号时先释放旧信号量再获取新信号量**（历史 bug：
  曾释放错账号信号量，已由 `tests/test_submit.py::test_switches_account_with_backoff` 锁死）。
- 批量提交两条任务之间随机 sleep `SUBMIT_JITTER` 秒，避免多台电脑同一时刻做出相同选线。
- 轮询线程每账号一个（daemon），终态（completed/failed/cancelled）必须 `REG.remove`。
- SQLite 写入统一走 `store/db.py`（RLock + timeout=30），支持 GUI/控制台双进程并发写。

## 6. 测试约定

- 回归测试放 `tests/`（pytest），批量手测脚本与对照仿真留在 `test/`（不计入回归）。
  选线分布类问题用 `python test/load_balance_sim.py` 复现（改前/改后/接口降级三组对比）。
- `tests/conftest.py` 通过 `AIGC_HOME` 把 SQLite/日志/输出隔离到临时目录；
  涉及网络的依赖一律 monkeypatch `workers.submit` / `core.api_client` 命名空间。
- 改动提交/轮询/账号选择主链路后，必须跑 `python -m pytest -q` 全绿再提交。
- 新增主链路行为时同步补断言，不写"只打印不 assert"的回归用例。
