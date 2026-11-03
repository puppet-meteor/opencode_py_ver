# R2-S7 收口微补丁批评审（席位B · T-B-022 · fix-diff-only）

- 日期：2026-11-04。对象：worktree `r2-s7b`（branch r2-s7b，基 `f1fdef4` = main 检查点#88）两提交 `f3ec726`（T-C1-022 批）+ `666362f`（T-C1-022b m-2 落地）；diff 面 `git diff f1fdef4..HEAD`：8 文件 +758/−121（service.py、repos.py、txn.py、store.py、runner.py、test_service_run.py、test_runner.py、进度文件）。
- 模式：**fix-diff-only**（沿 T-B-017B 先例）——只审全量 diff；门禁不重跑（调度器两轮亲跑全绿：pytest **628**=619+9 零回归 / ruff clean / format exit 0 **189 files** / mypy **61**；188/189 计数差调度器 L259 已记档解释，非判据）；已定裁决不重审（技术性异议须新证据）。
- 依据：T-C1-022 / T-C1-022b 任务书；契约 §7（L313-339 cancel/close 全序）/§3.2.1/§10（[T-A-009]③ L412、[T-A-010] L420-424）；本席 `doc/reviews/R2-S7-review.md` §2/§5.2/§5.3/§6（本批即该清单的落地）；#90 三裁决（ORCHESTRATION.md L259 亲读）。

## 1. 结论

**通过，不阻塞并回。** 本席 R2-S7 全部后置项（M-1×3 / m-1 / m-2 / n-1/2/3 / ④-3 / ④-4 / A-009③）逐一核销（§3 落地表），#90 三裁决均忠实落地、无技术性异议（§4），进度 ④ 披露完备无轻描淡写（§5）。发现 **1 minor**（m-3：runner `_interrupted_message` 非生成支 × observed 构造分支可构造未测未披露——语义已由生成支三测钉定，缺的是构造路径对称性）+ **2 nit**，均为微补丁/备案级，不影响并回。写面=8 文件全部在授权面内（store.py 经 #90 裁决②追认）；既有 14+28 测除 L828 授权翻转外零触碰（两测试 diff 纯追加亲核）。

## 2. 分级发现

**blocker 0 / major 0 / minor 1 / nit 2**

### m-3（minor）`_interrupted_message` 非生成支 × observed 构造分支：可构造、未测、未列 ③

- 位置：runner.py `_interrupted_message` 两个 AssistantMessage 构造点（生成支 L1145 / **非生成支 L1172**，四字段 `fields.finish/raw_finish/usage/cost_usd` 对称施加）× 三个 A-009③ 新测（test_runner.py L1436/1466/1494）全部含 `.text(...)` 脚本 → 仅**生成支**被测。
- 可构造场景：error-first 无 text 脚本（如 `_Script().usage().error(boom)`——流先 error、零 committed parts）→ `partial` 为空 → 开放 step 无 generated → 非生成支携 observed 终态化，usage 保留语义在该支未被任何测试走过。
- 判级理由：语义面（interrupted 保留已收观测、终态仍由 error 决定）已在生成支三测钉定，缺的是构造路径对称性而非新语义——故不沿 T-B-021 维度 11"漏报即 major"口径升级（彼批三处均为完整语义面：崩溃报告/R4 边界/腐败分类）。两支代码形状逐字对称（diff 亲核），漂移风险极低。
- 建议：微补丁一测（error-first 脚本断言 usage/cost_usd 保留 + parts 空 + 终态 error）或 ③ 补备案；B 倾向补测（脚本一行改动级成本）。

### n-1（nit）A-009③ 三测的"零工具执行"仅结构性保证

- 任务 9 spec 要求三测共断言"单 provider 请求 **+零工具执行**"；实际仅断言 `len(provider.requests)==1`，零工具执行由 harness 结构保证（`_wire` 不注册 adapter + 脚本无 tool call），未显式断言。字面缺口、风险可忽略；如需字面符合可各补一行（state 零 tool part 断言）。

### n-2（nit）M-1① 测试以 `await asyncio.sleep(0.1)` 静默崩溃记录

