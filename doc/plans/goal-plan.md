# GOAL 下一阶段特性规划（goal-plan）

> 状态：规划入库件（G-12 交付，2026-11-23；用户 11-23 立项同意）。目标以 [GOAL.md](../GOAL.md) 为唯一权威表述，本文件向其对齐；冲突时以 GOAL 与用户定夺为准。
> 纪律：设计未获用户批准前不实现、不写代码。§5 的 G-01 设计稿（v1.1）是待批件。

## 1. 本文件回答什么

GOAL 定义了目标：运维与变更场景专用的智能体框架，以确定性方案（langgraph 形式）为核心资产、动态方案兜底，以历史案例的评审发布（双门绿灯）为进化回路。本文件回答：从当前代码现状到 GOAL 之间差什么、按什么顺序补、哪些是承诺、哪些不是。

不在范围：上游 TS 增量移植（见 [port-232-increments](port-232-increments.md)）、Agent Memory 长期存储（见 [memory-longterm-plan](memory-longterm-plan.md)）。

## 2. 现状与缺口（事实清单）

以下为 2026-11-23 亲读本仓源码与 evolution 规格所得，是票表的依据。行尾标注「未追」的条目开工前先查，不当作事实。

| 面 | 现状 | 对 GOAL 的缺口 |
|---|---|---|
| 运行度量 | 会话 run 的 usage 只在 step 终态（`AssistantMessage.usage/cost_usd/completed_at`；上下文压缩摘要的用量另落在 `py.context.compacted`）；`py.run.finished` 只有 `run_id/outcome/error`。引擎 `py.job.finished` 只有 `job_id/outcome/error/result/trace`；`_BudgetWatchdog._account()`（`orchestrator/workers.py`）把一次工人调用的 worker.db 里的 `py.step.finished` 用量求和成一个整数（null 记 0、不含 cache_read），只供预算裁决；`llm_call` 节点的用量全仓无落点 | 门二（耗时、tokens 开销）所需的 run 级一等字段不存在 |
| 案例与回放 | 本仓 `src/` 零 `agentrun`/`evolution` 引用；案例由测评服务以 Parquet 提供，不是本仓导出 | 核心闭环（案例→优化→双门→发布）未接线 |
| 运行态权限 | `--auto` 全放行；副作用工具默认 ask；headless 下 ask=deny 的矛盾已登记（T-C1-PT-012 §4.2）；worker 权限四条写死；管线 tool 节点如何过审批未追 | 确定性方案的「无人值守」没有可审计的权限档 |
| 触发与看守 | automation 仅 `prompt` 动作（event 触发已有）；远程批复路由已有（`/permission/{id}/reply`、`/jobs/{id}/gate/{gate_id}/reply`）；无对外通知 | 预点名方案/参数的自动触发、人工看守的通知缺位 |
| 确定性方案 | DSL 发射面仅 `tool`/`llm_call`（8 条既有技能=2 可投影+6 超出该发射面）；langgraph E2E 已打通（T-C1-LG-001） | 多数既有技能不能投影为 PipelineSpec（跨仓，G-11） |
| 指标口径 | evolution 的 gate 指标是写死枚举四项：`diagnosis_accuracy`、`routing_accuracy`、`duration_ms`、`tokens`；`diagnosis_accuracy` 对照「标注根因块」评分，非诊断类案例的质量目前不可测 | GOAL「诊断/执行准确率」里的「执行准确率」在 evolution 侧没有对应指标 |

## 3. 票表与承诺级别

| 票 | 内容 | 承诺级别 | 顺序 | 前置 | 状态（2026-11-23） |
|---|---|---|---|---|---|
| G-12 | 规划入库+ROADMAP 向 GOAL 换代 | 本阶段承诺（文档） | 1 | 无 | 已闭合 |
| G-01 | 运行度量落库 | 本阶段承诺 | 2 | 设计获批 | 设计稿 v1.1 待批（§5） |
| G-05 | 无人值守权限档 | 本阶段承诺 | 3 | 先查管线 tool 节点的审批路径 | 规划 |
| G-06 | automation `job` 动作 | 可选 | 3+ | G-05 | 规划 |
| G-03 | evolution 回放面（薄切片） | 本阶段承诺（薄切片） | 4 | 跨仓口径对齐（§6） | 规划 |
| G-10 | 案例驱动优化循环（手工一例） | 本阶段承诺（手工一例） | 5 | G-03 | 规划 |
| G-02 | 案例导出 | 按需 | 6 | G-01 | 规划 |
| G-04 | 对外通知 | 按需 | 无 | 无 | 规划 |
| G-09 | 录制回放 | 按需 | 无 | 无 | 规划 |
| G-11 | 原生 spec 覆盖面 | 按需（跨仓，DSL 仓侧） | 无 | 无 | 跨仓备忘 |
| G-07 | 变更护栏 | 不进本阶段 | 无 | 无 | 记档 |
| G-08 | 零模型确定性入口 | 不进本阶段 | 无 | 无 | 记档 |

