# R1-S1..R1-S7 已交付公共面静态清单（滚动更新）

> 范围：覆盖 **R1-S1..R1-S7 已并入主树的交付面**（滚动更新：S8 并入后另行滚动）——`src/opencode/contracts/`、`src/opencode/storage/`、`src/opencode/session/`、`src/opencode/bus/`、`src/opencode/app.py` 的**源码定义处**公共符号（不含 `import` 绑定、不含包 `__init__` 的再导出）。全部数据由源码直读/正则扫描得出，未做任何修改。
> 符号总数：**121** = 首派 45（contracts 27 + storage 18）+ S3 新增 24（`store.py` 3 + `txn.py` 21）+ S4 新增 10（`session/state.py` 4 + `session/projector.py` 6）+ S5 新增 4（`bus/bus.py` 4）+ S6 新增 24（`bus/eventlog.py` 13 + `storage/repos.py` 11）+ S7 新增 14（`session/service.py` 11 + `app.py` 1 + `storage/repos.py` 2：`AcceptedPrompt` 与 `REASON_DEDUPE_ROW_UNEXPECTED`）。各包的 `__init__.py` 均只追加再导出、0 个新定义。
> 包级再导出：`contracts.__all__` 27 项；`storage.__all__` **27** 项（S6 的 26 + S7 追加 `AcceptedPrompt`，纯追加）；`session.__all__` **11** 项（S4 的 5 + S7 追加 `RunRef`/`RunStatus`/`RunStatusValue`/`SessionBusyError`/`SessionRef`/`SessionService`）；`bus.__all__` 4 项（S5/S6/S7 均不变）——未进各自 `__all__` 的公共名：`TransactionWriter`、`Diagnostic`、`epoch_ms`/`from_epoch_ms`、`storage/txn.py` 与 `session/projector.py` 的 `REASON_*`、`bus/eventlog.py` 全部公共名（直连 import）、`session/service.py` 的 `Clock`/`IdFactory`/`AscendingIds`/`prompt_dedupe_key`/`REASON_ACTIVE_RUN_WITHOUT_PROMPT`（直连 import）、`storage/repos.py` 的 `clamp_page_limit`/`REASON_*`/`MIN|MAX|DEFAULT_PAGE_LIMIT`。
> 扫描时间：2026-11-02（首派）→ 当日 S3/S4/S5/S6 滚动；2026-11-03 S7 滚动；工作树状态见 §3 注。

## 1. 公共面清单

### 1.1 R1-S1 / R1-S2（contracts、storage/db + migrations）

