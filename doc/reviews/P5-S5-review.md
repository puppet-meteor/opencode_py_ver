# P5-S5-review — T-C1-089 微评审：tool 节点真实体（run_command 绑定 command 臂+tool_ref 臂诚实拒绝）

- 评审对象：worktree `p5-s5` 单提交 `fd568fd`（父=`0fb3be8`，numstat 恰 10 路径：改 7[doc/CORE-CONTRACTS.md +2/−1、src/opencode/cli.py +5/−3、src/opencode/httpapi/serving.py +10/−7、src/opencode/orchestrator/engine.py +6/−3、src/opencode/orchestrator/llm_call.py +7/−30、tests/contract/test_llm_call.py +4/−15、tests/integration/test_llm_call_e2e.py +7/−4] + 新 3[orchestrator/tool.py 110L、tests/contract/test_tool.py 137L、tests/integration/test_tool_e2e.py 95L]——与票面 §8 交付面逐路径吻合零多零漏；票面拷贝 tasks/T-C1-089.md 正确排除、#180 B-② 遵守）。席位票 `T-C1-089.md` 钉面全量（调度器钉面残余亦同步：§0.5 venv 注记+§6 `-m` 形修订版）。
- 评审基据（全部独立现读，禁继承）：`git show fd568fd` 全量 diff 逐行亲读+3 新文件全文亲读；席位日志 `logs/T-C1-089.txt`（63KB）关键证据面亲读（定向 47/47+三快门绿+CORE-CONTRACTS 双落位自证+mypy invariance 修正循 engine.py L1583 `cast("list[JsonValue]", …)` 先例）；本席独立复跑四门禁（worktree 真 python venv，`-m` 形）：ruff 0 / format 550 files already formatted exit0 / mypy `Success: no issues found in 207 source files`（204 基+3 新文件恰增）/ pytest **2216 passed, 8 skipped, 3 warnings, 0 errors exit 0** 604.68s——席位全量面被单发截断，由本席补全承证。
- 评审时刻：2026-11-14 20:05:00（席位主弧 19:37:55 发射→19:50:35 SEAT-EXIT 0 ≈13min，止损 100min 内；席位 git 写零，提交/合流/台账归调度器）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 1 / 建议 2（席位进度档缺席一项；观察面两条）

## 六面逐条结论

