# R5-S4 契约证据清单：统一 JSON Schema 工件与六接缝 fixture 对账（T-C1-045）

- 席位：R5-S4（契约证据面）；任务书 `doc/orchestration/tasks/T-C1-045.md`。
- 基线：`d889810270a00c7abca6e58a2aa913b7d457c9f0`（checkpoint #159）。
- 权威源：`doc/CORE-CONTRACTS.md` §1 L22（「统一 JSON Schema 在 R5 由实际模型的 `model_json_schema()` 汇总生成并锁定差异」）、§2–§7（六接缝定义）、§8（错误与演进）；`doc/plans/r5-slices.md` §2.4；任务书 Done L16 与验收C 前半。
- 范围声明：本清单只陈述 Python 侧可核事实（模型枚举、工件结构、fixture 分类、测试锚）。按 parity §3 L41 口径，全文不含 TS wire 同构/兼容宣称；冻结模型类 docstring 经 `model_json_schema()` 的 `description` 字段原样进入工件——**现行模型类 docstring 实测零 TS 注记、工件零 TS 文本**（TS 历史注记均在模块 docstring/注释/函数 docstring，结构上不进入工件；若未来引入则原样透传，为差异对照非等价宣称）——实测与透传语义见进度文件 ③ 注记 b（T-B-049 一般-2 处方 3 修正，#160）。
- 修订：T-C1-060（P2-S8 M6 压缩 slice one）落 `py.context.compacted`——模型 64→65、durable 键 22→23、fixture 92→95（26 正例 / 49 schema 级 / 20 行为级），§1–§8 计数与 events.py 行锚随版更新。
- 修订：T-C1-076（P3-S1 job 注册表首切片）落 `py.job.*` 七事件 + `JobCard`——模型 65→76（events 36→46 + 新 cards 模块 1）、durable 键 23→30、fixture 95→116（33 正例 / 63 schema 级 / 20 行为级；S4 49→70），§1–§8 计数与 events.py / CORE-CONTRACTS（§1/§6.3）行锚随版更新。
- 修订：T-C1-077（P3-S2 引擎最小环）落 pipeline spec 契约——模型 76→79（+新 pipelines 模块 3：`PipelineEdge` `PipelineNode` `PipelineSpec`）、fixture 116→119（34 正例 / 65 schema 级 / 20 行为级）、工件表增 `pipelines.json`（`models`×3，9189 字节）行，§1–§8 计数与 CORE-CONTRACTS（§5.4）行锚随版更新。
- 修订：T-C1-079（P3-S4 主脑+工作流工具）落 `PipelineSpec.params_schema` 可选字段——模型 79 不变（字段新增非模型新增，`pipelines.json` 重生成 9189→9592 字节）、fixture 119→121（35 正例 / 66 schema 级 / 20 行为级；pipeline-spec 桶 3→5：+`pipelines_params_valid` 正例 +`pipelines_params_bad_schema` schema 级），§1–§8 计数与 CORE-CONTRACTS（§5.4）行锚随版更新。

## 1. 统一 JSON Schema 工件

生成器 `tools/r5/generate_schema_artifacts.py`（导入七模块公开定义面——五模块 + cards，T-C1-076 + pipelines，T-C1-077；`build_artifacts()` 即漂移测试的编程 API）；守卫 `tests/contract/test_schema_lock.py`。

| 工件 | 顶层结构 | 覆盖 | 字节 |
|---|---|---|---|
| `doc/reference/schema/errors.json` | `models`×1 | `CoreError` | 2117 |
| `doc/reference/schema/messages.json` | `models`×8 | messages 模块全模型 | 29706 |
| `doc/reference/schema/llm.json` | `models`×17 | llm 模块全模型（含 `_ChunkBase`） | 42378 |
| `doc/reference/schema/tools.json` | `models`×3 | `ToolRef` / `ToolSpec` / `ToolResult` | 8504 |
| `doc/reference/schema/events.json` | `models`×46 | events 模块全模型（含 3 泛型信封；+`ContextCompactedData` T-C1-060；+job 域 ×10：`JobCreatedData` 等 7 data + `JobBudget` `JobLineage` `JobTrace`——T-C1-076） | 134505 |
| `doc/reference/schema/event-envelopes.json` | `durable`×30 + `ephemeral`×4 + `generic`×3 | 注册表参数化信封（+`py.context.compacted.1` T-C1-060；+`py.job.*` durable 键 ×7——T-C1-076） | 161980 |
| `doc/reference/schema/cards.json` | `models`×1 | `JobCard`（jobs 卡片渲染 wire 契约）——T-C1-076 | 3469 |
| `doc/reference/schema/pipelines.json` | `models`×3 | pipelines 模块全模型（`PipelineEdge` / `PipelineNode` / `PipelineSpec`——pipeline spec wire 契约，CORE-CONTRACTS §5.4 S2；`PipelineSpec` 增可选 `params_schema` 字段，T-C1-079） | 9592 |

