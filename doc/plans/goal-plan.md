# GOAL 下一阶段特性规划（goal-plan）

> 状态：规划入库件（G-12 交付，2026-11-23；用户 11-23 立项同意）。目标以 [GOAL.md](../GOAL.md) 为唯一权威表述，本文件向其对齐；冲突时以 GOAL 与用户定夺为准。
> 纪律：设计未获用户批准前不实现、不写代码。§5 的 G-01 设计稿是待批件。

## 1. 本文件回答什么

GOAL 定义了目标：运维与变更场景专用的智能体框架，以确定性方案（langgraph 形式）为核心资产、动态方案兜底，以历史案例的评审发布（双门绿灯）为进化回路。本文件回答：从当前代码现状到 GOAL 之间差什么、按什么顺序补、哪些是承诺、哪些不是。

不在范围：上游 TS 增量移植（见 [port-232-increments](port-232-increments.md)）、Agent Memory 长期存储（见 [memory-longterm-plan](memory-longterm-plan.md)）。

## 2. 现状与缺口（事实清单）

以下为 2026-11-23 亲读本仓源码与 evolution 规格所得，是票表的依据。行尾标注「未追」的条目开工前先查，不当作事实。

| 面 | 现状 | 对 GOAL 的缺口 |
|---|---|---|
| 运行度量 | 会话 run 的 usage 只在 step 终态（`AssistantMessage.usage/cost_usd/completed_at`）；`py.run.finished` 只有 `run_id/outcome/error`。引擎 `py.job.finished` 只有 `job_id/outcome/error/result/trace`；`WorkerManager._account()` 从 worker.db 汇总 tokens，只供预算裁决 | 门二（耗时、tokens 开销）所需的 run 级一等字段不存在 |
| 案例与回放 | 本仓 `src/` 零 `agentrun`/`evolution` 引用；案例由测评服务以 Parquet 提供，不是本仓导出 | 核心闭环（案例→优化→双门→发布）未接线 |
| 运行态权限 | `--auto` 全放行；副作用工具默认 ask；headless 下 ask=deny 的矛盾已登记（T-C1-PT-012 §4.2）；worker 权限四条写死；管线 tool 节点如何过审批未追 | 确定性方案的「无人值守」没有可审计的权限档 |
| 触发与看守 | automation 仅 `prompt` 动作（event 触发已有）；远程批复路由已有（`/permission/{id}/reply`、`/jobs/{id}/gate/{gate_id}/reply`）；无对外通知 | 预点名方案/参数的自动触发、人工看守的通知缺位 |
| 确定性方案 | DSL 发射面仅 `tool`/`llm_call`（8 条既有技能=2 可投影+6 超出该发射面）；langgraph E2E 已打通（T-C1-LG-001） | 多数既有技能不能投影为 PipelineSpec（跨仓，G-11） |
| 指标口径 | evolution 的 gate 指标是写死枚举四项：`diagnosis_accuracy`、`routing_accuracy`、`duration_ms`、`tokens`；`diagnosis_accuracy` 对照「标注根因块」评分，非诊断类案例的质量目前不可测 | GOAL「诊断/执行准确率」里的「执行准确率」在 evolution 侧没有对应指标 |

## 3. 票表与承诺级别

| 票 | 内容 | 承诺级别 | 顺序 | 前置 | 状态（2026-11-23） |
|---|---|---|---|---|---|
| G-12 | 规划入库+ROADMAP 向 GOAL 换代 | 本阶段承诺（文档） | 1 | 无 | 已闭合 |
| G-01 | 运行度量落库 | 本阶段承诺 | 2 | 设计获批 | 设计稿待批（§5） |
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

## 5. G-01 运行度量落库——设计稿（待用户批准）

目标：run 级（会话 run 与引擎 job）的 tokens 与耗时成为一等字段，能直接映射到 evolution 的案例字段（`input_tokens`、`output_tokens`、`started_at_ms`、`ended_at_ms`），为 G-02/G-03 供数，支撑 GOAL 门二。

原则：纯增量、可选字段、不改既有字段语义；遵守 [ROADMAP §4](../ROADMAP.md) 兼容承诺 1（新增可选字段须验证旧 fixture 与旧消费者）。

设计要点：

