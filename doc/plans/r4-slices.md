# R4 实现切片拆分

> 修订：[T-B-036] 2026-11-05 首版产出（R3 收官 main **#131 `d1f618d`** 后；R4-S1 行#34 修复票已闭合并回 `7002ca8`、R4-S1b 行#35 收尾批在飞——**正式切片自 S2 起编，S1/S1b 编号已占用不复用**）；硬性输入①-⑤（T-B-036 任务书）逐条对照见 §7 自检清单。
> 席位B（T-B-036）产出，供 C 席按序实现。状态：**S2 起待派**（R4-S1 已闭合/R4-S1b 已派发收口中；主树 checkpoint 链与门禁基线见 §0.1）。
> 依据：doc/PHASE1-SLIM.md §4（L98-107 R4 全量+L118-132 必测故障矩阵）、doc/CORE-CONTRACTS.md（§2.1 L35/L51/L53、§3.3 L213-221、§4 L225-246、§5.1 L255/L259、§5.2 L266/L268/L270、§6 L285/L287/L302/L304、§7 L313/L320/L333/L337/L339、§8 L354/L356/L358）、doc/plans/r3-slices.md（头部+§0.1 衔接+§3 台账行#26-#35+§4 差异先例+§8 模板）、doc/reference/r3-exit-evidence.md（R3 收官基线）、doc/reviews/R4-S1-review.md（行#35 处方源）、doc/orchestration/tasks/T-B-036.md（硬输入五项）。
> TS 基准：`D:/26_code/opencode_py_ver/opencode-2.2.2/`（指纹 doc/source-baseline.json，r3-slices 头部口径 51 文件；行#34/#35 零新引用）。**R4 新引用 TS 文件须先补指纹（调度器亲算 SHA256 录入）**：`packages/core/src/mcp/{stdio,client,index}.ts`、`packages/cli/src/mini.ts`、`packages/schema/src/config/mcp.ts`（如引 `packages/core/src/file-mutation.ts` L134 TODO 佐证锚则同批）；`packages/cli/src/run/run.ts` 已录（source-baseline.json L38 亲证）。
> 拆分基准：按现行文档拆；A 席若修订 R4 定义，由调度器对齐后本文件再改版。

## 0. R4 范围锚定（摘自 PHASE1-SLIM §4 L98-107）

- 交付（L100 原文）："MCP manager/bridge、CLI 会话选择、启动恢复协调器、EventLog follow 的断连/慢消费者覆盖。"
- 验收 A（L102 原文）："显式信任一个 stdio server；命名空间无碰撞，text/structured 结果可回填；调用走同一 policy，生命周期退出后无子进程泄漏。"
- 验收 B（L103 原文）："覆盖 tool.started 前、外部副作用后但终态前、tool.finished 提交后的 kill/restart；前者未执行、中间 unknown、后者复用结果，均不自动重跑。generation batch 的 finalized→generated 必须原子提交；注入半批故障后只能看到完整批或没有批。"
- 验收 C（L104 原文）："待审批重启后失效，saved 决定只作审计需重新确认；相同 session 的重复 prompt、并发 cancel、慢订阅、丢通知和补读不造成重复消息/重复执行。恢复后再发新 prompt，由严格 fake server 检查每个合法 call 恰有一个 result，包含后续尚未 invoke 的计划调用。shutdown/crash race 验证 close 先持久取消和终态，再关闭 provider/MCP/DB；超时进入 cleanup_failed 或在下次启动恢复。"
- 验证（L105 原文）："测试自有的最小 MCP server + 故障注入；用计数器/临时文件证明副作用次数。MCP SDK 集成测在进程边界运行。"
- 会话选择（L107 原文）："`--continue` 选当前 workspace 最近会话；没有则新建。`--session` 不存在报 NotFound。若选中目录/信任快照失效则停止诊断，不自动换目录。"
- 必测故障矩阵相关行（§4 L118-132）：L123 "S2 订阅建立/重连/慢消费者——after/Synced/InvalidCursor 语义明确；durable 从 DB 补读，ephemeral 可丢"；L127/L128（S6 并发/取消/崩溃+accepted-cancel/shutdown race——恢复不重复副作用/close 先持久终态）；L129 "S1/S4 generation batch——finalized 按 ordinal 先写、step.generated 后写且同批原子；半批崩溃后不留下孤立 finalized/generated"；L131 "MCP 断线/schema 变更——新调用使用新 catalog，旧批准失效，结果未知不自动重试"。

### 0.1 R3→R4 交付面衔接（现状基线 #136 `e12e4bb`：pytest 924（→926 随行#35）/mypy 92 files 零问题/双 ruff 绿；CI 双 OS 绿先例 41952806310（#132），本轮 41963085214 在跑）