级别含义：本阶段承诺=已排入、按序推进；可选=有余量再做；按需=出现明确需要再做；不进本阶段=只记档。

- 顺序依据：本席推荐序为 口径对齐→G-12→G-01→G-05（+G-06）→G-03 薄切片→G-10 手工一例→G-02/G-04/G-09 按需；用户已同意 G-12 与 G-01 先行，其余顺序每票开工前复核。
- D1=X（确定性方案在本仓执行）之后，「量测脊柱在两种运行时上都要做」不再成立，G-01/G-02/G-03 落本仓。
- G-07/G-08 不进本阶段：G-08（零模型入口）与用户口径「先走 opencode 选 skill、提参」不一致，且需重开裁决 14/S5b；G-07（变更护栏）暂无对应需求方。
- G-11 改按需：属 DSL 仓侧，本台账只记跨仓备忘。

里程碑：M-A 量测脊柱（G-12→G-01，G-02 按需）；M-B 运行态双引擎（G-05，G-06 可选）；M-C 评审可复现（G-03 薄切片→G-10 手工一例，G-04/G-09 按需）。

## 4. 每票一句话范围

- G-01：run 与 job 的终态事件带上 tokens 与耗时（§5）。
- G-05：为确定性方案定义无人值守的权限档——哪些工具免批、哪些仍需批复、如何审计；开工前先确定性刻画现状，再改默认值。
- G-06：automation 增加 `job` 动作，按预点名的方案与参数触发；仅在出现明确用例时做。
- G-03（薄切片）：按 evolution 的回放契约暴露最小回放入口，让 evolution 能在同一案例集上驱动本仓运行态并取回 G-01 度量；定位随 D1=X。
- G-10：用一条真实案例手工走完「回放→评估→优化→再回放」，验证闭环可复现，不追求自动化。
- G-02：把 run/job 事实导出为 evolution 案例字段（脱敏、run 边界、skill 标识）。
- G-04：对外通知（看守面）。G-09：录制回放。G-11：DSL 仓补原生 spec 覆盖面。

## 5. G-01 运行度量落库——设计稿 v1.1（待用户批准）

> v1.1（2026-11-23）按 B 席评审订正，评审结论经本席逐条亲读与实跑复核；v1 的错误与漏项见 §8。行号为本席 2026-11-23 亲读所得（路径在 `src/opencode/` 下），标「评审读到」的条目本席未复核。

目标：run 级（会话 run 与引擎 job）的 tokens 与耗时成为一等字段，能直接映射到 evolution 的案例字段（`input_tokens`、`output_tokens`、`started_at_ms`、`ended_at_ms`），为 G-02/G-03 供数，支撑 GOAL 门二。

原则：纯增量、可选字段、不改既有字段语义；遵守 [ROADMAP §4](../ROADMAP.md) 兼容承诺 1；未知不伪装成 0；用量是事实，不随结局消失。

### 5.1 字段与口径（run 与 job 共用）

- `py.run.finished` v1 的 data 增两个可选字段：`usage`（`Usage` 对象或 null）与 `duration_ms`（整数或 null）；`py.job.finished` v1 同。事件版本仍为 1。
- `duration_ms` 加 `_reject_bool` 前置校验（`contracts/events.py` 整数字段的既有写法），并要求不小于 0。v1 漏了这条：裸 `int | None` 会把 `true` 收成 1、把 `"12"` 收成 12。
- 落库与 wire 形状：事件 data 以 `model_dump(mode="json")` 全量落库（`storage/txn.py:741`），httpapi 的 SSE 也是整事件序列化（`httpapi/app.py:738`）。所以每个 finished 事件都多出 `usage`、`duration_ms` 两个键，没有度量值时是显式 `null`，与现有的 `error: null` 同形。存量记录缺这两个键，解码为 None。
- 用量口径：只含本次 run/job 自身的模型调用。不含上下文压缩摘要的用量（`py.context.compacted` 在 run 结算之后才触发，不属于任何 run）、子 agent 的用量（在子会话里另起 run，`tools/builtin/subagent.py:298`）、`cost_usd`（计价数据，多模型不可加）。标题生成的模型调用归属未追，开工首日核实，默认不计入。G-02 导出时须写明这个口径，否则「案例成本」偏小。
- 耗时口径：墙钟差，毫秒；含等待审批、暂停、门等待与停机，不另做「净执行时长」。

