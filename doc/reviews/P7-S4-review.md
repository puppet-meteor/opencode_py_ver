# P7-S4-review — T-C1-103 微评审：websearch 工具新增——用户所选搜索集成（#5b 复评票，P7 主池 S4；提案 §3-C S–M 定级）

- 评审对象：worktree `p7-s4` 席位工作区（未提交态，席位 git 写零）：6 跟踪文件改动（cli +13/serving +24/−1/builtin __init__ +26/−1/policy +13/−1/test_httpapi_server +9/−7/test_workflow_tools +14/−10）+新 `src/opencode/websearch.py` 508L+新 `src/opencode/tools/builtin/websearch.py` 470L+新 `tests/integration/test_tool_websearch.py` 964L **29 测**+进度档+票面拷贝 untracked——全新增面（PY 零现成）+注册/权限/census 翻新，零邻接回归，恰合 S–M 票预判。
- 评审基据（全部独立现读，禁继承）：service 全文两段亲读（L1-270 模块 docstring=TS 映射/偏差全记录+四 tagged 错+schema 三 dataclass+`http_status`/`retry_after` 结构识别+`cooldown_millis` verbatim+`_JsonFileKV` 原子写；L270-508 registry/`_selection` 越域摘除/`_default_provider` 检视面/query 环 shape-for-shape）；adapter 全文两段亲读（L1-240 常量 verbatim+Spec+consent 协议形+两 form 文案；L240-470 markdown 渲染 truthiness+`_request_failure` mapError 表+adapter 执行序）；六 diff 全图；29 测签名结构；进度档 6 节全文；**TS 四面独立现读**（tool/plugin/websearch.ts 196L+core/src/websearch.ts 230L+schema/src/websearch.ts 44L+config/plugin/websearch.ts 19L——调度器派发前钉面+本评审时点复读双证）；**question.py L1-130 亲读**（consent 载体裁定关键证）；哈希亲证（四面 sha256 采集+追加 source-baseline.json 66 条）。
- 评审时刻：2026-11-16 06:55-08:0x（席位弧单段：06:55 发射→07:43 **SEAT-EXIT 0** ~48min；中含 compaction checkpoint 存活续跑——非 S3 attempt1 死亡形；席位 git 写零——HEAD 恒 `2eac833` 验收时点亲证）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 4（全观察面，无返工）

## 六面逐条结论

### 1. 票面纪律 — PASS

- 禁 git 写遵守：HEAD 恒 `2eac833`（验收时点独立核）；工作区=6 改+三新文件+两档件恰合席位终态。
- 单段闭合：全量 pytest 前台收齐（676.60s）+进度档全档后 SEAT-EXIT 0。**席位弧第三态记档**：中途遇对话压缩点（compaction checkpoint）**存活自续**（进度档 §0 续跑注记+git status/numstat 亲证恢复序）——与 S3 attempt1 compaction failure 死亡形对照：同会话压缩存活=零调度器介入（先例谱三态：存活自续/死亡后同会话续跑/死亡后新会话锚定）。
- **机制矫正一例全档（先例第六例）**：票 §3-3 预裁「consent 走 question 工具面」与库内机制冲突——`question__ask`=非阻塞面（question.py docstring 直证「RETURNS IMMEDIATELY...the answer channel is the conversation itself」；答案=下一用户消息，**无法解锁在飞工具调用**）+approval channel=once/always/reject 封闭枚举（`ApprovalCoordinator.resolve_reply`）无法承载 allow/choose/disable+provider 选择——三腿取证后矫正为**宿主可注入 `ConsentChannel` Protocol**（None→ProviderRequired fail-closed，测试直证）+consent 语义 verbatim 全保（三选项文案/60s deadline/disable→select(False)/cancelled 身份）；与 question.py 自身 T-C1-097 DIFFERENT-BY-DESIGN 分析同构（双票互证）。语义面零翻、仅载体换形记档——机制矫正条款（锚与库内机制冲突时三腿根因取证后矫正）恰用例。
- §5 十项披露全落位+§6 调度器须知三件（指纹补录[已执行 66 条]/真实 provider 集成+consent 宿主 wiring 后续票/KV 通用子系统迁移后续票）。

### 2. 决策锚落面（§3 九锚）— PASS