- **checkpoint 链**：#131 R3 收官（T-B-034 PASS 0/0/1/2+merge `9546a8c`）→ #132 CI round 3 双 OS 绿（41952806310）→ #133/#134 行#34 票链（T-C1-033 `eb01698`+T-B-035 评审 PASS 0/0/0/2）→ merge `7002ca8` → #135 R4-S1 关闭 → **#136 `e12e4bb`**（双派发：T-C1-034 行#35 在飞+T-B-036 本票）→ **#137 `c848f3b`**（T-C1-034 验收 PASS+r4-slices 入库+TS 指纹 51→57+OP-R4-1 用户裁决=自研）→ merge **`c4e70ec`**（行#35 B-baseline `96d60f0` 并回零冲突；T-B-037 PASS 0/0/0/1）→ **#138**（行#35 收口：r3-slices 状态翻牌+本节链更新；主树门禁 926/clean/format 0/mypy 92）→ **#139 `76d869c`**（推送 5 提交+CI `41984552949` 双 OS 绿[Unix dotfile 腿首跑=T-B-037 处方②]+T-C1-035 任务书入库+R4-S2 派发）→ **#140 `b187a82`**（T-C1-035 验收 PASS[36min/写面 6 项 1:1/门禁 939=926+13+98=92+6 双跑同值]+T-B-038 派发）→ merge **`9b24926`**（S2 B-baseline `e490cd2` 并回零冲突；T-B-038 PASS 0/0/0/2）→ **#141**（S2 收口：报告入库+本节链更新+§3 行 7 快照翻新；主树门禁 939/clean/format 0[289]/mypy 98——**S3 起基线=939/98**）→ **#142 `a3f97da`**（T-C1-036 任务书入库+TS 锚 `schema/src/mcp.ts` 指纹补录[57→58]+S3 派发）→ merge **`c7d7727`**（S3 B-baseline `4c7ae37` 并回零冲突；T-B-039 PASS 0/0/0/3）→ **#143**（S3 收口：报告入库+本节链更新；主树门禁 986/clean/format 0[294]/mypy 100——**S4 起基线=986/100**）→ **#144 `c22955b`**（T-C1-037 任务书入库+R4-S4 派发；推送 10 提交齐推）→ **#145 `ce31ad6`**（CI `42143155157` ubuntu 腿红[test_crash_sweeps_tree 1 败]→回流票热修：mcp/stdio.py 崩溃路径进程组清扫 +17/−1；CI 复跑 `42152172197` 双腿绿）→ **#146 `93430b3`**（T-C1-038 任务书入库+R4-S5 派发[S4 评审窗口并行，用户授权]）→ merge **`bc9e166`**（S4 B-baseline `11c1abe` 并回零冲突[merge-base `c22955b` 双侧路径交集=0]；T-B-040 PASS 0/0/3/3）→ **#147**（S4 收口：报告入库+台账收口+本节链更新；主树门禁 999/clean/format 0[300]/mypy 102——**S6 起基线=999/102**）→ **#148**（S5 返回验收 990=986+4 零产品改动+时间戳纪律合规→B-baseline `11635ad`+T-B-041 派发；推送[用户授权]`ce31ad6..8f9e3c6` 4 对象 CI `42156729748` 双腿绿；T-C1-039 微票任务书入库[S4 追加弧，用户裁决]）→ merge **`fe70bda`**（S5 B-baseline `11635ad` 并回零冲突[merge-base `93430b3` 路径交集=0]；T-B-041 PASS 0/0/0/4——format 定谳 canonical 298/21→20 六证勘误）→ merge **`9b56313`**（S4b 微票 B-baseline `9709aee` 并回零冲突[merge-base `3a26876` 路径交集=0]；T-B-042 微评审 PASS 0/0/0/1——处方 2 对账 a 闭合、b/c 并入 S6）→ **#149**（S5+微票双收口：双报告入库+台账收口+format 门禁语义注记入 ORCHESTRATION §2+本节 §2.4 状态行；主树门禁 1004/clean/format 0[306]/mypy 103——**S6 起基线=1004/103**）→ **#150**（推送[用户授权]`8f9e3c6..4090c3f` 14:07:51+CI `42172518444` 双腿绿[5 新测 Unix 腿首跑全绿，零回流票]+S6 法证批[TS 树定位姊妹目录/双锚哈希亲证/缝面钉死/自漂移陷阱/冲击面恰 1 枚举/计数钉 1004]+T-C1-040 任务书入库[R4-S6 派发；fold ①-④+OP-R4-3 保守=全等+v3/MCP 组合缝=处方 3 三件套]+fresh session ses_orch_C1r4s6+WATCHER93）。→ **T-C1-040 返回+验收**（15:48:44 SEAT-EXIT 0；80.1min 超止损 19min ④-4 如实；调度器 diff 级验收 PASS[三裁量采纳+门禁复跑三方逐值同 1014/104/309]）→ **B-baseline `d88af64`**（7 路径 +1298/−37）→ **T-B-043 PASS 0/0/1/3**（B 续用 ses_orch_B2 16:01-16:18：12 维全 PASS+五路独立复跑逐值同+三裁量采纳+折叠四项闭合；轻微-1 进度占位预填自纠+nit×3）→ merge **`d9d1747`**（--no-ff 零冲突；主树门禁 1014/clean/format 310[309+报告 md]/mypy 104——**S7 起基线=1014/104**）→ **#151**（S6 收口：报告入库+台账双翻牌+ORCHESTRATION §2 双条款增补[止损续跑+占位戳禁令]+本节链更新+§2.5 状态闭合）→ **#152**（S6 终收口：CI `42188197712` 双腿绿[16:38:33 completed success——test_cli_r4.py 10 测 Unix 腿首跑全绿零回流]+用户常设授权[推送+下任务派发自动执行，ORCHESTRATION §1 修订+待定夺 #9 记档]+S7 法证批六轮[基准文档/e2e 先例/注入 idiom/S4 已闭合面清点/T7 机具/App close 面]+bind_mcp_tools 生产接线裁决[测试 App 直组承载 run 级路由，生产 spawn+bind 留 R5+]+基线钉定 1014[16:36:06 collect-only 亲钉]+T-C1-041 任务书入库[R4-S7 派发；三窗 kill/restart+半批注入+严格配对审计+§7 L339 close 序；fresh session ses_orch_C1r4s7+WATCHER95]+本节 §2.6 状态行）→ **T-C1-041 返回+验收**（ECONNRESET 自愈后 4h shell cap 20:53 截杀→RELAUNCH-CONT 20:55:51 同会话续跑→21:07:30 SEAT-EXIT 0；7 测[4 真实 kill+1 monkeypatch+2 进程内 race]；调度器 1380L 亲读+门禁复跑 1021/105/313 逐值同+两裁决[close 序拆分+半批读法]）→ **B-baseline `38aeb68`**（2 路径 +1445/−0）→ **T-B-044 PASS 0/0/0/3**（B 续 ses_orch_B2 21:14-21:26：12 维全 PASS+五路独立复跑逐值同+两仲裁复核成立+时间戳零占位首证；nit×3 记录级）→ merge **`59a7d61`**（--no-ff 零冲突；主树门禁 1021/clean/format 0[314=313+在席评审报告 md]/mypy 105——**S8 起基线=1021/105**）→ **#153**（S7 收口：报告入库+台账双翻牌+ORCHESTRATION §2 增补[re-arm hard-limit 到线强制收口]+本节链更新+§2.6 状态闭合；常设授权下自动推送）→ **#154**（S7 推送 `c0db29a..48b56e5`+CI `42227544216` 双腿绿[test_e2e_r4.py 7 测 Unix 腿首跑全绿]+S8 任务书入库+T-C1-042 派发[worktree r4-s8+fresh session ses_orch_C1r4s8+WATCHER96；止损 60min——常设授权下自动推送+自动派发]+本节 §2.7 状态行）。
- **R4-S1/R4-S1b 编号已占用**：S1=行#34 修复票（已闭合；e2e 回归钉四面断言在册——S4/S7 复用其先例：_await_terminal/store (failed,unknown)/wire 失败回灌/EventLog 三账）；S1b=行#35 伴生收尾批（grep TS 对齐+冻结面 7 处 L277 去行号化批二+doc 修正批，**已闭合随 #138，不重复设行**：T-C1-034 23min 落地 pytest **926**/mypy 92=席位与调度器双跑同值+T-B-037 PASS 0/0/0/1+merge `c4e70ec`+主树四门禁复跑同值；r3-slices §3 行#35 状态已翻牌——**S2 起基线=926/92**）。
- **follow/慢消费者底座已在**：EventLog.follow/EventCursor 已实现（bus/eventlog.py；runner.py L58/L406 已消费）；SlowConsumerError 已实现（bus/bus.py 队列上限断开+cli.py L821 显示面零回归在册）——S5=覆盖/硬化非新建。
- **boot_id 底座已在**：app.py L116 `boot_{uuid4().hex}` 每次启动生成；policy/invoker 已按 boot_id 作用域（policy.py L159-172、invoker.py L906-921）——验收 C 首句机制面已在，S6/S7 落验证。
- **MCP 配置边界已在、可执行面不在**：loader.py HOST_ONLY_KEYS 含 "mcp"（L81——项目配置无权隐式扩大，§6 L285 语义已钉）；`_ASK_NAMESPACES={"bash","mcp"}`（policy.py L86——ask 默认档位已在）；**无 src/opencode/mcp 面**（S2 新建）；**ConfigSnapshotV2 省略 MCP ref/启动指纹**（loader.py docstring L28 明示——S3 按 §6 L287 补齐）。
- **CLI 会话选择不在**：cli.py 无 --continue/--session（grep 亲证零命中）——S6 落地。
- **`_drive_run` 吞没面在册**：service.py L1221-1239（行条款见 §2.3）——S4 核心票条款源。
- **恢复协调器不在**：启动期无未结束 run 扫描/终止面（§7 L333 行为无实现者）——S4 新建。ts 崩溃恢复 TS 亦未定义（file-mutation.ts L134 自带 TODO，差异表 1）。

