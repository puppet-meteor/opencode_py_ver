# P6-S3-review — T-C1-096 微评审：Compaction 接线（手动 compact HTTP 面+SDK+§2.4/§3.1 状态标注翻新+首批 HTTP 集成测试）

- 评审对象：worktree `p6-s3` 席位工作区（未提交态，席位 git 写零）：改 5 跟踪文件 +238/−8[session/service.py +61/−1、httpapi/app.py +73/−0、sdk/client.py +89/−1、tests/integration/test_sdk_client.py +8/−2、doc/CORE-CONTRACTS.md +7/−5] + 新 1[tests/integration/test_httpapi_compaction.py 488L 7 测] + 进度档——与票面 §6 交付面逐文件吻合；**contracts/projector/state/repos/schema-lock 全零触碰=零新事件类型纪律兑现**（numstat 无名亲证）；票面拷贝正确保持 untracked（#180 B-②）；进度档 `doc/orchestration/progress/T-C1-096.md`。
- 评审基据（全部独立现读，禁继承）：5 文件 `git diff` 全量逐行亲读（service 公面守卫序+锁释放语义+错误策略分立；app wire per-status 恰形+空体 422；SDK CompactionReceipt 恰形解码+census 21；CORE-CONTRACTS 六处翻新语义句零改写比对）+新测试文件结构全图+关键测试体逐行（compacted 全链算术钉到 seq 44/守卫序双钉/自动触发 HTTP 端到端/SDK 双面）+进度档全文亲读+席位日志终报亲读+**两事实声明独立核证**（T-C1-063=`session/llm_summarizer.py` `ProviderSummarizer` L179 真在库+progress 档在案；`test_app_compaction.py` 339L/`test_session_compaction.py` 642L 两集成文件真在库——席位档面引块列举属实）；本席独立复跑四门禁（§4 调度器行待档返回核证定稿）。
- 评审时刻：2026-11-15 14:3x-15:0x（席位弧：发射 #240→SEAT-EXIT 0 @14:26:48 单腿无 blocker；席位 git 写零——终态 HEAD 恒 `0edc663` 调度器亲证）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 3（全观察面，无返工）

## 六面逐条结论

### 1. 票面纪律 — PASS
- 禁 git 写遵守：席位全程 HEAD 恒 `0edc663`（调度器独立核：终态工作区=5 改未提交+3 未跟踪恰合席位终态）。
- 门禁期收尾修全在测试/格式面零产品码返工（ruff --fix 未用 import+三处 docstring 重排+mypy 嵌套形修正+①算术三元组整除修正）——诚实记档。
- **调度器钉面误差披露（评审侧诚实记档）**：票面 §2「集成级零覆盖」+§4-6「自动触发端到端亦无集成测」系调度器 grep 截断误差——service 级自动触发集成矩阵（`test_session_compaction.py` 642L 九测：post_settlement/boundary_cross/dormant/cancelled/crashed/failure/CAS_discard/crash_faces/NotFound）T-C1-062 已完备在库。席位沿票面表述非自创误差；档面 CORE-CONTRACTS §2.4 引块「集成级覆盖（test_app_compaction/test_httpapi_compaction/test_session_compaction）」列举**以事实修正正确**；测试⑥的真增量=**HTTP 面端到端首补**（真 `_run_app`+HTTP prompt+sqlite 探针——与 service 层直调 fake runner 分层不同）——增量合法，实害零。

