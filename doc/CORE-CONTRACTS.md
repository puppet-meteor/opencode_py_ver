# Core Contracts 0.1：最小核心接口

> 状态：设计契约，等待 Phase 1 用实现和测试验证。本文是接缝定义的唯一来源。
> 路线：[ROADMAP](ROADMAP.md)；任务：[Phase 1](PHASE1-SLIM.md)；决策：[ADR-0001](adr/0001-core-architecture.md)。

## 1. 共同规则

- wire/persisted model 用 Pydantic v2；运行时资源用 dataclass/Protocol。所有集合使用工厂默认值，不共享可变容器。
- Python 属性使用 snake_case；序列化名称在下面固定。SDK/ORM 对象不能跨接缝。
- JSON 扩展值只允许 JSON 数据，禁止任意 Python 对象。未知的可选扩展字段可保存在 metadata；未知事件版本/必需 content type 不能默默忽略。
- 本文的接口形状是实现约束；非完整可运行源码。每个切片产出真实类型和 JSON Schema，替换占位结构后才可称“契约已验证”。

## 2. S1：历史、模型输入和流式输出

### 2.1 历史消息

`SessionMessage` 的公共字段：`id: str`、`type`、`created_at: UTC datetime`、`metadata: JsonObject`。消息归属和排序由其 session 投影记录，不依赖 ID 字典序。

| type | 必需数据 | 可选数据 |
|---|---|---|
| user | `text: str` | `files: list[FileContent]`，Phase 1 非空则拒绝 UnsupportedCapability |
| assistant | `model: ModelRef`、`parts: list[AssistantPart]`、`status: running/completed/failed/interrupted` | `finish`、`raw_finish`、`usage`、`cost_usd`、`error`、`completed_at` |
| idle | `run_id`、`outcome: succeeded/failed/interrupted` | `error` |

`AssistantPart` 是 text、reasoning、tool 三种 tagged union。text/reasoning 含 `part_id`、`text`；reasoning 可选、不能假定每个端点都会返回。tool 含 `tool_call_id`、`execution_id`、`name`、`input: JsonObject`、`state`、`effect_status`、`content: list[ToolContent]`、可选 `structured_output` 和 `error`。

工具状态：pending 可到 running、denied、failed（校验失败）或 cancelled（含审批过期/重启）；running 可到 succeeded、failed、cancelled、unknown。pending 的所有直接终态必须 `effect_status=not_started`；只有已持久 started 且无 finished 的调用恢复为 unknown。终态只有一次。`effect_status: not_started/completed/unknown` 独立记录副作用确定性，取消/失败不等于没有副作用。

标识约束：同一 `step_id/message_id` 内 `tool_call_id` 必须唯一；同一 session 内 `execution_id` 必须唯一、不可复用，并且只能归属于一个 run/step/message。`tool.requested/started/finished` 提交时校验这组归属、`input_hash` 和状态；相同事件内容可幂等重放，跨调用复用或内容冲突返回 `IdempotencyConflict`（已存坏数据则进入只读诊断）。

状态迁移表：

| 事件/条件 | 旧状态 → 新状态 | effect_status |
|---|---|---|
| step.generated 的工具计划 | 不存在 → pending | not_started |
| tool.requested | pending → pending，只补 catalog/input 信息 | not_started |
| tool.started | pending → running；批准且未取消才能提交 | not_started，外部执行尚未确认 |
| tool.finished：输入/catalog 错误 | pending → failed | not_started |
| tool.finished：策略 deny/reject | pending → denied | not_started |
| permission.closed + tool.finished | pending → cancelled | not_started |
| tool.finished：adapter 返回成功 | running → succeeded | completed |
| tool.finished：异常/取消 | running → failed/cancelled | 由 adapter 证明，否则 unknown |
| tool.finished：恢复发现无终态 | running → unknown | unknown |

succeeded 只表示工具报告完成，不意味着整个 run 成功；已完成终态不可逆。权限关闭和对应未开始工具终态同批提交，不留下永久 pending。

Run/step 状态约束：