## 1. 切片序列总表

| ID | 一句话目标 | 接缝 | 前置 | C 席预计 |
|---|---|---|---|---|
| R4-S2 | stdio MCP transport/manager/bridge：进程生命周期+超时三分+退出栈杀树+最小测试 MCP server（进程边界） | src/opencode/mcp 新、process 复用、app/service close 链 | 行#35 收口（基线 926） | 90-120min |
| R4-S3 | MCP catalog/命名空间/policy/快照接线：server_id 参与命名+指纹绑定+ConfigSnapshot v3（确定性 upcaster） | mcp、tools/registry、tools/policy、config、contracts/events | S2 | 75-90min |
| R4-S4 | 启动恢复协调器（核心票）：未结束 run→interrupted+行#27 三条款+`_drive_run` 吞没面终态策略 | session/recovery 新、app、service、storage/repos | S3 | 75-90min |
| R4-S5 | EventLog follow 断连/慢消费者覆盖：重连 at-least-once 去重+丢通知补读+坏页终止+InvalidCursor | bus/eventlog、bus/bus、tests | 行#35 收口（**可与 S2/S3 并行**） | 45-60min |
| R4-S6 | CLI 会话选择 `--continue`/`--session`+重启审批面（saved 审计化/pending 失效） | cli.py、session/service | S4 | 45-60min |
| R4-S7 | 副作用不确定性+恢复 e2e：三窗 kill/restart+generation 半批注入+恢复后严格 fake server 配对审计 | tests、注入基建 | S2-S6 | 60-75min |
| R4-S8 | R4 退出证据收口（r4-exit-evidence.md）+验证行全量核对 | doc、tests | S2-S7 | 45-60min |

**依赖序**：S2→S3→S4→S6→S7→S8；S5 独立于 MCP 面（契约 §4 既有实现），**可与 S2/S3 并行派发**；S7 需 S2-S6 全量在位（MCP 计数器+恢复+审批重启面齐）；S8 收口全量。首切片基线=行#35 收口后 926/92（若派发时行#35 未并回则按 924 顺延，任务书标定）。

## 2. 每切片详情

### 2.1 R4-S2 stdio MCP transport/manager/bridge

- **目标**：`src/opencode/mcp/` 新包——stdio transport（JSON-RPC 帧收发+帧上限+stderr tail 捕获+优雅关闭升级强杀+意外退出码诊断）；MCP manager（每 server 生命周期：spawn/initialize 握手/tools/list/call_tool、超时三分（startup/catalog/execution）、close 杀进程树、断线→unknown 不自动重试副作用、最多重建连接供下一次显式调用——§6 L304 全文）；bridge→ToolAdapter 形态（text/structured 回填 ToolResult content/structured_output——§5.1 L253；命名空间映射归 S3）。实现载体=OP-R4-1（官方 `mcp` SDK vs 自研最小 JSON-RPC——依赖引入归调度器）。**最小测试 MCP server 自有 fixture**（L105"测试自有的最小 MCP server"+"集成测在进程边界运行"=subprocess 实跑非 in-process 桩）。
- **涉及接缝**：src/opencode/mcp/（新）、src/opencode/process/（winjob/runner 子进程环境复用——R3-S5 先例）、app.py/session/service.py（close 链挂接：§7 L320/L339 "依次关闭 MCP、provider、DB"——MCP 关闭面首次接线，归属 app 装配 vs service 持有由 C 席按 close 序落定）。
- **新增文件**：`src/opencode/mcp/`（transport/manager 拆分由 C 席定）、`tests/integration/test_mcp_stdio.py`、最小 MCP server fixture（测试自有）。
- **TS 基准映射**：`mcp/stdio.ts`（L9-18 常量：CLOSE_GRACE 2s/FORCE_KILL_AFTER 2s/OUTGOING_CAPACITY 64/MAX_FRAME_BYTES 16MiB/EXIT_WAIT 1s/STDERR_TAIL 1000；scope 持有进程=close 即杀进程组、经 Environment spawner 而非 SDK host-bound）、`mcp/client.ts`（L35-40 超时常量：startup 30s/catalog 30s/execution 12h/attached 10m/terminate 1s；isTransient；CallToolResult={isError,structured,content}；NeedsAuth/Connect/SessionExpired 错误族）、`mcp/index.ts`（L355-479：startServer 含 tools/list 失败即标 failed 不留空工具表先例；recover/recovering 为 HTTP 会话语义**不移植**——差异表 3）。**须先补指纹**。
- **验收标准**：验收 A 拆面——本切片落 transport/生命周期/无泄漏面（"生命周期退出后无子进程泄漏"双平台断言）；"命名空间无碰撞/调用走同一 policy"同链归 S3；A 句整体闭环见 S7。§6 L304 首句逐句（argv 直接 exec 不拼 shell、环境变量最小集+显式 secret refs、启动前宿主确认完整本机权限）。
- **测试计划**：最小 server 进程边界往返（initialize→tools/list→call_tool）；超时注入（startup/catalog）；server 中途 exit→unknown+stderr tail 诊断（L304"断线按 unknown"）；close/崩溃双路杀树+子进程退出断言（Windows Job Object/POSIX process group）；帧超限拒绝；Python 侧常量取值按 OP-R4-5 保守裁量并在 docstring 注记。
- **开放点**：OP-R4-1（SDK vs 自研）、OP-R4-5（常量取值）。

### 2.2 R4-S3 MCP catalog/命名空间/policy/快照接线

