# R2 实现切片拆分

> 修订：[T-B-014] 2026-11-03 首版产出（R1 收官 #64 `38f0b96` 后）；硬性输入①-⑧（T-B-014 任务书）逐条对照见 §7 自检清单。
> 席位B（T-B-014）产出，供 C 席按序实现。状态：**S1 起待派**（R1 全交付态+checkpoint 链见 doc/plans/r1-slices.md 头部）。
> 依据：doc/PHASE1-SLIM.md §4（L76-85）、doc/CORE-CONTRACTS.md（§2.1 L21-72、§2.2 L76-84、§2.3 L86-102、§3.1 L106-144、§4 L217-238、§5.1-§5.3 L240-271、§6.1-§6.2 L281-294、§7 L298-335、§8 L337-350）、doc/evidence/r0/provider.md（L94 证据延续硬条款、§7 L84-92 R2 承接清单、§8.1 L103-110 实测注记）、doc/ROADMAP.md（§3 L52、§4.5 L68、§6 L88-102、§7 L113-116）、doc/reestimate-r0.md L59-65、doc/plans/r1-slices.md（R1 交付面）、doc/reviews/R1-S7-review.md §6-4、doc/reviews/R1-S8-review.md §8-3。
> TS 基准：`D:/26_code/opencode_py_ver/opencode-2.2.2/`（指纹 doc/source-baseline.json，现锁 20 文件（调度器亲点 committed 基线 20 条，前记 21 系计数笔误）；**R2 新引用的 runner/execution/context/model-request/message-updater/run-coordinator/tool-runtime/read/cli 链须先补指纹**——ROADMAP §4.5，见 §4 前置项）。
> 拆分基准：按现行文档拆；A 席若修订 R2 定义，由调度器对齐后本文件再改版。

## 0. R2 范围锚定（摘自 PHASE1-SLIM §4 L76-85）

- 交付：ContextBuilder、MAAS adapter、runner、read 工具、CLI/REPL、run 任务管理和取消。
- 验收 A：fake server 下多轮文本 → tool call → read 结果 → 第二次生成 → run.finished；provider role/tool_call_id 配对正确。
- 验收 B：工具执行必须经 invoke；请求/开始/结束事件可审计；相同 request_id 不新增用户消息或重复调度。
- 验收 C：流中断、非法 JSON、未完成/未知工具 call、最大步数、上下文超限、Ctrl-C 均有明确终态和资源清理；下一次请求中每个合法 tool_call_id 都有唯一 tool_result。覆盖 accepted→cancelling、run.started 与 cancel_requested 的先提交者竞态，以及 tool_call_id/execution_id 冲突拒绝。
- 验证：fake HTTP → SDK adapter → runner → SQLite 全链集成测试。中断部分流不自动拼接新请求，测试生成次数和工具执行次数。
- 约束（L85）：Phase 1 多个工具按调用开始顺序串行执行；只支持一个 active run，不实现 steering/inbox。

### 0.1 R1 交付面衔接

- `SessionService.prompt` 已返回 RunRef（R1-S7，仅原子持久化 py.prompt.accepted）；§7 L325：**R2 接入调度后仍先提交 accepted 再调度，只调度本次新接受的请求，幂等重试不能再次调度**——runner 挂接点=prompt 的 accepted 提交成功之后、返回 RunRef 之前（调度任务创建与强引用归 service，§7 L325 末句）。
- 事件子集：R1 生产注册=py.session.created/py.prompt.accepted（§3.1 L140）；R2 扩容至 §3.1 表 R2 子集（py.run.started/cancel_requested/finished、py.step.started/generated/finished、py.text.finalized、py.reasoning.finalized、py.tool.requested/started/finished）+ ephemeral 家族（py.text.delta、py.reasoning.delta、py.tool.input.delta、py.tool.progress——生产注册随流式生产者落地）。py.permission.* 与 py.permission.saved/revoked/expired 归 R3（Policy/审批，PHASE1-SLIM §4 R3 L89）；不注册不投影。
- 读模型：0001 的 `messages` 为通用 data JSON 列——assistant/idle 消息直接落列，**无需 0002**；`tool_executions`/`permissions` 表已建（0001 L32-38）；sessions.active_run_id 释放与 idle 投影由 R2 fold 扩展承担（OP-2 的 runs 读模型除外）。
- 基线：main **#64 `38f0b96`**（210 passed = contract 82 + integration 96 + r0 32；CI run 41558180881 双平台 success）。

### 0.2 持久不变量与存量兼容（硬性输入⑥，R1-S7 审查 §6-4 定案）

- **必须兼容存量多 accepted 日志**：R1 存量日志含"prompt.accepted 无后续 run 事件"形态；同 session 多 accepted 是 R1 基线常态（R1-S7 审查理由①亲证 19 处 `_populate(prompts=3)` 站点）。S8 已裁决幸存行统一不变量 `applied_at_discovery > H → 隔离`（T-C1-012 落地 REASON_PROJECTION_AHEAD_OF_LOG）。
- R2 新事件类型的投影**不得破坏 R1 存量日志 replay/rebuild 语义**：
  1. fold 新规则只对"出现的新事件"生效——R1 日志前缀（created + accepted*）经 R2 fold 重放结果与 R1 fold 完全一致（状态 active、active_run_id=最后 accepted、消息集不变）；
  2. **不得**引入"同 session 第二个 accepted 须前一 run 已终态"的 events 层/fold 层约束——该约束只存在于 service busy 门禁（§7 L316/L323）；对存量直接 fail closed 即违反本节；
  3. rebuild（R1-S8）对含 R2 事件的日志天然可用（fold 是投影单一来源，R1-S4 模式延续）；空日志三分支/A>H 隔离/最小诊断记录语义不变。
- generation batch 不变量（§2.1 L65、§3.1 L144）：`finalized* → step.generated` 同批原子；失败整批回滚、重试沿用相同 batch/event ID；不留只有 finalized 或只有 generated 的半批。
- 落地机制：每切片任务书含存量兼容回归项（§5 模板⑤）；R2-S2 以测试钉死。

## 1. 切片序列总表

