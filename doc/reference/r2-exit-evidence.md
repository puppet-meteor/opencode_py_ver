# R2 退出条件证据汇总（PHASE1-SLIM §4 R2）

> 用途：R5（core 0.1 结项）直接引用的 R2 退出条件证据映射。数据 = 契约/计划文档行号 + 源码 file/symbol 锚 + 测试（file::test 名或 file 级）+ 审查记录（`doc/reviews/*`）+ checkpoint 链；**无新主张**（本文件只汇总既有实现与测试的位置）。
> 范围锚定：`doc/PHASE1-SLIM.md` L76-85（R2 交付 + 验收 A/B/C + 验证行 + L85 三约束）。
> 基线：main 检查点 #97（`de95bb9`，T-B-024 PASS 0/0/0/1 + merge r2-s8→main `08ce736` 零冲突 + 四门禁绿 648/clean/format exit 0/mypy 65 files）。本任务（T-C1-024，worktree `r2-s9`）新增 `tests/integration/test_e2e.py`（3 测）与本文件，R2 收官态 **651 passed**（见 §5）。
> 行号声明：PHASE1-SLIM 行号为原文逐行引用（L80-83/L85 本任务全文核验）；源码锚以 file+symbol 级为主（逐行 file:line 复核超出本任务止损线，删节项见 `doc/orchestration/progress/T-C1-024.md` ③；已核行号如 `contracts/messages.py` L109/L129/L141、`ai/maas.py` L272 保留）。测试引用：`test_e2e.py`/`test_messages_union.py` 精确到函数名（本任务全文读过），其余测试文件到 file 级（行为锚 = `doc/plans/r2-slices.md` §2 各切片测试计划 L70/L83/L95/L108/L121/L133/L143/L153）。

## 0. 切片交付映射（S1-S8）

