# R3 实现切片拆分

> 修订：[T-B-026] 2026-11-04 首版产出（R2 收官 main **#99 `c87f317`** 后）；硬性输入①-⑧（T-B-026 任务书）逐条对照见 §7 自检清单。
> 席位B（T-B-026）产出，供 C 席按序实现。状态：**S1 起待派**（R2 全交付态+checkpoint 链见 doc/plans/r2-slices.md 头部）。
> 依据：doc/PHASE1-SLIM.md §4（L87-96 R3 全量）、doc/CORE-CONTRACTS.md（§2.1 L35、§3.1 L140/L183、§3.2 L160/L185、§3.2.1 L195-204、§5.1 L257-261、§5.2 L266/L268、§5.3 L272-276、§7 L173/L316/L335/L337、§8 L348/L358、§9 L368、§10 L404/L414/L424）、doc/plans/r2-slices.md（头部+§0.1 衔接+L113 read 先例+L181 OP-9）、doc/reference/r2-exit-evidence.md（R2 收官基线）、doc/reviews/ R2 全部评审（R2-S2/S4/S5/S6/S7/S7-closeout/S7c/S8/S8-closeout 增项源）、doc/reestimate-r0.md L49/L62、doc/orchestration/progress/*（③未决源）。
> TS 基准：`D:/26_code/opencode_py_ver/opencode-2.2.2/`（指纹 doc/source-baseline.json 现锁 **51 文件**=20 R1+19 R2+12 R3 前置补录，含 tool-runtime/read/cli/schema 链）。**R3 新引用 12 文件指纹已随检查点#102 补录完成**（调度器亲算 SHA256 逐文件录入，r2-slices 前置项模式；对 B 原 11 文件清单的核对修正：+`tool/plugin/file-diff.ts`（§2.4 已引用而清单漏列）、−`config/discovery.ts`（R1 既有指纹））：`packages/core/src/tool/plugin/{write,edit,glob,grep,shell,file-diff}.ts`、`packages/core/src/permission/{saved,sql}.ts`、`packages/core/src/shell/{select,scan,result}.ts`、`packages/core/src/ripgrep/binary.ts`。
> 拆分基准：按现行文档拆；A 席若修订 R3 定义，由调度器对齐后本文件再改版。

## 0. R3 范围锚定（摘自 PHASE1-SLIM §4 L87-96）

- 交付（L89）：其余六个工具、Policy/审批/保存决定、ConfigLoader/快照、受控子进程环境。
- 验收 A（L91 原文）："write/edit 精确冲突检测、原子替换；glob/grep/ls 限制数量；路径越界/超大/二进制文件有诊断；明确 rg 为外部依赖并检查可用性。"
- 验收 B（L92 原文）："allow/deny/ask、once/always/reject 和审批到期；approve 在独立控制通道处理，不被 active run 锁死；无人审批时拒绝执行。审批与 cancel/expiry 通过条件更新竞争，首个合法提交者胜出，迟到回复返回 ApprovalExpired/Conflict。"
- 验收 C（L93 原文）："shell 超时/取消收回进程树、输出限额不导致管道死锁；项目配置不能扩宿主 roots/凭据/MCP 信任；重启后按原快照恢复。"
- 验证（L94 原文）："每个工具的真实临时 workspace 集成测试 + Policy 表驱动测试 + Windows/Linux 路径/子进程测试。"
- 约束（L96 原文）："权限 saved 决定使用 R1 已存在的 permissions 投影，经同一事件路径写入；不新增独立 JSON 权限文件。命令 deny 清单只能辅助提示，不是 shell 沙箱。"

### 0.1 R2 交付面衔接（现状基线 #99 `c87f317`：651 passed = contract+integration+r0；mypy 66 files；双 ruff 绿）

- **permissions 投影已建表未接线**：0001 迁移已建 `permissions` 表（0001 L32-38，r2-slices §0.1 L22 锚）；投影语义=§3.1 L183（待审批和保存决定的投影；input_hash、作用域、有效期、终态）；§3.2 L160 commit 序已含"同一 projector 更新 …permissions"。R3 = fold 扩展+projector 写入激活。
- **py.permission.* 事件未注册**：r2-slices §0.1 L21 定案"py.permission.* 与 py.permission.saved/revoked/expired 归 R3；不注册不投影"。R3 = 注册（封闭 wire §8 L358：versioned_type+显式 upcaster，不走同版本新增可选字段）+投影接线。型名以 §3.1 L140 事件表为准（契约已出现 py.permission.replied/closed——§5.2 L268、§7 L337），C 席对照契约落定，本方案不预写全集。
- **六工具现状=仅 read**：`tools/builtin/read.py`（fs 命名空间/working_dir 单根/串行/invoke 链/量化偏差 float-reported 声明先例——§5.3 L276 限额、1 MiB 窗/64 KiB 输出）。§5.2 L266 明文"根内 read/glob/grep/ls allow"——R2 只落 read，R3 落 ls/glob/grep（allow 面）+write/edit/bash（审批面）。
- **runner 现状=串行 invoke**：§5.1 L257/L259-261 时序（requested→catalog/schema 校验→policy/审批→started 提交确认→adapter 执行→结果限额/归一化→finished）——**R3 六工具全部经 invoke，串行不变量延续**（PHASE1-SLIM L85 约束 R3 沿用）；policy/审批挂点=L259"成功再走 policy/审批"。
- **CLI 现状=stdlib-only**（#92 OP-5）：参数面明示为 R3 ConfigLoader 注入点（cli.py docstring）；审批控制通道 CLI 化归 S7。
- **错误码已注册待实现**：ApprovalUnavailable/ApprovalExpired/PermissionDenied/ToolTimeout/ToolFailed/ExecutionUnknown 均在 §8 L348 注册表——R3 是消费面实现，不新增注册。
- **R2 评审增项**：见 §3 台账（23 项）。

## 1. 切片序列总表

| ID | 一句话目标 | 接缝 | 前置 | C 席预计 |
|---|---|---|---|---|
| R3-S1 | py.permission.* 注册+permissions 投影接线+工具状态 denied/cancelled 面+注册表单源化 | contracts/events、session/state、session/projector、（如需 migrations 0003） | R2 收官态 | 45-60min |
| R3-S2 | Policy 引擎（allow/deny/ask）+审批协议（once/always/到期/独立通道/竞态）+saved 决定经同一事件路径 | tools/policy 新、tools/invoker（挂点）、session/service（reply_permission/expiry） | S1 | 75-90min |
| R3-S3 | ls/glob/grep 三件+数量限制+rg 外部依赖可用性检查 | tools/builtin | S1（可与 S2 并行） | 60-75min |
| R3-S4 | write/edit：精确冲突检测+原子替换+根边界 | tools/builtin | S2+S3 | 45-60min |
| R3-S5 | bash+受控子进程环境：显式解释器/超时取消收树/输出限额无死锁/deny 辅助提示 | tools/builtin+子进程环境 | S2 | 75-90min |
| R3-S6 | ConfigLoader/快照：config 声明 roots+不扩宿主信任+重启按快照恢复+CLI 参数面注入 | config 新、cli 注入面 | S1-S5 | 45-60min |
| R3-S7 | CLI 审批面+R3 增项收口（P-1 真 SIGINT 双子场景必做等） | cli | S2+S6 | 60-75min |
| R3-S8 | 全链 e2e+R3 退出证据收口（r3-exit-evidence.md） | tests+doc | S1-S7 | 60-75min |

**依赖序**：S1→S2→{S3→S4、S5}→S6→S7→S8。S3 仅依赖 S1（事件/投影+registry 面；ls/glob/grep 走根内 allow 不依赖审批协议），**可与 S2 并行派发**；S4/S5 依赖 S2（审批挂点）；S6 可在 S5 后提前启动；S7 依赖 S2（审批 CLI 面）与 S6（参数面替换）；S8 收口全量。TS 指纹补录（12 文件）已随检查点#102 一次性完成（调度器核对 B 清单：+file-diff.ts、−discovery.ts 系 R1 既有）。

## 2. 每切片详情

### 2.1 R3-S1 契约与权限投影接线

- **目标**：`contracts/events.py` 注册 py.permission.* 家族（durable、封闭 wire：versioned_type+canonical_json+显式 upcaster，§8 L358；旧读者遇新版明确失败）；`session/state.py` fold 扩展权限态（§2.1 L35 工具状态机补面：pending→denied（审批拒）/cancelled（审批过期/重启），pending 直接终态必 `effect_status=not_started`，终态只有一次）；`session/projector.py` 写 permissions 表（激活 0001 L32-38；投影字段=§3.1 L183 input_hash/作用域/有效期/终态）；**注册表单源化（R2-S2 n-1 必做）**：`projector._REPLAY_REGISTRY = default_event_registry()` import 期冻结改为注入/惰性取（与 store 同源），防应用侧注册的 durable 型"txn 插入侧通过、projector 重放侧报 stored_event_decode_failed"双注册表漂移——R3 正是触发点。
- **涉及接缝**：contracts/events.py、session/state.py、session/projector.py、storage/txn.py（L160 commit 序不动，仅注册表同源）、storage/migrations（仅当 0001 permissions 列集不足时新增 0003 追加 revision——§3.2 L185"只追加"纪律，不改写 0001/0002）。
- **新增文件**：`tests/contract/test_permission_events.py`（+JSON fixtures 正反例）、`tests/integration/test_permission_projector.py`。
- **TS 基准映射**：`packages/schema/src/session-event.ts`（Permission* 事件型，L39-57 Created 邻域参照；已锁指纹）、`packages/core/src/permission/sql.ts`+`saved.ts`（140L：Info/AddInput/UpdateInput/ConflictError/isUniqueViolation——保存决定字段面参照；**已录指纹（#102）**）。
- **验收标准**：py.permission.* 注册+versioned_type 齐；投影四字段（input_hash/作用域/有效期/终态）落列；坏形 fail-closed（沿 §3.2.1 StorageCorrupt 分类，不塞半行）；重放=日志确定性函数（连续 rebuild 全等）；诊断 session 拒绝权限写入（§3.2.1 L195-204 只读纪律）。
- **测试计划**：契约 fixture 往返+未知版本/坏形拒绝；投影/重放等价（含冷暖重放）；denied/cancelled 终态一次性；0003 迁移旧库升级+新库建库双路（若触发 OP-R3-8）。
- **开放点（保守读法）**：OP-R3-8（permissions 表列集对齐——保守=优先复用 0001，不足才 0003 追加）；事件家族型名全集以 §3.1 L140 表为准不预写。

### 2.2 R3-S2 Policy 引擎+审批协议

- **目标**：`tools/policy.py`（或 policy 包）——allow/deny/ask 评估（§5.2 L266：根内 read/glob/grep/ls allow 先例延续；write/edit/bash 档位见 OP-R3-5）；saved 决定：once=单 permission_id+input_hash、always=当前进程本 session 的可展示具体 action/resource 规则、**禁止无范围通配**，shell 规则绑定完整 command/cwd（§5.2 L268）；**saved 经同一事件路径写入 permissions 投影，不新增独立 JSON 权限文件（L96 硬约束）**；审批协议：requested→（replied|closed|expired），默认 300 秒（L268）；策略直接 deny 不伪造宿主回复、实际 reject 才写 py.permission.replied（L268）；未连接审批→py.permission.closed（cancelled+ApprovalUnavailable）（L268）；**独立控制通道** `reply_permission(permission_id, reply) -> DecisionReceipt`（§7 L316：与运行锁分离，避免等待审批时死锁）；**条件更新竞争**（§7 L173：回复/过期/关闭只允许首个提交的合法迁移，迟到回复 ApprovalExpired/Conflict；不在等待审批时持提交锁）；**OP-9 激活**：close()/cancel 的 pending approval 分支（§7 L337 py.permission.closed；r2-slices L181 保守读法"分支代码面留位 R3 接入"兑现）；invoke 挂点接线（§5.1 L259：校验成功→policy/审批→started 提交确认；L261：输入在校验/审批后不可变）。
- **涉及接缝**：tools/policy.py（新）、tools/invoker.py（policy 挂点，串行不变量不动）、session/service.py（reply_permission+到期任务+close 分支激活）。
- **新增文件**：`tests/contract/test_policy.py`（表驱动）、`tests/integration/test_approval.py`。
- **TS 基准映射**：`packages/core/src/permission/saved.ts`（once/always/ConflictError 语义；**已录指纹（#102）**）；审批 UI 面 TS modal/session-ui **不沿用**（差异表 4）。
- **验收标准**：验收 B（L92）逐句——allow/deny/ask；once/always/reject+到期；独立通道不被 active run 锁死；无人审批拒绝执行；审批与 cancel/expiry 条件更新竞争、首个合法提交者胜出；迟到回复 ApprovalExpired/Conflict。
- **测试计划**：表驱动 policy（决策矩阵）；审批/取消/到期竞态两分支（注入提交顺序，沿 S7 `_CancelAfterStepFinishedCommitter` 同款 store 正常提交注入——**n-3 记档约束：注入基建绕过 committer 锁与 bus，复制场景须携注**）；迟到回复双错误码；审批中 run 并发可响应（锁分离阳控）；pending→denied/cancelled/not_started 状态面。
- **开放点**：OP-R3-3（CLI 审批 UX 形态）、OP-R3-5（write/edit 默认档位）。

### 2.3 R3-S3 静态读三件 ls/glob/grep + rg 依赖

- **目标**：`tools/builtin/ls.py`/`glob.py`/`grep.py`（fs 命名空间沿 read 先例）；数量限制+truncated 显式标记（验收 A；TS glob/grep EncodedOutput+toModelContent(…, truncated) 先例）；**rg 外部依赖+启动可用性检查**（验收 A：明确 rg 为外部依赖并检查可用性——启动检查+明确诊断，不静默降级；TS `ripgrep/binary.ts` verify(bytes, filename, expected) 发现+校验先例）；路径越界/超大/二进制诊断（验收 A；read 先例复用：traversal/symlink·junction realpath 检查、大小上限、二进制/invalid UTF-8 诊断——量化偏差 float-reported 声明同 §5.3 模式）；根内 allow 无审批（§5.2 L266）。
- **涉及接缝**：tools/builtin/（新三件）、tools/registry.py（注册，串行不变量）。
- **新增文件**：`tools/builtin/ls.py`、`glob.py`、`grep.py`、`tests/contract/test_tool_listing.py`（或并入 test_tools.py 扩容）、`tests/integration/test_tool_static_trio.py`（真实临时 workspace）。
- **TS 基准映射**：`tool/plugin/glob.ts`（139L：EncodedOutput/truncated）、`tool/plugin/grep.ts`（170L：同款）、`ripgrep/binary.ts`（166L：rg 发现校验）——**均已录指纹（#102）**；**ls 无 TS 对应物**（TS glob 覆盖列目录语义；Python 按验收 A 明列 ls——差异表 2）。
- **验收标准**：验收 A 前半逐句（数量限制/越界/超大/二进制诊断/rg 依赖检查）。
- **测试计划**：每工具真实临时 workspace（L94）；截断+标记断言；rg 缺失/不可用诊断面；越界 symlink/junction 逃逸面（read 同款）；Windows/Linux 路径面（L94）。
- **开放点**：OP-R3-1（rg 引入方式：PATH 发现 vs vendored vs pyproject 依赖——pyproject/uv.lock 写权归调度器；保守=PATH 发现+明确诊断）。

### 2.4 R3-S4 write/edit：精确冲突检测+原子替换

- **目标**：`tools/builtin/write.py`/`edit.py`；**精确冲突检测**（验收 A：edit 多处匹配/零匹配/边缘锚定诊断——TS edit.ts findOccurrences/findLineOccurrences/matchView 332L 先例）；**原子替换**（验收 A：同目录临时文件+原子 rename，失败不半写、不留中间态）；根边界（声明 roots 内，含 symlink/junction realpath 防逃逸——read 先例）；policy 挂点（审批档位 OP-R3-5）；量化偏差 float-reported（文件大小上限/输出限额，§5.3 同模式声明）。
- **涉及接缝**：tools/builtin/（新两件）、policy（S2 挂点）。
- **新增文件**：`tools/builtin/write.py`、`edit.py`、`tests/integration/test_tool_write_edit.py`。
- **TS 基准映射**：`tool/plugin/write.ts`（101L）、`tool/plugin/edit.ts`（332L：normalizeForMatch/findOccurrences/matchView/countBefore 冲突检测核心）、`tool/plugin/file-diff.ts`（邻接参照）——**均已录指纹（#102）**。
- **验收标准**：验收 A 首句（write/edit 精确冲突检测、原子替换）。
- **测试计划**：冲突检测全分支（unique/non-unique/missing/行尾与编码边角）；原子性（写入中断注入→无半写）；真实 workspace 往返（write→read 校验逐字）；越界写拒绝。
- **开放点**：OP-R3-5（默认审批档位——保守=ask）。

### 2.5 R3-S5 bash+受控子进程环境

- **目标**：`tools/builtin/bash.py`+受控子进程环境；**shell 显式选择解释器**（§5.3 L276：Unix bash/Windows PowerShell，启动时检查可用性，不依赖 create_subprocess_shell 平台默认——TS shell/select.ts resolve/list/gitbash 先例）；**超时/取消收回进程树**（验收 C：进程组/Job Object 机制见 OP-R3-9；取消传播沿 §7 L335 语义，已开始命令如实保存已确定结果）；**输出限额不导致管道死锁**（验收 C：持续排空+限额截断+显式标记，读端不可阻塞卡死）；**deny 清单=辅助提示非沙箱**（L96 硬约束：TS shell/scan.ts 2409L 完整词法扫描器**不移植**——Python 保守=最小 deny 正则集+文档化"仅辅助提示"，危险命令正则不能证明安全 §5.3 L276 末句）；完整 command/cwd 审批绑定（§5.2 L268）；超时默认值参照 TS shell.ts DEFAULT_TIMEOUT_MS。
- **涉及接缝**：tools/builtin/、子进程环境（新模块）、policy（ask 默认）。
- **新增文件**：`tools/builtin/bash.py`（或 shell.py，命名沿 wire 名纪律——fs 命名空间外新命名空间由 C 席对契约落定）、`src/opencode/process/`（受控子进程环境）、`tests/integration/test_tool_bash.py`。
- **TS 基准映射**：`tool/plugin/shell.ts`（293L：DEFAULT_TIMEOUT_MS/resultMessages/toolResult）、`shell/select.ts`（227L：解释器显式选择）、`shell/scan.ts`（2409L：仅参照**不移植**）、`shell/result.ts`——**均已录指纹（#102）**；TS persistent-pty/pty 常驻面**不沿用**（差异表 6）。
- **验收标准**：验收 C 首句逐句（超时/取消收树、输出限额无死锁）+L96 deny 约束。
- **测试计划**：超时→树回收断言；取消→树回收+not_started/unknown 效果语义（§2.1 L35）；输出洪水→限额+无死锁（**带超时的测试级断言**）；deny 提示触发+不阻断语义；Windows/Linux 双平台子进程（L94+A-PLATFORM）。
- **开放点**：OP-R3-9（Windows 进程树回收选型：Job Object vs taskkill /T——A-PLATFORM 要求双平台证据）、OP-R3-2（deny 清单内容——保守=最小集+文档化）。

### 2.6 R3-S6 ConfigLoader/快照

- **目标**：config 模块；**config 声明 roots**（read.py 模块 docstring 硬输入："R2 唯一允许 root=working_dir；R3 增加 config 声明 roots"——多根归置进 §5.2 根解析，read/ls/glob/grep/write/edit 全工具共用根边界面）；**项目配置不能扩宿主 roots/凭据/MCP 信任**（验收 C：拒绝面显式+测试钉死）；**重启后按原快照恢复**（验收 C：ConfigSnapshot canonical hash 漂移检测复用 R1 机制——CORE-CONTRACTS L300 Python 侧论证：摘要仅覆盖非秘密配置、固定序列化字节可重启重算检测漂移）；ConfigSnapshot v1 语义**不可静默改**（§9 L368；新增配置字段→版本升级+upcaster §8 L358）；CLI 参数面→ConfigLoader 注入（cli.py docstring 声明的 R3 注入点，替换 `--model`/`--context-limit` 直构路径）；**surrogate 前置扫描一行**（T-C1-002-review ② B 意见已采纳带入：ConfigLoader 引入字符串快照字段时的 surrogate 前置扫描）；system_prompt 单点替换（R2-S4 ④-1 注记：RuntimeSettings 构造器一字段+一分派行）；session 复用/配置层议题落位（T-C1-023 R-3 归 R3 议题——本切片承接）。
- **涉及接缝**：`src/opencode/config/`（新）、cli.py（注入面替换，stdlib-only 纪律延续）、session/context.py（RuntimeSettings system_prompt 单点）。
- **新增文件**：`src/opencode/config/loader.py`（形态见 OP-R3-10）、`tests/contract/test_config.py`、`tests/integration/test_config_loader.py`。
- **TS 基准映射**：`packages/core/src/config/discovery.ts`（145L：Sources/discover L15-25/59-145 多来源发现——**Python 不做多来源信任合并**，差异表 7；**已录指纹（R1 既有）**）、config/normalize.ts/variable.ts（参照）。
- **验收标准**：验收 C 后半逐句（不扩信任；重启按快照恢复）。
- **测试计划**：快照往返+重启恢复一致性；项目配置扩信任面拒绝（roots/凭据/MCP 三面）；config roots 边界（根内 allow/越界诊断）；快照漂移检测；CLI 注入面替换后 R2 既有 CLI 测试零回归。
- **开放点**：OP-R3-10（config 文件形态与位置——保守=最小单文件，不做 TS discovery 多来源）。

### 2.7 R3-S7 CLI 审批面+R3 增项收口

- **目标**：CLI 审批控制通道（approve/reject REPL 命令+待审批提示——reply_permission CLI 化；审批不被 active run 锁死的进程内语义已由 S2 落，本切片落宿主面）；**R3 增项台账收口（§3）**：**P-1 真 SIGINT 双子场景（必做）**——subprocess 级真实信号（对齐 R2 ⑫ 冒烟形态）：(a) mid-run Ctrl-C→cancel→等待→exit 2；(b) prompt 面 Ctrl-C→exit 130+**进程真实退出断言**（带超时 wait 防 join 挂起）；若 (b) 复现挂起→`os._exit(130)` 快路径或输入线程 daemon 化（R2-S8-review n-1 处方原样）；P-2 /history 非数值参数回显警告；P-3 `_print_terminal` 改取 `CoreError.message`；非 UTF-8 控制台批处理；exit 3/migration 注入（与 SIGINT 条目合并）；runner 拆包评估（host 装配面触发条件核对，R2-S6 ④）；n-3 注入基建泛化评估（携注纪律入测试计划）。
- **涉及接缝**：cli.py、tests/integration/test_cli.py 扩容。
- **新增文件**：`tests/integration/test_cli_r3.py`（审批面+SIGINT 双子场景）。
- **TS 基准映射**：TS 审批 UI（modal/session-ui）不沿用；SIGINT 面无 TS 对应物（Python CLI 独有）。
- **验收标准**：审批 B 句宿主面（独立通道 CLI 可达）；P-1 双子场景全绿。
- **测试计划**：subprocess 级双子场景+真实退出断言；审批 REPL 命令全分支；并发审批响应（run 等待中审批可完成）。
- **开放点**：OP-R3-3（UX 形态）、OP-R3-7（pyproject [project.scripts] entry）。

### 2.8 R3-S8 全链 e2e+R3 退出证据收口

- **目标**：`doc/reference/r3-exit-evidence.md`（对齐 r1/r2 模式：验收 A/B/C+验证行四项→file:line/测试/审查三方映射+S1-S7 收口+PHASE1-SLIM §4 对照；行号声明纪律沿 r2 教训——**继承 r1/r2 行号须重锚**，R2-S8-closeout m-1 类不再犯）；全链 e2e（六工具真实 workspace 串行链：write→edit→bash 验证→grep 检索+审批驱动面）；验证行 L94 落测核对：每工具真实临时 workspace 集成+Policy 表驱动+Windows/Linux 路径/子进程测试（分布 S3-S7，本切片核对全量+收口）；**函数级测试名枚举补齐**（pytest --collect-only 一次性，R2-S8-closeout ④(a)）；**r-1 e2e 断言增强**（requests[1] 历史含 interrupted assistant——R2-S8-closeout r-1）；**projector 折叠缓存内存压力实测**（§10 L424 裁决触发条件判定：成立→上浮调度器设票，不成立→记档维持）。
- **涉及接缝**：tests/integration/test_e2e_r3.py（新）、doc/reference/r3-exit-evidence.md（新）。
- **TS 基准映射**：无新引用（收口）。
- **验收标准**：L94 验证行全量；R3 退出证据文档映射完整（对照 PHASE1-SLIM §4 L87-96 逐行）。
- **测试计划**：e2e 多工具+审批链；每工具计数交叉验证（沿 R2 e2e EventLog SQL+requests 模式）。
- **开放点**：无（收口；projector 淘汰票上浮裁量）。

## 3. R3 增项台账（硬性输入③逐份清点：doc/reviews/ R2-S2..S8 全部评审 R3/后续节 + progress ③④）

| # | 项 | 来源 | 处置（并入/缓） |
|---|---|---|---|
| 1 | P-1 真 SIGINT subprocess 双子场景（prompt 面 exit-130 挂起风险；os._exit(130)/daemon 化备选） | R2-S8-review n-1（T-B-024 处方） | **R3-S7 必做** |
| 2 | P-2 /history 非数值参数告警 | R2-S8 r-1 | R3-S7（可选） |
| 3 | P-3 `_print_terminal` 渲染 str(CoreError) repr | R2-S8 r-3 | R3-S7（可选） |
| 4 | 函数级测试名枚举（pytest --collect-only 一次性补齐） | R2-S8-closeout ④(a) | R3-S8 |
| 5 | r-1 e2e requests[1] 历史含 interrupted 断言 | R2-S8-closeout r-1 | R3-S8（可选） |
| 6 | projector 双注册表漂移（`_REPLAY_REGISTRY` import 期冻结→注入/惰性） | R2-S2-review n-1 | **R3-S1 必做**（应用侧注册触发点） |
| 7 | m-1 O(n²) 重放热点（原"跟进 S7 前优化"） | R2-S2-review m-1 | R3-S1 状态核对（若未随 R2 合并修则顺带评估，不阻塞） |
| 8 | m-2 撞键文案小修 | R2-S2-review m-2 | 同上（状态核对） |
| 9 | CJK 乘数/真 tokenizer 缓存 | R2-S4 ④-6/m-3+§10-2 L404 | **缓 R4+**（§10-2 已定 R2 零行为变更；fail-visible 保持、需求未证实；决策归调度器） |
| 10 | ephemeral delta 合批（100ms 先例） | R2-S6 n-2 | **缓**（条件触发：R3 总线压力实测成立再议） |
| 11 | runner 单文件→包拆分 | R2-S6 ④ | R3-S7 评估（触发=host 装配面需求） |
| 12 | OP-9 permission.closed 分支激活 | r2-slices L181/R2-S7 §6-4 | **R3-S2 必做**（审批接入后协议可达） |
| 13 | m-1 残窗 cancelling() | R2-S7c/#92 | **显式缓不设票**（#92 裁定维持，R3 不重开） |
| 14 | R-3 类型收窄遗留（session 复用/配置层） | T-C1-023 ④ | R3-S6 承接（ConfigLoader 面） |
| 15 | 迁移失败注入（alembic 子进程→70） | R2-S8 §5 | R3-S7（与 16/17 合并条目） |
| 16 | exit 3（cancel 清理失败）真实触发 | R2-S8 §5 | R3-S7（同上合并） |
| 17 | 非 UTF-8 控制台 | R2-S8 §5 | R3-S7 批处理 |
| 18 | SlowConsumerError 显示 | R2-S8 §5 | **缓/低价值**（stderr 单行已满足契约） |
| 19 | surrogate 前置扫描一行 | T-C1-002-review ②（B 意见已采纳带入） | R3-S6 一行 |
| 20 | session 复用/配置层=CLI 参数面→ConfigLoader | R2-S8 §5/T-C1-023 R-3 | R3-S6 |
| 21 | pyproject [project.scripts] entry | OP-5 残留 | OP-R3-7（调度器裁量） |
| 22 | n-3 注入基建（绕过 committer 锁与 bus——复制场景须携注） | R2-S7c n-3（记档防扩散） | R3-S2/S8 测试纪律条款（沿用注入 idiom 必携注） |
| 23 | evidence 行号重锚（§3.1 L187→L195 类） | R2-S8-closeout m-1 | 状态核对：若未随 r2-s9 合并修正→R3-S1 首项一行带入 |
| 24 | upcaster 机制平台级缺位（`upcast` 全库 0 命中含 R1/R2；首个 py.* 事件族升 v2 前置条件：旧版本模型保留+确定性转换+旧 fixture 可读，§8 L356 最低门槛） | T-B-027 nit-1（B 处方） | **R3-S8 exit-evidence 检查项**（首个 v2 前必建，非阻塞） |
| 25 | rg 读循环超时收编（m-1：`_run_rg` 30s deadline 仅 stdout 行到达时检查——rg 零输出挂起→工具调用无限挂起→串行 invoker 整 run 挂起；TS 为整进程超时，同名常量弱执行面。R3-S5 受控子进程环境统一超时/收树机制落地时收编，一处 `_run_rg` 改动） | T-B-029 m-1（B 处方） | **R3-S5 任务书条款**（明列"rg 读循环超时收编"） |
| 26 | nit-2 `test_grep_real_path_absence` 直接 `_RG_CACHE.clear()` 无恢复（全局态测试卫生）；nit-1 截断分支"确定性"措辞仅覆盖已收集子集内次序（成员身份依赖枚举序） | T-B-029 nit-2/nit-1 | nit-2=后续触达该测试文件切片顺手改 monkeypatch.setattr 整体替换（非必须）；nit-1=S8 evidence 书写注意项（零代码） |
| 27 | m-1 close-timeout 硬取消掉队者可孤儿化 PENDING 权限（§2.1 L53 角落——close 硬取消绕过审批协调器正常 closed 配对面，且无恢复扫描兜底）；closed_outcome "restart" 词表零生产者=崩溃恢复面前瞻（非死词表）；T-C1-028 ④-1 崩溃窗 `.opencode-fs-*.tmp` 遗留（硬杀落于 temp 写入后/swap 前——跨进程 kill 无注入缝不可测，BaseException unlink 仅覆盖进程内失败路径；目标本身完好，无启动清扫兜底） | T-B-028 m-1（B 处方）+ T-C1-028 ④-1（席位浮报，裁定 concur） | **恢复切片任务书三条款**：①孤儿 pending 清理；②closed 门对账或受认定角落声明；③`.opencode-fs-*.tmp` 崩溃遗留宿主启动期清扫（同目录模式串，宿主运营面承载）——裁定：均非交付切片缺陷，零代码 |
| 28 | nit-1 默认表未知动作回落 declared side_effects 无契约锚+bash/MCP 动作无默认表键（开放点）；nit-2 reason 常量双份（tools/policy.py 与 session/service.py 本地复制）无 drift pin；nit-3 revoke check-then-act assert 窗口（当前不可达——唯一 saved 生产者写 expires_at=None） | T-B-028 nit-1/2/3 | nit-1=S6 补表键或受认定契约注记（开放点闭合载体）；nit-2=S4/S5 触达该面时顺手加 drift pin（非必须）；nit-3=到期决定落地（expires_at 非 None 生产者出现）时回收 assert |
| 29 | T-C1-028 ④-2/④-3 移植重启条件（前瞻记档，非缺陷）：④-2 formatter 钩子——TS write.ts 落盘后 format+sync BOM，Python 侧无 formatter 服务承载面（已量化偏差）；④-3 file-diff.ts——TS 侧 fileDiff 产物仅流向 permission.assert 的 metadata.files 审批预览与 Output.files 结构化输出，Python 侧 S2 审批通道无此二消费者（任务书④"仅参照"裁决成立） | T-C1-028 ④-2/④-3（席位浮报，裁定 concur） | 重启条件载明：formatter=Python 侧格式化集成承载面落地时连带处理；file-diff=approval metadata.files 预览或 structured-output 消费方出现时连带移植；④-4 cli.py 行号漂移（冻结 test_tool_static_trio.py docstring 引"cli.py L276"→实际 L283，import 括号化 +7）=leave-as-is float 仅台账记档不开行（装配面语义仍真，S2 events.py L333 同类先例；席位已把可编辑面内同款引述改为无行号措辞） |
| 30 | T-B-030 nit 三项（均登记级，零返工）：nit-1 write/edit TOOL_FAILED 包装措辞 "Cannot {path}: {error}" vs TS "Unable to"——语义等价纯措辞面，进度④披露档未列（集成测试已 pin "Cannot"）；nit-2 进度⑤"既有 8 测原名原断言"不精确——六件套扩展使 2 测断言必然扩展（目录清单断言纳入 write/edit+重复拒绝循环 3→5），测名未改属实；nit-3 ③四档缺 endingsOnly-unchanged 一行（该分支双实现结构不可达，edit.py 代码注释已披露"④ disclosure"而进度③未列） | T-B-030 nit-1/2/3 | nit-1=后续触达 write.py/edit.py 的切片顺手对齐措辞（连动测试断言）或维持 Python 措辞并在④补记（非返工）；nit-2/nit-3=恢复切片或下次触达进度文件/③四档时补记（本链保持评审对象与并回对象 1:1，不做事后文件修正）；开放点（悬空 symlink NOT_FOUND vs file-access.ts 未对拍）仅存评审报告不进台账 |

## 4. 关键设计差异备忘（Python vs TS，实现时勿"照抄"）

1. **架构延续**：TS Effect 服务图（Context.Service+Layer）vs Python 单协程状态机+SessionService 持强引用——R2 备忘沿用。
2. **ls 工具无 TS 对应物**：TS glob 覆盖列目录语义；Python 按 PHASE1-SLIM L91 明列 ls——参照 glob.ts 列举语义+read 先例，勿造 TS 不存在的行为面。
3. **审批 UI**：TS modal/session-ui 面板；Python=CLI REPL 命令（§7 L316 reply_permission 独立控制通道语义为唯一基准）。
4. **saved 决定存储**：TS permission/sql.ts drizzle 表 CRUD；Python=**permissions 投影+同一事件路径**（L96 硬约束），不新增独立 CRUD 面/JSON 文件。
5. **shell 扫描**：TS shell/scan.ts 2409L 完整 bash/PowerShell 词法器；Python 保守=最小 deny 正则辅助提示（L96 非沙箱+§5.3 L276"危险命令正则不能证明安全"），**不移植**。
6. **子进程形态**：TS persistent-pty/pty 常驻终端；Python=每命令受控子进程（§5.3 L276 显式解释器），Phase 1 无 pty。
7. **配置**：TS discovery.ts Sources/discover 多来源合并；Python=最小 ConfigLoader+ConfigSnapshot（canonical hash 已有），**项目配置不扩信任**（L93）。
8. **rg**：TS ripgrep/binary.ts vendored 二进制校验；Python=外部依赖+启动可用性检查（引入方式 OP-R3-1）。

## 5. 风险表（硬性输入④：reestimate-r0 L49/L62 + R2 执行期新识别）

| 风险 | 面 | 缓解/证据要求 |
|---|---|---|
| Windows 进程树回收（bash 超时/取消收不干净） | S5 | OP-R3-9 机制选型；双平台测试（L94+A-PLATFORM L62）；取消传播沿 §7 L335 |
| symlink/junction 逃逸（write/edit 新增变更面+原子替换临时文件） | S4 | read 先例 realpath 检查复用；临时文件同目录；测试钉死 |
| 项目配置扩大信任（roots/凭据/MCP） | S6 | 验收 C 硬拒+三面测试钉死；快照 hash 漂移检测 |
| 审批独立通道死锁/竞态 | S2 | §7 L173/L316 锁语义（不持锁等审批）；竞态两分支注入测试 |
| rg 外部依赖不可用/版本漂移 | S3 | 启动检查+明确诊断不静默降级；OP-R3-1 引入方式裁决 |
| bash 输出洪水→管道死锁 | S5 | 持续排空+限额截断；测试带超时断言 |
| 双注册表漂移（R3 应用侧注册触发 R2-S2 n-1） | S1 | 注册表单源化**先于**任何 py.permission.* 落库（S1 首位） |
| permissions 表列集不足（0001 L32-38 vs L183 四字段） | S1 | OP-R3-8；只追加 0003 不改写既有 revision（§3.2 L185） |
| scan.ts 全量移植陷阱 | S5 | 差异表 5 明令不移植；保守最小集 |
| evidence 文档行号继承漂移 | S8 | r2 教训（m-1 类）：继承 r1/r2 行号必须重锚 |

## 6. 开放问题（不预写裁决，留 A/调度器）

- **OP-R3-1**：rg 依赖引入方式（PATH 发现/vendored/pyproject 依赖）——pyproject/uv.lock 写权归调度器；保守=PATH+明确诊断。
- **OP-R3-2**：bash deny 清单内容与维护位置（最小集+文档化；仅辅助提示）。
- **OP-R3-3**：CLI 审批控制通道 UX 形态（REPL 命令名/非交互审批模式）。
- **OP-R3-4**：tool_ref wire 转换单一 owner（§10-4 L414 意见：R3 接缝重构时指定单一 owner，保留点分名/wire 往返/畸形名透传/未知工具配对；runner 不引入具体 MAAS 依赖）——建议落 S3 registry 面，最终方向调度器裁决。
- **OP-R3-5**：write/edit 默认审批档位（TS 参照 vs 保守 ask）。
- **OP-R3-6**：CJK 乘数/真 tokenizer（§10-2 L404：R3+ 决策；建议缓 R4+）。
- **OP-R3-7**：pyproject [project.scripts] entry（OP-5 残留）。
- **OP-R3-8**：permissions 表列集对齐（复用 0001 vs 追加 0003）。
- **OP-R3-9**：Windows 进程树回收机制（Job Object vs taskkill /T）。
- **OP-R3-10**：config 文件形态与位置（最小单文件 vs TS discovery 多来源）。

## 7. 硬性输入①-⑧对照自检清单

| 输入 | 落点 | 状态 |
|---|---|---|
| ① PHASE1-SLIM §4 L87-96 全量 | §0 逐行摘录（L89/91/92/93/94/96 verbatim）；验收 A→S3/S4、B→S2、C→S5+S6、验证行→S3-S8、L96 约束→S2（saved 同一事件路径）/S5（deny 非沙箱） | ✓ |
| ② CORE-CONTRACTS 锚点 | §0.1（L183/L140/L160/L348）+S1（L35/L183/L358/L185）+S2（L266/L268/L173/L316/L335/L337）+S3（L266）+S4（L272-276 同模式）+S5（L276/L268）+S6（L300 论证/L368/L358）+台账 9/6（L404/L414/L424） | ✓ |
| ③ R2 评审增项全量清点 | §3 台账 23 项（S2-S8 全评审+closeout+T-C1-002/023 progress 逐份扫描） | ✓ |
| ④ 六工具面+受控子进程 | S3（ls/glob/grep+rg 依赖+数量/诊断）/S4（write/edit 冲突+原子）/S5（bash 树回收+管道死锁+deny）；read 先例内联 | ✓ |
| ⑤ Policy/审批/保存决定 | S2 全量（allow/deny/ask、once/always/reject、到期、独立通道、竞态、ApprovalExpired/Conflict、py.permission.* 注册+投影） | ✓ |
| ⑥ ConfigLoader/快照 | S6 全量（声明 roots、不扩信任、快照恢复、v1 语义、CLI 注入点、surrogate 行、read docstring 硬输入） | ✓ |
| ⑦ TS 基准清单+行号锚 | 各切片 TS 映射（discovery L15-25/59-145、runtime L287-292 已锁）+头部指纹清单（**12 文件已随 #102 补录**，discovery 系 R1 既有） | ✓ |
| ⑧ 门禁基线与配方 | 头部基线（#99：651/66/双 ruff）+§8 模板（python -m 形式+per-tree PYTEST_DEBUG_TEMPROOT） | ✓ |

## 8. 每切片任务书模板（供调度器直接取用派发 C 席；对齐 T-C1-024 风格）

```markdown
# T-C1-0XX · 席位C-1：R3-SX <切片标题>

【角色章程·C 实现者】（三角色流水线：A 仲裁者 / B 规划评审 / C 实现者；本会话由调度器驱动，你是席位C-1。以契约文本+切片方案（doc/plans/r3-slices.md §2.X）为唯一基准；**禁止 git 写操作**（提交唯一归调度器）；门禁自跑后回报数字；④ 强制浮报所有未测/协议不可达防御分支；⑤ 存量兼容回归零破坏）
- 工作目录：worktree `C:/Users/lyu/.local/share/opencode/worktree/<id>/r3-sX`（分支 r3-sX，基于 main 检查点#NN——worktree HEAD 即基线，`git log -1` 自证）。
- 写权限（本任务显式授权，仅此）：<文件清单——逐文件列出，含"新增"/"修改"与红线声明（src 冻结面/契约文档/pyproject/uv.lock/CI/source-baseline.json/ORCHESTRATION 零触碰）>。**其余一切冻结**；e2e/测试若暴露 src 缺陷→停下浮报不修，修复归调度器另行授权；fixture 只读复用（跨模块导入或同形态新建，不改既有文件）。
- 门禁（worktree 内自跑，python -m 形式）：先导出专属 temproot（已预建）：`$env:PYTEST_DEBUG_TEMPROOT=$env:TEMP+'/opencode/pytest-tmp/r3-sX'`；四件：`uv run python -m pytest -q`（基线 651+N 零回归+新增如实计数——逐测落点入进度文件）；`uv run ruff check .`；`uv run ruff format --check .`（计数非验收标准，退出码 0 即过）；`uv run python -m mypy src tools/r0 tests/r0 tests/contract tests/integration`（基线 66 files+N 零问题）。
- 止损：NNmin，到线即停——已完成部分+剩余清单写进度文件，回报"部分完成"；超时如实记录不粉饰。防截断：进度文件增量写入；最终回报 1-2 段。
- TS 锚纪律：本切片引用 TS 文件须已在 source-baseline.json 指纹内（**12 文件已随检查点#102 补录完成**——discovery.ts 系 R1 既有）；不宣称与 TS 同构；差异按 r3-slices §4 备忘保守读法。
- 开放点纪律：r3-slices §6 所列 OP-* **不预写裁决**——实现触及时按保守读法落地+进度④浮报，越出即停下待裁。

【任务】<逐项：spec 引用（r3-slices §2.X 目标/验收句原文）+最低落测结构+每测落点要求（测名↔验收句映射入进度文件）>

【④ 强制浮报】协议不可达/未测防御/已测防御/注记四档（R2 定案模式）；注入基建沿用须携注（R2-S7c n-3 记档）。

【⑤ 存量兼容】全量基线零回归红线；src 冻结面零触碰（本切片授权清单外）；既有测试文件零触碰；新增仅授权清单。

【进度文件】`doc/orchestration/progress/T-C1-0XX.md`：①方案审视发现（选型论证）②门禁四件数字+新增测试逐测落点 ③强制浮报清单 ④未决问题 ⑤存量兼容声明。

【回报格式（回复正文，1-2 段）】完成度 / 门禁四件数字（含新增测数）/ 落点概要 / ③④ 概要 / 是否有需调度器裁决项。
```