- 锚1 工具面：wire name `websearch__search`（webfetch 先例双段形）；description verbatim 两段+动态年份句 import 时取年（TS module-scope 恰合）；Input `{query}`（schema Input 的 optional providerID 留内部面——仅 consent 流可设，记档）；`NO_RESULTS` verbatim；`idempotency_support=False`（webfetch True 之诚实反置：时变结果面）+`side_effects=True`（出站网络效应）。
- 锚2 provider seam：registry（set-by-id 重注册覆盖=Map.set 语义）+KV 持久 selection（`PROVIDER_KEY="websearch:provider"` verbatim；ID|`"random"`|`False` 三值域+越域存储摘除=TS decode-None hygiene 臂）+session 亲和表+random 轮换+429 cooldown（attempted 环+环尽回退末 cooldown 错+显式 provider_id 环盲，三语义测试钉）。**KV 落地=`_JsonFileKV` JSON 文件面**（`kv.json` 落数据目录 `app.database.path.parent`；同目录 temp+`os.replace` 原子写；损坏字节/非 dict 读空=corrupt-face；OSError 上抛→适配器 fallback 包裹）DIFFERENT-BY-DESIGN 记档（sqlite 新表=破 migration head 0004 钉+票 §3-8 零 schema 契约；内存面=破 §8 重启稳定）。**零内置 provider** DIFFERENT-BY-DESIGN（SEAM：Protocol+`register_provider`+`providers()` 名序——TS localeCompare vs PY code point，ASCII 恒同记档）。
- 锚3 consent 流：机制矫正（§1 详）——TS `Form.ask` 两表 verbatim（「Web Search」/「Allow OpenCode to search the web for up-to-date information?」/「Allow search via {names}」/「Choose another provider」/「Disable web search」/「Choose a web search provider」/「Choose a provider for web search.」）+实例级 `asyncio.Lock`（TS `Semaphore(1)` providerSelectionLock）+`asyncio.wait_for` 缺省 60s 可注入（TS `timeoutOrElse("1 minute")`）+超时/取消答复→`Web search cancelled` 身份+复查序保持（resolved default 直返[并发 consent 复用]/空 registry ProviderRequired 先于 form/channel None fail-closed）。
- 锚4 错误面：mapError 表 verbatim（429/401 两句+他 status 泛句 `Web search request failed (HTTP {status})`[map 缺省臂]+statusless fallback `Unable to search the web for {query}`+RequestError 各臂 `metadata={"provider": id}`）+非 Request 全族→fallback（无 metadata）+广谱 `except Exception`→fallback fail-closed；四类 tagged PY 化（**ProviderNotFound 独立 tag 非 ProviderRequired 子类**=TS union 分立形——席位自纠①）。
- 锚5 content 面：markdown `## [title](url)`（title??url）+`Published:` 行（truthiness：0/None→无行；`toISOString` 面=UTC 毫秒 Z 后缀）+content 块（truthiness：""/None→无块）+`\n\n` join+零结果 NO_RESULTS——逐面 TS 对位亲证（structured_output `time.published` 嵌套形=schema Result wire 形恰合；None=TS absent key 序列化省略）。
- 锚6 hook 摘除面：三 session hook+config 观察者+`websearch.updated` 事件=N/A/零记档（无对应机制不新建）——config/plugin/websearch.ts 19L 亲读对证（ConfigEntryObserver 影子层 PY 无对应=selection 只读 KV；Session Moved 事件 PY 无=forget_session 面留宿主接线）。
- 锚7 policy：`websearch__search="ask"` 行级默认（webfetch/subagent 先例形）+`extract_resources` query 第四绑定面（path/command/url 之后，既有三面零变 diff 直证）+`_ASK_NAMESPACES` 不触（git-trio 先例）+docstring 引 TS `permission.assert` `resources=[input.query]`。
- 锚8 契约面：路由/SDK census 恒零变兑现（工具非端点）；事件/schema/迁移零触（websearch.updated=零；kv.json=数据文件非契约面）。
- 锚9 非目标全兑现：webfetch 零回改/管理端点（S5 池票）零触碰/pyproject 零改/外网零依赖（fixture 全本地）/TS 插件生态 provider 不移植。

### 3. 测试面（§4 → 29 测）— PASS

