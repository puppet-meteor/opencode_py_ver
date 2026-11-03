# Core Contracts 0.1：最小核心接口

> 状态：设计契约，等待 Phase 1 用实现和测试验证。本文是接缝定义的唯一来源。
> 路线：[ROADMAP](ROADMAP.md)；任务：[Phase 1](PHASE1-SLIM.md)；决策：[ADR-0001](adr/0001-core-architecture.md)。

## Changelog（修订记录）

- 2026-11-02 [T-A-002]：定稿 R1 最小配置快照及 canonical JSON/SHA-256、expected_seq 与 applied_seq 水位、仅持久化的 prompt/RunRef/幂等与拒绝顺序、坏事件只读诊断及投影/日志读取边界；与 B 草案的对齐见 §9。
- 2026-11-02 [T-A-003]：裁决 R1 UserMessage 不含 files、py.* v1 严格 forbid 并以版本升级+upcaster 演进、evt_/msg_/ses_/run_ ID 前缀及整数拒绝 bool（§1/§2.1/§3.1/§8）；JSON Schema 由 R5 统一产出，各切片先以 fixture 锁定形状；ephemeral 生产类型注册留 R2，R1 不注册；CoreError.details 与解码异常家族差异追加至 parity.md，实施交接见 §10。

## 1. 共同规则

- wire/persisted model 用 Pydantic v2；运行时资源用 dataclass/Protocol。所有集合使用工厂默认值，不共享可变容器。
- Python 属性使用 snake_case；序列化名称在下面固定。SDK/ORM 对象不能跨接缝。
- JSON 扩展值只允许 JSON 数据，禁止任意 Python 对象。wire/persisted model 的 envelope、typed data 和嵌套消息采用 `extra="forbid"`，未知结构字段一律拒绝；只有已声明为 JsonObject 的 metadata 等扩展对象内部允许自由键，不能把未知结构字段自动移入 metadata 来容错。未知事件版本/必需 content type 不能默默忽略，演进路线见 §8。
- 仓库级规则：所有整数语义字段（整数 Literal，如 `Literal[1]`，或裸 `int`，含 version/seq/schema_version/expected_seq/ordinal/attempt/index 等）必须用 `field_validator(..., mode="before")` 在类型转换前显式拒绝 Python `True/False`（JSON `true/false`），模型构造与解码路径保持一致。依据：TS `packages/schema/src/event.ts:21–32` 的 `Event.Seq/Event.Version` 使用 `Schema.Int`；**TS 无对应物 + Python 侧论证**：Pydantic 的 bool→int/Literal 相等陷阱及其 before-validator 守卫是 Python 特有问题，必须阻止 lax 转换将布尔值伪装为 1/0。
- 本文的接口形状是实现约束；非完整可运行源码。每个切片产出真实类型并以正/反例 JSON fixture 锁定 wire 形状；统一 JSON Schema 在 R5 由实际模型的 `model_json_schema()` 汇总生成并锁定差异，R1 不要求逐切片落盘 schema。

## 2. S1：历史、模型输入和流式输出

### 2.1 历史消息

`SessionMessage` 的公共字段：`id: str`（模型层强制 `pattern=r"^msg_"`，见 §3.1）、`type`、`created_at: UTC datetime`、`metadata: JsonObject`。消息归属和排序由其 session 投影记录，不依赖 ID 字典序。

| type | 必需数据 | 可选数据 |
|---|---|---|
| user | `text: str` | R1 v1 wire 无可选 files 字段；`files: list[FileContent]` 留到 R2+ 的新版本引入 |
| assistant | `model: ModelRef`、`parts: list[AssistantPart]`、`status: running/completed/failed/interrupted` | `finish`、`raw_finish`、`usage`、`cost_usd`、`error`、`completed_at` |
| idle | `run_id`、`outcome: succeeded/failed/interrupted` | `error` |

R1 `UserMessage` v1 字段全集为 id/type/created_at/metadata/text；缺省不补 files，出现 `files` 键（包括 `files: []`）即按未知字段拒绝。新输入是校验错误；已存日志内出现则按 §3.2.1/§8 处理为损坏。R2+ 引入 files 时须升级承载该消息的持久事件版本并保留 v1 解码/upcaster；字段可编码不等于媒体已启用，未启用时非空文件请求仍报 UnsupportedCapability。依据：TS `packages/schema/src/session-message.ts:76–84` 的 `SessionMessage.User` 包含 `Prompt.fields.files`；Python R1 只有文本 prompt，以不含 files 的最小 v1 保持已交付 fixture，避免为仅接受空列表提前引入 FileContent 占位模型。这是有意缩小 R1 范围，不宣称 TS wire 等价。

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
| run accepted | running / cancelling / interrupted | py.prompt.accepted 已提交；R1 只保持 accepted，不调度、不补终态；R2 起 cancel 可先置为 cancelling，恢复协调器可终止为 interrupted（见 §7） |
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

`LLMMessage.role` 为 system/user/assistant/tool；内容可为 text/reasoning/tool_call/tool_result。tool_call part 的 `name` 必须为点分 `namespace.name`（如 `fs.read`），由 MAAS adapter 与 wire 名 `namespace__name` 双向转换。tool_result 必须携带配对的 tool_call_id，MAAS adapter 转成 OpenAI tool role。`ContextBuilder` 从历史 + 配置快照生成请求：不直接把 SessionMessage 或 openai.types 发给另一层。

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

同一次生成内，一个 text/reasoning part_id 只能对应一种 kind；同 kind 的连续 delta 按该 ID 聚合，跨 text/reasoning 复用时报 `ValidationError(details.violation=part_id_reused)`，本次生成失败且不执行任何工具，不猜测拆分或改名。

一次请求最多一个终止 chunk：finish 或 error。EOF 没有终止 chunk 视为流被中断；取消通过 CancelledError 传播。adapter 可以暂存 SDK 的 finish_reason，读完末尾 usage 后再发逻辑 finish；不能让 finish 后的 usage 丢失。provider_metadata 不能成为执行权限或恢复真相，保存前按敏感字段规则处理。

## 3. S6 + S4：唯一事件写入、投影与数据库

### 3.1 事件 envelope 和清单

Python 使用自己的 `py.*` 域事件；与 TS 的对应关系在 [parity](parity.md)，不冒充 TS wire schema。

`EventDraft` 公共字段为 `id: str`（`pattern=r"^evt_"`）、`type: str, version: int, session_id: str`（`pattern=r"^ses_"`）、`created_at: UTC datetime, data: typed event data, metadata: JsonObject`；时间序列化为 ISO 8601 UTC。ID 在重试前生成并复用，draft 不含 seq。运行内事件 metadata 必须含 run_id，其他可含 request_id/execution_id/correlation_id/causation_id，不能含凭据。

durable event = draft + `seq: int >= 1`。seq 在 DB 事务内按 session 分配；只有 durable 有 seq。ephemeral envelope 为 `id, type, session_id, created_at, data, metadata`，没有 durable cursor。

模型层统一强制四类 ID 前缀：事件 id 为 `pattern=r"^evt_"`，消息 id/message_id 为 `pattern=r"^msg_"`，session_id 为 `pattern=r"^ses_"`，run_id 为 `pattern=r"^run_"`。同一规则适用于 draft/durable/ephemeral、typed data、嵌套消息及 SessionRef/RunRef/游标；metadata 中的保留身份键 run_id 一旦出现，也必须是符合该 pattern 的字符串。request_id 由调用方给定，仍只要求非空字符串。前缀检查不替代归属验证或 seq/投影排序。

