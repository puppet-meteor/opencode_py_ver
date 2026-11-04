# R3-S1 结构化审查（席位B · T-B-027）

> 审查对象：worktree r3-s1 **`1071bc0`**（T-C1-025 交付，26 files +2302/−18）vs 基线 main `c7cc32d`（#102，r3-s1 分叉点）。diff 取法 `git -C <worktree> diff c7cc32d 1071bc0`（只读）。
> 审查人：席位B（T-B-027），止损 40min 内完成。写权限：仅本报告（新增单文件）；git 零写。
> 门禁基线：R2 收官 651/66/双绿；worktree 现值 **703 passed（651+52）/ mypy 68 files / 双 ruff 绿**——调度器亲跑复验（任务⑥免重复项），本审查未复跑。
> 调度器已验项（本报告直接引用、未重复执行）：四门禁复跑数字、5 文件 diff 全文亲读、§3.1 L135-139 六型字段逐行核对、T-C1-014 先例台账 L235 查证、写权限面核对（git status 无越权路径）。
> B 独立复核面：四 src diff 全文亲读（events.py +139 / state.py +326 / projector.py +206 / txn.py +86）+ test_events.py 探针段（5 行）、两新测试文件测名全清单与两关键测试体（词表反例 L223-257 / n-1 单源化 L591-637）、18 fixture 文件面、`0001_initial.py` permissions 建表 L116-129 亲证、CORE-CONTRACTS 新锚（L53/L135-146/L208-216/L258-280/L354-362）、TS `permission.ts`/`session-event.ts` 对照、r3-slices §2.1/§4/§5/§6、T-C1-025 任务书与进度文件全文。

## 结论

**PASS**（blocker 0 / major 0 / **minor 1** / **nit 2**）。

R3-S1 交付与 spec（r3-slices §2.1）和契约逐项对齐：六型注册逐字段精确匹配 §3.1 L135-139；封闭 wire 纪律（§8 L358）以 extra="forbid"+独立 R3 元组+未知版本拒绝落地；fold 门全集齐备且读法保守；投影四字段注册时钉死、迁移只 UPDATE state；注册表单源化彻底拆除 import 期冻结单例且 n-1 钉死测试真实有效；rebuild 重投影论证坚实（CASCADE 亲证）。唯一 minor 为**进度文件四处引注/计数精度偏差**（代码零改动需求，不阻塞合并）；两 nit 为前瞻注记与防御分支命名。OP-R3-8 未触发的判定经实际建表列亲证成立。

## 1. 审查维度逐条（12）

**维 1 · 六型注册 vs §3.1 L135-139 逐字段** ✓

| 型 | 契约字段 | 实现（events.py，diff +139） | 判 |
|---|---|---|---|
| requested（L135） | permission_id, execution_id, action, resources, input_hash, expires_at | `PermissionRequestedData`（~L413）：六字段齐；`input_hash` 带 `_SHA256_HEX` pattern；`expires_at: UtcDateTime`（tz 强制） | ✓ |
| replied（L136） | permission_id, reply: once/always/reject, source: host | `Literal["once","always","reject"]` + `Literal["host"]`（source 钉死 host） | ✓ |
| closed（L137） | permission_id, outcome: expired/cancelled/restart, reason | `Literal["expired","cancelled","restart"]`；reason `min_length=1` | ✓ |
| saved（L138） | decision_id, scope, rule, expires_at, boot_id | JsonObject×2 + `UtcDateTime | None` + boot_id；**boot_id 不入 `DecisionInfo`（仅事件侧）**，state.py docstring 明示"activation is producer policy (§5.2 L270)" | ✓ |
| revoked/expired（L139） | decision_id, reason | 两型同形，reason `min_length=1` | ✓ |

词表封闭性/tz/SHA256_HEX/scope·rule JSON 对象均有专项测试钉死：`test_requested_naive_expires_at_rejected`（L217）、`test_replied_vocabulary_closed`（L223-228，负探针 "always-forever"/"deny"/"ONCE"/""）、`test_closed_vocabulary_closed`（L236-241，负探针 "timeout"/"denied"/""）、`test_saved_scope_and_rule_must_be_json_objects`（L249-257，标量/数组/数字拒绝）。18 fixtures=6 型×{valid, missing_field, bad_extension} 与 diff --stat 逐文件对账一致。六型全 durable，与 L142 ephemeral 清单无交集。

