# P5-S9-review — T-C1-093 微评审：subworkflow restart idempotency（`_subworkflow` body 重入 adoption 前瞻+`subworkflow=True` 生产接线）

- 评审对象：worktree `p5-s9` 席位工作区（未提交态，席位 git 写零）：改 5[doc/CORE-CONTRACTS.md +2/−1、src/opencode/cli.py +6/−1、src/opencode/httpapi/serving.py +5/−0、src/opencode/orchestrator/engine.py +65/−15、src/opencode/orchestrator/jobs.py +15/−2] + 新 1[tests/integration/test_subworkflow_restart.py 316L 4 测]——与票面 §8 交付面逐路径吻合零多零漏；票面拷贝 tasks/T-C1-093.md 正确保持未跟踪（#180 B-②）；进度档 `doc/orchestration/progress/T-C1-093.md`（14.2KB，01:47:34 写毕先于 SEAT-EXIT 0 @01:47:58——前台收齐纪律内化）。
- 评审基据（全部独立现读，禁继承）：五文件 `git diff` 全量逐行亲读（engine.py 含 docstring 余量句改写+adoption 前瞻块全貌；jobs.py 公读面+私有委托面；cli/serving 接线面；CORE-CONTRACTS 三处改动区 UTF-8 直读核文）+新测试文件全文亲读（316L）；席位日志 `logs/T-C1-093.txt`（108KB）终报亲读；进度档全文亲读（改动面/锚映射/披露余量/门禁输出/4551 三腿补强全档）；本席独立复跑四门禁（worktree 真 python venv，`uv run --no-sync`+`-m` 形——§4 调度器行占位待档返回核证定稿）。
- 评审时刻：2026-11-15 01:5x（席位弧：01:14:23 发射→01:47:58 SEAT-EXIT 0 ≈34min 单腿无 blocker，止损 100min 内；席位 git 写零——终态 HEAD 恒 `4d55bef` 调度器亲证）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 3（全观察面，无返工）

## 六面逐条结论

### 1. 票面纪律 — PASS
- 禁 git 写遵守：席位全程 HEAD 恒 `4d55bef`（调度器独立核：终态工作区=5 改未提交+3 未跟踪[新测试/进度档/票面拷贝]，与席位终态恰合）。
- 进度档 01:47:34 写毕先于 SEAT-EXIT（01:47:58）；长门禁全量 pytest 前台收齐。
- 跨文件测试导入先例合法：test_subworkflow_restart 导入 test_subworkflow `_Stack`/`_gate_child_spec`/`_await_child`/`_other_job_ids`/`_sub_spec`/`_tool_spec` spine——单一真栈定义零漂移+既有文件零回改（票面 §4 预告面落地）；`_tiny_repo`/`_upgrade_head` 沿 091/092 既有 helper 复用先例。
- 诚实标注双落：死仿真（未标记 `task.cancel()`=run 包装层静默死纪律复刻）与 depth 违例伪造（公面 submit 结构上不可产生，in situ 标注）均入测试 docstring。

### 2. 决策锚落面（§3 六锚逐条）— PASS
- 锚1 adoption 前瞻全四支亲读逐行吻合：body 入口（DRIVE_CONTEXT 守卫后、submit 前）`non_terminal_children(context.job_id)` 谓词 `pipeline_id == node.workflow_ref`（parent 过滤内建公读面 lineage）——恰一=收养（跳 submit，`nested_job_id=adopted.job_id` 走既有 run+轮询+终态三分支原样）；零=fresh 路径原样（v1 行为零漂移、首入成本=一读）；≥2=fail-closed `WorkerError`（code=`ExecutionUnknown`）消息点名 node_id/匹配数/「cannot be proven, failing closed」；深度防御 `adopted.depth != context.depth + 1` 同 fail-closed（构造不变量违例=归因不可证面）。
- 锚2 公读面：`non_terminal_children(job_id) -> list[JobRecord]` records 形+docstring（body 匹配需 pipeline_id/depth 理由载明）；私有 id 遍归改委托公开面（单一遍归定义零漂移）；repos.non_terminal_jobs() 承载零翻面；numstat 亲证=contracts/events 零触碰（JobLineage wire `extra="forbid"` 不可扩=设计规避验证）。
- 锚3 收窄残量：docstring 尾段+CORE-CONTRACTS S5a 段双落同文——「子已终态而节点未完成仍 fresh-submit（节点↔子绑定唯一持久面=节点结果，完整幂等需绑定归因，v1 起结构不可行）」。
- 锚4 生产接线：cli.py/serving.py 两处 `PipelineEngine(...)` 各增 `subworkflow=True` 实参+装配注释增句（五元素全集点名）。
- 锚5 CORE-CONTRACTS：Changelog [T-C1-093] 行+§5.4 S5a 段巨行改写——numstat +2/−1 恰合（Changelog +1/−0；S5a 单行 −1/+1 双改：v1 余量句→adoption 语义句+收窄残量句、092 生产装配句尾增 093 接线句「subworkflow 自此生产可达+五元素全集零拒载」）；其余段零触碰。
- 锚6 fail-closed 择形：票面预裁理由（弃「最长者择一」——两 body 交叉认领=发明绑定语义+同择最长者双驱同子）记档进度档；消息措辞「the node-to-child binding cannot be proven」落面——§8 branch-④ 同构纪律。

