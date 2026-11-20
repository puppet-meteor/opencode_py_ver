# 上游 2.3.2 增量移植计划（T-C3 系列）

> 2026-11-20 开池。源树：`../opencode-2.3.2/`（tarball v2.3.2，2026-11-18）；基线对照：`../opencode-2.2.2/`。
> **移植基线仍为 2.2.2**（裁决基准不变，见 ORCHESTRATION.md 头注）；2.3.2 仅作增量特性源。
> 目标对齐：`doc/GOAL.md`——每票标注服务的目标面（双门/无人值守/诊断分工/事件触发…）。

## 增量画像（core/src：301 文件 +19908/−11967；server +874；cli +1095；protocol +402）

深读结论（14 簇，2026-11-20 explore 席）总表：

| 簇 | 内容 | 助益 | 移植量 | 票号 | 优先级 |
|---|---|---|---|---|---|
| 13 | 工具输出截断保头+全文落盘+tail 尾窗读 | High（token 双门直接手段） | S-M | **T-C3-001** | **P0** |
| 2 | 后台 job 持久化+重启重投（shell/subagent 后台化） | High（长变更执行不丢结果） | M | **T-C3-002** | **P0** |
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

### T-C3-001 工具输出截断（簇13）
- 上游：`packages/core/src/tool-output.ts` truncate()——保开头（行/字节限制先到者切一次，预览不以换行结尾）；全文写 `<data>/tool-output/`；marker `[showing lines 1-N of M (last line cut); full output saved to <file>]`；metadata.truncated+outputPath；多 content part 按序保结构；已截断幂等跳过。
- shell tail 尾窗：`shell.ts` L822-880——`sizeOf`+`readRange(size-maxBytes)` 尾窗读+utf8 边界对齐，保尾 maxLines，行中起窗丢半行保行号，超大单行「最后 N 字节(共 Y)」。
- py 落点：invoker 归一化面（现 64KiB structured marker，丢结构化）→ 升级为保结构截断+落盘+outputPath；bash/pty 大输出同策略。
- 目标面：**token 开销门禁**（案例回放输出 token 直接受控+可事后取证）。

### T-C3-002 后台 job 持久化（簇2）
- 上游：表 `job`（job/sql.ts，迁移 20261030054400）；Recovery 描述符（shell/image/codemode/subagent）；Active 注册表（done/backgrounded 双 Deferred、block 10s 转后台、cancel）；持久化时机=转后台 upsert/settle 终态/inbox admit 后删行；重启 `pendingBackground` 读表重投。
- py 落点：复用 storage/sqlite 事务基建；shell/subagent 工具后台化+重启恢复。
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
