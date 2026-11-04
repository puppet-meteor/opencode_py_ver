# R3-S8 评审报告（席位 B · T-B-034）

- **评审对象**：worktree r3-s8 B-baseline `7b64d42` vs 基线 `dbc0046`（=main 检查点#129 文档提交；代码面=#128 `41961cf` 零差量）。
- **写面对账**：恰 6 修改/新增路径 ↔ 6 授权项 1:1（name-only+numstat 亲证，+1768/−4：progress 46/0、r3-exit-evidence.md 1016/0、cli.py +6/−3、test_cli_r3.py +36/−1、test_e2e.py +9/−0、test_e2e_r3.py 655/0——任务书 per-file 面额 "+9/−4"/"+37/−1" 与实测有出入，系调度器侧笔误非 C 缺陷，总量闭合）；冻结面 diff 亲证为空；树净（仅 `?? doc/orchestration/logs/`=调度器 watcher 自建 21:46 起、非 C 写入、进度⑤如实注记）；单提交自证。
- **调度器已验门禁（B 不复跑全量）**：pytest 922 = 918+4 / ruff clean / format exit 0（席报 268=docs 落盘前读数 vs 调度器 270=落盘后读数，两读皆 exit 0，计数非判据，r-2 先例同档）/ mypy 92 = 91+1。B 抽查=collect-only 亲跑 922 ✓。
- **B 独立复核面**：③漏报抽查 13 项亲跑实录（§4）；6 修改文件 diff 全读+两新文件逐体（e2e 655L/evidence 1016L 全量）+进度 46L/任务书 50L/日志尾 150L 全读+契约六锚原文（§5.2 L265-268/§5.3 L276/§7 L316/§8 L354/§10 L424/§3.1 L136）+PHASE1-SLIM L87-96 原文+四层披露链 4 锚亲读+点名⑤ 抽样 7 锚亲证+附录 A 逐 node-id 与 B 亲跑 collect-only 双向比对。
- **结论：PASS（按现状合并）—— blocker 0 / major 0 / minor 1 / nit 2**（minor-1 处方归行#34 票面前提更正+台账记档，非 C 侧返工、非合并阻断；开放点六项不预写裁决）。

## §1 十二维逐维

**维 1 · 契约锚定**：§7 L316 原文亲证（`reply_permission` 宿主输入通道；与运行锁分离）——e2e 活审批腿兑现（run 等待中并发审批任务逐一 `reply_permission(pid,"once")`，测试①实测跑通）。§5.2 L266 档位表 vs `policy.py` `_DEFAULT_EFFECTS` L75-83 逐键对拍全中（fs read/glob/grep/ls=allow，write/edit=ask，bash__run=ask+L85 namespace 缺省 bash/MCP ask）——e2e 分腿实测：gen1 三 ask 各真实 requested、gen2 四 allow 零审批。§3.1 L136 once 词汇（`py.permission.replied` reply: once/always/reject）。§8 生成零拼接=行#5 双面证明语义（历史含 interrupted assistant 与既有零拼接断言并列同测；PHASE1 L83 验证原文+CORE-CONTRACTS L354 原文双锚）。S7 事件纪律三测逐处核对 ✓：raw sqlite 计数读全部严格在终态后+App close 后（三测事件账目读均位于 `async with App` 退出+`server.aclose()` 之后），唯一活读=审批 mid-run 轮询 `_await_permission_ids`（sanctioned，connect-per-poll+OperationalError 重试，测试 docstring 自declared）。

**维 2 · e2e 全链忠实度**：655L 全读。①六工具串行链真实性——Windows 真实解释器 `Get-Content note.txt`（探测链回退面：pwsh absent 注记、powershell 承腿真跑）、rg seam 沿 trio 模式（`_real_rg_or_skip` PATH→bundled VS Code rg 真二进制零 fake+`_RG_CACHE={}`/`_which_rg` monkeypatch）、tmp 真实文件读写（磁盘终值逐字断言 `"gamma line\nbeta marker line\n"`）、fs__edit `oldString` 精确替换 alpha→gamma（精确冲突语义的匹配面）。②EventLog per-tool 账目——execution_id join（`names_by_execution`）+`Counter` per-tool==1×7+state={succeeded}+effect_status={completed}+`py.tool.started` permission_decision={allow}。③未答审批到期——`approval_timeout_s=0.05` 无人回复→`closed(expired)`=1、工具折叠 cancelled/not_started、**副作用从未发生**（`note.txt` 不存在断言）、run 仍 succeeded。wire（`requests[1]/[2]` tool results 逐字回环）/store（messages page 类型序列+status+outcome）/file（磁盘真相）/ledger（event_types 计数+requested_actions+replies 逐字）四面断言完备 ✓。DB 移出 working_dir 注释如实引 progress（定界见 minor-1/nit-1）。

