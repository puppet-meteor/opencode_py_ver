# R1 实现切片拆分（草稿）

> 席位B（T-B-001）产出，供 C 席按序实现。状态：**草稿，待与 A 席 R0 复估/R1 定义对齐后定稿**。
> 依据：doc/PHASE1-SLIM.md §3 R1、doc/CORE-CONTRACTS.md §3/§4/§7、doc/ROADMAP.md §3/§5、doc/adr/0001。
> TS 基准：`D:/26_code/opencode_py_ver/opencode-2.2.2/`（指纹见 doc/source-baseline.json，全部已锁定）。
> 拆分基准：按现行文档拆；A 席若修订 R1 定义，由调度器对齐后本文件再改版。

## 0. R1 范围锚定（摘自 PHASE1-SLIM §3 R1）

- 交付：最小 contracts、迁移 0001、SessionStore、projector、只读 repo、Bus/EventLog、SessionService.create/messages。
- 里程碑演示：create → 用户事件 → 投影 → 重启查询。
- 验收 A：事件/投影/applied_seq 同事务；projector 抛错时全回滚，Bus 收不到未提交事件。
- 验收 B：ID/批次幂等优先于 expected_seq 检查；"已提交但没收到返回"的重试成功；混合旧/新 ID、不同内容、全新批次的 seq 冲突明确拒绝。cursor 分页/follow 在 commit 后丢通知仍能补读。
- 验收 C：从空库建库并写用户消息，退出、重新打开并得到相同消息；删除读模型后从日志重建一致。未知事件版本、投影损坏或序列冲突后，session 进入只读诊断，读取仍可用且新的写命令被拒绝。
- 验证：契约 JSON fixture、SQLite 集成测试、事务故障注入；未知版本/损坏事件使会话只读诊断。查询不得触发外部调用。
- 不在 R1：runner/provider/工具链路（R2）、恢复协调器与 unknown 终态补写（R4）、ConfigLoader 完整实现（R3）。

R1 涉及的事件子集（CORE-CONTRACTS §3.1 清单的 R1 最小集）：`py.session.created`、`py.prompt.accepted`。其余 py.* 事件类型本阶段只在 contracts 留注册表位（加类型=加一行定义），不实现投影。

## 1. 切片序列总表

| ID | 一句话目标 | 接缝 | 前置 | C 席预计 |
|---|---|---|---|---|
| R1-S1【试点】 | 最小 contracts：事件 envelope、type+version 注册表、R1 事件子集、UserMessage、CoreError | S1/S4 输入类型 | 无 | 30–45 min |
| R1-S2 | Alembic 0001 最小 schema + 引擎打开（WAL/FK/busy_timeout/目录锁复用 R0-B） | S4 | R1-S1 | 30–45 min |
| R1-S3 | SessionStore.commit + 私有 TransactionWriter（幂等先于 expected_seq、连续 seq、批次原子） | S4/S6 | R1-S1、R1-S2 | 45–60 min |
| R1-S4 | projector 纯折叠 + sessions/messages/applied_seq 同事务投影 + commit 时状态校验 | S4/S6 | R1-S3 | 45–60 min |
| R1-S5 | volatile Bus：live-only 订阅、队列上限、SlowConsumer 断开 | S2 | R1-S1 | 30–45 min |
| R1-S6 | EventLog.read/follow（Synced、cursor 补读、InvalidCursor）+ 只读 repo messages 分页 | S2/S4 | R1-S3、R1-S5 | 45–60 min |
| R1-S7 | SessionService.create/messages/prompt(仅持久化) + 每 session 提交锁 + app 组合入口 | S6 | R1-S3..S6 | 45–60 min |
| R1-S8 | rebuild 从日志重建 + 只读诊断终态 + R1 退出条件全量收口 | S4/S6 | R1-S7 | 45–60 min |

依赖序：S1 → S2 → S3 → S4 →（S5 可与 S3/S4 并行）→ S6 → S7 → S8。S5 只依赖 S1，调度器可视 C 席空闲度穿插。

## 2. 切片详情

### R1-S1【试点】最小 contracts

