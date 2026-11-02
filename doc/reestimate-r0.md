# R0 复估与定稿报告

## 1. R0 复估结论

本报告按 ROADMAP §5 的 R0 打样后复估要求，并对照 PHASE1-SLIM §3 的切片验收编制。

状态：R0 复估完成；R0-A 为“有条件通过”，R0-B 为“通过”。R0-A 的 fake provider、超时、取消、429、交错 tool call、终止帧和脱敏 fixture 已有证据：`doc/evidence/r0/provider.md` 记录 24 个 provider 契约用例和 7 个 fixture，`doc/evidence/r0/environment.md` 记录锁定依赖、双平台 CI、ruff、format 和 mypy 结果。真实 MAAS smoke 没有凭据，仍为“R0-A 外部证据待补”；这不阻止不依赖真实端点的 R1，但不能把 Phase 1 发布标为完成。R0-B 的解释器选择、进程树、路径 containment 和 SQLite writer lock 在 `doc/evidence/r0/platform.json`（本地 Windows，manual_verified）以及 `doc/platform-support.md` 引用的 CI run 41182347042（Ubuntu/Windows，ci_verified）中均有记录。当前本地无缓存复跑 `.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider tests/r0` 为 32 passed（24 个 provider + 8 个平台测试）。

| 闸门 | ROADMAP/PHASE1 基线 | 本次复估工作量* | 偏差判断 | 原因与证据 |
|---|---:|---:|---|---|
| R0-A provider/CI | 0.5 周（约 2.5 人日） | 2.5–3.5 人日 | 未发现超支信号 | 24 个 provider 用例、7 个 fixture、双平台 CI；httpx2 对无长度定界响应体的竞态、wire 违规拒绝和三级超时映射增加了验证工作（`provider.md` §1、§3、§5、§6）。 |
| R0-B 平台/锁探针 | 0.5–1 周（约 2.5–5 人日） | 2.5–3.5 人日 | 在基线范围内 | 进程树、symlink/junction、路径顺序、WAL/FK/busy_timeout、崩溃释放和第二 writer 无写入证据均覆盖（`platform-support.md` §5–§8，`platform.json`）。 |
| 合计 | 1–1.5 周（约 5–7.5 人日） | 5–7 人日（约 1–1.4 周） | 暂不调整 ROADMAP 基线 | 仓库没有工时台账，提交时间不能当作实际工时；上述为证据规模复估，不是“节省工时”的声明。 |

source-baseline 的实际清单是 20 项（`package.json` 加 19 个 TS 文件），不是任务文字中的 21 项；逐项 SHA-256 核验为 20/20 匹配。R0 仍未产生正式生产模块，`src/opencode/` 只有 `__init__.py`，因此 R1 不能把 spike 代码直接当作可复用实现。任务开始前工作区已有调度器的 `doc/orchestration` 修改和未跟踪产物；本任务只会新增本报告，不会用 Git 写操作清理或重置它们。

## 2. R1 定义确认与退出条件

R1 “最小事件/恢复闭环”成立，范围保持为 `doc/PHASE1-SLIM.md` §3（L65–74）定义的存储垂直闭环：最小 contracts、Alembic 0001、SessionStore、projector、只读 repo、Bus/EventLog，以及 `SessionService.create/messages`。R0-B 已满足进入 R1 的平台闸门；真实 MAAS 证据仍是发布闸门，不应把 R1 扩成 provider、工具或 MCP 实现。

