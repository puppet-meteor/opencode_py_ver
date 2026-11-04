# R3-S5 结构化审查（席位B · T-B-031）

> 审查对象：worktree r3-s5 **`004a59d`**（T-C1-029 交付，11 files +2014/−69）vs 基线 **`3a2c1aa`**（#117，分叉点）。diff 取法 `git -C <worktree> diff 3a2c1aa 004a59d`（只读）。
> 审查人：席位B（T-B-031），止损 40min 内完成。写权限：仅本报告（新增单文件）；git 零写；worktree 零写入。
> 门禁基线：调度器已验收（16:25 PASS），任务书免复跑——pytest **838**（=820+18：集成 14+契约 4，零跳过）/ ruff clean / format exit 0（250 files，计数非判据）/ mypy **85**（=79+6）。B 独立校验算术：18=14+4 ✓、6=process×4+bash.py+test_tool_bash.py ✓、250=243+6+1（进度文件两读差异，调度器解释成立）✓。
> 调度器已验项（直接引用、未重复执行）：四门禁数字、写面/冻结面/必答 6/④七项对账。**必答 1 为 B 对两项登记级观察的独立复核，非背书。**
> B 独立复核面：git 证据五连亲跑（stat / name-only / log / status / 冻结面 diff，§2 实录）+ grep.py 三 hunk diff + cli.py/+11 + builtin\_\_init\_\_.py/+27 + test_tool_listing.py diff 全文 + process 四件全文（runner 309L / winjob 163L / interpreter 130L / \_\_init\_\_ 45L）+ bash.py 316L 全文 + test_tool_bash.py 758L 全文（14 测体逐读）+ 进度 T-C1-029.md ①-⑤ 全文 + 契约 §5.3 L272-280（L276/L277 原文逐句）与 §2.1 L35/L37/L43-48 本会话直读（§5.2 L266-268/§5.1/§7 沿 S2-S4 累计亲证）+ PHASE1-SLIM L91-96 + reestimate-r0 L62（A-PLATFORM 定义）直读 + policy.py L68-86（`_ASK_NAMESPACES={"bash","mcp"}` L77-78 亲证）+ r3-slices §2.5（L85-93）/台账行#25（L152）/行#30（L157）直读 + **TS 三锚全文亲读**（tool/plugin/shell.ts 293L：L24/L55 描述/StructuredOutput/resultMessages/prepare env 四标记/ToolFailure 措辞/超时消息族；shell/select.ts 227L：args() L206-209/win() 序/unix()/executable/resolve 缓存；shell/result.ts 81L：notice 族）+ ORCHESTRATION.md 16:25 验收条目（两观察原文）。
> 实现过程披露：单段 37min（15:43-16:20），回合纪律条款生效零中断（与 S4 对照）。

## 结论

**PASS**（blocker 0 / major 0 / minor 0 / **nit 3**）；调度器两项登记级观察均 **concur**（①计入 nit-1，②非缺陷+载体处方）。

R3-S5 与 spec（r3-slices §2.5）和契约逐句对齐：L276 六要件全落（显式解释器 argv 无 shell=True / 首用探测+进程级缓存=启动检查 / 完整 command/cwd 审批绑定 #13 断言 / 命令串单 argv 元素 / L276 末句免责内嵌 advisory）；L277 五要件全落（process 包单点受控环境 / 120s=shell.ts L24 零偏差 / 越限续排空 flood-then-hang 排水证明 / Job Object+killpg 收树两代树进程级探测 / 无法确认清理→unknown 三源）；收树选型论证成立（原子/无枚举竞态/晚生覆盖/契约首名）；TS 三锚逐字对拍零行为偏差（五项解释器偏差全部量化披露）；审批面零 policy.py 改动挂接（冻结 `_ASK_NAMESPACES` 自带 bash）；rg 收编（行#25）三 hunk scoped 精确、弱读循环 deadline→强单调修复成立；14+4 测试逐体核读，A-PLATFORM Unix 未测零伪造；单段过程完整，回合纪律条款兑现 S4 教训。三 nit 均登记级：rg stderr 上限披露精度（=调度器①）、timeout=0 测试强度、进度② env 标记超报。无返工需求。

