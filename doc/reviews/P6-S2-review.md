# P6-S2-review — T-C1-095 微评审：全局事件流 `GET /events`（易失契约跨会话广播 SSE+SDK 流面）

- 评审对象：worktree `p6-s2` 席位工作区（未提交态，席位 git 写零）：改 5 跟踪文件 +504/−70[bus/bus.py +108/−32、httpapi/app.py +179/−31、sdk/client.py +80/−3、sdk/sse.py +130/−2、tests/integration/test_sdk_client.py +7/−2] + 新 1[tests/integration/test_httpapi_global_events.py 571L 11 测]——与票面 §6 交付面吻合（`sdk/sse.py` 为第五面=票 §3-9「SSE 客户端面复用 sdk/sse.py」授权内：解析器必须落此文件，记档）；票面拷贝正确保持 untracked（#180 B-②）；进度档 `doc/orchestration/progress/T-C1-095.md`。
- 评审基据（全部独立现读，禁继承）：5 文件 `git diff` 全量逐行亲读（bus.py 全局面+共享 `_normalized_types` 抽取；app.py 路由+帧形+`_sse_frames` 泛型化+守卫覆盖；sdk 两面）+新测试文件结构全图+关键测试体逐行亲读（易失契约+慢消费两判据锚）+进度档全文亲读+席位日志终报亲读；本席独立复跑四门禁（worktree p6-s2 真 python venv——§4 调度器行待档返回核证定稿）。
- 评审时刻：2026-11-15 13:0x-13:2x（席位弧：发射→SEAT-EXIT 0 @12:57:10 单腿无 blocker；席位 git 写零——终态 HEAD 恒 `0706f0d` 调度器亲证）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 3（全观察面，无返工）

## 六面逐条结论

### 1. 票面纪律 — PASS
- 禁 git 写遵守：席位全程 HEAD 恒 `0706f0d`（调度器独立核：终态工作区=5 改未提交+3 未跟踪恰合席位终态）。
- 席位会话期 `$null` 空文件（0B 重定向事故工件）filesystem 级移除——前后 porcelain 只读对证恰该一行差，诚实披露纪律满分。
- 首跑 1 红=harness 滤波未排除终态 error 帧（测试代码自纠，零产品码改动）——诚实记档。

### 2. 决策锚落面（§3 十锚逐条）— PASS
- 锚1 路由：`GET /events`（复数资源形；TS `/api/event` 不移植 DIFFERENT-BY-DESIGN 记档）；`?types=` 逗号分隔解析——空值/空段=422 严格形（fail-closed：静默空滤会无告知空转）。
- 锚2 底座：`VolatileBus.subscribe_all(types=None)`——`_Subscription` 以 `session_id=None` 表全局作用域；`notify()` 扇出=会话订阅者先、全局订阅者后（逐事件循环序保 publish 序）；`_register`/`_unregister` 按 None 分路（identity remove）；`subscription_count` 双面合计；types 过滤沿 `_accepts` 既有形（`_normalized_types` 共享抽取零漂移）；有界队列 DEFAULT_QUEUE_LIMIT 1000 沿用（TS 4096 不逐字移植记档）。
- 锚3 易失契约：无重放无补读无续读——**Last-Event-ID 头出现=422 显式拒收**（席位裁决记档：不静默忽略——忽略埋「看似续读」坑，拒收把易失契约钉在 422 面；票面 §3-3「忽略或 422 席位定」授权内）；会话级 durable SSE 零触碰（EventLog/续读面 numstat 无名）。
- 锚4 初始帧：`server.connected` 流级合成帧（`_connected_frame`——`event:` 行+`data: {"type":"server.connected","data":{}}`，**无 id 行**=不可推进 Last-Event-ID；非 registry 非 bus 事件，TS 对齐）；`SERVER_CONNECTED` 常量 app.py/sdk/sse.py 双面钉等（专测 `test_the_connected_marker_literals_are_pinned_across_the_two_faces`）。
- 锚5 帧形：`_global_frame_for`——durable 帧与会话 SSE `_frame_for` **逐字段同形**（`id: {session_id}:{seq}`+8 字段 envelope=最大复用）；ephemeral 帧无 id 行（无 seq 无游标——易失契约帧面承载）。
- 锚6 心跳：沿 `_sse_frames` keepalive 既有纪律（SSE_KEEPALIVE_SECONDS 零新增）；`_sse_frames[ItemT]` 泛型化（`frame_for`/`preface` kwargs）——既有两调用点显式传 `frame_for=_frame_for` 零语义漂移（泛型化=删除面的全部来源，亲证零漂移）。
- 锚7 响应头：`text/event-stream`+`Cache-Control: no-store`（PY 既有会话 SSE 形沿齐；TS no-cache/X-Accel-Buffering/nosniff 不移植记档）。
- 锚8 并发预算：`_is_sse_request` 结构判定 `path == "/events"`（零路径列表回改——锚 8 预裁兑现；专测探针钉面：`test_is_sse_request_routes_the_global_stream_onto_the_stream_budget`）。
- 锚9 SDK：`stream_global_events(*, types, timeout_s)` 惰性生成器（首次 next 连接+`ServerConnected` 先发+finally close）；`_open_global_stream` types 客户端侧校验镜像 bus 守卫+urlencode；`sdk/sse.py` 新解析器（durable/ephemeral 按 `seq` 键结构分派+registry 解码 fail-closed+error 帧终态异常）。
- 锚10 非目标：contracts/state/projector/repos/eventlog/jobs SSE/auth+Origin 守卫全零触碰（numstat 无名亲证）；既有 19 路由语义零回改（census 19→20=加法集合钉非语义钉）。

