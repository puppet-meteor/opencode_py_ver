# R3-S3 结构化审查（席位B · T-B-029）

> 审查对象：worktree r3-s3 **`7fa6e08`**（T-C1-027 交付，8 files +1803/−7）vs 基线 main `0020f68`（#106，r3-s3 分叉点）。diff 取法 `git -C <worktree> diff 0020f68 7fa6e08`（只读）。
> 审查人：席位B（T-B-029），止损 40min 内完成。写权限：仅本报告（新增单文件）；git 零写。
> 门禁基线：R3-S1 并回后 703/68/双绿；worktree 现值 **747 passed（703+44，零跳过）/ mypy 73 files / 双 ruff 绿**——调度器亲跑复验（任务⑥免重复项），本审查未复跑。
> 调度器已验项（本报告直接引用、未重复执行）：四门禁复跑数字、7 文件 diff 全文亲读、read.py 三助手签名亲证（`_resolve_within_root`/`_clamp_line`/`_failure` 单源复用）、写权限面对账（8 文件零越权）、cli.py docstring 裁定（调度器已修已披露，B-baseline 含）。
> B 独立复核面：三工具文件全文亲读（ls 187L/glob 188L/grep 422L）+ cli.py/`builtin/__init__.py` diff + 44 测名全清单 + rg 测试基建段（L1-165）与三 rg 测试体（L588-631）+ TS 四锚亲证（glob.ts L17-40 / grep.ts L17-35 / filesystem.ts L60-75·L160-205 / source-baseline.json L19·L43·L44·L52）+ 进度文件全文 + S2 冻结面 git 零差量亲证。

## 结论

**PASS**（blocker 0 / major 0 / **minor 1** / **nit 2**）。

三件静态读工具与 spec（r3-slices §2.3）和验收 A 前半逐句对齐：输入面逐字段匹配 TS 锚；数量限制+截断标记语义落地（含两边界测试）；路径三档+单源复用+glob pattern 护栏；rg 外部依赖 PATH 发现+版本自证+两档明确诊断+零静默回退；ls 参照链严谨（无 TS 对应物行为面仅两处字符串且已声明）。S2 并行冻结面（registry/invoker/service/app）git 零差量亲证。唯一 minor 为 rg 子进程超时在零输出挂起场景不可达（TS 同名常量为整进程超时语义，Python 落地为行到达时检查——病态首目标下工具调用无限挂起；③-H 已如实申报未测）；处方=记档+S5 受控子进程环境收编，不阻塞合并。

## 1. 审查维度逐条（12）

**维 1 · 三件 spec 面 vs §5.1 L251-253** ✓：三 ToolSpec 全字段齐（namespace="fs"/name/version=1/description/input_schema/supported_content_types=["text"]/side_effects=False/idempotency_support=True，三文件亲读）；register/snapshot 经公共面+重复 (namespace,name) 拒绝由契约测试钉死（`test_trio_quartet_registers_through_public_face_and_snapshots` L119——真 ToolRegistry+四件装配；`test_trio_registration_rejects_duplicate_wire_names` L133）；8 契约测覆盖 namespace/wire/注册/只读元数据/校验子集/TS 锚字段/限额（L104-193 全清单核对）。

**维 2 · wire 名纪律（§5.1 L255）** ✓：`fs__ls`/`fs__glob`/`fs__grep` reversible（`test_trio_wire_names_partition_reversibly` L112）；快照内解析；未知名行为面=既有 invoker 零触碰（维 12 冻结亲证）。

**维 3 · 输入 schema vs TS 锚逐字段** ✓：glob=glob.ts L17-28 原面（pattern 必填+path?/hidden?/limit?）；grep=grep.ts L17-35 原面（pattern 必填+path?/include?/literal?/caseSensitive?/limit?——TS 锚 L19 minLength(1) "Pattern must not be empty" 亲证）；ls=glob 面去 pattern（required=[]）；三件 additionalProperties=False；严格校验子集纪律成立——grep 空 pattern 规则落 adapter 内 VALIDATION_ERROR（grep.py `_execute` 首段 `if not pattern`，措辞同 grep.ts L19），invoker 子集不含 minLength 的读法与 invoker.py 支持关键字面一致（子集测试 L153 另有钉定）。

