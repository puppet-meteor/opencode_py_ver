# R1-S1 / R1-S2 已交付公共面静态清单

> 范围：`src/opencode/contracts/`、`src/opencode/storage/` 的**源码定义处**公共符号（不含 `import` 绑定、不含包 `__init__` 的再导出）。全部数据由源码直读/正则扫描得出，未做任何修改。
> 符号总数：**45**（contracts 27 + storage 18）。包级再导出：`contracts.__all__` 27 项（与本包定义一一对应）、`storage.__all__` 9 项（全部来自 `db.py`，不含迁移脚本符号）。
> 扫描时间：2026-11-02；基线提交工作树（`git status` 中存在他席位产生的改动，见 §3 注）。

## 1. 公共面清单

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

## 2. 错误码三方对照表

契约来源：`doc/CORE-CONTRACTS.md` §8，全部 22 个码位于**同一行 L340**（逐个直读，非记忆）。实现：`CoreErrorCode` 成员，`src/opencode/contracts/errors.py:24-45`。
**测试引用数注**：统计 `tests/` 下全部 `*.py`，口径 = 该码**引号字符串字面量**出现次数（`Select-String -CaseSensitive -Pattern '"<Code>"' -AllMatches`，按 `Matches` 计数） + **枚举成员名**出现次数（`-CaseSensitive -Pattern '\b<MEMBER_NAME>\b' -AllMatches`）；两者互斥，合计即表列数字。

| 码名 | 契约行 | 实现位置 | 测试引用数 | 一致性结论 |
|---|---|---|---|---|
| `ValidationError` | L340 | `errors.py:24`（`VALIDATION_ERROR`） | 5（1 字符串 + 4 成员引用） | 一致 |
| `UnsupportedCapability` | L340 | `errors.py:25`（`UNSUPPORTED_CAPABILITY`） | 1 | 一致 |
| `NotFound` | L340 | `errors.py:26`（`NOT_FOUND`） | 4（2 + 2） | 一致 |
| `SessionBusy` | L340 | `errors.py:27`（`SESSION_BUSY`） | 2 | 一致 |
| `Conflict` | L340 | `errors.py:28`（`CONFLICT`） | 3（2 + 1） | 一致 |
| `IdempotencyConflict` | L340 | `errors.py:29`（`IDEMPOTENCY_CONFLICT`） | 2 | 一致 |
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
| `StorageCorrupt` | L340 | `errors.py:41`（`STORAGE_CORRUPT`） | 3（1 + 2） | 一致 |
| `MigrationRequired` | L340 | `errors.py:42`（`MIGRATION_REQUIRED`） | 2 | 一致 |
| `SessionReadOnly` | L340 | `errors.py:43`（`SESSION_READ_ONLY`） | 2 | 一致 |
| `SessionClosed` | L340 | `errors.py:44`（`SESSION_CLOSED`） | 1 | 一致 |
| `LifecycleCleanupFailed` | L340 | `errors.py:45`（`LIFECYCLE_CLEANUP_FAILED`） | 1 | 一致 |

汇总：契约码数 **22** / 实现码数 **22** / 表行数 **22**。顺序、拼写、数量三方完全一致，无差异。

## 3. 盘点方法注记（可复现）

1. 目录枚举：`ctx_tree src/opencode/contracts`、`ctx_tree src/opencode/storage`（depth=3）。
2. 全文直读：`ctx_read <file> mode=full` 读取 `contracts/{__init__,errors,events,messages}.py` 与 `storage/{__init__,db}.py`、`storage/migrations/env.py`、`storage/migrations/versions/0001_initial.py`。
3. 定义行号：`Get-ChildItem -Recurse src/opencode/contracts,src/opencode/storage -Filter *.py | ForEach-Object { Select-String -Path $_.FullName -Pattern '^(class |def |type |[A-Za-z_][A-Za-z0-9_]*\s*(:|=))' }`（下划线前缀私有名已剔除）。
4. 契约定位：`Select-String -Path doc/CORE-CONTRACTS.md -Pattern '^## '` 得章节行号；`ctx_read doc/CORE-CONTRACTS.md lines:335-360` 直读 §8，码清单在 L340。
5. 实现码行号：`Select-String -Path src/opencode/contracts/errors.py -Pattern '^    [A-Z_]+ = '`。
6. 测试引用计数（区分大小写，避免 `CONFLICT` 命中 `IdempotencyConflict`）：`Get-ChildItem -Recurse tests -Filter *.py | Select-String -CaseSensitive -Pattern '"<Code>"' -AllMatches` 与 `-Pattern '\b<MEMBER>\b' -AllMatches`，对 `Matches` 求和。
7. 门禁：`uv run pytest -q`；`uv run ruff check .`；`uv run ruff format --check .`；`uv run mypy src tools/r0 tests/r0 tests/contract tests/integration`。
8. 变更核对：`git status --short`、`git log --oneline -3`（只读）。

### 观测到的非本任务差异（仅报告，未处理）

- 门禁 `ruff format --check .` 输出 **68 files already formatted**，与基线 62 相差 +6；无格式错误。限定范围复核 `uv run ruff format --check src tests tools` 为 **24 files already formatted**，与仓库内 `.py` 实际数量（24，排除 `.venv`）吻合；差异来自环境/扫描集合，不是本次改动。
- `git status --short` 在盘点开始前后均含他席位改动：` M doc/plans/r1-slices.md`、`?? doc/orchestration/logs/`。本任务只新增本文件，未触碰上述路径。