### 1. 票面纪律 — PASS
- 禁 git 写遵守：席位全程 HEAD 恒 `0fb3be8`（调度器独立核：席位终态工作区=10 路径未提交+票面拷贝，提交由调度器执行）；交付=工作区。
- 票面 tasks/*.md 未入提交（fd568fd numstat 亲证零 tasks 路径）；诚实纪律遵守——llm 侧注 `_no_llm_body`（AssertionError「no llm node should reach it」，非编造结果）。
- **次要 1（席位过程缺件——单发模式截断）**：席位把全量 pytest 放后台后续写进度档，单发会话在后台任务在飞时自然收束（终句「I'll write the progress file…」中断于 SEAT-EXIT 0）——`doc/orchestration/progress/T-C1-089.md` 缺席（票面 §0.4 返回前件）。验收证据无损：调度器独立四门禁补全（全绿）+席位日志/门禁档全在 `logs/`（T-C1-089.txt 63KB+gates-p5s5-acceptance.txt）——微评审判 0 返工；教训=单发席位禁后台化长门禁（§0.4 自证链应前台收齐或进度档先写）。

### 2. 决策锚落面（§3 八锚逐条）— PASS
- 锚1 新模块 `orchestrator/tool.py`（镜像 llm_call 命名先例）、无构造态模块级 async `run_command_tool_body`（满足 ToolFn）✓。
- 锚2 tool_ref 优先序：L70 `if node.tool_ref is not None` 先查拒绝、command 臂后行——attempt 形（双设面）绝不静默重跑主 argv（专测钉面 L132-137）✓。
- 锚3 `TOOL_REF_UNSUPPORTED_MESSAGE` 新常量（整文钉面）+旧 `TOOL_BODY_UNSUPPORTED_MESSAGE`/`unsupported_tool_body` 自 llm_call.py 删除干净（src 零残留亲证：席位 ctx_search 0 hits+diff 亲读）✓。
- 锚4 command 臂执行面全参数：`args=list(node.command)` argv 直传/cwd=None/env=None/`timeout_s=TOOL_NODE_TIMEOUT_S`（120.0 独立常量+注释点名三先例、调用时读常量）/`max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES` 显式传/无 should_cancel/on_stdout_line/`await asyncio.to_thread(...)` workers L630 先例 ✓。
- 锚5 成功信封恰五键 `{stdout, stderr, exit_code, stdout_truncated, stderr_truncated}`——e2e+单测双钉面（恰等断言非子集）✓。
- 锚6 错误映射四臂：timed_out→TOOL_TIMEOUT（点名节点/死线/树杀）、returncode!=0→TOOL_FAILED（rc+stderr 尾 `[-512:]` workers 同形）、OSError→TOOL_FAILED spawn 面、空 argv→TOOL_FAILED「argv is empty」（Popen 前拒）✓；零新异常类型（全 WorkerError 带码）✓。
- 锚7 装配换轨双落：cli.py L663+httpapi/serving.py L260 `tool=run_command_tool_body`+import 同步改+注释翻面（「the real one is a later slice」→真实体描述）✓。
- 锚8 CORE-CONTRACTS 双落位：顶部变更行 L19（2026-11-14 [T-C1-089] 全段）+§S2「执行面」子句 T-C1-089 订正（command 臂真实绑定面+tool_ref 臂点名后继切片+注入缝保持）——席位自证+调度器 diff 亲读双核 ✓。
- ToolFn Protocol docstring 翻牌（engine.py 仅此一处改——diff 亲证 9 行全在 docstring 区）✓。

### 3. 测试面（§4 12 新+2 迁移）— PASS
- `tests/contract/test_tool.py` 10 测全落：成功信封恰形/stderr 捕获/非零退出（code 3+stderr 尾）/超时臂（**先钉 `TOOL_NODE_TIMEOUT_S == 120.0` 生产值再 monkeypatch 0.3**+消息含节点名+树杀+「0.3s」）/spawn 失败/空 argv/64KiB 截断（**精确前缀 65536 x 恰等**+旗标 True）/params 不代换（`{argv0} {submitted}` 字面直跑）/tool_ref 拒绝整文/优先序臂（双设面拒绝且 argv 不执行）✓——真子进程全 `sys.executable` 形（runner/workers 先例）。
- `tests/integration/test_tool_e2e.py` 2 测全落：生产组合形（真 alembic 库+真 checkpointer+`App(pipeline_engine=...)`）单 command 节点→done+node_results 信封恰形；非零退出→failed+§8 `ToolFailed` face（error["code"]+details node_id/node_type）——**引擎保 body WorkerError code 面被 e2e 钉死** ✓。
- 迁移面：test_llm_call.py 删 `TestUnsupportedToolBody`+import 清理+docstring 指向新家；test_llm_call_e2e.py import/装配换轨+docstring 注记「unexercised by this spec's single llm_call node——pinned by test_tool_e2e.py」✓。其余 12 处 `PipelineEngine(` 装配零触碰（fd568fd numstat 亲证）✓。
- mypy invariance 修正循先例：e2e spec `command` 字段 `cast("list[JsonValue]", command)`（engine.py L1583 同形）——非随意 `as`/`type: ignore` ✓。

### 4. 门禁与算术 — PASS
- 四件全绿（本席独立复跑 exit 0，非继承席位自报）：ruff All checks passed!/format 550 files already formatted exit0（计数随未跟踪 md 漂移判 exit0）/mypy `Success: no issues found in 207 source files`（204 基+3 新恰增）/pytest **2216 passed, 8 skipped, 3 warnings, 0 errors** 604.68s exit 0。
- collect 算术闭合：**2224 = 2213（#222 基线）+12 新（单测 10+e2e 2）−1 迁移删除（TestUnsupportedToolBody 单测）恰合零漂移**；passed 2216 = 2199+12−1+**6（install 族 6E→6P 全转绿——SAC/WDAC 相位本轮消退，exit 0 全量面首见）**；8 skips=基线既存集；3 warnings=aiosqlite GC 竞争既存面（085/086/087/088 四方证先例）。

### 5. 语义面独立复核 — PASS
- run_command 契约核对：`timeout_s=120.0` 默认与 TOOL_NODE_TIMEOUT_S 同值独立（体侧显式传参——非隐式默认依赖）；`RunResult` 十一字段中 timed_out/cancelled/early_stopped/reaped 正确不入成功信封（皆失败/中断路径）；`DEFAULT_MAX_OUTPUT_BYTES=64*1024` 单一来源导入非复铸。
- fallback 复用形兼容：`_try_fallback_tools` attempt 节点（tool_ref 形）→真实体诚实拒绝→**raised attempt=异常记录非 job 失败**（L1573-1574 契约）——链耗尽主值不渲染，机器面=注入 fake 测试持续覆盖；生产惰性披露已在票面 §5-①（无隐瞒）。
- 信任面：argv=运营者注册管线静态字面（PHASE3 §1 非目标口径）；node_params 永不代换进 argv（专测双钉）。

### 6. 披露与记档 — PASS
- 票面 §5 五条余量全数有效落位（调度器复核无新增隐瞒面）：①fallback 惰性 ②无协作取消缝（硬死线+树杀兜底）③cwd/env 继承 ④params 对 command 臂不可用 ⑤运营者信任面。

## 发现明细

- **次要 1（席位进度档缺席——单发模式后台截断；处置零返工）**：见 §1。证据链由调度器独立门禁（全绿 exit 0）+席位日志（63KB 含全部过程证据）+本评审件闭合；进度档类缺件后续票面增补硬令「前台收齐门禁再返回」。
- **建议 1（观察面，无动作）**：install 族本轮 6E→6P 全转绿（前轮 6E=全量负载下 SAC 间歇相位、隔离重跑绿）——相位消退为时序事实，非本票代码效应；venv 真修形（真 python.exe）对 install 族 fixture venv 无因果（fixture 自建隔离 venv）；trampoline 阻断面随时可复发——worktree venv 纪律（真 python `-m venv` 自建+禁重建）+`-m` 形门禁命令保持。
- **建议 2（CI 面观察项）**：windows 腿时长链 13m30s→…→15.2m（cancelled）后，#223 已抬 `timeout-minutes: 15→25`（CI-WATCH-225 双腿绿验证收口）；本票 fd568fd 逼近「+12 测」增量（suite +~30s 量级），25min 余量充足；若腿时长再现 >24m 再议。
