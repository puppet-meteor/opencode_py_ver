# R1-S4 结构化审查（席位B · T-B-009）

- 审查对象：worktree `r1-s4` 检查点#35（commit `077caf6`，7 文件 +994）：`session/state.py`（125 行）/`session/projector.py`（182 行）/`session/__init__.py`/`storage/txn.py`（仅 N-1+N-3 两处）/`tests/integration/test_projector.py`（11 测）/`tests/integration/test_store_commit.py`（docstring bullet+两条 N-2 直测，14 测）/进度 `doc/orchestration/progress/T-C1-005.md`。
- 基线：`doc/CORE-CONTRACTS.md` §3.2（L146-187）/§3.2.1（L189-198）/§3.3（L200-206）/§2.1/§6.1 + TS `session/projector.ts`/`bus.ts` + `doc/plans/r1-slices.md` §2 R1-S4（L93-107，T-B-004 对齐版）。
- 复验方式：B 全文亲读 state.py/projector.py/两导出面/txn.py/test_projector.py 全文件/test_store_commit.py 全文件/进度文件；txn.py "仅两 hunk" 范围采信调度器 diff 亲核，B 以全文重读+T-B-008 对 S3 行为的逐点记载复核内容（两 hunk 均核实：N-1=txn L303-308、N-3=epoch_ms docstring L107-112，其余与 S3 已审行为一致）；四门禁（119 passed=106 无回归+13 新 / ruff / format 80 files / mypy 29 files）采信调度器亲跑。

## 1. 结论：**有条件通过**

S4 本体交付（纯折叠/真实投影器/导出面/13 新测/N-1/N-3）逐点核验全部通过：S4 新增代码 **0 blocker / 0 major / 0 minor / 1 nit**；发现全部位于**继承自 S3 的 txn.py 区段**——1 major（④ 幂等重放比较循环泄漏裸 `json.JSONDecodeError`）+1 nit（诊断定位硬编码）+1 nit（N-1 直测缺口，属 S4 范围）。**条件**：并回前授权 C-1 微任务修复 ④（一行级 wrap + 按任务书字面配方补 N-2① 直测 + 顺带 N-1 直测与诊断定位修正，预计 ≤30 min）。不建议并入 S5（通知面切片不触 storage 内核，混入模糊切片边界）。

## 2. §3.2 投影语义与 applied_seq 分工对照（维度1）——精确成立

- **投影器推进 A**：created → INSERT sessions 行含 `applied_seq=event.seq`（projector L125）；prompt → INSERT messages 行后 `_advance_applied_seq`（L174，独立方法=故障注入缝）。逐事件推进，即契约 L158"同一 projector 更新…和 sessions.applied_seq"。
- **store 复核 A=H+n**：txn L381-392 提交前复核 `applied_seq==high+n`，违反 → `StorageCorrupt(isolate=False, reason=applied_seq_not_advanced_after_projection)` 只回滚——`NoAdvanceProjector` 直测实证（test_projector #6，零残留）。T-B-008 确认的"投影器推进+store 复核"分工（L158/L163）原样落地。
- **同事务**：`_insert_and_project` 先 INSERT events 行、再逐事件 `projector.apply`（txn L546-555），全部在 commit 前——事件/投影/A 同一写事务（TS bus.ts L402-405 commit 内 projector、L626-631 逐事件投影的 Python 对应）。
- **协议缝**：SessionProjector 结构化实现 S3 `Projector` Protocol（签名逐参一致），SessionStore 显式注入，无第二入口。

## 3. 折叠/写库分离（§3.3，维度2）——通过

- state.py 纯函数：无时钟/随机/IO 导入（AST #10 钉死 + B 亲读：仅 dataclasses/enum/typing/contracts）；同输入同输出（#8 以 S1 契约 fixtures 直测，含 §6.1 字段与 working_dir）；非法过渡六路全拒（#9：无会话 prompt/二次 create/closed/归属/非 R1 类型 `py.run.started`/数据模型不匹配）——fail closed 不跳过，§3.3 L202 吻合。
- projector.py 零内联折叠规则：全部过渡经 `state.apply`（`_fold` 包装；#11 AST 断言 import + B 全文亲读证实仅"行物化+SQL"，无状态迁移内联）。
- **S8 rebuild 结构成立**：rebuild 按 §3.3"按 seq 重放"驱动同一 projector+fold（projector 无状态可共享、docstring 明示 S8 复用）；折叠态↔行映射完备——sessions 行字段（status/config_snapshot/snapshot_hash/applied_seq）全在 SessionState，messages 行 seq 取自事件流（重放逐事件可得），SessionState.messages 供 fold 消费方，无缺口。

## 4. TS 基线对照（维度3）——忠实

