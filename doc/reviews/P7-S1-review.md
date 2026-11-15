# P7-S1-review — T-C1-100 微评审：steering inbox+interrupt——运行中会话可转向性（#7 复评票，P7 主池 S1）

- 评审对象：worktree `p7-s1` 席位工作区（未提交态，席位 git 写零）：改 19 跟踪文件[doc/CORE-CONTRACTS.md/doc/reference/r5-fixture-inventory.md/两 schema 工件（event-envelopes.json+events.json）/src app.py+contracts/events.py+httpapi/app.py+sdk/client.py+session{projector,runner,service,state}.py+storage{repos,txn}.py/tests contract/test_schema_lock.py+integration/test_migrations.py+test_sdk_client.py+test_service.py/tools/r5/generate_schema_artifacts.py] + 新 3[`session/inbox.py` 386L；`storage/migrations/versions/0004_inbox.py`；`tests/integration/test_inbox_steering.py` 10 测] + 12 契约 fixture（四事件×valid/missing_field/bad_extension——r5 三态形）+ 进度档 + 票面拷贝 untracked——**面广恰合 M 级票预判**：契约预授权圈（§3-2 四事件+§3-8 CORE-CONTRACTS 最小增补+062/094/098 先例链）内触碰全记档。
- 评审基据（全部独立现读，禁继承）：inbox.py 全图（docstring 契约段+执行体）、service.py diff 逐段（+306：prompt `@overload` 双签名/step2.5 parked-retry 识别四分支/step3-4 admission 双臂/InterruptReceipt/`_promote_next`/`wake_pending_inbox`/`consume_steer`/inbox 三公面/`interrupt(resume=)`）、contracts/events+projector+state diff、runner.py+app.py diff（step 边界 hook 先于 re-fold/None 保位/扫描后 wake task+scan 死锁防护注释/close 取消序/late-bind steer hook）、httpapi 4 路由+delivery 手解析、sdk/client.py diff（+201：三 wire 类+4 方法+overload 透传）、state/repos/txn 折叠面 diff、service 尾段机制核、进度档全文、测试图（test_inbox_steering.py 10 测）、census 落点独立核证（test_sdk_client.py:955 `len(operations) == 25` 路由钉=席位翻新面；test_httpapi_server adapter census=工具面零触碰正确未动；test_schema_lock:57 钉 census 与 r5 档闭合同数）、TS 钉面语义对照（inbox.ts 628L/run-coordinator.ts 174L 全图/prompt.ts/session.ts 四端点——调度器派发前亲读）。
- 评审时刻：2026-11-16 01:2x-01:5x（席位弧单段闭合：发射 11-15 ~23:08→SEAT-EXIT 0 @~01:2x 11-16——三窗看哨超窗形：100min 主窗+两 30min 尾窗均为席位活跃超窗非挂死；全前台 Run B 672.09s+进度档全档；席位 git 写零——HEAD 恒 `65a5582` 调度器两时点亲证）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 4（全观察面，无返工）

## 六面逐条结论

### 1. 票面纪律 — PASS

- 禁 git 写遵守：席位全程 HEAD 恒 `65a5582`（调度器独立核两时点：席位窗前派发探针+验收时点；工作区=19 改+3 新源+12 fixture+进度档/票面拷贝 untracked 恰合席位终态）。
- 单段闭合（S0 纪律延续）：全量 pytest 前台收齐（席位遇 ctx_shell 120s 帽自矫正为 native shell 前台收齐——**前台纪律保持，合法工具面**）+进度档全档后 SEAT-EXIT 0——零续跑令。
- **Run A 10F→三根因修正全档**（全为实现内矫正，非测试迁就）：①dedupe 自撞 6 腿→**unkeyed-batch 裁定**（见 §5）；②httpapi 422→delivery 查询参数手解析；③迁移头钉→`0004_inbox` 头钉翻新+test_migrations 钉同步。修正后 Run B 全绿套内含 install 族=红绿双窗。
- §5 八项披露余量全落位+席位余量扩充（进度档全档）。

### 2. 决策锚落面（§3 十锚）— PASS

