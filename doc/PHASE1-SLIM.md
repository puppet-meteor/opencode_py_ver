# Phase 1：带接缝的核心闭环

> 本文是执行计划；接口以 [CORE-CONTRACTS](CORE-CONTRACTS.md) 为准。
> 预算：单人全职 + AI 辅助，R0-A/R0-B 约 1–1.5 周，其余功能切片约 4.5 周，另留 1–3 周集成/修正余量，共约 6.5–9 开发周；R0 后仍需复估。
> 当前只有规划文档，以下验收项均未执行。

## 1. Done 的定义

- [ ] 安装后 `opencode` 进入 REPL；`opencode run --prompt "..."` 支持脚本调用；同进程调用使用 SessionService。
- [ ] 一个真实 MAAS 端点完成多轮文本、流式工具调用；同样场景能在 fake server 下无网络复现。
- [ ] `read/write/edit/glob/grep/ls/bash` 全经 Registry.invoke，具备输入校验、权限、输出限额、超时和取消。
- [ ] 全局/项目 JSON 和 AGENTS.md 按规定顺序加载，session 固化可重建的配置快照，凭据只存引用。
- [ ] 一个显式配置的 stdio MCP server 能列出和调用工具，断线不自动重跑有副作用的调用。
- [ ] `--continue` / `--session ID` 恢复已提交历史；未开始工具变 cancelled/not_started，已开始但未提交终态的工具变 unknown，不自动重执行。
- [ ] EventLog 支持分页、补读、Synced、follow 和慢消费者处理；Bus 不承担持久写入。
- [ ] 每个接缝都有正例/反例 fixture、行为测试和至少一个真实/fake 适配器测试。
- [ ] README 运行说明、parity 差异、ADR、迁移/备份说明齐全；ruff/mypy/pytest 和 Windows/Linux CI 通过。

可用性以“小到中等上下文的本地 coding 任务”为边界；超上下文预算明确停止。自动 compaction、skills、subagent、HTTP、插件、PTY、媒体和多 provider 留给 Phase 2。

## 2. 推进规则

- 每个切片包含正常路径、失败路径和所需平台验证；测试从 R0 起交付，R5 负责汇总交付证据。
- 下表的切片是验收里程碑；开始每个切片时再拆成一次工作会话可完成的任务，每项有输入、受影响文件和验证命令。
- 接缝用 fake adapter 和真实适配器共同验证，不依赖真实 MAAS 的偶然成功。
- 发现估算超支时削减可选能力，不能用临时内存/JSON 文件旁路替代 Store、权限或恢复语义。
- 初版可只创建当前切片需要的文件，不预建所有空目录、插件基类和未来事件。
- 切片通过后保存可回退提交；tag 只在发布检查通过后创建，数据库回退另按备份方案执行。

## 3. 任务顺序和验收

| 切片 | 基础预算 | 前置 | 结束时可演示 |
|---|---:|---|---|
| R0-A provider/CI 风险打样 | 0.5 周 | 无 | 真实/fake 流、provider 生命周期和双平台质量门 |
| R0-B 平台/锁探针 | 0.5–1 周 | R0-A | 进程树、路径、SQLite WAL/OS writer lock |
| R1 存储垂直闭环 | 1 周 | R0-B | create → 用户事件 → 投影 → 重启查询 |
| R2 对话与一次工具调用 | 1 周 | R1 | CLI → provider → read → 回填 → idle |
| R3 工具/权限/配置 | 1 周 | R2 | 受策略控制的本地 coding 任务 |
| R4 MCP 与故障恢复 | 1 周 | R3 | stdio 工具 + crash-window 恢复 |
| R5 发布基线 | 0.5 周 | R4 | 可安装的 core 0.1 和完整验证记录 |

另留 1–3 周吸收修正，不把 R5 的发布工作当作全部缓冲。

### R0：先验证不确定性（执行卡片：[STEP-01-R0.md](STEP-01-R0.md)）

R0 分两个闸门，总预算约 1–1.5 周；R0-A 通过后才能开始 R0-B，R0-B 通过后才能进入 R1。具体步骤、源码依据和证据格式以执行卡片为准。

#### R0-A：provider 和 CI

交付：最小 pyproject/锁文件/pytest/ruff/mypy/Windows+Linux CI；MAAS spike；fake OpenAI-compatible HTTP server。

- 验收 A：真实端点完成文本流、两个交错 tool call、usage/finish；记录脱敏 fixture、模型上下文上限和 provider 差异。
- 验收 B：fake server 能控制交错调用、EOF、429、header/chunk/total timeout 和取消；CI 无凭据运行。非法 JSON、重复 ID、未知工具和 schema 参数错误列入 R2 承接清单，不计入 R0-A 通过条件。
- 验证：HTTP adapter 契约测试；R0-A 失败必须先修假设或收缩 provider 范围。