**维 3 · 点名① 审批算术对账**：3 requested / 3 replied "once" / 0 saved / 0 closed == 3 ask 工具（write/edit/bash）✓——EventLog SQL 双测断言（测试①②）+wire 侧 tool results 逐字回环（工具真实执行的 wire 证词）双侧对账；`replies==["once","once","once"]`+`requested_actions==["fs__write","fs__edit","bash__run"]` 逐字断言。**无 `rules=` 简化**——App 构造体无 rules 参数（测试源亲证）+进度①明记"未用 rules=（App 默认档位）"取舍 ✓。

**维 4 · 行#5 断言语义**：test_e2e.py **恰一 hunk 零越界**（diff 亲证 +9/−0）；追加断言组=`fresh_request` 中 assistant 恰 1+content=="partial tex"（历史含残段面=ContextBuilder 正常选择）与既有 `fresh_request[-1]==user "start over"`+scripted 精确收尾断言**并列=双面同测对照**；r-1 处方原文（`R2-S8-closeout-review.md` L87，B 亲读）逐字对照语义忠实；不增测（test_e2e.py 计数仍 3，collect 亲证）、不改既有断言 ✓。

**维 5 · 行#33 回归批对账**：三面全落——(a) nit-2 resolve-70 注入测（`--config` 携 context_limit=100+output_reserve=200→`resolve_runtime` ConfigError→exit 70+stderr "the resolved config is not a legal runtime face"），与既有 migration 注入面（`test_migration_failure_maps_to_exit_70`）互补不重复 ✓；(b) 泵回归钉显性化=P-2 测试 docstring 扩展，语义逐点与 B-S7 追迹分析相符（`include_pushback=False` read+`push_back` deferral=修复面；回归则"wrong queue→dead-lock at next prompt read"）✓；(c) cli.py 注记批逐行亲证**全注记零行为**（两处 "§5.3 L277"→"§5.3" 去行号化[注释 L156+docstring L172]、泵 KI 后行为注记 3 行、`_print_history` docstring "/" 回补）✓。

**维 6 · ④强制浮报完整性**：任务书预列五项对照进度③全落——(1) e2e 外审批分支注记引用 ✓（进度③首段+测试模块 docstring "deliberately NOT re-woven"+test_cli_r3/test_approval/test_fs_multiroot 承重注记）；(2) bash 探测链回退分支 ✓（pwsh absent/powershell 承腿/回退未触发如实记）；(3) 次根面已测不重复 ✓（e2e 单根注记）；(4) projector 单次测量非监控 ✓（进度③+evidence L78）；(5) Unix 分支不伪造 ✓（A-PLATFORM 纪律沿 S3/S5+CI 复验归调度器）。席位自报缺陷档详实（src 楔死链四层+触发实证）✓。宁缺毋假核对：测试面浮报 0；定界主张失实 1（minor-1）；披露清单精度 1（nit-2）。**整体诚实档成立**。

**维 7 · 点名② src 缺陷披露链**：四层链全锚亲读成立（§2-② 详）。

**维 8 · 点名③ DB 移出 workaround**：合法性裁定（§2-③ 详：测试适配正当；部署形态佐证不成立→minor-1）。

**维 9 · 点名④ projector 测量与判定**：方法+数字+外推+裁决合规全核（§2-④ 详）。

**维 10 · 点名⑤ evidence 行号重锚+行#4 枚举闭合**：头部 L6 声明（"保留的逐行 file:line 均为本任务亲证"+继承 r1/r2 未复核项入进度③删节清单）**抽样亲证 7 锚全中**：projector.py L251 `_FoldCacheEntry`/L314 `_fold_cache`/L475-488 `_project_permission`/L515-530 `_remember_fold_state`、process/runner.py L162-181 `_kill_tree`/L184-287 `run_command`/L290-308 `_capture_in_job`、grep.py L148-192 rg 发现链、service.py L898 `reply_permission`。行#4 **三方闭合**：附录 A 隔离计数恰 **922 node-id**（全文 '::' 935=附录 922+正文引用 7+含中文后缀 touched 行 1，口径核实）== B 亲跑 collect-only 922（**Compare-Object 双向零差**）== 门禁 pytest 922 ✓；§0 全 **16 个文件级计数**与 B 亲跑逐文件分组对拍全中（test_cli_r3 30=29+1、test_e2e 3 不增测、test_e2e_r3 3 等）。

