# R1 实现切片拆分

> 修订：[T-B-003] 2026-11-02 按审查报告 doc/reviews/R1-S1-review.md §4 九条修订——并入 T-A-002 契约裁决（§3.2 水位/§3.2.1 诊断/§4 坏页/§7 prompt/§9 冲突表）、S2 依赖更正、S1 交付追认、files 待裁预写、ID 生成器注记、§3 差异表补齐、开放问题清理。
> 修订：[T-B-004] 2026-11-02 按 T-A-003 终裁微调（契约 changelog/§10）：files 终裁=方案 (b)（R1 v1 无 files 字段、任何 files 键按未知字段拒绝、R2+ 升版本+upcaster 引入）；ID=四前缀强制（evt_/msg_/ses_/run_）+request_id 例外（§3.1）；extra-forbid=封闭演进（§8）；bool 守卫升 §1 仓库级规则；§4 #5 标已决、§3 差异表补第 10 行；全文清除 A-003 待裁预写字样。
> 修订：[T-B-007] 2026-11-02 S2 交付标记（worktree r1-s2 #19 99cf753 并回 #22 98d4355，审查通过 doc/reviews/R1-S2-review.md，N-1/N-2→T-C1-004）+ S3 在飞标记（T-C1-004，worktree r1-s3）+ §4 #6 标已决（T-C1-003 实证）+ §4 #7 补 S2 实测注记。
> 修订：[T-B-014] 2026-11-03 头部收口（R1 收官）：状态行更至 S1-S8 全部交付态+checkpoint 链（#62 merge/#63 终验+基线 210/CI 41558180881）；正文切片语义/表格/差异表零改动。R2 方案另立 doc/plans/r2-slices.md。
> 席位B（T-B-001）产出，供 C 席按序实现。状态：**R1 全交付收官**——S1-S8 全部交付且审查通过。checkpoint 链：S1 `e61cdd5`（审 R1-S1-review.md）；S2 worktree r1-s2 `99cf753` → 并回 #22 `98d4355`（审 R1-S2-review.md）；S3 branch r1-s3 `72de622` → 并回 #30 `0dad55d`（审 R1-S3-review.md，PASS）；S4 branch r1-s4 `077caf6` + 微修 `698b456` → 并回 `18f74ff`（审 R1-S4-review.md，条件通过→T-C1-006 微修闭环）；S5 branch r1-s5 `f27ade3` → 并回 #46 `f45f880`（审 R1-S5-review.md，PASS 零发现）；S6 branch r1-s6 `df7c899` + 微修 `f8f85ec` → 并回 `f8ce8f6`（审 R1-S6-review.md，PASS）；S7 branch r1-s7 `5c7be04` → 并回 #57 `ec24f3e`（审 R1-S7-review.md，PASS，nit①② 由 S8 落地）；S8 worktree r1-s8 `28d6efa`（T-C1-011）→ 修复 `eb19d55`（T-C1-012，T-B-013 有条件通过 1 major 闭环）→ **并回 #62 `90c5655`（210 passed）**；#63 `135d4a8` 终验+证据刷新（**基线 210 passed**=contract 82+integration 96+r0 32，doc/reference/r1-exit-evidence.md）；**#64 `38f0b96`：CI run 41558180881 双平台 success，R1 收官**。R2 拆分见 doc/plans/r2-slices.md（T-B-014）。
> 依据：doc/PHASE1-SLIM.md §3 R1、doc/CORE-CONTRACTS.md（含 T-A-002 修订：§3.2/§3.2.1/§4/§6.1/§6.2/§7/§9；T-A-003 修订：§1/§2.1/§3.1/§8/§10）、doc/ROADMAP.md §3/§5、doc/reestimate-r0.md、doc/adr/0001。
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
| R1-S1【试点·已交付 e61cdd5】 | 最小 contracts：事件 envelope、type+version 注册表、R1 事件子集、UserMessage、CoreError | S1/S4 输入类型 | 无 | 30–45 min（实际已交付，见审查报告） |
| R1-S2【已交付 99cf753→并回 #22，审查通过】 | Alembic 0001 最小 schema + 引擎打开（WAL/FK/busy_timeout/目录锁复用 R0-B） | S4 | R1-S1 | 30–45 min |
| R1-S3 | SessionStore.commit + 私有 TransactionWriter（幂等先于 expected_seq、连续 seq、批次原子） | S4/S6 | R1-S1、R1-S2 | 45–60 min |
| R1-S4 | projector 纯折叠 + sessions/messages/applied_seq 同事务投影 + commit 时状态校验 | S4/S6 | R1-S3 | 45–60 min |
| R1-S5 | volatile Bus：live-only 订阅、队列上限、SlowConsumer 断开 | S2 | R1-S1 | 30–45 min |
| R1-S6 | EventLog.read/follow（Synced、cursor 补读、InvalidCursor）+ 只读 repo messages 分页 | S2/S4 | R1-S3、R1-S5 | 45–60 min |
| R1-S7 | SessionService.create/messages/prompt(仅持久化) + 每 session 提交锁 + app 组合入口 | S6 | R1-S3..S6 | 45–60 min |
| R1-S8 | rebuild 从日志重建 + 只读诊断终态 + R1 退出条件全量收口 | S4/S6 | R1-S7 | 45–60 min |