- **粒度**：模块边界 = 机械稳定枚举单元（`vars(module)` + `issubclass(BaseModel)` + `__module__` 归属过滤，重导出零重复）；信封单独按注册表定义参数化；六接缝是语义维度，不在工件内分文件，由本清单显式承载（任务书 1a）。
- **durable 键** = `versioned_type` 形如 `py.session.created.1`，共 30 键（R1 2：`py.session.created.1`、`py.prompt.accepted.1`；R2 durable 11；R3 permission 6；session_created v2/v3 各 1；skills 1：`py.skill.activated.1`——T-C1-052a 增；compaction 1：`py.context.compacted.1`——T-C1-060 增；job 7：`py.job.created.1` /`py.job.spec_bound.1` /`py.job.started.1` /`py.job.stalled.1` /`py.job.gated.1` /`py.job.redispatched.1` /`py.job.finished.1`——T-C1-076 增）；**ephemeral 键** = `py.<type>`，4 键（`py.text.delta` 等）；**generic** = `EventDraft` / `DurableEvent` / `EphemeralEvent` 未参数化泛型，其 `data` 面为任意对象——即 unknown_type / unknown_version 探针的 schema 靶。
- **序列化**：`json.dumps(indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)` + 尾换行、`newline="\n"`（dump_fixtures.py 先例 + 键排序消除 dict 插入序依赖）。字节确定性双证：进程内 `build_artifacts()` 连调相等（测试钉）+ 跨进程重跑 8 文件 SHA256 全等（T-C1-045 会话内两轮实测立 6 文件；T-C1-076 增 cards.json、T-C1-077 增 pipelines.json 同配方）。

## 2. 六接缝 × 证据矩阵（契约模型 / fixture / 行为锚 / 适配器）

