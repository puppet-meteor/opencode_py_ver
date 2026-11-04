# R3-S2 结构化审查（席位B · T-B-028）

> 审查对象：worktree r3-s2 **`7f238bf`**（T-C1-026 交付，9 files +2775/−49）vs 基线 main **`0020f68`**（#106，r3-s2 分叉点；main 现值 `8528958` 含 S3 并回——按任务书规避幻影差量，diff 基准恒取分叉点）。diff 取法 `git -C <worktree> diff 0020f68 7f238bf`（只读）。
> 审查人：席位B（T-B-028），止损 40min 内完成。写权限：仅本报告（新增单文件）；git 零写。
> 门禁基线：分叉点态 703/68/双绿；worktree 现值 **736 passed（703+33=契约 21+集成 12，零跳过）/ mypy 71 files（68+3 新文件）/ 双 ruff 绿**——调度器亲跑复验（任务⑥免重复项），本审查未复跑。
> 调度器已验项（本报告直接引用、未重复执行）：四门禁复跑数字（736/71/format 0-225）、9 文件 diff 全文亲读（含 cancel/close 修订落地证实）、两断言零触碰 git status 亲证、三裁定（tools/__init__ 追认+流程 nit 记档 / events.py L333 留原样 / 止损 85-91min 压线如实记档）、读显示伪影 grep 复核（零命中）。
> B 独立复核面：policy.py 651L 全文亲读（本会话）+ invoker.py diff 全文（两半，含 R2_PERMISSION_DECISION 拆除、record_unknown 双路径、gate 挂点全部 hunk）+ service.py/app.py/registry.py/tools__init__.py diff 全文 + 33 测名全清单（Select-String）+ test_approval.py 八测试体三段亲读（L400-462/L555-675/L670-827）+ policy.py 定向锚（L52/L69/L73/L74/L302）+ r3-slices 回落面零命中检索 + 冻结面零差量 git 亲证（`diff 0020f68 7f238bf -- src/opencode/contracts/ state.py projector.py txn.py` 输出为空）+ 进度文件全文 + 契约锚（§2.1 L47/L48/L53、§3.1 L135-139、§3.2.1 L203、§3.3 L212、§5.1 L255/L257/L259/L261、§5.2 L265-270、§7 L173/L316/L317/L322/L337/L339、§8 L348）全部本会话累计亲证。

## 结论

**PASS**（blocker 0 / major 0 / **minor 1** / **nit 3**）。

R3-S2 与 spec（r3-slices §2.2）和契约逐项对齐：求值序六档精确落地（宿主硬 deny→双档根限→saved→规则→默认表→回落）；saved 语义逐句兑现（精确绑定/当前 boot/过期重求值序/unknown-retry 双路径）；审批协议以 coordinator 单 CAS 点执法 §7 L173 首合法胜、迟到三分分类留 service；生产者链分工（Invoker 五型+service revoked）与 §5.1 L259 读法相容（详 §4-1）；cancel/close 修订序（信号→锁内意图→锁外 close_run；close Phase 1.5）独立核验成立；R2 面零行为漂移（占位常量→字面量内联值等价，两断言存活机制成立）；S1 裁定④-4 三项回补（真实 Policy 链/成对纪律/boot_id 消费）全部兑现。唯一 minor 为 close-timeout 硬取消掉队者可孤儿化 PENDING 权限（§2.1 L53 角落违反）+ S1 fold closed 门与 §3.3 L212 restart 语义待调和——崩溃恢复面前瞻项，非本切片代码缺陷，处方=恢复切片任务书条款化。三 nit：默认表回落面无契约明文且 bash/MCP 无表键（开放点记档）、reason 常量双份无 drift pin、revoke check-then-act assert 窗口（当前 wiring 不可达）。

## 1. 审查维度逐条（12）