## 1. 审查维度逐条（12）

**维 1 · 契约锚定（L276/L277 逐句，原文本会话直读）** ✓：L276——显式解释器 Unix bash/Windows PowerShell（interpreter `_candidates` win32→[pwsh,powershell]、else→[bash]；`Interpreter.argv` 单命令元素；runner `Popen` 全程无 `shell=True`，create_subprocess_shell 平台默认零依赖）✓；可用性启动检查（首用真实 spawn 探测 `--version`/`-Command exit 0`+`_INTERPRETER_CACHE` 进程级缓存=懒装配宿主的启动检查读法，docstring 论证）✓；完整 command/cwd 审批（schema 用 S2 `extract_resources` 键约定；#13 `requested["resources"]==[command, str(tmp_path)]` §5.2 L268）✓；命令字符串不经 JSON 修复（单 argv 元素直传）✓；危险命令正则不能证明安全（advisory 永不阻断，末句内嵌 docstring+_ADVISORY_TEXT）✓。L277——受控环境（process 包单点纪律；env=继承 os.environ+四 TS prepare 标记，shell.ts prepare 亲证逐名对应）✓；超时默认 120 秒（`DEFAULT_TIMEOUT_S=120.0`=shell.ts L24 `2*60*1_000` 零偏差，亲读+sync 测钉定）✓；输出截断后仍排空管道（`_CappedBuffer` 越限只停追加、reader 到 EOF；flood-then-hang 测排水证明）✓；取消/超时终止并回收进程树（`_kill_tree` TerminateJobObject/killpg SIGKILL+`wait(KILL_GRACE_S)`；两代树进程级探测）✓；无法确认清理→effect_status=unknown（reaped=False→EXECUTION_UNKNOWN；超时/取消经冻结 invoker 映射 (failed,unknown)，#5/#6/#8 断言）✓。

**维 2 ·【点名复核①】收树机制与真实测试** ✓（winjob.py/runner.py 全文亲读）：**ctypes 面正确**——三结构体（`_IO_COUNTERS` 6×ULONGLONG、`_JOBOBJECT_BASIC_LIMIT_INFORMATION`、`_JOBOBJECT_EXTENDED_LIMIT_INFORMATION`）布局与 Win32 形状一致；7 个 kernel32 函数 argtypes+restype 全设；`KILL_ON_JOB_CLOSE=0x2000` 内核兜底（本进程死亡即杀 job 成员）；BREAKAWAY_OK 刻意不设（后代无法逃逸 job）；**capture 竞态降级**——已退进程（`poll() is not None`）返回 job 非错误（无可捕获），其他 OSError→close+None→回落主进程 kill（④-1 披露，树保证降级而非 spawn 失败）。**选型论证成立**——Job Object vs taskkill /T /F：原子（单内核调用 vs PID 快照枚举）、无枚举竞态（成员资格创建时继承 vs 快照可漏）、晚生覆盖（kill 开始后 spawn 的子进程仍在 job）、契约首名（L277 首名机制）。**两代树测试构成 L277"已验证"证据**——tree_child.py 父写 pid→spawn 孙→sleep 60；`_wait_pid_file` 双 pid 齐（15s 窗/50ms 轮询）→gen1≠gen2→`_assert_pids_gone` 用 **OpenProcess+GetExitCodeProcess==STILL_ACTIVE(259) 进程级探测**（10s 轮询窗）非 exit-code 推断；`terminate(1)` 退出码≠259 使探测方向保守（误判"活着"只会假失败、不会假通过）；探测不可达（OpenProcess 失败）计为已终止。Windows 面证据充分。

**维 3 · 解释器显式选择** ✓（interpreter.py 全文+select.ts 全文对拍）：argv 形状 vs `args()` L206-209 逐字一致（ps→`-NoLogo -NoProfile -NonInteractive -Command`；bash→`-c`；cmd `/c` 面不移植=无 cmd 候选）；探测缓存进程级（与 select.ts `resolve()` 模块缓存同构）；**偏差量化完整**——win() 完整序=[pwsh,powershell,gitbash,COMSPEC‖cmd.exe]（亲读），Python 取前二（契约只名 PowerShell）；unix()：SHELL 环境变量→/etc/shells→[/bin/bash,/bin/zsh,/bin/sh]，Python 只 bash（PATH→/bin/bash，契约钉 bash，/bin/sh 兜底不移植）；`executable()`=stat+which 不执行（亲读），Python 真实 spawn 探测**严格更严**（stat 过而不可执行的桩过 TS 败于此——论证成立）；五项偏差 docstring+进度①全量披露。