| ID | 一句话目标 | 接缝 | 前置 | C 席预计 |
|---|---|---|---|---|
| R2-S1 | contracts 扩容：LLM 请求/chunk 面 + 消息 union（assistant/idle/AssistantPart/ToolContent）+ R2 事件全集注册 + ToolSpec/ToolResult | S1/S4 输入类型 | R1 收官态 | 45–60 min |
| R2-S2 | fold/projector 扩展：run/step/tool/text 投影 + tool_executions 状态机 + 存量兼容钉死（+OP-2 runs 读模型） | S4/S6 | R2-S1 | 45–75 min |
| R2-S3 | ai/ 重建：Provider 协议 + MAAS adapter（pydantic、error chunk 终止形态）+ R0 证据延续硬条款 | S1 | R2-S1 | 45–75 min |
| R2-S4 | ContextBuilder：历史+运行设置→GenerationRequest、tool_result 配对、token 预算（reasoning 计入注记） | S1/S6 | R2-S1、R2-S3 | 45–60 min |
| R2-S5 | ToolRegistry + read 工具 + invoke 链（requested→校验→started→执行→finished，串行） | S3/S4/S6 | R2-S1 | 45–75 min |
| R2-S6 | runner 生成循环：step 流→finalized 批→generated→工具串行→终态；中断/非法 JSON/超限/取消全失败面 | S1/S3/S6 | R2-S2..S5 全量 | 60–90 min |
| R2-S7 | service 接线：prompt 后调度 + cancel + run_status + close() R2 生命周期 + 单 active run 强制 | S6 | R2-S6 | 45–75 min |
| R2-S8 | CLI/REPL + 全链集成收口（验收 A/B/C 全量）+ R2 退出证据 | S6 | R2-S7 | 45–75 min |

依赖序：S1 → {S2、S3、S5 可并行/穿插（均只依赖 S1 的类型面）；S4 依赖 S1+S3} → S6（需 S2+S3+S4+S5 全量）→ S7 → S8。调度器可视 C 席空闲度穿插 S2/S3/S5。

**前置项（调度器，派发 S1 前或随派）**：source-baseline.json 补 R2 新引用文件指纹（ROADMAP §4.5）——`packages/core/src/session/runner/{index,llm,step,max-steps,retry,model,publish-llm-event,to-llm-message}.ts`、`packages/core/src/session/{execution,context,model-request,message-updater,run-coordinator}.ts`、`packages/core/src/tool/runtime.ts`、`packages/core/src/tool/plugin/read.ts`、`packages/cli/src/{index,run/run,run/noninteractive,ui/prompt}.ts`（§2 各切片 TS 映射即引用清单）。

## 2. 切片详情

### R2-S1 contracts 扩容：LLM 面 + 消息 union + R2 事件全集 + 工具 wire 面

- **目标**：定义 R2 全部落库/跨接缝数据的 Pydantic v2 wire model：LLM 请求/chunk 面（§2.2/§2.3）、消息 union 扩展（§2.1）、R2 durable+ephemeral 事件注册（§3.1 表 R2 子集逐行）、工具 wire 面（§5.1 ToolSpec/ToolResult/ToolContent）。全部 version=1、extra-forbid、四前缀+整数 bool 守卫（§1 仓库级规则、§8 L348 封闭演进——新增字段一律升版本+upcaster，不走同版本可选字段）。
- **涉及接缝**：S1（模型请求/消息形状）、S4（事件 data 解码）。
- **新增/修改文件**：
  - `src/opencode/contracts/llm.py`：`ModelRef`、`Usage`（五 token 可选非负整数、未知=null 不冒充 0、`cost_usd` 可选定点字符串 §2.1 L74）、`GenerationRequest`（§2.2 L78 字段全集）、`LLMMessage`（system/user/assistant/tool 四角色；text/reasoning/tool_call/tool_result 内容 union）、`ProviderChunk`（§2.3 L88-98 七型表逐行：公共字段+按 type 必需数据；index 从 0 递增）、finish reason 枚举。
  - `contracts/messages.py` 扩展：`AssistantPart`（text/reasoning/tool 三态 tagged union，§2.1 L33）、`AssistantMessage`（model/parts/status: running/completed/failed/interrupted + 可选 finish/raw_finish/usage/cost_usd/error/completed_at，L28）、`IdleMessage`（run_id/outcome/error，L29）、`ToolContent`（text/file 两型，L69-70；file 可编码往返但未启用媒体不得读发 URI §2.1 L72——R2 仅 text）。
  - `contracts/events.py` 注册扩容：R2 durable 10 型（py.run.started/cancel_requested/finished、py.step.started/generated/finished、py.text.finalized、py.reasoning.finalized、py.tool.requested/started/finished——data 按 §3.1 L124-132 逐行）+ ephemeral 4 型（py.text.delta/py.reasoning.delta/py.tool.input.delta/py.tool.progress，L140）。
  - `src/opencode/contracts/tools.py`：`ToolSpec`（§5.1 L244 八字段）、`ToolResult`（content/structured_output?/metadata/error?，L245）。
  - fixtures：每新事件类型至少 1 正 1 反（含 run_id metadata 强制关联——RUN_SCOPED_EVENT_TYPES 扩容）。
- **TS 基准映射**：
  - `packages/schema/src/session-event.ts`（已锁指纹）：Execution L213-228/Step L244-263 及 Text/Tool data 形状（parity §3 L47-53 映射表——py.run.*↔execution.*、py.step.*↔step.*、py.text.finalized↔text.ended 等，Python 无一对一 wire 对应处按 parity 注记）。
  - `packages/schema/src/session-message.ts`（已锁）：L76-84 User.files 对照——Python 不引入（§2.1 L31 有意差异：R2 交付面无媒体，files 仍留版本化引入）。
  - `packages/schema/src/tool.ts`（已锁）：ToolSpec/Result 字段对照。
  - `packages/ai/src/schema/messages.ts`+`events.ts`（已锁）：LLM wire 面/流事件参考。
- **验收标准**：fixture round-trip 等值+未知字段拒绝+缺字段反例；验收 A/B/C 的类型前置（后续切片只消费这些类型）。
- **测试计划**：tests/contract/test_llm.py（新）+ test_events.py/test_messages 扩容（fixture 参数化，纯单测无 DB）；fake 策略=fixture 即假数据源（R1-S1 模式）。
- **开放点**：OP-3（step_id/part_id/execution_id ID 约定——data 模型字段本切片定型，生成约定归 S6）。

### R2-S2 fold/projector 扩展：run/step/tool/text 投影 + 存量兼容钉死