### 3. 测试面（§4 清单）— PASS
- 新 `tests/integration/test_subworkflow_restart.py`（316L 4 测，真栈镜像）：
  1. `test_a_mid_node_restart_adopts_the_in_flight_nested_job`——gate-child 天然 mid-node 驻留（父 body 轮询窗）→**未标记 `task.cancel()` 死仿真**（吞 CancelledError；父留 running/子留 gated=进程死面精确复刻）→ plain 重驱→**判据全落**：`_other_job_ids == [child_id]`（全程恰一子行=零重复提交）、节点结果 `nested["job_id"] == child_id`（收养身份面）、`_await_fresh_gate`（started→gated 周期重铸现役门——回复必须打新门）→ 双 done 收敛。
  2. `test_ambiguous_in_flight_children_fail_closed_execution_unknown`——公面 submit 预植两子（depth=1=父+1、lineage parent=父；`_check_lineage` 接受面亲证）→ 父 failed `ExecutionUnknown` 三点名（node "nested"/"2 in-flight children"/"cannot be proven"）+两子零触碰（仍 created）+无第三子行。
  3. `test_a_depth_invariant_violation_fails_closed_execution_unknown`——sqlite 直改 depth 构造不变量违例（**诚实标注伪造**）→ 同 fail-closed（"violates the parent+1 invariant"+子 id 点名）+子零触碰恰一行。
  4. `test_the_production_assembly_registers_subworkflow_specs`——`_assembled` 镜像 092 形+`subworkflow=True`：单 subworkflow 节点 spec **过语义门注册成功**（生产可达主断言）+五元素全型 spec 零拒载+try/finally `await engine.close()`（§0-6 退出挂起坑落地，T-C1-091 教训注记在案）。
- `_await_fresh_gate` =席位亲读 gate 重铸面后的判据适配（票面 §2 预告「席位亲读定确切重入路径，判据不因此变」执行面）——轮询窗 5s @0.01s 测试节奏面。
- collect 增量实际=4（票面预期 ~3-5 区间内）。

### 4. 门禁与算术 — PASS（install 族=WinError 4551 相位窗持续张开形；判据链 092/093 先例引用+本树三腿补强全档；调度器独立复跑三绿+同五员集）
- 席位自跑四件：ruff 0（1×I001 自愈——新测试文件导入序自纠后归零）/ format 562 files exit 0（计数随未跟踪件漂移判 exit 0）/ mypy `Success: no issues found in 210 source files`（**210=209 基+1 新测试文件预期漂移恰合**）/ 全量 **5F+2228P+8S@612.79s**——collected 2241=2237 基+4 新恰合零漂移；warnings 2 条=aiosqlite teardown 竞态族基线既有、新测试文件零贡献。
- **install 族=WinError 4551 相位窗（判据链引用面，免重复取证先例第二票）**：092 七腿补强先例「隔离不翻绿」形直接引用+本树三腿补强——隔离腿同五员集/装载腿 `python -m opencode --version` rc=0 v0.0.1（本票改动打包态可装载运行直接证明）/loader 腿同 4551（拒绝早于任何 Python 代码）；零非 spawn 失败模式（§6-4 红线未触）。
- 定向回归腿 **129P**（新 4+test_subworkflow 四判据[自引用深度帽恰 4 作业计数不变=每级前瞻零匹配→fresh 路径零扰动亲证]+test_pipelines 语义门钉面+test_gate_assembly_e2e 092 面）全绿——既有文件零回改零回归双证。
- 调度器独立四门禁（worktree p5-s9 真 python venv，`uv run --no-sync` 形，01:5x-02:1x 时段）：**ruff 0 / format exit 0 / mypy `Success: no issues found in 210 source files`（210-0=209 基+1 新测试文件预期漂移恰合，与席位侧同数）/ pytest `5 failed, 2228 passed, 8 skipped, 2 warnings` @601.01s**——5F 全为 install 族且与席位自跑**同五员集**（今夜窗口持续张开形，与 091「调度器侧翻转绿」形不同：本跑 4551 直接证据在档=相位证据肩在 092/093 两立「隔离不翻绿」七腿补强先例+席位本树三腿补强[隔离腿同五员/装载腿 `python -m opencode --version` rc=0 v0.0.1 打包态可装载运行直接证明/loader 腿同 4551 拒绝早于任何 Python 代码]+派发时窗口闭合负对照[四探针 install 族全绿 2229P exit 0=同窗时变形直接证据]三件）；非 install 面 2228P 零失败（含本票 4 新测，与席位侧同数）；2W=aiosqlite teardown 竞态族基线既有。CI 终证=CI-WATCH-236（云端 VM install 族历史全绿；#233 派发检查点 `4d55bef` CI-WATCH-235 已绿=同基线树 CI 终证先行面）。

