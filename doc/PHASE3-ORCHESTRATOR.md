# PHASE3-ORCHESTRATOR：网关编排层设计 + 代码演进切片规划（v2）

> 版本：v2.1，2026-11-10。v1 = 同日架构讨论收敛稿（43e3040）；v2 = orch-178 三席评审收敛——A 席四分叉终裁（T-A-015）+ B 席结构化审查（T-B-068：blocker 1/major 9/minor 7/nit 3）+ C-1 席实现核验（T-C1-059：12 条名不副实 + S1 四项隐藏前置）；v2.1 = 同日用户定夺 rider：路径①拆为可裁剪尾件 S5b（§1 实施顺位 + §11 S5a/S5b 分行）。逐项修数见附录 A/B。
> 形式：按 `multi-agent-orchestration-guide.md` 的席位/纪律/协议/看门狗形式构建。
> 用途：作为 opencode-2.2.2-py 后续代码演进规划（PHASE1-SLIM / PHASE2-MODULES 之后的新 Phase）。
> 状态：计划 v2（评审收敛，待排期）。争议裁决基准 = `doc/CORE-CONTRACTS.md`（下称 CC）+ 本文档。

---

## 0. 一句话方案

**常驻网关（orchestrator，进程内长出，扩展现有 httpapi）+ OC 主脑默认路径（方案匹配/参数提取 → `workflow.run` 原生工具 submit/poll）+ 显式 skill 声明 langgraph 才直连引擎（四条件严格判定，S5b 可裁剪尾件）+ 工件协议（manifest 扩展/pipeline spec/task brief/py.job.* 事件族/trace）+ 两条通道（对话/控制）+ 预算看门狗 + 工人子进程隔离（每 job 独立 worktree）。**
席位之间不直接通信：一切经网关状态机与持久化工件；智能外购自 OC 的 agent loop，自建面只做"不智能但必须可靠"的部分（路由/调度/事件/权限）。

## 1. 目标与非目标（路由规则，用户定案原文保留）

**路由规则（唯一入口判定）：**

```
请求/事件进入网关：
  ① 当且仅当"用户明确指定了 skill" 且 "该 skill 的 manifest 内容声明指定 langgraph"
     → 直接走 LangGraph 引擎执行（跳过 OC 主脑）
  ② 其它一切场景
     → OC 主脑做方案匹配、参数提取，然后调用 LangGraph
```

**直连判定四条件（A 席终裁 fork-4 + B 席 B-1 修复，全部满足才走路径①）：**

1. 请求来自**结构化显式入口**：slash `/skill-id`、前端按钮提交的 `skill_id`、保留语法 `skill:<canonical-id>`、或明确标记的精确 canonical ID 引用；
2. `skill_id` 存在于当前 manifest；
3. manifest 为 `execution: "langgraph"` 且 pipeline 引用存在；
4. 请求携带**结构化 params 对象**，且经网关按 pipeline 参数 schema 严格验证：字段完整、类型正确、无未知字段（网关零模型可执行的 JSON Schema 校验）。

裸自然语言中出现 skill 显示名或 canonical name——即使完全相等——一律路径②；纯自然语言、缺结构化 params、未知 skill、错误 execution、缺 pipeline、未知字段或类型错误 → 路径②，原请求+可识别 hint 交主脑。不做模糊匹配、不做意图分类，规则可枚举可测试。

**直连信封（v2 新增定义）：** 至少含 `skill_id`、canonical `params`、`conversation_anchor`；网关注入 `session_id`、`request_id/idempotency_key`、`manifest_hash/pipeline_version`、`trace_id`。有用户会话的 job 必绑 session anchor，结果与 trace 写 job 投影供主脑后续读取（知情不经转述）；无会话 API 显式标注 `origin=api`，经 job 端点取结果。前端只能预校验；主脑原生工具与 router 共用同一 gateway validator 并再次校验。