依据：TS `packages/schema/src/event.ts:9–14` 的 `Event.ID` 和 `packages/schema/src/session-message.ts:21–29` 的 `SessionMessage.ID` 分别强制 evt_/msg_。补充只读参照 `packages/schema/src/session-id.ts:5–11` 的 `SessionID` 校验以 `ses` 开头、工厂生成 `ses_`；Python 明确收紧为 ses_，统一领域标识以减少错传。**TS 无对应物 + Python 侧论证**：Python 的显式 run_id 及 run_ 模型约束没有直接 TS 对应；单独标识 run 可区分接受请求与执行身份，并使 data/metadata/返回引用使用同一身份域。

所有持久事件当前 version=1，`data` 必须按 type+version 解码为对应 Pydantic 模型：

| type | 必需 data（除表内字段均含 session 归属验证） |
|---|---|
| py.session.created | `working_dir, config_snapshot, snapshot_hash`；R1 快照精确形状、版本与哈希按 §6.1/§6.2 验证，snapshot_hash 与 config_snapshot 同属 data 字段 |
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

ephemeral 类型为 `py.text.delta/py.reasoning.delta`（message_id, part_id, delta）、`py.tool.input.delta`（call_index, delta）、`py.tool.progress`（execution_id, metadata）。delta 不落库；finalized/finished 必须含恢复所需的完整值。生产类型注册留 R2 随流式生产者落地；R1 生产注册表仅含 py.session.created/py.prompt.accepted 两个 durable 类型，ephemeral 只保留 envelope/注册机制和测试局部定义。

正常取消时尽量提交已收到的完整文本和中断终态；kill 时仍在内存的 delta 可能丢失。恢复保证已提交历史，不保证恢复未提交 token。

消息投影约束：finalized 按 message_id+part_id 定位并有唯一 ordinal；同一 part 的第二个 finalized 只接受完全相同值，不重复拼接。`step.generated` 必须是 generation batch 的最后一个事件，并与同批及此前已提交的 finalized 内容一致，同时为每个工具建立 pending 计划；step.finished 必须在所有计划工具终结后提交。其自包含 message 是持久终态，文本/工具结果必须与已折叠 parts 一致，只允许补终态 status/usage/finish/error；有差异则 StorageCorrupt，不能用旧 snapshot 覆盖。generation batch 提交失败时整批回滚，重试沿用相同 batch/event ID，不得留下只有 finalized 或只有 generated 的半批。不同 ID 的冲突终态/乱序状态迁移在提交前拒绝，读取已存坏数据时转只读诊断。

### 3.2 唯一 commit

`SessionService` 校验命令、runner/Invoker 生成事件；唯一提交接口是：

`EventBatch = {id: str, events: list[EventDraft]}`；events 非空，批内顺序固定。
`SessionStore.commit(session_id, batch: EventBatch, expected_seq: int) -> list[DurableEvent]`。

S4 的事务 writer 仅供此接口调用；Bus/工具适配器/查询 repo 无权写 events/messages。顺序：

1. writer 锁 + SQLite 写事务，先检查 session 写入门禁（首次 create 除外）；只读诊断立即返回 SessionReadOnly，再按 ID/批次查询是否已提交；
2. 整批已存在、内容/顺序/批次均一致则返回原事件，**先于 expected_seq 等值检查**；同 ID 内容不同或混合“已有 ID + 新 ID”的批次返回 IdempotencyConflict；
3. 全是新事件时先验证下述 H/A 不变量，再核对 expected_seq，校验归属、版本和合法状态迁移，分配连续 seq；
4. 插入 events；用同一 projector 更新 sessions/messages/tool_executions/permissions 和 sessions.applied_seq，一起 commit，失败整体回滚；
5. 按 seq 发 Bus 已提交通知。重试返回已有提交时不再投影或重发通知，遗漏通知由 EventLog 补读。

EventBatch.id 是稳定 batch_id，批内下标为 batch_ordinal；重试复用全部内容（含时间和 ID）。批量提交全有或全无；不得把两个旧批次拼成“同一重试”。全新批次的 expected_seq 不同才返回 Conflict。TransactionWriter 是 S4 私有实现名，外部模块只依赖 SessionStore.commit。

水位裁决：`H = COALESCE(MAX(events.seq), 0)`，范围只含该 session 已提交行；`A = sessions.applied_seq` 是同一 projector 已完整投影的最大连续 seq。尚未创建的 session 在首次 create 时逻辑 H=A=0，expected_seq 必须为 0；已有日志却缺失 sessions 行属于读模型缺失，不能当作新建会话。健康 active session 在每次事务对外可见时恒有 A=H；包含 n 个新事件的提交分配 H+1..H+n，并原子地置 A=H+n。rebuild 内部的暂时水位不可对外可见。

`expected_seq: int >= 0` 是调用方对提交前 H 的预期值，不是目标 seq，也不是独立水位；它可以因竞态落后，只有全新批次在写事务内满足 expected_seq=H 才能提交。健康时也等于 A，但不能只信 A 而不核对日志。A≠H、日志缺口/重复或非法 seq 是 StorageCorrupt，按 §3.2.1 隔离；它们不是调用方的 Conflict，不得通过改 expected_seq 继续写。健康库中旧批次的完全相同重试即使 expected_seq 落后仍返回原提交，不推进 A/H、不再次投影或通知。

SessionService、runner 和所有合法的 SessionStore 调用方必须在同一会话提交锁内读取 H/A，并把观察到的 H 作为 expected_seq；active 且完整时该值就是 A。调用方不能用任意较新的值“跳过”事件，也不能把 applied_seq 当作无需核对日志的替代值。

依据：锁定 TS `packages/core/src/bus.ts:321–327,338–395` 的 `commitDurableEvent` 读取 latest 并核对重放；`bus.ts:582–655` 的 `publishAll` 在同一事务分配连续 seq、执行 projector、保存事件；`packages/core/src/event/sql.ts:4–25` 的 `EventSequenceTable/EventTable` 保存聚合水位与唯一 seq。**TS 无对应物 + Python 侧论证**：TS 没有 expected_seq 或 sessions.applied_seq；Python 同步投影要求 A=H，显式乐观并发防止调用方把基于旧状态的新事件接到新日志上。

SessionService 的每个 session 有一把短期提交锁：读取状态/当前 seq → 生成本次命令事件/expected_seq → commit → 交付本地通知，整个过程串行；prompt、approval/expiry、cancel、恢复和 runner 结果回调都走它。**不在等待模型、审批或 adapter 时持锁**，也不在命令处理里等待自己持有锁的回调。cancel 先设置取消信号并关闭 pending 审批，started 提交再次检查取消状态；若结果已先提交则保留原终态，后来的重复完成回调只作去重。权限 pending 的回复/过期/关闭只允许首个提交的合法迁移，迟到回复返回 ApprovalExpired/Conflict。

重试只重试提交，不重新执行 adapter。取消若撞上 commit，先按批次 ID 确定事务结果，再补需要的终态；不得假设收到 CancelledError 就一定没写入。

初始迁移 `0001` 使用 Python 自己的最小 schema：

- `events`：id 主键、session_id、seq、type、version、时间、data/metadata JSON、batch_id、batch_ordinal、可选 dedupe_key；unique(session_id, seq)、unique(session_id, batch_id, batch_ordinal)、unique(session_id, dedupe_key)；
- `sessions`：状态、active_run_id、配置快照及 snapshot_hash、applied_seq、可选 diagnostic；事件创建时投影，诊断隔离记录按 §3.2.1 保留；
- `messages`：session_id、message_id、顺序、JSON；unique(session_id, message_id)；
- `tool_executions`：session_id、execution_id、run_id、step_id、message_id、tool_call_id、input_hash、state；unique(session_id, execution_id)、unique(session_id, step_id, tool_call_id)，由 commit 校验其归属和终态不可逆；
- `permissions`：待审批和保存决定的投影；input_hash、作用域、有效期、终态。