#### R0-B：平台和锁探针

交付：Windows/Unix 解释器选择、进程树回收、symlink/junction、SQLite WAL/OS 文件锁探针。

- 验收：两个独立 writer 进程竞争同一数据目录锁时，第二个在写事务前明确失败；进程树取消/超时后所有 descendant 在 grace period 内退出；路径越界和 symlink/junction 逃逸被拒绝。
- 验证：平台探针和证据矩阵记录到 `doc/platform-support.md`、`doc/evidence/r0/platform.json`；R0-B 失败必须停止进入 R1。

真实 MAAS 凭据暂不可用时可继续不依赖它的 R1；Phase 1 发布仍需实际端点证据，不能以 fake 成功替代。

### R1：持久化从第一条用户消息接入

交付：最小 contracts、迁移 0001、SessionStore、projector、只读 repo、Bus/EventLog、SessionService.create/messages。

- 验收 A：事件/投影/applied_seq 同事务；projector 抛错时全回滚，Bus 收不到未提交事件。
- 验收 B：ID/批次幂等优先于 expected_seq 检查；“已提交但没收到返回”的重试成功；混合旧/新 ID、不同内容、全新批次的 seq 冲突明确拒绝。cursor 分页/follow 在 commit 后丢通知仍能补读。
- 验收 C：从空库建库并写用户消息，退出、重新打开并得到相同消息；删除读模型后从日志重建一致。未知事件版本、投影损坏或序列冲突后，session 进入只读诊断，读取仍可用且新的写命令被拒绝。
- 验证：契约 JSON fixture、SQLite 集成测试、事务故障注入；未知版本/损坏事件使会话只读诊断。查询不得触发外部调用。

先建最小表，不先建 TS 最终 schema；第一版就用 Alembic，后续升级只追加 revision。

### R2：一条 prompt 到工具结果的完整链

交付：ContextBuilder、MAAS adapter、runner、read 工具、CLI/REPL、run 任务管理和取消。

- 验收 A：fake server 下多轮文本 → tool call → read 结果 → 第二次生成 → run.finished；provider role/tool_call_id 配对正确。
- 验收 B：工具执行必须经 invoke；请求/开始/结束事件可审计；相同 request_id 不新增用户消息或重复调度。
- 验收 C：流中断、非法 JSON、未完成/未知工具 call、最大步数、上下文超限、Ctrl-C 均有明确终态和资源清理；下一次请求中每个合法 tool_call_id 都有唯一 tool_result。覆盖 accepted→cancelling、run.started 与 cancel_requested 的先提交者竞态，以及 tool_call_id/execution_id 冲突拒绝。
- 验证：fake HTTP → SDK adapter → runner → SQLite 全链集成测试。中断部分流不自动拼接新请求，测试生成次数和工具执行次数。

Phase 1 多个工具按调用开始顺序串行执行；只支持一个 active run，不实现 steering/inbox。

### R3：工具、权限与配置完成实际 coding 任务

交付：其余六个工具、Policy/审批/保存决定、ConfigLoader/快照、受控子进程环境。

- 验收 A：write/edit 精确冲突检测、原子替换；glob/grep/ls 限制数量；路径越界/超大/二进制文件有诊断；明确 rg 为外部依赖并检查可用性。
- 验收 B：allow/deny/ask、once/always/reject 和审批到期；approve 在独立控制通道处理，不被 active run 锁死；无人审批时拒绝执行。审批与 cancel/expiry 通过条件更新竞争，首个合法提交者胜出，迟到回复返回 ApprovalExpired/Conflict。
- 验收 C：shell 超时/取消收回进程树、输出限额不导致管道死锁；项目配置不能扩宿主 roots/凭据/MCP 信任；重启后按原快照恢复。
- 验证：每个工具的真实临时 workspace 集成测试 + Policy 表驱动测试 + Windows/Linux 路径/子进程测试。

权限 saved 决定使用 R1 已存在的 permissions 投影，经同一事件路径写入；不新增独立 JSON 权限文件。命令 deny 清单只能辅助提示，不是 shell 沙箱。

### R4：stdio MCP、恢复和副作用不确定性

交付：MCP manager/bridge、CLI 会话选择、启动恢复协调器、EventLog follow 的断连/慢消费者覆盖。

