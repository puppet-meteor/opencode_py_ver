# R2-S2 结构化代码审查(席位B · T-B-018)

- 审查对象:worktree `r2-s2`(分支 r2-s2,单提交 `a5da097`,27 文件 +2621/-106,父=fdb979f;`git diff main...r2-s2` 即本切片)
- 直接输入:进度 `T-C1-014.md`(96L)、CORE-CONTRACTS(冻结)、r2-slices §0.2/§2、R2-S5-review 先例口径
- 门禁:采信调度器冻结面复跑(pytest 434 passed=410+24 零回归 / ruff 全过 / format 137 / mypy 45),无需重跑

## 1. 结论

**通过**(0 blocker / 0 major / 2 minor / 2 nit;需补测项均为已披露防御的覆盖缺口,可后置跟进,不阻塞合并)。

## 2. 发现分级

**Blocker:0 / Major:0 / Minor:2 / Nit:2**

- **m-1(minor)· projector.py L18-19,O(n²) 重放成本已披露但为真实热点**:`_project_r2` 对每条 R2 事件全量重放 `seq < event.seq` 前缀(`_replay_state` L294-321),单事件 O(n)、批内 m 条 O(n·m)、整会话累计 O(n²)。模块 docstring 已自曝("correctness first; incremental projection cache is a later optimization")且 §3.3 无性能条款,故非违规;但 runs/messages 查询面(S7)落地前建议开票跟进增量投影缓存,长会话(数千事件)下每条提交的解码+折叠开销不可忽视。
- **m-2(minor)· projector.py L459-466,`_write_accepted_run` IntegrityError 文案混淆两种撞键**:`except IntegrityError` 同时兜住 unique(session_id, request_id)(§2.1 L37 身份冲突,注释 L460-461 所述)与 **GLOBAL run_id 主键跨 session 撞车**(④-2 涟漪的正源),而报错文案只描述前者("request X is already accepted under another run for session Y")——跨 session 复用 run_id 时该文案事实错误(request 可能全新,撞的是 run_id)。行为面正确(两者均拒批回滚、isolate=False 一致),仅诊断语义失真;后置小修即可(按撞键约束区分文案,或 SELECT 预检按 run_id 全局查)。
- **n-1(nit)· projector.py L113,`_REPLAY_REGISTRY = default_event_registry()` 于 import 期冻结**:R2 内 13 型全为契约内建、无碍;但 R3 若以**应用侧注册**的 durable 型落库,txn 插入侧用 `self._registry` 解码通过、projector 重放侧用 import 期快照 → 合法事件反报 `stored_event_decode_failed`。建议 R3 扩容时改为注入/惰性取注册表(与 store 同源),避免双注册表漂移。
- **n-2(nit)· projector.py L126 `_SELECT_RUN` 列集含 session_id**:查询恒以 `:sid` 传入,返回列 session_id 冗余(仅 request_id/status 被消费)。纯整洁度,无行为影响。

另:state.py(884L)全文逐 handler 核对 §2.1 L37-65 两表,**零新发现**——dispatch 13 型 fail-closed、`_RUN_FINISH_OUTCOMES` 与 L59-61 逐字一致、step/tool 状态机与 L39-52 一致、`_require_finished_consistency` 落 §3.1 L144。

## 3. 契约符合性(12 维度)

