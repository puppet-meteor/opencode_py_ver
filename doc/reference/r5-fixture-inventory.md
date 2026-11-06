# R5-S4 契约证据清单：统一 JSON Schema 工件与六接缝 fixture 对账（T-C1-045）

- 席位：R5-S4（契约证据面）；任务书 `doc/orchestration/tasks/T-C1-045.md`。
- 基线：`d889810270a00c7abca6e58a2aa913b7d457c9f0`（checkpoint #159）。
- 权威源：`doc/CORE-CONTRACTS.md` §1 L17（「统一 JSON Schema 在 R5 由实际模型的 `model_json_schema()` 汇总生成并锁定差异」）、§2–§7（六接缝定义）、§8（错误与演进）；`doc/plans/r5-slices.md` §2.4；任务书 Done L16 与验收C 前半。
- 范围声明：本清单只陈述 Python 侧可核事实（模型枚举、工件结构、fixture 分类、测试锚）。按 parity §3 L41 口径，全文不含 TS wire 同构/兼容宣称；冻结模型 docstring 经 `model_json_schema()` 的 `description` 字段原样进入工件，其中含历史性 TS 对照注记（差异对照，非等价宣称），见 §8。

## 1. 统一 JSON Schema 工件

生成器 `tools/r5/generate_schema_artifacts.py`（导入五模块公开定义面；`build_artifacts()` 即漂移测试的编程 API）；守卫 `tests/contract/test_schema_lock.py`。

| 工件 | 顶层结构 | 覆盖 | 字节 |
|---|---|---|---|
| `doc/reference/schema/errors.json` | `models`×1 | `CoreError` | 2117 |
| `doc/reference/schema/messages.json` | `models`×8 | messages 模块全模型 | 29706 |
| `doc/reference/schema/llm.json` | `models`×17 | llm 模块全模型（含 `_ChunkBase`） | 42378 |
| `doc/reference/schema/tools.json` | `models`×3 | `ToolRef` / `ToolSpec` / `ToolResult` | 8504 |
| `doc/reference/schema/events.json` | `models`×34 | events 模块全模型（含 3 泛型信封） | 101624 |
| `doc/reference/schema/event-envelopes.json` | `durable`×21 + `ephemeral`×4 + `generic`×3 | 注册表参数化信封 | 119351 |

- **粒度**：模块边界 = 机械稳定枚举单元（`vars(module)` + `issubclass(BaseModel)` + `__module__` 归属过滤，重导出零重复）；信封单独按注册表定义参数化；六接缝是语义维度，不在工件内分文件，由本清单显式承载（任务书 1a）。
- **durable 键** = `versioned_type` 形如 `py.session.created.1`，共 21 键（R1 2：`py.session.created.1`、`py.prompt.accepted.1`；R2 durable 11；R3 permission 6；session_created v2/v3 各 1）；**ephemeral 键** = `py.<type>`，4 键（`py.text.delta` 等）；**generic** = `EventDraft` / `DurableEvent` / `EphemeralEvent` 未参数化泛型，其 `data` 面为任意对象——即 unknown_type / unknown_version 探针的 schema 靶。
- **序列化**：`json.dumps(indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)` + 尾换行、`newline="\n"`（dump_fixtures.py 先例 + 键排序消除 dict 插入序依赖）。字节确定性双证：进程内 `build_artifacts()` 连调相等（测试钉）+ 跨进程重跑 6 文件 SHA256 全等（会话内两轮实测）。

## 2. 六接缝 × 证据矩阵（契约模型 / fixture / 行为锚 / 适配器）