- 锚1 delivery opt-in：prompt `@overload` 双签名——**缺省 None=SessionBusyError 面逐字节不变**（默认签名=既有调用面零迁移；运行时四步序 verbatim 保持）；显式 steer/queue→admission 路径。TS 默认 steer vs PY 显式 opt-in=DIFFERENT-BY-DESIGN 记档兑现。
- 锚2 durable inbox 四事件：`py.inbox.enqueued/cancelled/delivery_changed/delivered` version=1；**session-resource 定级**（非 run-scoped——inbox 条目跨 run 存续，与 py.session.*/py.message.* 同族语义正确）；独立 `INBOX_EVENT_DEFINITIONS` 元组（保 R2 边界的独立加法面）；两 schema 工件再生成+12 fixture（r5 三态形）+r5-fixture-inventory 档翻新（124→136 fixture 链+§1 工件格现数 events.json 52+revision note）+`tools/r5/generate_schema_artifacts.py` 工具面同步。
- 锚3 steer 消费：runner step 边界 hook **先于 re-fold**（delivered 事件与 fold 序一致——EventCommitter 事件先/fold 后既有纪律）；每步至多一条（next-in-line）；None 面=hook 仅在 inbox 在场时点火=缺省面逐字节不变。
- 锚4 idle-wake 门铃：`_drive_run` settle 后 `wake_pending_inbox`——**per-session 锁内 durable 复查**（无 in-memory 信号=门铃状态即 durable 状态，重启安全）；`_promote_next` 门铃（update 翻 steer 亦触发）；app.py 扫描后 wake task（**gate-then-wake 非 scan 内 commit**——scan 内 commit 经 gated store 会 await 扫描自身=自等待死锁，注释与实现咬合直证）+close 取消序纳入 wake task+late-bind steer hook。
- 锚5 interrupt：cancel 机器包装 reason="user"（非新取消机器——cancel/close/recovery 既有语义零回改兑现）；InterruptReceipt；`{interrupted: bool}`（true=活跃执行被断/false=idle no-op）；**resume=true 仅驱动最老 pending steer**（单继任形——每步至多一条锚的一致延伸）；queued 保停泊（断言不消费直证）。
- 锚6 inbox 三端点：list（入队序 pending-only 读模型——projector 折叠面）/cancel（未交付可撤；已交付/已撤=no-op 200 False——TS unavailable=no-op 同义）/update（delivery 变更 re-arm；steer 翻转=门铃；已终态条目=ConflictError 结构化拒）。
- 锚7 SDK：+201 行三 wire 类+4 方法（inbox_list/inbox_cancel/inbox_update/interrupt）+prompt delivery 透传（overload 形）。
- 锚8 路由 21→25：app.py 四路由恰形（三 inbox+interrupt?resume）；census 钉翻新=`test_sdk_client.py:955 len(operations) == 25`（席位亲证面；新测试 §4-7 腿引用自证）。
- 锚9 CORE-CONTRACTS：§3.1 四 `py.inbox.*` 事件行+§7 opt-in steering 段（delivery 面/keyless-batch 裁定/step-2.5 retry/runs-unique 结构 backstop）+Changelog；**缺省面句零触碰**（§3-8 最小面纪律兑现）。
- 锚10 恢复面：R4 启动扫描后 pending inbox 保留+idle-wake 接续（重启腿测试直证）；txn rebuild 重投影面同步。

### 3. 测试面（§4 八组→10 测+基线翻新四处）— PASS

- 10 测全图结构核：入场矩阵（busy+None 拒/busy+steer 入/busy+queue 入/idle+steer 即刻消费/idle+queue 即刻消费）/idle 原子性/step 边界消费/auto-wake（settle→run B 续跑）/门铃不搁浅不重复/interrupt+resume/queue 保停泊/端点 no-op/重启 wake/SDK 往返——§4 八组全覆盖恰形。
- 基线翻新四处（超票面「两 census」预判——加法面全记档）：test_schema_lock（事件定义钉+fixture census 与 r5 档闭合同数）/test_migrations（0004 头钉）/test_sdk_client（operations 21→25+4 方法往返）/test_service（inbox 服务面）；adapter census（test_httpapi_server）=工具面零触碰正确未动。
- 零回归：Run B 2322P+8S 全绿套内含 install 族（红绿双窗）+e2e_r4/subagent/recovery 邻接面零失败。

### 4. 门禁与算术 — PASS（席位侧全档；调度器独立面占位待核证定稿）

- 席位自跑四件（全前台收齐）：ruff 0 / format **588 files exit 0**（583+3 新 .py+2 新 .md 精确闭合；ruff 0.17.3 计数 Markdown 机制披露——doc 364+README 1+Python 223）/ mypy **222-0**（219+3 新文件恰合；**首跑 66 union-attr 经 overload 源级修正归零**——非 `type: ignore` 面）/ 全量 **2322P+8S@672.09s** collect 2330=2308+10 新测+12 fixture 链恰合（+22 形算术全档）。
- Run A 10F→Run B 全绿=席位窗内红绿双窗（install 族含内）。
- 调度器独立四门禁（worktree p7-s1 真 python venv，`uv run --no-sync` 形，01:2x-01:4x 时段）：**ruff 0 / format exit 0 / mypy `Success: no issues found in 222 source files`（222-0 与席位侧同数）/ pytest `5 failed, 2317 passed, 8 skipped` @676.11s——5F=install 族五员同集+4551 逐员直证=相位窗时变判据链直接适用（092/093/095/096+S4 双态+S5 红绿双窗+S0 三窗同树双态；**本票三窗同树时变新形**：派发探针窗绿[2300P+8S exit 0]→席位窗绿[2322P+8S Run B 含 install 族]→验收窗红[5F+4551]——时变非树面缺陷；install 测试文件本票零触碰[19 改清单不含=numstat 证]=零因果交叠）；2317P+5F+8S=2330 collect=2308+10 新测+12 fixture 链恰合；inbox/SDK/census/e2e_r4/subagent 邻接面零失败=本票面全绿**。CI 终证=CI-WATCH-252（#249 派发检查点 `65a5582` CI-WATCH-251 已绿=同基线树 CI 终证先行面）。

