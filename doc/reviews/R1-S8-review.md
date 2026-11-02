# R1-S8 结构化审查（席位B · T-B-013）

- 审查对象：worktree `r1-s8` 分支 r1-s8 单提交（`28d6efa` S8 主体 7 文件 +1080/-5；父=#58 `271675e`）：`storage/txn.py`（+263 **零删除**：docstring 段+3 reason+6 SQL+`rebuild` L345+`_rebuild_transaction` L661+`_rebuild_sequence` L671）/`storage/store.py`（+18：`rebuild` L103 转发）/`session/service.py`（+41/-7：`rebuild` L534+AscendingIds 注释更正 L216-225）/`.github/workflows/ci.yml`（+6/-6）/`tests/integration/test_rebuild.py`（新增 12 测）/`doc/reference/r1-exit-evidence.md`（新增）/进度 `T-C1-011.md`（10 项未决）。既有测试文件零触碰（stat 亲验：7 文件清单内无既有测试）。
- 基线：`doc/CORE-CONTRACTS.md` §3.3 L200-213（L202 rebuild 全语义）+ §3.2.1 L189-198 + §3.1 L187 + §3.2 L153/L158/L165/L183 + §6.2 L283-290 + `doc/PHASE1-SLIM.md` L65-74（验收 C 后半+验证四项）+ `doc/plans/r1-slices.md` L151-160 + TS 四锚点 B 亲核属实（`bus.ts` replay L674-704（per-aggregate 锁+可选 publish——Python §3.3 更严不发通知，显式偏离已文档化）、`decodeStoredData` L74-76（decode 单点升级同构）、`InvalidDurableEventError` L60-66、`readAfter` L808-827 跳过未知——T-A-002 终裁不沿用）。
- 复验方式：B 全量亲读 txn/store/service/ci 四文件 diff 全文 + test_rebuild.py 602 行（12 测逐个清点，断言强度逐条核）+ 进度 10 未决 + 证据文件 80 行；亲证七件：①导入环推演（storage/__init__ L23→repos L55→txn→session/__init__ L13→projector L55→半初始化 storage→ImportError——C-1 环主张**属实**）；②锁对同款同序（commit L327-328 ∥ rebuild L371-372）；③**206 分布亲跑 `pytest --collect-only`**：contract 82 + integration 92（4/16/11/20/28/1/12 逐文件全中）+ r0 32 = 206，与证据 §5 完全吻合；④证据文件行号 grep 抽验（S3/S7/S8 三行全部 file:line——txn 11 处/service 9 处/repos 4 处/projector 5 处/state 2 处/store 3 处/app 2 处——**全中**）；⑤哈希 decode 层单点（contracts/events.py L106-111 model_validator 亲读）；⑥fold 机制（state.py 141 行全文：applied_seq=event.seq L110/L138、IllegalTransitionError 全转换规则 L88-96/L101-106/L130-135、messages 增量收集）；⑦ci.yml 全文。四门禁（**206 passed**=194+12，67.21s / ruff / format 117 files / mypy 40 files）采信调度器亲跑。

## 1. 结论：**有条件通过**（1 修复项，见 §8-1）

**0 blocker / 1 major / 0 minor / 1 nit + 2 观察**。§3.3 rebuild 事务语义（锁对+单事务+清空回滚+全量验证后替换+失败隔离+不通知不生新事件+幂等+A<H 修复）与 §3.2.1 诊断接线主路径逐条落地且测试钉死（§3）；调度器契约裁决与 T-B-012 nit①② 落地全部核实（注释更正 L216-225 与 B 的行为分析逐句吻合+clamp 归 R2 注记+零行为变化；nit② 微测 L574-602 即 B 建议的微测原样）；两专项裁决均**同意**（哈希 decode 层单点 §5-1；fold 调用点导入 §5-2——环亲证成立）；防御路径未设测**同意**（§6）；10 项未决**同意 9 · 需改 1**（⑩"无契约矛盾"自报被发现 1 推翻一角）；证据文件抽验全中+206 分布亲证吻合，唯 §6 末条承继发现 1 的失实须随修复更正（§7）；**无升 A 项**。

