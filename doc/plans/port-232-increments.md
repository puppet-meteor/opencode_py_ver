# 上游 2.3.2 增量移植计划（T-C3 系列）

> 2026-11-20 开池。源树：`../opencode-2.3.2/`（tarball v2.3.2，2026-11-18）；基线对照：`../opencode-2.2.2/`。
> **移植基线仍为 2.2.2**（裁决基准不变，见 ORCHESTRATION.md 头注）；2.3.2 仅作增量特性源。
> 目标对齐：`doc/GOAL.md`——每票标注服务的目标面（双门/无人值守/诊断分工/事件触发…）。

## 增量画像（core/src：301 文件 +19908/−11967；server +874；cli +1095；protocol +402）

深读结论（14 簇，2026-11-20 explore 席）总表：

| 簇 | 内容 | 助益 | 移植量 | 票号 | 优先级 |
|---|---|---|---|---|---|
| 13 | 工具输出截断保头+全文落盘+tail 尾窗读 | High（token 双门直接手段） | S-M | **T-C3-001**（✅ 已落地 `4a80ab1`） | **P0** |
| 2 | 后台 job 持久化+重启重投（shell/subagent 后台化） | High（长变更执行不丢结果） | M | **T-C3-002**（✅ 已落地，含评估集先行） | **P0** |
| 1 | automation（every/cron/at+event 触发+租约调度+退避） | High（无人值守执行载体） | L→裁剪 M | **T-C3-003**(prompt 动作)/004(event) | **P0**/P1 |
| 5 | 子代理完成回执 outbox+synthetic+深度链 | Med-High | M | **T-C3-005**（✅ 已落地，含评估集先行；实为恒后台语义翻转+每轮回执+outstanding 保活） | P1 |
| 6 | mcp connection 状态机+events（watch_mcp_event） | Med（事件触发前置） | M(L) | **T-C3-006**（✅ 已落地，含评估集先行：P0/M1-M6 七列 BEFORE 全灰→AFTER 全达；实现八文件+红集 21 契约） | P1 |
| 9 | flag 旗标解析链+shared-cache+canonical-json | Med | S-M | T-C3-007 | P1 |
| 8 | db storage-failure 分类+batch 分批 | Med | S | T-C3-008 | P1 |
| 11 | server 面 automation/job 路由 | Med | M | T-C3-009（随 002/003） | P1 |
| 14b | provider mTLS 客户端证书 | Med（内网自签网关） | S-M | T-C3-010 | P1 |
| 3 | artifact+blob-store（本地 provider） | Med | M | T-C3-011 | P2 |
| 4 | tool/search bounded(limit+1)+timeout | Low-Med | S | T-C3-012 | P2 |
| 14a | verbosity 默认低（token 面） | Med | S | T-C3-013 | P2 |
| 12 | cli 非交互三读序等后台 job | Low-Med | S | T-C3-014 | P2 |
| 7/10 | config-managed / codemode 四件 | Low | S/M | 暂缓 | — |

## P0 三票要点（上游机制摘要，源码为证）

### T-C3-001 工具输出截断（簇13）——✅ 已落地（`4a80ab1`，2026-11-20，CI 44668671726 绿）
- **壳余面**：shell tail 尾窗读+bash 全文落盘归 T-C3-002 连带；上游 read 帽统一化（2000 行/50KiB，py 暂 64KiB 自管帽）随 T-C3-002 评估——豁免契约下无碰撞。
- **层间契约**（本轮新增发现）：上游 L76 `metadata?.truncated !== undefined` = 自管截断豁免；py 7 适配器设旗、缝读旗跳过——后续新增自管帽工具必须遵守此契约。
- 上游：`packages/core/src/tool-output.ts` truncate()——保开头（行/字节限制先到者切一次，预览不以换行结尾）；全文写 `<data>/tool-output/`；marker `[showing lines 1-N of M (last line cut); full output saved to <file>]`；metadata.truncated+outputPath；多 content part 按序保结构；已截断幂等跳过。
- shell tail 尾窗：`shell.ts` L822-880——`sizeOf`+`readRange(size-maxBytes)` 尾窗读+utf8 边界对齐，保尾 maxLines，行中起窗丢半行保行号，超大单行「最后 N 字节(共 Y)」。
- py 落点：invoker 归一化面（现 64KiB structured marker，丢结构化）→ 升级为保结构截断+落盘+outputPath；bash/pty 大输出同策略。
- 目标面：**token 开销门禁**（案例回放输出 token 直接受控+可事后取证）。

