# R2-S8 收口评审报告（席位B · 结构化评审）

- **日期** 2026-11-04 · **评审人** 席位B（规划评审） · **任务** T-B-025
- **评审对象** worktree `r2-s9`（分支 r2-s9；单提交 `971fa37`，父=main 检查点#97 `de95bb9`；3 文件全新 +537/−0；src/ 红线零触碰、既有测试零改动——`git diff 971fa37^..971fa37 --stat` 自证）
- **评审范围** T-C1-024 三交付件：`tests/integration/test_e2e.py`（402L）+ `doc/reference/r2-exit-evidence.md`（79L）+ `doc/orchestration/progress/T-C1-024.md`（56L）
- **基准** `doc/plans/r2-slices.md` §2 L146-154（S8 全文）+ `doc/PHASE1-SLIM.md` §4 L76-85 + `doc/orchestration/tasks/T-C1-024.md` + `doc/reference/r1-exit-evidence.md`（模式模板）+ `doc/CORE-CONTRACTS.md` §4/§5.1/§7/§8（基准读 #97 态，被审三文件读 971fa37 态）
- **门禁依据（调度器亲跑，免复跑）** pytest **651 passed**（648 基线零回归 + 3 新增，155.38s）/ ruff check 全过 / format --check exit 0（209 files）/ mypy **66 files 0 errors**
- **纪律** git 零写（除本报告）；止损 45min 未触界

## 总裁决：**通过**

计数：blocker **0** / major **0** / minor **1** / nit **0**（m-1 为证据文档一处继承行号错位，一行修复，不阻塞合并——建议调度器随合并顺手改或并入 R3 首项）。五面全部成立；两项点名复核均 concur（独立给据，见 §2）。

## 1. 五面逐项结论

### 面1 · e2e 忠实度与强度（test_e2e.py）—— 通过（零发现）

**验收 A（L80 原文）逐要素对照全部落测**：

- "多轮文本"**两读并防真落**：会话轮读法=3 轮各自独立 prompt→独立 `run.finished`（EventLog 计 3）；多段流读法=第 2 轮脚本 2 段 delta（"seg one "+"seg two"）→finalized **单 part** 逐字断言（`_assistant_text(assistants[1])=="seg one seg two"` 且 `parts==["text"]`）。
- "tool call → read 结果 → 第二次生成 → run.finished"：scripted tool_calls→真实 `fs__read` 读 tmp 树自建 note.txt→渲染页**逐字回灌** role:"tool"（`"Read file note.txt, lines 1-2\n1: alpha line\n2: beta line"`）→第二次生成=**独立 provider 请求 requests[3]**→run.finished。
- "provider role/tool_call_id 配对正确"：**wire+store 双侧逐对**——store 侧 `tool_parts` id==`["call_read_note"]`；wire 侧 `tool_messages[0].tool_call_id`、`wire_calls` id/name=="fs__read"/arguments==`{"path":"note.txt"}` 逐项相等。
- 终态经 **run_status 读模型**（OP-2）：`_await_terminal` 轮询 `service.run_status`，不扫描 events；投影全序 `[user,assistant,idle]×2+[user,assistant,assistant,idle]` + user 文本往返 + idle outcome 全断言。

**中断不拼接构造有效性**：`omit_done=True`=干净 body 结束、无 SSE [DONE] 终止行（EOF-without-terminal）——maas.py L539-541 的确定性设计入口（"never via hard truncation"），非超时/竞态依赖构造；残段现状**如实断言**（`assistant.status=="interrupted"`+`idle.outcome=="interrupted"`，契约外行为零臆造）；新请求 assistant 文本=="fresh answer" **零旧片段**。

**计数真实性**（EventLog SQL + `server.requests` 交叉）：主链 3 run/4 step.generated/1 tool.finished/4 请求——与脚本语义推演一致（2 文本轮各 1 生成 + 工具轮 2 生成 = 4；partial content 后无重试）；中断面 2/1/0/2——中断 run 不提交 step.generated 与 S7c m-4 非生成支语义同构（L586-593 早返回），两 run 均达 durable 终态故 run.finished=2。数值非臆造。

**事件序断言语义**：`remaining = iter(...)` + `any(observed == expected for observed in remaining)` 是**正确的有序子序列算法**（迭代器消费至命中、耗尽即败）——非全等、亦不可乱序通过；零 delta 持久化以 type 级子串扫描覆盖全部 `*delta*` ephemeral 事件类。

