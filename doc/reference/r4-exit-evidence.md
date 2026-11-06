# R4 退出条件证据汇总（PHASE1-SLIM §4 R4）

> 用途：R5（core 0.1 结项）直接引用的 R4 退出条件证据映射。数据 = 契约/计划文档行号 + 源码 file/symbol 锚 + 测试（file::test 名或 file 级）+ 审查记录（`doc/reviews/*`）+ checkpoint 链；**无新主张**（本文件只汇总既有实现与测试的位置）。
> 范围锚定：`doc/PHASE1-SLIM.md` §4 L98-107（R4 节头 L98 + 交付 L100 + 验收 A/B/C L102-104 + 验证行 L105 + 会话选择 L107；L98-107 本任务全文核验）+ 必测故障矩阵 §4 L127/L128/L129/L131；切片定界以 `doc/plans/r4-slices.md` §2 为准（§0 同源锚定）。契约锚：`doc/CORE-CONTRACTS.md` §3.3 L210/L212/L214/L216-221 + §7 L333/L337/L339（本任务亲证行）。
> 基线：main 检查点 #153（`48b56e5`，S7 收口四门禁绿：pytest 1021[253.02s]/ruff clean/format 退出码 0[314 files]/mypy 105——调度器核实）代码面；worktree `r4-s8` 自检查点 #154 建出（HEAD=`dd305db` 任务书提交，相对 `48b56e5` 零代码差量、仅 doc/ 新增）。本任务（T-C1-042）**零代码变更**：新增本文件 + `doc/orchestration/progress/T-C1-042.md`，仅此两项写面；R4 收官态 **1021 passed**（见 §6）。
> 行号声明：PHASE1-SLIM/CORE-CONTRACTS 行号为原文逐行引用；源码锚以 file+symbol 级为主，本文件保留的逐行 file:line 均为本任务亲证（Select-String 现树重锚）：`cli.py` L152/L458/L488/L517/L631/L844、`session/service.py` L471/L505/L1067/L1162-1163/L1256、`app.py` L74/L146/L151/L189/L232/L235/L268、`session/runner.py` L912、`mcp/stdio.py` L105、`mcp/manager.py` L137/L168/L182/L197/L229、`mcp/bridge.py` L26/L68、`tools/registry.py` L263/L288/L328/L337、`tools/policy.py` L370/L658/L742、`config/loader.py` L317/L333/L897/L944、`session/recovery.py` L163/L194/L238、`storage/repos.py` L639/L661/L682/L703、`bus/eventlog.py` L256/L302/L389、`tests/integration/test_e2e_r4.py` L697/L795/L886/L981/L1091/L1207/L1305。继承 r1/r2/r3 evidence 与计划文档的行号未逐行复核项见进度 ③ 删节清单（实证：`_drive_run` r3 期引用 L1221-1239 → 现树 def L1256 漂移，本文件以现树值为准）。
> TS 锚纪律：R4 新引用 TS 五件（`mcp/{stdio,client,index}.ts`+`cli/src/mini.ts`+`schema/src/config/mcp.ts`）指纹已录 `doc/source-baseline.json`（调度器 #137/#142 亲算补录，51→58 条）；本文件零新 TS 引用、零「与 TS 同构」类未证断言；TS 差异以 `r4-slices.md` §4 差异备忘为准（§2.1/§2.2 所引差异表 3/4：recover/recovering HTTP 会话语义与 onToolsChanged 热刷新不移植）。

## 0. 切片交付映射（S2-S8，切片定界以 `doc/plans/r4-slices.md` §2 为准）

**S1/S1b（编号已占用不复用，r4-slices §0.1）**：S1=行#34 修复票（rg 优雅失败楔死 run——e2e 回归钉四面断言在册，922→924，T-B-035 PASS 0/0/0/2）；S1b=行#35 伴生收尾批（grep TS 对齐+冻结面去行号化+doc 修正批，924→926，T-B-037 PASS 0/0/0/1；#138 收口=**S2 起基线 926/92**）。

| 切片 | 交付 | 主要实现位置（symbol 锚） | 主要测试 | 审查记录 | 门禁/收口锚 |
|---|---|---|---|---|---|
| S2 stdio MCP transport/manager/bridge | 进程生命周期（spawn→initialize→tools/list→call_tool）+超时三分+close 升级杀树+stderr tail+断线 unknown 不自动重试+懒重建 | `mcp/stdio.py`（`StdioTransport` L105）；`mcp/manager.py`（`McpManager` L137：`add_server` L168/`start_server` L182/`call_tool` L197/`close` L229）；`mcp/bridge.py`（`to_tool_result` L26/`MCPToolAdapter` L68）；close 链挂接=`app.py` L232（`mcp_close=mcp_manager.close`）+`service.py` L471/L505/L1067/L1162-1163 | `tests/integration/test_mcp_stdio.py`（13）+`mcp_stdio_server.py` 测试自有 fixture（subprocess 实跑） | `doc/reviews/R4-S2-review.md`（T-B-038 PASS 0/0/0/2） | 939/98（merge `9b24926`→#141 收口） |
| S3 MCP catalog/命名空间/policy/快照接线 | `mcp__<name>` 两段式命名+归属索引+碰撞拒绝+server 启动指纹/catalog 版本绑定+unknown 重试重新 ask+ConfigSnapshot v3+events V3 upcaster | `tools/registry.py`（`register_mcp` L263/`replace_mcp_server` L288/`mcp_server_of` L328/`mcp_catalog_version` L337）；`tools/policy.py`（`ApprovalCoordinator` L370/`bind_mcp_tools` L658/`record_unknown` L742）；`config/loader.py`（`mcp_startup_fingerprint` L317/`mcp_fingerprint_drift` L333/`resolve_runtime_v3` L897）；`contracts/events.py`（快照 v3+upcaster） | `tests/contract/test_mcp_naming.py`（44）+`tests/integration/test_mcp_catalog.py`（3）+v1/v2/v3 fixtures | `doc/reviews/R4-S3-review.md`（T-B-039 PASS 0/0/0/3） | 986/100（merge `c7d7727`→#143 收口） |
| S4 启动恢复协调器（核心票） | 未结束 run→interrupted 绝不自动续跑+三窗对账+恢复三停住面+行#27 三条款（孤儿 pending 清理/cleanup_failed/幂等恢复批次） | `session/recovery.py`（`RecoveryCoordinator` L238/`RecoveryGate` L194/`sweep_crash_temp_files` L163）；`app.py`（`_startup_scan` L74/`recovery_gate.bind` L189/`sweep_roots` L151）；`config/loader.py`（`recover` L944）；`storage/repos.py`（`non_terminal_runs` L639/`pending_permissions` L661）；`session/service.py`（`_drive_run` L1256 终态策略红线面） | `tests/integration/test_recovery.py`（13，S4 期） | `doc/reviews/R4-S4-review.md`（T-B-040 PASS 0/0/3/3） | 999/102（merge `bc9e166`→#147 收口） |
| S4b 追加弧·window-2 补测微票 | started-无-finished→`unknown/unknown` 合成分支补测（零产品改动） | （纯测试票：`recovery.py` 只读） | `test_recovery.py` 第 14 测（`test_window2_started_tool_without_finished_synthesizes_unknown` L486） | `doc/reviews/R4-S4b-review.md`（T-B-042 PASS 0/0/0/1） | 1000/102（merge `9b56313`→#148/#149） |
| S5 EventLog follow 断连/慢消费者覆盖 | 慢消费者受控断开+follow 重连 at-least-once 按 session+seq 去重+durable DB 补读 vs ephemeral 可丢对照（零产品改动——纯覆盖票） | `bus/eventlog.py`（`read` L256/`follow` L302/`_page_corrupt` L389——R3 既有实现零改动承载）；上限=测试面注入（OP-R4-4） | `tests/integration/test_follow_recovery.py`（4：L172/L254/L311/L386） | `doc/reviews/R4-S5-review.md`（T-B-041 PASS 0/0/0/4） | seat 990/101→并入 1004/103（merge `fe70bda`→#149） |
| S6 CLI 会话选择+重启审批面 | `--continue`/`--session` 选择期（App 构造前只读）+exit 4 诊断+MCP 组合（definitions 构造不 spawn）+v3 采拥+重启审批失效（boot_id 隔离+旧 id 显式拒断） | `cli.py`（`_SELECTION_EXIT_CODE` L152/`_select_session_async` L458/`_select_session` L488/`_compose_mcp_manager` L517/`recovery_gate.wait()` L844）；`storage/repos.py`（`latest_session_created_by_working_dir` L703/`session_created_by_id` L682） | `tests/integration/test_cli_r4.py`（10，承载 T1-T8） | `doc/reviews/R4-S6-review.md`（T-B-043 PASS 0/0/1/3） | 1014/104（merge `d9d1747`→#151/#152） |
| S7 副作用不确定性+恢复 e2e | 三窗 kill/restart 全链 e2e+generation 半批注入+恢复后严格配对审计+close 序 App 级断言（纯测试票：src 零改动） | `tests/integration/test_e2e_r4.py`（1380 行，7 测：L697/L795/L886/L981/L1091/L1207/L1305；helpers `_audit_tool_pairing`/`_audit_wire_pairing`/`_ParkedAfterCallAdapter`/`_OrderRecordingManager`/`_OrderRecordingProvider`）；只读复用 `mcp_stdio_server.py`+`tests/r0/fake_openai_server.py` | 同左 | `doc/reviews/R4-S7-review.md`（T-B-044 PASS 0/0/0/3） | 1021/105（merge `59a7d61`→#153=`48b56e5`） |
| S8 退出证据收口（本任务 T-C1-042） | R4 退出证据汇总（本文件） | `doc/reference/r4-exit-evidence.md` | 门禁五件自跑（§6） | 本任务进度文件 | 1021/105/clean/format exit 0（本任务实测，§6） |

## 1. 验收 A（L102 原文）："显式信任一个 stdio server；命名空间无碰撞，text/structured 结果可回填；调用走同一 policy，生命周期退出后无子进程泄漏。"

- **显式信任一个 stdio server（S3/S6）**：MCP server 定义仅宿主层装载（`config/loader.py` host-tier mcp 面——项目层不能扩大宿主信任；专测 `test_mcp_naming.py::test_project_layer_cannot_extend_host_tier_mcp` L509 + r3 既有 roots/凭据面零回归在册）；`McpManager.add_server`（L168）显式 `StdioServerDefinition` 注入；S6 `_compose_mcp_manager`（cli.py L517）自 loader 构造 definitions——注册不 spawn（`test_cli_r4.py::test_mcp_composition_registers_definitions_without_spawning_and_closes` L666）。
- **命名空间无碰撞（S3）**：`registry.py` `register_mcp`（L263）两段式 `mcp__<name>` 命名+归属索引+碰撞拒绝（`replace_mcp_server` L288/`mcp_server_of` L328）；专测 `test_mcp_naming.py`（44）——`test_wire_name_and_ownership_roundtrip` L101/`test_same_server_same_name_rides_the_duplicate_rejection` L112/`test_second_server_cannot_claim_an_owned_name` L118；`test_mcp_catalog.py::test_two_servers_exposing_the_same_tool_name_are_rejected` L236。
- **text/structured 结果可回填（S2）**：`bridge.py` `to_tool_result`（L26）text/media/structured 回填 `ToolResult` + `MCPToolAdapter`（L68）工具适配；专测 `test_mcp_stdio.py::test_bridge_to_tool_result_faces` L226 + `test_mcp_tool_adapter_executes_through_manager` L264。
- **调用走同一 policy（S3）**：`policy.py` `ApprovalCoordinator`（L370）+`bind_mcp_tools`（L658，server+启动指纹+catalog 版本绑定）+`record_unknown`（L742，unknown 后同输入重试重新 ask）；mcp 命名空间默认 ask（`test_mcp_namespace_defaults_to_ask` L221）；catalog/指纹变更强制 re-ask（`test_catalog_version_change_forces_a_re_ask` L238/`test_fingerprint_change_forces_a_re_ask` L251/`test_identical_retry_after_unknown_re_asks` L264）。
- **生命周期退出后无子进程泄漏（S2）**：`McpManager.close`（L229）无条件清树；崩溃路径扫树（#145 CI ubuntu 腿 `test_crash_sweeps_tree` 回流热修后双腿绿——`mcp/stdio.py` 崩溃路径进程组清扫 +17/−1）；专测 `test_close_escalation_kills_tree` L394/`test_crash_sweeps_tree` L410；e2e 侧证 `test_e2e_r4.py` ⑥a/⑥b `_wait_pids_gone` 断言（L1300/L1378 一带）。
- **审查**：S2/S3/S6 评审记录台账为准（`doc/reviews/`）；T-C1-042 进度②。

## 2. 验收 B（L103 原文）："覆盖 tool.started 前、外部副作用后但终态前、tool.finished 提交后的 kill/restart；前者未执行、中间 unknown、后者复用结果，均不自动重跑。generation batch 的 finalized→generated 必须原子提交；注入半批故障后只能看到完整批或没有批。"

- **三窗语义锚（CORE-CONTRACTS §3.3 L216-221）**：tool.started 提交前退出=按未开始处理；started 后 finished 前退出=标记 unknown（即使命令可能已成功）；finished 已提交=直接读取结果绝不重跑；用户重试=新 execution_id。
- **三窗 kill/restart 全链 e2e（S7）**：`test_e2e_r4.py::test_w1_kill_while_approval_pending`（L697，tool.started 前=未执行：真实 kill 于稳定阻塞态→not_started 终态）；`test_w2_kill_while_tool_executing`（L795，外部副作用后但终态前=unknown——task-book 认可 wrapper 驻留，真实 fixture 调用已计数→`unknown/unknown`）；`test_w3_kill_while_generation_streaming`（L886，生成流中断面）。窗 1/窗 2/窗 3 对拍=`test_recovery.py::test_accepted_window_closes_interrupted_and_allows_a_new_prompt` L389/`test_running_window_rebuilds_the_interrupted_step_from_finalized_text` L416/`test_cancelling_window_full_terminal_batch_closed_first` L444/`test_window2_started_tool_without_finished_synthesizes_unknown` L486（窗 3=已有 finished 原样保留直接读取）。
- **均不自动重跑（S4）**：恢复协调器未结束 run→interrupted、绝不自动续跑（CORE-CONTRACTS §7 L333；`recovery.py` `RecoveryCoordinator` L238；`test_recovery.py` 全套+e2e 恢复段重启侧证 L1152-1191）。
- **generation batch 原子提交+半批注入（S7）**：finalized→generated 同批原子（PHASE1-SLIM 矩阵 L129）；`test_generation_half_batch_crash_leaves_no_orphan_rows`（L1091）——类级 monkeypatch `EventCommitter.commit` armed-once 注入半批故障→崩溃后 finalized==0 AND generated==0（无孤立行）→重启恢复补 `py.step.finished`/`py.run.finished`[interrupted] 不复活半批（L1168-1176）→恢复后健康对照面一批完整原子提交（L1178-1189）；调度器裁定=撕裂态单次原子 store.commit 不可达+直拟面窗 2 已闭合（T-B-044 两仲裁之一）。
- **审查**：S4/S4b/S7 评审记录台账为准；T-C1-042 进度②。

## 3. 验收 C（L104 原文）："待审批重启后失效，saved 决定只作审计需重新确认；相同 session 的重复 prompt、并发 cancel、慢订阅、丢通知和补读不造成重复消息/重复执行。恢复后再发新 prompt，由严格 fake server 检查每个合法 call 恰有一个 result，包含后续尚未 invoke 的计划调用。shutdown/crash race 验证 close 先持久取消和终态，再关闭 provider/MCP/DB；超时进入 cleanup_failed 或在下次启动恢复。"