- **目标**：MCP server_id 参与命名（§5.1 L255：模型名可逆/碰撞检测映射，MCP server ID 必须参与命名——两 server 同名工具拒绝）；catalog 快照语义（每次 provider 请求固定快照+只能调用已展示且有 capability 的工具，L255；远端 tools/list 改变后先建立新 catalog、不能用旧批准调用改名工具——L304 末句）；policy 接线（§5.2 L266 bash/MCP ask 默认=既有 `_ASK_NAMESPACES`；L268 MCP 绑定 server 启动指纹+schema/catalog 版本；L270 对 unknown 执行相同输入的重试必须重新 ask 不复用旧 once/always）；**ConfigSnapshot 承载 MCP ref+启动指纹**（§6 L287；现状 V2 省略——loader.py docstring L28）→ §8 L358 纪律=v3 快照+py.session.created v3+确定性 upcaster（沿行#24 落地 idiom：双版本注册并存+旧 fixture 可读+v1 不静默补齐）；恢复期指纹变化停住面归 S4（L302）。
- **涉及接缝**：tools/registry.py（mcp 命名空间映射）、tools/policy.py（指纹/catalog 版本绑定）、config/loader.py（host-tier mcp 装载+快照 v3 装配）、contracts/events.py（snapshot v3+upcaster）、session/service.py（快照入口 `_coerce_config_snapshot` 扩容）。
- **新增文件**：`tests/contract/test_mcp_naming.py`（或并入 test_tools.py）、`tests/integration/test_mcp_catalog.py`、v1/v2/v3 快照 fixtures。
- **TS 基准映射**：`packages/schema/src/config/mcp.ts`（ConfigMCP 配置形态参照）、`mcp/index.ts`（catalog/tools 变更面；watch onToolsChanged 自动热刷新**不移植**——差异表 4）。**须先补指纹**。
- **验收标准**：验收 A 后半（命名空间无碰撞、text/structured 结果可回填、调用走同一 policy）+L304 末句+故障矩阵 L131（新调用使用新 catalog，旧批准失效，结果未知不自动重试）。
- **测试计划**：命名碰撞双分支（同 server_id 同名拒绝/异 server 同名拒绝+可逆映射往返）；tools/list 变→新 catalog→旧 approval 失效→重新 ask（unknown 后同输入重试重新 ask）；快照 v3 upcaster 往返（v1/v2/v3 fixture+canonical hash 漂移检测+§6.2 重算）；项目 config 注入 mcp 定义拒绝面（HOST_ONLY_KEYS 既有测试零回归+新钉）。
- **开放点**：OP-R4-2（启动指纹 canonical 形态——server 名+command/argv+env refs 摘要的哈希构成，A 席裁量）。

### 2.3 R4-S4 启动恢复协调器（核心票）

- **目标**：`src/opencode/session/recovery.py` 新面——App 启动期扫描全部 session：**未结束 run（accepted/running/cancelling）终止为 interrupted**（§7 L333：绝不自动续跑 R1 遗留 accepted——恢复协调器是该行为的唯一实现者）；三窗对账（§3.3 L216-221：started 前=未开始/started 后 finished 前=unknown 即使可能已成功/finished 已提交=直接读取结果绝不重跑）；L302 恢复三停住面（工作目录失效/凭据引用失效/MCP 启动指纹变化→明确停住要求宿主重新配置/批准，不静默换一套权限）；cleanup_failed 恢复（§7 L339 末句：不可写则保留未完成记录供显式诊断或下次通过完整性验证后的恢复）；诊断 session 不得追加业务事件（L339 只读纪律）；`session_status` 不触发恢复红线不动（§7 L313）。
- **行#27 三条款（r3 台账在册裁定"恢复切片任务书三条款"——本切片兑现，粒度=文件面+计数+验收口径）**：
  - **① 孤儿 PENDING 清理**：启动扫描权限投影/事件流，close-timeout 硬取消掉队的 pending→closed(cancelled)（§2.1 L53 角落；现状无恢复扫描兜底）。验收=注入孤儿 pending fixture→清扫后该审批不可激活+审计可查+既有 close 硬取消路径零回归。
  - **② closed 门对账**：启动扫描对 active/诊断/cleanup_failed 各 session 的 closed_outcome 词表对账——`"restart"` 零生产者前瞻（r3 行#27 裁定"崩溃恢复面前瞻非死词表"）：恢复协调器成为其首个生产者（遗留 pending/run 因重启而关闭时记 restart）或作受认定角落声明；C 席落定后进度④浮报，不预写裁决。
  - **③ `.opencode-fs-*.tmp` 启动清扫**：App/CLI 启动期对声明 roots 内 `.opencode-fs-*.tmp` 模式串清理（write.py L196 `tempfile.mkstemp(prefix=".opencode-fs-", suffix=".tmp")` 遗留面；r3 裁定**宿主运营面承载**=App 启动路径非 session 服务层）。跨进程 kill 无注入缝不可测（T-C1-028 ④-1 先例）→测试以受控临时目录+直接构造遗留文件承载，如实披露。
- **`_drive_run` 吞没面行条款（硬输入③，文件面+计数+验收口径）**：`src/opencode/session/service.py` `_drive_run` **L1221-1239**——恰 1 处 `except Exception` 吞没面（L1236-1237：错误记入 `_schedule_errors[run_id]`，仅 cancel/close 路径浮现为 LifecycleCleanupFailed）+1 处 CancelledError re-raise（L1234-1235=close-timeout 路径自有信号非错误）+1 处 finally 出册（L1238-1239）。S4 增补语义=**该记账面为进程内纵深防御，恢复协调器不得依赖之**（跨进程崩溃后内存账不在场——恢复判定只认 durable 日志）；被吞错误对应的 run 须在启动扫描按 durable 日志获得明确终态（interrupted/按三窗 unknown），不停留 running。验收口径=双面测试：①进程内任务崩溃→cancel 浮现 LifecycleCleanupFailed 既有行为零回归；②模拟跨进程崩溃（无 `_schedule_errors` 记账）→恢复扫描按 durable 日志给出终态。service.py 触碰红线=仅 `_drive_run` docstring 恢复语义一行注记（可选）+零行为改动，超出即停下上浮。门禁预期=pytest 926+N 零回归/mypy 92+N files 零问题/双 ruff 绿。
- **涉及接缝**：session/recovery.py（新）、app.py（挂接点）、session/service.py（仅上述红线内）、storage/repos.py（扫描查询面）。
- **TS 基准映射**：**无对应物**——TS 无启动恢复协调器（TS file-mutation.ts L134 自带 TODO "Define crash recovery and idempotency for side effects between Tool.Called and durable settlement" 佐证 TS 亦未定义；契约 §7 L333 Python 专有裁决为准——差异表 1，勿找 TS 蓝本）。
- **验收标准**：验收 C 后段（shutdown/crash race 超时进入 cleanup_failed 或在下次启动恢复）+§7 L333/L339 逐句+行#27 ①②③+`_drive_run` 行条款双面。
- **测试计划**：三窗 kill/restart 注入（started 前/外部副作用后终态前/finished 提交后——计数器/临时文件证明副作用次数，L105；S7 e2e 扩全链）；半开 DB 状态扫描（accepted 遗留→interrupted 零自动续跑+新 prompt 可发）；L302 三停住面（明确诊断不静默）；行#27 ①②③；`_drive_run` 双面（上述）。
- **开放点**：OP-R4-6（触发点与范围：App 启动同步全量 vs CLI 命令前置惰性——保守=App 启动全量，`session_status` L313 红线不动）。

### 2.4 R4-S5 EventLog follow 断连/慢消费者覆盖

