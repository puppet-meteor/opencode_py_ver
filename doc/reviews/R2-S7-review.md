# R2-S7 结构化代码审查（席位B · T-B-021）

- 日期：2026-11-03。审查对象（S7 窗口两 worktree，各单提交，只读 git 亲核）：
  - **r2-s7 @ aa67bd3**（父 e6d80ad；`feat(session): R2-S7 service wiring - schedule/cancel/run_status/close (T-C1-021)`；4 files +1583/−52：`src/opencode/session/service.py` +532/−37、`src/opencode/app.py` +91/−15、`tests/integration/test_service_run.py` 新 876L 14 测、进度文件）。
  - **r2-s7a @ 02644ab**（父 e6d80ad；`perf(session): projector incremental fold cache + S6 M-1 filing (T-C1-021a)`；3 files +445/−16：`src/opencode/session/projector.py` +193/−16（席位自报 "+209" 系 stat 误读，调度器已纠正）、`tests/integration/test_projector.py` 纯追加 +194/−0、进度文件含 ⑥ 备案专节）。
- 基准：`doc/CORE-CONTRACTS.md`（检查点 #84 后行号，418L）；`doc/plans/r2-slices.md` §2 L136-144；execution.ts（Interface L17/terminal L47/layer L54）+ run-coordinator.ts；`doc/reviews/R2-S6-review.md`（M-1/m-1 出处）、`doc/reviews/R2-S2-review.md` §2 m-1（缓存排期原文）；T-C1-021/T-C1-021a 任务书。
- 门禁（调度器亲跑全量，B 未复跑——任务书豁免）：r2-s7 pytest **616**=602+14（125.13s）/ruff clean/format **181** exit 0/mypy **61**=60+1；r2-s7a pytest **605**=602+3（119.76s）/clean/format **180** exit 0/mypy **60**。写权面逐路径亲核=两包授权清单精确一致；versions/ 零触碰。

## 1. 结论

**通过。** 两包主路径未发现代码缺陷：12 维逐条对照全部落位（§3），S6→S7 接缝纯消费零重实现（§4），A-009 判例边界（裁决②/③）两包均未触碰（§4.2/§4.3）。发现 **1 major**（M-1：③ 漏报三处——cancel 收尾两分支 + run_status 未知 session 状态分支；三处代码行为均亲核正确，缺陷纯在披露/测试完备面，且均可低成本构造测试）、**2 minor**（m-1 调度器注记 1：close 超时硬取消与并发 cancel 的 CancelatedError 穿出；m-2 close 超时支对 grace 内已退出任务的保守过标）、**4 nit**。M-1/m-1/m-2 处置均为微补丁批次，不构成并回阻塞；排期建议见 §6。

## 2. 发现分级

**blocker 0 / major 1 / minor 2 / nit 4**

### M-1（major）③ 漏报三处：可构造的未测分支未列 ③（维度 11 抽查命中）

- 位置：r2-s7 `src/opencode/session/service.py` L704-711、L777-785、L786-788。
- 理由（逐处亲核）：
  1. **cancel 收尾·任务已崩分支**（L777-785）：`_drive_run` 捕获非 CancelledError 异常记入 `_schedule_errors`（L958-959），cancel 据此走 `_fail_cleanup` → cleanup_failed + LifecycleCleanupFailed（§7 L335 "清理无法确认"）。可达且可构造：runner 是注入的 `RunScheduler` 协议实现，不保证自吞一切异常（如存储面错误穿透），scripted provider 流中 raise 即可触发。未测且未列 ③。
  2. **cancel 收尾·无活任务面**（L786-788）：engine 已接而 runner 未接（无 provider 的 App）时 prompt 落 accepted 且不调度（`_schedule_run` L938 no-op）——cancel 应提交 intent 后原样返回 CANCELLING（A-009 ② 的 R4 边界面，进度 ① 明文记载该读法）。现有 `_run_app` 免 provider 构造即是现成注入法；close 的对称面已有测（test L859），cancel 缺位不对称。未测未列 ③。
  3. **run_status 未知 session 状态分支**（L704-711）：`SessionStatus(str(owner.status))` ValueError → StorageCorrupt。与已测变体 (c)（test L739-746，未知 **run** 状态）同构、同一构造法（一行 `UPDATE sessions SET status='bogus'`）。未测未列 ③。