- **待审批重启后失效+saved 只作审计（S6）**：`test_cli_r4.py::test_restart_chain_closes_orphan_reasks_and_refuses_the_old_permission_id`（L587）——boot_id 隔离下孤儿 pending 清扫（closed[cancelled]）+重新 ask+旧 permission_id reply 显式拒断（NotFoundError，新 boot coordinator 不回灌）；孤儿 pending 清扫面=S4 行#27 ①（`repos.py` `pending_permissions` L661+`recovery.py`）；saved 决定投影沿用 r3 在册面（同一事件路径写入，零独立 JSON 权限文件）。
- **重复 prompt/并发 cancel/慢订阅/丢通知/补读零重复（S5+既有面）**：重复 prompt/条件更新竞争=R2/R3 在册（r3-exit-evidence §2 引证）；S5 新覆盖=`test_follow_recovery.py`（4）——慢消费者受控断开后 durable 事件全可补读（L172）/唤醒溢出只耗 wakeup、poll 完整性不损（L254）/follow 重连 at-least-once+消费端按 session+seq 去重（L311）/ephemeral 不入 follow 而 live 预览可见对照（L386）；承载=`bus/eventlog.py` `follow` L302/`read` L256（R3 既有实现零改动）。
- **恢复后再发新 prompt 严格配对审计（S7）**：`test_e2e_r4.py::test_multi_call_plan_recovery_pairs_every_call`（L981）——恢复后新 prompt，严格 fake server 配对审计每个合法 call 恰有一个 result、含尚未 invoke 的计划调用 cancelled 合成（CORE-CONTRACTS §3.3 L214；`_audit_tool_pairing`/`_audit_wire_pairing` DB+wire 双侧审计）。
- **shutdown/crash race close 序（S7，CORE-CONTRACTS §7 L339 逐句）**：`test_close_folds_pending_approval_then_mcp_provider_db`（L1207）——durable 序 shutdown intent（`py.run.cancel_requested[shutdown]`）→closed approval（`py.permission.closed[cancelled]`）→not-started tool terminal→interrupted step/run；资源序 MCP（DB 仍可读）→provider（DB 仍可读）→DB handle（`_CLOSE_ORDER` 断言 L1263）；never-approved write 永不落盘（L1265）+fixture 零调用（L1266）。`test_close_timeout_folds_in_flight_mcp_call_unknown`（L1305）——超时面：已发出的真实调用在 grace 窗内折叠 `unknown/unknown` 终态+interrupted step/run+外部调用不重执行（fixture 计数恰 1，L1363）；`close_timeout_s=1.0` ctor 注入（`app.py` L146）。
- **超时 cleanup_failed 或下次启动恢复（S4）**：`test_recovery.py::test_drive_run_in_process_crash_cancel_reports_lifecycle_cleanup_failed`（L846）+`test_cross_process_crash_closed_by_the_app_startup_scan`（L878）+`test_gate_blocks_writes_until_the_scan_completes_and_fails_closed`（L904，`RecoveryGate` L194 fail-closed）+`test_app_startup_sweep_reports_counts`（L981，崩溃临时文件清扫=`sweep_crash_temp_files` L163）。
- **审查**：S4/S5/S6/S7 评审记录台账为准；T-C1-042 进度②。

## 4. 验证行（L105 原文）："测试自有的最小 MCP server + 故障注入；用计数器/临时文件证明副作用次数。MCP SDK 集成测在进程边界运行。"

- **测试自有最小 MCP server**：`tests/integration/mcp_stdio_server.py`（S2 建立，MCP_FIXTURE_MODE 多故障模式+MCP_FIXTURE_LOG 副作用计数；subprocess 实跑；S7 只读复用零新文件）。
- **故障注入**：①真实进程 kill（e2e W1/W2/W3 稳定阻塞态 `proc.kill`）；②类级 monkeypatch `EventCommitter.commit` armed-once（半批）；③`_ParkedAfterCallAdapter` wrapper 驻留（⑥b 超时面，marker 临时文件同步）；④fixture 故障模式（startup/catalog 超时、tools/list error、exit-during-call——`test_mcp_stdio.py` L301/L316/L328/L349）。
- **计数器/临时文件证明副作用次数**：`fixture.log` 副作用计数（⑥b 断言恰 1 次不重执行 L1363；⑥a 断言 0 次 L1266）+`wrapper.marker` 临时文件（park 到位证据）+`note.txt` never-approved 不存在断言（L1265）。
- **进程边界（OP-R4-1 裁决如实映射）**：用户裁决=自研最小 JSON-RPC stdio client（2026-11-05，零新依赖；`mcp/stdio.py` `StdioTransport` L105 承载）——L105「MCP SDK 集成测在进程边界运行」语义=自研 client+subprocess 实跑承载（stdio 子进程真实 spawn，Windows JobObject/POSIX 进程组；不照抄字面）。
- **审查**：T-C1-042 进度②（映射口径注记）。

## 5. 会话选择（L107 原文）："`--continue` 选当前 workspace 最近会话；没有则新建。`--session` 不存在报 NotFound。若选中目录/信任快照失效则停止诊断，不自动换目录。"

- **--continue 选当前 workspace 最近会话**：`test_cli_r4.py::test_continue_selects_the_latest_session_in_the_working_directory`（L362）；实现=`repos.py` `latest_session_created_by_working_dir`（L703，OP-R4-3 保守=working_dir 字符串全等）+`cli.py` `_select_session_async`（L458，App 构造前只读选择期）。
- **没有则新建**：`test_continue_with_no_prior_session_creates_a_fresh_v3_session`（L390，v3 快照新会话）。
- **--session 不存在报 NotFound**：`test_session_unknown_id_exits_4_with_one_stderr_line`（L422；`_SELECTION_EXIT_CODE=4` cli.py L152/L631）+有效 id 恢复不新建（`test_session_with_a_valid_id_resumes_without_creating` L439）+互斥组（`test_continue_and_session_are_mutually_exclusive` L558）。
- **选中目录/信任快照失效则停止诊断**：v1 快照停住（`test_continue_stops_on_a_v1_snapshot_session` L468）/MCP 指纹漂移停住（`test_continue_stops_on_mcp_fingerprint_drift` L488）/working_dir 失效停住（`test_session_stops_when_the_recorded_working_dir_is_gone` L527）；停住面生产者=`config/loader.py` `recover`（L944）+`mcp_fingerprint_drift`（L333）；单测对拍=`test_config_loader.py::test_recover_replays_the_recorded_v2_snapshot`/`test_recover_stops_on_a_v1_snapshot`/`test_recover_stops_when_the_working_directory_is_gone`（L375/L388/L399）。
- **不自动换目录**：三停住面统一 exit 4 诊断输出停止（不换目录、不自动重试；与上项同测承载——T-B-043 契约逐句：L107 三句→T1-T4）；选择期完成前不构造 App（`recovery_gate.wait()` cli.py L844 在选中后才进入启动扫描等待）。
- **审查**：S6 评审记录台账为准（T-B-043 契约逐句锚）；T-C1-042 进度②。

## 6. 门禁与 CI（R4 收官态实测，T-C1-042 自跑，worktree `r4-s8`）

| 命令 | 结果 |
|---|---|
| `uv run python -m pytest -q` | **1021 passed**（零代码变更零回归），243.51s（temproot 偏差披露见进度④） |
| `uv run ruff check .` | **All checks passed!** |
| `uv run ruff format --check .` | **317 files already formatted**（退出码 0；判定面=退出码 0——ORCHESTRATION §2，计数非判据；计数算术披露见进度④） |
| `uv run python -m mypy src tools/r0 tests/r0 tests/contract tests/integration` | **Success: no issues found in 105 source files** |
| `uv run python -m pytest --collect-only -q` | **1021 collected**（1.08s；附录 A 数据源+对账闭合） |

- **门禁数字演化（r4-slices §0.1 checkpoint 链+ORCHESTRATION 任务表，本任务亲读）**：R3 收官 922（#131）→ 伴生批 S1 924（T-B-035）/S1b 926（#138，**S2 起基线**）→ S2 **939/98**（merge `9b24926`→#141）→ S3 **986/100**（merge `c7d7727`→#143）→ S4 **999/102**（merge `bc9e166`→#147）→ S4b **1000**（merge `9b56313`→#148）→ S5 并入 **1004/103**（merge `fe70bda`→#149；seat 990=986+4 零产品改动）→ S6 **1014/104**（merge `d9d1747`→#151/#152）→ S7 **1021/105**（merge `59a7d61`→#153=`48b56e5`，**S8 起基线**）→ S8 本任务 **1021**（零代码变更零回归）。mypy files 演化 92→98→100→102→103→104→105 同链在册。
- **collect-only 对账**：1021 collected == 门禁 pytest 1021 passed，闭合（附录 A 函数级枚举，file 级分组，R3-S8 先例形）。
- **CI（调度器核实口径，任务书 §2.8；结论以调度器 #154 台账记录为准引用）**：#154 推送 `c0db29a..48b56e5` 触发 run **42227544216** 双 OS 绿（`test_e2e_r4.py` 7 测 Unix 腿首跑全绿）。R4 途中 CI 链（r4-slices §0.1）：41984552949（#139 双绿）→ 42143155157（#145 ubuntu 腿红 `test_crash_sweeps_tree` 1 败→回流热修 `mcp/stdio.py` +17/−1→42152172197 双腿绿）→ 42156729748（#148）→ 42172518444（#150，S5 5 新测 Unix 首跑绿）→ 42188197712（#152，`test_cli_r4.py` 10 测 Unix 首跑绿）。本票（S8）推送与 CI 复验归调度器合并流程记档（r2 §5 先例形）。

## 7. 已知边界与 R5+ 移交台账（非 R4 退出条件缺口）

- **OP-R4-1..6 终值**：① OP-R4-1=自研最小 JSON-RPC stdio client（用户裁决，零新依赖；`mcp/stdio.py` 承载）。② OP-R4-2=启动指纹 canonical=argv+env 键形哈希（env 值永不入 digest、cwd 排除=保守缺省；`loader.py` `mcp_startup_fingerprint` L317；`test_fingerprint_tracks_argv_and_env_keys_not_values_or_cwd` L520；同 argv 异 cwd 代价面=S4 重开审视记档）。③ OP-R4-3=working_dir 字符串全等（保守；`repos.py` L703）。④ OP-R4-4=受控上限注入（S5 以注入 queue_limit 构造受控断开；产品默认面零改动）。⑤ OP-R4-5=TS 常量 Python 取值保守裁量+docstring 注记（`mcp/stdio.py` 常量注记，T-B-038 亲证在册）。⑥ OP-R4-6=App 启动同步全量扫描+session_status 红线不动（`app.py` `_startup_scan` L74；CORE-CONTRACTS §7 L313 红线）。
- **bind_mcp_tools 生产 spawn+bind 接线 R5+ 立项评估**：S7 测试以 App 组合根直组 `MCPToolAdapter` 承载 run 级路由；生产 CLI 注册不 spawn=T8 既定（#152 调度器裁决记档）。
- **src 存量 34 处历史 TS mapping 注释清理评估**（T-B-044 nit-2；非义务低优先，零 M 亲证非 R4 新增）。
- **故障矩阵 L131 后两句（新 catalog/boot_id 旧批准失效）e2e 直载 R5+**（T-B-044 nit-3；本票引用承载在册：S3 `test_mcp_naming.py` catalog/指纹 re-ask 面+S6 T7 boot_id 隔离+S2 断线 unknown 不自动重试；R5+ catalog 轮换票顺手直载）。
- **其余 float-reported 边界以各评审报告为准逐件引注**（R4-S2..S7 review nit/处方清单：如 S2 协议面 12 项未覆盖[cancellation/progress/ping]、POSIX 腿 CI 承载、S6 T5② 漂移面停止行内容未钉等——详见 `doc/reviews/` 各件）。
- **r3 §6 边界行的 R4 处置**：「优雅工具失败楔死 run」已由 R4-S1 修复闭合（行#34，e2e 回归钉四面断言在册）；「rg 遇锁定文件容忍语义」表述更正已随 S1b 落（T-C1-034 doc 修正批）；r3 §6 其余开放池（OP-\* 不预写裁决项）以 r3-slices §6/r4-slices §6 为准。

## 附录 A 函数级测试枚举（uv run python -m pytest --collect-only -q 一次性全量）

R4 期间新增测试 99（922→1021）：S1 +2 / S1b +2 / S2 +13（`test_mcp_stdio.py`）/ S3 +47（`test_mcp_naming.py` 44+`test_mcp_catalog.py` 3）/ S4 +13+S4b +1（`test_recovery.py` 14）/ S5 +4（`test_follow_recovery.py`）/ S6 +10（`test_cli_r4.py`）/ S7 +7（`test_e2e_r4.py`）；存量修改：`test_config_loader.py`（v3 recover 面+期望翻转）、`test_events.py`/`test_config.py`（v3 翻牌注释+更名一枚）。测试基建只读复用：`tests/r0/fake_openai_server.py`、`tests/integration/mcp_stdio_server.py`。

全量 file 级分组枚举（总数 **1021 == §6 门禁 pytest 1021 passed，闭合**；沿 R3-S8 先例，collect 输出机械转录）：

### tests/contract/test_ai_adapter.py (36)
- tests/contract/test_ai_adapter.py::test_validate_sequence_rejects_violations[index-skip]
- tests/contract/test_ai_adapter.py::test_validate_sequence_rejects_violations[index-duplicate]
- tests/contract/test_ai_adapter.py::test_validate_sequence_rejects_violations[request-id-mismatch]
- tests/contract/test_ai_adapter.py::test_validate_sequence_rejects_violations[double-terminal]
- tests/contract/test_ai_adapter.py::test_validate_sequence_rejects_violations[chunk-after-terminal]
- tests/contract/test_ai_adapter.py::test_validate_sequence_rejects_violations[no-terminal]
- tests/contract/test_ai_adapter.py::test_validate_sequence_accepts_contract_sequence
- tests/contract/test_ai_adapter.py::test_golden_text_usage_after_finish
- tests/contract/test_ai_adapter.py::test_golden_text_usage_before_finish
- tests/contract/test_ai_adapter.py::test_golden_interleaved_tool_calls
- tests/contract/test_ai_adapter.py::test_golden_eof_without_terminal
- tests/contract/test_ai_adapter.py::test_golden_429_retry_then_success
- tests/contract/test_ai_adapter.py::test_golden_wire_double_finish_rejected
- tests/contract/test_ai_adapter.py::test_golden_wire_content_after_finish_rejected
- tests/contract/test_ai_adapter.py::test_golden_wire_tool_call_after_finish_rejected
- tests/contract/test_ai_adapter.py::test_error_chunk_is_the_sole_terminal
- tests/contract/test_ai_adapter.py::test_header_timeout
- tests/contract/test_ai_adapter.py::test_chunk_timeout
- tests/contract/test_ai_adapter.py::test_total_deadline_mid_stream
- tests/contract/test_ai_adapter.py::test_retry_without_retry_after_header_waits_zero
- tests/contract/test_ai_adapter.py::test_retry_wait_truncated_to_total_deadline
- tests/contract/test_ai_adapter.py::test_retry_exhaustion_caps_at_two_retries
- tests/contract/test_ai_adapter.py::test_no_retry_after_first_content
- tests/contract/test_ai_adapter.py::test_wire_tool_name_mapping_round_trip
- tests/contract/test_ai_adapter.py::test_unparseable_wire_name_passes_through
- tests/contract/test_ai_adapter.py::test_tool_call_without_id_is_rejected
- tests/contract/test_ai_adapter.py::test_cancellation_propagates_and_cleans_up
- tests/contract/test_ai_adapter.py::test_stream_generator_supports_early_close
- tests/contract/test_ai_adapter.py::test_sdk_types_do_not_leak
- tests/contract/test_ai_adapter.py::test_wire_body_message_assembly
- tests/contract/test_ai_adapter.py::test_file_content_replaced_with_placeholder
- tests/contract/test_ai_adapter.py::test_wire_body_request_params
- tests/contract/test_ai_adapter.py::test_temperature_omitted_when_unset
- tests/contract/test_ai_adapter.py::test_retry_after_seconds_prefers_ms_header
- tests/contract/test_ai_adapter.py::test_connect_refused_exhausts_retries
- tests/contract/test_ai_adapter.py::test_5xx_error_surfaced_without_retry