### 3. 测试面（§4 清单）— PASS
- 新 `tests/integration/test_httpapi_global_events.py`（571L 11 测，§4 八项全覆盖+三额外）：
  1. server.connected 首帧恰一先于任何 live 帧。
  2. 跨会话广播：两 session 各 settled run→单流收两 session 事件（session_id 字面集合证）。
  3. types 过滤：`?types=py.prompt.accepted` 只收该类型/省略全收/空段 422。
  4. **易失契约负证**：断线期 session B offline 事件 missed+重连首帧=marker 非历史+新 live 事件（session_c）锚定「若有重放必先于该帧」的反证面+A/B 历史帧零出现+`subscription_count` 1→0→0 断连释放无泄漏三查。
  5. **慢消费溢出**：bus `queue_limit=2` 注入（构造器既有旋钮——HTTP 面故意无旋钮）+paced reader→服务端已断证（`subscription_count==0`）+终态错误帧全 details 钉（`SLOW_CONSUMER`/`scope:"global"`/`session_id`/`queue_limit:2`/`seq:3`=首个不可投递 run.started）+已收前缀 `[created, prompt_accepted]` 不默弃+error 帧恰为线上最后一帧。
  6. 心跳：空转窗 keepalive 注释帧（沿 test_httpapi_sse 先例）。
  7. 守卫面：未认证 401/Origin 拒收（守卫自动覆盖新路由）+`_is_sse_request` 预算探针。
  8. SDK：真 uvicorn server connected 首帧+≥1 live 帧；解析器单元探针（durable/ephemeral 双 envelope+畸形 fail-closed）；两面字面钉。
  - collect 增量=11；零回归腿 88P（四既有文件）+test_bus.py 24P（bus 合同面扇出扩展后旧面零漂移）双证。
- 首跑 1 红（harness 滤波）自纠复跑 11 绿——诚实记档。

### 4. 门禁与算术 — PASS（席位侧全档；调度器独立面占位待核证）
- 席位自跑四件：ruff 0 / format 567 files exit 0（基线 566+1 新测试文件预期漂移）/ mypy 212-0（211+1 预期漂移恰合）/ 全量 **5F+2249P+8S@602.31s** collected 2262=2251+11 恰合——5F 全为 install 族五员同员集，逐员 4551 直接证据；2249P=2243 基+11 新-5 install 员本跑红（2243 含 install 五员绿位）算术恰合。
- install 族=4551 相位窗（092/093/094 判据链引用形+本树三腿补强：隔离腿同五员集+装载腿 `python -m opencode --version` rc=0+loader 腿 venv shim rc=0 红绿时变对照）；零非 spawn 失败模式。
- 定向回归腿：新 11（7.51s）+四既有文件 88P（census 20 钉含内）+test_bus 24P 全绿。
- 调度器独立四门禁（worktree p6-s2 真 python venv，`uv run --no-sync` 形，13:0x-13:2x 时段）：**ruff 0 / format exit 0 / mypy `Success: no issues found in 212 source files`（212-0=211 基+1 新测试文件预期漂移恰合，与席位侧同数）/ pytest `5 failed, 2249 passed, 8 skipped` @651.73s——5F 全为 install 族同五员集，逐员 `OSError [WinError 4551]` 直接证据在档；collected 2262=2251 基+11 新恰合**——与席位终跑同树同窗同面逐字段复现（同五员集/同 2249P/同 collected）=相位窗张开形（092/093 判据链引用形；席位三腿补强[隔离腿 5F/3P 同五员集+装载腿 rc=0+loader 腿 rc=0 红绿时变对照]在案；零非 spawn 失败模式——per-spawn 竞态相位面直接证据，非回归）。CI 终证=CI-WATCH-241（云端 VM install 族历史全绿；#238 派发检查点 `0706f0d` CI-WATCH-240 已绿=同基线树 CI 终证先行面）。