- Created handler L541-568：insert+onConflictDoNothing → `SessionAlreadyProjected`（TS fatal/进程死亡）↔ Python INSERT → IntegrityError → `StorageCorrupt(duplicate_session_projection, isolate=False)`——die→调用方可见错误的既定错误族适配，docstring+注释明确（#3：第二 created 新事件 id + 批内双 create 同缺陷，会话保持可写）。
- insertMessage L481-504：`seq: event.durable.seq` 同源 ↔ messages.seq=事件 seq（#1 断言 seq=2）；messages 无独立 type/created 列 → data=完整 UserMessage canonical JSON（#1 逐列断言）。
- run()/SessionMessageUpdater（L261-476）/projectIdle（L506-535）R1 不做（assistant/idle=R2），代码零痕迹；`bus.project` 注册（L904-912）不做——TransactionWriter 显式注入（S3 既定单一写入路径差异）✓。

## 5. ④ S3 缺口专节——**Major-1（并回前修复）**

- **位置**：txn.py `_idempotent_replay` 比较循环 L485-486 `_json_column(row.data/metadata)`——`_json_column`（L122-126）对 str/bytes 裸 `json.loads` 且无捕获；模块自身的解码失败包装（`_decode_stored` → L492-503 catch → `REASON_STORED_DECODE_FAILED`+隔离）位于比较循环**之后**，非法 JSON 触达不到。
- **触发**：已提交行 data/metadata 被外部篡改为非法 JSON 后整批重试（提交路径恒写 canonical 合法 JSON，仅带外损坏可触发）。
- **行为**：裸 `json.JSONDecodeError`（ValueError）逃出 SessionStore.commit——非 StoreError（破坏 §8 错误族/CoreError 映射接缝）、无 §3.2.1 隔离、零诊断、会话保持 active，该批每次重试同样崩溃。
- **契约归属**：§3.2.1 L191 检测范围明文含"无效 JSON"；比较循环读取存量行=提交路径上读取已存日志，其解码失败属模块已承诺的 `stored_event_decode_failed` 检测类。**B 判定：§3.2.1 检测面应覆盖幂等重放路径**——此为 S3 一处未包的实现遗漏，非契约空白，无需升 A。
- **严重度：major**（非 blocker：幸福路径与数据完整性不受影响，泄漏先于重试中任何写入；非 minor：唯一提交路径上违背已承诺的隔离类+错误族接缝，且 R1 当前 commit 重试是存量事件数据的唯一读取者，S6 读取面落地前无兜底）。
- **修复建议（一行级）**：比较循环内存列解码捕获 `json.JSONDecodeError`（或改经包装 helper）→ `_corruption(REASON_STORED_DECODE_FAILED, Diagnostic(first_bad=该行 seq/id, applied_seq=发现时 A))`；`IdempotencyConflictError` 是 StoreError 不受影响，先解码后比较仍可区分内容分歧。
- **归属/授权建议**：**merge 前微任务给 C-1**（txn.py 第三处改动，随附 literal N-2① 直测 + ⑤ N-1 直测 + Nit-2 定位修正）。C-1 在两处授权边界内不改而如实上报，处置正确。

## 6. 八项未决问题 B 意见（维度4；均无需升 A，④/⑤ 需调度器授权微任务）

1. **① working_dir 不落 sessions 行**：**同意 C-1 处置**——任务书 INSERT 列清单系笔误，契约 §3.2 L178 与锁定迁移 0001（T-B-006 已验列集精确断言）均无该列；working_dir 留在事件 data（SessionCreatedData wire 字段）+折叠态（SessionState.working_dir，#8 断言）已完备。未来列化走 §3.3 新 revision，现无越权改动 ✓。
2. **② prompt 无 sessions 行 → StorageCorrupt(isolate=False)、不 INSERT 最小诊断行**：**同意，无需升 A**——经提交路径达此必为 H=0+无行+批首非 created（首次创建豁免）=调用方坏输入，§3.2.1 L191"仅提交输入不合法时拒绝并回滚，不把调用方的坏输入当作已存损坏"字面适用；隔离会为从未存在的会话伪造诊断行（L193 最小记录语境是"已有日志"的会话）。与 H>0+无行 READ_MODEL_MISSING（隔离+最小行，txn L354-356）形成正确分野；#5 实证零残留+同 id 可再 create。
3. **③ 重复投影/重复 message_id isolate=False**：**B 确认同意**（关闭"待 A/B 确认"）——前置态健康（写闸门+不变量已过），缺陷在当前批内容，回滚后仍健康 → L191"不隔离健康 session"；TS fatal↔调用方可见 StorageCorrupt 为既定错误族适配（§4），延续 T-C1-004 未决 4 口径一致。
4. **④ 缺口**：见 §5——major，建议 merge 前微任务。
5. **⑤ N-1 异常链无直测**：**同意暂缓，建议随 ④ 微任务顺带补**——触达需在 `_isolate_locked` 内注入持久化失败（monkeypatch engine.connect），断言原 StorageCorrupt 照常上抛+`__cause__`=IsolationPersistFailedError+内存闸门生效（后续 commit→SessionReadOnly）。S4 测试授权边界内不补是正确的克制。
6. **⑥ active_run_id 不推进**：**同意**——R1 事件子集无 run 生命周期类型，行保持 NULL + #1 显式断言钉死；run 槽位占用属 S7/R2。
7. **⑦ 投影器失败一律 isolate=False**：**B 确认同意**（同 ③ 原理）——隔离面=store 对存量数据检测（`_corruption` 默认 isolate=True：A≠H/gap/dup/illegal seq/unknown status/read-model-missing/存量解码失败）+S8 rebuild；投影期失败=健康前置态上的当前批失败→只回滚，L191 字面。
8. **⑧ Windows proactor 封禁面**：**接受，证明强度足够**——类本体替换破坏 proactor 自管道 `isinstance` → await 全死锁（进度②有实测：全套件停在 68 点）是真实平台约束；方法级封禁（create_connection/getaddrinfo/socket.connect/Popen/run/asyncio.create_subprocess_*）覆盖一切 TCP/HTTP 客户端与进程族，"查询不触发外部调用"回归性成立。残余（socket 构造本身、os.system 族）已如实记录；如需硬保证另立非 Windows/selector 通道。非缺陷。

