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

- 名义 deadline（试点后校准；实测：方案 7min、复估 38min）：冒烟 ~10min｜审查 ~15min｜方案 ~20min｜复估定稿 ~40min｜实现 30–60min
- 巡检时机：任一席位回报事件、每次门禁、用户喊"查状态"
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
| T-A-002 | A | R1 契约裁决（4 项落条款） | 14:20（resume 续接） | ≈14:50 | 派发中 | doc/CORE-CONTRACTS.md（修订） |
| T-C1-001 | C-1 | R1-S1 试点：最小 contracts+fixture | 14:20 | ≈15:05 | 派发中 | src/opencode/contracts/、tests/contract/ |

## 7. 调度日志

- 11:32 名册确认（A=codex/GPT-6.1-Sol max；B、C-1=opencode/GLM-5.3#max；C-2=opencode/DeepSeek-V4.1-Flash#max）。codex 0.163.0 `exec`/`resume`/`--json`/`-o` 验证通过；opencode `run --session/--model/--auto` 验证通过。台账建立（含网络恢复协议）。四席位冒烟并行派发（B/C1/C2 初用 orch-seat-* 会话ID）。
- 11:36 B/C1/C2 首派即败（退出码 1）：`--session` 值必须以 `ses` 开头——客户端参数校验错误，秒败、无模型调用、非网络故障。会话 ID 更正为 `ses_orch_B/C1/C2` 重派。A（codex）事件流确认存活。用户指示：git 提交与版本控制唯一归调度器，固化入 §1。检查点#1 提交。
- 11:40 B/C1/C2 冒烟验收通过：回报行（PROBE-*-OK）、交付文件（seat-*.md，R1 定义正确）、git 无异常变更，三项全过。A 冒烟进入收尾（seat-A.md 已产出，codex 用 python 自检通过：单行『R1：最小事件/恢复闭环』）。观察：席位日志开头有一条非致命 stderr 记录（"Expected a string starting with ses"，PS 2>&1 重排所致），未影响结果，持续观察。T-B-001 派发至 B（GLM-5.3#max，按默认模型）。检查点#2 提交。
- 11:44 A 冒烟验收通过（PROBE-A-OK / seat-A.md / git 干净），四席冒烟全过。A 线程 ID 捕获并记入 §1。（勘误：本行及 11:40 行所记 T-A-001/T-B-001 派发与检查点#2 提交均未实际发生——调度器在启动进程前会话中断。观察：A 读中文文件正常，其 stdout 捕获存在控制台编码噪声（锟斤拷，仅显示层，文件内容无损）。）
- 11:50 用户指示：grilling 提问上限 ≤3→≤5 问（§5 及全部暂停点生效）。断点恢复：T-A-001 经 `codex exec resume` 派发（实测续接机制）、T-B-001 经 `opencode run --session ses_orch_B` 派发；启动 11:50，名义deadline A≈12:15 / B≈12:10；检查点#2 提交（台账修订+任务包+冒烟证据，排除在飞 logs/）。
- 11:57 T-B-001 回报"完成"（用时 ~7min，远低于名义 20min）。验收通过：依赖序明确（S1→S2→S3→S4→[S5 可并行]→S6→S7→S8）、试点切片 R1-S1 标注+粒度 30–45min、逐切片 TS 文件+行号映射、验收标准逐条归属 PHASE1-SLIM R1 验收 A/B/C+测试计划齐全；git 仅新增 doc/plans/r1-slices.md。开放问题 4 项（R1 prompt 语义/config_snapshot 最小形状与 hash/未知事件版本读取路径 fail-closed vs 读取可用/expected_seq 是否即 applied_seq）提请与 A 汇合裁决——暂停点① 素材。检查点#3 提交。
- 12:28 T-A-001 回报"完成"（11:50 启动，用时 ~38min，超名义 25min——通知驱动下调度器无独立定时器，回报到达时任务已完成，无需 kill/恢复；名义值校准至 ~40min，见 §3）。验收通过：五节齐全（复估结论/R1 定义确认+退出项/契约修订 3 项均附 TS 行号引用+Python-only 边界标注/R1–R5 基线表目标 24 上限 30 人日/高风险 A-S4·A-S6·A-RECOVERY·A-PLATFORM·A-PROVIDER 终审标记）；抽查一致：doc/evidence/r0/ 三文件+fixtures 在盘、source-baseline 实测 20 项（任务包误写 21，A 已修正）、R0 测试 26 函数/参数化 32 用例与回报相符、platform-support.md 含所引 CI run 41182347042、git 仅新增 reestimate-r0.md。汇合要点：R1 定义维持 PHASE1-SLIM §3 不变；R0-A 条件通过（真实 MAAS smoke 凭据缺，不阻塞 R1）；B 开放问题中"未知版本读取路径"A 已有取向（只读诊断+已投影内容可读），余 3 项（R1 prompt 语义/config_snapshot 最小形状与 hash/expected_seq 是否即 applied_seq）待 A 书面裁决；B 纯实现 1.5–2.5 工作日 vs A 基线 R1 5 人日——口径不同（C 循环 vs 全流程），以 A 基线为进度对照、B 口径为 C 派发节奏。检查点#4 提交。
- 14:20 暂停点① 用户全部同意（5/5；答复于 12:35–14:20 间到达，调度器 14:20 续接处理）：①接受 A 基线 R1–R5 目标 24/上限 30 人日（超目标 30% 触发复估）②prompt() 保留在 R1（仅持久化 py.prompt.accepted+返回 RunRef，不调度）③真实 MAAS smoke 凭据推迟至 R2 前单独补齐 ④R1-S1 试点批准、主树直做（纯新增文件）、S2 起切独立 worktree ⑤技术性契约裁决授权 A 直接落条款。据此派发：T-A-002（4 项裁决落 CORE-CONTRACTS，30min 名义）、T-C1-001（试点 R1-S1，45min 名义）并行。调整：pydantic 依赖引入由 S2 前移至 S1（S1 需 Pydantic v2 wire model，B 方案边界笔误；已授权 C-1 仅经 `uv add "pydantic>=2"` 修改 pyproject/uv.lock）。检查点#5 提交。
- 下一事件：T-A-002 / T-C1-001 回报 → 各自验收 → 裁决×实现交叉对齐（冲突则派 C 修订）→ B 审查 R1-S1（T-B-002）→ 通过后按 S2 起（worktree）依赖序派发。

## 待用户定夺（不阻塞冒烟）

1. B 是否换 `gateway/deepseek-v4-pro#max` 做异源评审（默认：不换，维持 GLM-5.3#max）
2. A 派发是否按次加 `-s workspace-write` 收紧沙箱（默认：不动用户全局配置）