### 2. 决策锚落面（§3 十锚逐条）— PASS
- 锚1 路由：`POST /session/{session_id}/compact`（无 /api 前缀单数资源）；空体/缺省体接受（`_read_optional_json_object`——S1 新增帮手复用）；非空对象 422（§1 unknown-fields 规则+载荷面明文「empty body only」）；TS `{id, delivery?}` inbox-steering 不移植 DIFFERENT-BY-DESIGN 记档。
- 锚2 service 公面：`compact_session`——守卫序全程在会话锁内（`_require_session_id_arg`→closed/closing 双查→`_read_live_record`[缺席/墓碑 404]→状态类三分→active_run 409），**锁释放后**引擎趟（引擎自有三相位锁序拥有释放-至-提交竞态；摘要不持锁=§2.4 L126）；`_compaction()`=None→`StoreValidationError`（`_compaction_event_log` 同形惰性接缝先例映射→HTTP 422 VALIDATION_ERROR）。
- 锚3 outcome wire：per-status 恰形键集——`compacted` 七键（`{status,covered_seq,retained_from,message_count,summary_hash,event_id,usage}`；**summary_text 留 durable-only**=席位白名单裁量[票 §5-6 授权]，hash+event_id 承载审计）；`below_threshold` 单键；`discarded` 两键（`{status,usage}`——已花 usage 诚实上报）；usage/capture/event 非空断言=引擎不变量钉。
- 锚4 守卫序：S1 delete 先例形全落；测试⑤守卫序双钉（never-created 404 **先于** 422 引擎门；R1 accepted prompt 活 run 409 **先于** 422——存在性与 busy 均先于可用性，fail-closed 层序亲证）。
- 锚5 notify 纪律：`outcome.event is not None`→`_bus.notify`（`_post_run_compaction` 同形）；below_threshold/discarded 不 notify。
- 锚6 错误策略分立：手动不吞+不入 `_compaction_errors`（该台账属自动触发后台尽力策略）——docstring 全文披露语义分立理由；调用方必须看见结果。
- 锚7 SDK：`compact_session` `_call` 同步 JSON 形+`CompactionReceipt` per-status 恰形解码（`_expect_object` frozenset 检查——compacted 七键/below_threshold 单键/discarded 两键/未知 status 拒收；usage 经 §2.1 `Usage.model_validate` fail-closed）；census 20→21（测试名 twenty→twenty_one+逐路 spot-check+`len==21` 钉）。
- 锚8 自动触发零回改：`_drive_run`/`_post_run_compaction` 逐字未动（service.py 61 新增行=1 import+60 方法块，0 删除——numstat −1 为 import 行改形）；验证性集成测⑥落地=票可选腿兑现。
- 锚9 档翻新：六处全落（§2.4 标题/状态引块翻牌+实现事实清单/阈值行 40+8 现值句/fixture 在库事实句/§3.1 行/Changelog 行）；**契约语义句零改写**（逐字比对：执行语义/红线承接/最低验收原文保持——纯状态词+票据引用+在库事实翻新，§6.3 翻牌先例形）。
- 锚10 非目标：既有 20 路由语义零回改（census 21=加法集合钉）；contracts/projector/state/repos/schema-lock/bus/eventlog/jobs/auth 守卫全零触碰。

### 3. 测试面（§4 清单）— PASS
- 新 `tests/integration/test_httpapi_compaction.py`（488L 7 测，§4 八项全覆盖）：
  1. compacted 全链：42 settled（14 三元组 import——imported 消息落自身 seq 无 run→自动触发不燃的巧妙 fixture）→200 恰形七键+**seq 算术钉到 44**（created+42 imports+本事件）+durable 事件 sqlite 探针（七字段全等含 summary_text durable-only）+**真实 `ProviderSummarizer` 链**（scripted provider 流经真适配器——请求恰一 user-role no-tools+transcript=covered 区间[question 0-11 含/answer 11 不含/question 12 不含]边界钉）+投影推进（applied_seq=44+messages 42 行物理保留）+**二次手动 compact→below_threshold**（summary None-linked 不计+尾 8<40=幂等面证）。
  2. below_threshold：单键恰形+零事件+非空 body 422。
  3. 404 双面：never-created+S1 墓碑。
  4. 409 busy：gated parked run（S1 同形探针）→settle 后再 compact=200（busy 解除面）。
  5. 休眠 422+守卫序双钉（§2 锚4 述）。
  6. 自动触发 HTTP 端到端：14 趟 settled run（14×3=42≥40>13×3=39 恰跨阈）→无手动调用 durable row 已 committed（sqlite 轮询探针）+provider.requests==15（14 run+恰一摘要）+手动面 below_threshold 诚实应答。
  7. SDK：真 uvicorn server fixture 往返 compacted+below_threshold 双面（CompactionReceipt 七字段全出/惰性字段全 None）。
  8. 零回归：邻接九文件 151P（含 census 21+既有 compaction 全家族[engine/projection/app/session 四文件]）。
- collect 增量=7；fixture 策略不 mock 摘要器（scripted provider 流经真实适配器链——忠实纪律）。