**实施顺位（v2.1，用户 2026-11-10 定夺）：路径①为可裁剪尾件（S5b）**——功能上路径②完全覆盖直连（主脑同样检索 catalog、选型、填参、提交），路径①的全部价值 = 省一轮大模型（延迟 + 成本），属纯优化项。S1–S4 全量走路径②先跑通；S5b（router+信封+真值表）是否落地，待 S1–S4 运行数据（直连命中率/主脑绕行成本）与用户届约定夺。本节四条件门为 S5b 完整规格，保留不删。

**非目标（本阶段明确排除，留后续 Phase）：** 小模型意图分类/模糊路由；主脑现场编写 ad-hoc spec（v1 引擎只执行已注册 pipeline）；无 agent 直答问答路径；引擎独立 worker 进程池（v1 进程内 asyncio 足够）。

## 2. 角色与席位（运行时角色去字母化——字母席位保留给开发期治理体系）

| 运行时角色 | 职责 | 实现 | 会话/生命周期策略 | 典型任务 |
|---|---|---|---|---|
| 网关（orchestrator） | 编排+唯一状态机+唯一对外 | `orchestrator/` 模块 + httpapi 扩展 | 常驻进程，贯穿全程 | 路由判定/job 管理/事件扇出/权限门/取消传播 |
| OC 主脑（brain） | 方案匹配+参数提取+总结 | OC 会话（`BrainDriver` 驱动，`ses_` 前缀） | 每用户会话一个，常驻（上下文累积） | 检索 catalog、选型、提取参数、submit job、poll 结果、总结 |
| LangGraph 引擎（engine） | 图执行平面 | 网关进程内 asyncio job（非子进程） | job 级；checkpointer 仅 advisory cursor | 编译/语义门/顺序与条件执行/断点恢复 |
| OC 工人（worker） | 节点执行体 | **独立 `opencode run` 子进程 + 每 job 独立 worktree**（A 终裁 fork-2） | 每节点起子进程，干完回收；dirty 留置诊断 | 按 task brief 编码/改文件/跑门禁，交结构化返回 |
| 用户（HITL） | 裁决者 | 前端确认卡（控制通道） | 按需弹卡 | human_gate 批准/拒绝/改参、取消、暂停 |
| 小模型节点（llm_call） | critique/校验/摘要 | 引擎内低档模型节点 | 每节点一次性调用，档位入 config 快照 | 方案评审、结果校验、摘要 |

选型逻辑（对齐 guide §1）：**大模型跑主脑与工人（关键路径），小模型跑机械面节点，路由判定零模型（规则）**。
v1 的 workflow-mcp 薄壳角色**取消**（工具形态终裁为原生工具，见 §7 与附录 A-g）；MCP client 保留为未来跨进程边界的挂载面。

## 3. 核心纪律（六条，全部映射自 guide §2，实测纪律平移；v2 修订第 1/3/6 条）

1. **唯一状态机归网关**——job 读模型、trace、会话映射全部归网关；**job 状态唯一事实源 = 网关 EventLog + job 关系投影**（A 终裁 fork-3）；LangGraph checkpointer 只存图执行游标与图状态，不对外声明 job 状态；主脑不执行图、工人不共享主脑上下文。
2. **争议以工件裁决，不商量**——spec（注册 pipeline）是智能层↔执行层唯一交接工件；语义门不过 → 回主脑修，不许跳过；manifest 与 pipeline 定义不符 → 拒绝注册。裁决基准 = CC + 本文档。
3. **产出必落盘**——job/trace 走 **py.job.\* 新事件族**（按 CC §3.1 注册纪律：类型注册、version、正/反例 fixture 落地），**流键 = 每 job 一个合成 `ses_job_*` 会话**（CC 四前缀纪律下零契约改动，C 核验最小路径）；报告正文与文件不符时以文件/存储为准。
4. **任务包走结构化工件**——task brief（§5.3），工人第一句指令恒为"Read the task brief and execute it"等价结构化形式。
5. **派发≠已实发，验收不采信自报**——job 状态机 + 派发后 30 秒自检（工人进程在跑/事件心跳/日志落墨）；节点完成验收以结构化返回+文件事实为准，不信正文自报。
6. **止损前置**——brief 必带 budget（max tool calls / max tokens / timeout），超限即杀 worker 进程树并回填 job 状态；**max-tokens 超限执法为新建面**（现仓 Usage 只记账不执法，C#11），S3 交付。

