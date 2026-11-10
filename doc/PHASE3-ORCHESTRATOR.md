# PHASE3-ORCHESTRATOR：网关编排层设计 + 代码演进切片规划

> 来源：2026-11-10 架构讨论收敛稿（OpenCode 主脑 + LangGraph 执行平面的分层设计）。
> 形式：按 `multi-agent-orchestration-guide.md` 的席位/纪律/协议/看门狗形式构建。
> 用途：作为 opencode-2.2.2-py 后续代码演进规划（PHASE1-SLIM / PHASE2-MODULES 之后的新 Phase）。
> 状态：计划（未排期）。争议裁决基准 = `doc/CORE-CONTRACTS.md` + 本文档。

---

## 0. 一句话方案

**常驻网关（orchestrator，进程内长出，扩展现有 httpapi）+ OC 主脑默认路径（方案匹配/参数提取 → workflow.run）+ 显式 skill 声明 langgraph 才直连引擎 + 工件协议（manifest 扩展/spec/task brief/job/trace）+ 两条通道（对话/控制）+ 预算看门狗。**
席位之间不直接通信：一切经网关状态机与持久化工件；智能外购自 OC 的 agent loop，自建面只做"不智能但必须可靠"的部分（路由/调度/事件/权限）。

## 1. 目标与非目标（本阶段路由规则，原文定案）

**路由规则（v1 唯一入口判定）：**

```
请求/事件进入网关：
  ① 当且仅当"用户明确指定了 skill" 且 "该 skill 的 manifest 内容声明指定 langgraph"
     → 直接走 LangGraph 引擎执行（跳过 OC 主脑）
  ② 其它一切场景
     → OC 主脑做方案匹配、参数提取，然后调用 LangGraph
```

**判定细则：**
- "明确指定"= 用户点名 skill（slash 命令 / 前端按钮 / 精确名称引用）。**参数不全视同"未明确指定"**，落回主脑路径——不做模糊匹配、不做意图分类，规则保持可枚举。
- 两条路径最终收敛到同一引擎、同一 spec 注册表；差别只在"谁做的匹配与提取"（网关规则判定 vs 主脑大模型判定）。
- 主脑路径中，主脑同样能在 skill catalog 里检索到声明了 langgraph 的 skill——主脑的"方案匹配"就是在这些注册方案中选型并填参。

**非目标（本阶段明确排除，留后续 Phase）：**
- 小模型意图分类 / 模糊路由（此前讨论过的三档路由阶梯，本阶段只做第一档的一半）；
- 主脑现场编写 ad-hoc spec（v1 引擎只执行已注册 pipeline）；
- 无 agent 直答问答路径；
- 引擎独立 worker 进程池（v1 进程内 asyncio 足够）。

## 2. 角色与席位

| 席位 | 角色 | 实现 | 会话/生命周期策略 | 典型任务 |
|---|---|---|---|---|
| 调度器=网关 | 编排+唯一状态机+唯一对外 | `orchestrator/` 模块 + httpapi 扩展 | 常驻进程，贯穿全程 | 路由判定/job 管理/事件扇出/权限门/取消传播 |
| B 席=OC 主脑 | 方案匹配+参数提取+总结 | OC 会话（sdk 驱动，`ses_` 前缀会话） | 每用户会话一个，常驻（固定会话策略，上下文累积） | 检索 catalog、选型、提取参数、调 workflow.run、结果总结 |
| 执行席=LangGraph 引擎 | 图执行平面 | 网关进程内 asyncio job（非子进程） | job 级生命周期，checkpointer 持久化 | 编译/语义门/顺序与条件执行/断点恢复 |
| C 席=OC 工人 | 节点执行体 | `runner.py` 起新 OC 会话 | **每节点执行新会话**（隔离防上下文污染），干完即回收 | 按 task brief 编码/改文件/跑门禁，交结构化返回 |
| 工具壳=workflow-mcp | 主脑的 workflow.run 工具 | 薄 MCP server（stdio） | 随主脑进程托管 | 收 spec/参数 → POST 网关 → 等 job → 返回结果 |
| A 席=用户（HITL） | 裁决者 | 前端确认卡（控制通道） | 按需弹卡 | human_gate 批准/拒绝/改参、取消、暂停 |
| 小模型席 | critique/校验/摘要节点 | 引擎内 `llm_call` 节点（低档模型） | 每节点一次性调用 | 方案评审、结果校验、摘要 |