依赖序：S1 → S2 → S3 → S4 →（S5 可与 S3/S4 并行）→ S6 → S7 → S8。S5 只依赖 S1，调度器可视 C 席空闲度穿插。

## 2. 切片详情

### R1-S1【试点】最小 contracts（已交付：commit e61cdd5；审查通过 doc/reviews/R1-S1-review.md，遗留 minor 见该报告 §5）

- **目标**：定义 R1 全部落库/跨接缝数据的 Pydantic v2 wire model 与 JSON 往返规则，产出正/反例 fixture。后续所有切片只消费这些类型。
- **涉及接缝**：S1（UserMessage 形状）、S4（durable 事件 envelope/解码规则）。
- **新增文件**：
  - `src/opencode/contracts/__init__.py`
  - `src/opencode/contracts/events.py`（EventDraft/DurableEvent/EphemeralEvent envelope；`type+version → data model` 注册表；所有事件 version=1；未知版本解码=显式错误）
  - 【已交付追加项·T-B-003 追认】events.py 还交付了 T-A-002 中途合入的：`ConfigSnapshot`（§6.1 精确形状：schema_version=1 必需、permission_rules 必需、非整数 1 拒绝、无内嵌 hash）、`canonical_json`/`compute_snapshot_hash`（§6.2：sort_keys+紧凑分隔符+ensure_ascii=False，sha256）、`RUN_SCOPED_EVENT_TYPES`（metadata 强制 run_id 关联集合）。
  - `src/opencode/contracts/messages.py`（UserMessage：id/type/created_at/metadata/text）
  - 【T-A-003 终裁·T-B-004 更正】UserMessage **无 `files` 字段**（终裁=方案 (b)，CORE-CONTRACTS §2.1/§8/§10）：R1 v1 wire 字段全集精确为 id/type/created_at/metadata/text；缺省不补 files，出现 `files` 键（含 `files: []`）一律按未知字段拒绝——新输入=ValidationError，已存日志=损坏按 §3.2.1/§8 进只读诊断；R2+ 引入 files 须升级承载事件的 version+upcaster，不走同版本可选字段路径；字段可编码不等于媒体启用（§2.1）。现实现 e61cdd5 无 files 字段、`files: []` 被拒——与终裁一致，**无需改动**。
  - 【实施交接·契约 §10】C 席 contracts 补丁（T-C1-002）按 §10 落实：四类 ID pattern、version/seq 等全部整数语义字段 before-validator 拒 bool（§1 仓库级规则）、相应反例补测（B 审查 M-4/N-2）与 UserMessage files 阶段说明更新；现 R1 files 拒绝行为与裁决一致，本补丁不动其行为。
  - `src/opencode/contracts/errors.py`（CoreError：code/message/retryable/details；至少覆盖 R1 用到的 code）
  - 【已交付追加项·T-B-003 追认】errors.py 实际锁定 §8 全量 22 个 CoreErrorCode + 未知 code 拒绝（超出本方案原定"至少 R1 子集"，审查通过）。
  - `tests/contract/test_events.py`、`tests/contract/__init__.py`、`tests/contract/fixtures/events_*.json`
  - 【已交付】fixture 共 8 个：2 正例（session_created/prompt_accepted valid）+6 反例（unknown_type/unknown_version/缺字段×2/坏扩展×2）；测试含 R1 摘要常量 `77c932c3…f802` 钉死测试与快照篡改检测（44 用例，B 复验全绿）。