### T-C3-002 后台 job 持久化（簇2）——✅ 已落地（2026-11-20，p15-s1；M1 −70%/−55%、M2M3 0%→100% 实测见评估集段）
- 上游：表 `job`（job/sql.ts，迁移 20261030054400）；Recovery 描述符（shell/image/codemode/subagent 联合，job/recovery.ts）；Active 注册表（done/backgrounded 双 Deferred、generation、blockingSessions 计数、COMPLETED_LIMIT 内存裁剪）；持久化时机=**backgrounded 即 upsert**（notification_id 主键，start(background) 先于 fork 落行）+settle 终态（非 cancelled、非 settling/consumed）再 upsert+completeBackground（inbox admit）删行+cancelSession 按 session/child 批删；block=done/backgrounded 双 Deferred race（上游调用面 10s 超时转后台）；重启重投（session/execution/restart.ts L370-425）=boot 读表→settled 行 synthetic 通知重放→running+subagent 且 execution 不在→prepareResume 决定 resume/RESUME_EXHAUSTED→孤儿 cancel claim 清理→唤醒序=shell/工具结果先、未决调用次、子代理最后（defer 醒防超预算）。
- py 落点决策：①**命名区隔**——py `storage/jobs_store.py`=workflow 事件投影（JobRecord/DurableEvent，概念不同源）；上游 Job 表 py 落名 `background_job`+独立模块（Alembic versions/ 新迁移，storage 建表面包=db.py/migrations/env.py 既有）；②裁剪=Recovery 第一期只做 **shell**（bash 大输出后台化+全文落盘复用 tool_output 目录面）+**subagent**（行持久化+重启重投 synthetic 通知）；image/codememode 不做（py 无此面）；子代理回执 outbox（prepareSubagentReport/recordSubagentReport）留 T-C3-005；③注册表=进程内 dict+asyncio.Future 对位双 Deferred+generation 计数；④重投 shell running 行=不可恢复→标 error 重放「服务器重启中断」（py 无 shell daemon 可重挂）。
- **评估集（先行落地，2026-11-20）**：`tests/integration/test_bg_jobs_eval.py`——恒绿测量面（能力探测式，非断言面），同一入参在实现前/后各跑一遍，差值即本票对 GOAL 双门的证据；探针契约钉死=模块 `opencode.tools.background.replay_pending(database)`（async）+`ToolLimits(background_after_s=)`（测试注 1.0s 防 CI 抖动）+表 `background_job`——实现与 harness 不齐时报 `api-mismatch:<Err>` 而非静默，修对齐不改读数。**仪器修正两笔（随实现落地并记档）**：①字段探针 `model_fields`→`dataclasses.fields`（ToolLimits 是 frozen dataclass 非 pydantic，旧探针从未注入阈值；BEFORE M1 读数不受影响——无能力在场时阈值注入同样全阻塞）；②计时点移入协程内（末次 outcome 返回即停，`asyncio.run` 收尾杀死续体的进程树清理非模型可见时延；BEFORE 侧内联调用无待杀续体，读数等价）。**BEFORE→AFTER（2026-11-20 实测）**：
  - **M1-single**（速率门）：单次 3s 长调用墙钟 **3.31s→1.00s（−70%，=阈值）**，`backgrounded_markers 0→1`、inline 1→0；
  - **M1-batch**（速率门）：模型回合式 3×2s 串行墙钟 **6.81s→3.05s（−55%，=3×阈值）**；M1-concurrent 6.81s→3.03s；
  - **M2M3-restart**（无人值守门）：能力探针 capability-absent→probed；db-wired 后台调用「崩溃不 settle」→ 行存活 **0→1**、重启重投交付 **null→1**（running 行按契约重放中断错误，非伪造成功）——生存率 **0%→100%**；
  - **M4-bounded**（token 门）：262144B 洪泛模型可见 **65606B 有界保持**（truncation_markers 1），`disk_full_bytes:null` 维持——后台化全文落盘（>帽时尾窗不丢）随壳余面后补，本票行内 payload 已带边界内全文。
  - 契约面（红→绿，随实现同票）：`tests/integration/test_background_jobs.py` 8 测——阈值字段/registry database 参/短调用不变 face/长调用转后台+先行落行+终态 settle/重投空库·settled·running 三态/按 session 删。
- 目标面：**执行速率门禁可测**（批量案例回放长任务后台化+重启不丢）。

