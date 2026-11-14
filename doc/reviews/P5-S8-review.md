# P5-S8-review — T-C1-092 微评审：gate registry re-cast（GateBroker 生产装配接线）

- 评审对象：worktree `p5-s8` 席位工作区（未提交态，席位 git 写零）：改 3[doc/CORE-CONTRACTS.md +2/−1、src/opencode/cli.py +10/−5、src/opencode/httpapi/serving.py +10/−5] + 新 1[tests/integration/test_gate_assembly_e2e.py 170L 3 测]——与票面 §8 交付面逐路径吻合零多零漏；票面拷贝 tasks/T-C1-092.md 正确保持未跟踪（#180 B-②）；进度档 `doc/orchestration/progress/T-C1-092.md`（13.2KB，00:30:07 写毕先于 SEAT-EXIT 0 @00:30:41——前台收齐纪律内化）。
- 评审基据（全部独立现读，禁继承）：三文件 `git diff` 全量逐行亲读+新测试文件全文亲读；席位日志 `logs/T-C1-092.txt`（235KB）终报亲读；进度档全文亲读（改动面/测试面/决策锚映射/披露余量/门禁双跑留档/install 族相位判据链含今日窗宽偏差诚实载档+超出链面的补强探针七腿）；本席独立复跑四门禁（worktree 真 python venv，`uv run --no-sync` + `-m` 形，§4 面记独立结果）。
- 评审时刻：2026-11-15 00:3x（席位弧：~23:5x 发射→00:30:41 SEAT-EXIT 0 ≈40min，止损 100min 内；席位 git 写零，提交/合流/台账归调度器；单腿无 blocker——GateBroker 零参构造，091 式宿主值源面结构上不存在）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 3（全观察面，无返工）

## 六面逐条结论