- **目标**：定义 R1 全部落库/跨接缝数据的 Pydantic v2 wire model 与 JSON 往返规则，产出正/反例 fixture。后续所有切片只消费这些类型。
- **涉及接缝**：S1（UserMessage 形状）、S4（durable 事件 envelope/解码规则）。
- **新增文件**：
  - `src/opencode/contracts/__init__.py`
  - `src/opencode/contracts/events.py`（EventDraft/DurableEvent/EphemeralEvent envelope；`type+version → data model` 注册表；所有事件 version=1；未知版本解码=显式错误）
  - `src/opencode/contracts/messages.py`（UserMessage：id/type/created_at/metadata/text）
  - `src/opencode/contracts/errors.py`（CoreError：code/message/retryable/details；至少覆盖 R1 用到的 code）
  - `tests/contract/test_events.py`、`tests/contract/__init__.py`、`tests/contract/fixtures/events_*.json`
- **TS 基准映射**：
  - `packages/schema/src/event.ts`：`Event.ID`（evt_ 前缀，L9-14）、`Event.Seq`（L21-25）、`Event.Version`（L28-32）、`DurableEnvelope`（L34）、`durable()/ephemeral()` 定义工厂（L90+）、`versionedType`。Python 差异：seq≥1、初值 1（TS Seq≥0、latest 缺省 -1）；metadata 关联 ID 规则按 CORE-CONTRACTS §3.1。
  - `packages/schema/src/session-message.ts`：user 消息字段形状。
  - `packages/schema/src/session-event.ts`：`session.created` / inbox→user 消息 data 形状（Python 用 `py.session.created`/`py.prompt.accepted` 替代，见 parity §3 映射）。
- **验收标准（引用 PHASE1-SLIM R1）**：验证行"契约 JSON fixture"——每个 R1 事件类型有正例 fixture（可解码、字段全）与反例（未知 type、未知 version、缺必需字段、非法 JSON 扩展值→拒绝不静默）。`py.*` 命名与 version=1 落在 fixture 里可检。
- **测试计划**：纯单测，无 DB。fixture 驱动参数化；round-trip（model→JSON→model 等值）。fake 适配器策略：本切片无适配器，fixture 即"假数据源"。
- **试点说明**：粒度=一次 C 实现循环（30–60 min），无外部依赖、门禁（pytest/ruff/mypy）齐全，适合三角色+watchdog 全流程演练。

### R1-S2 迁移 0001 + 存储引擎打开

- **目标**：从空库用 Alembic 建 0001 最小 schema；提供带 WAL/foreign_keys/busy_timeout 与数据目录 OS 锁的引擎打开/关闭入口。
- **涉及接缝**：S4。
- **新增文件**：
  - `src/opencode/storage/__init__.py`、`src/opencode/storage/db.py`（SQLAlchemy 2.0 async + aiosqlite；PRAGMA；目录锁，复用 tools/r0/platform_probe.py 已验证的 msvcrt/fcntl 方案）
  - `src/opencode/storage/migrations/env.py`、`src/opencode/storage/migrations/versions/0001_initial.py`
  - 仓库根 `alembic.ini`；`pyproject.toml` 增加 alembic/sqlalchemy/aiosqlite/pydantic 依赖（C 席在 worktree 内改，含 uv.lock）
- **TS 基准映射**：
  - `packages/core/src/database/sqlite.bun.ts`：`nativeLayer`（L83-96）——open 参数（readonly/create/readwrite）、close finalizer、`PRAGMA journal_mode = WAL;`（L93）；`make`（L23-81）——语句执行与错误分类。差异：TS 用 bun:sqlite + Effect Layer，Python 用 aiosqlite + 显式 app 组合。
  - `packages/core/src/event/sql.ts`：`EventSequenceTable`（L4-8：aggregate_id PK/seq/owner_id）、`EventTable`（L10-26：id PK、aggregate_id FK cascade、seq、created、type、data JSON，`uniqueIndex event_aggregate_seq_idx`）。Python 差异（按 CORE-CONTRACTS §3.2 L157-163）：不建独立 sequence 表——seq 按 events 表已有最大值+1 分配，`sessions.applied_seq` 记投影水位；events 另有 batch_id/batch_ordinal/dedupe_key 唯一约束。
  - `packages/core/src/session/projector.ts` 引用的 `SessionTable/SessionMessageTable`（sql.ts 未指纹锁定，只作列名对照）：Python `sessions`（状态/active_run_id/快照/applied_seq）、`messages`（session_id、message_id、顺序、JSON，unique(session_id,message_id)）、`tool_executions`、`permissions` 按 CORE-CONTRACTS L160-161 建最小列。