| 接缝（CORE-CONTRACTS §） | 契约模型 | fixture | 行为测试锚 | 适配器（真实/fake） |
|---|---|---|---|---|
| S1 §2 历史、模型输入和流式输出 | messages 8 + llm 17 = 25 | 0（见注） | `tests/contract/test_messages.py`、`test_llm.py`、`test_ai_adapter.py` | `tests/r0/fake_openai_server.py`（fake OpenAI 兼容 HTTP 服务） |
| S2 §4 live 与 durable feed | events 4（ephemeral data） | 16 | `tests/contract/test_events.py`（R2 正/负例与 run 元数据组） | `tests/integration/test_eventlog.py`、`test_bus_commit.py`（真实 SQLite 事件日志/总线提交）+ `tests/contract/test_bus.py` |
| S3 §5 工具、权限与生命周期 | tools 3 + events permission 6 = 9 | 18 | `tests/contract/test_permission_events.py`、`test_tools.py`、`test_tool_listing.py` | `tests/integration/test_tool_bash.py`、`test_tool_read.py`、`test_tool_static_trio.py`、`test_tool_write_edit.py`（真实 fs/bash 工具）；`tests/integration/mcp_stdio_server.py`（fake stdio MCP 服务端） |
| S4 §3（S6+S4）唯一事件写入、投影与数据库 | events 15（3 信封 + 12 durable data） | 49 | `tests/contract/test_events.py`（R1/R2 正负例、注册表探针组） | `tests/integration/test_store_commit.py`、`test_projector.py`、`test_rebuild.py`、`test_recovery.py`、`test_migrations.py`（真实 SQLite + alembic） |
| S5 §6 配置、指令和 MCP 信任 | events 9（snapshot / session_created 家族） | 9 | `tests/contract/test_config.py`（TestSessionCreatedV2Wire）、`test_mcp_naming.py`（TestSessionCreatedV3Wire） | `tests/integration/test_config_loader.py`（真实 fs 配置加载） |
| S6 §7 应用命令接口和任务所有权 | 0（运行时资源，非 wire 模型） | 0（见注） | `tests/integration/test_service.py`、`test_service_run.py`、`test_cli.py`、`test_cli_r3.py`、`test_cli_r4.py`、`test_e2e*.py` | 真实 service/CLI 进程面（无 fake） |

注（S1/S6 零 fixture 属预期，非缺口）：92 个落盘 fixture 全部是事件 wire 面（`events_*.json`）。S1 的消息/生成 wire 形状由模型 + 单测内联构造锁定（R1 起即此口径，CORE-CONTRACTS §1 L17），其统一 schema 面由本任务工件补齐；S6 命令面是运行时行为（无 wire 模型），由集成测试承载。两接缝的模型/行为锚/适配器证据列均非空。

## 3. 模型→接缝映射（63 = errors 1 + messages 8 + events 34 + llm 17 + tools 3）

| 模块 | 接缝 | 模型 |
|---|---|---|
| `contracts/errors.py` | 跨接缝（§8 错误面） | `CoreError` |
| `contracts/messages.py` | S1 | `AssistantMessage` `AssistantReasoningPart` `AssistantTextPart` `AssistantToolPart` `IdleMessage` `ToolFileContent` `ToolTextContent` `UserMessage` |
| `contracts/llm.py` | S1 | `ErrorChunk` `FinishChunk` `GenerationRequest` `LLMMessage` `LLMReasoningPart` `LLMTextPart` `LLMToolCallPart` `LLMToolResultPart` `ModelRef` `ReasoningDeltaChunk` `TextDeltaChunk` `ToolCallArgsDeltaChunk` `ToolCallEndChunk` `ToolCallStartChunk` `Usage` `UsageChunk` `_ChunkBase` |
| `contracts/events.py` | S2 | `ReasoningDeltaData` `TextDeltaData` `ToolInputDeltaData` `ToolProgressData` |
| `contracts/events.py` | S3 | `PermissionClosedData` `PermissionExpiredData` `PermissionRepliedData` `PermissionRequestedData` `PermissionRevokedData` `PermissionSavedData` |
| `contracts/tools.py` | S3 | `ToolRef` `ToolResult` `ToolSpec` |
| `contracts/events.py` | S4 | 信封：`EventDraft` `DurableEvent` `EphemeralEvent`；durable data：`PromptAcceptedData` `ReasoningFinalizedData` `RunCancelRequestedData` `RunFinishedData` `RunStartedData` `StepFinishedData` `StepGeneratedData` `StepStartedData` `TextFinalizedData` `ToolFinishedData` `ToolRequestedData` `ToolStartedData` |
| `contracts/events.py` | S5 | `ConfigSnapshot` `ConfigSnapshotV2` `ConfigSnapshotV3` `InstructionSource` `McpServerRef` `SessionCreatedData` `SessionCreatedDataV2` `SessionCreatedDataV3` `SnapshotLimits` |