### T-C3-003 automation prompt 动作（簇1 裁剪版）
- 上游（2.3.2 侦察已固化）：表 `automation`（trigger∈at/every/cron/event JSON + action∈prompt/code JSON + status 默认 active/next_due_at/expires_at/run_count/failures/last_run_at/last_message_id/last_outcome/last_output/retry_at/state/event_position+时间戳；3 索引=project/session/partial `automation_active_idx` ON next_due_at WHERE status='active'）；常量（MIN_PROMPT_INTERVAL=1min/LATE_AFTER=1min/MAX_SLEEP=30s/LEADER_TTL=2×MAX_SLEEP/FAILED_RETRY=5min/DISABLED_RETRY=1h/TICK_CONCURRENCY=4）；调度 `following()`（at→None 一次性；every 保相位 `due+(floor((now-due)/every)+1)*every`；cron 严格下一步+回退时钟安全）；settle 乐观并发（WHERE id+run_count+status）；租约=KV insert onConflict where owner=自己或 until≤now，逐 run claim；runAction（run=run_count+1、消息 id=`msg_{id}_{run}`、complete 在递交后 settle{last_outcome=late?fired,retry_at=null,+advance}、coalesce=无 event+定时器+last_message_id 仍在 inbox→wake+skipped）；失败面（InvalidError→pauseWith；目录消失→pause；其他→FAILED_RETRY 5min）；create 校验（relative at 固化 ISO+须未来、every≥1min、cron 须每分钟至多一次且至少一解、prompt 无 session 须 to、to.session 须同 project、subagent 须 owner session、expires 早于首跑拒）；resume（at 落期间跑一次除非过期；repeat 取 following；failures/retry_at 清零）；pause/resume/remove=幂等条件 UPDATE。
- py 落点决策（2026-11-20 固化）：
  1. **表**：迁移 0006 建 `automation`（列/索引对上游全量，`session_id` 无 FK 同 background_job 先例；event_position/state 列保留判别空间）+**租约表 `automation_lease`**（key TEXT PK/owner TEXT/until_ms INTEGER，单行）——py 无 KV 表，独立小表贴上游 KV 语义（TTL 时间衰减，WriterLock 文件锁无 TTL 不合）。
  2. **模块**：新包 `src/opencode/automation/`（`service.py`=CRUD+tick+runAction+settle；`cron.py`=自研 5 字段 cron；`scheduler.py`=选主循环）——对位 watcher/service.py 的 async CM 服务模式；工具适配器 `src/opencode/tools/automation.py` 挂 registry namespace `opencode`。
  3. **cron 选型自研**（无新依赖，纪律）：5 字段（min hour dom month dow）+`*`/`,`/`-`/`/step`；next()=逐「本地日」扫描（日字段筛→当日 valid (h,m) 集→tz 转 UTC instant 取 >now 最小；DST 歧义 fold=0 第一次出现、不存在 instant 跳过=上游回退安全语义；上限 5 年无解→None=「cron never runs」拒）。
  4. **时钟/参数注入**：服务构造 `clock: Clock|None`（SessionService 先例）；`max_sleep_s/leader_ttl_s/late_after_s/failed_retry_s/tick_concurrency` 全部构造参（测试注短值防抖）。
  5. **递交面**：`SessionService.prompt(session_id, text, request_id, delivery=...)` 现成；coalesce=递交前 `SessionInbox.pending_by_request` 命中→skip；subagent 目标=child id `ses_aut_{aut_id}_{run}`（`is_run_session`=startswith `ses_aut_`）经 `create(parent_id=owner)` 派生可重试收养。
  6. **裁剪记档**：event 触发（watch/replay cursor）→T-C3-006 后；code 动作+authorize/permission.ask→暂缓；flag `experimental.automations` 每目录复查→py 无 flag 面，第一期默认启用记偏差；command `automation`→不移植。
- 评估集（先行）：`tests/integration/test_automation_eval.py`——能力探测（模块/表 0006/工具注册）；M1=every 到期触发墙钟、M2=at 重启补跑一次即 done、M3=every 保相位（错槽不补跑）、M4=prompt 失败退避 retry_at、M5=coalesce。BEFORE 预期 capability-absent/0%。
- 红契约集：`tests/integration/test_automation.py`——create 校验矩阵/following 三 trigger（含 DST 固定表达式）/tick 选取与 retry_at 门/租约单飞（双 scheduler 同库一 tick）/settle 乐观并发/退避/coalesce/pause-resume-remove 幂等/subagent child 派生收养。
- **BEFORE→AFTER（2026-11-20 实测，p16-s1）**：
  - **P-capability**：capability-absent → **probed**（模块 `opencode.automation` 在场、0006 双表、5 工具 `opencode__automation_*` 全注册）。
  - **M1 触发墙钟**：无能力 → **2.03s @ every=2.0s**（=周期+唤醒粒度 max_sleep 50ms，无人值守触发达成）。
  - **M2 重启补跑**：→ deliveries=1、二次 tick 零新增、`last_outcome=late`、status=done（不丢不重）。
  - **M3 保相位**：→ run_count=1、**phase_drift_ms=0**（错 3 槽只跑一次、从原槽相位推进，无补跑突发）。
  - **M4 失败退避**：→ attempts 1/1/2（首槽试一次、退避窗内零新增、窗后一次；failed_retry_s=0.5 注入）。
  - **M5 coalesce**：→ deliveries=1、`last_outcome=skipped`（pending 期跳过不堆叠）。
  - **仪器修正两笔（随实现落地并记档）**：①假件 `send_prompt` 改 async——契约钉的是 async 端口，同步假件被 `await` 后副作用先落再抛 TypeError，读数歪成 failed（红集假件本就 async，纯 eval 漂移）；②M4 计量源 `len(sends)`→假件 attempts 计数——失败端口在 append 前抛，sends 把「尝试过」误记为「没尝试」。
  - **实现期修的真实 bug**：settle 绑定名 `:run_count` 同名覆盖（set 的 run_count 与 WHERE 的期望值撞名）→ WHERE 改绑 `:expected_run_count`；红集 `_BASE_MS` 手算错 6 天致 DST/cron 假红（改为推导值+断言守卫）；租约测试同进程双 database 句柄撞 WriterLock→共享 engine（跨进程单飞由 TTL 语义保证并同测）。
  - **全量**：2509P/8S/5F（基线 2485P/8S/5F+24 新测；5F 恒为 install WDAC WinError 4551 环境面，CI 双腿仲裁绿）。依赖：`tzdata>=2024.1; sys_platform=='win32'`（uv.lock 已再生随落）。
- 目标面：**无人值守运行态**（变更窗口定时巡检、告警触发诊断/回退的执行骨架）。