- **验收标准**：验收 C 的"从空库建库"前置；验证命令 `python -m alembic upgrade head` 幂等（空库→0001；已有 0001→no-op）。WAL/FK/busy_timeout 生效用 PRAGMA 查询断言；第二 writer 进程锁失败复用 R0-B 探针结论不重测。
- **测试计划**：tests/integration/test_migrations.py（tmp_path 建库、列/索引存在性、二次 upgrade no-op）；不测平台差异（R0-B 已覆盖，见 doc/evidence/r0/platform.json）。

### R1-S3 SessionStore.commit + 私有 TransactionWriter

- **目标**：实现 CORE-CONTRACTS §3.2 的唯一提交路径：writer 锁 + 写事务 → 按 ID/批次查已提交（幂等判定**先于** expected_seq）→ 全新事件核对 expected_seq/归属/版本 → 分配连续 seq → 插入 events → 调 projector 更新读模型与 applied_seq → commit → 返回 DurableEvent 列表。
- **涉及接缝**：S4（事务 writer）、S6（唯一公共提交入口）。
- **新增文件**：
  - `src/opencode/storage/store.py`（SessionStore 公共门面）
  - `src/opencode/storage/txn.py`（TransactionWriter，S4 私有；进程内写事务串行）
- **TS 基准映射**（`packages/core/src/bus.ts`）：
  - `commitDurableEvent`（L284-450）：读 latest seq（L321-327）→ 幂等重放对比 `stored.id/type/created + isDeepStrictEqual(升级后 data)`（L346-352）→ seq=latest+1（L373）→ projector 在**同一事务内**执行（L402-405）→ EventSequenceTable upsert + EventTable insert（L406-432）→ commit 后才 wake/notify（L438-445）。Python 差异：幂等键=EventBatch.id+事件 ID+内容；TS 无 expected_seq，Python 增加乐观并发核对；TS owner/strictOwner 语义（replay 用）Python 不实现。
  - `publishAll`（L535-672）：批内同 aggregate 校验（L570-578）、KeyedMutex 按 aggregate 串行（L579）、firstSeq..finalSeq 连续分配（L592-593）、批内重复 ID 拒绝（L604-610）、已存在 ID 拒绝（L612-624）、整批单事务（L582-657）。Python 差异：批次以 EventBatch.id 为重试身份，"已有 ID+新 ID 混合"→IdempotencyConflict（TS 直接 die InvalidDurableEventError）。
  - `latestSequence`（L22-33）/`reserveSequence`（L35-49）：Python 由 events max(seq) 替代独立表。
  - `durableLocks`（KeyedMutex，L205）：Python 用 per-session asyncio.Lock + 进程内单 writer。
- **验收标准（引用验收 B）**："ID/批次幂等优先于 expected_seq 检查"——同批次重试（expected_seq 落后）返回原事件不重投影；"已提交但没收到返回"的重试成功；混合旧/新 ID、同 ID 不同内容、全新批次 seq 冲突均明确拒绝（IdempotencyConflict/Conflict）。
- **测试计划**：tests/integration/test_store_commit.py（真实 SQLite tmp_path）：正常路径、三类幂等重放、expected_seq 冲突、seq 连续且 unique(session_id,seq) 生效；projector 用注入的 fake（记录调用），事务失败注入（fake projector 抛错）断言 events 表零写入。fake 适配器策略：projector 为 Protocol 注入，S4 换真实实现复用同一测试。

### R1-S4 projector + applied_seq 同事务 + commit 状态校验

- **目标**：实现确定性状态折叠 `apply(state, event) -> state`（不读时钟/不随机/不发外部调用）与读模型写入；py.session.created 建 sessions 行、py.prompt.accepted 写 messages 行；applied_seq 与事件同事务更新；commit 时校验事件归属与合法迁移。
- **涉及接缝**：S4（投影/回滚）、S6（状态机校验前置）。
- **新增文件**：
  - `src/opencode/session/__init__.py`
  - `src/opencode/session/state.py`（SessionState 折叠状态 + SessionStatus）
  - `src/opencode/session/projector.py`（读模型写入 + 后续 rebuild 复用同一 fold）