选型逻辑（对齐 guide §1）：**大模型跑主脑与工人（关键路径），小模型跑机械面节点，路由判定零模型（规则）**。

## 3. 核心纪律（六条，全部映射自 guide §2，实测纪律平移）

1. **唯一状态机归网关**——job 表、trace、会话映射全部归网关；主脑不执行图、工人不共享主脑上下文；席位之间不直接通信，一切经网关与工件。
2. **争议以工件裁决，不商量**——spec（注册 pipeline）是智能层↔执行层唯一交接工件；语义门不过 → 回主脑修，不许跳过；skill manifest 与 pipeline 定义不符 → 拒绝注册。裁决基准 = `doc/CORE-CONTRACTS.md` + 本文档。
3. **产出必落盘**——job 记录、trace 事件、checkpoint 全部持久化（复用 bus/EventLog 已有能力）；报告正文与文件不符时以文件/存储为准。
4. **任务包走结构化工件**——每次派发给工人的是一个 task brief（§5.3），不是对话；工人第一句指令恒为"Read the task brief and execute it"的等价结构化形式。
5. **派发≠已实发，验收不采信自报**——job 状态机 + 派发后 30 秒自检（工人进程在跑/事件心跳/日志落墨）；节点完成验收以结构化返回+文件事实为准，不信正文自报。
6. **止损前置**——brief 必带 budget（max tool calls / max tokens / timeout），超限即杀并回填 job 状态；防工人无限烧钱。

## 4. 两条通道

```
对话通道：用户 ↔ 网关 ↔ OC主脑        （聊天、诉求、主脑选型提问、结果总结）
控制通道：用户 ↔ 网关 ↔ 引擎           （确认卡、取消、暂停/恢复）——不经过主脑
```

- 控制通道刻意绕开主脑：human gate 是用户与引擎之间的事；绕道主脑每道门多烧一轮大模型且可能转述走样。
- 两条通道在网关汇合，事件统一带 trace 上下文（§5.4）供前端分轨道渲染。
- HITL 复用现有 permission 模式的骨架（httpapi `/permission/{id}/reply` 同构）→ `/jobs/{id}/gate/{gate_id}/reply`。

## 5. 工件协议

### 5.1 skill manifest 扩展（本设计的唯一契约变更点）

现有 manifest 为严格 schema（未知字段拒收，见 `tests/contract/skills_fixtures/skills_manifest_entry_unknown_field.json`）。新增可选字段：

```json
{
  "id": "refactor-tests",
  "execution": "langgraph",          // 新增枚举：缺省 "default"
  "pipeline": "pipelines/refactor-tests.graph.json"   // execution=langgraph 时必填
}
```

- `execution: "langgraph"` + pipeline 引用 = 路由直连的声明依据；
- schema_lock、manifest fixtures、catalog 校验测试随切片同步更新（含 `missing pipeline`、`unknown execution` 用例）；
- pipeline 定义文件来自 agent_dsl_flow_framework 的产出（compile → semantic gate → fixture rehearsal 已在那边闭环），本仓库只消费注册产物。

### 5.2 pipeline spec（引擎输入）

v1 节点类型五件：`llm_call`（小模型）/ `opencode_agent`（工人）/ `tool`（shell/静态工具）/ `human_gate`（interrupt）/ `subworkflow`（嵌套，v1 只允许 depth ≤ 3）。带参数 schema、模型档位、budget 声明。**运行中图拓扑不可热改**——修订一律走"取消 → 修 spec → 重新提交"，checkpoint 续状态。

### 5.3 task brief（工人任务包，照 guide §3.2 解剖平移）

```
goal（一句话目标）
scope（工作目录/授权文件白名单——硬边界，越界即拒收）
context（背景摘要 + 关键文件引用）
constraints（禁止事项：git 写操作、白名单外文件）
outputs（结构化返回契约：files_changed[] / artifacts / summary / tests_passed）
acceptance（验收标准）
budget（max tool calls / max tokens / timeout —— 止损线）
```

### 5.4 job 与 trace

- job：`{id, pipeline, params, status(created/running/gated/done/failed/cancelled), depth, budget, checkpoint_ref}`；
- trace 事件复用 `contracts/events.py` parts 模型扩展：每事件带 `trace_id / span_id / parent_id / depth`；卡片协议（前端渲染契约）独立成模块，落 `contracts/`（对位 TS 原版 `packages/protocol` 的做法）。

## 6. HTTP 面（httpapi 扩展，现有 5 端点不动）

