# R1-S7 结构化审查（席位B · T-B-012）

- 审查对象：worktree `r1-s7` 分支 r1-s7 单提交（`5c7be04` S7 主体 11 文件 +1648/-11；父=#53 `848031f`）：`session/service.py`（新增 564 行）/`app.py`（新增 90 行）/`session/state.py`+`session/projector.py`（任务④ active_run_id 接线）/`storage/repos.py`（仅追加）/双 `__init__.py`（仅追加导出）/`tests/integration/test_service.py`（新增 694 行 28 测）/既有断言跟进 3 处/进度 `T-C1-010.md`（12 项未决）。
- 基线：`doc/CORE-CONTRACTS.md` §7 L298-335（方法表 L298-312/prompt 语义 L316/四步 L318-323/占槽 L325）+ §3.1 L114 + §3.2 L146-173（commit 五步 L155-159/H·A L163-165/锁纪律 L167/L171）+ §3.2.1 L189-196 + §6.1/6.2 L283-290 + §8 + `doc/plans/r1-slices.md` §2 R1-S7 L134-149 + TS 锚点六处 B 亲核属实（`schema/src/event.ts` L9-14、`schema/src/identifier.ts` L8-31、`bus.ts` publish L511-533、KeyedMutex `withLock` L579、`session/projector.ts` Layer L537+、`database/sqlite.bun.ts` L100-103）。
- 复验方式：B 全文亲读 service.py/app.py/state.py/test_service.py（28 测逐个清点：18 测函数+参数化 6+6 展开；FixedClock/SeqIds 双注入钉子）+ git diff 全量亲验（projector/repos 追加面/双 `__init__` 纯追加/3 处既有测试跟进均为断言增强非弱化）+ T-C1-010 12 项未决全文 + 契约五节 + TS 六锚点；dedupe 存储键直查 SQL 亲验（`("prompt:req_1",)`）；主树 test_eventlog.py 基线依赖 grep 亲核（§5 理由① 行号级）。四门禁（**194 passed**=166+28，58.74s / ruff / format / mypy 39 files）采信调度器亲跑。

## 1. 结论：**通过**

**0 blocker / 0 major / 0 minor / 2 nit + 1 观察**（nit 均非并回前置：① 为注释精确性+R2 一行加固，② 为可选微测）。§7 四步/占槽/返参形状/L316 prompt 语义 + §3.2 commit 五步 notify + §6.1 快照 + §8 wire 逐条行号级落地（§3）；调度器契约冲突裁决（r1-slices L144 括注作废、read_only_diagnostic 始终 SessionReadOnly 含同 request_id 重试）落地且有专门测试钉死（`test_prompt_read_only_gate_precedes_idempotent_retry` L247-266）；active_run_id 接线（调度器预检发现的 S4 真实缺口）fold `replace` 携带 + projector 同事务 UPDATE 正确落位；**fold 反弹 = 同意反弹**（§5：三理由逐条独立验证成立 + 旁路风险可接受）；12 项未决全部同意（§4）；**无升 A 项**。

## 2. 发现分级

