# PHASE3-ORCHESTRATOR.md 结构化审查报告（B 席 · 规划评审）

> 任务：T-B-068 ｜ 审查对象：`doc/PHASE3-ORCHESTRATOR.md`（2026-11-10 架构收敛稿，提交 43e3040，本次核验在库）。
> 评审基准：`doc/CORE-CONTRACTS.md`（下称 CC）+ 文档自身一致性 + TS 原始实现（`../opencode-2.2.2/`）。
> TS 侧核验范围声明：路径可读（非边界问题）；本次实核 = packages/protocol、packages/server 存在性 + 根 README "gateway" 引用核验（0 命中，见 m-6）。packages/protocol 事件信封 / packages/server SSE 形态的深度对照未展开，相关结论标注【未验-范围】。
> 结论速览：**blocker 1 ｜ major 9 ｜ minor 7 ｜ nit 3 ｜ 未决（建议升 A）4**。整体判断：架构方向（网关唯一状态机 + 主脑智能外购 + 进程内引擎）成立且与 CC 纪律兼容；但 §1 路由判定细则 as-written 不可实现、§11 模块落位代码块整体错位一行、job 事件域归属未定义，三处须先修再排切片。
> 报告时间：2026-11-10 12:35:39（先 Get-Date 后落盘）。

---

## 一、逐节结论表

| 节 | 行号 | 结论 | 关联发现 |
|---|---|---|---|
| §0 一句话方案 | L10-14 | 通过 | — |
| §1 目标与非目标（路由规则） | L15-37 | **不通过** | B-1（blocker） |
| §2 角色与席位 | L38-51 | 有条件通过 | M-8(i)、m-5、预判 h |
| §3 核心纪律 | L52-60 | 通过 | — |
| §4 两条通道 | L61-71 | 有条件通过 | M-6、M-8(i) |
| §5.1 manifest 扩展 | L74-89 | 有条件通过 | M-3、m-1 |
| §5.2 pipeline spec | L90-93 | 有条件通过 | M-9、m-1 |
| §5.3 task brief | L94-105 | 有条件通过 | M-8(ii) |
| §5.4 job 与 trace | L106-110 | **不通过** | M-2、M-3、M-4 |
| §6 HTTP 面 | L111-122 | 有条件通过 | M-6、m-2、m-3 |
| §7 生命周期 | L123-144 | **不通过** | M-5、m-7 |
| §8 监控与故障处置 | L145-157 | 有条件通过 | M-2（stalled）、m-4 |
| §9 审查与质量门 | L158-168 | 通过 | m-1（跨仓契约关联） |
| §10 席位经济学 | L169-179 | 通过 | — |
| §11 模块落位与切片 | L180-203 | **不通过** | M-1（错位一行） |
| §12 已知坑 | L204-213 | 有条件通过 | n-3（与 M-8 联动） |
| §13 复用清单 | L214-225 | 有条件通过 | M-8(i)（进程关系矛盾） |
| §14 与现有治理体系的关系 | L226-234 | 有条件通过 | M-3（"纯增量面"表述） |

---

## 二、发现清单

### blocker（1）

**B-1 ｜ §1 L20-22 + L28-30 ｜ "参数不全落回主脑"的判定者未定义，路由规则 as-written 不可实现。**
"参数不全视同'未明确指定'，落回主脑路径"——网关路由是零模型规则面，无法从自然语言做参数提取；slash/前端按钮是否携带结构化 params、params 的形状与校验时机均未定义。判定"参数齐"需要（i）前端传结构化参数对象，或（ii）模型提取（等于路径②本身）。§7 L133-135 把"参数齐"列为路径①前置条件，但全文无一处定义"齐"的判定机制。该规则标注"原文定案"且是 v1 唯一入口判定的根，直接阻塞 S5 的"直连/落回两路径判定表测试"。
**建议**：路径① 增加第三条件——"请求携带结构化 params 对象且通过 pipeline 参数 schema 校验（网关零模型可执行的 JSON Schema 校验）"；纯自然语言、缺结构化 params → 一律路径②。同步在 §6 POST /jobs 请求契约中固定 params 的 schema 校验语义。