**维 4 ·【点名复核②】输出限额防死锁** ✓（runner.py `_CappedBuffer` 亲读）：线程安全（`threading.Lock` 包住 append_line/snapshot 全部变更）；字节精确截断（行编码 utf-8/replace→越限取 `encoded[:remaining].decode("utf-8","ignore")`——字节精确前缀+多字节字符中界丢弃）；越限续排空（`_truncated` 后仅计数不追加，reader 阻塞读至 EOF——"cap only stops *appending*"）；主循环 poll 不依赖读管道。**flood 测证明力度**——200001 字节单行→marker "showing 65536 of 200001 bytes (limit 65536 bytes)"+`len(stdout_item.encode())==65536` 字节精确钉死；**flood-then-hang 排水证明**——150001 原始计数满量（若 reader 停在 cap，管道缓冲满→子进程写阻塞→计数停在管道容量，满量即越限持续消费的直接证据）+3s deadline 照常触发+正常返回无死锁=L277"截断后仍排空"实证。流分离 vs TS pty 合并：确定性渲染+stdlib-only（无 ConPTY 面），docstring 量化披露 ✓。

**维 5 · 取值对账** ✓：120s=shell.ts L24 精确（亲读 `2 * 60 * 1_000`）；timeout=0 禁用=shell.ts Input 描述亲读+runner `timeout_s > 0 else None`（测试强度见 nit-2）；64 KiB **per-stream** vs L274 末句"输出默认 64 KiB"——L274 为通用限额语（read 面同款先例），流分离下 per-stream 是自然读法（总量限额会让 stderr 饿死 stdout），进度④明列"per stream"——已浮报非瞒报；TS 毫秒→Python 秒单位换算（120 值精确）量化披露。

**维 6 · 取消与效果语义** ✓：§7 L337 已开始命令如实保存已确定结果——cancel 路径保留已捕获 content+structured_output+EXECUTION_UNKNOWN（"tree was terminated and the final state could not be confirmed"），#6 断言 (failed,unknown)；§2.1 L35（原文亲读）三源 unknown：取消（EXECUTION_UNKNOWN）/超时（TOOL_TIMEOUT→冻结 invoker 映射 unknown，#5 (failed,unknown)+TOOL_TIMEOUT 断言）/完成不可确认（not reaped→EXECUTION_UNKNOWN）；**TOOL_TIMEOUT retryable 分类正当**——超时确定性可重试（TS 消息族原文"Retry with a larger timeout"）、取消 retryable=True（重跑=新命令）、不可确认清理 retryable=False（效果不可证不可安全重试），三档与语义面一致；exit-code-as-data vs result.ts——非零→succeeded+notice "Exited with code N"（逐字）+structured exit（#2 exit 3）；signal 面：负 returncode→structured `"SIG{-N}"`（TS StructuredOutput.signal 对应）；TS "Killed by {signal}" notice 分支不移植——仅外部杀（非我方超时/取消）可达、今日零生产者（非计数微观察，§4）。

**维 7 · S2 门挂接** ✓：side_effects=True（BASH_SPEC）+冻结 `_ASK_NAMESPACES={"bash","mcp"}`（policy.py L77-78 亲读）→命名空间级 ask 默认，**零 policy.py 改动**（冻结面 diff 空亲证）；契约测 `test_bash_asks_by_default_in_the_permission_table` evaluate→(ask,"default") 钉定；command/cwd 绑定 #13 `resources==[command, str(tmp_path)]`；cwd 缺省→`(command,)` 绑定（④披露——always 决定覆盖任意工作目录同命令的宽面，诚实披露）；once/reject 真实 App 链——#13 全链（alembic 真迁移+scripted provider+once→allow+文件落盘+started `permission_decision=="allow"`+finished (succeeded,completed)）；#14 reject→(denied,not_started)+out.txt 不存在+`started==[]` 零执行。

