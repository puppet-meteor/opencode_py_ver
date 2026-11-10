# 多 Agent 协同调度方案（Multi-Agent Orchestration Guide）

> 来源：OpenCode TS→Python 迁移项目（`D:\26_code\opencode_py_ver\opencode-2.2.2-py`，2026-11-02）实战沉淀。
> 用途：任何"一个调度者驱动多个 AI agent 席位协同完成软件工程任务"的场景，可直接按本文档复制搭建。
> 实测规模：单调度器会话 + 4 个 agent 席位（A/B/C-1/C-2），30+ 检查点，3 任务并行派发，全周期无人值守。

---

## 0. 一句话方案

**单一调度器（唯一 git 写权、唯一状态机）+ 三角色席位（仲裁/规划评审/实现）+ 文件协议（任务包→进度→审查→台账）+ git 检查点链 + 看门狗监控。**
席位之间**不直接通信**：一切经仓库文件与 git 提交；调度器靠后台命令完成自动唤醒驱动整个循环。

## 1. 角色与席位

| 席位 | 角色 | 模型/CLI | 会话策略 | 典型任务 |
|---|---|---|---|---|
| 调度器 | 编排+验收+唯一 git 写权 | 宿主 agent（本文档读者等价物） | 单会话贯穿全程 | 派发/验收/提交/监控/裁决 |
| A | 仲裁者 | codex CLI（GPT-6.1 max） | `codex exec resume <固定id>` | 契约终裁、争议裁决 |
| B | 规划评审 | opencode + GLM-5.3#max | `ses_orch_B` 固定会话 | 切片方案维护、结构化代码审查 |
| C-1 | 实现者（关键路径） | opencode + GLM-5.3#max | **每 worktree 新会话** | 切片实现（代码） |
| C-2 | 实现者（机械/批量） | opencode + DeepSeek-V4.1-Flash#max | `ses_orch_C2` 固定会话 | 滚动盘点、批量测试、夜间回归 |

选型逻辑：**贵模型跑关键路径与裁决，便宜模型跑机械面**（轮换经济学见 §9）。
A 席按需启用（低频高价值）；B/C 为常飞席位。

## 2. 核心纪律（六条，全部实测必要）

1. **git 提交唯一归调度器**——席位只改工作区文件，任务包明文"禁止 git 写操作"（只读 git 允许）。每个提交=一个编号检查点，链式记录完整审计轨迹。
2. **争议以基准裁决，不投票**——基准=契约文档（`doc/CORE-CONTRACTS.md`）+ 原始实现（TS 基准）。席位发现矛盾只上报（"未决问题"通道）、按最保守口径实现并标注，**不自行裁决**；裁决归 A 或调度器按契约文本直接关闭（实测：15 项未决问题全部由契约行号级依据关闭，0 升 A）。
3. **产出必落仓库文件**——席位回报正文可能丢（日志显示层乱码/数字漂移实测发生过），一切以文件为准；回报数字与文件不符时以文件为准。
4. **任务包走文件**——每次派发=一个 markdown 任务包；席位第一句指令恒为 `Read the file <任务包> and execute the instructions in it.`
5. **声明派发≠已实发，验收不采信自报**——派发后 30 秒自检（§7.3）；验收时门禁亲跑、代码亲读（实测抓到过席位计数假阳性与文档-代码不符）。
6. **止损前置**——任务包写明止损线（"预计超 X 分钟→立即停止→已完成部分写进度文件→回报部分完成"），防席位无限烧钱。

## 3. 文件协议

### 3.1 仓库目录结构

```
doc/ORCHESTRATION.md                  # 台账（唯一事实来源）：任务表+调度日志+下一事件
doc/orchestration/tasks/T-*.md        # 任务包（调度器写，随检查点提交）
doc/orchestration/progress/T-*.md     # 席位进度文件（席位写：改动清单/门禁结果/未决问题）
doc/orchestration/logs/*.txt          # 席位运行日志（stdout 重定向，不提交）
doc/reviews/*.md                      # 审查报告（B 席产出）
doc/plans/*.md                        # 切片方案（B 席维护）
doc/CORE-CONTRACTS.md                 # 契约（争议裁决基准）
```

### 3.2 任务包解剖（可直接复制的模板）

