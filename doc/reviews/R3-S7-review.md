# R3-S7 评审报告（席位 B · T-B-033）

- **评审对象**：worktree r3-s7 B-baseline `6c9d197` vs 基线 `4ce2d4b`（=检查点 #125 文档提交；代码面 = #124 `f49af04` 零差量）。
- **写面对账**：恰 9 修改/新增路径 ↔ 9 授权项 1:1（name-only 亲证；+1604/−55）；冻结面 diff 亲证为空；树净（仅 `?? doc/orchestration/logs/`，`.venv` gitignored）；`git log -1` 单提交自证。
- **调度器已验门禁（B 不复跑）**：pytest 918 = 881+37 / ruff clean / format exit 0（席报 263 vs 调度器 264=进度 .md 落盘后读数，两读皆 exit 0，计数非判据）/ mypy 91 = 89+2。
- **B 独立复核面**：③漏报抽查九项亲跑实录（§4）；6 修改文件 diff 全读（cli.py +456/app.py +7/policy.py +53/loader.py +46/test_cli.py +7/test_config_loader.py +68）+ 两新测试文件逐体（778L+145L）+ 进度 99L 全量 + 契约五锚原文对拍 + 点名⑥考古 git 命令亲跑 + Temp 取证脚本在册核实。
- **结论：PASS（按现状合并）—— blocker 0 / major 0 / minor 0 / nit 3**（nit-1/nit-2/nit-3 见 §5；开放点五项不预写裁决）。

## §1 十二维逐维

**维 1 · 契约锚定**：§7 L316 亲证——`reply_permission` 为宿主输入通道、与运行锁分离；实现以并行泵兑现独立性（审批 mid-run 即时分发，审批测以真实 DB 轮询取 permission_id 完整跑通）。§5.2 L265-268：决策面/宿主硬 deny+根限制优先/项目规则不放宽宿主（四 pin 钉死链序，见维 3）；once/always 语义（L268）——always 落存决定、二次同 call 免问，事件计数 1 requested/1 replied/1 saved 亲断言；300s 默认——`DEFAULT_APPROVAL_TIMEOUT_S` 既有面零改动，single-shot 审批不在本切片、走 300s 超时闭合路径（④披露）。§3.1 L136：once/always/reject 三词汇经命令+参数直达（`/approve <id> [always]` 缺省 once、`/reject <id>`）。§6 L285：规则条目形校验=unknown-field 同族同面（装载期 exit-2，错误含文件路径）。§5.3 显示安全：escape_for_display 覆盖审批渲染全域（id/action/resources/错误消息/结局行）；**锚点字面漂移**：显示安全条文实位 L279（任务书与既有 docstring 引 L277，R2-S8 后契约上移所致，语义满足，见开放点 2）。§8"中断不重发"：twin (a) 取消永不重发请求、流结束折叠 interrupted；语义条文实位 L330+附录（任务书引 L350 同为漂移）。

**维 2 · 审批宿主面忠实度**：命令语法两支+`always` 旗标解析齐；`[permission] {id} {action} awaiting approval: /approve <id> [always] or /reject <id>`+可选 `resources:` 行（approve_once 测断言原文）；`PY_PERMISSION_REQUESTED` 入 `_PREVIEW_TYPES`（七类型）+printer 分支；`_pump_input` 并发分发（run 等待中审批完成=§7 L316 兑现，五测亲证）；`_permission_error_line` 四标签（unknown permission id/approval expired/already resolved/read-only session）+fallback 单行、全 escape、永不 traceback（纯函数测全标签+两条 e2e）；usage 面代码在册（未直测，轻量池见开放点 4）；结局行 approved/rejected 断言在册。

**维 3 · 规则接线与引擎不变性**：`permission_rules_from_snapshot` 全量 `source="project"`（硬编码，无透传）；app.py 构造点恰 `rules=` 参数+`PermissionPolicy(rules=…)` 装配区（+7 最小缝合，注释引 B-S6 报告 §6 开放点 1——处方兑现落位）；**引擎域零改动亲证**（policy.py 仅 import cast+键集+转换助手三 hunk，判定/链序逻辑零触碰）；四 pin 逐面成立：project deny 收紧默认 allow→("deny","rule:project")、host ask 压倒 project allow[bash__run]→("ask","rule:host")、project allow 不放宽 root_restriction→("deny","root_restriction")、不放宽 host 硬 deny→("deny","host_deny")——§5.2 L266 链序钉死。

