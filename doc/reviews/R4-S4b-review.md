# T-B-042 · R4-S4b（T-C1-039 交付批）微评审报告

> 席位B · 2026-11-06 · 评审对象 worktree r4-s4b `9709aee` vs `3a26876`（main #148） · 总裁决 **PASS 0/0/0/1**

- **评审对象**：worktree `r4-s4b`（分支 r4-s4b，单提交 `9709aee` feat(tests): window-2 started-without-finished synthesizes unknown coverage（T-C1-039, R4-S4b），父=`3a26876`=main 检查点 #148，`git log -1`+`%cI`=**2026-11-06T12:11:23+08:00** 自证 ✓）；diff 面=恰 **2 授权路径 +212/−0**（M `tests/integration/test_recovery.py` **+166/−0** + A `doc/orchestration/progress/T-C1-039.md` 46L），**产品代码零改动**（recovery.py 只读硬条款遵守——name-status 亲证仅 1 M 路径=test 文件，src/ 全零改动）。亲读全集：任务书 26L+进度 46L+`recovery.py` L428-652（窗 2 分支 L533-546+窗 1 L522-532+窗 3 continue L438+批序 L434-484+`_interrupted_message` 全文）+契约 §3.3 L208-221（L212 恢复配方/L216-221 三窗/L219「即使命令可能已经成功」）+新测全文 L486-650+既有 test 1-3/_scan/_projector/helpers 定位图+T-B-040 处方 2/nit-2 原文（在卷）。纪律：worktree 只读零 git 写；唯一写面=本报告；定向复跑 temproot `r4-s4b-b` 自建；全量 pytest/mypy 不自跑（信调度器 1000/102）；止损 30min 未触。
- **写面对账（1:1）**：授权恰 2 项使用恰 2——① 扩容 `test_recovery.py`（任务书双选项之「扩容」，落点决定+理由入进度①：复用 `_Fixture`/`_draft`/`_commit` 面+避免跨模块导入/复制助手；新文件选项未取=「席定其一」合规）② 进度文件 ✓；porcelain 树净 ✓；**numstat 166/0+单 hunk `@@ -483,6 +483,172 @@`=纯插入铁证**（既有 13 测+全部 helpers 逐字零改动；插入位点=test 3 之后主题相邻）✓。
- **门禁复核**：B 定向复跑新测=**1 passed, 1.67s**（0.75s call=真 alembic+SQLite 组合层量级）✓；`--collect-only` test_recovery.py=**14（13 既有+1 新）** ✓；`ruff format --check .`=**302 files already formatted（退出码 0）**——B 第三方精确同值 ✓（=main#148 之 301+1 进度 md，T-B-041 nit-2 语义[py+md 合计]正确适用；且 C 本票门禁自跑于进度文件落盘[11:50:43]之后=终态计数，T-B-041 nit-1 机制的正确形态）；`ruff check .`=clean ✓（B 复跑）；**1000=999+1** ✓、**mypy 102 N=0** ✓（ls-tree 亲证 3a26876=104=HEAD .py 恰零新增）。
- **总裁决**：**PASS——放行并入。阻塞 0 / 严重 0 / 轻微 0 / nit 1**（nit-1 进度文件首建晚于起始 3min41s——数值仍为可能真读、全部戳≤mtime、止点在=**时间戳纪律合规、复发条款不触发**（T-C1-037 前科后连续第二票合规）；窗 2 分支首次被执行且逐句钉死、红线三面全守、④ 诚实度全绿——不阻并入）。

## §1 六维逐维（每维裁决+亲证）