```
POST /jobs                          提交 job（pipeline_id + params）        → {id}
GET  /jobs/{id}                     状态/结果
GET  /jobs/{id}/events              SSE 事件流（卡片直播/trace 树）
POST /jobs/{id}/cancel              取消传播（网关→引擎→杀工人进程）
POST /jobs/{id}/gate/{gate_id}/reply  HITL 确认（批准/拒绝/改参）
```

命令面（v1 无新增 CLI 必要，测试用 SDK 直调；如需：`opencode job run <pipeline> --params-file`）。

## 7. 生命周期（两条路径的派发-执行循环）

```
路径②（默认）：用户消息 → 网关 → 主脑会话
  → 主脑方案匹配（检索 catalog，含 langgraph skill）+ 参数提取
  → 主脑调 MCP 工具 workflow.run(pipeline_id, params) → 挂起等待
  → workflow-mcp POST /jobs → 引擎执行
  → 完成 → tool result 回注主脑 → 主脑总结 → 用户

路径①（直连）：用户显式指定 skill（slash/按钮/点名）+ manifest 声明 langgraph + 参数齐
  → 网关 router 判定 → 跳过主脑 → 直接 POST /jobs
  → 结果写 trace/session 存储（主脑后续可读，保持"知情"）

执行期（两路径共用）：
  引擎按图执行 → opencode_agent 节点 → runner 起新工人会话喂 brief
  → human_gate → interrupt → 确认卡经控制通道直达前端 → 批/拒/改 → 继续
  → 全程事件经 SSE 直通前端卡片 → 主脑仅挂起等结果，不做执行者
```

- 取消传播：卡片取消 → 网关 → 引擎 interrupt/cancel → runner 杀工人进程，全链路单一 cancellation token；
- 恢复：checkpointer（SQLite）持久化；网关重启后 job 可续跑；主脑限额中断同会话续跑（guide §4.3 指令模板直接适用）。

## 8. 监控与故障处置（对齐 guide §7）

| 故障 | 特征 | 处置 |
|---|---|---|
| 工人超 budget | tool calls/tokens/timeout 超线 | 杀进程，job 节点标 failed，回填部分产出，回主脑决定重试或缩范围 |
| 工人卡死 | 事件心跳 1h 零增量（对齐 WATCHER 口径） | 看门狗 kill + job 标 stalled，同 pipeline 重派一次，再败上报用户 |
| 主脑限额中断 | exit 1 + truncated | 同会话续跑 + "增量写文件"指令（guide §4.3 实测模板） |
| 语义门拒绝 | pipeline 与 manifest/契约不符 | 拒绝注册/拒绝执行，4xx 回主脑修 |
| 网关重启 | job 中断 | checkpointer 恢复，gated/done 状态无损 |
| 递归套娃 | depth 超限 | 硬上限 3，超限直接拒绝提交 |

派发后 30 秒自检（对齐 guide §7.3）：工人进程在跑 / 事件心跳 / 日志落墨，三项全绿才算"已实发"。

## 9. 审查与质量门（三角色流水线，对齐 guide §8）

```
pipeline 注册（agent_dsl_flow_framework：compile → semantic gate → fixture rehearsal）
  → 人工确认卡（计划即工件：用户可改 spec → 重过门禁 → 确认）
  → 引擎执行 → 节点产出 → llm_call 校验节点（小模型）+ 结构化验收
  → 失败 → 诊断回主脑（修 pipeline/缩范围）→ 重新提交
```

- 门禁口径沿用仓库现行四件：pytest / ruff check / ruff format --check / mypy，**format 类以退出码为准**（计数漂移教训，guide §12）。

## 10. 席位经济学

| 席位 | 模型档位 | 成本策略 |
|---|---|---|
| OC 主脑 | 大模型（max） | 每会话常驻但仅对话轮次计费；路径①命中时零消耗 |
| OC 工人 | 大模型（max） | 按节点任务计费；budget 封顶 |
| llm_call 节点 | 小模型 | critique/校验/摘要，批处理廉价 |
| 路由 | 零模型 | 规则判定，可枚举可测试 |

路径①的价值 = 跳过主脑一轮大模型（延迟 + 成本），且判定零成本、零误判空间（规则可枚举）。

## 11. 模块落位与切片规划