**维 8 ·【点名复核③】deny 提示不阻断** ✓：L96 原文亲读（"命令 deny 清单只能辅助提示，不是 shell 沙箱"）——`_ADVISORY_TEXT` "advisory hint, not a sandbox -- the command was not blocked" 逐语义对应；5 正则集保守（rm 递归根/家删除、fork bomb、mkfs、Windows format 驱动器、dd of=/dev/——均为真实毁灭性模式，假阳/假阴 blanket 披露④-6；单一 `_DENY_HINT_PATTERNS` 元组维护；docstring+进度①+OP-R3-2 文档化）；L276 末句免责内嵌（docstring 引用+advisory 文本自陈）；**假阳性用例双向证明**——`Write-Output 'mentions mkfs somewhere'` 命令无害但命中 mkfs 模式→"[advisory]"+"filesystem format" 出现（证触发）+原样输出在结果+succeeded/completed（证不阻断）——单测同时证明两面，设计力度强；scan.ts 2409L 不移植=L96 硬约束+§2.5 明定（仅参照）。

**维 9 ·【点名复核④】rg 收编 delta（行#25）** ✓：**语义修复论证成立**——旧 deadline 仅 stdout 行到达时检查（旧 diff 亲读：`for out_line in process.stdout: if time.monotonic() > deadline`）→零输出挂起 rg 永不触发、串行 invoker 整 run 挂起；新 runner 强单调 poll（POLL_INTERVAL_S=20ms）无论输出有无照常触发。**三 hunk scoped 亲证**——hunk1 import 区（−threading −time、+`from opencode.process import run_command`；subprocess 保留供 rg --version 探测）；hunk2 `_RG_WAIT_S` 删；hunk3 `_run_rg` 重写（Popen+drain 线程+读循环→`run_command`+`on_stdout_line` 回调）——**其余零触碰**（diff 仅此三 hunk）。per-line 收集免疫 64 KiB 上限（matches 在 `_collect` 回调内收集，不经 capped buffer）；early stop 杀 rg→非零退出码预期（docstring 披露）；既有 rg 测试零回归（test_tool_static_trio.py 不在 11 路径=name-only 穷尽亲证+838 绿）。调度器观察①独立裁定见必答 1（=nit-1）。

**维 10 · 测试覆盖对账** ✓：14 测名与进度②逐一对应（逐体亲读；13 async+1 sync=`test_bash_default_timeout_is_120_seconds`）；4 契约新测+2 处既有断言扩展（catalog 七件套 `["run","edit","glob","grep","ls","read","write"]`+`{bash,fs}` 命名空间+`bash__run` resolve+重复拒绝循环六适配器）——进度②/⑥ **如实描述断言扩展**（行#30 nit-2 先例执行到位，与 S4 对照本次措辞精确）；③ A-PLATFORM Unix 未测诚实（测试 docstring "only the Windows leg ran in anger...no faked Linux evidence"+`_interpreter_or_skip` 跳过+Unix pid 探测 PermissionError/僵尸=活着的④披露）——无伪造；调度器观察②见必答 1；env 标记面测试缺口=nit-3。

**维 11 · 结构与风格** ✓：process 包分层清晰（interpreter 选择/runner 纪律/winjob 平台 API；`__init__` 公共面；runner 仅 win32 导入 winjob）；`asyncio.to_thread` 工作线程论证成立（长命令不阻塞事件循环——#6 从事件循环侧 `cancel.set()` 即证活性；cancel 每 tick 从工作线程轮询）；bash.py 措辞 "Unable to execute command: {command}: {error}"=shell.ts ToolFailure `Unable to execute command: ${input.command}` 对齐（亲读；**与 S4 write/edit "Cannot" 形成跨切片措辞不一致——row #30 nit-1 已载，此处反证 TS 对齐词为 "Unable"**）。

**维 12 · 写权限面对账** ✓：11 路径=授权清单精确（新增 7：process 四件+bash.py+test_tool_bash.py+T-C1-029.md；修改 4：builtin\_\_init\_\_/cli.py/grep.py/test_tool_listing.py），+2014/−69；单提交 `004a59d`；树净；冻结面十路径零差量（§2）。