**发现 1（major）= 空日志早退分支：DELETE 先于早退提交清空**——`_rebuild_sequence` L712-713 先 DELETE messages+sessions 行，L715-722 才判 `if not rows: return`，`_rebuild_transaction` L666 照常 commit → 幸存行被删。仅 SQL 篡改 events 可达（协议不可达）、206 绿（分支无测）、修复小（重排+三处文档更正+两微测），故 major 非 blocker。

## 2. 发现分级

- **major ①（空日志+幸存行：清空被提交+三处文档与行为相反）**。位置：txn.py L710-722（DELETE L712-713 → 早退 L715-722）→ 提交 L666。理由：(a) **最小诊断记录被清除**——最小记录（§3.2.1 L193，applied_seq=0、read_only_diagnostic、diagnostic 非空）+空日志（行删→隔离建最小记录→events 再被清）→ rebuild 返回**成功**且记录被删 → 此后读取 NotFound——借 rebuild 自动清除了诊断标记，直接违反 L196"再次 rebuild 都不能自动清除诊断标记；重建须保留该管理标记"；(b) **A>H fail-open**——active 行（applied_seq=N≥1）+events 全被清（H=0）→ A≠H 属 §3.2 L165/§3.2.1 L191"已存投影不一致"，应 StorageCorrupt+隔离；实际假成功+销毁幸存读模型（该 session 存在过的最后证据）；(c) **注释/进度/证据三处文档与行为相反**——代码注释 L717-719"kept as-is"、进度①-6"原样保留"、证据§6"no-op 返回（最小诊断记录原样保留）"均称保留——实现滑误而非设计取舍（意图明确是保留，代码未做到）。可达性：协议不可达（create 行+事件同事务 §3.2 L183；隔离入口建最小记录时日志必非空；协议无 events 删除路径）——需 SQL 篡改 events 表；206 全绿（该分支无任何测试）。建议（最小改动）：把日志空判**提前到清空之前**；空日志+幸存行——最小记录特征（applied_seq==0 且 read_only_diagnostic 且 diagnostic 非空）→ 真 no-op 保留（兑现三处文档）；applied_seq>0 → A>H → `_corruption`+隔离（建议复用 REASON_SEQ_GAP first_bad_seq=1"日志必须从 seq 1 起"或专用 reason）；同步更正代码注释/进度①-6/证据§6 三处；补两分支微测（最小记录+空日志→rebuild→记录逐字段原样；active 行+清空 events→rebuild→StorageCorrupt+隔离+行保留）。修复不触及 206 基线（分支今日无测）。
- **nit ①（测试覆盖）**：closed/cleanup_failed 行的 verbatim 携带无直接测例（read_only_diagnostic 已测 L388-432；同一携带代码路径 L706-708+L810-815；closed 行 R1 协议不可产生——§3.1 L187 无会话关闭命令）。可选微测：closed 行+健康日志→rebuild→status 仍 closed。
- **观察 ①（B 新增，需裁决）**：A>H 且日志非空（尾部 events 被删，日志自洽 1..k）→ rebuild **静默规范化** A:=H（信任日志），与被 blessing 的 A<H 修复（任务审查重点 1+docstring）对称；但 A>H 的语义是**日志被截**（§3.2 L183 不删除日志前缀），契约 L165 字面 A≠H→StorageCorrupt。任务/进度/测试三方均未处置 A>H。见 §8-2 裁决请求。
- **观察 ②（无行动）**：service.rebuild 与 prompt 的 service-closed 门禁次序不同——rebuild 先 closed gate 后 NotFound 判定（L560-566 区段）；prompt 契约钦定先状态门禁后 service gate（§7 L318-323 步1）。管理面保守统一（开放点 E docstring 注明），rebuild 非 §7 表方法无四步约束——可辩护，记录即可。

## 3. 契约符合性清单（逐条 → 实现/测试行号）

### §3.3 rebuild 事务语义（L202）