- **nit ①（注释精确性·回拨时钟）**：`AscendingIds._next` 的注释宣称时钟回拨下"IDs stay unique and ascending within the process"（service.py L217-219），两句保证在回拨下均不严格成立。位置：service.py L211-220。分析：仅当 `ms > last_ms` 才把 counter 清零，`ms <= last_ms`（含回拨）counter 续增——(a) **回拨**（ms 变小）：新 ID 的 13 位 ms 段变小，10 位 counter 是低位段 → ID 字典序**变小**（违反 ascending）；(b) **往返振荡**（回拨后再次前进越过 last_ms）：counter 清零 → 可重复早前已发出的 (ms, counter) 对 → ID **重复**（违反 unique）。影响有界：events.id 主键 + unique(session_id,seq) + unique(session_id,batch_id,batch_ordinal) 全部 fail closed——重复 ID 提交被拒为存储错误，无数据损坏；恒定/单调 clock（含全部测试注入与正常系统钟）下两句保证成立。TS 基准对照：`ascending()`（identifier.ts L16-31）回拨同样不保 ascending，但其 ID 尾部是 14 位 crypto 随机字符——重复概率可忽略；Python 为确定性与可断言性去掉了随机后缀，唯一性遂完全依赖 clock 单调，而注释把该前提写成了无条件保证。建议：R2 一行加固——`_next` 首行 `ms = max(ms, self._last_ms)` 单调钳制（两句保证同时成立，恒定/单调 clock 行为不变）+ 注释措辞更正；非并回前置。
- **nit ②（测试覆盖）**：跨会话 request_id 复用无直接测例。位置：service.py 步2 L426-436 + repos.py `prompt_by_dedupe_key`（查询以 session_id 为参）。理由：§7 L321 幂等键为 session 内 request_id——结构上两 session 用同一 request_id 互不可见（dedupe 查询按 session 限定），实现正确，但"跨会话复用互不冲突"这一作用域语义无测试钉住。建议：可选微测——两 session 以同一 request_id 各自 prompt，均成功返回不同 RunRef。
- **观察（B 新增）**：repos.py `session_by_active_run` 在（理论不可达的）多行情形静默 `ORDER BY session_id LIMIT 1`。理由：单写者 + 服务唯一入口 + fold 为唯一置槽来源（projector `_SET_ACTIVE_RUN` 的值唯一来自 fold 输出）之下多行不可达；若真发生，下游以 `active_run_without_prompt_event` 类 StorageCorrupt 暴露，但丢失"多行"这一更精确病因。与 T-B-011 形状归一观察同性质，记台账（§6-3）无行动。

## 3. 契约符合性清单（逐条 → 实现/测试行号）

### §7 方法表 R1 子集（L298-312）

- 六方法 create/session_status/messages/prompt/run_status/close 落地；cancel/reply_permission/revoke_permission 不实现**不留 stub**（service.py docstring L4-10 自文档，L325）。create L317-348（§7 表签名不变；working_dir 构造注入=未决①）；session_status 走 `_read_record` L537-564（未知 status→StorageCorrupt fail closed L556-563；不触发恢复/外部调用 L305；非 active 状态仍可查 §3.2.1）；messages 入参校验→S6 repos 复用（游标归属/钳制/fail closed，测试 L624-664）；run_status L477-507；close L511-525（closed gate+锁映射清空=自有两类资源；重复 close 幂等；读面照常）。
- §7 L312 不得遗留后台任务：R1 无调度任务；App.close 按序 service→database（engine dispose+writer lock 释放）——`test_app_close_releases_engine_and_writer_lock_idempotently` L670-694：close 后 create/prompt→SessionClosed、`bus.subscription_count==0`、writer lock 释放后**新 App 可开同目录并读回**。

### §7 L316 prompt 语义

- 只提交一个 `py.prompt.accepted(version=1)`：步4 L445-473 构造单 draft EventBatch；data 三标识一致（session_id/run_id/request_id）+message=UserMessage+metadata 含 run_id/request_id；projector 同事务插 UserMessage+**置 active_run_id**（`_project_prompt`：message INSERT → `_set_active_run_id` → `_advance_applied_seq`，同一事务）；只有事务提交成功才返回 RunRef；不产生 run.started/assistant/调度、不调 provider/工具（service.py 无任何 runner/调度面）。测试：闭环 L189-227 + 步4 L459-494 + 28 测全绿。

### §7 L318-323 四步固定顺序（顺序即语义）