- 判级依据：任务维度 11 "漏报即 major"。三处代码行为均正确（崩→链上原错误的 LifecycleCleanupFailed；无活任务→诚实 CANCELLING 交 R4；未知状态→fail-closed）——与 R2-S6 M-1 同类，但彼判例为协议不可达→备案即可；本批三处**可构造**→须补测。
- 建议：微补丁批 3 测：① raising scripted provider → cancel → `LifecycleCleanupFailedError` + cleanup_failed 落库；② 免 provider App prompt→cancel → 返回 CANCELLING 且 intent 单条落库；③ 变体 (g) `UPDATE sessions SET status='bogus'` → StorageCorrupt。

### m-1（minor）close 超时硬取消 × 并发 cancel：CancelledError 穿出 cancel()（调度器注记 1）

- 位置：service.py L770 `await task` × close L842-843 `task.cancel()`。
- 理由：close×cancel 重叠极端窗（cancel 正 await 活任务，close 超时支硬取消同一任务）→ `await task` 将任务的 CancelledError 重抛进 cancel() 调用方，违反 §7 L315 `cancel(run_id) -> RunStatus` 签名。状态面无损害：intent 已 durable；close 侧超时标记不依赖 cancel 的收尾（L845-847 独立执行）；A-009 ② 兼容（残留归 R4，不虚报）。判 minor 不判 blocker：窗极窄、无状态腐化、两路径各自收敛。
- 建议：`try: await task / except asyncio.CancelledError: if not task.cancelled(): raise`（区分"任务被硬取消"与"cancel 调用方自身被取消"——`_drive_run` L956-957 保证硬取消任务必以 cancelled 态收尾）→ 落入既有终态重读/`_fail_cleanup` 流。附 1 测（`close_timeout_s=0.2` + 不可中断工具 + 并发 cancel）。

### m-2（minor）close 超时支对 grace 内已退出的任务仍标 cleanup_failed（保守过标）

- 位置：service.py L844-847：`await asyncio.wait(pending, timeout=_CLOSE_GRACE_S)` 的返回值被丢弃，随后 `for task in pending:` 遍历的是**第一次 wait 的原 pending 集**。
- 理由：grace 期间已携确定终态退出的任务（如恰在超时点前后提交了 succeeded/interrupted 终态），其 session 仍被标 cleanup_failed → 重开 LifecycleCleanupFailed、不可再执行（§3.2 L195），运维面误伤。字面读法**契约一致**（§7 L339 以 close_timeout_s 到期为"超时外部结果仍不确定"→cleanup_failed；L831-837 docstring 与代码自洽），且方向保守安全（宁可误标不谎报已清理）——故 minor 而非 major。与 ③-未测防御 2 的"放弃支"不同：本处可测（现有 L828 测试的变体即可构造 grace 内退出）。
- 建议：`_done2, pending2 = await asyncio.wait(pending, timeout=_CLOSE_GRACE_S)`，仅对 pending2 标 cleanup_failed；或调度器裁定维持字面保守读法并记档。附 1 测。

### n-1（nit）close Phase 1 docstring 锁持有声明与代码不符

- 位置：service.py L821-822（"each under its session's commit lock"）× L826-830 实际**不取** `_session_lock`，直接调 `_commit_cancel_intent`。
- 理由：安全性由 store 的 expected_seq 乐观并发 + 重试环内终态重查（L983-996）承担，行为正确；但文档声称持锁会误导后续维护者（cancel 路径 L757-763 确在锁内调用同一助手——两调用点纪律不同）。建议改 docstring 为"check-first 去重 + ConflictError 重试环保证"。

### n-2（nit）close_timeout_s 构造校验缺 isfinite（对称 r2-s6a n-2）