| 符号全名 | kind | 签名 / 值形态 | 职责 | 位置 |
|---|---|---|---|---|
| `opencode.contracts.messages.JsonObject` | type alias | `type JsonObject = dict[str, JsonValue]` | JSON 对象扩展类型，wire 模型共用 | `src/opencode/contracts/messages.py:15` |
| `opencode.contracts.messages.UtcDateTime` | type alias | `UtcDateTime = Annotated[datetime, AfterValidator(_as_utc)]` | 拒绝 naive、归一化到 UTC 的时间戳类型 | `src/opencode/contracts/messages.py:25` |
| `opencode.contracts.messages.UserMessage` | class (BaseModel) | `id: str(^msg_); type: Literal["user"]; created_at: UtcDateTime; metadata: JsonObject; text: str`（`extra="forbid"`） | 用户消息 wire 模型（§2.1） | `src/opencode/contracts/messages.py:28` |
| `opencode.contracts.errors.CoreErrorCode` | class (StrEnum) | 22 个成员，值见 §2 表 | 封闭错误码注册表 | `src/opencode/contracts/errors.py:17` |
| `opencode.contracts.errors.CoreError` | class (BaseModel) | `code: CoreErrorCode; message: str(min_length=1); retryable: bool; details: JsonObject = {}`（`extra="forbid"`） | 跨 seam 统一错误 wire 形状 | `src/opencode/contracts/errors.py:48` |
| `opencode.contracts.events.Durability` | type alias | `Literal["durable", "ephemeral"]` | 事件持久性标签 | `src/opencode/contracts/events.py:25` |
| `opencode.contracts.events.PY_SESSION_CREATED` | constant | `"py.session.created"` | session.created 事件类型名 | `src/opencode/contracts/events.py:27` |
| `opencode.contracts.events.PY_PROMPT_ACCEPTED` | constant | `"py.prompt.accepted"` | prompt.accepted 事件类型名 | `src/opencode/contracts/events.py:28` |
| `opencode.contracts.events.EVENT_VERSION` | constant | `1` | 当前事件版本 | `src/opencode/contracts/events.py:30` |
| `opencode.contracts.events.RUN_SCOPED_EVENT_TYPES` | constant | `frozenset[str] = frozenset({PY_PROMPT_ACCEPTED})` | 必须携带 `metadata.run_id` 的事件类型集 | `src/opencode/contracts/events.py:33` |
| `opencode.contracts.events.versioned_type` | function | `(event_type: str, version: int) -> str` | 生成注册键 `"${type}.${version}"` | `src/opencode/contracts/events.py:53` |
| `opencode.contracts.events.canonical_json` | function | `(value: JsonValue) -> str` | 确定性 JSON（排序键、无空白、UTF-8、拒 NaN） | `src/opencode/contracts/events.py:58` |
| `opencode.contracts.events.compute_snapshot_hash` | function | `(snapshot: ConfigSnapshot) -> str` | 快照规范 JSON 的 sha256 十六进制 | `src/opencode/contracts/events.py:63` |
| `opencode.contracts.events.ConfigSnapshot` | class (BaseModel) | `schema_version: Literal[1]; permission_rules: list[JsonObject] = Field(max_length=0)`；`classmethod create() -> ConfigSnapshot`（`extra="forbid"`） | R1 唯一合法配置快照（§6.1） | `src/opencode/contracts/events.py:72` |
| `opencode.contracts.events.SessionCreatedData` | class (BaseModel) | `working_dir: str(min_length=1); config_snapshot: ConfigSnapshot; snapshot_hash: str(^[0-9a-f]{64}$)`（`extra="forbid"`） | `py.session.created` v1 data，解码时重算摘要 | `src/opencode/contracts/events.py:97` |
| `opencode.contracts.events.PromptAcceptedData` | class (BaseModel) | `request_id: str(min_length=1); run_id: str(^run_); message: UserMessage`（`extra="forbid"`） | `py.prompt.accepted` v1 data | `src/opencode/contracts/events.py:114` |
| `opencode.contracts.events.EventDraft[DataT]` | class (BaseModel, generic) | `id: str(^evt_); type: str; version: int(ge=1); session_id: str(^ses_); created_at: UtcDateTime; data: DataT; metadata: JsonObject`（无 seq） | 提交前事件草稿（§3.1） | `src/opencode/contracts/events.py:138` |
| `opencode.contracts.events.DurableEvent[DataT]` | class (EventDraft) | `seq: int = Field(ge=1)` | 已提交持久事件（seq 由事务分配） | `src/opencode/contracts/events.py:167` |
| `opencode.contracts.events.EphemeralEvent[DataT]` | class (BaseModel) | `id; type; session_id; created_at; data; metadata`（无 version/seq） | 仅 live 的临时事件 | `src/opencode/contracts/events.py:178` |
| `opencode.contracts.events.EventDecodeError` | exception | `class EventDecodeError(Exception)` | 解码失败基类（fail closed） | `src/opencode/contracts/events.py:196` |
| `opencode.contracts.events.UnknownEventTypeError` | exception | `class UnknownEventTypeError(EventDecodeError)` | 类型未注册 | `src/opencode/contracts/events.py:200` |
| `opencode.contracts.events.UnknownEventVersionError` | exception | `class UnknownEventVersionError(EventDecodeError)` | 类型已注册但版本未知 | `src/opencode/contracts/events.py:204` |
| `opencode.contracts.events.DurabilityMismatchError` | exception | `class DurabilityMismatchError(EventDecodeError)` | 事件存在但挂在另一条总线 | `src/opencode/contracts/events.py:208` |
| `opencode.contracts.events.EventDefinition` | dataclass (frozen) | `event_type: str; durability: Durability; data_model: type[BaseModel]; version: int = EVENT_VERSION` | 注册表单条定义 | `src/opencode/contracts/events.py:213` |
| `opencode.contracts.events.EventRegistry` | class | `__init__(definitions: Sequence[EventDefinition] = ()); register(d); resolve_durable(type, version); resolve_ephemeral(type); decode_durable(raw); decode_ephemeral(raw)` | type+version 注册与失败关闭解码 | `src/opencode/contracts/events.py:268` |
| `opencode.contracts.events.R1_EVENT_DEFINITIONS` | constant (tuple) | `tuple[EventDefinition, ...]`，2 条（`py.session.created`、`py.prompt.accepted`，均 durable） | R1 事件子集 | `src/opencode/contracts/events.py:331` |
| `opencode.contracts.events.default_event_registry` | function | `() -> EventRegistry` | 基于 R1 定义新建注册表 | `src/opencode/contracts/events.py:337` |
| `opencode.storage.db.OpenMode` | type alias | `Literal["readonly", "create", "readwrite"]` | `open_database` 打开语义 | `src/opencode/storage/db.py:35` |
| `opencode.storage.db.BUSY_TIMEOUT_MS` | constant | `5000` | SQLite busy timeout（§5） | `src/opencode/storage/db.py:40` |
| `opencode.storage.db.WRITER_LOCK_NAME` | constant | `"writer.lock"` | 数据目录写者锁文件名 | `src/opencode/storage/db.py:43` |
| `opencode.storage.db.StorageError` | exception | `class StorageError(RuntimeError)` | 存储引擎错误基类 | `src/opencode/storage/db.py:46` |
| `opencode.storage.db.WriterLockHeldError` | exception | `class WriterLockHeldError(StorageError)` | 另一写者进程持锁 | `src/opencode/storage/db.py:50` |
| `opencode.storage.db.DatabaseNotFoundError` | exception | `class DatabaseNotFoundError(StorageError)` | readonly/readwrite 目标文件不存在 | `src/opencode/storage/db.py:54` |
| `opencode.storage.db.WriterLock` | class | `__init__(path: Path); property held -> bool; acquire() -> None; release() -> None`（`__slots__`） | 单写者 OS 字节区间锁 | `src/opencode/storage/db.py:83` |
| `opencode.storage.db.Database` | class | `path; mode; engine; property closed -> bool; async close() -> None; async __aenter__/__aexit__` | 引擎句柄 + 关闭即释放锁 | `src/opencode/storage/db.py:161` |
| `opencode.storage.db.open_database` | function | `(path: str | Path, *, mode: OpenMode = "create") -> Database` | 打开引擎；写者先取锁再开库 | `src/opencode/storage/db.py:203` |
| `opencode.storage.migrations.env.config` | constant | `config = context.config` | Alembic 环境脚本配置对象 | `src/opencode/storage/migrations/env.py:19` |
| `opencode.storage.migrations.env.run_migrations_offline` | function | `() -> None` | 离线模式产出 SQL（`upgrade --sql`） | `src/opencode/storage/migrations/env.py:34` |
| `opencode.storage.migrations.env.run_migrations_online` | function | `() -> None` | 在同步引擎上执行迁移 | `src/opencode/storage/migrations/env.py:41` |
| `opencode.storage.migrations.versions.0001_initial.revision` | constant | `revision: str = "0001"` | 迁移版本号 | `src/opencode/storage/migrations/versions/0001_initial.py:50` |
| `opencode.storage.migrations.versions.0001_initial.down_revision` | constant | `down_revision: str | None = None` | 前序迁移 | `.../0001_initial.py:51` |
| `opencode.storage.migrations.versions.0001_initial.branch_labels` | constant | `branch_labels: str | None = None` | 分支标签 | `.../0001_initial.py:52` |
| `opencode.storage.migrations.versions.0001_initial.depends_on` | constant | `depends_on: str | None = None` | 迁移依赖 | `.../0001_initial.py:53` |
| `opencode.storage.migrations.versions.0001_initial.upgrade` | function | `() -> None` | 建五表（events/sessions/messages/tool_executions/permissions）与具名唯一索引 | `.../0001_initial.py:56` |
| `opencode.storage.migrations.versions.0001_initial.downgrade` | function | `() -> None` | 拒绝降级，要求从校验备份恢复 | `.../0001_initial.py:132` |

> S3 说明：以下 `store.py` / `txn.py` 符号按首派口径（源码定义处公共符号，下划线私有名不收）收录；**导出性**列标注是否在 `opencode.storage.__all__` 中。`TransactionWriter` 为模块级公共名但明确**不导出，S4 私有**（CORE-CONTRACTS §3.2：仅供 `SessionStore` 调用）。