**维 2 · 封闭 wire 纪律（§8 L358）** ✓：六 data model 全 `extra="forbid"`（不走同版本新增可选字段）；`R3_PERMISSION_EVENT_DEFINITIONS` 独立元组（~L745），R1/R2 元组零行改动（仅注释更新，`test_r1_and_r2_tuples_unchanged` L96 冻结 pin 佐证+651 基线零回归）；`default_event_registry()` 全联三元组；未知版本显式拒绝有测（`test_unknown_version_rejected_closed_wire` L118，v2 → 错误）。**upcaster 形态**：契约 L358 的显式 upcaster 是**版本升级条件性义务**（"新增字段一律升级 version，并提供显式、确定性的 upcaster"）——六型首版 v1 无旧版本存量，无可 upcast 对象；"旧读者遇新版明确失败"半句由版本拒绝钉死。`upcast` 在 events.py 全文 0 匹配（R1/R2 亦然），属平台既有形态而非本切片偏离——见 nit-1 前瞻注记。

**维 3 · fold 门全集** ✓（state.py diff +326 亲读）：requested 门八项全落地（~L852-906）：活跃 session（ACTIVE）/已知 execution/工具 PENDING/`input_hash` 与折叠工具匹配（同 py.tool.requested 所有权规则，docstring 引 §3.1 L129/L131）/全新 permission_id/每 execution 至多一审批（线性扫描）/run RUNNING（cancelling 不准新审批——§2.1 L61"不再启动 step/tool"的保守外延）/envelope run 匹配（`_require_run`，run_id 取被钉 execution 的 run）。replied/closed 门：PENDING+首个合法迁移（§7 L173）+run 存活（`is_terminal or ACCEPTED` 拒绝——RUNNING/CANCELLING 合法，与 L268"审批与 cancel/expiry 通过条件更新竞争"的竞态窗口相容）。决定三型：session 域无 run 关联（`RUN_SCOPED_EVENT_TYPES` 不扩展、decode 期不要求 run——`test_permission_types_are_not_run_scoped_at_decode` L108）/全新 decision_id/终态一次。`PermissionState`/`DecisionState` 枚举+`is_terminal` 属性（~L175-201）。

**维 4 · 终态一次+幂等重放（§7 L173）** ✓：字节全同终态重放仅推进 `applied_seq`（replied：REPLIED+同 reply；closed：CLOSED+同 outcome+同 reason；决定终态：同 state+同 reason——`_terminate_decision` 共享路径）；异 reply/outcome/reason 一律 IllegalTransitionError。**同批 tool.finished(denied) 配对的"生产者纪律归 S2"读法与 §2.1 L53 相容**：L53"权限关闭和对应未开始工具终态同批提交，不留下永久 pending"是对生产者的提交义务；fold 逐事件执法无法看见批边界，且下游已有兜底——§3.1 L146"step.finished 必须在所有计划工具终结后提交"（违反则 StorageCorrupt）使"永久 pending"在 step 终结处必然暴露。state.py docstring 的论述（"No cross-entity invariant pairs a permission terminal with the tool terminal: §2.1 L53's same-batch pairing is producer discipline (S2), not a fold gate"）成立。集成测试 1/2 以同批配对+迟到/漂移反例钉死（L181/L308）。

**维 5 · 投影纪律（§3.1 L183+§3.2 L160）** ✓（projector.py diff +206 亲读）：`_write_permission_diff`（~L712-804）——INSERT 钉死四字段面（approval 行：input_hash/action/resources/expires_at+state；decision 行：scope/expires_at+state），**迁移只 UPDATE state**；pre-SELECT 查重（accepted-run 模式，`REASON_DUPLICATE_PERMISSION` fail-closed）；UPDATE rowcount=0 → `read_model_row_missing` fail-closed；**permissions diff 为权限事件唯一读模型写**（`_project_permission` 不触 messages/tools/runs/active-run 槽，docstring 明示）。权限事件全部经 fold（pre-state→fold→diff→applied_seq→缓存更新），与 R2 面同构形（同款增量缓存）。

