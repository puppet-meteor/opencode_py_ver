# T-B-070 · 席位B微评审——T-C1-061（四测试面定点稳定化微票）

- 评审时刻：2026-11-10 18:18:38（现读时钟，先读后落盘零占位）；评审者=席位B（ses_orch_B2 串行会话）。
- 对象：worktree p2-s9 HEAD a92fbff（父 c4c14bf=main 现 HEAD 17:09:08，git log -1 亲证）；恰 4 路径 3M+1新 +279/−15（B 复读 numstat：进度件 91/0 新增+test_httpapi_server 113/10+test_mcp_stdio 8/2+test_sdk_client 67/3 加法算术闭合；porcelain 0 行净树=无第 5 路径）；冻结面零触亲证（src/**、.github/、pyproject、uv.lock、conftest/pytest 配置面均不在 diff；三处同名 helper 拷贝所在文件 test_e2e_r4/test_mcp_production/test_httpapi_sse 零触）。
- 方法：六面独立复核（票面「调度器已验项免重复」条款引用：git 状态面+三文件 diff 逐行亲读+进度件数字与行号锚+调度器 17:52-17:58 四门禁复跑；B 独立面=face ② 四环证据链 src 只读逐环复核+diff hunk 头独立核+①③④ 落点现读对拍+四门禁 B 独立复跑）；B 零 git 写（起止 HEAD 恒 a92fbff）；temproot=p2-s9-b（调度器预建）；报告=唯一写面。

## 六面结论

1. 用户裁决面对照：PASS——仅四已观察测试：①③④ 所在 helper/测试触改+face ② 零改动（hunk 头亲证：httpapi 四 hunk 全在 L29-37 import 区与 L1364+ helper/证明区，L1210-1235 零 hunk；mcp_stdio 单 hunk 恰 L108-121；sdk 单 hunk 恰 L626+）+2 件可注入性证明测试（C 票明文授权面）；仅测试侧：src/** 零触；零 CI 面：.github/pyproject/uv.lock 零触+重试全为测试内 helper 非配置级 retry 面（「反对 CI 步级自动重试」=零自动重试机制落地核实）。
2. 四 face 机制读法+硬化选型 vs 实装：PASS——①默认窗 10→30 选型成立（8 调用点全零参一致继承 vs 显式传宽值留 6 同构暴露面于窄窗的取舍论证在册；绿跑早退零成本+真泄漏同 assert 仍红；L111 现读=30.0）；③连接级判定面精确（_is_connection_abort L1369-1375 仅 Abort/Reset 及 URLError 同因包裹形；HTTPError 任意状态首答即返 L1413-1417；非中止 URLError/耗尽原样再抛 L1418-1420；3 发+0.25s×attempt 线性退避；恰 4 调用点位参零改）；④类层级论证成立（仅捕 OpencodeConnectionError[ClientError 子]；OpencodeApiError 姊妹零捕获首答即穿；OpencodeStreamError L53-58 不可达于 401 探针[无 200 前置]定谳合理；流面 lambda:next(wrong.stream_events(...)) 每尝试整条重发=懒连接语义正确）；② stop-and-escalate 定谳链 B 逐环独立复核成立：R1 中间件 L100 预算 8+L440-448 第 9 个 cancel 即答 429 SLOW_CONSUMER retryable=True 不进内层；R2 service L884-886/L905-907 终态快径+L914-918 八泊同一 run task；R3 runner L934 工具边界唯一 is_set 消费+L945 ToolContext 传递+B 全文件 grep 无其他消费点；R4 测试 L1216-1217 纯 text+gate、_Gate L213-220 asyncio.Event 唯测试可开——9-done 唯一可达读法=run 已提前终态（src 侧异常候选），硬化反会在「429 先落地+八快径作答」子情形把暴露异常的红转绿=触红线，零改动交付+上浮处置正确。
3. 零断言弱化红线：PASS——face ② 整测试零行（hunk 亲证）；① assert 语句/消息零改（单 hunk 边界止于 docstring，assert 在不变区）；③④ 存量断言逐字保留（调度器 diff 级亲证引用+B 落点现读对拍：④ L654/L663 pytest.raises(OpencodeApiError) 双面保留+L656-659[status_code==401+PERMISSION_DENIED+retryable is False]+L665-667 逐字+L660 良好 client 面 ref=_create 零触；③ 401 断言面/六命令面/单 worker/关停断言调度器逐字亲证）；确定性违约仍红（③④ 证明测试恒抛模式 call==3 耗尽再抛在册）。
4. 可注入性 TDD 证据：PASS——③ 四模式（N-1 中止后成功/恒中止耗尽 call==3/HTTPError 零重试 call==1/非中止 URLError 零重试 call==1）+④ 三模式（N-1 连接错后成功/恒连接错耗尽再抛/OpencodeApiError 零重试 call==1）证明测试落授权文件（B 亲证锚：httpapi L1443+sdk L670 现读命中）；不可注入者（① 真慢树杀/④ 真 10053）结构论证档；URLError 包裹形注入子模式回退事实核实成立（交付态含 _is_connection_abort 第二析取支=与已测 raw 形同路径结构论证，注入用例未落地如实入未测防御档，交付字节=最后验证态一致）。
5. 门禁+算术独立复算：PASS——B 复跑：收集 1426=1424+2 恰合+install 族外 1420P+2S=1422/1422（对 T-C1-060 同环境 1418P+2S 恰 +2=两新证明测试，存量零回归）+ruff clean+format 425（=424+进度件 .md）+mypy 130/0；修后语法面（TypeVar→PEP695/stdlib HTTPError 类型/死代码 close() 移除）零行为差论证+定点复跑绿记录（sdk 2P @1.81s/httpapi 2P @2.91s）在册一致。
6. 冻结面+写权限+浮报完备性+席位纪律面：PASS——恰 4 路径 vs 授权清单（三测试文件+进度件）1:1；③④ 浮报四档完备性反向抽查两疑点消解（③ L1422 尾行 AssertionError("unreachable")=attempts≥1 语义下不可达防御死码[循环每支或 return 或 raise]，仅 attempts≤0 病态入参可达且为响亮失败非静默绿；④ helper 仅捕 OpencodeConnectionError，KeyboardInterrupt/CancelledError[BaseException 面]天然穿透不吞）；进度件与交付态逐锚一致（L111/L1369-1375/L1378-1423/L629-650/L652-667/L670/L1443/L1497 B 现读全中）；席位纪律如实性（close-out 收尾+语法修+URLError 子模式回退记录）与交付 diff 一致——如实记档成立非惩罚面。

## 独立复跑（temproot p2-s9-b，uv run 口径）

- pytest 全量：1423 passed+2 skipped+1 failed @338.31s（收集=1423+2+1=1426 恰合；判据面=install 族外全绿 1420P+2S；2 warnings=PytestUnhandledThreadExceptionWarning/Event loop is closed 间歇族观察面，C 轮同族在册）。
- install 族处置（#167 口径）：1F=tests/integration/test_cli_install.py::test_installed_command_help_is_zero_side_effect_exit_0（WinError 4551 WDAC 环境面）；B 隔离重试=4F/3P @4.18s 亦红——分钟级翻转链续录：C 轮 {全量 4F @342.10s→隔离 4F/3P @5.54s}→调度器 {全量 4F @328.69s}→B 轮 {全量 1F→隔离 4F}；本 diff 零 install 面触碰+隔离同构+T-C1-060 clean-baseline 对照组先例=环境面定谳成立，CI 双 OS 终证承载。
- ruff check：All checks passed!（exit 0）。
- ruff format --check：425 files already formatted（exit 0=424+进度件 .md；与 C 席/调度器逐值同）。
- mypy：Success: no issues found in 130 source files（exit 0）。

## 终裁与处方

- 终裁：PASS——阻断 0/重大 0/次要 0/建议 1。
- 建议1（纯防御微面，零行为缺陷）：③④ 重试 helper 尾行 raise AssertionError("unreachable") 仅在 attempts≤0 病态入参下可达（attempts≥1 语义下循环每支或 return 或 raise，尾行不可达）——现状为响亮失败非静默绿无缺陷；可随任意后续票 rider 加 attempts<1 卫兵或 docstring 注明前置，收口该可达面。
- 处方（供调度器执行）：①并回（p2-s9 merge --no-ff 入 main 为 #181+主树四门禁复跑+temproot main-181 预建）②翻牌（ORCH 台账 T-C1-061 DONE+票 md 51L+进度 91L+本报告随 merge 入库）③face ② 上浮处置（B 背书 C 定谳链：src 侧 run 提前终态异常候选挂候选册「再现即排程」纪律 triage 票+携 Temp/opencode/ci-177c-failed.log）④记档三则（WDAC 翻转链 B 轮续录全量 1F→隔离 4F/2W thread 族间歇面续录/建议1 微面）。
- 附注非缺陷一则（工序面）：B 首读 face ④ 断言窗时 .NET 相对路径解析走进程原 CWD 误取主树旧态（Set-Location 仅改 provider 位）——绝对路径复读即纠，四环链/hunk/锚点核均不受影响（Select-String/git 子进程面走 provider 位故首批锚点即正确）；如实记档供后续席位避坑。

T-B-070-PASS 0/0/0/1
sha256=5A321217296495AD81B29A68E3344181BD1450BD67AC5ED5B22013741226AD4C（Get-FileHash -Algorithm SHA256；本行之前文件内容，调度器截去本行对证）