- 29 测与 §4 映射恰形：consent×8（allow/choose/disable 持久+deadline→cancelled 身份+取消答复+未绑 channel fail-closed+空 registry 先于 form+已解析 selection 跳过）/selection×3（KV 重启稳定+越域摘除+random 亲和）/progress×1/cooldown×5（429 跳过+环尽末错+过期重试+非 429 不轮换+`cooldown_millis` TS 面[缺省/数值/负/HTTP-date/ISO/naive-UTC/不可解析]）/结果×3（markdown 形+truthiness 塌缩+零结果句）/错误×3（句表+tagged fallback+显式未知 provider NotFound）/权限×1（denied→零 provider 调用直证）/policy·Spec 钉×1/注册×3（wire name 解析+builtin 导出面+未 bind=wiring 缺陷）/输入×1（空 query 双面拒）。
- fixture 族：`_ScriptedProvider`（settable error/results+calls 记录）+`_ScriptedChannel`（scripted 回复队列+空队遇未期 ask=响亮失败）+`_MsClock`/`_Rng`（scripted 时钟/随机）+`_http_error`（httpx2.HTTPStatusError 造 429+retry-after 头）+`_kv_document`（kv.json 持久断言）——零外网零竞速（#254 纪律：KV 断言=写后读）。
- 测试 bug 自纠一例记档：expired-cooldown 测推进时钟后未清 `alpha.error`（测试侧时钟腿 bug 非 src 缺陷）——席位定谳+一行修+§0 记档。

### 4. 门禁与算术 — PASS（席位侧全档；调度器独立面待 accept 档定稿）

- 席位自跑四件（全前台收齐）：ruff 0 / format **599→600 终态**（+3 新 .py+T-C1-102.md 归位差+票拷=599；进度档入库后 600 T-C1-015 终态口径先例；实证口径 .py+.md 双族逐一归因）/ mypy **228-0**（225+3 恰合）/ 全量 **2388P+0F+8S@676.60s** collect **2396**=2367+29 恰合（websearch 29 测隔离窗复验 29P/0.84s）。
- install 族 4551：**本窗零遇形**——S3 三窗终证后首个全绿全量窗（flap 链谱连续绿相位面）；winpty 抖动族同零现；无判据链引用需求。
- 调度器独立四门禁（worktree p7-s4 真 python venv，`uv run --no-sync` 形，gates-p7s4-accept.txt 档）：**ruff 0 / format exit 0（`600 files already formatted` 与席位终态同数）/ mypy `Success: no issues found in 228 source files`（228-0 与席位同数）/ pytest `5 failed, 2383 passed, 8 skipped` @714.68s——5F 全员工 `test_cli_install.py`（`OSError: [WinError 4551]` SAC/WDAC 相位窗 flap）**：2383+5=2388 恰席位窗算术=websearch 29 测+全邻接零失败；**隔离窗整文件重跑 8P 全绿 in 22.60s**（gates-p7s4-accept-install-rerun.txt 档；窗间零代码变更=相位窗定谳非树面缺陷；flap 链 {0F↔1F↔4F↔5F} 谱内时变形：S2 窗五员全红形同谱+席位窗 0F 首全绿窗后回摆）——判据链引用恰形，非阻断。CI 终证=CI-WATCH-259（#257 收口检查点哨）。

### 5. 语义面独立复核 — PASS

