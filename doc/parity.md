# TS 行为对照与已知差异

> 当前没有 Python 实现，下面均为规划/设计差异，不是运行验证结果。
> 参考目录：`D:\26_code\opencode_py_ver\opencode-2.2.2`。该目录没有 Git 元数据，不能伪造 upstream commit/tag。
> [source-baseline.json](source-baseline.json) 记录本次读取的 12 个关键文件 SHA-256；它锁定审查依据，不代表锁定整个上游仓库。

## 1. 状态和更新证据

- planned：本版本计划实现，尚无代码/测试。
- deferred：移到 Phase 2 的明确卡片。
- verified：只有记录了 Python 测试路径、源码基线和实际结果才使用。
- 设计差异单独列出，不把“有意不同”标成测试已通过。

新增参考文件先补 SHA-256；源码变化先比较行为/契约，再更新基线和 fixture。获得完整 Git 版本后可追加 commit/tag，仍保留已有指纹。源码锁定与运行时 MAAS fixture 分开管理，fixture 必须脱敏。

## 2. 能力对照

TS 路径相对参考目录；Python 路径是预定落点，不是已有文件。

| TS 依据 | Python 目标 | 状态 | 设计范围/差异 |
|---|---|---|---|
| schema/src/session-message.ts | contracts/messages.py | planned | user/assistant/idle；工具状态嵌在 assistant.parts；无独立 ToolMessage |
| schema/src/event.ts；core/src/bus.ts | contracts/events.py + storage.TransactionWriter + S2/S6 | planned | Python py.*、session 内 seq、version；S4 的 TransactionWriter 为私有实现，公共写入口只有 SessionStore.commit；durable 与 ephemeral 分开 |
| core/src/bus.ts 的 subscribe/log | Bus + EventLog | planned | live-only subscribe；follow 补读/Synced/轮询，不保证 ephemeral 重放 |
| core/src/session/projector.ts | session/projector.py | planned | 单事务同步投影；纯重放 + 单独恢复协调；不执行外部副作用 |
| schema/src/tool.ts；core/src/tool.ts | tools/registry.py + invoker.py | planned | 唯一 invoke、严格校验、catalog 快照；text + bounded structured result |
| schema/src/permission.ts | tools/policy.py | planned | Phase 1 本 session saved 决定；重启后重新确认，Python 审批审计 durable |
| ai/src/schema/messages.ts、events.ts | ai/provider.py + maas.py | planned | 一个 OpenAI-compatible MAAS；SDK 类型不外泄；缺失 usage/cost 为 null |
| core/src/config/discovery.ts | config/loader.py | planned | 只做规定 JSON/AGENTS 来源及快照，不复制 MDM/完整 discovery |
| MCP stdio 行为 | mcp/manager.py + bridge.py | planned | Phase 1 R4；接入时补 SDK/上游引用、版本和真实进程测试 |
| HTTP/client | M5 | deferred | Python 本地单用户协议；不是 TS server 的替代地址 |
| skills/subagent/HTTP MCP | M6 对应卡片 | deferred | 各有生命周期/权限/恢复测试 |
| compaction | M6 compaction | deferred | Phase 1 超预算明确停止；不删除原始日志 |
| 媒体/provider/worktree | M6 对应卡片 | deferred | file schema 预留不等于支持媒体；worktree 依赖 git/workspace |
| 插件/codemode/PTY | M3/M4/M2 | deferred | 范围和兼容测试先做 spike，不宣称完整 TS 等价 |

表内 TS 文件前缀均为 `packages/`，如 `schema/src/event.ts` 对应 `packages/schema/src/event.ts`。`core/src/session/message.ts`、`event.ts` 只是重导出，真正的契约需看 `packages/schema/src/`。

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
| py.permission.requested/replied/closed/saved | permission.asked/replied、权限保存机制 | TS asked/replied 为 ephemeral；Python durable 审计且显式记录系统关闭 |
| py.text.delta/py.reasoning.delta/py.tool.progress | session.text.delta/reasoning.delta/tool.progress | 都不作可靠恢复事实 |

后续如需 TS wire/server 兼容，单独实现映射 adapter 并用两端 JSON fixture 验证；同名字符串不能作为“行为一致”的证据。不直接共享 TS SQLite 文件，不按 TS 迁移链建 Python 库。

## 4. 每次实现后填写的证据

将对应 planned 项改 verified 前，必须补实际的 Python 测试路径、正常/失败 fixture、运行平台/依赖版本、验证结果和仍有差异。故意不兼容的变化更新 ADR 和事件版本；不改旧事件内容来掩盖差异。

## 5. R0 探针实测记录（平台行为，非正式模块）

以下为 R0-B 平台探针实测结论（探针 `tools/r0/platform_probe.py`；测试 `tests/r0/test_platform_probe.py` 8 项；证据 `evidence/r0/platform.json`）。它们验证未来模块将依赖的平台行为，不代表正式模块已实现；§2 表内条目仍为 planned。

- filesystem.ts 路径顺序（lexical resolve → containment → realpath(strict) → containment(realpathed root)）：Windows 11 本地实测 9 场景全部符合——`..` 与绝对越界路径在 realpath 前拦截（`escapes_workspace_before_realpath`），越界 symlink/junction 在 realpath 后拦截（`escapes_workspace_after_realpath`），缺失目标 strict realpath 拒绝，workspace 内路径（含指向内部的 symlink）放行。
- SQLite + OS 单 writer（CORE-CONTRACTS L165）：WAL、`foreign_keys=ON`、`busy_timeout=5000` 实测生效（FK 违规 IntegrityError 实测）；OS 数据目录锁（Windows `msvcrt.locking`/POSIX `fcntl.flock`）先于 DB 打开；第二 writer 在写事务前锁失败返回 conflict 且无任何写入（数据目录快照一致 + writer 1 行数稳定）；持锁 writer 异常退出后 OS 自动释放锁。Windows 已实测；POSIX 锁语义待 CI 实测补记。
- 进程树回收（CORE-CONTRACTS L234）：Windows Job Object（KILL_ON_CLOSE + proceed-gate 保证 Assign 先于孙进程创建）cancel/timeout 两场景子+孙进程均在 grace 内退出、临时目录清理验证；POSIX process group 机制待 CI 实测补记。
- 平台结果与限制（restricted ≠ 失败；CI 与本地分开记录）：见 [platform-support.md](platform-support.md)。
