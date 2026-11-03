# R2-S7c 收尾微批评审（席位B · T-B-023 · fix-diff-only）

- 日期：2026-11-04。对象：worktree `r2-s7c`（branch r2-s7c，基 `eb7dfc1` = main 检查点#92），单提交 `2666e1f`（T-C1-022c，4 文件 +163/−2：runner.py 1 行、test_runner.py、test_service_run.py 注释、progress ⑦ 节）；diff 面 `git diff eb7dfc1..HEAD` 全量亲读。
- 模式：**fix-diff-only**（沿 T-B-017B/T-B-022 先例）——门禁不重跑（调度器亲跑全绿：pytest **630**=628+2 零回归 / ruff clean / format exit 0 **194 files** / mypy **61**）；已定裁决不重审。
- 依据：T-C1-022c 任务书（#92 裁决转述）；本席 `doc/reviews/R2-S7-closeout-review.md` §2（m-3/n-1/n-2）/§4 附带意见/§6；契约 §2.1（run/step/tool 状态表 L59-61、L65）/§3.1 L144/§3.3 L210-212/§8 L350；ORCHESTRATION.md L268-272（#92/#93 链）。
- 行号基注：closeout 意见项"L564"系 f1fdef4 基线旧行号（r2-s7b 批 +69 行后实为 **runner.py:662**）；实现者按内容正确解析（run.finished(succeeded) 拒绝回退支，区别于 result 不可达的 step.started 拒绝支 L558-566）并在进度 ⑦ 明示映射——无错位修补。本批 1 行为等行替换，全文件行号无漂移（closeout 构造点引用 L1145/L1172 复核仍准）。

## 1. 结论

**通过，不阻塞并回。** T-B-022 后置项全落地且核验成立：m-3 补测（构造与断言全 spec）、L662 observed 扩面（#92 授权边界内，**惰性论证代码级成立且普适**）、竞窗对照测（**确定性构造成立**，注入经 store 正常提交、纪律遵守）、n-1 五测、n-2 注释（技术准确且比本席原始表述更精确）。#92 三项裁决忠实执行，无技术性异议。发现 **1 minor（m-4：m-3 构造支归因倒置——三个 A-009③ 测实走非生成支，真正无断言的生成支 observed 仍未测未披露；源头在本席 closeout m-3，零行为风险）** + **1 nit**。写面恰四文件=授权面；既有测触碰仅限任务书明示项（A-009③ 三测各 +1 断言行、M-1① 一处注释，零断言变化）；git 零写。

## 2. 分级发现

**blocker 0 / major 0 / minor 1 / nit 1**

### m-4（minor）m-3 构造支归因倒置：三个 A-009③ 测实走非生成支；生成支 observed 施用至今无断言、未列 ④

- **证据链（三步全代码亲证）**：
  1. runner.py L586-593：`result.interrupted` 支于 L593 早返——`PY_STEP_GENERATED` 仅成功路径 L607-615 提交，**任何中断流（含 text 的 error/EOF）都不提交生成事件**；
  2. state.py `_apply_finalized` L483-523：finalized 事件只写 `state.finalized`（L516-523），不置 `step.generated`（L499 仅读作守卫）、不挂 state.messages；唯 `_apply_step_generated` L540-541 置位并追加消息（`StepInfo.generated` L189 默认 False）；
  3. 故 `_interrupted_message` L1097 `if step.generated:` 在一切中断流上为 False——三个 A-009③ 测（脚本 L1444/1474/1502 全含 text）与落地 m-3 测（L1541）**全部走非生成支 L1153-1179**；生成支 L1097-1152 仅经工具取消路径 L623（生成提交后的取消）可达。
- **后果一（归因错误全链继承）**：倒置源头=本席 closeout m-3（"全部含 text 脚本 → 仅生成支被测"——实为仅**非**生成支被测）；#92/任务包/落地测 docstring（"the symmetric counterpart of the generated-branch tests above"）/进度 ⑦ 忠实继承。有 text 与无 text 的中断流走**同一构造支**，差别仅 `state.finalized` 非空/空。
- **后果二（真实缺口重定位）**：非生成支 observed 语义 r2-s7b 起已被测 (i)/(ii) 钉定（`message.usage==Usage(5,7)` 即经 L1175）；m-3 落地测的真实增量=非生成支**空 finalized 边缘**（L588-589 `if partial:` 跳过提交 + parts==[]——真覆盖、有价值）；而**生成支 observed 施加（L1146-1149，经 L623 可达）无任何测试断言**：两测试文件 usage 观测亲扫仅 test_runner.py L524/1444/1474/1541/1601 与 test_service_run.py L433/449/710/989——无一为工具取消场景（close/cancel 族测均不带 usage 观测）。可构造：usage-bearing 完成流+工具计划+取消（既有 close/_GatedAdapter 基建，脚本加 `.usage()` 即可）→ 断言 interrupted message 的 finish/usage/cost_usd 保留。
- **判级**：沿 closeout m-3 同口径（语义在受测支钉定、缺对称构造路径）→ minor 而非 major；本例残余面更小——两支四字段施用（L1146-1149/L1173-1176）逐字对称+共享 `_observed_fields`。落代码与全部断言均正确，零行为风险，不阻塞并回。
- **建议**：①微补丁更正测 docstring+进度 ⑦ 归因表述（各一行级）；②可选补测生成支 observed（B 倾向补：基建齐备，脚本一行改动）。

### n-3（nit）注入基建绕过 committer 锁与 bus（记档防扩散）