### major（9）

**M-1 ｜ §11 L182-190 ｜ 模块落位代码块描述整体错位一行，与 §2/§6 直接矛盾。**
首行注释"← 新模块（命名避开 gateway…）"折行占用了 router.py 的描述位，其后全部描述下移一行。按文中渲染：jobs.py=直连判定、engine.py=job 注册表、runner.py=图执行、workflow_mcp=run_task(brief)、httpapi/app.py=薄 MCP server、contracts/=/jobs 端点——与 §2（workflow-mcp=薄壳、runner=起工人会话）、§6（httpapi 挂 /jobs 端点）、§5.3（runner 喂 brief）逐一矛盾。按此排切片会建错模块职责。
**建议**：重排代码块使 router.py=直连判定 / jobs.py=job 注册表 / engine.py=图执行 / runner.py=run_task(brief) / workflow_mcp=薄 MCP server / httpapi=+/jobs 端点 / contracts=+卡片与 trace 契约，与 §2/§5/§6 一一对应。

**M-2 ｜ §5.4 L107 + §8 L150 ｜ job 状态机缺 `stalled`。**
§8 故障表两处使用"job 标 stalled"，§5.4 状态枚举 `created/running/gated/done/failed/cancelled` 无此值。状态机是"唯一状态机归网关"纪律的核心面，枚举不全会阻塞 S1 契约测试编写。
**建议**：或在 §5.4 枚举补 `stalled` 并给迁移规则（stalled→running[重派一次]/failed），或定义 stalled 为 running 的 health 子状态（不进状态枚举、进 job.health 字段），二选一写死。

**M-3 ｜ §5.1 L74 + §5.4 L109 ｜ "manifest 扩展是本设计的唯一契约变更点"失实。**
§5.4 "trace 事件复用 contracts/events.py parts 模型扩展"同样触碰契约面（CC §3.1 事件注册表纪律）；S1 验收判据本身就含"事件带全 trace 字段"。§14 "本 Phase 是纯增量面"同病。
**建议**：改口径为——"manifest 扩展是唯一的既有契约字段级变更；events.py 的 trace 扩展与卡片协议是事件面新增，须按 CC §3.1 注册纪律（类型注册、version、正/反例 fixture）落地；/jobs 端点为纯新增 HTTP 面"。

**M-4 ｜ §5.4 L108-110 ｜ job/trace 事件信封归属未定义（与 CC §3.1 冲突）。**
CC L146：EventDraft 强制 `session_id`（`ses_` 前缀）。路径① 无主脑会话，job 本身也无会话归属。job/trace 事件是进 session 事件日志（哪个 session？）、还是独立 job 域信封（`job_` 前缀键控？）全文未定义；S1 的"SSE trace 贯通"无法开工。§7 L140"结果写 trace/session 存储（主脑后续可读）"同样未指明目标 session（见 m-7）。
**建议**：定义 job 域事件信封（`job_id` 键控 + trace_id/span_id/parent_id/depth 字段族），与 session 域 EventDraft 并列、不复用其 envelope；或给 job 分配合成 `ses_` 会话并在 §5.4 写明映射关系。此为契约级决策，见未决问题 #2。

**M-5 ｜ §7 L133-137 + §12 L211 ｜ 主脑挂起语义三连缺。**
(i) workflow.run 工具调用的超时未定义——job 可含 human_gate，小时级挂起是常态，主脑工具调用的 timeout/budget 如何与此协调无定义；(ii) 网关重启后的孤儿工具调用——主脑与 workflow-mcp 均随进程消亡，job 经 checkpointer 续跑但 tool result 无人接收，回注协议缺失；(iii) 挂起期间用户追问由谁应答——主脑阻塞在工具调用中，对话通道无应答者。
**建议**：workflow.run 改两段式（submit→返回 job_id + poll/status 工具查询），从根上消解三问；或定义挂起窗语义 + 网关以 job 状态直接代答追问 + 重启后由网关向主脑会话回注 synthetic tool result。此决策影响工具协议形态，见未决问题 #3。