R2 在 `0001` 后追加 Alembic revision `0002`，新增第五个读模型；不改写 `0001`、既有事件或其 wire/version：

- `runs`：`run_id` 主键、`session_id`、`request_id`、`status`、可选 `error`；unique(session_id, request_id)。`py.prompt.accepted` 投影 `accepted` 行，`py.run.started`、`py.run.cancel_requested`、`py.run.finished` 按 §2.1 合法迁移分别更新为 `running`、`cancelling` 和 `succeeded/failed/interrupted`。所有更新由同一纯 fold/projector 在 events、其他投影及 applied_seq 的同一 commit 事务中完成，失败整体回滚；rebuild 复用该 fold，events 仍是权威来源。

`0001 → 0002` 升级须按 §3.3 备份并验证旧数据，在对外提供 runs 查询前按 session/seq 用同一 fold 回填全部 accepted。R1 多个 accepted 均保留为 accepted，原有消息、session 状态与最后一个 `active_run_id` 不变；不得新增“前一个 run 必须终态才接受下一个”的 fold 门禁，不补写终态、不自动调度。`run_status(run_id)` 通过 run_id 查表解析归属和状态，正常查询不扫描全量 events；所需投影缺失、归属或状态损坏遵循 §3.2.1 返回 `StorageCorrupt`，不得臆造 RunStatus。

events 不依赖可重建的 messages 外键。创建 session 与其首条事件同事务。seq 在写事务内按已有最大值 + 1 分配，初值 1；不删除日志前缀、不重用 session ID。

Phase 1 每个数据目录只允许一个 writer 进程（持有 OS 文件锁）；SQLite 使用 WAL、foreign_keys、busy_timeout，进程内写事务串行。第二个进程明确失败；asyncio/aiosqlite 本身不提供多 writer 正确性。M1 才增加租约/跨进程协调。

`SessionStatus` 只有 `active`、`read_only_diagnostic`、`cleanup_failed`、`closed`。关闭阶段无法确认资源回收使 `active → cleanup_failed`；诊断隔离规则如下。三种非 active 状态都不自动恢复为 active：业务写命令分别以 SessionReadOnly、LifecycleCleanupFailed 或 SessionClosed 拒绝。只有经过验证的备份恢复、迁移或人工修复命令才可清除诊断状态；Phase 1 不提供静默修复。`SessionService.close()` 是进程生命周期操作，不会把正常 session 永久标记为 `closed`；`closed` 仅供显式会话关闭/管理操作，若未提供该命令则不会产生该状态。

### 3.2.1 只读诊断裁决

- 检测范围：打开/重建/读取已存日志时，未知 type 或 version、无效 JSON/必需字段/content type、快照哈希不符、归属/状态迁移损坏、seq 不从 1 连续递增（含缺口、重复、非法值），以及已存投影不一致，都使该 session 进入 read_only_diagnostic。会话首次允许写入前必须验证日志和投影水位；读模型缺失时先重建，失败即隔离。仅提交输入不合法时拒绝并回滚，不把调用方的坏输入当作已存损坏，也不隔离健康 session。
- 隔离由 SessionStore 的私有 writer 在会话锁下完成；发现问题的业务/重建事务先整体回滚，再用独立短事务持久化 sessions.status 与脱敏 diagnostic（reason、可定位时的首个坏 seq/event_id、发现时的 applied_seq）。这是受控的管理状态写入，不追加业务事件、不推进 seq/applied_seq、不通知 Bus；EventLog/repo 只能请求该隔离入口，无权自行写表。DB 无法持久化隔离时保持内存写门禁并报告存储错误，下次打开重新验证，不声称隔离已持久化。
- 保留最后一次已提交的读模型及其 applied_seq；它是历史投影水位，不再证明当前日志完整或 A=H。若 sessions 行已被删除，隔离入口可建立最小诊断记录（status、diagnostic、applied_seq=0，配置/active_run_id 为空）；这不是 py.session.created，也不能转为可执行会话。缺失投影不伪造为空历史或成功的 RunStatus。
- “读取仍可用”指 session_status 和未损坏的已投影 messages/run_status 仍可查，查询不重放坏日志、不触发恢复或外部调用。读模型自身某条数据无法解码，或所需投影缺失，则该查询返回 StorageCorrupt（details.reason=projection_unavailable 或具体损坏原因）；其他可解码投影与状态查询仍可用。它不承诺磁盘/DB 不可读时仍能返回数据，也不保证现存投影与损坏后的日志相符。
- typed EventLog 的坏日志读取按 §4 报 StorageCorrupt；不把原始坏行塞进 DurableEvent。进入诊断后 prompt（含重试）、cancel、权限回复/撤销、runner/tool 回调及所有新业务 commit 均以 SessionReadOnly 拒绝。健康库的幂等成功不构成绕过诊断门禁的例外。发现损坏的那次操作报 StorageCorrupt，后续写命令报 SessionReadOnly；显式读 API 用于查原结果。
- 重新打开进程、成功读取旧投影或再次 rebuild 都不能自动清除诊断标记；重建须保留该管理标记。诊断期间只允许读取、资源释放和受控隔离管理，不能自动追加 interrupted/unknown 等“修复”事件。

依据：锁定 TS `packages/schema/src/event.ts:28–34,174–186` 的 `Version/DurableEnvelope/versionedType/durableMap` 按版本辨认事件；`packages/core/src/bus.ts:674–703` 的 `replay` 对未知 durable 定义失败，但 `bus.ts:786–827` 的 `readAfter` 会跳过未注册类型并推进 raw seq。**TS 无对应物 + Python 侧论证**：TS 没有本节的持久只读诊断协议；Python 必须显式偏离跳过行为，避免把不完整历史当作可信执行依据，同时保留可解码投影供诊断。

### 3.3 重放、迁移与副作用

projector 是确定的状态折叠：`apply(state, event) -> state`，不读时钟、不生成随机 ID、不执行工具、不发模型请求。消息创建时间取事件时间；idle ID 由 run_id 确定。rebuild 在会话锁和同一事务下重建该 session 的读模型、按 seq 重放并更新 applied_seq；不会再次发业务通知或生成新事件。仅在全量验证和投影成功后提交替换；失败时包括清空操作在内全部回滚，保留原投影，再按 §3.2.1 隔离，不能提交坏点前的半份读模型、跳过坏点或借重建清除诊断状态。

恢复检查不是纯 replay 的一部分，R1 只做日志/投影重建，不做下述运行恢复补写；read_only_diagnostic 时禁止运行恢复写入。运行恢复协调器获得独占运行权且会话通过完整性验证后，按已提交 py.step.generated 的完整计划处理：pending → cancelled/not_started，started 无 finished → unknown，已有 finished 原样保留。先追加待审批 py.permission.closed，再补工具终态、包含全部结果的 interrupted py.step.finished 和 py.run.finished；同一恢复批次使用稳定 ID，重复启动可幂等恢复。

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
返回 seq>after.seq 的 durable 事件。None 等于本 session 的 seq=0；limit 为 1–1000。session 不同、负数或编码非法为 InvalidCursor；seq 超过当前高水位 H 也明确报 InvalidCursor，不空等。这里 H 是 §3.2 的已提交日志最大 seq，不以投影水位代替。Phase 1 不裁剪日志；M1 若裁剪必须新增 CursorExpired/重建协议，不能在旧协议中静默跳过。