**维 11 · 点名⑥ 验收 A/B/C+L94/L96 映射完整性**：PHASE1-SLIM §4 L87-96 原文逐行对照 evidence §1-§4+§6——A→S3/S4 ✓（write/edit 精确冲突+原子替换→S3[36 专测+e2e 端到端]；glob/grep/ls 限额+越界/超大/二进制+rg 依赖检查→S4[36 专测+trio rg-present 15 测 0 skip]）；B→S2/S7 ✓（档位表/once·always·reject/到期→S2；独立通道不被 active run 锁死→§7 L316+S7 CLI 面+e2e mid-run reply 实测；无人审批拒绝→S2 专测+e2e 测③；条件更新竞争/首合法胜出/迟到 ApprovalExpired·Conflict→S2 test_approval）；C→S5/S6 ✓（超时/收树/输出限额防死锁→S5[14 专测+e2e bash 真跑侧证]；项目配置不扩宿主→S6 loader；重启按快照恢复→S6 snapshot v2+test_rebuild 17）；L94 ✓（每工具真实临时 workspace+Policy 表驱动 21+Windows 真跑/Unix 不伪造注记）；L96 ✓（saved 决定同一事件路径零独立 JSON→`_project_permission`+test_permission_projector 11+e2e saved=0 算术侧证；deny 清单 advisory 非沙箱→bash.py `_deny_label`）。**映射不虚不漏**（未复核的 S1-S5 中段门禁数字/审查记录如实标"台账为准"）。

**维 12 · 写权限面+冻结面+覆盖对账**：name-only 恰 6 路径 1:1=6 授权项（3M+3 新，excl. logs/）✓；冻结面（contracts/service/invoker/registry/app/policy/loader/context/projector/state/storage/process/builtin/pyproject/uv.lock）B 独立 diff **输出为空** ✓；覆盖算术 922=918+4 ✓（3 e2e_r3+1 nit-2，collect 亲证）；TS 锚纪律：evidence 头部 L7 声明"本切片无新 TS 引用（收口）"+正文 L1-82 通读零 TS 引用（grep.py 源内 "grep.ts L162" 系 S4 冻结面既有注释非本批新增）✓；watcher `logs/` 归属调度器自建、C 零触碰 ✓。

## §2 点名复核六项（独立结论）

**① 审批算术对账——成立**（维 3 详）：3/3/0/0==3 ask；EventLog SQL 双测+wire 工具结果回环双侧；无 `rules=` 简化（源码+进度①双证）。

**② src 缺陷披露链——四层链全锚亲证成立**：(1) `grep.py` `_execute` **L369-374**：`returncode ∉ (0,1)` 且非早停→优雅 `_failure(TOOL_FAILED)`（rg 撞锁 exit 2 实证入口）；(2) `invoker.py` execute 异常路径 **L1092-1117**：adapter 异常→`_finish(state="failed", effect_status=not_started[无副作用]/unknown, error=CoreError(TOOL_FAILED))`（优雅 error-result 同面于 L1119 续接）；(3) fold 校验拒绝该迁移（`StorageCorruptError: cannot finish failed/not_started from running`，进度实录）；(4) `runner.py` `_run_tools` **L912-970** 仅特判 StoreError-cancel 竞态（L952-960）余者原样重抛→`service.py` `_drive_run` **L1221-1239** `except Exception → _schedule_errors[run_id]` 吞没→**run 永久 "running"，无终态、无用户可见错误**（仅 cancel/close 时经 LifecycleCleanupFailed 侧溢）。独立判定=**生产级缺陷**（触发面=started 后任意优雅 TOOL_FAILED；用户可见性=静默楔死；数据一致性=active run 永不释放）。"e2e 暴露→停下浮报不修"纪律兑现：三处涉面全在冻结面、零触碰、测试侧仅环境适配+四层浮报+修复票诉求 ✓。

**③ DB 移出 working_dir workaround——测试适配定性正当；部署形态佐证不成立（minor-1）**：适配本身正确（e2e 不应钉缺陷行为；`db_dir=tmp_path.parent/<name>-db` 为确定性环境构造；evidence/测试注释披露为"测试环境适配非缺陷修复"）。但进度①+evidence §6 L76 的"**真实部署形态本就如此：DB 不在项目目录内**"与实测相反：`cli.py` **L205** `--db` 默认 `./opencode.db`（L369 `Path(args.db).resolve()` 落 cwd）+`app.py` **L144** `working_dir` 缺省 `os.getcwd()`→**默认部署形态 DB+其活动 writer.lock（db.py L43 数据目录锁、App 开启期持有）恰在 session working_dir 内**，`fs__grep "."` 默认形态即可命中→楔死触发面**默认可达**。掩缺陷风险评估=无（缺陷全量浮报+票诉求、适配不改变开放状态）；与行#34 关系=票面可达性前提须更正（见处方）。