### 5.2 `usage` 的聚合规则

- 复用 `Usage`（`contracts/llm.py:39`，五个计数，null 表示未知），不新建平行类型；新增一个纯函数聚合器。
- 逐字段严格：某个字段对本次 run/job 的每一步都已知，才输出各步之和；只要有一步在这个字段上未知，它就是 null。五个字段全是 null 时 `usage` 整体为 null。零步（没有任何模型调用）也是 null。
- 于是 `usage` 的每个字段要么是完整的和，要么是 null。v1 的「全部分片都是 null 才保持 null」会把已知的那部分当成总量，静默少算。
- 代价（明确接受）：硬取消、崩溃后恢复、违规终止的 run，以及取消时仍有节点在飞的 job，已知的前缀用量不进终态事件（`usage` 为 null）。这些都不是成功结局，门二不对它们取值；会话 run 各步的用量仍在 step 终态消息里，需要时可另行聚合。
- 备选（未采用）：输出已知和，另加一个「未报账步数」字段。信息更多，但消费者漏看标记就会拿少算的值去过门。

### 5.3 会话 run 侧取数

聚合只读本次 run 内存里的 step 终态消息，不新增数据库读；与 `py.run.finished` 同一次提交写入。`session/runner.py` 里提交 finished 事件的构造点逐个定规则：

| 构造点 | `usage` | `duration_ms` |
|---|---|---|
| 步内致败（runner:686、695）、步数上限（runner:530） | 本次 run 全部 step 终态消息。当步取本轮生成结果上的 usage，不能用进入本步前的状态快照（这两处的 `state` 是 runner:518 读的旧快照） | 有 |
| 上下文超限（runner:550，没有 step） | null | 有 |
| 中断收尾 `_finalize_interrupted`（runner:1114） | 前序各步，加同批合成的开步消息：带已观察到的 usage 就计入，没有则该步未知（硬取消时为 null） | 有；下一行两种入口除外 |
| 启动前即取消（runner:470、484，没有 `py.run.started`） | null（零步） | null（没有起点） |
| 违规终止（runner:1240-1257，v1 漏列） | 步终态消息目前不带 usage，这一步的量因此丢失；改为带上已拿到的 usage，确实拿不到才按未知计 | 有 |
| 重启后的恢复路径（`session/recovery.py`） | 从日志折叠出的 step 终态消息聚合。合成步的 usage 恒未知，所以中断时有开步的 run 必为 null，这就是残缺值的标记方式 | 起点=原始事件 `py.run.started.created_at`，终点=本次 finished 事件时间 |

- `duration_ms` = finished 事件时间 − `py.run.started` 事件时间，两个时间都截到毫秒，与落库的 `created` 列同精度。活路径在内存里记下 `py.run.started` 的时刻，恢复路径从原始事件取，定义是同一个。runner:496 那处中断收尾发生在 `py.run.started` 已提交之后，有起点。
- 时钟回拨使差值为负时记 null（测量无效），不钳成 0。
- 事件 id 由时钟派生（runner:174），测试须用步进时钟，否则时长恒为 0。

### 5.4 引擎 job 侧取数（v1 要点 5 作废，整条重写）

事实（v1 写错的部分）：工人是真子进程，跑在自己的 worker.db 上。v1 说复用的 `_account()` 实为 `_BudgetWatchdog._account()`（`orchestrator/workers.py:498`），返回一个整数、null 记 0、不含 `cache_read`；worker.db 的路径是 `WorkerManager.__call__` 的局部变量（workers:631），`JobService.emit_finished`（`orchestrator/jobs.py:1028`）拿不到。`llm_call` 节点的用量则被直接丢弃：`orchestrator/llm_call.py:317-327` 只处理文本、错误、结束三种 chunk，不接 `UsageChunk`。

取数链：