**维 4 · 装载期验证落位（必答②裁定面）**：loader 三处恰位（点名②）；exit-2 面+错误含文件路径（实现亲证；断言面见 nit-1）；policy 装配期 fail-closed ValueError 函数级直测（转换面六断言）；source 自声明双侧 fail-closed（loader：source ∉ _RULE_KEYS→unknown field exit-2；policy：unknown field 'source'→ValueError 直测）；**双键集同构**：`_RULE_KEYS` ≡ `_SNAPSHOT_RULE_KEYS` = {action, effect, resources, reason}，diff 亲证逐键相同。

**维 5 · P-1 双子形态**：subprocess 真 `python -m opencode`（PYTHONPATH 注入 REPO_ROOT/src）；win32 `CREATE_NEW_PROCESS_GROUP`+父侧 SIG_IGN 包裹 CTRL_C_EVENT+0.2s hold+1314 族 skip-not-fail（本机全量零跳过）；(a) mid-run Ctrl-C→`_wait_for_run_status("running")` 后注信号→cancel 意图→等待折叠→**exit 2+interrupted 终态+恰 1 cancel_requested** 三断言；(b) prompt 面 Ctrl-C→**exit 130+进程真实退出（15s wait 护栏）+^C 输出**——修复回归钉在册。

**维 6 · 修复形态论证面**：input 工作线程 daemon 化+显式放弃 join——`_thread` 全 src 无 join 调用亲证；非 `os._exit(130)` 快路径（跳过 DB flush/finally 副作用面，论证+注记档成立）；`_sigint_router`/`_install_sigint_router`/`_restore_sigint` 带内路由+`_ACTIVE_INPUT` 模块级对象+main() try/finally 还原（前处理器保存/复原语义正确，非主线程面 suppressed 见开放点 4）。

**维 7 · 增项收口对账**：P-2 `/history` 非数值警告+保留缺省（测断言警告行+缺省限额仍出历史）；P-3 零代码+双断言钉（message 在场+`CoreError(` 缺席——点名⑥ 链独立复核成立）；非 UTF-8 `_harden_console_streams` errors="replace" 损失性降级（合成 ascii-strict 流钉"h?llo"，仅改错误策略编码仍归控制台）；exit 3 `LifecycleCleanupFailedError` 注入 pin `_CLEANUP_EXIT_CODE`（stderr 断言）；exit 70 migration 失败注入 pin `_MIGRATION_EXIT_CODE`（与 S6 nit-2 exit-70 专测合并落点不重复造轮——resolve 半面残留见 nit-2）；行 #11 runner 拆包零代码（App 装配面已全，论证合理）；行 #16 n-3 零代码（subprocess 直构+monkeypatch+fake server 已覆盖注入面，条款化无增量）。

**维 8 · S6 回归批对账（行 #32）**：minor-1 三适配器全落地——glob/ls/grep 次根允许+**绝对形渲染断言**（posix 形逐字符断言）+缺省适配器外面拒绝（PERMISSION_DENIED，R2/S6 语义分立钉）；`_display_path` 跨盘/".." 双回退以词法构造钉（win32 跨盘恒 ValueError 走 ValueError 分支、POSIX 同参数走 ".." 分支同结局——skip-if-single-drive 条款零 skip 化解，④如实注记）；grep 次根 `./` wart 披露钉现状（`{sec}/./b.txt`，cosmetic 非缺陷判定 concur）；nit-2 (a)(c)(d)(e) 四分支全落（跨盘/路由/类型失配/非 http(s) 拒绝——五组失配组合+五组非法 URL 断言）。

**维 9 · 测试覆盖对账**：29+5+3=37 算术 ✓（918−881 ✓）；collect-only 亲跑尾行 "34 tests collected in 0.88s"=29+5 ✓；构成：审批 6+接线 6+装载 ×10+双子 2+收口 5+multiroot 5+loader 3 ✓。**泵死锁回归钉非显性但在册**（B 逐步追迹确认）：P-2 测试中 "hi" run 等待期由泵读走 "/history abc"（scripted 迭代器下泵为 run 期间唯一读者，时序确定）→push_back→下一 prompt 重放执行→警告行断言+exit 0——恰为"等待期非命令行→重放不挂起"的确定性钉（见点名③）。缺口：resolve 期 ConfigError→70 半面（nit-2）、usage 分支（轻量池）。

