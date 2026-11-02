# R1-S1..R1-S4 已交付公共面静态清单（滚动更新）

> 范围：覆盖 **R1-S1..R1-S4 已并入主树的交付面**（滚动更新：S5 并入后另行滚动）——`src/opencode/contracts/`、`src/opencode/storage/`、`src/opencode/session/` 的**源码定义处**公共符号（不含 `import` 绑定、不含包 `__init__` 的再导出）。全部数据由源码直读/正则扫描得出，未做任何修改。
> 符号总数：**79** = 首派 45（contracts 27 + storage 18）+ S3 新增 24（`store.py` 3 + `txn.py` 21）+ S4 新增 10（`session/state.py` 4 + `session/projector.py` 6；`session/__init__.py` 仅再导出、0 个新定义）。
> 包级再导出：`contracts.__all__` 27 项（与本包定义一一对应）；`storage.__all__` 21 项（db 9 + store 3 + txn 9，S4 前后不变）；`session.__all__` 5 项（`apply`/`IllegalTransitionError`/`SessionProjector`/`SessionState`/`SessionStatus`）——`TransactionWriter`、`Diagnostic`、`epoch_ms`/`from_epoch_ms`、`storage/txn.py` 的 `REASON_*`、`session/projector.py` 的 `REASON_*` 均**未进** `__all__`。
> 扫描时间：2026-11-02（首派）；2026-11-02 S3 滚动；2026-11-02 S4 滚动；工作树状态见 §3 注。

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
| `opencode.session.state.SessionState` | class (dataclass, frozen, slots) | `session_id: str \| None = None; status: SessionStatus \| None = None; applied_seq: int = 0; working_dir: str \| None = None; config_snapshot: ConfigSnapshot \| None = None; snapshot_hash: str \| None = None; messages: tuple[UserMessage, ...] = (); property created -> bool` | 单 session 的折叠状态（created 前为空态） | `src/opencode/session/state.py:56` | 导出 |
| `opencode.session.state.apply` | function | `(state: SessionState, event: DurableEvent[Any]) -> SessionState` | 纯函数折叠：created 建态、prompt 追加消息；其余类型 fail closed | `src/opencode/session/state.py:79` | 导出 |
| `opencode.session.projector.REASON_DUPLICATE_SESSION_PROJECTION` | constant | `"duplicate_session_projection"` | sessions 行重复投影 | `src/opencode/session/projector.py:58` | 不导出 |
| `opencode.session.projector.REASON_PROMPT_WITHOUT_SESSION` | constant | `"prompt_accepted_without_session"` | prompt 事件缺少 sessions 行 | `src/opencode/session/projector.py:59` | 不导出 |
| `opencode.session.projector.REASON_ILLEGAL_TRANSITION` | constant | `"illegal_projection_transition"` | fold 抛出的非法转移/不可投影类型 | `src/opencode/session/projector.py:60` | 不导出 |
| `opencode.session.projector.REASON_UNKNOWN_SESSION_STATUS` | constant | `"unknown_session_status"` | 存储的 status 非法 | `src/opencode/session/projector.py:61` | 不导出 |
| `opencode.session.projector.REASON_DUPLICATE_MESSAGE_ID` | constant | `"duplicate_message_id"` | 同 session 内 message_id 重复 | `src/opencode/session/projector.py:62` | 不导出 |
| `opencode.session.projector.SessionProjector` | class | `async apply(connection: AsyncConnection, event: DurableEvent[Any]) -> None`（无状态；内部 `_project_created`/`_project_prompt`/`_advance_applied_seq` 为私有） | §3.2 真实读模型投影器：sessions/messages 落库并在事务内推进 `applied_seq` | `src/opencode/session/projector.py:94` | 导出 |

**S4 对 `storage/txn.py` 的核对（N-1/N-3/M-1/Nit-2）**：逐条比对 `git diff 72de622 077caf6`（N-1/N-3）与 `git diff 077caf6 698b456`（M-1/Nit-2）——**均未新增模块级公共符号**，结论如下：

| 项 | 改动实质 | 是否新增公共符号 |
|---|---|---|
| N-1 | 隔离持久化失败时由 `pass` 改为 `raise corruption from persist_error`（显式异常链） | 否（`commit` 内部行为） |
| N-3 | `epoch_ms` docstring 补充毫秒截断语义 | 否（仅注释；符号已在 §1.2 收录） |
| M-1 | 幂等比较循环内按行解码 JSON，`ValueError` → `StorageCorruptError`（`REASON_STORED_DECODE_FAILED`） | 否（`_idempotent_replay` 内部，私有） |
| Nit-2 | 重放解码改为逐行，诊断定位坏行并携带 `applied` | 否（`_idempotent_replay` 内部，私有） |

附带核对：`txn.py` 公共名集合与 S3 完全一致（8 个 `REASON_*`、`epoch_ms`/`from_epoch_ms`、9 个错误类、`Diagnostic`、`TransactionWriter`；行号因改动顺移）；`src/opencode/storage/__init__.py` 的 `__all__` 仍为 **21** 项，无变化。