- test_service_run.py L938（测试主体 L926）。亲析：该 sleep **非正确性关键**——cancel() 若先于任务完成进入，则经 `await task`（L767-768）等到崩溃记录完成后同样收敛 `_fail_cleanup`；若后进入则 entry=None 径读 `_schedule_errors`——**两序同果**，sleep 仅缩短路径。建议加一行注释钉定该两序收敛论证，防后人误当脆弱时序"修复"。

## 3. 落地核对表（B 侧 R2-S7 清单 → r2-s7b diff 逐项核销）

| 项 | 落地位置（亲核） | 测试（断言强度） | 判 |
|---|---|---|---|
| M-1① cancel 崩溃支 | service.py L777-785 一带（`_schedule_errors` → `_fail_cleanup` 链原错误） | test_service_run.py L926：`LifecycleCleanupFailedError` + 消息含 "RuntimeError" + `__cause__` 链 RuntimeError 原文 + session 落 cleanup_failed + diagnostic reason=cleanup_unconfirmed + run 保持 cancelling | ✓ |
| M-1② 无活任务面 | L786-788（无活任务→返回 CANCELLING 交 R4） | L950：免 provider App（engine 接线）prompt→accepted 不调度 → cancel→CANCELLING + cancel_requested intent **恰 1 条** reason=user + 重复 cancel 幂等（事件计数级断言） | ✓ |
| M-1③ 变体 (g) | L704-711（owner session 未知状态→StorageCorrupt fail-closed） | L971：终态静默后 `UPDATE sessions SET status='bogus'` → StorageCorrupt REASON_RUN_OWNER_MISSING（同步 sqlite 仅终态后——纪律遵守） | ✓ |
| m-1 判别式 | L767-776：`try: await task / except asyncio.CancelledError: if not task.cancelled(): raise` **逐字落地**（区分任务硬取消与调用方自身取消）+ cancel() docstring 同步 | L1008：`_GatedCloseProvider` 令 close() 驻 Phase 3 保 engine 存活使重读确定完成 → cancel() 返回 INTERRUPTED **而非抛 CancelledError**；释放门后 close 完成、provider 单关 | ✓ |
| m-2（022b，#90①） | L869：`_done2, pending2 = await asyncio.wait(pending, timeout=_CLOSE_GRACE_S)` 捕获第二次 wait；标记环仅遍历 **pending2**；Phase 2 注记改 applied 语义（引 #90）+ 不可测残留如实保留；close() docstring 标记句对齐 | **L828 原地翻转最小性亲核**：改名 `test_close_timeout_grace_exit_keeps_session_executable` + docstring 改写 + 标记面三断言（session `"active"` / 重开 `SessionStatus.ACTIVE` / 重开 prompt **成功**且 run_status==ACCEPTED——运维重开面修复正向钉定）；其余断言（tool.finished 恰 1 条 unknown/unknown、run interrupted、closed、provider close_count==1、0.2s+`_GatedAdapter` 构造）**逐字未动**，文件其余段落零改动 | ✓ |
| n-1 锁真相注记 | Phase 1 注记改写为"check-first 去重 + ConflictError 重试环（无锁被持，'随后释放提交锁'无需释放）"——失实声明纠正 | —— | ✓ |
| n-2 isfinite | L407-413：`math.isfinite(close_timeout_s)` 入守卫，消息改 "positive finite" | L990：inf/nan/-inf 三态均 `StoreValidationError`（直构 SessionService） | ✓ |
| n-3 | `self._locks.clear()` **删除**（grep 零命中亲证；选型理由：无任何面依赖撕裂窗，dict 随 service 终结回收——① 节给否决注释方案的论证） | —— | ✓ |
| ④-3 收编 | service.py 七条 sa.text 常量出清（`sa.` 零残留、仅存 AsyncEngine 类型导入）；四方法 rewiring：`run_by_id`/`session_id_by_active_run`/`session_id_by_message_id`/`session_owner`/`event_by_dedupe_key`/`run_status_by_id` 六新读面 + `SessionStore.cleanup_failed` 薄委托 → `TransactionWriter.cleanup_failed`（txn.py：UPDATE **verbatim** 含 `status='active'` 作用域 + diagnostic `json.dumps(sort_keys=True)` 逐字节同 + 独立短事务零 asyncio 锁——锁纪律论证成立："取锁会改变卡死写者下的 close-timeout 标记"）；engine 参数保留为接线判别（注释重写），四个失真 `assert self._engine` 删除（数据路径经 repos 自有 engine，入口门语义保留） | 619 基线 runs 六变体/竞态两分支/close 全序/28 runner 测零回归（628 绿）；错误分类面逐变体等价亲比（见下） | ✓ |
| ④-4 容忍（A-010①） | L841-848：逐 live run 的 `_commit_cancel_intent` 单独 try/except **且仅捕** `SessionReadOnlyError`；协作信号设置在 **except 块之外**（隔离 session 照常发信号）；其余存储异常与调用方取消穿出；close() docstring 补容忍句 | L1035：**A-010 变体 (e) spec 断言点全覆盖**——close 正常返回 / 事件数+applied_seq 水位保全 / 无 cancel_requested+无 tool.finished（无修复事件）/ run 保持 running / session 保持 read_only_diagnostic / provider 单关 / service+DB 门关 / 后续 prompt 拒 SessionReadOnly（隔离门先于 closed 门——§7 L328 门序的正向钉定）/ 重复 close 单清理任务；隔离 UPDATE 经**异步 engine**（`exec_driver_sql`）——S7 同步 sqlite 纪律遵守 | ✓ |
| A-009③ | runner.py：`_ObservedFields`（frozen，L258 一带）+ `_observed_fields()` 投影（镜像 succeeded 终态读法）；`_finalize_interrupted`/`_interrupted_message` keyword-only `observed` 参数；**两调用点传 observed=result**（错误支 L592 / 工具取消支 L623）；**双构造点**（L1145/L1172）统一施加四字段；docstring 保守 None 措辞修订。观测字段落 AssistantMessage（§3.1 L144 消息面）而非 `_run_finished_draft`——RunFinishedData 无观测字段，"或等价机制"条款落法正确（① 节选型论证） | test_runner.py 三测：(i) usage→error：终态=error verbatim + usage/cost_usd 保留 + finish/raw_finish None；(ii) finish+usage→error：双观测保留（finish=STOP）+ **防御不放宽**（终态仍 error）；(iii) EOF：四观测保守 None 不推断 0 + PROVIDER_STREAM_INTERRUPTED；均断言 `len(provider.requests)==1` | ✓（m-3 缺口另计） |