**维 10 · 写权限面**：9 路径 1:1 对账（cli.py/app.py/policy.py/loader.py/test_cli.py 恰 docstring+7/test_cli_r3.py 新/test_config_loader.py +3 测/test_fs_multiroot.py 新/进度文件）；fixtures 零使用 ✓；越权零声明；B 侧 git 零写。

**维 11 · 冻结面**：contracts 全部（events.py 零改动——`PY_PERMISSION_REQUESTED` 现成即用）/service.py（`reply_permission`/错误类型现状即用）/invoker/registry/context/projector/state/storage/process/builtin 全部/pyproject/uv.lock——B 独立 diff 亲证**输出为空**。

**维 12 · 两诚实发现与诚实档**：pump 自吞 pushback 死锁=生产级缺陷判定 concur（必答 1，含修复完备性独立清点）；twin (a) proactor 流结束约束=④注记档正当（必答 1）；④四档整体诚实（必答 3 逐项核对），一处断言面措辞浮报（nit-1）、一处披露续列缺口（nit-2）。

## §2 点名复核六项（独立结论）

**① P-1(b) 挂起证据与 daemon 修复副作用面——成立**。取证脚本 4 件 Temp 在册亲证（approve_repro/preface_hang_repro/twin_a_repro/twin_a_self_repro.py，2026/11/4 20:18-20:28 同窗时间戳，不入库）；进度如实记 dump 呈 **poll-parked**（主线程 proactor `_poll`）而非 join-parked——不回贴 R2-S8 n-1 旧叙事、以 twin (b) 绿为终局证据，证据形态记录诚实。副作用面：在途行丢失=进程退出语境可接受；stdin 句柄 OS 于进程退出关闭；`_thread` 全 src 零 join 调用亲证——放弃 join 语义兑现；daemon 线程于解释器退出被遗弃不阻 atexit/flush 主路径。

**② loader 校验 hunk 范围——成立**。diff 恰三 hunk：`@@ -104`（`_RULE_KEYS` 常量）、`@@ -301`（`_validate_permission_rule` 函数）、`@@ -318`（`_validate_layer_keys` 内钩子 6 行）；无任何越界 hunk。

**③ `_pump_input` 死锁修复语义——成立**。`include_pushback=False` 在泵读面（pushback 仅经 `_ReplInput.read_line` 缺省参数于下一 prompt 重放）；机理对拍成立：pushback 同步重读→紧旋→每旋 `push_back`→`_wake`→`call_soon_threadsafe` 自管写满自管道→proactor 阻塞 send（loop 线程正执行泵协程无从排水）→死锁；修复切除唯一旋源。**回归钉经 B 独立追迹确认在册**：P-2 测试的 mid-run 非命令行 "/history abc" 恰走泵→push_back→重放路径（scripted 迭代器下泵为 run 期间唯一读者，时序确定；若泵回读自管 pushback 则紧旋挂起、测试必失败）——进度"新测试钉住"声明属实，钉为 P-2 测试兼载（非显性命名，见 §5 建议显性化）。

**④ twin (a) proactor 重设计——成立**。`Frame(text_delta_frame("partial"), delay_s=3.0)`+`omit_done=True` 使流于信号后 ~3s 自然结束；断言 exit 2+`interrupted` 终态+恰 1 `py.run.cancel_requested`（cancelling 折叠 interrupted 终态亲断言）；生产路径靠请求超时兜底的披露正当：Windows proactor 在途 overlapped socket read 不可中止=平台约束，替代（transport.abort()）风险更大；cancel 意图即刻持久化（run_status 可见 cancelling）+终态延迟有界（≤ timeout_s，内置缺省 30s）；Unix selector 无此约束如实注记。设计正当、非妥协过当（残余见开放点 3）。

**⑤ 规则接线 source 强制——成立**。`_SNAPSHOT_RULE_KEYS` 无 source 键；自声明条目→`ValueError: unknown field 'source'` 直测；转换硬编码 `source="project"` 无透传路径；四 pin 的 (effect, source) 对与 §5.2 L266 链序对拍全中。