| 接缝（CORE-CONTRACTS §） | 契约模型 | fixture | 行为测试锚 | 适配器（真实/fake） |
|---|---|---|---|---|
| S1 §2 历史、模型输入和流式输出 | messages 8 + llm 17 + events 1（`ContextCompactedData`，§2.4，T-C1-060）= 26 | 3（context_compacted 三元组，T-C1-060；见注） | `tests/contract/test_messages.py`、`test_llm.py`、`test_ai_adapter.py`、`test_compaction_engine.py` | `tests/r0/fake_openai_server.py`（fake OpenAI 兼容 HTTP 服务） |
| S2 §4 live 与 durable feed | events 4（ephemeral data） | 16 | `tests/contract/test_events.py`（R2 正/负例与 run 元数据组） | `tests/integration/test_eventlog.py`、`test_bus_commit.py`（真实 SQLite 事件日志/总线提交）+ `tests/contract/test_bus.py` |
| S3 §5 工具、权限与生命周期 | tools 3 + events permission 6 = 9 | 18 | `tests/contract/test_permission_events.py`、`test_tools.py`、`test_tool_listing.py` | `tests/integration/test_tool_bash.py`、`test_tool_read.py`、`test_tool_static_trio.py`、`test_tool_write_edit.py`（真实 fs/bash 工具）；`tests/integration/mcp_stdio_server.py`（fake stdio MCP 服务端） |
| S4 §3（S6+S4）唯一事件写入、投影与数据库 | events 25（3 信封 + 12 durable data + job 域 10：`JobCreatedData` 等 7 data + `JobBudget` `JobLineage` `JobTrace`——T-C1-076）+ cards 1（`JobCard`——T-C1-076）+ pipelines 3（`PipelineEdge`/`PipelineNode`/`PipelineSpec`——T-C1-077） | 75 | `tests/contract/test_events.py`（R1/R2 正负例、注册表探针组）、`test_job_events.py`（py.job.* 正负例与 trace 四字段——T-C1-076）、`test_pipelines.py`（pipeline spec 正反例与三层门拒绝——T-C1-077） | `tests/integration/test_store_commit.py`、`test_projector.py`、`test_rebuild.py`、`test_recovery.py`、`test_migrations.py`（真实 SQLite + alembic）、`test_jobs_store.py`、`test_jobs_api.py`、`test_jobs_recovery.py`（jobs 写入/读模型/重启恢复矩阵——T-C1-076）、`test_engine.py`（真实 LangGraph 引擎最小环：顺序执行/断点续跑/双库边界/恢复矩阵——T-C1-077）、`test_workflow_tools.py`（工作流工具对+params 网关校验器+M-9 递归守卫+注册隔离——T-C1-079）、`test_brain_e2e.py`（主脑默认路径全链 e2e——T-C1-079） |
| S5 §6 配置、指令和 MCP 信任 | events 10（snapshot / session_created 家族 + skills 激活记录，T-C1-052a） | 9 | `tests/contract/test_config.py`（TestSessionCreatedV2Wire）、`test_mcp_naming.py`（TestSessionCreatedV3Wire） | `tests/integration/test_config_loader.py`（真实 fs 配置加载） |
| S6 §7 应用命令接口和任务所有权 | 0（运行时资源，非 wire 模型） | 0（见注） | `tests/integration/test_service.py`、`test_service_run.py`、`test_cli.py`、`test_cli_r3.py`、`test_cli_r4.py`、`test_e2e*.py` | 真实 service/CLI 进程面（无 fake） |

注（S6 零 fixture 属预期，非缺口；S1 自 T-C1-060 起非零）：121 个落盘 fixture 中 116 个是事件 wire 面（`events_*.json`）、5 个是 pipeline spec wire 面（`pipelines_*.json`——锁 CORE-CONTRACTS §5.4 S2 spec 形状与 S4 params_schema 字段面，T-C1-077/T-C1-079），接缝归属按语义定而非文件前缀——S1 承载 §2.4 压缩事件三元组（正例 / schema 级 / 行为级各 1，T-C1-060）；S4 承载 py.job.* 七类三元组（正例 / schema 级 ×2 各 7，T-C1-076）与 pipeline spec 三元组（正例 / schema 级 ×2 各 1，T-C1-077）及 params 声明对（正例 / schema 级各 1，T-C1-079）。S1 的消息/生成 wire 形状仍由模型 + 单测内联构造锁定（R1 起即此口径，CORE-CONTRACTS §1 L22），其统一 schema 面由本任务工件补齐；S6 命令面是运行时行为（无 wire 模型），由集成测试承载。两接缝的模型/行为锚/适配器证据列均非空。

## 3. 模型→接缝映射（79 = errors 1 + messages 8 + events 46 + llm 17 + tools 3 + cards 1 + pipelines 3）