读取必须按原 seq 连续解码 type+version；发现 §3.2.1 所列损坏时，read 整页失败并报 `StorageCorrupt(retryable=false)`，不返回半页、不推进 next_cursor。details 包含脱敏 reason、session_id、可定位的坏 seq/event_id 及 last_confirmed_cursor，不含坏行原文；read 失败时该 cursor 等于输入 after（None 为 0），follow 则为最后已 yield 的事件游标，不取失败页内部已解码但未交付的 seq。发现前已经返回的健康页仍有效。进入 read_only_diagnostic 后，typed read/follow 都报 StorageCorrupt，不能凭更大的 after 或类型过滤绕过坏点；查看原始坏字节属于显式诊断工具，不在 DurableEvent/EventPage 接口内。

`EventLog.follow(session_id, after: EventCursor | None = None) -> AsyncContextManager[AsyncIterator[LogItem]]`：

1. 先注册 wakeup，再在短读事务中捕获已提交高水位 H；该读取是初始游标验证/补读窗口的线性化点；
2. 分页补读 after.seq<seq<=H，发一次 `Synced(cursor=EventCursor(session_id, H))`；
3. 此后始终查询 seq>last_seq，只把 Bus 当唤醒信号；重复信号合并、最长 1 秒轮询兜底。H 前后的 commit 由查询范围分开，不把通知 payload 再发送一遍；
4. 同一连接按 seq 严格递增；重连允许 at-least-once，消费者按 session+seq/id 去重。补读、yield 和等待期间不持会话提交锁，不阻塞 runner。

follow 遇到坏页时可保留此前已 yield 的健康事件，随后抛 StorageCorrupt 并终止，退出时注销 wakeup；不得 yield 坏事件、后续事件或越过坏点的 Synced。若坏点发生在初始 H 之后，已经发出的 Synced(H) 仍只确认旧窗口；新窗口失败不能伪造新 marker。该错误和诊断状态不改变“已投影读取可用”的 §3.2.1 保证。

follow 只保证 durable，不回放 ephemeral token。实时 UI 可用 Bus 做预览，以 durable 完整值覆盖；M5 首版只保证 durable SSE。游标不可跨 session 使用。outbox、全局 cursor 和跨实例 wakeup 不在 Phase 1。依据为 §3.2.1 引用的 TS readAfter/log 差异；Python 不沿用 TS 跳过未知类型并推进 cursor 的行为。

## 5. S3：工具、权限与生命周期

### 5.1 唯一调用入口

`ToolSpec`：`namespace, name, version, description, input_schema, supported_content_types, side_effects, idempotency_support`。
`ToolAdapter.execute(context, validated_input) -> ToolResult` 仅注册中心内部可访问。`ToolResult` 为 `content, structured_output?, metadata, error?`；不用字符串承担所有结果。

`ToolRegistry` 只公开 `register(adapter)`、`snapshot(session) -> ToolCatalog`、`invoke(context, tool_ref, input, execution_id) -> ToolOutcome`。重复 namespace/name/version 拒绝；模型名通过可逆/碰撞检测映射，MCP server ID 必须参与命名。每次 provider 请求固定 catalog 快照，只能调用已展示且有 capability 的工具。`invoke` 的 `tool_ref` 参数接收调用接缝保留的原始 requested name（S5 catalog 接缝为 wire 名，如 `fs__read`；模型层点分名按 §2.2 转换），由 `invoke` 在当前 catalog 快照内解析；调用方不得预解析，未知名仍产生 requested→finished(not_started) 配对，事件中的 `requested_name` 保留原值，解析成功时才填可选 `tool_ref`。

`ToolContext` 是运行时 dataclass：session_id、run_id、message_id、tool_call_id、working_dir、limits、取消信号和 progress callback；run_id 用于工具事件 metadata，tool_call_id 用于 `py.tool.requested` 身份。这些是运行时上下文字段，不扩展已版本化的 wire 形状。PermissionPolicy 注入 Invoker，工具不能自行批准自己。

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

运行期 `ConfigSnapshot` 承载 schema_version、model/endpoint_ref、允许 roots、permission rules、指令文本+provenance、MCP ref+启动指纹和 limits；其摘要统一放创建事件的 snapshot_hash，不在快照内部重复存 hash。R1 使用下面的存储专用最小版本，不能冒充可执行配置。每份 AGENTS.md 上限 64 KiB、总指令 256 KiB，超限明确报错。指令按外到内排列，不提升系统权限；不自动执行文档里的命令或加载其中声明的插件/MCP。

### 6.1 R1 最小快照

R1 `config_snapshot` 的字段全集精确为 `schema_version: Literal[1]`、`permission_rules: list[JsonObject]`，且 permission_rules 必须为空；两字段都必须显式存在，版本号必须是整数 1，不接受额外键、null、字符串/布尔/浮点版本号或非空规则。最小且唯一合法对象为 `{"schema_version":1,"permission_rules":[]}`；事件 data 另含 working_dir 和 snapshot_hash。R1 不装载配置、不求值规则；空列表不授予工具权限。运行所需的 model/endpoint/roots/instructions/MCP/limits 不伪造默认值进入此版本。后续可执行快照使用新的 schema_version 并显式提供运行字段，保留版本 1 的解码和旧 fixture；不得在重启时读取当前配置来静默补齐旧快照。

### 6.2 canonical JSON 与摘要

1. 先对已完成敏感字段处理的快照验证 schema；拒绝重复 JSON 对象键、非字符串键、NaN/Infinity、非法 Unicode（含孤立 surrogate），不能先经类型强转“修复”再算 hash。
2. 只序列化 config_snapshot 对象本身：对象键递归按 Unicode code point 升序；数组顺序保持；键值/元素分隔符固定 `":"`/`","`，无多余空白、缩进或末尾换行；非 ASCII 字符保持原 Unicode，不做 NFC/NFD、大小写或换行归一化，JSON 必需的引号/反斜杠/控制字符转义仍保留。UTF-8 编码，无 BOM。规范算法为 Python 3.12+ `json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`；R1 值域只有整数 1 和空数组，无浮点格式差异。
3. `snapshot_hash = hashlib.sha256(canonical_bytes).hexdigest()`，恰为 64 位小写十六进制，无 `sha256:` 前缀。hash 不纳入 config_snapshot；working_dir、事件 id/时间/metadata 也不参与。create 从快照计算摘要；提交和读取 py.session.created 时重新验证，输入不符为 ValidationError，已存不符为 StorageCorrupt 并隔离。
4. 版本 1 的 canonical 文本固定为 `{"permission_rules":[],"schema_version":1}`，对应摘要固定为 `77c932c31585517c128479af02c40cd377e26d4d25ce57df197fc8509e14f802`。键输入顺序或 JSON 表面空白改变不改变摘要；合法内容改变则按重新规范化后的字节计算，不能沿用旧摘要。

依据：锁定 TS `packages/schema/src/session-event.ts:39–57` 的 `Created` 含 permissions/model 等创建字段，`packages/core/src/config/discovery.ts:15–25,59–145` 的 `Sources/discover` 定义发现来源；它们都没有 Python 的最小快照和 canonical hash。**TS 无对应物 + Python 侧论证**：R1 未接入 ConfigLoader/模型/工具，用关闭扩展字段的最小存储版本即可验证持久化，不制造运行权限；摘要仅覆盖非秘密配置内容，排除自身可避免递归，固定序列化字节可重启重算并检测漂移。

快照是运行行为基线：存可重建的非秘密配置及指令文本，凭据只存引用；URL userinfo/query 和敏感键不能原样入库。恢复若工作目录失效、凭据引用失效或 MCP 启动指纹变化，明确停住并要求宿主重新配置/批准，不能静默换一套权限。Phase 1 无热重载。