**④ projector 测量方法与数字——成立，判定 concur**：方法=真实 App（fake provider SSE）+120 会话×6 prompts=720 runs/2160 消息；`_fold_cache` 恰 120 条目=每会话一条（与 L314 dict 逐 session 加性设计同构、无淘汰面可测量）；gc referents 深度 sizeof **5,131,733 B≈4.9 MiB**（≈42,764 B/条、18 消息/会话）；tracemalloc peak 12.8 MiB；测量脚本 Temp throwaway 在册亲证（4684 B，22:22:51）。线性外推正当——缓存结构性逐会话加性、消息深度受 6 prompts 定界、千会话 ≈42 MB 量级温和；"单次测量非监控/增长曲线未穷举"如实注记。判定=**不成立记档零动作**，与 T-A-010 §2 裁决原文（CORE-CONTRACTS **L424** B 亲读："R3 若实测长期进程内存压力成立，再由调度器授权容量/淘汰票并验证淘汰后冷重放等价、篡改检出及回滚恢复，本裁决不预定 N"）合规——未establishing 压力、未预定 N、evidence §6 L77 记档落位 ✓；R4+ 观察维持恰当。

**⑤ evidence 行号重锚声明+行#4 枚举闭合——成立**（维 10 详）：声明在册+抽样 7 锚全中+三方闭合（附录 922==collect 922==门禁 922，双向零差）。

**⑥ 验收 A/B/C+L94/L96 映射完整性——成立**（维 11 详）：不虚不漏。

## §3 必答三项

**1（src 缺陷独立立场）**：**生产级判定 concur，且经 B 独立实锚强化**——默认部署形态（`--db ./opencode.db` 落 cwd=working_dir）下 `fs__grep "."` 即可命中活动 writer.lock（db.py L43/L106-112 数据目录锁语义）→楔死**默认可达、非边缘场景**。行#34 票范围正当性 concur：吞没链拆解三处（invoker 失败映射×fold 合法迁移集×`_drive_run` 错误面）与四层链逐面对应、修复必裁"failed/not_started 自 running 迁移合法性"（迁移集合法化或 invoker 侧改射二择）+`_drive_run` 错误面折终态，拆解恰当不虚不漏；rg skip 语义对拍 grep.ts 并入票评估恰当（同簇不可读文件部署语义）；冻结面去行号化"顺带"包络=裁量可支持，但**文件清单须更正**（nit-2：§8 L350 承载=maas.py×4[L11/L35/L169/L289]+session/runner.py≥8[L30/L42/L336/L587/L681/L774/L800/L999]；process/runner.py 为 §5.3 L277×7[L1/L3/L20/L28/L99/L163/L194]），且去行号化文件与修复触碰面（invoker/service/fold/session runner）不同文件，是否同票顺带归调度器裁量。

**2（projector 判定独立立场）**：**concur 不成立记档零动作**（点名④ 详：方法真实+数字自洽+外推结构成立+裁决原文合规+记档落位）；R4+ 观察项维持恰当（不预定 N 与单次测量注记双守）。

**3（47min 过程完整性）**：**兑现**。单段零失联——watcher 哨四点（10/20/30/40m）全 idle=0m、size 连续增长 38,616→80,410→169,172→225,370 B 无平台段；C 自报 ~50min、止损 120min 内、最终 pytest 前台本回合完成（198.62s 亲录）；③④档完整性=预列五项全落+自报缺陷档详实+删节项如实注记（唯一出入=minor-1 定界主张+nit-2 清单精度，均非测试面浮报）；授权清单外零越界（git status 仅 6 授权面+调度器 watcher logs/；Temp throwaway 脚本不入库在册核实）。

## §4 ③漏报抽查记录（B 亲跑实录）

