# P5-S7-review — T-C1-091 微评审：workers capability closure（WorkerManager 生产装配接线+ConfigLoader.project_root() 读面）

- 评审对象：worktree `p5-s7` 席位工作区（未提交态，席位 git 写零）：改 5[doc/CORE-CONTRACTS.md +2/−1、src/opencode/cli.py +37/−1、src/opencode/config/loader.py +4/−0、src/opencode/httpapi/serving.py +40/−5、src/opencode/orchestrator/workers.py +28/−0] + 新 1[tests/integration/test_workers_e2e.py 164L 4 测]——与票面 §8 交付面（修订版：blocker 裁决后含 loader.py）逐路径吻合零多零漏；票面拷贝 tasks/T-C1-091.md 正确保持未跟踪（#180 B-②）；进度档 `doc/orchestration/progress/T-C1-091.md`（16.8KB，23:04:01 写毕先于 SEAT-EXIT 0 @23:04:23——前台收齐纪律内化，无延迟落盘相竞）。
- 评审基据（全部独立现读，禁继承）：五文件 `git diff` 全量逐行亲读+新测试文件全文亲读；席位日志 `logs/T-C1-091.txt`（194KB，双轮弧全文）终报亲读；进度档全文亲读（leg1 blocker 9.4KB+leg2 全程 16.8KB：改动面/测试面/决策锚映射/披露余量/门禁双跑留档/install 族相位判据链/skill_activation flake 消解弧/退出挂起坑）；本席独立复跑四门禁（worktree 真 python venv，`uv run --no-sync` + `-m` 形，§4 面记独立结果）。
- 评审时刻：2026-11-14 23:10（席位弧：21:25 首轮发射→21:54 SEAT-EXIT 1 §0-3 诚实停笔（零代码写入）→21:57 调度器 #229 裁决→~21:59 续跑（同 session 保留预读）→23:04:23 SEAT-EXIT 0——有效工作段 ~55min，止损 100min 内；席位 git 写零，提交/合流/台账归调度器）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 3（全观察面，无返工）

## 六面逐条结论