- **目标**：state.py fold 扩展至 R2 事件全集（§2.1 L39-53 工具状态迁移表 + L55-63 run/step 状态约束表逐行）：finalized 按 message_id+part_id 定位+唯一 ordinal（第二个 finalized 同值幂等/异值 StorageCorrupt §3.1 L144）；step.generated 建工具 pending 计划（工具初始 pending/not_started）；tool.requested/started/finished 状态机（effect_status 独立记录副作用确定性 L35；pending 终态必 not_started）；step.finished 自包含终态与已折叠 parts 一致性（只允许补终态 status/usage/finish/error，差异=StorageCorrupt §3.1 L144）；run.finished 投影 idle 消息+释放 active_run_id（L65"idle 只由 run.finished 投影"）。projector 写 messages（assistant/idle data JSON）/tool_executions（state/input_hash）；commit 时校验标识约束（§2.1 L37：同 step 内 tool_call_id 唯一、execution_id session 内唯一不可复用、归属单一 run/step/message；相同事件内容幂等重放、跨调用复用或内容冲突=IdempotencyConflict）。**存量兼容硬项**（§0.2）：R1 日志（created+accepted*，含多 accepted）replay/rebuild 结果与 R1 fold 完全一致——测试钉死。
- **涉及接缝**：S4（投影/回滚）、S6（状态机校验前置）。
- **新增/修改文件**：`session/state.py`（fold 规则+assistant 消息组装）、`session/projector.py`（messages assistant/idle 写入+tool_executions 写入）、`storage/txn.py`（仅扩展：新表写入 SQL+归属/迁移校验）；（OP-2 采纳时）`storage/migrations/versions/0002_runs.py` + runs 投影列。
- **TS 基准映射**：
  - `packages/core/src/session/message-updater.ts`（**须补指纹**）：Adapter L6/update L33——事件→消息增量更新对应物；Python 折进 fold 单一来源（R1-S4 已定：状态机集中 state.apply，不散在 projector/updater——差异表第 3 条延续）。
  - `packages/core/src/session/projector.ts`（已锁）：run/SessionMessageUpdater L261-476、projectIdle L506-535（time_idle 单调——Python idle 由 run.finished 投影，parity L47）；`packages/core/src/session/event.ts`? no——用 `schema/src/session-event.ts`（已锁）。
  - §2.1 L65 正常顺序（run.started → step.started → [finalized* → step.generated] → (tool.requested → 终态)* → step.finished → run.finished）=batch 语义直接来源。
- **验收标准**：验收 A 的"事件/投影/applied_seq 同事务"对新事件面复验；A-S4 复验点（§6 风险表）；§0.2 存量兼容。
- **测试计划**：tests/integration/test_projector.py 扩容：run 生命周期各态投影、迁移表逐行正反例（非法迁移→提交前拒绝）、execution_id 复用/跨 run 归属/tool_call_id 重复拒绝、半批禁止（generation batch 原子性——只 finalized 无 generated 的批不成立）、idle 投影+active_run 释放、**存量兼容**（R1 fixture 日志 replay+rebuild 全等）；test_rebuild.py 追加：含 R2 事件的日志 rebuild 全等。
- **开放点**：OP-2（runs 读模型 0002——本切片落位，A 席确认）。

### R2-S3 ai/ 重建：Provider 协议 + MAAS adapter + R0 证据延续

- **目标**：`src/opencode/ai/`：provider.py——`Provider.stream(request: GenerationRequest) -> AsyncIterator[ProviderChunk]`（§2.2 L83-84：async for 消费、必须支持关闭和取消）+ maas.py——spike 行为 pydantic 重建：三级超时（header=create() 全阶段/chunk=socket read 仅已建立流/total=显式 deadline，provider.md §3 映射表照抄）、Retry-After ms 优先、SDK `max_retries=0`（重试策略在 adapter）、**仅未产出内容且明确可重试**（429+连接建立失败）≤2 次重试、部分流后不重试**不拼接**（§8 L344 retry owner 唯一）、错误以**终止 error chunk** 交付（§2.3 L98/L102——spike 差异 #3 的正式承接；一次请求最多一个终止 chunk）、wire 工具名 `namespace__name` 可逆反解（spike 差异 #2——`fs__read`↔(fs, read)）、`stream_options.include_usage` 透传+`cache_write` 无对应字段=null（§8.1 L109）、SDK 类型不出 adapter（A-PROVIDER 红线）。**证据延续硬条款（provider.md L94）**：重跑 tests/r0/ 全部用例（spike 不搬移不修改）+ fixtures 7 个再生成+新 adapter golden（机制见 OP-4）+ §7 R2 承接清单（L84-92）前两项（错误分片=本切片、重复 tool call ID=adapter/runner 边界）。
- **涉及接缝**：S1。
- **新增文件**：`src/opencode/ai/__init__.py`、`src/opencode/ai/provider.py`、`src/opencode/ai/maas.py`（错误码/常量并入，不强制独立 errors.py）。
- **TS 基准映射**（provider.md L4 权威锚，四文件已锁指纹）：
  - `packages/core/src/aisdk.ts` L65-169（流转换/usage 压末）、L746-857（tool call 碎片按 call_index 聚合——adapter 聚合 ID/名、runner 聚合参数，§2.3 L100 分工）。
  - `packages/ai/src/route/transport/http.ts` L94-140（SSE 读循环/超时包裹）；`packages/ai/src/route/executor.ts` L22-35（请求执行）；`packages/core/src/model-retry.ts` L62-103（重试决策——Python 收窄为 adapter 内 429/connect，见差异表）；`packages/ai/src/testing.ts` L40-150（fake 行为参考——Python 复用 tests/r0/fake_openai_server.py，注意 chunked 编码教训 provider.md §5）。
- **验收标准**：验收 C 的流中断面（EOF 无终止→error chunk interrupted 终态）；验收 A 的 provider 侧（tool_result 配对 tool_call_id→OpenAI tool role 由 adapter 组装，§2.2 L80）。
- **测试计划**：tests/contract/test_ai_adapter.py（新，复用 fake_openai_server）：7 场景 golden 平移+error chunk 终止形态+wire 重复 finish/终态后内容拒绝（spike L21-23 语义平移）+取消传播/资源清理（`asyncio.all_tasks()` 无泄漏）+（OP-7 裁决后）jitter；tests/r0 全量回归绿（每门禁自动覆盖——pytest 全量含 tests/r0）。
- **开放点**：OP-4（证据比对语义）、OP-7（jitter：契约 L344"带抖动" vs provider.md §4"无 jitter"已记差异——冲突待裁）、OP-8（5xx 不自动重试 vs TS"明确没跑"重试——维持 R0 行为，真实后端对照后再定）。