- **锁切分评估（审查重点 1）**：状态无关入参校验 `_require_*` L238-257 + `prompt_dedupe_key` L260-262 在取锁**之前**；一切触 DB 步骤（门禁/幂等/H 读取/commit/notify）在 `_session_lock` L529-535 之内。**切分正确**：纯校验无 DB 读取、无可竞态观察，镜像 S4 txn `_validate_arguments`-before-lock 模式；§3.2 L167"同锁内读 H/A"的线性化要求不受影响。
- **步1 状态门禁 L407-424**：NotFound（缺行且 H==0，`test_prompt_rejects_missing_session_not_found` L229）；**缺 sessions 行且有存活日志→StorageCorrupt(`sessions_row_missing_with_log`) 不伪造 NotFound**（`_read_record` L547-555，§3.2 L163"已有日志却缺失 sessions 行属于读模型缺失"）；read_only_diagnostic **始终** SessionReadOnly 含同 request_id 重试（契约 L320；调度器对 r1-slices L144 括注的作废裁决）——**裁决落地钉死**：`test_prompt_read_only_gate_precedes_idempotent_retry` L247-266（隔离会话同 request_id+同 text 仍拒 + session_status 仍可查）；cleanup_failed/closed 存储行各拒（L269-288）；session active 后查 service 级 closed gate→SessionClosed（L289-319：prompt/create 拒、读面照常、重复 close 安全）。
- **步2 durable 幂等 L426-436（先于步3/4）**：`prompt_by_dedupe_key` 命中→text 逐 code point 相同→原 RunRef **零副作用**（`test_prompt_idempotent_retry_returns_original_run_without_side_effects` L320-355：事件数不变/messages 不变/活订阅 `wait_for` TimeoutError 证不重发通知+阳控——新 session 的 prompt 通知可达）；不同→IdempotencyConflict 即使 busy（L358-379；含 " hello"/"Hello"/"hello " 近似文本无 trim/大小写折叠）。dedupe_key=`"prompt:"+request_id` 精确拼接（L260-262）——B 直查 SQL 亲验存储键 `("prompt:req_1",)`。
- **步3 L438-443**：active_run_id 已占→SessionBusy（`SessionBusyError` L117-126，details 含 active_run_id）——在幂等未命中之后；并发不同 request_id（gather）→恰一 RunRef+一 SessionBusy+日志恰一条 accepted（`test_concurrent_prompts_one_run_one_busy` L408-439）；并发同 request_id→同一 RunRef（`test_concurrent_identical_prompts_share_one_run` L440-453）。
- **步4 L445-473**：通过后才生成并固定 run→msg→evt→batch ID+时间戳（**SeqIds 钉定生成序**，`test_prompt_expected_seq_is_the_pre_commit_high_watermark` L459-494：run_0004/msg_0005/evt_0006+seq=[1,2]+applied_seq=2+active_run_id 落库+重开后 MAX(seq)/applied_seq 不前移+第二 session 从 H=0 独立编号）；`expected_seq=log_high`（提交前 H，§3.2 L165/L167——H 读取在锁内）；`EventBatch(batch_id,(draft,),dedupe_key)` 提交；成功后 bus.notify；失败回滚不留痕（SQLite 事务整体回滚，S4 已测）。

### §7 L325 占槽

- R1 accepted 跨 close/reopen 持续占槽：`test_r1_accepted_run_holds_the_slot_across_close_and_reopen` L385-404（重开新 engine+新 service：新 request_id→SessionBusy、同 request_id→原 RunRef、run_status 仍 accepted）。不自动释放：全库唯一置槽路径是 projector `_SET_ACTIVE_RUN`、值唯一来源 fold 输出；service.close 不碰 sessions 行（只释放自有资源）。

### §7 L314 返参形状

- SessionRef={session_id}（ses_ pattern）；RunRef={session_id,run_id,request_id} 三项非空+ses_/run_ pattern+request_id 不强制前缀——`test_prompt_rejects_invalid_inputs_before_any_read` L592-605（参数化×6：session_id 非字符串/空/前缀错+request_id 非字符串/空+text 非字符串→ValidationError 且 events 零行=**校验先于任何读取**）；空字符串 text 合法（L608-618，仅类型校验非 falsy 拒绝）；RunStatus 六值枚举 R1 仅 accepted 可达；MessagePage=S6 复用。

### §3.2 commit 五步 + 锁纪律（L155-159/L167/L171）

- L155 门禁先于幂等查询：服务层步1→步2 与 store 层 txn 门禁次序双面一致（S4 已测 + 服务层 L247-266 钉死）。L156 整批已存在先于 expected_seq 等值检查：服务层幂等命中早退零副作用；store 层重试兼容（expected_seq 落后仍返回原提交，L165 末句）。L157/L158 归属/版本/合法迁移+连续 seq+同事务 projector 一起 commit：S4 落地；S7 增量=`_project_prompt` 的 active_run_id UPDATE 同事务（message INSERT 之后、`_advance_applied_seq` 之前——投影序正确）。L159 步5 按 seq notify：prompt commit 成功后 + **create 亦 notify**（对称实现=未决⑤；无订阅者=no-op；重试返回已有提交不重投影/重发——服务层幂等早退天然满足）。
- L167 同锁内读 H→expected_seq：`log_high` 调用在锁内（步4）。L171 短期锁不跨外部等待：锁内仅本地 SQLite 快路径+内存 notify，无 model/approval/adapter 等待（R1 无这些面）。