**维 6 · 注册表单源化（R2-S2-review n-1）** ✓：`_REPLAY_REGISTRY = default_event_registry()` import 期冻结单例**彻底拆除**（diff 删除该行，替换为类属性 `_registry: EventRegistry | None = None`）；`bind_registry()`（~L329）分歧重绑 RuntimeError（"rebinding is a wiring defect"）；`_replay_registry()`（~L347）惰性兜底 default（standalone rebuild/直接构造场景）；重放调用点 `_REPLAY_REGISTRY.decode_durable` → `self._replay_registry().decode_durable`（~L592）；txn 侧 `TransactionWriter.__init__` 经 `getattr(projector, "bind_registry", None)` 鸭子绑定（txn.py ~L361-369，注释明示环规避论证：session.projector imports storage 包、isinstance 会成环；录制型 fake 无该方法零影响）。**n-1 场景测试钉死度：真实有效**（`test_registry_single_sourcing_replay_decodes_through_the_bound_registry`，L591-637 亲读）——应用侧注册 `py.test.heartbeat` durable 型经 store.commit 落库（events 表 2 行亲证）；第二开库换 plain `SessionProjector`+同 registry 提交新事件触发 pre-state 重放，断言链精确区分两种失败：**不是** `stored_event_decode_failed`（旧行为=漂移面），**而是** `illegal_projection_transition`（heartbeat 经绑定注册表 decode 成功、被 fold 封闭拒绝）+ 事件零落库（COUNT=2）。这正是 n-1 原文"txn 插入侧通过、projector 重放侧报 stored_event_decode_failed"漂移的正面钉死。

**维 7 · rebuild 重投影（裁定②复核）** ✓：**CASCADE 论证成立性亲证**——`0001_initial.py` L116-129 permissions 建表：列 session_id/permission_id/decision_id/execution_id/action/resources/input_hash/scope/expires_at/state（permission_id/decision_id/execution_id/action/resources/input_hash/scope/expires_at 全 nullable，state NOT NULL），**L128 `ForeignKeyConstraint(["session_id"], ["sessions.session_id"], ondelete="CASCADE")`**——rebuild 删 sessions 行若不显式重插，permissions 行静默丢失，违反"连续 rebuild 全等"验收（集成测试 4 含两次连续 rebuild 全等断言，L466）。重插列与 commit 路径**逐列一致**：`_INSERT_REBUILT_PERMISSION`（txn.py ~L176-180）vs `_INSERT_PERMISSION`（projector.py ~L189-193）同 8 列同参数形态（resources=`canonical_json(list(...))`、expires_at=`epoch_ms(...)`、state=`.value`）；`_INSERT_REBUILT_DECISION` vs `_INSERT_DECISION` 同 5 列、expires_at None 处理一致。`applied_seq=H` 不变量检查原样保留（rebuild 尾部 A=H 校验未动）；IntegrityError → `_corruption(REASON_REBUILD_DUPLICATE_PERMISSION)` fail-closed（注释自证：无唯一索引故该守卫防御性仅 NOT NULL/FK 面，重复结构性不可能——fold fresh-id 门）。commit 序（§3.2 L160）零改动（diff 仅 rebuild 语句面+注释）。**裁定② concur 坚实**。

**维 8 · L96 硬约束** ✓：saved 决定经 `py.permission.saved` 事件 → projector `_INSERT_DECISION` 写 permissions 投影（同一事件路径）；diff --stat 26 文件中**零**独立 JSON 权限文件/零权限 CRUD 模块/零 drizzle 同构面（18 个 JSON 全部为 tests/contract/fixtures 测试 fixture，非运行时权限存储）；差异表 4 遵守。

**维 9 · 诊断只读（§3.2.1 L195-204）** ✓：fold 六型 handler 均要求 `state.status is SessionStatus.ACTIVE`（诊断态拒绝）；专项测试 `test_diagnostic_session_rejects_permission_writes`（L661）。

**维 10 · 测试覆盖 vs ②映射 + ③漏报抽查** ✓：契约 41 = TestR3Registration 7 + PositiveFixtures 11（6 参数化+5）+ NegativeFixtures 12（6+6 参数化）+ DataShapes 11，测名清单逐一存在（Select-String 全清单亲证）；集成 11 测名逐一存在（L181/308/393/466/591/640/661/702/737/766/810）且与进度②映射条目一一对应。冷暖/双 rebuild 全等（L466）、store 级零半行落库（L810，计数断言）均在。**③漏报抽查结果见 §2**。

**维 11 · ④四项浮报逐项意见+四裁定 concur** ✓：见 §3 表。