**④-3 等价性论证复核（行为零变化红线）**：① 五 SELECT 常量 verbatim 亲核（`_SELECT_SESSION_OWNER` 与原 `_SELECT_SESSION_EXISTS` 同文、`_SELECT_EVENT_ENVELOPE_BY_DEDUPE` 与原 `_SELECT_EVENT_BY_CANCEL_DEDUPE` 同文）；② `session_id_by_active_run` 复用既有 `_SELECT_SESSION_ID_BY_RUN`（repos.py L82-84，`ORDER BY session_id LIMIT 1`）——对 `.first()` 存在性等价成立：active_run_id busy 槽至多一持有者，且探针值**仅作 None 判定**（双持有腐化场景两语句均入同一 StorageCorrupt 分类，具体 id 不被消费）；③ 逐方法独立连接 vs 原单连接：pysqlite SELECT autocommit 无快照持有、原语句间亦无快照——等价成立；④ 轻量探针不解 JSON 列 = 与原内联读分类面一致（undecodable config_snapshot 不致探针失败），按"分类不同则新设面"复用规则正确；⑤ 错误分类逐变体等价：NotFound×1 + StorageCorrupt×3（投影缺失/owner 缺失/状态未知）+ REASON_* 详情字段 + isolate 标志 + 消息文本逐变体亲比一致；str 强转移位（RunRow/SessionOwnerRow 构造时）不改变值语义，`error` None 保 None。

## 4. 对 #90 三裁决的技术性异议

**无（零新证据触发异议）。** 逐项核验：