- **TS 基准映射**：
  - `packages/schema/src/event.ts`：`Event.ID`（evt_ 前缀，L9-14）、`Event.Seq`（L21-25）、`Event.Version`（L28-32）、`DurableEnvelope`（L34）、`durable()/ephemeral()` 定义工厂（L90+）、`versionedType`。Python 差异：seq≥1、初值 1（TS Seq≥0、latest 缺省 -1）；metadata 关联 ID 规则按 CORE-CONTRACTS §3.1。
  - `packages/schema/src/session-message.ts`：user 消息字段形状。
  - `packages/schema/src/session-event.ts`：`session.created` / inbox→user 消息 data 形状（Python 用 `py.session.created`/`py.prompt.accepted` 替代，见 parity §3 映射）。
- **验收标准（引用 PHASE1-SLIM R1）**：验证行"契约 JSON fixture"——每个 R1 事件类型有正例 fixture（可解码、字段全）与反例（未知 type、未知 version、缺必需字段、非法 JSON 扩展值→拒绝不静默）。`py.*` 命名与 version=1 落在 fixture 里可检。
- **测试计划**：纯单测，无 DB。fixture 驱动参数化；round-trip（model→JSON→model 等值）。fake 适配器策略：本切片无适配器，fixture 即"假数据源"。
- **试点说明**：粒度=一次 C 实现循环（30–60 min），无外部依赖、门禁（pytest/ruff/mypy）齐全，适合三角色+watchdog 全流程演练。

### R1-S2 迁移 0001 + 存储引擎打开【已交付：worktree #19 99cf753 并回 #22；审查通过 doc/reviews/R1-S2-review.md（N-1/N-2→T-C1-004）】

- **目标**：从空库用 Alembic 建 0001 最小 schema；提供带 WAL/foreign_keys/busy_timeout 与数据目录 OS 锁的引擎打开/关闭入口。
- **涉及接缝**：S4。
- **新增文件**：
  - `src/opencode/storage/__init__.py`、`src/opencode/storage/db.py`（SQLAlchemy 2.0 async + aiosqlite；PRAGMA；目录锁，复用 tools/r0/platform_probe.py 已验证的 msvcrt/fcntl 方案）
  - `src/opencode/storage/migrations/env.py`、`src/opencode/storage/migrations/versions/0001_initial.py`
  - 仓库根 `alembic.ini`；`pyproject.toml` 增加 alembic/sqlalchemy/aiosqlite 依赖（pydantic 已在 R1-S1 引入，调度器已批准前移——本行原误列 pydantic，T-B-003 更正；C 席在 worktree 内改，含 uv.lock）
- **TS 基准映射**：
  - `packages/core/src/database/sqlite.bun.ts`：`nativeLayer`（L83-96）——open 参数（readonly/create/readwrite）、close finalizer、`PRAGMA journal_mode = WAL;`（L93）；`make`（L23-81）——语句执行与错误分类。差异：TS 用 bun:sqlite + Effect Layer，Python 用 aiosqlite + 显式 app 组合。
  - `packages/core/src/event/sql.ts`：`EventSequenceTable`（L4-8：aggregate_id PK/seq/owner_id）、`EventTable`（L10-26：id PK、aggregate_id FK cascade、seq、created、type、data JSON，`uniqueIndex event_aggregate_seq_idx`）。Python 差异（按 CORE-CONTRACTS §3.2"初始迁移 0001"）：不建独立 sequence 表——seq 按 events 表已有最大值+1 分配，`sessions.applied_seq` 记投影水位；events 另有 batch_id/batch_ordinal/dedupe_key 唯一约束。
  - `packages/core/src/session/projector.ts` 引用的 `SessionTable/SessionMessageTable`（sql.ts 未指纹锁定，只作列名对照）：Python `sessions`（状态/active_run_id/快照及 snapshot_hash/可选 diagnostic——T-A-002 修订——/applied_seq）、`messages`（session_id、message_id、顺序、JSON，unique(session_id,message_id)）、`tool_executions`、`permissions` 按 CORE-CONTRACTS §3.2 0001 清单建最小列。
- **验收标准**：验收 C 的"从空库建库"前置；验证命令 `python -m alembic upgrade head` 幂等（空库→0001；已有 0001→no-op）。WAL/FK/busy_timeout 生效用 PRAGMA 查询断言；第二 writer 进程锁失败复用 R0-B 探针结论不重测。
- **测试计划**：tests/integration/test_migrations.py（tmp_path 建库、列/索引存在性、二次 upgrade no-op）；不测平台差异（R0-B 已覆盖，见 doc/evidence/r0/platform.json）。

### R1-S3 SessionStore.commit + 私有 TransactionWriter