- 位置：service.py L425-426：`float("inf") > 0` 为真 → inf 通过校验，close Phase 2 永等；NaN 已被 `not > 0` 拒。建议补 `math.isfinite(close_timeout_s)`（r2-s6a n-2 同型修复先例）。

### n-3（nit）`_locks.clear()` 的撕裂锁窗

- 位置：service.py L853（close 末尾）。clear 后迟到的同 session 命令经 `_session_lock` get-or-create 拿到**新锁对象**，与旧锁失去互斥。安全性仍保：prompt/create/rebuild 在锁内重查 `_closing/_closed`（L569/L877）必被拒；store 写闸与乐观并发兜底；cancel 不查关闭门但收敛于去重/终态重读（m-1 窗除外）。建议删去 clear（dict 随 service 终结回收）或注释钉定该依赖。

### n-4（nit）stat/format 计数误读（调度器已纠正，记档）

- r2-s7a 席位自报 projector "+209/−16" 实为 +193/−16（209=stat 总行数）；r2-s7 进度 ② 表记 format "180" 实跑 **181**（新测试文件入计）。均非判据（门禁以调度器亲跑为准），记录供 C-1 自报纪律参考——与 r2-s6a 同类先例一致。

## 3. 契约符合性清单（12 维；契约行号=检查点 #84 后）

