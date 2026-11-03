# R2-S6 结构化代码审查（席位B · T-B-020）

- **审查对象**：
  1. worktree `r2-s6`，分支 r2-s6 单提交 **0571167**（`feat(session): R2-S6 runner generation loop (T-C1-019)`，3 files +2698：`src/opencode/session/runner.py` 1205L + `tests/integration/test_runner.py` 28 测 1428L + 进度文件；父=e91bb40）。
  2. worktree `r2-s6a`，分支 r2-s6a 单提交 **cd94103**（`fix(session): micro-patch batch - pins + isfinite guard + collision msg split (T-C1-020)`，5 files +446/−7；父=e91bb40）。
- **基准**：CORE-CONTRACTS.md 工作树文本（含 §10 L399-402 A-008 两裁决：④-5 ContextLimitExceeded 留会话层 / m-3 取 (a) 款 docstring 指引、无乘数）；r2-slices.md §2 L124-134（R2-S6 切片方案）；TS 原始链 `packages/core/src/session/runner/`（index.ts 44L、llm.ts 457L、step.ts L32-43 Outcome、max-steps.ts MAX_STEPS_PROMPT、publish-llm-event.ts L60/L97-98、execution.ts L17/L47-51）+ retry.ts（对照面，未移植）；前置评审 R2-S5-review §6-5（S6 义务五项）、R2-S4-review §8（本批微补丁来源）、T-B-016 §6-5。
- **门禁**：本审查不重跑，引用调度器亲跑数字——r2-s6：**589 passed / ruff clean / format 172 exit 0 / mypy 60**；r2-s6a：**574 passed / ruff clean / format 170 exit 0 / mypy 58**。r2-s6 实测历时 ~2h31m 超 90min 止损线（全程无失联、完整产出，调度器已如实记档；本席注记见 §8）。
- **写权限合规**：两 worktree 均只读访问；本报告为唯一新建文件；git 零写操作。

---

## 1. 结论

**通过（PASS）**，附条件：1 条 major 属**浮报完整性程序项**（③ 漏报 3 处协议不可达断言 + 3 处守卫分支，无任何代码缺陷，实现面全部核验正确），要求下一进度批补 ③ 备案后闭环；不阻塞合入。发现计数：**blocker 0 / major 1 / minor 1 / nit 2**。

- ③ 处置汇总（T-C1-019）：**同意 8 · 补测 0 · 升 A 0**（8 组浮报全部诚实且推演成立；M-1 为本席新发现，不在其列）。
- ④ 处置汇总（T-C1-019 六项 + T-C1-020 六项）：**同意 11 · 意见 1（折叠至升级清单）· 否决 0**。
- 四接缝（S5 §6-5 义务）：**全部闭合**——catalog 接线 ✓ / 真 sink（③-24）✓ / generation 三限额（④-7）✓ / 重放一致性（A-006）✓，另 ③-4 ToolContext 构造面测试已落地。
- r2-s6a 逐点位核验：**9/9 点位全部与 T-C1-020 授权清单一致**；n-2 `isfinite` 确为唯一行为改动（收紧式）；m-2 文案区分零行为变化且错误路径 O(1) 复查；13 新增测试零回归。

## 2. 发现清单