### §6.1/6.2 快照（L283-290）

- `_coerce_config_snapshot` 复用 contracts `ConfigSnapshot`/`compute_snapshot_hash`（单点，S2 已测 canonical/SHA-256）：`{"schema_version":1,"permission_rules":[]}` 精确形状——两字段显式/无额外键/版本整数 1/规则必须空；非法→ValidationError 且 sessions 零行（`test_create_rejects_invalid_snapshots` L568-578，参数化×6：额外键/非空规则/字符串版本/布尔版本/缺字段/非对象）。

### §8 wire

- `SessionBusyError`（L117-126）`StoreError` 子类、code=SESSION_BUSY、携 `.core` wire、retryable=False 沿用家族默认（未决⑧）；ValidationError=contracts 面（无 store wire）。

### app.py 组合根（r1-slices L139 TS 映射）

- `open_database`→`VolatileBus`→`SessionService`（真 store/projector/repos）显式构造注入——非 Effect 替代 `projector.ts` Layer L537+（deps [Bus.node, Database.node]）与 `sqlite.bun.ts` L100-103 Layer 结构；无全局单例、无隐式资源；`close()` 按序 service→database 幂等+async context manager 同序；**不跑 migration**（Alembic CLI 拥有 schema，§3.2/§5——组合已迁移数据目录）；working_dir 缺省 `os.getcwd()`。测试 L670-694。

### repos 仅追加 + 三属性（审查重点 5）

- `log_high`/`prompt_by_dedupe_key`/`prompt_by_run_id`/`session_by_active_run`/`AcceptedPrompt`/`_decode_prompt_row` 全为追加面（diff 亲验：唯一 `-1` 行=import 机械展开）；只读/bus-free/无外部调用保持（T-C1-009 结构断言绿=166 基线无回归佐证）；fail closed：非 v1 类型/版本→`REASON_STORED_DECODE_FAILED`+新增 reason `dedupe_row_unexpected_type`；`AcceptedPrompt.message_text` verbatim 幂等比较、最高 seq 胜；`log_high` 本地 SQL 不 import bus（eventlog 私有同义逻辑不共享）。

### state.py/projector.py 接线（任务④，S4 缺口补齐，审查重点 4）

- `SessionState.active_run_id: str|None=None`（L72 末位新增）；`_apply_prompt` 以 `replace` 携带 `active_run_id=event.data.run_id`（L118-141；docstring L124-128 注明 S8 按 seq 重放重建读模型需此携带）——fold 纯函数性质保持（无 IO/无时钟/无随机）。projector `_SELECT_SESSION` 增列+`_SET_ACTIVE_RUN`+`_project_prompt` 同事务 UPDATE。3 处既有断言跟进均为**增强**非弱化（`is None`→`=="run_1"`：test_projector×1+test_eventlog×2——钉住新接线值；`test_store_commit.py` 隔离记录断言无 active_run_id 不受影响，B 核实）。

### §3.2.1 读边界

- read_only_diagnostic：prompt（含重试）始终拒而 session_status/messages/run_status 读面仍可用（L247-266+L624-664）。

## 4. 12 项未决逐项意见表（T-C1-010 §③ 1-12）

