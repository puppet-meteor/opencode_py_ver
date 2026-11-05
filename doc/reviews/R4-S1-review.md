# R4-S1 评审报告（席位 B · T-B-035）

- **评审对象**：worktree r4-s1 B-baseline `eb01698`（单提交 "fix(tools): post-start failures map to unknown + rg skip semantics (T-C1-033)"）vs 基线 `38d72ad`（=main 检查点 #133 文档提交）。
- **写面对账**：恰 10 路径 ↔ 任务书 10 授权项 1:1（name-only+numstat 亲证，+286/−47：progress 84/0、maas 4/4、process/runner 7/7、session/runner 8/8、grep 37/9、invoker 14/11、test_tools 1/1、test_e2e_r3 91/3、test_tool_read 2/2、trio 38/2）；冻结面 diff 亲证**为空**（contracts/service/state/projector/context/storage/policy/registry/config/app/cli/bus/write/edit/ls/glob/read/bash/pyproject/uv.lock 全集）；树净、单提交、本评审零 git 写（报告落主树=唯一写面，提交归调度器）。
- **门禁复核（沿 T-B-034 模式，不复跑全量）**：C 报 pytest **924**（922+2 零回归+新增 2）/ruff check clean/format 274 files exit 0（计数非判据）/mypy **92** 零问题（零新文件——两新测均落既有文件，自洽）。B 亲跑 collect-only=**924 collected**（1.24s，逐文件计数求和=924）；基线函数计数亲证 trio 36→37、e2e_r3 3→4，恰 +2 ✓。工件在册：`%TEMP%/opencode/pytest-tmp/r4-s1/`（rg_probe.py 10:33:32、rg_probe2.py 10:34:33、pytest-of-lyu 10:57:36=门禁 210.18s 全程跑尾迹）。
- **前评兑现**：S8 open-1（修复批补「优雅失败→run 明确终态」回归钉）=本票 ④ 钉；S8 nit-2 文件清单（maas+session/runner+process/runner 共 19 处）=任务 C 零偏差兑现。
- **结论：PASS（放行并入）—— blocker 0 / major 0 / minor 0 / nit 2**（nit-1 代码记录级/nit-2 过程保障级，均无返工；开放点五项不预写裁决）。

## §1 十二维逐维

**维 1 · 契约锚定**：§2.1 L50 原文亲证——迁移表行「tool.finished：异常/取消 | running → failed/cancelled | **由 adapter 证明，否则 unknown**」：改射=该行的 invoker 侧兑现（started 已证明调用开始，其后失败 effect 不可证为 not_started；invoker 两 locus 新注释逐字引 §2.1 L50）。§5.2 L270 原文亲证——「对 unknown 执行相同输入的重试必须重新 ask，不能复用旧 once/always」：record_unknown 无条件化使全部 post-start 失败落入该规则（点名①）。迁移表 L43-51 全表读毕：pre-start 面（L43/L44/L46/L47/L48，effect=not_started）与 post-start 面（L49 completed/L50 unknown）分界清晰，改射未跨线、未漏行。§2.1 L53「succeeded 只表示工具报告完成」=run 终态锚（必答①）。state.py `_apply_tool_finished`（进度引 L827-837）：running→failed 合法 effect 仅 {completed, unknown}——原 failed/not_started 组合自 fold 即非法；修复=停止产出非法组合而非放松 fold，方向正确。