| 级 | 编 | 内容 | 位置（impl / test） |
|---|---|---|---|
| major | M-1 | **③ 浮报漏项（程序项，零行为风险）**：与已浮报的 `_finalize_interrupted` 两断言同类、但未入 ③ 清单的未测防御分支共 6 处——穷举断言 `assert run.status is RunStatus.ACCEPTED`（run() L433）、折叠不变量断言 `assert working_dir is not None`（L588）、ProviderChunk 闭合 union 穷举断言 `assert isinstance(chunk, ErrorChunk)`（L741）；EventCommitter 守卫 `commit` 空 drafts ValueError（L183-184）与跨会话 draft ValueError（L185-187）、`sink` 空 drafts 早退（L205-207）。按 T-B-020 第 12 维规则（漏报防御分支=major）记 major。处置：**无需代码改动**，要求下一批进度文件补 ③ 备案（与已浮报项同判：协议不可达/不变量守卫，不设测） | runner.py L183-187 / L205-207 / L433 / L588 / L741；test_runner.py 28 测无一构造这些分支（核对成立） |
| minor | m-1 | **interrupted 终态保守丢弃 finish/usage**：error chunk 在 finish/usage 之后到达时（error 赢，L740-746 + L760-762），`_finalize_interrupted` 重建的终态 message 将已收到的 `finish`/`usage`/`cost_usd` 置 None（L973-974 docstring 自述保守）。§2.3 L96 只规定 usage 不得因到达序丢失，未规定 interrupted 终态必须保留——**无契约违反**，但成本核算面（usage/cost 已收而弃）值得 A 座一句话裁决。同场景下该 usage 数据在 `_StreamResult` 中已捕获后被丢弃，属可再议设计而非缺陷 | runner.py L735-746 / L760-772 / L969-974；无测试覆盖 finish→error 序（未浮报，并入本条） |
| nit | n-1 | **门禁记档数字分歧（r2-s6a）**：进度文件 T-C1-020 ② 记 format "169 files already formatted"，任务书/调度器记 **170**（且 r2-s6 侧 172=基线 170+两新文件可互佐基线=170）。两侧均 exit 0、零行为影响；请调度器对账归档（疑为一次计数口径/残留缓存差异） | progress T-C1-020.md ② vs T-B-020.md L7 |
| nit | n-2 | **ephemeral 发布节奏与 TS 不同构**：TS publish-llm-event.ts L98 `deltaBatchInterval = 100`（100ms 批量冲刷），Python 侧逐 delta 即发（L690-703/L719-724）。VolatileBus notify 非阻塞（§4 L226），语义合规；仅当 R3 出现总线压力再议合批 | runner.py L690-703 vs publish-llm-event.ts L98 |

## 3. 契约符合性核对表（12 维，impl+test 双行号）

