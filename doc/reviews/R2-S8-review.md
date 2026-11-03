# R2-S8 评审报告（席位B · 结构化评审）

- **日期** 2026-11-04 · **评审人** 席位B（规划评审） · **任务** T-B-024
- **评审对象** worktree `r2-s8`（分支 r2-s8；基线 main `f3e1e7a` → 单提交 `8cf82e8`；13 文件 +1395/−21；工作区 clean）
- **评审范围** T-C1-023（S8 主体：A 组 messages() 联合扩面 / B 组 stdlib-only CLI / C 组 m-4 承接）+ T-C1-023b（#95 R-1 收窄 `_user_texts`）
- **基准** `doc/CORE-CONTRACTS.md`（424L）+ 切片方案 + 调度器裁决策项（#92/#94/#95 及 S8 预决策）
- **门禁依据（调度器亲跑，免复跑）** pytest **648 passed**（630 基线 + 18 新增 = union 4 + cli 13 + runner 1，零回归）· ruff check 0 err · ruff format --check exit 0（202 files）· mypy **65 files / 0 errors**（61 + 4 新文件）
- **diff 授权面** 调度器逐 hunk 亲验通过（023 十一面 + 023b 五面全在授权内，含 #95 对 R-1/R-2/R-3 的裁定）
- **纪律** git 零写（除本报告）；止损 45min 未触界

## 总裁决：**通过**

计数：blocker **0** / major **0** / minor **0** / nit **1**（n-1，已给可执行处方挂 R3，不阻塞合并）。五面全部成立；点名复核结论与实现侧一致（详见 §2）；对涉本切片裁决策项（#92/#94/#95、S8 预决策）无异议。

## 1. 五面逐面结论

### 面1 · A组 messages() 联合扩面 —— 通过

**结论**：`SessionMessage` 归置权威、三态 tag 分派、三路 fail-closed 解码、payload-id 一致性检查全部正确；四个测试桩均为真实全栈断言。

- **归置权威**（`src/opencode/contracts/messages.py`，紧随 `IdleMessage` 之后 +6 行）：`SessionMessage` 联合别名落位契约模块（`#:` doc 注记），符合 §2.1 L23-29 的归置与 docstring 风格——联合类型权威定义在契约层而非使用侧。
- **公开导入面不变**（`src/opencode/session/context.py` +7/−1）：import + 再导出，`opencode.session.context.SessionMessage` 继续可用，既有消费方零改动。
- **三态 tag 分派**（`src/opencode/storage/repos.py` L71-77 `_MESSAGE_MODELS`）：user/assistant/idle 三 tag 恰为合法投影全集，无缺漏无多余；`MessagePage.items: list[SessionMessage]` 类型面收口。
- **三路 fail-closed**（repos.py L325-350 `_decode_message_row`）：tag 缺失（KeyError）/未知 tag（ValueError）/坏形（ValidationError）三路全部汇入既有 `REASON_MESSAGE_ROW_DECODE_FAILED` 的 StorageCorrupt（异常元组扩为 `(KeyError, ValueError, TypeError)`）——解码失败整页原子失败，无半解码头泄漏。
- **payload-id 检查保留**（repos.py L349-350）：行内 `message_id` 与解码后 `id` 的一致性断言逐字保留，S7 完整性纪律未削弱；docstring L29（023b）与三态语义对齐。
- **测试四桩**（`tests/integration/test_messages_union.py`，315L / 4 测）：
  ① store-commit 夹具正当——无 provider 的 run 永不终止（busy 闸门不可穿越），以手工 watermark 0..3 直接提交构造跨页序列，是全栈语境下唯一可行夹具形态，且未绕开任何被测路径（解码/分页/游标全在真实读路径上）；
  ② 真实全栈 user/assistant/idle 分页 + 游标 + has_more；
  ③ assistant 游标→idle 边界推进 + 跨会话游标 InvalidCursor；
  ④ 两种腐坏变体（未知 tag / 坏形）整页 StorageCorrupt；变更断言严格 post-terminal + close（S7 纪律）。

### 面2 · B组 stdlib-only CLI/REPL —— 通过

**结论**：451 行 CLI 是"纯宿主"的范本实现——只做输入与显示，终态权威严格让位 `service.run_status`，exit 码面与 #94 逐项对齐。