- **会话锁+同一事务**：`rebuild` L345-372——入参校验（StoreValidationError）→ per-session lock + 进程写锁（**与 commit 同款同序**：commit L327-328 ∥ rebuild L371-372，B 亲证）；`_rebuild_transaction` L661-669 单事务（sequence → commit；任何 `BaseException` → rollback 整批）。
- **清空在事务内**：DELETE messages+sessions L712-713 在事务内；任何后续失败连清空整批回滚 → 原投影保留——测试断言 `after[1:5]==original_row[1:5]`（管理面除外的数据面逐项保留）+ messages 原文全等（SQL 直查 data 列）：L229-230/L296-297/L326-327（三路失败回滚测试）。
- **全量验证成功才 commit 替换**：逐行 fail closed——seq 非法/缺口/重复 L729-757（first_bad_seq=首个缺失 seq，镜像 commit 路 `_SELECT_FIRST_GAP_SEQ`）、`_decode_stored` L758-769（未知 type/version/坏 JSON/§6.2 哈希不符）、fold 转换损坏 L770-791（`IllegalTransitionError`——首事件非 py.session.created 即 fold 抛错，state.py L134-135 亲证"fold 是转换规则单一来源"）；后验 applied_seq=H L832-844。**不留半份**=单事务原子；**不跳坏点**=无任何跳过逻辑（TS `readAfter` L808-827 跳过保游标前进——T-A-002 终裁显式不沿用，S6 已测、S8 不改）。
- **失败后隔离**：rebuild 包装器 L373-388——`StorageCorruptError` 且 isolate → 事务已整体回滚后 `_isolate_locked` L846-870 独立短事务（UPDATE rowcount=0 → INSERT 最小记录 L862-863；隔离持久化失败 → 内存写门禁+链式上报 L865-869，与 commit 路同款 N-1）；isolate=False（REASON_REBUILD_INVARIANT）→ 仅回滚不隔离。
- **不再次发业务通知/不生成新事件**：结构保证（rebuild 的 6 条 SQL 只 DELETE messages/sessions+INSERT sessions/messages，无 events INSERT；txn 模块结构不可达 bus——`test_store_and_txn_never_import_a_bus` 既有断言）+ 测试阳控 L438-462（events 计数不变+活订阅 0.05s 超时+同机制新 commit 通知可达阳控）。TS 对照：replay L674-704 带 `options.publish` 可通知——Python §3.3 更严（永不），显式偏离已在 r1-slices L157 文档化。
- **幂等**：同一日志重放=确定性函数——测试 L186-188（连续第二次 rebuild 全等，messages+sessions 六字段）。
- **A<H 非损坏**：applied_seq:=H 即 rebuild 职能——测试 L363-382（手改 applied_seq=1→rebuild→active+A=2+messages 全等；status 保持 active）。
- **例外**：空日志+幸存行分支=发现 1（L712-722 DELETE 先于早退提交——L196 标记保留与 A≠H fail-closed 被破）。

### §3.2.1 只读诊断裁决（L189-196）

- **检测范围（L191，rebuild 路径全量）**：未知 type/version、无效 JSON、快照哈希不符 → decode 层 L758-769（哈希经 `DurableEvent[SessionCreatedData]` envelope 强制，events.py L106-111，专项裁决 §5-1）；seq 不从 1 连续递增（缺口/重复/非法值）→ L729-757；归属/状态迁移损坏 → fold L770-791；已存投影不一致 → A<H 修复（blessed）/A>H 未处置（观察①）/空日志 A>H=fail-open（发现 1b）。
- **隔离机制（L192）**：`_isolate_locked` L846-870——私有 writer 会话锁下独立短事务、脱敏 diagnostic（`Diagnostic.to_json` 四字段 reason/first_bad_seq/first_bad_event_id/applied_seq）、不追加业务事件、不推进 seq/applied_seq、不通知 Bus（结构+阳控）；DB 无法持久化 → 内存写门禁+报告存储错误 L865-869（测试：S3 既有 `test_isolation_persist_failure_chains_and_keeps_memory_gate`）。
- **最小诊断记录（L193）**：行删+首事件损坏 → reason=seq_gap/first_bad_seq=1/applied_seq=0+INSERT 四元组 `("read_only_diagnostic", None, None, 0)`+payload 全等——测试 L465-499 逐字段断言。**例外**：空日志+最小记录→发现 1a（记录被删）。
- **标记持久（L196）**：幸存行 status+diagnostic verbatim 携带 L706-708 → 重 INSERT L806-817；修复日志后成功 rebuild → 数据面还原而标记保留+写三路（同 id 同文/同 id 异文/全新）均 SessionReadOnly+健康读取可用+二次 rebuild 标记仍在——测试 L388-432。**例外**：空日志→发现 1a。
- **读取仍可用（L194）**：隔离测试内联断言（session_status 返回 READ_ONLY_DIAGNOSTIC L239/L419、messages 返回原投影项 L230/L297）+ S6 既有读面。
- **写命令拒绝含重试（L195）**：发现那次报 StorageCorrupt、后续写（含同 request_id 同文本重试）SessionReadOnly——测试 L238-245/L299-300/L422-427/L543-544。

