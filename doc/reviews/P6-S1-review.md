# P6-S1-review — T-C1-094 微评审：Session HTTP 资源面补齐（list/get/delete/fork/export/import+单条 message get+SDK 七方法+两新 durable 事件）

- 评审对象：worktree `p6-s1` 席位工作区（未提交态，席位 git 写零）：改 15 跟踪文件 +1934/−58[contracts/events.py +54/−1、contracts/__init__.py +8/−0、session/state.py +65/−1、session/projector.py +51/−0、session/service.py +592/−24、storage/txn.py +7/−0、storage/repos.py +114/−0、storage/__init__.py +4/−0、bus/eventlog.py +14/−0、httpapi/app.py +123/−1、sdk/client.py +196/−26、tests/contract/test_schema_lock.py +5/−4、tests/integration/test_sdk_client.py +30/−1、doc/CORE-CONTRACTS.md +4/−0、doc/reference/schema/events.json +667/−0] + 新 1[tests/integration/test_httpapi_session_resources.py 528L 10 测]——与票面 §6 交付面吻合（新事件契约链全组=票面「contracts 事件新增最小面」预裁的合法展开）；票面拷贝 tasks/T-C1-094.md 正确保持未跟踪（#180 B-②）；进度档 `doc/orchestration/progress/T-C1-094.md`（席位弧含中断续跑，SEAT-EXIT 0 @11:19:11）。
- 评审基据（全部独立现读，禁继承）：15 文件 `git diff` 全量逐行亲读（service.py 含七公面+import 校验帮手群+`_created_draft` 共享重构全貌；projector/txn/eventlog/state 四投影-门禁面；repos 两新读面+messages deleted 过滤；app.py 七路由+两 wire 帮手+`_read_optional_json_object`；sdk 七方法+`_wire_message` 抽取重构+OpenAPI census 12→19 翻名；CORE-CONTRACTS Changelog+§3.1 两行 UTF-8 直读核文；schema_lock 计数钉 46→48）+新测试文件全文亲读（528L 主断言区逐行）+进度档全文亲读+events.json 工件 face 核（两新模型 title/description 在档）+席位日志终报亲读；本席独立复跑四门禁（worktree p6-s1 真 python venv，`uv run --no-sync`+`-m` 形——§4 调度器行待档返回核证定稿）。
- 评审时刻：2026-11-15 11:2x-11:5x（席位弧：发射→SEAT-EXIT 0 @11:19:11 含中断续跑；席位 git 写零——终态 HEAD 恒 `2dce414` 调度器亲证）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 3（全观察面，无返工）

## 六面逐条结论

### 1. 票面纪律 — PASS
- 禁 git 写遵守：席位全程 HEAD 恒 `2dce414`（调度器独立核：终态工作区=15 改未提交+3 未跟踪[新测试/进度档/票面拷贝]，与席位终态恰合）。
- **schema-lock 工作流正面承接**：首全量跑 6F 中唯一非 install 员=TestArtifactDrift events.json（两新 data 模型入注册表后工件未再生——真实票面债）→当场以官方生成器 `python -m tools.r5.generate_schema_artifacts` 再生+`PINNED_MODEL_COUNTS` 46→48 计数钉同步（+667 行纯增）→schema_lock 全量 136 passed——沿 076 jobs 票先例的正确工作流，非绕测。
- envelope/fixture 精选面零触碰判断正确：event-envelopes.json 为逐票精选清单（durable 30/ephemeral 4 钉不变），本票票面未列 envelope/fixture 交付物——与 076 票显式列面不同形，后继票需要再入（披露 7 记档）。
- PS 5.1 `&&` 坑实操一次即遇即改分跑（进度档诚实记档）。