**维 2 · invoker 改射忠实度**：恰两 locus 亲证（diff 原文，任务书锚 L1092-1097/L1119-1128）：
① crash 路径（`except Exception` 分支）：`effect: Literal["not_started","completed","unknown"] = ("not_started" if not side_effects else "unknown")` ternary 整体消亡（含 Literal 注解），`_finish(effect_status="unknown")` 常量直入；`if effect == "unknown" and policy is not None` → `if policy is not None`（record_unknown 无条件化、policy 守卫保留）；注释引 §2.1 L50+记 T-C1-033 楔死链（failed/not_started 被 fold 拒绝→楔死）。
② 优雅路径（`result.error is not None` 分支）：同形（ternary 消亡、常量、policy 单守卫、§2.1 L50 注释）。
HEAD 全库 `not_started` 对账：BASE 21 行 → HEAD 21 行 = 移除恰 3 行（crash ternary 2 行+graceful ternary 1 行）+新增 3 行注释（L1096/L1099/L1128，纯 prose）；**8 个 pre-start 代码位点**（L631/717/763/809/845/1001/1021/1041，§2.1 L46/L47/L48 诸面）+2 处三值 Literal 类型域（L194/L518）+8 行既有注释/docstring 逐字保持——pre-start/post-start 分界**零误伤**。invoker 仅 2 hunks（diff 亲证），无第三位点 ✓。

**维 3 · state.py+fold+契约表零改动**：冻结面 diff 空（亲跑全集输出零字节）；CORE-CONTRACTS.md/state.py/service.py/projector.py 均不在 10 路径内。修复=纯 invoker 侧改射，fold 拒绝逻辑/契约表/状态机零动，与进度声明一致 ✓。

**维 4 · grep 三诊对拍忠实度**：TS L138-146 亲读：`Stream.take(limit+1)` → `truncated = rows.length > limit` → **truncated 先 return、exitCode 后读**（截断根本不进诊断）；诊断链 L146-152。Python 新序（diff 原文）：`early_stop = len(matches) > limit` → `if not early_stop:` 包裹全部诊断 → `returncode == 2 and _is_invalid_pattern(stderr)` → "Invalid regex pattern" → `returncode not in (0,1,2)` → "Unable to grep" → 落空容忍渲染 rows。逐点对拍：early_stop 豁免序=TS truncated 先 return ✓；code 2+banner=pattern 错 ✓；code∉{0,1,2} 硬失败 ✓；其余 code（1=空 rows 渲染 No matches found、2 非 pattern 错=锁文件）容忍 rows=TS "otherwise" 支路 ✓；detail 现仅取 stderr（原 f-string fallback 移入下一分支——code==2 时 stderr 必有 banner）✓。

**维 5 · flag 对齐面**：args=`[rg, --no-config, --json, --no-messages]`+可选 `-g/-F/-i`+`-e pattern -- file`——与 TS L224/L227 对齐；`--no-config`（config 隔离）/`--no-messages`（stderr 噪声抑制）均无结果集变化（本机无 rg config，实证零干扰；**banner 在 --no-messages 下仍达 stderr=rg 15.2.0 直跑实证**：exit 2+`rg: regex parse error:`——诊断可达性成立，TS InvalidPatternError 同款前提）。`--hidden`/`--glob=!**/.git/**`（TS L226/L231）未实施=**parked 正确**：任务书 B2 硬上浮，代码内注释钉记（L361-364）+docstring 如实披露偏差 ✓。

**维 6 · 去行号化对账**：19 处=maas §8 L350×4（L11/L35/L169/L289）+session/runner §8×8（L30/L42/L336/L587/L681/L774/L800/L999）+process/runner §5.3 L277×7（L1/L3/L20/L28/L103/L167/L198）——与任务书/调度器亲证计数零偏差（进度对账同）；diff 亲证全部为注释/docstring 纯文本（§8 L350→§8），**零行为** ✓。残留全扫：`L277` 全库恰 7 处且全在冻结面（interpreter.py L45、winjob.py L1/L15、bash.py L8/L74/L233/L287）未触碰 ✓；`L350` 全库零残留 ✓；三授权文件零残留 ✓。

**维 7 · 五钉子+pre-start 回归**：五钉子=断言更新、测试数零增减：contract `test_adapter_exception_becomes_failed_result`（L560 effect not_started→unknown；state/error.code/RuntimeError 文本断言保持）+read 二测（binary L334 元组 `(failed,unknown)`；invalid_utf8 原无 effect 断言→钉入元组——error.code/UTF-8 文本保持）+trio 二测（grep binary/rg unavailable：元组 `(failed,unknown)`——诊断文本断言保持）。tests 全库 not_started 对账：**BASE 55 → HEAD 55** = −4 钉子断言行（test_tools 10→9、test_tool_read 1→0、trio 2→0）+4 行新 prose（④钉 docstring/module docstring 描述 pre-fix 缺陷，零断言）；**其余 12 文件逐文件计数 byte-identical**——pre-start 断言面零漂移（C 报「pre-start 库存面 tests 内 37 处」为其分类计数；B 对账给出更强的性质级结论：无一处 pre-start 断言被触碰）。