## 2. ③漏报抽查结果（B 义务亲跑实录，本会话只读）

- `git diff 3a2c1aa 004a59d --stat` → 11 files changed, **2014 insertions(+), 69 deletions(-)**。
- `git diff 3a2c1aa 004a59d --name-only` → 恰授权 11 路径逐一相符，**无多余路径**（test_tool_static_trio.py/test_tool_write_edit.py 等冻结测试零触碰的穷尽证明）。
- `git log --oneline 3a2c1aa..004a59d` → 单提交 `004a59d`（"S5 B-baseline: bash__run + controlled subprocess environment (T-C1-029) - explicit interpreter + Job Object tree reaping + flood-safe output caps"）。
- `git status --porcelain` → **空**（树净）。
- 冻结面 diff（`-- src/opencode/contracts/ src/opencode/tools/policy.py src/opencode/tools/invoker.py src/opencode/tools/registry.py src/opencode/session/service.py src/opencode/app.py src/opencode/tools/builtin/read.py ls.py glob.py write.py edit.py`）→ **空**（审批引擎/fs 族/契约面零差量；零 policy.py 改动的独立佐证）。
- **结论：文件级零漏报**；内容级与进度①-⑤对账仅 nit-1/nit-3 两处措辞级出入（§4），无实质漏测、无未披露行为面。

## 3. 必答三项（B 独立意见，给据）

| # | 项 | B 意见 | 依据 |
|---|---|---|---|
| 必答1a | 调度器观察①：rg stderr 亦受 64 KiB 上限（旧无界）而进度⑤仅明列 stdout | **concur（登记级披露精度 nit=本审 nit-1）** | 独立核验：新 `_run_rg` 取 `result.stderr`（runner `_CappedBuffer` 64 KiB 截断），旧实现 drain 线程无界收集（旧 diff 亲读）——行为增量真实；方向保守（有界化符合 L274/L277 纪律）；实际影响≈零（rg stderr 为诊断文本，>64 KiB 属病态）；且 rg 侧截断**无标记**（标记是 bash.py `_render` 的关切，rg 不经 `_render`）——诊断文本静默截断。⑤"rg's stdout buffer...subject to the same 64 KiB cap machinery"明列 stdout 面、stderr 未列=披露精度缺口非瞒报（matches 不受影响属实）。**载体：台账行#30 追加一行**（沿 nit 先例）或下次触达 grep.py/进度文件的切片顺手补记；非返工 |
| 必答1b | 调度器观察②：frozen-invoker 诊断后置（解释器不可用/缺 cwd/空命令落 started 后 failed/unknown）=结构性属性；pre-execute 诊断钩子=重启条件候选 | **concur（披露成立，非缺陷；载体处方见下）** | 独立核验：三诊断均在 `_execute` 内（bash.py 亲读）→py.tool.started 之后；#10 断言 (failed,unknown)+TOOL_FAILED=冻结 invoker 映射实证；契约面：§5.1 L257 钉 invoker 只做 schema 校验（空串过 schema——无 minLength，语义级检查本属 adapter）；L276"启动时检查"要求检查存在、未命令 not_started 终态；§2.1 L35 started→failed 合法迁移+effect unknown=保守诚实面（进度④-3 披露准确）。**载体：台账新增一行条件式**（沿行#29 先例——条件=invoker 获 pre-execute 诊断钩子时语义级诊断可升级 not_started）；该候选属 invoker 面演进、独立于行#29（formatter/file-diff 条件），建议新行不挂靠 |
| 必答2 | ③④档浮报完整性独立清点 | **③④档本体无漏项；B 发现两处浮报精度缺口（nit-1/nit-3）+两处非计数微观察** | ③ A-PLATFORM：Unix 未测+Windows 真实，核实无伪造 ✓。④ 七项逐项核实：pre-assignment race ✓（降级路径+已退非错误亲证）/killpg 竞态 ✓/frozen-invoker not_started 不可表达 ✓/cwd 缺省 (command,) ✓/env 继承 by design ✓/deny 假阳假阴 ✓/per-stream+行粒字节精确 ✓。浮报缺口=nit-3（进度② test#1"Proves"列称 env 四标记——测试体无 env 断言，代码已设而测试未钉）+nit-1（⑤ stderr 面措辞）；非计数微观察：AI_AGENT 无条件覆写 vs TS `\|\|=`（env 继承披露伞下微差异）、"Killed by {signal}" notice 分支不移植（今日零生产者面）。冻结面零差量排除未披露代码面 |
| 必答3 | 单段过程完整性（vs S4 对照） | **过程完整无瑕疵** | 37min 单段（15:43-16:20）回合纪律条款生效（最终 pytest 回合内等待）零中断；终树对账：单提交+树净+恰 11 路径+无孤儿文件（§2 五连）；六代码文件全文亲读无 TODO/stub/半成品；进度①-⑤ 与 diff 面一致（除 nit-1/nit-3 措辞级）。**对照结论：S4 中断（后台 pytest 通知丢失→两段续行）vs S5 单段完成——回合纪律硬条款有效性的直接对照证据，同一教训的预防性条款在下一切片即兑现** |