### T-C3-005 子代理恒后台+完成回执+outstanding 链（簇5 裁剪版）
- 上游（2.3.2 diff 侦察已固化，四文件 vs 2.2.2 实 diff 为证）：**语义翻转为恒后台**——`subagent` 工具删前台 block 链（jobs.block/onInterrupt/error/cancelled ToolFailure 面）与 `background` 参，Output status 收缩 Literal("running")，调用即返四行指引（文案改「You will receive a `<subagent>` message when it goes idle, and again after each later round of work」+follow-up sessionID 句）；**每轮 idle 回执**——SubagentJob.start 单口化（run=execution.resume→""，background:true，notificationID 入 job 行；onInterrupt 仅 jobs.isCancelled 显式 cancel 打断子，shutdown 保子 resumable），每轮 settle 投一次；**投递语义**——synthetic 投 parent：`<subagent sessionID state description>` 包裹，metadata={source:"subagent",childID,agent,state}，text=末条 assistant parts join 空则 NO_TEXT；**outstanding 保活**——recovery 行嵌 outstanding 快照（OutstandingItem kind=codemode/shell/image/subagent），completed+outstanding 非空→报文 state="running"（metadata.state 仍真值）+formatBody 追加「Background work still running:」行表（四 kind 行格式+跨 session `, in {sessionID}` location）；cancelled 分支三级串 output??error??"Subagent cancelled"；**active 集**——mid-turn 子（execution 持 claim、job 行已 settle）计入 outstanding（cancelled draining 跳过，仅后代，不与既有行重复）；**深度链**——SubagentDepth.reached 抽出（walk parentID 链 limit 步仍有 parent 则拒，`experimental.subagent_depth`??1）；**follow-up**——sessionID 须当前 session 之子；existing 子 active 或 job running→prompt 不带 resume:false（job 的 resume owns the start）；**重投矩阵**（restart.ts deliverSubagentSafe/recoverSubagent）——settled 行按记录态投（deleted parent→删行清账；SyntheticConflictError→保行下届再投；他错→parent 不在删行/在则告警保行）；running+子 isActive→不动；不可 resume→error 回执 RESUME_EXHAUSTED；可 resume→延迟统一启动。
- py 落点决策（2026-11-20 固化）：
  1. **恒后台化**：`tools/builtin/subagent.py` 删前台 await 链（SUBAGENT_TIMEOUT_S/SUBAGENT_POLL_INTERVAL_S/SUBAGENT_CANCEL_WAIT_S 随删）、input_schema 去 `background` 参、ToolResult 恒 `_background_result`（TS 2.3.2 四行文案逐句）、description 改恒后台句；`SUBAGENT_VERSION` 1→2（schema 演进）。
  2. **每轮回执**：T-C1-101 observer 扩多轮——观察 run 终态投递后**再武装**（follow-up 起新 run 即新 generation，notification id `subagent:{run_id}` 天然 per-run 一投；不再一子一投封顶）。
  3. **outstanding 快照+尾表**：settle 时查 background_job 行（kind shell/subagent+status running）构 OutstandingItem 进 `BackgroundJobRow.payload`（列已在，**零迁移**）；投递点 formatBody 尾表+state 保活+cancelled 三级串对齐。
  4. **active 集等价**：py 无 claim/execution——「子 run 非终态（含 cancelling）」+「子名下 running 行」合取判定，偏差记档。
  5. **深度**：`SUBAGENT_MAX_DEPTH` 常量保持（S4 先例；TS config 化偏差记档）。
  6. **follow-up 递交**：running 子→`prompt(delivery="steer")` 加入当前 run（T-C1-100 face=TS「joins its current run」）；idle 子→默认起新 run；resume:false N/A 沿 T-C1-101 记档。
  7. **重投矩阵**：`background.py replay_pending` 扩 kind='subagent'——settled 行按记录态重投；parent session 删→删行清账；inbox 冲突→保行下届再投；他错→parent 不在删/在则告警保；子 run 非终态→保行不动（py 无 execution.resume，首期不自动续跑，偏差记档）。**status 词面**：subagent 行 settle 复用既有终态集（`succeeded`/`error`/`cancelled`），投递构面映射 TS 报文 state（`succeeded`→`completed`；BEFORE 实测证据：v1 `_TERMINAL` 不含 "completed"，裸造 completed 行走 running→error 分支）。
  8. **裁剪记档**：codemode/image OutstandingItem kind 暂缓（py 无对应工具）；`experimental.subagent_depth`→常量（S4）；Permission.invocation 统一重构 N/A（policy 面零改动）；agent 目录/换 agent→SUBAGENT_AGENTS={"task"} 不变（既有 DIFFERENT-BY-DESIGN）。
  9. **lazy import（实现期钉）**：service.py 顶层 import `tools.background` 必成环（`opencode.tools.__init__`→registry→`session.service`）——outbox 行 helper 全部调用点 lazy import。
  10. **parent 判活（实现期钉）**：py 无 sessions 表（event-sourced）→上游 sessions-table liveness 映射为 events 表 distinct session_id（replay 删行清账臂的判活依据）。
  11. **failed 产线与 ErrorChunk 语义（实现期钉）**：provider ErrorChunk 落 run "interrupted"（PROVIDER_STREAM_INTERRUPTED 既有语义）→回执 state="cancelled"（三级串 tier-1=子末条 output，实测 "never reached" 生效；常数句臂由 restart-fold 测钉）；唯一确定性 FAILED 产线=步数天花板（max_steps=1+tool-call 轮，v1 配方）。v1 前台 TOOL_FAILED/ToolTimeout/协作 cancel 三面随前台链删除（上游删面）。
  12. **InboxRef 不 arm（实现期钉）**：busy follow-up（parked steer）不 commit marker——该 run 既有 observer 持整代唯一回执（TS「job 的 resume owns the start」对位）；仅 RunRef（新起 run）arm+commit。
  13. **session.background 收敛（实现期钉）**：上游 2.3.2 保留端点（语义挪到 job backgroundAll）→py 端点保留为**结构性 no-op**（恒 204+False；404 面不变），死码转换机制（`_subagent_foreground_waits`/`_subagent_background_requests`/wait begin-end/consume）随前台链删除；job 系后续票再赋牙。
  14. **writer lock 单写者（实现期钉）**：App 存活期内二次 `open_database` 必 WriterLockHeldError（设计面）——测试经 `app.database` 属性写行（§outstanding/replay 两处配方）。