## 4. 两条通道

```
对话通道：用户 ↔ 网关 ↔ OC主脑        （聊天、诉求、主脑选型提问、结果总结）
控制通道：用户 ↔ 网关 ↔ 引擎           （确认卡、取消、暂停/恢复）——不经过主脑
```

- 控制通道刻意绕开主脑：human gate 是用户与引擎之间的事；绕道主脑每道门多烧一轮大模型且可能转述走样。
- **统一事件信封（v2 新增，M-6 修复）**：`trace_id + job_id + 关联 session_id` 字段族；前端按 trace_id 关联 `/session/{id}/events` 与 `/jobs/{id}/events` 双流；human_gate 卡片事件必须携带触发对话上下文（conversation ref）。
- HITL 参照 permission 骨架（httpapi `/permission/{id}/reply` 形制），但 **gate reply 枚举 = approve / reject / revise（改参）**，非 once/always/reject 同构直用（C#12）；`gate_id` job 内唯一，超时复用 expires_at 300s 先例（m-3）。

## 5. 工件协议

### 5.1 skill manifest 扩展（唯一"既有契约字段级"变更——M-3 口径修正）

现有 manifest 为严格 schema（未知字段拒收，见 `tests/contract/skills_fixtures/skills_manifest_entry_unknown_field.json`）。新增可选字段：

```json
{
  "id": "refactor-tests",
  "execution": "langgraph",
  "pipeline": "pipelines/refactor-tests.graph.json"
}
```

- `execution` ∈ {default, langgraph}；=langgraph 时 `pipeline` 必填；**pipeline 引用路径基准 = skill 目录相对路径**（m-1）；
- graph.json 须带 `schema_version` 字段，注册期校验 + 哈希锁定（本仓引擎解析外部仓产物，防御面 m-1）；
- 改动面：catalog.py `_ENTRY_KEYS` + `SkillEntry` + 条件校验 + 3 个新夹具（valid-langgraph / missing-pipeline / unknown-execution）+ 测试清单 + **CC §6.3 六字段表修订**；
- **manifest 面不触 schema lock**（生成器只收 contracts 五模块，C#7）；schema-lock 的真实解锁点在 S1 的 py.job.* 事件族；
- 口径修正（M-3）：manifest 扩展是唯一的**既有契约字段级**变更；events.py 的 trace 扩展与卡片协议是**事件面新增**（走 CC §3.1 注册纪律）；/jobs 端点为纯新增 HTTP 面。

### 5.2 pipeline spec（引擎输入）

v1 节点类型五件：`llm_call`（小模型）/ `opencode_agent`（工人）/ `tool`（shell/静态工具）/ `human_gate`（interrupt）/ `subworkflow`（嵌套，depth ≤ 3）。带参数 schema、模型档位、budget 声明。**运行中图拓扑不可热改**——修订一律走"取消 → 修 spec → 重新提交"，checkpoint 续状态。

**工具级递归守卫（M-9 修复，A fork-1）**：job 提交携带 lineage（父 job_id / 发起会话 id）；网关检测到发起方为工人身份或祖先链超限 → 拒绝提交；depth 上限为第二道防线。

### 5.3 task brief + 工人启动协议（A 终裁 fork-2）

brief 七段不变：goal / scope（工作目录/授权文件白名单——硬边界，越界即拒收）/ context / constraints（禁止事项：git 写操作、白名单外文件）/ outputs（结构化返回契约：files_changed[] / artifacts / summary / tests_passed）/ acceptance / budget（max tool calls / max tokens / timeout——止损线）。