### 2. 决策锚落面（§3 十锚逐条）— PASS
- 锚1 路由命名：七路由全落（`GET /session`/`GET /session/{id}`/`DELETE /session/{id}`/`POST /session/{id}/fork`/`GET /session/{id}/export`/`POST /session/import`/`GET /session/{id}/message/{message_id}`）——无 /api 前缀、单数资源、TS method 一一对应；既有 12 路由零触碰（app.py diff 纯增+import 行改形亲证）。
- 锚2 响应形状：`_session_info_wire` 恰四字面白名单（config_snapshot/snapshot_hash/diagnostic 不出 HTTP 面）；list 元素=get 同形+created_at_ms（§3-7「席位定 created 时间戳面」授权内）；扁平 JSON 无 data 包裹（DIFFERENT-BY-DESIGN 记档）。
- 锚3 fork=事件重放：**席位细化=消息级重放**（不翻面）——`events.id`/`runs.run_id` 为全局主键（0001 schema），原行改 session_id 插回不可行；落形=全新 session_id+`py.session.created`（working_dir/config_snapshot/snapshot_hash 逐字+version 沿源 created 行路由[1/2/3 fail-closed]）+截断点前每条投影消息一枚 `py.message.imported`，`expected_seq=0` 单批原子提交；before 探针先于分页游走（缺失/异会话→404）；游走 unreachable 角 fail-closed StorageCorrupt 兜底。源会话只读不动（fork after fork 稳定）——测试 `after_fork == before_fork` 亲证。
- 锚4 delete=tombstone：`py.session.deleted` 空 data（extra=forbid）+SessionStatus.DELETED 值域增员（0001 status 裸 TEXT 零迁移）+折叠守卫（仅 ACTIVE+无 active_run 始可删，其余=IllegalTransition）+投影=恰一列 status 翻转（rowcount 零=corruption 兜底）+txn 写门禁（deleted→SessionClosedError）+eventlog read/follow 双面 404 隐藏+读面五处一律 NotFound（get/list/messages/events/message）；无级联（DIFFERENT-BY-DESIGN）；活跃 run→409 SESSION_BUSY（`_commit_deleted` 携 `_COMMIT_ATTEMPTS` 重试同 `_commit_revoked` 先例形）；首删 204 空体/重复 404（TS NoContent-then-NotFound 形）。
- 锚5 export/import=往返自洽：`SessionExport` 7 键自描述 payload（`to_payload` HTTP/SDK 共享；messages 逐字 §2.1 wire JSON）；import=严格键集+类型校验+§6.2 canonical snapshot_hash 重验+消息联合逐条解码+payload 内重复 message id 预提交拒绝——fail-closed 全套（不可解码消息/篡改 hash/未知键各态零落库，消息数守恒断言在测试）；重放=created+逐条 imported 事件批（禁直写读模型——投影纪律）；同 id 已存 409；**席位细化=仅全新 id 恢复**（已删 id 墓碑占位仍 409——sessions 行存续之设计面）；恢复后 status 恒 ACTIVE（boot-local 标记非可转移态，披露 7 记档）。
- 锚6 单条 message get：`repos.message_by_id` 单行点查（④-3 探针先例形，messages 唯一键 (session_id,message_id)）；错配/缺失→同 404 无跨会话泄漏。
- 锚7 list 形：`session_summaries`=created 行枚举 LEFT JOIN sessions（join 方向语义记档：极小诊断行合法不现/created 行无 sessions 行=corruption 非静默丢）；`created DESC, session_id DESC` 双键确定性；deleted 过滤；v1 无分页（§5-1 披露）。
- 锚8 SDK 七方法：全同步 JSON（`_call` 既有形）；`_wire_message` 抽取共享（`_message_page` 改委托——既有解码逻辑零漂移重构）；`import_session` 收 `SessionExport|JsonObject` 双形；`delete_session -> None`。
- 锚9 service 公面：**七方法（六+get_session）**——偏差记档合理：get 线形需四字面（status/applied_seq/active_run_id 自 SessionRecord），不落第七面则 HTTP 路由须内联直读 repos（越层）或扩 SessionStatus 公面（更大既有面回改）——最小面=service 一行读法，记档进度档。
- 锚10 非资源面不碰：§7 全守（零回归腿 78 passed 双证+census 12→19 钉含新面不误删旧面）。