| 维 | 基准 | 实现（亲核） | 测试（断言强度） | 判 |
|---|---|---|---|---|
| 1 prompt 调度/幂等 | §7 L328-333 | service.py L532-625：状态门 L556-570（含 service 级关闭门 L569）；durable 幂等 L573-582（逐 code point `accepted.message_text == text`，异文 IdempotencyConflict）；busy 门 L585-589；**accepted 提交成功后才调度**（L624 在 L615-618 commit+notify 之后）；**任务强引用** L448（`_tasks` dict，防注册表弃引后 GC 中途回收）；幂等重试永不调度（L573-578 提前 return，仅本次新接受调度） | L443（全事件序列+槽释放+idle 消息落地）；L482-498（`retried == run_ref` + `len(provider.requests)==1` + **事件计数级** `_event_count == events_before` ✓）；L506（cancelling 占 busy 槽——A-S6 已裁） | 符 |
| 2 cancel 全分支 | §7 L337 | L730-788：终态→原样 RunStatus（L754/L761 两级重读，晚到 cancel 绝不覆盖）；锁内 check-first 去重 `cancel:run_id`（L963-998；**重试环内终态重查** L984-988——intent 永不落终态 run，否则 fold IllegalTransition→StorageCorrupt）；锁外设协作信号后 await **同一任务**（L765-770，重复 cancel 共享清理任务）；不中断在途 provider 流（`_consume` 不看信号，S6 冻结面消费）；迟到回调 execution_id 去重（S5 invoker 冻结面，消费未重实现）；清理无法确认→cleanup_failed+LifecycleCleanupFailed（L774-785/L1059-1076） | L535（终态原样返回）；L506/L583（重复/并发 cancel 单 intent 同终态——清理任务单例 ✓）；L553（收文保留/无重发/无新工具/await 返回 interrupted）；L608（cancel-first 拒 run.started）；L647（无引擎诚实拒绝 StoreValidationError）。缺口=M-1 ①② | 符（M-1 另计） |
| 3 run_status 查表 | §3.2 L187+OP-2、§3.2.1 L202 | L629-726：引擎路=runs 单行 PK + owner 检查（L678-726），**零 events 扫描**；无引擎=R1 槽位回退面原样（L647-676）；投影缺失/归属/状态损坏→StorageCorrupt 分类不臆造（L683-719 + `_decode_run_error` L371-394 fail-closed）；历史终态 run 可查 | L670（**手法核实：SQLAlchemy `before_cursor_execute` 监听断言零 "FROM events" 语句** ✓）；L701-769 六变体 a-f（分类理由码逐一断言）。缺口=M-1 ③ | 符（M-1 另计） |
| 4 close() 全序 | §7 L337/L339 | L792-853：首调**原子**进关闭门（L812-816 查与置 `_closing`/`_close_task` 间无 await，单线程循环内原子 ✓）；Phase 1 仅对在册 LIVE 任务提交 shutdown intent（L826-830；storage-only accepted 保留交 R4，不谎称已清理）；Phase 2 `asyncio.wait(timeout)`→超时硬 cancel→grace `_CLOSE_GRACE_S=1.0`→cleanup_failed（L838-847）；期间终态经同一 store.commit 流动；Phase 3 provider 先关→`_closed=True`（L850-852）；并发/重复 close 复用同一清理任务（L812-814）；**OP-9 permission.closed R2 不可达已列 ③**（③-协议不可达 1 ✓） | L777（intent 单条/协作信号经 interrupted 终态/无 cleanup_failed/provider 单关/service→DB 次序）；L808（并发+重复 close 单清理任务/单 intent/单 provider close）；L828（0.2s 超时+不可中断工具：unknown/unknown→interrupted→cleanup_failed→重开 LifecycleCleanupFailed）；L859（storage-only 穿越close 记录全保） | 符（m-2 保守过标另计） |
| 5 竞态两分支 | §2.1 L60（首次 durable commit 决定） | 分支 1（cancel_requested 先→拒 run.started，accepted→cancelling 直达）与分支 2（run.started 先→running 进 cancelling）均由 store 写闸+runner 门承担，service 不复制判定 | L608（**真实注入**：同 engine/bus/ids 手工构造 Runner 注入 service，无 mock ✓）；L553（门控 provider 流+轮询至 RUNNING 再 cancel ✓）；硬取消路径 L828（`close_timeout_s=0.2`+永不开门工具） | 符 |
| 6 app.py 组合 | r2-slices L136-144、S6 冻结接口 | app.py L60-120：`EventCommitter(repos, store, bus, ids, clock)`（L95）/`ToolRegistry(sink=committer.sink, clock, ids)`（L96-98）/`Runner(provider, context_builder, registry, committer, log=EventLog(engine, repos, bus), bus, ids, clock, limits, max_steps)`（L99-110）——S6 冻结签名逐参原样消费；共享 AscendingIds/时钟（L92，防同毫秒重复 id）；`_ProviderHandle` L40-55 结构化最小 Protocol（Provider 协议无 close 的绕行缝，docstring 陈明）；无 provider→R1 面完整退化（runner=None→`_schedule_run` L938 no-op） | 既有 test_service.py R1 面全绿（602 基线）+本批 14 | 符 |
| 7 S6→S7 接缝+A-009 | §10 L406-418 | 见 §4 专节 | 见 §4 | 符 |
| 8 ⑤ 存量兼容+越界 | 冻结面清单 | r2-s7 4 文件=授权清单精确一致；r2-s7a 3 文件同（projector 指定带/测试纯追加/进度）；versions/ 与 S1-S6 全部冻结文件零触碰（`git show --stat` 逐路径亲核）；616/605=602 基线零回归；既有测试零改动（r2-s7 仅两 M+一 ??，r2-s7a 纯追加） | 门禁调度器亲跑 | 符（零越界） |
| 9 r2-s7a 四红线 | T-C1-021a 任务一 | ①③-9 语义不削弱：digest=blake2b-256 长度前缀累积链，覆盖 `_replay_state` 送入解码器的全部 8 原始列（`_FoldCacheEntry` L196、`_verify_prefix` L395-409、`_chain_event_row` L428+）；命中须 `seq==last_seq+1` **且**全前缀链验通过（`_pre_state` L386-393）；任何 mismatch/gap→无状态全量重放=改前行为。②折叠逐位等价（双对照，见测试列）。③身份漂移族（③-10/11/13/14/15/18）语义不变——漂移检测全在全量重放路径内。④rebuild 全量重放语义不变——rebuild 不经 projector（txn.py L684 唯一 `_projector.apply` 调用点亲核）。⑤DB schema 零变更（versions/ 冻结未动） | L1411（13 事件长会话跨 warm projector，四读模型面逐行快照，对照 (a) `store.rebuild` 全量重放 (b) 第二库逐事件提交+每提交冷 projector——**逐位相等**+events 内容列跨库逐行相等）；L1465（warm 态篡改**前缀深处** seq 2 行→digest 断链→全量重放→`stored_event_decode_failed` **钉被篡改行 id/seq**+批次零残留 ✓）；L1500（5 事件批 `full_replays==1`/digest 校验 4 次/第二 run prompt 保温扩展仍增量） | 符 |
| 10 r2-s7a M-1 备案六条 | T-C1-021a 任务二（runner L183-187/L205-207/L433/L588/L741） | ⑥ 专节六条逐一亲核，理由均成立：空 drafts/cross-session ValueError（调用面恒以 `_draft` 现构非空同会话列表，fail-fast 正确）；sink 空批早退（§5.1 一相一批，Invoker 恒产非空相位列表，无 durable 副作用可发生）；L433 `assert run.status is ACCEPTED`（闭集穷举：unknown/terminal→ConflictError、CANCELLING→终态化+return 均前置，防漂移守卫）；L588 `assert working_dir is not None`（created 首事件必钉折叠不变量）；L741 `assert isinstance(chunk, ErrorChunk)`（ProviderChunk 闭集 union 穷举）——与 T-C1-019 `_finalize_interrupted` 两断言备案先例同判 | 备案（doc-only；runner.py 零改动亲核） | 符 |
| 11 ③ 漏报抽查 | R1-S8 教训 | 通读 service.py 1096L+app.py 166L 全量防御分支：**命中 3 处（M-1）**；其余候选裁定不成立（逐一见 §5.1） | —— | M-1 |
| 12 ④ 逐项裁决 | —— | r2-s7 5 项+r2-s7a 4 项，见 §5.3/§5.5 | —— | 见表 |