**M-6 ｜ §4 L63-71 + §6 L113-114 ｜ 双 SSE 端点的前端关联协议未定义。**
`/session/{id}/events` 与 `/jobs/{id}/events` 之间的统一信封、多路复用、trace 关联未定义。路径② 下用户停留在对话视图，前端如何得知订阅哪个 job 流、确认卡如何从 job 流路由回触发它的对话上下文，全文无定义（§4 仅一句"事件统一带 trace 上下文供前端分轨道渲染"，不够开工）。
**建议**：定义统一事件信封（trace_id + job_id + 关联 session_id 字段族），前端按 trace_id 关联两流；human_gate 卡片事件必须携带触发对话上下文（conversation ref）。

**M-7 ｜ §5.4 L107-110 + §7 L140 ｜ 持久化双写边界未定义。**
job 注册表（本仓存储）与 LangGraph checkpointer（独立 SQLite）的职责边界、恢复顺序、事实源未定义；且与 CC §3.2（L184-231）单 writer 进程纪律的关系未声明——两个 DB 文件 = 两个 writer 域，须显式声明各自归属，否则与"每数据目录单 writer"的既有裁决相抵。
**建议**：job 表 = 状态机事实源；checkpoint = 图内部执行状态；恢复序 = 读 job 表 → 按 checkpoint_ref 恢复图；不一致以 job 表为准并标 diagnostic。§13 复用清单补一行"job 库与 session 库分属两个 writer 域"的纪律承接。

**M-8 ｜ §2 L47 + §5.3 L100 + §4/§7/§8（杀进程措辞）+ §13 L222 ｜ 工人隔离机制矛盾/缺失（两子项）。**
(i) 进程关系自相矛盾：§13 明示 `sdk/` 为"v1 进程内调用"，则工人 = 进程内 SDK 会话，无进程可杀；但 §4 L68、§7 L137、§8 L147-150 反复出现"杀工人进程/看门狗 kill/runner 杀工人进程"。(ii) brief scope 白名单是"越界即拒收"的事后验收；运行时前摄拦截（工具调用期按白名单校验路径/命令）未定义，"硬边界"名不副实。
**建议**：(i) 二选一并全文统一——v1 工人 = 进程内 SDK 会话（取消 = cancellation token 传播 + 预算熔断，全文"杀进程"措辞改写）或升级子进程并同步 §13；(ii) 白名单前摄拦截复用 CC §5.2 权限门骨架（fs 写/命令执行按 brief.scope 校验），拒收仅作第二道验收。进程关系终裁见未决问题 #4。

**M-9 ｜ §5.2 L92 + §5.4 L107 + §8 L152 ｜ 递归守卫只覆盖 subworkflow。**
depth ≤ 3 只约束 `subworkflow` 节点嵌套；工人/主脑经 workflow.run 工具再发起 job 的工具级递归未定义——工人会话里再调 workflow.run 即可绕过 depth 上限（job.depth 只数图内嵌套）。
**建议**：job 提交时携带 lineage（父 job_id / 发起会话 id）；网关检测到发起方为工人会话（或 job 祖先链超限）即拒绝提交或计入同一 depth 预算。§8 递归套娃行同步改写。

### minor（7）

- **m-1 ｜ L84-88（§5.1）+ L90-93（§5.2）｜** pipeline 引用路径基准（相对 skill 目录/仓库根？）未定义；跨仓产物 graph.json 的 schema 版本字段、注册期校验与哈希锁定契约未定义（本仓引擎要解析外部仓产物，防御面缺失）。建议：补路径基准 + graph.json schema_version 字段 + 注册期校验/锁定要求。
- **m-2 ｜ L113-114（§6）｜** POST /jobs 参数校验失败的错误契约（4xx 形状）与 SSE 流鉴权未定义。
- **m-3 ｜ L118（§6）｜** gate_id 命名空间（job 内唯一？格式前缀？）与 gate 超时策略未定义（permission 骨架有 expires_at 可复用，应写明）。
- **m-4 ｜ L151（§8）｜** "同 pipeline 重派一次"语义模糊：整 pipeline 重跑还是失败节点重试？
- **m-5 ｜ L50（§2）｜** 小模型席 llm_call 的模型档位来源/配置接缝未定义。
- **m-6 ｜ L182（§11）｜** "命名避开 gateway……见 opencode-2.2.2 README"——本次核验 TS 仓根 README 无 "gateway" 字样（0 命中），引用失效或指涉不明。建议改引确切文件行号，或降为一般性命名理由。【TS 深度对照未验-范围】
- **m-7 ｜ L140（§7）｜** 路径① 结果"写 trace/session 存储"的目标 session 未指明（与 M-4 同根，随其一并裁决）。