- 评估集（先行）：`tests/integration/test_subagent_eval.py`——能力探测（spec v2 无 background 参/恒后台描述句/Output running 单值）；M1=恒后台即返（返回时延 << 子 run 时长且 status=running）；M2=idle 回执（`<subagent ...>` 包裹+末条 assistant text）；M3=outstanding 尾表+保活（子挂 running shell 行→回执 state=running+「Background work still running:」行）；M4=follow-up steer（running 子 follow-up 不 SessionBusy、并入当前 run）；M5=深度拒（嵌套 spawn 拒+文案）；M6=重启重投分类（parent 删→行清；settled→重投一次）。BEFORE 预期 capability-absent/0%（现 background 为 opt-in 面≠恒后台）。
  - **BEFORE 实测（2026-11-20，main `c31719d`，7 passed 12.85s）**：P0=capability-absent（v1/background 键在/无恒后台句）；M1=blocking-or-absent（returned_s=null，前台 block 至 deadline）；M2=no-receipt；M3=no-tail-or-flat；M4=spawn-absent（spawn 本身超时，诚实读数）；M5=rejected（深度守卫在场，回归行恒绿）；M6=plain-or-absent（replayed=1 但文本=重启 error 串——"completed" 不在 `_TERMINAL` 词表，走 running→error 分支；wrapped=false）。
  - **AFTER 实测（2026-11-20，p17-s1 实现毕，7 passed 7.46s）**：P0=capability-present（spec v2/schema 四键无 background/恒后台句）；M1=immediate（returned_s≈0.05s，status=running，四行文案逐字）；M2=receipted（synthetic 包裹+state=completed+末条 text）；M3=keep-alive（state_attr=running+metadata.state=completed+still_running_tail）；M4=steered（followup=delivered 非 busy；修 harness 自伤——`_spawn_gated_child` 原无条件放行 gate，follow-up 与子 run finalize 提交竞速致 ConflictError 逃逸，加 `release=False` 参保持子 run 停泊）；M5=rejected（恒绿）；M6=wrapped-replay（改造：真实 parent 会话+`succeeded` 词面行→wrapped 重投一次+行清+二遍空；原裸造 "completed" 状态行与假 parent 在新面下走静默清/保行臂，非重投）。
- 红契约集：`tests/integration/test_subagent_receipt.py`——恒后台 ToolResult 面钉死（文案四行+status 单值+schema 无 background）；每轮投递（首 run+follow-up run 各一投、同 generation 不重投）；formatBody 行格式（shell/subagent 两 kind+location+clean 空白归一）；state 保活矩阵（completed+outstanding→running/无 outstanding→completed/error/cancelled 三级串）；深度链 walk（直系/隔代/limit 边界）；follow-up parentID 校验+steer 递交；重投矩阵四臂（删行清账/冲突保行/他错删或保/running 不动）。
- 目标面：**无人值守+token 双门**（长任务委派不阻塞主线；回执按需唤醒 parent 免轮询探测——token 直接节约；outstanding 尾表防模型重复劳动）。