**维 4 · 数量限制+truncated 语义** ✓：默认 100（filesystem.ts L66 `DEFAULT_SEARCH_LIMIT = 100` 亲证）；Python 硬上限 1000 经 `min(limit, MAX)` 钳制（量化偏差浮报）；glob/grep 为 limit+1 提前停+sort+slice(0,limit)（glob.py `if len(collected) > limit: break`；grep.py `_run_rg` 同款）；**ls 为全收集→排序→切片**——目录优先+按名排序语义要求先见全集，与 glob/grep 的提前停属合理异形，docstring 声明"bounded by the directory itself"；标记措辞沿 glob.ts L39 原文（亲证："Consider using a more specific path or pattern."），ls 去 "or pattern"（LS_TRUNCATION_SUFFIX 拼接实现，无 pattern 面）；恰好限额/超限 1 项两边界测试三件各有专项（`test_{ls,glob,grep}_limit_boundary_marks_truncation`）。

**维 5 · 路径越界三档+单源复用** ✓：`_resolve_within_root` 自 read.py **import 复用零复制**（三文件 import 行亲读；调用形态统一 `real, reason = _resolve_within_root(context.working_dir, path)`）；NUL→VALIDATION_ERROR/realpath 失败→NOT_FOUND/越界→PERMISSION_DENIED 映射三件各 `_reject` 落地（ls 用 "Path not found"、glob/grep 用 glob.ts L84 措辞 "Search path does not exist"）；词法 `..`/symlink/junction realpath 三档测试齐（junction win32 实测）；**glob pattern 护栏**（`_pattern_stays_inside`：空/绝对/`..` 组件→VALIDATION_ERROR，含反斜杠归一化）——stdlib glob 把 pattern 当路径解析可越界、rg 只过滤 cwd 下文件故 TS 无此护栏，Python 侧必要性论证成立（`test_glob_escaping_pattern_is_rejected`）。

**维 6 · rg 外部依赖面（OP-R3-1 保守读法）** ✓：`shutil.which("rg")` PATH 发现（binary.ts L69/L121 which 先例映射）+ `rg --version` 自证（banner startswith "ripgrep "，5s 版本探针超时，可暴露执行别名桩）+ 进程级缓存（`_RG_CACHE` 首次调用面）+ 两档诊断分开（"not found on PATH" vs "did not identify as ripgrep"，TOOL_FAILED 均含依赖名+修复指引+"no silent fallback search is performed"）+ **零纯 Python 回退**；binary.ts L155-158 SHA-256 verify 差异声明如实（PATH 模式无可哈希对象→版本自证替代）；pyproject/uv.lock 零触碰（stat 亲证）。`_discover_rg`/`_which_rg`/`_rg_version_ok` 单一替换点结构成立（④-1 接缝论证）。

**维 7 · rg 子进程纪律（点名复核①，详 §5-1）** ✓（附 minor m-1）：`--json` 流解析（type=="match" 过滤/非 match 记录含 begin/end/summary 跳过/bytes base64 回退替换解码）；limit+1 停止→kill→wait(5s) 收割（TimeoutExpired→returncode -1 回退）；stderr 排空守护线程（防管道死锁）；退出码 0/1 正常、early-stop 容忍非零、2=错误+stderr 细节（400 字符钳制）+"regex parse error"→"Invalid regex pattern"（grep.ts L162 措辞）/否则 "Unable to grep"（L163）；`-e pattern -- file_arg` 列表形参传递无 shell 注入面；单文件 8 KiB NUL 预检（read 先例）；(path,line) 排序后 grep.ts L44-55 分组渲染。**超时面独立意见见 m-1**。

**维 8 · ls 无 TS 对应物参照链（差异表 2）** ✓：输入面 glob.ts L16-27；列举语义 filesystem.ts L186（仅 file/directory，亲证 `item.type !== "file" && item.type !== "directory"`）/L191（目录尾随分隔符——TS 用 `path.sep` 平台相关，Python 归一 "/" 为声明偏差）/L196（目录优先再按名排序——localeCompare→朴素比较，声明偏差）；约束族 read.py+glob.ts L84-89 措辞；**仅两处无 TS 祖先字符串**（"No entries found"+标记去 "or pattern"）声明属实；limit/截断面为 L91 验收句强制（"glob/grep/ls 限制数量"）+glob.ts L39 措辞+filesystem.ts L66 常量锚，非臆造行为面——TS list 本无 limit（ListInput 仅 path?，L60-62 亲证），Python 侧加面有验收句背书且已声明。

**维 9 · 量化偏差声明核对** ✓：默认 100（锚）/硬上限 1000（Python 侧）/排序确定性见 n-1 精度注记/locale→朴素/30s（filesystem.ts L67 亲证）/预览 2000 字符钳制（`_clamp_line` 单源复用）/glob 无 .gitignore 感知（TS 走 rg 有）声明如实/grep 保留 rg 默认 hidden/ignore 行为（docstring 声明）。进度①-5/③ 与代码逐项对应。