MCP 只支持显式用户配置的 stdio server，argv 直接 exec、不拼 shell，环境变量为最小集和显式 secret refs；启动前宿主确认该 server 的完整本机权限。SDK manager 拥有子进程、会话、退出栈和重连：调用超时/断线按 unknown 处理，不自动重试副作用；最多重建连接供下一次显式调用。远端 tools/list 改变后先建立新 catalog，不能用旧批准调用改名工具。

## 7. S6：应用命令接口和任务所有权

`SessionService` 是 CLI、同进程调用和未来 HTTP 的唯一入口：

| 方法 | 结果/保证 |
|---|---|
| `create(config_snapshot) -> SessionRef` | 创建并持久化 session |
| `session_status(session_id) -> SessionStatus` | 返回 active/只读诊断/清理失败/closed；不触发恢复或外部调用 |
| `prompt(session_id, text, request_id) -> RunRef` | R1 仅原子持久化 py.prompt.accepted 并返回 RunRef，不调度；幂等、输入冲突及状态门禁按下文 |
| `cancel(run_id) -> RunStatus` | 提交 cancel_requested、幂等请求取消；释放提交锁后等待资源清理和持久终态 |
| `reply_permission(permission_id, reply) -> DecisionReceipt` | 宿主输入通道；与运行锁分离，避免等待审批时死锁 |
| `revoke_permission(decision_id) -> None` | 宿主撤销已保存规则；不撤销已完成副作用 |
| `messages(session_id, after_message_id?, limit=100) -> MessagePage` | 有序分页；cursor 必须属于该 session |
| `run_status(run_id) -> RunStatus` | 运行、完成、失败、中断及错误信息 |
| `close() -> None` | 按下述顺序取消并收回自有任务，关闭 provider/MCP/DB；不得遗留后台任务 |

`SessionRef={session_id: str}`；`RunRef={session_id: str, run_id: str, request_id: str}`，三项均为非空字符串，session_id/run_id 另须满足 §3.1 的 ses_/run_ pattern，分别指向原 session、首次接受时生成的 run 和调用方请求，不含调度承诺；`RunStatus` 含上述标识及 `status: accepted/running/cancelling/succeeded/failed/interrupted`、可选 error。`MessagePage` 含 items、next_message_id 和 has_more；消息游标按投影顺序分页。`DecisionReceipt` 含 permission_id、decision 和 execution_id，不携带可复用执行权限。

同一 session 最多一个 active run，包含 accepted/running/cancelling；Phase 1 不支持 steering/inbox。R1 的 prompt 只提交一个 `py.prompt.accepted(version=1)`，data 中 request_id/run_id 与返回 RunRef 一致，message 为本次 text 创建的 UserMessage，metadata 含同一 run_id/request_id；其 projector 同事务插入一条 UserMessage、置 active_run_id 并得到 accepted 的 RunStatus。只有事务提交成功才返回；不产生 run.started、assistant/idle 或调度任务，不调用 provider/工具。

prompt 在每 session 提交锁下按以下固定顺序处理：

1. 验证 session_id/request_id 是非空字符串、session_id 符合 §3.1 的 ses_ pattern、text 是字符串（不隐式转换）；检查 session 存在及其写入门禁。尚不存在返回 NotFound；read_only_diagnostic 时始终返回 SessionReadOnly，包括同 request_id 的重试；cleanup_failed/closed 分别返回 LifecycleCleanupFailed/SessionClosed。session 为 active 后再检查 service 的 closing/closed gate，关闭中或已关闭返回 SessionClosed。
2. 在 durable 日志中以 `(session_id, dedupe_key)` 查原接受记录；key 精确为 `"prompt:" + request_id`，大小写敏感，不是字面字符串 prompt:request_id。key 是存储字段，由 py.prompt.accepted 的 request_id 派生，并受 §3.2 唯一约束保护，不要求调用方传 event ID。相同 request_id 可用于不同 session。
3. 已存在且原 message.text 与本次 text 逐 Unicode code point 相同，则直接返回原 RunRef，不生成新 message/run/event/batch ID，不重投影或重发通知；不 trim、不归一化换行/Unicode。run 已终结时也返回首次的 RunRef，当前状态另查 run_status。相同 key 的输入不同则 IdempotencyConflict，即使当前 busy；比较不使用新生成的 ID/时间，也不只依赖摘要。
4. 无原记录时才检查 active_run_id：存在则 SessionBusy；不存在才生成并固定 message/run/event/batch ID 与时间，按 §3.2 读取 H 作为 expected_seq 并提交。完全相同的提交重试复用这些值；提交失败/回滚不留下 request_id、消息或 active run。不同 request_id 的并发 prompt 最多一个成功；相同请求并发成功后都指向同一个 RunRef。

R1 的 accepted 持续占用 active-run 槽，关闭并重开后保持 accepted；新 request_id 仍返回 SessionBusy，同一请求仍按上述规则幂等返回。R1 close 只释放其 storage/Bus 等自有资源，不为存储专用 accepted 追加取消/终态；R1 不实现下述运行 cancel/恢复协调器。R2 接入调度后仍先提交 accepted 再调度，只调度本次新接受的请求，幂等重试不能再次调度；启动恢复将未结束 run 终止为 interrupted 的行为由运行恢复协调器实现，绝不自动续跑 R1 遗留 accepted。SessionService 对实际调度任务持有强引用；CLI 等待 RunStatus，用 Bus 看实时输出、EventLog 看可靠历史。

依据：锁定 TS `packages/schema/src/session-event.ts:213–228` 的 `InboxDelivered/InboxEnqueued` 将输入接收独立记录，`:244–263` 的 `Execution.Started/Interrupted` 定义执行生命周期。**TS 无对应物 + Python 侧论证**：TS 无本节 SessionService.prompt/RunRef/request_id 幂等协议；Python 不实现 TS inbox，使用独立接受事件使 R1 无模型依赖即可验证用户消息与请求身份持久化；accepted 占槽可防止尚无调度/恢复协调器时静默积压多个 run。

cancel 的 dedupe_key 为 cancel:run_id；已结束 run 直接返回原 RunStatus。未结束则提交取消意图并关闭待审批/未开始的调用，状态改为 cancelling；已开始工具仍如实保存已确定的结果，不把成功伪装成“未执行”。如果 py.run.finished 先提交，迟到 cancel 不覆盖；取消意图先提交时不再启动新 step/tool，清理后 run 结束 interrupted。重复 cancel 等待同一清理任务；迟到完成回调按 execution_id 去重。若清理无法确认，报告 LifecycleCleanupFailed，保持该 session 不可再执行，不能假装已回收并启动下一 run。

R2 起的 `close()` 运行生命周期（R1 按上文只释放资源）：首次调用先原子地进入 closing gate，拒绝新的 prompt、工具调度和权限写入；为每个可写 session 的活动 run 提交 `py.run.cancel_requested(reason=shutdown)`，并为 pending approval 提交 `py.permission.closed(outcome=cancelled)`。随后释放提交锁，等待 provider stream、adapter、MCP 和子进程结束；等待期间仍允许通过同一 `SessionStore.commit` 为可写 session 写入已确定的 tool/step/run 终态。诊断 session 只清理资源，不能越过 SessionReadOnly 追加业务事件。达到关闭超时而外部结果仍不确定时，若 session/DB 可写则提交 `tool.finished(effect_status=unknown)`、interrupted 的 step/run 终态并把 session 置为 `cleanup_failed`；若不可写则保留未完成记录供显式诊断或下次通过完整性验证后的恢复，不得报告已清理。只有所有自有任务已退出或已被 durable 标记后，才依次关闭 MCP、provider、DB，并把 service 标记为 closed；这不改变可恢复 session 的 `SessionStatus`。并发或重复 `close()` 复用同一个清理任务，返回相同结果/错误且不重复提交关闭事件。