计数：S1 25 + S2 4 + S3 9 + S4 15 + S5 9 + S6 0 + 跨接缝 1 = 63。

## 4. fixture 枚举（92；接缝分布 S2:16 / S3:18 / S4:49 / S5:9）

| # | 文件 | 接缝 | 类别 | 行为锚 |
|---|---|---|---|---|
| 1 | `events_permission_closed_bad_extension.json` | S3 | 反例·schema 级（additionalProperties:false） | test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected |
| 2 | `events_permission_closed_missing_field.json` | S3 | 反例·schema 级（required） | test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected |
| 3 | `events_permission_closed_valid.json` | S3 | 正例 | test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless |
| 4 | `events_permission_expired_bad_extension.json` | S3 | 反例·schema 级（additionalProperties:false） | test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected |
| 5 | `events_permission_expired_missing_field.json` | S3 | 反例·schema 级（required） | test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected |
| 6 | `events_permission_expired_valid.json` | S3 | 正例 | test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless |
| 7 | `events_permission_replied_bad_extension.json` | S3 | 反例·schema 级（additionalProperties:false） | test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected |
| 8 | `events_permission_replied_missing_field.json` | S3 | 反例·schema 级（required） | test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected |
| 9 | `events_permission_replied_valid.json` | S3 | 正例 | test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless |
| 10 | `events_permission_requested_bad_extension.json` | S3 | 反例·schema 级（additionalProperties:false） | test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected |
| 11 | `events_permission_requested_missing_field.json` | S3 | 反例·schema 级（required） | test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected |
| 12 | `events_permission_requested_valid.json` | S3 | 正例 | test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless |
| 13 | `events_permission_revoked_bad_extension.json` | S3 | 反例·schema 级（additionalProperties:false） | test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected |
| 14 | `events_permission_revoked_missing_field.json` | S3 | 反例·schema 级（required） | test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected |
| 15 | `events_permission_revoked_valid.json` | S3 | 正例 | test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless |
| 16 | `events_permission_saved_bad_extension.json` | S3 | 反例·schema 级（additionalProperties:false） | test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected |
| 17 | `events_permission_saved_missing_field.json` | S3 | 反例·schema 级（required） | test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected |
| 18 | `events_permission_saved_valid.json` | S3 | 正例 | test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless |
| 19 | `events_prompt_accepted_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestNegativeFixtures::test_invalid_extension_value_rejected |
| 20 | `events_prompt_accepted_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestNegativeFixtures::test_missing_required_field_rejected |
| 21 | `events_prompt_accepted_valid.json` | S4 | 正例 | test_events.py::TestPositiveFixtures::test_round_trip_is_lossless |
| 22 | `events_reasoning_delta_bad_extension.json` | S2 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 23 | `events_reasoning_delta_missing_field.json` | S2 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 24 | `events_reasoning_delta_missing_run_id.json` | S2 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 25 | `events_reasoning_delta_valid.json` | S2 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 26 | `events_reasoning_finalized_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 27 | `events_reasoning_finalized_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 28 | `events_reasoning_finalized_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 29 | `events_reasoning_finalized_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 30 | `events_run_cancel_requested_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 31 | `events_run_cancel_requested_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 32 | `events_run_cancel_requested_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 33 | `events_run_cancel_requested_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 34 | `events_run_finished_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 35 | `events_run_finished_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 36 | `events_run_finished_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 37 | `events_run_finished_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 38 | `events_run_started_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 39 | `events_run_started_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 40 | `events_run_started_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 41 | `events_run_started_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 42 | `events_session_created_bad_extension.json` | S5 | 反例·schema 级（additionalProperties:false） | test_events.py::TestNegativeFixtures::test_invalid_extension_value_rejected |
| 43 | `events_session_created_missing_field.json` | S5 | 反例·schema 级（required） | test_events.py::TestNegativeFixtures::test_missing_required_field_rejected |
| 44 | `events_session_created_v2_bad_extension.json` | S5 | 反例·schema 级（additionalProperties:false） | test_config.py::TestSessionCreatedV2Wire::test_bad_extension_fixture_is_rejected |
| 45 | `events_session_created_v2_bad_hash.json` | S5 | 反例·行为级（_snapshot_hash_verified 校验器） | test_config.py::TestSessionCreatedV2Wire::test_bad_hash_fixture_is_rejected |
| 46 | `events_session_created_v2_valid.json` | S5 | 正例 | test_config.py::TestSessionCreatedV2Wire::test_valid_fixture_decodes_and_round_trips |
| 47 | `events_session_created_v3_bad_extension.json` | S5 | 反例·schema 级（additionalProperties:false） | test_mcp_naming.py::TestSessionCreatedV3Wire::test_bad_extension_fixture_is_rejected |
| 48 | `events_session_created_v3_bad_hash.json` | S5 | 反例·行为级（_snapshot_hash_verified 校验器） | test_mcp_naming.py::TestSessionCreatedV3Wire::test_bad_hash_fixture_is_rejected |
| 49 | `events_session_created_v3_valid.json` | S5 | 正例 | test_mcp_naming.py::TestSessionCreatedV3Wire::test_valid_fixture_decodes_and_round_trips |
| 50 | `events_session_created_valid.json` | S5 | 正例 | test_events.py::TestPositiveFixtures::test_round_trip_is_lossless |
| 51 | `events_step_finished_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 52 | `events_step_finished_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 53 | `events_step_finished_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 54 | `events_step_finished_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 55 | `events_step_generated_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 56 | `events_step_generated_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 57 | `events_step_generated_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 58 | `events_step_generated_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 59 | `events_step_started_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 60 | `events_step_started_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 61 | `events_step_started_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 62 | `events_step_started_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 63 | `events_text_delta_bad_extension.json` | S2 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 64 | `events_text_delta_missing_field.json` | S2 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 65 | `events_text_delta_missing_run_id.json` | S2 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 66 | `events_text_delta_valid.json` | S2 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 67 | `events_text_finalized_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 68 | `events_text_finalized_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 69 | `events_text_finalized_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 70 | `events_text_finalized_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 71 | `events_tool_finished_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 72 | `events_tool_finished_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 73 | `events_tool_finished_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 74 | `events_tool_finished_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 75 | `events_tool_input_delta_bad_extension.json` | S2 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 76 | `events_tool_input_delta_missing_field.json` | S2 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 77 | `events_tool_input_delta_missing_run_id.json` | S2 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 78 | `events_tool_input_delta_valid.json` | S2 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 79 | `events_tool_progress_bad_extension.json` | S2 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 80 | `events_tool_progress_missing_field.json` | S2 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 81 | `events_tool_progress_missing_run_id.json` | S2 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 82 | `events_tool_progress_valid.json` | S2 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 83 | `events_tool_requested_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 84 | `events_tool_requested_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 85 | `events_tool_requested_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 86 | `events_tool_requested_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 87 | `events_tool_started_bad_extension.json` | S4 | 反例·schema 级（additionalProperties:false） | test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected |
| 88 | `events_tool_started_missing_field.json` | S4 | 反例·schema 级（required） | test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected |
| 89 | `events_tool_started_missing_run_id.json` | S4 | 反例·行为级（_require_run_metadata 校验器） | test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected |
| 90 | `events_tool_started_valid.json` | S4 | 正例 | test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless |
| 91 | `events_unknown_type.json` | S4 | 反例·行为级（注册表 UnknownEventTypeError） | test_events.py::TestNegativeFixtures::test_unknown_type_rejected |
| 92 | `events_unknown_version.json` | S4 | 反例·行为级（注册表 UnknownEventVersionError） | test_events.py::TestNegativeFixtures::test_unknown_version_rejected |