**维 8 · B3 实证测试质量**：`test_grep_locked_sibling_file_is_tolerated_not_failed`（trio:560）：`msvcrt.locking(fd, LK_NBLCK, 1)`@offset 0 真锁（db.py writer.lock 同款形状；os.open fd+finally unlock/close）；断言 `(succeeded, completed)`+精确文本 `"Found 1 matches\nreadable.txt:\n  Line 1: needle here"`——**实测非假设**（前置探针 rg_probe.py 五组在册：unlocked rc=0 双命中；locked+新 flags rc=2 命中保留 stderr 空；locked+旧 flags rc=2 stderr os error 33；locked+nomatch rc=2 rows=[]；bad-regex 含 --no-messages banner 在）；A-PLATFORM 披露 ✓（skipif win32+运行时守卫+mypy 窄化注释+docstring 声明 Unix 腿不伪造证据留 CI）；`_real_rg_or_skip`=VS Code bundled rg（探针同源解析，直跑 15.2.0）✓。

**维 9 · 回归钉质量**：`test_post_start_graceful_failure_reaches_run_terminal`（e2e_r3:670）四面断言亲证：①终态 `_await_terminal` succeeded（楔死面回归钉——pre-fix 该 poll 永不达终态）②store：tool part `(failed, unknown)`+收尾 assistant 文本+idle(succeeded) ③wire：第二请求 tool result 含 `"File not found: missing.txt"`（§3.3 失败可见性/回灌面）④EventLog：`py.tool.finished`=(failed,unknown)、`py.tool.started`=1、`py.run.finished`=1（真实 started+fold 接受+run 收口）。载体裁量合理（fs__read missing path=post-start 优雅失败、全平台确定性、无 rg/审批依赖；crash 路径由契约钉覆盖——进度③如实浮报）。run 终态形态依据=runner.py L620-667 亲证+§2.1 L53+test ③ 先例（必答①）。

**维 10 · nit-1 等值正确性**：`assert _content_str(allow_results[2]...) == "note.txt"`+注释更新（DB 及 writer.lock 居 db_dir 兄弟目录、tmp 恰 note.txt）——等值成立（外置后 ls 渲染面唯一文件）；进度报实测绿 ✓。原 containment 断言的陈旧注释一并修正 ✓。

**维 11 · 写权限面+覆盖对账**：见头（10 路径 1:1、+286/−47、冻结面空、树净、零 git 写）。924=922+2 算术三重闭合：collect-only 亲跑 924+逐文件求和=924+基线函数计数（trio 36/e2e_r3 3）→HEAD（37/4）恰 +2；mypy 92=零新文件自洽。

**维 12 · ③漏报抽查**：文件级 1:1+冻结面 diff 空+invoker 恰两 locus（维 2）+not_started 全库分解（维 7）+L277/L350 全扫（维 6）+collect 逐文件对账（维 11）——授权范围内**零漏报、零越界**；授权外发现=7 处冻结 §5.3 L277 残留（正确不动，处方 2）+`_drive_run` 吞没面（票面明确划出，处方 4）。

## §2 点名五项立场

**① record_unknown 无条件化的 re-ask 扩面——concur。** §5.2 L270 语义=unknown-effect 重试必须 re-ask；原实现仅 side_effects=True 工具走 unknown，side-effect-free 工具 post-start 失败记 not_started=宣称「无副作用」——但 started 后失败（crash/优雅）的副作用不可知（§2.1 L50「由 adapter 证明，否则 unknown」），这正是行#34 楔死根因（failed/not_started 自 fold 非法→`_drive_run` 吞没→永久 running）。扩面代价=无副作用工具失败后重试须 re-ask 审批（不复用旧 once/always）——保守方向，与 L270「宁 re-ask 勿复用」精神一致；副作用面影响=bash 等原 side_effects=True 面行为不变（进度亲证）。TS 对拍：TS 无 effect 台账——工具失败以 error part 回灌循环、模型决策重试；Python 的 unknown 台账+审批 re-ask 是该形态在事件溯源+审批架构下的同构物（失败信息回灌+不自动复用旧决定），语义面等价。