| 模块 | 接缝 | 模型 |
|---|---|---|
| `contracts/errors.py` | 跨接缝（§8 错误面） | `CoreError` |
| `contracts/messages.py` | S1 | `AssistantMessage` `AssistantReasoningPart` `AssistantTextPart` `AssistantToolPart` `IdleMessage` `ToolFileContent` `ToolTextContent` `UserMessage` |
| `contracts/llm.py` | S1 | `ErrorChunk` `FinishChunk` `GenerationRequest` `LLMMessage` `LLMReasoningPart` `LLMTextPart` `LLMToolCallPart` `LLMToolResultPart` `ModelRef` `ReasoningDeltaChunk` `TextDeltaChunk` `ToolCallArgsDeltaChunk` `ToolCallEndChunk` `ToolCallStartChunk` `Usage` `UsageChunk` `_ChunkBase` |
| `contracts/events.py` | S1 | `ContextCompactedData`（§2.4 压缩事件 data，T-C1-060） |
| `contracts/events.py` | S2 | `ReasoningDeltaData` `TextDeltaData` `ToolInputDeltaData` `ToolProgressData` |
| `contracts/events.py` | S3 | `PermissionClosedData` `PermissionExpiredData` `PermissionRepliedData` `PermissionRequestedData` `PermissionRevokedData` `PermissionSavedData` |
| `contracts/tools.py` | S3 | `ToolRef` `ToolResult` `ToolSpec` |
| `contracts/events.py` | S4 | 信封：`EventDraft` `DurableEvent` `EphemeralEvent`；durable data：`PromptAcceptedData` `ReasoningFinalizedData` `RunCancelRequestedData` `RunFinishedData` `RunStartedData` `StepFinishedData` `StepGeneratedData` `StepStartedData` `TextFinalizedData` `ToolFinishedData` `ToolRequestedData` `ToolStartedData` |
| `contracts/events.py` | S4 | job 域（T-C1-076）：`JobBudget` `JobLineage` `JobTrace` + data `JobCreatedData` `JobSpecBoundData` `JobStartedData` `JobStalledData` `JobGatedData` `JobRedispatchedData` `JobFinishedData` |
| `contracts/cards.py` | S4 | `JobCard`（T-C1-076） |
| `contracts/pipelines.py` | S4 | `PipelineEdge` `PipelineNode` `PipelineSpec`（pipeline spec wire 契约，T-C1-077） |
| `contracts/events.py` | S5 | `ConfigSnapshot` `ConfigSnapshotV2` `ConfigSnapshotV3` `InstructionSource` `McpServerRef` `SessionCreatedData` `SessionCreatedDataV2` `SessionCreatedDataV3` `SkillActivatedData` `SnapshotLimits` |

计数：S1 26 + S2 4 + S3 9 + S4 29（15 + job 10 + cards 1 + pipelines 3） + S5 10 + S6 0 + 跨接缝 1 = 79。

## 4. fixture 枚举（119；接缝分布 S1:3 / S2:16 / S3:18 / S4:73 / S5:9）