**维 1 · 引擎求值序 vs §5.2 L266 原文** ✓（policy.py 全文亲读）：宿主硬 deny→根目录限制（词法+realpath 双档，测试 `test_root_restriction_denies_fs_escape` L133/`test_root_restriction_denies_unresolvable_resource` L149 双档钉定）→saved（当前 boot/本 session/未过期未撤销/unknown-retry 屏蔽）→显式规则（source 优先级+序，`test_rule_priority_and_resource_pinning` L206）→默认表→声明 side_effects 回落，与 L266 原文序精确一致；"项目规则不能放宽宿主限制"有专项测试（`test_rules_cannot_relax_root_restriction` L196；`test_host_deny_outranks_rules_and_saved_decisions` L179）。默认表键亲证：`fs__read: allow`（policy.py L69）、`fs__write/fs__edit: ask`（L73/L74），read/glob/grep/ls allow 行为面由 `test_default_table_reads_allow_writes_ask`（L108）钉定；**bash__exec/mcp__server_call 在 policy.py 全文 0 命中（ctx_search 亲证）——L266 明列的 bash/MCP ask 无表键，其 ask 语义实际由 side_effects 回落承载**（真实 bash/MCP 工具 side_effects=True→ask 行为等价；但注册 side_effects=False 的 bash 型工具将 L266 字面 ask vs 实现 allow 分歧）→回落读法无契约依据（L266 亲证无未列动作条款）亦无计划层锚（r3-slices `side_effect|未知动作|回落` 0 命中亲证）→**标开放点**，见 nit-1。

**维 2 · saved 语义 vs L268/L270 逐句** ✓：once=单 permission_id+input_hash 精确绑定（`test_saved_decision_exact_binding_allows` L238；unknown-retry 屏蔽 L283）；always=当前进程本 session+具体 action/resource 规则（`_start_approved` 以实际 action/resources 构造 rule，无通配——测试体亲证 `rule == {"action": "tst__write", "resources": ["out.txt"]}` L648）+durable 先于内存激活（`register_saved` 在 `sink((replied, saved, started))` 之后，diff 亲读）；expires_at 用前检查+过期经提交锁追加 `py.permission.exexpired` 后重求值（invoker `_apply_policy`：`due_expired → sink(expired 批) → mark_expired → evaluate` 序 diff 亲读——与调度器亲读证实一致，B 事件面复核双证；`test_saved_decision_expiry_lifecycle` L251）；撤销/过期不改已执行结果+started 前复核（revoke docstring "never changes already executed results"+gate 在 started 前）；unknown 相同输入重试必须重新 ask——**record_unknown 双路径亲证**：异常路径（invoke_tool 新 L1095-1104 hunk）+优雅失败路径（新 L1118-1131 hunk），均仅 `effect == "unknown"`（side_effects 工具）时记录（`test_unknown_effect_retry_is_re_asked_despite_saved_decision` L739：byte-同参→同 input_hash→重试被重问，注释亲读）。

**维 3 · boot_id 消费（裁定④-3）** ✓：app.py 铸造 `boot_id=f"boot_{uuid4().hex}"`（diff 亲读，新 L112）+ApprovalCoordinator/PermissionPolicy 同点构造注入 registry 与 service；只激活当前 boot 内宿主批准且未过期/未撤销（policy 当前 boot 检查+外来 boot 注册 fail-closed，`test_foreign_boot_and_duplicate_registration_fail_closed` L293）；恢复后 saved 保留审计不自动激活（boot 重启测试 L702：saved 计数恒 1、审计行休眠）；新 always 产生新决定（mint_decision_id 每次铸造）。**测试=第二 App 同目录模拟重启的构造性等价论证成立**：对所测面（saved 激活集）而言第二 App 与真进程重启在构造上同构（内存注册表两种情形皆空 by construction、boot_id 皆新、durable 审计行皆留存——in-test ④披露注释 L688-690 亲读）；未测差量=boot 恢复/孤儿清理（今日无实现，无可测对象）→披露充分、读法诚实。