### §3.1 L187（诊断清除归 R5/人工）

- S8 不实现解除诊断命令：diff 内无任何清诊断/恢复 active 的代码路径（rebuild 只 verbatim 携带）✓；证据 §6-1 台账化。

### §3.2 L153/L158（受控管理写不产生新 events 行）

- rebuild 的写面=DELETE messages/sessions+INSERT sessions/messages（6 条 SQL 亲核），无 events 表写、无 bus 可达（txn 结构断言既有）✓；测试 events 计数不变 L245/L448/L454。

### §6.2 快照哈希（L283-290）

- decode 层单点：`SessionCreatedData._snapshot_hash_verified`（events.py L106-111，`model_validator(mode="after")` 每次 decode 重算 `compute_snapshot_hash`）——所有 decode 路径必经；测试 L505-544：JSON 合法仅哈希改坏 → reason=stored_event_decode_failed、first_bad_seq=1、隔离+回滚全款。专项裁决 §5-1。

### PHASE1-SLIM L71 验收 C 后半 + L72 验证四项

- **删除读模型后从日志重建一致**：测试 L157-188（DELETE messages+sessions → 读取先 fail closed `sessions_row_missing_with_log` → rebuild → messages 逐项+sessions 六字段全等+第二次 rebuild 全等）。
- **未知事件版本/序列冲突后只读诊断、读取可用、写被拒**：测试 L194-247/L248-301/L303-328（三路）+ 哈希 L505-544。
- **"投影损坏后进入只读诊断"的调和**：任务书 endorsed"投影损坏≠日志损坏"——投影行坏（日志健康）→ 读取 fail closed（`message_row_decode_failed`，§3.2.1 L194 逐查询语义）→ rebuild **修复**不隔离、status 保持 active、后续 prompt 照常答 busy——测试 L333-360。PHASE1-SLIM L71 措辞较粗，§3.2.1 精确语义（L194 读模型解码失败=查询级 StorageCorrupt；L191 诊断隔离=日志/投影一致性损坏）支持此读法；与同句前半"删除读模型后重建一致"（删除=极端投影损坏→重建）自洽。
- **验证四项**：证据文件 §4 映射（fixture 58+8/SQLite 集成 92/故障注入清单/只读诊断+无外部调用三结构断言）——测试函数名与实文件逐一亲读吻合；206 分布亲跑 collect-only 吻合（§7）。

### service 面（rebuild L534-565 管理入口）

- 入参校验先于锁（`_require_session_id_arg` 镜像 prompt 模式）→ per-session 锁 → closed gate→SessionClosed（开放点 E）→ "无行且 log_high==0"=NotFound（缺行有日志=**重建目标而非损坏**——与 `_read_record` 的损坏映射有意不同，docstring 注明 ✓）→ `store.rebuild`。诊断 session 允许 rebuild（受控管理+数据面恢复），管理标记由 txn 层保留（L196）。测试 L550-571（NotFound/ValidationError×4/SessionClosed）。
- **TOCTOU 评估（审查重点 5）**：`repos.session`+`log_high` 两读在 writer 锁外但在 service 会话锁内；单写者+协议无删除路径+service 唯一入口下，唯一穿插源=直连 store 的越架构调用方；最坏=过时 NotFound 误判，rebuild 事务内自带一致快照（同连接同事务），无真实窗口。可接受。
- **nit①② 落地（T-B-012）**：AscendingIds 注释 L216-225 与 B 的 T-B-012 §2 nit① 行为分析**逐句吻合**（回拨→更小 ID 破 ascending；振荡回访已见毫秒→重复 (ms,counter) 破 unique；"仅时钟不回拨前提下成立"；clamp 归 R2 明确不做）+零行为变化（`self._counter += 1`/return 未动）✓；nit② 微测 `test_same_request_id_across_sessions_yields_distinct_runs` L574-602=B 建议原样（两 session 同 request_id 均成功+不同 RunRef+各自消息/active run/事件计数）✓。