**测试纪律**：sqlite 读严格在终态+close 后（`_event_types` 调用位于 `async with App` 退出与 `server.aclose()` 之后，S7 纪律）；events 表直读为既有 idiom（`test_service_run._event_data` 同款）；fixture 只读复用（tests/r0 导入零改 + `_upgrade_head`/`_await_terminal` 同形态重建，diff stat 三文件自证零改既有）；异步形态与既有 integration 测试一致；alembic 真 CLI（REPO_ROOT cwd）。

### 面2 · 证据文档映射真实性（r2-exit-evidence.md）—— 通过（含 m-1）

- **A/B/C+验证行四项映射完整**：L80-83 原文逐句对照，每要素有落点、无缺项。验收 C 子面表 **10 行**对照 L82 原文逐子面全覆盖（流中断/非法 JSON/未完成未知 call/最大步数/上下文超限/Ctrl-C/资源清理/唯一 tool_result/accepted→cancelling 与 run.started vs cancel_requested 两竞态/tool_call_id·execution_id 冲突拒绝）。
- **L85 三约束** §6 落点齐（串行=invoker 串行断言；单 active run=busy 门禁+cancelling 占槽复验；无 steering/inbox=约束声明本身）。
- **引用独立抽查 13 处全命中（唯 m-1 错位）**：
  - §0 面 5 处：`contracts/messages.py` L109=`class AssistantTextPart`/L129=`class AssistantToolPart`/L141=`tool_call_id` 字段（逐字命中 ×3）；`ai/maas.py` L272=`class MaasAdapter`（逐字）；`doc/reviews/` 枚举——R2-S2/S3/S4/S5/S5-hotfix/S6/S7/S7-closeout/S7c/S8 全在、**R2-S1-review.md 确不存在**（"—" 标注属实）。
  - §3 面 7 处：`test_tool_read.py` 21 测失败面全实证（offset 越界/缺失/目录/nul/绝对越界/traversal/symlink·junction 逃逸/二进制/invalid UTF-8/1 MiB 窗/64 KiB 截断标记/串行序）；`test_runner.py` max_steps（L1154-1170 violation 断言）+未知工具 not_started 仍配对（L649）+工具轮二次请求（L584）；`test_context.py` ContextLimitExceeded（L374-433）；`test_cli.py` `--context-limit 4097`（L134/L142）；`test_ai_adapter.py` `asyncio.all_tasks` 泄漏检查（L829-867）；`test_service_run.py` cancelling 占槽（L506）+cancel_requested（L520）；`test_projector.py` 冲突/非法迁移拒绝（L242-468）。
- **门禁演化数字一致**：210（r1-exit-evidence §5 ✓）→ 630（ORCHESTRATION 台账 L140 ✓）→ 648（L142/L143 ✓）→ 651（本任务自跑+调度器亲跑双证）。§5 "207 files" 如实定界为自跑数+计数非验收标准（调度器 209 同 exit 0，任务书预注为已闭合类）。
- **删节项逐项合理**（定界诚实非掩盖）：S2-S6 中间数字归台账/S2-S7 测试引用 file 级（行为锚=r2-slices 各切片测试计划行）/R2-S1 评审缺失/源码 symbol 级锚/CI 复验归调度器——五项全在 progress ③ 如实列出。
- **零"与 TS 同构"未证断言**：全文扫描通过（TS 仅以只读参照纪律出现）。
- **r1 模板对齐**：章节骨架（头部四要素+§0 切片表+§1-4 验收/验证映射+§5 门禁 CI+§6 已知边界）与映射粒度照搬成立；源码锚 file:line→file+symbol 的粒度收缩在行号声明中如实定界并保留四个已核行号。

### 面3 · progress 披露完备（T-C1-024.md）—— 通过

- ③ 四档结构与代码实况抽查一致（协议不可达 3 项/未测防御 3 组/已测防御 5 项/注记 4 项）。
- **"既有测试覆盖"声明 2 处实证成立**：(a) fs__read 失败面在 test_tool_read.py——21 测逐一在场，覆盖所列全部面；(b) 多工具/多步循环/max_steps 在 test_runner.py——`test_tool_round_drives_second_request` L584 + max_steps L1154-1170 在场。
- scripted "非真实"声明充分（loopback 脚本服务器/omit_done 非真实网络故障/tmp 自建文件但文件系统与 fs__read 执行真实）。
- ⑤ 存量兼容逐条真：648 零回归（651=648+3）、src 零 diff、既有测试零触碰（diff stat 三文件自证）、fixture 只读复用、无 git 写。
- ④ 预算超支如实披露（超 $5.00 止损线后完成三件交付、一次通过无返工）——不粉饰。