- **目标**：实现 CORE-CONTRACTS §3.2 的唯一提交路径（T-A-002 水位裁决并入）：writer 锁 + 写事务 → **写入门禁先于幂等查询**（session 状态为 read_only_diagnostic 即拒绝 SessionReadOnly，不进入幂等判定）→ 按 ID/批次查已提交（幂等判定**先于** expected_seq）→ 全新事件核对 expected_seq/归属/版本 → 分配连续 seq → 插入 events → 调 projector 更新读模型与 applied_seq → commit → 返回 DurableEvent 列表。水位定义（§3.2）：H=COALESCE(MAX(events.seq),0)（日志水位，提交前读取）、A=sessions.applied_seq（投影水位）；健康不变量 A=H；写入 n 个事件时 seq 依次为 H+1..H+n，H/A 与 events 同事务原子推进；发现 A≠H、seq 间隙、重复 seq、非法 seq 即 StorageCorrupt（不是 Conflict）。expected_seq 语义：调用方传入的"提交前 H 期望值"（非独立水位、非目标 seq），核对不等→Conflict。projector 覆盖 sessions/messages/tool_executions/permissions 四读模型表（同事务）。**§3.2.1 隔离入口归本切片实现**：SessionStore 为私有 TransactionWriter、短事务持久化诊断状态、诊断写入不产生业务事件、不通知 Bus；检测范围全量（读路径发现坏数据同样进诊断）与 rebuild-失败隔离路径（保留原投影）归 R1-S8。
- **涉及接缝**：S4（事务 writer）、S6（唯一公共提交入口）。
- **新增文件**：
  - `src/opencode/storage/store.py`（SessionStore 公共门面）
  - `src/opencode/storage/txn.py`（TransactionWriter，S4 私有；进程内写事务串行）
- **TS 基准映射**（`packages/core/src/bus.ts`）：
  - `commitDurableEvent`（L284-450）：读 latest seq（L321-327）→ 幂等重放对比 `stored.id/type/created + isDeepStrictEqual(升级后 data)`（L346-352）→ seq=latest+1（L373）→ projector 在**同一事务内**执行（L402-405）→ EventSequenceTable upsert + EventTable insert（L406-432）→ commit 后才 wake/notify（L438-445）。Python 差异：幂等键=EventBatch.id+事件 ID+内容；TS 无 expected_seq，Python 增加乐观并发核对；TS owner/strictOwner 语义（replay 用）Python 不实现。
  - `publishAll`（L535-672）：批内同 aggregate 校验（L570-578）、KeyedMutex 按 aggregate 串行（L579）、firstSeq..finalSeq 连续分配（L592-593）、批内重复 ID 拒绝（L604-610）、已存在 ID 拒绝（L612-624）、整批单事务（L582-657）。Python 差异：批次以 EventBatch.id 为重试身份，"已有 ID+新 ID 混合"→IdempotencyConflict（TS 直接 die InvalidDurableEventError）。
  - `latestSequence`（L22-33）/`reserveSequence`（L35-49）：Python 由 events max(seq) 替代独立表。
  - `durableLocks`（KeyedMutex，L205）：Python 用 per-session asyncio.Lock + 进程内单 writer。