| # | 文件 | 接缝 | 类别 | 行为锚 |
|---|---|---|---|---|
| 1 | `events_context_compacted_bad_run_id.json` | S1 | 反例·行为级（_require_run_metadata 校验器 whenever-present 规则，T-C1-060） | test_schema_lock.py::TestBehaviorLevelLockDiff::test_behavior_level_negative_is_schema_legal_but_semantically_rejected |
| 2 | `events_context_compacted_missing_field.json` | S1 | 反例·schema 级（required，T-C1-060） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 3 | `events_context_compacted_valid.json` | S1 | 正例（T-C1-060） | test_schema_lock.py::TestPositiveLockDiff::test_positive_fixture_passes_schema_and_decodes |
| 4 | `events_permission_closed_bad_extension.json` | S3 | 反例·schema 级（additionalProperties:false） | test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected |
| 5 | `events_permission_closed_missing_field.json` | S3 | 反例·schema 级（required） | test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected |
| 6 | `events_permission_closed_valid.json` | S3 | 正例 | test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless |
| 7 | `events_permission_expired_bad_extension.json` | S3 | 反例·schema 级（additionalProperties:false） | test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected |
| 8 | `events_permission_expired_missing_field.json` | S3 | 反例·schema 级（required） | test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected |
| 9 | `events_permission_expired_valid.json` | S3 | 正例 | test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless |
| 10 | `events_permission_replied_bad_extension.json` | S3 | 反例·schema 级（additionalProperties:false） | test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected |
| 11 | `events_permission_replied_missing_field.json` | S3 | 反例·schema 级（required） | test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected |
| 12 | `events_permission_replied_valid.json` | S3 | 正例 | test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless |
| 13 | `events_permission_requested_bad_extension.json` | S3 | 反例·schema 级（additionalProperties:false） | test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected |
| 14 | `events_permission_requested_missing_field.json` | S3 | 反例·schema 级（required） | test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected |
| 15 | `events_permission_requested_valid.json` | S3 | 正例 | test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless |
| 16 | `events_permission_revoked_bad_extension.json` | S3 | 反例·schema 级（additionalProperties:false） | test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected |
| 17 | `events_permission_revoked_missing_field.json` | S3 | 反例·schema 级（required） | test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected |
| 18 | `events_permission_revoked_valid.json` | S3 | 正例 | test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless |
| 19 | `events_permission_saved_bad_extension.json` | S3 | 反例·schema 级（additionalProperties:false） | test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected |
| 20 | `events_permission_saved_missing_field.json` | S3 | 反例·schema 级（required） | test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected |
| 21 | `events_permission_saved_valid.json` | S3 | 正例 | test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless |
| 22 | `events_prompt_accepted_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestNegativeFixtures::test_invalid_extension_value_rejected |
| 23 | `events_prompt_accepted_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestNegativeFixtures::test_missing_required_field_rejected |
| 24 | `events_prompt_accepted_valid.json` | S4 | 正例 | test_events.py::TestPositiveFixtures::test_round_trip_is_lossless |
| 25 | `events_reasoning_delta_bad_extension.json` | S2 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 26 | `events_reasoning_delta_missing_field.json` | S2 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 27 | `events_reasoning_delta_missing_run_id.json` | S2 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 28 | `events_reasoning_delta_valid.json` | S2 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 29 | `events_reasoning_finalized_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 30 | `events_reasoning_finalized_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 31 | `events_reasoning_finalized_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 32 | `events_reasoning_finalized_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 33 | `events_run_cancel_requested_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 34 | `events_run_cancel_requested_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 35 | `events_run_cancel_requested_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 36 | `events_run_cancel_requested_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 37 | `events_run_finished_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 38 | `events_run_finished_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 39 | `events_run_finished_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 40 | `events_run_finished_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 41 | `events_run_started_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 42 | `events_run_started_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 43 | `events_run_started_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 44 | `events_run_started_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 45 | `events_session_created_bad_extension.json` | S5 | 反例·schema 级（additionalProperties:false） | test_events.py::TestNegativeFixtures::test_invalid_extension_value_rejected |
| 46 | `events_session_created_missing_field.json` | S5 | 反例·schema 级（required） | test_events.py::TestNegativeFixtures::test_missing_required_field_rejected |
| 47 | `events_session_created_v2_bad_extension.json` | S5 | 反例·schema 级（additionalProperties:false） | test_config.py::TestSessionCreatedV2Wire::test_bad_extension_fixture_is_rejected |
| 48 | `events_session_created_v2_bad_hash.json` | S5 | 反例·行为级（_snapshot_hash_verified 校验器） | test_config.py::TestSessionCreatedV2Wire::test_bad_hash_fixture_is_rejected |
| 49 | `events_session_created_v2_valid.json` | S5 | 正例 | test_config.py::TestSessionCreatedV2Wire::test_valid_fixture_decodes_and_round_trips |
| 50 | `events_session_created_v3_bad_extension.json` | S5 | 反例·schema 级（additionalProperties:false） | test_mcp_naming.py::TestSessionCreatedV3Wire::test_bad_extension_fixture_is_rejected |
| 51 | `events_session_created_v3_bad_hash.json` | S5 | 反例·行为级（_snapshot_hash_verified 校验器） | test_mcp_naming.py::TestSessionCreatedV3Wire::test_bad_hash_fixture_is_rejected |
| 52 | `events_session_created_v3_valid.json` | S5 | 正例 | test_mcp_naming.py::TestSessionCreatedV3Wire::test_valid_fixture_decodes_and_round_trips |
| 53 | `events_session_created_valid.json` | S5 | 正例 | test_events.py::TestPositiveFixtures::test_round_trip_is_lossless |
| 54 | `events_step_finished_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 55 | `events_step_finished_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 56 | `events_step_finished_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 57 | `events_step_finished_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 58 | `events_step_generated_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 59 | `events_step_generated_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 60 | `events_step_generated_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 61 | `events_step_generated_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 62 | `events_step_started_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 63 | `events_step_started_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 64 | `events_step_started_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 65 | `events_step_started_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 66 | `events_text_delta_bad_extension.json` | S2 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 67 | `events_text_delta_missing_field.json` | S2 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 68 | `events_text_delta_missing_run_id.json` | S2 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 69 | `events_text_delta_valid.json` | S2 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 70 | `events_text_finalized_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 71 | `events_text_finalized_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 72 | `events_text_finalized_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 73 | `events_text_finalized_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 74 | `events_tool_finished_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 75 | `events_tool_finished_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 76 | `events_tool_finished_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 77 | `events_tool_finished_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 78 | `events_tool_input_delta_bad_extension.json` | S2 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 79 | `events_tool_input_delta_missing_field.json` | S2 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 80 | `events_tool_input_delta_missing_run_id.json` | S2 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 81 | `events_tool_input_delta_valid.json` | S2 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 82 | `events_tool_progress_bad_extension.json` | S2 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 83 | `events_tool_progress_missing_field.json` | S2 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 84 | `events_tool_progress_missing_run_id.json` | S2 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 85 | `events_tool_progress_valid.json` | S2 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 86 | `events_tool_requested_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 87 | `events_tool_requested_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 88 | `events_tool_requested_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 89 | `events_tool_requested_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 90 | `events_tool_started_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 91 | `events_tool_started_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 92 | `events_tool_started_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 93 | `events_tool_started_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 94 | `events_unknown_type.json` | S4 | 反例·行为级（注册表 UnknownEventTypeError） | test_events.py::TestNegativeFixtures::test_unknown_type_rejected |
| 95 | `events_unknown_version.json` | S4 | 反例·行为级（注册表 UnknownEventVersionError） | test_events.py::TestNegativeFixtures::test_unknown_version_rejected |
| 96 | `events_job_created_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 97 | `events_job_created_missing_field.json` | S4 | 反例·schema 级（required，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 98 | `events_job_created_valid.json` | S4 | 正例（T-C1-076） | test_schema_lock.py::TestPositiveLockDiff::test_positive_fixture_passes_schema_and_decodes |
| 99 | `events_job_finished_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 100 | `events_job_finished_missing_field.json` | S4 | 反例·schema 级（required，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 101 | `events_job_finished_valid.json` | S4 | 正例（T-C1-076） | test_schema_lock.py::TestPositiveLockDiff::test_positive_fixture_passes_schema_and_decodes |
| 102 | `events_job_gated_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 103 | `events_job_gated_missing_field.json` | S4 | 反例·schema 级（required，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 104 | `events_job_gated_valid.json` | S4 | 正例（T-C1-076） | test_schema_lock.py::TestPositiveLockDiff::test_positive_fixture_passes_schema_and_decodes |
| 105 | `events_job_redispatched_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 106 | `events_job_redispatched_missing_field.json` | S4 | 反例·schema 级（required，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 107 | `events_job_redispatched_valid.json` | S4 | 正例（T-C1-076） | test_schema_lock.py::TestPositiveLockDiff::test_positive_fixture_passes_schema_and_decodes |
| 108 | `events_job_spec_bound_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 109 | `events_job_spec_bound_missing_field.json` | S4 | 反例·schema 级（required，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 110 | `events_job_spec_bound_valid.json` | S4 | 正例（T-C1-076） | test_schema_lock.py::TestPositiveLockDiff::test_positive_fixture_passes_schema_and_decodes |
| 111 | `events_job_stalled_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 112 | `events_job_stalled_missing_field.json` | S4 | 反例·schema 级（required，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 113 | `events_job_stalled_valid.json` | S4 | 正例（T-C1-076） | test_schema_lock.py::TestPositiveLockDiff::test_positive_fixture_passes_schema_and_decodes |
| 114 | `events_job_started_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 115 | `events_job_started_missing_field.json` | S4 | 反例·schema 级（required，T-C1-076） | test_schema_lock.py::TestSchemaLevelLockDiff::test_schema_level_negative_fails_schema_and_decode |
| 116 | `events_job_started_valid.json` | S4 | 正例（T-C1-076） | test_schema_lock.py::TestPositiveLockDiff::test_positive_fixture_passes_schema_and_decodes |
| 117 | `pipelines_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false，T-C1-077） | test_schema_lock.py::TestPipelineSpecLockDiff::test_pipeline_spec_fixture_locks_the_wire_shape |
| 118 | `pipelines_missing_field.json` | S4 | 反例·schema 级（required，T-C1-077） | test_schema_lock.py::TestPipelineSpecLockDiff::test_pipeline_spec_fixture_locks_the_wire_shape |
| 119 | `pipelines_valid.json` | S4 | 正例（T-C1-077） | test_pipelines.py::TestStructuralLayer::test_valid_fixture_loads_and_passes_the_semantic_gate + test_schema_lock.py::TestPipelineSpecLockDiff::test_pipeline_spec_fixture_locks_the_wire_shape |
| 120 | `pipelines_params_valid.json` | S4 | 正例（声明 `params_schema` 的 spec——S4 网关校验器字段面，T-C1-079） | test_schema_lock.py::TestPipelineSpecLockDiff::test_pipeline_spec_fixture_locks_the_wire_shape |
| 121 | `pipelines_params_bad_schema.json` | S4 | 反例·schema 级（`params_schema` 非对象——wire 模型拒绝，T-C1-079） | test_schema_lock.py::TestPipelineSpecLockDiff::test_pipeline_spec_fixture_locks_the_wire_shape |

