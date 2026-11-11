# ADR-0001：最小核心的所有权与兼容策略

> 日期：2026-10-30。状态：已采纳；核心已实现并经 R5 收官全量验证（六切片收口；pytest 1134/mypy 109/双 ruff 绿+双 OS CI 绿链，证据=[r5-exit-evidence §8](../reference/r5-exit-evidence.md)）。core 0.1 尚未标记稳定/发布（tag 待用户定夺：ROADMAP L62 稳定门的真实端点短会话证据已闭合——T-C1-048 证据回路 2026-11-11 入库，证据=[maas-real-session](../evidence/maas-real-session.md)）；平台已知限制 15 件在册（[platform-support §9](../platform-support.md)）。状态行刷新：2026-11-11（T-C1-048：L62 稳定门证据闭合；恢复与 MCP 增量见 [ADR-0002](0002-recovery-coordinator-and-mcp-client.md)）。
> 契约：[CORE-CONTRACTS](../CORE-CONTRACTS.md)；路线：[ROADMAP](../ROADMAP.md)。

## 背景

先交付本地单用户 coding agent，避免未来接入 MCP、HTTP 或其他 provider 时重写核心。需要持久会话和可审计执行，但不需要一期实现全部 TS provider、插件、分布式存储或 UI。

主要约束是：历史消息不等于模型输入；live 通知不等于 durable 日志；SQLite 无法与 shell/文件/MCP 副作用原子提交。

## 决策

1. S6 的 SessionService 是唯一应用命令入口，持有 runner 任务、会话互斥和审批控制通道；CLI/HTTP 不直接调用工具或写 DB。
2. runner/Invoker 生成 EventDraft；SessionStore.commit 是唯一公共提交入口。S4 使用私有 TransactionWriter 完成事件、读模型、applied_seq 的同事务更新。Bus 不落库，查询 repo 不暴露第二个 append。
3. 同一 session 内分配连续 seq，事件按 type+version 解码。核心 envelope 为 id/type/version/session_id/created_at/data/metadata；durable 另有 seq，关联 ID 放 metadata。确切字段只在契约定义。
4. S2 分开 live Bus 和 EventLog。可靠消费补读 durable 日志，通知只作唤醒。一期单 writer + DB 轮询已足够，不提前加入 outbox/消息中间件。
5. 只保存完整文本/调用计划/终态作为恢复事实，token/progress ephemeral。生成完成批次按 finalized parts → step.generated 原子提交，整批 generated 调用计划提交后才开始工具，恢复时保证 call/result 配对。
6. 工具统一经过 invoke：catalog、schema、固定输入、权限、started 提交、adapter、finished 提交。pending 恢复为 cancelled/not_started；已 started 无 finished 才是 unknown，绝不自动重跑。
7. 幂等判断先于 expected_seq：完整相同 batch 返回原提交；ID 内容不同、混合旧/新 batch 拒绝；新 batch 才检查并发版本。事件重试不重跑 adapter。
8. Python core 0.1 定义自己的 py.* 事件和最小数据模型；与 TS 比较行为，暂不提供 TS wire/DB 兼容。变更需 fixture、事件版本/upcaster 和 ADR。
9. 单进程 writer 和本地受信任 workspace 是一期支持范围；M5 也固定单 server/单 ASGI worker，禁止多 worker/reload 绕过任务所有权。shell/MCP/受信任 Python 插件不被声称为沙箱。多实例、恶意代码隔离分别在有真实需求时设计。
10. run 的 accepted→cancelling 及 run.started/cancel_requested 竞态由首次 durable commit 决定；tool_call_id/execution_id 有明确作用域和唯一性。SessionStatus 对损坏/清理失败会话 fail closed，`close()` 先持久取消和未知终态，再关闭外部资源。

## 备选方案和取舍

| 方案 | 取舍 |
|---|---|
| 先内存 runner，再接恢复 | 短期快，但状态所有权和工具副作用迟早重写；改为最小持久垂直切片 |
| Bus.publish 同时负责落库/投影/通知 | 与 S6 重复写入所有权，commit 边界不清；分离提交和读取 |
| 只有 CRUD/messages，无事件日志 | 纯对话足够，但无法满足本计划的审计、执行状态和可重建要求；保留最小事件日志 |
| 完整 CQRS/outbox/多实例先做 | 一期代价大，尚无需要；只做同步投影、单 writer、durable 补读 |
| 复刻所有 TS 消息/物理表/迁移 | 扩大首版范围；改为最小契约和显式 parity，兼容适配另选 |
| 自动重跑未结束工具 | 无法判断外部效果，可能重复写/删除；保留 unknown 并由宿主重新决定 |

## 后果

- 第一个用户闭环要包含迁移/事件/投影，启动开发比内存 demo 慢；后续不再增加临时恢复旁路。
- raw 历史、有效模型上下文和展示事件可以分别演进；M5 复用 SessionService。
- 旧库升级追加 Alembic revision；回退数据库使用已验证备份，代码 tag 不是数据回退方案。
- saved 权限的作用域和恢复策略是安全语义，不能无版本说明地扩成全局 always。
- M1 只在同机多进程协调验证通过后替换独占 writer；fencing 也不能撤销外部副作用。
- 任何修改核心 schema/权限/恢复含义的后续决策，新增 ADR 引用并 supersede 本文相关段落；保留历史。