1. **窗 2 覆盖真实性 PASS**：**确定性构造**——直拟日志形态给出崩溃窗（py.tool.started 已提交 L590-598、**无 py.tool.finished**，L599 注释「the crash point: no py.tool.finished ever commits (§3.3 L219)」钉死崩点）；折叠态=RUNNING 非终态→`_tool_terminal` 走 L533-546 unknown 分支（若漏 started 则折叠 PENDING 走窗 1 分支——构造面区分有效）。**断言逐句钉**：① 合成物事件数据（L613-628 直查 events：恰 1 条 py.tool.finished+`state=="unknown"`/`effect_status=="unknown"`+content f-string「Tool execution {tool_id} was cancelled while running; its result could not be confirmed.」与 recovery.py L539-544 **逐字符同构**）② 报告三面（L606-609：outcome=recovered/closed_runs=((rid,"running"),)/tool_terminals=(tool_id,)/step_terminals=(step_id,)）③ 批内位序（L633：`types[-3:]==[py.tool.finished, py.step.finished, py.run.finished]`=§3.3 L212 工具终态先于 step/run 终态）④ run→interrupted（L602 预扫 running+L634 终态 interrupted）⑤ 投影落点（L638-649：interrupted 消息已投影+`parts[0]` 文本 part **逐字保留** `{"type":"text","part_id":"p1","text":"hello world"}`+`parts[1]` 工具 part unknown/unknown+content 与合成事件数据一致）✓。构造诚实=真 alembic subprocess+SessionStore/EventCommitter 组合层+同一 `_Fixture` ids 贯穿构造与 `_scan`（T-C1-037 计数器碰撞教训吸收）✓；B 复跑绿 1.67s ✓。
2. **红线三面 PASS**：① recovery.py 及一切 src/ 零 M（name-status 仅 1 M=test 文件——只读硬条款遵守；「若新测暴露缺陷→停下浮报」未触发=新测零产品缺陷暴露）② 既有 13 测+helpers 零改动（numstat **166/0**+**单 hunk** 纯插入=git 级铁证，无任何行内修改）③ 写面与授权 1:1（恰 2 路径=任务书授权恰 2 项，落点决定在案）✓。
3. **时间戳纪律 PASS（带 nit-1；T-C1-037 前科后第二票）**：mtime **11:58:57** ≥ 止点 11:58:48（+9s 经典收尾形）✓；全部自称戳（起 11:47:02/① 11:49:44/②③④⑤ 11:58:07/止点 11:58:48）**≤ mtime=全部可能真读，无不可能值**（T-C1-037 反模式不在场）✓；止点单行在 ✓；跨度 11min46s<30min 止损 ✓；**全链时序自洽**（worktree checkout 11:43:23[测试文件 CreationTime]→会话起 11:47:02→进度文件建 11:50:43→测试终稿 11:52:34[含 finalized 修正]→全量门禁[220.59s≈3.7min 自洽]→② 11:58:07→止点 11:58:48→mtime 11:58:57→提交 12:11:23[调度器侧]）✓；CreationTime 11:50:43 晚于起 3min41s（nit-1，轨迹较 T-B-041 之 17.5min 改善）——**复发升严重条款不触发**。
4. **门禁算术独立复核 PASS**：**1000=999+1** ✓（B collect 14=13+1 佐证增量恰 1）；**mypy 102 N=0** ✓（ls-tree 104=104 恰零新增 .py）；**format 302** ✓（B 第三方同值；=301+1 进度 md，nit-2 语义正确适用+门禁时点在终态树=C 的 302 为终态真值）；ruff check clean（B 复跑）✓；调度器全量 1000/220.59s 与 C 一致（信调度器）✓。
5. **④ 诚实度 PASS**：飞行中自误披露**亲证为真**——进度② 明记「首轮 13 passed+1 failed：新测 fixture 漏 py.text.finalized（state.py `_require_finalized_match`——测试构造缺陷非产品缺陷，修正后绿；如实记录）」；B 亲读证实修正后在卷（L562-568 py.text.finalized text="hello world" 与 generated 消息文本 part L511 逐字匹配）、失败模式与 state.py 校验面吻合、定性正确（构造缺陷非产品缺陷——产品零改动即可绿）；③ 浮报 6 项含诚实不超报（本测无幂等断言/无 closed-first 全序=仍由 test 3 承载/kill 注入缝与副作用可观测性 protocol-unreachable 如实声明）✓；无其他未浮报项迹象（B 复跑绿+代码路径分析无隐藏失败面）✓。
## §2 处方 2 对账（T-B-040 原文 L40「同票顺手三项」——调度器据以定夺 b/c）

- **a. generated 分支 interrupted 消息工具 part 终态直接断言——已承载（闭合）**：新测投影断言组 L636-649 即该项落地——text part 逐字保留+tool part `state=="unknown"`/`effect_status=="unknown"` 终态**直接断言**+content 与合成事件数据一致（经 `_interrupted_message` L583-596 synthesized 应用面）；T-B-040 Minor-3 同源的「generated 分支工具 part 终态无直接断言」缺口就此闭合 ✓。
- **b. 恢复后对 closed permission 的 reply 显式拒断——仍为零覆盖缺口（未闭合，已被正确上浮）**：本票任务书授权面=窗 2 补测（不含此项），C 依授权边界未实施+③-6/④-1 如实上浮 ✓；结构性保证仍在（closed=terminal、迟到 reply 折叠拒——state.py L966-975，T-B-040 在卷）但「恢复后直接 reply 被拒」的显式行为断言仍缺。**B 建议：并入 S6 任务书**（一行级断言：恢复完成后对已关 permission 发 reply→断言拒/不激活；S6 触及恢复交互面时顺手，不值得独立微票）。
- **c. test 11 阻塞期读面一行断言（T-B-040 nit-2）——仍为零覆盖缺口（未闭合，已被正确上浮）**：本票单 hunk 纯插入不触 test 11 区域（L904-975 零改动亲证），C 如实上浮 ✓；结构性证明独立成立（SessionStore 零读方法+repos 零门引用=读面物理无从触门），仅缺阻塞期一行 run_status 读调用的显式断言。**B 建议：并入 S6 任务书**（一行断言即闭，与 b 同票顺手）。
- **对账结论**：处方 2 三项之 a 闭、b/c 开且经正确流程上浮（授权边界纪律+如实浮报双合规）——b/c 归调度器裁量，B 侧建议已给（并入 S6，一次闭合）。

## §3 nit 清单

- **nit-1（进度文件首建延迟 3min41s）**：CreationTime 11:50:43 vs 首行自称起 11:47:02（① 11:49:44 亦先于创建 59s）——数值可为真读（全部戳≤mtime 无不可能值）、止点在、跨度真=不构成违约、**复发升严重条款不触发**（T-C1-037 前科后连续第二票合规）；T-B-041 处方 4「起始即建文件写首行」部分遵循（轨迹改善：17.5min→3min41s）；口径维持记录级，无新处方。

---

**终裁行：PASS——放行并入。阻塞 0 / 严重 0 / 轻微 0 / nit 1（N/N=0/1）。**

**处置建议（调度器）**：并回 r4-s4b → main（2 路径 +212/−0，零产品改动纯测试新增）；并回后期望基线 **pytest 1004 / mypy 103 / format 305**（main fe70bda=1003/103/304[105 py+199 md] 之上 +1 测/+0 py/+1 md；以并回实跑为准入台账）；b/c 两缺口并入 S6 任务书一次闭合（§2 建议）。