### nit（3）

- n-1 ｜ L7 ｜ 状态"计划（未排期）"与 §11 已给出切片级验收判据的成熟度不一致（信息性提示）。
- n-2 ｜ L171-179（§10）｜ "路径①命中时零消耗"表述成立，但宜注明"主脑常驻会话的存量上下文不因此产生新消耗"。
- n-3 ｜ L206（§12）｜ 已知坑"opencode serve worker 的 CWD 会锁目录"在 M-8 裁为进程内会话后将失效，须联动改写。

### 核验备注（非发现）

- 提交 43e3040 存在且信息吻合（git log 核验通过）。
- `tests/contract/skills_fixtures/skills_manifest_entry_unknown_field.json`、`src/opencode/contracts/events.py`、`src/opencode/skills/catalog.py`、`src/opencode/bus/` 均在库（Test-Path 全真）——§5.1/§13 引用有效。
- "现有 5 个 httpapi 端点"计数本次未验（httpapi 路由风格正则未命中，未展开框架细节）——不影响结论，标注待核。

---

## 三、调度器 8 项预判裁决

| # | 预判 | 裁决 | 依据与补充 |
|---|---|---|---|
| a | §1 直连路径参数来源未定义 | **确认（升级为 blocker B-1）** | 网关零模型无法做 NL 参数提取；"参数不全"判定者缺位；§7 把"参数齐"列为路径①前置但全文无机制。修复路径见 B-1 建议（结构化 params + schema 校验）。 |
| b | §7 主脑挂起语义未定义 | **确认（M-5）** | 超时/孤儿工具调用/挂起期追问三问全中。补充：孤儿回注与 workflow-mcp 薄壳生命周期同根（见 g 补充）。 |
| c | 双 SSE 端点关联协议未定义 | **确认（M-6）** | §4 仅一句"统一带 trace 上下文"，信封/多路复用/卡片回对话上下文均无定义。 |
| d | 持久化双写边界未定义 | **确认（M-7）** | 补充：不仅是职责/顺序/事实源三缺，还与 CC §3.2 单 writer 纪律存在未声明的相抵面。 |
| e | 工人隔离：进程关系 + 前摄拦截缺失 | **确认（M-8）** | 两子项均有实据：§13 进程内 vs §4/§7/§8 杀进程的直接矛盾；白名单仅事后拒收。 |
| f | 工具级递归未定义 | **确认（M-9）** | depth 语义仅数 subworkflow；lineage 检测缺失，工人再调 workflow.run 可绕上限。 |
| g | 形态 B 下 workflow-mcp 薄壳是否多余 | **部分反驳（保留薄壳）** | 主脑挂工具的既有面就是 MCP client（文档 §13 复用清单自证）；原生工具 + agent 挂载面隔离路线须改 OC 内核工具注册契约、把编排面泄漏进内核，代价高于一个 stdio 壳。薄壳是形态 B 下成本最低的隔离面，不多余。但补充两点：① 最终形态与 T-A-015 工具形态分叉绑定 → 未决 #1 建议升 A；② 薄壳须补"主脑进程亡→壳亡→在飞等待丢失"的重启联动语义（与 M-5(ii) 同根）。【TS 侧对应形态未验-范围】 |
| h | 席位命名与开发期调度体系混淆 | **确认（minor 级）** | 本任务体系 B 席=规划评审、文档 B 席=OC 主脑，直接撞名；治理面 A 席=用户与文档一致但放大混淆。建议运行时角色去字母化（主脑/工人/引擎/用户门/HITL），字母席位保留给开发期治理体系。 |

---

## 四、契约触碰面（对照 `doc/CORE-CONTRACTS.md`）