ContextBuilder 保留完整工具 call/result 配对，只接受 provider 支持的内容；预留输出 token + 安全余量。R0 获取模型上下文上限；未知时要求宿主配置。达到预算时返回 ContextLimitExceeded，保留历史、提示新建会话；Phase 1 不静默裁剪或做无边界摘要。

max_steps 默认 20；超限结束为 failed。工具默认串行，调用异常/denied 转成模型可读 tool result；存储、权限基础设施错误立即 fail closed。取消由 service 协调传播到流和子进程，资源关闭后才确认终态。

## 8. 错误、重试和兼容

`CoreError = {code, message, retryable, details: JsonObject}`；构造/输入时可省略 details，wire 输出恒包含 details，缺省值为工厂创建的 `{}`，有信息时保留脱敏对象。code 至少区分：
`ValidationError, UnsupportedCapability, NotFound, SessionBusy, Conflict, IdempotencyConflict, InvalidCursor, PermissionDenied, ApprovalUnavailable, ApprovalExpired, ToolTimeout, ToolFailed, ExecutionUnknown, ProviderUnavailable, ProviderStreamInterrupted, ContextLimitExceeded, SlowConsumer, StorageCorrupt, MigrationRequired, SessionReadOnly, SessionClosed, LifecycleCleanupFailed`。

R2 文件读取只支持文件目标；目录目标因此返回 `UnsupportedCapability`（消息可保留 “Path is not a file” 语义），不得将目录拒绝归为 `NotFound`。

details/异常链做脱敏，不把认证头或原始 SDK request 暴露到 wire。用户错误与内部故障分别日志记录，但对外错误结构相同。

只有一个 retry owner：MAAS adapter 关闭 SDK 默认重试，未收到首个内容且明确可重试的 429/短暂网络错误最多 2 次重试（尊重 Retry-After；Phase 1 使用确定性的 `min(retry_after, remaining)` 等待，无 Retry-After 按 0 处理，不加入抖动，并受总超时限制）。部分流已到达则结束并标 interrupted，不能把旧流和新流拼接。用户要求重试生成时创建新 attempt；既有 tool.finished 绝不重新执行。MCP、shell、文件变更默认不自动重试。

版本演进以旧 JSON fixture/旧 DB fixture 能读取为最低门槛。新增 event/content/capability 不等于旧消费者自动支持；不支持时显式协商或报 UnsupportedCapability，不能只靠“字段可选”声称向后兼容。

`py.*` v1 采用封闭 wire 形状：envelope、typed data 或嵌套消息新增字段（包括可选字段）一律升级受影响事件的 version，并提供显式、确定性的 upcaster；不走同版本新增可选字段路径。新版读者先用保留的旧版本模型校验旧记录，再转换为当前内部形状；旧 fixture 保持可读，原日志不改写。已声明 JsonObject 内的诊断扩展键不是新增结构字段，但不得借此增加执行/恢复所必需的语义来绕过版本升级。

未知结构字段在已存日志中等同 schema 损坏，由 S4/S6 映射为 StorageCorrupt 并按 §3.2.1 进入只读诊断；新提交输入非法仅拒绝/回滚，不隔离健康 session。旧读者遇新版本仍明确失败，不声称可自动读取新字段。依据：TS `packages/schema/src/event.ts:28–32,174–187` 的 `Version/versionedType/durableMap` 按类型与版本区分定义，`packages/core/src/bus.ts:74–88` 的 `decodeStoredData/decodeSerializedEvent` 集中转换并校验旧载荷；**TS 无对应物 + Python 侧论证**：全量 forbid 与逐版本 upcaster 的闭合演进协议是 Python 裁决，严格版本边界避免旧读者将同版本新增字段误判为已存损坏，或静默丢字段后继续执行。

## 9. T-A-002 与 B 草案的冲突及最终口径

本节对照只读的 `doc/plans/r1-slices.md` §2/§4；本契约为最终实现依据。与 `doc/reestimate-r0.md` §2 的同事务、幂等优先、坏数据隔离取向一致；R1 保留 prompt 是本次用户授权的范围澄清。

| B 草案假设/冲突点 | 最终口径与实现对齐 |
|---|---|
| §4.2“schema_version+空规则+hash”，未定义字段名/字节算法；并行 R1-S1 已采用 ConfigSnapshot.hash、宽版本号与可省略/非空规则 | 支持空规则与 SHA-256 方向；§6.1 的快照只有显式 schema_version=1、permission_rules=[]，hash 仅在创建事件 data.snapshot_hash。C 的 R1-S1 模型/fixture/测试须移除内嵌 hash，拒绝省略规则、额外字段、非空规则和非整数 1；既有最小对象的摘要字节不变。R3 不可静默改变版本 1 的含义。 |
| §4.4“expected_seq 恒等 applied_seq”，未区分陈旧调用参数和损坏投影 | 健康已提交状态 A=H；expected_seq 是预期旧 H，新批次不匹配报 Conflict，旧批次完全相同重试先于等值检查；A≠H 是损坏而非乐观并发冲突。S3/S4 按 §3.2 对齐。 |
| §4.1 提议 prompt 仅持久化，并列出删去 prompt 的备选 | 正式保留 prompt，备选不采用；R1 不调度，与草案主方案一致。补全 RunRef、精确 key/输入比较、状态门禁→幂等→busy 顺序；R1 accepted 重启后仍占槽，不自动补终态。S4/S7 对齐。 |
| §4.3 提议坏日志 read 返回 StorageCorrupt，但 S8 笼统称 EventLog.read 仍可用 | 采纳 §4.3 的错误取向；“读取可用”专指可解码的已有投影/状态，诊断后的 typed read/follow 报 StorageCorrupt。坏页不返回半页、不推进游标，follow 不发越界 Synced，重建失败保留原投影且诊断不自动清除。S6/S8 对齐。 |

上述都是 Python 契约补充/有意差异，TS 证据及 Python 侧理由分别见 §3.2、§3.2.1、§6、§7；不据此宣称实现或 TS wire 兼容已验证。

## 10. T-A-003 与 B 审查的对齐及实施交接

对照 `doc/reviews/R1-S1-review.md` §2/§3/§5：M-2 选择 B 给出的方案 (b)，以最小文本 v1 避免空列表/FileContent 占位；M-3、四类 ID 前缀、整数拒绝 bool、R5 JSON Schema、R2 ephemeral 注册与两项 parity 记录均采纳 B 建议，无裁决方向分歧。ses_ 相比 TS 校验更严格及 run_ 为 Python 专有约束已在 §3.1 明示，不以“对齐 TS”隐去差异。

本次完成契约定稿，不表示 S1 已实现新增约束；后续由 B 将裁决并入方案、C 在获授权的 contracts 补丁中落实 ID pattern、version/seq before-validator 与相应反例，更新 UserMessage 的 files 阶段说明，并一并处理 B 的 M-4/N-2 补测。现有 R1 files 拒绝行为与裁决一致，无需引入 FileContent；本任务不修改实现或测试。

`[T-A-005] 2026-11-03`：OP-2 采纳 `0002` runs 读模型。§7 的 `run_status(run_id)` 需要保留历史 run 的归属和终态，sessions.active_run_id 单值不足；每次扫描 events 又无法直接使用 §3.2.1 要求保留的诊断读模型。TS `packages/core/src/session/projector.ts:481–535` 展示事件驱动的 message/idle 投影，Python 显式 run_id 的读模型按自身接口补充，不声称 TS 表结构同构。追加 revision 符合 `doc/PHASE1-SLIM.md:69–74`；§3.2 的同事务、回填及多 accepted 兼容约束承接 `doc/plans/r2-slices.md:25–33,73–84` 和 `doc/reviews/R1-S7-review.md:94–100`。B/调度器须将裁决并入 R2-S2/S7 方案与任务书，C 后续验证真实旧库升级、历史 run 查询、回滚和 rebuild 一致性。