| # | 维度 | 契约锚 | 实现证据 | 测试证据 | 裁决 |
|---|---|---|---|---|---|
| 1 | 主循环事件序 | §2.3 L100、§3.1 L144、§2.1 L65 | run() 前置三分支 L424-433（unknown→ConflictError/terminal→ConflictError/CANCELLING→不发 run.started 直接终态）；每步序：重读折叠态 L472→ceiling 检 L478→catalog L488→build L494→step.started L510-526（attempt=1）→流消费+校验 L540-541→generation batch 单批 L566-584（finalized 按 ordinal + 恰一 step.generated）→**工具严格在整计划 durable 后**L586-589→step.finished L594-615（finish/usage 仅载终态 message）→无 tool part 则 run succeeded L616-634 | L520-557（单步全链含 idle 投影+槽释放）、L560-580（ordinal 序 0/1）、L583-645（双 step 工具轮） | ✓ |
| 2 | catalog 接线 | §5.1 L253 | 每步重钉快照 L488 `registry.snapshot(session_id)`→`catalog.specs` L497（registry.py L195 返回 ToolCatalog，非 ConfigSnapshot——S5 面消费正确）；未知工具 catalog miss→Invoker 产 failed/not_started（L896-898 docstring） | L609-612（first/second.tools==[echo.spec] 逐请求钉定）、L648-678（空 catalog：unknown→NOT_FOUND+配对保留） | ✓ |
| 3 | persist-before-invoke + 批原子性 | §3.1 L65/L144、§3.2 L159 | EventCommitter.commit L181-201：per-session asyncio.Lock 下读 `log_high`→`store.commit` 单写入口→`bus.notify`（seq 序）；EventBatch 幂等键（同 drafts+batch id）ConflictError 有界 ×3 重试（L191-201）；工具仅在 generation batch 提交返回后启动（L584-589） | L718-771（③-24 全表：runs/tool_executions/messages/sessions 行 + events GROUP BY 类型计数恰等于折叠序列）、L755 注记引用 | ✓ |
| 4 | 流消费/聚合 | §2.3 L88-102、§4 L226、§2.3 L96 | _consume L638-773：text/reasoning 按 part_id 聚合（kind 复用→`part_id_reused` L674-678）；tool 片段按 call_index（L660-667/L704-734）；UsageChunk 后至不丢（last-wins L735-636 注释）、FinishChunk first-wins（L737-739）；ErrorChunk verbatim+break（L740-746）；EOF 无终态→合成 PROVIDER_STREAM_INTERRUPTED retryable=True（L763-772）；ephemeral 三类 delta 即发不持久（L690-703/L719-724，EventLog 零写入） | L774-828（订阅序五事件逐一断言+持久表 0 行）、L836-862（EOF）、L865-888（error verbatim：`result.error == boom`） | ✓ |
| 5 | 失败面 | §7 L335、§8 L350、§2.3 L100、§5.3 L276 | 取消：(a) 预分支 L430-432；(b) 软信号——_run_tools 每 tool 前查 L903-906，StoreError 竞态重读 L921-929；(c) 硬取消——_consume best-effort 提交收文 L747-757→顶层仅 CANCELLING 才终态化后 re-raise L450-458。malformed：`tool_call_without_end`/`tool_arguments_not_json`（parse_constant 拒 NaN/Inf + canonical_json 拒 1e999 溢出双闸 L811-830）/`tool_arguments_not_object`（JsonObject 线面 L818-822）/`duplicate_tool_call_id`（响应内+跨响应 L831-836 + `_history_call_ids` L1192-1205）→全部 `_fail_generation` L1126-1161（空 parts、零 finalized 提交、零工具执行）。限额：text 流 1MiB 含 reasoning（L684-689）、单调用参数 64KiB（L713-718）、≤16 调用（仅计首个 end L727-733）；max_steps L478-486；ContextLimitExceeded 零 step 足迹 L499-506 | L891-1062（四类 malformed+重复 id 双形态，每测断 `echo.contexts == []` 零执行+`not state.finalized`）、L1070-1138（三限额各一）、L1141-1169（ceiling）、L1172-1200（context limit：`provider.requests == []`+零 step 事件）、L1208-1388（取消三路径+两硬取消：mid-tool unknown/unknown、mid-stream 收文存活） | ✓ |
| 6 | 四接缝 | §5.1 L255、③-24、A-006、OP-6 | ③-4 ToolContext 构造 L907-916（六成员+run_id/tool_call_id+cancel 事件对象同一性+progress 发布器 L941-954）；③-24 sink=commit 同路径 L203-207（无第二写者）；A-006 重放：折叠历史 verbatim（点分名/参数/配对 tool result）；OP-6 attempt 恒 1（L522），retry.ts 不移植（模块 docstring L42 明示，无任何生成重试循环——唯一重试是 §3.2 存储层 ConflictError，非生成重试） | L680-715（ToolContext 身份全字段+`context.cancel is cancel_event(_RUN_ID)` 对象同一性+A-007 metadata run_id）、L718-771（真 sink）、L615-625（第二请求角色序 [user,assistant,tool]+计划 verbatim）、L672-678（unknown 工具配对保留 §3.3 L212）、L852/L879/L916/L1163/L1305（各中断/失败面 `len(provider.requests)==1` 不重发） | ✓ |
| 7 | 存量兼容 | ⑤ 强制 | r2-s6：仅 3 新文件，561 基线零改（589−28）；r2-s6a：4 授权文件内 −7 行均为授权点位行级改写，测试纯追加（+1 import 行备案），574=561+13 | git show --stat 双提交亲核；test_projector.py 追加区 L1098+ | ✓ |
| 8 | r2-s6a 逐点位 | T-C1-020 授权清单 | 见 §7（9/9 点位一致） | 见 §7 | ✓ |
| 9 | 基线兼容（TS 对照） | §8 L350 | Effect-services 图塌缩为单协程状态机；step.ts L32-43 Outcome 六型塌缩为"任何 finish reason 完成 step+tool-part 驱动续步"（L616-619 注记）；max-steps.ts MAX_STEPS_PROMPT 仅参考语义（run failed，L478-486/L1174-1184）；execution.ts L47-51 terminal() 三分（succeeded/interrupted/failed）↔ RunOutcome L136 同构 | — | ✓ |
| 10 | ④ 裁决 | ④ 两清单 | 见 §6（同意 11/意见 1/否决 0） | — | ✓ |
| 11 | ③ 抽查 | ④ 强制浮报 | M-1：漏报 6 处（见 §2）；其余抽查 8 组全部属实（见 §5） | — | △（M-1） |
| 12 | 升级清单 | 任务书回报格式 | 见 §8（调度器 7 项 + A 1 项） | — | ✓ |

## 4. 四接缝专项核验（S5 §6-5 义务闭环）