| 符号全名 | kind | 签名 / 值形态 | 职责 | 位置 | 导出性 |
|---|---|---|---|---|---|
| `opencode.storage.store.EventBatch` | class (dataclass, frozen, slots) | `id: str; events: tuple[EventDraft[Any], ...]; dedupe_key: str \| None = None` | 一次原子提交单元：batch 幂等键 + 有序草稿（+可选去重键） | `src/opencode/storage/store.py:31` | 导出 |
| `opencode.storage.store.Projector` | Protocol | `async apply(connection: AsyncConnection, event: DurableEvent[Any]) -> None` | 读模型投影 seam，在提交事务内按批次顺序调用 | `src/opencode/storage/store.py:45` | 导出 |
| `opencode.storage.store.SessionStore` | class | `__init__(engine: AsyncEngine, *, projector: Projector, registry: EventRegistry \| None = None); async commit(session_id: str, batch: EventBatch, expected_seq: int) -> list[DurableEvent[Any]]` | 唯一提交接口，内部委托 `TransactionWriter` | `src/opencode/storage/store.py:62` | 导出 |
| `opencode.storage.txn.REASON_READ_MODEL_MISSING` | constant | `"sessions_row_missing_with_log"` | 有日志行但 sessions 行缺失 | `src/opencode/storage/txn.py:63` | 不导出 |
| `opencode.storage.txn.REASON_APPLIED_SEQ_MISMATCH` | constant | `"applied_seq_mismatch"` | A≠H | `src/opencode/storage/txn.py:64` | 不导出 |
| `opencode.storage.txn.REASON_SEQ_GAP` | constant | `"seq_gap"` | seq 不连续 | `src/opencode/storage/txn.py:65` | 不导出 |
| `opencode.storage.txn.REASON_DUPLICATE_SEQ` | constant | `"duplicate_seq"` | seq 重复 | `src/opencode/storage/txn.py:66` | 不导出 |
| `opencode.storage.txn.REASON_ILLEGAL_SEQ` | constant | `"illegal_seq"` | seq < 1 | `src/opencode/storage/txn.py:67` | 不导出 |
| `opencode.storage.txn.REASON_UNKNOWN_STATUS` | constant | `"unknown_session_status"` | sessions.status 非法 | `src/opencode/storage/txn.py:68` | 不导出 |
| `opencode.storage.txn.REASON_STORED_DECODE_FAILED` | constant | `"stored_event_decode_failed"` | 已存事件解码失败 | `src/opencode/storage/txn.py:69` | 不导出 |
| `opencode.storage.txn.REASON_PROJECTION_INVARIANT` | constant | `"applied_seq_not_advanced_after_projection"` | 投影后 A 未推进到 H+n | `src/opencode/storage/txn.py:70` | 不导出 |
| `opencode.storage.txn.epoch_ms` | function | `(value: datetime) -> int` | datetime → 整数 epoch 毫秒（`events.created` 精度） | `src/opencode/storage/txn.py:106` | 不导出 |
| `opencode.storage.txn.from_epoch_ms` | function | `(value: int) -> datetime` | `epoch_ms` 的逆函数（毫秒精度） | `src/opencode/storage/txn.py:112` | 不导出 |
| `opencode.storage.txn.StoreError` | exception | `class StoreError(StorageError)`；`code: ClassVar[CoreErrorCode]; retryable: ClassVar[bool] = False; __init__(message: str, *, details: JsonObject \| None = None)`，携带 `self.core: CoreError` | 提交拒绝基类，锁定 §8 错误码 | `src/opencode/storage/txn.py:124` | 导出 |
| `opencode.storage.txn.SessionReadOnlyError` | exception | `class SessionReadOnlyError(StoreError)`；`code = CoreErrorCode.SESSION_READ_ONLY` | session 已隔离（read_only_diagnostic） | `src/opencode/storage/txn.py:145` | 导出 |
| `opencode.storage.txn.SessionClosedError` | exception | `class SessionClosedError(StoreError)`；`code = CoreErrorCode.SESSION_CLOSED` | session 已关闭 | `src/opencode/storage/txn.py:151` | 导出 |
| `opencode.storage.txn.LifecycleCleanupFailedError` | exception | `class LifecycleCleanupFailedError(StoreError)`；`code = CoreErrorCode.LIFECYCLE_CLEANUP_FAILED` | 资源清理失败 | `src/opencode/storage/txn.py:157` | 导出 |
| `opencode.storage.txn.StoreValidationError` | exception | `class StoreValidationError(StoreError)`；`code = CoreErrorCode.VALIDATION_ERROR` | 调用参数/所有权/版本/批次形状非法 | `src/opencode/storage/txn.py:163` | 导出 |
| `opencode.storage.txn.ConflictError` | exception | `class ConflictError(StoreError)`；`code = CoreErrorCode.CONFLICT` | `expected_seq` 与健康日志水位不等 | `src/opencode/storage/txn.py:169` | 导出 |
| `opencode.storage.txn.IdempotencyConflictError` | exception | `class IdempotencyConflictError(StoreError)`；`code = CoreErrorCode.IDEMPOTENCY_CONFLICT` | 批次身份冲突（混 id/内容漂移/跨 session） | `src/opencode/storage/txn.py:175` | 导出 |
| `opencode.storage.txn.StorageCorruptError` | exception | `class StorageCorruptError(StoreError)`；`code = CoreErrorCode.STORAGE_CORRUPT`；`__init__(message: str, *, isolate: bool = True, details=None)` | 存储不变量破坏；`isolate` 决定是否进入 §3.2.1 隔离 | `src/opencode/storage/txn.py:181` | 导出 |
| `opencode.storage.txn.IsolationPersistFailedError` | exception | `class IsolationPersistFailedError(StorageError)` | 诊断行落库失败（内存写门仍生效） | `src/opencode/storage/txn.py:198` | 导出 |
| `opencode.storage.txn.Diagnostic` | class (dataclass, frozen, slots) | `reason: str; first_bad_seq: int \| None = None; first_bad_event_id: str \| None = None; applied_seq: int = 0; to_json() -> JsonObject` | 脱敏 §3.2.1 诊断载荷 | `src/opencode/storage/txn.py:203` | 不导出 |
| `opencode.storage.txn.TransactionWriter` | class | `__init__(engine: AsyncEngine, *, projector: Projector, registry: EventRegistry); async commit(session_id: str, batch: EventBatch, expected_seq: int) -> list[DurableEvent[Any]]`（另有 per-session asyncio 锁 + 进程级写锁） | §3.2 事务序列与 §3.2.1 隔离实现；**不导出，S4 私有** | `src/opencode/storage/txn.py:258` | **不导出（S4 私有）** |

### 1.3 R1-S4（session/state.py、session/projector.py）

S4 说明：`session/__init__.py` 仅再导出（`__all__` 5 项），无模块级新定义，故不单独计符号。`session/projector.py` 的 `REASON_*` 为模块级公共名但**不导出**（沿用 `TransactionWriter` 的标注方式）。