**② code 2 容忍与 early_stop 豁免序——正确。** TS L138-142：`Stream.take(limit+1)`→truncated 先 return（exitCode 后读=截断根本不进诊断）；Python `if not early_stop:` 包裹全部诊断=同构——kill 非零码（win32 taskkill/POSIX 信号）在两实现均不落码诊断。诊断链序 pattern 错（code 2+banner）→code∉{0,1,2} 硬失败→容忍，与 TS L146-152 逐点一致：pattern 错优先消费 code 2，其余 code 2 落「otherwise」容忍支（锁文件形状）。

**③ run 终态——concur succeeded-with-failed-tool**（依据与独立立场见必答①，同一裁量的两面）。

**④ `_is_invalid_pattern` .lower() 超集面——可接受（nit-1 记录级）。** TS L100-101 字面小写双 banner；Python `.lower()`=大小写不敏感超集。实证：rg 15.2.0 直跑+探针同证 banner 恒小写（`rg: regex parse error:`）且 --no-messages 下仍达 stderr→现实等价；理论超集的误报方向=把含大小写变体 banner 的 stderr 认作 pattern 错（报 Invalid regex pattern 而非 Unable to grep——同 TOOL_FAILED 用户可修正面，损失轻微）。docstring 如实披露（"Python-side superset, float-reported"）✓。另注：TS 有 `input.pattern &&` 前置（防 pattern 可选的 glob/find 复用面误判）；Python grep schema 恒有 pattern→前置冗余、等价性成立（记档；若后续将 helper 复用于 pattern 可选面则需补）。

**⑤ --hidden/--glob 上浮连带面——零连带，输入见必答②。** 事实：Python 全库仅 grep.py 调 rg；glob.py=stdlib `iglob(..., include_hidden=hidden)`（L157，hidden 入参门控 L90/L138）+ls.py=dotfile 过滤（L159，hidden 入参门控）——TS glob/find 的条件 --hidden 语义已以原生形态实现（R3-S3 sanctioned 偏差：stdlib 无 .gitignore 感知，量化披露在案）。故裁决实施面=**grep.py args 一行**，glob/ls 零连带。

## §3 必答三项

**① run 终态形态独立立场——concur succeeded-with-failed-tool**（run outcome=succeeded、tool `(failed,unknown)`；④钉四面断言恰钉此形态）。依据三叠：runner.py L620-667 亲证（`_run_tools` 将失败工具结果折入 parts→step.finished(completed)→喂下一请求；无 tool part 的生成才 run.finished(succeeded)）；§2.1 L53 原文；test ③ 先例（cancelled/not_started 工具后 run 仍 succeeded）。failed-run 会混淆两层语义：工具失败=模型可见反馈数据（§3.3 每个合法 tool_call_id 必须有对应 tool result——④钉 wire 断言正是此面），run failed 保留给基础设施面（adapter/协议 fold 违例）；且 failed-run 将截断 re-ask 循环、与 §5.2 L270 相悖。