### R2-S4 ContextBuilder：请求组装 + token 预算

- **目标**：`session/context.py`：从历史（repos messages+折叠态）+ 运行设置（**注入面**，OP-1）生成 `GenerationRequest`：LLMMessage 四角色组装；tool_result 必须携带配对 tool_call_id（§2.2 L80——OpenAI tool role 转换归 adapter，ContextBuilder 不把 SessionMessage 或 openai.types 发给另一层，同条红线）；每次 provider 请求固定 catalog 快照（§5.1 L247——只能调用已展示且有 capability 的工具）；token 预算：模型上下文上限（运行时提供；未知时要求宿主配置——§7 L333）− 输出预留 − 安全余量；**reasoning 计入 output 预算设计注记（§8.1 L107 硬输入：实测 max_output_tokens=64 被 reasoning 61 全额消耗、零正文、finish=length——短预算+reasoning 模型可能零正文截断；预算推导必须把 reasoning 纳入输出侧估算并显式留余量，注记落 docstring+测试）**；超限→`ContextLimitExceeded`（保留历史、提示新建会话，不静默裁剪不做无边界摘要）；max_output_tokens 推导（§2.2 L78）。
- **涉及接缝**：S1（请求形状）、S6（service 调用）。
- **新增文件**：`src/opencode/session/context.py`。
- **TS 基准映射**：
  - `packages/core/src/session/model-request.ts`（**须补指纹**）：outputLimit L89（输出预算推导=Python 预算语义对应物）、baseTranscript L99（历史→请求组装）、unsupportedParts L183（不支持内容剔除——Python 对应"只接受 provider 支持的内容"§7 L333）、Interface L222/layer L231。
  - `packages/core/src/session/context.ts`（**须补指纹**）：Selection L28/Loaded L35（上下文选择/加载面）。
  - `packages/core/src/session/runner/to-llm-message.ts`（**须补指纹**）：toLLMMessages L318（历史→LLM 消息映射——role/content 转换对照）。
- **验收标准**：验收 A 前半（多轮文本请求组装正确）；验收 C 的"上下文超限"终态面。
- **测试计划**：tests/contract/test_context.py（纯单测）：历史 fixture→请求逐字段断言、预算边界（恰好/超限→ContextLimitExceeded/输出预留+reasoning 余量）、tool_result 配对完整性（每个合法 tool_call_id 唯一 tool_result——验收 C 后半断言复用）、catalog 快照不可变性。
- **开放点**：OP-1（运行设置来源：保守=注入，不触碰 ConfigSnapshot v1 与 py.session.created version；R3 ConfigLoader 落地可执行快照后单点替换注入面实现）。

### R2-S5 ToolRegistry + read 工具 + invoke 链

- **目标**：`src/opencode/tools/`：registry.py——`register(adapter)`（重复 namespace/name/version 拒绝，RegistrationError 语义 §5.1 L247）、`snapshot(session) -> ToolCatalog`（每次 provider 请求固定快照）、`invoke(context, tool_ref, input, execution_id) -> ToolOutcome`（唯一调用入口，L247）；invoker.py——§5.1 L251 时序：整批 py.step.generated 提交后 `requested`（原始 requested_name、解析后 tool_ref、canonical input/hash）→ catalog/schema 校验（输入严格验证不隐式转换修改；失败=failed/not_started 结果）→ `started` 提交确认 → adapter 执行 → 结果限额/归一化 → `finished` 提交；**Phase 1 串行按调用开始顺序**（PHASE1-SLIM L85）；工具异常/denied 转模型可读 tool result（§7 L335）；tool 终态必至少一条模型可读文本（§2.1 L72）。builtin/read.py——fs read：路径按允许 roots 解析、拒绝 traversal/越界 symlink/junction（复用 R0-B 已验证方案，§5.3 L266-267）、默认最多 1 MiB、分页行号、输出默认 64 KiB 截断+明确标记截断和原始大小；限额照抄 §5.3 L270（参数流每调用 64 KiB/每次生成最多 16 工具调用/文本流 1 MiB/structured_output+metadata 各 64 KiB——超限停止本次生成报错不执行超限计划）。**R2 无 policy**：read 默认根内 allow（§5.2 L258"根内 read/glob/grep/ls allow"——Policy/审批归 R3；R2 不产生 py.permission.* 事件，permission_decision 字段以默认 allow 语义入 tool.started data）。
- **涉及接缝**：S3（工具调用）、S4（事件提交）、S6（runner 调用）。
- **新增文件**：`src/opencode/tools/__init__.py`、`registry.py`、`invoker.py`、`builtin/__init__.py`、`builtin/read.py`。`ToolContext` 运行时 dataclass（§5.1 L249：session_id/message_id/working_dir/limits/取消信号/progress callback——R2 无 PermissionPolicy 注入，注记 R3 接入点）。
- **TS 基准映射**：
  - `packages/core/src/tool.ts`（已锁）：RegistrationError L21、Editor L26、Snapshot L53、Interface L43。
  - `packages/core/src/tool/runtime.ts`（**须补指纹**）：definition L21（ToolSpec 组装）、execute L29（校验+执行管线）、normalizeContent L272（结果归一化——Python 对应"结果限额/归一化"）、normalizedName L287/effectiveName L289（wire 名映射）。
  - `packages/core/src/tool/plugin/read.ts`（**须补指纹**）：name L15、Input L26（LocationInput）、Plugin L29、toModelContent L169（结果→模型内容+行号分页——Python 行号分页对照）。
- **验收标准**：验收 B（工具执行必须经 invoke；请求/开始/结束事件可审计=tool.requested/started/finished durable 链）；验收 A 的 read 结果链路。
- **测试计划**：tests/contract/test_tools.py（注册重复拒绝/快照固定/invoke 校验失败→not_started 结果形态）+ tests/integration/test_tool_read.py（真实 tmp_path：正常读/越界拒绝/traversal/symlink+Windows junction（R0-B 方案复用）/1 MiB 截断/64 KiB 输出标记/二进制诊断；requested→started→finished 事件序列+input_hash 断言；串行顺序断言（两调用按开始顺序逐个执行））。
- **开放点**：OP-3 落位（execution_id 经 IdFactory 生成）；无新增。

### R2-S6 runner 生成循环