### 5. 语义面独立复核 — PASS
- 扇出序独立核：会话订阅者先、全局订阅者后（逐事件双循环）——全局流观察序=publish 序（同一 notify 调用内）；跨 notify 调用序=调用序（单事件环保证）。
- `_sse_frames` 泛型化零漂移：既有会话/jobs 两调用点 `frame_for=_frame_for` 显式传参+`preface` 缺省空元组——旧路径行为逐行等价（亲读 diff 全貌）；SlowConsumerError 捕获新增=全局流专属路径（旧路径 bus follow 不产此错——事件源是 EventLog 非 bus）。
- Last-Event-ID 422 拒收语义独立核：与 `_last_event_id_cursor` 会话面形成**显式分立**（会话流受理/全局流拒收）——两设计面在 HTTP 层各有明文契约，无静默歧义。
- durable/ephemeral 结构分派（`"seq" in payload`）独立核：durable envelope 恒带 seq/wire 契约面；ephemeral 恒不带——registry 解码双侧 fail-closed，无白名单发明。
- `server.connected` 无 id 行=流级标记不可推进游标（拒收面之外的第二道防「看似续读」防线）——TS 对齐 `{type, data:{}}` 载荷形。
- bus 全局面零发明：`subscribe_all` 参数形镜像 `subscribe`（共享 `_normalized_types`）；`SlowConsumerError` details 全局角 `scope:"global"`+session_id 取事件自身（无 owning session）——§8 sanitized 形纪律延续。
- 既有 19 路由零语义漂移（census 20=纯加法+`/events` 18th path key 专钉）。

### 6. 披露与记档 — PASS
- 票面 §5 六条全数落位进度档；席位另记落案自带余量 4 条（connected 无 id 行/SDK 无去重无游标回调/sse.py 第五面授权内/census 19→20 预期最小面）。
- 首跑修正+`$null` 工件清理两诚实注记全档。

## 发现明细

- **建议 1（观察面，无动作）**：全局流慢消费 overflow 后服务端即断（`subscription_count==0`）而客户端待 release 才见终态帧——若客户端长 park，TCP 缓冲+4MB 帧帽内前缀仍可达；极端场景（前缀超缓冲）连接复位=客户端见网络错而非 error 帧——易失契约容忍面，无需动作。
- **建议 2（观察面，无动作）**：`_normalized_types` 空迭代=匹配一切皆无（空 frozenset `_accepts` 恒 False）——语义「显式空集=收不到任何事件」记档即可；HTTP 面 `?types=` 空值已被 422 拦截不达此角。
- **建议 3（观察面，无动作）**：TS `types` query 描述「plus the initial server.connected event」——connected 帧恒发不受过滤（PY 落形同构：`_connected_frame` 在 preface 无条件先发）——行为一致，零动作。

## 附录：席位弧时序

- 发射（四探针全绿：ruff 0/format 0/mypy 211-0/pytest 2243P+8S exit 0 窗口闭合形+CI-WATCH-240 绿）→SEAT-EXIT 0 @12:57:10；进度档先于退出写毕；零 git 写；看哨 TRUE-EXIT 自终。
- 单腿无 blocker：bus 全局面+路由+帧形泛型化+SDK 双面+11 测一次成形（首跑 1 红=harness 滤波自纠）。