**② --glob/--hidden 裁决输入（给建议不裁决）：** 事实面——TS grep=无条件 `--hidden`+`--glob=!**/.git/**`；TS glob/find=入参门控 --hidden；Python grep=rg 默认（隐藏不搜；rg 默认跳隐藏目录故 .git 已天然排除、--glob 在无 --hidden 时冗余）。真实结果集偏差=非 .git 隐藏文件（.env/.dotfile 等）：TS 搜、Python 不搜。**建议：对齐 TS grep 面**（加 --hidden 必须同票加 --glob=!**/.git/**，否则 .git 内匹配涌入；实施=grep.py args 一行；建议补两回归=隐藏文件命中+.git 排除；既有 trio/e2e 断言零影响——无隐藏/.git 场景，进度同判）；glob/ls 不随（原生 hidden 入参门控已是 TS 形状）。备选=维持现状并作为量化偏差常驻差异表（docstring 已如实披露）。倾向=对齐（忠实度收益明确、TS 侧 grep 用户语义=搜隐藏文件）。附带说明：对齐后 grep（硬开）与 glob（门控）的 hidden 非对称=TS 同款非对称，非新偏差。

**③ 27min 单段过程完整性+WDAC 处置：** WDAC 合规——本树 .venv 若以 uv 受管 CPython 3.12.16 为 base 则 trampoline 被 WDAC 策略阻（os error 4551）；C 按 r1-s8/主仓既有形态 `uv sync --python D:/miniconda3/python.exe` 重建，**三仓 pyvenv.cfg 同源亲证**（home=D:\miniconda3、3.13.12、uv 0.13.6）——基线 922/92 历史口径即该形态采集（r1-s8 同源实证），门禁可比性成立；进度如实披露并上浮调度器（④-5）。过程完整性：本轮**无独立 watcher 留痕**（树净、无 logs/ 对应物——对比 r3-s8 有调度器 watcher 自建）；B 佐证链=工件时间戳（rg_probe.py 10:33:32→rg_probe2.py 10:34:33→pytest-of-lyu 10:57:36=210.18s 门禁尾迹）+diff 形状与任务书逐项零偏差+门禁数字自洽（924/274/92、922+2、92 恒定）——一致性证据充分、无反证；如实记 nit-2（保障级，不扣分）。

## §4 处方清单

1. **（调度器裁决，本票上浮必达项）** `--hidden`/`--glob=!**/.git/**`：建议对齐 TS grep 面（输入见必答②）——实施=grep.py args 一行+两回归用例（隐藏命中+.git 排除）；glob/ls 零连带。
2. **（后续票授权候选）** 冻结面 7 处 §5.3 L277 去行号化：interpreter.py L45、winjob.py L1/L15、bash.py L8/L74/L233/L287（超出本票写授权，正确未触碰）。
3. **（下票 doc 批）** e2e ①②③ "DB must live OUTSIDE working_dir" 注释随容忍语义改写（⑤排除面本票不动；可与 evidence §6 L76 句修正同批）。
4. **（R4 恢复面票）** `_drive_run` 吞没面收口（本票明确划出；改射后吞没路径降为纵深防御）。
5. **（调度器环境策略）** WDAC venv 基址统一（新树首跑会再遇 4551）。
6. **nit-1（记录级）**：`_is_invalid_pattern` 无 TS `input.pattern &&` 前置——grep schema 恒有 pattern 等价成立+已披露；若 helper 后续被 pattern 可选面复用则补前置（无需当前行动）。

## §5 nit 清单

- **nit-1（代码·记录级）**：`_is_invalid_pattern` 的 `.lower()` 超集+无 `input.pattern` 前置——docstring 披露+现实等价（rg 15.2.0 banner 恒小写直跑实证）；无返工。
- **nit-2（过程·保障级）**：27min 单段无独立 watcher 留痕——工件时间戳链+门禁数字自洽佐证、无反证；建议后续票恢复过程留痕（调度器 watcher 面或进度附时间戳）。

## §6 开放点（不预写裁决）

- OP-1：`--glob`/`--hidden` 裁决（=处方 1）。
- OP-2：第二 banner "error parsing regex" 无当代 rg 实例（15.2.0 只产第一 banner，直跑+探针同证；TS 兼容保留，无行动）。
- OP-3：B3 Unix 腿 CI/Linux 复验（A-PLATFORM 纪律，进度③如实浮报）。
- OP-4：code∉{0,1,2} 分支无真实实例（rg 崩溃/异常信号面；fail-closed 保留）。
- OP-5：`record_unknown` 的 policy-None 跳过面未织专测（R2 默认面设计跳过；进度③如实浮报）。
