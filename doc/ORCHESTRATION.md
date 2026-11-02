# ORCHESTRATION — TS→Python 三角色调度台账

> 唯一调度事实来源（调度器维护，各席位只读）。
> 裁决基准：一切问题以 opencode 原始 TS 实现（`../opencode-2.2.2/`，指纹见 `doc/source-baseline.json`）为基准解决。
> 方案 v1 已获用户批准（2026-11-02）。

## 1. 席位绑定与派发模板

| 席位 | 角色 | 命令 | 模型 | 会话续接 |
|---|---|---|---|---|
| A | 仲裁者：架构定稿/契约裁决/疑难根因/连续失败升级/高风险终审/沉淀规范 | `codex exec`（0.163.0） | gpt-6.1-sol · reasoning max（本机 codex config，不覆盖） | `codex exec resume <sid>`（sid 从 --json 事件流捕获） |
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

- 名义 deadline（试点后校准）：冒烟 ~10min｜审查 ~15min｜方案 ~20min｜复估定稿 ~25min｜实现 30–60min
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
- 暂停点（等用户确认，grilling ≤3 问、单主题、逐层推进；争议默认以 TS 原始实现裁决）：① A 复估定稿后 ② 高风险模块 A 终审后 ③ 升级事件处置后 ④ 里程碑完成时

## 6. 任务表

| ID | 席位 | 任务 | 启动 | 名义deadline | 状态 | 产出 |
|---|---|---|---|---|---|---|
| T-PROBE-A | A | 冒烟测试 | 11:33 | ≈11:43 | 派发中（事件流活跃） | doc/orchestration/probe/seat-A.md（待） |
| T-PROBE-B | B | 冒烟测试 | 11:37 重派 | ≈11:47 | 重派中（首派参数错，见日志） | doc/orchestration/probe/seat-B.md（待） |
| T-PROBE-C1 | C-1 | 冒烟测试 | 11:37 重派 | ≈11:47 | 重派中（同上） | doc/orchestration/probe/seat-C1.md（待） |
| T-PROBE-C2 | C-2 | 冒烟测试 | 11:37 重派 | ≈11:47 | 重派中（同上） | doc/orchestration/probe/seat-C2.md（待） |

## 7. 调度日志

- 11:32 名册确认（A=codex/GPT-6.1-Sol max；B、C-1=opencode/GLM-5.3#max；C-2=opencode/DeepSeek-V4.1-Flash#max）。codex 0.163.0 `exec`/`resume`/`--json`/`-o` 验证通过；opencode `run --session/--model/--auto` 验证通过。台账建立（含网络恢复协议）。四席位冒烟并行派发（B/C1/C2 初用 orch-seat-* 会话ID）。
- 11:36 B/C1/C2 首派即败（退出码 1）：`--session` 值必须以 `ses` 开头——客户端参数校验错误，秒败、无模型调用、非网络故障。会话 ID 更正为 `ses_orch_B/C1/C2` 重派。A（codex）事件流确认存活。用户指示：git 提交与版本控制唯一归调度器，固化入 §1。检查点#1 提交。
- 下一事件：四席冒烟验收 → T-A-001（R0 复估）∥ T-B-001（R1 切片草稿）→ 暂停点①（grilling）。

## 待用户定夺（不阻塞冒烟）

1. B 是否换 `gateway/deepseek-v4-pro#max` 做异源评审（默认：不换，维持 GLM-5.3#max）
2. A 派发是否按次加 `-s workspace-write` 收紧沙箱（默认：不动用户全局配置）