## 7. 验收 A 与 N-1/N-2/N-3 落地质量（维度5/6）——通过

- **验收 A**（r1-slices L106）：#2 以 FailingProjector（真实 projector 子类，evt_a2 抛 RuntimeError）→ events/sessions/messages 全零残留；换健康 SessionProjector 同批重试 seq 重起 1,2——S3 故障注入复测以真实投影器完成 ✓；#3/#4 重复投影=StorageCorrupt+零残留+A 保留+会话可写 ✓；外部调用封禁 #7（⑧ 评估见上）。
- **N-1**：txn L303-308 `raise corruption from persist_error`——原 StorageCorrupt 照常上抛、持久化失败经 `__cause__` 显式保留、`_isolated` 先 add（L577）内存闸门生效——与契约 L192"保持内存写门禁并报告存储错误、不声称隔离已持久化"一致 ✓（直测缺口见 ⑤）。
- **N-2①（等价路径有效性）**：**有效**——seq=0 走"比较循环不比对 seq"（L479-487 实证）→ `_decode_stored` 包装路径（L492-503）→ 同 reason 常量+同一隔离入口+闸门先于幂等（测试 L505-506 断言），完整证明隔离机制；docstring 如实记录与字面配方的偏离及原因。字面配方（data=非法 JSON）在 ④ 修复前不可用（泄漏裸 JSONDecodeError，无法按字面断言）——偏离处置正确，修复后补测。
- **N-2②**：伪造 status='bogus' → `unknown_session_status` 隔离 + A=1 保留 + 诊断载荷全等断言（含 applied_seq:1=闸门读取值，正确）+ 已提交批重试被拒 + 诊断写零业务事件——覆盖充分 ✓。
- **N-3**：epoch_ms docstring L107-112 明确"毫秒截断=存储精度…'重试复用内容'（§3.2）之意"——充分 ✓。

## 8. 测试质量与越界（维度7/8）——通过

13 新测 vs r1-slices §2 R1-S4 验收点全覆盖（行内容逐列/canonical JSON/§8 code+reason 硬断言/零残留计数/seq 重起/fixture 确定性/AST×3 含 store+txn 零 Bus 延续）。越界：无 assistant/idle 折叠（fold 对非 R1 类型 raise）✓；无 Bus ✓；无全局注册 ✓；无 S6+ 读 API ✓；session 导出面恰 5 符号 ✓；TransactionWriter 仍不导出（storage/__init__ 无此名）✓；working_dir 决策未越权改契约/迁移 ✓；txn.py 除两 hunk 外与 S3 已审行为一致 ✓。

## 9. 修订项汇总

| 级别 | 条目 | 归属 |
|---|---|---|
| major | M-1（④）`_idempotent_replay` 比较循环裸 `json.loads` 泄漏 JSONDecodeError：不隔离、零诊断、破坏 StoreError 族（§3.2.1 L191"无效 JSON"应辖） | **并回前 C-1 微任务**（+literal N-2① 直测 + ⑤ 直测 + N-2 定位修正） |
| nit | N-1（⑤）N-1 异常链无直测 | 随 ④ 微任务 |
| nit | N-2（B 新发现）重放解码失败诊断硬编码 `first_bad=committed[0]`+`applied_seq=0`（txn L499-501）——坏行非首行时定位错误（N-2① 自构场景即坏 evt_n2a2 而诊断将指向 evt_n2a1）、A 实为 2 载荷报 0，与 L192"可定位时的首个坏 seq/event_id、发现时的 applied_seq"本意不符 | 随 ④ 微任务（逐行 catch 顺带修正） |
| 观察 | ⑧ 封禁面残余（socket 构造/os.system 族）已记录 | 如需硬保证另立通道，非本切片 |

条件满足后（④ 微任务完成）：R1-S4 可合入；R1-S5（volatile Bus）可立即派发。