## 5. 反例分类与拒绝语义

| 类别 | 数量 | 拒绝面（锚） |
|---|---|---|
| 正例 | 35 | 注册表 34 条定义（R1 2 + R2 15 + R3 permission 6 + R3 config v2 1 + R4 config v3 1 + skills 1——T-C1-052a + compaction 1——T-C1-060 + job 7——T-C1-076）；正例与其中 33 条一一对应（compaction / job 缺口即时闭合——T-C1-060 / T-C1-076）；+2 pipeline spec 正例（T-C1-077/T-C1-079）非注册表事件面——锁 CORE-CONTRACTS §5.4 S2/S4 spec wire 形状与 params_schema 字段面，不入 34 条定义对应账；skills 定义 fixture 归 P2-S2 实现切片（§6.3 L366），为在册待闭合缺口；events.py L1033/L1041/L1065/L1082/L1094/L1106/L1119 + JOB L1348，`default_event_registry` L1359 |
| 反例·schema 级 | 66 | bad_extension 33（envelope/typed data `extra="forbid"`，CORE-CONTRACTS §1 L20；+7 job，T-C1-076；+1 pipeline，T-C1-077）+ missing_field 32（`required` 缺失拒绝；+1 context_compacted，T-C1-060；+7 job，T-C1-076；+1 pipeline，T-C1-077）+ bad_schema 1（`params_schema` 非对象——`PipelineSpec` wire 模型拒绝，T-C1-079） |
| 反例·行为级 | 20 | missing_run_id 15（`_require_run_metadata`，events.py L826）+ bad_run_id 1（同校验器 whenever-present 规则，T-C1-060：run-scoped 之外类型带非字符串 `metadata.run_id` 也拒）+ bad_hash 2（`_snapshot_hash_verified`，events.py L349）+ 注册表探针 2（`UnknownEventTypeError` L902 / `UnknownEventVersionError` L906；raise 于 L1001/L1002/L1011） |

