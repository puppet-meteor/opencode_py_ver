# T-B-038 · R4-S2（T-C1-035 交付批）结构化评审报告

> 席位B · 2026-11-05 · 评审对象 worktree r4-s2 `e490cd2` vs `76d869c`（main #139） · 总裁决 **PASS 0/0/0/2**

- **评审对象**：worktree `r4-s2`（分支 r4-s2，B-baseline `e490cd2` 单提交，父=`76d869c`=main 检查点 #139，`git log -1` 自证 ✓）vs 基线 `76d869c`；diff 面=恰 9 授权路径 +1983/−7，**全量亲读**（mcp 包 4 件+两测试件全文+app/service diff 逐行+进度 104L+任务书 37L）。TS 锚三件绝对路径亲读：`stdio.ts` 196L 全文、`client.ts` L1-140、`index.ts` L330-500；契约锚四段亲读：§5.1 L253/§6 L304/§7 L320+L339/§8 L354；r4-slices §2.1/§4 差异表亲读。纪律：worktree 只读零 git 写；唯一写面=本报告；止损 40min 未触；全量 pytest 不自跑（调度器已独立复跑）。
- **写面对账（1:1）**：`progress/T-C1-035.md` +104（新）/`app.py` 17±（=+15/−2 逐行核）/`mcp/__init__.py` +68/`mcp/bridge.py` +94/`mcp/manager.py` +378/`mcp/stdio.py` +531/`session/service.py` 19±（=+14/−5 逐行核）/`mcp_stdio_server.py` +252/`test_mcp_stdio.py` +527；Σ=+1983+−7 ✓。mcp/ 目录枚举恰 4 .py ✓；tests/integration 枚举恰 2 新文件、既有测试零触碰 ✓；porcelain 树净+diff 全集恰 9 路径=面外零写 ✓；process/ 只读复用（winjob import 复用+`_capture_in_job` 形态镜像为 stdio.py 模块内 helper，runner.py 零触碰）✓；pyproject/uv.lock 零触碰 ✓。
- **门禁复核**：B 定向复跑（专属 temproot）`tests/integration/test_mcp_stdio.py` = **13 passed, 5.90s** ✓；调度器独立复跑 939/lint clean/format 288/mypy 98 与 C 自述四件一致 ✓（B 不信自跑数字、以调度器复跑+C 自述对账为准）；算术：939=926+13 ✓（13 测函数亲数）、98=92+6 ✓（6 新 .py=mcp 4+测试 2）。
- **总裁决**：**PASS——放行并入。阻塞 0 / 严重 0 / 轻微 0 / nit 2**（nit-1 进度披露精度、nit-2 docstring 锚行号漂移；§4，均不阻并入）。

## §1 十二维逐维（每维裁决+亲证）

