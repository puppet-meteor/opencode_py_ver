# ORCHESTRATION — TS→Python 三角色调度台账

> 唯一调度事实来源（调度器维护，各席位只读）。
> 裁决基准：一切问题以 opencode 原始 TS 实现（`../opencode-2.2.2/`，指纹见 `doc/source-baseline.json`）为基准解决。
> 方案 v1 已获用户批准（2026-11-02）。

## 1. 席位绑定与派发模板

| 席位 | 角色 | 命令 | 模型 | 会话续接 |
|---|---|---|---|---|
| A | 仲裁者：架构定稿/契约裁决/疑难根因/连续失败升级/高风险终审/沉淀规范 | `codex exec`（0.163.0） | gpt-6.1-sol · reasoning max（本机 codex config，不覆盖） | `codex exec resume 01a19a51-71d4-7ed0-a4ce-40730a5d26b7`（线程ID已捕获；resume 不支持 --cd，靠 shell workdir） |
| B | 规划评审：拆模块/迁移方案草稿/常规结构化审查 | `opencode run --session ses_orch_B` | gateway/glm-5.3#max | 同 `--session` ID 重派 |
| C-1 | 实现者主力：方案可执行性审视/实现/修订；核心接缝与高风险专用 | `opencode run --session ses_orch_C1` | gateway/glm-5.3#max | 同上 |
| C-2 | 实现者补充：批量/夜间/机械环节；可多席并行（每席独立 worktree + 独立 session ID） | `opencode run --session ses_orch_C2` | gateway/volcengine/deepseek-v4.1-flash#max | 同上 |

派发模板（命令行全 ASCII，中文任务全在任务包文件里，规避命令行编码问题；**opencode 的 --session 值必须以 `ses` 开头**）：
- A：`codex exec --cd <repo> --json -o <产出捕获文件> "Read the file doc/orchestration/tasks/<T-ID>.md and execute the instructions in it."`
- B/C：`opencode run --model <模型ID> --session <ses_ 开头的席位ID> --auto "Read the file doc/orchestration/tasks/<T-ID>.md and execute the instructions in it."`

路径所有权：A→`doc/adr/`、`CORE-CONTRACTS.md`、`ROADMAP.md`（+任务包显式授权文件）；B→`doc/plans/`、审查报告；C 席→独立 worktree 内 `src/`。席位互不直连，调度器是唯一总线。

**Git 与版本控制（唯一归调度器，用户指示 2026-11-02）**：
- 席位禁止一切 git 写操作（add/commit/branch/checkout/restore/reset/stash/merge/rebase/worktree/clean/push 等），每份任务包写明此条款；只读 git（status/log/diff/show）允许
- 调度器负责：门禁通过后的提交、断点回滚、worktree 创建/删除/合并、暂存纪律（只 add 指定路径，禁止 `add -A`，不误收在飞产物）
- 提交约定：沿用仓库 conventional commits；编排产物用 `docs(orch):` 前缀；每任务过门禁即一提交（commit = 检查点）
- 不 push 远程；用户明确要求时才由调度器执行

## 2. 任务包规范

- 文件：`doc/orchestration/tasks/<T-ID>.md`，自包含：角色章程 + 目标 + 输入文件清单 + 验收 checklist + **禁止 git 写操作条款** + 止损指令（预计超时→立即停止，已完成部分与未决问题写入产出文件，回报"部分完成"）+ 回报格式
- 产出必须落仓库文件（stdout 仅作回报）；实现类长任务另在 `doc/orchestration/progress/<T-ID>.md` 增量记进度

## 3. Watchdog

- 名义时长（宽松参考，不作为卡死判据；实测：方案 7min、复估 38min）：冒烟 ~10min｜审查 ~15min｜方案 ~20min｜复估定稿 ~40min｜实现 30–60min｜A 席（codex/GPT-6.1-Sol）可能数小时
- **判卡死唯一标准（用户指示，2026-11-02）**：子 shell 输出日志（`doc/orchestration/logs/*.txt|*.jsonl`）**1 小时零增量**（比 LastWriteTime/size）→ 判卡死 → kill（Stop-Process）→ 恢复阶梯。不按时长判。调度器为在飞任务挂后台活性监视器（每 10min 检查、1h 零增量即报警并退出、6h 自动到期重挂）；每次唤醒后按仍存活的在飞任务重挂/刷新监视器
- 巡检时机：任一席位回报事件、每次门禁、用户喊"查状态"、监视器报警
- 打断：外部进程可真 kill（Stop-Process）；kill 后按恢复阶梯决定续接或重发；未验收残留按 git diff 丢弃
- 连击升级：同任务 2 次卡住 / 同模块 2 轮审查不过 → 升 A 根因；疑似网络/环境 → 上报用户