1. **catalog 接线（§5.1 L253）——闭合**。每步循环内重钉 `registry.snapshot(session_id)`（L488），spec 序列原样进 `GenerationRequest.tools`（L497）；工具执行用 raw requested wire name 经 `_wire_tool_name` 单点转换（L919），未知工具由 catalog miss→Invoker 兜底终态，配对不缺（测试 L648-678 双断言：状态 failed/not_started + 下一请求 tool result part 在场）。
2. **真 sink（③-24）——闭合**。`EventCommitter.commit` 是唯一 durable 写路径：runner 批、Invoker 单相事件（`sink` 面 L203-207）同存储同总线，per-session 锁+`log_high` 水位+幂等 batch id。测试 L718-771 以 events 全类型计数表（11 类恰等于折叠序）+runs/tool_executions/messages/sessions 四表行值钉定"一个真 sink、零旁路"。
3. **generation 三限额（④-7）——闭合**。三限额全部在 `_consume` 内联执法（属 §5.3 L276 "generation-level" 语义，S5 只定义不越权执行的边界被正确消费）；violation→`_fail_generation` 一批两事件（step.finished failed 空 parts + run.finished failed），收文不保存（§3.1 L142 只属取消，L1139-1141 注记成立）、零工具执行。三测各自钉 violation code 与零执行（L1070-1138）。
4. **重放一致性（A-006）——闭合**。折叠历史 verbatim 回放：第二请求 [user, assistant, tool] 角色序、点分名 `app.echo` 原样、`input` 对象原样、tool result 与计划逐字配对（L615-625）；unknown 工具 failed 终态仍保留 tool_result 配对（L672-678，§3.3 L212）。跨响应 `tool_call_id` 不回收（L1192-1205 扫全部折叠 AssistantToolPart；测试 L986-1020：step1 完整保留、step2 失败、仅 step1 的调用执行过）。

## 5. ③ 强制浮报逐项裁决（T-C1-019 进度文件）

| ③ 项 | 席位C 自报 | B 核验 | 裁决 |
|---|---|---|---|
| 协议不可达竞态三窗（run.started 撞先行 cancel / succeeded-after-CANCELLING / tool.requested 撞 cancel） | 未测，语义由折叠合法性保证（StoreError 重读改判 L439-447/L625-633/L921-929） | 属实：28 测无一构造第二并发写者（软取消路径皆先查信号/折叠态，未走到 except StoreError 分支）；单进程锁下窗口已被串行化 | 同意，不设测（与"需第二提交者"论证一致）；升级清单第 6 项关联 |
| ConflictError ×3 重试 | 未测（单进程无第二并发写者） | 属实：EventCommitter 单写者场景不可触发 | 同意 |
| `_finalize_interrupted` 两断言（L1020/L1059） | 未测防御（折叠不变量） | 属实；同类断言另有漏报 3 处→M-1 | 同意（漏报部分见 M-1） |
| 未知非 CANCELLING StoreError re-raise | 未测（存储真故障） | 属实（所有取消测试的 StoreError 分支均被前置信号检查短路） | 同意 |
| execution_id 冲突不可达推演 | 计划内唯一性+跨 step 前缀排除 | 推演成立（L837/L842 call_ordinal 单调+step_id 前缀） | 同意 |
| `part_id_reused` 防御性加入 | 规范未列，chunk 面可表达 | 同意且已测（L1047-1062）；名称入规范=升级清单第 3 项 | 同意 |
| 架构注记四项（`_wire_tool_name` 与 maas 重复 / text 限额计 reasoning / `_fail_generation` 不保存收文 / error-after-finish→error 赢） | 浮报 | 四项全部核实成立；usage 保守丢弃另记 m-1 | 同意 |
| 测试期实修 `_interrupted_message`（pre-batch 折叠态与同批合成 tool.finished 分歧→synthesized 终态表传入） | 集成测试捕获的真实缺陷，处置正确 | 复核成立：synthetic 取合成事实、已终态取折叠事实（L1043-1090），实现与测试期记录一致——过程诚实 | 同意 |
| 存量缺口二项（`ReadRepositories.messages()` 仅 UserMessage / R2-S4-review.md 不在 worktree） | 非本席位 | 前者亲核成立（repos.py L254 `items: list[UserMessage]`）；后者属实（S3/S5 有、S4 无——文档同步缺口记升级清单） | 同意，升级第 2/5 项 |

**汇总：同意 8 · 补测 0 · 升 A 0**（M-1 为 B 席新发现，非对其 ③ 的否决）。

## 6. ④ 未决问题逐项裁决（两批进度文件）

### 6-1 T-C1-019（r2-s6）

