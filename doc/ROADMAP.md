# OpenCode Python：先完成核心闭环，再按需扩展

> 更新：2026-10-30。状态：实现前的设计基线；本文的验收项尚未实现。
> 目标：Python 3.12+，单用户、本地 workspace、headless 优先。

## 1. 交付边界

先做一个能对话、能修改代码、能恢复会话的 Python agent CLI。核心版本包含完整的输入、模型、工具、权限、事件、持久化和取消链路，后续功能通过小接口接入。

**“骨架完备”指核心链路没有临时旁路，不要求提前实现最终产品的全部接口。** 新模块以新增文件为主，允许修改组合入口、适配器和受影响的内部实现；不承诺“只加文件、不改文件”。

| 范围 | Phase 1 的决定 |
|---|---|
| 模型 | 一个 MAAS 端点，通过 `openai` SDK 调用其 OpenAI 兼容 API |
| 操作能力 | `bash/read/write/edit/glob/grep/ls`；工具执行串行 |
| 入口 | REPL + 同进程 `SessionService`；HTTP 和远程 SDK 后续再做 |
| 数据 | SQLite、SQLAlchemy 2.0 async、aiosqlite、Alembic |
| 上下文 | 全局/项目配置、AGENTS.md；有预算限制，超限明确停止 |
| 扩展证明 | 一个显式配置的 stdio MCP server |
| 暂缓 | skills、subagent、HTTP MCP、插件、codemode、自动 compaction、PTY、watcher、媒体、worktree、多 provider |

这是本地受信任用户运行的工具，不是执行恶意代码的沙箱。文件工具的路径限制、shell 审批、MCP 信任分别生效；shell 与 MCP 子进程仍拥有操作系统授予的权限。

## 2. 文档导航与唯一事实来源

| 文档 | 负责的内容 |
|---|---|
| [PHASE1-SLIM.md](PHASE1-SLIM.md) | 切片、依赖、验收、测试、交付顺序 |
| [STEP-01-R0.md](STEP-01-R0.md) | R0 风险打样的逐步执行、证据和闸门 |
| [CORE-CONTRACTS.md](CORE-CONTRACTS.md) | 六个接缝的接口、事件数据、错误与恢复语义 |
| [PHASE2-MODULES.md](PHASE2-MODULES.md) | 可选模块、真实依赖、变更范围和估算 |
| [parity.md](parity.md) | TS 对照、已知差异、参考源码锁定 |
| [ADR-0001](adr/0001-core-architecture.md) | 单一写入路径、可信范围、兼容策略的理由 |

契约只在 `CORE-CONTRACTS.md` 定义，阶段文档引用它，避免重复的接口示例产生冲突。当前仓库只有规划文档，不能把规划中的验收项视为已经通过。

## 3. 六个接缝和依赖方向

| 接缝 | 落点 | 小接口和责任 | 后续扩展 |
|---|---|---|---|
| S1 消息与模型 | `contracts/`、`ai/` | 历史消息、模型输入、provider stream 三层分离；SDK 类型限于适配器 | 媒体、多 provider |
| S2 通知与日志读取 | `bus/` | live 订阅；durable 日志的分页、游标和 follow；不写业务数据 | SSE、跨进程 feed |
| S3 工具调用 | `tools/` | 注册/快照/唯一 invoke；校验、权限、执行、结果 | MCP、插件、codemode |
| S4 存储 | `storage/` | DB 事务、只读查询、顺序约束、迁移；底层写方法仅供 S6 使用 | workspace、kv、多实例 |
| S5 配置 | `config/` | 来源优先级、字段合并、指令发现、不可变快照 | skills、插件配置、reload |
| S6 会话应用层 | `session/` | `SessionService` 接收命令并持有任务；`SessionStore` 唯一提交事件；projector 重建历史 | CLI/HTTP 共用、subagent、compaction |

CLI 和未来 server 都只调用 `SessionService`。Bus 是输出接口，不能替代 create/prompt/cancel/approve 的命令接口。运行时依赖在组合入口注入，Phase 1 使用真实适配器和 fake 适配器验证接缝；不提前建通用插件框架。M5 首版固定单 server/单 ASGI worker，不能以多 worker 或 reload 破坏 Phase 1 的单 writer/任务所有权假设。

### 单一 durable 写入路径

`SessionService/runner/工具调用器 → SessionStore.commit(events) → 同一 DB 事务内写事件和投影 → commit → Bus 通知`。

- S6 控制状态变更，S4 执行事务，S2 分发已提交事件；没有第二个 `Bus.publish → 落库` 入口。
- 事件和读模型原子提交；投影失败则整笔回滚。replay 只读日志、重建状态，不调用模型或工具。
- token delta/progress 是 ephemeral，不逐 token 落库；durable 完整值和终态负责恢复。
- commit 后通知前崩溃，通过 durable cursor 补读；Phase 1 不引入 outbox/消息中间件。
- 外部副作用不属于 DB 事务：结果不确定的工具执行标为 unknown，禁止自动重跑。