**维 4 · invoker 挂点+R2 面保持** ✓：门位置=校验成功↔started 之间（`invoke_tool` 内 gate 替换原 started 提交块，diff hunk @@ +1044,49 亲读；`_apply_policy` docstring "between validation and py.tool.started" 与 §5.1 L259 挂点序一致）；输入不可变（L261——gate 只读 raw_input 用于 extract_resources/evaluate，零改动，docstring 钉定）；deny→§2.1 L47 denied/not_started 终态、**零 py.permission.\* 事件、无伪主机回复**（`_apply_policy` deny 分支直落 `_finish`，diff 亲读）；**R2_PERMISSION_DECISION 占位拆除**（旧 L79-81 常量删除 hunk 亲读）+started 携带生效决策：ask-allow 路径 `_start_approved` 的 started 草案 `permission_decision="allow"`（"生效决策非 ask"读法落地，docstring "carries the host reply's allow"）；**policy=None 保持 R2 默认 allow 面行为不变**——占位常量（`Literal["allow"] = "allow"`）→字面量 `"allow"` 内联为**值等价**（两分支注释 "both the policy-allow and default-allow paths record allow"），此即两断言零触碰存活的机制根据：断言比对的 wire 值恒同；佐证：diff --stat 零既有测试文件触碰 + 调度器 736 绿。

**维 5 · 审批协议+CAS（L268+L173）** ✓：默认 300s（`DEFAULT_APPROVAL_TIMEOUT_S = 300.0` policy.py L52 亲证）；requested 单独先提交（durable 先于内存等待，diff 亲读）；实际 reject 才写 replied（invoker reject 分支 `PermissionRepliedData(reply="reject", source="host")` 同批 pre）；未连接→closed(cancelled)+ApprovalUnavailable 决不静默 allow（`_request_approval` approvals is None 分支 diff 亲读+`test_unconnected_approval_channel_cancels_without_silent_allow` L485）；迟到回复不覆盖 closed（coordinator CAS 单点+`test_approval_expiry_cancels_the_tool_and_late_reply_expires` L526）；resolve_reply 单 CAS 点返回 replied/late/unknown、错误分类留 service 面（`ReplyResolution` 跨 await 不抛，进度①披露兑现）；allow 唤醒后 cooperative cancel 复检折叠（`if context.cancel.is_set()` → cancelled 对+closed(cancelled) pre，"reply 留内存、permission 仍 fold-pending"注释亲读——started 在 cancelling run 上 fold 非法故复检承重）；**closed_outcome Literal 含 "restart" 成员但赋值仅 expired/cancelled**（invoker 条件表达式二值赋值 diff 亲读）——**B 判断：前瞻非死词表**（§3.3 L212 将 restart 保留给恢复协调面；当前无生产者是设计使然，非词表腐化）→并入 m-1 前瞻条目。

**维 6 · py.permission.\* 生产者链（点名复核①，详 §4-1）** ✓：requested/replied/closed/saved/expired 生产者=Invoker（五型全部在 invoker.py diff 内亲证：`_request_approval`/`_request_approval` reject 分支/`_start_approved`/`_apply_policy` due-expired 批）；revoked 生产者=service revoke 面（`_commit_revoked`，service diff 亲读）；run-scoped vs session-scoped metadata 分档亲证（requested/replied/closed 走 `_draft` 默认 `{"run_id": ...}`；saved/expired 显式 `metadata={}`；revoked `metadata={}`——与 S1 `RUN_SCOPED_EVENT_TYPES` 三型分档逐一对上）；S1 手工注入测试面保留未回头改（S1 两测试文件不在 9 文件 diff 内，stat 亲证）；**S1 裁定④-4 三项回补全部兑现**：真实 Policy 链（12 集成测试全走真 App+registry+policy）、跨实体终态成对纪律（batch_id 等值断言三形态：[replied,started] L441-442、[closed,finished] L583-584/L618-619、[replied,saved,started] trio L656-657）、boot_id 消费（维 3）。

**维 7 · cancel/close 修订序（点名复核②，详 §4-2）** ✓：修订落地独立核验——cancel()：`cancel_event.set()` **锁前**→锁内重读+`_commit_cancel_intent`→**锁外** `close_run(run_id, ...)`（service diff 新 L809-835 三个 hunk 亲读，含"解决审批先行会唤醒提交脉冲对抗意图 seq 重试"的机制注释）；close()：全部意图+信号→**Phase 1.5** `close_all(REASON_APPROVAL_SHUTDOWN)`（新 L1066-1081，全部意图后）→Phase 2 等待；与 §7 L173 文本序（先设取消信号）及 L337/L339（cancel 关闭待审批/未开始调用；close 为 pending 提交 closed）相容；ConflictError 风暴机制论证闭合（早稿 close 先于意图→被唤醒的 [closed,finished] 提交与意图提交的 expected_seq 互踩→有界重试风暴——修订序下意图先落、parked Invoker 静默期解决，窗口消除）；修订序下 ④-2 同批+closed-first 不变式保持由竞态测试钉死（§4-2）。