### 5. 语义面独立复核 — PASS
- adoption 语义零发明独立核：匹配谓词两字段=JobRecord 既有字面（pipeline_id/depth+lineage.parent_job_id 内建公读面）；收养路径全复用既有 run/轮询/三分支——diff 亲证改动面=入口前瞻块+`handle.job_id→nested_job_id` 变量统一（尾部三分支/返回面逐行比对零语义变化）。
- 终态三分支零改：cancelled→`WorkerSuspended`/failed→`WorkerError` 携嵌套 §8 code/else `{"job_id","status","result"}`——全部仅变量名变化。
- `_implemented()` 五元素全集独立核：subworkflow 旗注入 ctor 既有缝+语义门 else 动态消息自此仅存缺型组合——S2/S3 钉面消息零回归（test_pipelines 全量绿）+092 四元素消息钉测仍成立零改（其自有组合不带 subworkflow 旗，else 分支照发——定向腿亲证）。
- JobLineage/wire/事件/submit 签名零触碰（numstat 亲证=改动面恰 5 文件）——§7 非目标全守（无 broker 持久化/无绑定持久化/无深度帽语义改动/无 drive 重入律改动）。
- 死仿真机械独立核：未标记 `task.cancel()`=静默死纪律（jobs.py cancel docstring 载面）——测试复刻面与票面 §2 钉面吻合（marked interrupt 才是 run 包装层吸收面）。

### 6. 披露与记档 — PASS
- 票面 §5 五条余量全数落位进度档「披露余量」节：收窄残量原文承接/歧义 fail-closed 择形理由+被弃最长者形/broker in-memory by design 承接/五元素全集 else 分支面/M-9 工具级递归观察项（后继评估面）。
- 4551 三腿补强载档（092 七腿先例引用+本树隔离/装载/loader 三腿）——判据链免重复取证先例续立。
- 退出挂起坑 §0-6 落地（装配测 try/finally close 注记 T-C1-091 教训）。

## 发现明细

- **建议 1（观察面，无动作）**：adoption 前瞻每次 body 重入执行一次 `repos.non_terminal_jobs()` 全表读+内存 lineage 过滤（公读面遍归先例同构）——单父子图规模 v1 无瓶颈；后继作业量级增长可评估 repos 层按 parent 的定向读形。
- **建议 2（观察面，无动作）**：fail-closed 角「匹配子零触碰」的善后=§8 恢复扫既有语义（引擎注释亲证「the recovery scan settles them on their own」）——created 态孤儿行由恢复扫非终态重驱收敛，非本票新面。
- **建议 3（观察面，无动作）**：M-9 工具级递归（工人再调 workflow__run 绕深度帽）=PHASE3 评审既有观察项——P5 池尽后下一阶段规划候选（本票 §5-5 承接原文）。

## 附录：席位弧时序

- 01:14:23 发射（四探针全绿后：ruff 0/format 0/mypy 209-0+全量 2229P+8S exit 0 含 install 族——4551 窗当时已闭合，无需判据链=发射门通过）→01:47:58 SEAT-EXIT 0（≈34min 单腿无 blocker）；进度档 01:47:34 先于退出落盘；看哨 TRUE-EXIT @01:48:53 baseHits=0→1 正常自终。
- 单腿无 blocker：adoption 前瞻全四支+公读面+生产接线+契约改写+4 测一次成形。