| # | 未决问题 | C-1 取舍 | B 意见 |
|---|---|---|---|
| ① | A. working_dir 构造器注入，create 保持 §7 表签名；App 缺省 `os.getcwd()` | 保守读法（备选 create 增参未采） | **同意**——§7 L304 表签名钦定 `create(config_snapshot)`；R1 单 service=单进程单 CWD 无 per-session 切换；构造注入保表签名完整+测试可注入 |
| ② | B. `AscendingIds` 落位 service.py（contracts 冻结）；prefix+13 位 ms+10 位 counter 精确整数数学；跨进程不保证唯一 | 单 writer 前提下成立 | **同意**——落位合理（contracts 冻结；TS 对应物也在 schema 层）；精确整数 epoch-ms 无浮点；恒定 clock 严格递增（docstring L200-201 该句成立）。**挂 nit ①**：L217-219 回拨保证注释不精确（§2），R2 一行 clamp 入台账 §6-1——实现行为本身正确（DB fail closed） |
| ③ | C. run_status 按 active_run_id+`prompt_by_run_id` typed data 取 request_id（非 metadata）；orphan 槽→StorageCorrupt | typed data 优先 | **同意**——§7 L316 data 三标识一致是钦定来源、metadata 非权威；orphan→`active_run_without_prompt_event` fail closed 符合 §3.2.1 L193"不伪造 RunStatus"；`test_run_status_finds_the_active_run_and_fails_closed` L523-551 钉死（含 SQL 置 run_orphan 面） |
| ④ | D. `batch_` 前缀无 §3.1 约束 | batch id=幂等组键 | **同意**——§3.1 L114 四类清单不含 batch；§3.2 L161 只要求稳定 batch_id+重试复用全部内容；AscendingIds 保证字典序可排 |
| ⑤ | E. create 亦 notify（对称 §3.2 L159 步5） | commit→notify；无订阅者=no-op | **同意**——L159"按 seq 发 Bus 已提交通知"适用一切提交（含 create 的首事件）；重试不重发由服务层幂等早退+store 层 L159 末句双面保证；测试阳控已覆盖 |
| ⑥ | F. `SessionBusyError` 本地定义 service.py+`session/__init__` 导出（任务书"storage 已导出"系笔误） | storage 无此类（诚实浮报） | **同意**——B 亲验 storage 包 grep 零命中（调度器同验）；busy 是 §7 服务协议门禁（步3）非 §3.2 store 写门禁，落位 service 语义正确；`StoreError` 子类保 §8 wire 家族一致；移入 storage 属纯移动无必要 |
| ⑦ | fold 层不拒第二个 prompt.accepted（任务④推荐保守读法**未采**） | 反弹（三理由，进度③-7） | **同意反弹**——专项裁决见 §5：三理由逐条独立验证成立+旁路风险可接受 |
| ⑧ | `SessionBusyError.retryable=False` 沿用家族默认 | §8 未逐 code 钦定 | **同意**——§8 无逐 code retryable 表；R1 storage wire 全家族 False 保持一致；retryable=true 的语义（客户端可重试）在 R2 调度器落地后才有意义，届时单行覆写即可 |
| ⑨ | service.close 与"drain bus/dispose engine"调和：engine/writer lock 所有权归组合根 | 按任务②释放序（service.close→engine dispose） | **同意**——调和论证成立：prompt 的状态读取先于 service gate，engine 若已 dispose 则"close 后 prompt→SessionClosed"无从实现；bus 无状态（S5/S6 定案）无需 drain；App close 序 service→database 测试 L670-694 |
| ⑩ | 锁映射无上界（close 清空） | TS KeyedMutex 同为进程内映射 | **同意**——TS bus.ts `durableLocks` 同为 get-or-create 无淘汰（L579 亲核）；R1 单进程会话数有限；close 清空+重复 close 幂等已测 |
| ⑪ | 跨进程窗口不构造 | 双开已 WriterLockHeldError 拦截（R0-B 已测） | **同意**——§3.2 L185 Phase 1 每数据目录单 writer（OS 文件锁）；构造该窗口需绕过组合根双开，R0-B 已测失败面；M1 租约落地前无威胁模型 |
| ⑫ | 无契约矛盾自报；r1-slices L144 括注冲突按契约为准 | 以契约为准（任务书明示） | **同意**——B 逐条对齐 §7/§3.2/§3.2.1/§6.1/§6.2 与 L134-149 无矛盾；裁决落地+测试钉死（§3 步1） |

未决处置：**同意 12 · 需改 0 · 升 A 0**（② 挂 nit ① 注记——注释精确性台账项，非行为缺陷，不构成需改）。

## 5. fold 反弹专项裁决（审查重点 3）

**裁决：同意反弹。** C-1 拒绝"fold 拒绝第二个 prompt.accepted"的任务④推荐保守读法正确。三理由逐条独立验证：