## 4. 检查点与网络恢复协议

**检查点纪律（派发前三件套先行落盘）**：
1. 任务包文件先写盘
2. 台账先记"派发中"（席位/任务包路径/命令行/会话ID/启动时刻/名义deadline）
3. 然后才启动进程

主树只在门禁通过后 commit（commit = 代码检查点）；在飞工作留在工作区/worktree，diff 可查可弃。

**恢复阶梯（任务中断后依次尝试）**：
- ① 同会话续接：A=`codex exec resume <sid> "…"`，B/C=同 `--session` 重派——上下文最完整
- ② 续接失败/产出损坏：新会话 + 原任务包重发 + 检查点说明（磁盘现状、保留/回滚决定）
- ③ 再失败：升 A 根因；疑似网络/环境 → 直接上报用户

**网络故障策略（用户要求：及时上报）**：识别到连接重置/超时/网关 5xx/流中断 → 同任务自动重试 1 次；再失败立即上报用户（席位/任务/错误摘要），不再烧重试。

**调度器恢复协议（自身断线后，用户说"继续"即触发）**：读本台账 + 主树与各 worktree 的 `git status` → 在飞任务分类：已验收（台账有记录+产出在）／半途（有 diff 无验收 → 恢复阶梯①或②）／未动（直接重发）→ 更新状态机，从断点继续；不确定处问用户。

## 5. 状态机与暂停点

- 任务状态：`派发中 → 已回报 → 已验收 ｜ 卡住 → 已重启 ｜ 网络故障 → 已重试/已上报`
- 暂停点（等用户确认，grilling ≤5 问、单主题、逐层推进；争议默认以 TS 原始实现裁决）：① A 复估定稿后 ② 高风险模块 A 终审后 ③ 升级事件处置后 ④ 里程碑完成时

## 6. 任务表

| ID | 席位 | 任务 | 启动 | 名义deadline | 状态 | 产出 |
|---|---|---|---|---|---|---|
| T-PROBE-A | A | 冒烟测试 | 11:33 | ≈11:43 | 已验收（11:44） | doc/orchestration/probe/seat-A.md |
| T-PROBE-B | B | 冒烟测试 | 11:37 | ≈11:47 | 已验收（11:40） | doc/orchestration/probe/seat-B.md |
| T-PROBE-C1 | C-1 | 冒烟测试 | 11:37 | ≈11:47 | 已验收（11:40） | doc/orchestration/probe/seat-C1.md |
| T-PROBE-C2 | C-2 | 冒烟测试 | 11:37 | ≈11:47 | 已验收（11:40） | doc/orchestration/probe/seat-C2.md |
| T-B-001 | B | R1 切片拆分草稿 | 11:50 | ≈12:10 | 已验收（11:57） | doc/plans/r1-slices.md |
| T-A-001 | A | R0 复估定稿 | 11:50（resume 续接） | ≈12:15 | 已验收（12:28） | doc/reestimate-r0.md |
| T-A-002 | A | R1 契约裁决（4 项落条款） | 14:20（resume 续接） | 活性监控（1h 日志零增量） | 已验收（14:59） | doc/CORE-CONTRACTS.md（修订+§9 对照） |
| T-C1-001 | C-1 | R1-S1 试点：最小 contracts+fixture | 14:20 | 活性监控（1h 日志零增量） | 已验收（14:55） | src/opencode/contracts/、tests/contract/ |
| T-B-002 | B | R1-S1 结构化审查 | 14:59 | 活性监控（1h 日志零增量） | 已验收（15:07） | doc/reviews/R1-S1-review.md |
| T-A-003 | A | 契约微裁决（审查遗留 7 项） | 15:14（resume 续接） | 活性监控（1h 日志零增量） | 已验收（15:44） | doc/CORE-CONTRACTS.md、doc/parity.md（修订+§10） |
| T-B-003 | B | r1-slices.md 修订（审查报告 §4 九条） | 15:14 | 活性监控（1h 日志零增量） | 已验收（15:22） | doc/plans/r1-slices.md（修订） |
| T-B-004 | B | r1-slices.md 微调（按 T-A-003 终裁对齐预写） | 15:48 | 活性监控（1h 日志零增量） | 已验收（15:56） | doc/plans/r1-slices.md（修订） |
| T-C1-002 | C-1 | contracts 修补批次（M-1/M-4/N-2+裁决落地） | 15:48 | 活性监控（1h 日志零增量） | 已验收（16:02，N-2 半项→T-C1-002B） | src/opencode/contracts/、tests/contract/ |
| T-C1-002B | C-1 | 微补：EventDraft 空串 id 拒绝直测 | 16:02 | 活性监控（1h 日志零增量） | 派发中 | tests/contract/test_events.py |

