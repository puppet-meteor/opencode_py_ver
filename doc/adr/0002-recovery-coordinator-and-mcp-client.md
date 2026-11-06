# ADR-0002：启动恢复协调器与自研 MCP stdio 客户端

> 日期：2026-11-07。状态：已采纳——两大决策均随 R4 切片交付（S2/S4/S6），并经 R5 收官全量验证（pytest 1134/mypy 109/双 ruff 绿，证据=[r5-exit-evidence §8](../reference/r5-exit-evidence.md)）。
> 契约：[CORE-CONTRACTS](../CORE-CONTRACTS.md)；前卷：[ADR-0001](0001-core-architecture.md)。本卷按 0001「后果」末条（L43：修改核心 schema/权限/恢复含义的后续决策，新增 ADR 引用并 supersede 相关段落，保留历史）立卷：决策 A 对 0001 决策 6 的恢复语义增量负责——supersede 为「由启动恢复协调器在组合根启动期独占实现」的具体条款，语义本身不变；决策 B 增量承接 0001 决策 8（与 TS 比较行为、不提供 wire 兼容）与决策 9（MCP 不声称为沙箱）的立场，钉定客户端载体=自研。0001 原文保留。

## 背景

R4 交付 MCP 与故障恢复，把 0001 的两条抽象承诺逼成具体决策。其一，0001 决策 6 立下的恢复终态（pending→cancelled/not_started；已 started 无 finished 才是 unknown，绝不自动重跑）需要明确的启动期承载者与触发时机（开放点 OP-R4-6）。其二，MCP stdio 客户端面临载体选择，依赖引入归调度器，用户裁决为自研（开放点 OP-R4-1，2026-11-05）。契约基线：CORE-CONTRACTS §3.3 恢复配方与副作用三窗、§3.2.1 查询不触发恢复、§7 session_status 红线与绝不自动续跑、§6 MCP 信任条款。

## 决策 A：启动恢复协调器

### A.1 背景与约束

契约 §3.3（L212）：运行恢复协调器获得独占运行权且会话通过完整性验证后，按已提交 py.step.generated 的完整计划补写——先追加待审批 py.permission.closed，再补工具终态、含全部结果的 interrupted py.step.finished 与 py.run.finished；同一恢复批次使用稳定 ID，重复启动幂等。副作用三窗（L218-221）：tool.started 提交前退出按未开始处理；tool.started 后、tool.finished 前退出标 unknown；tool.finished 已提交直接读结果，绝不重跑。§3.2.1（L202）查询不重放坏日志、不触发恢复或外部调用；§7（L313）session_status 不触发恢复，L333 启动恢复将未结束 run 终止为 interrupted、绝不自动续跑。开放点 OP-R4-6 保守缺省=App 启动同步全量扫描（非 CLI 命令前置惰性），L313 红线不动。

### A.2 决策内容