- **目标**：`src/opencode/session/runner.py`（单文件，R1 模式；或 runner/ 包由 C 席定并浮报）：提交 `run.started` → step 循环：`step.started`（run_id/step_id/message_id/model/attempt）→ ContextBuilder 组请求 → `provider.stream` 消费（text/reasoning delta→Bus ephemeral 发布（py.text.delta 等，live-only §4 L220；adapter 聚合 ID/名碎片、runner 按 call_index 聚合参数 §2.3 L100）→ 流终止（finish/error chunk）→ generation batch：`py.text.finalized`/`py.reasoning.finalized`（按 ordinal）+ `py.step.generated`（完整调用计划=AssistantMessage，工具初始 pending §3.1 L128）**同批原子提交** → **收到成功 finish 并把整批调用计划持久化后才开始 invoke**（§2.3 L100）→ 工具串行循环（S5 invoke 链）→ `py.step.finished`（自包含终态含 usage/finish/error）→ finish 无 tool_calls → `py.run.finished`（outcome 由 step/工具终态折叠+idle 投影+释放 active run）。**失败面（验收 C 全量）**：流中断（EOF 无终止/error chunk）→ 已收到完整文本+interrupted step/run 终态（正常取消尽量提交已收文本 §3.1 L142；**不拼接不重发** §8 L344——测试生成次数）；重复 ID/不完整 JSON/超限 → 本次生成失败**且不执行任何工具**、不猜测修复后执行（§2.3 L100）；合法 JSON 但未知工具/参数不符 schema → 该调用 not_started 失败结果（tool_result 配对保持）；max_steps 默认 20 超限→run failed（§7 L335）；上下文超限→ContextLimitExceeded 终态；取消（CancelledError 经 adapter 传播 §2.2 L84）→ cancelling→interrupted；每次生成工具调用数/参数/文本超限→停止本次生成报错不执行超限调用计划（§5.3 L270）。用户重试生成=新 attempt（§8 L344）——R2 attempt 恒 1（OP-6）。
- **涉及接缝**：S1/S3/S6。
- **新增文件**：`src/opencode/session/runner.py`。
- **TS 基准映射**（runner 链全部**须补指纹**）：
  - `packages/core/src/session/runner/index.ts`：RunError L12、Continuation L21、DrainResult L23-28、Interface L31——runner 服务面对应物（TS Effect 服务图+续延；Python 单协程状态机，差异表第 1 条）。
  - `runner/llm.ts`（node L438 起主循环——LLM 步执行/流消费）；`runner/step.ts`（Outcome L32-43、make L67——单步执行与结果分派）；`runner/max-steps.ts`（MAX_STEPS_PROMPT L1——超限提示文本参考）；`runner/retry.ts`（Decision L26/policy L32/make L57——**Python 不移植**：retry owner 唯一=adapter，§8 L344）；`runner/publish-llm-event.ts`（StepRecord L60、createLLMEventPublisher L97——流事件发布对应物，Python=Bus ephemeral）；`session/execution.ts`（Interface L17、terminal L47——终态分派）。
- **验收标准**：验收 A 全链主体；验收 C 全部失败面；§8 L344"部分流已到达则结束并标 interrupted，不能把旧流和新流拼接"。
- **测试计划**：tests/integration/test_runner.py（fake Provider chunk 脚本+真实 store）：正常单步/多步/工具轮次→run.finished+idle；流中断→interrupted+**生成次数断言（=1 不重发）**；非法 JSON→生成失败+工具零执行；未知工具→not_started 结果+每个合法 tool_call_id 有唯一 tool_result（验收 C 后半）；max_steps=20；ContextLimitExceeded；ephemeral delta 发布（Bus 订阅断言）与 durable finalized 对应；tool_call_id 重复/execution_id 冲突拒绝（验收 C 末项）。
- **开放点**：OP-6（attempt 恒 1）。

### R2-S7 service 接线：调度 + cancel + run_status + close() R2 生命周期

- **目标**：service.prompt 在 accepted 提交成功后**创建并调度 runner 任务**（只调度本次新接受的请求；幂等命中返回原 RunRef **不再调度**、事件数不增 §7 L325；对调度任务持强引用 §7 L325 末句）；`cancel(run_id)`（§7 L329：dedupe_key=`cancel:run_id`；已结束→原 RunStatus；未结束→提交取消意图+关闭待审批/未开始调用→cancelling；已开始工具如实保存已确定结果不伪装"未执行"；run.finished 先提交→迟到 cancel 不覆盖；取消意图先提交→不再启动新 step/tool、清理后 interrupted；重复 cancel 等待同一清理任务；迟到完成回调按 execution_id 去重；清理无法确认→LifecycleCleanupFailed 且 session 不可再执行）；`run_status(run_id)`（OP-2 落位：runs 读模型或派生）；`close()` R2 生命周期（§7 L331 全序：首次调用原子进 closing gate（拒新 prompt/工具调度/权限写入）→每个可写 session 活动 run 提交 `py.run.cancel_requested(reason=shutdown)`→（R2 无 pending approval，permission.closed 分支不可达——防御分支浮报）→释放提交锁→等待 provider stream/adapter→期间仍可经同一 SessionStore.commit 写已确定 tool/step/run 终态→关闭超时外部结果仍不确定：可写则 `tool.finished(effect_status=unknown)`+interrupted step/run 终态+session cleanup_failed、不可写则保留记录不报告已清理→全部自有任务退出或 durable 标记后关 provider/DB→service closed；并发/重复 close 复用同一清理任务不重复提交）；单 active run 强制（busy 门禁含 cancelling 占槽复验——A-S6）。
- **涉及接缝**：S6。
- **新增/修改文件**：`session/service.py`（调度/cancel/run_status/close 扩展）、`app.py`（组合 runner+provider+tools 注入）。
- **TS 基准映射**：`packages/core/src/session/execution.ts`（**须补指纹**：Interface L17、terminal L47、layer L54——执行生命周期/终态分派）；`packages/core/src/session/run-coordinator.ts`（**须补指纹**——run 任务协调对应物）；CORE-CONTRACTS §7 L325/L329/L331 为行为基准（TS 无同构 SessionService——ADR-0001 决策 1 延续，R1-S7 同款论证）。
- **验收标准**：验收 B 调度幂等；验收 C 的 accepted→cancelling、run.started 与 cancel_requested 先提交者竞态（两分支：cancel 先→拒后续 run.started；started 先→running 进 cancelling——§2.1 L60 竞态由首次 durable commit 决定）。
- **测试计划**：tests/integration/test_service_run.py：调度幂等（同 request_id 重试→不二次调度+事件数不增）；cancel 全分支（含已结束/重复 cancel/迟到 cancel）；竞态两分支（注入提交顺序）；close 全序（正常路径+超时→cleanup_failed+unknown 注入）；cancelling 占槽 busy。
- **开放点**：OP-9（permission.closed 分支 R2 不可达——S7 任务书浮报清单必列）。