1. `git log -1` → `7b64d42 S8 B-baseline: …`（单提交自证）✓
2. `git status --porcelain` → 仅 `?? doc/orchestration/logs/` ✓
3. `git show 7b64d42 --stat` → 6 files +1768/−4 ✓
4. `git diff dbc0046..7b64d42 --name-only` → 恰 6 路径 1:1 对账 6 授权项 ✓
5. `git diff dbc0046..7b64d42 --numstat` → progress 46/0、evidence 1016/0、cli.py **+6/−3**、test_cli_r3 **+36/−1**、test_e2e +9/−0、test_e2e_r3 655/0（和=1768/−4 闭合；任务书面额出入记档）✓
6. 冻结面 diff（contracts/service/invoker/registry/app/policy/loader/context/projector/state/storage/process/builtin/pyproject/uv.lock）→ **输出为空** ✓
7. collect-only 亲跑 → **922 ids+"922 tests collected in 1.14s"** ✓
8. 附录 A 隔离计数（L83 起 `^\S+::\S+$`）→ **922 node-id**；与我亲跑 collect-only **Compare-Object 双向零差**（ONLY_IN_EVIDENCE=0/ONLY_IN_COLLECT=0）——三方闭合 ✓
9. §0 全 16 文件级计数 vs B 亲跑逐文件分组对拍全中（test_cli_r3 30=29+1、test_e2e 3 不增测、trio 36、write_edit 36、approval 12、policy 21、permission_projector 11、rebuild 17、migrations 6、config 16、config_loader 30、bash 14、fs_multiroot 5、tool_listing 16、permission_events 41、cli 13）✓
10. 点名⑤ 抽样 7 锚亲读全中（projector L251/L314/L475-488/L515-530、prunner L162-181/L184-287/L290-308、grep L148-192、service L898）✓
11. 四层披露链 4 锚亲读（grep L369-374/invoker L1092-1117/runner L912-970/service L1221-1239）✓
12. Temp 取证 `fold_cache_meas.py` 在册（4684 B，2026/11/4 22:22:51，throwaway 不入库）✓
13. writer.lock 部署链亲证：db.py L43 `WRITER_LOCK_NAME`（数据目录锁、开启期持有）+cli.py L205 默认 `--db ./opencode.db`+L369 resolve+app.py L144 `working_dir` 缺省 `os.getcwd()` → 默认形态 DB 在 working_dir 内 ✓

## §5 可执行处方

- **minor-1（部署形态主张更正）**：进度①+evidence §6 L76 "真实部署形态本就如此：DB 不在项目目录内" 与实锚相反（cli.py L205+app.py L144：默认 `--db ./opencode.db` 落 cwd=working_dir，DB+writer.lock 默认即在项目目录内；楔死触发面**默认可达**）。**处方**：(a) 调度器在行#34 修复票面按"生产默认形态可达"定级授权；(b) "部署注记：会话 DB 勿置于工作目录内"可作 guidance 保留，但"真实部署形态本就如此"句须在后续 doc 批删正（evidence 为已交付件不回改，记台账）。载体=本报告+调度器台账。
- **nit-1（陈旧注释理由）**：test ① ls 断言用 containment 的注释理由"db side files are environment-dependent"系 DB 移出前的残留——移出后 tmp 内 ls 时仅 note.txt，理由不再适用（断言仍有效，可升等值）。**处方**：下次触碰 test_e2e_r3.py 时升等值断言或改注。
- **nit-2（披露清单文件归属）**：进度③注记"'§8 L350' 该锚在冻结的 maas.py/process/runner.py"不精确——实查 §8 L350 ∈ maas.py×4+session/runner.py≥8；process/runner.py 的陈旧锚为 §5.3 L277×7。**处方**：④-3 去行号化授权票文件面按 B 实查清单为准。

## §6 开放点（R4+ 登记级，不预写裁决）

1. **行#34 修复票**：吞没链三面+`_drive_run` 错误面折终态+rg skip 语义对拍 grep.ts（含不可读文件部署语义）——前提按 minor-1 更正后授权；修复批建议补"优雅失败→run 明确终态"回归钉。
2. **冻结面行号锚去化授权批**：§8 L350（maas.py×4、session/runner.py≥8）+§5.3 L277（process/runner.py×7）——文件面以实查为准（nit-2）。
3. **A-PLATFORM Unix 分支 CI 复验时点**（killpg/start_new_session/Unix 解释器链）——归合并流程。
4. **projector 持续监控缺口**（单次测量、增长曲线未穷举）——维持 R4+ 观察，触发条件按 T-A-010 §2"实测压力成立"再议。
5. **e2e ls 等值升级**（nit-1 顺带）。
6. **format 计数口径**（268 席跑=docs 落盘前 vs 270 调度器=落盘后）——台账记一笔即可，无动作（r-2 先例同档）。