| 状态 | 允许的下一状态 | 规则 |
|---|---|---|
| run accepted | running / cancelling / interrupted | py.prompt.accepted 已提交但调度尚未开始时，cancel 可先把它置为 cancelling；进程退出恢复时可直接终止为 interrupted |
| run running | cancelling / succeeded / failed / interrupted | `py.run.cancel_requested` 只能从 accepted/running 产生一次；不覆盖已提交 py.run.finished |
| run cancelling | interrupted / failed | 不再启动 step/tool；等待 provider、adapter、MCP 和子进程清理 |
| step running | finished / failed / interrupted | cancel_requested 后 provider finish 可记录为 interrupted；已收到且已提交的 tool 终态保留 |
| step finished/failed/interrupted | 无 | 重复同内容幂等；不同内容为 StorageCorrupt |

正常顺序是 `run.started → step.started → [generation batch: finalized* → step.generated] → (tool.requested → permission/tool terminal)* → step.finished → run.finished`。generation batch 内先按 `ordinal` 写入全部 `py.text.finalized/py.reasoning.finalized`，最后写且只写一个 `py.step.generated`；该批次原子提交，`step.generated.message` 必须与同批及此前已提交的 finalized parts 完全一致。若流在生成完成前中断，可以只提交已收到的 finalized parts 和 interrupted 的 step.finished，不得生成不完整的 tool 计划。`py.run.cancel_requested` 可在 py.prompt.accepted 之后、run.finished 之前插入；accepted 与 run.started 的竞态由 session command queue 的首次 durable commit 决定：cancel 先提交则拒绝后续 run.started，started 先提交则 cancel 从 running 进入 cancelling；两者不得放进同一可产生歧义的批次。step.finished 和 run.finished 可分不同提交批次，但必须按 session command queue 排序；step.finished 前所有 generated tool call 必须终结，run.finished 前当前 step 必须终结。恢复在半批之间按已提交计划处理，不假设内存事件存在；事务保证 generation batch 要么全出现、要么全不出现。run.finished 的 outcome 由 step/工具终态折叠得到，idle 只由 run.finished 投影。

`ToolContent`：

- `{type: "text", text: str}`；
- `{type: "file", uri: str, mime: str, name?: str}`。

Phase 1 内置工具只产生 text；file 可编码往返，但未启用媒体能力时不得读取或发送其 URI，模型侧使用明确的“未加载附件”文本，历史保留受限引用。M6 再实现受控文件/媒体读取。MCP `structuredContent` 放 `structured_output: JsonValue | None`，纯结构化结果再生成有大小上限的 canonical JSON 文本供模型读取，不能丢失为 `str(result)`。tool 终态必须至少有一条模型可读文本（包括 denied/cancelled/unknown 的说明）。

`ModelRef = {provider: str, model: str}`；Phase 1 provider 固定为 maas。`Usage` 的 input/output/reasoning/cache_read/cache_write token 均是可选非负整数；未知为 null，不能冒充 0。`cost_usd` 是可选十进制定点字符串，只有配置了价格或端点返回可信费用时才赋值。

### 2.2 模型请求

`GenerationRequest` 包含 `request_id`、`model: ModelRef`、`messages: list[LLMMessage]`、`tools: list[ToolSpec]`、`max_output_tokens`、`temperature?` 和 `timeout_s`。

`LLMMessage.role` 为 system/user/assistant/tool；内容可为 text/reasoning/tool_call/tool_result。tool_result 必须携带配对的 tool_call_id，MAAS adapter 转成 OpenAI tool role。`ContextBuilder` 从历史 + 配置快照生成请求：不直接把 SessionMessage 或 openai.types 发给另一层。

运行时接口：
`Provider.stream(request: GenerationRequest) -> AsyncIterator[ProviderChunk]`。
这是返回异步迭代器的方法；调用方使用 `async for`，不再 `await` 一个迭代器。实现可以是带 yield 的 async generator，必须支持关闭和取消。

### 2.3 ProviderChunk

公共字段：`request_id`、`index: int`（从 0 递增）、`type`、`provider_metadata: JsonObject`。按 type 分派的必需数据：

| type | 数据 |
|---|---|
| text_delta / reasoning_delta | `part_id, delta` |
| tool_call_start | `call_index, tool_call_id?, name?` |
| tool_call_args_delta | `call_index, delta` |
| tool_call_end | `call_index, tool_call_id, name, raw_arguments` |
| usage | `usage: Usage, cost_usd?` |
| finish | `reason: stop/tool_calls/length/filter/unknown, raw_reason?` |
| error | `error: CoreError` |