### tests/contract/test_bus.py (24)
- tests/contract/test_bus.py::TestSubscriptionTiming::test_notifications_before_entering_are_invisible
- tests/contract/test_bus.py::TestSubscriptionTiming::test_notifications_between_subscribe_and_enter_are_invisible
- tests/contract/test_bus.py::TestBoundedQueue::test_exact_fill_passes_and_the_queue_recovers
- tests/contract/test_bus.py::TestBoundedQueue::test_overflow_disconnects_and_reports_slow_consumer
- tests/contract/test_bus.py::TestBoundedQueue::test_overflow_details_on_ephemeral_have_no_seq
- tests/contract/test_bus.py::TestBoundedQueue::test_overflowing_subscriber_does_not_affect_the_draining_one
- tests/contract/test_bus.py::TestContextExit::test_exit_unregisters_and_releases_the_queue
- tests/contract/test_bus.py::TestContextExit::test_fresh_subscription_after_exit_sees_only_new_events
- tests/contract/test_bus.py::TestContextExit::test_subscription_context_cannot_be_entered_twice
- tests/contract/test_bus.py::TestTypeFilter::test_type_filter_delivers_only_matching_types
- tests/contract/test_bus.py::TestTypeFilter::test_empty_type_filter_matches_nothing
- tests/contract/test_bus.py::TestTypeFilter::test_single_string_type_filter_is_rejected
- tests/contract/test_bus.py::TestDelivery::test_committed_durable_and_ephemeral_arrive_in_publish_order
- tests/contract/test_bus.py::TestDelivery::test_duplicate_ids_within_one_call_are_delivered_once
- tests/contract/test_bus.py::TestDelivery::test_notifications_are_isolated_per_session
- tests/contract/test_bus.py::TestValidation::test_notify_rejects_events_of_another_session
- tests/contract/test_bus.py::TestValidation::test_registry_validation_fails_closed
- tests/contract/test_bus.py::TestValidation::test_validation_happens_before_any_delivery
- tests/contract/test_bus.py::TestValidation::test_without_registry_the_bus_does_not_validate_types
- tests/contract/test_bus.py::TestValidation::test_queue_limit_must_be_a_positive_integer
- tests/contract/test_bus.py::TestValidation::test_subscribe_rejects_empty_session_id
- tests/contract/test_bus.py::TestStructuralGuards::test_bus_modules_are_structurally_db_free
- tests/contract/test_bus.py::TestStructuralGuards::test_volatile_bus_has_no_dropped_ts_surfaces
- tests/contract/test_bus.py::TestStructuralGuards::test_event_union_covers_both_envelopes

### tests/contract/test_config.py (16)
- tests/contract/test_config.py::TestConfigSnapshotV2Model::test_legal_snapshot_round_trips
- tests/contract/test_config.py::TestConfigSnapshotV2Model::test_extra_field_is_rejected
- tests/contract/test_config.py::TestConfigSnapshotV2Model::test_missing_limits_is_rejected
- tests/contract/test_config.py::TestConfigSnapshotV2Model::test_bool_is_rejected_for_integer_limits
- tests/contract/test_config.py::TestConfigSnapshotV2Model::test_empty_roots_entry_is_rejected
- tests/contract/test_config.py::TestConfigSnapshotV2Model::test_non_empty_permission_rules_are_legal_in_v2
- tests/contract/test_config.py::TestConfigSnapshotV2Model::test_v1_fingerprint_is_frozen
- tests/contract/test_config.py::TestSessionCreatedV2Wire::test_valid_fixture_decodes_and_round_trips
- tests/contract/test_config.py::TestSessionCreatedV2Wire::test_bad_hash_fixture_is_rejected
- tests/contract/test_config.py::TestSessionCreatedV2Wire::test_bad_extension_fixture_is_rejected
- tests/contract/test_config.py::TestSessionCreatedV2Wire::test_v1_fixture_still_decodes_version_one
- tests/contract/test_config.py::TestSessionCreatedV2Wire::test_unregistered_version_is_still_unknown
- tests/contract/test_config.py::TestSessionCreatedV2Wire::test_both_versions_are_registered_side_by_side
- tests/contract/test_config.py::TestUpcaster::test_v2_row_passes_through_with_its_snapshot
- tests/contract/test_config.py::TestUpcaster::test_v1_row_upcasts_to_no_runtime_face
- tests/contract/test_config.py::test_snapshot_limits_wire_shape_pins_the_runtime_knobs

### tests/contract/test_context.py (46)
- tests/contract/test_context.py::TestRequestAssembly::test_history_fixture_maps_to_request_field_by_field
- tests/contract/test_context.py::TestRequestAssembly::test_no_system_message_when_prompt_unset
- tests/contract/test_context.py::TestRequestAssembly::test_empty_system_prompt_omitted
- tests/contract/test_context.py::TestRequestAssembly::test_idle_message_excluded_from_request
- tests/contract/test_context.py::TestRequestAssembly::test_empty_user_text_dropped
- tests/contract/test_context.py::TestRequestAssembly::test_empty_assistant_text_and_reasoning_parts_dropped
- tests/contract/test_context.py::TestRequestAssembly::test_assistant_message_with_only_empty_parts_contributes_nothing
- tests/contract/test_context.py::TestRequestAssembly::test_empty_history_builds_empty_messages
- tests/contract/test_context.py::TestRequestAssembly::test_no_tools_is_an_empty_list_state
- tests/contract/test_context.py::TestRequestAssembly::test_emitted_messages_never_mix_part_kinds
- tests/contract/test_context.py::TestRequestAssembly::test_unknown_history_entry_rejected
- tests/contract/test_context.py::TestRequestAssembly::test_request_id_must_be_non_empty
- tests/contract/test_context.py::TestToolResultPairing::test_every_legal_tool_call_id_has_exactly_one_result
- tests/contract/test_context.py::TestToolResultPairing::test_every_terminal_tool_state_replays_call_and_result[succeeded]
- tests/contract/test_context.py::TestToolResultPairing::test_every_terminal_tool_state_replays_call_and_result[failed]
- tests/contract/test_context.py::TestToolResultPairing::test_every_terminal_tool_state_replays_call_and_result[denied]
- tests/contract/test_context.py::TestToolResultPairing::test_every_terminal_tool_state_replays_call_and_result[cancelled]
- tests/contract/test_context.py::TestToolResultPairing::test_every_terminal_tool_state_replays_call_and_result[unknown]
- tests/contract/test_context.py::TestToolResultPairing::test_non_terminal_tool_part_rejected[pending]
- tests/contract/test_context.py::TestToolResultPairing::test_non_terminal_tool_part_rejected[running]
- tests/contract/test_context.py::TestToolResultPairing::test_duplicate_tool_call_id_rejected
- tests/contract/test_context.py::TestToolResultPairing::test_terminal_tool_part_with_empty_content_carried_verbatim
- tests/contract/test_context.py::TestTokenBudget::test_exactly_at_budget_builds_with_full_output_reserve
- tests/contract/test_context.py::TestTokenBudget::test_over_budget_raises_context_limit_exceeded
- tests/contract/test_context.py::TestTokenBudget::test_replayed_reasoning_counts_toward_input_estimate
- tests/contract/test_context.py::TestTokenBudget::test_catalog_counts_toward_estimate
- tests/contract/test_context.py::TestTokenBudget::test_ceil_rounding_overflows_on_a_non_multiple_of_four
- tests/contract/test_context.py::TestTokenBudget::test_ceil_rounding_reaches_exactly_at_budget_on_a_non_multiple_of_four
- tests/contract/test_context.py::TestCatalogSnapshot::test_mutating_input_tools_after_build_does_not_affect_request
- tests/contract/test_context.py::TestCatalogSnapshot::test_mutating_nested_input_schema_after_build_does_not_affect_request
- tests/contract/test_context.py::TestDottedNames::test_tool_call_part_name_is_dotted_and_matches_catalog
- tests/contract/test_context.py::TestDottedNames::test_dotted_names_round_trip_through_wire_mapping
- tests/contract/test_context.py::TestRuntimeSettings::test_context_limit_has_no_default_host_must_configure
- tests/contract/test_context.py::TestRuntimeSettings::test_settings_require_positive_input_budget
- tests/contract/test_context.py::TestRuntimeSettings::test_invalid_settings_values_rejected[context_limit-0]
- tests/contract/test_context.py::TestRuntimeSettings::test_invalid_settings_values_rejected[context_limit-True]
- tests/contract/test_context.py::TestRuntimeSettings::test_invalid_settings_values_rejected[context_limit-100.0]
- tests/contract/test_context.py::TestRuntimeSettings::test_invalid_settings_values_rejected[output_reserve-0]
- tests/contract/test_context.py::TestRuntimeSettings::test_invalid_settings_values_rejected[output_reserve-True]
- tests/contract/test_context.py::TestRuntimeSettings::test_invalid_settings_values_rejected[safety_margin--1]
- tests/contract/test_context.py::TestRuntimeSettings::test_invalid_settings_values_rejected[safety_margin-True]
- tests/contract/test_context.py::TestRuntimeSettings::test_invalid_settings_values_rejected[timeout_s-0]
- tests/contract/test_context.py::TestRuntimeSettings::test_invalid_settings_values_rejected[timeout_s--5.0]
- tests/contract/test_context.py::TestRuntimeSettings::test_invalid_settings_values_rejected[timeout_s-True]
- tests/contract/test_context.py::TestRuntimeSettings::test_invalid_settings_values_rejected[temperature-True]
- tests/contract/test_context.py::TestRuntimeSettings::test_timeout_s_must_be_finite