**工人启动协议（v2 新增）**：从干净 immutable ref 创建 job worktree → 以该 worktree 为 CWD 与唯一允许 root → 注入最小 `ConfigSnapshot/PermissionPolicy`（roots 适配器边界 + host deny + `register_saved` 预置三件 = **前摄拦截**，白名单拒收为第二道验收）/ → 关闭 `workflow.run` capability → 记录 pid、worktree、brief hash 和 budget。取消或超时**杀整棵进程树**（`run_command` 的 Windows JobObject / Unix process group 原语）；成功 job 验收后转移/合并产出再删 worktree；失败、取消或副作用未知的 dirty worktree **不得 force remove**，保留或隔离供诊断。

### 5.4 job 与 trace（M-2/M-4 修复）

- job：`{id, pipeline, params, status, depth, budget, checkpoint_ref, retry_count, attempt_id, stalled_reason, lineage}`；
- **状态闭集扩为**：`created / running / stalled / gated / done / failed / cancelled`（A blocker-5 + M-2）。迁移：`running→stalled`（看门狗，记 stalled_reason）；`stalled→running` 仅允许一次重派；`stalled→failed/cancelled`；重派语义 = **失败节点级重试**，非整图重跑（m-4）；
- 事件：**py.job.\* 事件族**（jobs 读模型投影对位 0002_runs 先例：表 + 投影 + fold + rebuild 回填）；流键 = 合成 `ses_job_*` 会话；每事件带 `trace_id / span_id / parent_id / depth`；卡片协议（前端渲染契约）独立成模块落 `contracts/`。

## 6. HTTP 面（httpapi 扩展，**现有 7 路由冻结**——C#1 修正）

```
POST /jobs                            提交 job（pipeline_id + 结构化 params）  → {id}
GET  /jobs/{id}                       状态/结果
GET  /jobs/{id}/events                SSE 事件流（卡片直播/trace 树）
POST /jobs/{id}/cancel                取消传播（网关→引擎→杀 worker 进程树）
POST /jobs/{id}/gate/{gate_id}/reply  HITL 确认（approve/reject/revise）
```

- 现有 7 路由（app.py L521-586：session/prompt/cancel/status/messages/events/permission-reply）不动；
- `_is_sse_request` 结构化匹配增加 `('', 'jobs', id, 'events')`——否则 job 流吃 8 槽请求预算，8 条挂起 job 流即可饿死全部命令路由（C 任务 1）；
- `Last-Event-ID` 解析泛化为"流键:seq"（流键 `ses_` 形，零契约改动）；`_sse_frames`/keepalive/单帧上限原样复用；
- POST /jobs 参数校验失败的 4xx 错误契约与 SSE 流鉴权入 S1 验收（m-2）；
- 命令面：v1 无新增 CLI 必要，测试用 SDK 直调。

## 7. 生命周期（M-5 修复：workflow.run = submit/poll 两段式）

```
路径②（默认）：用户消息 → 网关 → 主脑会话
  → 主脑方案匹配（检索 catalog，含 langgraph skill）+ 参数提取
  → 主脑调原生工具 workflow.run(pipeline_id, params)
    【submit：网关验证 + 建 job，立即返回 job_id——工具调用不阻塞】
  → 挂起期用户追问由主脑按 job 状态应答
  → 主脑以 workflow.status(job_id) 轮询/事件感知完成 → 结果回注 → 主脑总结 → 用户

路径①（直连）：四条件全命中（§1）
  → 网关 router 判定 → 直接建 job（信封字段齐）
  → 结果与 trace 写 job 投影 + session anchor（主脑后续可读，保持"知情"）

执行期（两路径共用）：
  引擎按图执行 → opencode_agent 节点 → workers.py 起子进程 + job worktree + 喂 brief
  → human_gate → interrupt → 确认卡经控制通道直达前端（带 conversation ref）→ 批/拒/改 → 继续
  → 全程事件经 SSE 直通前端卡片
```