1. **OP-R4-1 载体忠实度 PASS**：零新依赖亲证——pyproject/uv.lock 不在 9 路径 diff 内 ✓；mcp/ 四件 import 面纯标准库+自有模块（asyncio/json/os/signal/sys+winjob+contracts/tools），无任何 SDK 引入 ✓。wire 行为对拍 TS 可观测行为：帧序 initialize→notifications/initialized→tools/list→tools/call（TS index.ts L463-471 connect 即含 tools/list 同序）✓；"不宣称同构"明文（stdio.py docstring：TS 为 behavioral anchor, not structural）✓。
2. **stdio transport 忠实度 PASS**：五常量值全=TS 原值（CLOSE_GRACE 2s/FORCE_KILL_AFTER 2s/MAX_FRAME_BYTES 16MiB/EXIT_WAIT 1s/STDERR_TAIL 1000，stdio.ts L9-17 亲读对拍）✓；帧上限=自上一 newline 起字节计数跨 chunk 精确（TS trailingBytes 语义，`_feed` 双检：pending+完整行）✓；stderr tail 增量维护 slice(-1000) ✓；close 升级序=stdin EOF→wait CLOSE_GRACE→terminate 树→wait FORCE_KILL_AFTER→hard kill→再界等待→放弃（④ 披露 unknown）+幂等/并发安全（lock+重检）+closing 先置（TS phase="closed" 先置镜像）✓；意外退出=stdout 尽且非 closing→stderr drain 界 EXIT_WAIT→exit code 界 EXIT_WAIT→"exited with code N: tail" 诊断→fail pending+回调+close（TS ensuring 块 L174-179 镜像）✓；id 关联=自增 id dict+迟到/重复响应按 id 丢弃 ✓；"Not connected" 面 ✓；差异如实披露（帧错 fail-closed vs TS 继续排水；双 onerror 折叠单 `_fail`——stderr 异常不拆协议面）✓。
3. **manager 生命周期 PASS**：spawn→initialize（startup 界）→serverInfo 校验→notifications/initialized→tools/list（catalog 界）→connected+tools 填充；链上任一失败=transport.close() 杀树+status=failed+error 记录+`McpConnectError`，entry.tools=None（无空表虚构，index.ts L451-499 startServer 先例+L461-462 注释语义亲证）✓；超时三分=startup/catalog/execution 构造器注入+TS 值默认（client.ts L35-37 亲读）✓；断线=`_mark_lost`（connected→failed+诊断记录）+在途调用经 pending futures 浮现 unknown+零后台重连 ✓；懒重建=`call_tool` 内 status≠connected 才 `_connect`（仅服务本次显式调用）✓；close=幂等+逐 server best-effort+job.close() KILL_ON_JOB_CLOSE 兜底扫树（主进程已死也扫）✓。
4. **bridge 回填四面 PASS**：text→`ToolTextContent(type="text")` 1:1；media→`ToolFileContent(type="file", uri="data:{mime};base64,{data}")`（裁量承载+④ 注记：TS 无对应转换先例，§5.1 L253 只钉 content/structured_output 面）；isError→`CoreError(TOOL_FAILED, retryable=False, details={mcp_server})`+content 原样透传（错误文本仍乘 content 予模型）；structuredContent→`structured_output`；§5.1 L253 四件形（content/structured_output/metadata/error）完整承载 ✓；命名空间零涉（spec 由调用方注入=S3 面）✓；manager 异常穿透不二次折叠（Invoker L1094-1120 既有规则承载 §6 L304 面，④ 注记）✓。
5. **close 链挂接 PASS**：service `__init__` 新增 `mcp_close`（对称 `provider_close` 形态）+Phase 3 于 provider close **之前** await（亲读 L1130-1140 diff）✓；App 新增 `mcp_manager` 参数+read-only property+装配传 `mcp_close=manager.close` ✓；归属论证成立（§7 L339 详条"依次关闭 MCP、provider、DB"：provider close 在 service 内、DB 在 App——挂 App 侧则违序）；差量亲证：app.py +15/−2、service.py +14/−5 逐行核对恰挂接所需 ✓；序钉双测（§1⑧）✓。*维度内观察（不记分）*：§7 L320 方法表枚举"provider/MCP/DB"与 L339 详条"MCP、provider、DB"措辞序不一致——L320"按下述顺序"本身指向 L339 详条，语义无冲突，实现按 L339 正确落序；契约措辞对齐=doc 修正批候选（§3-3）。
6. **§6 L304 首句逐句 PASS**：argv 直接 exec 不拼 shell=`create_subprocess_exec(command, *args)`（零 shell）✓；环境变量最小集=definition.env 原样传 Popen、无 host-env merge（契约"最小集"压倒 TS SDK extendEnv——stdio.py docstring+进度①双处披露+空 env spawn 探针实证）✓；显式 secret refs=StdioServerDefinition.env 调用方显式构造注入（config=S3 面，manager 零 config 装载）✓；启动前宿主确认=调用方显式注入行为承载（manager docstring 明文）✓。
7. **常量裁量表 PASS**：OP-R4-5 九常量全=TS 原值+docstring 逐处注记（stdio.py 五常量+manager.py 三超时+OUTGOING_CAPACITY 结构承载注记**恰在 stdio.py L449**——Select-String 亲证）✓；OUTGOING_CAPACITY 64 无独立常量=`StreamWriter.drain()` 背压结构性承载（不引入环形缓冲留死代码，注记明示"bound is structural, not a count"）✓；MCP_PROTOCOL_VERSION 固定不协商（④ 披露）✓。
8. **测试质量 PASS**：13 测函数亲数=13，与进度②映射表 13/13 同名逐句对应（a-f 六腿+§6 L304 语义+§5.1 L253+§7 L339 双挂接钉）✓；fixture 9 模式（ok/startup_hang/catalog_hang/tools_list_error/big_frame/exit_during_call/crash_once/ignore_eof_with_child/crash_with_child）+JSONL 日志计数器（L105 副作用计数承载）✓；subprocess 实跑非 in-process 桩（`sys.executable`+SERVER 真管道真进程树真退出码）✓；A-PLATFORM 披露（模块 docstring：仅 win32 腿本机实跑，POSIX 腿同断言归 CI 复验）✓；两 bug 自纠记录与终态一致（bridge type 判别字段在+roundtrip 事件等待在 spawn 后）✓；B 定向复跑 13 passed 5.90s ✓。
9. **写面+覆盖对账 PASS**：9 路径 1:1（头部逐文件）；mcp/ 目录恰 4 .py；939=926+13 ✓；98=92+6 ✓。
10. **冻结面零改动 PASS（亲证）**：diff 全集恰 9 授权路径（面外零触碰）；process/ 只读复用零修改（winjob.py/runner.py 不在 diff；`_capture_in_job` 为 stdio.py 模块内形态镜像非复制文件）；pyproject/uv.lock/CI/source-baseline.json/ORCHESTRATION/其余 builtin 均零触碰；porcelain 树净 ✓。
11. **④ 披露完整性 PASS**：未覆盖协议面清单核对——cancellation/progress/ping/sampling/elicitation/roots/resources/prompts/tools/list 分页/completion/logging/setLevel/协议版本协商，均超 §2.1 范围且未留死代码（`_dispatch` 对 server→client 请求应 -32601、notification 忽略=差异表 4 承载）✓；POSIX 腿未实跑披露（进度③+测试 docstring 双处）✓；TS 差异四处对拍属实（帧错 fail-closed/双 onerror 折叠/OUTGOING_CAPACITY 结构承载/env 原样——stdio.py+manager.py docstring 与进度③三处一致）✓；升级序中间态 Windows 无 SIGTERM 不可稳定注入披露 ✓。
12. **③漏报抽查+过程完整性 PASS（含 nit-1）**：进度②文件清单/常量表/测试映射/门禁数字与实物全对账零漏报（mcp 4 件+2 测试件+挂接差量逐项亲读；13 测亲数；调度器复跑与 C 自述一致）；时间线自洽（TS 锚亲读 15:0x 起→① 方案审视 15:16–15:39→门禁 15:39 完成，全 span≈36min 与任务书一致；止损 120min 远未触）；WDAC 自愈记录（uv sync --python D:/miniconda3，35 packages，先例同形态）✓；波折如实记（两 bug 自纠+5 项 ruff 修复+2 文件 format 重排+复跑绿）✓；T-B-037 处方⑤首用=实质合规（起始 15:16+分节时间戳承载止点；单行起止形式未落=nit-1 组成）。进度②行数自述三处失准（474/312/82 vs 实测 531/378/94）+"15:0x"起点模糊=nit-1（§4）。