- **目标**：契约 §4 既有实现的覆盖收口——follow 重连 at-least-once+消费者按 session+seq/id 去重（L242）；丢通知补读（wakeup 只作唤醒信号+重复信号合并+最长 1 秒轮询兜底，L241）；坏页 StorageCorrupt 终止+已 yield 健康事件有效+不越过坏点 Synced（L244-245）；SlowConsumer 断开（L228 队列上限溢出断开不静默丢 durable+退出 context 注销释放队列）；InvalidCursor 语义（L233：负数/跨 session/超已提交高水位 H 明确报错不空等；H=§3.2 已提交日志最大 seq 不以投影水位代替）。
- **涉及接缝**：bus/eventlog.py、bus/bus.py（既有面零回归）、tests（新增覆盖）。
- **新增文件**：`tests/integration/test_follow_recovery.py`（或并入 test_eventlog.py 扩容——C 席定，进度文件记落点）。
- **TS 基准映射**：`bus.ts` L786/L860 readAfter（TS 锚已录指纹；Python 不沿用 TS 跳过未知类型并推进 cursor——L246 已裁决，差异表 6）。
- **验收标准**：验收 C 中段（慢订阅、丢通知和补读不造成重复消息/重复执行）+故障矩阵 L123 逐句。
- **测试计划**：慢消费者（队列上限受控注入→断开+不静默丢 durable+cli.py L821 显示面零回归）；丢 wakeup（通知抑制→1s 轮询兜底仍补读+durable 从 DB 补读而 ephemeral 可丢）；重连去重（同 seq 重复交付→按 session+seq 幂等）；坏页（注入损坏→StorageCorrupt+此前健康事件有效+无越过坏点的新 Synced）；InvalidCursor 三分支。
- **开放点**：OP-R4-4（测试形态：真队列上限注入 vs 合成 SlowConsumerError——保守=受控上限注入，确定性优先）。
- **状态（#149 收口）**：**已闭合**——T-C1-038 交付 `11635ad`（新文件 test_follow_recovery.py 4 测 461L，零产品改动）+T-B-041 PASS 0/0/0/4+merge `fe70bda`；OP-R4-4 裁量=受控上限注入（VolatileBus queue_limit ctor 公开面）已落地；主树基线传播 1003/103（S4b 微票并回后终值 1004/103）。

### 2.5 R4-S6 CLI 会话选择+重启审批面

- **目标**：cli.py `--continue`（当前 workspace 最近会话，没有则新建）/`--session <id>`（不存在报 NotFound）——L107；选中目录/信任快照失效→停止诊断不自动换目录（L107 末句+§6 L302 快照漂移停住面复用）；重启审批面：pending 审批重启后失效+saved 决定只作审计需重新确认（验收 C 首句；机制底座=boot_id 作用域已在 §0.1——本切片落宿主可验证面：恢复后新 prompt 触发重新 ask 而非自动激活旧 saved）；`--fork` **不移植**（差异表 5）。
- **涉及接缝**：cli.py（参数面+会话枚举/选择）、session/service.py（会话枚举/读取面，若缺）。
- **新增文件**：`tests/integration/test_cli_r4.py`（会话选择+重启审批面）。
- **TS 基准映射**：`cli/src/mini.ts` L201、`cli/src/run/run.ts` L73（`--fork requires --continue or --session` 守卫先例——Python 无 --fork 仅取 --continue/--session 语义）。**mini.ts 须先补指纹**；run/run.ts 已录。
- **验收标准**：L107 逐句+验收 C 首句（宿主面）。
- **测试计划**：--continue 命中最近/无会话新建两分支；--session NotFound；快照失效停止诊断（漂移 hash 注入→不自动换目录/不静默重建）；重启后 saved 不激活+pending 失效（依赖 S4 恢复闭环——恢复→新 prompt→重新 ask 全链）。
- **开放点**：OP-R4-3（最近会话判定口径：快照 working_dir 字符串全等 vs 规范化/前缀——保守=全等）。
- **折叠记档（#150 任务书承载）**：①T-B-042 处方 2b（恢复后 closed permission reply 显式拒断）→T7 全链；②处方 2c（gate 测试阻塞期读面恰 1 断言——T-B-040 时点 test 11、微票插入后现位 #12 `test_gate_blocks_writes_until_the_scan_completes_and_fails_closed`）；③T-B-040 处方 3 三件套=真 S6 缝：v3 采拥（resolve_runtime_v3 同参直换）+MCP 注入（definitions 构造[win32 最小集 SystemRoot/PATH=T-B-038 ⑥② 既承]+add_server 不 start——App 边界「只拥有 close 不注册 MCP 工具」既定）+mcp_refs 宿主声明（**恒取 snapshot.mcp_servers；严禁自已解析 definition.env 派生 env_refs——最小集键入指纹=每会话自漂移停摆陷阱**；[]≠None 两态分明，CLI 后者恒不取）；④进度首建硬条款随任务书强化。
- **勘误（#150）**：mini.ts 已在指纹册（58 条之 L57，sha256 调度器亲证逐字节相符）——本节「mini.ts 须先补指纹」+§8 模板同句系 S3 时点陈旧文案；TS 树实位=`D:/26_code/opencode_py_ver/opencode-2.2.2`（Python 树姊妹目录）。
- **落点修正（#150）**：枚举/读取面落 **repos 层**（App 构造前只读选择期——chicken-and-egg：App 构造需 recovered runtime 注入，故选择必须前置且 service 尚不存在）；service.py 本票零触碰（「若缺」裁定=不缺，缝在组合根+repos）。sessions 表无 working_dir 列（0001_initial 亲证）→枚举=events 按 type 扫描（created=INTEGER epoch ms 数值排序+session_id 尾序 tie-break）。
- **范围裁定（#150）**：bind_mcp_tools 生产接线（tools/list→registry 绑定+server 启动）**不在本票**（T-B-040 处方 3 缝=注入+声明；app.py L237-241 既定边界）——遗留 R5+/S7-e2e 评估项。v3 采拥存量冲击面=恰 1 测试（test_config_loader.py L453 单测 version==2 断言→任务书授权单测期望翻转 2→3）。
- **状态（#151 闭合）**：已闭合——T-C1-040 交付 PASS（写面 7 路径 +1298/−37；10 测承载 T1-T8[T5 拆三]；门禁 1014/104/309 三方逐值同；三裁量采纳[env 解析回退语义/NotFoundError 拒断面/两写面内防御]）+T-B-043 评审 **PASS 0/0/1/3**（12 维全 PASS；轻微-1 进度占位预填自纠——条款硬化随 #151；nit-3 止损超时首例追认+条文化）；merge `d9d1747`；主树门禁 1014/clean/format 310/mypy 104。**S7 起基线=1014/104**（S7 e2e 需 S2-S6 全量在位——MCP 计数器+恢复+审批重启面现已齐备；bind_mcp_tools 生产接线评估遗留 S7 任务书撰定面）。

### 2.6 R4-S7 副作用不确定性+恢复 e2e