| 切片 | 交付 | 主要实现位置（symbol 锚） | 主要测试 | 审查记录 | 门禁/收口锚 |
|---|---|---|---|---|---|
| S1 contracts 扩容 | LLM 请求/chunk 面、消息 union（AssistantPart 三态/AssistantMessage/IdleMessage/ToolContent）、R2 事件全集注册（durable 11 + ephemeral 4）、ToolSpec/ToolResult | `src/opencode/contracts/llm.py`（`ModelRef`/`GenerationRequest`/`ProviderChunk`/finish 枚举）；`contracts/messages.py`（`AssistantTextPart` L109、`AssistantToolPart` L129（`tool_call_id` L141）、`AssistantMessage`/`IdleMessage`/`UserMessage`）；`contracts/events.py`（R2 注册 + `versioned_type`/`canonical_json`）；`contracts/tools.py`（`ToolSpec`/`ToolResult`） | `tests/contract/test_llm.py` + `test_events.py`/`test_messages` 扩容（r2-slices L70） | —（`doc/reviews/` 枚举无独立 R2-S1 评审文件，见进度③） | R1 收官 210 起步 |
| S2 fold/projector 扩展 | run/step/tool/text 投影 + tool_executions 状态机 + runs 读模型（OP-2，T-A-005）+ 存量兼容钉死 | `session/state.py`（fold R2 全集）；`session/projector.py`（assistant/idle/tool_executions 写入 + runs 投影）；`storage/txn.py`（扩展）；`storage/migrations/versions/0002_runs.py` | `tests/integration/test_projector.py` 扩容 + `test_rebuild.py` 追加（r2-slices L83） | `R2-S2-review.md` | 台账为准（进度③） |
| S3 ai/ 重建 | Provider 协议 + MAAS adapter（三级超时、确定性重试、error chunk 终止、wire 名 `namespace__name`）+ R0 证据延续 | `src/opencode/ai/provider.py`（`Provider.stream`）；`ai/maas.py`（`MaasAdapter` L272） | `tests/contract/test_ai_adapter.py` + `tests/r0` 全量回归（r2-slices L95） | `R2-S3-review.md` | 台账为准 |
| S4 ContextBuilder | 历史+运行设置→GenerationRequest、tool_result 配对、token 预算（reasoning 计入注记）、ContextLimitExceeded | `src/opencode/session/context.py`（`RuntimeSettings`/ContextBuilder/`ContextLimitExceeded`） | `tests/contract/test_context.py`（r2-slices L108） | `R2-S4-review.md` | 台账为准 |
| S5 工具面 | ToolRegistry + read 工具 + invoke 链（requested→校验→started→执行→finished，串行） | `src/opencode/tools/registry.py`；`tools/invoker.py`（§5.1 L257 时序）；`tools/builtin/read.py`（`READ_NAMESPACE="fs"`/`READ_NAME="read"`/`READ_INPUT_SCHEMA`/`_resolve_within_root`/`_render`/`FsReadAdapter`） | `tests/contract/test_tools.py` + `tests/integration/test_tool_read.py`（r2-slices L121） | `R2-S5-review.md`、`R2-S5-hotfix-review.md` | 台账为准 |
| S6 runner 生成循环 | step 流→finalized 批→generated→工具串行→终态；中断/非法 JSON/超限/取消全失败面 | `src/opencode/session/runner.py`（单协程状态机） | `tests/integration/test_runner.py`（r2-slices L133） | `R2-S6-review.md` | 台账为准 |
| S7 service 接线 | prompt 后调度 + cancel + run_status + close() R2 生命周期 + 单 active run 强制 | `src/opencode/session/service.py`；`src/opencode/app.py`（provider/tools 注入） | `tests/integration/test_service_run.py`（r2-slices L143） | `R2-S7-review.md`、`R2-S7-closeout-review.md`、`R2-S7c-review.md` | S7 收口 **630**（T-C1-024 任务书引台账） |
| S8 CLI/REPL + 收口 | CLI/REPL + 全链集成收口 + R2 退出证据 | `src/opencode/cli.py`；`tests/integration/test_cli.py`（13 测）+ `test_messages_union.py`（4 测）+ `test_runner.py`（1 测）——T-C1-023/023b；**`tests/integration/test_e2e.py`（3 测）+ 本文件——T-C1-024** | 同左 | `R2-S8-review.md` | S8 前半 **648**（checkpoint #97）→ 收官 **651**（本任务） |

## 1. 验收 A（L80 原文）："fake server 下多轮文本 → tool call → read 结果 → 第二次生成 → run.finished；provider role/tool_call_id 配对正确。"

- **端到端收口**：`tests/integration/test_e2e.py::test_main_chain_multiround_text_then_tool_round`——真 loopback HTTP fake server → `ai/maas.py` SSE → runner → `fs__read` 真实读 tmp 树文件 → SQLite 投影。三轮：2 轮纯文本（第 2 轮多段 delta "seg one "+"seg two"→finalized 单 part 全文逐字）+ 完整工具轮 user→assistant(tool_calls)→真实 read→结果逐字回灌 role:"tool"（rendered `Read file note.txt, lines 1-2\n1: alpha line\n2: beta line`）→第二次生成（独立 provider 请求 requests[3]）→run.finished。断言：库内投影全序 `[user,assistant,idle]×2+[user,assistant,assistant,idle]`；**wire+store 双侧 tool_call_id 配对**（`call_read_note` 逐对相等、wire `function.name=="fs__read"`、arguments `{"path":"note.txt"}`）；finalized 全文逐字（非 delta 拼接）；终态经 `run_status` 读模型（OP-2）。
- **分缝锚**：多轮请求组装=`session/context.py`（S4）；adapter tool role 组装=`ai/maas.py`（S3，r2-slices L94）；step 循环/第二次生成=`session/runner.py`（S6）；read 结果链路=`tools/builtin/read.py`+`tools/invoker.py`（S5）；投影全序=`session/projector.py`（S2）。
- **审查**：`R2-S8-review.md`；T-B-024（checkpoint #97，provider-500→INTERRUPTED 命名复核 concur）。