1. 复用 `Usage`（`contracts/llm.py`，五个计数，null 表示未知），不新建平行类型。新增一个聚合函数：逐字段求和，某字段全部分片都是 null 才保持 null——未知不伪装成 0。
2. `RunFinishedData` 增可选 `usage` 与 `duration_ms`；`JobFinishedData` 同。事件版本不变（v1 加可选字段），旧记录缺字段解码为 None。
3. 聚合只读本次 run 内存中的 step 终态消息，不新增数据库读；与 `py.run.finished` 同一次提交写入。
4. 失败与取消的 run/job 也带上已发生的 usage——成本是事实，不随结果消失。
5. 引擎侧复用 `WorkerManager._account()` 的同一查询，返回逐字段 `Usage` 而不只是求和。
6. `cost_usd` 不聚合（计价数据，多模型时不可加），不进本票。

开工首日核实项（设计批准后第一步，结果可能微调实现，不改目标）：

- Q1 wire 形状：事件以 `model_dump(mode="json")` 全量落库，并对账比较（`storage/txn.py`、`storage/jobs_store.py`）。新增可选字段后输出会多出 null 键，须核实 golden fixture 与存量事件对账路径是否受影响；受影响则按兼容承诺更新 fixture 并写明「新增可选字段」。
- Q2 `duration_ms` 口径：建议=从 `py.run.started`（或 job 受理）到终态的墙钟差，含等待与暂停；是否另需「净执行时长」待定。
- Q3 恢复路径：`session/recovery.py` 对中断的 run 另行提交 `py.run.finished`，该路径的 usage 取已持久化的 step 终态聚合，取不到则保持 null。
- Q4 httpapi 已有零散 usage 输出点（`httpapi/app.py`），核对是否有可复用的聚合。
- Q5 不加投影列、不加迁移：首版度量只进事件；G-02/G-03 需要按 run 查询时再加只读查询。

验收（红测先行）：单步/多步/零步（无模型调用则 usage 为 null、`duration_ms` 不小于 0）/失败/取消/中断恢复各一例；job 侧同；旧 fixture 解码出新字段为 None；聚合函数对 null 与 `reasoning`/`cache_*` 的口径有刻画测试；变异核验（漏 `reasoning`、单位错、取消路径漏带）。契约文档事件表同步补字段。

预估：会话 run 侧与引擎 job 侧各一个实现切片。

## 6. 跨仓对齐项（G-03 开工前完成）

1. skill 包构成（用户 11-23 定夺）：langgraph 形式的 skill=`SKILL.md`+publish 链发射的 `pipeline.opencode.json`；字段细节在 G-03 联调前与 evolution、框架仓对齐。
2. 案例字段映射：`input_tokens`/`output_tokens`←G-01 的 usage；`started_at_ms`/`ended_at_ms`←run/job 起止；`status`←outcome；`model_name`←模型引用；`skill_name`←选中的 skill（G-02 范围）。
3. 回放契约：evolution 如何驱动本仓运行态（D1=X：同一网关下 opencode 选 skill、提参，langgraph 形式直接启动执行）。
4. tokens 口径：evolution 的 `tokens` 指标是否包含 reasoning 与 cache，待问 evolution 侧。
5. 准确率命名（待用户一句话，见台账待定夺 26）。分析结论：evolution 的四项 gate 指标是写死枚举，`diagnosis_accuracy` 只对带「标注根因」的诊断类案例可测；按场景分名需要逐指标阈值并改动四处枚举，而现有阈值字段是各指标共享的。建议统一一个质量准确率槽位、场景作为标签维度（分场景明细只展示），命名 `task_accuracy`；不建议用 `execution_accuracy`，它和既有的 `success_rate`/`execution_success` 撞义。前提是各场景的质量都能归一为「产出对照标注的 [0,1] 评分」，归不进去的另立非门禁约束。对本仓无阻塞：本仓只负责记事实（选中的 skill、状态、tokens、耗时）。

## 7. 风险与开放问题

- 跨仓依赖：G-03 起依赖 evolution 与框架仓的排期；G-01、G-05 不受影响，先行。
- 指标不可测：非诊断类质量在 evolution 侧没有评分器，G-10 的手工一例须选诊断类技能，或先补评分口径。
- 权限档的波及面：G-05 触及审批链默认值，必须先刻画现状再改，且保留 `--auto` 语义不回退。
- 席位容量：B/C-1 同用 glm-5.4，仅一把 key 在扛（重试与回退规则见台账 §1）。
- 规划漂移：每票闭合时刷新 §3 状态列；GOAL 变更只由用户令刷入。

## 8. 变更记录

- 2026-11-23 v1：首版（G-12）。
