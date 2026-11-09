# T-B-056 · P2-A1（T-A-012 文档票）微评审报告

> 席位B · 2026-11-09 · 评审对象 worktree p2-a1 B-baseline `a38d859` vs `34bf0a5`（#166 主树检查点；父链 a38d859←34bf0a5←`5a5511c` merge p2-s0，git log 亲证） · 总裁决 **PASS 0/0/0/1（严重/一般/轻微/nit——本票四档口径）** · 门禁（B 独立复跑·原生前台阻塞承载·fresh temproot `pytest-tmp/p2-a1-b` 系）：ruff check **clean** / ruff format --check **358** / mypy **110**（首试 mypyc DLL 加载失败[同窗口瞬态]→立即重试绿）/ pytest **当日未复现全量绿**——3 次全量失败子集非确定（4F/2F/4F，收集总数恒 1139），run3 全 traceback 落盘取证=**WinError 4551「应用程序控制策略已阻止此文件」**（OS 应用控制策略阻止测试自建 venv 内新构建 `opencode.exe`，非代码面）；隔离复跑该文件 7/7 绿；代码面绿证=A 席同树 1139[259.23s, 1 warning]@11-07+调度器 #166 主树 1139（预核输入）——本票 diff 零代码零测试，定义性不影响门禁（详见 §1-4/§3 发现 A）。

- **评审对象**：worktree p2-a1（分支 p2-a1，单提交 `a38d859`「a-p2a1: ADR-0002 B.2-6/B.4 + ROADMAP L3/L88 stale refresh (T-A-012) - 4 doc spots, decision content unchanged, + progress」）；评审起始 git status 净；diff 面=恰 **3 授权路径 +55/−4 纯文档零代码零新测**（M doc/ROADMAP.md numstat 2/2 + M doc/adr/0002-recovery-coordinator-and-mcp-client.md numstat 2/2 + A doc/orchestration/progress/T-A-012.md 51L）——与调度器预核逐值同。
- **亲读全集**：A 席任务书 43L（三条纪律通知+写权限恰 3+五红线+门禁预期 1139/clean/~356[以实跑为准]/110）+进度 51L（五节戳链+② 门禁 1139[259.23s, 1 warning]/clean/358/110+写后终态复验同+④ 三项记档）+diff 全文（temp 中转 7651B，恰 4 处行对）+ADR-0002 81L+ROADMAP 122L+R5-A1-review.md 48L（形态参照）+全部代码/契约/记档锚独立现读重锚（禁继承兑现）。
- **门禁复核**：四件中三件逐值吻合（check clean/format 358/mypy 110[重试]）；pytest=B 侧环境面（OS 策略阻止新构建二进制），非本票可影响面（§3 发现 A）。
- **总裁决**：**PASS 0/0/0/1**——七面全过；唯一 nit-1=A 席进度首建迟 4m28s（记录级）；发现 A（B 复跑环境面）为 OS 策略面浮报归记档，非本票缺陷非席位行为。

## 1. 七面逐面结论