同次 OP-7 选 B：§8 原“带抖动”修订为 Phase 1 无 jitter 的确定性等待，保留 retry owner、次数、Retry-After 和总 deadline。jitter 用于打散并发调用者的重试时刻、减轻雷鸣群；TS `packages/core/src/model-retry.ts:22–35,62–103` 的通用 jittered schedule 由 buffered/one-shot 调用复用，兼顾服务端并发场景。Python 本阶段以本地单用户 CLI 和单 active run 为目标（`doc/PHASE1-SLIM.md:78–85`），通常串行发请求，尚无同步重试拥塞的证据，故保留 `doc/evidence/r0/provider.md:53–60`、`tools/r0/provider_spike.py:340–364` 已验证的有限确定性策略；R0 证明实际行为，不证明所有并发场景都不需要 jitter。单 writer 不保证跨 session 只有一个 provider 请求；若引入并发宿主/多进程，再评估并修订本条。§10 以本条记录显式契约变更；B/调度器须同步 S3 方案/任务书并安排确定性延迟、Retry-After/总 deadline 与两次重试上限验证，冻结的 R0 证据保留原文。

`[T-A-006] 2026-11-03`：OP-4 终审维持 S3 落法并关闭：冻结 spike 的七个 fixture 再生成须逐字节相同，正式 adapter 对相同七场景采用新 golden，两者不要求 JSON 逐字节相同。证据延续结果见 `doc/evidence/r0/provider.md:94,137–150`；正式差异以 §11.2 七条为准，其中终止 ErrorChunk、Pydantic 全字段面（含空 metadata 与可选 null）、可反解工具名的 `fs__read → fs.read` 分别兑现本契约 §2.3 L98/L102、§1 和 R0 §6-2。TS `packages/core/src/aisdk.ts:784–812,841–856` 保留 provider toolName、以 Effect.fail 交付流错误，Python 按自己的请求/chunk 接缝正式化，不宣称 TS wire 或 spike JSON 同构；这符合 `doc/PHASE1-SLIM.md:78–83` 的 adapter→runner 全链验收。§8 L350 与 `doc/evidence/r0/provider.md:55–60,147` 的重试边界不因 golden 差异而放宽；B/调度器须据此把 r2-slices §4 OP-4 标为已裁决，保留冻结 R0 原文和 fixture。

同次 OP-8 终审维持：5xx 交付一个 `ProviderUnavailable(retryable=True)` 终止 error chunk，adapter 不自动重试；retryable 是错误属性，不授权 runner 增设自动重试。依据为 §8 L350 的 429/连接建立失败集合、`doc/evidence/r0/provider.md:55–60,80,142,147` 及 `src/opencode/ai/maas.py:450–468`。TS `packages/core/src/model-retry.ts:80–103` 的 notRun 明列 503/529 等条件，Python 本阶段缺少真实端点支持该扩展的对照证据，保守收窄可避免重复请求，与 `doc/PHASE1-SLIM.md:82–83` 的中断不拼接和生成次数验证一致。B/调度器须将 r2-slices §4 OP-8 标为已裁决，并派发 ③-3 status=500 注入测试：恰一个末位 error、code=ProviderUnavailable、retryable=True、仅一次请求且无 finish；该补测是后续义务，本次裁决不声称已覆盖。

同次 ④-3 选 A：§2.2 L80 明定 `LLMToolCallPart.name` 为 `namespace.name`，避免 S4 产出裸 `read` 或误传 wire 名使 namespace 丢失。依据为 `doc/reviews/R2-S3-review.md:117–121,129,133`、`doc/evidence/r0/provider.md:73,144`、`src/opencode/ai/maas.py:99–119,325–335`；TS `packages/core/src/aisdk.ts:784–812` 原样携带工具名，Python 的双向命名映射须在本地接缝明确。B/调度器须让 S4/S6 任务书引用 §2.2，S4 核对请求 part 的点分名及 `fs.read ↔ fs__read` 往返，S6 核对 chunk→计划→请求回放的一致性并承接跨响应重复 tool_call_id 查重（`doc/evidence/r0/provider.md:154–155`），满足 PHASE1-SLIM L80–83 的调用/结果配对；adapter 对不可反解 provider 名的原样保留、未知工具 not_started 失败语义和 S5 注册面已有的 `__`-free 约束不变。

排期建议（非新增实现授权）：M-1 注释、M-2 连接建立失败补测和 OP-8/③-3 的 500 补测合为独立 adapter 微补丁，在当前 S4 窗口安排、S6 接入前验收，不留到 S8。M-1 仅记录 `asyncio.timeout(header_s)` 与 connect timeout 竞争及两路径皆合法失败的理由（`doc/reviews/R2-S3-review.md:19–28,130–132`）；M-2 用立即拒绝的未监听端口避开计时竞态，核对单个终止 error、ProviderUnavailable/retryable=True、attempts=3，并直接计数初次加两次重试，不以耗时下界推断零等待重试次数。两项补测同落 `tests/contract/test_ai_adapter.py`，M-1 注释落 maas.py；此处只记交接，冻结的实现、测试、方案及任务书由下游另行获授权后处理。

`[T-A-007] 2026-11-03`：A 席位终审追认 R2 五项，依据 `doc/reviews/R2-S2-review.md` §5–§6、`doc/reviews/R2-S5-review.md` §5–§6 及已冻结契约/迁移/测试证据：

1. **S2 ④-2 `runs.run_id` 主键：维持 GLOBAL 单列主键。** §3.2 已明确该主键，§7 的 `run_status(run_id)` 以 `run_id` 直接定位单行；T-A-005 已明确 Python 读模型按自身接口补充、不承担 TS 表结构同构义务。保留该不变量不触发迁移或测试联动；跨 session 撞键的 `_write_accepted_run` 诊断文案属于后置小修，不改变本裁决。
2. **S2 ④-3 idle ID：确认 `msg_idle_{run_id}`，关闭为“契约已裁”。** §3.3 已规定 idle ID 由 `run_id` 确定，Python projector 的构造与之相符；TS 侧以事件 ID 实现是实现差异，不改 Python 契约或投影。
3. **S5 ④-13 目录读取错误码：追认为 `UnsupportedCapability`。** R2 读取能力面不提供目录列举，目录目标是能力类型不支持而非资源缺失；§8 已补充该选型及 “Path is not a file” 消息语义。该裁决与 `PathKindError` 锚点及目录测试保持一致。
4. **S5 ④-1 `ToolContext`：确认增补 `run_id` 与 `tool_call_id`。** 两字段分别满足工具事件 metadata 的 `run_id` 和 `py.tool.requested` 的 `tool_call_id`，并与 TS Context 的 call ID 对齐；§5.1 已注明它们属于运行时上下文，不是 wire 版本字段。
5. **S5 ④-3 `invoke` 的 `tool_ref`：确认接收原始 requested name，并在 `invoke` 内解析。** S5 catalog 接缝保留 wire 名（如 `fs__read`），模型层点分名按 §2.2 在接缝转换；§5.1 已明确解析使用当前 catalog 快照。未知工具仍可先提交 requested、再提交 `finished(not_started)`，同时保留原始 `requested_name`，因此满足未知工具配对契约。本裁决不替代 S5 M-1 的命名碰撞热修，namespace 单下划线边界仍由 T-C1-018 处理。