- test_runner.py L1574-1589：`super().commit` 返回（per-session 锁已释放，runner.py L191-201）后直连 `self._store.commit`——绕过锁与 `bus.notify`。本测单写者+零订阅者 → 零观测差、确定性成立（注入在 runner 自身 commit 调用栈内同步执行，无并发交错窗口）；若该模式被复制到多写者/带订阅者场景，锁外注入可能与并发批次交错（high 水位 ConflictError 未处理）且事件不经 bus。记档：此为投影状态构造手段，非生产 cancel 路径仿真。

## 3. 逐项落地核对（五评审面）

| 面 | 核验（亲读代码/测试） | 判 |
|---|---|---|
| ① runner.py L662 一行 | `observed=result` 于 run.finished(succeeded) 拒绝回退支（L656-664）；result 自 L571 在作用域。**惰性论证成立且普适**：该支仅在 step.finished（L639-646）已提交后 run.finished 被拒时触发 → 该 run 全部 step 必已终态（工具续步若取消早于 L621-624 返回）→ `_finalize_interrupted` L1020-1023 找不到非终态 step → L1024 消息构造块整体跳过 → `_interrupted_message` 不运行 → observed 不改任何输出（仅 L1073 run 终态提交）；终态观测经 succeeded 路径 L625-646 已落库。#92 授权边界：恰 1 行+条件授权的对照测（可构造→配），一致 | ✓ |
| ② m-3 测 | L1531-1562：`_Script().usage().error(boom)`（L1541）→ partial 空 → L589 跳过 → 非生成支 parts==[]+observed 保留；断言=result 全等（interrupted/steps=1/error）+单请求+零工具+run/message 状态+usage=Usage(5,7)/cost_usd/finish/raw_finish None+**parts==[]**——closeout 建议断言面全落地（归因见 m-4） | ✓ |
| ③ 竞窗注入测 | L1592-1650+`_CancelAfterStepFinishedCommitter` L1565-1589：注入经 `self._store.commit(EventBatch, log_high)`——与 `EventCommitter.commit` 内路径（runner.py L191-197）同源，**无 sqlite 直写**（纪律 ✓）；armed 单发护栏（L1576-1577 置 False；注入直连 store 无递归；finalize 批不含 step.finished 且 armed 已 False，双保险）✓；**确定性**=注入在 step.finished 的 commit 调用栈内同步执行，严格介于 runner 两次提交之间 → run.finished(succeeded) 折叠必见 CANCELLING（§2.1 L59-61）→ IllegalTransitionError → StorageCorruptError(isolate=False) → `except StoreError` L656 ✓；内联 wiring 与 `_wire` L383-426 逐件等价（仅 committer 类替换，A-009③ 测同走默认参）✓；断言面=run INTERRUPTED+error None+单请求+零工具+step 终态 completed 读逐字（finish=STOP/usage/cost_usd/raw_finish None）——分支语义+惰性行为级钉定（step message 未被重终态化）✓ | ✓ |
| ④ n-1 | 5 处 `assert not state.tools`（L1456/1486/1515+L1552/L1641）：`state.tools` 为 execution_id→ToolInfo 注册表（state.py L552/568），空 ⇔ 零执行——closeout 建议（state 零工具断言，各一行）字面落地；进度 ⑦ 类型注记准确 | ✓ |
| ⑤ n-2 | test_service_run.py L938-946 注释块+L946 sleep（零断言变化）：两序论证与 service.py L764-794 **逐行吻合**——崩溃已记录（entry 弹出）→ L783-791 `_schedule_errors` 链 cause（本测变体）；竞速记录器（entry 仍在）→ L765-776 await-task（m-1 判别式）→ run 无终态 → L780-781 "exited without a terminal run" 无链 cause；两序同收敛 LifecycleCleanupFailedError+cleanup_failed+诚实 cancelling run。注释补齐了 closeout 原始表述未区分的 cause 链差异，技术准确 | ✓ |

## 4. ④ 披露核对（progress ⑦）

| 项 | 实有（亲核） | 判 |
|---|---|---|
| 惰性披露（observed 在 L662 改不改输出） | ✓ 如实，且本席复核其**普适性**（到达该支的一切场景 step 必已终态）——比披露陈述更强成立；错误链源码验证（折叠拒绝→IllegalTransitionError→StorageCorruptError(isolate=False)→except StoreError）与 txn 折叠层一致 | 完备 |
| step.started 拒绝支不可扩 | ✓ 亲核：L558-566 支保持 None，result 于 L571 才产生——保守正确 | 完备 |
| m-1 残窗 R3 维持 | ✓（#92 不设票） | 完备 |
| 硬取消穿线待裁 | ✓ 维持原状 | 完备 |
| 生成支 observed 未测 | **缺**（m-4——因归因倒置，全链认为已被 A-009③ 测覆盖） | 缺 1 |
| 轻描淡写扫描 | 无：629→630 预期差如实记档；既有测触碰明示授权范围；门禁双跑（席自跑+调度器亲跑）数字一致 | —— |

## 5. 建议（供调度器排期，均不阻塞并回）

1. **m-4**：微补丁更正测 docstring+进度 ⑦ 归因（一行级）+ 可选补测生成支 observed（usage-bearing 脚本+取消工具，既有 close 基建）——B 倾向同批带上（可与 S8 批或独立微批）。
2. **n-3**：记档（注入基建锁/bus 旁路，复制场景需携注）。
3. 硬取消穿线+m-1 残窗维持 #92 R3 备案原状，不随本批重开。
4. 并回后 main 四门禁亲跑（预期 630/clean/format/61）→ r2-s7c worktree 清退 → T-C1-023（S8 切片）窗口开启。