| # | 维度 | 判定 | 行号级依据 |
|---|---|---|---|
| 1 | fold 两表逐行落位 | ✅ | state.py `_apply_*` 13 handler vs §2.1 L37-65:`_RUN_FINISH_OUTCOMES` 与 L59-61 精确一致;step.started 串行门+唯一性(state.py L448-460);tool 全链与 L44-52 一致;`_tool_for_event` 落 L125(cancel 阻断 requested/started) |
| 2 | §2.1 L37 身份约束 + T-B-016 §6-5 S6 联动 | ✅ | fold 级:known-run 同 request_id 幂等再收、异 request_id 拒(`_apply_prompt`);读模型级:`_write_accepted_run` SELECT+unique(session_id,request_id) 双门(③-23)。T-B-016 ④-3 的 S6 runner 点名格式对齐义务与 fold 身份键正交——fold 工具身份靠 execution_id/tool_call_id/input_hash(③-22),name 格式不入折叠,无耦合冲突 |
| 3 | generation batch 原子性 | ✅ | txn.py `_insert_and_project` L676-684:单条 `execute(_INSERT_EVENT, rows)` 整批插入→逐事件投影;半批失败整事务回滚(test ② 23 探针含半批原子回滚锚点) |
| 4 | projector 四面同事务 | ✅ | projector.py `_project_r2` L285-292:同一 connection 写 messages/tool_executions/runs + active_run_id + applied_seq;批内可见性成立(整批先插,L676→L683);幂等重放路径(⑥)、R1 面(L222-275)同构 |
| 5 | runs OP-2 语义 | ✅ | 最小面:status/error 两列,转换仅 UPDATE 此二列(L415-423);查询面按方案归 S7(④-1);与 T-A-005 L377"不声称 TS 表结构同构"一致 |
| 6 | 0002 迁移 | ✅ | 采信调度器 schema 核验(runs 列序/unique(session_id,request_id)/FK/主键);`_INSERT_RUN`/`_UPDATE_RUN` 列集与迁移逐列对齐;legacy 保留测+回填测见 test_migrations 2 测(progress ①) |
| 7 | RUN_SCOPED 翻转授权边界 | ✅ | 台账 #71 专项 1-5 逐条落位(progress ③ 头部);冻结测试文件仅包络/数据级补净(dispatcher 已核),断言语义零弱化;`run_ses_b` 两站点为 GLOBAL PK 强制涟漪(④-2) |
| 8 | legacy 兼容 §0.2 | ✅ | R1 多 accepted:fold 同 request 幂等再收 + `_write_accepted_run` SELECT 返回既有 accepted 行(L436-447);`test_r1_legacy_log_replays_and_rebuilds_identically` 三面全等钉死 |
| 9 | rebuild 扩展 | ✅ | txn.py `_rebuild_transaction` L687-689 单事务清+重放+重投影;复用同一 projector/同一 fold(docstring L11);`test_r2_log_rebuilds_every_face_byte_identically` 四面逐字节全等+二次幂等;R1 无终态不补写=§3.2 L187 字面(④-6) |
| 10 | txn extend-only | ✅ | dispatcher numstat 核验纯追加;`_insert_and_project`/`_isolate_locked`(L988-1012)既有语义零改动 |
| 11 | ③ 披露完备性 + 11 项裁决 | ✅(见 §4) | state.py+projector.py 全文复读,未发现未披露分支;协议不可达 7 项分类全部成立(isinstance 守卫/else 支/run-INSERT 支等均验证) |
| 12 | ④ 6 项裁决 | ✅(见 §5) | 含两项 GLOBAL 分歧(run_id 主键/idle id),均已有裁据 |

**TS 锚点抽查**:projector.py docstring 映射四锚(Created L541-568 / insertMessage L481-504 / run L261-476 / projectIdle L506-535)与既有核验记录一致,无漂移;TS 侧无 runs 表,T-A-005 已豁免表结构同构。

## 4. ③ 披露完备性 + 11 项裁决

**完备性**:state.py(884L)、projector.py(484L)全文逐分支复读,txn.py 关键区(insert-then-project/rebuild/isolate)定向核验——**未发现未披露的可防御分支**;协议不可达 7 项(③-1..7)分类逐项成立(①③-1 isinstance:注册表 decode 按 type→data_model 配对,`_REPLAY_REGISTRY.decode_durable`/`_decode_stored` 双路均经注册表;②③-2 else 支:L193-194 验证;③③-3 run-INSERT 支:L405-414 验证;④③-4 两处 INSERT IntegrityError:L339-344/L379-384 验证;⑤③-5 rebuild 重复:行集来自 fold dict 键唯一;⑥⑦③-6/7:R2 无策略层/attempt 恒 1,沿 T-C1-013 先例)。

**未测防御 11 项裁决(③-8..18)**:

| 项 | 裁决 | 依据 |
|---|---|---|
| ③-8 finalized 幂等窗口止于 generated | **同意**(窗口读法自洽) | §3.1 L144:parts 以 generation batch 为身份载体;step.generated 后 assistant 消息已落地,晚到 finalized 无消费者,拒收是读法结果而非额外分支。终态后永久幂等需保留消息后 part 集可变性,与"自包含终态消息"矛盾。关联 ④-4 |
| ③-9 增量重放 stored_event_decode_failed 未测 | **需补测** | 同款防御 rebuild 路径已测、增量路径未测——SQL 篡改已存事件后 commit 一例即可闭合;两入口一测一缺不对称 |
| ③-10 `_write_accepted_run` 再接受冲突支未测 | **需补测** | 行存在但异 request/已过 accepted 的正例探针缺失(unique 撞车支已测);一测即闭合 |
| ③-11 fold 级 known-run/异 request 未直测 | **需补测** | 仅全量 replay 可达、经 replay_illegal_transition 呈现;R1 legacy 测只覆盖合法同 request 再收。低优先 |
| ③-12 三处 UPDATE rowcount==0 | **同意(暂不设测)** | 需同事务内读模型行消失,SQLite 单写者+同 connection 下不可达,SQL 级篡改注入成本>价值;等价防御已由 fold 单一来源前置拒 |
| ③-13 tool.requested 身份漂移未测 | **需补测** | foreign-run/input_hash 已测,message_id/tool_call_id 漂移探针一行级,建议补 |
| ③-14 tool.started input_hash 未测 | **需补测** | requested 路径同款已测、started 路径缺;与 ③-13 同批补 |
| ③-15 pending→终态直达正例未覆盖 | **需补测** | 合法映射表 L721-726 只有拒绝探针无正例;合法路径无正例=状态机半面无证 |
| ③-16 finalized/generated 的 run-status 门未单独测 | **同意(暂缓)** | 同门同表,step_started/run.finished 两入口已锚定;重复探针价值低 |
| ③-17 "generated message must be running" 未测 | **同意(暂缓)** | 同 ③-16,同门变体 |
| ③-18 step.started 重复 step_id/message_id 未测 | **需补测** | tool_call_id 唯一性在 23 探针内有、step 侧同性质守卫缺,对称缺口 |