- **提交顺序（A fork-3）**：网关先提交 job created/spec/params/hash/pipeline/session anchor 事件 → 成功后才启动 LangGraph；节点结果经结构化返回、工件和 git 事实验证后，网关再提交 job 状态与结果事件；稳定 `job_id/node_execution_id/idempotency_key` + lease + 幂等状态迁移 + 补偿；
- 取消传播：卡片取消 → 网关 → 引擎 interrupt/cancel → 杀 worker 进程树，全链路单一 cancellation token；
- 主脑限额中断：同会话续跑 + "增量写文件"指令（guide §4.3 实测模板）；submit/poll 形态下**不存在孤儿工具调用**（工具调用即返回 job_id）。

## 8. 监控与故障处置（对齐 guide §7；含恢复矩阵）

| 故障 | 特征 | 处置 |
|---|---|---|
| worker 超 budget | tool calls/tokens/timeout 超线 | 杀进程树，job 节点标 failed，回填部分产出，回主脑决定重试或缩范围 |
| worker 卡死 | 事件心跳 1h 零增量 | `running→stalled`（记 stalled_reason/attempt_id）→ 失败节点级重派一次 → 再败 `stalled→failed` 上报用户 |
| 主脑限额中断 | exit 1 + truncated | 同会话续跑 + 增量写文件指令 |
| 语义门拒绝 | pipeline 与 manifest/契约不符 | 拒绝注册/拒绝执行，4xx 回主脑修 |
| 网关重启 | job 中断 | **恢复矩阵（A fork-3）**：先取网关恢复所有权并查 host 状态——host terminal 优先（残留 checkpoint 不得复活 job）；host nonterminal 且 checkpoint/spec hash 匹配才恢复；host created 且无副作用可重新启动；checkpoint 缺失/损坏/副作用未知 **fail closed**；checkpoint 领先 host = 未确认事实，网关验证后补写幂等 host 事件 |
| 递归套娃 | lineage 超限 / 工人身份提交 | 网关拒提交 + depth 硬上限 3 第二道防线 |

派发后 30 秒自检（对齐 guide §7.3）：工人进程在跑 / 事件心跳 / 日志落墨，三项全绿才算"已实发"。

## 9. 审查与质量门（三角色流水线，对齐 guide §8）

```
pipeline 注册（agent_dsl_flow_framework：compile → semantic gate → fixture rehearsal）
  → 人工确认卡（计划即工件：用户可改 spec → 重过门禁 → 确认）
  → 引擎执行 → 节点产出 → llm_call 校验节点（小模型）+ 结构化验收
  → 失败 → 诊断回主脑（修 pipeline/缩范围）→ 重新提交
```

门禁口径沿用仓库现行四件：pytest / ruff check / ruff format --check / mypy，**format 类以退出码为准**（计数漂移教训，guide §12）。v1 引用的 "test_mcp_naming 契约" 删除（原生工具形态下无 MCP wire 映射面，C#6 随工具形态终裁消解）。

## 10. 席位经济学

| 角色 | 模型档位 | 成本策略 |
|---|---|---|
| OC 主脑 | 大模型（max） | 每会话常驻但仅对话轮次计费；路径①命中时零新增消耗（常驻会话存量上下文不产生新消耗，n-2） |
| OC 工人 | 大模型（max） | 按节点任务计费；budget 封顶 |
| llm_call 节点 | 小模型 | critique/校验/摘要，批处理廉价 |
| 路由 | 零模型 | 规则判定，可枚举可测试 |

路径①的价值 = 跳过主脑一轮大模型（延迟 + 成本），且判定零成本、零误判空间（规则可枚举）。

## 11. 模块落位与切片规划（M-1 修复：文件与描述一一对应）