| 符号全名 | kind | 签名 / 值形态 | 职责 | 位置 | 导出性 |
|---|---|---|---|---|---|
| `opencode.session.state.SessionStatus` | class (StrEnum) | `ACTIVE="active"; READ_ONLY_DIAGNOSTIC="read_only_diagnostic"; CLEANUP_FAILED="cleanup_failed"; CLOSED="closed"` | §3.2 四种 session 状态（`.value` 即存储文本） | `src/opencode/session/state.py:37` | 导出 |
| `opencode.session.state.IllegalTransitionError` | exception | `class IllegalTransitionError(ValueError)` | 事件无法合法延展当前状态（fold 失败关闭） | `src/opencode/session/state.py:46` | 导出 |
| `opencode.session.state.SessionState` | class (dataclass, frozen, slots) | `session_id: str \| None = None; status: SessionStatus \| None = None; applied_seq: int = 0; working_dir: str \| None = None; config_snapshot: ConfigSnapshot \| None = None; snapshot_hash: str \| None = None; messages: tuple[UserMessage, ...] = (); active_run_id: str \| None = None; property created -> bool`（`active_run_id` 为 S7 追加） | 单 session 的折叠状态（created 前为空态） | `src/opencode/session/state.py:56` | 导出 |
| `opencode.session.state.apply` | function | `(state: SessionState, event: DurableEvent[Any]) -> SessionState` | 纯函数折叠：created 建态、prompt 追加消息并置 `active_run_id`；其余类型 fail closed | `src/opencode/session/state.py:80` | 导出 |
| `opencode.session.projector.REASON_DUPLICATE_SESSION_PROJECTION` | constant | `"duplicate_session_projection"` | sessions 行重复投影 | `src/opencode/session/projector.py:58` | 不导出 |
| `opencode.session.projector.REASON_PROMPT_WITHOUT_SESSION` | constant | `"prompt_accepted_without_session"` | prompt 事件缺少 sessions 行 | `src/opencode/session/projector.py:59` | 不导出 |
| `opencode.session.projector.REASON_ILLEGAL_TRANSITION` | constant | `"illegal_projection_transition"` | fold 抛出的非法转移/不可投影类型 | `src/opencode/session/projector.py:60` | 不导出 |
| `opencode.session.projector.REASON_UNKNOWN_SESSION_STATUS` | constant | `"unknown_session_status"` | 存储的 status 非法 | `src/opencode/session/projector.py:61` | 不导出 |
| `opencode.session.projector.REASON_DUPLICATE_MESSAGE_ID` | constant | `"duplicate_message_id"` | 同 session 内 message_id 重复 | `src/opencode/session/projector.py:62` | 不导出 |
| `opencode.session.projector.SessionProjector` | class | `async apply(connection: AsyncConnection, event: DurableEvent[Any]) -> None`（无状态；内部 `_project_created`/`_project_prompt`/`_advance_applied_seq` 为私有；S7 追加 `active_run_id` 投影，未新增公共名） | §3.2 真实读模型投影器：sessions/messages 落库并在事务内推进 `applied_seq` | `src/opencode/session/projector.py:95` | 导出 |

**S4 对 `storage/txn.py` 的核对（N-1/N-3/M-1/Nit-2）**：逐条比对 `git diff 72de622 077caf6`（N-1/N-3）与 `git diff 077caf6 698b456`（M-1/Nit-2）——**均未新增模块级公共符号**，结论如下：

| 项 | 改动实质 | 是否新增公共符号 |
|---|---|---|
| N-1 | 隔离持久化失败时由 `pass` 改为 `raise corruption from persist_error`（显式异常链） | 否（`commit` 内部行为） |
| N-3 | `epoch_ms` docstring 补充毫秒截断语义 | 否（仅注释；符号已在 §1.2 收录） |
| M-1 | 幂等比较循环内按行解码 JSON，`ValueError` → `StorageCorruptError`（`REASON_STORED_DECODE_FAILED`） | 否（`_idempotent_replay` 内部，私有） |
| Nit-2 | 重放解码改为逐行，诊断定位坏行并携带 `applied` | 否（`_idempotent_replay` 内部，私有） |

附带核对：`txn.py` 公共名集合与 S3 完全一致（8 个 `REASON_*`、`epoch_ms`/`from_epoch_ms`、9 个错误类、`Diagnostic`、`TransactionWriter`；行号因改动顺移）；`src/opencode/storage/__init__.py` 的 `__all__` 仍为 **21** 项，无变化。

### 1.4 R1-S5（bus/bus.py）

S5 说明：`bus/__init__.py` 仅再导出（`__all__` 4 项），无模块级新定义，故不单独计符号。与源码核对结果：`bus/bus.py` 公共名恰为预期的 4 个（`_Terminated`/`_Subscription` 为下划线私有，不收）。

| 符号全名 | kind | 签名 / 值形态 | 职责 | 位置 | 导出性 |
|---|---|---|---|---|---|
| `opencode.bus.bus.Event` | type alias | `type Event = DurableEvent[Any] \| EphemeralEvent[Any]` | live 订阅可产出的事件（committed durable + ephemeral）；契约无统一别名，由 bus 自持 | `src/opencode/bus/bus.py:53` | 导出 |
| `opencode.bus.bus.DEFAULT_QUEUE_LIMIT` | constant | `1000` | 每订阅有界队列默认上限（§4） | `src/opencode/bus/bus.py:56` | 导出 |
| `opencode.bus.bus.SlowConsumerError` | exception | `class SlowConsumerError(Exception)`；`__init__(message: str, *, details: JsonObject \| None = None)`，`self.core: CoreError`（`code=SLOW_CONSUMER`、`retryable=False`） | 订阅队列溢出 → 断开并向迭代器抛错 | `src/opencode/bus/bus.py:59` | 导出 |
| `opencode.bus.bus.VolatileBus` | class | `__init__(*, queue_limit: int = DEFAULT_QUEUE_LIMIT, registry: EventRegistry \| None = None); property subscription_count -> int; subscribe(session_id: str, types: Iterable[str] \| None = None) -> AbstractAsyncContextManager[AsyncIterator[Event]]; notify(session_id: str, events: Sequence[Event]) -> None` | 纯内存 live 总线：订阅/通知、类型过滤、溢出断开；不持久化、不重放 | `src/opencode/bus/bus.py:190` | 导出 |

**S5 对 `storage/`、`session/` 的核对**：`git diff 18f74ff f45f880 --stat` 显示源码改动仅 `src/opencode/bus/__init__.py`、`src/opencode/bus/bus.py` 与两个新测试文件；其余为文档/任务/进度文件（`doc/ORCHESTRATION.md`、`doc/orchestration/{progress/T-C1-007,tasks/T-B-010}.md`、`doc/reviews/R1-S5-review.md`、本 inventory）。**`storage/` 与 `session/` 零触碰**；`storage.__all__` 仍 **21** 项、`session.__all__` 仍 **5** 项，均无变化。

### 1.5 R1-S6（bus/eventlog.py、storage/repos.py）

S6 说明：`bus/eventlog.py` 是 **DB 耦合面**，`bus/__init__.py` 有意**不 re-export**（volatile 出口保持 4 项），其全部公共名标为「不导出（直连 import）」。`storage/repos.py` 的 5 个类型/异常经 `storage/__init__.py` 追加导出，其余常量与 `clamp_page_limit` 未导出。两文件的下划线前缀私有名（`_Follow`/`_resolve_cursor`/`_corrupt` 等）不收。