**维 10 · 测试覆盖 vs ②映射 + ③漏报 + rg-present 读法（点名复核②，详 §5-2）** ✓：契约 8+集成 36 测名逐一存在（Select-String 全清单亲证）且与进度②映射一一对应；真实临时 workspace（tmp_path→conftest 审批 temproot）=L94；**③漏报抽查结果见 §2**。

**维 11 · ④浮报意见+两必答一裁定 concur** ✓：见 §3 表。

**维 12 · S2 并行面冻结核查** ✓：`git diff 0020f68 7fa6e08 -- registry.py invoker.py service.py app.py` **输出为空**（零差量亲证）；既有测试文件零触碰（diff 仅两新测试文件）；pyproject/uv.lock/CORE-CONTRACTS/r3-slices/source-baseline/CI/ORCHESTRATION 零触碰（stat 8 文件全在授权清单内）。

## 2. ③漏报抽查结果（R1-S8 教训方法）

- **文件级全对账**：diff --stat 8 文件 = ls.py/glob.py/grep.py（新增）+ builtin/__init__.py（三件导出）+ cli.py（import 行+L276 四元组+调度器已披露 docstring 行）+ 两新测试文件 + 进度文件——与授权清单 8 项精确一致，**无越权路径、无未披露文件**。
- **内容级**：三工具文件+cli/init diff 全文亲读，改动面与进度①-8 逐项对应，无未披露行为面；cli.py 三 hunk 均装配面（docstring 行=调度器裁定内修复，披露链完整）。
- **TS 锚指纹核验（硬输入⑤）**：glob.ts（source-baseline.json L43）/grep.ts（L44）/ripgrep/binary.ts（L52）/**filesystem.ts（L19，实际路径 `packages/core/src/filesystem.ts`）**均已在指纹内——进度①-5"已录"声称属实。附注：任务书锚路径写作 `tool/filesystem.ts` 为笔误（实际在 `packages/core/src/` 根，非 tool/ 子目录），不影响结论。
- **结论：③清单完整覆盖改动面，无漏报**（A-H 八档分档如实，A 十项未测防御均属防御/低概率分支且逐项有据）。

## 3. ④两必答+一裁定 concur 表（B 独立意见，给据）

| 项 | B 意见 | 依据 |
|---|---|---|
| OP-R3-1 落地形态（PATH 发现+版本自证+缓存+两档诊断+零新增依赖） | **concur** | 代码亲证（维 6）：保守读法全要素落地；零 pyproject/uv.lock 触碰；单一替换点接缝真实（`_discover_rg`/`_which_rg`/`_rg_version_ok` 三函数隔离）；"不静默降级"原则以显式 TOOL_FAILED 诊断+无回退实现；版本自证替代 SHA-256 的差异声明如实（PATH 模式无可哈希对象） |
| OP-R3-4 未触及 | **concur** | 四文件零差量亲证（维 12）；三件全走既有 register/snapshot/invoke 公共面，无 wire 转换语义面改动 |
| cli.py docstring 裁定（调度器顺手修） | **concur** | diff 亲证：仅一行 "the fs.read builtin"→"the fs builtins"（注释级零行为），B-baseline 含且披露链完整；进度⑤"cli.py 两行"表述与裁定一致（import 行+L276 装配行+docstring=调度器涟漪） |
| ④-4 rg 最低版本门槛缺失 | 意见：可接受，调度器裁量 | 版本自证已暴露"非 ripgrep"面；假想未来 rg 弃用 `--json` 以 "Unable to grep" 呈现（③-A-1 已申报）；如需门槛 `_rg_version_ok` 单行可加（解析 major 下限），非验收句要求 |
| ④-5 glob 引擎选型（stdlib vs rg） | **concur stdlib 保守** | rg 外部依赖收窄到唯一需要其语义的 fs__grep（最小依赖面原则）；.gitignore 无感知为已声明偏差；L91 未要求 glob 走 rg；接口稳定可后续换引擎 |

## 4. TS 收窄/差异声明核对

三文件 docstring 差异声明逐条与代码及 TS 锚对账：grep.py（排序确定性/预览钳制/1000 上限/单文件二进制预检 vs 目录委托 rg 内建/hidden-ignore 保留）✓；glob.py（stdlib 引擎/无 ignore 感知/排序/`..` pattern 护栏为 Python 特有面）✓；ls.py（locale→朴素/`path.sep`→"/"/1000 上限/两处无 TS 祖先字符串）✓。零"与 TS 同构"断言。**附注（n-1）**：glob/grep 的 "sorted … for deterministic output" 声明在截断分支仅保证已收集子集内次序确定——子集成员身份依赖枚举/遍历序（iglob 扫描序/rg 并行序），>limit 时跨机器非全确定；ls 无此问题（全收集后排序）。TS 同性质（ripgrep 序+slice），非缺陷，措辞精度注记。

## 5. 点名复核两项（给据）

**5-1 · rg 子进程超时可达性（→ m-1）**：`_run_rg` 的 30s deadline 仅在 stdout 行循环顶部检查（`for out_line in process.stdout: if time.monotonic() > deadline`）。两分支后果：(a) rg 零输出且不退出（首目标为 FIFO/特殊文件/死网络挂载）→ readline 永久阻塞 → 超时分支不可达 → 工具调用无限挂起（串行 invoker → 整 run 挂起）；(b) 单一大文件慢扫描（begin 后长时间无下一行）→ 阻塞窗口内 deadline 不生效，迟到或永不触发。**TS 基准为整进程超时**（filesystem.ts L67 常量的语义在 TS 由 Effect 超时包裹全搜索），Python 落地为行到达时检查——同名常量、弱执行面。病态场景概率低+验收句（L91）只要求"检查可用性"+③-H 已如实申报未测。**B 意见：minor 记档，推荐 S5 收编**——R3-S5 受控子进程环境（§5.3 L277 超时/取消/收树）落地时把 rg 读循环统一到该机制（watchdog 线程/poll+deadline/超时包裹），S5 任务书建议明列"rg 读循环超时收编"条款；调度器如需本切片热修亦可（一处 `_run_rg` 改动），但不阻塞合并。

**5-2 · rg-present 测试捆绑发现读法（→ 相容+n-2）**：`_real_rg_or_skip`（L143-151 亲读）PATH 优先，其次 `~/.vscode/extensions/*/bin/windows-x86_64/rg.exe`（真实 VS Code 捆绑 ripgrep 发行物，`sorted(...) [-1]` 取最新扩展位），找不到诚实 skip——**真二进制非 mock，与"真实链"标准相容**；捆绑路径为 win32 测试基建专选，不影响产品面（生产 `_discover_rg` 仅 PATH）。双向诚实性成立：`test_grep_real_path_absence` skipif rg-on-PATH（L617-618）→ 无 rg 机器跑真面、有 rg 机器由注入测试钉定（`_patch_rg_path` L154-157 整体替换 `_RG_CACHE`+`_which_rg`，monkeypatch 自动恢复）。**n-2**：该测试 L624 直接 `grep_module._RG_CACHE.clear()` 无恢复——进程级全局态测试卫生 nit；当前零实际影响（定义序在全部 rg 注入测试之后、字母序文件靠后、所有 rg-present 面均经 `_patch_rg_path` 整体替换缓存），处方：后续顺手改 monkeypatch.setattr 整体替换，非必须。

## 6. 发现清单

**m-1（minor）· rg 子进程超时零输出挂起不可达**：详 §5-1。30s 常量锚自 filesystem.ts L67 但执行面弱于 TS（行到达时检查 vs 整进程超时）；病态首目标下工具调用无限挂起。③-H 已申报。**处方**：记档+S5 任务书加"rg 读循环超时收编"条款（推荐）；或调度器裁量本切片热修。不阻塞合并。

**nit-1 · 确定性声明精度**：详 §4 附注——glob/grep 截断分支"确定性"仅覆盖已收集子集内次序，成员身份依赖枚举序；docstring/进度①-3 措辞可更精确。**处方**：记档（S8 evidence 书写时注意），零代码动作。

**nit-2 · `_RG_CACHE.clear()` 无恢复**：详 §5-2。**处方**：后续测试卫生顺手项，非必须。

## 7. 建议（调度器裁量，无阻塞）

- 按现状合并 r3-s3（四门禁绿+12 维全过+③无漏报+冻结面零差量）。
- R3-S5 任务书建议增列："受控子进程环境统一超时/收树机制落地时收编 `_run_rg` 读循环"（m-1 处方，一处改动）。
- nit-2 可随 S5 或后续任一触达该测试文件的切片顺手处理。

— 席位B（T-B-029），2026-11-04。基线 `0020f68` → 审查对象 `7fa6e08`；本报告引用的全部 file:line/测试名均经本会话亲证（调度器已验项除外，已标注）。