## 4. 兼容承诺

先交付 Python 的 `core 0.1` 契约。接口只有经过真/假适配器、golden fixture 和失败场景验证后，才标为该版本稳定；不是在编码前永久冻结。

1. 新增可选字段需验证旧 fixture 和旧消费者；改变必填字段、事件含义或方法签名，走 ADR、版本升级和兼容适配。
2. 给 Protocol 新增必需方法同样可能破坏实现；新能力优先用独立 capability 或新版本接口。
3. durable 事件按 `type + version` 解码；旧记录保留，使用显式 upcaster，不回写历史冒充新格式。
4. Python 内部事件使用 `py.*` 名称，行为对照 TS；不宣称与 TS server、消息 JSON 或 SQLite 文件直接兼容。未来确需 TS wire 兼容，在适配器中实现并补契约测试。
5. 固定 TS 参考版本。当前源目录没有 Git 元数据，已用 [源码指纹](source-baseline.json) 锁定本次审查依赖的 12 个文件；新增参考文件先补指纹，源码变化先评估再更新基线。

## 5. 路线与估算

按单人全职、AI 辅助、一个目标 MAAS 端点和已有 Python 异步开发经验估计，Phase 1 以 **6–8 开发周**为预算参考。功能切片约 5 周，另留 1–3 周用于平台差异、集成和修正；R0 打样后必须复估。外部端点/凭据等待另计。

`R0 风险打样 → R1 最小事件/恢复闭环 → R2 对话和一次工具调用 → R3 工具/权限/配置 → R4 MCP/故障恢复 → R5 交付验证`。

| 组合 | 粗估开发周 | 达到的能力 |
|---|---|---|
| Phase 1 | 6–8 | 本地 CLI + 同进程程序化调用 |
| Phase 1 + M5 | 8–11 | 单用户 HTTP + Python 远程客户端 |
| Phase 1 + M1 + M5 + M7 | 11–17 | 多实例能力和所选路径的进一步验证；不自动变成多租户平台 |

可选模块的估算见 [Phase 2](PHASE2-MODULES.md)。M6 按子能力单独选择；未确定 codemode 兼容范围、媒体种类和插件迁移清单前，不给“全量 TS 对齐”固定总工期。

每个模块都可以在**未选中时不实现**；这不等于已部署模块可直接卸载。安装、升级、停用、数据回退需要各模块明确迁移和兼容方案。

## 6. 技术与结构

`Python 3.12+/asyncio + pydantic v2 + SQLAlchemy/aiosqlite/Alembic + openai + 官方 mcp SDK + rich + pytest/pytest-asyncio + ruff + mypy`。

`openai`、MCP 和数据库对象留在适配器内部。配置 wire model 用 Pydantic；包含 callback、锁和运行时依赖的上下文用 dataclass/Protocol，避免把运行时服务塞进 JSON 模型。

    src/opencode/
      contracts/    # messages, events, tools, config
      ai/           # provider protocol, maas adapter
      bus/          # volatile bus, durable event feed
      tools/        # registry, invoker, policy, builtin/
      storage/      # db, private write transaction, read repos, migrations/
      config/       # discovery, merge, instructions, snapshot
      session/      # service, runner, store, projector, context builder
      mcp/          # stdio 生命周期和工具适配
      app.py        # 显式组合依赖、资源创建/关闭
      cli.py        # 只负责输入/显示/审批
    tests/
      contract/
      integration/
      security/
    doc/

## 7. 主要风险与发现时机

| 风险 | 必须何时解决 | 交付条件 |
|---|---|---|
| MAAS 流式 tool call 偏差 | R0 | 实测录制脱敏 fixture；非标准差异集中在 maas adapter |
| SQLite 事务/顺序/投影错误 | R1 | 原子提交、幂等、重建测试；Phase 1 单 writer 进程 |
| 执行副作用后丢失结果 | R2 起 | execution_id、unknown 状态、crash-window 测试 |
| shutdown/crash 竞态和清理失败 | R2/R4 | close 先持久取消、等待资源、写 unknown/终态；无法确认时 session 进入 cleanup_failed 并禁止新 run |
| shell/路径/MCP 信任错误 | 第一次接入对应能力前 | 策略、默认值、取消/输出限制和回归测试同时交付 |
| Windows 子进程/路径差异 | R0 起 | 原生 Windows CI；未验证的平台行为不得写“支持” |
| 未来模块迫使改核心 | 每个模块开始前 | 检查依赖、变更清单和 fixture；必要时升级契约 |
| 计划范围膨胀 | 每个切片结束 | 只按退出条件进入下一切片，延期时削减可选能力 |

测试和基础硬化随每个切片交付。M7 只做进一步差异、负载和长时间运行验证，不能承担补齐核心正确性的职责。