**⑥ P-3 前提过时链——独立复核成立**。B 亲跑：`git log -S "_decode_run_error"` → 诞生恰 **aa67bd3**（R2-S7 service wiring）；现位 service.py L412 定义、L803 `error=_decode_run_error(run_id, run.error)` 装配；`RunStatus.error: str | None`（L267）；cli.py L871 `error: {escape_for_display(status.error)}` 渲染；`git grep CoreError -- src/opencode/cli.py` **空**、`git grep str(CoreError) -- src/` **空**——failed 行自 CLI 诞生即消息形，r-3 观察确系评审侧静态误判经 r-3→行#3→任务书三层传递未复核。**成因独立意见**：误判源于从 CoreError 类型静态外推显示面（假定 repr 泄到 `[run] failed` 行），未追 service 边界处 `_decode_run_error` 的 `.message` 提取（§8 脱敏设计）；三层传递各层继承前层断言为既定事实，静态猜测被逐层放大为工作项。教训两条：显示语义类发现须至少一次 wire 级/打印点路径追迹方可入台账；台账行宜携带证据类型标注，使下游能以 O(1) 成本复核而非再度全链考古。**处置 concur**：零代码+双断言钉（message 在场+`CoreError(` 缺席）将陈旧观察转为永久回归卫兵，正确。

## §3 必答三项

**1（两诚实发现独立立场）**：(1) **pump 死锁=生产级缺陷，concur**——run 等待期任意非命令键入即整进程死锁，属常规交互面上的全量失败，severity 判定恰当。**修复面完备性 concur**：全 src `call_soon_threadsafe` 唯一经 `_wake`；`_wake` 调用点清点——`_read_lines` 每行一次（排水节奏）、`push_back` 每延迟行恰一次、`interrupt`/`route_sigint` 每信号一次——无第二个自管紧旋面；修复切除的正是唯一旋源（泵同步重读自管 pushback）。回归钉在册（点名③）。(2) **proactor 流结束设计=设计正当非妥协过当，concur**——平台约束真实、cancel 意图即刻持久化、终态有界（≤请求超时）、测试确定性钉折叠语义、Unix 面如实披露；生产超时兜底可接受（开放点 3 留实验余地）。

**2（116min 贴线过程完整性）**：**兑现**。单段四重侧证：单一 B-baseline 提交 `6c9d197`；树净无 WIP 残留（仅预期 logs/，.venv gitignored）；进度末尾明示"本文件为最后交付物；其后零代码变更"；Temp 取证 4 件时间戳同窗（20:18-20:28）；门禁四数字自跑回合内等待声明（pytest 197s 前台，T-C1-028 教训兑现）。B 无法独立复演时钟，四重侧证相互印证、无任何反证，裁定如实无截断。

**3（③④档浮报完整性）**：六预列逐名核对：1314 族→未测防御 ✓（测试 skip-not-fail 实现在册，本机零跳过）；非 UTF-8 构造环境→未测防御 ✓（合成流钉降级面、真实 OEM 控制台页未构造如实记）；os._exit 副作用→注记档 ✓（选型未采、无从测试，分档合理）；规则形错误路径→装载 ×10+装配函数级均已测防御 ✓（resolve 半面残留→nit-2）；审批并发竞态窗→未测防御 ✓（commit-before-register 秩序+0.1s grace 如实记）；once-always 边界→未测防御 ✓（精确 tuple 等值边界无专测）。**浮报 1 处**（nit-1：断言面措辞）；**漏列 2 处**（nit-2：resolve 期 ConfigError→70 半面；开放点 1：泵 KI 后同 run 分发停止行为）；轻量 suppress 卫兵群（usage 分支/非主线程安装分支/`_wake` 循环已关分支）未列——皆一行卫兵，注记档裁量，不计浮报。**整体诚实档成立**。

## §4 ③漏报抽查记录（B 亲跑实录）