adapter 按 call_index 聚合工具 ID/名称碎片；runner 按同一 index 聚合参数。tool_call_end 才允许解析完整 JSON。**收到成功 finish 并把整批调用计划持久化后，才开始 invoke**；多个调用交错按开始顺序串行执行。重复 ID、超限/不完整 JSON 使本次生成失败且不执行任何工具，不猜测修复后执行；合法 JSON 但工具未知/参数不符 schema，则给该调用返回 not_started 的失败结果。

一次请求最多一个终止 chunk：finish 或 error。EOF 没有终止 chunk 视为流被中断；取消通过 CancelledError 传播。adapter 可以暂存 SDK 的 finish_reason，读完末尾 usage 后再发逻辑 finish；不能让 finish 后的 usage 丢失。provider_metadata 不能成为执行权限或恢复真相，保存前按敏感字段规则处理。

## 3. S6 + S4：唯一事件写入、投影与数据库

### 3.1 事件 envelope 和清单

Python 使用自己的 `py.*` 域事件；与 TS 的对应关系在 [parity](parity.md)，不冒充 TS wire schema。

`EventDraft` 公共字段为 `id: str, type: str, version: int, session_id: str, created_at: UTC datetime, data: typed event data, metadata: JsonObject`；时间序列化为 ISO 8601 UTC。ID 在重试前生成并复用，draft 不含 seq。运行内事件 metadata 必须含 run_id，其他可含 request_id/execution_id/correlation_id/causation_id，不能含凭据。

durable event = draft + `seq: int >= 1`。seq 在 DB 事务内按 session 分配；只有 durable 有 seq。ephemeral envelope 为 `id, type, session_id, created_at, data, metadata`，没有 durable cursor。

所有持久事件当前 version=1，`data` 必须按 type+version 解码为对应 Pydantic 模型：

| type | 必需 data（除表内字段均含 session 归属验证） |
|---|---|
| py.session.created | `working_dir, config_snapshot, snapshot_hash`；快照有 schema version 和敏感字段处理 |
| py.prompt.accepted | `request_id, run_id, message: UserMessage` |
| py.run.started | `run_id` |
| py.run.cancel_requested | `run_id, reason: user/shutdown`；阻止后续 step/tool 启动，不覆盖已有结果 |
| py.step.started | `run_id, step_id, message_id, model, attempt` |
| py.text.finalized / py.reasoning.finalized | `message_id, part_id, ordinal, text` |
| py.step.generated | `step_id, message: AssistantMessage`；完整调用计划，工具初始为 pending/not_started |
| py.tool.requested | `message_id, tool_call_id, execution_id, requested_name, tool_ref?, input, input_hash` |
| py.tool.started | `execution_id, permission_decision, input_hash` |
| py.tool.finished | `execution_id, state, effect_status, content, structured_output?, error?` |
| py.step.finished | `step_id, message: AssistantMessage`（自包含终态，含 usage/finish/error） |
| py.permission.requested | `permission_id, execution_id, action, resources, input_hash, expires_at` |
| py.permission.replied | `permission_id, reply: once/always/reject, source: host` |
| py.permission.closed | `permission_id, outcome: expired/cancelled/restart, reason`；系统关闭，不伪造宿主 reject |
| py.permission.saved | `decision_id, scope, rule, expires_at, boot_id`；仅当前进程的本 session |
| py.permission.revoked / py.permission.expired | `decision_id, reason`；前者是宿主撤销，后者是到期检查产生 |
| py.run.finished | `run_id, outcome, error?`；投影 idle 消息并释放 active run |

ephemeral 类型为 `py.text.delta/py.reasoning.delta`（message_id, part_id, delta）、`py.tool.input.delta`（call_index, delta）、`py.tool.progress`（execution_id, metadata）。delta 不落库；finalized/finished 必须含恢复所需的完整值。

正常取消时尽量提交已收到的完整文本和中断终态；kill 时仍在内存的 delta 可能丢失。恢复保证已提交历史，不保证恢复未提交 token。

消息投影约束：finalized 按 message_id+part_id 定位并有唯一 ordinal；同一 part 的第二个 finalized 只接受完全相同值，不重复拼接。`step.generated` 必须是 generation batch 的最后一个事件，并与同批及此前已提交的 finalized 内容一致，同时为每个工具建立 pending 计划；step.finished 必须在所有计划工具终结后提交。其自包含 message 是持久终态，文本/工具结果必须与已折叠 parts 一致，只允许补终态 status/usage/finish/error；有差异则 StorageCorrupt，不能用旧 snapshot 覆盖。generation batch 提交失败时整批回滚，重试沿用相同 batch/event ID，不得留下只有 finalized 或只有 generated 的半批。不同 ID 的冲突终态/乱序状态迁移在提交前拒绝，读取已存坏数据时转只读诊断。