1. 捕获：`llm_call` 节点接住流里的 `UsageChunk`；`opencode_agent` 节点在子进程退出后读 worker.db 的 `py.step.finished` 行，与 `_account()` 同一数据源，但保留五个字段与 null。db 缺失或不可读时该节点记为未知，不记 0。
2. 上报与累加：每个节点按逐字段 `Usage` 上报，按 job 累加；节点从 agent job context 拿到 job_id。
3. 透传：`emit_finished` 与 `engine._finish`（`orchestrator/engine.py:1774`）加度量参数。`done` 由图的终态经 `_finish` 传入，其余结局由服务层取累加值。

各结局的取值：

| 结局 | `usage` |
|---|---|
| `done` | 全部节点已报账，完整和 |
| `failed`（预算超限、工人失败等） | 已报账的部分。失败节点读得到 worker.db 就报账，读不到记未知，于是为 null |
| `cancelled` | 不改取消的时序，仍是 `task.cancel()` 之后立即提交终态（jobs:1271-1274）。取消时有节点在飞，该节点未报账，为 null；没有在飞节点，则是已报账的部分 |
| 门拒（jobs:1124） | 没有任何模型调用，null |
| 分支④，重启后结局不明（jobs:1348） | null |

- `duration_ms` = finished 事件时间 − job 受理时间（`JobRecord.created_epoch_ms`，job 行由首个事件折叠建立时写入，jobs:469-471）。不用 `py.job.started` 作起点：它在门批复之后会再发一次（jobs:1135）。
- 写回（分支⑤ `_write_back`，jobs:1364；v1 漏列）：`done` 的原提交没落库时，服务层从 checkpoint 重建 finished 事件。`JobCheckpoint`（jobs:591）增 `usage` 字段，图的终态把累加值写进 checkpoint 通道值，engine reader（engine.py:2007）取出；重建事件的 `usage` 取 checkpoint，`duration_ms` 用 checkpoint 自带的时间戳算。重建只依赖持久化数据，重复恢复才能逐字节一致。升级前写入的 checkpoint 没有这个通道值，重建为 null。
- 跨重启约束：重启后继续跑到终态的 job，已报账的量不能因进程内存丢失而少算。累加值须落在持久化通道里，取不到的节点记未知。

### 5.5 兼容、消费者与回滚

- 前向兼容成立：存量记录缺键解码为 None（在现模型上实跑确认）。
- 同进程重试不受影响：重试复用同一批 draft；对账比较的是含事件时间在内的整个 data 字典（`storage/txn.py:679-691`、`storage/jobs_store.py:412-420`）。
- 必做交付：重生成 `doc/reference/schema/events.json` 与 `event-envelopes.json`（`tests/contract/test_schema_lock.py` 守卫）；`doc/CORE-CONTRACTS.md` §3.1 事件表补两个事件的字段；按兼容承诺 1 跑一遍旧 fixture，并在票的说明里写明「新增可选字段」。
- 消费者（评审读到）：投影只读 outcome 与 error，jobs fold、SDK、CLI 都不读这两个事件的新键，fixture round-trip 不受影响；现有测试没有对这两个事件的 data 做整键集合断言。
- **回滚不安全**：两个 data 模型是 `extra="forbid"`，旧版本读到带新键的行会解码失败，而重放把解码失败判为存储损坏（txn:696-707；实跑确认加字段前的模型对带新键的行报 `extra_forbidden`）。换成新事件类型或新版本也一样：注册表对未知类型、未知版本抛的是同族解码错误（`contracts/events.py:1176-1177`）。所以新版本一旦写下带新键的 finished 事件，就不能直接回滚到旧版本。
- 处置办法（待批点 2）：(a) 随票交付一个幂等的剥离迁移，回滚前删掉两个事件 data 里的新键；(b) 不回滚，只前向修。按名字检索，事件行上没有批次、序号之外的哈希链或校验和（`storage/txn.py`、`jobs_store.py`、`db.py`），(a) 的代价不大；开工首日再核一遍。

### 5.6 验收（红测先行）

- 聚合器：全已知、部分未知、全未知、零步；`reasoning` 与两个 `cache_*` 逐字段刻画；bool 入参被拒。
- 会话 run：单步、多步、零步、失败、软取消、硬取消、启动前取消、违规终止、中断后恢复各一例，断言 `usage` 与 `duration_ms`；步进时钟；回拨记 null。
- 引擎 job：`llm_call` 单节点、工人单节点、混合多节点、`done`、预算超限 `failed`、取消（有节点在飞与没有）、门拒、分支④、写回（重复恢复逐字节一致）、重启续跑后 `done` 用量完整。
- 兼容：旧 fixture 解码出新字段为 None；带新键的行被加字段前的模型拒绝（钉住回滚不安全）；schema-lock 工件重生成后守卫绿。
- 变异核验：漏 `reasoning`、单位错（秒与毫秒）、取消路径漏带、写回重建丢字段、恢复路径用错起点。