- **stdlib-only**（OP-5 #92）：alembic 迁移以 subprocess 驱动（不 import），无第三方依赖进入 CLI 层；`__main__.py` 8 行纯委托（`python -m opencode` → `cli.main`，无 `[project.scripts]`，符合 S7 预决策）。
- **仅走 App 公开面**：组合根零改动；CLI 只消费 §7 命令面（create/prompt/cancel/run_status/messages）+ fs.read builtin + MaasAdapter；参数面明示为 R3 ConfigLoader 注入点而非配置层。
- **终态权威**：`run_status` 轮询是唯一终态权威（§7 L314 durable truth，`_POLL_INTERVAL_S=0.05`）；bus 订阅仅为 live preview——订阅先于 prompt（不丢早期事件）、终态后 `_DRAIN_S=0.2` 收尾排空；`_PREVIEW_TYPES` 六事件展示面（不含 `py.tool.input.delta`，输入增量不属于显示面，可接受）。
- **exit 码面**（#94 逐项对齐）：`_ROUND_EXIT_CODES {SUCCEEDED:0, FAILED:1, INTERRUPTED:2}`；cancel 清理失败→3；迁移失败/未捕获异常/宿主配置构造失败→70；prompt 面 Ctrl-C→130；mid-run Ctrl-C→幂等 cancel→继续等待终态→2（§7 L335/L337）。`SlowConsumerError`→stderr 仅显示，不影响 exit 码。
- **参数面分野正确**：`--model`/`--context-limit` required + env 回退，缺失→argparse exit 2（与 interrupted 双占用 2 已 #94 裁定并披露）；`--context-limit 1`→RuntimeSettings 构造 ValueError→70（构造期），`--context-limit 4097`→构造通过、请求不可容纳→ContextLimitExceeded→FAILED→1（运行期预算）——两条路径不打混。
- **显示与 REPL**：`escape_for_display` 以 maketrans 将 C0（保留 \t\n）+ DEL + C1 映射 U+FFFD，str 级契约正确；REPL 输入 `asyncio.to_thread` + `_EndOfInput` 线程内捕获重抛（PEP 479 合规）；`/history` 参数钳制 1..1000、页 ≤100。
- **迁移子进程**：cwd 向上发现 alembic.ini → `parents[2]` 回退，路径策略自洽。
- **13 测质量**：seam 仅 `lines`/`wait_hook` 两个（不 monkeypatch 内部）；`_ServerHost` 专用线程 loop（URL 先于 main() 已知、daemon 线程存活期覆盖 asyncio.run）；⑤ mid-run preview 竞态诚实豁免（由 ⑥ preview 连续性桩钉住替代）；⑫ 真实 subprocess 冒烟（`PYTHONPATH=src`）补进程级 exit 码面。质量面无保留。

### 面3 · C组 m-4 承接 —— 通过

**结论**：S7c 评审的 ④"缺 1"（generated 分支中断保观测）以**强于处方**的形态闭环。

- **docstring 修正**（`tests/integration/test_runner.py` L1533）：忠实转述 R2-S7c-review §2 证据链（三个 A-009③ 测试走的是非 generated 分支——`PY_STEP_GENERATED` 仅在成功路径 L607-615 提交，而 L586-593 早返回已截胡；`state.py _apply_finalized` L483-523 不设 `step.generated`，仅 `_apply_step_generated` L540-541 设）——**零断言改动**，纯文档纠偏。
- **新增测试** `test_interrupted_from_generated_plan_keeps_observed_usage_and_finish`（test_runner.py L1655+）：gated tool 计划（text + 2 calls + usage + finish=TOOL_CALLS）生成后 cancel；**直接断言 `step.generated`**（强于我处方的结果面断言）；单请求；finish=TOOL_CALLS / raw_finish=None / usage(5,7) / cost "0.0012"；tool_a succeeded / tool_b cancelled + not_started——归因（谁生成、谁被砍）与观测保真（usage/finish 不丢）双面钉死。
- **progress 纠偏**：T-C1-022.md ⑧ append-only 修正，披露纪律执行。

### 面4 · 023b 收窄（#95 R-1）—— 通过