**维 8 · service 独立通道（L316/L317/L322）** ✓：reply_permission 词表门（once/always/reject §3.1 L136，`reply not in (...)` → StoreValidationError，`test_reply_permission_validation_and_missing_channel` L772）；与运行锁分离（L316 死锁面——**活性阳控亲证**：审批 pending 时 `run_status` 与 reply 均响应，once 测试 L421-425 注释+断言）；诊断态 CAS 前拒（L203——`pending_request → _read_record → == READ_ONLY_DIAGNOSTIC → SessionReadOnlyError`，且**非破坏性**：拒绝后恢复 ACTIVE 同一 reply 仍落（测试 L816-826 亲读：refusal gated the input, not the CAS））；错误分类三分（unknown→NotFoundError/late+expired→ApprovalExpiredError/late+other→ConflictError，`ApprovalExpiredError` code=预注册 APPROVAL_EXPIRED、Conflict 复用 storage 导出——§8 L348 零新增）；DecisionReceipt=ack 非令牌（回执先于 durable 回复、钉 execution_id、docstring "carries no reusable execution permission"，§7 L322 形状 `{permission_id, decision: allow|deny, execution_id}`）；revoke 面=current-boot only（内存注册表=激活集，前 boot 审计行 NotFound，①披露）+**内存先失效 fail-closed**（deactivate→commit 序，失败提交留下已失效规则）+有界 ConflictError 重试（`_COMMIT_ATTEMPTS` 循环）+无去重键论证（状态门+fold terminal-once 围栏，`_commit_revoked` docstring）；**is→== 诊断门自修亲证**：reply_permission 与 revoke_permission 两门均为 `== SessionStatus.READ_ONLY_DIAGNOSTIC`（diff 亲读）——`_read_record` 返回 raw str（repos.py L140 既有事实），StrEnum 成员与裸串 `==` 值等价成立、`is` 恒 False（门死）→修复承重且语义正确；**理由常量本地复制取舍意见**：维持本地复制（环真实：service→`opencode.tools` 包 `__init__`→registry→invoker→service 四边全部今日 diff/基线亲证；移 contracts 则 wire 契约层承载运行期措辞词汇——可辩非优）→但缺 drift pin，见 nit-2。

**维 9 · OP-9 激活（L337/L339）** ✓：close() Phase 1.5 approvals.close_all（全部意图后、信号已设，diff 亲读+docstring 更新删除 R2 "protocol-unreachable" 段）；挂起审批钉住 run 非终态→配对永远 fold 合法（cancel() 注释亲读）；close 幂等（`await app.close()  # idempotent` L622 亲证）；watchdog 任务清理（coordinator close_all 解析全部 pending+拒绝新注册，`test_close_all_cancels_every_pending_and_refuses_new_registrations` L394+policy.py 全文亲读）；quarantined session 跳过不致命（A-010：意图循环 try/except pass 保留，diff 上下文亲证）。**附 m-1 角落**：close-timeout 硬取消掉队者（woken invoker 于 [closed, finished] 批提交前被杀）留下孤儿 PENDING 权限——详发现清单。