- **验收标准（引用验收 B + T-A-002 §3.2/§3.2.1 裁决）**："ID/批次幂等优先于 expected_seq 检查"——同批次重试（expected_seq 落后）返回原事件不重投影；"已提交但没收到返回"的重试成功；混合旧/新 ID、同 ID 不同内容、全新批次 seq 冲突均明确拒绝（IdempotencyConflict/Conflict）。追加：H/A 不变量断言（提交后 A=H、seq 连续无间隙，A≠H/重复/间隙→StorageCorrupt 而非 Conflict）；写入门禁断言（read_only_diagnostic 会话的 commit 以 SessionReadOnly 拒绝，且先于幂等命中——已提交事件的"重放读取"仍可用）；隔离入口断言（诊断状态持久化事务内不产事件、不通知 Bus，Bus 收不到诊断写入）。
- **测试计划**：tests/integration/test_store_commit.py（真实 SQLite tmp_path）：正常路径、三类幂等重放、expected_seq 冲突、seq 连续且 unique(session_id,seq) 生效；projector 用注入的 fake（记录调用），事务失败注入（fake projector 抛错）断言 events 表零写入。fake 适配器策略：projector 为 Protocol 注入，S4 换真实实现复用同一测试。追加（T-A-002）：水位测试点——提交后 H=A 断言、手写 A≠H/seq 间隙/重复 seq 行→StorageCorrupt；隔离测试点——诊断会话写入门禁拒绝 + 诊断写入零 Bus 通知（挂订阅者断言零事件）。

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
  - `readAfter`（L786-828）：`gt(seq, after) + lte(through)`、asc、`limit logReadPageSize=512`（L207）→ Python limit 1–1000 默认 100；TS 对未知 durable 类型跳过但 raw seq 前进（L809-827）→ Python 差异（T-A-002 终裁，CORE-CONTRACTS §3.2.1/§4，撤回本方案原开放问题 #3 的临时方案）：未知版本/损坏数据 fail closed 不跳过——坏页整页 StorageCorrupt(retryable=false)，不返半页、不推进 next_cursor；session 进只读诊断（"读取仍可用"指已投影 messages/状态及健康日志前缀，非坏页本身）。
  - `subscribeDurable`（L830-848）：`PubSub.sliding(1)` wake 信号（合并重复唤醒）→ Python asyncio.Event/单槽信号；重复信号合并、1 秒轮询兜底按契约。
  - InvalidCursor 语义：session 不同/负数/超高水位明确报错不空等（CORE-CONTRACTS §4；TS 无此错误码）。
- **验收标准（引用验收 B 后半 + T-A-002 §4 坏页裁决）**：cursor 分页/follow 在 commit 后丢通知（Bus 订阅者断开）仍能补读——测试直接断开 Bus 后继续 read 拿到全部事件。坏页语义（逐项断言）：整页 StorageCorrupt(retryable=false)，无半页返回；next_cursor 不推进（失败后重试同 cursor 仍报同错）；read 失败后 cursor=输入 cursor（after=None 即 0；follow 用最后已 yield 的 cursor）；follow 遇坏页先保健康事件 yield，随后终止并注销 wakeup，不发越界 Synced；无带外 Synced（Synced 只在真实追平 H 时发）；H=日志水位 MAX(events.seq)（非投影水位 applied_seq）；details 含脱敏 reason/session_id/坏 seq/event_id/last_confirmed_cursor。
- **测试计划**：tests/integration/test_eventlog.py：read 分页边界（空页保持 cursor、limit 钳制）、follow 补读+Synced+严格递增、丢通知补读、InvalidCursor 三类；repos messages 分页 after_message_id。查询无外部调用同 S4 断言。

### R1-S7 SessionService + 提交锁 + app 组合

- **目标**：应用命令入口：`create(config_snapshot)`、`messages()`、`prompt()`（R1 仅持久化 `py.prompt.accepted` 并返回 RunRef，不调度 run——调度属 R2；契约 §7：先持久 accepted 再调度，R1 只做到持久）；每 session 短期提交锁（固定顺序：状态门禁→生成事件/expected_seq=提交前 H→commit→本地通知，全程串行，不跨外部等待持锁）；app.py 显式组合依赖与资源创建/关闭。**RunRef/ID 注记（T-B-004 按 T-A-003 终裁更正）**：RunRef={session_id, run_id, request_id} 三项均非空字符串，session_id/run_id 另须满足 §3.1 的 ses_/run_ pattern，request_id 由调用方给定、仅非空字符串、**不强制前缀**（§7/§3.1）。ID 生成对齐 TS `Event.ID.create = "evt_" + ascending()`（event.ts L9-14）——时间有序、可字典序排序的递增 ID（ULID 风格），不用随机 UUID（保证日志可排序、测试可断言前缀）。模型层**四前缀强制**（§3.1，T-A-003）：evt_/msg_/ses_/run_ pattern，适用 draft/durable/ephemeral envelope、typed data、嵌套消息及 SessionRef/RunRef/游标；metadata 保留键 run_id 一旦出现也须匹配；前缀检查不替代归属验证/seq 排序。TS 依据：event.ts:9-14（evt_）、session-message.ts:21-29（msg_）、session-id.ts:5-11（ses，Python 收紧为 ses_）；run_ 为 Python 侧约束。C 席 T-C1-002 补丁落实 pattern 与 bool 守卫。
- **涉及接缝**：S6（应用层）、S4（调用唯一 commit）。
- **新增文件**：`src/opencode/session/service.py`、`src/opencode/app.py`（组合入口；R1 只组合 storage+bus+session）。
- **TS 基准映射**：TS 无同构 SessionService（ADR-0001 决策 1：Python 新增应用层）。行为基准取：
  - `bus.ts publish/publishAll`（L511-533/L535-672）：唯一写路径、KeyedMutex 串行（L579）→ Python SessionService 提交锁 + SessionStore.commit。
  - `session/projector.ts` layer 组合（L537+：deps [Bus.node, Database.node]）→ Python app.py 显式注入（对应 sqlite.bun.ts Layer 结构 L100-103 的非 Effect 替代）。
  - CORE-CONTRACTS §7 表：create/session_status/messages/run_status 的 R1 子集。