**结论**：纯机械收窄，零语义变化，648 计数自证。

- `_user_texts` helper 以循环内 `assert isinstance(message, UserMessage)` 作 runtime-true pin（mypy union-attr 14→0 的合法收窄，非 cast 掩盖）；落位 3 个此前冻结文件（eventlog/rebuild/service 测试）。
- 7 个消费点逐一 verbatim 替换（eventlog 1 / rebuild 2 / service 4），无一处顺手改语义；repos.py docstring L29 同步对齐。
- **L380 `PromptAcceptedData` 未触碰**（S7c 评审面保持原样）。
- 门禁 648 计数不变（18 新增全属 023 面）自证 023b 零行为变化。

### 面5 · progress 披露审计 —— 通过

**结论**：T-C1-023.md ①-⑥ 披露与代码实况抽查一致，无粉饰。

- ③ 未测四层（协议不可达 4 / 未测防御 10 / 已测防御 8 / 注记 5）分层诚实——真实 SIGINT、非 UTF-8 控制台等确在"未测防御"层，未冒充已测。
- ④ R-1（mypy 14 union-attr 冲突→#95 裁定 helper 收窄→023b→14→0）/ R-2（已解决）/ R-3（挂 R3）处置链完整可溯。
- ⑤ 兼容性声明属实：service.py 零改动的风险被诚实浮出而非隐藏；630 基线零回归由计数证实。

## 2. 点名复核：provider HTTP 500 → §8 保守 INTERRUPTED（exit 2）而非 FAILED —— **裁定：同意实现侧（concur）**

独立复核链（不依赖实现侧自述，以契约文本 + 代码为准）：

1. **契约层**（CORE-CONTRACTS §8 L354）：retry owner = MAAS 适配器，仅 429/短暂网络错误可重试（×2）；"部分流已到达则结束并标 interrupted"——provider 侧可用性故障按中断语义收尾。
2. **适配器层**（`src/opencode/ai/maas.py` L470-477）：HTTP 错误映射为**终态 ErrorChunk**（`CoreErrorCode.PROVIDER_UNAVAILABLE`，`retryable = status_code >= 500`）。该 retryable 标志是建议性元数据（向宿主披露可重试性），runner 从不据此重试——§8 明定 runner 非重试主体，两层语义无冲突。
3. **runner 层**（runner.py L586-593）：错误块到达 → `_finalize_interrupted` → INTERRUPTED——保守路径保留已观测 usage/finish（与 S7c m-4 修复语义同构）。
4. **FAILED 保留面未被稀释**：仅 `_GenerationViolation`（§2.3 L100）、max_steps（§7 L343）、context budget（§7 L339）三类生成契约违约入 FAILED。provider 不可用是环境故障而非生成违约，不入。
5. **exit 映射**：INTERRUPTED→2（#94）。
6. **测试选型复核**：②a 以 context-budget 面（`--context-limit 4097`→ContextLimitExceeded→FAILED→exit 1）代表 FAILED 路径正当——CLI 硬连 MaasAdapter、无 provider seam，无法注入 `_GenerationViolation`/max_steps 触发器，4097 是全栈可自然触发的 FAILED 代表；provider 500→INTERRUPTED 的判定本身以上述 1-4 的契约+代码独立复核为准。

**裁定意见**：实现侧"HTTP 500 → 保守 INTERRUPTED（exit 2）"与 §8 契约文本、maas.py 错误映射、runner 收尾路径三方吻合，**维持**。

## 3. 分级发现

### nit-1（n-1）· 真实 SIGINT 下 prompt 面 exit-130 路径存在挂起隐患

- **文件/点位**：`src/opencode/cli.py` prompt 面 KI 处理 + 进程退出路径（`asyncio.to_thread(input, ...)` 工作线程）。
- **形态**：KI 中断的是主线程；阻塞在 stdin 的 `input()` 工作线程不被打断。`asyncio.run` 退出时 `shutdown_default_executor` 会 join 默认执行器线程——被阻塞的 input 工作线程可能永不 join，**exit-130 路径在真实信号下可能挂起**（直至用户再按回车）。进程内注入测试（wait_hook KI）测不到此路径。
- **定级依据**：nit 而非 minor——该场景已在 progress ③"未测防御"层第 7 项（真实 SIGINT 未测）诚实披露，R2 合并面内无回归风险；但若不挂账，R3 真实 SIGINT 条目可能只测 mid-run→2 而漏掉 prompt 面→130。
- **处方**：见 P-1（并入 R3 真实 SIGINT 条目）。