### 面4 · 任务书兑现核对（tasks/T-C1-024.md）—— 通过

- **A 组①-④ 逐条落**：①主链（≥2 文本轮+多段流两读并防+完整工具链+双侧配对+run_status 终态）②中断不拼接（INTERRUPTED+残段如实+零拼接）③计数（EventLog SQL+requests 交叉）④可审计最小面（有序子序列+零 delta、字段级归 S6/S7 定界遵守）。
- **B 组四要求落**：r1 模式照搬 ✓/验收 A·B·C+验证行逐条三方映射 ✓/S2-S7 收口+门禁数字演化 ✓/L85 三约束 ✓。**"验收 C 不入 e2e=B 组映射既有测试"定界兑现**（§3 十行映射表，抽查七处实证）。
- **TS 零新指纹**：source-baseline.json 零触碰（diff stat 三文件自证）；不宣称 TS 分页同构 ✓。
- 门禁节兑现（python -m 形式；651=648+3；mypy 66=65+1；format exit 0）；① 选型论证四项齐备（e2e 组合/两读并防/中断注入与计数/核验方法）；每测落点入进度文件 ✓；止损 90min 内；防截断增量写入 ✓。

### 面5 · 点名复核（named recheck，独立给据）—— 两项均 concur

**1. omit_done EOF-without-terminal → INTERRUPTED 的契约链：concur，测试未过度解读契约。**

- 契约层（§8 L354）单句同摄两面："部分流已到达则结束并标 interrupted，**不能把旧流和新流拼接**"；retry owner 唯一=MAAS adapter、仅 429/短暂网络可重试（×2）。§8 L348 错误码注册表含 `ProviderStreamInterrupted`。
- 适配器层（maas.py）：L42-43 模块 docstring 明文 "after partial content: no retry, no splicing (the stream ends in an ``error`` chunk with ProviderStreamInterrupted)"；L539-541 代码——"EOF or [DONE] without any finish frame … raise `_failure(CoreErrorCode.PROVIDER_STREAM_INTERRUPTED, \"stream ended without a terminal chunk\")`"（确定性 omit_done 入口，注释明示 provider.md §5 契约依据）。
- runner 层（L586-593）：错误块→`_finalize_interrupted`→INTERRUPTED。
- **与 T-B-024 已 concur 的 HTTP-500 面同归一理**：adapter 侧部分流失败的两个入口（HTTP 错误→`PROVIDER_UNAVAILABLE`；EOF 无终止→`PROVIDER_STREAM_INTERRUPTED`）汇入同一 runner 收尾路径→同一终态语义 INTERRUPTED；FAILED 保留面（生成违约 §2.3 L100/max_steps §7 L343/预算 §7 L339）不被稀释。
- 测试断言的正是 §8 明文（interrupted 终态+不拼接），残段现状如实（status/outcome）无契约外臆造——**无过度解读**。

**2. "中断部分流不自动拼接"语义边界：concur，不误标。**

- 测试断言面=**生成文本零拼接**（`fresh_assistant` 文本=="fresh answer"）+ 新请求自有 user turn（`requests[1][-1]` role user=="start over"、请求数 2）；**未断言亦未否认**新请求历史含 interrupted 消息（S4 上下文组装正常行为——归 test_context.py/S4 面，任务书"验收 C 不入 e2e"定界内）。
- §8 "不能把旧流和新流拼接"本义即**流/生成层**（adapter 不将旧 partial 流与新流拼接），非历史组装层；测试恰断此层。文档/progress 表述一致限定（"新 assistant 文本==新脚本全文零旧片段"），边界清晰。
- 可选增强见 r-1（R3/后续轮，非缺口）。

## 2. 分级发现

### minor-1（m-1）· r2-exit-evidence.md §6 继承行号错位（一处一行）