```
src/opencode/orchestrator/          ← 新模块（命名避开 "gateway"：TS 原版该词已被
  router.py                           模型 API 网关占用，见 opencode-2.2.2 README）
  jobs.py                            skill→langgraph 直连判定（§1 规则）
  engine.py                          job 注册表/trace/checkpoint 编排
  runner.py                          图执行：编译/语义门/asyncio job/interrupt
src/opencode/workflow_mcp/            run_task(brief) → 工人会话封装/预算/结构化返回
httpapi/app.py                       薄 MCP server（stdio），主脑 workflow.run 工具
contracts/                           +/jobs 系列 5 端点（§6）
                                     + 卡片/trace 事件契约（events.py 扩展）
```

| 切片 | 内容 | 验收判据（全部要过门禁四件） |
|---|---|---|
| **S1** | job 注册表 + /jobs API + SSE trace 贯通（无引擎，fake 执行体） | POST/GET/events/cancel 端点契约测试；事件带全 trace 字段；重启后 job 状态可读 |
| **S2** | 引擎最小环：pipeline 加载 + 编译 + checkpointer + 顺序执行（节点仅 `llm_call`/`tool`） | 固定 spec fixture 跑通；断点续跑测试；语义门拒绝路径测试 |
| **S3** | runner + `opencode_agent` 节点 | brief 渲染/白名单拒收/预算杀/结构化返回四组契约测试；集成测试起真实工人会话（fake_openai_server 模式） |
| **S4** | workflow-mcp 薄壳 + 主脑默认路径（路径②） | mcp naming 契约对齐（test_mcp_naming 同口径）；e2e：消息→主脑→workflow.run→job→结果回注；`ses_` 会话前缀校验 |
| **S5** | manifest 扩展 + router 直连（路径①）+ human_gate + 取消传播 | schema_lock 更新 + manifest fixtures（valid/bad/unknown/missing-pipeline）；直连/落回两路径判定表测试；gate 弹卡-批复-续跑 e2e；取消全链 e2e |

依赖链 S1→S2→S3→S4→S5 单向；S3/S4 可在 S2 后并行（文件集零交集）。每片独立 worktree + 分支提交 + 审查 + merge，台账走 `doc/ORCHESTRATION.md` 现行流程。

## 12. 已知坑（继承 guide §12 实测 + 本设计新增风险）

- `--session` 必须 `ses` 前缀（客户端校验，秒败）——主脑/工人会话命名统一带前缀；
- opencode serve worker 的 CWD 会锁目录——工人会话干完必须回收，防 worktree 清理被锁；
- 席位日志 stdout 乱码（UTF-16）——一切以结构化返回与落盘工件为准；
- `Select-String` 默认大小写不敏感——路由判定/验收统计正则显式 `-CaseSensitive`；
- ruff format 计数漂移——门禁以退出码为准；
- 主脑在 workflow.run 工具调用里挂起时，事件/确认走控制通道直通前端——**事件不得经主脑转述**（纪律 5 的推论）；
- LangGraph 不是子进程、不是 MCP server 内嵌长任务——引擎只在网关进程内跑（语义门/彩排期除外），不出现第二个执行平面。

## 13. 复用清单（本仓库已有地基，不重建）

| 能力 | 现有模块 | 本设计角色 |
|---|---|---|
| 事件提交一致性/恢复 | `bus/` + `eventlog.py` + recovery（R4 已收） | job/trace 持久化地基 |
| 会话/存储/进程管理 | `session/` `storage/` `process/` | 工人会话生命周期 |
| 主脑驱动收敛点 | `sdk/` | v1 进程内调用；将来拆独立网关只改此层 |
| HITL 骨架 | httpapi `/permission/{id}/reply` | gate reply 同构扩展 |
| skill 目录 | `skills/catalog.py` + manifest schema | 路由判定数据源（+execution 字段） |
| MCP client | `mcp/`（stdio/catalog，test_mcp_naming 契约在） | 主脑挂 workflow-mcp 的加载通道 |
| 契约先行 | `contracts/`（events/messages/tools） | 卡片/trace 协议扩展点 |

## 14. 与现有治理体系的关系

- 本文档 = PHASE3 规划契约；排期后按 §11 切片进 `doc/plans/` 与台账，检查点链编号沿用现行 orch-N 序列；
- 争议裁决：契约文本 + 本文档行号；升级 A 席（用户）裁决；
- 现有 5 个 httpapi 端点、schema lock、全部存量契约测试不动——本 Phase 是纯增量面。

---

*文档生成于 2026-11-10，来源架构讨论：常驻网关拓扑、OC 主脑默认路径、skill 声明 langgraph 直连、进程内引擎、两条通道、预算看门狗。*