## 2. 验收 B（L81 原文）："工具执行必须经 invoke；请求/开始/结束事件可审计；相同 request_id 不新增用户消息或重复调度。"

- **invoke 唯一入口**：`tools/invoker.py`（§5.1 L257 时序）；`test_tools.py`/`test_tool_read.py`（S5 计划：requested→started→finished 序列+input_hash+串行顺序）。
- **可审计（端到端侧证）**：`test_e2e.py::test_event_log_records_the_full_lifecycle_in_order`——工具轮全程 durable EventLog 有序子序列（session.created→prompt.accepted→run.started→step.started→text.finalized→step.generated→tool.requested→tool.started→tool.finished→step.started→text.finalized→step.generated→run.finished）且零 delta 类事件持久化（ephemeral 不落库）。字段级归 S6/S7 既有测试（r2-slices L146 定界）。
- **request_id 幂等**：`test_service.py` R1 幂等面（`prompt_by_dedupe_key` 等，r1-exit-evidence §2）+ `test_service_run.py`（S7 计划：同 request_id 重试→不二次调度+事件数不增）。
- **审查**：`R2-S5-review.md`、`R2-S7-review.md`、`R2-S8-review.md`。

## 3. 验收 C（L82 原文）："流中断、非法 JSON、未完成/未知工具 call、最大步数、上下文超限、Ctrl-C 均有明确终态和资源清理；下一次请求中每个合法 tool_call_id 都有唯一 tool_result。覆盖 accepted→cancelling、run.started 与 cancel_requested 的先提交者竞态，以及 tool_call_id/execution_id 冲突拒绝。"

（按任务书定界：验收 C 不入 e2e——端到端收口即本节映射既有测试，逐子面证已覆盖非遗漏；file 级引用，函数级枚举为进度③删节项。）

| 子面 | 既有测试落点 |
|---|---|
| 流中断明确终态+不拼接 | `test_runner.py`（interrupted+生成次数=1 不重发，S6 计划）+ 端到端 `test_e2e.py::test_interrupted_partial_stream_next_request_starts_fresh`（验证行归属，见 §4） |
| 非法 JSON | `test_runner.py`（生成失败+工具零执行） |
| 未完成/未知工具 call | `test_runner.py`（未知工具→not_started 失败结果+tool_result 配对保持） |
| 最大步数 | `test_runner.py`（max_steps=20→run failed） |
| 上下文超限 | `test_context.py`（ContextLimitExceeded）+ `test_cli.py`（`--context-limit 4097` 确定性 FAILED 面，T-C1-023） |
| Ctrl-C | `test_cli.py`（KeyboardInterrupt 注入→cancel 路径）+ `test_service_run.py`（cancel 全分支） |
| 资源清理 | `test_service_run.py`（close 全序+超时→cleanup_failed）+ `test_ai_adapter.py`（取消传播/`asyncio.all_tasks()` 无泄漏） |
| 每个合法 tool_call_id 唯一 tool_result | `test_context.py`（配对完整性）+ `test_runner.py`（未知工具面）+ `test_e2e.py` ①（wire+store 配对断言） |
| accepted→cancelling / run.started vs cancel_requested 先提交者竞态 | `test_service_run.py`（竞态两分支，S7 计划 L143） |
| tool_call_id/execution_id 冲突拒绝 | `test_projector.py`（S2 计划 L83）+ `test_runner.py` |

- **审查**：`R2-S2-review.md`、`R2-S6-review.md`、`R2-S7-review.md`、`R2-S8-review.md`。

## 4. 验证行（L83 原文）："fake HTTP → SDK adapter → runner → SQLite 全链集成测试。中断部分流不自动拼接新请求，测试生成次数和工具执行次数。"

