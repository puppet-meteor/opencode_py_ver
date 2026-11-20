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
- 上游：表 `automation`（trigger∈at/every/cron/event + action∈prompt/code + status/next_due_at/退避字段/event_position）；KV 租约选主（TTL=2×MAX_SLEEP）+doorbell 30s；退避矩阵（prompt 失败 5min/code 1min×3→paused）；settle 乐观并发；恢复语义（at 补跑一次/every/cron 保相位跳槽/event 从 cursor replay）。
- 裁剪：第一期只做 prompt 动作（to session/subagent）+every/cron/at；event 触发与 code 动作分别缓到 T-C3-006 之后/暂缓。
- 工具面：automation_create/list/pause/resume/delete（policy ask）。
- 目标面：**无人值守运行态**（变更窗口定时巡检、告警触发诊断/回退的执行骨架）。

## 执行纪律
- 每票独立 worktree（p<NN>-s1）+三门禁+定向测+全量+CI 哨（repo 常规）。
- wire/API 形态以 TS 2.3.2 为参考、以 py 仓既有契约为准（同 PT-012 memory 先例：自定 wire 记档偏差）。
- 各票完成后台账时序条补录；本文件票状态随进度更新。