| R1 退出项 | 必须交付与验证 | 失败时的结论 |
|---|---|---|
| A：同事务 | 事件、投影和 `applied_seq` 在同一事务；projector 异常整体回滚；Bus 不可见未提交事件。用 SQLite 集成测试和 projector 故障注入证明。 | 不能进入 R2；禁止用内存/JSON 旁路替代 Store。 |
| B：幂等与游标 | ID/批次幂等先于 `expected_seq`；“已提交但未收到返回”的重试返回原结果；混合旧/新 ID、内容冲突和新批次 seq 冲突明确拒绝；commit 后丢通知仍可由 cursor 分页/follow 补读。 | 不能声称可靠事件日志；需保留稳定 batch/event ID。 |
| C：重启与重建 | 空库迁移后写入用户消息，关闭再打开得到相同消息；删除读模型后按日志重建一致；未知事件版本、投影损坏、序列冲突进入只读诊断，读取可用且新写命令拒绝。 | 不得静默跳过坏事件或自动继续执行。 |

R1 的最小表不复制 TS 最终 schema。TS `packages/core/src/event/sql.ts:4–25` 只有 aggregate/seq/event 基础表，`packages/core/src/bus.ts:284–446,535–667` 也没有 Python `EventBatch`、`batch_id`、`expected_seq` 和诊断状态；这些是明确的 Python `py.*` 契约，必须以 Python fixture 和测试验收，不能写成 TS 已提供的兼容保证。

## 3. 契约待修订项与证据

以下只列因 R0 实测需要澄清或纳入后续验收的条款；不为迁就 spike 而放宽正式契约。

| 位置/现文 | R0 事实 | 建议 | 依据 |
|---|---|---|---|
| `CORE-CONTRACTS.md` §2.2 L70 的 `timeout_s`，以及 §2.3 L94 的流终止说明 | spike 把 header、chunk、total 分成不同计时边界；header 包住 `create()`，chunk 是已建立连接的 socket read，total 在 adapter 迭代层计算；这与 SSE 事件粒度并非同构。 | 补充三级超时的设置归属、计时范围和错误分类；R2 验收 header/chunk/total、取消和无终止 chunk。保留正式的 `ProviderChunk`/终止约束，不把 spike 的异常形态当最终 API。 | TS `packages/core/src/aisdk.ts:65–169`；`packages/ai/src/route/transport/http.ts:107–140`；`doc/evidence/r0/provider.md` §3。 |
| §8 L287 的 retry owner（现文要求 Retry-After、抖动、总超时和最多 2 次） | spike 最多 2 次、无 jitter、只实现 ms/秒 Retry-After，5xx 不由 adapter 自动重试；R0 fixture 只证明首内容前 429 可重试、首内容后不重试。 | 保留“有 jitter”的正式合同，把无 jitter/5xx 策略列为 R2 补齐项；补明 `retry-after-ms`、秒、HTTP-date、15 分钟上限和剩余预算的边界。不要把合同降级成 spike 行为。 | TS `packages/core/src/model-retry.ts:11–34,38–58,62–103`；`packages/ai/src/route/executor.ts:20–34`；`provider.md` §4、§6。 |
| §2.3 L90/L94 的 `error` chunk 与单终止规则 | spike 的 transport/protocol 错误通过 `CoreError` 异常上抛；重复 finish、finish 后文本/tool call 会产生 `ValidationError`，而 usage 在 finish 后仍合法。 | 正式合同继续要求最多一个 finish/error，并由 R2 统一 error chunk 或明确异常边界；增加重复终止、finish 后内容、EOF 的 fixture。TS 没有 Python `ProviderChunk` 的一对一类型，不能声称 TS 已保证这些 wire 校验。 | TS `packages/core/src/aisdk.ts:746–856`、`packages/ai/src/schema/events.ts:496–517,745–754`；`provider.md` §1、§6。 |

不因 R0 改动的边界：S4 的 OS writer lock、`unknown/effect_status`，S2 的 bounded queue/`InvalidCursor`，以及 S6 的只读诊断状态都属于 Python 设计。TS `packages/core/src/database/sqlite.bun.ts:86–94` 只显示打开 SQLite、关闭 finalizer 和 WAL；`packages/core/src/bus.ts:198–208` 的 live PubSub 是 unbounded；`packages/schema/src/session-message.ts:135–183` 没有 `execution_id`、`unknown`、`cancelled` 或 `effect_status`。这些差异应在 parity 中保持“Python-only/未验证”，而不是伪造 TS 依据。