- **验收标准（引用验收 C 前半 + T-A-002 §7 拒绝顺序裁决，按固定顺序实现）**：从空库建库并写用户消息，退出、重新打开（新 engine 新 service 实例）并得到相同消息。prompt 固定顺序（§7.1-§7.4，顺序即语义）：
  1. **状态门禁**（含入参校验，§7.1）：session_id/request_id 非空字符串、session_id 符合 §3.1 ses_ pattern、text 为字符串不隐式转换；session 不存在→NotFound；read_only_diagnostic→SessionReadOnly（同 request_id 重试同样拒绝，除非命中第 2 步幂等）；cleanup_failed→LifecycleCleanupFailed；closed→SessionClosed；session active 后再查 service 级 closing/closed gate→SessionClosed。
  2. **durable 幂等查询**（先于第 3/4 步）：按 `(session_id, dedupe_key)` 查已提交批次，dedupe_key=`"prompt:" + request_id`（字符串拼接、大小写敏感、跨 session 可复用）；命中→按 code point 比较文本：**相同文本→返回原 RunRef**（不生成新 ID、不重投影、不通知）；**不同文本→IdempotencyConflict**（即使会话 busy 也如此）。
  3. **active_run_id 门禁**：前两步通过且 active_run_id 已占→SessionBusy。
  4. 通过后才生成/固定 ID 与时间戳（事件 id `evt_`、message id `msg_`、run id `run_`——均须满足 §3.1 pattern，T-A-003 终裁）、expected_seq=提交前 H，走 SessionStore.commit。
  追加：R1 accepted 的 run 跨 close/reopen 持续占用 active-run 槽（无终态、不自动释放）；R1 close 只释放自有资源（不触碰 run 恢复——归 R4）。
- **测试计划**：tests/integration/test_service.py：完整闭环（create→prompt→messages→关→重开→messages 相同）；request_id 幂等；提交锁串行（并发 prompt 一个成功一个 SessionBusy 或幂等返回）。fake 策略：不需要 provider fake（R1 无模型调用）；config_snapshot 用最小合法快照对象。追加（T-A-002 §7）：四步顺序各分支断言——① NotFound/SessionReadOnly（含同 request_id 重试）/LifecycleCleanupFailed/SessionClosed 各拒绝码；② 相同 request_id+相同文本→原 RunRef（事件数不增）、相同 request_id+不同文本→IdempotencyConflict（即使 busy）；③ busy→SessionBusy 且发生在幂等未命中之后；④ ID 生成期望 seq=H（提交前水位）。

### R1-S8 rebuild + 只读诊断 + R1 收口

- **目标**：`rebuild(session_id)`（T-A-002 §3.2.1 语义并入）：会话锁+事务下清空该 session 读模型、按 seq 重放、更新 applied_seq——**重建与验证在同一事务**，仅全部事件通过校验后才 commit 替换读模型；任何失败（未知版本/损坏数据/序列冲突）整批回滚**连同清空操作**，原投影保留不变，session 置 `read_only_diagnostic`（不发通知不生新事件，沿用 S3 隔离入口）。检测范围全量归本切片：写路径（S3 commit 时）、读路径（S6 read/follow）、打开/重建路径发现 A≠H、seq 间隙/重复、未知 type/version、投影损坏，一律进只读诊断。汇总 R1 退出条件全量证据。
- **涉及接缝**：S4（重放）、S6（诊断状态 fail closed）。
- **新增文件**：`src/opencode/session/rebuild.py`（或并入 projector.py，C 席定；倾向独立文件便于复用 S4 fold）；诊断状态写入可复用 sessions 表更新，不新建文件。
- **TS 基准映射**（`packages/core/src/bus.ts`）：
  - `replay`（L674-704）+ `decodeStoredData`（L74-76，SessionLegacy upcaster 单点升级）→ Python 差异（parity §3）：无跨库 replay；rebuild 只读本库日志；未知版本不跳过（TS readAfter 跳过未知类型保游标前进，L809-827）而是 fail closed 进只读诊断。
  - `InvalidDurableEventError`（L60-66）→ Python StorageCorrupt/只读诊断路径。