1. **启动单扫**（OP-R4-6）：组合根构造期绑定一次全量扫描——`app.py` `_startup_scan`（L74）先做崩溃临时文件清扫、再跑恢复扫描并合并报告；`RecoveryGate.bind` 在组合根接线（app.py L189），事件循环内构造即 eager 调度（recovery.py L218-226，任务名 `opencode-startup-recovery`）。恢复完成前组合根写面不可用，是 §3.3 L212 独占运行权的结构性实现。
2. **写门非读门**：`_RecoveryGatedStore`（app.py L87）继承 SessionStore，共享 inner 的单一 TransactionWriter（不二开 writer/锁集，L101-103），恰覆写三个写面 `commit`/`rebuild`/`cleanup_failed`（L105/L109/L113）——先 `await gate.wait()` 再原样委托；读面不过门（§3.2.1 L202、§7 L313：读走 ReadRepositories，零门引用）；恢复协调器自身经未加门的 inner store 提交（app.py L177 EventCommitter，与 runner 同一提交缝：稳定事件 ID+整批 ConflictError 有界重试）。
3. **扫描失败 fail-closed**：`RecoveryGate.wait`（recovery.py L228-235）对每个门调用者 re-raise 扫描异常——组合根保持写不可用，绝不带病放行（test_recovery.py::test_gate_blocks_writes_until_the_scan_completes_and_fails_closed，L904）。
4. **保守 unknown 语义**：`RecoveryCoordinator.scan`（recovery.py L268）以 durable 日志为唯一输入，只输出终态事件、绝不续跑（模块 docstring L11-12）；非 active 会话（read_only_diagnostic/closed/cleanup_failed）扫描计数但绝不写（L24-27）；完整性失败（A≠H、不可解码、非法折叠）跳过该会话不写、不修复读模型（L27-30）。逐窗测试：test_recovery.py L389/L416/L444/L486。
5. **崩溃临时文件清扫**（r3 在册移交项行#27③）：`sweep_crash_temp_files`（recovery.py L163）在声明 roots 下移除 write 工具原子替换的 `.opencode-fs-*.tmp` 崩溃遗留——组合根运营面而非会话服务层（r3 裁定）；清扫失败上报不 raise、不阻扫描（app.py L79-80；test_recovery.py L952/L981）。
6. **会话选择接入**（R4-S6）：`--continue`/`--session` 走独立只读选择窗（cli.py `_select_session_async` L458，自持循环与只读 DB 句柄、App 存在前闭合）→ 创建行按版本路由模型（`_model_created_data` L434，未知版本=选择停住而非就近强制转换）→ recover-verify（`_select_session` L488：`loader.recover` L511 以记录快照为行为基线；工作目录匹配=记录字符串精确全等[OP-R4-3]；v1 快照、工作目录消失、v3 MCP 指纹漂移=停住，单行 stderr+exit 4，不静默换一套权限）。会话驱动前 `await app.recovery_gate.wait()`（cli.py L844）。

实现锚：src/opencode/session/recovery.py（670L：RecoveryCoordinator L238、scan L268、_recover_session L314、RecoveryGate L194、RecoveryReport L148、SessionRecoveryReport L126、sweep_crash_temp_files L163）；src/opencode/app.py（282L：_startup_scan L74、_RecoveryGatedStore L87、接线 L161-190、recovery_gate property L259）；src/opencode/cli.py（1147L：选择面 L404-514）。

测试锚：tests/integration/test_recovery.py（14 测：三窗 L389/L416/L444/L486、停住面 L657/L674、v1 可恢复 L742、非 active 不写 L768、完整性跳过 L788、进程内/跨进程崩溃 L846/L878、门 fail-closed L904、清扫 L952/L981）；tests/integration/test_cli_r4.py（选择面 L362-L587 九测，含 test_continue_stops_on_a_v1_snapshot L468、test_continue_stops_on_mcp_fingerprint_drift L488、test_session_stops_when_the_recorded_working_dir_is_gone L527）。

### A.3 备选方案和取舍

| 方案 | 取舍 |
|---|---|
| CLI 命令前置惰性扫描 | 首用时才扫、冷启动快；但留出未扫先写窗口，「恢复完成前组合根不可写」不变式不成立——OP-R4-6 保守缺省弃用 |
| 读写皆过门 | 表面更稳；违反 §3.2.1 查询不触发恢复与 §7 L313 session_status 红线，诊断可用性受损——只门写面 |
| 恢复走第二写入路径 | 绕开唯一提交缝破坏 seq 连续与审计；恢复提交共享 EventCommitter 同一 commit 缝（稳定 ID+整批重试） |
| 自动重跑/续跑未结束 run | 外部副作用不可判定（§3.3 L216-221），可能重复写删；保留 unknown 终态，由宿主重新决定（沿 0001 决策 6） |
| 给损坏会话补写修复事件 | §3.2.1 禁止诊断期追加 interrupted/unknown「修复」事件；integrity_failure 跳过留显式诊断 |
| 清扫交会话服务层 | 越层且时点错误（崩溃遗留=宿主运营面）；随启动扫描同遍承载于组合根 |

### A.4 后果（含已知边界与未覆盖面）

