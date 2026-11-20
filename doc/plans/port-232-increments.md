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
| 5 | 子代理完成回执 outbox+synthetic+深度链 | Med-High | M | T-C3-005 | P1 |
| 6 | mcp connection 状态机+events（watch_mcp_event） | Med（事件触发前置） | M(L) | T-C3-006 | P1 |
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

## 执行纪律
- 每票独立 worktree（p<NN>-s1）+三门禁+定向测+全量+CI 哨（repo 常规）。
- wire/API 形态以 TS 2.3.2 为参考、以 py 仓既有契约为准（同 PT-012 memory 先例：自定 wire 记档偏差）。
- 各票完成后台账时序条补录；本文件票状态随进度更新。
