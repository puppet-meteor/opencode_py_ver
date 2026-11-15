# P7-S2-review — T-C1-101 微评审：SubagentJob 后台作业+完成交付 durable 导入（#248 池序 S2；提案 §3-C M–L 定级）

- 评审对象：worktree `p7-s2` 席位工作区（未提交态，席位 git 写零）：18 跟踪文件改动+3 新 fixture（`events_subagent_backgrounded_{valid,missing_field,bad_extension}.json`）+新 `tests/integration/test_subagent_background.py` 1070L 13 测+进度档+票面拷贝 untracked——**面广恰合 M–L 票预判**：契约预授权圈（§3-7+100 先例链）内触碰全记档。
- 评审基据（全部独立现读，禁继承）：subagent.py diff 全图（docstring 翻新+UNSUPPORTED 移除+background 分支+`_background_result` 四行 verbatim+block 双触发转换环+begin/end finally 平衡+终态 fail-closed+`subagent_final_text` 服务化共享[前台字节保位]）、service.py diff **+484 全图**（常量组 verbatim/`commit_subagent_background` 锁内 marker+arm/`_arm_subagent_observer` 幂等/`_observe_subagent` durable 轮询+三态映射+CAS 突围重入/`_deliver_subagent_completion` 三态文本+XML+steer·queue+completeBackground 幂等门/`request/consume_session_background`/`recover_subagent_observations` 全折叠 re-arm/`_subagent_event_log` engine-None fail-closed/close Phase 2.5 观察者收口）、state.py diff +91 全图（SubagentObservation+fold pop in `_apply_inbox_enqueued`+`_apply_subagent_backgrounded` ACTIVE 前置+duplicate 拒）、contracts/events.py diff（新事件+独立 SUBAGENT_EVENT_DEFINITIONS 元组保 R2 边界+registry 链）、inbox.py diff（`admit_synthetic`=S1 L146 预留缝兑现+metadata carrier+request_id=None）、httpapi diff（POST 路由+idle no-op 204+404 面）、sdk diff（`background()` 无体 204→None）、test_subagent_background.py 13 测签名全图、census/lock 钉独立核证（`len(operations)==26`+pinned pair 20→21/PINNED_TOTAL 139/DURABLE_KEYS 35）、fixture 样本形核、进度档全文 16 面披露、TS 钉面（调度器派发前亲读 subagent-job/subagent-completion/job.ts L100-185/background.ts/session.ts L695/plugin background 分支）。
- 评审时刻：2026-11-16 02:13-04:3x（席位弧单段闭合：发射 02:13→SEAT-EXIT 0 @~04:0x——主窗 100min 满程 EXPIRED[round 67]+尾窗一 TRUE-EXIT=席位活跃超窗，S1 三窗同形常态；全前台 694.09s+进度档全档；席位 git 写零——HEAD 恒 `6eb7206` 验收时点亲证）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 3（全观察面，无返工）

## 六面逐条结论

### 1. 票面纪律 — PASS

- 禁 git 写遵守：HEAD 恒 `6eb7206`（验收时点独立核）；工作区=18 改+3 fixture+新测试+两档件恰合席位终态。
- 单段闭合：全量 pytest 前台收齐（694.09s）+进度档全档后 SEAT-EXIT 0。
- **机制矫正二例全档**（三腿根因取证，零翻面零测试迁就）：①观察者交付 CAS 突围重入（runner EventCommitter 与 service 会话锁异锁→settle 落父活跃 run 提交突发窗时 tight CAS 界耗尽[隔离复现 4/8]→ConflictError 整体重入 ×3+poll tick 间隔；末次再抛→delivery_failures+marker 留 durable-outstanding→重启 re-arm 兜底）；②测试 steer 驱动先等父 run settle（S1 契约：后继 run 首 step 边界消费 pending STEER）。
- §5 八项披露全落位+席位扩充至 12 项。

### 2. 决策锚落面（§3 八锚）— PASS