- **TS 基准映射**（`packages/core/src/session/projector.ts`）：
  - `bus.project(definition, projector)` 注册模型（bus.ts L904-912）：projector 在 commit 事务内被调用（bus.ts L402-405/L626-631）。Python 差异：不用全局注册表，projector 由 TransactionWriter 显式注入调用（单一写入路径，无第二入口）。
  - `SessionEvent.Created` handler（L541-568）：insert SessionTable + `onConflictDoNothing` + 冲突时 `SessionAlreadyProjected`（L39）→ Python：py.session.created 插 sessions，重复投影=StorageCorrupt。
  - `insertMessage`（L481-504）：SessionMessageTable 插入 `seq: event.durable.seq`（消息顺序=事件 seq）→ Python messages 顺序同源；unique(session_id,message_id)。
  - `run()` + SessionMessageUpdater adapter（L261-476）与 `projectIdle`（L506-535，time_idle 单调）：R1 只需 user 消息 append 路径；assistant/idle 折叠留 R2（contracts 已有状态迁移表可依）。
- **验收标准（引用验收 A）**：事件/投影/applied_seq 同事务；projector 抛错时全回滚（连同 R1-S3 的故障注入复测）；applied_seq == 已提交最大 seq。
- **测试计划**：tests/integration/test_projector.py：create→accepted→查询 sessions/messages；projector 抛错→回滚（验收 A）；重复 message_id/未知 session 归属→拒绝；"查询不得触发外部调用"用 monkeypatch 断言 repo 无网络/子进程。契约 fixture（S1）作为 projector 输入复用。

### R1-S5 volatile Bus

- **目标**：live-only 订阅通道：进入订阅后的事件才可见（含 ephemeral/committed durable 通知）、队列上限、溢出断开并报 SlowConsumer、退出 context 注销释放。
- **涉及接缝**：S2。
- **新增文件**：`src/opencode/bus/__init__.py`、`src/opencode/bus/bus.py`（VolatileBus；不落库、无 durable cursor）。
- **TS 基准映射**（`packages/core/src/bus.ts`）：
  - `subscribe`（L751-782）：typed PubSub per subscription、acquireRelease 注销（L763-779）→ Python `AsyncContextManager[AsyncIterator[Event]]`。
  - `notify`（L496-509）：listeners→live PubSub→typed 顺序。Python 差异：无 location 路由（L729-749 `local()`/routes 不做）、无 deprecated listen/project。
  - TS `PubSub.unbounded`（L199）→ Python 有界队列 + SlowConsumer（CORE-CONTRACTS §4 差异项：溢出断开不静默丢 durable）。
- **验收标准（引用验收 A 后半）**：Bus 收不到未提交事件——commit 失败/回滚后订阅者零事件；Bus 不承担持久写入（无任何 DB 依赖注入）。
- **测试计划**：tests/contract/test_bus.py（纯 asyncio 单测）：订阅时机（订阅前事件不可见）、队列上限→SlowConsumer、context 退出后注销（发布不再入队）、类型过滤。与 S4 联测一条集成：commit 成功→Bus 收到、commit 失败→收不到。

### R1-S6 EventLog read/follow + 只读 repo

- **目标**：durable 日志读取：`read(session_id, after, limit)` 分页；`follow` 先注册 wakeup→短读事务捕获高水位 H→补读→`Synced(cursor)`→之后 seq 严格递增、Bus 仅作唤醒、最长 1 秒轮询兜底；messages 有序分页 repo。
- **涉及接缝**：S2（EventLog）、S4（只读查询）。
- **新增文件**：`src/opencode/bus/eventlog.py`（EventLog：read/follow/EventCursor/EventPage）、`src/opencode/storage/repos.py`（只读 repo：messages 分页、session 状态读取）。
- **TS 基准映射**（`packages/core/src/bus.ts`）：
  - `log`（L850-893）：`target = latestSequence`（L878）、`Synced` marker（L879-884，`log.synced` 类型）、replay=分页 readThrough(target)+marker（L884）、live= wake→latestSequence→`filter target > sequence`→readThrough（L886-890）。Python 差异：游标 seq≥1、`None`=本 session seq=0；不保证 ephemeral 重放（CORE-CONTRACTS §4）。
  - `readAfter`（L786-828）：`gt(seq, after) + lte(through)`、asc、`limit logReadPageSize=512`（L207）→ Python limit 1–1000 默认 100；TS 对未知 durable 类型跳过但 raw seq 前进（L809-827）→ Python 差异：未知版本使会话进只读诊断（fail closed，不跳过）——待与 A 确认读取路径是否也 fail closed（见开放问题 #3）。
  - `subscribeDurable`（L830-848）：`PubSub.sliding(1)` wake 信号（合并重复唤醒）→ Python asyncio.Event/单槽信号；重复信号合并、1 秒轮询兜底按契约。
  - InvalidCursor 语义：session 不同/负数/超高水位明确报错不空等（CORE-CONTRACTS §4；TS 无此错误码）。
