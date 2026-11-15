# P6-S4-review — T-C1-097 微评审：工具包A——patch+todowrite+question 三工具落 PY builtin 形

- 评审对象：worktree `p6-s4` 席位工作区（未提交态，席位 git 写零）：改 6 跟踪文件[cli.py+9、serving.py+12、`builtin/__init__.py`+59/−1、test_httpapi_server.py+9/−4、test_workflow_tools.py+23/−11、policy.py+11] + 新 5[三 builtin：patch.py 773L/todo.py 211L/question.py 290L；新测试 test_tool_patch_todo_question.py 885L 27 测；进度档] + 票面拷贝 untracked——**两处与票面 §6 预裁面的偏差均有完整记档与先例支撑**（锚5 落点矫正→app.py 零触碰+双宿主组合面；httpapi 零触碰预期→serving.py 组合面三行逃逸）；**契约面（contracts/events/projector/state/repos/schema-lock/registry）全零触碰=numstat 亲证**；SDK/路由/wire 零触碰。
- 评审基据（全部独立现读，禁继承）：三新 builtin 全图+关键体（patch `_verify`/`_execute` 事务两相位+seek 四级+heredoc/BOM/CRLF；todo/question 执行面与恰键集）、三改面+两 census 面 diff 逐行、**app.py 零 diff 核证**、新测试 27 测全图+census/policy/Spec 关键体+**census 终形核证**（机制矫正后重写形）、进度档全文亲读、**winpty 零因果交叠独立核证**（子进程裸 stdlib 脚本 import os/signal/time+测试体纯 pty 机械零工具/App/registry 面）、TS patch.txt 规约对照。
- 评审时刻：2026-11-15 18:0x-18:3x（席位弧两段：发射 #242→SEAT-EXIT 0 @16:19:57 首段[全量门禁后台化违规=弧未闭合：进度档未写+全量未收齐]→调度器续跑令[前台收齐+补档指令]→SEAT-EXIT 0 第二次 @~18:00 续段[机制矫正+全前台门禁+进度档全档]——首段违规经续跑令矫正闭环，两段弧全记档；席位 git 写零——终态 HEAD 恒 `aa0f3df` 调度器亲证）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 4（全观察面，无返工）

## 六面逐条结论

### 1. 票面纪律 — PASS

- 禁 git 写遵守：席位全程 HEAD 恒 `aa0f3df`（调度器独立核：终态工作区=6 改未提交+5 未跟踪恰合席位终态）。
- 首段违规处理：全量 pytest 后台化+等待通知时客户端退出（票 §0「前台收齐禁后台化」违规）——调度器续跑令点名违规+三条收口指令；续段全前台收齐+进度档全档+零 git 写——违规-矫正闭环完整记档（诚实纪律面）。
- **调度器钉面误差披露（评审侧诚实记档）**：票锚 5「app.py L239 注册循环接入三 Adapter」**前提失实**——该循环=宿主 `tools` 形参管道（循环体注册的是宿主传入的 adapter，builtin 13 从不经此）；调度器把宿主参数管道误钉为 builtin 注册面。席位三腿根因取证（faulthandler 线程栈=事件环 idle+aiosqlite 停泊；HEAD 中和二分=还原三跟踪文件后 1.98s 绿/还文件后挂死复现；adapter 级二分=单件即挂）+根因定谳（recovery 紧凑夹具 context_limit=1000，trio catalog 估算 ~899 token 独占 900 可用预算→ContextLimitExceeded→run 先于 provider.stream 落 failed→`provider.crashed` 永不置位→`crashed.wait()` 永久等待）+矫正（trio 落两生产宿主组合面）=**§3「席位可依亲读细化但不翻面」条款正当行使**——票面意图「三工具随宿主注册入面+census 13→16」经双宿主组合面完整兑现。调度器流程改进项：锚定注册面须验形参语义（循环体注册的是谁的东西）非仅行号。

### 2. 决策锚落面（§3 十锚）— PASS