注（口径，T-B-092 MINOR-1）：本表与 §8 为**折叠口径**（35/66/20——pipeline spec 全部按语义折入正例/schema 级）；消费面测试 `PINNED_COUNTS`（test_schema_lock.py）为**四桶口径**（events 33/63/20 + pipeline 5 = 121）。两口径总数一致，逐桶差恰为 pipeline 五件——对账勿以 34-vs-33 误读为漂移。

## 6. UNSUPPORTED_CAPABILITY 三面对账

- 定义面：`contracts/errors.py` L25——`CoreErrorCode` 枚举含 `UnsupportedCapability`。
- schema 面：`errors.json` `models.CoreError.$defs.CoreErrorCode.enum` 含 `"UnsupportedCapability"`（`test_schema_lock.py` 钉）。
- 行为面（五集成锚，与调度器预核一致）：
  1. `tests/integration/test_tool_read.py::test_read_directory_is_unsupported`（L229）
  2. `tests/integration/test_tool_static_trio.py::test_ls_file_path_is_unsupported`（L248）
  3. `tests/integration/test_tool_static_trio.py::test_glob_search_path_not_a_directory`（L414）
  4. `tests/integration/test_tool_write_edit.py::test_write_directory_target_is_unsupported`（L500）
  5. `tests/integration/test_tool_write_edit.py::test_edit_directory_target_is_unsupported`（L755）

## 7. schema 面未覆盖角（行为级规则，工件不表达、由测试锚守卫）