1. `git log -1` → `6c9d197 S7 B-baseline: …`（单提交自证）✓
2. `git status --porcelain` → 仅 `?? doc/orchestration/logs/` ✓
3. `git diff 4ce2d4b..6c9d197 --stat` → 9 文件 +1604/−55 ✓
4. `git diff 4ce2d4b..6c9d197 --name-only` → 恰 9 路径，1:1 对账 9 授权项 ✓
5. 冻结面 diff（contracts 全部/service.py/invoker/registry/context/projector/state/storage/process/builtin 全部/pyproject.toml/uv.lock）→ **输出为空** ✓
6. `pytest --collect-only -q`（两新测试文件）→ 尾行 **"34 tests collected in 0.88s"** = 29+5 ✓（Measure-Object 35 行=34 ID+1 summary 行，计数口径核实）
7. 点名⑥ 考古命令五支亲跑（`git log -S`/`aa67bd3` 自证/`git grep CoreError` cli.py 空/`str(CoreError)` src 空/`RunStatus.error: str`+L871 escape 渲染）→ 全链成立 ✓
8. Temp 取证脚本在册核实 → 4 件（名字+大小+时间戳）与进度声明逐字相符 ✓
9. 契约锚 grep → 显示安全条文实位 L279、不重发语义实位 L330+附录（任务书引 L277/L350 为陈旧锚，语义满足，开放点 2）✓

## §5 可执行处方（nit 三项）

- **nit-1（进度精度）**：进度②逐测落点称装载期 ×10 测试"断言错误信息含文件路径"——测试体仅 `assert excinfo.value.code == 2`，无消息内容断言；"错误信息含文件路径"为 loader **实现**事实（diff 亲证 `{path}` 在 ConfigError 消息内）而非**断言**面，措辞浮报（S4 nit-2 同档）。载体=本报告记档（进度为已交付件不回改）；后续进度模板宜区分"实现含 X/断言含 X"。
- **nit-2（披露续列缺口）**：S6 披露候选"exit-70 无专测"的 **resolve 期半面**（config 供 `context_limit ≤ output_reserve` → parse 期形状合法 → `resolve_runtime` ConfigError → `except Exception` → 70）在 S7 未直测亦未在 ③④ 档续列；行 #32"exit-70 专测"落点为 migration 面（行 #15 合并，落点本身合理——migration 为常见真实 70 面，且与注入 pin 合并不重复造轮）。装配期 ValueError→70 后备为函数级直测、CLI 级端到端当前不可达（CLI 无 recover 路径），分档合理不另计。**处方**：下一回归批补一行级注入测试（`--config` 写 context_limit=100+output_reserve=200 → 断言 exit 70），约 10 行。
- **nit-3（cosmetic）**：`_print_history` docstring 前导 "/" 丢失（`"[history] [limit]"`，P-2 编辑波及），与 `_repl` 实际 `/history` 命令面不一致。**处方**：下次触碰 cli.py 时复原一字。

## §6 开放点（不预写裁决，留调度器）

1. **泵 KI 后行为注记**：mid-run SIGINT（cancel intent）后泵即返回——同 run 余程内后续行（含 `/approve`）不再即时分发，缓冲至下一 prompt；语义无害（cancel 在途、pending 审批按 §5.2 closed 收口），未披露未测，建议注记档补记。
2. **契约锚漂移**：任务书与既有 docstring 引"§5.3 L277"（显示安全）实位 **L279**、"§8 L350（中断不重发）"语义实位 **L330/附录**——R2 后契约增行所致，本切片语义要求全部满足（非 C 缺陷）；建议后续文档锚点刷新批（docstring 内 §行号引用建议改引节号或去行号化）。
3. **Windows proactor 生产取消终端延迟** ≤ 在途请求超时（timeout_s 内置缺省 30s）——④注记正当；若产品需更利落取消，后续可评估 transport.abort() 实验面（Unix selector 不受影响）。
4. **未直测轻量面池**：`_reply_permission` usage 分支、`_install/_restore_sigint` 非主线程/平台不支持分支、`_wake` 循环已关抑制分支——皆一行卫兵，注记档裁量即可。
5. **S6 披露候选 IPv6 括号分支**维持披露-未测态（行 #32 未列名、非本批义务）——台账披露态保留。
6. **回归钉显性化建议**：泵死锁回归钉由 P-2 测试兼载（非显性命名）——建议下一批为该测试补一行注释或拆显性测试名，使钉的意图在测试清单可见（B 追迹确认其在册，但寻常读者难发现）。