## §2 必答三项

**① env 原样传 vs TS extendEnv（席位裁量成立、披露完整）**：TS stdio.ts Options.environment 注释明示 host env 由 spawner 经 `extendEnv` 合并（本地 driver=host `process.env`）；Python=definition.env 原样传 Popen 零 merge。裁量依据成立：CORE-CONTRACTS §6 L304"环境变量为最小集和显式 secret refs"是 Python 侧 operative 条款，契约压倒 TS SDK 形态（TS 为行为锚非结构锚，stdio.py docstring 明文）；差异面三处如实披露（stdio.py docstring+进度①+进度③ 差异 4）且附实证（fixture server 以 3 变量最小 env spawn 真跑通过=13 passed 亲复跑）。边界注记：最小 env 下 Windows 侧 SystemRoot/PATH 等必需变量的纳入=S3 装配面（调用方构造 definition.env 时）裁量，本票以调用方显式注入承载无误。

**② OUTGOING_CAPACITY 64 以 drain 背压结构性承载（等价性论证成立，上限语义差异无实质面）**：TS=Queue.bounded(64) 出帧队列，溢出时 send 拒绝（"Not connected"）；Python=`StreamWriter.drain()` 背压（写缓冲超高水位即挂起发送方），无独立常量、注记恰在 stdio.py L449。等价性成立的部分：两者承载同一目的——防出帧无界积压/施加背压，且在 request/response 关联模型下（manager 串行发请求、await 响应后才发下一帧）队列深度自然有界（≈在途请求数），64 上限在本票用法面永不触及。语义差异面：TS 溢出=send 失败（硬界）、Python=挂起背压（软界、缓冲理论无界）——差异已披露，且在关联模型下不可达；**边界立场**：若未来引入并发批量出帧（如批量 notification 流），须重开上限审查（届时补有界队列或显式拒绝），现面无此情形。