- **验收标准（引用验收 C 后半 + T-A-002 §3.2.1 裁决）**：删除读模型（messages/sessions 行）后从日志重建一致；未知事件版本、投影损坏、序列冲突后 session 只读诊断。§3.2.1 精确语义逐项断言：**"读取仍可用"** = messages/EventLog.read 健康前缀、session 状态可查（诊断态本身可见）；**写命令拒绝** = prompt/cancel/新写入以 SessionReadOnly 拒绝（先于幂等查询）；**诊断标记跨 reopen/rebuild 保留**（重开/重建不自动恢复 active，修复须人工/专门流程）；**诊断期间禁止运行恢复补写**（R4 恢复协调器遇诊断会话跳过补写）；发现即 StorageCorrupt、此后写入门禁 SessionReadOnly；rebuild 幂等成功不绕过诊断门禁（重建成功≠诊断解除，除非专门修复流程显式置回）。
- **测试计划**：tests/integration/test_rebuild.py + test_diagnostic.py：手写坏事件行（未知 version/type、孤儿 seq、重复 seq）→ 打开会话即诊断；重建幂等（重复 rebuild 结果一致）；R1 全量退出条件 checklist 跑一遍并记录到产出（供 R5 证据）。收口含 ruff/mypy/pytest 全绿 + Windows/Linux CI。追加（T-A-002 §3.2.1）：**rebuild 失败回滚测试**——坏日志中途失败→读模型保持重建前原样（含清空被回滚）；**诊断标记持久性测试**——置诊断后 reopen/rebuild 标记仍在、写命令仍拒、健康读取路径可用。

## 3. 关键设计差异备忘（Python vs TS，实现时勿"照抄"）

1. seq 域：TS Seq≥0/latest=-1、独立 EventSequenceTable；Python seq≥1、events max+1 分配、applied_seq 落 sessions 表（CORE-CONTRACTS §3.2）。
2. 幂等：TS 按 (aggregate,seq) 单事件对比 + EventTable 已存在 ID 检查；Python 按 EventBatch.id+事件 ID+内容，且幂等判定先于 expected_seq（ADR-0001 决策 7）。
3. Python commit 增加归属/版本/状态迁移校验（TS commitDurableEvent 只做 seq/结构校验，状态机散在 projector/updater）。
4. Bus：Python 有界队列 + SlowConsumer（TS PubSub.unbounded）；EventLog 游标协议显式 InvalidCursor（TS 无）。
5. 未知 durable 类型：TS 读取时跳过、replay 时 die；Python 统一 fail closed → read_only_diagnostic（CORE-CONTRACTS §3.3/§3.2.1，T-A-002 终裁；原"读取路径是否 fail closed"的开放问题 #3 撤回）。
6. Python 无 location 路由/fork/claim/owner 机制（TS bus.ts prepareRoutes/projectFork/claim 整块不做）。
7. `versionedType`/`durableMap`（event.ts L174-187）已对齐：`f"{type}.{version}"` 与 TS `` `${type}.${version}` `` 逐字符一致（S1 测试钉死）；重复 durable 定义拒绝语义一致。
8. 异常面：Python `EventDecodeError` 家族 + `DurabilityMismatchError`（envelope/注册表/耐久性错配分层）为 TS 单一 `InvalidDurableEventError`（bus.ts L60-66）的细化——行为更精确、无契约冲突；S4/S6 负责映射到 StorageCorrupt/只读诊断。
9. TS `readAfter` 遇未知 durable 类型跳过但 seq 前进（bus.ts L809-827）vs Python 坏页整页 StorageCorrupt、不返半页、不推进 next_cursor——T-A-002 终裁落 CORE-CONTRACTS §3.2.1/§4（fail closed），见 R1-S6 验收标准。
10. ID 前缀与整数 bool 守卫（T-A-003，§1/§3.1）：TS 仅单点强制 evt_（event.ts:9-14）/msg_（session-message.ts:21-29）并校验 ses 前缀（session-id.ts:5-11，Python 收紧为 ses_）；Python **四前缀全量强制**（含 run_——TS 无对应物，Python 侧论证：独立标识 run 身份、统一身份域）且适用范围扩至 SessionRef/RunRef/游标与 metadata 保留键。全部整数语义字段 before-validator 拒 bool（pydantic bool→int 陷阱为 Python 特有，TS `Schema.Int` 无此问题）。

