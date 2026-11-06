# TS 行为对照与已知差异

> Python 实现已落地：R0-R4 全链+R5 S1-S4 在库（src/opencode 53 .py+tests 51 .py+fixtures 92 JSON，现读枚举亲证）。§2 表原 9 行 planned 中 **8 行已按实测翻 verified**；provider 1 行**待 MAAS smoke 终翻**（OP-R5-2 已裁：T-C1-048 独立票）；deferred 5 行维持。本文件为实测态对照记录（R5-S5 T-C1-047 逐行翻新），不再是 R0 期规划面。
> 参考目录：`D:\26_code\opencode_py_ver\opencode-2.2.2`。该目录没有 Git 元数据，不能伪造 upstream commit/tag。
> [source-baseline.json](source-baseline.json) 记录读取的关键文件 SHA-256——现册 **58 条**（R0 期首册 12 条经 R1-R4 逐次扩册，现读计数 58）；它锁定审查依据，不代表锁定整个上游仓库。

## 1. 状态和更新证据

- planned：本版本计划实现，尚无代码/测试。（R5-S5 翻新后 §2 表已无 planned 行）
- deferred：移到 Phase 2 的明确卡片。
- verified：只有记录了 Python 测试路径、源码基线和实际结果才使用——六列证据逐行落在 §2.1。
- 设计差异单独列出，不把“有意不同”标成测试已通过。

本册现态：verified 8 行+待 MAAS smoke 终翻 1 行（provider）+deferred 5 行。新增参考文件先补 SHA-256；源码变化先比较行为/契约，再更新基线和 fixture。获得完整 Git 版本后可追加 commit/tag，仍保留已有指纹。源码锁定与运行时 MAAS fixture 分开管理，fixture 必须脱敏。

## 2. 能力对照

TS 路径相对参考目录（前缀 `packages/`，见表后注）——表内 TS 路径为 R0 期在册引用（source-baseline.json 指纹册锁定），翻新复用不构成新引用；本票零新 TS 引用。Python 路径为**实际落点**（R0-R4+R5 S1-S4 实测在库，文件面现读亲证）——不再是预定落点。verified 行六列证据逐行见 §2.1。

| TS 依据 | Python 落点 | 状态 | 设计范围/差异 |
|---|---|---|---|
| schema/src/session-message.ts | contracts/messages.py | verified | user/assistant/idle；工具状态嵌在 assistant.parts；无独立 ToolMessage |
| schema/src/event.ts；core/src/bus.ts | contracts/events.py + storage.TransactionWriter + S2/S6 | verified | Python py.*、session 内 seq、version；S4 的 TransactionWriter 为私有实现，公共写入口只有 SessionStore.commit；durable 与 ephemeral 分开 |
| core/src/bus.ts 的 subscribe/log | Bus + EventLog | verified | live-only subscribe；follow 补读/Synced/轮询，不保证 ephemeral 重放 |
| core/src/session/projector.ts | session/projector.py | verified | 单事务同步投影；纯重放 + 单独恢复协调；不执行外部副作用 |
| schema/src/tool.ts；core/src/tool.ts | tools/registry.py + invoker.py | verified | 唯一 invoke、严格校验、catalog 快照；text + bounded structured result |
| schema/src/permission.ts | tools/policy.py | verified | Phase 1 本 session saved 决定；重启后重新确认，Python 审批审计 durable |
| ai/src/schema/messages.ts、events.ts | ai/provider.py + maas.py | **待 MAAS smoke 终翻**（OP-R5-2 已裁：T-C1-048 独立票，端点供给后 S6 前执行） | 一个 OpenAI-compatible MAAS；SDK 类型不外泄；缺失 usage/cost 为 null。fake server 无网络面已测（§2.1 provider 行），真实端点短会话证据未落——不虚过不冒充 verified |
| core/src/config/discovery.ts | config/loader.py | verified | 只做规定 JSON/AGENTS 来源及快照，不复制 MDM/完整 discovery |
| MCP stdio 行为 | mcp/manager.py + bridge.py + stdio.py | verified | 自研最小 JSON-RPC stdio client（OP-R4-1 用户裁决，零 SDK 依赖）；环境变量最小集原样传、不 merge host env；行为对拍非结构同构；协议面子集与 POSIX 腿承载面=已知限制（platform-support §9 逐件引注） |
| HTTP/client | M5 | deferred | Python 本地单用户协议；不是 TS server 的替代地址 |
| skills/subagent/HTTP MCP | M6 对应卡片 | deferred | 各有生命周期/权限/恢复测试；R4 已落 stdio MCP client（上行），HTTP MCP 传输层仍 deferred |
| compaction | M6 compaction | deferred | Phase 1 超预算明确停止；不删除原始日志 |
| 媒体/provider/worktree | M6 对应卡片 | deferred | file schema 预留不等于支持媒体；worktree 依赖 git/workspace；单 OpenAI-compatible MAAS provider 已落（上行 provider 行），多 provider/媒体处理/worktree 管理仍 deferred |
| 插件/codemode/PTY | M3/M4/M2 | deferred | 范围和兼容测试先做 spike，不宣称完整 TS 等价 |