| PHASE3 面 | CC 条款 | 判定 |
|---|---|---|
| §5.1 manifest 增 `execution`/`pipeline` 可选字段 | CC §6.3（L342-392 SkillEntry/manifest schema 面）+ §1 L17（extra=forbid，未知字段拒收） | **触碰**——字段级变更，须版本化 + schema_lock + fixtures 同步（文档已自知并排入 S5） |
| §5.4 events.py parts 模型 trace 扩展 + 卡片协议 | CC §3.1（L142-182：py.* 注册表、EventDraft 强制 session_id、version=1、extra=forbid） | **触碰且含冲突面**——job 域事件无 session 归属（M-4），注册纪律须走 version/fixture 路径 |
| job 持久化 + LangGraph checkpointer 双库 | CC §3.2（L184-231 单 writer/唯一 commit） | **对齐义务**——两库两 writer 域须显式声明归属（M-7） |
| 工人 OC 会话（每节点新 ses_ 会话 + 事件提交） | CC §2.1/§3.1/§3.2 既有面 | 零契约触碰（纯复用） |
| §6 /jobs 5 端点 + gate reply（同构 /permission/{id}/reply） | CC §5.2（L299-307 权限面）仅语义同构复用，不改既有 | 零契约触碰（纯新增面，须新增契约测试） |
| workflow-mcp stdio 壳 | mcp/ client 面 + test_mcp_naming 既有契约 | 零契约触碰（纯复用） |
| 取消传播（网关→引擎→工人） | CC §2.1 L61-63 run cancel 语义不修改（session 域既有） | 零契约触碰（新增 job 域面） |
| 路由判定读 skills catalog | CC §6.3 既有面只读 | 零契约触碰 |
| 主脑会话（sdk 驱动 ses_ 会话） | CC §3.1/§3.2 既有面 | 零契约触碰（纯复用） |

小结：真正的契约触碰 = manifest 字段（既有字段级变更）+ events.py trace 扩展（事件注册面新增/修改）；其余为纯新增或纯复用。文档"唯一契约变更点/纯增量面"口径须按 M-3 修正。

---

## 五、完整性审查（"按此排切片开工"视角的缺失决策）

1. **参数结构化传递通道**（=B-1）——阻塞 S5 路由判定表。
2. **job 事件信封**（=M-4）——阻塞 S1（S1 验收即"事件带全 trace 字段"）。
3. **跨仓 graph.json schema 契约**（=m-1）——阻塞 S2（引擎加载/校验外部产物无防御面）。
4. **工人工作目录策略**（brief.scope 相对基准；job 是否独占 worktree）——阻塞 S3 白名单拒收测试。
5. **主脑-用户绑定与并发模型**（"每用户会话一个主脑"——v1 单用户？多用户并发？鉴权？）——阻塞 S4 e2e 搭建。
6. **预算看门狗度量来源**（tool calls 的计数点、tokens 的 usage 事件源）——影响 S3"预算杀"验收可测性。
7. **gate 超时/expires 策略**（=m-3）——影响 S5 gate e2e。
8. **重启后孤儿工具调用的回注协议**（=M-5(ii)）——影响 S4 e2e 完备性。

---

## 六、未决问题（建议升 A）

1. **workflow-mcp 薄壳 vs 原生工具挂载**（预判 g 的终局）——与 T-A-015"工具形态"分叉直接绑定；本席倾向保留薄壳（反驳"多余"论），但终裁归 A 席。**建议升 A**。
2. **job 事件域归属**：独立 job_ 信封 vs 合成 ses_ 会话映射（M-4）——契约级决策，影响 S1 开工与 CC §3.1 边界。**建议升 A**。
3. **主脑挂起模型**：长挂起 tool call vs submit/poll 两段式（M-5）——影响工具协议形态，与 T-A-015 工具形态分叉相关。**建议升 A**。
4. **工人进程关系终裁**：进程内 SDK 会话 vs 子进程（M-8）——牵动 §4/§7/§8/§12/§13 五处一致性改写；建议随 T-A-015"工人隔离"分叉一并终裁。**建议升 A**。

---

*审查人：B 席（T-B-068）｜ 本报告 docs-only，无代码改动。*