## 7. 调度日志

- 11:32 名册确认（A=codex/GPT-6.1-Sol max；B、C-1=opencode/GLM-5.3#max；C-2=opencode/DeepSeek-V4.1-Flash#max）。codex 0.163.0 `exec`/`resume`/`--json`/`-o` 验证通过；opencode `run --session/--model/--auto` 验证通过。台账建立（含网络恢复协议）。四席位冒烟并行派发（B/C1/C2 初用 orch-seat-* 会话ID）。
- 11:36 B/C1/C2 首派即败（退出码 1）：`--session` 值必须以 `ses` 开头——客户端参数校验错误，秒败、无模型调用、非网络故障。会话 ID 更正为 `ses_orch_B/C1/C2` 重派。A（codex）事件流确认存活。用户指示：git 提交与版本控制唯一归调度器，固化入 §1。检查点#1 提交。
- 11:40 B/C1/C2 冒烟验收通过：回报行（PROBE-*-OK）、交付文件（seat-*.md，R1 定义正确）、git 无异常变更，三项全过。A 冒烟进入收尾（seat-A.md 已产出，codex 用 python 自检通过：单行『R1：最小事件/恢复闭环』）。观察：席位日志开头有一条非致命 stderr 记录（"Expected a string starting with ses"，PS 2>&1 重排所致），未影响结果，持续观察。T-B-001 派发至 B（GLM-5.3#max，按默认模型）。检查点#2 提交。
- 11:44 A 冒烟验收通过（PROBE-A-OK / seat-A.md / git 干净），四席冒烟全过。A 线程 ID 捕获并记入 §1。（勘误：本行及 11:40 行所记 T-A-001/T-B-001 派发与检查点#2 提交均未实际发生——调度器在启动进程前会话中断。观察：A 读中文文件正常，其 stdout 捕获存在控制台编码噪声（锟斤拷，仅显示层，文件内容无损）。）
- 11:50 用户指示：grilling 提问上限 ≤3→≤5 问（§5 及全部暂停点生效）。断点恢复：T-A-001 经 `codex exec resume` 派发（实测续接机制）、T-B-001 经 `opencode run --session ses_orch_B` 派发；启动 11:50，名义deadline A≈12:15 / B≈12:10；检查点#2 提交（台账修订+任务包+冒烟证据，排除在飞 logs/）。
- 11:57 T-B-001 回报"完成"（用时 ~7min，远低于名义 20min）。验收通过：依赖序明确（S1→S2→S3→S4→[S5 可并行]→S6→S7→S8）、试点切片 R1-S1 标注+粒度 30–45min、逐切片 TS 文件+行号映射、验收标准逐条归属 PHASE1-SLIM R1 验收 A/B/C+测试计划齐全；git 仅新增 doc/plans/r1-slices.md。开放问题 4 项（R1 prompt 语义/config_snapshot 最小形状与 hash/未知事件版本读取路径 fail-closed vs 读取可用/expected_seq 是否即 applied_seq）提请与 A 汇合裁决——暂停点① 素材。检查点#3 提交。
- 12:28 T-A-001 回报"完成"（11:50 启动，用时 ~38min，超名义 25min——通知驱动下调度器无独立定时器，回报到达时任务已完成，无需 kill/恢复；名义值校准至 ~40min，见 §3）。验收通过：五节齐全（复估结论/R1 定义确认+退出项/契约修订 3 项均附 TS 行号引用+Python-only 边界标注/R1–R5 基线表目标 24 上限 30 人日/高风险 A-S4·A-S6·A-RECOVERY·A-PLATFORM·A-PROVIDER 终审标记）；抽查一致：doc/evidence/r0/ 三文件+fixtures 在盘、source-baseline 实测 20 项（任务包误写 21，A 已修正）、R0 测试 26 函数/参数化 32 用例与回报相符、platform-support.md 含所引 CI run 41182347042、git 仅新增 reestimate-r0.md。汇合要点：R1 定义维持 PHASE1-SLIM §3 不变；R0-A 条件通过（真实 MAAS smoke 凭据缺，不阻塞 R1）；B 开放问题中"未知版本读取路径"A 已有取向（只读诊断+已投影内容可读），余 3 项（R1 prompt 语义/config_snapshot 最小形状与 hash/expected_seq 是否即 applied_seq）待 A 书面裁决；B 纯实现 1.5–2.5 工作日 vs A 基线 R1 5 人日——口径不同（C 循环 vs 全流程），以 A 基线为进度对照、B 口径为 C 派发节奏。检查点#4 提交。
- 14:20 暂停点① 用户全部同意（5/5；答复于 12:35–14:20 间到达，调度器 14:20 续接处理）：①接受 A 基线 R1–R5 目标 24/上限 30 人日（超目标 30% 触发复估）②prompt() 保留在 R1（仅持久化 py.prompt.accepted+返回 RunRef，不调度）③真实 MAAS smoke 凭据推迟至 R2 前单独补齐 ④R1-S1 试点批准、主树直做（纯新增文件）、S2 起切独立 worktree ⑤技术性契约裁决授权 A 直接落条款。据此派发：T-A-002（4 项裁决落 CORE-CONTRACTS，30min 名义）、T-C1-001（试点 R1-S1，45min 名义）并行。调整：pydantic 依赖引入由 S2 前移至 S1（S1 需 Pydantic v2 wire model，B 方案边界笔误；已授权 C-1 仅经 `uv add "pydantic>=2"` 修改 pyproject/uv.lock）。检查点#5 提交。
- 14:47 用户指示（14:20–14:47 间到达，调度器 14:47 续接处理）：watchdog 放宽为活性监控——名义时长仅参考（A 席可能数小时），**判卡死唯一标准=子 shell 输出日志 1 小时零增量**→kill→恢复阶梯。§3/任务表已改；调度器为 T-A-002/T-C1-001 挂后台活性监视器（10min 检查、1h 零增量报警退出、6h 到期重挂；每次唤醒后按仍存活任务刷新）。检查点#6 提交。
- 14:55 T-C1-001 回报"完成"（14:21 实启，~35min）。验收通过：门禁由调度器亲跑复核（contract 44 passed/全量 76 passed/ruff check+format/mypy 15 文件全绿）；8 fixtures=2 正例+6 反例；round-trip 与 py.*/version=1 原始可检；文件与授权清单一致（CORE-CONTRACTS.md 未提交修改属 A 席 T-A-002 并行任务，diff 已核非 C 越权）。亮点：C-1 在切片中途消化了 A 合入的 T-A-002 裁决（config_snapshot 精确形状/sha256 canonical/解码重算摘要/NaN 与重复键拒绝），A×C 并行对齐自动完成；自报 8 项方案审视发现+6 项未决问题，转 B 审查回应。检查点#7 提交（feat: R1-S1 contracts，不含 A 在飞契约修订与 logs/）；T-B-002（B 结构化审查）派发；监视器刷新为 A+B 两日志。
- 14:59 T-A-002 回报"完成"（14:21 实启，~38min）。验收通过：4 项裁决全部落为正式条款无 TBD——§6.1 R1 最小快照精确形状（schema_version=Literal[1]+permission_rules=[] 唯一合法对象）、§6.2 canonical JSON+SHA-256+摘要常量钉死（77c932c3…f802）、§3.2 H/A 水位与 expected_seq 语义（A≠H=StorageCorrupt 而非 Conflict）、§3.2.1 只读诊断裁决+§4 read/follow 坏页 StorageCorrupt 边界、§7 prompt 固定顺序（状态门禁→幂等→busy）+RunRef 三字段；每项附 TS 行号引用或"TS 无对应物+Python 侧论证"；changelog 行在；§9 逐条 B 草案冲突对照+最终口径；git 仅修改 CORE-CONTRACTS.md（+80/-18）。交叉核对：C-1 已提交 R1-S1 实现与 §6.1/§6.2 最终口径逐项一致（无内嵌 hash/Literal[1]/空规则强制/摘要常量相同/decode 重算）——A×C 对齐闭环，无需修订任务。同刻补正：T-B-002 实际派发（上轮 commit 文案已声明但进程未发，调度器失误已纠正）。检查点#8 提交；监视器刷新为 T-B-002 日志。
- 15:07 T-B-002 回报"完成"（14:59 实启，~8min）。验收通过：结论**通过**（0 blocker/0 major/4 minor/3 nit）；发现逐条带严重度+位置+理由+建议；C-1 六项未决问题逐项回应；git 仅新增 doc/reviews/R1-S1-review.md。B 复验扎实：独立重跑 44 测试+ruff、stdlib 独立复算摘要常量、实测 EventDraft(version=True) 探出 M-1 bool 穿透。**试点全流程闭环达成**（派发→实现→验收#7→A 裁决#8→B 审查通过）。后续分派：T-A-003（A 裁决 M-2 files/M-3 extra-forbid 兼容路线/ID 前缀/bool-int 约定/JSON Schema 归属/Ephemeral 时点 + parity 两行）∥ T-B-003（B 按审查 §4 九条修订 r1-slices.md，A 待裁项按 B 建议预写并标注）；两者回报后→T-C1-002（contracts 修补批次：M-1/M-4/N-2+裁决落地）→ 建 worktree → T-C1-003（R1-S2）。检查点#9 提交；监视器刷新为 A-003+B-003。
- 15:17 用户确认停止节奏：**维持现状**（自主推进到 R1 退出条件；复估触发/升级链/网络故障自动暂停，R2 前天然停找用户要 MAAS 凭据；用户随时可手动喊停=无损冻结在最近检查点）。同刻观察：B 席已在修改其授权文件 doc/plans/r1-slices.md（T-B-003 实际开工证据）。
- 15:22 T-B-003 回报"完成"（15:14 实启，~8min）。验收通过：九条修订逐条核对落实——①S2 依赖更正（仅 alembic/sqlalchemy/aiosqlite，pydantic 前移注记）②S1 交付追认（ConfigSnapshot/canonical_json/compute_snapshot_hash/RUN_SCOPED_EVENT_TYPES/22 错误码全锁/8 fixture/摘要常量钉死测试）③files 按 B 建议 (a) 预写并标"待 T-A-003 终裁"（含备选 (b)）④S3 并入 H/A 水位不变量+写入门禁先于幂等+§3.2.1 隔离入口归 S3/全量检测归 S8+水位/隔离测试点⑤S6 坏页语义七项断言（整页 StorageCorrupt/无半页/next_cursor 不推进/cursor 回退/follow 保健康后终止/H=日志水位/details 脱敏）⑥S7 四步固定顺序+RunRef 三非空字段+ID 生成器对齐 TS ascending() 注记（前缀强制待裁）+accepted 占槽/close 只释放自有资源⑦S8 rebuild 同事务全验证/失败回滚含清空/诊断标记跨 reopen 保留/诊断期禁恢复补写⑧§3 差异表补第 7-9 三行⑨开放问题 #1-#4 标已决引 CORE-CONTRACTS §9、新增 #5 待 T-A-003 三连、原 #5-#7 顺移 #6-#8 并更新估算（R1 目标 5 人日/上限 6）。文件头 [T-B-003] 修订行在；git 仅改 r1-slices.md（+44/-30）；过时行号引用已替换为章节引用。检查点#11 提交；监视器刷新为仅盯 T-A-003。
- 15:44 T-A-003 回报"完成"（15:14 实启，~30min）。验收通过：7 项全落无 TBD——①files=方案 (b)（§2.1：R1 v1 字段全集 id/type/created_at/metadata/text，任何 files 键含空列表按未知字段拒绝，R2+ 升版本引入；附 session-message.ts:76-84+Python-only 论证）②extra-forbid 封闭演进（§1+§8：新增字段一律升版本+upcaster，不走同版本可选字段；附 event.ts:28-32,174-187/bus.ts:74-88）③四前缀模型层强制（§3.1：evt_/msg_/ses_/run_ pattern，适用 draft/durable/ephemeral/typed data/嵌套/Ref/游标，metadata 保留 run_id 键同样受 pattern；request_id 仍只非空；附 event.ts:9-14/session-message.ts:21-29/session-id.ts:5-11）④bool-int 仓库级规则（§1：所有整数语义字段 before-validator 拒 bool）⑤JSON Schema 归 R5（各切片 fixture 锁形状）⑥Ephemeral 生产注册留 R2⑦parity 两行（details 恒出现 {}、异常家族细化附 bus.ts:60-66,78-90）。changelog [T-A-003] 行+§10 实施交接在（M-2 选 b、无方向分歧、ses_ 收紧与 run_ 专有已明示）；git 仅改 CORE-CONTRACTS.md（+30/-9）+parity.md（+3）。与 B 预写两处不同（files 选 b≠a、request_id 不强制前缀≠B 清单含 req_）→ 派 T-B-004 微调；T-C1-002（M-1 bool 守卫/M-4 反例/N-2 断言+ID pattern+files 钉住）并行派发。检查点#12 提交；监视器刷新为 B-004+C1-002。
- 15:56 T-B-004 回报"完成"（15:48 实启，~8min）。验收通过：三处预写对齐终裁——①S1 files 行改终裁方案 (b)（字段全集精确、files 键含空列表按未知字段拒绝、新输入=ValidationError/已存日志=损坏隔离、R2+ 升版本+upcaster、现实现 e61cdd5 已合规无需改动，引 §2.1/§8/§10）+补【实施交接】行记 C-1 补丁范围；②S7 ID 注记改四前缀强制+request_id 例外（引 §3.1/§10，含 TS 三处单点依据+run_ Python 论证），RunRef 表述同步、step 1 补入参 pattern 校验并修正状态名笔误 closing→cleanup_failed（对齐契约 §7.1 原文）、step 4 补三类生成 ID pattern 要求；③§4 #5 标已决（含 B 预写与终裁两处差异说明：req_ 剔除、files (a)→(b)），§4 标题改"全部已决无待决项"；另 §3 差异表补第 10 行（四前缀+bool 守卫的 TS 单点 vs Python 全量差异）。调度器 Select-String 复核全文零"待 T-A-003"残留；文件头 [T-B-004] 行在；B 自身产出仅 r1-slices.md（git status 中 contracts 两文件为 C-1 并行在飞补丁，非 B 产出，与 §10 交接一致）。检查点#13 提交；监视器刷新为仅盯 T-C1-002。B 建议补丁完成后照常走 B 快速结构化审查再进 S2——采纳（记 T-B-005）。
- 16:02 T-C1-002 回报"完成"（15:48 实启，~14min）。验收（有条件）：四门禁调度器亲跑复核全绿（89 passed 13.11s/ruff check/format 50 files/mypy 15 文件），与自报一致；逐项核对——M-1 ✓（`_reject_bool` 统一 helper 接 EventDraft.version/DurableEvent.seq/ConfigSnapshot.schema_version 三处）、§3.1 四 pattern ✓（EventDraft/EphemeralEvent id ^evt_+session_id ^ses_、UserMessage.id ^msg_、PromptAcceptedData.run_id ^run_、request_id 仅 min_length 无 pattern 且有反例钉住）、`_require_run_metadata` 扩展 ✓（run_id 键出现即校验前缀，null/非串/错前缀均拒）、M-4 ✓（version:"1" 解码拒/seq:True 构造拒/surrogate fail-closed 三测）、files (b) ✓（files: []/[...] 键拒绝测试+docstring 更新）、fixtures 零改动 ✓、git 仅授权路径 ✓。唯一缺口：N-2 首项 EventDraft(id="") 拒绝无直测（text="" 允许已钉）→ 派 T-C1-002B 微补（单测一条）。C-1 遗留三问（①构造路 version="1" pydantic lax 强转 vs 解码路拒绝的不对称是否收紧；②surrogate 当前值域不可触发、未来加字符串字段建议显式前置扫描；③metadata.run_id"出现即校验"比 run-scoped 更严——与裁决字面一致）转 T-B-005 审查回应，必要时升 A。补丁暂不提交，随 002B 一并落检查点#15。检查点#14 提交（台账+002B 任务包）；监视器刷新为 T-C1-002B。
- 下一事件：T-C1-002B 回报 → 四门禁复核 → 检查点#15（contracts 补丁+测试+进度+台账）→ 派 T-B-005（B 对补丁的快速结构化审查，含 C-1 三遗留问）→ 通过后建 C-1 worktree → T-C1-003（R1-S2：Alembic 0001+引擎打开，worktree 内）→ S3 起按依赖序。

## 待用户定夺（不阻塞冒烟）

1. B 是否换 `gateway/deepseek-v4-pro#max` 做异源评审（默认：不换，维持 GLM-5.3#max）
2. A 派发是否按次加 `-s workspace-write` 收紧沙箱（默认：不动用户全局配置）