### R2-S8 CLI/REPL + 全链集成收口 + R2 退出证据

- **目标**：`src/opencode/cli.py`（ROADMAP L102：只负责输入/显示/审批——R2 无审批面）：REPL 循环（读输入→service.prompt→Bus 订阅流式显示（ephemeral delta 实时预览+durable finalized 覆盖——§4 L238 实时 UI 语义）→run 终态显示+RunStatus 退出码）；Ctrl-C→service.cancel（等待清理与持久终态 §7 L307"释放提交锁后等待"）；消息历史显示（messages 分页）；**escape 转义**（§5.3 L271：模型内容可能有任意终端控制字符，CLI 展示必须转义危险 escape）；非交互单发模式（一次 prompt→等待终态→退出码按 RunStatus）。**全链集成收口**：验证行全量（fake HTTP→SDK adapter→runner→SQLite；中断部分流不自动拼接+测试生成次数和工具执行次数）；R2 退出证据文档（`doc/reference/r2-exit-evidence.md`，对齐 r1-exit-evidence 模式：验收 A/B/C+验证行逐条 file:line/测试/审查映射）。入口：`python -m opencode`（pyproject scripts 是否加 entry 由调度器定——OP-5 连带 pyproject/uv.lock 授权）。
- **涉及接缝**：S6。
- **新增文件**：`src/opencode/cli.py`、`doc/reference/r2-exit-evidence.md`。
- **TS 基准映射**（CLI 链**须补指纹**；架构差异大，仅作输入/显示行为参考）：`packages/cli/src/run/run.ts`+`run/noninteractive.ts`（一次性 prompt 执行/等待终态）、`ui/prompt.ts`（交互输入）、`cli/src/index.ts`（入口）。差异：TS CLI→server RPC；Python 同进程直连 SessionService（ROADMAP L48"CLI 和未来 server 都只调用 SessionService"）。
- **验收标准**：验收 A/B/C+验证四项全量（PHASE1-SLIM L80-83 逐行——S2-S7 各切片测试在此收口为端到端）；R2 退出条件对照 PHASE1-SLIM §4。
- **测试计划**：tests/integration/test_cli.py（内嵌事件循环或 subprocess：单发模式端到端（fake server 全栈）+终态退出码+KeyboardInterrupt 注入→cancel 路径）；全链 test_e2e（验收 A 场景：多轮文本→tool call→read 结果→第二次生成→run.finished+role/tool_call_id 配对断言）；收口门禁四件+CI（CI 已全量 pytest，无需改 ci.yml——R2 预期不改工作流）。
- **开放点**：OP-5（CLI 依赖：保守=零新依赖标准库实现；rich 引入需调度器授权 pyproject+uv.lock）。

## 3. 关键设计差异备忘（Python vs TS，实现时勿"照抄"）

1. 架构：TS runner/execution 为 Effect 服务图（Context.Service+Layer+续延 Continuation，runner/index.ts L23/L44）；Python runner=单协程状态机+SessionService 持任务强引用（§7 L325），无 Effect 服务图。
2. 重试：TS model-retry.ts L62-103 模型级重试调度（含 jittered）；Python **retry owner 唯一=MAAS adapter**（§8 L344）——429/connect ≤2 次、部分流后不重试不拼接、5xx 上抛不自动重试（OP-7/OP-8）；runner 无自动重试面（runner/retry.ts 不移植）。
3. 错误交付：TS aisdk 错误经 Effect fail；Python=终止 `error` ProviderChunk（§2.3 L98/L102；spike 差异 #3 的正式承接）+错误详情脱敏（§8 L342）。
4. 状态机位置：TS 散在 projector/updater/message-updater adapters（projector.ts L261-476、message-updater.ts L33）；Python 集中 state.apply 纯函数（R1-S4 已定，commit 投影/rebuild/测试共享）。
5. 工具执行：TS tool/runtime.ts 有并行/调度面；Python Phase 1 **按调用开始顺序串行**（PHASE1-SLIM L85）、单 active run、无 steering/inbox（同条）。
6. CLI：TS packages/cli→server RPC 架构；Python cli.py 同进程直连 SessionService（ROADMAP L48）；TS approval 面 Python R2 无（审批归 R3）。
7. 未知 durable 类型：TS readAfter 跳过推进 cursor（bus.ts L809-827）；Python fail closed 进只读诊断（R1 已定，R2 不变——r1-slices §3 第 9 条延续）。
8. ID：TS execution/step ID 生成散布各处；Python IdFactory.AscendingIds 统一（evt_/msg_/ses_/run_ 四前缀 §3.1 强制；step_/part_/exec_ 为内部约定不入契约——OP-3）。
9. 预算：TS model-request.ts outputLimit L89 输出预算推导；Python 同构+**reasoning 计入输出预算注记**（§8.1 实测——TS 无此端点行为记录，Python 侧硬输入）。
10. 配置：TS config discovery.ts 完整发现/快照（§6 L279）；Python R1 最小快照 v1+R2 运行时注入（OP-1），R3 才落 ConfigLoader/可执行快照（PHASE1-SLIM L89）——TS 无此分阶段。
11. wire 工具名：OpenAI 函数名正则不含 `.`→`namespace__name` 可逆映射（provider.md §6-2）；Python adapter 反解+ToolRegistry 按 (namespace,name,version) 查（S3/S5 联合）。
12. jitter：TS model-retry jittered 调度；Python R0 实测精确 `min(retry_after, remaining)` 无 jitter（provider.md §4 已记差异）——与契约 L344"带抖动"字面冲突，OP-7 待裁。

## 4. 开放问题（不预写裁决；保守读法供 C 席先行实现+浮报，终裁归 A/调度器）