```markdown
# T-<席位>-<序号> · 席位X：<一句话任务>

【角色章程】（角色定位/工作目录/上下文背景一段）
- 写权限（显式授权白名单——白名单外禁止修改任何文件）
- 禁止 git 写操作（只读 git 允许）；提交由调度器负责
- 止损指令（预计超过 X 分钟→停止→写进度文件→回报"部分完成"）
- 门禁（全部自跑全绿才算完成；附基线数值）
- 防截断纪律（增量写文件）；前台纪律（门禁勿放后台等待）

【背景】（前置切片状态、关联文件、审查遗留项）

【任务】（编号步骤；授权路径逐条列出；
        契约矛盾→写进度文件"未决问题"，按最保守口径实现并标注）

【产出】（文件清单）

【验收 checklist】（调度器逐条机器复核项）

【回报格式】（正文 1-2 段：状态/改动清单/门禁四行结果/未决问题）
```

关键设计：
- **写权限白名单是硬边界**——验收时 `git status` 必须恰好等于授权清单，多一个文件即拒收；
- **"未决问题上浮"通道**——席位不越权自裁，矛盾集中到审查/裁决环节，切片边界保持干净（实测：未决问题→审查逐项意见→一行级修复微任务，闭环不返工）。

### 3.3 台账（ORCHESTRATION.md，唯一事实来源）

三段式：

```markdown
## 任务表
| 任务 | 席位 | 内容 | 派发时间 | 监控方式 | 状态 | 产出 |

## 调度日志
- HH:MM <事件描述：数据+依据+处置>（每条=一次派发/验收/故障/裁决）
- 下一事件：<滚动维护的下一步计划，每轮覆盖>
```

纪律：调度日志每条带**数据与依据**（如"119 passed 27.07s""契约 L163 字面"），事后可审计；崩溃/换会话后靠台账无损恢复。

## 4. 调用命令模板（Windows/PowerShell 实测）

### 4.1 B/C 席（opencode CLI）

```powershell
# 后台实发 + 日志重定向（workdir 决定席位的工作目录）
opencode run --model "gateway/glm-5.3#max" --session "ses_orch_B" --auto `
  "Read the file doc/orchestration/tasks/T-B-008.md and execute the instructions in it." `
  > "doc/orchestration/logs/T-B-008.txt" 2>&1
```

- `--session` 值**必须以 `ses` 开头**（客户端参数校验，否则秒败 exit 1、无模型调用——实测踩坑）；
- 会话策略：**B/C-2 固定会话**（上下文累积，越用越顺手）；**C-1 每 worktree 新会话**（切片隔离，防跨切片上下文污染）；
- `--auto` 免交互；后台运行（宿主 shell 的 background 模式，完成时自动唤醒调度器）。

### 4.2 A 席（codex CLI）

```powershell
codex exec resume <session-id> --json -o "<final.txt>" "<指令>"
```

- `resume` 无 `--cd` 参数——**靠 shell 的 workdir 定位工作目录**；
- `--json -o` 落盘事件流，结论从 final 文件取（stdout 是 UTF-16 事件流，显示层乱码属噪声）。

### 4.3 会话续跑（故障恢复的关键能力）

同一 `--session` 再次 `opencode run` = **带全部历史上下文继续**，用于：
- 模型输出限额中断（exit 1 + `Response still truncated after two output token limit continuations`）→ 续跑指令补一句"write files incrementally rather than emitting very long tool arguments in a single response"；
- 席位提前结束回合未收尾（实现落盘但门禁/进度未交）→ 续跑指令补"run the gates in the foreground, finish, and give the final report"。

两条续跑指令模板都实测救活过任务，**无需从头重派**。

## 5. 隔离与并行

### 5.1 worktree 隔离（代码任务）

```powershell
# 调度器建 worktree（基线=当前检查点，任务包随之对席位可见）
opencode worktree_create --name r1-s4     # → C:\Users\lyu\.local\share\opencode\worktree\<hash>\r1-s4
# （等价：git worktree add）

# 席位在 worktree 内工作（干净基线，git status 即授权面审计）
# 验收通过 → worktree 内 switch -c <同名分支> 提交（=检查点）→ B 审查 → 通过 → merge 回主树
# merge 后 force 清理（含 .venv）
```