### 1. 票面纪律 — PASS
- 禁 git 写遵守：席位全程 HEAD 恒 `321af96`（调度器独立核：终态工作区=3 改未提交+新测试文件+进度档+票面拷贝四件未跟踪）；交付=工作区。
- 票面 ```python 块纪律（091 首现后固化）：本票票面无 python 示例块，无再犯面。
- 进度档 00:30:07 写毕先于 SEAT-EXIT（00:30:41）；长门禁全量双跑前台收齐。
- **§4-3 择形执行到位**：席位亲读 test_gate_e2e.py 后定谳既有覆盖形（TestGateCycle=JobService 手工组装全链+TestGateReplyRoute=**App 级全链同形**）→ 装配测只钉组合缝白箱 `app._jobs._gate_broker is broker`，避免重复驱动——选择+理由（JobService 无公开 broker 读面+091 `manager._cancel_event` 白箱先例+ruff select 无 SLF001）记进度档，票面授权弧完整。

### 2. 决策锚落面（§3 六锚逐条）— PASS
- 锚1 装配双缝 ✓：cli.py+serving.py 各 `gate_broker=GateBroker()` 内联实参（零参直形无局部变量——锚 1 从优直形落面）+`from opencode.orchestrator.gates import GateBroker` 导入；每宿主组合恰一实例。
- 锚2 装配注释翻面 ✓：两处宿主装配注释块各增门面句（P5-S8, T-C1-092, gate registry re-cast——S5a 门面生产可达+回复链经 App gate_broker 直通缝点名）。
- 锚3 零触碰清单 ✓：**numstat 亲证**=改动面恰 cli/serving/CORE-CONTRACTS 三件——App/engine/gates/jobs/httpapi/contracts 全零（纯装配票成立）。
- 锚4 动态消息钉面 ✓：测 (b) 精确串匹配「this composition executes human_gate/llm_call/opencode_agent/tool nodes only」（4 元素排序 join 定谳）+`UnsupportedCapability` code+details 钉面；S2/S3 钉面消息零回归（test_pipelines.py 全量绿证）。
- 锚5 CORE-CONTRACTS ✓：Changelog [T-C1-092] 行+§5.4 S5a 段尾生产装配记载句（三面全载：零参构造注入/四元素 else 动态消息仅 subworkflow 拒/直通缝同 broker 实例）；其余段零触碰。
- 锚6 broker 生命周期 ✓：per-process 单例（每宿主组合恰一，与 engine 同构）；无跨 App 共享、无持久化（§5 披露承接）。

### 3. 测试面（§4 清单）— PASS
- 新 `tests/integration/test_gate_assembly_e2e.py`（170L 3 测，镜像 test_workers_e2e 先例——落点独立新文件不并入 test_gate_e2e.py：S5a 行为链套件与装配接线面两票钉面不混+§4-4 既有文件零改，理由记档）：
  1. `test_the_production_assembly_registers_a_gate_spec`——生产参数形组装（真迁移库+真 EngineCheckpointer+`_tiny_repo` 载 WorkerManager 镜像 091 装配缝+GateBroker）→ 单 human_gate 节点 spec **过语义门注册成功**+`engine.gate_broker is broker` 装配缝断言+try/finally `await engine.close()`（退出挂起坑 §0-6 落地）。
  2. `test_the_four_element_composition_names_its_executed_set`——同组合注册 subworkflow spec → PipelineSpecRejected **完整消息精确匹配**（unimplemented node type 句+4 元素排序 join+closed set 句）+code+details 三钉；拒绝路径 close 纪律同落。
  3. `test_the_app_conducts_the_broker_into_the_jobs_service`——`App(pipeline_engine=engine)` 手工组装镜像生产 → `app.pipeline_engine is engine`+`app._jobs._gate_broker is broker` 传导缝白箱钉（直通缝远端）；App 生命周期自持 close（App.close→engine.close 亲证注记）。
- `_assembled` helper=091 形+门面扩展（复用 `_tiny_repo`/`_upgrade_head` 既有 helper 导入——跨文件 helper 复用先例合法）；`_unreached_body` 诚实 fake raise；docstring 诚实范围声明（App 级全链归 test_gate_e2e.py 既有覆盖，装配测只钉接线面）。
- collect 增量实际=3（票面预期 ~2-4 区间内）。

### 4. 门禁与算术 — PASS（install 族=WinError 4551 相位窗（今日更宽），席位侧判据链含七腿补强探针全档；独立复跑见下）
- 席位自跑四件：ruff 0 / format 558 files exit 0（计数随未跟踪件漂移判 exit 0）/ mypy `Success: no issues found in 209 source files`（**209=208 基+1 新测试文件预期漂移恰合**）/ 全量双跑：Run1 **5F+2224P+8S@685.62s**、Run2 **5F+2224P+8S@564.16s**——双跑同五员集（全量负载下成员集稳定，091 同形）；collected 2237=票基线 2234+3 新恰合零漂移。
- **install 族相位链（今日窗宽形，诚实偏差载档）**：隔离腿×2 未全绿（腿1=1F/7P 成员漂移四员翻绿；腿2=5F/3P 窗张开——非 spawn 三测绿=轮安装/entry-point 健康）——与 091 当日 8/8 绿形不同，席位如实记档不套链；**补强取证七腿**（超链面）：①本树轮装 exe 现场spawn rc=0 输出版本串；②pytest-4 会话 exe 会话内红→数分钟后直接 spawn rc=0（时变翻转定谳）；③三会话 shim 同 47104B sha256 各异（pip shim 内嵌 venv 路径=预期非损坏）；④venv python.exe spawn rc=0（venv 健康，拒绝面 latch 在 shim 文件）；⑤shim 字节拷贝至新路径同 4551（文件级 latch）；⑥本树轮装 CLI `--version` rc=0 三连（**本票改动打包态可装载可运行直接证明**——entry-point 导入完整 opencode.cli 含 GateBroker 接线后 argparse 应答）；⑦PowerShell 直启同 4551（拒绝在 Windows loader 层早于任何 Python 代码——Python-only 改动结构上不可触达）。零非 spawn 失败模式（§6-4 红线未触）。
- 调度器独立四门禁（worktree p5-s8 真 python venv，`uv run --no-sync` 形，00:3x-00:4x 时段）：**ruff 0 / format exit 0 / mypy `Success: no issues found in 209 source files`（209-0=208 基+1 新测试文件预期漂移恰合）/ pytest `5 failed, 2224 passed, 8 skipped, 4 warnings` @595.21s**——5F 全为 install 族且与席位双跑**同五员集**（今夜窗口持续全机张开形，与 091「调度器侧翻转绿」形不同：本跑 4551 直接证据在档=相位证据肩在席位七腿补强+派发时负对照基线（主仓同刻同 5F）+CI-WATCH-233 同基线树全绿三件）；非 install 面 2224P 零失败（含本票 3 新测）；4W=aiosqlite GC 竞争时序既存面。CI 终证=CI-WATCH-234（云端 VM install 族历史全绿；#231 派发检查点 `321af96` CI-WATCH-233 已绿=同基线树 CI 终证先行面）。
- warnings：run1 1 条/run2 4 条（aiosqlite teardown 竞态族，基线既有，计数随竞态漂移；新测试文件零贡献）。

### 5. 语义面独立复核 — PASS
- 生产 `_implemented()` 本票后={llm_call, tool, opencode_agent, human_gate}：四元素集走语义门既有 else 动态分支（engine.py 校验区三分支亲证——S2/S3 钉面消息零变化，仅拒 subworkflow）；动态消息排序 join 定谳独立核（sorted() 字母序 human_gate/llm_call/opencode_agent/tool——测 (b) 精确串与源面逐字节吻合）。
- 直通缝独立核：App L340 `gate_broker=pipeline_engine.gate_broker if ... else None` property 直通（078/080 预埋面）——生产传 broker 后回复面 resolve 与节点体 take 同一实例（测 (c) 传导缝远端钉面）；比 091 canceller isinstance 缝更简（无类型探测）。
- broker 语义零改独立核：gates.py 81L 零触碰（numstat 亲证）；in-memory by design 承接（重启丢未消费回复=recovery 重铸门，§5.4 S5a 既披露——持久化非目标未发明）。
- 纯装配票语义边界：生产代码改动面=cli/serving 各 2-3 行+注释（numstat +10/−5 含注释重排）——零引擎/契约/jobs 面发明。

### 6. 披露与记档 — PASS
- 票面 §5 五条余量全数落位进度档「披露余量」节（承接原文+退出挂起坑适用面落地面）。
- 判据链今日窗宽偏差诚实载档（隔离腿未全绿不套链+七腿补强取证「相位非代码」强证据定谳）——诚实纪律标杆续面（091 blocker 弧后第二例）。
- §4-3 择形白箱理由+§8 落点报告（独立新文件）+锚映射逐条成文。

## 发现明细

- **建议 1（观察面，无动作）**：4551 相位窗今日形（全量双跑稳定 5F+隔离腿漂移+文件级 latch 在 shim）与历史形（087-091 隔离全绿翻转形）的差异面已由席位七腿探针定性——后续票遇「隔离不翻绿」形可直接引用本档补强腿（时变翻转+loader 层拒绝早于 Python+本树轮装可运行三件套）免重复取证成本。
- **建议 2（观察面，无动作）**：测 (c) 白箱 `app._jobs._gate_broker` 依赖 JobService 私有面——若后继票为 JobService 增公开 broker 读面（如测试缝需求），该断言可翻公开形；当前 ruff select 无 SLF001 合法+091 先例同构，无返工面。
- **建议 3（CI 面观察项）**：本票 +3 测增量（suite +~5s 量级），windows 腿 25min 天花板余量充足；crash_sweeps_tree 树杀竞速已于 #229 检查点 attempt 3 绿定谳（间歇竞速，CI-179b 先例闭合——载 #232 收口条）。

## 附录：席位弧时序

- ~23:5x 发射（探针判据链定谳后：ruff 0/format 0/mypy 208-0 三绿+install 族 5F 负对照基线同刻同红+CI-WATCH-233 同树全绿=发射门通过）→00:30:41 SEAT-EXIT 0（≈40min）；进度档 00:30:07 先于退出落盘。
- 单腿无 blocker（票面预告「GateBroker 零参构造，091 blocker 面结构上不存在」验证成立）。