表内 TS 文件前缀均为 `packages/`，如 `schema/src/event.ts` 对应 `packages/schema/src/event.ts`。`core/src/session/message.ts`、`event.ts` 只是重导出，真正的契约需看 `packages/schema/src/`。

### 2.1 verified 行实测证据（§4 纪律逐项承载；测试计数=`--collect-only` 现读，T-C1-047 亲证）

运行平台通用口径（verified 8 行一致，分列不冒充）：本机 **Windows 11 + CPython 3.12.16**（worktree .venv 现读）实测；CI **ubuntu-latest + windows-latest 双 OS** 同套件（绿链见 [platform-support.md](platform-support.md) §1.1）；本地 manual 与 CI 分列互不冒充。依赖版本=pyproject/uv.lock 现读口径（④ 注记：未做逐依赖矩阵实测）：pydantic 2.14.2 / sqlalchemy 2.1.6 / aiosqlite 0.22.1 / openai 3.42.0 / httpx2 2.16.0 / alembic 1.20.1 / pytest 9.2.0。验证结果通用口径：全量套件 **1134 passed**（collect-only 1134 对账——本票现读亲证；S5 基线 1134/109 见 r5-slices §0.1 #161 链在册，本票门禁复跑对账入 [T-C1-047 进度②](orchestration/progress/T-C1-047.md)）。fixture 总清单=[r5-fixture-inventory.md](reference/r5-fixture-inventory.md)（R5-S4 交付物）。