### T-C3-006 mcp connection 状态机+MCP events/watch_mcp_event（簇6 裁剪版）
- 上游（2.3.2 diff 侦察已固化，四新件+六改件 vs 2.2.2 实 diff 为证）：
  - **connection.ts（新）**：单连接状态机——Events 回调表（connecting/connected/failed(failure,answered)/settled/closed(reason)/changed(kind∈tools/prompts/resources)/legacy(version)）按序 inline 上报 owner；连接即列工具（失败整服 failed，不留空目录假连）；watch 五挂（onClose/onSessionExpired/onToolsChanged/onPromptsChanged/onResourcesChanged）；recovering=会话过期 stop→start 重连一次再跑；stop 清目录+changed×3；endpoint 轮流握手（remote 同 URL 不并发握手爆发）。
  - **pool.ts（新）**：跨 Location 共享连接——key=remote(url+headers/secret 指纹+oauth 指纹+codemode+credential+protocol+timeout+clientInfo+events)/local(command+resolveCwd+env 指纹)；slot+holders Set+outcome Deferred(connected/private/failure)；unpoolable/legacy 两集合（answered 拒绝→私有重连；legacy 协议→私有 legacy）；最后 holder 放手才关；elicitation 广播 race 首答胜。
  - **index.ts（重写）**：状态词面不变（connected/pending/disabled/failed/needs_auth）但转换改 connection 回调驱动；attempt 窗口+awaitStartup（STARTUP_GRACE_MS=2000）；reconcile 首次 boot 批量注册+逐服务器异步 start；servers() 带 title/integrationID；connect/disconnect/remove/凭据切换 reconnect 过渡面；**MCP Events 订阅托管**（subscribe 幂等 id=`mcpsub_`+sha256(canonicalJson)−24、follow 流机器、EVENT_REOPEN=1s 指数封顶 30s、cursor 推进=bus 发布成功后[发布失败可重放]、active.truncated→gap、实验 bus 事件 `experimental.mcp.event`/`experimental.mcp.subscription.changed`、SubscriptionStatus=pending/active/ended{unavailable|terminated|rejected}）。
  - **client.ts（大改）**：ConnectOptions 单对象（roots/events/clientInfo）；协议协商 negotiation(protocol)——legacy→纯握手/auto→discover 探测/`2026-07-28`→pin（协议词面集不变）；**MCP Events 草案扩展**（initialize 声明 `extensions:{"io.modelcontextprotocol/events":{}}`+serverInfo.capabilities.extensions 探测；`events/list` 分页 nextCursor 上限 100 页；`events/stream` 长挂靠心跳判死 EVENT_STREAM_IDLE=60s；四通知 active/event/heartbeat/error+terminated；`io.modelcontextprotocol/subscriptionId` meta=请求 id 路由到 Queue）；工具列取改本地逐工具校验（非对象/无 name/非 object schema→skip+warn 非整服失败）；callTool 带 toolDefinition+HEADER_MISMATCH(-32020)重列重试一次；ConnectError.answered（超时/401/403/transient→answered=false，被到达但拒绝→true）。
  - **event-errors.ts（新）**：EventStreamRejectedError/EventStreamTerminatedError 两终态（草案实验面，无兼容保证）。
  - **mcp-events.ts（新，watch_mcp_event 工具）**：flag `mcp_events` 门；无 event→列 connected 服务器 EventType 清单（不可达/无事件列空）；watch→stream 订阅（cursor 续传+干净关闭也重开 1s/2s/4s 封顶 3 轮+Terminated 不重开+session.deleted 中断）；每事件 synthetic wake——确定性 id=sha256(sessionID\0server\0name\0args\0eventId)[:26] 重放幂等、metadata={source:"mcp.event",server,event,eventID}、wakeText 注入防护句逐字+DATA_LIMIT=16384 截断+fence 反引号加长+控制字符清洗 line()；watch 失败投「watch stopped」synthetic；once 默认 true（false=持续至会话删）。
  - **stdio.ts**：新 resolveCwd（相对 cwd 对 directory 解析+`~` 展开，pool key 与 spawn 共用保证一致）；帧解析手写行切（16MiB 超限 onerror+主动 close 不断读流）；kill 单调用 SIGTERM+forceKillAfter。
  - **schema/server**：Mcp.Status 词面不变；新增 pool:boolean（默认 false，描述=共享连接语义）+SubscriptionStatus；server 侧无新端点（仅 client WebSocket holdLocation 借用）。oauth.ts：connectProvider 只用预选凭据行（selectCredential=最新行，同选择进 pool key）——连接期不再发起登录。