| 规则 | 锚 | 为何 schema 面不覆盖 |
|---|---|---|
| run_id 元数据规则 | `_require_run_metadata`（events.py L826） | metadata 是 JsonObject 自由键，键存在性约束非 JSON Schema 可表达 |
| 快照摘要 | `compute_snapshot_hash` / `_snapshot_hash_verified`（events.py L146/L349） | 内容哈希是计算性校验 |
| 注册表成员资格 | `UnknownEventTypeError` / `UnknownEventVersionError`（L902/L906） | 类型/版本全集是注册表运行时状态，泛型信封 `data` 面为任意对象 |
| durability 路由 | `EventDefinition.durability`（注册表属性） | durable/ephemeral 分派不是信封 schema 字段 |
| ≥1 可读文本 | `_has_model_readable_text`（events.py L597） | 跨字段计数约束 |
| env_refs 规范形 | `McpServerRef.env_refs` field_validator（events.py L286/L288） | 规范化语义（计算性），schema 只钉容器形状 |
| naive datetime 拒绝 | 模型层 `_as_utc` 等行为 | schema `format` 按 draft 语义仅为注记不判定 |
| bool→int 拒绝层 | CORE-CONTRACTS §1 L21 仓库级 before-validator | lax 转换守卫是 Python/Pydantic 特有行为，非 schema 面能力 |

## 8. 对账结论（零差异）

1. **76 vs 65**：前版机械枚举 65 = grep 口径 55（53 R5-S4 时点 + T-C1-052a `SkillActivatedData` + T-C1-060 `ContextCompactedData`，class 行含 `BaseModel` 字样）+ 10 条子类声明行（`SessionCreatedDataV2` / `SessionCreatedDataV3` 与 8 个 `(*Chunk)` chunk 子类声明行无 `BaseModel` 字样）；T-C1-076 增量 +11 = events job 域 10 模型（`JobBudget` `JobLineage` `JobTrace` + 7 data）+ cards `JobCard` 1，65 + 11 = 76（新模块/新模型机械枚举导出）。两口径皆真、非契约缺陷；T-C1-077 增量 +3 = 新 pipelines 模块（`PipelineEdge` `PipelineNode` `PipelineSpec`），76 + 3 = 79；工件覆盖全 79。T-C1-079 模型计数不变（`PipelineSpec` 增可选 `params_schema` 字段——字段新增非模型新增，79 仍真）。
2. **121 fixture lock-diff**：工件 + 测试内置最小 JSON Schema 校验器（draft 2020-12 pydantic 发射子集）重放全部 121 个 fixture，分类 35 正例 / 66 schema 级 / 20 行为级与消费面测试语义一致，零差异（+21 = py.job.* 七类三元组随参数化位自然扩容，T-C1-076；+3 = pipeline spec 三元组，`TestPipelineSpecLockDiff` 承载，T-C1-077；+2 = params 声明对——正例 + bad_schema 各 1，同承载，T-C1-079）。口径注（T-B-092 MINOR-1）：本条与 §5 为折叠口径（35/66/20），消费面 `PINNED_COUNTS` 为四桶（events 33/63/20 + pipeline 5 = 121），逐桶差恰为 pipeline 五件。
3. **33 正例 ↔ 34 注册表定义**：33 条已注册定义与正例一一对应（含 T-C1-060 compaction、T-C1-076 job 七定义，缺口即时闭合）；+1 skills 定义（T-C1-052a）fixture 归 P2-S2（§6.3 L366），缺口在册（§5）；+2 pipeline spec 正例（T-C1-077 三元组之正例 + T-C1-079 params 对之正例）非注册表事件面，不入本对应账（CORE-CONTRACTS §5.4 S2 spec wire / S4 params_schema 字段面）。
4. **接缝分布闭合**：3 + 16 + 18 + 75 + 9 = 121；S6 零 fixture 属预期，S1 三件即 §2.4 压缩三元组、S4 增 21 件即 py.job.* 七类三元组（T-C1-076）、再增 3 件即 pipeline spec 三元组（T-C1-077）、再增 2 件即 params 声明对（T-C1-079）（§2 注）。

——生成/锁定/测试执行细节与门禁数字见 `doc/orchestration/progress/T-C1-045.md`。