- **目标**：验收 B 全量（三窗 kill/restart：前者未执行、中间 unknown、后者复用结果，均不自动重跑；generation batch finalized→generated 原子提交+注入半批故障后只能看到完整批或没有批——若 R2 既有测试已覆排序/原子则本切片增半批注入回归钉，状态核对后定）；验收 C 后段（恢复后再发新 prompt——严格 fake server 检查每个合法 call 恰有一个 result，包含后续尚未 invoke 的计划调用）；shutdown/crash race（close 先持久取消和终态，再关闭 provider/MCP/DB——§7 L339 顺序断言：cancel_requested(reason=shutdown)→permission.closed(outcome=cancelled)→释放提交锁→等待→超时→tool.finished(unknown)+interrupted+cleanup_failed→依次关 MCP/provider/DB）。计数器/临时文件证明副作用次数（L105）；MCP server 计数器（call_tool 落临时文件）证明不重跑。
- **涉及接缝**：tests/integration/test_e2e_r4.py（新，沿 test_e2e.py/test_e2e_r3.py 命名先例）、既有 fake server 严格配对面扩展、注入基建（沿 `_CancelAfterStepFinishedCommitter` 同款 store 正常提交注入——n-3 携注纪律沿用）。
- **新增文件**：`tests/integration/test_e2e_r4.py`。
- **TS 基准映射**：无新引用（e2e 收口）。
- **验收标准**：验收 B 逐句+验收 C 后段+故障矩阵 L127-129/L131 全行。
- **测试计划**：三窗 kill 注入点=子进程自杀/monkeypatch commit 中断（跨进程 kill 无注入缝先例=T-C1-028 ④-1，协议不可达分支按④如实披露）；generation 半批（commit 钩子注入→只见完整批或没有批）；恢复后配对审计（fake server 严格模式：每个合法 call 恰一个 result 含未 invoke 计划调用的 cancelled 结果——§2.1 §3.3 L214 合成配对面）；MCP 副作用计数交叉验证（沿 R2 e2e EventLog SQL+requests 模式）。
- **开放点**：无（注入形态按④纪律如实披露，不预写裁决）。
- **状态（#153 闭合）**：已闭合——T-C1-041 返回验收+T-B-044 评审 PASS 0/0/0/3+merge `59a7d61` 全链落定：7 测档位如实（三窗 kill/restart+多调用计划合成=4 真实 kill[稳定阻塞态 proc.kill]+generation 半批=类级 monkeypatch EventCommitter.commit armed-once+⑥a/⑥b close 序=进程内 race）+双侧严格配对审计（_audit_tool_pairing durable[execution_id 串+计划部件覆盖]+_audit_wire_pairing wire[tool_calls↔role:tool 零缺失/重复/多余]）+§7 L339 App 级 close 序断言（含 durable 库可读等价证）+③ 条件路径零触发（既有 mcp_stdio_server.py 只读复用，零 server 新文件）+④ 两事件如实披露（ECONNRESET 停滞+4h shell cap 截杀→RELAUNCH-CONT 20:55:51 同会话续跑→21:07:30 SEAT-EXIT 0 干净全量复跑全绿；止损 75min 跨线 ≈3h 记录级追认——ORCHESTRATION §2 re-arm 上限收紧条文化[本票先例锚]）+两调度器裁决（A=close 序拆 ⑥a/⑥b 成立：registry.py L419 Phase-1 串行 invoke 锁下字面合并协议不可达，T-C1-028 ④-1 披露+最近可构造形先例；B=半批零孤立行=finalized==0 AND generated==0 任务书字面，撕裂态单次原子 store.commit 不可达+直拟面 test_recovery.py 窗 2 已闭合——不开新票）+门禁三方逐值同（席位 1021[238.68s]/调度器复跑 1021[244.03s]/B 1021 collected+定向 7 passed 14.73s；ruff clean/format 313[107py+206md]/mypy 105）→B-baseline `38aeb68`（2 A 路径 +1445/−0）→merge `59a7d61`（--no-ff 零冲突；主树门禁 1021[253.02s]/format 314[313+评审报告 md]/mypy 105——**S8 起基线=1021/105**）；bind_mcp_tools 生产 spawn+bind 接线 R5+ 移交记档（T-B-044 处方 5）。

### 2.7 R4-S8 退出证据收口

- **目标**：`doc/reference/r4-exit-evidence.md`（对齐 r1/r2/r3 模式：验收 A/B/C+验证行+L107 五项→file:line/测试/审查三方映射+S2-S7 收口+PHASE1-SLIM §4 L98-107 对照；**继承行号必须重锚**——r2 教训行#23 类不再犯）；验证行 L105 全量核对（最小 MCP server+故障注入+计数器/临时文件+进程边界）；函数级测试名枚举（pytest --collect-only 一次性，沿 R3-S8 先例）；OP-R4-* 遗留清单移交 R5+。
- **涉及接缝**：doc/reference/r4-exit-evidence.md（新）、tests 全量核对。
- **新增文件**：`doc/reference/r4-exit-evidence.md`。
- **TS 基准映射**：无新引用（收口）。
- **验收标准**：L98-107 全行对照+本方案 §0 逐句映射闭合。
- **测试计划**：全量 collect-only 枚举+门禁四件终值（含 CI 双 OS 绿 run id 记档）。
- **开放点**：无（收口）。
- **状态（#154 派发）**：已派发——T-C1-042 任务书入库（对齐 r1/r2/r3 先例形态：验收 A/B/C+验证行+L107 五项→file:line/测试/审查三方映射+S2-S7 收口+PHASE1-SLIM §4 L98-107 对照[**继承行号必须重锚——r2 教训**]+collect-only 1021 函数级枚举[附录 A，R3-S8 先例]+OP-R4-* 遗留清单移交 R5+[含 bind_mcp_tools 生产接线/src TS 注释清理评估/L131 后两句直载]；零代码变更票写面仅 2 新增；止损 60min；fresh session ses_orch_C1r4s8+WATCHER96——常设授权下自动派发）。

## 3. R4 台账（硬输入①②③处置+r3 在册残留再分流；硬输入④行#35 见 §0.1+本表行 7）