### 3.2 唯一 commit

`SessionService` 校验命令、runner/Invoker 生成事件；唯一提交接口是：

`EventBatch = {id: str, events: list[EventDraft]}`；events 非空，批内顺序固定。
`SessionStore.commit(session_id, batch: EventBatch, expected_seq: int) -> list[DurableEvent]`。

S4 的事务 writer 仅供此接口调用；Bus/工具适配器/查询 repo 无权写 events/messages。顺序：

1. writer 锁 + SQLite 写事务，先按 ID/批次查询是否已提交；
2. 整批已存在、内容/顺序/批次均一致则返回原事件，**先于 expected_seq 检查**；同 ID 内容不同或混合“已有 ID + 新 ID”的批次返回 IdempotencyConflict；
3. 全是新事件时核对 expected_seq，校验归属、版本和合法状态迁移，分配连续 seq；
4. 插入 events；用同一 projector 更新 sessions/messages/permissions 和 sessions.applied_seq，一起 commit，失败整体回滚；
5. 按 seq 发 Bus 已提交通知。重试返回已有提交时不再投影或重发通知，遗漏通知由 EventLog 补读。

EventBatch.id 是稳定 batch_id，批内下标为 batch_ordinal；重试复用全部内容（含时间和 ID）。批量提交全有或全无；不得把两个旧批次拼成“同一重试”。全新批次的 expected_seq 不同才返回 Conflict。TransactionWriter 是 S4 私有实现名，外部模块只依赖 SessionStore.commit。

SessionService 的每个 session 有一把短期提交锁：读取状态/当前 seq → 生成本次命令事件/expected_seq → commit → 交付本地通知，整个过程串行；prompt、approval/expiry、cancel、恢复和 runner 结果回调都走它。**不在等待模型、审批或 adapter 时持锁**，也不在命令处理里等待自己持有锁的回调。cancel 先设置取消信号并关闭 pending 审批，started 提交再次检查取消状态；若结果已先提交则保留原终态，后来的重复完成回调只作去重。权限 pending 的回复/过期/关闭只允许首个提交的合法迁移，迟到回复返回 ApprovalExpired/Conflict。

重试只重试提交，不重新执行 adapter。取消若撞上 commit，先按批次 ID 确定事务结果，再补需要的终态；不得假设收到 CancelledError 就一定没写入。

初始迁移 `0001` 使用 Python 自己的最小 schema：

- `events`：id 主键、session_id、seq、type、version、时间、data/metadata JSON、batch_id、batch_ordinal、可选 dedupe_key；unique(session_id, seq)、unique(session_id, batch_id, batch_ordinal)、unique(session_id, dedupe_key)；
- `sessions`：状态、active_run_id、配置快照、applied_seq；事件创建时投影；
- `messages`：session_id、message_id、顺序、JSON；unique(session_id, message_id)；
- `tool_executions`：session_id、execution_id、run_id、step_id、message_id、tool_call_id、input_hash、state；unique(session_id, execution_id)、unique(session_id, step_id, tool_call_id)，由 commit 校验其归属和终态不可逆；
- `permissions`：待审批和保存决定的投影；input_hash、作用域、有效期、终态。

events 不依赖可重建的 messages 外键。创建 session 与其首条事件同事务。seq 在写事务内按已有最大值 + 1 分配，初值 1；不删除日志前缀、不重用 session ID。

Phase 1 每个数据目录只允许一个 writer 进程（持有 OS 文件锁）；SQLite 使用 WAL、foreign_keys、busy_timeout，进程内写事务串行。第二个进程明确失败；asyncio/aiosqlite 本身不提供多 writer 正确性。M1 才增加租约/跨进程协调。

`SessionStatus` 只有 `active`、`read_only_diagnostic`、`cleanup_failed`、`closed`。未知事件版本、损坏数据或序列冲突使 `active → read_only_diagnostic`；关闭阶段无法确认资源回收使 `active → cleanup_failed`。这两种诊断状态以及 `closed` 都不自动恢复为 active：`messages`、`run_status`、`EventLog.read/follow` 等读取仍可用，但 `prompt`、`cancel`、权限回复/撤销和任何新工具写入分别以 `SessionReadOnly`、`LifecycleCleanupFailed` 或 `SessionClosed` 拒绝。只有经过验证的备份恢复、迁移或人工修复命令才可清除诊断状态；Phase 1 不提供静默修复。`SessionService.close()` 是进程生命周期操作，不会把正常 session 永久标记为 `closed`；`closed` 仅供显式会话关闭/管理操作，若未提供该命令则不会产生该状态。