**③ close 链归属+36min 过程完整性（论证与时间线自洽）**：归属论证成立——§7 L339 详条"依次关闭 MCP、provider、DB"：provider close 在 service 内部、DB close 在 App，MCP 挂 service 生命周期（Phase 3 先于 provider）是唯一不违序的落位；App 持 manager 引用（read-only property）供装配+兜底，close 责任在 service——两文件差量恰挂接所需（+15/−2/+14/−5 逐行核）+双测序钉（service 级三元素序钉+App 级组合钉）。36min 时间线自洽：锚读 15:0x→进度起始 15:16→方案审视+门禁 15:39 完成，span≈36min 与任务书陈述一致，止损 120min 远未触；WDAC 自愈+两 bug 自纠+lint/format 修复+复跑全记录在案；调度器独立复跑四门禁与自述一致。无反证。

## §3 处方清单

1. **调度器并回**：r4-s2 → main（9 路径 +1983/−7）；并回后基线传播 **939/98**（pytest/mypy files）+format 288/lint clean；更新 r4-slices §0.1/§3 台账 R4-S2 状态；CI 触发对齐先例复核。
2. **CI 双 OS 复验=Unix 腿首跑**：POSIX process-group/killpg 树杀腿+close 升级序 POSIX 分支（win32 设计性无 SIGTERM 不可注入，升级序为 TS 对拍移植未逐态注入测试——④ 在案）——红则回流本票。
3. **记录级（零行动）**：CORE-CONTRACTS §7 L320 方法表枚举"provider/MCP/DB"与 L339"MCP、provider、DB"措辞序对齐——doc 修正批候选（实现已按 L339 正确落序，纯文本一致性）。
4. **进度披露精度修正（nit-1 对应，下次触达顺手）**：T-C1-035.md ② 行数自述 474/312/82→531/378/94；"15:0x"起点精确化；起止单行形式（T-B-037 处方⑤ 全量落地）。
5. **锚行号引注精度（nit-2 对应，记录级）**：stdio.py 常量 docstring TS 行号 ±1 漂移（L13/L15/L17/L18→实文 L12/L14/L16/L17；值全对）；另 r4-slices §2.1/任务书引 index.ts"L355-479"区间与实文 startServer L451 起有漂移（C 代码内引 L455-479 更近实文；语义锚 L461-462/L471/L493-499 亲证成立）——亲读为准原则已覆盖，备查。
6. **R4-S3 前瞻注记**：OUTGOING_CAPACITY 上限语义在并发批量出帧引入时重开审查（§2②）；env 最小集的 Windows 必需变量（SystemRoot 等）纳入=S3 装配面裁量。

## §4 nit 清单

- **nit-1（进度披露精度）**：进度②三处行数自述失准（stdio 474/manager 312/bridge 82 vs 实测 531/378/94——实测恰=diff-stat 权威值）；"15:0x"锚读起点模糊；起止时间戳以分节承载无单行（T-B-037 处方⑤ 形式缺口，实质合规）。均为自述精度问题，实物/门禁/映射零失真，不阻并入；修正见 §3-4。
- **nit-2（docstring 锚行号漂移）**：stdio.py 常量 docstring 的 TS 行号引注 5 处中 4 处 ±1 漂移（值与语义全对，L449 OUTGOING_CAPACITY 注记行位精确）；manager.py client.ts L35-37 引注精确。纯引注精度，亲读为准已覆盖；备查 §3-5。