- **OP-1 模型/端点/roots/limits 的 R2 运行时来源**：快照 v2（§6.1 L283"后续可执行快照使用新的 schema_version"——须升 py.session.created v2+upcaster §8 L348）vs 运行时注入（App/CLI 构造注入，快照不动）。**保守读法=注入**：R2 交付面无 ConfigLoader（R3 L89 才落），R2 定义 v2、R3 loader 若改形状再升 v3=版本摇摆；ContextBuilder/adapter 设置来源做成注入面，R3 单点替换。落点 S4/S7/S8。
- **OP-2 run_status(run_id) 的 run→session 解析**：0002 追加 revision 建 runs 读模型表（prompt.accepted 投影 run 行+run.started/finished 更新）vs events 派生扫描（无索引）。**保守读法=0002**（PHASE1-SLIM L74"后续升级只追加 revision"；CORE-CONTRACTS §3.2 读模型清单为四表——**新增第五表属契约面扩容，须 A 席确认后 CORE-CONTRACTS 随改**，本方案不预写）。落点 S2。
- **OP-3 step_id/part_id/execution_id ID 约定**：§3.1 只强制四前缀。**保守读法**=IdFactory.AscendingIds 派生+内部命名约定（step_/part_/exec_ 前缀），不改 §3.1；浮报留 R5 契约评审。落点 S1（字段形状）/S6（生成）。
- **OP-4 R0 证据延续的比对语义（provider.md L94）**：7 fixture 重跑=同 fake server+spike 再生成（环境延续，byte 级可复现）+新 adapter 对同 7 场景 golden 断言；spike 差异 #3（错误上抛→error chunk）/#4（dataclass→pydantic）使错误场景 golden **必然不同**。**保守读法**=新 golden 测试钉行为+差异记入 provider.md 追记节（不覆盖 R0 证据原文；spike/tests/r0 零改动）。落点 S3。
- **OP-5 CLI 呈现依赖**：ROADMAP L88 技术栈含 rich；pyproject 现无。**保守读法**=零新依赖（标准库 ANSI/异步输入）；rich 引入由调度器单独授权（pyproject+uv.lock 变更+理由）。落点 S8。
- **OP-6 用户级重试生成（§8 L344"用户要求重试生成时创建新 attempt"）**：R2 是否暴露显式 retry 命令。**保守读法**=不暴露（新 prompt=新请求；attempt 恒 1；attempt>1 留 R4 恢复协调器/未来显式 retry 面）。落点 S6/S8。
- **OP-7 jitter**：契约 L344"带抖动"vs provider.md §4"无 jitter"（R0 实测记录）——**契约与 R0 证据记录冲突，须 A 席裁决**。保守读法=按契约补 jitter（重试 sleep 加抖动，不改上限/条件）；A 若维持 R0 行为则以 R0 证据为准并改契约措辞。落点 S3。
- **OP-8 5xx 不自动重试**：provider.md §6-9（TS 对"明确没跑"重试）——留 R2 对照真实后端再定。**保守读法**=维持 R0 行为（5xx→ProviderUnavailable retryable 上抛不重试）；真实端点对照后如改，单独微任务。落点 S3。
- **OP-9 close() 的 permission.closed 分支**：R2 无权限流（read 默认 allow、无 ask）→§7 L331"为 pending approval 提交 py.permission.closed"在 R2 **协议不可达**。**保守读法**=分支代码面留位（R3 接入），S7 任务书浮报清单必列【协议不可达】。落点 S7。

### 归置决定（任务⑦，B 定并写明理由；三项默认 R5）

1. **AscendingIds clamp 加固 → R5**。理由：Phase 1 单进程单 writer 不变（R2 runner 与 service 同进程），R2 新增 ID 消费面复用同一 counter 不扩大竞态域；加固涉及时钟回拨 clamp+振荡微测，独立微任务更干净。**R2 顺带触发条件**：S6/S7 若发现新增 ID 消费方破坏 counter 单调前提（如跨 reset 复用同 counter）→ 提前。
2. **session_by_active_run 多行精确化 → R5；R2 留注记**。理由：多行=损坏（last-accepted-wins 旁路已裁决 R1-S7 不可达），rebuild/隔离路径已收窄；R2 涉及 active_run 语义处（S2 fold 释放/S7 cancel/close）在 docstring 留"多行=损坏"注记即可。**顺带触发条件**：S7 若以 session_by_active_run 做调度判据且多行窗口影响语义 → 加精确化。
3. **contracts 共享 .core 错误基类（Obs-2）+ codec.py 单点化 + per-tree PYTEST_DEBUG_TEMPROOT 隔离 → 默认 R5**。**R2 顺带触发条件**：(a) S1/S3 若新增第 N 处重复错误映射点使漂移风险可见 → .core 基类；(b) S3 若 decode/错误组装出现第二实现点 → codec 单点化；(c) S2+ 之后集成测试树与 r0 树并行互撞复现（调度器门禁与 C 树并行）→ TEMPROOT 按树隔离（已实证可行）。

## 5. 每切片任务书模板（调度器直接取用派发 C 席；对齐 T-C1-012 风格）