### 3.3 重放、迁移与副作用

projector 是确定的状态折叠：`apply(state, event) -> state`，不读时钟、不生成随机 ID、不执行工具、不发模型请求。消息创建时间取事件时间；idle ID 由 run_id 确定。rebuild 在会话锁和事务下清空该 session 的读模型、按 seq 重放并更新 applied_seq；不会再次发业务通知或生成新事件。未知事件版本、损坏数据或序列冲突使该会话进入只读诊断，不能跳过后继续“恢复”。

恢复检查不是纯 replay 的一部分。获得独占运行权后，按已提交 py.step.generated 的完整计划处理：pending → cancelled/not_started，started 无 finished → unknown，已有 finished 原样保留。先追加待审批 py.permission.closed，再补工具终态、包含全部结果的 interrupted py.step.finished 和 py.run.finished；同一恢复批次使用稳定 ID，重复启动可幂等恢复。

下一次生成前必须保证 assistant 的每个合法 tool_call_id 都有对应 tool result，包括 cancelled/ExecutionUnknown 的合成错误结果。已提交计划中的后续调用即使尚未 invoke，也补 cancelled 结果。未完成/非法 JSON 没有可执行调用计划，不把原始残片放入下一请求的 tool_calls，只保留诊断文本/错误。没有 generated 时，只用已提交 finalized 文本生成 interrupted 消息。unknown 终止当前 run，不自动继续下一轮生成；下次显式 prompt 会看到未知副作用说明。

DB 与外部副作用不能 exactly-once：

- tool.started 提交前退出：按未开始处理；
- tool.started 后、tool.finished 前退出：标记 unknown，即使命令可能已经成功；
- tool.finished 已提交：直接读取结果，绝不重跑；
- 用户重试是新的 execution_id，引用旧 ID；仅适配器声明支持幂等键时可复用外部幂等键。事件幂等不能代替外部幂等。

使用 Alembic 从空库建立 0001。升级已用库必须新建 revision、备份并验证旧数据；不复制 TS 历史迁移，不把已有 Python 库“直接建最终态”。降级默认从已验证备份恢复，tag 不能代替数据回退。

## 4. S2：live 与 durable feed

`Bus.subscribe(session_id, types?) -> AsyncContextManager[AsyncIterator[Event]]`：
只看进入订阅后的 live 通知，含 ephemeral/committed durable；有队列上限，溢出断开并报 SlowConsumer，不静默丢 durable。退出 context 必须注销并释放队列。

`EventCursor = {session_id: str, seq: int >= 0}`；0 表示日志之前。`EventPage = {items: list[DurableEvent], next_cursor: EventCursor, has_more: bool}`。next_cursor 是最后一个 items 的 seq，空页保持输入 cursor；未建 session 返回 NotFound。

`EventLog.read(session_id, after: EventCursor | None = None, limit=100) -> EventPage`：
返回 seq>after.seq 的 durable 事件。None 等于本 session 的 seq=0；limit 为 1–1000。session 不同、负数或编码非法为 InvalidCursor；seq 超过当前高水位也明确报 InvalidCursor，不空等。Phase 1 不裁剪日志；M1 若裁剪必须新增 CursorExpired/重建协议，不能在旧协议中静默跳过。

`EventLog.follow(session_id, after: EventCursor | None = None) -> AsyncContextManager[AsyncIterator[LogItem]]`：

1. 先注册 wakeup，再在短读事务中捕获已提交高水位 H；该读取是初始游标验证/补读窗口的线性化点；
2. 分页补读 after.seq<seq<=H，发一次 `Synced(cursor=EventCursor(session_id, H))`；
3. 此后始终查询 seq>last_seq，只把 Bus 当唤醒信号；重复信号合并、最长 1 秒轮询兜底。H 前后的 commit 由查询范围分开，不把通知 payload 再发送一遍；
4. 同一连接按 seq 严格递增；重连允许 at-least-once，消费者按 session+seq/id 去重。补读、yield 和等待期间不持会话提交锁，不阻塞 runner。