**维 12 · TS 收窄边界** ✓：events.py docstring 声明"TS keeps its permission events ephemeral (`packages/schema/src/permission.ts` Asked/Replied), a Python-contract difference, not a port"——**属实**：permission.ts L45-46 `Asked = ephemeral(...)`/`Replied = ephemeral(...)`（L5 import ephemeral）；session-event.ts L39-57 的 `Created.permissions: Permission.Ruleset` 为会话配置字段与事件族无关。saved.ts 字段面（Info/AddInput/UpdateInput/ConflictError/isUniqueViolation）作决定字段参照、冲突检测以 pre-SELECT 查重实现（无唯一索引可用）——progress ③注记如实。**零"与 TS 同构"断言**：新增两测试文件 0 匹配；src 全树唯一 `isomorphic` 命中为既有 service.py:42 的反同构声明（"behavioral, not isomorphic"），非本切片引入。

## 2. ③漏报抽查结果（R1-S8 教训方法：③清单 vs 实际 diff 面交叉）

- **文件级全对账**：diff --stat 26 文件 = 4 src（授权清单 1-4）+ 18 契约 fixtures（授权清单 6）+ 2 新测试文件（授权清单 6/7）+ test_events.py（唯一既有文件触碰，③注记已披露待裁）+ 进度文件（授权清单 8）。授权清单 5（0003）未创建——OP-R3-8 未触发，与进度①论证一致（且经 0001 实际列集亲证成立：四字段全映射，input_hash/scope/expires_at/state 列俱在，L116-129）。**无清单外路径、无未披露文件**。
- **内容级**：四 src diff 全文亲读，改动面与进度①描述逐项对应（事件型全集/单源化选型/RUN_SCOPED 不扩展/fold 规则/投影细节/rebuild 扩展），**无未披露行为面改动**；R1/R2 元组仅注释行差异；txn commit 序零改动属实（diff 中 commit 相关仅 duck-binding 构造器新增与 rebuild 语句）。
- **结论：③清单完整覆盖改动面，无漏报**（唯一精度偏差为引注/计数类，归 m-1，不构成漏报）。

## 3. ④四项裁定 concur 表（B 独立意见，给据）

| # | 调度器裁定 | B 意见 | 依据 |
|---|---|---|---|
| ① | 冻结探针翻转（test_events.py L816-821，`py.permission.requested`→`py.permission.granted`）追认 | **concur** | 断言语义不变（`resolve_durable` 未知型仍 UnknownEventTypeError，diff 亲读：仅探针字符串+注释 5 行）；旧探针值因本切片注册而失效（requested 现为真实注册型——若不翻转该测试转为永久红）；`py.permission.granted` 为表外臆造名，保持"永久未知型"语义；T-C1-014 同构先例模式成立（探针值因注册失效→翻转至另一表外名）。**引用精度偏差见 m-1(a)**：代码注释（"same T-C1-014 probe-flip precedent"）在模式层面准确；进度③的"saved 注册时"措辞与文件归属不准——判定为**精度 nit 而非实质偏差**（先例的语义内容真实适用）。 |
| ② | txn rebuild 权限行重投影范围扩展追认 | **concur** | 验收标准所迫："连续 rebuild 全等"（切片验收句）要求权限行纳入全等断言；CASCADE 亲证（0001 L128）表明不重插则静默丢行；重插列与 commit 路径逐列一致（维 7）；commit 序零改动（§3.2 L160 面未触）。保守性成立：`_DELETE_SESSION_PERMISSIONS` 显式删除沿既有模式（messages/tool_executions/runs 同款显式删除，非新增设计面）。 |
| ③ | upcaster 旧版本读者路径不可达声明接受 | **concur** | 契约 L358 使显式 upcaster 成为版本升级条件性义务；六型首版 v1 无 v0 存量，升级分支无真实数据可驱动；封闭 wire 行为已由 v2 未知拒绝反例钉定（`test_unknown_version_rejected_closed_wire`）。**附注（nit-1）**：upcaster 机制在全平台（含 R1/R2 既有面）结构性缺位（events.py `upcast` 0 匹配），首个 v2 出现前须先建机制——建议入台账前瞻。 |
| ④ | S1 无协议侧生产者记档转 R3-S2 硬输入 | **concur** | py.permission.* 六型 S1 均无真实生产者（Policy/审批协议归 S2），手工注入覆盖 fold/投影面已如实分档（③协议不可达段）；S2 任务书应明列三项回补：真实 Policy 链、跨实体终态成对纪律（§2.1 L53 同批配对+§3.3 L212 closed-first 序）、boot_id 消费面（§5.2 L270 只激活当前 boot）。 |

## 4. 发现清单