- 锚1 工具面：Input `background` 可选 bool+TS 描述句 verbatim（"DO NOT sleep, poll, or proactively check on its progress."）；UNSUPPORTED_CAPABILITY hoisted 拒移除=S5 面翻新（Spec 8→9 字段+description 钉+UNSUPPORTED 测改写归新文件）；入场校验序 S5 形保持+description double-face 新校验（marker/notification 载体 fail-closed）；四行 `backgroundResult` verbatim `\n` join+structured/metadata/content 同载。
- 锚2 完成交付：**S1 inbox synthetic seam 唯一入口**（`admit_synthetic` inbox_id=notification_id=TS `synthetic({id})` 对位；`py.message.imported` 零触碰）；三态文本 TS verbatim（completed→共享 extraction[no-text 折入 NO_TEXT 句]/error→`error ?? "Subagent failed"`/else `"Subagent cancelled"`）；XML 包裹+metadata {source,childID,agent,state} 逐字段对位；admission=父 ACTIVE→steer/父 IDLE→queue+同锁 `_promote_next("input")`（TS resume:false 无 PY 悬挂面→恒唤醒映射记档诚实）。
- 锚3 观察者+durable 标记：标记形=**新最小 durable 事件 `py.subagent.backgrounded`**（锚7 预授权圈内；session-resource 定级 metadata={} 合法；独立元组保 R2 边界；选型+「为何非他形」全档：py.run.* metadata 侵入/p幂等原语单独不成观察记录）；notification_id=`subagent:{run_id}` 代际唯一+重启稳定；**前台完成零投递直证**（§4-5 测）；**消费 pop 在 `_apply_inbox_enqueued`**（synthetic 入场即 completeBackground ack——append-only 幂等：入场后无论 inbox 行后续 delivered/cancelled 观察已消费，重启零二投——幂等原语先例链延伸）；per-generation 恰一（fold duplicate-notification_id=IllegalTransition 拒，非静默覆写）；重启恢复=`recover_subagent_observations` post-scan follow-up（recovery_gate.wait() 后=recovery.py `_wake_pending_inbox_after_scan` 先例同形；全折叠→已投 pop→恰 outstanding re-arm；StoreError 逐父跳过不阻塞；R4-interrupted 在飞子经观察者自终态轮询投 cancelled 态恰一）。
- 锚4 block 转换：前台等待环 poll 双触发（`consume_subagent_background_request` 端点信号∪父 pending inbox 计数增长>基线快照）→停等+marker+backgroundResult（子不中断继续跑）；onInterrupt/600s/0.05s 诸 S5 形保位；begin/end finally 平衡（取消无 stale 转换靶）。
- 锚5 端点：`POST /session/{id}/background` 204 双臂（活跃转换=idle no-op——TS 契约 verbatim 结构化兑现：无注册即无可转换）；404=SessionNotFound 面；路由 25→26+SDK `background()` +1 方法+census `len(operations)==26`+pinned pair 入钉集（20→21）。
- 锚6 outstanding：席位钉面=TS 无 HTTP/内部消费面→不移植 DIFFERENT-BY-DESIGN 记档（诚实面）。
- 锚7 契约圈：contracts+两工件再生成（models 52→53/durable 34→35）+fixture 三态 136→139+schema-lock 钉翻新（139/38·73/53·35）+r5 档修订注+CORE-CONTRACTS §3.1 行+Changelog+generator 第十注册族——100 先例链全档。
- 锚8 非目标：管线 JobService 零触碰/policy+`_ASK_NAMESPACES`+agents 零触碰/TS shell·image 后台类不移植记档/S5 前台语义零回改（`subagent_final_text` 服务化=字节保位亲证）/S1 inbox 四事件语义零回改（admit_synthetic=唯一新入场）/`py.message.imported` 语义零触碰。

### 3. 测试面（§4 八组→13 测）— PASS

- 13 测名全图与 §4 八组逐组映射恰形（全链/三态×3/前台不投递/block 转换 queue 臂/端点双臂+SDK 真服务器往返×2/重开零二投/R4 折叠 cancelled 恰一/Spec×2）；db 级 events 探针+`_migrated_db` 迁移头腿+真 uvicorn SDK 腿（S1 §4-7 先例同形）；S5 11 测翻新保绿（Spec 9 钉+description 新值）。
- 零回归：席位 Run 全量 2338P+8S+0F（定向腿 397P 含 schema_lock/tool_subagent/sdk_client/inbox_steering/e2e_r4/cancel_e2e/recovery/events）。

### 4. 门禁与算术 — PASS（席位侧全档；调度器独立面待 accept 档定稿）

- 席位自跑四件（全前台收齐）：ruff 0 / format **590→591**（588+1 新 .py+1 untracked .md[票面拷贝]=590；进度档落盘后复跑 591——精确闭合零未解释漂移）/ mypy **223-0**（222+1 新测试文件精确闭合；canonical `mypy .` 形——席位自矫自 `src tests` 显式形 214 同绿非票形记档）/ 全量 **2338P+8S@694.09s** collect 2346（1 warning=aiosqlite 线程竞态族基线既有非本票贡献；install 族套内全绿=本窗零相位红）。
- 调度器独立四门禁（worktree p7-s2 真 python venv，`uv run --no-sync` 形，~04:1x-04:2x 时段）：**ruff 0 / format exit 0（`591 files already formatted` 与席位侧同数）/ mypy `Success: no issues found in 223 source files`（223-0 与席位同数）/ pytest `5 failed, 2333 passed, 8 skipped` @700.32s——5F=install 族五员同集+4551 逐员直证=相位窗时变判据链直接适用（092/093/095/096+S4 双态+S5 红绿双窗+S0 红绿红+S1 绿绿红；**本票三窗同树时变=绿绿红复现形**：派发探针窗绿[2322P+8S exit 0]→席位窗绿[2338P+8S 含 install 族]→验收窗红[5F+4551]——时变非树面缺陷；install 测试文件本票零触碰[18 改清单不含]=零因果交叠）；2333P+5F+8S=2346 collect=2330 基线+13 新测+3 fixture 链恰合；subagent/inbox/sdk_client/schema_lock/e2e_r4/cancel/recovery 邻接面零失败=本票面全绿**。CI 终证=CI-WATCH-254（#251 派发检查点 `6eb7206` CI-WATCH-253 已绿=同基线树 CI 终证先行面）。