follow 只保证 durable，不回放 ephemeral token。实时 UI 可用 Bus 做预览，以 durable 完整值覆盖；M5 首版只保证 durable SSE。游标不可跨 session 使用。outbox、全局 cursor 和跨实例 wakeup 不在 Phase 1。

## 5. S3：工具、权限与生命周期

### 5.1 唯一调用入口

`ToolSpec`：`namespace, name, version, description, input_schema, supported_content_types, side_effects, idempotency_support`。
`ToolAdapter.execute(context, validated_input) -> ToolResult` 仅注册中心内部可访问。`ToolResult` 为 `content, structured_output?, metadata, error?`；不用字符串承担所有结果。

`ToolRegistry` 只公开 `register(adapter)`、`snapshot(session) -> ToolCatalog`、`invoke(context, tool_ref, input, execution_id) -> ToolOutcome`。重复 namespace/name/version 拒绝；模型名通过可逆/碰撞检测映射，MCP server ID 必须参与命名。每次 provider 请求固定 catalog 快照，只能调用已展示且有 capability 的工具。

`ToolContext` 是运行时 dataclass：session_id、message_id、working_dir、limits、取消信号和 progress callback；PermissionPolicy 注入 Invoker，工具不能自行批准自己。

整批 py.step.generated 提交后，`invoke` 先把相应调用记为 requested（含原始 requested_name、可选解析后的 tool_ref、canonical input/hash），再做 catalog/schema 校验。输入严格验证，不隐式类型转换或修改参数；失败生成 failed/not_started 结果。成功再走 policy/审批 → started 提交确认 → adapter 执行 → 结果限额/归一化 → finished 提交。只有 Invoker 的事件回调可提交，adapter 不拿 Store/DB。py.step.finished 中的终态必须与工具事件折叠一致，不能用旧 snapshot 覆盖新结果。

Phase 1 不做通用 before_execute hook。M3 若变更输入，必须发生在校验/审批前；之后输入不可变，否则重新校验和审批。插件不能越过 deny、扩充 capability 或直接调用 adapter。

### 5.2 权限

`PermissionDecision = {effect: allow/ask/deny, reason, action, resources, input_hash, source, expires_at?, audit_id}`。
宿主硬性 deny/根目录限制优先；项目规则不能放宽宿主限制。剩余匹配规则按来源优先级和规则顺序覆盖，默认：根内 read/glob/grep/ls allow，write/edit/bash/MCP ask。无人处理 ask 时返回 ApprovalUnavailable，绝不变成 allow。

`once` 只批准一个 permission_id + input_hash；`always` 在 UI 中标为“当前进程的本 session”，保存可展示的具体 action/resource 规则，禁止生成无范围通配许可。shell 规则还绑定完整 command/cwd，MCP 绑定 server 启动指纹和 schema/catalog 版本。策略直接 deny 不伪造宿主回复；实际 reject 才写 py.permission.replied。审批未连接、超时/取消写 py.permission.closed（未连接用 cancelled + ApprovalUnavailable），pending 工具结束为 cancelled/not_started。审批默认 300 秒；迟到回复拒绝，不能覆盖 closed。

每次应用启动生成 boot_id；只激活当前 boot_id 内经宿主批准且未过期/撤销的规则。恢复后 saved 决定保留作审计，不自动激活；宿主新的 always 回复产生新决定。`revoke_permission(decision_id)` 追加 py.permission.revoked；Policy 在使用前检查 expires_at，过期时经提交锁追加 py.permission.expired 后重新求值。规则撤销/到期不改变已经执行的结果，但 tool.started 前必须复核有效性。对 unknown 执行相同输入的重试必须重新 ask，不能复用旧 once/always。模型文本、项目文档、MCP 输出不构成宿主批准。

### 5.3 执行限制