### 1. 票面纪律 — PASS
- 禁 git 写遵守：席位全程 HEAD 恒 `4d664f5`（调度器独立核：终态工作区=5 改未提交+新测试文件+进度档+票面拷贝四件未跟踪）；交付=工作区。
- **Blocker 弧诚实纪律标杆**：首轮发现锚 6 事实性冲突（`ConfigLoader.project_root` 公开读面不存在——锚 1 表达式运行时 AttributeError）即停笔零代码写入，进度档 9.4KB 全记（证据链：loader.py 13 处命中全私有/内部面、公开方法全集七枚无一承载、无 `__getattr__`、`ResolvedRuntime` 三字段无仓库根）+四备选路线逐一排雷（cli 直读 args 可 None 不复现 VCS 回退/serving 重跑发现违 one-source/`skills_directory().parent` 耦合 hack/`runtime.roots` 权限根非仓库根）——不发明语义、不绕行、点名返回，§0-3 完美执行；调度器认领钉面错误（#229），裁决 B 方法形，票面三处修订后续跑。
- 进度档 23:04:01 写毕先于 SEAT-EXIT（23:04:23）——089「长门禁禁后台化+前台收齐」纪律执行到位。
- 票面 md ```python 代码块纪律（本票首现）：席位从修正后票面工作（format 554 exit 0 维持），无再犯面。

### 2. 决策锚落面（§3 八锚逐条，修订版）— PASS
- 锚1 装配双缝 ✓：cli.py +37/−1 + serving.py +40/−5——两处宿主各构造生产形 WorkerManager 全参 kw-only（repo_root/runs_root/default_budget/base_url/api_key/model/context_limit）注入 `PipelineEngine(opencode_agent=...)`；值源 cli=args+host_runtime、serving=入参+runtime（各自作用域）；装配注释翻面（P5-S7, T-C1-091，workers capability closure——真 CLI 子进程三件竞技场+App isinstance 探测缝自动闭合点名）。
- 锚2 runs_root 派生 helper ✓：`workers_runs_path_for(db_path)` 落 workers.py（内聚裁决=常量同文件），`with_name(f"{stem}-workers")` sibling 派生（`checkpointer_path_for` 先例同构）；装配处 `mkdir(parents=True, exist_ok=True)` 宿主承基面（两处），per-worker 子目录树归 manager 语义（078 契约零改）。
- 锚3 墙钟线常量 ✓：`WORKER_DEFAULT_TIMEOUT_S = 1800.0` workers.py 模块常量（注释点名 089 TOOL_NODE_TIMEOUT_S 先例+语义区别于 LLM 调用死线——**`settings.timeout_s` 全程未接**，语义坑锚死生效）；`default_budget=JobBudget(timeout_s=WORKER_DEFAULT_TIMEOUT_S)` max_tokens/max_tool_calls 留 None；workers.py +28 全为常量+helper+注释，**执行体零触碰**。
- 锚4 cancel 链零新代码 ✓：app.py 零触碰（5 改文件亲证无 app.py）；`engine.opencode_agent` property+App isinstance 探测缝自动闭合（e2e 测断言面）。
- 锚5 worker_command 不传 ✓：缺省 `[sys.executable, "-m", "opencode"]` 真 CLI 面（两处装配均无 worker_command 实参）。
- 锚6 repo_root ✓：**blocker 裁决 B 形落面**——loader.py +4：`def project_root(self) -> Path` 三行体返回 `self._project_root`+docstring，落点 `skills_directory()` 后（沿 `pipeline_tiers()` 宿主 reader 方法风格先例）；装配表达式 `repo_root=loader.project_root()`（cli=--project-root 钉定根、serving=VCS 发现根，两处各自 loader）。
- 锚7 CORE-CONTRACTS ✓：顶部 Changelog [T-C1-091] 行+§5.4 S3 段生产装配记载行（+2/−1，两处宿主构造 WorkerManager 注入+墙钟线常量+cancel 链 App isinstance 缝三面全载）。
- 锚8 引擎/契约零触碰 ✓：engine.py、contracts/*、errors.py、schema-lock、app.py 全零（工作区路径亲证）；loader.py 仅裁决授权的 4 行读面。

### 3. 测试面（§4 清单）— PASS
- 新 `tests/integration/test_workers_e2e.py`（164L 4 测，模块 docstring 诚实范围声明——真 worker 驱动已由 test_workers.py 全链覆盖，装配测不重复真子进程跑，只钉生产接线面；llm_call/tool 缝=honest fakes raise AssertionError：节点到达即接线 bug 非捏造结果）：
  1. `test_the_runs_root_derives_from_the_db_path`——helper sibling 三断言（`<stem>-workers` 同目录派生+与 db 路径同名异缀+`checkpointer_path_for` 同款形）。
  2. `test_the_default_worker_wall_clock_line_is_the_pinned_constant`——`WORKER_DEFAULT_TIMEOUT_S == 1800.0` 生产值钉（089 先钉后 monkeypatch 模式之值钉面）。
  3. `test_production_assembly_passes_the_engine_semantic_gate`——生产参数形全参组装（tmp repo/runs/JobBudget(timeout_s=1800)/fake base_url）→ 注册含 opencode_agent 节点的 spec **过语义门注册成功**（生产面可达性主断言）+`engine.opencode_agent is manager` 装配缝断言+`await engine.close()` 收尾纪律（退出挂起坑内化——见 §6）。
  4. `test_app_cancel_chain_closes_through_the_manager`——App(pipeline_engine=) 手工组装镜像生产→cancel 链经 manager 的可观测副作用（latch 断言）+host-only events **全表扫描**（bus 事件全表无 base_url/api_key 泄漏——§4-4 断言面落位）。
- `_tiny_repo` 镜像 test_workers.py 先例形；4 测=票面预测 ~4-6 下沿，增量恰合。

### 4. 门禁与算术 — PASS（install 族=WinError 4551 OS 策略阻断相位，席位侧判据链留档；skill_activation=载荷 flake 隔离消解；独立复跑见下）
- 席位自跑四件：ruff 0 / format 556 files exit 0（计数随未跟踪 md 漂移判 exit 0）/ mypy `Success: no issues found in 208 source files`（**208=207 基+1 新测试文件预期漂移恰合**）/ 全量双跑：Run1 6F（5 install+1 skill_activation）→ Run2 **5 failed + 2221 passed + 8 skipped**——install 族 5 测=WinError 4551 spawn 阻断相位（席位侧窗口活跃：直接证据+同树双跑+隔离重跑全腿留档进度档，相位判据链照 §6 执行非 spawn 模式不得当相位之约定遵守）；**skill_activation 消解弧**：Run1 第 6 败隔离重跑绿×2+Run2 翻转绿=载荷 flake 定谳非回归；新测 4/4 套内绿。
- collect 算术闭合：**2234 = 2230（`4d664f5` 基线：2222P+8S）+ 4 新（test_workers_e2e）恰合零漂移**；Run2 面 2221P = 2222 基−5 install 相位+4 新；8S 持平；票面预测 ~4-6，实际 4 落于预测区间。
- 调度器独立四门禁（worktree p5-s7 真 python venv，`uv run --no-sync` 形，23:0x-23:2x 时段）：**ruff 0 / format exit 0 / mypy `Success: no issues found in 208 source files`（208-0=207 基+1 新测试文件预期漂移恰合）/ pytest `2226 passed, 8 skipped, 2 warnings` exit 0 @612.03s 全量绿含 install 族**——与席位自跑面互证：席位 Run2 install 族红（22:5x-23:0x 窗口活跃）vs 调度器绿（23:1x 窗口消退）=同树时序翻转再现（窗口时开时合与代码树无关终证）；非 install 面两跑一致绿（含本票 4 新测）；2W=aiosqlite GC 竞争时序既存面（085-089 五方证先例）。CI 终证=CI-WATCH-232（云端 VM install 族历史全绿，本地 SAC 不随行）。
- **CI 附带面（与席位工作零因果）**：#229 检查点 `4d664f5`（doc-only，代码与 CI-WATCH-229 绿面逐字节同树）windows 腿 `test_mcp_stdio.py::test_crash_sweeps_tree` 树杀竞速失败两次（PID 10040/7424 不同=同窗竞速非同进程；2026-11-10 CI-179b 同面历史先例；T-C1-061 曾 widened 10s→30s、T-C1-075 P1 capture pin 本轮绿=非 capture 面）——attempt 3 终证在飞，结果随 #230 收口条载档。

### 5. 语义面独立复核 — PASS
- 墙钟线语义分离独立核：`RuntimeSettings.timeout_s`=LLM 请求死线（120s 量级）全程未接 worker 面；`WORKER_DEFAULT_TIMEOUT_S=1800.0` 独立常量——真 worker（CLI 子进程+三件竞技场）30min 量级止损合理，语义坑锚死闭环。
- `model=host_runtime.settings.model.model`——ModelRef.model id 字符串（workers.py 要 str 非 ModelRef），值源同 host_runtime 无漂移；context_limit 同源。
- cancel 链独立核：App L330-339 isinstance 探测缝（078 预埋）生产传 manager 后自动闭合——e2e 测④行为面断言（latch 可观测副作用）落位，`JobService.cancel` canceller 触发面非白箱戳。
- 生产 `_implemented()` 本票后={llm_call, tool, opencode_agent}——语义门 S3 变体消息翻面（opencode_agent 入 implemented 集），gate_broker/subworkflow 仍拒（池 6/7 归属不变）；`loader.project_root()` 返回 --project-root 钉定根或 VCS 发现根——与构造期根发现同源零漂移（one-source 纪律闭环，席位排雷面调度器采纳之核心理由）。
- runs_root 基面责任划分独立核：宿主装配处 mkdir 承基面、manager per-worker 子目录树自管——078 契约（worktree/run dir 语义）零改动。

### 6. 披露与记档 — PASS
- 票面 §5 五条余量全数落位进度档「披露余量」节：①墙钟线=常量 1800.0 定量（配置化归后继——pipeline 命名空间 090 已预留）②worker_command 缺省真 CLI 无配置面 ③gate/subworkflow 归池 6/7 ④runs_root 基面宿主承责 ⑤per-job budget 覆盖走既有提交 params 面。
- **退出挂起坑诚实载档（后继票资产）**：engine-only 测试（无 App）`register()` 物化 aiosqlite saver 线程，不 `await engine.close()` 解释器不退——席位亲踩亲记+测试③收尾纪律内化（e2e 测③有 close 收尾）。
- Blocker 弧全程记档：leg1 证据链+四备选排雷+#229 裁决引用+leg2 全程——审计面完整可回放。

## 发现明细

- **建议 1（观察面，无动作）**：blocker 弧（锚 6 证伪→诚实停笔→调度器裁决→续跑）是本流程纪律标杆案例——席位不发明语义零绕行+调度器认领钉面错误+裁决有据（B 方法形沿 090 `pipeline_tiers()` 先例），全程台账可回放；后续票面钉面有「属性/方法面」断言时调度器钉面阶段即 grep 公开面实证（本轮教训固化）。
- **建议 2（观察面，无动作）**：退出挂起坑（engine-only register() 物化 saver 线程）为后继 engine 相关票的高频地雷——进度档已载，后续票面遇 engine-only 测试场景宜直接引用本坑先例（测试收尾 `await engine.close()` 纪律）。
- **建议 3（CI 面观察项）**：`test_crash_sweeps_tree` windows 腿树杀竞速两连败（doc-only 检查点面上）——attempt 3 结果定谳：绿=间歇竞速（CI-179b 先例重现闭合）；红=三连败升格 runner 池面观察项，测试硬化票（teardown 竞速窗）候选进 backlog 评估。本票 +4 测增量对 windows 腿 25min 天花板余量无碍。

## 附录：席位弧与门禁时序（blocker 双轮弧完整载档）

- 首轮 21:25 发射→21:54 SEAT-EXIT 1（§0-3 停笔，零代码写入，进度档 9.4KB blocker 全记）——根因：调度器票面锚 6「loader 属性面已备」事实性错误（loader.py L654 `self._project_root` 内用误推公开读面存在）。
- 调度器 21:57 #229 裁决（B 方法形）+票面三处修订（锚 1 表达式/锚 6/§8 交付面）+worktree 基推进 `4d664f5`（reset --hard 安全窗，未跟踪件保全）。
- 续跑 ~21:59（同 session `ses_orch_C_p5s7` 保留预读上下文——#229 预读全绿面免复核指令）→23:04:23 SEAT-EXIT 0；进度档 23:04:01 先于退出落盘。
- 门禁双跑时窗：Run1（6F：5 install 4551+1 skill_activation 载荷 flake）→隔离重跑+直接证据留档→Run2（5F：install 族窗口持续+skill_activation 翻转绿消解）——席位侧 install 族 4551 窗口（21:4x 探针消退后 22:5x-23:0x 复开）为「调度器探针绿→席位红」的反向时序翻转，相位面再证（窗口时开时合，与代码树无关）。