### 4. 门禁与算术 — PASS（席位侧全档；调度器独立面占位待核证）
- 席位自跑四件：ruff 0 / format 570 files exit 0（568 预期+2 席位间合入库漂移，门绿为准+SEAT-EXIT 复核同绿）/ mypy 213-0（票面基线 212+1 新测试文件=213 预期恰合）/ 全量 **5F+2256P+8S@623.86s** collected 2269=2262+7 恰合——5F 全 install 族五员同员集逐员 4551 直接证据；2256P=2249[095 面]+7 新算术恰合。
- install 族=4551 相位窗三腿取证：隔离腿 5F/3P（同五员集不翻绿+逐员一行直证+三非 spawn 测绿）；装载腿 `python -m opencode --version` rc=0（本票 app/service/sdk 改动后可装载可运行直接证明）+`--bogus-flag` rc=2 反证；**shim 腿本窗翻红**（`.venv/Scripts/opencode.exe` 被「应用程序控制策略已阻止」——与 pytest 内 4551 逐字同族同文=环境级政策直接复现；该 shim 系 venv 装配期产物本票零触碰，095 收口同腿绿→本窗红=**政策态时变窗口证明**，席位记档为强化非阻断——裁决合理：同一未触碰二进制红绿翻转本身即相位面证据）。
- 定向回归腿：新 7（8.82s）+邻接九文件 151P 全绿。
- 调度器独立四门禁（worktree p6-s3 真 python venv，`uv run --no-sync` 形，14:3x-15:0x 时段）：**ruff 0 / format exit 0 / mypy `Success: no issues found in 213 source files`（213-0=212 基+1 新测试文件预期漂移恰合，与席位侧同数）/ pytest `2261 passed, 8 skipped` 零失败 @622.41s exit 0——collected 2269=2262 基+7 新恰合**——install 族本跑全绿=相位窗闭合负对照（与席位终跑 5F 同树同窗异象=per-spawn 竞态相位面直接证据；091/092/093/095 判据链引用形；零失败面免判据链定谳，install 族通过直接证明）。CI 终证=CI-WATCH-243（#240 派发检查点 `0edc663` CI-WATCH-242 已绿=同基线树 CI 终证先行面）。

### 5. 语义面独立复核 — PASS
- 锁语义独立核：守卫序锁内读+锁释放后引擎趟——与引擎三相位（捕获锁→释放摘要→短提交锁 CAS）无缝衔接，释放-至-提交竞态由 expected_seq/CAS 拥有（§3.2 基础不变量保持）；摘要调用永不持锁（§2.4 L126 原文兑现）。
- 错误分立独立核：`_compaction_errors` 台账语义（自动触发后台尽力）与手动面调用方可见分立——两策略各有明文 docstring；无交叉污染（手动失败不入台账=台账保留「后台触发史」语义纯度）。
- per-status 恰形键集独立核：服务端 wire 与 SDK 解码器两侧 frozenset 恰形互为镜像（compacted 7/below 1/discarded 2）；无共享形=状态面歧义零空间；`_expect_object` fail-closed（未知键拒收）与 §1 未知字段规则同构。
- summary_text durable-only 裁决独立核：事件体自带正文（durable 读面/events SSE 可达）；wire 以 hash+event_id 承载审计——防大摘要重复出 HTTP 面的合理白名单（票 §5-6 授权内）。
- 档翻新零语义漂移独立核：执行语义段/红线承接段/最低验收句/场景注记逐字未动（diff 仅状态词+票据引用+在库事实句——六处全为标注面）；「已实现态」引块实现事实清单四件（引擎/接线/自动触发/手动面）全数与库内事实对应（本评审独立核证 llm_summarizer/compaction.py/service.py/app.py 四面）。
- census 21=纯加法（`/session/{session_id}/compact` 19th path key）+逐路 spot-check 保旧面。

### 6. 披露与记档 — PASS
- 票面 §5 七项全数落位进度档+席位余量 3 条（CompactionReceipt 不经 `sdk/__init__` 再输出=票 §6 numstat 钉面保持；raw-ASGI 缺省体 `body=b""` 镜像 SDK 真实 `body={}` 面；二次 compact below_threshold=引擎幂等非新守卫面）。
- 门禁期收尾修+shim 腿翻红时变记档+format +2 漂移归因全档。

## 发现明细

- **建议 1（观察面，无动作）**：调度器钉面 §2/§4-6「集成级零覆盖」表述不精确（grep 截断漏两集成文件）——实况=service 级矩阵完备[T-C1-062 九测]+HTTP 面零覆盖（本票⑥首补）。席位沿票面表述+档面引块以事实修正；票面生成侧教训：`git grep | Select-Object -First N` 截断须配 `-l` 文件清单交叉核（调度器流程改进项，非席位缺陷）。
- **建议 2（观察面，无动作）**：shim 腿红绿时变（095 绿→096 红）已记强化证据——后续席位若 shim 腿持续红且装载腿同红，判据链须回「隔离腿+4551 逐员直证」两腿最低形（本窗装载腿绿=装载健康独立证在）。
- **建议 3（观察面，无动作）**：`compacted` outcome 的 `usage` 出 HTTP 面（票面留给席位裁量已行使）——`discarded` 的 usage 同出（一致性合理）；后继若 wire 需瘦身可议 `?include=usage` 形（非本票面）。

## 附录：席位弧时序

- 发射（四探针全绿：ruff 0/format 0/mypy 212-0/pytest 2254P+8S exit 0 窗口闭合形+CI-WATCH-242 绿）→SEAT-EXIT 0 @14:26:48；进度档先于退出写毕；零 git 写；看哨 TRUE-EXIT 自终。
- 单腿无 blocker：service 公面+路由+wire+SDK+档六处+7 测一次成形（门禁期收尾修全在测试/格式面）。