```
# T-C2-0XX · 席位C-2：R2-SX <标题>

【角色章程·C 实现者】（三角色流水线：A 仲裁者 / B 规划评审 / C 实现者；本会话由调度器驱动，你是席位C-2——实现者。基准裁决：一切以契约文本+TS 原始实现+切片方案 doc/plans/r2-slices.md §2 R2-SX 为行号级依据；无依据处按方案 §4 对应 OP-n 保守读法实现并在进度文件浮报，不预写裁决）
- 工作目录：worktree 仓库根 <调度器填>（相对路径即 worktree 根；日志重定向主树，worktree git status 保持干净——不在 worktree 内建日志/临时文件）。
- 基线：main #64 38f0b96（210 passed）+ <在飞切片链/检查点>。
- 写权限（显式授权清单，仅此——调度器按切片填写）：
  - 新增：<文件清单>
  - 修改：<文件清单+限定语（"仅追加"/"仅限 X 区段"/"不修改不删除既有断言"）>
- 冻结（零触碰）：其余全部文件——<冻结面：未涉及模块/契约冻结条款/既有测试/迁移既有 revision/pyproject·uv.lock·ci.yml（R2-S8 OP-5 裁决前）>。
- 禁止 git 写操作；只读 git 允许。提交由调度器负责。
- 止损指令：预计超过 <按 §1 预计带上限> 分钟 → 立即停止，已完成部分写清楚，回报"部分完成"。
- 门禁纪律：与其他树的 pytest 并行可能瞬态互撞（共享 PYTEST_DEBUG_TEMPROOT，已知模式）——偶发 1 failed 先单独复跑一次再下结论。

【背景】<切片目标一句话+方案 §2 R2-SX 引用+前置切片交付面+OP-n 保守读法（如有）>

【任务】
① 实现项分解：按方案 §2 R2-SX"目标/验收标准"逐条（含 TS 映射锚——须补指纹的文件以 source-baseline.json 补录为准）。
② 测试：tests/<文件>（按方案"测试计划"逐条；fake 策略按方案）。
③ 门禁（全部自跑全绿后才算完成；**环境注记 2026-11-03（调度器更正）**：本机应用管控拦截 uv 直接 spawn pytest/mypy（os error 4551），pytest/mypy 须用 `uv run python -m pytest`/`uv run python -m mypy` 等效形式，ruff 两件不受影响）：uv run python -m pytest -q（预期 <N> passed=<210+新增>，以实跑为准）；uv run ruff check .；uv run ruff format --check .（退出码 0）；uv run python -m mypy src tools/r0 tests/r0 tests/contract tests/integration（<文件数> files）。
④ **强制浮报：不可达/防御分支清单（B §8-3 定案条款，R1-S8 教训）**：进度文件须逐分支列出本切片实现的全部防御/不可达分支，逐条标注【协议不可达】（需 SQL 篡改等违反协议前提才可达）或【未测防御】（合法路径但无测试覆盖）；漏报一条未测防御分支即 R1-S8 空日志早退同型教训（major 级审查发现）。
⑤ 存量兼容回归（方案 §0.2 硬项）：R1 日志（created+accepted*，含多 accepted）replay/rebuild 全绿。
⑥ 进度文件回报：改动清单/门禁四行/未决问题（按方案 §4 OP-n 标注，不预写裁决）。

【回报格式（回复正文，1-2 段）】状态 / 改动文件清单 / 实现要点+取舍 / 门禁四行结果 / 不可达·防御分支清单 / 未决问题。
```

## 6. 风险表（A-S4/A-S6/A-PROVIDER 的 R2 部分；行号=doc/reestimate-r0.md）

| 标记 | R2 复验时点 | B 规划的风险面（R2 部分） |
|---|---|---|
| A-S4（L59） | R2-S2/S6 审查 | 新事件仍走唯一 commit 入口（ROADMAP L52）；generation batch 原子（finalized*+generated 同批、无半批——§3.1 L144）；step/run 终态重复/乱序提交前拒绝（§2.1 L63）；tool_call_id/execution_id 标识约束（§2.1 L37）；R1 存量日志 replay/rebuild 兼容（§0.2）。 |
| A-S6（L60） | R2-S7 审查 | 单 active run 保持（含 cancelling 占槽）；prompt/cancel 经同一提交路径；accepted/running/cancelling/idle 投影可重启重建（rebuild 含 R2 事件）；run.started 与 cancel_requested 先提交者竞态（§2.1 L60）；幂等重试不重复调度（§7 L325）；close 竞态与 cleanup_failed（§7 L331）。 |
| A-PROVIDER（L63） | R2-S3 审查 | R0 三级超时/Retry-After/jitter/错误 chunk/真实 MAAS 差异是否按 Python/TS 边界记录——R2 复验：错误 chunk 正式形态落地（spike 差异 #3 承接）；SDK 类型不越 adapter（红线）；jitter 契约 vs 证据冲突（OP-7）；5xx 维持 R0（OP-8）；§8.1 三注记入设计（reasoning 预算 S4/include_usage 映射 S3/SSE 逐帧 S3·S8）。 |

B 补充（非 reestimate 表但 R2 实测相关）：httpx2 close-delimited body 竞态（provider.md §5——新 adapter 测试沿用 fake server chunked 编码，勿改回 close-delimited）；reasoning 零正文截断（§8.1——S4 余量+CLI 终态提示，防"静默无输出"）；Ctrl-C 与 asyncio 取消传播的 Windows 信号差异（R0-B 平台证据 doc/platform-support.md 参照；S8 测试用 KeyboardInterrupt 注入）。

## 7. 方案自检清单（硬性输入①-⑧逐条对照；缺失即浮报）

- **① PHASE1-SLIM §4 全量（L76-85）**：§0 逐行 verbatim 锚定；L78 交付六件→S3（adapter）/S4（ContextBuilder）/S5+（read 工具）/S6（runner）/S7（run 任务管理和取消）/S8（CLI/REPL）；L80-83 验收 A/B/C+验证行→各切片"验收标准"逐条引用+S8 收口；L85 约束→S5（串行）/S7（单 active run）/全程（无 steering/inbox）。✓
- **② CORE-CONTRACTS 锚点**：§2.2 L78-84→S1（类型）/S3（Provider.stream）；L80 转换语义→S3（adapter 组装 OpenAI tool role）+S4（不把 SessionMessage/openai.types 发给另一层，红线双落）；L344 retry owner→S3（唯一重试点+不拼接）+S6（失败面）+OP-6/7/8。✓
- **③ provider.md L94 证据延续**：S3 硬条款（重跑 tests/r0 全部用例+7 fixture 再生成+新 golden——机制 OP-4）；门禁体现：tests/r0 已在 pytest 全量面内=每次门禁自动回归，fixture 再生成以显式命令记录进 S3 任务书（调度器模板①项）。✓
- **④ §8.1 行为注记三条**：reasoning 计入 output 预算→S4 目标+设计注记+测试；include_usage 透传+cache_write=null→S3；SSE 逐帧无缓冲失真（36 chunk 实测）→S3 基线+S8 流式显示参考。✓
- **⑤ B §8-3 定案条款**：§5 模板④——强制浮报不可达/防御分支清单+两类标注+R1-S8 教训引用。✓
- **⑥ 存量兼容**：§0.2 专节（多 accepted+统一不变量+三条禁则+generation batch 不变量）+模板⑤回归项+S2 测试钉死。✓
- **⑦ 归置决定**：§4 尾部——三项默认 R5+R2 顺带触发条件+session_by_active_run 注记落点（S2/S7）。✓
- **⑧ TS 基准**：每切片 TS 映射（spike 四文件沿 provider.md L4 权威锚+runner/execution/context/model-request/message-updater/run-coordinator/tool-runtime/read/cli 链 B 亲锁行号锚）；新增参考文件先补指纹（ROADMAP §4.5）→§1 前置项（调度器动作）。✓

**无缺失项**。硬输入①-⑧全部有落点；未决=§4 OP-1..OP-9（不预写裁决）。