### 5. 语义面独立复核 — PASS

- **unkeyed-batch dedupe 裁定独立核**：store unique(session_id, dedupe_key) 使 keyed batch 单行化；accept-carrying inbox commit 天然多行（admission+事件同批）→带键=同批自撞（Run A 6 腿失败根因直证）→三形批次一律去键=结构自洽；**runs-unique 既有约束=结构 backstop**（§7 段记档）。
- session-resource 定级独立核：inbox 条目 enqueue 于 run 活跃时、deliver 于 step 边界或后继 run——生命周期跨 run→非 run-scoped 正确。
- 门铃闭隙独立核：TS pendingWake（内存信号闭检查-迁移间隙）vs PY durable 锁内复查形——**PY 无内存信号面=门铃状态即 durable 状态**（重启后 wake 接续腿直证）；update 翻 steer 的 `_promote_next` 门铃同形。
- scan 死锁防护独立核：wake task 先 await gate（幂等起扫描）再 commit——「scan 内 commit 会 await 自身任务」注释与 gate-then-wake 实现咬合；close 取消序纳入=关闭竞态面闭合。
- step 边界 hook 先于 re-fold：事件序/fold 序一致性独立核（EventCommitter 纪律延伸非新序）。
- InterruptReceipt/resume 语义核：resume=true 单继任（仅最老 pending steer）+queue 保停泊——TS「pending steering+next-in-line」的 PY 收敛形自洽（每步至多一条锚）；interrupt 复用 cancel 机器 reason="user"=用户面包装语义钉兑现。
- overload 双签名核：mypy 66 union-attr 首跑根=overload 后 delivery None/str 分支窄化——源级修正（运行时分支显式化）非 suppression，正确面。

### 6. 披露与记档 — PASS

- 票 §5 八项全落位+席位余量至 16+ 条（进度档全档：dedupe 裁定三形/httpapi 422 根因/迁移头钉/66 union-attr 源级修正/format 588 机制/collect +22 算术/fixture 链 124→136/late-bind hook/scan 死锁防护/census 现数/三窗超窗时序等）。
- 席位弧时序单段全档。

## 发现明细

- **建议 1（观察面，无动作）**：三窗看哨超窗形（100min 主窗+两 30min 尾窗均为席位活跃超窗）——M 级大面票席位时长超主窗为常态形；后继 M–L 票（S2 SubagentJob）看哨预算可预裁「100+30+30 三窗形」或加长主窗（调度器裁量项）。
- **建议 2（观察面，无动作）**：format 588=583+3 .py+2 .md 精确闭合（ruff 0.17.3 计数 .md 机制披露）——后继席位 format 基线按 588 起算。
- **建议 3（观察面，无动作）**：collect 2330=+22 形（10 新测+12 fixture 链参数化）——fixture 链=collect 增量新来源面，后继契约事件票算术须计入 fixture 链增量（非仅新测试数）。
- **建议 4（观察面，无动作）**：Run A 10F→三根因实现内修正全档（非测试迁就）——「机制矫正先例条款」正面范本延续（S4/S5 先例链第三例）。

## 附录：席位弧时序

- 派发 #249 检查点 `65a5582` push（INSERT OK 22:51，numstat 1/0 钉）→worktree p7-s1 建面（venv 3.12.16+69 包一次过）→四探针全绿绿窗（2300P+8S@818.31s）→CI-WATCH-251 success（run 43726619940）→发射 C 席（`ses_orch_C_p7s1`）→单段闭合：亲读锚定（TS runner 消费序钉面=提案 §3-B-#7 风险钉落位）→实现+Run A 10F→三根因修正→Run B 全量前台收齐（672.09s）→进度档全档→SEAT-EXIT 0 @~01:2x→三窗看哨（主窗 EXPIRED round 67 @00:49+尾窗一 EXPIRED round 30 @01:19+尾窗二 TRUE-EXIT——席位活跃超窗全程）；零 git 写（HEAD 恒 `65a5582`）。