- 验收 A：显式信任一个 stdio server；命名空间无碰撞，text/structured 结果可回填；调用走同一 policy，生命周期退出后无子进程泄漏。
- 验收 B：覆盖 tool.started 前、外部副作用后但终态前、tool.finished 提交后的 kill/restart；前者未执行、中间 unknown、后者复用结果，均不自动重跑。generation batch 的 finalized→generated 必须原子提交；注入半批故障后只能看到完整批或没有批。
- 验收 C：待审批重启后失效，saved 决定只作审计需重新确认；相同 session 的重复 prompt、并发 cancel、慢订阅、丢通知和补读不造成重复消息/重复执行。恢复后再发新 prompt，由严格 fake server 检查每个合法 call 恰有一个 result，包含后续尚未 invoke 的计划调用。shutdown/crash race 验证 close 先持久取消和终态，再关闭 provider/MCP/DB；超时进入 cleanup_failed 或在下次启动恢复。
- 验证：测试自有的最小 MCP server + 故障注入；用计数器/临时文件证明副作用次数。MCP SDK 集成测在进程边界运行。

`--continue` 选当前 workspace 最近会话；没有则新建。`--session` 不存在报 NotFound。若选中目录/信任快照失效则停止诊断，不自动换目录。

### R5：确认 core 0.1 的交付证据

交付：README、fixture 清单、parity 实测记录、ADR 更新、发布/备份操作说明。

- 验收 A：干净环境安装后运行 CLI 和同进程示例；缺配置、解释器或 rg 时明确提示；退出码区分成功/失败/中断。
- 验收 B：全量当前测试、类型、格式/静态检查通过；空库迁移、最早可用库 fixture、备份恢复演练完成。只有 0001 时验证空库/已有 0001 no-op，出现 0002 后必须验证真实升级。
- 验收 C：六个接缝的旧 JSON fixture 可读、未知能力显式拒绝；记录已支持平台和已知限制后才标记 core 0.1 稳定。
- 验证：下节发布检查 + 实际 MAAS 短会话，证据记录到 parity；不在此时补做此前缺失的基础测试。

## 4. 必测故障矩阵

| 接缝/场景 | 必须证明的行为 |
|---|---|
| S1 部分 JSON/交错/重复 ID | 不提前执行、不猜修 JSON；完整 call/result 配对；生成计划与终态一致 |
| S2 订阅建立/重连/慢消费者 | after/Synced/InvalidCursor 语义明确；durable 从 DB 补读，ephemeral 可丢 |
| S3 权限/恶意路径/超时 | deny/未知 capability 不执行；输入审批后不可变；审批/取消 CAS；进程回收 |
| S4 回滚/重试/未知版本 | 无双写、无半投影、幂等先于 seq；冲突终态/乱序 fixture 明确拒绝，损坏会话只读 |
| S5 优先级/秘密/恢复 | 快照决定行为、凭据引用不落明文、项目不能扩大宿主信任 |
| S6 并发/取消/崩溃 | 一个 active run；短提交锁不跨外部等待；cancel_requested/终态竞争幂等；恢复不重复副作用 |
| S6 accepted-cancel / shutdown race | accepted 可转 cancelling；run.started 与 cancel_requested 以首次 durable commit 决定；close 阻止新命令、等待并持久终态，超时标 unknown/cleanup_failed |
| S1/S4 generation batch | finalized 按 ordinal 先写、step.generated 后写且同批原子；半批崩溃后不留下孤立 finalized/generated |
| S4 标识冲突/诊断状态 | session 内 execution_id 和 step 内 tool_call_id 唯一；冲突、坏事件进入只读诊断，prompt/tool/permission 写入拒绝 |
| MCP 断线/schema 变更 | 新调用使用新 catalog，旧批准失效，结果未知不自动重试 |
| 上下文/超长输出/终端 | 不截断工具配对；标记输出截断；转义危险终端控制字符 |

## 5. 运行验证

实现建立的标准命令（当前文档仓库尚不能运行）：

    python -m pytest
    python -m ruff check .
    python -m ruff format --check .
    python -m mypy src
    python -m alembic upgrade head

CI 至少有 Windows + Linux，固定测试 SDK 版本；真实 MAAS 作为单独显式 smoke，不是默认 CI 网络依赖。依赖锁文件同时约束本地和 CI。

## 6. 缩减和停点

预算超出时先移出 R4 的 MCP 扩展证明，发布为“core preview”；保留 S3 adapter 接口和工具契约。满足全部 Done 后才能称 Phase 1 完成。其次延后 REPL polish 和非关键展示能力；不推迟错误恢复、权限、取消和实际支持平台的验证。

如果 Windows 进程树回收验证失败，Windows 的 shell/MCP 执行能力不能发布为支持；继续修复或显式收缩发布平台并更新 Done/ROADMAP。CLI 纯对话成功不能替代子进程测试。

后续每次契约变更都做 fixture 回归；不要求永久冻结内部类名和模块位置。