### 5.7 拆批与待批点

拆批（评审建议）：会话 run 侧与引擎 job 侧各一个实现切片，可以分别批准；run 侧先开工，job 侧的规则也在本稿。

待用户一句话（台账待定夺 28）：

1. `usage` 的未知表示：逐字段严格、未知即 null（本席建议），还是已知和加「未报账步数」。
2. 回滚处置：随票交付剥离迁移（本席建议），还是不回滚、只前向修。
3. 起点口径：run 取 `py.run.started`，job 取受理时刻，含门等待与停机。是否认可。
4. 是否 run 侧先批先开工。

## 6. 跨仓对齐项（G-03 开工前完成）

1. skill 包构成（用户 11-23 定夺）：langgraph 形式的 skill=`SKILL.md`+publish 链发射的 `pipeline.opencode.json`；字段细节在 G-03 联调前与 evolution、框架仓对齐。
2. 案例字段映射：`input_tokens`/`output_tokens`←G-01 的 usage（非 null 即完整和，null 不得当 0，见 §5.2；run 之外还有压缩与子 agent 的用量，见 §5.1）；`started_at_ms`/`ended_at_ms`←run/job 起止；`status`←outcome；`model_name`←模型引用；`skill_name`←选中的 skill（G-02 范围）。
3. 回放契约：evolution 如何驱动本仓运行态（D1=X：同一网关下 opencode 选 skill、提参，langgraph 形式直接启动执行）。
4. tokens 口径：evolution 的 `tokens` 指标是否包含 reasoning 与 cache，待问 evolution 侧。
5. 准确率命名（待用户一句话，见台账待定夺 26）。分析结论：evolution 的四项 gate 指标是写死枚举，`diagnosis_accuracy` 只对带「标注根因」的诊断类案例可测；按场景分名需要逐指标阈值并改动四处枚举，而现有阈值字段是各指标共享的。建议统一一个质量准确率槽位、场景作为标签维度（分场景明细只展示），命名 `task_accuracy`；不建议用 `execution_accuracy`，它和既有的 `success_rate`/`execution_success` 撞义。前提是各场景的质量都能归一为「产出对照标注的 [0,1] 评分」，归不进去的另立非门禁约束。对本仓无阻塞：本仓只负责记事实（选中的 skill、状态、tokens、耗时）。

## 7. 风险与开放问题

- 跨仓依赖：G-03 起依赖 evolution 与框架仓的排期；G-01、G-05 不受影响，先行。
- 指标不可测：非诊断类质量在 evolution 侧没有评分器，G-10 的手工一例须选诊断类技能，或先补评分口径。
- 权限档的波及面：G-05 触及审批链默认值，必须先刻画现状再改，且保留 `--auto` 语义不回退。
- 席位容量：B/C-1 同用 glm-5.4，仅一把 key 在扛（重试与回退规则见台账 §1）。
- 回滚面：G-01 落地后写下的 finished 事件带新键，旧版本读到会判存储损坏（§5.5）；落地前须先定回滚处置。
- 规划漂移：每票闭合时刷新 §3 状态列；GOAL 变更只由用户令刷入。

## 8. 变更记录

- 2026-11-23 v1：首版（G-12）。
- 2026-11-23 v1.1：按 B 席评审订正，评审结论经本席逐条复核后改入——①要点 5 作废：`_account()` 的归属与返回形状写错，job 侧取数链整条重写（5.4）。②`usage` 改逐字段严格，部分未知不再静默少算（5.2）。③`duration_ms` 补 bool 守卫、起点定死、回拨记 null（5.1、5.3）。④补 v1 漏列的构造点：违规终止、`_write_back` 写回。⑤v1 的 Q1–Q5 原是开工后再核实，现已核实并入正文：Q1 须重生成 schema-lock 工件；Q2 有两个构造点没有 `py.run.started`；Q3 恢复聚合必为残缺；Q4 无可复用聚合（评审读到，httpapi 的 usage 输出只是压缩回执里单次调用的用量）；Q5 成立。⑥写明回滚不安全与处置（5.5）。⑦§2 现状行订正：`_account()` 的归属、压缩用量的落点、`llm_call` 无落点。