- **位置**：`doc/reference/r2-exit-evidence.md` §6 首条边界注记——"诊断清除命令 Phase 1/2 不提供（R1 遗留，CORE-CONTRACTS §3.1 L187，归 R5/人工）"。
- **形态**：现行（#97 态）CORE-CONTRACTS.md **L187 为 runs 读模型规范**（`run_id` 主键/unique(session_id, request_id)/投影迁移），非诊断清除句；该句现行位置为 **L195**（"只有经过验证的备份恢复、迁移或人工修复命令才可清除诊断状态；Phase 1 不提供静默修复"）。根因：从 r1-exit-evidence §6 **逐字继承 R1 态行号**——r1 头部明示 "file:line 以 R1 收官态（merge #62）为准"，R1 时 L187=该句；R2 契约扩容（现 424L）后行号漂移，继承时未重锚亦未声明行号基准。
- **定级理由**：minor——事实本身为真（r1 §6 原文+引文可溯、归 R5/人工处置未变），但证据文档的全部价值即引用可核性，失锚行号会把 R5 消费方导向无关条文；任务书 B 组纪律"只引用**存在的** file:line/测试名/报告节，写前逐一核验，宁缺毋假"为无条件要求。非 major：单一非退出条件边界注记、一处一行、事实可经 r1 §6 溯源。
- **建议**：改引 "CORE-CONTRACTS L195"，或改锚 `r1-exit-evidence.md` §6 并注 "R1 态行号"。**写权在调度器**：随合并顺手修正（推荐）或并入 R3 首项；不阻塞。

**未分级备注**（不计数）：

- **r-1**：e2e ② 可选增强——追加 `requests[1]` 历史含 interrupted assistant 消息的断言，使零拼接证明独立于 ContextBuilder 的历史选择（"历史含残段（S4 正常）vs 生成零拼接（§8）"两面同测对照）。R3/后续轮可选，非缺口。
- **r-2**：§5 "207 files" 为席位自跑数（调度器 209、同 exit 0）——文档已如实定界（自跑+计数非验收标准），无动作。
- **r-3**：§0 S1 行审查记录列 "—" 诚实标注 R2-S1 评审文件缺失并指向 progress ③——支持 C 侧 ④(b) 上浮，无动作。

## 3. ③④ 意见（对 progress 的浮报/未决核对）

- **③ 浮报核对意见**：四档结构与代码实况抽查一致，无虚报；"既有测试覆盖"两处声明实证成立（面3）；删节项五条全部合理——file 级测试引用以 r2-slices 测试计划行为行为锚是可接受的收缩，函数级枚举可后续补齐；"协议不可达"档把 close 超时面（既有覆盖）如实注明而非冒充未覆盖，分层诚实。
- **④ 未决问题意见**：(a) 函数级枚举补齐——建议 R3 或 R5 结项前以 `pytest --collect-only` 一次性补齐，不占当前合并窗口（裁量归调度器）；(b) R2-S1 评审文件缺失——经目录枚举证实确无 R2-S1-review.md，从台账看 R2-S1（contracts 扩容）可能并卷于相邻记录或未单独立卷，建议调度器在 ORCHESTRATION 台账核对后补一行注记（证据文档 S1 行现表述已诚实，无需阻塞合并）。预算超支披露属实、处置正当（三件交付一次通过，未因超支降质）。

## 4. 上浮调度器项

1. **m-1 一行修复**（写权在调度器）：r2-exit-evidence.md §6 行号重锚 L187→L195（或改锚 r1-exit-evidence §6 并注 R1 态行号）——建议随合并顺手完成。
2. **R2-S1 评审文件台账核对**（C 侧 ④(b)）：确认命名/并卷后在台账补注记。
3. **④(a) 函数级枚举补齐时机**：建议 R3/R5 前，不占当前窗口。

## 5. 结语

S8 收口交付质量高：e2e 三测是**真全栈强断言**（真 HTTP→SSE→ContextBuilder→runner→Invoker→真实文件系统→SQLite，双侧配对+SQL 计数交叉+有序子序列算法正确），非冒烟测试；证据文档四项验收映射完整、独立抽查 13/14 命中且唯一错位为一行可修的继承行号漂移（m-1）；progress 披露无粉饰、超支如实；两项点名复核经契约+代码独立验证均 concur（EOF 断流与 HTTP-500 同归 §8 L354 单句之理，零拼接断言面恰在流/生成层不误标历史组装层）。**通过——建议 m-1 随合并顺手修正后走合并流程**（预期 main 门禁 651 / clean / format / mypy 66）。