### 3. 测试面（§4 清单）— PASS
- 新 `tests/integration/test_httpapi_session_resources.py`（528L 10 测，§4 八组全映射）：
  1. list：空库 `[]`/双 session created 倒序（`[second, first]`）+五字面恰形+applied_seq==1+created_at_ms 整数序。
  2. get：四字面恰形/never-created 404+CoreError 面。
  3. delete 全矩阵：never-created 404/204 空体（`payload is None`）/读四面 404（get/messages/events）/list 排除保他行/重复 404/**sqlite 三探针**（sessions.status=='deleted'+events 尾行==PY_SESSION_DELETED+messages 计 3 物理保留=墓碑非清除直接证）/活跃 run 409（`gate.entered` parked 证明 T-C1-061b 形+busy slot 在 get wire 可见）→gate.open settle interrupted→再删 204。
  4. fork 矩阵：before=<assistant id> 截断恰 `items[:1]`/空 body 全历史（`items` 全等）/源只读（get wire+history 双 `==`）/源 404/before 缺席 404/**异会话 before 404（无跨会话复制）**/422 严格形（未知键+int before）/拒绝后源仍可 fork（零消耗）。
  5. export→import 往返：7 键恰形+messages 逐字全等/活 id 409/fresh-id 恢复（消息数守恒+applied_seq==4+created 戳 list 存活）/再 import 409/**fail-closed 四态各 422+404 零落库**（不可解码消息=del text/篡改 hash/重复 message id/未知键）/删后逐字 import 409（墓碑占位面）。
  6. message get：在册逐条 wire 全等/缺失 404/异会话 404/已删会话 404。
  7. SDK 面：真 server fixture 两测（list/get/message 三面+delete/fork/export/import 全往返含 SessionExport 对象入参形与再 import 409）。
  8. 零回归：test_httpapi_server/test_httpapi_sse/test_sdk_client 全量 78 passed（census 12→19 翻名钉含内）。
- collect 增量实际=10（票面 §4 八组映射，进度档记 10 测清单全列）。
- `_settled_session`/`_message_items` 帮手+`_final_script` 复用先例（test_httpapi_server 形）。

### 4. 门禁与算术 — PASS（席位侧全档；调度器独立面占位待核证）
- 席位自跑四件：ruff 0（初跑 2 处自愈——events.py E501 超长 docstring+eventlog format 单行形，修后清）/ format 564 files exit 0 / mypy `Success: no issues found in 211 source files`（**211=210 基+1 新测试文件预期漂移恰合**）/ 全量终跑 **1F+2242P+8S@612.18s** collected 2251=2241+10 恰合——首跑 6F=5×install 族+1×TestArtifactDrift（真实工件债当场官方生成器清偿见 §1）；终跑唯一失败=install 单员同 4551 直接证据（085 见 1/093 见 5/本票首 5 终 1——**per-spawn 竞态计数漂移本身即相位面证据**）；2242P=2228 基+10 新+4 install 员本跑翻绿，算术恰合；warnings 3-4=aiosqlite teardown 竞态族基线既有。
- install 族=WinError 4551 相位窗（判据链引用面）：092/093 两立「隔离不翻绿」先例+席位本树三腿补强（隔离腿/schema 腿 136P/装载腿 `python -m opencode --version` rc=0 v0.0.1/loader 腿 .venv/Scripts/opencode.exe rc=0）；零非 spawn 失败模式（§6-4 红线未触）。
- 定向回归腿：新 10（套内 9.02s）+三既有文件 78P+schema_lock 136P 全绿。
- 调度器独立四门禁（worktree p6-s1 真 python venv，`uv run --no-sync` 形，11:2x-11:4x 时段）：**ruff 0 / format exit 0 / mypy `Success: no issues found in 211 source files`（211-0=210 基+1 新测试文件预期漂移恰合，与席位侧同数）/ pytest `2243 passed, 8 skipped` 零失败 @613.97s exit 0——collected 2251=2241 基+10 新恰合**——install 族本跑全绿=相位窗闭合负对照（与席位终跑 1F install 单员同树同窗异象=per-spawn 竞态直接证据；091「调度器侧翻转绿」先例形同象；零失败面无需判据链定谳，install 族通过直接证明）。CI 终证=CI-WATCH-239（云端 VM install 族历史全绿；#236 派发检查点 `2dce414` CI-WATCH-238 已绿=同基线树 CI 终证先行面）。
- mypy func-returns-value 裁决记档：SDK 204 None 运行时钉触 [func-returns-value]（`-> None` 平方法任何取值用形均拒）→落语句调用形，None 契约由签名静态钉+HTTP 空体 204 钉双承载——零 type:ignore 引入，裁决合理。

### 5. 语义面独立复核 — PASS
- 墓碑设计零发明独立核：`py.session.deleted` 非 run-scoped 合法性=资源面事件（jobs 先例——metadata={} 合法）；空 data+extra=forbid=wire 最小面；折叠守卫（ACTIVE+无 run）与 service 预检（§7 门序：存在→状态类→active-run 409）双层——手工构造/重放日志无法删除带 run 会话（fold re-state）；投影恰一列翻转=状态语义归属 state fold 不内联字面量（transition-rule ownership 纪律）；tombstone 非物理删除（M1 archive 设计面对齐 compaction 先例——data-preserving hide）。
- import 重放纪律独立核：禁直写读模型全守（created+imported 事件批→projector 投影）；`expected_seq=0` CAS 双栅（check-to-commit 竞态+log-without-row 面）；无重试循环理由正确（此处冲突=「id 被占」是答案非可重试条件——与 `_commit_deleted` 重试形语义分立正确）。
- `_created_draft` 共享重构零漂移：create/fork/import 三路共用版本路由（v3 先查=独立模型非 v2 子类；hash 同模型计算=§6.2 重验不可能不一致）——既有 create 面行为逐行比对零语义变化。
- MessageImportedData=§2.1 联合原样承载（message id 源会话逐字；新鲜性=fold 职责）——事件即重放契约，import fail-closed 根面。
- `_message_page` 委托重构零漂移（`_wire_message` 抽取形语义等价亲读）。
- 既有 12 路由/messages SSE/jobs 族零回改（app.py diff 纯增+OpenAPI census 17 path keys 集合比对旧 12 全在）。

### 6. 披露与记档 — PASS
- 票面 §5 六条全数落位进度档「披露余量」节；席位另记 7 条落形自带余量（import 恒 ACTIVE/墓碑占位 id 不可复用/读面 404 vs 写面 410 分立/消息 id 跨会话重复与 ④-3 探针良性面/envelope-fixture 精选面未扩员/…）——诚实披露纪律满分。
- §3 两细化+§3-9 七方法偏差+schema-lock 工作流+mypy 裁决四决策全记档。

## 发现明细

- **建议 1（观察面，无动作）**：`session_summaries` 全表 created 行枚举+内存 JOIN 后处理——单用户本地库 v1 规模无瓶颈；后继会话量级增长可评估 SQL 侧分页（LIMIT/OFFSET 或 keyset）承接收 cursor 形。
- **建议 2（观察面，无动作）**：import 恒 ACTIVE 面=导出时点 status 不恢复——若后继需要「导出物审计」（保留 deleted 态证据），可在 payload 增 `exported_status` 旁证字段（非本票面）。
- **建议 3（观察面，无动作）**：`message_by_id` 与 `session_id_by_message_id`（④-3 既有探针）在 fork/import 副本下同 id 多会话——前者 (session_id,message_id) 键查无歧义，后者仅馈 `is not None` 分类器良性（进度档已记）；后继若该探针改用途需先收窄。

## 附录：席位弧时序

- 发射（四探针全绿后：ruff 0/format 0/mypy 210-0/pytest 5F install 族+2228P@574.73s=4551 相位窗判据链 092/093 先例+CI-WATCH-238 绿）→SEAT-EXIT 0 @11:19:11（含中断续跑，进度档记档）；零 git 写；看哨 TRUE-EXIT 自终。
- 首跑 6F→工件债清偿→终跑 1F install 单员：schema-lock 工作流正面闭环。