| 行 | 实际 Python 测试路径（collect 计数） | 正常/失败 fixture | 运行平台/依赖版本 | 验证结果 | 仍有差异 |
|---|---|---|---|---|---|
| messages | tests/contract/test_messages.py（29）+ tests/integration/test_messages_union.py（4） | 测试内联正/反例 JSON 字面量（本行无独立 JSON fixture 文件，如实分列） | 通用口径；pydantic 2.14.2 | 全量 1134 绿内含本行 33 测（②对账） | user/assistant/idle 判别式（contracts/messages.py 现读）；工具状态嵌 assistant.parts；无独立 ToolMessage |
| events + TransactionWriter | tests/contract/test_events.py（181）+ tests/contract/test_schema_lock.py（103，R5-S4 统一 schema 汇总+漂移锁定）+ tests/integration/test_bus_commit.py（1）+ test_store_commit.py（16）+ test_eventlog.py（20）+ test_follow_recovery.py（4，S2 断连/慢消费者补读）+ test_recovery.py（14，S6 启动恢复） | tests/contract/fixtures/ **92 JSON**（events_* 家族 valid/missing_field/bad_extension/missing_run_id 正反例，目录现读 92 件）+ doc/reference/schema/ 6 工件（R5-S4） | 通用口径；pydantic 2.14.2+sqlalchemy 2.1.6+aiosqlite 0.22.1 | 全量 1134 绿内含本行 339 测（②对账） | py.*、session 内 seq、显式 version；TransactionWriter（storage/txn.py）私有，公共写入口仅 SessionStore.commit；durable/ephemeral 分开 |
| Bus + EventLog | tests/contract/test_bus.py（24）+ tests/integration/test_eventlog.py（20） | 测试内联事件序列+断连/慢消费者/丢通知构造 | 通用口径；pydantic 2.14.2+aiosqlite 0.22.1 | 全量 1134 绿内含本行 44 测（②对账） | live-only subscribe；follow 补读/Synced/轮询，不保证 ephemeral 重放 |
| projector | tests/integration/test_projector.py（29）+ test_rebuild.py（17）+ test_recovery.py（14，恢复协调三窗） | 真 SQLite+真 alembic 链上事件流重放对照（integration 构造，非静态 fixture） | 通用口径；sqlalchemy 2.1.6+aiosqlite 0.22.1 | 全量 1134 绿内含本行 60 测（②对账） | 单事务同步投影；纯重放+单独恢复协调（session/recovery.py）；不执行外部副作用 |
| registry + invoker | tests/contract/test_tools.py（28）+ tests/contract/test_tool_listing.py（16）+ tests/integration/test_runner.py（34） | 测试内联工具定义正/反例+未知 capability 显式拒绝 | 通用口径；pydantic 2.14.2 | 全量 1134 绿内含本行 78 测（②对账） | 唯一 invoke、严格校验、catalog 快照；text+bounded structured result |
| policy | tests/contract/test_policy.py（21）+ tests/contract/test_permission_events.py（41）+ tests/integration/test_approval.py（12）+ tests/integration/test_permission_projector.py（11） | events_permission_* 6 事件家族×3 形态=18 JSON fixture（valid/missing_field/bad_extension，目录现读） | 通用口径；pydantic 2.14.2 | 全量 1134 绿内含本行 85 测（②对账） | 本 session saved 决定；重启后重新确认；审批审计 durable |
| config loader | tests/integration/test_config_loader.py（30）+ tests/contract/test_config.py（16） | 测试内联 v1/v2/v3 快照+JSON/AGENTS 来源正反例+启动指纹/漂移停住面 | 通用口径；pydantic 2.14.2 | 全量 1134 绿内含本行 46 测（②对账） | 规定 JSON/AGENTS 来源及快照（v3 snapshot+MCP refs+启动指纹）；不复制 MDM/完整 discovery |
| MCP stdio | tests/integration/test_mcp_stdio.py（13）+ tests/integration/test_mcp_catalog.py（3）+ tests/contract/test_mcp_naming.py（44）+ tests/integration/test_e2e_r4.py（7） | tests/integration/mcp_stdio_server.py 真子进程+9 模式（ok/startup_hang/catalog_hang/tools_list_error/big_frame/exit_during_call/crash_once/ignore_eof_with_child/crash_with_child） | 通用口径；client 面纯标准库（OP-R4-1 零 SDK）；POSIX 腿=CI 承载非本机实测（platform-support §9） | 全量 1134 绿内含本行 67 测（②对账） | 自研 client；env 最小集原样传；协议面子集+POSIX 腿承载面=已知限制逐件引注（platform-support §9） |
| provider（未翻 verified） | tests/contract/test_ai_adapter.py（36）+ tests/contract/test_llm.py（63）+ tests/r0/test_provider_spike.py（24，R0-A spike） | tests/r0/fake_openai_server.py 无网络 fake server（多轮文本+流式工具调用面已测） | 通用口径；openai 3.42.0+httpx2 2.16.0（pyproject/uv.lock 现读口径，④ 注记） | fake server 面在全量 1134 绿内（123 测）；**真实端点短会话未执行** | **待 MAAS smoke 终翻**（OP-R5-2 已裁：T-C1-048 独立票，端点供给后 S6 前执行）——SDK 类型不外泄、缺失 usage/cost 为 null 维持原述，不虚过不冒充 |

## 3. 事件语义映射

这里只是对照，不是 JSON 字段转换器；Python 的版本 1 不能当作 TS 的同名版本。

| Python 事件/状态 | TS 对照 | 差异 |
|---|---|---|
| py.session.created | session.created | Python 保存 config snapshot；不要求 TS project/location 全形态 |
| py.prompt.accepted | session.inbox.enqueued/delivered + user 消息 | Phase 1 无 inbox/steering，独立 request_id 幂等 |
| py.run.started/finished | session.execution.started/succeeded/failed/interrupted | Python 显式 run_id；idle 由 finished 投影 |
| py.step.started/finished | session.step.started/ended/failed | Python finished 自包含 assistant 终态 |
| py.step.generated | session.step.streamed + tool/input 事件的相关行为 | Python 先持久完整调用计划再执行；没有一对一 wire 对应 |
| py.text.finalized / py.reasoning.finalized | session.text.ended / session.reasoning.ended | Python part_id，TS ordinal/message ID 字段不同 |
| py.tool.requested/started/finished | session.tool.input.* / called / success / failed | Python 额外有 unknown/effect_status；TS success/failed 当前 durable version=2 |
| py.permission.requested/replied/closed/saved/revoked/expired | permission.asked/replied、权限保存机制 | TS asked/replied 为 ephemeral；Python durable 审计且显式记录系统关闭；revoked/expired 为 Python 侧终态补全（contracts/events.py 现读 6 枚 permission.*） |
| py.text.delta/py.reasoning.delta/py.tool.progress | session.text.delta/reasoning.delta/tool.progress | 都不作可靠恢复事实 |