```
src/opencode/orchestrator/          ← 新模块（命名避开 "gateway"：该词在 TS 生态常指
                                       模型 API 网关，多义易混；v1 所引 README 出处
                                       未核到，降为一般性命名理由，B m-6）
  router.py                         直连四条件判定（§1）
  jobs.py                           job 注册表/状态机 + py.job.* 事件族 + jobs 读模型投影
  engine.py                         LangGraph 进程内执行 + checkpointer adapter（spec hash/lease/幂等）
  workers.py                        工人子进程生命周期（run + worktree + 预算杀 + 结构化返回）
  brain_driver.py                   主脑驱动 seam（v1=进程内 SessionService 直调；v2=OpencodeClient）
src/opencode/tools/workflow.py      原生 workflow.run（submit）+ workflow.status（poll）工具；
                                     仅注入主脑的 ToolRegistry 实例（每 agent 独立注册表，C#5 最小路径）
httpapi/app.py                      + /jobs 5 路由（§6）+ _is_sse_request 扩展
contracts/                          + py.job.* 事件定义 + 卡片/trace 契约
```

注：sdk 现为**同步 urllib HTTP 客户端**（client.py L202）——BrainDriver v1 实现落在既有进程内件（SessionService + EventLog.follow + `_wait_terminal` 轮询模式）；若 v2 走 OpencodeClient，asyncio 网关内须 executor/线程包裹（C 任务 4）。

| 切片 | 内容 | 验收判据（全部要过门禁四件） |
|---|---|---|
| **S1** | job 注册表 + /jobs API + SSE trace 贯通（fake 执行体）——**隐藏前置显式化（C 任务 8）**：0003_jobs 迁移；py.job.* 事件族 + fold + 投影 + **schema-lock 解锁**（PINNED 计数/夹具 census 同步——真实解锁点在此，非 S5）；`_is_sse_request` 扩展；`ses_job_*` 流键；stalled 状态闭集；重启恢复矩阵测试 | 端点契约测试（含 4xx 形状）；事件带全 trace 字段；重启后 job 状态可读且恢复矩阵五分支覆盖 |
| **S2** | 引擎最小环：pipeline 加载 + 编译 + checkpointer + 顺序执行（节点仅 `llm_call`/`tool`）——**LangGraph 依赖加录 + 版本锁定 + CI 面（C#9）** | 固定 spec fixture 跑通；断点续跑测试；语义门拒绝路径测试；双库边界（job 表事实源）断言 |
| **S3** | workers.py + `opencode_agent` 节点：子进程 + worktree 生命周期、前摄拦截三件、预算杀三件（**max-tokens 执法新建**，C#11）、结构化返回 | brief 渲染/白名单前摄拒收/预算杀/结构化返回四组契约测试；集成测试起真实工人会话（fake_openai_server 模式）；取消杀进程树断言 |
| **S4** | brain_driver + 原生 workflow 工具 + 主脑默认路径（路径②） | 每 agent 注册表实例（工人 catalog 不可见 workflow.run）+ 递归提交拒绝测试；e2e：消息→主脑→submit→poll→结果回注（fake_openai_server） |
| **S5a（必建）** | manifest 扩展 + human_gate + 取消传播 | manifest 夹具（valid/bad/unknown/missing-pipeline）；gate 弹卡-批复-改参-续跑 e2e；取消全链 e2e |
| **S5b（可裁剪，待定夺）** | router 直连（路径①）+ 直连信封 + 前端预校验契约 | 直连/落回**四条件真值表**测试（slash/按钮/保留语法/裸 NL/缺参/未知 skill/错误 execution/extra params）；动工前置 = S5a + S1–S4 运行数据 + 用户届约定夺 |

依赖链 S1→S2→S3/S4→S5a 单向；S3/S4 可在 S2 后并行（文件集零交集）；S5b 依赖 S5a，另需 S1–S4 运行数据与用户届约定夺方可动工（v2.1）。每片独立 worktree + 分支提交 + 审查 + merge，台账走 `doc/ORCHESTRATION.md` 现行流程。