- 启动先扫全量 durable 状态才可写：比无恢复 demo 慢一次全量折叠，换取崩溃后终态闭合与审计完整。
- 报告面诚实计数：RecoveryReport（recovery.py L148）按会话 outcome 分桶（recovered/clean/halted/skipped/integrity_failure，SessionRecoveryReport L126-135）+tmp 清扫计数，宿主可见每个「拒不恢复」的原因。
- 已知边界（如实记档）：credentials-reference 停住面在任何快照版本都无生产者（协议不可达，recovery.py L18-19）；v1 快照「无 runtime 面」的停住属 ConfigLoader.recover 单独所有、v1 终态卫生仍在此恢复（L20-22）；无事件循环构造时扫描推迟到首个门写——每个门写面皆 async=循环必在，无写先于扫描完成的不变式完整（L197-200）；CLI 组合现不声明 MCP 面（current_mcp_servers=None 跳过 v3 指纹比较，L244-247 ④ 披露）。
- 未覆盖面（platform-support §9 逐件引注）：故障矩阵「新 catalog/boot_id 使旧批准失效」两句未 e2e 直载（§9 B3——间接承载在册：catalog/指纹 re-ask+boot_id 隔离+断线 unknown）；会话选择漂移停止行的消息内容未断言（§9 D2）；POSIX 腿进程组语义=CI 承载非本机实测（§9 A1）。

## 决策 B：自研最小 JSON-RPC stdio MCP 客户端

### B.1 背景与约束

契约 §6（L304）：MCP 只支持显式用户配置的 stdio server，argv 直接 exec、不拼 shell，环境变量为最小集和显式 secret refs，启动前宿主确认该 server 的完整本机权限；SDK manager 拥有子进程、会话、退出栈和重连——调用超时/断线按 unknown 处理，不自动重试副作用，最多重建连接供下一次显式调用；远端 tools/list 改变后先建新 catalog，不能用旧批准调用改名工具。§5.2（L268）MCP 权限绑定 server 启动指纹和 schema/catalog 版本。开放点 OP-R4-1：官方 mcp SDK vs 自研最小 JSON-RPC stdio 客户端——依赖引入（pyproject/uv.lock 写权）归调度器；用户裁决 2026-11-05=自研：零新依赖红线、行为对齐而非依赖对齐、owned 代码、未覆盖协议面如实披露（T-C1-035 记录；mcp/__init__.py L3-6 docstring 同锚）。

### B.2 决策内容

1. **自研最小 JSON-RPC stdio 客户端，零新依赖**：src/opencode/mcp/ 四件——stdio.py（547L：StdioTransport L105、异常族 McpTransportError 及五子类 L72-92、帧/关闭常量 MAX_FRAME_BYTES、CLOSE_GRACE_S、FORCE_KILL_AFTER_S、STDERR_TAIL_CHARS 等）、manager.py（378L：McpManager L137、StdioServerDefinition L60、远端工具与内容模型 L77-104、McpConnectError/McpUnknownOutcomeError L112/L116、超时三分常量 startup/catalog/execution）、bridge.py（94L：MCPToolAdapter L68、to_tool_result L26——text/structured 回填 ToolResult）、__init__.py（68L：门面导出；命名空间映射/目录绑定/权限接线=S3 面刻意不在包内）。pyproject/uv.lock 零触碰。
2. **行为对齐而非依赖对齐**：wire 帧序 initialize→notifications/initialized→tools/list→tools/call、超时语义、错误面以 TS 可观测行为对拍（对拍记录=T-C1-035 进度/评审件；parity.md MCP stdio 行 verified 在册），不宣称结构同构；TS 常量取值=Python 侧保守裁量+docstring 注记（OP-R4-5）。
3. **生命周期与进程树纪律**：manager 每 server 生命周期——add_server L168 注册不 spawn、start_server L182（spawn+握手+tools/list）、call_tool L197、close L229；close 优雅关闭升级强杀（stdio.py close L181→_terminate_tree L507 进程组 SIGTERM→宽限→_kill_tree_hard L528 SIGKILL；POSIX start_new_session 使 killpg 收整树 L24、L521/L542；Windows=Job Object 承载，R3-S5 winjob 复用 L482）；#145 热修：server 先死（死组长）时对仍驻留的进程组即时 killpg 清扫（stdio.py L221-229；test_mcp_stdio.py::test_crash_sweeps_tree L410 钉住）——POSIX 孤儿面。
4. **断线/超时=unknown**：McpUnknownOutcomeError（manager.py L116）；不自动重试副作用，最多重建连接供下一次显式调用（test_mcp_stdio.py::test_no_auto_retry_rebuild_only_on_next_explicit_call L368、test_exit_during_call_unknown_with_stderr_tail L349）；stderr tail 捕获辅助诊断（stdio.py stderr_tail L288）。
5. **权限绑定面**：policy.py `bind_mcp_tools`（L658，§5.2 L268）——saved 决策绑定 server 启动指纹+catalog，绑定变化即停激活、下次调用重新 ask；fail-closed 形状检查（action 必须 `mcp__` wire 名、绑定字段非空，L667-674）。
6. **现态接线（OP-R5-1 关联面，只记现态零预写）**：生产 CLI 组合注册 MCP 定义但不 spawn（cli.py `_compose_mcp_manager` L517-530——add_server 从不 spawn，零副作用组合，App 拥有 close、service 按序回收[§7 L339：MCP→provider→DB，app.py L237-241]）；run 级 MCP 路由由组合根测试直组 MCPToolAdapter 承载（test_cli_r4.py::test_mcp_composition_registers_definitions_without_spawning_and_closes L666）——T8 既定 #152 调度器裁决；生产 spawn+bind 接线的立项评估在 R5 台账在册（doc/plans/r5-slices.md §3 行 1，OP-R5-1），归调度器定夺，本卷不预写方案。