## 5. 反例分类与拒绝语义

| 类别 | 数量 | 拒绝面（锚） |
|---|---|---|
| 正例 | 25 | 与注册表 25 条定义一一对应（R1 2 + R2 15 + R3 permission 6 + R3 config v2 1 + R4 config v3 1；events.py L939/L947/L971/L988/L1000，`default_event_registry` L1005） |
| 反例·schema 级 | 48 | bad_extension 25（envelope/typed data `extra="forbid"`，CORE-CONTRACTS §1 L15）+ missing_field 23（`required` 缺失拒绝） |
| 反例·行为级 | 19 | missing_run_id 15（`_require_run_metadata`，events.py L732）+ bad_hash 2（`_snapshot_hash_verified`，events.py L337）+ 注册表探针 2（`UnknownEventTypeError` L808 / `UnknownEventVersionError` L812；raise 于 L907/L908/L917） |

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
| run_id 元数据规则 | `_require_run_metadata`（events.py L732） | metadata 是 JsonObject 自由键，键存在性约束非 JSON Schema 可表达 |
| 快照摘要 | `compute_snapshot_hash` / `_snapshot_hash_verified`（events.py L337） | 内容哈希是计算性校验 |
| 注册表成员资格 | `UnknownEventTypeError` / `UnknownEventVersionError`（L808/L812） | 类型/版本全集是注册表运行时状态，泛型信封 `data` 面为任意对象 |
| durability 路由 | `EventDefinition.durability`（注册表属性） | durable/ephemeral 分派不是信封 schema 字段 |
| ≥1 可读文本 | `_has_model_readable_text`（events.py L585） | 跨字段计数约束 |
| env_refs 规范形 | `McpServerRef.env_refs` field_validator（events.py L264/L274） | 规范化语义（计算性），schema 只钉容器形状 |
| naive datetime 拒绝 | 模型层 `_as_utc` 等行为 | schema `format` 按 draft 语义仅为注记不判定 |
| bool→int 拒绝层 | CORE-CONTRACTS §1 L16 仓库级 before-validator | lax 转换守卫是 Python/Pydantic 特有行为，非 schema 面能力 |

## 8. 对账结论（零差异）

1. **63 vs 53**：调度器 grep 预核 53 = 「class 行含 `BaseModel` 字样」的行匹配口径；机械枚举 63 = 53 + 10 条子类声明行（`SessionCreatedDataV2` / `SessionCreatedDataV3` 与 8 个 `(*Chunk)` chunk 子类声明行无 `BaseModel` 字样）。两口径皆真、非契约缺陷；工件覆盖全 63。
2. **92 fixture lock-diff**：工件 + 测试内置最小 JSON Schema 校验器（draft 2020-12 pydantic 发射子集）重放全部 92 个 fixture，分类 25 正例 / 48 schema 级 / 19 行为级与消费面测试语义一致，零差异。
3. **25 正例 ↔ 25 注册表定义**一一对应（§5）。
4. **接缝分布闭合**：16 + 18 + 49 + 9 = 92；S1/S6 零 fixture 属预期（§2 注）。

——生成/锁定/测试执行细节与门禁数字见 `doc/orchestration/progress/T-C1-045.md`。