- **全链**：`test_e2e.py` 三测——真 HTTP loopback（FakeOpenAIServer）→ `ai/maas.py` → runner → `fs__read` → SQLite（alembic 真 CLI 建库）。
- **中断不拼接**：`test_e2e.py::test_interrupted_partial_stream_next_request_starts_fresh`——`omit_done=True`（干净 body 结束、无 SSE [DONE] 终止行=EOF-without-terminal）→ INTERRUPTED 终态（`run_status` 读模型）；同 session 新请求→succeeded，新 assistant 文本==新脚本全文**零旧片段**；残段落库现状如实断言（assistant.status=="interrupted"+idle.outcome=="interrupted"）。
- **计数**：两测 EventLog SQL 计数+provider 请求数交叉验证——主链 run.finished=3/step.generated=4/tool.finished=1/requests=4；中断面 run.finished=2/step.generated=1/tool.finished=0/requests=2。
- **审查**：`R2-S8-review.md`（S8 全链收口）+ checkpoint #97。

## 5. 门禁与 CI（R2 收官态实测，T-C1-024 自跑，worktree `r2-s9`）

| 命令 | 结果 |
|---|---|
| `uv run python -m pytest -q` | **651 passed**（648 基线零回归 + 本任务新增 3），145.40s |
| `uv run ruff check .` | **All checks passed!** |
| `uv run ruff format --check .` | **207 files already formatted**（退出码 0；计数非验收标准） |
| `uv run python -m mypy src tools/r0 tests/r0 tests/contract tests/integration` | **Success: no issues found in 66 source files**（65 基线+test_e2e.py） |

- **门禁数字演化（已核验锚点）**：R1 收官 210（r1-exit-evidence §5）→ S7 收口 **630**（T-C1-024 任务书引台账）→ S8 前半 **648**（checkpoint #97；T-C1-023/023b：messages union 4 + CLI 13 + runner 1）→ 收官 **651**（本任务）。S2-S6 逐切片中间数字未在本任务复核（进度③删节项），以 `doc/ORCHESTRATION.md` 台账为准。
- **CI**：r2-slices L153——CI 自 R1-S8 起 Test 步=全量 pytest+mypy 对齐门禁（r1-exit-evidence §5），R2 全程**零改 ci.yml**；本任务为 worktree 内自跑门禁，推送/CI 复验归调度器合并流程。

## 6. 已知边界与后续轮次台账（非 R2 退出条件缺口）

- **L85 三约束**（实现+测试落点）：① 工具按调用开始顺序串行=`tools/builtin/read.py` 模块 docstring（Phase-1 serial under registry lock）+ `test_tool_read.py` 串行顺序断言（S5 计划）；② 单 active run=service busy 门禁（`test_service_run.py` cancelling 占槽复验，S7 计划）；③ 无 steering/inbox=R2 无该面（L85 原文即约束声明）。
- **read 工具量化偏差**（r2-slices §5.3 数字，float-reported；`read.py` 模块 docstring 逐条声明）：读窗口 1 MiB（非 TS 50 KiB）/输出预算 64 KiB/invalid UTF-8 直接失败（TS U+FFFD 替换）/目录=UnsupportedCapability。
- **真实 SIGINT 挂起隐患（n-1）**：R3 P-1 必须+P-2/P-3 可选（checkpoint #97，T-B-024 裁决）。
- **诊断清除命令 Phase 1/2 不提供**（R1 遗留，CORE-CONTRACTS §3.2 L195，归 R5/人工）。
- **R3 增加 config 声明 roots**（`read.py` docstring：R2 唯一允许 root=working_dir）。
- **TS 锚纪律**：CLI 链指纹已在 source-baseline.json（r2-slices L151）；`history.ts:125-150` 仅只读参照；本文件零"与 TS 同构"类未证断言（架构差异见 r2-slices §3）。