## 4. 发现清单与处方

**nit-1 · rg stderr 上限披露精度（=调度器观察①，concur）**：rg 收编后 stderr 受 64 KiB 上限（旧无界）且 rg 侧无截断标记；进度⑤仅明列 stdout 面。处方：台账行#30 追加一行，或下次触达 grep.py/进度文件切片顺手补记；非返工。

**nit-2 · timeout=0 测试强度**：`test_bash_timeout_zero_disables_the_deadline`（1s 命令）不能区分"禁用"与经典 `input.get("timeout") or DEFAULT` falsy-回退 bug 类（两语义下 1s 命令同判 succeeded）——仅排除"立即超时"误读；实际代码正确（`input.get("timeout", BASH_DEFAULT_TIMEOUT_S)` 亲读）+语义由 runner 代码/常量测/TS 锚三面钉定。可加强（monkeypatch 小默认值+长于该值的 sleep 即可区分）。处方：后续触达测试文件切片顺手加强，非必须。

**nit-3 · 进度② env 标记超报**：test#1"Proves"列称 `AGENT`/`OPENCODE`/`AI_AGENT`/`OPENCODE_SESSION_ID` 四标记而测试体无任何 env 断言——代码面已设（bash.py 亲证，与 shell.ts prepare 四标记逐名对应）但测试未钉定，env 标记面=未测而②称已证。处方：进度文件措辞更正，或后续切片补一条 env 回显断言（`$env:AGENT` 类命令）；沿行#30 nit-2 模式记档。

**非计数微观察**（登记于本报告，不开台账行）：AI_AGENT 无条件覆写 vs TS `||=` 保留既有值（env 继承披露伞内微差异，无行为面后果）；TS "Killed by {signal}" notice 分支不移植（仅外部杀可达，超时/取消路径各有自己的 notice/错误面）；deny 正则 `[c-z]:` 不含 a:/b: 驱动器（blanket 假阴披露④-6 已覆盖）。

**调度器②载体处方**：台账新增条件式行（invoker pre-execute 诊断钩子→not_started 升级候选，沿行#29 条件式先例）。

## 5. 建议

- **按现状合并（PASS）**：零 blocker/major/minor；nit 三项均登记级、零返工。
- 处方载体：nit-1/nit-3 → 行#30 追加或触达切片顺手；nit-2 → 测试强度备忘；调度器② → 新台账条件式行。
- 行#25（rg 读循环超时收编）就此**闭合**：弱读循环 deadline→强单调的语义修复落地+三 hunk scoped 精确+零回归，无需后续动作（nit-1 仅披露精度补记）。
- S6（ConfigLoader/快照，r3-slices §2.6）可开工，S5 无遗留阻塞；S2 nit-1（默认表回落面+bash/MCP 表键）在 S6 落地表键时可一并闭合（行#28 处方沿承）。
- 跨切片措辞统一备忘：bash.py "Unable to"（TS 对齐，本切片亲证）vs write/edit "Cannot"（row #30 nit-1）——后续统一时以 "Unable" 为准。