### CI 对齐（审查重点 7）

- ci.yml 全文亲读：Test 步 `uv run pytest -q` 全量（原仅 tests/r0）+Type check 对齐本地门禁（+tests/contract tests/integration）；双 OS matrix（fail-fast: false）/15min timeout/单 job/Lock check/平台探针+上传步骤**不变** ✓。时长评估：本地 206 测 67s；CI=uv sync ~1-2min+206 测（92 集成各起 alembic subprocess，Windows 慢侧估 2-4s/个）≈ 3-7min < 15min，安全余量充足。

### fold 复用与消息重投影一致性（审查重点 4 接线）

- `state.apply` 纯函数重放（调用点导入见 §5-2）；messages 增量收集 L771-781（`state.messages[appended_from:]`）+逐行 INSERT `canonical_json(model_dump(mode="json"))` L774-781——与 commit 路投影序列化同源，测试以 SQL 直查 data 列全等证实（L183/L206 等）。

## 4. 未决意见表（T-C1-011 §③ 浮报 10 项 → B 裁决）

| # | 未决项（C-1 主张） | B 裁决 | 依据 |
|---|---|---|---|
| ① A. rebuild 入口落位 | 并入既有类面：`SessionService.rebuild`（L534）→ `SessionStore.rebuild`（L103）→ `TransactionWriter.rebuild`（L345）；未建独立 `session/rebuild.py` | **同意** | r1-slices L155 本就"C 席定（或并入）"；锁对+事务机具是 txn 私务（独立文件须反注入/复制）；单入口架构（CLI/同进程统一调用面）成立 |
| ② B. 幸存行管理面 verbatim 携带 | status+diagnostic verbatim（L706-708 → 重 INSERT L810-815），数据面从 fold 终态重建；"行删+fold 终态非 active"协议不可达，浮报不设测 | **同意**（挂 nit①） | §3.2.1 L196 直接要求；测试 L388-432 钉定；边界可达性主张成立（行删=SQL 手改）；closed/cleanup_failed 携带无直接测例=nit①（同代码路径；§3.1 L187 协议不可产生） |
| ③ C. messages 重投影直接 INSERT | rebuild 事务内按 fold 终态增量收集+直接 INSERT（L771-781），projector.py 冻结零触碰 | **同意** | projector.apply 是提交期增量语义（active_run_id/applied_seq 推进、IntegrityError 映射），复用须引入"重建模式"面=更大偏离；两路序列化同源 `canonical_json(model_dump(mode="json"))`，消息全等经 SQL 直查钉定（L183/L206 等） |
| ④ D. 行删+日志健康→重建完整行 | 重放 `SessionCreatedData` 重建完整行（验收 C 正路）；仅首事件缺失/损坏才最小诊断记录 | **同意** | 验收 C 后半正路（测试 L157-188：先 fail closed `sessions_row_missing_with_log` → 重建全字段全等）；首事件必须 created（fold L134-135）→ 最小记录=§3.2.1 L193 镜像（测试 L465-499） |
| ⑤ E. closed 后 rebuild→SessionClosed | 与 prompt/create 统一的服务级门禁 | **同意**（挂观察②） | 保守统一可辩护（开放点 E docstring 注明）；门禁次序与 prompt 四步（§7 L318-323 步1 先状态门禁后 service gate）相反=观察②（rebuild 非 §7 表方法，无四步约束，记录即可） |
| ⑥ F. 单测试文件 | 单 `test_rebuild.py`（602 行 12 测），未拆 test_diagnostic.py | **同意** | 对 r1-slices L160 双文件计划的偏离=组织性：rebuild 语义与诊断持久性同路径交织（同一失败既测回滚又测置诊断），拆文件无增量价值且须复制会话构造模板；docstring 已声明范围 |
| ⑦ 检测分工+decode 层单点 | 哈希复核由 contracts decode 层统一强制（events.py L110），txn 层显式检查冗余已移除；S3/S6 无缺口未改既有面 | **同意**（专项 §5-1） | decode 单点=所有 decode 路径必经（model_validator L106-111）；测试以 reason=stored_event_decode_failed 钉定（L505-544）；TS decodeStoredData L74-76 同设计 |
| ⑧ fold 调用点导入 | `_rebuild_sequence` 内调用点 import（L689），模块级会成环（C 自验）；双 docstring 已注明 | **同意**（专项 §5-2） | B 亲证环成立（storage/__init__ L23→repos L55→txn→session/__init__ L13→projector L55→半初始化 storage→ImportError）；调用点导入双序均安全 |
| ⑨ 防御路径未设测 | 三例不设专项测：`REASON_REBUILD_DUPLICATE_MESSAGE`/`REASON_REBUILD_INVARIANT`/"最小记录后再次 rebuild" | **同意**（§6） | 各与已测机制同构或协议不可达；但发现 1 即"未测+未浮报防御分支"的教训 → §8-3 建议强制浮报 |
| ⑩ 无契约矛盾 | 自报与 §3.3/§3.2.1/§3.1/§3.2/§4/r1-slices L151-160 逐条对齐 | **需改** | 空日志+幸存行分支违反 §3.2.1 L196（诊断标记被清）+ §3.2 L165/§3.2.1 L191 A≠H fail-closed（发现 1）——"逐条对齐"不成立，须修复+三处文档更正 |