| # | 项 | 来源 | 处置（设行/维持登记/已闭合） |
|---|---|---|---|
| 1 | **r3 行#27 恢复三条款**：①孤儿 PENDING 清理（§2.1 L53 角落）②closed_outcome "restart" 词表零生产者=恢复面前瞻 ③`.opencode-fs-*.tmp` 崩溃遗留启动清扫 | T-B-028 m-1（B 处方）+T-C1-028 ④-1（r3 台账在册裁定"恢复切片任务书三条款"） | **设行兑现**：R4-S4 任务书三条款（§2.3，文件面+计数+验收口径粒度）——r3 裁定从"零代码登记"升级为恢复切片实体条款 |
| 2 | **`_drive_run` 吞没面**（service.py L1221-1239；invoker 改射后已降为纵深防御，恢复语义需明确终态策略） | T-B-036 硬输入③（任务书 L11） | **设行兑现**：R4-S4 核心票行条款（§2.3：恰 1 处 except Exception 计数+恢复不依赖内存账+双面验收口径+门禁预期） |
| 3 | r3 行#31 frozen-invoker 诊断后置（pre-execute 诊断经冻结 invoker 落 started 后 failed/unknown 而非 not_started——§2.1 L35 更精确终态不可表达，冻结面结构性属性） | T-B-031 必答1b（r3 在册；行#29 先例=重启条件前瞻记档模式） | **维持登记**：非缺陷零代码；R4-S4 触达 invoker/service 面时挂**评估条款**——是否获 pre-execute 诊断钩子承载面（not_started 精确终态+诊断直报），评估结论无论正负入进度④ |
| 4 | r3 行#28 nit-2（reason 常量双份=tools/policy.py 与 session/service.py 本地复制无 drift pin）/nit-3（revoke check-then-act assert 窗口，当前不可达） | T-B-028 nit-2/3（r3 在册；nit-1 已闭合 R3-S6） | **维持登记**：条件触发式——nit-2 挂 R4-S4 触达 service.py 关停/取消 reason 面时顺手加 drift pin；nit-3 挂"expires_at 非 None 生产者出现"条件（R4 各切片无该生产者，预期不动） |
| 5 | r3 行#30 六项登记级（T-B-030 nit-1 write/edit 措辞、nit-2/nit-3 进度披露精度+T-B-031 nit-1 rg stderr 披露、nit-2 timeout=0 区分度、nit-3 "Proves"列高估） | T-B-030/T-B-031（r3 在册） | **维持登记**：零独立票；R4 各切片触达 write/edit/grep/进度文件/对应测试文件时**顺手项条款**承载（任务书"顺手补记或强化"字句），不触达则顺延 R5+ |
| 6 | r3 行#26（nit-2 `test_grep_real_path_absence` 直接 `_RG_CACHE.clear()` 无恢复；nit-1 截断分支"确定性"措辞）——**编号歧义记档**：T-B-036 硬输入①称"row#26 恢复条款"，r3 在册恢复三条款实为**行#27**（行#26=rg 测试卫生 nit，verbatim 亲证 r3-slices §3 L153/L154）；本方案按内容吸收行#27 为台账行 1，歧义随回报上浮调度器 | T-B-029 nit-2/nit-1（r3 在册）+T-B-036 任务书 L9 | **维持登记**：R4 切片触达该测试文件时顺手 monkeypatch.setattr 整体替换（非必须）；歧义=记档项零代码 |
| 7 | r3 行#35（R4-S1b 伴生收尾批：grep TS 对齐+7 处 L277 批二+doc 修正批） | T-B-035 处方 1/2/3+调度器裁定（r3 台账"待派发"→已派发） | **已闭合随 #138（本行快照翻新随 #141）**：T-C1-034 23min/926/92+T-B-037 PASS 0/0/0/1+merge `c4e70ec`；r3-slices §3 行#35 已翻牌；S2 起基线=926/92 |
| 8 | r3 行#34（R4-S1 楔死 run 修复票） | T-C1-032 上浮（r3 台账已闭合） | **已闭合零动作**：仅作 S4/S7 先例引用（e2e 回归钉四面断言+unknown 终态三叠依据 runner.py L620-667+§2.1 L53+test ③） |

## 4. 关键设计差异备忘（Python vs TS，实现时勿"照抄"）

1. **恢复协调器无 TS 对应物**：TS 用 job recovery/inbox 模型，无"启动扫描将未结束 run 终止为 interrupted"语义；TS `file-mutation.ts:134` 自带 TODO 佐证崩溃恢复与幂等在 TS 亦未定义——契约 §7 L333 为 Python 专有裁决（绝不自动续跑遗留 accepted），实现以契约为唯一基准，勿在 TS 找蓝本。
2. **MCP 客户端栈形态**：TS=@modelcontextprotocol/client SDK+Effect Scope/ChildProcess（stdio.ts 经 Environment spawner）；Python 载体=OP-R4-1（SDK vs 自研最小 JSON-RPC）；"scope 持有进程=close 即杀进程组"的所有权语义按 §6 L304 归 manager，Python 侧等价物=app 生命周期+受控子进程环境（R3-S5 process/winjob.py 先例）。
3. **MCP 重连语义不移植**：TS index.ts L355-379 recover/recovering（SessionExpired→重连→重跑一次）为 HTTP 会话设计；Python R4 仅 stdio——断线=unknown+最多重建连接供下一次显式调用（L304），不自动重试副作用（§8 L354）。
4. **MCP catalog 热更新不移植**：TS watch onToolsChanged 自动刷新；Python=L304 末句（tools/list 改变先建新 catalog）+L302 "Phase 1 无热重载"——catalog 变更走（重）连接时重建，不做主动 watch。
5. **CLI 会话选择**：TS mini.ts L201/run.ts L73 三件 `--continue/--session/--fork`；Python=L107 仅两件，**不移植 --fork**（Phase 1 无会话分叉面）。
6. **follow 语义**：TS bus.ts readAfter（L786/860）跳过未知类型并推进 cursor；Python 契约 §4 L246 明文不沿用——EventLog.follow 以 L237-246 为唯一基准（wakeup→H→补读→Synced→seq 严格递增→at-least-once 去重）。
7. **审批 UI**：TS modal/session-ui 面板；Python=CLI REPL 独立控制通道（§7 L316，R3-S2 先例延续）。

## 5. 风险表（R4 执行期识别+既有残留）

| 风险 | 面 | 缓解/证据要求 |
|---|---|---|
| MCP server 子进程泄漏（验收 A"生命周期退出后无子进程泄漏"） | S2 | Job Object/process group 双平台（R3-S5 winjob.py 先例复用）；close/崩溃双路测试+子进程退出断言 |
| MCP SDK 依赖引入与版本漂移 | S2 | OP-R4-1 裁决（pyproject/uv.lock 写权归调度器——C 席零触碰，需依赖即停下上浮）；L105 进程边界测试约束 |
| snapshot v3 wire 变更破坏旧 fixture | S3 | §8 L356 最低门槛：旧 v1/v2 fixture 可读+确定性 upcaster（行#24 idiom：双版本注册+旧版本模型保留）+canonical hash 漂移检测测试 |
| 恢复扫描误写坏会话（诊断 session 追加业务事件违 §7 L339） | S4 | 只对可写 session 补终态；read_only_diagnostic 只清理资源；双分支测试钉死 |
| 恢复与并发启动竞态 | S4 | db.py L43 数据目录锁 App 开启期持有=单实例假设在册；协调器在该锁内运行 |
| 跨进程 kill 无注入缝（三窗测试） | S4/S7 | 子进程自杀/monkeypatch commit 中断承载；协议不可达分支按④如实披露（T-C1-028 ④-1 先例） |
| follow/慢消费者测试时序脆弱 | S5 | 受控注入（队列上限/时钟假人）优先于 sleep 竞态；测试级超时断言 |
| MCP 常量移植水土不服（execution 12h 等） | S2 | OP-R4-5 保守裁量；契约只锁语义（超时存在+断线=unknown）不定值 |
| evidence 行号继承漂移 | S8 | r2/r3 教训（行#23 类）：继承行号必须重锚+file:line 亲证+--collect-only 枚举 |

## 6. 开放问题（不预写裁决，留 A/调度器）