### 5. 语义面独立复核 — PASS

- **标记消费语义独立核**：pop 于 `_apply_inbox_enqueued`（admission=commit point）> delivered/cancelled 消费面——已投 inbox 行 durable 驻留+观察 pop 幂等逐重启跳过=「入场即 ack」正确形；与 TS completeBackground（notify 承认后 marker 消费）语义等价且更稳（TS marker 进 consumed history 25 条界内存面；PY fold pop=append-only 幂等）。
- **CAS 突围重入独立核**：EventCommitter 异锁=提交突发窗根因直证（S1 admission 双臂同窗先例）；重入安全=入场门折叠已投代际+拒批零半态；末次再抛→durable-outstanding→re-arm 兜底=无丢失面（恒 at-least-once→pop 幂等收敛 exactly-once）。
- 三态映射独立核：SUCCEEDED→completed/FAILED→error+error_text/其余（interrupted[R4 fold+协作 cancel]）→cancelled——PY 终态全集诚实覆盖；running 不投递=观察环仅终态出环结构性达成（TS deliver running-return 语义同）。
- 队列臂 `_promote_next("input")` 同锁面核（S1 先例直接复用）；steer 臂=父活跃 run step 边界消费（S1 面）。
- close Phase 2.5 独立核：调度 run 全 settle 后取消观察者=无新交付面；mid-flight admission bounded 收完=零半态；请求/等待簿记随进程面清=正确（durable marker 独立存续）。
- idle no-op 结构性核：idle 无 foreground_wait 注册→request 返 False→路由恒 204——TS 契约 verbatim 无需特判。
- `subagent_final_text` 服务化核：工具旧 `_final_text` 逐字节迁移（cursor-paged 到真尾+texts join `\n`[TS text parts join `\n\n` 差异=S5 既有披露面沿记]）+两消费面共享=同子 run 恒同文本。

### 6. 披露与记档 — PASS

- §5 八项+席位扩充至 12 项全档（标记形选型论证/resume:false 两处 N/A 映射/block 轮询形/outstanding 钉面结论/终态映射表/契约触碰全档/新测试文件选型/算术/矫正二例/join 形/端点 convert-only/fail-closed 面）；席位弧时序全档。

## 发现明细

- **建议 1（观察面，无动作）**：进度档 collect 分解标「2333 既有+13 新【票钉 2330 之 +3=中间票漂移——席位间调度器合入】」标签误——+3 实为席位自身 3 新 fixture 参数化链（S1 评审建议 3 已立 fixture 链=collect 增量来源面；本票 136→139 恰 +3），非调度器合入。总数 2346=2330+13+3 恰合，零实差。
- **建议 2（观察面，无动作）**：交付文本 join 形 `\n`（S5 完成面同源）vs TS text parts join `\n\n`——席位已诚实记档（S5 既有披露面沿记非本票新差异）；后继若 TS 对齐需求出现另票处理。
- **建议 3（流程面，无动作）**：主窗看哨 EXPIRED 后台壳报退码 1（脚本面 exit 5）——壳退码面不可信又一例（S1 accept 壳早退先例同族）；哨档（wlog EXPIRED 行）=真信号，后继看哨判定一律档面优先。

## 附录：席位弧时序

- #251 派发检查点 `6eb7206` push→worktree p7-s2 建面一次过（venv 3.12.16+69 包）→探针四腿全绿（**cmd /c 引号面首跑假腿 4×语法错——S2 脚本写入形 `` `\" `` vs S1 工作形 `` `" `` 字节差；修后重跑 ruff 0/format 0/mypy 0/2322P+8S@810.94s collect 2330 恰合**）→CI-WATCH-253 success（run 43743704912）→双门齐发射 C 席（`ses_orch_C_p7s2`）02:13→单段闭合：亲读锚定（TS 七面+PY 全谱）→设计五点细化→实现（contracts/state/projector/repos/inbox/service/app/tool/httpapi/sdk/census/S5 翻新/契约链 10 面/新测试 13 例）→机制矫正二例→门禁四件全绿（694.09s 前台收齐）→进度档全档→SEAT-EXIT 0 @~04:0x；主窗 100min EXPIRED（round 67）+尾窗一 TRUE-EXIT=活跃超窗形；零 git 写（HEAD 恒 `6eb7206`）。