## 12. 已知坑（继承 guide §12 实测 + 本设计新增；v2 修订）

- `--session` 必须 `ses` 前缀（客户端校验，秒败）——主脑/工人/合成 job 流键统一带前缀；
- worker CWD 锁目录——子进程 + 独立 worktree 模型下，worktree 随 job 回收自然消解（n-3）；dirty worktree 留置不强删；
- 席位日志 stdout 乱码（UTF-16）——一切以结构化返回与落盘工件为准；
- `Select-String` 默认大小写不敏感——路由判定/验收统计正则显式 `-CaseSensitive`；
- ruff format 计数漂移——门禁以退出码为准；
- 主脑在 workflow 工具调用期间，事件/确认走控制通道直通前端——**事件不得经主脑转述**（纪律 5 的推论）；
- LangGraph 不是子进程、不是 MCP server 内嵌长任务——引擎只在网关进程内跑（语义门/彩排期除外），不出现第二个执行平面；
- sdk 同步 urllib 在 asyncio 网关内会阻塞事件循环——BrainDriver v1 进程内直调规避；v2 HTTP 形态须 executor/线程包裹（新增）。

## 13. 复用清单（v2 名实对齐——按 C-1 席 12 条核验修正）

| 能力 | 现有模块 | v2 角色与真实边界 |
|---|---|---|
| durable SSE 机器（Last-Event-ID/keepalive/帧上限/错误帧） | httpapi app.py L243-368 | **真实可复用**（C 正面确认）；job 流只需 `_is_sse_request` 扩展 + 流键泛化 |
| 事件读/follow 面 | bus/eventlog.py | 可复用，前提 = `ses_` 形流键 + 已注册类型；**写面 = py.job.* 新事件族 + fold + 投影 + 迁移的真实工程量**（C#3） |
| 进程原语（超时/输出封顶/杀树/取消回调） | process/runner.py `run_command` | 预算看门狗原语真实；**工人会话生命周期 = workers.py 全新**（C#4） |
| 会话/存储/迁移 | session/ storage/ alembic | 0003_jobs 迁移走既有启动迁移通道（真实） |
| 主脑驱动 | sdk/（同步 HTTP 客户端） | **BrainDriver 为新建收敛层**；v1 "进程内调用"口径废弃（C#2） |
| MCP client | mcp/（stdio/catalog） | 保留为未来跨进程 adapter 的挂载面；v1 无 workflow-mcp |
| skill 目录 | skills/catalog.py + manifest schema | 路由判定数据源（+execution/pipeline 两字段，真实） |
| HITL 骨架 | permission/approval 面 | 形制参照；reply 枚举须扩 approve/reject/revise（C#12） |
| 测试模式 | fake_openai_server（r0 先例） | S3/S4 e2e 基座（真实） |
| 启动恢复门 | recovery_gate | job 恢复扫描对位此形态新增（C#10） |

## 14. 与现有治理体系的关系

- 现有 **7 路由**、全部存量契约测试不动；**schema lock 于 S1 因 py.job.* 事件族解锁一次**（PINNED 计数/夹具 census/信封元组同步）——v1 "schema lock 不动/纯增量面" 口径废弃（M-3/C#7）；
- **双库双 writer 域**（job 库与 session 库分属两个 writer 域，M-7 对齐 CC §3.2 单 writer 纪律）：job 表 = 事实源，checkpointer DB = 图内部执行状态；
- 排期后按 §11 切片进 `doc/plans/` 与台账，检查点链编号沿用现行 orch-N 序列；争议裁决：契约文本 + 本文档行号，升级治理 A 席（用户）裁决。

---

## 附录 A · 深层问题与处置（8 项：B 席裁决 × A 席终裁 × v2 落点）