- 文件路径按允许 roots 解析，拒绝 traversal、越界 symlink/junction；write/edit 对读到的版本做冲突检测并用同目录临时文件原子替换。read 默认最多 1 MiB、分页行号；输出默认 64 KiB，明确标记截断和原始大小。
- 路径检查是本地受信任 workspace 的防误用措施，不保证抵御恶意并发替换链接的进程。R0 验证各平台能力；无法可靠校验的路径拒绝变更。
- shell 显式选择解释器：Unix bash、Windows PowerShell；可用性在启动时检查。不依赖 create_subprocess_shell 的平台默认解释器。默认每次审批完整 command/cwd，命令字符串不经过 JSON 修复；危险命令正则不能证明安全。
- 子进程用受控环境、超时默认 120 秒、输出截断后仍排空管道；取消/超时必须终止并回收进程树（Windows Job Object、Unix process group 或已验证等价方案）。无法确认清理完成时标 effect_status=unknown。
- 参数流每个调用上限 64 KiB、每次生成最多 16 个工具调用、文本流上限 1 MiB；超过即停止本次生成并报错，不执行超限调用计划。structured_output 和 metadata 各限制为 64 KiB，超限明确标记并保留 bounded 文本说明，不无限写入数据库/提示。
- 命令输出、模型内容可能有任意终端控制字符，CLI 展示必须转义危险 escape。日志不记录认证头/环境凭据；任意文件输出无法保证识别所有秘密，保存和发送模型前受同一工作区信任范围约束。

## 6. S5：配置、指令和 MCP 信任

优先级：内置默认 < 用户全局 `opencode.json` < 已选项目根到当前目录的各层 `opencode.json` < CLI。项目根由 CLI 指定或最近 VCS 根确定；无 VCS 时使用选定 cwd，不向磁盘根无限搜索。全局默认路径为 `~/.config/opencode-py/opencode.json`，可用显式 CLI 路径覆盖。

标量后者覆盖，映射递归合并，列表替换；instructions 按来源由外到内追加，路径去重；permission 追加有序规则后再与宿主硬限制求交。未知结构字段报配置错误；endpoint、workspace roots、可执行 MCP 定义属于宿主配置，项目配置无权隐式扩大它们。

`ConfigSnapshot` 含 schema_version、model/endpoint_ref、允许 roots、permission rules、指令文本+provenance、MCP ref+启动指纹、limits 和 hash。每份 AGENTS.md 上限 64 KiB、总指令 256 KiB，超限明确报错。指令按外到内排列，不提升系统权限；不自动执行文档里的命令或加载其中声明的插件/MCP。

快照是运行行为基线：存可重建的非秘密配置及指令文本，凭据只存引用；URL userinfo/query 和敏感键不能原样入库。恢复若工作目录失效、凭据引用失效或 MCP 启动指纹变化，明确停住并要求宿主重新配置/批准，不能静默换一套权限。Phase 1 无热重载。

MCP 只支持显式用户配置的 stdio server，argv 直接 exec、不拼 shell，环境变量为最小集和显式 secret refs；启动前宿主确认该 server 的完整本机权限。SDK manager 拥有子进程、会话、退出栈和重连：调用超时/断线按 unknown 处理，不自动重试副作用；最多重建连接供下一次显式调用。远端 tools/list 改变后先建立新 catalog，不能用旧批准调用改名工具。

## 7. S6：应用命令接口和任务所有权

`SessionService` 是 CLI、同进程调用和未来 HTTP 的唯一入口：

| 方法 | 结果/保证 |
|---|---|
| `create(config_snapshot) -> SessionRef` | 创建并持久化 session |
| `session_status(session_id) -> SessionStatus` | 返回 active/只读诊断/清理失败/closed；不触发恢复或外部调用 |
| `prompt(session_id, text, request_id) -> RunRef` | 返回 run_id；相同 request_id+相同输入返回原 run，输入不同则冲突 |
| `cancel(run_id) -> RunStatus` | 提交 cancel_requested、幂等请求取消；释放提交锁后等待资源清理和持久终态 |
| `reply_permission(permission_id, reply) -> DecisionReceipt` | 宿主输入通道；与运行锁分离，避免等待审批时死锁 |
| `revoke_permission(decision_id) -> None` | 宿主撤销已保存规则；不撤销已完成副作用 |
| `messages(session_id, after_message_id?, limit=100) -> MessagePage` | 有序分页；cursor 必须属于该 session |
| `run_status(run_id) -> RunStatus` | 运行、完成、失败、中断及错误信息 |
| `close() -> None` | 按下述顺序取消并收回自有任务，关闭 provider/MCP/DB；不得遗留后台任务 |

`SessionRef={session_id}`；`RunRef={session_id, run_id, request_id}`；`RunStatus` 含上述标识及 `status: accepted/running/cancelling/succeeded/failed/interrupted`、可选 error。`MessagePage` 含 items、next_message_id 和 has_more；消息游标按投影顺序分页。`DecisionReceipt` 含 permission_id、decision 和 execution_id，不携带可复用执行权限。