| 符号全名 | kind | 签名 / 值形态 | 职责 | 位置 | 导出性 |
|---|---|---|---|---|---|
| `opencode.bus.eventlog.MIN_READ_LIMIT` | constant | `1` | §4 读 limit 下界 | `src/opencode/bus/eventlog.py:93` | 不导出（直连 import） |
| `opencode.bus.eventlog.MAX_READ_LIMIT` | constant | `1000` | §4 读 limit 上界 | `src/opencode/bus/eventlog.py:94` | 不导出（直连 import） |
| `opencode.bus.eventlog.DEFAULT_READ_LIMIT` | constant | `100` | §4 默认页大小 | `src/opencode/bus/eventlog.py:95` | 不导出（直连 import） |
| `opencode.bus.eventlog.DEFAULT_FOLLOW_PAGE_SIZE` | constant | `512` | follow 内部追平/实时页大小（TS `logReadPageSize`） | `src/opencode/bus/eventlog.py:98` | 不导出（直连 import） |
| `opencode.bus.eventlog.MAX_POLL_INTERVAL` | constant | `1.0` | 轮询兜底上限（秒，契约不超过 1s） | `src/opencode/bus/eventlog.py:101` | 不导出（直连 import） |
| `opencode.bus.eventlog.STATUS_READ_ONLY_DIAGNOSTIC` | constant | `"read_only_diagnostic"` | 隔离 session 的 status wire 字面量 | `src/opencode/bus/eventlog.py:104` | 不导出（直连 import） |
| `opencode.bus.eventlog.REASON_SESSION_READ_ONLY` | constant | `"session_read_only_diagnostic"` | 隔离 session 的 typed read/follow 拒绝原因 | `src/opencode/bus/eventlog.py:107` | 不导出（直连 import） |
| `opencode.bus.eventlog.REASON_NON_INCREASING_SEQ` | constant | `"non_increasing_seq"` | follow 连接内 seq 非严格递增 | `src/opencode/bus/eventlog.py:108` | 不导出（直连 import） |
| `opencode.bus.eventlog.EventCursor` | class (dataclass, frozen, slots) | `session_id: str; seq: int` | §4 游标 `{session_id, seq>=0}`；0 表示日志起点 | `src/opencode/bus/eventlog.py:124` | 不导出（直连 import） |
| `opencode.bus.eventlog.EventPage` | class (dataclass, frozen, slots) | `items: list[DurableEvent[Any]]; next_cursor: EventCursor; has_more: bool` | §4 单次读取页 | `src/opencode/bus/eventlog.py:139` | 不导出（直连 import） |
| `opencode.bus.eventlog.Synced` | class (dataclass, frozen, slots) | `cursor: EventCursor` | follow 的追平标记（每次 follow 恰好一次） | `src/opencode/bus/eventlog.py:152` | 不导出（直连 import） |
| `opencode.bus.eventlog.LogItem` | type alias | `type LogItem = DurableEvent[Any] \| Synced` | follow 产出项 | `src/opencode/bus/eventlog.py:164` | 不导出（直连 import） |
| `opencode.bus.eventlog.EventLog` | class | `__init__(engine: AsyncEngine, *, bus: VolatileBus \| None = None, registry: EventRegistry \| None = None, repos: ReadRepositories \| None = None, poll_interval: float = 1.0, page_size: int = DEFAULT_FOLLOW_PAGE_SIZE); async read(session_id: str, after: EventCursor \| None = None, limit: int = DEFAULT_READ_LIMIT) -> EventPage; follow(session_id: str, after: EventCursor \| None = None) -> AbstractAsyncContextManager[AsyncIterator[LogItem]]` | §4 durable feed：分页读取 + 四步 follow（先注册唤醒→取 H→追平+Synced→实时轮询兜底） | `src/opencode/bus/eventlog.py:217` | 不导出（直连 import） |
| `opencode.storage.repos.MIN_PAGE_LIMIT` | constant | `1` | 读侧页 limit 下界 | `src/opencode/storage/repos.py:64` | 不导出 |
| `opencode.storage.repos.MAX_PAGE_LIMIT` | constant | `1000` | 读侧页 limit 上界 | `src/opencode/storage/repos.py:65` | 不导出 |
| `opencode.storage.repos.DEFAULT_PAGE_LIMIT` | constant | `100` | 读侧默认页大小 | `src/opencode/storage/repos.py:66` | 不导出 |
| `opencode.storage.repos.REASON_MESSAGE_ROW_DECODE_FAILED` | constant | `"message_row_decode_failed"` | 投影消息行解码失败原因 | `src/opencode/storage/repos.py:69` | 不导出 |
| `opencode.storage.repos.REASON_SESSION_ROW_DECODE_FAILED` | constant | `"session_row_decode_failed"` | sessions 行 JSON 解码失败原因 | `src/opencode/storage/repos.py:72` | 不导出 |
| `opencode.storage.repos.NotFoundError` | exception | `class NotFoundError(StoreError)`；`code = CoreErrorCode.NOT_FOUND` | 未创建 session | `src/opencode/storage/repos.py:99` | 导出 |
| `opencode.storage.repos.InvalidCursorError` | exception | `class InvalidCursorError(StoreError)`；`code = CoreErrorCode.INVALID_CURSOR` | 游标非法/不属本 session 或本查询 | `src/opencode/storage/repos.py:105` | 导出 |
| `opencode.storage.repos.SessionRecord` | class (dataclass, frozen, slots) | `session_id: str; status: str; applied_seq: int; active_run_id: str \| None = None; config_snapshot: JsonObject \| None = None; snapshot_hash: str \| None = None; diagnostic: JsonObject \| None = None` | 只读 sessions 行视图 | `src/opencode/storage/repos.py:112` | 导出 |
| `opencode.storage.repos.MessagePage` | class (dataclass, frozen, slots) | `items: list[UserMessage]; next_after_message_id: str \| None; has_more: bool` | 按 seq 排序的投影消息页 | `src/opencode/storage/repos.py:125` | 导出 |
| `opencode.storage.repos.clamp_page_limit` | function | `(limit: int) -> int` | 将 limit 夹到 1..1000；bool/非整数拒绝 | `src/opencode/storage/repos.py:152` | 不导出 |
| `opencode.storage.repos.ReadRepositories` | class | `__init__(engine: AsyncEngine); async session(session_id: str) -> SessionRecord \| None; async messages(session_id: str, *, after_message_id: str \| None = None, limit: int = DEFAULT_PAGE_LIMIT) -> MessagePage; async log_high(session_id: str) -> int; async prompt_by_dedupe_key(session_id: str, dedupe_key: str) -> AcceptedPrompt \| None; async prompt_by_run_id(session_id: str, run_id: str) -> AcceptedPrompt \| None; async session_by_active_run(run_id: str) -> SessionRecord \| None`（后 4 个为 S7 追加方法，非模块级符号） | 只读查询面（sessions/messages + S7 提交锁/幂等四查询），不写、不加锁、不依赖 bus | `src/opencode/storage/repos.py:159` | 导出 |