- **验收标准（引用验收 B 后半）**：cursor 分页/follow 在 commit 后丢通知（Bus 订阅者断开）仍能补读——测试直接断开 Bus 后继续 read 拿到全部事件。
- **测试计划**：tests/integration/test_eventlog.py：read 分页边界（空页保持 cursor、limit 钳制）、follow 补读+Synced+严格递增、丢通知补读、InvalidCursor 三类；repos messages 分页 after_message_id。查询无外部调用同 S4 断言。

### R1-S7 SessionService + 提交锁 + app 组合

- **目标**：应用命令入口：`create(config_snapshot)`、`messages()`、`prompt()`（R1 仅持久化 `py.prompt.accepted`（dedupe_key=prompt:request_id）并返回 RunRef，不调度 run——调度属 R2）；每 session 短期提交锁（读状态→生成事件/expected_seq→commit→本地通知，全程串行，不跨外部等待持锁）；app.py 显式组合依赖与资源创建/关闭。
- **涉及接缝**：S6（应用层）、S4（调用唯一 commit）。
- **新增文件**：`src/opencode/session/service.py`、`src/opencode/app.py`（组合入口；R1 只组合 storage+bus+session）。
- **TS 基准映射**：TS 无同构 SessionService（ADR-0001 决策 1：Python 新增应用层）。行为基准取：
  - `bus.ts publish/publishAll`（L511-533/L535-672）：唯一写路径、KeyedMutex 串行（L579）→ Python SessionService 提交锁 + SessionStore.commit。
  - `session/projector.ts` layer 组合（L537+：deps [Bus.node, Database.node]）→ Python app.py 显式注入（对应 sqlite.bun.ts Layer 结构 L100-103 的非 Effect 替代）。
  - CORE-CONTRACTS §7 表：create/session_status/messages/run_status 的 R1 子集。
- **验收标准（引用验收 C 前半）**：从空库建库并写用户消息，退出、重新打开（新 engine 新 service 实例）并得到相同消息；重复 prompt 相同 request_id 返回原 RunRef 不重复建用户消息；SessionBusy/只读拒绝路径可测。
- **测试计划**：tests/integration/test_service.py：完整闭环（create→prompt→messages→关→重开→messages 相同）；request_id 幂等；提交锁串行（并发 prompt 一个成功一个 SessionBusy 或幂等返回）。fake 策略：不需要 provider fake（R1 无模型调用）；config_snapshot 用最小合法快照对象。

### R1-S8 rebuild + 只读诊断 + R1 收口

- **目标**：`rebuild(session_id)`：会话锁+事务下清空该 session 读模型、按 seq 重放、更新 applied_seq（不发通知不生新事件）；未知事件版本/损坏数据/序列冲突→`active → read_only_diagnostic`（读取仍可用、写命令拒绝）；汇总 R1 退出条件全量证据。
- **涉及接缝**：S4（重放）、S6（诊断状态 fail closed）。
- **新增文件**：`src/opencode/session/rebuild.py`（或并入 projector.py，C 席定；倾向独立文件便于复用 S4 fold）；诊断状态写入可复用 sessions 表更新，不新建文件。
- **TS 基准映射**（`packages/core/src/bus.ts`）：
  - `replay`（L674-704）+ `decodeStoredData`（L74-76，SessionLegacy upcaster 单点升级）→ Python 差异（parity §3）：无跨库 replay；rebuild 只读本库日志；未知版本不跳过（TS readAfter 跳过未知类型保游标前进，L809-827）而是 fail closed 进只读诊断。
  - `InvalidDurableEventError`（L60-66）→ Python StorageCorrupt/只读诊断路径。