## 4. S6→S7 接缝专项结论

**4.1 调度消费、不重实现（维度 6/7）。** service.py 对 runner 的全部接触面=注入的 `RunScheduler` 协议（`run`/`cancel_event` 两方法）：`_drive_run` 仅 `await self._runner.run(...)`（L955）+ 记录崩溃（L958-959）+ 终态判断全部经 runs 读模型；cancel 仅 `cancel_event(run_id).set()`（L766），绝不复制生成循环。**生成循环零复制核实**：service.py 全文无 provider/step/generation/phase 逻辑，唯一 runner 语义=协议两方法；工具面仅经 app.py 的 ToolRegistry(sink) 组合，service 不接触 catalog/invoker。S6 已闭合四接缝（catalog 快照、真 sink、EventCommitter、cancel_event 协作信号）经 service 层零回退；RunScheduler 协议置于 service.py 的循环引用规避（runner 反向 import service）成立。

**4.2 A-009 裁决②边界核对（硬取消残留=R4，非 S7 缺陷）。** close 超时支的硬取消残留依判例（§10 L410+§3.3 L212）保持非终态占槽、不声称已恢复：宽限后仍卡死任务放弃+记录保留（L831-837 docstring+③-未测防御 2），对应 session 标 cleanup_failed 而非伪造终态；storage-only accepted（引擎未接/跨进程遗留）从不在册、穿越 close 记录全保（L859 测钉定）。m-1 的 CancelledError 穿出发生在该窗内且状态面无害——B 依判例不升 blocker/major，判 minor 缺陷附微补丁。

**4.3 A-009 裁决③边界核对（usage-on-interrupted）。** r2-s7 diff 四文件零含 runner.py（`git show --stat` 亲核）——interrupted 终态的 finish/raw_finish/usage/cost_usd 语义零触碰，与调度器微补丁（另派）不冲突；r2-s7a 同为零触碰（M-1 备案为 doc-only）。§2.3 L102 `part_id_reused` 条款（裁决①）两包 diff 亦零涉及。

## 5. ③ 漏报抽查结论 + 浮报/④ 逐项裁决表

### 5.1 ③ 抽查结论与候选裁定（维度 11）