**维 10 · 测试覆盖+③漏报抽查** ✓：21 契约（test_policy.py L108-407）+12 集成（test_approval.py L409-796）测名逐一存在（Select-String 全清单亲证）且与进度②映射逐测对应；真实链标准兑现（真 App+alembic 子进程 `_upgrade_head`+SQLite+runner+真实 asyncio 调度器；**batch_id SQL 等值证同批=④-2 断言形态**亲证三种）；**竞态测试经真实调度器非注入 idiom**（cancel/close-during-approval 两测试体 L561-622 全读：零 monkeypatch、零注入 future，真实取消/关闭时序+事件表事后断言）；n-3 注入携注（backlog #22）：out-of-band sqlite UPDATE 隔离面唯一性论证成立（in-test 注释 L807-810 "the only way a live run's session flips read_only_diagnostic mid-approval"——正常面无中途翻转路径；携注含恢复对照段 L820-826）；预注册窗口 NotFound 处理诚实（事件表轮询 `_await_permission_requested` 先于 reply 确保落库亲证；所读三段零人工 sleep/零注册后置注入；`_reply_registered` 辅助体未独立亲读，调度器 9 文件全文亲读覆盖）；boot 重启模拟披露充分（维 3）。**③漏报抽查结果见 §2**。

**维 11 · ④三必答+裁定 concur（点名复核③）** ✓：见 §3 表（六项全 concur）。

**维 12 · 写权限面对账** ✓：9 文件=授权清单 8 项精确（policy.py 新/invoker/registry/service/app/两测试新/进度文件）+清单外 1 处（tools/__init__.py——已裁追认：纯机械再导出 10 名+`__all__`+docstring，无新依赖边（registry 本就 import policy，diff 亲证））；清单内未做 2 项处置：contracts/permissions.py 条件项未建=①论证成立（PermissionDecision/DecisionReceipt 为运行期/服务面形状非 wire 契约，六型 wire 已 S1 版本化——concur）；txn.py 可选项跳过=nit-2 必答（concur，详 §3）；既有测试文件零触碰（stat：仅两新测试文件——含两断言所在文件）；contracts/events.py/state.py/projector.py/txn.py/pyproject/uv.lock/CI/CORE-CONTRACTS/r3-slices/source-baseline/ORCHESTRATION 零触碰（stat 9 文件清单穷尽+冻结面零差量 git 亲证）。

## 2. ③漏报抽查结果（R1-S8 教训方法：③清单 vs 实际 diff 面交叉）

- **文件级全对账**：diff --stat 9 文件=7 src+2 新测试+进度文件，与授权清单 8 项+已裁追认 1 处精确一致，**无未披露文件、无越权路径**。
- **内容级**：policy.py 651L 全文+invoker/service/app/registry/init diff 全文+八测试体三段亲读，改动面与进度①-⑧逐项对应，**无未披露行为面**；③四档浮报中五项关键披露全部落地核实：is→== 双门 ✓、cancel/close 修订序 ✓、n-3 out-of-band sqlite（含唯一性论证+恢复对照）✓、预注册窗口诚实处理 ✓、boot 重启第二 App（含 ④披露注释）✓。
- **冻结面**：`git diff 0020f68 7f238bf -- src/opencode/contracts/ src/opencode/session/state.py src/opencode/session/projector.py src/opencode/storage/txn.py` **输出为空**（events.py 零触碰=L333 裁定的独立佐证；S1 fold/投影面零改动只读复用；nit-2 跳过的零触碰证明）。
- **结论：③清单完整覆盖改动面，无漏报**。

## 3. ④三必答+裁定 concur 表（B 独立意见，给据）