测试锚：tests/integration/test_mcp_stdio.py（13 测，真子进程 fixture tests/integration/mcp_stdio_server.py 九模式：roundtrip L190、bridge 面 L226、adapter 经 manager 执行 L264、startup 超时 L301、catalog 超时 L316、tools/list 错误 L328、调用中退出 unknown+stderr tail L349、不自动重试 L368、close 升级杀树 L394、崩溃清扫树 L410、帧溢出 L430、close 链 MCP 先于 provider L458/L494）；tests/contract/test_mcp_naming.py（44）+tests/integration/test_mcp_catalog.py（3）+tests/integration/test_e2e_r4.py（7）（计数=parity.md §2.1 MCP stdio 行引注口径）。

### B.3 备选方案和取舍

| 方案 | 取舍 |
|---|---|
| 官方 mcp SDK 依赖引入 | 生态面即得；但违反零新依赖红线、引入版本漂移与供给面，owned 代码要求落空——OP-R4-1 用户裁决=自研 |
| 依赖对齐（复刻 SDK 结构） | 结构同构不保证行为一致且拖入无关面；改为最小 owned 子集+TS 可观测行为对拍 |
| 断线自动重试副作用 | 体验顺；外部效果不可判定，违反 §6 L304 unknown 语义——最多重建连接供下一次显式调用 |
| shell 拼接启动命令 | 注入面；argv 直接 exec（§6 L304） |
| 继承宿主全环境变量 | 泄漏宿主环境；最小集+显式声明键（宿主导出值优先于文件值，cli.py L522-524），不 merge 宿主环境 |
| 现在实现 resources/prompts/HTTP/remote 面 | 超出 R4 范围且无需求牵引；未覆盖协议面如实披露、未来 MCP 面再裁决（OP-R4-1 披露口径；HTTP MCP=M6 能力卡片在册） |

### B.4 后果（含已知边界与未覆盖面）

- owned 全栈：协议面子集自有维护，协议版本升级是显式工程而非依赖跟随；帧上限、stderr tail、升级强杀等运维面自有裁量（OP-R4-5 docstring 注记在册）。
- 未覆盖协议面（13 面值，platform-support §9 B1 实枚举）：cancellation/progress/ping/sampling/elicitation/roots/resources/prompts/tools·list 分页/completion/logging/setLevel/协议版本协商——不实现不留死代码；server→client 请求应 -32601、notification 忽略。
- 承载边界：POSIX process-group/killpg 树杀腿+close 升级序 POSIX 分支=CI 复验承载非本机实测（§9 A1）；Windows 无 SIGTERM，close 升级序中间态不可稳定注入（R4-S2 评审在案）。
- 现态生产面：生产 CLI 注册不 spawn（§9 B2）；bind_mcp_tools 生产 spawn+bind 接线=OP-R5-1（归调度器）；未来 HTTP MCP=M6 能力卡片（PHASE2-MODULES 在册，届时再裁决）。
- 环境语义：环境变量=最小集+显式声明键，不 merge 宿主环境（parity.md MCP stdio 行在册）；win32 最小集（T-B-038 ⑥②，cli.py L525）。