- **每切片独立 worktree+独立分支**：多实现任务可并行、失败整体丢弃、审查在分支上进行不污染主树；
- worktree 分支需要主树新文件（任务包/审查报告）入席位视野时：调度器在 worktree 内 `git merge main`（实测无冲突——任务包不与席位改动交集）；
- 清理注意：服务进程 CWD 锁可能残留 0 字节空目录壳（无害，进程重启自消）；**不杀在飞服务进程**。

### 5.2 并行派发模式（实测三任务并行）

```
代码任务（worktree r1-s3 内 C-1）
  ∥ 文档任务（主树 B：方案维护）
  ∥ 盘点任务（主树 C-2：API inventory 滚动更新）
```

- 选任务时保证**文件集零交集**（各自 git status 独立可审计）；
- 每席位独立日志文件+独立进程；一个 WATCHER 可盯多个日志；
- 先回先验，检查点号按实际回报顺序顺延（台账记录真实时序）。

## 6. 派发-验收循环（单任务生命周期）

```
写任务包 → 台账登记+检查点提交
→ （代码任务：建 worktree → 验基线[HEAD=检查点、任务包可见、干净]）
→ 后台实发 → 杀旧 WATCHER/挂新 WATCHER → 30 秒自检
→ 等回报（后台命令完成自动唤醒；宿主不支持则 WATCHER 报警人工触发）
→ 验收：
    ① 门禁四件亲跑（pytest / ruff check / ruff format --check / mypy）
    ② 代码亲读，逐项对照任务包 checklist
    ③ git status 恰等于授权白名单
→ 台账落验收（数据+依据）+检查点提交
→ 后续：B 审查 → 通过 → merge → post-merge 门禁复核 → 下一切片
```

门禁口径：**format 类工具以退出码为准**（"N files formatted"计数随扫描集合漂移，不作验收口径——实测 61→81 漂移排查后定案）。

## 7. 监控与故障处置

### 7.1 WATCHER（卡死检测，后台 PowerShell 循环）

```powershell
$files = @('doc/orchestration/logs/T-X-001.txt')   # 可多文件
$end = (Get-Date).AddHours(6)
while ((Get-Date) -lt $end) {
  Start-Sleep -Seconds 600
  $stuck = @()
  foreach ($f in $files) {
    if (Test-Path $f) {
      $it = Get-Item $f
      if (((Get-Date) - $it.LastWriteTime).TotalMinutes -gt 60) { $stuck += ($f + ' stale since ' + $it.LastWriteTime) }
    } else { $stuck += ($f + ' MISSING') }
  }
  if ($stuck.Count -gt 0) { 'STUCK-ALERT-<N>'; $stuck; exit 0 }
}
'WATCHER<N>-REHANG-6H'    # 唯一标记：kill 时按命令行过滤定位本进程
```

- 判卡死=日志 1h 零增量（席位正常工作会持续写日志）；
- **杀法**：`Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '*WATCHER<N>-REHANG-6H*' } | Stop-Process -Force`（exit 255=正常被杀）；
- **换班纪律**：任务完结/换任务必须杀旧挂新（盯已冻结日志必然误报）——编号单调递增（WATCHER1→17…）。

### 7.2 故障分类与处置（全部实测）

| 故障 | 特征 | 处置 |
|---|---|---|
| 模型输出限额 | exit 1 + "Response still truncated..." | 同会话续跑+指令加"增量写文件"；再败上报用户 |
| 回合未收尾 | exit 0 但进度/门禁未交 | 同会话续跑+指令加"前台跑完再回报" |
| 会话 ID 非法 | 秒败 exit 1，无模型调用 | 改 `ses` 前缀重发 |
| 网络故障 | 连接类错误 | 重试 1 次；再败上报用户 |
| 同任务 2 次卡住 | WATCHER 连续报警 | **升 A 席**接手（换更强模型/调任务粒度） |
| worktree 清理被锁 | Remove-Item IOException | 不杀在飞服务；内容清掉即可，空壳无害 |

### 7.3 30 秒自检（每次派发后必做）

```powershell
Start-Sleep -Seconds 30
# ① WATCHER 进程存活？（Get-CimInstance 过滤标记）
# ② 席位进程在跑？（opencode.exe / codex.exe）
# ③ 日志已落墨？（size>0 且 lastwrite=刚刚）
```