- py 落点决策：
  1. **范围=stdio-only，pool 不移植**：py R4-S2 即 stdio 极简客户端（无 remote/oauth/elicitation/credentials/sampling/roots——④ 已披露面不扩）；单进程单目录无 Location/workspace 概念，恒私有连接——pool.ts 整件+endpoint 轮流握手 N/A；oauth 凭据选择面 N/A。
  2. **状态机移植（本票脊柱）**：manager 现四词面（pending/connected/failed/stopped）保持——stopped≈TS disabled（needs_auth 无 oauth 面 N/A）；引入 connection 回调表对位（connecting/connected/failed(error)/settled/closed(reason)/changed(tools)——prompts/resources 目录暂缓，py 只 tools）+stdio 通知挂点（stdio.py L483-486 现忽略→`on_notification` 回调）→`notifications/tools/list_changed` 热刷新+unexpected_exit→closed→failed 上报。**背景重连不引入**（§6 L304 既有裁决不变：至多下次显式调用重建；recovering/会话过期属 HTTP remote 面，stdio 无此语义——记档裁剪）。
  3. **协议协商裁剪**：现固定 MCP_PROTOCOL_VERSION="2025-06-18"→declare `2026-07-28`+接受 server 回复原值（auto 最小面：无 discover 探测、无 legacy pin 配置）；扩展协商面=initialize capabilities 加 extensions events 声明+serverInfo.capabilities.extensions 探测（决定 client.events 可用性）。
  4. **MCP Events 面（本票核心）**：`events/list` 分页+`events/stream` 长挂；通知路由=subscriptionId(=stream 请求 JSON-RPC id)→订阅 Queue 表（py stdio 已有按 id pending futures，加通知路由 dict）；60s 心跳判死；EventStreamRejected/Terminated 两终态异常类对位。flag 门=S4 常量先例（模块常量 `MCP_EVENTS_ENABLED=True`，非 config 面）。
  5. **watch_mcp_event 工具**：namespace `opencode`（automation 同先例）；list 面（server 省略→全 connected 服务器 EventType 清单，失败列空）+watch 面（cursor 续传/断流重开 1s-2s-4s 封顶 3 轮/Terminated 终态/session 删除中断）；**投递复用 T-C3-005 synthetic inbox seam**（manager 不触 inbox——经绑定的 SessionService admit_synthetic→S1 queue/promote 唤醒，绑定先例=subagent bind 面）；确定性消息 id 五元组 hash[:26]；wakeText 四要素逐字（防护句/清洗/截断/fence）；once 默认 true；permission=allow 常量规则（subagent 先例）。
  6. **订阅托管面裁剪**：index.ts 的 subscribe/follow/receive 服务级订阅表+实验 bus 事件+SubscriptionStatus **暂缓**——watch 工具自带流机器（工具内后台任务+删除中断），服务级托管是 server 推送面（py 无对应 server 事件流端点）；T-C3-004 event 触发需要时以 manager 通知回调为钩子再补（留口不预建）。
  7. **stdio.resolveCwd 移植**：StdioServerDefinition.cwd 直传→解析后值（相对 App working_dir 解析+`~` 展开）；帧/kill 面保持 py 现状（已有 16MiB 帽 McpFrameTooLargeError+两段树杀，行为对齐即可不逐字复刻）。
  8. **工具列取校验对齐**：_parse_tools 现全或无（一坏全败）→逐工具 skip+warn（上游对齐，坏工具不拖垮整服）；callTool toolDefinition/-32020 重试=TS SDK 内部缓存/校验层面，py 自研直发客户端 N/A。
- 评估集（先行）：`tests/integration/test_mcp_events_eval.py`——P0 能力探测（watch_mcp_event spec 注册/extensions 声明常量/manager on_notification 面）；M1=list 面（脚本服务器报 EventType）；M2=watch 单事件唤醒（once 默认→一事件 synthetic 入 parent inbox+唤醒 run，文本含防护句）；M3=cursor 续传+断流重开（退避后从 cursor 续）；M4=terminated 终态（不重开+watch stopped synthetic）；M5=注入防护钉（恶意字段单行化+>16KiB 截断+fence 加长）；M6=tools/list_changed 热刷新（通知→目录更新+changed 上报）。BEFORE 预期 capability-absent 全灰（现无通知回调/无 watch 工具/无扩展声明）。
  - **BEFORE 实测（2026-11-20，main `2993f6e`，7 passed 16.78s）**：P0=capability-absent（module false；extension_declared/stream_requested/notification_routed 三字面探针全 false）；M1-M5=capability-absent（watch 工具面全缺）；M6=**ignored**（真实面跑通：fixture `list_changed` 模式通知已送达且被现传输层忽略——目录恒 echo/fail/structured 三工具平，notification_sent=true）。评估集伴随扩 fixture 服务器两模式（`events_ok`：extensions 声明+events/list 分页+events/stream 长挂线程推 active/event/heartbeat 通知+clean_break/terminate/reject 三流为+cancelled 停+事件 id/cursor 全局单调跨流续列；`list_changed`：initialized 后通知+二次列加 late_tool）；共享 fixture 回归 72 passed（stdio/catalog/production/naming 四套件）。
  - **AFTER 实测（2026-11-20，worktree p18-s1，7 passed 6.54s）**：P0=**probed**（module true、`opencode__watch_mcp_event` 注册解析、三字面探针全 true）；M1=**probed**（deploys+drift 双页 EventType）；M2=**delivered-once**（wakes=1、wall 0.22s、防护句+fence 具真、inbox_id=`msg_mcp_33bc…`、metadata 四键 {source,server,event,eventID}）；M3=**reopened-with-cursor**（stream_requests=2、第二流 params cursor=c1、evt-1/evt-2 跨流连续）；M4=**terminated-clean**（1 流不重开、wakes=2=事件+stopped 通知）；M5=**sanitized**（payload_escaped/truncated_at_limit/fence_safe 全真、最长反引号 run=4>3）；M6=**refreshed**（late_tool 入目录）。BEFORE→AFTER delta 全列翻转，事件驱动免轮询面（token 双门）实证。