| ④ 项 | C 立场 | B 裁决 |
|---|---|---|
| 单文件 vs `runner/` 包 | 单文件（状态机内聚），S7/R3 需 host 装配面再拆 | **同意**：1205L 中过半为 docstring/注记，逻辑主体 _consume/_plan/_finalize_interrupted 内聚度高，无并发分片需求；拆分触发条件表述合理 |
| `tool_ref` 接缝归属 | 现内联 `_wire_tool_name` 过渡；④-3 方向=点分为不变量、wire 转换下沉 registry/adapter 面 | **意见（折叠至升级清单第 1 项）**：与 maas 的实现重复是真实债务（两处 `partition(".")` 语义镜像，L282-295）；接口冻结期不越权正确，最终归属待调度器裁决 |
| ④-8 非对象参数归类 | `tool_arguments_not_object`（VALIDATION_ERROR 族），不新增错误码 | **同意**：§8 错误注册表封闭 + `AssistantToolPart.input` 线面 JsonObject 无法表示非对象——属 plan 校验而非工具执行失败，归类成立；测试 L934-957 双向钉定 |
| OP-6 attempt=1 | StepStartedData.attempt 恒 1，重试归属 adapter，用户重试=新 run | **同意**：与 retry.ts 不移植（§8 L350）自洽；全链无生成重试嵌入，`provider.requests` 计数断言反向钉定 |
| 消费面缺口二项 | messages() UserMessage-only；事件表无 run_id 列 | **同意**：均属实（前者 L254 亲核；后者事件按 metadata JSON 查 run）——升级清单第 2/5 项 |
| 调度器裁决项四项 | 见其清单 | **确认转呈**（升级清单第 1-4 项） |

### 6-2 T-C1-020（r2-s6a）

| ④ 项 | C 立场 | B 裁决 |
|---|---|---|
| ③-9 增量路径可达性 | 可达已闭（篡改例实证，未改 fold 面） | **同意**：测试以 UPDATE 事件 data='not-json' 绕过一切 store 面，下一提交经 `_replay_state` 解码失败报 `stored_event_decode_failed` 且 details 钉被篡改行 id/seq——可达性由构造证明 |
| ③-15 选例 pending→failed/not_started | §2.1 L46-52 表 pending 行唯一合法终态族 | **同意**：副作用未发生故 effect 必须 not_started，选例精准 |
| ③-13/14/15/18/11 fold 级直测 vs store 级孪生 | fold 级钉分支本身，store 级孪生属加强项 | **同意**：projector 的 IllegalTransition→StorageCorrupt 映射已泛测；分支语义在 fold 层，直测成本最低语义最大 |
| 早退支 store 级正例缺失 | 存量、非阻塞、下批可补 | **同意**：其 fold 孪生已由 ③-11 合法对照钉定（同 request 幂等再收合法，messages 追加）；不阻塞 |
| §8-1 ContextLimitExceeded 归属不在授权面 | 属 A 裁，本批未动 | **同意**：A-008 已裁决（§10 L399-402）留会话层——且 r2-s6 侧消费面合规：runner 仅捕获并收口进终态（L499-506 `error.core`、零 step 足迹），未重定义 |
| 技术性增行备案（import 一行+format 重排） | 语义零触碰，diff 亲核 | **同意**：git show 亲核仅 L57 import + L1098+ 追加 |

**汇总：同意 11 · 意见 1 · 否决 0**。

## 7. r2-s6a 逐点位核验（git show cd94103 全 diff 对照 T-C1-020 授权清单）