未决处置合计：**同意 9 · 需改 1 · 升 A 0**。

## 5. 专项裁决

### 5-1 哈希检测落位：decode 层单点 —— **同意**

- C 主张：§6.2 快照哈希复核由 contracts decode 层统一强制（`SessionCreatedData._snapshot_hash_verified`，events.py L106-111，`model_validator(mode="after")` 每次 decode 重算 `compute_snapshot_hash`），txn rebuild 层显式检查为冗余已移除；检测分工注记=未决⑦。
- B 亲证：① rebuild 路径 `_decode_stored`（L609，调用块 L758-769）→ `registry.decode_durable` → envelope `model_validate` → validator **必经**，无绕行支路；② 测试实证 L505-544：JSON 合法仅哈希改坏 → reason=stored_event_decode_failed、first_bad_seq=1、回滚+隔离全款（哈希不符是 ValidationError 的一种，与未知 version/坏 JSON 映射同一 reason，§3.2.1 L191 三例并列，正确）；③ 双点（decode 层+rebuild 专列哈希巡检）=同一规则两处维护、漂移风险、覆盖零增益（专列巡检是 decode 层的严格子集）；④ TS 同设计：decodeStoredData L74-76 注释自证 "Every stored event decodes through here" 单点；⑤ S6 读路径已测（S6 审查），S8 复用不加新点。
- 裁决：**同意**——单点正确，不另设巡检；txn 层移除冗余检查是对的。

### 5-2 fold 调用点导入（防环）—— **同意**

- C 主张：`_rebuild_sequence` 内调用点 `from opencode.session.state import ...`（L689），模块级 import 会构成环；txn 模块 docstring 与 `_rebuild_sequence` docstring 均已注明。
- B 亲证环成立（六步推演）：`storage/__init__.py` L23（import repos）→ `repos.py` L55（`from opencode.storage.txn import ...`）→ txn 若模块级 `from opencode.session.state import apply` → `session/__init__.py` L13（import projector）→ `projector.py` L55（`from opencode.storage import StorageCorruptError`）→ 此时 storage 仍在 L23 repos 导入中、L32-42 未执行（`StorageCorruptError` 未绑定）→ **ImportError**。即 storage-first 导入序下模块级 import 必炸。
- 调用点导入安全性：rebuild 实际执行时 storage 必已完全初始化（唯一写入方由 storage 自身提供）→ sys.modules 缓存命中；storage-first/session-first 双序均安全。
- 替代评估：TYPE_CHECKING+函数内 import 等价无增益；构造注入（如 Projector 接缝）是长期更净解，但为纯函数引入构造面=过度注入，R2+ 可再议。
- 裁决：**同意**——防环主张属实，调用点导入+双 docstring 注明是当前最小正解。

## 6. 防御路径未设测裁决 —— **同意不设测**