1. **裁决①（m-2 选项 (a)：m-2 重落 + L828 原地翻转）**：落地忠实（§3 表）；翻转最小性 hunk 级亲核（仅改名+docstring+标记面三断言，其余逐字不动）；pending2 标记支不可测披露如实（沿 ③-4 测试纪律：需屏蔽 CancelledError 的工具）。本席 R2-S7 §2 m-2 原始建议即"区分第二次 wait 结果"，无立场反转；调度器"#86 已裁采纳 × 零触碰红线"的内蕴矛盾经 C-1 实证上浮而非静默改测——披露机制按设计工作（L259 记档），程序无瑕疵。
2. **裁决②（store.py 薄委托照准）**：13 行纯委托亲核（writer 实现在 txn.py、公共面在 SessionStore）；任务书路径清单漏列系调度器笔误（L259 已记档），④-2 上浮→裁决链完整。
3. **裁决③（runner 硬取消支保守 None）**：亲核 `_finalize_interrupted` 全部 **7 个调用点**——仅裁定钉定的两处传 observed（错误支 L592 / 工具取消支 L623），硬取消四支（L462/476/488/507）+ succeeded→interrupted 竞窗回退支（L564）保守 None。第三穿线（result 进 run() 的 CancelledError 处理器链）需扩写面，本批不扩、保守正确。

**附带意见（非异议，供调度器后续裁量，均为可选微补丁）**：
- L564 竞窗回退支：result 在作用域内，1 行扩面即可携带 observed（进度 ④-3 已浮报待裁）——B 倾向授权扩面（与已落地两支同语义，无新风险面）。
- m-1 残窗（③-未测防御 1，如实披露且技术准确——外层 Task 取消经 `_fut_waiter` 转发进被 await 任务时 `task.cancelled()` 不可区分，判别式会吞调用方 CancelledError）：如需"调用方取消必达"语义，可在 except 收敛后以 `asyncio.current_task().cancelling()` 复核重抛（重读完成后再抛，兼顾两者）。

## 5. ④ 披露清单核对（进度文件 T-C1-022.md）

| 档 | 应有 | 实有（亲核） | 判 |
|---|---|---|---|
| 协议不可达 | 2（OP-9 + 调度同步窗，沿 T-C1-021） | 2 ✓ | 完备 |
| 未测防御 | 本批新增/沿承防御支 | 5：m-1 残窗（**新增且技术准确**）/ runner 硬取消观测丢失（新增，#90 已裁）/ 竞窗回退支 observed=None（新增，待裁）/ 放弃支（沿 ③-4）/ 重试环窗（沿 ③-1） | **缺 1：m-3（非生成支×observed，§2）** |
| 已测防御 | 9 测 | 9 ✓（逐测对应关系亲核） | 完备 |
| 架构注记 | —— | 4：store.py 委托矛盾上浮 / m-2 回退-重落双钉 / repos 分类留席主 / 测试纪律（函数级导入+异步 engine 构造） ✓ | 完备 |
| ⑥ 残留披露（022b） | m-2 重落后 | 3：pending2 不可测 / grace 内退出但 handler 崩溃支不再被过标兜住→**交 R4 恢复协调员**（与 §7 L325 storage-only 同族，裁定一致）/ ①-⑤ m-2 表述由 ⑥ 覆盖声明 ✓ | 完备 |
| 矛盾处理 | m-2 × 零触碰不可兼得 | 实证上浮（L828 变红机理 + 三选项 + 未静默改测）→ #90 裁决 → 022b 落地，全程留痕（Phase 2 注记 + ⑥ 节） | 完备 |

**轻描淡写扫描**：未见。n-3 选型给了否决注释方案的理由；④-3 等价性论证四条（常量 verbatim/独立连接/存在性探针/JSON 列不解码）均实质成立（§3 复核）；唯一缺口即 m-3。

## 6. 建议（供调度器排期，均不阻塞并回）

1. **m-3**：微补丁一测（error-first 无 text 脚本：`_Script().usage().error(boom)` → 断言非生成支 message usage/cost_usd 保留 + parts 空 + 终态 error verbatim）或 ③ 补备案——B 倾向补测。
2. **意见项两枚**（§4 附带）：L564 observed 扩面（1 行）；m-1 残窗 `cancelling()` 复核（2-3 行）——可与 m-3 同批微补丁。
3. **n-1/n-2**：记档或随下批顺手（各一行断言/一行注释）。
4. 并回后 main 四门禁亲跑（调度器已计划，预期 628/clean/format/61）+ S8 窗口预备的两前置决策（messages() 扩面取向 + OP-5 CLI 依赖）不在本评审面内。