- **query 环独立核**（TS core L173-208 vs PY L448-508 逐行对位）：choice 解析（显式 provider_id→choice=None）/requireProvider 显式腿（无轮换无 cooldown 读）/defaultProvider 缺省腿/None→ProviderRequired/affinity 保持（「deletion/movement cannot reinsert an in-flight session's entry」注释 verbatim 语义）/random 腿插回+轮换（`?? provider` 缺省保持=PY `if rotated is not None`）/cooldown 仅 random 腿读/429-only 轮换（非 429 或非 random→直抛）/环尽回退 `cooldown.error`（PY `assert cooldown is not None` 显式化同一不变量）/attempted 集——shape-for-shape。
- **cooldown_millis 独立核**：blank/missing→60_000；finite 数值（含空白容忍/科学计数）非负→×1000、负→60_000；HTTP-date/ISO→`max(0, delta)`；Infinity/NaN/不可解析→60_000——TS `Number`/`Date.parse` 对位（PY `float()`+`parsedate_to_datetime`/`fromisoformat` 双臂+naive→UTC）。
- **consent 序独立核**：lock→resolved default 直返→空 registry ProviderRequired 先于 form→channel None fail-closed→表 1→cancelled/disable/choose 分腿→表 2→providerID 缺省 `random`→select 持久→re-search（provider_id hint 仅非 random 腿携带）——TS `withPermit`+`timeoutOrElse`+`flatMap(search)` 恰合（超时仅裹 consent 段不裹 re-search，同形）。
- **mapError 独立核**：RequestError+status→句表+metadata provider；statusless→fallback；非 Request 全族（ProviderRequired/ProviderNotFound/Disabled/cancelled）→fallback 无 metadata；广谱→fallback——TS mapError 亲读对位（含席位自纠②泛句缺省臂）。
- **KV 独立核**：temp 同目录+`os.replace` 原子；FileNotFoundError/损坏字节/非 dict JSON→空库；stored-null 读 None（手工损坏=无选择，记档 conflation 面）；`_selection` 越域摘除（42/True/dict/list）。
- **census/policy 独立核**：期望 tuple `WebSearchAdapter` 恰位（QuestionAsk/SubagentRun/WebFetch/WebSearch 序）；`len(catalog.specs)` 19 在/18 缺两臂；`_BASE_WIRE_NAMES`+`websearch__search`；policy 行+query 绑定（既有三面零变）。
- **双宿主 bind 独立核**：`cli._drive`+`serving.serve` 命名实例+`bind(WebSearchService(app.database.path.parent / KV_FILE_NAME))`——webfetch 双胞胎形；`_builtin_adapters` keyword-only `websearch` 参+None 缺省补新实例（census 完整性保持）；`app.py` 零触（service 直取数据目录——与 S3 property 面的差异记档）。

### 6. 披露与记档 — PASS

- §5 十项全档（四面亲读结论/KV 选型+为何非他形/consent 映射形/registry·亲和·cooldown 实现形/policy·`_ASK_NAMESPACES`/注册·Spec 形/fixture 形/hook N/A/算术实证/命名选型）+§6 三件须知。
- 调度器已执行 §6 须知件：source-baseline.json 62→66 条（websearch 四面指纹追加，哈希亲证）。

## 发现明细

- **建议 1（记档面，无动作）**：进度档 §1 表行数陈旧（426/408/792 vs 实测 508/470/964）——纯 §1 表口径面（实现期一次测量后未随 docstring 丰富化刷新）；门禁四数全为文件计数/收集计数面不受行数影响；后继引用以实测为准。
- **建议 2（观察面，无动作）**：consent 载体=机制矫正先例第六例（question 工具面结构性不可能，三腿取证）——真实 provider 集成+宿主 consent wiring（question 工具面承载可行性评估含内）=席位 §6 已列后续增量票；台账观察池收面。
- **建议 3（流程面，无动作）**：compaction-checkpoint 存活续跑=席位弧第三态记档（S3 attempt1 死亡→同会话续跑先例；本席=压缩点存活零介入）——后继席位遇压缩点先验存活可能，超窗再判死亡。
- **建议 4（观察面，无动作）**：install 族 4551 零遇形=flap 链谱连续绿相位面（S3 三窗红红绿→S4 探针窗绿→席位窗全绿全量）——判据链零引用=谱内最良性相位。

## 附录：席位弧时序

- #256 trust_env 修复 `2eac833` push→CI-WATCH-258 success（run 43770625017）→探针重跑四腿全绿（2359P+8S exit 0@844.07s collect 2367）→发射 C 席（`ses_orch_C_p7s4`，票面 T-C1-103 §0-§8 websearch）→**单段弧** [06:55→07:43 ~48min：票面+TS 四面亲读→PY 锚钉面（Invoker progress/_DEFAULT_EFFECTS 行先例/question 工具面/webfetch 双胞胎参照/census 钉测）→service 508L→adapter 470L→注册双宿主→policy 行+query 绑定→census 钉翻新→29 测落盘→首轮 26/29→两处实现自纠（ProviderNotFound 独立 tag+mapError 泛句缺省臂）→余 1 定谳测试 bug（时钟腿）→一行修→29/29→四门禁全绿前台收齐（676.60s）→进度档 6 节→SEAT-EXIT 0]；**中含 compaction checkpoint 存活续跑**（进度档 §0 注记）；席位 git 写零（HEAD 恒 `2eac833`）；调度器侧 source-baseline 66 条指纹补录。