- **OP-R4-1**：MCP 客户端实现载体（官方 `mcp` SDK vs 自研最小 JSON-RPC stdio client）——依赖引入方式归调度器（pyproject/uv.lock 零触碰红线）；L105"MCP SDK 集成测在进程边界运行"为依据；TS 锚=@modelcontextprotocol/client（stdio.ts/client.ts）。
- **OP-R4-2**：MCP 启动指纹 canonical 形态（server 名+command/argv+env refs 摘要的哈希构成）——§5.2 L268 要求绑定指纹但未定形态；A 席裁量。
- **OP-R4-3**：`--continue` 最近会话判定口径（快照 working_dir 字符串全等 vs 规范化/前缀）——保守=全等。
- **OP-R4-4**：慢消费者覆盖测试形态（真队列上限注入 vs 合成 SlowConsumerError）——保守=受控上限注入。
- **OP-R4-5**：TS MCP 超时/生命周期常量 Python 取值（startup 30s/catalog 30s/execution 12h/attached 10m/terminate 1s/CLOSE_GRACE 2s/FORCE_KILL_AFTER 2s/MAX_FRAME_BYTES 16MiB/EXIT_WAIT 1s/STDERR_TAIL 1000）——契约只锁语义不定值，取值保守裁量。
- **OP-R4-6**：恢复协调器触发点与范围（App 启动同步全量扫描 vs CLI 命令前置惰性）——保守=App 启动全量；`session_status` L313"不触发恢复"红线不动。

## 7. 硬性输入①-⑤对照自检清单

| 输入 | 落点 | 状态 |
|---|---|---|
| ① row#26 恢复条款（r3-slices 在册） | **编号歧义记档**：r3 在册恢复三条款=行#27（行#26=rg 测试卫生 nit，r3-slices §3 L153/L154 verbatim 亲证）；按内容吸收=§3 行 1+§2.3 row#27 ①②③ 三条款（文件面+计数+验收口径）；歧义随回报上浮 | ✓ |
| ② row#27/#31/#30/#28 残留再分流建议 | §3 台账：行 1 设行（#27→S4 三条款）/行 3 维持登记（#31+S4 评估条款）/行 4 维持登记（#28 nit-2/nit-3 条件触发）/行 5 维持登记（#30 顺手项条款）/行 6 维持登记（#26 顺手+歧义记档） | ✓ |
| ③ `_drive_run` 吞没面=R4 恢复核心票 | §2.3 行条款：service.py L1221-1239 文件面+恰 1 处 except Exception（L1236-1237）计数+双面验收口径（进程内零回归/跨进程按 durable 日志终态）+门禁预期 926+N/92+N/双 ruff | ✓ |
| ④ 行#35（R4-S1b）在飞 | §0.1+§3 行 7：已派发收口中登记**不重复设行**（T-C1-034 执行中；预期 926/92；落地后调度器更新 r3 台账） | ✓ |
| ⑤ 基线口径 | 头部+§0.1：pytest 924（→926 随行#35）/mypy 92 files 零问题/双 ruff 绿；CI 双 OS 绿先例 41952806310（#132），本轮 41963085214 在跑；首切片基线随行#35 收口标定 | ✓ |

## 8. 每切片任务书模板（供调度器直接取用派发 C 席；对齐 T-C1-024/T-C1-033 风格）

```markdown
# T-C1-0XX · 席位C-1：R4-SX <切片标题>

【角色章程·C 实现者】（三角色流水线：A 仲裁者 / B 规划评审 / C 实现者；本会话由调度器驱动，你是席位C-1。以契约文本+切片方案（doc/plans/r4-slices.md §2.X）为唯一基准；**禁止 git 写操作**（提交唯一归调度器）；门禁自跑后回报数字；④ 强制浮报所有未测/协议不可达防御分支；⑤ 存量兼容回归零破坏）
- 工作目录：worktree `C:/Users/lyu/.local/share/opencode/worktree/<id>/r4-sX`（分支 r4-sX，基于 main 检查点#NN——worktree HEAD 即基线，`git log -1` 自证）。
- 写权限（本任务显式授权，仅此）：<文件清单——逐文件列出，含"新增"/"修改"与红线声明（src 冻结面/契约文档/pyproject/uv.lock/CI/source-baseline.json/ORCHESTRATION 零触碰；**MCP 依赖如需 pyproject/uv.lock 触碰=停下上浮，写权归调度器（OP-R4-1）**）>。**其余一切冻结**；e2e/测试若暴露 src 缺陷→停下浮报不修，修复归调度器另行授权；fixture 只读复用（跨模块导入或同形态新建，不改既有文件）。
- 门禁（worktree 内自跑，python -m 形式）：先导出专属 temproot（已预建）：`$env:PYTEST_DEBUG_TEMPROOT=$env:TEMP+'/opencode/pytest-tmp/r4-sX'`；四件：`uv run python -m pytest -q`（基线 926+N 零回归+新增如实计数——逐测落点入进度文件；若派发时行#35 未并回则基线按 924 顺延，以任务书标定值为准）；`uv run ruff check .`；`uv run ruff format --check .`（计数非验收标准，退出码 0 即过）；`uv run python -m mypy src tools/r0 tests/r0 tests/contract tests/integration`（基线 92 files+N 零问题）。
- 止损：NNmin，到线即停——已完成部分+剩余清单写进度文件，回报"部分完成"；超时如实记录不粉饰。防截断：进度文件增量写入；最终回报 1-2 段。
- TS 锚纪律：本切片引用 TS 文件须已在 source-baseline.json 指纹内——**R4 新引用五件（mcp/{stdio,client,index}.ts、cli/src/mini.ts、schema/src/config/mcp.ts）须调度器先补指纹后方可引**；run/run.ts 已录（L38）。（**勘误 #150**：五件均已入册[58 条指纹，mini.ts=ab34cd… 亲证哈希相符]——本句为 S3 时点文案，留档不改写。）不宣称与 TS 同构；差异按 r4-slices §4 备忘保守读法（恢复协调器无 TS 蓝本=差异表 1）。
- 开放点纪律：r4-slices §6 所列 OP-R4-* **不预写裁决**——实现触及时按保守读法落地+进度④浮报，越出即停下待裁。
- 顺手项纪律：r4-slices §3 行 3-6 维持登记项若本切片触达对应文件，按台账处置列顺手补记/强化并在进度⑤记档；不触达则不顺延不改道。

【任务】<逐项：spec 引用（r4-slices §2.X 目标/验收句原文）+最低落测结构+每测落点要求（测名↔验收句映射入进度文件）>

【④ 强制浮报】协议不可达/未测防御/已测防御/注记四档（R2 定案模式）；注入基建沿用须携注（R2-S7c n-3 记档）；跨进程 kill 无注入缝类按 T-C1-028 ④-1 先例如实披露。

【⑤ 存量兼容】全量基线零回归红线；src 冻结面零触碰（本切片授权清单外，含 service.py 仅限 r4-slices §2.3 行条款红线面）；既有测试文件零触碰；新增仅授权清单。

【进度文件】`doc/orchestration/progress/T-C1-0XX.md`：①方案审视发现（选型论证）②门禁四件数字+新增测试逐测落点 ③强制浮报清单 ④未决问题 ⑤存量兼容声明。

【回报格式（回复正文，1-2 段）】完成度 / 门禁四件数字（含新增测数）/ 落点概要 / ③④ 概要 / 是否有需调度器裁决项。
```