- 实现期落点（p18-s1，相对 649ab74）：
  - `src/opencode/mcp/stdio.py` 三处：ctor `on_notification(method, params)` 回调（通知帧派发路由，best-effort 防破读循环）；`request(timeout_s=None)` 无界等待（events/stream 长挂）+`on_id(request_id)` 回调（id 分配后帧发送前——subscriptionId 路由挂点）；`_dispatch` 通知分支由"忽略"改路由。
  - `src/opencode/mcp/manager.py` 重写：MCP_PROTOCOL_VERSION→`2026-07-28`+initialize 声明 `capabilities.extensions[EVENTS_EXTENSION]`；`_declares_events` 探测→entry.events（`events_supported()` 面）；`add_event_listener`/`_emit`（connecting/connected/failed{error}/closed{reason}/changed{kind} 五事件按序 inline）；`resolve_cwd(cwd, base)` 模块函数+ctor `base_dir`（决策 7）；`list_events` 分页（EVENT_LIST_PAGES=100）；`stream_events` 异步生成器（queue 路由/EVENT_STREAM_IDLE_S=60s 心跳判死/干净结束 return/REJECTED 码集→EventStreamRejectedError/terminated→EventStreamTerminatedError/finally 取消+notifications/cancelled）；`_make_notification_handler`（list_changed→`_refresh_tools` 热刷新保旧目录容错+changed 事件；events/* 按 (server,str(subscriptionId)) 路由，迟到丢弃）；`_parse_tools` 逐工具 skip（决策 8）；EventStreamRejectedError/EventStreamTerminatedError 两终态类。
  - `src/opencode/tools/builtin/mcp_events.py` 新：MCP_EVENTS_ENABLED/MCP_EVENTS_DATA_LIMIT=16384/MCP_EVENTS_REOPEN_DELAYS_S=(1,2,4) 三常量；wake_text/sanitize_line/fence/wake_inbox_id/stopped_text/watching_text 六纯函数（上游逐字：防护句/控制串折叠单空格/fence=max(3,内最长 run+1)+`json` 标/DATA_LIMIT 截断行/五元组 sha256[:26]·sort_keys 紧凑 JSON）；WatchMcpEventAdapter（list 面 unreachable 列空；watch 面 `_watch` 机器=干净关闭也重开+任何消息重置计数+terminated 不重开+耗尽/terminated 投 stopped（确定性 id eventId="stopped" 槽）+投递拒绝（会话删/服务关）静默停——interruptWhen(session.deleted) 映射+`reopen_delays_s` 可注入）；McpEventSessions Protocol。
  - `src/opencode/session/service.py`：`deliver_event_wake(session_id,*,inbox_id,text,metadata)` 投递端口（镜像 `_deliver_subagent_completion`：会话锁+closed 检查+steer/queue+promote；**幂等改 durable**——`_repos.inbox_enqueued_seen` 查 append-only 事件日志，重放已消费事件不二投（TS 确定性 message id「admitted once」对位；0004 表 pending-only 消费即删故不能查现表）；PY 差异记档：TS description 面由 metadata 承载）。
  - `src/opencode/storage/repos.py`：`inbox_enqueued_seen(session_id, inbox_id)` 持久查重面（`_SELECT_INBOX_ENQUEUED_DATA` 事件日志扫描，坏行 skip）。
  - 两 `__init__` 导出扩面（mcp 包 11 新名+builtin 包 WATCH_* 9 名——**WatchMcpEventAdapter 不入 builtin 导出面**：双射 census 守卫（test_workflow_tools 钉「每个导出 `*Adapter` 必入 `_builtin_adapters` 组合」）与该适配器的 manager/sessions 运行态构造冲突，装配对齐 automation 先例=宿主组合层手工面，S3 namespace 映射票收口——首全量双射红实证后收缩，收缩后定向 108P 复绿）。
  - fixture 服务器扩：`bad_tool` 模式（两条畸形目录项）+initialize params 落日志（协议声明断言面）+stream active 后一帧可恢复 error 通知+`silent` 流为（idle 判死）+分页第二页修复（eval BEFORE 期 wart：二页并入了一页条目——M1 名字在但重复，AFTER 前修复）。
  - 红契约集 `tests/integration/test_mcp_events.py` 21 passed：状态机矩阵/协议声明+支持探测/list 分页/stream 四通知+error 观察+心跳续命/idle 判死/rejected/terminated/双流按 subscriptionId 路由/bad_tool skip/list_changed 热刷新+changed/resolve_cwd/清洗纯函数五件/once 取消/断流重开带 cursor/terminated→stopped/投递拒绝静默停/真实 service 幂等单行。
  - 全量对照（p18-s1 三轮，基线 2523P/8S/6E/0F）：首轮 2549P/8S/6E/**2F**（tail 截断无名单）→波及面定向 255P 全绿排除；二轮 2550P/8S/6E/**1F**——1F=双射真波及（见上句收缩修复），另一首轮 failed 转 green=时序 flake；修复后定向 108P 复绿（workflow_tools+mcp events/eval+tool 契约）+三轮终跑 **2551P/8S/6E/0F**（756.80s，6E 恒=test_cli_install uv sync tzdata 网络面）。净增 28P=红集 21+自然增长 7。
- 红契约集：状态机转换矩阵（connecting→connected/failed/settled 时序+closed→failed）/通知路由表（subscriptionId 分派+迟到通知丢弃）/list 分页上限/stream 四通知+terminated 词面/once=false 持续投递/session 删除中断/消息 id 幂等（重放同 id 不二投）/清洗三件套钉死/列取逐工具 skip。
- 目标面：**无人值守事件驱动**（MCP 服务器事件→按需唤醒会话免轮询——token 双门直接手段；兼解 T-C3-004 event 触发前置）。

## 执行纪律
- 每票独立 worktree（p<NN>-s1）+三门禁+定向测+全量+CI 哨（repo 常规）。
- wire/API 形态以 TS 2.3.2 为参考、以 py 仓既有契约为准（同 PT-012 memory 先例：自定 wire 记档偏差）。
- 各票完成后台账时序条补录；本文件票状态随进度更新。