**命中 3 处（= M-1，见 §2）。** 其余候选逐一定为不成立：

1. **cancel() 不设关闭门**——契约 §7 L339 关闭门清单（拒新 prompt/工具调度/权限写入）不含 cancel；行为收敛于去重（close Phase 1 的 intent 命中 check-first 去重→零重复提交）+ 两级终态重读 + 活任务缺位（返回诚实 CANCELLING）——无代码分支可浮报，收敛链各环有测或属 m-1 窗。建议 docstring 补一句 close 重叠语义。
2. **prompt 关闭门与 `_schedule_run` 重查间竞窗**——`_schedule_run` L938 查与 L940 `create_task` 同一同步块、无 await，单线程事件循环内不可达；③-协议不可达 2 已备案，亲核成立。
3. **`_decode_run_error` 边沿**（非 str/bytes raw、空 message、非 str message）——全部收敛于已测变体 (d) 的 fail-closed 分类面，无独立分支。
4. **app.py `provider without settings` StoreValidationError**（L78-79）——构造参数守卫，与 service ctor 守卫同型，非防御分支。

### 5.2 r2-s7 ③ 浮报项裁决（6 项）

| ③ 项 | 裁决 |
|---|---|
| 协议不可达 1：OP-9 close 的 permission.closed 分支 | **同意备案**（R2 无 pending approval 环节，§2.1 L48 消费面归 R3+；任务书维度 4 明令必列——已列 ✓） |
| 协议不可达 2：prompt 调度注册同步序竞窗 | **同意备案**（L569/L938-941 同步块亲核） |
| 未测防御 1：ConflictError 重试环终态重查窗 | **同意不设测**（store 面无注入钩子；防线 L984-988 亲核；相邻面由 L535/L583 钉定） |
| 未测防御 2：grace 后仍卡死任务放弃支 | **同意不设测**（构造需屏蔽 CancelledError 的工具=违背测试纪律；前缀路径 L828 已测；放弃支收敛 A-009 ②） |
| 未测防御 3：双痕迹齐删 NotFound 边界 | **同意不设测**（与变体 (f) 同一分类分支：行 None+两痕迹 None→NotFound；删痕构造不新增代码路径） |
| 未测防御 4：close×quarantine（SessionReadOnlyError 穿出 close） | **备案成立，处置升 A**（行为=R1 一致错误传播、字面自洽；后果 provider/engine 不关——政策选择见 ④-4） |

### 5.3 r2-s7 ④ 逐项意见（5 项）

| ④ 项 | B 意见 |
|---|---|
| 1 close 超时参数来源（`DEFAULT_CLOSE_TIMEOUT_S=30.0`+App 旋钮；CLI 归 R2-S8） | **同意**（模块常量+ctor 旋钮+L425 校验面完备——除 n-2 isfinite） |
| 2 竞态注入法（分支 1 手工 Runner 真注入/分支 2 门控流/硬取消 0.2s+不可中断工具） | **同意**（真实注入核实：L608 无 mock、L553 门控流、L828 超时注入——维度 5 满足） |
| 3 runs 读模型缺口（cancel-dedupe 存在性查询/cleanup_failed 管理 UPDATE/run 单行查询三条 `sa.text` 内嵌 service.py；建议 repos 收编） | **同意收编建议，开票**：过渡缝可接受（常量具名+类型化+全测覆盖；T-C1-014 推迟决定仍有效），但 R2-S8 扩大调用面前应收编（runs 查询面+管理写面）。授权面归调度器 |
| 4 close×quarantine（传播 vs 容忍+cleanup_failed） | **升 A 裁决**。B 倾向容忍：Phase 1 逐 run try/except SessionReadOnlyError→仍设信号→wait/超时流自判（quarantine session 的 cleanup_failed UPDATE 被 `status='active'` 作用域自然 no-op，记录已保留交 R4）。理由：close 自身目标（§7 L339 不得遗留资源）优于传播一致性；现行为使 App 处于半关状态（provider/engine 泄漏）。若改需配套 1 测（变体 (e) 构造法去终态等待可复现） |
| 5 双痕迹齐删边界 | **同意不设测**（同 ③-未测防御 3 裁定；一行 DELETE events 变体可选补钉） |