1. **写面对账 PASS**：恰 3 授权路径（M ROADMAP 2/2+M ADR-0002 2/2+A 进度 51L=+55/−4）；冻结面（src/tests/migrations/pyproject/uv.lock/.github CI/CORE-CONTRACTS/doc 其余含 p2-slices/r5-slices/ORCHESTRATION/doc/reviews 既有件）diff 全空；零 git 写（评审全程 HEAD 恒 `a38d859`，本报告为唯一写面，落盘后终态复验数字照录于终报）。
2. **四翻新面逐句审计 PASS**（新句↔代码实态逐句现读对照）：①**B.2-6（ADR-0002 L60）**——`App.start_mcp_servers`（app.py L282-333：recovery gate 后/session create 前驱动[cli.py L846-853 现读序=L846 gate wait→L851 start→L852-853 每失败一行 stderr]）、每失败 server `(McpConnectError, OSError)` 捕获+boot 继续+失败不注册空工具面、全注册后单次 `mcp_catalog_version()` digest+绑定恒取注入 refs 的 `ref.fingerprint`（app.py L328-330）、close 回收序[§7 L339「依次关闭 MCP、provider、DB」现读原文；app.py L335-338]、`_compose_mcp_manager`（cli.py L517-530）纯注册面保持（docstring 含 P2-S0 spawn-moved 注记）、T8 新名 `test_mcp_composition_registers_and_spawns_definitions_and_closes`（test_cli_r4.py L668）、OP-R5-1 闭合链 #163 ⑩→#165 路 A→#166（并回 `5a5511c`+P2-S0-review PASS 0/0/0/3）、r5-slices §3 行 1 已闭合句（L116 现读）、HTTP MCP=M6 不预写——逐句吻合；②**B.4（L80）**——5 生产测试承载↔test_mcp_production.py 五测现读 L353/462/519/557/618（正常/失败×2/断线零重试/close 序），旧「（§9 B2）」引用删除合理（platform-support L87 现读仍旧态=④-1 记档项，A 席已如实浮报）；③**ROADMAP L3**——可主张面（核心已实现+全量验证 pytest 1139/mypy 110/双 ruff 绿+CI 绿链+「当前基线含 P2-S0 生产 MCP 接线增量」限定语，防与 r5-exit-evidence §8 的 R5 收官值 1134/109 混读，1139=1134+5/110=109+1 算术在册）与不可主张面（已发布/稳定[tag 暂缓 OP-R5-4]/平台无限制）边界正确，状态行刷新尾注循 ADR-0001 L3 先例形（T-A-011）；④**ROADMAP L88**——`官方 mcp SDK`→`自研 MCP stdio client`+行内注记（manager/stdio/bridge 等自研四件；官方 SDK 不采用；MCP server=显式配置 stdio spawn）↔ADR-0002 B 卷决策实态，行内其余技术项零改动。
3. **红线对照 PASS**（五红线逐条）：决策内容零重写（diff 恰 4 处行对，均为现态描述更新，自研 client+恢复协调器决策结构保留）；只记现态零预写（无 HTTP MCP/M6/未来面预写）；门禁新基线 1139/110 零回归（文档票零代码——A 侧② 全绿在案；B 侧见 §1-4/发现 A）；引用行号现读重锚（A 席③ 已测防御清单逐锚核验为真+本评审独立重锚全过）；TS 锚零新引用（触达两文件「TS:」扫描 0 命中，doc 面预期兑现）。
4. **门禁算术+B 独立复跑 PASS（环境面浮报见 §3-A）**：算术闭合——format 353[#165 树]→357[#166 树=+4：c2bdb87..HEAD added 枚举亲证 T-C1-050.md/T-C1-050C.md/P2-S0-review.md/test_mcp_production.py]→358[本票 +1 进度 md]，B 实跑 **358** 逐值同；票面预估 ~356（=355+1，B 评审时点 worktree 口径）偏差 +2=A 席②/④-3 已如实分解（基数未含 #166 主树两 md）。B 独立复跑四件：ruff check clean（exit 0）/format **358**（exit 0）/mypy **110**（首试 mypyc DLL 加载失败=同窗口瞬态，立即重试 exit 0）/pytest 3 次全量均非绿（失败集=install 族+run2 另含预核登记时机性 flake 本尊 1 次；机制取证=WinError 4551 OS 策略面；隔离复跑 7/7 绿；收集总数恒 1139=4F+1135P/2F+1137P/4F+1135P）。
5. **时间戳法证 PASS（nit-1 记录级）**：进度 mtime——Created=2026-11-07 10:46:49 与首行创建戳逐字同（反占位完整满足），LastWrite=11:06:14=末节戳+1s；五节戳链单调（10:46:49→10:55:37→11:06:12-13）。nit-1：A 会话 LAUNCH 10:42:21（logs/T-A-012.txt 首行）→进度首建 10:46:49 迟 **4m28s**（「会话起始即首建」字面延迟；先例 T-B-042 3min41s/T-B-055 2m20s 均记录级）。
6. **④ 浮报充分性+记档面复核 PASS**：A 席④ 三项记档逐项核实为真——①platform-support §9 B2（L87 现读）双过期（生产不 spawn 现态+OP-R5-1「待定夺」均已被 #166 过时化）；②A.2 锚漂移记录准确（app.py 282L→349L/cli.py 1147L→1156L——本评审数组计数实测同；A.2-6 cli.py L844→L846[await 行]；_startup_scan L74→L80）；③format 预估偏差分解如实。强制浮报四档（协议不可达[CI 双 OS]/已测防御[锚逐句]/注记[warning+format 偏差+TS 扫描]/未测防御[无]）齐备无隐藏面。
7. **顺手项与边界 PASS**：无顺手项；授权面外零触碰（ADR-0002 卷首 L3 未动=正确范围纪律——R5 收官历史值 1134/109 快照与 ROADMAP L3 现值并存为观察项，见 §3）；platform-support §9 刷新不在授权面（④-1 已记档归调度器裁量）。

## 2. 处方清单

1. **并回**：`merge --no-ff` 入主树+主树四门禁（merge 时点实跑为准；pytest 若再现 install 族 WinError 4551 同面→按 §3-A 同判处置：非代码回归，隔离重跑定谳+OS 策略面排查归操作者）+台账翻牌。
2. **翻牌**：ORCH 台账 T-A-012 行（调度器）。
3. **条文化**：无。
4. **记档**：①nit-1 首建迟 4m28s（记录级，先例同族）；②B 复跑环境面（§3-A：WinError 4551 应用控制策略族+mypyc DLL 同窗口+platform_probe 预核 flake 本尊 1 次）；③ADR-0002 卷首 L3 历史数字并存观察（非本票缺陷）；④A 席④ 三项既有记档复核在案。
5. **无他**。

## 3. 发现明细

- **nit-1（记录级）**：A 席进度首建迟 4m28s——LAUNCH 10:42:21 vs Created 10:46:49；反占位核心完整满足（Created=首建戳逐字同+LastWrite=末节戳+1s+五节戳链单调）；先例 T-B-042/T-B-055 同族均记录级。无需行动，调度器知悉即可。
- **发现 A（环境面·记录级浮报，非本票缺陷非席位行为）**：B 独立复跑 pytest 当日 3 次全量非绿——run1 **4F/1135P**[275.81s]（install 族×4）+同批 mypy 首试 mypyc DLL 加载失败（ImportError，乱码中文消息可辨识片段与 4551 消息族吻合；立即重试绿 110）；隔离复跑 test_cli_install.py **7/7 绿**[14.29s]；run2 **2F/1137P**[287.92s]（install×1[command_help]+platform_probe×1=预核登记时机性 flake 本尊，run1/run3 该测绿）；run3 **4F/1135P**[260.98s]（install 族×4，全输出落盘取证）。run3 全部 4 失败同一根因：`OSError: [WinError 4551] 应用程序控制策略已阻止此文件`——`_winapi.CreateProcess` 执行测试自建 venv 内新构建 `opencode.exe`（模块级 fixture 产物：uv build --offline --wheel→uv sync --offline --frozen→uv pip install --no-deps）被 Windows 应用控制策略（Smart App Control/WDAC 族）阻止。判定：**OS 策略面非代码面**——本票 diff 零代码零测试零构建配置（numstat 自证）定义性无门禁影响；A 席同树 11-07 全量 1139 绿[259.23s, 1 warning=在册同族间歇面]+调度器 #166 主树 1139 绿（预核输入）；失败子集非确定（4/2/4 变动）排除确定性回归；隔离 7/7 绿；磁盘余量 67.6GB 排除盘满；run1/run2 失败仅存名录未存栈（同测试族+同窗口同族判定，run3 栈为直接证据）；复跑窗口另有非本会话 opencode 进程（PID 3228 @09:30:11）并发在机（旁证非结论）。处置=记录级浮报归调度器：merge 时点门禁同面同判；终端机 SAC/WDAC 排查/temproot 路径排除归操作者裁量，非本票范围非代码面。
- **观察项（不计入四档）**：ADR-0002 卷首 L3 仍记 R5 收官验证值 1134/109（历史陈述形，证据链指向 r5-exit-evidence §8 自洽）与 ROADMAP L3 现值 1139/110（含 P2-S0 增量限定语）并存——两处框架不同（历史快照 vs 当前基线）无逻辑冲突，B.2-6 未引门禁数字故无抵触；卷首不在授权面=本票未触碰正确；后续 doc-hygiene 是否统一归调度器裁量。