C 浮报三例，B 逐条同构论证：
1. `REASON_REBUILD_DUPLICATE_MESSAGE`：messages 唯一索引使正常提交面不可达（同 id 二次 INSERT 在 commit 面即 IntegrityError→IdempotencyConflict，L651-656）；仅 SQL 篡改 events 日志可达，失败面与已测隔离入口同构（`_corruption`→回滚+隔离）。
2. `REASON_REBUILD_INVARIANT`（重投影后验不变量，isolate=False 仅事务内回滚）：镜像 commit 路 A=H+n 复核（L461-476 已测）的同构后验。
3. "最小诊断记录之后的再次 rebuild"：行为即未决② 幸存管理面 verbatim 路径（L706-708/L810-815），同机制已被 L388-432 钉定（read_only_diagnostic 幸存行+健康日志→rebuild→标记保留）。

裁决：**同意不设测**——三例各为"已测机制同构"或"协议不可达（SQL 篡改专属）"，加测无新断言价值。**但**：发现 1 恰是"无测+未浮报"的防御分支（空日志+幸存行）酿成——⑨ 的浮报纪律没错，错在漏报；故 §8-3 建议下一切片任务书把"不可达/防御分支浮报清单"列为强制项。

## 7. r1-exit-evidence 抽验结论

- **行号抽验：全中**。证据文件 §0 表 S3/S7/S8 行及其后各节 file:line 抽验——txn 11 处（commit L323/`_commit_sequence` L401/`_healthy_high` L478/`_idempotent_replay` L530/`_insert_and_project` L623/`_decode_stored` L609/`_isolate_locked` L846/Diagnostic L242/rebuild L345/`_rebuild_transaction` L661/`_rebuild_sequence` L671）、service 9 处（AscendingIds L194/SessionService L282/create L322/session_status L357/messages L370/prompt L391/run_status L482/close L516/rebuild L534）、repos 4 处（log_high L300/prompt_by_dedupe_key L310/prompt_by_run_id L335/session_by_active_run L356）、projector 5 处（SessionProjector L95/apply L102/`_project_created` L111/`_project_prompt` L137/`_advance_applied_seq` L200）、state 2 处（apply L80/fold 转换规则 L134-135）、store 3 处（SessionStore L62/commit L84/rebuild L103）、app 2 处（App L30/close L76）、events 2 处（哈希 validator L106-111/EventDraft L138）——全部命中。
- **206 分布亲证吻合**：B 在 worktree 亲跑 `pytest --collect-only`——contract 82 + integration 92（migrations 4/store_commit 16/projector 11/eventlog 20/service 28/bus_commit 1/rebuild 12 逐文件）+ r0 32 = **206**，与证据 §5 完全一致。
- **门禁四项**：采信调度器亲跑（206 passed 67.21s / ruff / format 117 files / mypy 40 files）。
- **唯一失实项**：证据 §6 末条"空日志+幸存 sessions 行 → no-op 返回（最小诊断记录原样保留）"——与实现相反（发现 1：DELETE 先于早退提交，记录被删）。该条须随发现 1 修复同步更正；除此之外"无新主张"（L3 头注）成立，其余主张抽验均中。

## 8. 裁决事项

1. **修复项（有条件通过的条件）**：空日志早退分支重排（txn.py `_rebuild_sequence` L710-722）——日志空判**提前到清空之前**；空日志+幸存行且最小记录特征（applied_seq==0 ∧ read_only_diagnostic ∧ diagnostic 非空）→ 真 no-op 保留（兑现三处文档）；applied_seq>0 → A>H → `_corruption`+隔离（建议复用 REASON_SEQ_GAP first_bad_seq=1"日志必须从 seq 1 起"或专用 reason）；同步更正三处文档（代码注释 L717-719 / 进度 ①-6 / 证据 §6 末条）；补两微测（最小记录+空日志→rebuild→记录逐字段原样；active 行+清空 events→rebuild→StorageCorrupt+隔离+行保留）。不动 206 基线（该分支今日无测）。
2. **裁决请求（调度器）**：A>H 且日志非空（尾部 events 被删、日志自洽 1..k）→ rebuild 静默规范化 A:=H（观察①）。建议：A>H → 按已存投影不一致损坏+隔离（与 §3.2 L165/§3.2.1 L191 字面一致）；或对称 blessing 信任日志（如 A<H 先例）并文档化。任务书/进度/测试三方目前均未处置。
3. **建议（下一切片任务书）**：强制浮报不可达/防御分支清单（区分"协议不可达"与"未测防御"）——发现 1 即未测防御分支漏报的教训。

**R1-S8 修复后可并回**（`28d6efa`，父 #58 `271675e`）。