后续如需 TS wire/server 兼容，单独实现映射 adapter 并用两端 JSON fixture 验证；同名字符串不能作为“行为一致”的证据。不直接共享 TS SQLite 文件，不按 TS 迁移链建 Python 库。

## 4. 每次实现后填写的证据

将对应 planned 项改 verified 前，必须补实际的 Python 测试路径、正常/失败 fixture、运行平台/依赖版本、验证结果和仍有差异——现行承载=§2.1 六列证据子表（R5-S5 T-C1-047 逐行落）。故意不兼容的变化更新 ADR 和事件版本；不改旧事件内容来掩盖差异。后续翻新行（provider 待 MAAS smoke 终翻=T-C1-048）同纪律。

## 5. R0 探针实测记录（平台行为，非正式模块）

以下为 R0-B 平台探针实测结论（探针 `tools/r0/platform_probe.py`；测试 `tests/r0/test_platform_probe.py` 8 项；证据 `evidence/r0/platform.json`）。它们验证正式模块所依赖的平台行为（filesystem 路径序/进程树回收/SQLite+OS 单 writer——R4 各模块实测沿用这些基线）；正式模块现已实现（R0-R4+R5 S1-S4），§2 表 8 行已翻 verified、provider 行待 MAAS smoke 终翻；探针本身保留为平台行为基线实测（8 项测试在全量 1134 内，collect 现读）。

- filesystem.ts 路径顺序（lexical resolve → containment → realpath(strict) → containment(realpathed root)）：Windows 11 本地实测 9 场景全部符合——`..` 与绝对越界路径在 realpath 前拦截（`escapes_workspace_before_realpath`），越界 symlink/junction 在 realpath 后拦截（`escapes_workspace_after_realpath`），缺失目标 strict realpath 拒绝，workspace 内路径（含指向内部的 symlink）放行。
- SQLite + OS 单 writer（CORE-CONTRACTS L165）：WAL、`foreign_keys=ON`、`busy_timeout=5000` 实测生效（FK 违规 IntegrityError 实测）；OS 数据目录锁（Windows `msvcrt.locking`/POSIX `fcntl.flock`）先于 DB 打开；第二 writer 在写事务前锁失败返回 conflict 且无任何写入（数据目录快照一致 + writer 1 行数稳定）；持锁 writer 异常退出后 OS 自动释放锁。Windows 本地与 CI、ubuntu CI（fcntl）均已实测（CI run 41182347042）。
- 进程树回收（CORE-CONTRACTS L234）：Windows Job Object（KILL_ON_CLOSE + proceed-gate 保证 Assign 先于孙进程创建）cancel/timeout 两场景子+孙进程均在 grace 内退出、临时目录清理验证；POSIX process group（bash 5.3.9）经 ubuntu CI 实测同样成立（两场景 ~100ms）；POSIX zombie（已退出未收割）按已退出处理。
- 平台结果与限制（restricted ≠ 失败；CI 与本地分开记录）：见 [platform-support.md](platform-support.md)。

- [T-A-003] Python `CoreError.details` 在 wire 层恒出现；输入省略时序列化为 `{}`（有内容时保留脱敏对象），对应 CORE §8 的“输入可选、输出固定”约定；TS 无对应 wire 形状。
- [T-A-003] Python `DurabilityMismatchError`/`EventDecodeError` 家族细化 TS `InvalidDurableEventError`（contracts/events.py L804/L816 现读；TS `packages/core/src/bus.ts:60–66,78–90` 为在册引用），区分未知 type/version、durability 错配与坏 envelope；已存日志统一映射 `StorageCorrupt`/只读诊断。