**m-1（minor）· 进度文件四处引注/计数精度偏差**（代码零改动需求；进度文件为 worktree 历史资产随提交入库，不阻塞合并）：
- (a) ③注记引"T-C1-014 先例（**saved 注册时**同类探针翻转）"——台账 L235 实录为 test_projector not-foldable 探针**改型为** py.permission.requested（fold 侧探针），非 test_events 注册探针在"saved 注册时"翻转；test_events 探针生于 T-C1-013 `67bf9f3`。先例**模式**成立（裁定① concur 不受影响），归属细节不准。
- (b) ①"decision 行 **`rule` 列**载 scope JSON"——实为 `scope` 列（`_INSERT_DECISION` 五列：session_id/decision_id/**scope**/expires_at/state；0001 L125 为 scope 列，表无 rule 列）；且同节下一句"boot_id/`rule` 原文细粒度留在事件侧不进投影列"自相矛盾——后句为准。
- (c) ②两处词表括注错列：`test_replied_vocabulary_closed（approve/reject 封闭词表）`——实词表 once/always/reject；`test_closed_vocabulary_closed（granted/denied/cancelled/expired）`——实词表 expired/cancelled/restart（负探针 timeout/denied/空串）。测试本体正确（L223-241 亲读），仅进度括注错。
- (d) ② ruff format "216 files already formatted" vs 调度器复跑口径 217 files（计数非判据，退出码 0 双方一致）。
- **处方**：调度器合并时代码/测试零改动；于台账或合并注记中记一笔更正（后续消费方以代码与契约为准，不以进度括注为准——R2-S8-closeout m-1 同类处置）；R3-S2 任务书可加一行"进度文件引注须逐名核对"提醒。

**nit-1 · upcaster 机制前瞻注记**：契约 L358 的显式确定性 upcaster 在全平台（含 R1/R2 既有面）无实现钩子（`upcast` 0 匹配），当前以版本拒绝封闭（合规——义务条件于版本升级）。任何 py.* 事件族首次升 v2 前须先建：旧版本模型保留+确定性转换+旧 fixture 可读（§8 L356 最低门槛）。**处方**：调度器入 R3 增项台账或 R3-S8 exit-evidence 检查项（"首个 v2 前置条件"），非本切片工作。

**nit-2 · rebuild 防御分支 reason 常量复用**：txn.py decision 侧 IntegrityError 亦报 `rebuild_duplicate_permission`（无 decision 专用 reason）；注释已自证该守卫防御性仅 NOT NULL/FK 面、重复结构性不可能（fold fresh-id 门），分支实际不可达。**处方**：可选清理；S2 触达 txn 时顺手，不单开任务。

## 5. 点名复核两项（给据）

1. **n-1 单源化钉死度**（维 6）：`test_registry_single_sourcing_replay_decodes_through_the_bound_registry`（集成 L591-637）以 `py.test.heartbeat` 应用侧注册型走完整 store.commit 落库（events 表类型清单亲证 L620-623），再于新开库以绑定注册表重放——断言 `reason == "illegal_projection_transition"` 且非 `stored_event_decode_failed`（L632-634 注释+断言双明示）+ 事件零落库（L637）。**真能证**"decode 侧经绑定注册表通过、失败面移至 fold 封闭"，即 n-1 漂移（插入侧通过/重放侧 decode 失败）正面钉死。
2. **rebuild CASCADE 论证成立性**（维 7）：0001 L128 `ondelete="CASCADE"` 亲证 + 重插列与 commit 路径逐列比对一致（8 列/5 列双路）+ `applied_seq=H` 不变量保留 + 集成测试 4 双 rebuild 全等断言（L466）。**论证成立**，裁定② concur。

## 6. 建议（调度器裁量，无阻塞）

- 按现状合并 r3-s1（四门禁绿+12 维全过+③无漏报）；m-1 更正记档即可。
- R3-S2 任务书硬输入建议明列（承裁定④）：真实 Policy 链生产者、跨实体终态成对纪律（§2.1 L53 同批+§3.3 L212 closed-first）、boot_id 消费面（§5.2 L270）；可顺带 nit-2 清理与"引注精度"提醒。
- nit-1 upcaster 前瞻条目入台账（R3-S8 检查项或增项行）。

— 席位B（T-B-027），2026-11-04。基线 `c7cc32d` → 审查对象 `1071bc0`；本报告引用的全部 file:line/测试名均经本会话亲证（调度器已验项除外，已标注）。