1. **理由①（基线依赖）——验证成立**。B 亲核主树 test_eventlog.py：`_populate(store, ses, prompts=3)` 辅助（L148/L157）在单 session 连续提交 3 个 accepted，是 **19 处**调用的标准夹具（L197/229/273/313/338/387-388/415/472/516/548/574/597/628/699/797-798/830/870，含 ses_b 两处）；分页测试另以 3 个单事件 accepted 批构造 msg 与 seq 序刻意错开（L748-758，batch_p1/p2/p3）；follow 系列在 populate 后再追加 accepted（L489/L528/L554/L643 循环/L658/L715）——**单 session ≥4 个 accepted 是已并主树基线常态**。若 fold 拒第二个 accepted，S4 commit 时投影即抛错，166 项基线大面积破坏。"同 session 多 accepted 可提交"是既定事实，不是待裁决的开放语义。
2. **理由②（契约层级）——验证成立**。§3.2 L155-158 写入门禁清单（只读诊断/整批已存在/H·A 不变量/归属·版本·合法状态迁移）**无 run 占槽项**；§7 L323 把占槽检查放在服务协议步 3（幂等未命中之后）——占槽是服务层协议规则，非持久层/fold 规则。TS 对应物 bus.ts L579 `durableLocks.withLock(aggregateID)` 仅按聚合串行化提交，持久层同样无占槽门禁（B 亲读 L573-586）。fold 是 §3.3 纯函数规则集（S4 定案），把协议门禁下沉 fold 属层级错置，且会同时劫持 S8 rebuild（重放即撞门禁）。
3. **理由③（实质后果）——成立，但措辞最松**。R1 无 run 释放终态事件（cancel/finish 属 R2；§7 L325 钦定 accepted 占槽不自动释放），fold 拒绝将把会话永久封顶在 1 个 accepted：理由① 中 ≥4-accepted 类合法日志不可生产、S8 rebuild 对存量日志直接 fail——重建敌对。进度文件"dedupe 查询需要已提交事件"一句是最弱一环（dedupe 命中的前提本就是第一个 accepted 已提交，与第二个被 fold 拒无直接逻辑关系），但该句可删而结论不变（①② 已独立充分）。

**旁路风险评估：可接受**。直连 `SessionStore.commit` 绕过 service 可在读模型覆盖 active_run_id（last-accepted-wins）。缓解充分性逐项：① §7 L300 service=CLI/同进程/未来 HTTP 的唯一入口——绕过即违反架构前提，非受支持用法；② §3.2 L185 Phase 1 每数据目录单 writer（OS 文件锁，R0-B 已测双开失败）——旁路者必是同进程持锁 writer 自己；③ 失败有界且自洽——fold 纯函数，任意日志重放结果一致，S8 重建不放大不一致；④ §3.2 L153 Bus/工具适配器/查询 repo 无权写——写路径面已收窄到 service+store。结论：R1 威胁模型内不可达，R2 持久不变量选项留台账（§6-4）。

## 6. 裁决事项（需调度器/用户）

无 blocker/major/minor 级裁决；**无升 A 项**。台账转下游（不阻塞并回）：

1. **nit ① clamp 加固**（R2，与未决② 合并处理）：`AscendingIds._next` 首行 `ms = max(ms, self._last_ms)` 单调钳制 + L217-219 注释更正——一行改动，恒定/单调 clock 行为不变。
2. **nit ② 可选微测**（R1 收尾或下一切片 C 微任务）：两 session 同 request_id 各自成功——钉住"幂等键 session 内作用域"。
3. **观察（`session_by_active_run` 多行原因精确化）**：与 T-B-011 形状归一观察同台账；S8 消费该查询前裁决。
4. **R2 持久不变量设计注记**（源自 §5 旁路缓解）：若 R2 调度器需"同 session 第二个 accepted 须前一 run 已终态"类 events 层约束，须**兼容 R1 已生产的多 accepted 日志**（存量日志无终态事件——需版本化/迁移语义，不能对存量直接 fail closed）。

R1-S7 **可并回**（`5c7be04`，父=#53 `848031f`），R1-S8（rebuild/恢复）可派发。