| 点位 | 授权内容 | 实际 diff | 裁决 |
|---|---|---|---|
| context.py ①（n-1） | docstring TS 锚精度注记 | L23-38 区：L206 空 reasoning 带 providerMetadata 结构不可达（Python AssistantReasoningPart 无 metadata 字段）+ L173-185 reasoning→text 降级族无 Python 对应（TUI 层关切，maas 重放丢弃 reasoning——引 R2-S3-review ③-9） | ✓ 纯注记、零行为、锚点陈述准确 |
| context.py ②（m-3） | A-008 (a) 款：指引句，无乘数 | `_CHARS_PER_TOKEN` 注释区 L111-123：CJK 主导历史低估 2-4x、host 须据此调 safety_margin、乘数/真分词器为 R3+ 决策（已记 A 座） | ✓ 与 A-008 语义严格一致，无 R2 行为变化 |
| context.py ③（n-3） | temperature 不设界注记 | L163-168：契约 §2.2 L78 未设界→本地不发明界、越界由 provider 侧拒、数值界属 A 裁 | ✓ 纯注记 |
| context.py ④（n-2） | **唯一行为改动**：isfinite 守卫 | L77 import + L176-185：bool→isinstance→isfinite→>0 四闸，报错文案 "finite positive"；`math.isfinite` 显式拒 inf/nan（原 `nan>0` 偶然拒、inf 放行） | ✓ 收紧式改动，授权面内；inf/nan 双测钉定（test_context.py L562-568） |
| projector.py（S2 m-2） | 文案区分，零行为 | `_write_accepted_run` IntegrityError 处理器：错误路径内复查 `SELECT run_id FROM runs WHERE session_id=:sid AND request_id=:request_id`——有行=身份撞键沿用旧文案；无行=GLOBAL run_id 主键撞车报新文案；两源均 REASON_DUPLICATE_RUN+回滚不变 | ✓ happy path 零查询成本（复查仅错误路径）；SQLite 语句级回滚下同连接复查可行（两测实证）；**本批唯一 src 行为面=文案，符合"零行为变化"授权口径** |
| test_context.py +4 | m-1 双向 / m-2 / n-2 | L436（3001 chars→ceil 751>750 拒，details 钉 751——floor/round 实现必红）、L446（2997→ceil 750 恰过+全额 reserve 200）、L470（嵌套 properties/required 变异隔离——浅拷贝必红）、L562（inf/nan 双拒） | ✓ 四测全部为"实现漂移必红"的强断言（钉 details 数值/钉全量 reserve/钉嵌套隔离/钉文案 match） |
| test_projector.py +9 | S2 m-2 两测 + need-tests ③-9/10/11/13/14/15/18 | L1106 跨 session 复用 run_id（新文案全文钉定+零残留+首 session 行保留）、L1151 同 session 同 request 异 run（旧文案首次全文钉定）、L1175 行存在异 request（③-10）、L1202 篡改已存事件（③-9，details 钉 id/seq）、L1250 fold known-run 异 request 拒+同 request 幂等孪生（③-11）、L1277 身份漂移双形态（③-13）、L1303 started hash 漂移（③-14，from pending 态探针——落点修正论证成立）、L1320 pending 直达终态正例（③-15）、L1342 重复 step_id+已用 message_id 双支（③-18） | ✓ 九测落点与进度文件宣称行号逐一相符；既有测试零触碰（diff hunk 亲核） |
| 进度文件 | 备案 | 84L，②③④⑤ 齐全；唯一瑕疵=② 的 format 计数 169 与调度器 170 分歧（n-1） | ✓（n-1 对账） |

**r2-s6a 逐点位结论：9/9 一致；授权写面之外零触碰（diff --stat 四文件+进度）；n-2 为唯一行为改动且属收紧。**

## 8. 升级清单（调度器/A）+ 记录事项

**转呈调度器（含其清单四项确认）**：
1. `tool_ref` wire 转换归属（T-C1-019 ④/scheduler-1）：`_wire_tool_name` 与 `maas.tool_wire_name_from_dotted` 重复——建议下沉 registry/maas 消重，点分域为不变量；
2. `ReadRepositories.messages()` UserMessage-only（repos.py L254）：**R2-S8 分页消费者将受影响**，需在 S8 排期前决定扩面（AssistantMessage 解码）或另设读面；
3. `part_id_reused` 违规名是否入 §2.3 规范清单（现 runner 防御性执法+已测）；
4. 非取消性硬取消（无 durable cancel 的 CancelledError）留开放 step 的恢复协调者归属（§3.3 L210）——后续席位还是不实现，需显式决策；
5. 事件表无 run_id 列（按 metadata JSON 查询）——R2 消费面记录项；
6. M-1 ③ 补报备案（6 处：runner.py L183-187/L205-207/L433/L588/L741）与 m-1 usage-on-interrupted——**随下一批进度文件补录**；
7. n-1 format 计数对账（169 vs 170）；另 r2-s6 止损超线（~2h31m vs 90min）已由调度器如实记档，本席复核产出完整、四门禁绿，不再另行处置。

**转呈 A 座（开放 OP 项，仅意见）**：
- m-1：interrupted 终态对已收到 finish/usage/cost 的保守丢弃（runner.py L969-974，含 error-after-finish 场景）——无契约违反，但成本核算语义需一句话裁决（保留 or 维持保守 None）。

---
*席位B · T-B-020 · 2026-11-03 · 基准：CORE-CONTRACTS.md（工作树）+ TS 原始链 + r2-slices §2 · 门禁数字引自调度器亲跑，未重跑*