- 锚1 三新文件：patch 独立+todo/question 各自单文件（席位定记档）；常量形沿 edit.py；`__init__` 翻新+**Adapter 类入 `__all__`**（双射 census 守卫联动——组合面带 trio 则 export surface 必带其类，翻转因由档）。
- 锚2 patch：`patch__apply`；PY 自有解析器逐语义移植（信封/三头/`*** Move to:`/`@@` 锚上下文/Add 体全 `+`/Delete 无体+JS trim Unicode 面+heredoc 剥离+`*** End of File` 锚+BOM split/joinBom+CRLF）；roots+`PATCH_MAX_SOURCE_BYTES`=1MiB 沿 edit 形；**事务语义席位裁定=prepare-everything-then-apply**（拒绝补丁零触碰+apply 逐序+失败即停+已应用清单 `. Completed before failure:` 如实）——TS `Effect.forEach` 形，票开放裁决闭合；失败面 fail-closed（存在性三失败 VALIDATION_ERROR/读上限 UTF-8 TOOL_FAILED/roots 越界 PERMISSION_DENIED）；权限 "ask" 突变族先例；side_effects=True+**idempotency=False**（席位裁——语义正确：补丁不可重放）。
- 锚3 todowrite：`todo__write`；无状态回显全量替换（零持久化零会话态）；四值枚举 `pending/in_progress/completed/cancelled`（TS 字面）；输出=TS summarize 逐字（分数先行+零计数抑制+in-progress JSON 引号）；恒注册（experimental 门不移植 DIFFERENT-BY-DESIGN）；allow/false/true。
- 锚4 question：`question__ask`；结构化出题即返非阻塞；条目恰键集 `{question, header, options:[{label,description}], multiple?}`；输出 text+structured_output/metadata（multiple 缺省补 False）；**无 answers 键**——应答=对话下一 user turn 自然承载（TS Form 阻塞回填+CancelledError 缺陷隧道不移植 DIFFERENT-BY-DESIGN）；allow/false/true；描述席位自撰（TS 无 .txt）。
- 锚5 宿主接线：**落点矫正**（§1 述）——trio 落 cli.py `_drive` tools 元组+serving `_builtin_adapters` 两生产宿主组合面（互相镜像 same order；宿主 16/16）；无工具 App 组合保持无工具（工具主权归宿主，WorkflowRunAdapter 显式门先例同精神）；app.py 复位 HEAD 字节。
- 锚6 policy 三行恰合（ask/allow/allow）+`_ASK_NAMESPACES` 零触碰。
- 锚7 契约面零触碰（numstat 证）。
- 锚8 CORE-CONTRACTS 零触碰：席位亲读=无逐工具 catalog 枚举——**调度器独立 grep 核证同判**（`fs__read` 等仅语义段内示例）。
- 锚9 描述文本：patch/todo 沿 .txt 英文原文+question 席位自撰。
- 锚10 非目标：既有 13 adapter+workflow 语义零回改（census 两处=纯加法面）；sdk 零触碰；`_ASK_NAMESPACES` 不动。

### 3. 测试面（§4 六项→27 测+两基线 census 翻新）— PASS

- §4-1 解析器矩阵 11 测：信封缺失/失配、Add 体无 `+`、Add 已存在（盘上+补丁内重复）、Delete 缺失+体行+链、Update 缺失（**测期发现的存在性预检缺口反哺实现**——测试先红后绿真实收口）/锚失配/`@@` 消歧/`*** End of File` 双臂/空行重试双侧 trim（对 TS 核正）/排版 seek 层级（later-exact-beats-earlier-normalized）/Move 链/混合/空信封/heredoc/CRLF/roots 越界 vs 扩展/1MiB+非 UTF-8。
- §4-2 应用端到端 8 测：四操作真文件系统（含锚第二匹配消歧）；in-patch 状态链（add→update 双跳）；**注入 `_atomic_replace` 失败的诚实已应用清单**；拒绝补丁零触碰；BOM 保真；eof 锚定。
- §4-3/4 todo 3 测+question 2 测：四值计数+空数组 cleared+无状态替换+fail-closed；双题渲染+structured_output 面+即返终态设计钉（无 answers 键）。
- §4-5/6 census/policy/Spec/export 4 测：**census 终形=机制矫正后重写形**（App 组合 composed 恰等集+`len==len(composed)` 15→16 pty 平台+trio 可解析+无 workflow 泄漏+provider.close_count==1）；policy 三行**行为级**断言（evaluate() effect+source="default" 非表检）；三 Spec 八字段钉（含 idempotency）；export surface 20 常量+3 类。
- 零回归腿：邻接 12 文件 231P（**test_recovery 全文件在内=挂死根治直接证**）+census 两文件 63P。
- collect 增量=27（2296=2269+27 恰合）。

### 4. 门禁与算术 — PASS（席位侧全档；调度器独立面占位待核证定稿）

- 席位自跑四件（全前台收齐）：ruff 0 / format 576 files exit 0（574 预期[570+4 新]+2 漂移——连续两席同 +2 漂移形，门绿为准）/ mypy 217-0（213 基+3 新 builtin+1 新测试文件=217 预期恰合）/ 全量 **1F+2287P+8S@639.29s** collected 2296=2269+27 恰合。
- **1F=winpty resize 时序抖动（新 flake 族首例，非 install 族）三腿裁定**：隔离腿=全文件重跑 4P 绿（失败测含内）；因果腿=子进程裸 stdlib 脚本+测试体纯 pty 机械零工具面（**调度器独立核证同判**）——与本票 diff 零因果交叠；环境腿=同窗机器竞争在场（他席全量套件 2h+）。结论=环境时序非代码因不阻断——裁定链可作后续同族先例。
- install 族**本窗 8/8 全绿**=4551 政策相位期中 lifting；**本席双态取证**（前段红态：隔离腿 5F/3P 全 spawn 型 4551+装载腿 rc=0+shim 腿红；后段绿态：全量窗 8/8+装载同绿+shim 仍红）=政策态时变直接证据（092/093/095/096 判据链引；shim 全席恒红与 096 翻红形续证）。
- 调度器独立四门禁（worktree p6-s4 真 python venv，`uv run --no-sync` 形，18:1x-18:3x 时段）：**ruff 0 / format exit 0 / mypy `Success: no issues found in 217 source files`（217-0 与席位侧同数）/ pytest `2288 passed, 8 skipped` 零失败 @636.72s exit 0——collected 2296=2269 基+27 新恰合；2288P=席位 2287P+1 winpty（本窗绿）=席位三腿时序抖动裁定独立复证（干净窗即过）；install 族本窗续绿（零 4551=相位续 lifted；席位双态取证续证）**——调度器窗环境基线独立核查=零外部竞争（老 python 进程全零 CPU idle）——零失败面免判据链定谳，install 族+全量双绿直接证明。CI 终证=CI-WATCH-245（#242 派发检查点 `aa0f3df` CI-WATCH-244 已绿=同基线树 CI 终证先行面）。