## 2. 错误码三方对照表

契约来源：`doc/CORE-CONTRACTS.md` §8，全部 22 个码位于**同一行 L340**（逐个直读，非记忆）。实现：`CoreErrorCode` 成员，`src/opencode/contracts/errors.py:24-45`。
**测试引用数注**：统计 `tests/` 下全部 `*.py`（含 S3 `tests/integration/test_store_commit.py` 与 S4 `tests/integration/test_projector.py`），口径 = 该码**引号字符串字面量**出现次数（`Select-String -CaseSensitive -Pattern '"<Code>"' -AllMatches`，按 `Matches` 计数） + **枚举成员名**出现次数（`-CaseSensitive -Pattern '\b<MEMBER_NAME>\b' -AllMatches`）；两者互斥，合计即表列数字。错误类断言（如 `pytest.raises(StorageCorruptError)`）不计入本列，仅计码字面量/枚举成员。

| 码名 | 契约行 | 实现位置 | 测试引用数 | 一致性结论 |
|---|---|---|---|---|
| `ValidationError` | L340 | `errors.py:24`（`VALIDATION_ERROR`） | 6（4 + 2） | 一致 |
| `UnsupportedCapability` | L340 | `errors.py:25`（`UNSUPPORTED_CAPABILITY`） | 1 | 一致 |
| `NotFound` | L340 | `errors.py:26`（`NOT_FOUND`） | 4（2 + 2） | 一致 |
| `SessionBusy` | L340 | `errors.py:27`（`SESSION_BUSY`） | 2 | 一致 |
| `Conflict` | L340 | `errors.py:28`（`CONFLICT`） | 4（1 + 3） | 一致 |
| `IdempotencyConflict` | L340 | `errors.py:29`（`IDEMPOTENCY_CONFLICT`） | 4（1 + 3） | 一致 |
| `InvalidCursor` | L340 | `errors.py:30`（`INVALID_CURSOR`） | 2 | 一致 |
| `PermissionDenied` | L340 | `errors.py:31`（`PERMISSION_DENIED`） | 1 | 一致 |
| `ApprovalUnavailable` | L340 | `errors.py:32`（`APPROVAL_UNAVAILABLE`） | 1 | 一致 |
| `ApprovalExpired` | L340 | `errors.py:33`（`APPROVAL_EXPIRED`） | 1 | 一致 |
| `ToolTimeout` | L340 | `errors.py:34`（`TOOL_TIMEOUT`） | 1 | 一致 |
| `ToolFailed` | L340 | `errors.py:35`（`TOOL_FAILED`） | 1 | 一致 |
| `ExecutionUnknown` | L340 | `errors.py:36`（`EXECUTION_UNKNOWN`） | 1 | 一致 |
| `ProviderUnavailable` | L340 | `errors.py:37`（`PROVIDER_UNAVAILABLE`） | 5（R0 spike 断言） | 一致 |
| `ProviderStreamInterrupted` | L340 | `errors.py:38`（`PROVIDER_STREAM_INTERRUPTED`） | 3（R0 spike 断言） | 一致 |
| `ContextLimitExceeded` | L340 | `errors.py:39`（`CONTEXT_LIMIT_EXCEEDED`） | 1 | 一致 |
| `SlowConsumer` | L340 | `errors.py:40`（`SLOW_CONSUMER`） | 2 | 一致 |
| `StorageCorrupt` | L340 | `errors.py:41`（`STORAGE_CORRUPT`） | 14（1 + 13） | 一致 |
| `MigrationRequired` | L340 | `errors.py:42`（`MIGRATION_REQUIRED`） | 2 | 一致 |
| `SessionReadOnly` | L340 | `errors.py:43`（`SESSION_READ_ONLY`） | 3（1 + 2） | 一致 |
| `SessionClosed` | L340 | `errors.py:44`（`SESSION_CLOSED`） | 1 | 一致 |
| `LifecycleCleanupFailed` | L340 | `errors.py:45`（`LIFECYCLE_CLEANUP_FAILED`） | 1 | 一致 |

汇总：契约码数 **22** / 实现码数 **22** / 表行数 **22**。顺序、拼写、数量三方完全一致，无差异。测试引用合计 **61**（首派 45 → S3 53 → 本次 61，+8，全部来自 S4 新增 `test_projector.py` 与 `test_store_commit.py` 直测的 `STORAGE_CORRUPT` 枚举引用）。

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

### 观测到的非本任务差异（仅报告，未处理）

- 门禁 `ruff format --check .` 本次输出 **88 files already formatted**、退出码 0（基线口径为退出码，计数不作口径）；S3 滚动记录为 77，变化来自扫描集合（工作树新增 S4 文件），无格式问题。
- `git status --short` 本次滚动前后均含他席位/调度器改动：仅 `?? doc/orchestration/logs/`（S3 滚动时的 ` M doc/plans/r1-slices.md` 已随 merge #30 落库；S4 已随 merge #41 落库，无新增源码改动）。本任务只修改本文件，未触碰该路径。