| # | 项 | B 意见 | 依据 |
|---|---|---|---|
| 必答1 | OP-R3-5 触及仅落档位可配置面+默认保守 ask+档位裁决留 S4 | **concur** | rules 为数据注入（PermissionRule+`test_rule_priority_and_resource_pinning` L206）、默认表为模块数据（L69-74）、超时经 App 参数（approval_timeout_s，diff 亲证）；最终裁决词 deny/ask 留代码侧保守；可配置面归 S4 ConfigLoader 切片——引擎切片不预开配置面符合切片纪律 |
| 必答2 | 两断言零触碰存活 | **concur** | 机制亲证：占位常量 `Literal["allow"]="allow"`→字面量内联**值等价**（wire 值恒同）；stat 零既有测试文件触碰（B 独立）；736 绿（调度器）；断言存活是设计等价非侥幸 |
| 必答3 | nit-2 跳过（txn.py 零触碰） | **concur** | 冻结面零差量 git 亲证（§2）；S1 处方原文条件式"触达 txn 时顺手"——S2 无需触达（生产者链不需 txn 改动），跳过=避免对冻结邻接面的无谓扰动；留作台账项待真实触达切片 |
| 裁定a | tools/__init__.py 追认（机械再导出）+流程 nit 记档 | **concur** | 机械性亲证（10 名再导出+`__all__`+docstring，无行为面、无新依赖边）；流程意见：**记档足够**——零风险机械改动+③完整披露+即时裁定，更强流程动作只会增加摩擦；前瞻处方：后续新增 tools 子模块的任务书预列包 `__init__` 涟漪行（调度器裁量） |
| 裁定b | events.py L333 过时注释留原样 | **concur** | events.py 零差量 git 亲证；注释级陈旧（"R2 has no policy layer"）且改之即越 9 文件写权限面——留原样正确；后续触达 events.py 的切片顺手更新（与本席 S1/S3 顺手项模式一致） |
| 裁定c | 止损压线 85-91min 如实记档 | **concur** | 如实记档+哨兵 10min 粒度+席位自然完成未被击杀（任务书实录）——止损为纪律工具，诚实记录保其有效性；处置与 R2-S8-closeout m-1 同构（记档零代码动作） |

## 4. 点名复核三项（给据）

**4-1 · py.permission.\* 生产者链分工 vs §5.1 L259（裁定④-1）——读法成立（不标开放点）**：五型（requested/replied/closed/saved/expired）生产者=Invoker、revoked=service，全部 diff 亲证（§1 维 6）。L259"只有 Invoker 的事件回调可提交"的语境为**工具调用链**（挂点序原文辖调用管线事件——同批配对/closed-first/fold 合法性协作均系于 parked Invoker 的单一回调提交点）；revoke 为宿主动作面（§7 L317 将其指派给 service 面），被撤销的决定其执行早已终结、**无链条可挂**——不存在可承载其提交的 Invoker 回调，service 不提交则 L317 无从 durable 化。metadata 分档佐证分工：revoked `metadata={}`（session 域，脱离 run 链）。结论：分工与 L259 相容且是唯一自洽落位。

**4-2 · cancel/close 修订序 vs §7 L173/L337/L339——独立核验成立**：(a) 修订序亲证（§1 维 7：cancel 信号锁前→锁内意图→锁外 close_run；close 全部意图→Phase 1.5 close_all→Phase 2）与 L173 文本序（先设取消信号）相容——意图提交介于信号与审批解决之间是 fold 所需的 durable 化，不违文本序；(b) 竞态论证闭合：早稿风暴机制（close 先行→woken [closed,finished] 提交与意图 expected_seq 互踩）真实——服务侧 `_commit_revoked` 的有界 ConflictError 重试即同款机制的证据；修订序消除窗口（parked Invoker 静默期解决）；(c) 修订序下 ④-2 不变式钉死：cancel-during（[closed,finished] 同批 L583-584、零 started L589、零 replied L590——复检折叠路径的证据）与 close-during（同批 L618-619、reason="service close while awaiting approval" L612、run interrupted L621、close 幂等 L622）两测试体亲读，全走真实调度器。

**4-3 · ④三必答+四裁定 concur（=点名复核③）**：六项全 concur（§3 表），无一 non-concur；流程 nit 处置意见（记档足够+任务书前瞻行）与 OP-R3-5/nit-2 权衡意见均已给据。

## 5. 发现清单