- **验收标准（引用验收 C 后半）**：删除读模型（messages/sessions 行）后从日志重建一致；未知事件版本、投影损坏、序列冲突后 session 只读诊断——messages/EventLog.read 仍可用，prompt/cancel/新写入以 SessionReadOnly 拒绝；诊断不自动恢复 active。
- **测试计划**：tests/integration/test_rebuild.py + test_diagnostic.py：手写坏事件行（未知 version/type、孤儿 seq、重复 seq）→ 打开会话即诊断；重建幂等（重复 rebuild 结果一致）；R1 全量退出条件 checklist 跑一遍并记录到产出（供 R5 证据）。收口含 ruff/mypy/pytest 全绿 + Windows/Linux CI。

## 3. 关键设计差异备忘（Python vs TS，实现时勿"照抄"）

1. seq 域：TS Seq≥0/latest=-1、独立 EventSequenceTable；Python seq≥1、events max+1 分配、applied_seq 落 sessions 表（CORE-CONTRACTS §3.2）。
2. 幂等：TS 按 (aggregate,seq) 单事件对比 + EventTable 已存在 ID 检查；Python 按 EventBatch.id+事件 ID+内容，且幂等判定先于 expected_seq（ADR-0001 决策 7）。
3. Python commit 增加归属/版本/状态迁移校验（TS commitDurableEvent 只做 seq/结构校验，状态机散在 projector/updater）。
4. Bus：Python 有界队列 + SlowConsumer（TS PubSub.unbounded）；EventLog 游标协议显式 InvalidCursor（TS 无）。
5. 未知 durable 类型：TS 读取时跳过、replay 时 die；Python 统一 fail closed → read_only_diagnostic（CORE-CONTRACTS §3.3/§3.2）。
6. Python 无 location 路由/fork/claim/owner 机制（TS bus.ts prepareRoutes/projectFork/claim 整块不做）。

## 4. 风险与开放问题（供与 A 汇合）

1. **R1 的 prompt 语义**：R1 交付清单只有 create/messages，但"写用户消息"需走 `py.prompt.accepted`。本方案取"prompt 仅持久化+返回 RunRef，不调度"（契约 §7：先持久 accepted 再调度；调度 R2 接入）。备选：R1 不暴露 prompt、测试经 SessionStore.commit 直接写事件。若 A 席修订 R1 定义为"只到存储层"，S7 相应删 prompt。**关联**：R1 退出后库中存在无终态 run（恢复协调器在 R4），`--continue` 类恢复 R1 不做。
2. **config_snapshot 最小形状**：`py.session.created` 必需 config_snapshot/snapshot_hash，但 ConfigLoader 是 R3。本方案：contracts 定义最小合法快照 wire model（schema_version+空规则+hash），R1 测试用常量快照；R3 替换为真实 loader 产物。需 A 确认 schema 版本号与 hash 算法（建议 sha256(canonical JSON)）。
3. **未知事件版本在读取路径的行为**：CORE-CONTRACTS L171"未知事件版本…使该会话进入只读诊断"与 read/follow 的关系需澄清：EventLog.read 遇到未知 version 是报错还是返回该 session 诊断状态？本方案按"读取返回 StorageCorrupt + session_status 变诊断"实现，但这与"读取仍可用"（验收 C）字面冲突——推测"读取仍可用"指已投影的 messages/状态，而非坏日志本身。**需 A 裁决**。
4. **expected_seq 与 applied_seq 的关系**：契约说 expected_seq 是调用方传入的乐观并发值；建议明确 expected_seq == 该 session 已提交最大 seq（即 applied_seq），避免两个水位。需 A 确认。
5. **Alembic env 与测试库**：migrations 需在 async 环境跑（aiosqlite）；建议 env.py 用同步引擎跑迁移、运行时用 async 引擎（SQLite 文件级兼容）。C 席需验证此组合在 Windows 路径下无坑。
6. **估算**：8 切片 × 30–60 min C 循环 + 评审/返工，合计约 1.5–2.5 个工作日纯实现，低于 R1 的 1 周预算——余量留给 S3/S4 的事务故障注入测试深化与 CI 双平台验证；若 A 复估后 R1 范围扩大（如含恢复协调器前移），优先加切片而不是加大单切片粒度。
7. **目录锁与测试并行**：数据目录 OS 锁（单 writer）会让 pytest 并行/同目录多库受限；建议集成测试每用例独立 tmp_path 目录（现行 R0 测试已如此）。