**S6 对既有面的核对**：`git diff 35bc781 f8ce8f6 --stat` 显示源码改动仅 `src/opencode/bus/eventlog.py`（新增）、`src/opencode/storage/repos.py`（新增）、`src/opencode/storage/__init__.py`（+12 行，纯追加 5 导出）与 `tests/integration/test_eventlog.py`（新增）；`bus/bus.py`、`bus/__init__.py`、`storage/txn.py`、`storage/store.py`、`storage/db.py`、`src/opencode/session/` **零触碰**。导出面核对：`storage.__all__` **26**（21+5，无删除）、`bus.__all__` **4**、`session.__all__` **5**。

### 1.6 R1-S7（session/service.py、app.py、storage/repos.py 追加）

S7 说明：`session/__init__.py` 与 `storage/__init__.py` 均只追加再导出（分别 +6/+1），无模块级新定义；`app.py` 为新增组合模块（模块级仅 `App` 一个公共名）。`session/state.py`/`session/projector.py` 仅为 `active_run_id` 接线（新增字段与私有 SQL/私有 helper），**0 个新公共名**；`storage/repos.py` 追加 `AcceptedPrompt`、`REASON_DEDUPE_ROW_UNEXPECTED` 与 4 个 `ReadRepositories` 方法（方法不计入符号行）。

| 符号全名 | kind | 签名 / 值形态 | 职责 | 位置 | 导出性 |
|---|---|---|---|---|---|
| `opencode.session.service.REASON_ACTIVE_RUN_WITHOUT_PROMPT` | constant | `"active_run_without_prompt_event"` | `run_status` 读到的 active-run 槽与 accepted 事件不一致时的脱敏原因 | `src/opencode/session/service.py:100` | 不导出（直连 import） |
| `opencode.session.service.SessionBusyError` | exception | `class SessionBusyError(StoreError)`；`code = CoreErrorCode.SESSION_BUSY` | 已有 active run 时 `prompt` 拒绝（§7 L316/L323）；**唯一在 service 层定义的实现错误类**（无 storage 模块持有） | `src/opencode/session/service.py:117` | 导出 |
| `opencode.session.service.SessionRef` | class (dataclass, frozen, slots) | `session_id: str` | §7 `create` 返回的 session 引用 | `src/opencode/session/service.py:129` | 导出 |
| `opencode.session.service.RunRef` | class (dataclass, frozen, slots) | `session_id: str; run_id: str; request_id: str` | §7 `prompt` 返回的 run 引用（不含调度承诺） | `src/opencode/session/service.py:136` | 导出 |
| `opencode.session.service.RunStatusValue` | class (StrEnum) | `ACCEPTED="accepted"; RUNNING="running"; CANCELLING="cancelling"; SUCCEEDED="succeeded"; FAILED="failed"; INTERRUPTED="interrupted"` | §7 run 状态枚举；R1 只能到达 `accepted` | `src/opencode/session/service.py:148` | 导出 |
| `opencode.session.service.RunStatus` | class (dataclass, frozen, slots) | `session_id: str; run_id: str; request_id: str; status: RunStatusValue; error: str \| None = None` | §7 `run_status` 读模型 | `src/opencode/session/service.py:160` | 导出 |
| `opencode.session.service.Clock` | type alias | `type Clock = Callable[[], datetime]` | 可注入时钟（返回 aware UTC） | `src/opencode/session/service.py:171` | 不导出（直连 import） |
| `opencode.session.service.IdFactory` | Protocol | `session_id() -> str; event_id() -> str; message_id() -> str; run_id() -> str; batch_id() -> str` | 可注入 ID 工厂（§7 step 4 / open point B） | `src/opencode/session/service.py:174` | 不导出（直连 import） |
| `opencode.session.service.AscendingIds` | class | `__init__(clock: Clock \| None = None); session_id(); event_id(); message_id(); run_id(); batch_id()`；输出 `<prefix><13 位 ms><10 位计数>` | 默认 ID 工厂：时间有序、可字典序排序、进程内唯一（非 UUID） | `src/opencode/session/service.py:194` | 不导出（直连 import） |
| `opencode.session.service.prompt_dedupe_key` | function | `(request_id: str) -> str`；返回 `"prompt:" + request_id` | §7 L321 幂等去重键（大小写敏感、跨 session 可复用） | `src/opencode/session/service.py:260` | 不导出（直连 import） |
| `opencode.session.service.SessionService` | class | `__init__(*, store: SessionStore, repos: ReadRepositories, bus: VolatileBus, working_dir: str, clock: Clock \| None = None, ids: IdFactory \| None = None); property closed -> bool; async create(config_snapshot: ConfigSnapshot \| JsonObject) -> SessionRef; async session_status(session_id: str) -> SessionStatus; async messages(session_id: str, *, after_message_id: str \| None = None, limit: int = 100) -> MessagePage; async prompt(session_id: str, text: str, request_id: str) -> RunRef; async run_status(run_id: str) -> RunStatus; async close() -> None` | §7 应用命令面（R1 子集）；每 session 短命提交锁下的四步固定序 | `src/opencode/session/service.py:277` | 导出 |
| `opencode.app.App` | class | `__init__(db_path: str \| Path, *, mode: OpenMode = "create", working_dir: str \| None = None, bus: VolatileBus \| None = None, clock: Clock \| None = None, ids: IdFactory \| None = None); property database -> Database; property bus -> VolatileBus; property service -> SessionService; async close() -> None; async __aenter__/__aexit__` | R1 显式组合根：持有 Database/bus/service，按固定序关闭 | `src/opencode/app.py:30` | 不导出（模块级，无包 `__all__`） |
| `opencode.storage.repos.AcceptedPrompt` | class (dataclass, frozen, slots) | `request_id: str; run_id: str; message_text: str` | 按 dedupe key / run id 定位的一条已提交 `py.prompt.accepted`（保留原文 `message.text`） | `src/opencode/storage/repos.py:134` | 导出 |
| `opencode.storage.repos.REASON_DEDUPE_ROW_UNEXPECTED` | constant | `"dedupe_row_unexpected_type"` | 幂等/运行查询命中行形状异常的原因 | `src/opencode/storage/repos.py:76` | 不导出 |

**S7 既有面的核对**：