**m-1（minor）· close-timeout 硬取消掉队者可孤儿化 PENDING 权限 + restart 恢复面待调和**：Phase 2 超时硬取消（close docstring 既有语义"on timeout hard-cancel the stragglers"）若击中**已唤醒但 [closed, finished] 批未落地**的审批 Invoker（CancelledError 不被捕获——invoke_tool docstring 亲证；批为单原子提交），则该 py.permission.requested 落库后永无闭合：run 经 CancelledError handler 折 interrupted（R2-S7 既定崩溃诚实面——run 终态可与未终结工具共存），而 **S1 fold 的 closed 门要求 run 存活**（终态 run 上的 closed 迁移 fold 非法）→孤儿 PENDING 永久留存读模型，抵触 §2.1 L53"不留下永久 pending"的角落。同面关联：closed_outcome "restart" 词表成员零生产者（§1 维 5——§3.3 L212 保留给恢复协调面），即崩溃/重启孤儿清理机制整体为后续切片范围。**影响评估**：触发条件苛刻（close_timeout 30s 内提交脉冲未完成+恰好被硬取消）、无安全性/正确性损伤（工具未启动、审批不可再回复、读模型诊断可见）；S2 自控的全部正常路径（cancel/close/到期/拒绝/allow 竞态）同批配对完备（§4-2）。**处方**：非阻塞——台账记行+恢复切片任务书明列两条款：(i) 孤儿 pending 清理路径（closed(restart) 落地时 fold closed 门须重审 state.py——允许终态 run 上的 restart 迁移，或恢复期预排空审批提交脉冲）；(ii) §2.1 L53 与硬取消角落的接受声明（若选择不修）。本切片代码零改动需求。

**nit-1 · 默认表回落面无契约/计划锚 + bash/MCP 无表键（开放点）**：未列动作回落 declared side_effects 在 CORE-CONTRACTS L266 无明文（本会话亲证 L265-270 无未列动作条款）、r3-slices 零提及（0 命中亲证）——纯实现外推；L266 明列的 bash/MCP ask 无表键（policy.py 0 命中亲证），ask 语义由该无锚回落承载。方向保守（side_effects=True→ask、False→allow）、现实工具面零分歧（唯一现存 side-effect 工具面走 write/edit 表键或 True→ask），且为 R2 allow 面测试工具所必需。**处方**：开放点记档——CORE-CONTRACTS 后续文档轮注记回落语义，或 S6（bash/MCP）落地时补表键/修订 L266；任务书已预设该裁定请求（"是否有契约依据或应标开放点"），B 答复：应标开放点。

**nit-2 · reason 常量双份无 drift pin**：`REASON_APPROVAL_RUN_CANCELLED/SHUTDOWN/REVOKED` 于 service 本地复制 policy 典范值（双方注释交叉自证"same strings"），环 guard 论证成立（§1 维 8 四边链亲证）；但 33 测名无相等性钉定（清单亲证）。分歧后果=wire 自由文本措辞漂移（fold 无关、语义无损）+两类路径（invoker 直引 policy 值 vs coordinator 转存 service 值）措辞不一致。**处方**：S4/S5 触达 service 或 policy 时顺手一行相等性 pin 测试，或提升共享叶子常量；非必须。

**nit-3 · revoke check-then-act assert 窗口（当前不可达）**：revoke_permission 的状态门（`state != "saved"`→ConflictError）与 `deactivate` 之间，唯一并发变更者为 invoker due-expired flush 的 `mark_expired`（调用点 diff 亲读）；极窄竞态下 `assert deactivated` 以内部错误而非 ConflictError 呈现（服务 diff 注释自证该失败条件）。**当前 wiring 不可达**：S2 唯一 saved 生产者写 `expires_at=None`（`_start_approved` diff 亲证）→due_expired 恒空（到期机器仅对构造注入的带过期决定激活，测试 L251 形态）。**处方**：引入带过期 saved 决定的切片收回此注记（assert→重读状态映射 ConflictError，一处改动）；记档即可。

## 6. 建议（调度器裁量，无阻塞）

- 按现状合并 r3-s2（四门禁绿+12 维全过+③无漏报+冻结面零差量+两断言存活机制成立）。
- m-1 恢复面两条款入台账/恢复切片任务书（孤儿清理路径+fold closed 门调和）；与 S1 nit-1（upcaster 前瞻）、S3 m-1（rg 超时收编→S5）同列 R3 前瞻台账。
- nit-1 开放点择载体（契约注记 vs S6 补键）；nit-2/nit-3 留待触达切片顺手项。
- 后续任务书模板可加一行：新增 tools 子模块时预列包 `__init__` 再导出涟漪（裁定a 前瞻处方）。

— 席位B（T-B-028），2026-11-04。基线 `0020f68` → 审查对象 `7f238bf`；本报告引用的全部 file:line/测试名均经本会话亲证（调度器已验项除外，已标注；契约锚为本会话累计亲证）。