## 4. R1–R5 切片估算基线

按 5 人日/开发周；表中“目标”是计划基线，“上限”是已知风险未解决时的复估上限。总目标 24 人日（4.8 周），略高于 ROADMAP 其余切片 4.5 周；上限 30 人日（6 周）必须由 1–3 周缓冲或缩减可选范围吸收。任一切片实际工作量超过目标 30% 即触发复估。

| 切片 | 目标 | 验收重点 | 目标/上限（人日） | 主要风险 |
|---|---|---|---:|---|
| R1 存储垂直闭环 | create → 用户事件 → 投影 → 重启查询 | 0001、同事务投影、幂等先于 seq、cursor/follow 补读、读模型重建、坏事件只读诊断 | 5 / 6 | S4 事务边界、迁移升级、projector 故障注入和诊断状态。 |
| R2 对话与一次工具调用 | CLI → provider → read → 回填 → idle | 正式 MAAS adapter、ContextBuilder、runner、read invoke、tool_call/result 配对、流中断/取消/最大步数终态 | 5 / 6 | R0 spike 不能直接搬移；error chunk、Retry-After/jitter、accepted→cancelling 竞态。 |
| R3 工具/权限/配置 | 受策略控制的本地 coding 任务 | 六个工具、路径/输出限额、Policy allow/deny/ask、审批 CAS、配置快照和受控子进程 | 5 / 6 | Windows 进程回收、symlink/junction、项目配置扩大信任、审批独立通道。 |
| R4 MCP 与故障恢复 | stdio 工具 + crash-window 恢复 | MCP 生命周期/命名空间、started 前/副作用后/finished 后重启、unknown 不重跑、慢消费者与 shutdown race | 6 / 8 | 外部副作用不可 exactly-once、MCP schema 变化、cleanup_failed、半批 generation。 |
| R5 发布基线 | 可安装 core 0.1 + 完整验证记录 | 安装/CLI、全量测试和类型检查、0001/升级/备份恢复、fixture/parity/ADR、真实 MAAS smoke | 3 / 4 | 真实端点证据仍缺、发布平台限制和旧 fixture 兼容。 |

## 5. 高风险标记与下一步

以下情况触发 A 终审，并把审查结果作为切片闸门：

| 标记 | 触发范围 | A 终审必须回答的问题 |
|---|---|---|
| A-S4 | R1 退出前 | `SessionStore.commit` 是否是唯一写入口；events/projector/applied_seq 是否原子；幂等、seq 冲突、重建和坏数据只读诊断是否有正反例。 |
| A-S6 | R1、R2、R4 | `SessionService` 是否保持单 active run；prompt/cancel/approval 是否经同一提交路径；accepted/running/cancelling、shutdown 和 idle 投影是否可重启重建。 |
| A-RECOVERY | R1、R2、R4 | commit 后丢通知、半批 generation、tool.started 后崩溃、finished 后重启、unknown 和后续 tool result 是否都不会重复副作用。 |
| A-PLATFORM | R3、R4、R5 | bash/PowerShell 是否显式选择；进程树、路径 containment、symlink/junction 和 writer lock 是否在目标平台均有证据；受限能力是否明确收缩发布范围。 |
| A-PROVIDER | R2、R5 | R0 的三级超时、Retry-After、jitter、错误 chunk 和真实 MAAS 差异是否已按 Python/TS 边界记录，是否仍禁止 provider SDK 类型越过 adapter。 |

下一步先在 R1 建立迁移 0001、EventBatch/投影 JSON fixture 和同事务故障注入，完成 A-S4 后再接入 R2；保留 R0 的 7 个脱敏 fixture 并在正式 adapter 重建时全部重跑。真实 MAAS smoke 作为外部证据单独补齐；若任何切片超过目标 30%，立即更新本表并削减可选能力，不用内存或临时 JSON 绕过持久化、权限和恢复语义。