## 4. 开放问题（T-B-003/004 清理：#1–#4 已决于 T-A-002、#5 已决于 T-A-003；全部已决，无待决项）

1. ~~R1 的 prompt 语义~~ **【已决·T-A-002，见 CORE-CONTRACTS §7/§9】**：prompt 仅持久化+返回 RunRef（三字段非空字符串），不调度；固定拒绝顺序与 dedupe_key=`"prompt:" + request_id` 已并入 R1-S7 验收标准。R1 退出后库中存在无终态 run（恢复协调器在 R4），`--continue` 类恢复 R1 不做。
2. ~~config_snapshot 最小形状~~ **【已决·T-A-002，见 CORE-CONTRACTS §6.1/§6.2/§9】**：最小快照=仅 `schema_version:1`+`permission_rules:[]`；hash=sha256(canonical JSON)；已在 R1-S1 交付（`ConfigSnapshot`/`canonical_json`/`compute_snapshot_hash`，摘要常量 `77c932c3…f802` 钉死测试）并追认于 R1-S1 新增文件清单。R3 替换为真实 loader 产物。
3. ~~未知事件版本在读取路径的行为~~ **【已决·T-A-002，见 CORE-CONTRACTS §3.2.1/§4/§9】**：fail closed——坏页整页 StorageCorrupt(retryable=false)、不返半页、不推进 next_cursor；"读取仍可用"=健康前缀+已投影 messages/状态。已并入 R1-S6 验收标准与 §3 差异表第 9 条；B 原临时方案撤回。
4. ~~expected_seq 与 applied_seq 的关系~~ **【已决·T-A-002，见 CORE-CONTRACTS §3.2 水位裁决/§9】**：expected_seq=提交前 H 期望值（非独立水位、非目标 seq）；H=日志水位、A=applied_seq、健康 A=H 不变量。已并入 R1-S3 目标/验收标准。
5. ~~contracts 边角三连~~ **【已决·T-A-003，见 CORE-CONTRACTS changelog/§10，具体 §1/§2.1/§3.1/§8】**：(a) ID 前缀=**四前缀强制**（evt_/msg_/ses_/run_ pattern 全量适用 draft/durable/ephemeral、typed data、嵌套消息、SessionRef/RunRef/游标及 metadata 保留键 run_id；request_id 例外仅非空字符串，§3.1）——B 建议采纳（原预写清单含 `req_`，终裁剔除）；M-1 bool 守卫升为 §1 仓库级规则（全部整数语义字段 before-validator 拒 bool），C 席 T-C1-002 补丁落实；(b) files=**方案 (b)**：R1 v1 无 files 字段、任何 files 键按未知字段拒绝，R2+ 升版本+upcaster 引入（§2.1/§8），现实现 e61cdd5 已合规无需改动——B 原预写倾向 (a)，未采纳；(c) extra-forbid=**封闭演进**：envelope/typed data/嵌套消息新增字段（含可选）一律升版本+upcaster，不走同版本可选字段路径（§8 新条款）——B 建议采纳。三处预写差异已由 T-B-004 全文对齐（§2 R1-S1/R1-S7/§3 差异表第 10 行）。
6. ~~Alembic env 与测试库~~ **【已决·T-C1-003 实证，B 审查 T-B-006 §5-7 采纳】**：env.py 用同步引擎跑迁移 + 运行时 async 引擎（open_database）同一 DB 文件，Windows 路径下全链路验证通过（async 写入/PRAGMA 三项/WAL 持久/锁释放后重开）；sqlalchemy 必须带 `[asyncio]` extra（greenlet），纯包 async 引擎运行时失败。S2 已按此落地。
7. **估算（T-B-003 更新）**：S1 已交付（30–60 min 循环成立）；S2 实测 C 循环 ~47 min，落在 30–60 min 预估带内；余 7 切片 × 30–60 min C 循环 + 评审/返工，对照 doc/reestimate-r0.md §4 的 R1 目标 5 人日/上限 6 人日——纯实现约 1.5–2.5 个工作日，余量留给 S3/S4 事务故障注入深化、T-A-002 并入的水位/坏页/四步顺序测试点与 CI 双平台验证；若范围再扩大，优先加切片不加大单切片粒度。
8. **目录锁与测试并行**：数据目录 OS 锁（单 writer）会让 pytest 并行/同目录多库受限；建议集成测试每用例独立 tmp_path 目录（现行 R0 测试已如此）。