### 5. 语义面独立复核 — PASS

- 事务语义独立核：prepare 全过零触碰（拒绝补丁不触盘）+apply 逐序+失败即停+已应用清单——两相位边界清晰；Move=写新删旧两步+删失败诚实上报（「Wrote X but failed to remove Y」+已应用清单含 X）——禁半成静默兑现。
- 机制矫正根因独立核：因果链完整自洽（App `tools` 缺省空=工具主权归宿主；trio catalog 估算独占紧凑夹具 900 token 预算→ContextLimitExceeded→failed 先于 provider.stream→crashed 永不置位→挂死）；矫正后 test_recovery 全文件绿=根治直接证；census 终形重写与矫正机制一一对应。
- JSON 字符串入参裁定独立核：Invoker 严格 schema 子集无 array/enum（`_SUPPORTED_TYPE_NAMES`）——`todos`/`questions` JSON 编码+适配器内 `json.loads`+fail-closed 校验=协议约束下正确落形 DIFFERENT-BY-DESIGN（后继 Invoker 扩类型时可翻牌原生形）。
- 双宿主镜像独立核：cli.py drive 清单与 serving `_builtin_adapters` 同序同员（census 测试钉 same order）；**PatchApplyAdapter 宿主面无 roots 实参**=保守单根（严于 fs/git 族的 fail-closed 方向；`roots` kwarg 留测试扩展缝——测试④用 kwarg 直证）。
- todo summarize 逐字核：TS `todo.ts` 输出形（`0/N completed`分数+状态计数+in-progress 列表 JSON 引号）移植无漂移。
- policy 行为级核：三行默认经 `evaluate()` 行为断言+`source="default"`——非仅 dict 表检。

### 6. 披露与记档 — PASS

- 票 §5 七项全数落位进度档+席位余量至 **16 条**（含：Formatter 不移植/fileDiff 无先例不出/Form 面不移植/experimental 门不移植/事务语义裁定/MCP 不入票/question 自撰/Add-on-existing 拒收严于 TS[TS 静默覆盖]/未知键拒收严于 TS/存在性消息 display 相对路径措辞偏差/宿主面无 roots 实参/锚5 矫正+§6 逃逸理由/烟雾对拍 47/49 两例误标逐例核实/pty 抖动裁定/install 双态取证）。
- 首段违规-矫正闭环+两段弧时序全档。

## 发现明细

- **建议 1（观察面，无动作）**：调度器票锚 5 注册面前提失实（宿主参数管道误钉为 builtin 注册面）——流程改进：锚定注册面须验形参语义非行号；席位三腿根因取证+矫正为正面范本。
- **建议 2（观察面，无动作）**：winpty resize 时序抖动=新 flake 族首例——三腿裁定链（隔离绿+零因果交叠双核证+竞争在场）立为后续同族先例引用形。
- **建议 3（观察面，无动作）**：PatchApplyAdapter 宿主面无 `roots` 实参（保守单根）——后继若需与 fs/git 族 roots 扩展语义对齐可议 `roots=runtime.roots`（保守面无实害，现形合法）。
- **建议 4（观察面，无动作）**：todo/question JSON 字符串入参=Invoker schema 子集约束落形——后继 Invoker 扩 array/enum 支持时可翻牌原生数组入参（非本票面）。

## 附录：席位弧时序

- 发射（四探针相位窗张开形[5F+2256P+8S+4551 直证]+CI-WATCH-244 绿）→首段 SEAT-EXIT 0 @16:19:57（码面 27 测绿+静态三门绿，全量后台化违规=弧未闭合）→调度器续跑令（点名违规+前台收齐+补档三指令）→续段：亲读复验（cli/serving 组合面锚定）→**机制矫正**（app.py 还原+双宿主组合面+`__all__` 联动+两 census 翻新）→全前台四门禁收齐（639.29s）→进度档全档→SEAT-EXIT 0 第二次；零 git 写；看哨两窗（100min 到期+30min 尾窗 TRUE-EXIT）。