### 5.4 r2-s7a ③ 浮报项裁决（6 项）

| ③ 项 | 裁决 |
|---|---|
| 1 `_chain_event_row` 读回缺行→None | **同意不设测**（txn L677→L683 同事务整批先插后投影，读回不可缺；fail-open 仅弃缓存更新不吞投影错误——亲核） |
| 2 `_remember_fold_state` 仅折叠+写行全成功后更新 | **同意不设测**（异常路径条目停批前态→seq 断档→下次全量重放；不变量推理成立） |
| 3 `_extend_fold_cache_after_r1` 吞 IllegalTransition/ValueError 弃条目 | **同意不设测**（两面——prompt 面行级预态与全量折叠合法域不同/条目跨库陈旧——均由下次 R2 事件全量重放自愈，= 改前语义） |
| 4 命中路径跳过旧行解码的等价性 | **同意不设测**（锚两条不变量成立：digest 链覆盖解码器消费的逐字节列+写者同实例序列化落库 round-trip 恒等；纯函数下"同字节异解码"对抗构造不存在） |
| 5 批回滚后陈旧条目 | **同意不设测**（命中算术 `seq==last_seq+1` 在 log high<last_seq 下不可达；既有回滚测试故障 projector 整体覆写 apply 不入缓存路径） |
| 6 `_CountingProjector` 无构造器模式 | **同意**（基类无 `__init__` 系设计约束——冻结测试子类兼容；与存量 FailingProjector 同构） |

### 5.5 r2-s7a ④ 逐项意见（4 项）

| ④ 项 | B 意见 |
|---|---|
| 1 读放大保底（前缀整段重读=信息论下界；消除的是解码+折叠 O(n²) 累计；批内免复扫需 txn hook） | **同意**（airtight 论证成立：逐段验必漏老行篡改=削弱 ③-9"下次提交必检出"；L1500 计数证明 O(n·m)→O(n+m)；行读取量持平。若调度器需批内免复扫→**开票** txn 面 hook，冻结面不可私扩） |
| 2 缓存无淘汰策略 | **同意备案+LRU 归 A**（单 App 进程内条目数=会话数，实际有界；策略 A 裁定后另开票） |
| 3 prompt 面保温扩展成本 | **同意保留**（纯性能面无语义差；L1500 已钉多 run 保温；若 A 认为不可接受可退化 run 边界冷启动） |
| 4 format 计数随 .md 浮动 | **同意**（exit 0 为准——与 r2-s7 181/180 记档差异同类，见 n-4） |

## 6. 需调度器/A 裁决事项清单

1. **M-1 微补丁**（3 测：cancel 崩溃支/无活任务面/未知 session 状态变体）：B 建议微补丁批尽快落，勿拖至 R2-S8（S8 将扩大 service 调用面）——不构成并回阻塞（三处代码行为已亲核正确，缺陷纯在披露/测试完备面；R2-S6 M-1 判例同构）。
2. **m-1 微补丁**（`task.cancelled()` 判别式 try/except 两行修复+1 测）：排期同上。
3. **m-2**：区分第二次 wait 结果 vs 维持字面保守读法记档——调度器裁定（B 倾向区分）。
4. **④-4 close×quarantine（升 A）**：传播 vs 容忍+cleanup_failed；B 倾向容忍（§5.3 理由），若改需配套 1 测。
5. **④-3 repos 收编开票**：runs 三面（单行查询/存在性查询/管理 UPDATE）——授权+排期归调度器（建议先于 R2-S8）。
6. **r2-s7a ④-1 txn hook**（可选，批内免复扫）：如需，开票授权 txn 面。
7. **r2-s7a ④-2 LRU 策略**：归 A 裁定，后另开票。
8. n-1/n-2/n-3/n-4：随任一微补丁批顺手携带（docstring/isfinite/删 `_locks.clear()`/记档），不单开批次。