- `ReadRepositories` 新增 4 个**方法**（`log_high` :300、`prompt_by_dedupe_key` :310、`prompt_by_run_id` :335、`session_by_active_run` :356），不是模块级符号，故不新增符号行；其签名已并入 §1.5 的 `ReadRepositories` 行。
- `SessionBusyError` 在 `session/service.py` 定义（storage 各模块均不持有），经 `session/__init__.py` 导出；实现侧错误类由 **22 → 23**（22 个 §8 码基类不变，仅多此一个 `SESSION_BUSY` 的具体类）。
- `session/state.py`（`SessionState.active_run_id` 字段）与 `session/projector.py`（`_SET_ACTIVE_RUN` 私有 helper/投影接线）**新增公共符号 0 个**。
- **零触碰面**：`contracts/`、`storage/txn.py`、`storage/store.py`、`storage/db.py`、`bus/`、`storage/migrations/` 均未被 S7 改动；`storage.__all__` 26→27（+`AcceptedPrompt`）、`session.__all__` 5→11（+6）、`bus.__all__` 4 不变。
- **与任务预期的差异（仅报告）**：任务书写 S7 diff 为“11 files（4 new + 7 modified）”，实际 `git diff 848031f ec24f3e --stat` 为 **15 files**（新增 3：`src/opencode/app.py`、`src/opencode/session/service.py`、`tests/integration/test_service.py`；修改 12，含 docs/进度文件）。本盘点以实际 diff 为准。

## 2. 错误码三方对照表

契约来源：`doc/CORE-CONTRACTS.md` §8，全部 22 个码位于**同一行 L340**（逐个直读，非记忆）。实现：`CoreErrorCode` 成员，`src/opencode/contracts/errors.py:24-45`。
**测试引用数注（口径）**：统计 `tests/` 下全部 `*.py`，表列数字 = 两个**互斥**分量之和：
1. **码名引号字面量**次数：码名作为字符串字面量出现，例如双引号形式 `"ValidationError"`、单引号形式 `'ValidationError'`，亦覆盖 `"NotFound"` 等被 `CoreError.model_validate` 直接解析的 wire 值；命令 `Select-String -CaseSensitive -Pattern '"<CodeName>"' -AllMatches`（单引号字面量另以 `"'<CodeName>'"` 统计），按 `Matches` 计数。
2. **枚举成员名**次数：例如 `'\bVALIDATION_ERROR\b'`（`CoreErrorCode.VALIDATION_ERROR` 的成员名形态），命令 `Select-String -CaseSensitive -Pattern '\b(MEMBER_NAME)\b' -AllMatches`，按 `Matches` 计数。

两分量针对不同文本形态（字符串值 vs 成员标识符），不重复计数；合计即表列数字。错误类断言（如 `pytest.raises(StorageCorruptError)`）不计入本列，仅计码字面量/枚举成员。

| 码名 | 契约行 | 实现位置 | 测试引用数 | 一致性结论 |
|---|---|---|---|---|
| `ValidationError` | L340 | `errors.py:24`（`VALIDATION_ERROR`） | 8（4 + 4） | 一致 |
| `UnsupportedCapability` | L340 | `errors.py:25`（`UNSUPPORTED_CAPABILITY`） | 1 | 一致 |
| `NotFound` | L340 | `errors.py:26`（`NOT_FOUND`） | 10（2 + 8） | 一致 |
| `SessionBusy` | L340 | `errors.py:27`（`SESSION_BUSY`） | 3（1 + 2） | 一致 |
| `Conflict` | L340 | `errors.py:28`（`CONFLICT`） | 4（1 + 3） | 一致 |
| `IdempotencyConflict` | L340 | `errors.py:29`（`IDEMPOTENCY_CONFLICT`） | 5（1 + 4） | 一致 |
| `InvalidCursor` | L340 | `errors.py:30`（`INVALID_CURSOR`） | 7（1 + 6） | 一致 |
| `PermissionDenied` | L340 | `errors.py:31`（`PERMISSION_DENIED`） | 1 | 一致 |
| `ApprovalUnavailable` | L340 | `errors.py:32`（`APPROVAL_UNAVAILABLE`） | 1 | 一致 |
| `ApprovalExpired` | L340 | `errors.py:33`（`APPROVAL_EXPIRED`） | 1 | 一致 |
| `ToolTimeout` | L340 | `errors.py:34`（`TOOL_TIMEOUT`） | 1 | 一致 |
| `ToolFailed` | L340 | `errors.py:35`（`TOOL_FAILED`） | 1 | 一致 |
| `ExecutionUnknown` | L340 | `errors.py:36`（`EXECUTION_UNKNOWN`） | 1 | 一致 |
| `ProviderUnavailable` | L340 | `errors.py:37`（`PROVIDER_UNAVAILABLE`） | 5（R0 spike 断言） | 一致 |
| `ProviderStreamInterrupted` | L340 | `errors.py:38`（`PROVIDER_STREAM_INTERRUPTED`） | 3（R0 spike 断言） | 一致 |
| `ContextLimitExceeded` | L340 | `errors.py:39`（`CONTEXT_LIMIT_EXCEEDED`） | 1 | 一致 |
| `SlowConsumer` | L340 | `errors.py:40`（`SLOW_CONSUMER`） | 3（1 + 2） | 一致 |
| `StorageCorrupt` | L340 | `errors.py:41`（`STORAGE_CORRUPT`） | 21（1 + 20） | 一致 |
| `MigrationRequired` | L340 | `errors.py:42`（`MIGRATION_REQUIRED`） | 2 | 一致 |
| `SessionReadOnly` | L340 | `errors.py:43`（`SESSION_READ_ONLY`） | 4（1 + 3） | 一致 |
| `SessionClosed` | L340 | `errors.py:44`（`SESSION_CLOSED`） | 4（1 + 3） | 一致 |
| `LifecycleCleanupFailed` | L340 | `errors.py:45`（`LIFECYCLE_CLEANUP_FAILED`） | 2（1 + 1） | 一致 |

汇总：契约码数 **22** / 实现码数 **22** / 表行数 **22**。顺序、拼写、数量三方完全一致，无差异。测试引用合计 **89**（首派 45 → S3 53 → S4 61 → S5 62 → S6 74 → 本次 89，+15）：`ValidationError` 6→8（+2）、`NotFound` 6→10（+4）、`SessionBusy` 2→3（+1）、`IdempotencyConflict` 4→5（+1）、`InvalidCursor` 6→7（+1）、`StorageCorrupt` 20→21（+1）、`SessionReadOnly` 3→4（+1）、`SessionClosed` 1→4（+3）、`LifecycleCleanupFailed` 1→2（+1），全部来自 S7 新增 `tests/integration/test_service.py`；其余 13 码不变（含 `SlowConsumer` 3）。

## 3. 盘点方法注记（可复现）