### tests/contract/test_events.py (181)
- tests/contract/test_events.py::TestPositiveFixtures::test_round_trip_is_lossless[py.prompt.accepted-events_prompt_accepted_valid.json]
- tests/contract/test_events.py::TestPositiveFixtures::test_round_trip_is_lossless[py.session.created-events_session_created_valid.json]
- tests/contract/test_events.py::TestPositiveFixtures::test_fixtures_expose_py_naming_and_version_one
- tests/contract/test_events.py::TestPositiveFixtures::test_session_created_fixture_hash_is_real_sha256
- tests/contract/test_events.py::TestPositiveFixtures::test_prompt_accepted_fixture_carries_run_id_metadata
- tests/contract/test_events.py::TestNegativeFixtures::test_unknown_type_rejected
- tests/contract/test_events.py::TestNegativeFixtures::test_unknown_version_rejected
- tests/contract/test_events.py::TestNegativeFixtures::test_missing_required_field_rejected[events_session_created_missing_field.json]
- tests/contract/test_events.py::TestNegativeFixtures::test_missing_required_field_rejected[events_prompt_accepted_missing_field.json]
- tests/contract/test_events.py::TestNegativeFixtures::test_invalid_extension_value_rejected[events_session_created_bad_extension.json]
- tests/contract/test_events.py::TestNegativeFixtures::test_invalid_extension_value_rejected[events_prompt_accepted_bad_extension.json]
- tests/contract/test_events.py::TestNegativeFixtures::test_snapshot_hash_mismatch_rejected
- tests/contract/test_events.py::TestNegativeFixtures::test_missing_metadata_run_id_rejected
- tests/contract/test_events.py::TestEnvelopes::test_draft_has_no_seq_and_durable_adds_it
- tests/contract/test_events.py::TestEnvelopes::test_durable_seq_must_be_positive
- tests/contract/test_events.py::TestEnvelopes::test_naive_datetime_rejected
- tests/contract/test_events.py::TestEnvelopes::test_created_at_normalized_to_utc
- tests/contract/test_events.py::TestEnvelopes::test_metadata_rejects_non_json_python_values
- tests/contract/test_events.py::TestEphemeralEnvelope::test_round_trip
- tests/contract/test_events.py::TestEphemeralEnvelope::test_version_field_is_rejected
- tests/contract/test_events.py::TestEphemeralEnvelope::test_direct_construction_has_no_seq_or_version
- tests/contract/test_events.py::TestEphemeralEnvelope::test_durable_decode_rejects_ephemeral_definition
- tests/contract/test_events.py::TestEphemeralEnvelope::test_ephemeral_decode_rejects_durable_definition
- tests/contract/test_events.py::TestRegistry::test_duplicate_durable_definition_rejected
- tests/contract/test_events.py::TestRegistry::test_duplicate_ephemeral_definition_rejected
- tests/contract/test_events.py::TestRegistry::test_malformed_json_envelope_rejected
- tests/contract/test_events.py::TestRegistry::test_non_json_constant_rejected
- tests/contract/test_events.py::TestRegistry::test_duplicate_json_object_keys_rejected
- tests/contract/test_events.py::TestRegistry::test_missing_type_and_version_rejected
- tests/contract/test_events.py::TestRegistry::test_boolean_version_rejected
- tests/contract/test_events.py::TestRegistry::test_versioned_type_matches_ts_format
- tests/contract/test_events.py::TestSnapshotHashing::test_canonical_json_sorts_keys_and_compacts
- tests/contract/test_events.py::TestSnapshotHashing::test_canonical_json_rejects_nan
- tests/contract/test_events.py::TestSnapshotHashing::test_r1_snapshot_is_unique_and_digest_pinned
- tests/contract/test_events.py::TestSnapshotHashing::test_snapshot_requires_both_fields_and_literal_one
- tests/contract/test_events.py::TestSnapshotHashing::test_snapshot_rejects_non_empty_rules_and_extra_keys
- tests/contract/test_events.py::TestCoreError::test_round_trip
- tests/contract/test_events.py::TestCoreError::test_details_default_to_empty_object
- tests/contract/test_events.py::TestCoreError::test_r1_codes_are_available
- tests/contract/test_events.py::TestCoreError::test_code_registry_matches_contract_section_8
- tests/contract/test_events.py::TestCoreError::test_unknown_code_rejected
- tests/contract/test_events.py::TestUserMessage::test_round_trip
- tests/contract/test_events.py::TestUserMessage::test_type_tag_enforced
- tests/contract/test_events.py::TestUserMessage::test_prompt_accepted_data_nests_user_message
- tests/contract/test_events.py::TestContractHardening::test_bool_version_rejected_on_construction
- tests/contract/test_events.py::TestContractHardening::test_bool_seq_rejected_on_construction
- tests/contract/test_events.py::TestContractHardening::test_string_version_rejected_on_decode_path
- tests/contract/test_events.py::TestContractHardening::test_bool_seq_rejected_on_decode_path
- tests/contract/test_events.py::TestContractHardening::test_lone_surrogate_fails_closed_in_canonical_bytes
- tests/contract/test_events.py::TestContractHardening::test_empty_event_id_rejected
- tests/contract/test_events.py::TestContractHardening::test_event_and_session_id_prefixes_enforced
- tests/contract/test_events.py::TestContractHardening::test_ephemeral_id_prefixes_enforced
- tests/contract/test_events.py::TestContractHardening::test_user_message_id_prefix_enforced
- tests/contract/test_events.py::TestContractHardening::test_user_message_files_key_rejected
- tests/contract/test_events.py::TestContractHardening::test_user_message_empty_text_allowed
- tests/contract/test_events.py::TestContractHardening::test_run_id_prefix_enforced_in_data
- tests/contract/test_events.py::TestContractHardening::test_metadata_run_id_validated_wherever_present
- tests/contract/test_events.py::TestContractHardening::test_request_id_has_no_prefix_pattern
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.reasoning.delta-events_reasoning_delta_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.reasoning.finalized-events_reasoning_finalized_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.run.cancel_requested-events_run_cancel_requested_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.run.finished-events_run_finished_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.run.started-events_run_started_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.step.finished-events_step_finished_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.step.generated-events_step_generated_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.step.started-events_step_started_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.text.delta-events_text_delta_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.text.finalized-events_text_finalized_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.tool.finished-events_tool_finished_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.tool.input.delta-events_tool_input_delta_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.tool.progress-events_tool_progress_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.tool.requested-events_tool_requested_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_round_trip_is_lossless[py.tool.started-events_tool_started_valid.json]
- tests/contract/test_events.py::TestR2PositiveFixtures::test_fixtures_expose_py_naming_version_and_run_metadata
- tests/contract/test_events.py::TestR2PositiveFixtures::test_run_metadata_matches_data_run_id
- tests/contract/test_events.py::TestR2PositiveFixtures::test_step_fixtures_carry_assistant_messages
- tests/contract/test_events.py::TestR2PositiveFixtures::test_tool_finished_fixture_is_model_readable
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.reasoning.delta-events_reasoning_delta_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.reasoning.finalized-events_reasoning_finalized_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.run.cancel_requested-events_run_cancel_requested_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.run.finished-events_run_finished_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.run.started-events_run_started_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.step.finished-events_step_finished_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.step.generated-events_step_generated_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.step.started-events_step_started_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.text.delta-events_text_delta_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.text.finalized-events_text_finalized_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.tool.finished-events_tool_finished_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.tool.input.delta-events_tool_input_delta_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.tool.progress-events_tool_progress_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.tool.requested-events_tool_requested_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_missing_required_field_rejected[py.tool.started-events_tool_started_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.reasoning.delta-events_reasoning_delta_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.reasoning.finalized-events_reasoning_finalized_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.run.cancel_requested-events_run_cancel_requested_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.run.finished-events_run_finished_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.run.started-events_run_started_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.step.finished-events_step_finished_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.step.generated-events_step_generated_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.step.started-events_step_started_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.text.delta-events_text_delta_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.text.finalized-events_text_finalized_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.tool.finished-events_tool_finished_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.tool.input.delta-events_tool_input_delta_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.tool.progress-events_tool_progress_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.tool.requested-events_tool_requested_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_unknown_data_field_rejected[py.tool.started-events_tool_started_valid.json]
- tests/contract/test_events.py::TestR2NegativeFixtures::test_durability_mismatch_rejected_both_directions
- tests/contract/test_events.py::TestR2NegativeFixtures::test_ephemeral_version_key_rejected
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_run_scoped_set_covers_the_full_r2_subset
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.reasoning.delta-events_reasoning_delta_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.reasoning.finalized-events_reasoning_finalized_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.run.cancel_requested-events_run_cancel_requested_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.run.finished-events_run_finished_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.run.started-events_run_started_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.step.finished-events_step_finished_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.step.generated-events_step_generated_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.step.started-events_step_started_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.text.delta-events_text_delta_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.text.finalized-events_text_finalized_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.tool.finished-events_tool_finished_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.tool.input.delta-events_tool_input_delta_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.tool.progress-events_tool_progress_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.tool.requested-events_tool_requested_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_metadata_rejected[py.tool.started-events_tool_started_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.reasoning.delta-events_reasoning_delta_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.reasoning.finalized-events_reasoning_finalized_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.run.cancel_requested-events_run_cancel_requested_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.run.finished-events_run_finished_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.run.started-events_run_started_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.step.finished-events_step_finished_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.step.generated-events_step_generated_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.step.started-events_step_started_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.text.delta-events_text_delta_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.text.finalized-events_text_finalized_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.tool.finished-events_tool_finished_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.tool.input.delta-events_tool_input_delta_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.tool.progress-events_tool_progress_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.tool.requested-events_tool_requested_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_missing_run_id_fixture_rejected[py.tool.started-events_tool_started_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.reasoning.delta-events_reasoning_delta_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.reasoning.finalized-events_reasoning_finalized_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.run.cancel_requested-events_run_cancel_requested_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.run.finished-events_run_finished_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.run.started-events_run_started_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.step.finished-events_step_finished_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.step.generated-events_step_generated_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.step.started-events_step_started_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.text.delta-events_text_delta_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.text.finalized-events_text_finalized_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.tool.finished-events_tool_finished_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.tool.input.delta-events_tool_input_delta_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.tool.progress-events_tool_progress_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.tool.requested-events_tool_requested_valid.json]
- tests/contract/test_events.py::TestR2RunScopedMetadata::test_bad_run_id_metadata_rejected_whenever_present[py.tool.started-events_tool_started_valid.json]
- tests/contract/test_events.py::TestR2Registration::test_r1_definitions_unchanged
- tests/contract/test_events.py::TestR2Registration::test_r2_definitions_shape
- tests/contract/test_events.py::TestR2Registration::test_r1_and_r2_types_are_disjoint_and_permission_rows_absent
- tests/contract/test_events.py::TestR2Registration::test_default_registry_resolves_all_r2_types
- tests/contract/test_events.py::TestR2Registration::test_unknown_version_for_r2_type_rejected
- tests/contract/test_events.py::TestR2Registration::test_unknown_type_still_rejected
- tests/contract/test_events.py::TestR2DataShapes::test_step_started_shape_and_model_ref
- tests/contract/test_events.py::TestR2DataShapes::test_finalized_ordinal_rejects_bool[True-TextFinalizedData]
- tests/contract/test_events.py::TestR2DataShapes::test_finalized_ordinal_rejects_bool[True-ReasoningFinalizedData]
- tests/contract/test_events.py::TestR2DataShapes::test_finalized_ordinal_rejects_bool[False-TextFinalizedData]
- tests/contract/test_events.py::TestR2DataShapes::test_finalized_ordinal_rejects_bool[False-ReasoningFinalizedData]
- tests/contract/test_events.py::TestR2DataShapes::test_step_started_attempt_rejects_bool[True]
- tests/contract/test_events.py::TestR2DataShapes::test_step_started_attempt_rejects_bool[False]
- tests/contract/test_events.py::TestR2DataShapes::test_tool_input_delta_call_index_rejects_bool[True]
- tests/contract/test_events.py::TestR2DataShapes::test_tool_input_delta_call_index_rejects_bool[False]
- tests/contract/test_events.py::TestR2DataShapes::test_finalized_ids_are_located_by_message_and_part
- tests/contract/test_events.py::TestR2DataShapes::test_tool_finished_requires_model_readable_text
- tests/contract/test_events.py::TestR2DataShapes::test_tool_finished_state_is_terminal_only
- tests/contract/test_events.py::TestR2DataShapes::test_tool_finished_effect_status_vocabulary
- tests/contract/test_events.py::TestR2DataShapes::test_tool_finished_error_nests_core_error
- tests/contract/test_events.py::TestR2DataShapes::test_tool_started_permission_decision_vocabulary
- tests/contract/test_events.py::TestR2DataShapes::test_run_finished_outcome_and_error
- tests/contract/test_events.py::TestR2DataShapes::test_run_cancel_requested_reason_vocabulary
- tests/contract/test_events.py::TestR2DataShapes::test_tool_requested_shape_ref_and_hash
- tests/contract/test_events.py::TestR2DataShapes::test_step_generated_and_finished_locate_by_step_id
- tests/contract/test_events.py::TestR2DataShapes::test_delta_and_progress_shapes

### tests/contract/test_llm.py (63)
- tests/contract/test_llm.py::TestModelRef::test_round_trip
- tests/contract/test_llm.py::TestModelRef::test_missing_and_empty_fields_rejected
- tests/contract/test_llm.py::TestModelRef::test_unknown_field_rejected
- tests/contract/test_llm.py::TestUsage::test_all_fields_optional_and_null
- tests/contract/test_llm.py::TestUsage::test_explicit_zero_is_zero_not_null
- tests/contract/test_llm.py::TestUsage::test_negative_rejected[input]
- tests/contract/test_llm.py::TestUsage::test_negative_rejected[output]
- tests/contract/test_llm.py::TestUsage::test_negative_rejected[reasoning]
- tests/contract/test_llm.py::TestUsage::test_negative_rejected[cache_read]
- tests/contract/test_llm.py::TestUsage::test_negative_rejected[cache_write]
- tests/contract/test_llm.py::TestUsage::test_bool_rejected[True-input]
- tests/contract/test_llm.py::TestUsage::test_bool_rejected[True-output]
- tests/contract/test_llm.py::TestUsage::test_bool_rejected[True-reasoning]
- tests/contract/test_llm.py::TestUsage::test_bool_rejected[True-cache_read]
- tests/contract/test_llm.py::TestUsage::test_bool_rejected[True-cache_write]
- tests/contract/test_llm.py::TestUsage::test_bool_rejected[False-input]
- tests/contract/test_llm.py::TestUsage::test_bool_rejected[False-output]
- tests/contract/test_llm.py::TestUsage::test_bool_rejected[False-reasoning]
- tests/contract/test_llm.py::TestUsage::test_bool_rejected[False-cache_read]
- tests/contract/test_llm.py::TestUsage::test_bool_rejected[False-cache_write]
- tests/contract/test_llm.py::TestUsage::test_unknown_field_rejected
- tests/contract/test_llm.py::TestFinishReason::test_values_match_contract
- tests/contract/test_llm.py::TestFinishReason::test_ts_only_values_rejected
- tests/contract/test_llm.py::TestGenerationRequest::test_full_round_trip_with_tools_and_temperature
- tests/contract/test_llm.py::TestGenerationRequest::test_temperature_optional_timeout_required
- tests/contract/test_llm.py::TestGenerationRequest::test_messages_and_tools_are_required_keys_but_may_be_empty_lists
- tests/contract/test_llm.py::TestGenerationRequest::test_max_output_tokens_positive_and_bool_rejected
- tests/contract/test_llm.py::TestGenerationRequest::test_timeout_s_positive_and_bools_rejected
- tests/contract/test_llm.py::TestGenerationRequest::test_request_id_non_empty_without_prefix_rule
- tests/contract/test_llm.py::TestGenerationRequest::test_unknown_field_rejected
- tests/contract/test_llm.py::TestLLMMessage::test_four_roles_accepted[system]
- tests/contract/test_llm.py::TestLLMMessage::test_four_roles_accepted[user]
- tests/contract/test_llm.py::TestLLMMessage::test_four_roles_accepted[assistant]
- tests/contract/test_llm.py::TestLLMMessage::test_four_roles_accepted[tool]
- tests/contract/test_llm.py::TestLLMMessage::test_unknown_role_rejected
- tests/contract/test_llm.py::TestLLMMessage::test_tool_call_part_shape
- tests/contract/test_llm.py::TestLLMMessage::test_tool_result_part_carries_content_union
- tests/contract/test_llm.py::TestLLMMessage::test_missing_identity_fields_rejected
- tests/contract/test_llm.py::TestLLMMessage::test_unknown_part_type_rejected
- tests/contract/test_llm.py::TestLLMMessage::test_full_request_round_trip_exercises_all_four_part_types
- tests/contract/test_llm.py::TestProviderChunk::test_each_chunk_type_round_trips[text_delta]
- tests/contract/test_llm.py::TestProviderChunk::test_each_chunk_type_round_trips[reasoning_delta]
- tests/contract/test_llm.py::TestProviderChunk::test_each_chunk_type_round_trips[tool_call_start]
- tests/contract/test_llm.py::TestProviderChunk::test_each_chunk_type_round_trips[tool_call_args_delta]
- tests/contract/test_llm.py::TestProviderChunk::test_each_chunk_type_round_trips[tool_call_end]
- tests/contract/test_llm.py::TestProviderChunk::test_each_chunk_type_round_trips[usage]
- tests/contract/test_llm.py::TestProviderChunk::test_each_chunk_type_round_trips[finish]
- tests/contract/test_llm.py::TestProviderChunk::test_each_chunk_type_round_trips[error]
- tests/contract/test_llm.py::TestProviderChunk::test_common_fields_required
- tests/contract/test_llm.py::TestProviderChunk::test_provider_metadata_defaults_to_empty_object
- tests/contract/test_llm.py::TestProviderChunk::test_provider_metadata_rejects_non_json_values
- tests/contract/test_llm.py::TestProviderChunk::test_index_non_negative_and_bool_rejected
- tests/contract/test_llm.py::TestProviderChunk::test_call_index_bool_rejected[tool_call_start]
- tests/contract/test_llm.py::TestProviderChunk::test_call_index_bool_rejected[tool_call_args_delta]
- tests/contract/test_llm.py::TestProviderChunk::test_call_index_bool_rejected[tool_call_end]
- tests/contract/test_llm.py::TestProviderChunk::test_tool_call_start_identity_optional
- tests/contract/test_llm.py::TestProviderChunk::test_tool_call_end_requires_identity_and_raw_arguments
- tests/contract/test_llm.py::TestProviderChunk::test_usage_chunk_carries_usage_and_optional_cost
- tests/contract/test_llm.py::TestProviderChunk::test_finish_chunk_reason_enum_and_raw_reason_optional
- tests/contract/test_llm.py::TestProviderChunk::test_error_chunk_carries_core_error
- tests/contract/test_llm.py::TestProviderChunk::test_unknown_chunk_type_rejected
- tests/contract/test_llm.py::TestProviderChunk::test_unknown_chunk_field_rejected
- tests/contract/test_llm.py::TestProviderChunk::test_args_delta_shape

### tests/contract/test_mcp_naming.py (44)
- tests/contract/test_mcp_naming.py::TestMcpNaming::test_mcp_tool_spec_pins_the_conservative_catalog_face
- tests/contract/test_mcp_naming.py::TestMcpNaming::test_wire_name_and_ownership_roundtrip
- tests/contract/test_mcp_naming.py::TestMcpNaming::test_same_server_same_name_rides_the_duplicate_rejection
- tests/contract/test_mcp_naming.py::TestMcpNaming::test_second_server_cannot_claim_an_owned_name
- tests/contract/test_mcp_naming.py::TestMcpNaming::test_non_mcp_namespace_is_rejected
- tests/contract/test_mcp_naming.py::TestMcpNaming::test_catalog_digest_is_deterministic_and_mcp_scoped
- tests/contract/test_mcp_naming.py::TestMcpNaming::test_catalog_digest_tracks_the_visible_face
- tests/contract/test_mcp_naming.py::TestMcpNaming::test_rejected_replace_leaves_the_previous_set_intact
- tests/contract/test_mcp_naming.py::TestPolicyMcpBinding::test_mcp_namespace_defaults_to_ask
- tests/contract/test_mcp_naming.py::TestPolicyMcpBinding::test_register_saved_enriches_with_the_current_binding
- tests/contract/test_mcp_naming.py::TestPolicyMcpBinding::test_catalog_version_change_forces_a_re_ask
- tests/contract/test_mcp_naming.py::TestPolicyMcpBinding::test_fingerprint_change_forces_a_re_ask
- tests/contract/test_mcp_naming.py::TestPolicyMcpBinding::test_identical_retry_after_unknown_re_asks
- tests/contract/test_mcp_naming.py::TestPolicyMcpBinding::test_unwired_host_keeps_pre_r4_s3_semantics
- tests/contract/test_mcp_naming.py::TestPolicyMcpBinding::test_bind_rejects_non_mcp_actions
- tests/contract/test_mcp_naming.py::TestSessionCreatedV3Wire::test_valid_fixture_decodes_and_round_trips
- tests/contract/test_mcp_naming.py::TestSessionCreatedV3Wire::test_bad_hash_fixture_is_rejected
- tests/contract/test_mcp_naming.py::TestSessionCreatedV3Wire::test_bad_extension_fixture_is_rejected
- tests/contract/test_mcp_naming.py::TestSessionCreatedV3Wire::test_v1_and_v2_fixtures_still_decode
- tests/contract/test_mcp_naming.py::TestSessionCreatedV3Wire::test_three_versions_are_registered_side_by_side
- tests/contract/test_mcp_naming.py::TestSessionCreatedV3Wire::test_v3_snapshot_is_standalone_not_a_v2_instance
- tests/contract/test_mcp_naming.py::TestUpcasterV3::test_v3_row_passes_through_with_its_v3_snapshot
- tests/contract/test_mcp_naming.py::TestUpcasterV3::test_v2_row_passes_through_with_its_v2_snapshot
- tests/contract/test_mcp_naming.py::TestUpcasterV3::test_v1_row_upcasts_to_no_runtime_face
- tests/contract/test_mcp_naming.py::TestConfigSnapshotV3Model::test_missing_mcp_servers_is_rejected
- tests/contract/test_mcp_naming.py::TestConfigSnapshotV3Model::test_empty_mcp_servers_is_the_explicit_empty_face
- tests/contract/test_mcp_naming.py::TestConfigSnapshotV3Model::test_env_refs_must_be_sorted
- tests/contract/test_mcp_naming.py::TestConfigSnapshotV3Model::test_env_refs_must_be_unique
- tests/contract/test_mcp_naming.py::TestConfigSnapshotV3Model::test_fingerprint_must_be_sha256_hex
- tests/contract/test_mcp_naming.py::TestConfigSnapshotV3Model::test_empty_command_entry_is_rejected
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_legal_block_loads_v3_refs_and_keeps_the_v2_face
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_no_mcp_block_yields_the_explicit_empty_face
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_disabled_server_is_excluded
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_servers_are_emitted_sorted_by_name
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_remote_type_fails_closed
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_missing_type_is_rejected
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_timeout_key_gets_a_named_out_of_scope_error
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_out_of_scope_server_fields_fail_closed[protocol-2025-06-18]
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_out_of_scope_server_fields_fail_closed[codemode-ask]
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_command_shape_is_rejected
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_environment_shape_is_rejected
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_project_layer_cannot_extend_host_tier_mcp
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_fingerprint_tracks_argv_and_env_keys_not_values_or_cwd
- tests/contract/test_mcp_naming.py::TestHostTierMcpLoading::test_surrogate_in_the_v3_face_fails_closed

### tests/contract/test_messages.py (29)
- tests/contract/test_messages.py::TestToolContent::test_text_content_round_trip
- tests/contract/test_messages.py::TestToolContent::test_file_content_round_trip_with_and_without_name
- tests/contract/test_messages.py::TestToolContent::test_file_content_requires_uri_and_mime
- tests/contract/test_messages.py::TestToolContent::test_unknown_content_type_rejected
- tests/contract/test_messages.py::TestToolContent::test_unknown_keys_rejected_on_both_variants
- tests/contract/test_messages.py::TestAssistantParts::test_text_and_reasoning_parts_round_trip
- tests/contract/test_messages.py::TestAssistantParts::test_part_id_must_be_non_empty
- tests/contract/test_messages.py::TestAssistantParts::test_tool_part_pending_plan_with_empty_content
- tests/contract/test_messages.py::TestAssistantParts::test_tool_part_content_is_a_required_key
- tests/contract/test_messages.py::TestAssistantParts::test_tool_part_vocabularies_are_closed[state-bad_values0]
- tests/contract/test_messages.py::TestAssistantParts::test_tool_part_vocabularies_are_closed[effect_status-bad_values1]
- tests/contract/test_messages.py::TestAssistantParts::test_tool_part_error_nests_core_error
- tests/contract/test_messages.py::TestAssistantParts::test_unknown_part_type_rejected
- tests/contract/test_messages.py::TestAssistantMessage::test_running_message_minimal_round_trip
- tests/contract/test_messages.py::TestAssistantMessage::test_completed_message_with_all_optional_fields
- tests/contract/test_messages.py::TestAssistantMessage::test_id_prefix_and_type_tag_enforced
- tests/contract/test_messages.py::TestAssistantMessage::test_status_vocabulary_is_running_completed_failed_interrupted
- tests/contract/test_messages.py::TestAssistantMessage::test_model_field_is_a_model_ref
- tests/contract/test_messages.py::TestAssistantMessage::test_cost_usd_is_decimal_fixed_point_only
- tests/contract/test_messages.py::TestAssistantMessage::test_usage_null_means_unknown_not_zero
- tests/contract/test_messages.py::TestAssistantMessage::test_naive_timestamps_rejected
- tests/contract/test_messages.py::TestAssistantMessage::test_unknown_field_rejected
- tests/contract/test_messages.py::TestAssistantMessage::test_deferred_models_are_complete_after_package_import
- tests/contract/test_messages.py::TestIdleMessage::test_round_trip_with_error
- tests/contract/test_messages.py::TestIdleMessage::test_error_optional_and_defaults_to_none
- tests/contract/test_messages.py::TestIdleMessage::test_outcome_vocabulary
- tests/contract/test_messages.py::TestIdleMessage::test_run_id_prefix_and_id_prefix_enforced
- tests/contract/test_messages.py::TestIdleMessage::test_unknown_field_rejected
- tests/contract/test_messages.py::TestUsageShapeFromMessages::test_usage_round_trips_inside_assistant_message

### tests/contract/test_permission_events.py (41)
- tests/contract/test_permission_events.py::TestR3Registration::test_r3_definitions_shape
- tests/contract/test_permission_events.py::TestR3Registration::test_r1_and_r2_tuples_unchanged
- tests/contract/test_permission_events.py::TestR3Registration::test_default_registry_resolves_all_permission_types
- tests/contract/test_permission_events.py::TestR3Registration::test_permission_types_are_not_run_scoped_at_decode
- tests/contract/test_permission_events.py::TestR3Registration::test_unknown_version_rejected_closed_wire
- tests/contract/test_permission_events.py::TestR3Registration::test_durable_mismatch_rejected
- tests/contract/test_permission_events.py::TestR3Registration::test_duplicate_registration_rejected
- tests/contract/test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless[py.permission.closed-events_permission_closed_valid.json]
- tests/contract/test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless[py.permission.expired-events_permission_expired_valid.json]
- tests/contract/test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless[py.permission.replied-events_permission_replied_valid.json]
- tests/contract/test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless[py.permission.requested-events_permission_requested_valid.json]
- tests/contract/test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless[py.permission.revoked-events_permission_revoked_valid.json]
- tests/contract/test_permission_events.py::TestPermissionPositiveFixtures::test_round_trip_is_lossless[py.permission.saved-events_permission_saved_valid.json]
- tests/contract/test_permission_events.py::TestPermissionPositiveFixtures::test_fixtures_expose_py_naming_and_version_one
- tests/contract/test_permission_events.py::TestPermissionPositiveFixtures::test_approval_trio_carries_run_id_metadata
- tests/contract/test_permission_events.py::TestPermissionPositiveFixtures::test_decision_trio_has_no_run_linkage
- tests/contract/test_permission_events.py::TestPermissionPositiveFixtures::test_requested_fixture_fields
- tests/contract/test_permission_events.py::TestPermissionPositiveFixtures::test_saved_fixture_fields
- tests/contract/test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected[events_permission_requested_missing_field.json]
- tests/contract/test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected[events_permission_replied_missing_field.json]
- tests/contract/test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected[events_permission_closed_missing_field.json]
- tests/contract/test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected[events_permission_saved_missing_field.json]
- tests/contract/test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected[events_permission_revoked_missing_field.json]
- tests/contract/test_permission_events.py::TestPermissionNegativeFixtures::test_missing_required_field_rejected[events_permission_expired_missing_field.json]
- tests/contract/test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected[events_permission_requested_bad_extension.json]
- tests/contract/test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected[events_permission_replied_bad_extension.json]
- tests/contract/test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected[events_permission_closed_bad_extension.json]
- tests/contract/test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected[events_permission_saved_bad_extension.json]
- tests/contract/test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected[events_permission_revoked_bad_extension.json]
- tests/contract/test_permission_events.py::TestPermissionNegativeFixtures::test_extra_data_key_rejected[events_permission_expired_bad_extension.json]
- tests/contract/test_permission_events.py::TestPermissionDataShapes::test_requested_input_hash_must_be_sha256_hex
- tests/contract/test_permission_events.py::TestPermissionDataShapes::test_requested_empty_identifiers_rejected
- tests/contract/test_permission_events.py::TestPermissionDataShapes::test_requested_naive_expires_at_rejected
- tests/contract/test_permission_events.py::TestPermissionDataShapes::test_replied_vocabulary_closed
- tests/contract/test_permission_events.py::TestPermissionDataShapes::test_replied_source_pinned_to_host
- tests/contract/test_permission_events.py::TestPermissionDataShapes::test_closed_vocabulary_closed
- tests/contract/test_permission_events.py::TestPermissionDataShapes::test_closed_empty_reason_rejected
- tests/contract/test_permission_events.py::TestPermissionDataShapes::test_saved_scope_and_rule_must_be_json_objects
- tests/contract/test_permission_events.py::TestPermissionDataShapes::test_saved_expires_at_is_nullable
- tests/contract/test_permission_events.py::TestPermissionDataShapes::test_terminal_pair_shapes
- tests/contract/test_permission_events.py::TestPermissionDataShapes::test_data_models_reject_wrong_type_payloads

### tests/contract/test_policy.py (21)
- tests/contract/test_policy.py::test_default_table_reads_allow_writes_ask
- tests/contract/test_policy.py::test_unknown_action_falls_back_to_declared_side_effects
- tests/contract/test_policy.py::test_root_restriction_denies_fs_escape
- tests/contract/test_policy.py::test_root_restriction_denies_unresolvable_resource
- tests/contract/test_policy.py::test_host_deny_outranks_rules_and_saved_decisions
- tests/contract/test_policy.py::test_rules_cannot_relax_root_restriction
- tests/contract/test_policy.py::test_rule_priority_and_resource_pinning
- tests/contract/test_policy.py::test_saved_decision_exact_binding_allows
- tests/contract/test_policy.py::test_saved_decision_expiry_lifecycle
- tests/contract/test_policy.py::test_saved_decision_revocation_is_fail_closed
- tests/contract/test_policy.py::test_unknown_effect_retry_blocks_saved_decision
- tests/contract/test_policy.py::test_foreign_boot_and_duplicate_registration_fail_closed
- tests/contract/test_policy.py::test_id_minters_are_prefixed_and_unique
- tests/contract/test_policy.py::test_extract_resources_binds_path_and_command
- tests/contract/test_policy.py::test_coordinator_timeout_validation_and_default_deadline
- tests/contract/test_policy.py::test_reply_once_resolves_the_future_exactly_once
- tests/contract/test_policy.py::test_reply_reject_and_always_carry_their_vocabulary
- tests/contract/test_policy.py::test_late_and_unknown_replies_are_classified
- tests/contract/test_policy.py::test_close_run_cancels_only_that_run
- tests/contract/test_policy.py::test_close_all_cancels_every_pending_and_refuses_new_registrations
- tests/contract/test_policy.py::test_expiry_watchdog_resolves_expired

### tests/contract/test_tool_listing.py (16)
- tests/contract/test_tool_listing.py::test_trio_specs_use_fs_namespace_and_version_one
- tests/contract/test_tool_listing.py::test_trio_wire_names_partition_reversibly
- tests/contract/test_tool_listing.py::test_trio_quartet_registers_through_public_face_and_snapshots
- tests/contract/test_tool_listing.py::test_trio_registration_rejects_duplicate_wire_names
- tests/contract/test_tool_listing.py::test_trio_specs_are_side_effect_free_read_tools
- tests/contract/test_tool_listing.py::test_trio_input_schemas_stay_in_the_validator_subset
- tests/contract/test_tool_listing.py::test_trio_input_schema_fields_match_the_ts_anchors
- tests/contract/test_tool_listing.py::test_trio_limits_follow_the_ts_default
- tests/contract/test_tool_listing.py::test_mutation_pair_specs_declare_side_effects_and_idempotency
- tests/contract/test_tool_listing.py::test_mutation_pair_input_schemas_stay_in_the_validator_subset
- tests/contract/test_tool_listing.py::test_mutation_pair_input_schema_fields_match_the_ts_anchors
- tests/contract/test_tool_listing.py::test_mutation_pair_asks_by_default_in_the_permission_table
- tests/contract/test_tool_listing.py::test_bash_spec_declares_side_effects_without_idempotency
- tests/contract/test_tool_listing.py::test_bash_input_schema_stays_in_the_validator_subset
- tests/contract/test_tool_listing.py::test_bash_input_schema_fields_match_the_ts_anchors
- tests/contract/test_tool_listing.py::test_bash_asks_by_default_in_the_permission_table

### tests/contract/test_tools.py (28)
- tests/contract/test_tools.py::test_register_and_snapshot_catalog
- tests/contract/test_tools.py::test_snapshot_is_pinned_per_session
- tests/contract/test_tools.py::test_register_rejects_exact_duplicate
- tests/contract/test_tools.py::test_register_rejects_wire_name_version_collision
- tests/contract/test_tools.py::test_register_rejects_invalid_names
- tests/contract/test_tools.py::test_register_rejects_namespace_ending_with_underscore
- tests/contract/test_tools.py::test_register_leading_underscore_name_round_trips_over_wire
- tests/contract/test_tools.py::test_register_rejects_schema_outside_validator_subset
- tests/contract/test_tools.py::test_register_rejects_malformed_adapter
- tests/contract/test_tools.py::test_snapshot_rejects_bad_session_id
- tests/contract/test_tools.py::test_invoke_requires_event_sink
- tests/contract/test_tools.py::test_invoke_rejects_malformed_arguments
- tests/contract/test_tools.py::test_invoke_unknown_tool_is_failed_not_started
- tests/contract/test_tools.py::test_invoke_schema_failure_is_failed_not_started
- tests/contract/test_tools.py::test_invoke_does_not_coerce_input_types
- tests/contract/test_tools.py::test_invoke_minimum_bound_is_enforced_before_adapter
- tests/contract/test_tools.py::test_invoke_success_sequence
- tests/contract/test_tools.py::test_invoke_passes_input_through_unmodified
- tests/contract/test_tools.py::test_cancel_before_start_is_cancelled_not_started
- tests/contract/test_tools.py::test_adapter_exception_becomes_failed_result
- tests/contract/test_tools.py::test_adapter_exception_with_side_effects_keeps_effect_unknown
- tests/contract/test_tools.py::test_adapter_error_result_maps_to_failed
- tests/contract/test_tools.py::test_structured_output_gets_model_readable_text
- tests/contract/test_tools.py::test_oversized_structured_output_dropped_with_marker
- tests/contract/test_tools.py::test_result_without_output_gets_explanatory_text
- tests/contract/test_tools.py::test_file_only_content_gains_text_item
- tests/contract/test_tools.py::test_progress_metadata_is_capped
- tests/contract/test_tools.py::test_progress_without_runner_callback_is_noop

### tests/integration/test_approval.py (12)
- tests/integration/test_approval.py::test_once_approval_pairs_reply_and_started_then_succeeds
- tests/integration/test_approval.py::test_reject_denies_the_tool_in_the_same_batch
- tests/integration/test_approval.py::test_unconnected_approval_channel_cancels_without_silent_allow
- tests/integration/test_approval.py::test_approval_expiry_cancels_the_tool_and_late_reply_expires
- tests/integration/test_approval.py::test_cancel_during_approval_folds_interrupted_without_started
- tests/integration/test_approval.py::test_close_during_approval_folds_interrupted_op9
- tests/integration/test_approval.py::test_always_reply_saves_the_decision_and_auto_allows_later_calls
- tests/integration/test_approval.py::test_boot_restart_asks_again_and_never_activates_old_decisions
- tests/integration/test_approval.py::test_revoke_permission_re_asks_and_gates_repeats
- tests/integration/test_approval.py::test_unknown_effect_retry_is_re_asked_despite_saved_decision
- tests/integration/test_approval.py::test_reply_permission_validation_and_missing_channel
- tests/integration/test_approval.py::test_diagnostic_session_refuses_the_reply

### tests/integration/test_bus_commit.py (1)
- tests/integration/test_bus_commit.py::test_bus_receives_only_committed_events

### tests/integration/test_cli.py (13)
- tests/integration/test_cli.py::test_single_shot_e2e_exit_0_and_projection_rows
- tests/integration/test_cli.py::test_failed_run_maps_to_exit_1
- tests/integration/test_cli.py::test_host_config_error_maps_to_exit_70
- tests/integration/test_cli.py::test_interrupted_run_maps_to_exit_2
- tests/integration/test_cli.py::test_repl_mid_run_interrupt_returns_to_prompt
- tests/integration/test_cli.py::test_repl_prompt_face_interrupt_maps_to_130
- tests/integration/test_cli.py::test_escape_replaces_control_sequences_in_stream
- tests/integration/test_cli.py::test_escape_for_display_replaces_c0_del_and_c1
- tests/integration/test_cli.py::test_history_shows_union_order_after_one_run
- tests/integration/test_cli.py::test_history_on_empty_session_notices_without_error
- tests/integration/test_cli.py::test_durable_override_shows_delta_preview_and_final
- tests/integration/test_cli.py::test_subprocess_entry_smoke
- tests/integration/test_cli.py::test_argparse_missing_required_args_exit_2

### tests/integration/test_cli_r3.py (30)
- tests/integration/test_cli_r3.py::test_approve_once_completes_mid_run_and_tool_writes
- tests/integration/test_cli_r3.py::test_approve_always_saves_decision_and_second_call_does_not_ask
- tests/integration/test_cli_r3.py::test_reject_denies_tool_and_run_still_succeeds
- tests/integration/test_cli_r3.py::test_second_reply_is_already_resolved
- tests/integration/test_cli_r3.py::test_unknown_permission_id_is_one_readable_line
- tests/integration/test_cli_r3.py::test_permission_error_line_maps_every_store_error_to_a_label
- tests/integration/test_cli_r3.py::test_permission_rules_from_snapshot_conversion_faces
- tests/integration/test_cli_r3.py::test_engine_project_deny_tightens_the_default_allow
- tests/integration/test_cli_r3.py::test_engine_host_ask_outranks_project_allow_for_bash
- tests/integration/test_cli_r3.py::test_engine_project_allow_cannot_loosen_root_restriction
- tests/integration/test_cli_r3.py::test_engine_project_allow_cannot_loosen_host_hard_deny
- tests/integration/test_cli_r3.py::test_config_deny_rule_blocks_fs_write_without_asking
- tests/integration/test_cli_r3.py::test_bad_rule_shape_fails_at_load_time_exit_2[rule0]
- tests/integration/test_cli_r3.py::test_bad_rule_shape_fails_at_load_time_exit_2[rule1]
- tests/integration/test_cli_r3.py::test_bad_rule_shape_fails_at_load_time_exit_2[rule2]
- tests/integration/test_cli_r3.py::test_bad_rule_shape_fails_at_load_time_exit_2[rule3]
- tests/integration/test_cli_r3.py::test_bad_rule_shape_fails_at_load_time_exit_2[rule4]
- tests/integration/test_cli_r3.py::test_bad_rule_shape_fails_at_load_time_exit_2[rule5]
- tests/integration/test_cli_r3.py::test_bad_rule_shape_fails_at_load_time_exit_2[rule6]
- tests/integration/test_cli_r3.py::test_bad_rule_shape_fails_at_load_time_exit_2[rule7]
- tests/integration/test_cli_r3.py::test_bad_rule_shape_fails_at_load_time_exit_2[rule8]
- tests/integration/test_cli_r3.py::test_bad_rule_shape_fails_at_load_time_exit_2[not-an-object]
- tests/integration/test_cli_r3.py::test_subprocess_mid_run_ctrl_c_cancels_and_exits_2
- tests/integration/test_cli_r3.py::test_subprocess_prompt_face_ctrl_c_exits_130
- tests/integration/test_cli_r3.py::test_history_non_numeric_argument_warns_and_keeps_default
- tests/integration/test_cli_r3.py::test_failed_run_error_line_carries_message_not_repr
- tests/integration/test_cli_r3.py::test_non_utf8_console_degrades_lossily
- tests/integration/test_cli_r3.py::test_cancel_cleanup_failure_exits_3
- tests/integration/test_cli_r3.py::test_migration_failure_maps_to_exit_70
- tests/integration/test_cli_r3.py::test_resolve_phase_config_error_maps_to_exit_70

### tests/integration/test_cli_r4.py (10)
- tests/integration/test_cli_r4.py::test_continue_selects_the_latest_session_in_the_working_directory
- tests/integration/test_cli_r4.py::test_continue_with_no_prior_session_creates_a_fresh_v3_session
- tests/integration/test_cli_r4.py::test_session_unknown_id_exits_4_with_one_stderr_line
- tests/integration/test_cli_r4.py::test_session_with_a_valid_id_resumes_without_creating
- tests/integration/test_cli_r4.py::test_continue_stops_on_a_v1_snapshot_session
- tests/integration/test_cli_r4.py::test_continue_stops_on_mcp_fingerprint_drift
- tests/integration/test_cli_r4.py::test_session_stops_when_the_recorded_working_dir_is_gone
- tests/integration/test_cli_r4.py::test_continue_and_session_are_mutually_exclusive
- tests/integration/test_cli_r4.py::test_restart_chain_closes_orphan_reasks_and_refuses_the_old_permission_id
- tests/integration/test_cli_r4.py::test_mcp_composition_registers_definitions_without_spawning_and_closes

### tests/integration/test_config_loader.py (30)
- tests/integration/test_config_loader.py::test_merge_laws_scalar_map_list_and_two_append_exceptions
- tests/integration/test_config_loader.py::test_full_hierarchy_builtin_global_project_layers_then_cli
- tests/integration/test_config_loader.py::test_endpoint_ref_strips_userinfo_and_query
- tests/integration/test_config_loader.py::test_host_only_keys_are_rejected_in_project_layers[api_key]
- tests/integration/test_config_loader.py::test_host_only_keys_are_rejected_in_project_layers[credentials]
- tests/integration/test_config_loader.py::test_host_only_keys_are_rejected_in_project_layers[endpoint]
- tests/integration/test_config_loader.py::test_host_only_keys_are_rejected_in_project_layers[mcp]
- tests/integration/test_config_loader.py::test_host_only_keys_are_rejected_in_project_layers[roots]
- tests/integration/test_config_loader.py::test_unknown_field_is_a_config_error
- tests/integration/test_config_loader.py::test_no_vcs_marker_means_cwd_is_the_project_root
- tests/integration/test_config_loader.py::test_project_root_must_be_the_cwd_or_an_ancestor
- tests/integration/test_config_loader.py::test_explicit_global_config_path_must_exist
- tests/integration/test_config_loader.py::test_broken_global_config_json_is_an_error
- tests/integration/test_config_loader.py::test_agents_md_and_config_instructions_assemble_outer_to_inner_with_dedup
- tests/integration/test_config_loader.py::test_instruction_per_source_cap_exact_boundary
- tests/integration/test_config_loader.py::test_instruction_total_cap_exact_boundary
- tests/integration/test_config_loader.py::test_surrogate_instruction_is_rejected_by_the_pre_scan
- tests/integration/test_config_loader.py::test_roots_declared_globally_reach_snapshot_and_adapters_identically
- tests/integration/test_config_loader.py::test_read_adapter_multi_root_allow_and_reject
- tests/integration/test_config_loader.py::test_write_adapter_multi_root
- tests/integration/test_config_loader.py::test_cross_root_symlink_escape_is_denied
- tests/integration/test_config_loader.py::test_recover_replays_the_recorded_v2_snapshot
- tests/integration/test_config_loader.py::test_recover_stops_on_a_v1_snapshot
- tests/integration/test_config_loader.py::test_recover_stops_when_the_working_directory_is_gone
- tests/integration/test_config_loader.py::test_cli_end_to_end_config_supplied_runtime_and_v3_snapshot_in_db
- tests/integration/test_config_loader.py::test_cli_missing_model_without_config_exits_2
- tests/integration/test_config_loader.py::test_cli_broken_config_exits_2
- tests/integration/test_config_loader.py::test_coerce_config_snapshot_routes_by_schema_version
- tests/integration/test_config_loader.py::test_merge_json_type_mismatch_replaces_whole
- tests/integration/test_config_loader.py::test_sanitize_endpoint_rejects_non_absolute_and_non_http

### tests/integration/test_e2e.py (3)
- tests/integration/test_e2e.py::test_main_chain_multiround_text_then_tool_round
- tests/integration/test_e2e.py::test_interrupted_partial_stream_next_request_starts_fresh
- tests/integration/test_e2e.py::test_event_log_records_the_full_lifecycle_in_order

### tests/integration/test_e2e_r3.py (4)
- tests/integration/test_e2e_r3.py::test_r3_six_tool_chain_with_live_approvals
- tests/integration/test_e2e_r3.py::test_event_log_ledger_per_tool_cross_validation
- tests/integration/test_e2e_r3.py::test_unanswered_approval_expires_tool_not_started_run_succeeds
- tests/integration/test_e2e_r3.py::test_post_start_graceful_failure_reaches_run_terminal

### tests/integration/test_e2e_r4.py (7)
- tests/integration/test_e2e_r4.py::test_w1_kill_while_approval_pending
- tests/integration/test_e2e_r4.py::test_w2_kill_while_tool_executing
- tests/integration/test_e2e_r4.py::test_w3_kill_while_generation_streaming
- tests/integration/test_e2e_r4.py::test_multi_call_plan_recovery_pairs_every_call
- tests/integration/test_e2e_r4.py::test_generation_half_batch_crash_leaves_no_orphan_rows
- tests/integration/test_e2e_r4.py::test_close_folds_pending_approval_then_mcp_provider_db
- tests/integration/test_e2e_r4.py::test_close_timeout_folds_in_flight_mcp_call_unknown

### tests/integration/test_eventlog.py (20)
- tests/integration/test_eventlog.py::test_read_paginates_with_cursor_and_limit_boundaries
- tests/integration/test_eventlog.py::test_read_clamps_limit_into_bounds
- tests/integration/test_eventlog.py::test_read_and_follow_report_not_found_for_unbuilt_session
- tests/integration/test_eventlog.py::test_read_invalid_cursor_rejections
- tests/integration/test_eventlog.py::test_read_high_watermark_is_log_max_not_applied_seq
- tests/integration/test_eventlog.py::test_read_bad_page_fails_closed_as_a_whole
- tests/integration/test_eventlog.py::test_read_unknown_version_or_type_fails_closed_instead_of_skipping
- tests/integration/test_eventlog.py::test_read_only_diagnostic_session_fails_closed_on_any_cursor
- tests/integration/test_eventlog.py::test_follow_catches_up_emits_one_synced_and_streams_strictly_increasing
- tests/integration/test_eventlog.py::test_follow_poll_fallback_backfills_lost_notifications
- tests/integration/test_eventlog.py::test_read_backfills_everything_after_the_bus_disconnects
- tests/integration/test_eventlog.py::test_follow_enter_validates_cursor_and_session
- tests/integration/test_eventlog.py::test_follow_bad_page_in_catchup_terminates_before_any_synced
- tests/integration/test_eventlog.py::test_follow_bad_page_after_synced_keeps_healthy_then_terminates
- tests/integration/test_eventlog.py::test_follow_uses_the_bus_as_a_wake_signal_only
- tests/integration/test_eventlog.py::test_repos_messages_paginate_by_seq_not_id_order
- tests/integration/test_eventlog.py::test_repos_missing_projection_and_undecodable_rows_fail_closed
- tests/integration/test_eventlog.py::test_repos_session_record_reads_the_s4_read_model
- tests/integration/test_eventlog.py::test_reads_and_queries_make_no_external_calls
- tests/integration/test_eventlog.py::test_repos_and_storage_package_are_structurally_bus_free

### tests/integration/test_follow_recovery.py (4)
- tests/integration/test_follow_recovery.py::test_slow_consumer_disconnect_keeps_every_durable_event_recoverable
- tests/integration/test_follow_recovery.py::test_follow_wakeup_overflow_only_costs_wakeups_poll_stays_complete
- tests/integration/test_follow_recovery.py::test_follow_reconnect_is_at_least_once_and_the_consumer_dedups
- tests/integration/test_follow_recovery.py::test_follow_never_yields_ephemeral_while_the_live_preview_sees_it

### tests/integration/test_fs_multiroot.py (5)
- tests/integration/test_fs_multiroot.py::test_glob_secondary_root_allows_and_renders_absolute
- tests/integration/test_fs_multiroot.py::test_ls_secondary_root_allows_and_renders_absolute
- tests/integration/test_fs_multiroot.py::test_grep_secondary_root_allows_and_renders_absolute
- tests/integration/test_fs_multiroot.py::test_glob_default_adapter_keeps_the_secondary_root_out
- tests/integration/test_fs_multiroot.py::test_display_path_cross_drive_and_dotdot_fallbacks

### tests/integration/test_mcp_catalog.py (3)
- tests/integration/test_mcp_catalog.py::test_v3_snapshot_loads_creates_and_recovers
- tests/integration/test_mcp_catalog.py::test_tools_list_change_replaces_catalog_and_re_asks
- tests/integration/test_mcp_catalog.py::test_two_servers_exposing_the_same_tool_name_are_rejected

### tests/integration/test_mcp_stdio.py (13)
- tests/integration/test_mcp_stdio.py::test_roundtrip_initialize_tools_call_and_close
- tests/integration/test_mcp_stdio.py::test_bridge_to_tool_result_faces
- tests/integration/test_mcp_stdio.py::test_mcp_tool_adapter_executes_through_manager
- tests/integration/test_mcp_stdio.py::test_startup_timeout_marks_failed_and_kills_server
- tests/integration/test_mcp_stdio.py::test_catalog_timeout_marks_failed_no_empty_tools
- tests/integration/test_mcp_stdio.py::test_tools_list_error_marks_failed_no_empty_tools
- tests/integration/test_mcp_stdio.py::test_exit_during_call_unknown_with_stderr_tail
- tests/integration/test_mcp_stdio.py::test_no_auto_retry_rebuild_only_on_next_explicit_call
- tests/integration/test_mcp_stdio.py::test_close_escalation_kills_tree
- tests/integration/test_mcp_stdio.py::test_crash_sweeps_tree
- tests/integration/test_mcp_stdio.py::test_frame_overflow_rejected
- tests/integration/test_mcp_stdio.py::test_service_close_chain_mcp_before_provider
- tests/integration/test_mcp_stdio.py::test_app_close_closes_mcp_manager_before_provider

### tests/integration/test_messages_union.py (4)
- tests/integration/test_messages_union.py::test_pure_user_session_pages_unchanged_through_the_union_face
- tests/integration/test_messages_union.py::test_mixed_projection_types_page_in_projection_order
- tests/integration/test_messages_union.py::test_cursor_ownership_holds_for_the_union_face
- tests/integration/test_messages_union.py::test_corrupted_rows_fail_the_whole_page

### tests/integration/test_migrations.py (6)
- tests/integration/test_migrations.py::test_upgrade_head_builds_0001_schema
- tests/integration/test_migrations.py::test_upgrade_head_is_idempotent
- tests/integration/test_migrations.py::test_runtime_engine_pragmas_and_wal_persistence
- tests/integration/test_migrations.py::test_open_modes_and_close_finalizer
- tests/integration/test_migrations.py::test_upgrade_0001_to_0002_preserves_legacy_data
- tests/integration/test_migrations.py::test_0002_backfill_via_rebuild_keeps_every_accepted_run

### tests/integration/test_permission_projector.py (11)
- tests/integration/test_permission_projector.py::test_approval_reject_flow_projects_the_permissions_read_model
- tests/integration/test_permission_projector.py::test_closed_flow_is_terminal_once_and_rejects_the_late_reply
- tests/integration/test_permission_projector.py::test_decision_lifecycle_transitions_are_terminal_once
- tests/integration/test_permission_projector.py::test_permission_projection_is_identical_warm_cold_and_across_rebuilds
- tests/integration/test_permission_projector.py::test_registry_single_sourcing_replay_decodes_through_the_bound_registry
- tests/integration/test_permission_projector.py::test_bind_registry_guards
- tests/integration/test_permission_projector.py::test_diagnostic_session_rejects_permission_writes
- tests/integration/test_permission_projector.py::test_fold_approval_terminals_are_first_commit_wins
- tests/integration/test_permission_projector.py::test_fold_decision_terminals_and_registration_gates
- tests/integration/test_permission_projector.py::test_fold_permission_ownership_gates
- tests/integration/test_permission_projector.py::test_store_level_permission_transition_negatives

### tests/integration/test_projector.py (29)
- tests/integration/test_projector.py::test_projector_writes_session_and_message_rows
- tests/integration/test_projector.py::test_projector_failure_rolls_back_and_retry_restarts_seq
- tests/integration/test_projector.py::test_projector_duplicate_created_projection_is_storage_corrupt
- tests/integration/test_projector.py::test_projector_duplicate_message_id_is_storage_corrupt
- tests/integration/test_projector.py::test_projector_prompt_without_session_is_storage_corrupt
- tests/integration/test_projector.py::test_projector_missing_advance_is_caught_by_store_recheck
- tests/integration/test_projector.py::test_commit_and_queries_make_no_external_calls
- tests/integration/test_projector.py::test_state_fold_is_deterministic_over_contract_fixtures
- tests/integration/test_projector.py::test_state_fold_rejects_illegal_transitions
- tests/integration/test_projector.py::test_state_module_is_structurally_pure
- tests/integration/test_projector.py::test_projector_delegates_transition_rules_to_the_fold
- tests/integration/test_projector.py::test_r2_full_lifecycle_projects_all_four_faces
- tests/integration/test_projector.py::test_r2_illegal_transitions_are_rejected_with_zero_residue
- tests/integration/test_projector.py::test_r2_step_finished_without_generated_uses_finalized_text_only
- tests/integration/test_projector.py::test_r2_runs_rows_share_the_event_transaction
- tests/integration/test_projector.py::test_r1_legacy_log_replays_and_rebuilds_identically
- tests/integration/test_projector.py::test_r2_fold_idempotent_replays_change_nothing
- tests/integration/test_projector.py::test_cross_session_run_id_reuse_reports_the_global_pk_collision
- tests/integration/test_projector.py::test_same_request_under_another_run_reports_the_identity_collision
- tests/integration/test_projector.py::test_accepted_run_row_with_a_different_request_reports_identity_mismatch
- tests/integration/test_projector.py::test_incremental_replay_of_a_tampered_stored_event_is_storage_corrupt
- tests/integration/test_projector.py::test_r2_fold_known_run_with_a_different_request_is_rejected
- tests/integration/test_projector.py::test_r2_fold_tool_requested_identity_drift_is_rejected
- tests/integration/test_projector.py::test_r2_fold_tool_started_input_hash_drift_is_rejected
- tests/integration/test_projector.py::test_r2_fold_pending_tool_may_finish_directly
- tests/integration/test_projector.py::test_r2_fold_step_started_duplicate_step_and_message_ids_are_rejected
- tests/integration/test_projector.py::test_incremental_cache_projection_is_bit_identical_to_full_replay
- tests/integration/test_projector.py::test_warm_fold_cache_still_detects_a_tampered_stored_event
- tests/integration/test_projector.py::test_multi_event_batches_apply_once_through_the_fold_cache

### tests/integration/test_rebuild.py (17)
- tests/integration/test_rebuild.py::test_rebuild_restores_deleted_read_model_identically
- tests/integration/test_rebuild.py::test_rebuild_unknown_version_rolls_back_clearing_and_isolates
- tests/integration/test_rebuild.py::test_rebuild_seq_gap_rolls_back_and_isolates
- tests/integration/test_rebuild.py::test_rebuild_undecodable_stored_data_rolls_back_and_isolates
- tests/integration/test_rebuild.py::test_rebuild_repairs_tampered_projection_row
- tests/integration/test_rebuild.py::test_rebuild_projection_lag_is_not_corruption
- tests/integration/test_rebuild.py::test_diagnostic_mark_survives_successful_rebuild
- tests/integration/test_rebuild.py::test_rebuild_appends_no_events_and_never_notifies
- tests/integration/test_rebuild.py::test_first_event_damage_over_deleted_read_model_writes_minimal_record
- tests/integration/test_rebuild.py::test_rebuild_snapshot_hash_mismatch_isolates
- tests/integration/test_rebuild.py::test_rebuild_service_face_gates
- tests/integration/test_rebuild.py::test_same_request_id_across_sessions_yields_distinct_runs
- tests/integration/test_rebuild.py::test_minimal_record_over_empty_log_is_preserved_verbatim
- tests/integration/test_rebuild.py::test_active_row_over_cleared_log_isolates_projection_ahead
- tests/integration/test_rebuild.py::test_projection_ahead_of_a_self_consistent_log_isolates
- tests/integration/test_rebuild.py::test_closed_status_is_carried_over_verbatim_by_rebuild
- tests/integration/test_rebuild.py::test_r2_log_rebuilds_every_face_byte_identically

### tests/integration/test_recovery.py (14)
- tests/integration/test_recovery.py::test_accepted_window_closes_interrupted_and_allows_a_new_prompt
- tests/integration/test_recovery.py::test_running_window_rebuilds_the_interrupted_step_from_finalized_text
- tests/integration/test_recovery.py::test_cancelling_window_full_terminal_batch_closed_first
- tests/integration/test_recovery.py::test_window2_started_tool_without_finished_synthesizes_unknown
- tests/integration/test_recovery.py::test_halt_on_working_dir_gone
- tests/integration/test_recovery.py::test_halt_on_v3_fingerprint_drift_but_v1_ignores
- tests/integration/test_recovery.py::test_v1_session_recovers_despite_drifted_refs
- tests/integration/test_recovery.py::test_non_active_sessions_are_scanned_but_never_written
- tests/integration/test_recovery.py::test_integrity_failure_skips_the_session
- tests/integration/test_recovery.py::test_drive_run_in_process_crash_cancel_reports_lifecycle_cleanup_failed
- tests/integration/test_recovery.py::test_cross_process_crash_closed_by_the_app_startup_scan
- tests/integration/test_recovery.py::test_gate_blocks_writes_until_the_scan_completes_and_fails_closed
- tests/integration/test_recovery.py::test_sweep_removes_only_crash_temp_files
- tests/integration/test_recovery.py::test_app_startup_sweep_reports_counts

### tests/integration/test_runner.py (34)
- tests/integration/test_runner.py::test_single_step_succeeds
- tests/integration/test_runner.py::test_reasoning_and_text_finalize_in_ordinal_order
- tests/integration/test_runner.py::test_tool_round_drives_second_request
- tests/integration/test_runner.py::test_unknown_tool_fails_not_started_and_still_pairs
- tests/integration/test_runner.py::test_tool_context_carries_run_identity
- tests/integration/test_runner.py::test_committed_events_reach_true_sink_tables
- tests/integration/test_runner.py::test_generation_deltas_publish_live_and_never_persist
- tests/integration/test_runner.py::test_eof_without_finish_ends_interrupted
- tests/integration/test_runner.py::test_error_chunk_rides_verbatim
- tests/integration/test_runner.py::test_truncated_arguments_fail_generation_without_execution
- tests/integration/test_runner.py::test_non_object_arguments_fail_generation
- tests/integration/test_runner.py::test_duplicate_tool_call_id_within_generation
- tests/integration/test_runner.py::test_duplicate_tool_call_id_across_steps
- tests/integration/test_runner.py::test_tool_call_without_end_fails_generation
- tests/integration/test_runner.py::test_part_id_reused_across_kinds_fails_generation
- tests/integration/test_runner.py::test_text_stream_cap_counts_reasoning_too
- tests/integration/test_runner.py::test_argument_cap_fails_generation
- tests/integration/test_runner.py::test_tool_call_count_cap_fails_generation
- tests/integration/test_runner.py::test_step_ceiling_fails_the_run
- tests/integration/test_runner.py::test_context_limit_fails_run_without_step_footprint
- tests/integration/test_runner.py::test_cancel_before_run_started_finalizes_interrupted
- tests/integration/test_runner.py::test_cancel_between_tools_keeps_started_and_synthesizes_pending
- tests/integration/test_runner.py::test_cancel_signal_mid_stream_synthesizes_pending_tool
- tests/integration/test_runner.py::test_hard_cancel_mid_tool_finalizes_unknown
- tests/integration/test_runner.py::test_hard_cancel_mid_stream_commits_received_text
- tests/integration/test_runner.py::test_unknown_run_is_a_conflict
- tests/integration/test_runner.py::test_terminal_run_cannot_restart
- tests/integration/test_runner.py::test_max_steps_ctor_validation
- tests/integration/test_runner.py::test_interrupted_after_usage_keeps_the_observed_usage
- tests/integration/test_runner.py::test_interrupted_after_finish_and_usage_keeps_both_observations
- tests/integration/test_runner.py::test_eof_without_terminal_keeps_the_terminal_fields_conservative
- tests/integration/test_runner.py::test_interrupted_after_usage_without_text_keeps_the_observed_usage
- tests/integration/test_runner.py::test_cancel_between_step_finished_and_run_finished_interrupts_the_run
- tests/integration/test_runner.py::test_interrupted_from_generated_plan_keeps_observed_usage_and_finish

### tests/integration/test_service.py (28)
- tests/integration/test_service.py::test_full_cycle_persists_and_reopens_identically
- tests/integration/test_service.py::test_prompt_rejects_missing_session_not_found
- tests/integration/test_service.py::test_prompt_read_only_gate_precedes_idempotent_retry
- tests/integration/test_service.py::test_prompt_rejects_cleanup_failed_and_closed_rows
- tests/integration/test_service.py::test_service_close_gate_rejects_writes_reads_keep_working
- tests/integration/test_service.py::test_prompt_idempotent_retry_returns_original_run_without_side_effects
- tests/integration/test_service.py::test_prompt_same_request_id_different_text_conflicts_even_while_busy
- tests/integration/test_service.py::test_r1_accepted_run_holds_the_slot_across_close_and_reopen
- tests/integration/test_service.py::test_concurrent_prompts_one_run_one_busy
- tests/integration/test_service.py::test_concurrent_identical_prompts_share_one_run
- tests/integration/test_service.py::test_prompt_expected_seq_is_the_pre_commit_high_watermark
- tests/integration/test_service.py::test_default_ids_are_prefixed_and_lexicographically_ascending
- tests/integration/test_service.py::test_run_status_finds_the_active_run_and_fails_closed
- tests/integration/test_service.py::test_create_rejects_invalid_snapshots[extra-key]
- tests/integration/test_service.py::test_create_rejects_invalid_snapshots[non-empty-rules]
- tests/integration/test_service.py::test_create_rejects_invalid_snapshots[string-schema-version]
- tests/integration/test_service.py::test_create_rejects_invalid_snapshots[bool-schema-version]
- tests/integration/test_service.py::test_create_rejects_invalid_snapshots[missing-schema-version]
- tests/integration/test_service.py::test_create_rejects_invalid_snapshots[not-an-object]
- tests/integration/test_service.py::test_prompt_rejects_invalid_inputs_before_any_read[session-id-not-a-string]
- tests/integration/test_service.py::test_prompt_rejects_invalid_inputs_before_any_read[session-id-empty]
- tests/integration/test_service.py::test_prompt_rejects_invalid_inputs_before_any_read[session-id-wrong-prefix]
- tests/integration/test_service.py::test_prompt_rejects_invalid_inputs_before_any_read[request-id-not-a-string]
- tests/integration/test_service.py::test_prompt_rejects_invalid_inputs_before_any_read[request-id-empty]
- tests/integration/test_service.py::test_prompt_rejects_invalid_inputs_before_any_read[text-not-a-string]
- tests/integration/test_service.py::test_prompt_accepts_empty_string_text
- tests/integration/test_service.py::test_session_status_and_messages_read_faces
- tests/integration/test_service.py::test_app_close_releases_engine_and_writer_lock_idempotently

### tests/integration/test_service_run.py (20)
- tests/integration/test_service_run.py::test_prompt_schedules_the_runner_and_completes_the_run
- tests/integration/test_service_run.py::test_idempotent_retry_does_not_reschedule_or_append_events
- tests/integration/test_service_run.py::test_cancelling_run_occupies_the_busy_slot
- tests/integration/test_service_run.py::test_cancel_on_a_terminal_run_returns_the_original_status
- tests/integration/test_service_run.py::test_cancel_mid_run_commits_intent_and_finalizes_interrupted
- tests/integration/test_service_run.py::test_concurrent_cancels_commit_one_intent_and_wait_one_cleanup
- tests/integration/test_service_run.py::test_cancel_first_rejects_run_started_and_finalizes_interrupted
- tests/integration/test_service_run.py::test_cancel_without_the_runs_read_model_is_rejected
- tests/integration/test_service_run.py::test_run_status_resolves_historical_runs_without_scanning_events
- tests/integration/test_service_run.py::test_run_status_fails_closed_on_missing_and_corrupt_projection
- tests/integration/test_service_run.py::test_close_commits_shutdown_intent_and_closes_the_provider
- tests/integration/test_service_run.py::test_concurrent_close_reuses_one_cleanup_task
- tests/integration/test_service_run.py::test_close_timeout_grace_exit_keeps_session_executable
- tests/integration/test_service_run.py::test_close_keeps_storage_only_accepted_runs_for_recovery
- tests/integration/test_service_run.py::test_cancel_reports_a_crashed_task_with_the_original_cause
- tests/integration/test_service_run.py::test_cancel_without_a_live_task_commits_exactly_one_intent
- tests/integration/test_service_run.py::test_run_status_fails_closed_on_unknown_owner_status
- tests/integration/test_service_run.py::test_close_timeout_s_rejects_non_finite_numbers
- tests/integration/test_service_run.py::test_cancel_survives_a_hard_cancelled_runnable_task
- tests/integration/test_service_run.py::test_close_tolerates_a_quarantined_session

### tests/integration/test_store_commit.py (16)
- tests/integration/test_store_commit.py::test_commit_normal_path_advances_watermark_and_consecutive_seq
- tests/integration/test_store_commit.py::test_commit_full_batch_retry_replays_without_reprojection
- tests/integration/test_store_commit.py::test_commit_idempotency_conflicts
- tests/integration/test_store_commit.py::test_commit_expected_seq_mismatch_raises_conflict
- tests/integration/test_store_commit.py::test_commit_projector_failure_rolls_back_entirely
- tests/integration/test_store_commit.py::test_commit_applied_seq_mismatch_isolates_and_gates
- tests/integration/test_store_commit.py::test_commit_seq_corruption_isolates_per_reason
- tests/integration/test_store_commit.py::test_commit_missing_read_model_isolates_and_gates
- tests/integration/test_store_commit.py::test_commit_stored_batch_retry_with_corrupted_row_isolates
- tests/integration/test_store_commit.py::test_commit_unknown_session_status_isolates
- tests/integration/test_store_commit.py::test_commit_stored_batch_retry_with_illegal_json_data_isolates
- tests/integration/test_store_commit.py::test_isolation_persist_failure_chains_and_keeps_memory_gate
- tests/integration/test_store_commit.py::test_commit_validates_caller_input
- tests/integration/test_store_commit.py::test_alembic_without_db_path_fails_fast
- tests/integration/test_store_commit.py::test_open_database_rejects_unknown_mode
- tests/integration/test_store_commit.py::test_store_and_txn_never_import_a_bus

### tests/integration/test_tool_bash.py (14)
- tests/integration/test_tool_bash.py::test_bash_runs_command_through_explicit_interpreter
- tests/integration/test_tool_bash.py::test_bash_nonzero_exit_code_is_data_not_tool_failure
- tests/integration/test_tool_bash.py::test_bash_timeout_zero_disables_the_deadline
- tests/integration/test_tool_bash.py::test_bash_default_timeout_is_120_seconds
- tests/integration/test_tool_bash.py::test_bash_timeout_kills_the_two_generation_tree
- tests/integration/test_tool_bash.py::test_bash_cancel_reaps_tree_and_reports_unknown
- tests/integration/test_tool_bash.py::test_bash_output_flood_truncates_with_original_size
- tests/integration/test_tool_bash.py::test_bash_flood_then_hang_times_out_while_draining_pipes
- tests/integration/test_tool_bash.py::test_bash_deny_hint_fires_but_does_not_block
- tests/integration/test_tool_bash.py::test_bash_interpreter_unavailable_diagnostic
- tests/integration/test_tool_bash.py::test_bash_missing_cwd_fails_with_diagnostic
- tests/integration/test_tool_bash.py::test_bash_empty_command_is_rejected
- tests/integration/test_tool_bash.py::test_bash_once_approval_executes_and_binds_full_command_cwd
- tests/integration/test_tool_bash.py::test_bash_reject_denies_without_execution

### tests/integration/test_tool_read.py (21)
- tests/integration/test_tool_read.py::test_read_renders_numbered_lines
- tests/integration/test_tool_read.py::test_read_resolves_nested_and_absolute_paths_inside_root
- tests/integration/test_tool_read.py::test_read_normalizes_crlf_line_endings
- tests/integration/test_tool_read.py::test_read_empty_file_is_zero_lines
- tests/integration/test_tool_read.py::test_read_paginates_with_offset_and_limit
- tests/integration/test_tool_read.py::test_read_limit_is_capped_at_max_read_lines
- tests/integration/test_tool_read.py::test_read_offset_out_of_range_is_validation_error
- tests/integration/test_tool_read.py::test_read_missing_file_is_not_found
- tests/integration/test_tool_read.py::test_read_directory_is_unsupported
- tests/integration/test_tool_read.py::test_read_nul_in_path_is_validation_error
- tests/integration/test_tool_read.py::test_read_absolute_path_outside_root_is_denied
- tests/integration/test_tool_read.py::test_read_traversal_outside_root_is_denied
- tests/integration/test_tool_read.py::test_read_symlink_inside_root_is_allowed
- tests/integration/test_tool_read.py::test_read_symlink_escape_is_denied_after_realpath
- tests/integration/test_tool_read.py::test_read_junction_escape_is_denied_after_realpath
- tests/integration/test_tool_read.py::test_read_binary_file_is_tool_failed
- tests/integration/test_tool_read.py::test_read_invalid_utf8_is_tool_failed
- tests/integration/test_tool_read.py::test_read_window_is_capped_at_one_mib
- tests/integration/test_tool_read.py::test_read_output_is_capped_at_64_kib_with_marker
- tests/integration/test_tool_read.py::test_read_event_sequence_and_hashes
- tests/integration/test_tool_read.py::test_invoke_executes_serially_in_call_start_order

### tests/integration/test_tool_static_trio.py (39)
- tests/integration/test_tool_static_trio.py::test_ls_lists_entries_directories_first_sorted
- tests/integration/test_tool_static_trio.py::test_ls_hidden_entries_excluded_unless_requested
- tests/integration/test_tool_static_trio.py::test_ls_nested_path_renders_relative_to_working_dir
- tests/integration/test_tool_static_trio.py::test_ls_limit_boundary_marks_truncation
- tests/integration/test_tool_static_trio.py::test_ls_empty_directory_message
- tests/integration/test_tool_static_trio.py::test_ls_missing_path_is_not_found
- tests/integration/test_tool_static_trio.py::test_ls_file_path_is_unsupported
- tests/integration/test_tool_static_trio.py::test_ls_nul_in_path_is_validation_error
- tests/integration/test_tool_static_trio.py::test_ls_traversal_escape_is_denied
- tests/integration/test_tool_static_trio.py::test_ls_symlink_escape_is_denied_after_realpath
- tests/integration/test_tool_static_trio.py::test_ls_junction_escape_is_denied_after_realpath
- tests/integration/test_tool_static_trio.py::test_ls_event_sequence_allow_root_internal
- tests/integration/test_tool_static_trio.py::test_glob_matches_pattern_sorted_files_only
- tests/integration/test_tool_static_trio.py::test_glob_recursive_double_star_matches_nested
- tests/integration/test_tool_static_trio.py::test_glob_hidden_excluded_by_default
- tests/integration/test_tool_static_trio.py::test_glob_no_matches_message
- tests/integration/test_tool_static_trio.py::test_glob_limit_boundary_marks_truncation
- tests/integration/test_tool_static_trio.py::test_glob_search_path_missing_is_not_found
- tests/integration/test_tool_static_trio.py::test_glob_search_path_not_a_directory
- tests/integration/test_tool_static_trio.py::test_glob_escaping_pattern_is_rejected
- tests/integration/test_tool_static_trio.py::test_glob_symlink_escape_is_denied_after_realpath
- tests/integration/test_tool_static_trio.py::test_grep_finds_matches_grouped_by_file
- tests/integration/test_tool_static_trio.py::test_grep_case_sensitive_by_default_with_insensitive_flag
- tests/integration/test_tool_static_trio.py::test_grep_literal_mode_escapes_regex
- tests/integration/test_tool_static_trio.py::test_grep_include_filter
- tests/integration/test_tool_static_trio.py::test_grep_hidden_files_are_searched_after_ts_alignment
- tests/integration/test_tool_static_trio.py::test_grep_git_directory_is_excluded_by_glob
- tests/integration/test_tool_static_trio.py::test_grep_single_file_path_targets_that_file
- tests/integration/test_tool_static_trio.py::test_grep_no_matches_message
- tests/integration/test_tool_static_trio.py::test_grep_limit_boundary_marks_truncation
- tests/integration/test_tool_static_trio.py::test_grep_empty_pattern_is_validation_error
- tests/integration/test_tool_static_trio.py::test_grep_invalid_regex_is_tool_failed
- tests/integration/test_tool_static_trio.py::test_grep_locked_sibling_file_is_tolerated_not_failed
- tests/integration/test_tool_static_trio.py::test_grep_missing_search_path_is_not_found
- tests/integration/test_tool_static_trio.py::test_grep_symlink_escape_is_denied_after_realpath
- tests/integration/test_tool_static_trio.py::test_grep_binary_file_is_tool_failed
- tests/integration/test_tool_static_trio.py::test_grep_rg_unavailable_is_explicit_diagnostic
- tests/integration/test_tool_static_trio.py::test_grep_broken_rg_is_explicit_diagnostic
- tests/integration/test_tool_static_trio.py::test_grep_real_path_absence_is_the_diagnostic_face

### tests/integration/test_tool_write_edit.py (36)
- tests/integration/test_tool_write_edit.py::test_write_creates_new_file_with_exact_bytes
- tests/integration/test_tool_write_edit.py::test_write_creates_missing_parent_directories
- tests/integration/test_tool_write_edit.py::test_write_overwrites_existing_file_completely
- tests/integration/test_tool_write_edit.py::test_write_then_read_round_trip_is_verbatim
- tests/integration/test_tool_write_edit.py::test_write_absolute_path_inside_root_is_accepted
- tests/integration/test_tool_write_edit.py::test_write_traversal_escape_is_denied
- tests/integration/test_tool_write_edit.py::test_write_symlink_escape_is_denied_after_realpath
- tests/integration/test_tool_write_edit.py::test_write_junction_escape_is_denied_after_realpath
- tests/integration/test_tool_write_edit.py::test_write_directory_target_is_unsupported
- tests/integration/test_tool_write_edit.py::test_write_nul_in_path_is_validation_error
- tests/integration/test_tool_write_edit.py::test_write_over_limit_content_is_rejected
- tests/integration/test_tool_write_edit.py::test_write_interrupt_before_replace_leaves_existing_file_intact
- tests/integration/test_tool_write_edit.py::test_write_interrupt_on_create_leaves_no_target_and_no_temp
- tests/integration/test_tool_write_edit.py::test_write_event_sequence_default_allow
- tests/integration/test_tool_write_edit.py::test_edit_replaces_unique_occurrence_preserving_rest
- tests/integration/test_tool_write_edit.py::test_edit_multiple_matches_fail_without_replace_all
- tests/integration/test_tool_write_edit.py::test_edit_replace_all_applies_every_occurrence
- tests/integration/test_tool_write_edit.py::test_edit_zero_matches_fails_without_touching_file
- tests/integration/test_tool_write_edit.py::test_edit_empty_old_string_is_rejected
- tests/integration/test_tool_write_edit.py::test_edit_identical_strings_are_rejected
- tests/integration/test_tool_write_edit.py::test_edit_unicode_variant_fallback_matches_typographic_file
- tests/integration/test_tool_write_edit.py::test_edit_unicode_noop_reports_unchanged
- tests/integration/test_tool_write_edit.py::test_edit_trailing_whitespace_tolerant_match
- tests/integration/test_tool_write_edit.py::test_edit_line_candidates_dedupe_overlaps
- tests/integration/test_tool_write_edit.py::test_edit_matches_lf_view_and_preserves_crlf_endings
- tests/integration/test_tool_write_edit.py::test_edit_mixed_region_endings_follow_matched_lines
- tests/integration/test_tool_write_edit.py::test_edit_line_endings_only_change_applies_exact_bytes
- tests/integration/test_tool_write_edit.py::test_edit_missing_file_is_not_found
- tests/integration/test_tool_write_edit.py::test_edit_directory_target_is_unsupported
- tests/integration/test_tool_write_edit.py::test_edit_traversal_escape_is_denied
- tests/integration/test_tool_write_edit.py::test_edit_symlink_escape_is_denied_after_realpath
- tests/integration/test_tool_write_edit.py::test_edit_invalid_utf8_file_is_tool_failed
- tests/integration/test_tool_write_edit.py::test_edit_source_over_limit_is_tool_failed
- tests/integration/test_tool_write_edit.py::test_edit_interrupt_leaves_file_unchanged
- tests/integration/test_tool_write_edit.py::test_write_asks_and_once_approval_writes_the_file
- tests/integration/test_tool_write_edit.py::test_edit_ask_rejected_leaves_file_unchanged

### tests/r0/test_platform_probe.py (8)
- tests/r0/test_platform_probe.py::test_probe_interpreter_is_explicit_and_versioned
- tests/r0/test_platform_probe.py::test_path_resolution_follows_filesystem_ts_order
- tests/r0/test_platform_probe.py::test_symlink_escape_rejected_after_realpath
- tests/r0/test_platform_probe.py::test_junction_escape_rejected_after_realpath
- tests/r0/test_platform_probe.py::test_probe_process_tree_reaps_all_descendants
- tests/r0/test_platform_probe.py::test_probe_sqlite_writer_lock_conflict_without_writes
- tests/r0/test_platform_probe.py::test_cli_writes_sanitized_evidence
- tests/r0/test_platform_probe.py::test_evidence_generation_modes_and_leak_guard

### tests/r0/test_provider_spike.py (24)
- tests/r0/test_provider_spike.py::test_text_stream_usage_after_finish
- tests/r0/test_provider_spike.py::test_text_stream_usage_before_finish
- tests/r0/test_provider_spike.py::test_interleaved_tool_calls_with_argument_shards
- tests/r0/test_provider_spike.py::test_wire_double_finish_is_rejected
- tests/r0/test_provider_spike.py::test_wire_text_after_finish_is_rejected
- tests/r0/test_provider_spike.py::test_wire_tool_call_after_finish_is_rejected
- tests/r0/test_provider_spike.py::test_eof_without_terminal_is_interrupted
- tests/r0/test_provider_spike.py::test_429_before_content_retries_then_succeeds
- tests/r0/test_provider_spike.py::test_429_before_content_retry_exhaustion
- tests/r0/test_provider_spike.py::test_no_retry_after_first_content
- tests/r0/test_provider_spike.py::test_header_timeout
- tests/r0/test_provider_spike.py::test_chunk_timeout
- tests/r0/test_provider_spike.py::test_total_deadline
- tests/r0/test_provider_spike.py::test_cancellation_propagates_and_cleans_up
- tests/r0/test_provider_spike.py::test_validate_sequence_rejects_violations[index-skip]
- tests/r0/test_provider_spike.py::test_validate_sequence_rejects_violations[index-duplicate]
- tests/r0/test_provider_spike.py::test_validate_sequence_rejects_violations[request-id-mismatch]
- tests/r0/test_provider_spike.py::test_validate_sequence_rejects_violations[double-terminal]
- tests/r0/test_provider_spike.py::test_validate_sequence_rejects_violations[chunk-after-terminal]
- tests/r0/test_provider_spike.py::test_validate_sequence_rejects_violations[missing-required-field]
- tests/r0/test_provider_spike.py::test_validate_sequence_rejects_violations[no-terminal]
- tests/r0/test_provider_spike.py::test_validate_sequence_accepts_contract_sequence
- tests/r0/test_provider_spike.py::test_retry_after_seconds_prefers_ms_header
- tests/r0/test_provider_spike.py::test_sdk_types_do_not_leak