同一 session 最多一个 active run：不同 request_id 并发 prompt 返回 SessionBusy；Phase 1 不支持 steering/inbox。SessionService 持有强引用任务表；端点/CLI 结束不任由任务脱离管理。重复 prompt 在重启后也查 durable request_id/dedupe_key，不创建第二条用户消息。

先持久 py.prompt.accepted（dedupe_key=prompt:request_id）再调度；此后若进程退出，恢复将未结束 run 标 interrupted，**不会重新自动调度**。重复 prompt 的身份/输入检查先于 SessionBusy 判断。RunRef 只代表已接受，不代表一定成功执行。CLI 等待 RunStatus，使用 Bus 看实时输出、EventLog 看可靠历史。

cancel 的 dedupe_key 为 cancel:run_id；已结束 run 直接返回原 RunStatus。未结束则提交取消意图并关闭待审批/未开始的调用，状态改为 cancelling；已开始工具仍如实保存已确定的结果，不把成功伪装成“未执行”。如果 py.run.finished 先提交，迟到 cancel 不覆盖；取消意图先提交时不再启动新 step/tool，清理后 run 结束 interrupted。重复 cancel 等待同一清理任务；迟到完成回调按 execution_id 去重。若清理无法确认，报告 LifecycleCleanupFailed，保持该 session 不可再执行，不能假装已回收并启动下一 run。

`close()` 生命周期：首次调用先原子地进入 closing gate，拒绝新的 prompt、工具调度和权限写入；为每个活动 run 提交 `py.run.cancel_requested(reason=shutdown)`，并为 pending approval 提交 `py.permission.closed(outcome=cancelled)`。随后释放提交锁，等待 provider stream、adapter、MCP 和子进程结束；等待期间仍允许通过同一 `SessionStore.commit` 写入已确定的 tool/step/run 终态。达到关闭超时而外部结果仍不确定时，若 DB 可写则提交 `tool.finished(effect_status=unknown)`、interrupted 的 step/run 终态并把 session 置为 `cleanup_failed`；若 DB 也不可写则保留未完成记录，交由下次启动恢复，不得报告已清理。只有所有自有任务已退出或已被 durable 标记后，才依次关闭 MCP、provider、DB，并把 service 标记为 closed；这不改变可恢复 session 的 `SessionStatus`。并发或重复 `close()` 复用同一个清理任务，返回相同结果/错误且不重复提交关闭事件。

ContextBuilder 保留完整工具 call/result 配对，只接受 provider 支持的内容；预留输出 token + 安全余量。R0 获取模型上下文上限；未知时要求宿主配置。达到预算时返回 ContextLimitExceeded，保留历史、提示新建会话；Phase 1 不静默裁剪或做无边界摘要。

max_steps 默认 20；超限结束为 failed。工具默认串行，调用异常/denied 转成模型可读 tool result；存储、权限基础设施错误立即 fail closed。取消由 service 协调传播到流和子进程，资源关闭后才确认终态。

## 8. 错误、重试和兼容

`CoreError = {code, message, retryable, details?: JsonObject}`。code 至少区分：
`ValidationError, UnsupportedCapability, NotFound, SessionBusy, Conflict, IdempotencyConflict, InvalidCursor, PermissionDenied, ApprovalUnavailable, ApprovalExpired, ToolTimeout, ToolFailed, ExecutionUnknown, ProviderUnavailable, ProviderStreamInterrupted, ContextLimitExceeded, SlowConsumer, StorageCorrupt, MigrationRequired, SessionReadOnly, SessionClosed, LifecycleCleanupFailed`。

details/异常链做脱敏，不把认证头或原始 SDK request 暴露到 wire。用户错误与内部故障分别日志记录，但对外错误结构相同。

只有一个 retry owner：MAAS adapter 关闭 SDK 默认重试，未收到首个内容且明确可重试的 429/短暂网络错误最多 2 次重试（尊重 Retry-After、带抖动、受总超时限制）。部分流已到达则结束并标 interrupted，不能把旧流和新流拼接。用户要求重试生成时创建新 attempt；既有 tool.finished 绝不重新执行。MCP、shell、文件变更默认不自动重试。

版本演进以旧 JSON fixture/旧 DB fixture 能读取为最低门槛。新增 event/content/capability 不等于旧消费者自动支持；不支持时显式协商或报 UnsupportedCapability，不能只靠“字段可选”声称向后兼容。