1. 目录枚举：`ctx_tree src/opencode/contracts`、`ctx_tree src/opencode/storage`（depth=3）。
2. 全文直读：`ctx_read <file> mode=full` 读取 `contracts/{__init__,errors,events,messages}.py` 与 `storage/{__init__,db}.py`、`storage/migrations/env.py`、`storage/migrations/versions/0001_initial.py`。
3. 定义行号：`Get-ChildItem -Recurse src/opencode/contracts,src/opencode/storage -Filter *.py | ForEach-Object { Select-String -Path $_.FullName -Pattern '^(class |def |type |[A-Za-z_][A-Za-z0-9_]*\s*(:|=))' }`（下划线前缀私有名已剔除）。
4. 契约定位：`Select-String -Path doc/CORE-CONTRACTS.md -Pattern '^## '` 得章节行号；`ctx_read doc/CORE-CONTRACTS.md lines:335-360` 直读 §8，码清单在 L340。
5. 实现码行号：`Select-String -Path src/opencode/contracts/errors.py -Pattern '^    [A-Z_]+ = '`。
6. 测试引用计数（区分大小写，避免 `CONFLICT` 命中 `IdempotencyConflict`）：`Get-ChildItem -Recurse tests -Filter *.py | Select-String -CaseSensitive -Pattern '"<Code>"' -AllMatches` 与 `-Pattern '\b<MEMBER>\b' -AllMatches`，对 `Matches` 求和。
7. 门禁：`uv run pytest -q`；`uv run ruff check .`；`uv run ruff format --check .`；`uv run mypy src tools/r0 tests/r0 tests/contract tests/integration`。
8. 变更核对：`git status --short`、`git log --oneline -3`（只读）。
9. S3 滚动（T-C2-002）定义行号：`Get-ChildItem src/opencode/storage/store.py,src/opencode/storage/txn.py | ForEach-Object { Select-String -Path $_.FullName -Pattern '^(class |def |REASON_[A-Z_]+|@dataclass)' }`。
10. S3 导出面核对：`ctx_read src/opencode/storage/__init__.py mode=full`，逐项比对 `__all__`（21 项）与源码定义处公共名，标出 `TransactionWriter`/`Diagnostic`/`epoch_ms`/`from_epoch_ms`/`REASON_*` 为不导出。
11. S3 错误码引用重算：沿用第 6 条同一命令与同一口径，仅扩到含 `tests/integration/test_store_commit.py` 的全 `tests/` 树；`Select-String` 全部显式 `-CaseSensitive`。
12. S4 滚动（T-C2-003）定义行号：`Get-ChildItem -Recurse src/opencode/session -Filter *.py | ForEach-Object { Select-String -Path $_.FullName -Pattern '^(class |def |type |[A-Za-z_][A-Za-z0-9_]*\s*(:|=)|@dataclass)' }`；`storage/txn.py` 用 `-Pattern '^(class |def |REASON_[A-Z_]+)'` 复核公共名集合。
13. S4 导出面核对：`ctx_read src/opencode/session/__init__.py mode=full` 得 `__all__` 5 项；`Select-String -Path src/opencode/storage/__init__.py -Pattern '^    "'` 计数得 21 项（与 S3 相同）。
14. S4 对 `txn.py` 的改动核对：`git diff 72de622 077caf6 -- src/opencode/storage/txn.py`（N-1/N-3）与 `git diff 077caf6 698b456 -- src/opencode/storage/txn.py`（M-1/Nit-2），逐行判断是否新增模块级公共符号。
15. S4 错误码引用重算：沿用第 6 条命令与口径，扫描含 `tests/integration/test_projector.py` 的全 `tests/` 树，显式 `-CaseSensitive`。
16. S5 滚动（T-C2-004）定义行号：`Select-String -Path src/opencode/bus/bus.py -Pattern '^(type |class |def |[A-Z_]+ = )'`；公共名 4 个（`Event`/`DEFAULT_QUEUE_LIMIT`/`SlowConsumerError`/`VolatileBus`）。
17. S5 导出面与零触碰核对：`Select-String -Path src/opencode/{storage,session,bus}/__init__.py -Pattern '^    "'` 计数得 21/5/4；`git diff 18f74ff f45f880 --stat` 确认源码改动仅 `bus/` 两文件 + 两测试文件。
18. S5 错误码引用重算：沿用第 6 条命令与口径，扫描含 `tests/contract/test_bus.py`、`tests/integration/test_bus_commit.py` 的全 `tests/` 树，显式 `-CaseSensitive`。
19. S6 滚动（T-C2-005）定义行号：`Get-ChildItem src/opencode/bus/eventlog.py,src/opencode/storage/repos.py | ForEach-Object { Select-String -Path $_.FullName -Pattern '^(type |class |def |[A-Z_]+ = |@dataclass)' }`；公共名 13 + 11。
20. S6 导出面与零触碰核对：`Select-String -Path src/opencode/{storage,session,bus}/__init__.py -Pattern '^    "'` 计数得 26/5/4；`git diff 35bc781 f8ce8f6 --stat` 确认源码改动仅 `bus/eventlog.py`、`storage/repos.py`、`storage/__init__.py`（纯追加 5 导出）+ 一测试文件。
21. S6 错误码引用重算：沿用第 6 条命令与口径，扫描含 `tests/integration/test_eventlog.py` 的全 `tests/` 树，显式 `-CaseSensitive`。
22. S7 滚动（T-C2-006）定义行号：`Get-ChildItem src/opencode/session/service.py,src/opencode/app.py,src/opencode/storage/repos.py,src/opencode/session/state.py,src/opencode/session/projector.py | ForEach-Object { Select-String -Path $_.FullName -Pattern '^(type |class |def |[A-Z_]+ = |@dataclass)' }`；新公共名 11（service）+ 1（app）+ 2（repos 追加）。
23. S7 导出面核对：`Select-String -Path src/opencode/{storage,session,bus}/__init__.py -Pattern '^    "'` 计数得 **27/11/4**；`Select-String -Path src/opencode/storage/repos.py -Pattern '^    async def '` 得 `ReadRepositories` 新增 4 方法（`log_high`/`prompt_by_dedupe_key`/`prompt_by_run_id`/`session_by_active_run`，不计入符号行）；`git diff 848031f ec24f3e --stat` 为 15 files（含 3 新增 + 12 修改，与任务书预期 11 files 不同，仅报告）。
24. S7 错误码引用重算：沿用第 6 条命令与口径，扫描含 `tests/integration/test_service.py` 的全 `tests/` 树，显式 `-CaseSensitive`；`state.py`/`projector.py` 的 `active_run_id` 接线另以 `git diff 848031f ec24f3e -- src/opencode/session/state.py src/opencode/session/projector.py` 逐行确认 0 个新公共名。

### 观测到的非本任务差异（仅报告，未处理）

- 门禁 `ruff format --check .` 本次输出 **114 files already formatted**、退出码 0（基线口径为退出码，计数不作口径）；S6 滚动记录为 106，变化来自扫描集合（工作树新增 S7 文件），无格式问题。
- `git status --short` 本次滚动前后均含他席位/调度器改动：仅 `?? doc/orchestration/logs/`（调度器常驻未跟踪目录；S7 已随 merge #57 `ec24f3e` 落库，源码无待提交改动）。本任务只修改本文件，未触碰该路径。