三项全绿才算"已实发"；任一红立即处理（重发/换会话/查进程）。

## 8. 审查与质量门（三角色流水线）

```
C-1 实现（worktree 分支提交）
   → B 结构化审查（审查任务包模板同 §3.2；产出 doc/reviews/*.md）
      结论：通过 / 有条件通过（条件=修复项）/ 不通过
      发现分级：blocker / major / minor / nit + 位置 + 理由 + 建议
      未决问题逐项 B 意见（需升 A 的标注）
   → 调度器验收审查（亲验 B 的行号级主张——实测 B 主张抽验 100% 属实，仍要验）
   → 通过 → merge 主树 → post-merge 门禁复核
   → 有条件通过 → 授权修复微任务（同席位同会话续跑，最小改动+直测）→ 复验 → merge
```

- 审查基准=契约+TS 原始实现+切片方案（三重）；
- **修复微任务模式**：审查发现的缺陷 → 单独小任务包（写权限收到具体文件具体处数）→ 同席位续跑（对代码最熟）→ 门禁复验 → merge。切片边界干净，不返工整个切片。

## 9. 席位轮换经济学（C-2 席实证数据）

低价席位纳入流程的评估判据（每项都要有数据）：

| 维度 | 判据 | C-2 实测（首派→二派） |
|---|---|---|
| 质量 | checklist 一次全过、返工 0 | 19 符号零偏差 → 15+ 符号零偏差 |
| 调度开销 | 任务包+验收 ≤30min | ~15min → ~9min（模板化后下降） |
| 耗时 | vs 高价席位同型任务 | 7min → 2min |
| 主线增益 | 产出被后续任务实际引用 | inventory 被 S4 任务包引用（首证） |

**结论规则**：一次过+被引用 → 常驻轮换（C-2 定稿角色=滚动盘点员：每切片并回后增量滚动 API inventory）；返工≥1 或开销>30min → 退回夜间批量专用。

## 10. 复制这套方案的最小清单

1. **可脚本化的 agent CLI**（支持无交互 `run --session` 式调用+后台运行+stdout 重定向；能固定/新建会话）——不限于 opencode/codex，任一满足三条件的 CLI 均可；
2. **一个 git 仓库**（worktree 隔离+分支检查点+merge 回收）；
3. **四份协议文件**：台账（ORCHESTRATION.md）/ 任务包模板（§3.2）/ 进度文件约定 / 审查报告约定；
4. **一段 WATCHER 脚本**（§7.1）+ 30 秒自检习惯（§7.3）；
5. **（强烈建议）一份契约文档**作为争议裁决基准——多 agent 的分歧不靠商量，靠基准文本+行号。

## 11. 本实例检查点链样例（片段，未 push 本地链即完整审计轨迹）

```
#23 三任务包并行派发 → #24 B 方案维护验收 → #25 C-2 首派验收
→ #26 故障记录（输出限额，同会话续跑）→ #27 worktree 分支提交（S3 实现）
→ #28 台账+审查派发 → #29 审查验收 → #30 merge S3 → #31 S4 任务包
→ #32 旧 worktree force 清理 → #33 C-2 二派 → #34 盘点验收
→ #35 worktree 分支提交（S4 实现）→ #37 审查验收+门禁微任务派发 → …
```

## 12. 已知坑（Windows/工具链实测）

- `--session` 必须以 `ses` 开头（客户端校验）；
- 席位日志 stdout 显示层乱码（UTF-16 重排）——**以产出文件为准**，日志只看结构不抠字；
- PowerShell `Select-String` 默认**大小写不敏感**——统计正则要显式 `-CaseSensitive`（调度器与席位都踩过）；
- ruff format 计数漂移与文件集合无关——门禁以退出码为准；
- opencode serve 服务池 worker 的 CWD 会锁 worktree 目录——清理留 0 字节空壳，不杀服务；
- `codex exec resume` 无 `--cd`——用 shell workdir 定位；
- 后台命令完成自动唤醒调度器是宿主（opencode）能力——无此能力的宿主用 WATCHER 报警+人工续跑替代。

---

*文档由调度器会话生成于 2026-11-02 20:36；对应项目台账见 `D:\26_code\opencode_py_ver\opencode-2.2.2-py\doc\ORCHESTRATION.md`。*