小计:**同意 4(③-8/12/16/17)·需补测 7(③-9/10/11/13/14/15/18)·升A 0**。需补测 7 项全部为已披露 fail-closed 分支的覆盖缺口,无行为缺陷,可合并后跟进(建议随 m-2 同批小改)。

## 5. ④ 6 项裁决

| 项 | 裁决 | 依据 |
|---|---|---|
| ④-1 OP-2 查询面归 S7 | **同意** | 切片方案 §2 R2-S2 明确只钉表级投影+迁移;查询面提前需追加 repos 授权 |
| ④-2 runs.run_id GLOBAL 主键 | **同意维持 GLOBAL**(分歧一,详见 §6) | T-A-005 L377:Python 读模型"按自身接口补充,不声称 TS 表结构同构"——无 TS 同构义务;§7 `run_status(run_id)` 按 run 查询天然单行;run_id 生成域(ULID)本即全局。测试 `run_ses_b` 数据级涟漪为最小正确适配。**附 m-2**:跨 session 撞 PK 时错误文案失真,后置小修。若终审改判 session 复合主键需迁移+测试联动,维持则零动作 |
| ④-3 idle id 分歧 | **契约裁死:msg_idle_{run_id} 正读,维持**(分歧二,详见 §6) | **CORE-CONTRACTS §3.3 L208 原文:"idle ID 由 run_id 确定"**——Python 实现即契约正读;TS 用事件 id 为 TS 侧实现差异,Python 无须跟进。且 msg_idle_{run_id} 对事件 id 变化的幂等重放更稳 |
| ④-4 finalized 幂等窗口 | **维持现状** | 同 ③-8;OP-6(attempt>1)若引入重试重开窗口再议,届时属行为变更需授权 |
| ④-5 包含性一致性读法 | **同意** | 有序包含/子序列为容纳 S1 锁定 fixture(合法 saga finish 丢 reasoning part/file content)的保守读法;伪造内容仍被拒(包含方向单向)。边界注记:允许 drop-to-empty,与"终态消息自包含可读"目标不冲突(saga 层职责)。严格相等会拒合法历史,不可取 |
| ④-6 rebuild 不补写推断终态 | **同意** | §3.2 L187 字面:R1 多 accepted 保留 accepted 行(⑤+迁移回填测已钉);补写推断终态属行为变更需再授权,且会破坏"rebuild=纯重放"语义 |

小计:同意 4(④-1/5/6 + ④-3 契约正读)·维持分歧待终审 2(④-2/④-3 中 ④-2)·需改 0·升A 0。

## 6. 裁决清单与合并意见

**两项 GLOBAL 分歧意见**:
1. **runs.run_id 主键(④-2)**:建议**维持 GLOBAL 单列主键**。依据:T-A-005 已豁免 TS 表结构同构;§7 run_status(run_id) 查询面直取单行;ULID 生成域全局;强制跨 session 唯一是更强不变量。代价=测试数据级涟漪(已最小适配)+ m-2 文案小修(唯一反对理由是改复合主键可消涟漪,但削弱不变量且需迁移联动,不取)。
2. **idle id(④-3)**:**非分歧**——契约 §3.3 L208 已明文"idle ID 由 run_id 确定",`msg_idle_{run_id}` 为正读,TS 事件 id 是对面偏差;维持,建议调度器将 ④-3 关闭为"契约已裁"。

**是否有需升 A 项:无**(blocker 0 / major 0)。

**合并意见**:通过。r2-s2 可并回 main;2 minor(m-1 O(n²) 重放热点跟进 S7 前优化,m-2 撞键文案小修)与 2 nit(n-1 R3 注册表注入前瞻,n-2 SELECT 列集)可随需补测 7 项(③-9/10/11/13/14/15/18)合并后一批小改,均不阻塞。

**审查面声明**:state.py 884L 全文、projector.py 484L 全文、txn.py 关键区(L645-690/L985-1031)、进度 ③④ 全单、契约 §2.1/§3.1/§3.2/§3.3(L208)/§7(L322)/T-A-005(L377)已核;0002 schema/events 翻转/冻结测试包络/txn numstat 采信调度器机器核验;TS 锚点抽查四锚无漂移。