**未分级备注**（不计数，R3 顺手项）：

- **r-1**：`/history` 非数字参数被静默忽略（`suppress(ValueError)`→默认 50）——显示面瑕疵，建议 R3 改为回显警告后取默认。
- **r-2**：exit 2 双占用（argparse usage + interrupted）——#94 已裁定且 progress 已披露，无动作。
- **r-3**：`_print_terminal` 以 `str(CoreError)` 渲染错误——pydantic model repr（`CoreError(code=..., message=...)`）而非人读消息；数据正确、显示待抛光。

## 4. 可执行处方

- **P-1**（承接 n-1，R3 必做）：R3 真实 SIGINT 条目扩为双子场景，**必须 subprocess 级真实信号**（对齐 ⑫ 冒烟形态）：(a) mid-run Ctrl-C→cancel→等待→exit 2；(b) prompt 面 Ctrl-C→exit 130，并断言**进程真实退出**（带超时的 wait，防 join 挂起）。若 (b) 复现挂起：退出清理后走 `os._exit(130)` 快路径，或将 input 工作线程 daemon 化/显式放弃 join。
- **P-2**（承接 r-1，R3 可选）：cli.py `/history` 参数解析——无效参数回显警告行后取默认值，替代静默 suppress。
- **P-3**（承接 r-3，R3 可选）：cli.py `_print_terminal` 错误渲染改取 `CoreError.message` 字段。

## 5. R3 备案取舍意见

| 条目 | 意见 | 理由 |
|---|---|---|
| SlowConsumerError 显示 | **缓/低价值** | stderr 单行披露已满足 R2 契约；完整显示 UX 属产品面，不占 R3 早期带宽 |
| 迁移失败注入（alembic 子进程真实失败→70） | **R3** | 70 面已有 ②c 代表覆盖（宿主配置构造 ValueError→70）；迁移注入为增强非缺口 |
| exit 3（cancel 清理失败）真实触发 | **R3** | 现仅故障注入可达；保留披露即可，建议与真实 SIGINT 条目合并考虑 |
| 真实 SIGINT | **R3 必做 + 并入 n-1** | n-1 的 prompt 面挂起隐患必须随此条目闭环（P-1 双子场景） |
| 非 UTF-8 控制台 | **R3** | `escape_for_display` str 级契约已正确；真实控制台编码行为是平台面，随 R3 CLI 抛光批处理 |
| session 复用/配置层 | **R3（按计划）** | CLI 参数面即为此预留的 ConfigLoader 注入点（cli.py docstring 已声明） |

## 6. 裁决策项异议检查

- **#92**（OP-5 stdlib-only、`python -m opencode`、无 `[project.scripts]`）：实现符合，无异议。
- **#94**（exit 码面 0/1/2/3/70/130）：实现逐项对齐，exit 2 双占用已披露，无异议。
- **#95**（R-1/R-2/R-3 → `_user_texts` 收窄授权）：收窄形态合法（runtime-true pin，非 cast 掩盖），7 消费点 verbatim、L380 未触碰，无异议。
- **S8 预决策**（A-009⑤ messages() 扩面路径）：A 组实现即此路径，无异议。
- 本次评审面未触及需对 #96 提异议的争点。

## 7. 结语

S8 是 R2 的收口切片：A 组把 messages() 联合扩面做成"契约权威 + 三路 fail-closed + 真实全栈夹具"的教科书形态；B 组 451 行 CLI 严守纯宿主边界（终态权威=run_status、exit 码面=#94、stdlib-only=#92）；C 组以强于处方的断言闭环 m-4；023b 收窄零语义变化由 648 计数自证；点名复核（HTTP 500→INTERRUPTED）经契约+代码独立验证与实现侧一致。**通过，建议调度器走合并流程**（合并后预期门禁 648 / ruff clean / format 202 / mypy 65）。唯一 nit（n-1）已给可执行处方（P-1）并挂 R3 真实 SIGINT 条目，不阻塞。