| # | 深层问题（调度器预判） | B 席裁决 | 终裁与 v2 落点 |
|---|---|---|---|
| a | §1 直连参数来源未定义 | **确认，升 blocker（B-1）** | 直连条件 4：结构化 params + 网关 JSON Schema 严格校验（§1）；POST /jobs 契约同步 |
| b | 主脑挂起语义未定义 | 确认（M-5 三连缺：超时/孤儿/追问） | **submit/poll 两段式**（B 建议，与 A 工具形态兼容）：工具调用即返回 job_id，孤儿问题消解；挂起期追问由主脑按 job 状态应答（§7） |
| c | 双 SSE 端点关联协议未定义 | 确认（M-6） | 统一信封 trace_id + job_id + 关联 session_id；gate 卡带 conversation ref（§4） |
| d | 持久化双写边界未定义 | 确认（M-7，另涉 CC §3.2 单 writer 相抵） | **A fork-3 终裁**：job 表 = 事实源 / checkpointer = advisory cursor + 五分支恢复矩阵；双 writer 域显式声明（§5.4/§8/§14） |
| e | 工人隔离与前摄权限缺失 | 确认（M-8 两子项） | **A fork-2 终裁**：独立子进程 + 每 job worktree；前摄拦截 = roots + host deny + register_saved 三件（§5.3） |
| f | 工具级递归守卫缺失 | 确认（M-9） | lineage 检测 + 工人身份拒提交 + depth 第二道（§5.2/§8） |
| g | 形态 B 下 workflow-mcp 是否多余 | **部分反驳（倾向保留薄壳）**，终局交 A | **A fork-1 终裁：原生工具**——同进程内部调用再包 MCP = 重复 transport/生命周期/取消/未知结果边界；B 成本论点（MCP client 既有挂载面）记录在案；**翻案条件（A）**：主脑与引擎拆独立进程 / 跨语言远程调用 / 需稳定跨进程协议时改 MCP adapter |
| h | 运行时席位命名与治理体系混淆 | 确认（用户点名） | §2 运行时角色去字母化（网关/主脑/引擎/工人/用户/小模型节点）；字母席位保留给开发期治理体系 |

## 附录 B · 三席评审与闭合记录（orch-178，2026-11-10）

- **A 席 T-A-015**（GPT-6.1，12:09 完成）：四分叉终裁 + blocker-5（stalled 闭集缺口）——全文 95L 存 `doc/orchestration/logs/T-A-015-final.txt`；四项 §11 影响面已并入 v2 切片表。
- **B 席 T-B-068**（GLM-5.3#max，12:36 完成）：blocker 1/major 9/minor 7/nit 3；8 项预判 a-f/h 确认（a 升 blocker）、g 部分反驳；4 项未决建议升 A——**全部闭合**：#1 工具形态与 #4 工人进程 = A fork-1/fork-2 直接终裁；#2 job 事件域 = CC 四前缀纪律下取合成 `ses_job_*` 流键（零契约改动，C 方案 A）；#3 主脑挂起 = submit/poll。报告 161L 存 `doc/reviews/PHASE3-ORCHESTRATOR-review.md`。
- **C-1 席 T-C1-059**（GLM-5.3#max，11:53 完成）：九面核验 + 12 条名不副实 + S1 四项隐藏前置（0003 迁移/事件族/schema-lock 时点/`_is_sse_request`+流键）——progress 174L 存 `doc/orchestration/progress/T-C1-059.md`；正面确认（durable SSE/MCP 通道/fake_openai_server/夹具纪律/alembic 通道/恢复门/roots 边界）已入 §13。
- B 席 minor/nit 处置：m-1→§5.1/§5.2；m-2→§6/S1；m-3→§4；m-4→§5.4；m-5→§2；m-6→§11（引用降级）；m-7→随 M-4 闭合；n-1→头部状态；n-2→§10；n-3→§12。

---

*v2 生成于 2026-11-10（orch-178 周期），来源：v1 架构讨论收敛稿 + 三席评审收敛。*