`[T-A-008] 2026-11-03`：A 席位终审追认 R2-S4 两项收尾，依据 `doc/reviews/R2-S4-review.md` §7–§8、`src/opencode/session/context.py`、`src/opencode/session/service.py` 与 `src/opencode/contracts/errors.py` 的冻结证据：

1. **S4 ④-5 `ContextLimitExceeded` 归属：维持 session 层定义。** 异常继续由 `session/context.py` 定义并继承 `StoreError`；`CoreErrorCode.CONTEXT_LIMIT_EXCEEDED` 仍由 contracts 提供 wire code，基类负责 `.core` 形状。该分层与 `SessionBusyError` 的 session 协议拒绝先例一致，当前不搬移代码、测试或契约条款。下游无需为此项排期变更，S6 继续消费标准 `.core` 面。
2. **S4 ④-6 / m-3 CJK sizing：采纳方案 (a)。** R2 保留确定性的 chars/4 估算器和 `safety_margin` 语义；由 T-C1-020 在 `context.py` 估算器注释区补充宿主指引：CJK 主导文本可能系统性低估约 2–4 倍，宿主应显著增大 `safety_margin` 或下调可用预算。可配置乘数或真实 tokenizer 缓存留待 R3+ 决策与扩展，不在 R2 引入行为变更；现有 fail-visible、禁止静默裁剪的契约保持不变。

`[T-A-009] 2026-11-03`：R2-S6 三项裁决、两项意见及一项消费面记档；对应 `doc/reviews/R2-S6-review.md:25–26,114–123` 与 `doc/orchestration/progress/T-C1-019.md:38–49,61–65`。本轮只追加本节，正式条款及实现变更由调度器另行落地；以下第 4/5 项为意见，不预写开放项裁决。

1. **裁决：`part_id_reused` 纳入 §2.3 规范。** §2.3 L92 的 text/reasoning 共用 part_id，§3.1 L144 的持久定位键为 message_id+part_id，跨 kind 复用会使该身份歧义；`src/opencode/session/runner.py:674–678` 已拒绝，`tests/integration/test_runner.py:1047–1062` 已钉定整次生成失败、零 finalized/工具。拟在 §2.3 L100 后补：“同一次生成内，一个 text/reasoning part_id 只能对应一种 kind；同 kind 的连续 delta 按该 ID 聚合，跨 text/reasoning 复用时报 `ValidationError(details.violation=part_id_reused)`，本次生成失败且不执行任何工具，不猜测拆分或改名。”TS `packages/core/src/session/runner/publish-llm-event.ts:440–471` 将 text/reasoning 分开分派，未规定 Python 的共享持久身份键；本条是 Python 补充，不宣称 TS 有同名违规码。`doc/plans/r2-slices.md:126,133` 的生成前校验与零工具执行验收承接本条；调度器下轮检查点补正式 §2.3，现有实现无需修改。

2. **裁决：无 durable cancel 的硬取消残留归 R4 启动恢复协调器。** `src/opencode/session/runner.py:450–458,747–757` 继续传播 CancelledError、尽力保存收文而不伪造取消意图；R2-S7 只按 §7 L335/L337 和 `doc/plans/r2-slices.md:138–143` 完成正常 cancel/close 的持久意图、资源清理及终态，不把任意 task.cancel 当作用户取消或直接释放未结束 run。R4 实现席位依 `doc/PHASE1-SLIM.md:98–104` 与 §3.3 L208–212，取得独占运行权、验证日志/投影且确认旧执行已退出后，补 pending→cancelled/not_started、started 无终态→unknown，保留已有终态；无 generated 时只用已提交 finalized，稳定批次 ID 保证重复恢复幂等，禁止重跑副作用或自动续生成，诊断会话不补业务事件。TS `packages/core/src/session/execution.ts:20–39,73–77` 区分进程内清理与 durable claim 恢复，`runner/step.ts:140–152` 保护终态清理；Python 按上述阶段交付，不移植 TS 启动续跑。调度器须将此项交接 R4 并安排开放 step/有无 generated/重复启动恢复验证；R2 期间残留明确保持非终态和占槽，不声称已恢复。

3. **裁决：m-1 保留本 step 已收的 `finish/raw_finish/usage/cost_usd` 观测值，终态仍由 interrupted/error 决定。** §2.1 L28/L74、§2.3 L96/L102 与 §3.1 L144 已提供这些可选字段；已收费用不因随后中断变成未知，未收到或 crash 后未持久化的值仍为 None、不推断为 0。TS `packages/core/src/session/runner/step.ts:234–255` 在 failure 分支仍传 usage，`publish-llm-event.ts:412–427` 仍可携 rawFinish；这是观测事实与执行结果分离的依据，不宣称 TS 在所有失败中保留同一 normalized finish。`doc/plans/r2-slices.md:126,132–133` 的 interrupted、不拼接不重发及零非法工具执行语义不变；error-after-finish 是对非法双终止序列的防御，不据此放宽 §2.3 L102。当前 `runner.py:735–746,969–974` 的保守 None 不追溯判为旧契约违规；调度器另派微补丁保留已收值，并补 usage→error、finish/usage→error 与缺值对照，断言 interrupted、原错误、单次请求和零工具执行。本轮不修改代码或声称补测完成。

4. **意见（非裁决）：`tool_ref` wire 转换倾向 R3 下沉到 registry/adapter 面，R2 维持过渡实现。** `runner.py:282–295` 与 `src/opencode/ai/maas.py:114–119` 都对点分名执行 `partition(".")` 镜像转换；§2.2 L80、§5.1 L253 及 T-A-006/007 的接缝边界仍有效，`doc/plans/r2-slices.md:118,126` 将命名映射与工具入口分工，TS `packages/core/src/tool/runtime.ts:287–292` 也将 effectiveName 放在工具运行层（不沿用其单下划线算法）。当前 runner 保持 provider-agnostic、接口冻结且未知名仍由 S5 catalog miss 走 `not_started`，因此 R2 零动作是合适边界；R3 接缝重构时指定单一转换 owner，保留点分名、wire 往返、畸形名原样透传和未知工具配对，不让 runner 引入具体 MAAS 依赖。最终方向由调度器裁决，不以本意见改写现有接口。

5. **意见（非裁决）：R2-S8 倾向扩展现有 `ReadRepositories.messages()` 为完整消息联合读面。** 当前 `src/opencode/storage/repos.py:125–130,254–274` 将 `MessagePage.items` 和解码器锁定为 `UserMessage`，会把已投影的正常 Assistant/Idle 行误报为解码损坏；§2.1 L23–29 和 §7 L316/L320 要求按投影顺序读取消息，`doc/plans/r2-slices.md:148–153` 的 S8 历史显示需要完整对话。TS `packages/schema/src/session-message.ts:304–321` 以 `Info` 联合包含 User/Assistant/Idle，补充只读参照 `packages/core/src/session/history.ts:125–150` 集中按消息形状解码（该补充文件未列指纹台账，不据此宣称 TS 分页接口同构）。倾向在同一 `MessagePage`/解码器扩面，保留 seq/cursor 与整页 fail-closed，避免第二套分页规则；调度器须在 S8 排期前确定取向并授权兼容面核验：旧 User fixture、Assistant/Idle 混合分页、游标归属及坏行整页失败。本条只给意见。

6. **记档：** §3.2 L177 和 `src/opencode/storage/migrations/versions/0001_initial.py:57–74` 的 `events` 表无独立 `run_id` 列，R2 消费面依 §3.1 L110 及 `runner.py:393,407` 写入的 `metadata.run_id` 做 JSON 查询；不追加列、迁移或跨 session 的身份推断。
