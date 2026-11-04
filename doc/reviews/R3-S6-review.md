# R3-S6 评审报告（席位 B · T-B-032）

- **评审对象**：worktree r3-s6 B-baseline `5edd5cd` vs 基线 `069e835`（=检查点 #121 文档提交；代码面 = #120 `c02643e` 零差量）。
- **授权对账**：恰 22 路径 ↔ 16 授权项（name-only 亲证，映射见 §1 维 12）；diff 总量 +2094/−105；冻结面 diff 亲证为空；树净（仅 `?? doc/orchestration/logs/` 预期项）；`git log -1` 单提交自证。
- **调度器已验门禁（B 不复跑）**：pytest 881 = 838+43 / ruff clean / format 257 exit 0 / mypy 89 = 85+4。
- **B 独立复核面**：③漏报抽查五项 git 读命令亲跑（§4）；`config/loader.py` 669L + `config/__init__.py` 34L + 12 个修改 src 文件 diff 1147 行 + 两测试文件 689L 逐体全读；契约锚 §6 L283/285/287、§6.1 L289-291、§6.2 L293-298、L300、L302、§8 L356/L358、§9 L368 与主树契约原文逐锚对拍；cli.py exit-70 捕获子句原文亲证。
- **结论：PASS —— blocker 0 / major 0 / minor 1 / nit 2**（minor-1、nit-1、nit-2 见 §1 维 7/11、§3、§5）。

## §1 十二维逐维

**维 1 · 契约锚定**：全部锚点对拍成立。L283 四层链由 `test_full_hierarchy_builtin_global_project_layers_then_cli` 全链钉死（项目层压全局层、cwd 层压项目层、CLI 压项目层、内置层浮出 `timeout_s=30.0`/`safety_margin=0`）；项目根确定三分支各有专测（worktree 式 `.git` 文件标记 / 无标记 cwd 即根不落盘根 / `--project-root` 须 cwd 或祖先）。L285 三分合并律+未知字段拒绝+宿主键分权落维 2/3。L287 快照字段面+64KiB/256KiB 恰界四面+指令不提权（instructions 只进 system_prompt，结构性无执法面）+不自动执行（无任何执行面=结构性保证）。§6.1 L289-291：v1 固定域不可冒充（指纹硬编码测试+`Literal[2]`）、不静默补齐（upcast v1→`snapshot=None`、recover v1→停住）。§6.2 L293-298：canonical 前置验证（loader 侧 encode 预扫描）、固定序列化（共享 `canonical_json`）、摘要不入快照（`snapshot_hash` 为 `SessionCreatedData` 兄弟字段，fixture 形亲证）、读取重验（继承 validator+bad_hash fixture）、v1 固定指纹不变（三重自证，见点名②）。L300 Python 侧论证（loader 模块 docstring OP-R3-10 裁定全文）。L302：快照=行为基线（`create` 持久化+App 消费 `snapshot.limits.max_steps`）、恢复停住三测、凭据只存引用（快照无凭据字段，`endpoint_ref` 清洗后入快照——结构性）、无热重载（无重载面，物化一次）。§8 L356/L358：v1 定义并存注册+旧 fixture 可读+>v2 拒绝+显式确定性 upcaster。§9 L368：v1 语义零改动（点名②）。

**维 2 · 层级合并语义忠实度**：`_merge_json` 三分律（标量后层覆盖/映射递归/列表整体替换）+`_APPEND_LIST_PATHS`={instructions, permission.rules} 唯二追加例外，`test_merge_laws` 四断言面全钉；deepcopy 防回染（合并产物全新构，层不可回染）。类型失配 override 整体替换分支无直接构造用例（→nit-2(d)）。层级发现三分支测试见维 1。

**维 3 · 宿主信任拒绝面（点名③前置）**：`HOST_ONLY_KEYS` 五键（roots/endpoint/mcp/api_key/credentials）项目层 ×5 参数化拒绝（match="host-tier"），全局层可载（hierarchy 测试全局层合法载 model 等、roots 专测全局声明）。契约 L285 逐名对照：endpoint/roots/MCP/credentials 四名全中，api_key 为 Python 侧 credentials 的具体键形（Python 保留独立键，两键皆拒——无缺口）。绕行面排查：未知键 fail-closed 兜底（`test_unknown_field_is_a_config_error`）；项目层 instructions 仅限有界塑形 system_prompt（64KiB/源+256KiB 总量封顶，无权限/根/端点触达）；permission.rules 记档不执法（维 10）；roots 仅全局声明且唯一注入面=cli.py 六适配器构造点（diff 亲证无旁路构造）。**完备性成立**。

**维 4 · 指令装配与上限**：AGENTS.md 外到内+config 声明串按序装配；解析路径去重专测（全局配置目录兼项目根时同一 AGENTS.md 双层发现只计一次）；64KiB/源恰界（65536 过/65537 拒）+256KiB 总量恰界（4×65536 过/+1 字节拒）四面齐；surrogate 前置扫描具名 `ConfigError`（台账行 #19 闭合，非裸 UnicodeEncodeError）；不提权/不自动执行=结构性（loader 无任何执行/提权面，全量亲读）。

**维 5 · 快照 v2 字段面（点名②）**：`ConfigSnapshotV2` 独立模型非子类（docstring 明示 v1 冻结理由），全字段显式必填（extra="forbid"、missing limits 拒绝测、bool 拒绝测带具名 match、roots 空串拒绝测）；MCP ref+启动指纹整体省略（R4 面 ④ 披露，非空值伪造）；`endpoint_ref` 清洗（userinfo/query/fragment 剥离专测；http(s) 强制分支未直测→nit-2(e)；IPv6 括号卫兵未直测=席位已披露候选）；canonical/hash 泛化复用 §6.2 同一管线（v1 值域常量不变）。`temperature: float | None = None` 为 SnapshotLimits 唯一带默认字段——None=未设置语义而非伪造值（wire-shape 测试钉形；对照内置层数值默认，快照层不伪造数值），与"无伪造默认"精神相容，记档不计数。v1 固定指纹三重自证成立（点名②）。

**维 6 · upcaster 与版本演进门槛（行 #24 首个 py.* v2）**：`(py.session.created,1)/(·,2)` 并存注册专测（`test_both_versions_are_registered_side_by_side`）；`upcast_session_created` 确定性双测（v2 透传带快照 / v1→`snapshot=None` 不补齐）；`UnknownEventVersionError` 对 v3 不变，`events_unknown_version.json` 2→3 翻转正当（本切片注册 v2 后原前提破坏，翻 3 保前提——T-C1-025 先例，test_events.py 仅 +3 注释行）；旧 fixture 可读（§8 L356 最低门槛达标）。

**维 7 · roots 通道与多根遏制（点名①）**：六适配器 keyword-only `roots=()`（缺省空=R2 语义）+read.py 共享核四件（`_resolve_within_roots` 首成功/`_REASON_RANK` 分级聚合/`_effective_roots` working_dir 恒首/`_validate_roots`）+`_display_path` 跨盘卫兵；write.py 创建目标核经 resolver 注入（同聚合律）；缺省 roots=() 与 R2 逐位同义的双断言专测（绝对越界→PERMISSION_DENIED、working_dir 内相对缺失→NOT_FOUND）；快照 roots 与适配器 roots 同出 loader 单源（测试钉两侧一致）；ToolContext/invoker/app 零触碰（冻结面 diff 空）；跨根 symlink 逃逸真实拒绝（措辞→nit-1）。**缺口：glob/grep/ls 三适配器的多根面零直测，次根条目绝对形渲染零断言→minor-1**。

**维 8 · CLI 注入面**：参数面终形齐（--model/--context-limit 层感知 required 化、--config 显式路径须存在专测、--project-root 祖先规则专测、--output-reserve/--timeout-s/--max-steps 缺省 None→内置层）；解析期 exit-2 双测（无 config 缺 model、坏 JSON）vs resolve 期 exit-70——`except Exception: traceback.print_exc(); return 70` 宽捕获原文亲证，ConfigError 必落 70 面，与注释声明一致；直构点替换完整（六适配器 roots 注入+App 消费 runtime.settings+`create(runtime.snapshot)`）；R2 既有 CLI 测试零回归（838 存量全绿）。双次装载一致性→点名④。

**维 9 · 恢复停住语义（L302）**：`recover` 只重放已存 v2 快照——指令从记录文本回放（loader 结构性不重读盘），`test_recover_replays_the_recorded_v2_snapshot` 钉 settings/roots/snapshot 三面等值；v1 行→`RecoveryStoppedError`（match="v1 minimal config snapshot"）专测；working_dir 失效→停住（match="no longer exists"）专测；凭据引用失效/MCP 指纹变化=协议不可达档分档**正当**（S6 无 MCP 生产者，快照整体省略 MCP ref→条件不可构造；R4 生产者落地后补协议测试）。

**维 10 · S2 门挂接/审批面零回归**：policy.py 行 #28 nit-1 闭合——`"bash__run": "ask"` 显式键+引据注释（`_ASK_NAMESPACES` 同为 ask，bash 审批/契约存量测试全绿=零行为闭合）。permission rules 记档不执法的分立评估：适配器多根放行 × policy/Invoker 单根冻结面（read.py ①注记）——Phase 1 自洽（policy 按 action+决定门控而非路径遏制，R2 语义保持）；config 声明 rules 的执法接线= S7 审批面预期→§6 开放点 1。

**维 11 · 测试覆盖与对账**：43 = 16（test_config.py：V2 模型 7+wire 6+upcaster 2+limits 1）+ 27（test_config_loader.py：23 函数，host-only ×5 参数化→27 收集）= 881−838 算术 ✓；host-only ×5、恰界四面、surrogate 具名触发、CLI e2e（DB 内 `py.session.created` version=2 行直验+upcast→recover 与 resolve 交叉一致）、exit-2 ×2、R2 CLI 零回归（§2.6 明列验收项）全达标。点名四项各有专测承重。**唯一覆盖缺口=minor-1**：多根行为测试集中于 read/write 两适配器（含 symlink 逃逸），glob/grep/ls 三适配器既无 roots 构造测试亦无次根条目渲染断言——`_display_path` 的绝对形转换（".." 前缀与跨盘 ValueError 两分支）是本切片**新逻辑且零测试**；缓解：三处改动与 read/write 机械同型（同四行模式）、共享核已被覆盖、缺省面存量回归全绿。

**维 12 · 写权限面**：22 路径 ↔ 16 授权项逐一映射成立——config 包 2 文件=2 项；events.py、contracts `__init__`、cli.py、service.py、read.py、write.py、builtin `__init__`、policy.py 各 1 项（8）；四涟漪 ls/glob/grep/edit=1 项（4 路径）；test_config.py、test_config_loader.py=2 项；v2 fixture ×3=1 项（3 路径）；既有扩展恰 2（events_unknown_version.json 2→3 + test_events.py 注释）=1 项；进度文件=1 项。合计 16 项/22 路径 ✓。冻结面 diff 亲证空（invoker/registry/app/context/projector/state/process 全包/bash.py + v1 fixture ×3）；越权零声明核实；B 侧 git 零写（只读命令）。

## §2 点名复核四项（独立结论）

**① 多根遏制健全性——成立**。根序排队效应：`_effective_roots` working_dir 恒首+声明根去重追加，首成功语义下可双根解析的路径取 working_dir 副本（R2 逐位保持，docstring 明示）；理由分级聚合正确（realpath 逃逸 3 > 词法逃逸 2 > 缺失 1，跨根取最强证据——symlink 经全根逃逸时报 realpath 理由而非 missing）。跨根 symlink 逃逸测试为真实力度（真实 FS 对象+真实遏制检查，非 mock；本环境全量零跳过=symlink 实际建成），机制等价性成立：strict realpath 对 symlink 与 junction 均解析终目标、逐根 containment of realpathed root 拒绝等价——安全结论不受 nit-1 措辞影响。空 roots 防御分支经 `_effective_roots` 协议上不可达（防御档分档正确；代码 docstring 已具注记）。

**② v1 零改动亲证——成立**。events.py diff hunk 逐块核对：`@@ L131`（compute_snapshot_hash 签名泛化，区域**外**的共享助手，v1 值域常量与管线不变）、`@@ L165+`（新类插入于 ConfigSnapshot 类体之后）、`@@ L182+`（V2 子类插于 v1 类之后）、`@@ L757+`（新定义+注册表追加）——**L143-165 v1 模型区零 hunk**。v1 解码路径错误信息不变（service 路由将非 v2 dict 全部送回冻结 v1 模型，docstring 明示 unchanged）。固定指纹三重自证齐：硬编码 `R1_SNAPSHOT_DIGEST` 测试 + v1 fixture ×3 零改（冻结面 diff 空）+ §6.2 常量文本指纹与契约 L298 原文一致；外加 v1 fixture 可读测试与 (·,1) 注册解析测试。亲证力度充分。

**③ 宿主信任拒绝完备性——成立**。五键对照契约 L285 宿主配置枚举（endpoint、workspace roots、MCP 定义、credentials）逐名全中，api_key 为 credentials 的 Python 键形，无遗漏宿主键；非枚举键由 unknown-field fail-closed 兜底（完备性双保险）。项目层绕行面排查：instructions 有界塑形不提权（L287 语义边界内）、permission.rules 记档不执法（无运行时效果）、roots 无任何旁路注入面（唯一构造点=cli.py 六适配器）。五键覆盖完整。

**④ CLI 双次装载——注记成立，concur**。解析期（exit-2 早断面：完整性+坏 config）与 resolve 期（完整运行面→exit-70）双次读盘，构造参数一致。两次装载间漂移窗（ms 级 TOCTOU）的后果分析：resolve 期输出自洽且为权威（全量再验证），解析期检查仅 UX 早断——漂移最坏情形=exit-2 判据基于过时值，无正确性洞（fail-closed 方向：resolve 期 ConfigError→70 有宽捕获亲证）。L302 无热重载下进程内物化后磁盘不可达；跨进程=重启即新会话新快照（基线按会话持久化，语义自洽）。更优形态候选与登记建议见必答 1c。

## §3 必答三项

**1a（junction/symlink 措辞精度）——concur，登记级 nit-1 成立**。事实核：测试代码为 `Path.symlink_to(target)`（文件 symlink 非 junction）；Windows 建链需特权/开发者模式，`winerror 1314` 跳过分支在册但本环境未触发（全量零跳过⇒symlink 实际建成——"实跑"为真，"junction"名不副实）。等价性论证（点名①）使安全结论不受影响；但依引注纪律"宁缺毋假"，机制命名精度构成登记级 nit。**载体裁定：本报告记档（即此条，已完成）**；进度文件为已交付件不作回改；后续席报/进度措辞以"symlink 实跑（Windows 特权面，1314 跳过分支在册）"为基准表述（§5 建议）。

**1b（ls.py 改名+渲染面换共享 helper 的授权边界）——concur 边界内，沿 S2 裁定 a 追认**。名冲突必然性成立：read.py 导出共享 `_display_path(working_dir, target)`，与 ls.py 局部 `_display_path(working_dir, real_dir, name, is_dir)` 同名异签名，不改名则遮蔽/冲突——改名 `_display_entry` 为机械必然。渲染面换共享 helper 为必然连动：glob/grep/ls 旧渲染 `os.path.relpath(target, working_dir)` 对次根条目产生 ".." 越界形或 Windows 跨盘 `ValueError`——不换面则授权特性（多根结果展示）交付即坏。缺省 roots=() 下与 R2 逐位同义（in-root containment 保证无 ".." 形，渲染输出逐字节同形）。授权文本字面：item 9"机械涟漪四件同款：根解析调用点+可选 roots 构造参数"未列渲染面；item 35"六 fs 工具共用根边界面"实质涵盖多根结果的展示面。裁定：必然连动+缺省零变化+共享单源不漂移→**追认成立，不计缺陷**；登记=本报告记档；处方=后续任务书涟漪条款字面增列"渲染面（多根条目绝对形）"（§5）。

**1c（CLI 双次装载一致性）——concur 注记成立**。理由见点名④：Phase 1 无热重载（L302 末句）下，双次读盘不构成一致性缺陷——进程内物化后不可达、跨进程=重启即新会话（快照按会话持久化为行为基线）、漂移窗最坏后果为 UX 级非正确性级（resolve 期权威+fail-closed）。**更优形态候选：单次装载结果传递**——解析期 `load()` 所得 LayeredConfig 直接传入 `resolve_runtime`，消双读与漂移窗并省一次装载成本。**登记建议：不进台账**；作为 §6 开放点留 S7（CLI 审批面切片）裁量——S7 将再动 cli.py，届时一并重构成本最低。

**2（③④档浮报完整性独立清点）——浮报 0，新发现四项漏列候选（nit-2）+一处覆盖缺口（minor-1）**。席位六候选逐名独立核实（两测试文件逐体亲读）：(1) IPv6 括号分支——endpoint 测试用普通 host，无 IPv6 字面量，属实未直测 ✓；(2) exit-70 无专测——仅 exit-2 ×2+e2e happy，属实 ✓；(3) CLI 恰同值——hierarchy 测试用互异值（cli-model vs proj-model），属实 ✓；(4) 退化构造 permission 双追加——merge 测试仅同型良构 dict，属实 ✓；(5) `_validate_roots` 纯空入参——适配器构造仅合法 roots/缺省，属实 ✓；(6) AGENTS.md OSError——无不可读构造，属实 ✓。**六项全属实，零浮报**。新发现未列候选（皆未测防御/分支档）：(a) `_display_path` 跨盘 `ValueError` 绝对形回退分支（测试全同盘）；(c) `_coerce_config_snapshot` dict 且 `schema_version==2` 的 v2 路由分支（e2e 走 instance 面，dict 面仅 v1 存量覆盖）；(d) `_merge_json` 类型失配整体替换分支（无跨型构造）；(e) `_sanitize_endpoint` 非 http(s) 方案拒绝分支（无非 http(s) URL 测试）。另 (b) `_resolve_within_roots` 空 roots 聚合分支已有代码级注记披露（read.py docstring"①disclosure"），入档与否属进度文件组织裁量，不计漏报。minor-1（glob/grep/ls 多根面零直测）亦为席位披露面的漏项，主记于维 11。

**3（单段过程完整性）——兑现**。进度文件过程段自证 60min 单段零中断；git 侧证：单一 B-baseline 提交 `5edd5cd`、树净无 WIP 残留（仅预期 logs/）；对照 S4（中断+续行已披露）与 S5（单段）——S6 沿 S5 单段模式，证据面无反证。回合纪律硬条款兑现。

## §4 ③漏报抽查实录（B 义务亲跑）

1. `git -C <WT> diff 069e835 5edd5cd --stat` → 22 文件，+2094/−105，与授权清单对账一致。
2. `git -C <WT> diff 069e835 5edd5cd --name-only` → 恰 22 路径，逐一映射 16 授权项（映射明细见维 12）。
3. `git -C <WT> log -1 --oneline` → `5edd5cd` 单 B-baseline 提交自证。
4. `git -C <WT> status --porcelain` → 仅 `?? doc/orchestration/logs/`，预期项，无越权残留。
5. 冻结面 diff（invoker.py/registry.py/app.py/session context/projector/state/process 全包/bash.py + events_session_created_*.json ×3）→ **输出为空**，零触碰裁定面亲证。
6. fixtures+test_events diff → v2 fixture ×3 新增、`events_unknown_version.json` version 2→3、`test_events.py` +3 注释行，与既有扩展恰 2 项一致。
7. 进度文件引注抽查：契约锚 L283/L285/L287/L291/L293-298/L300/L302/L356/L358/L368 与主树契约原文逐锚一致；v1 指纹常量三处一致（契约 L298/测试硬编码/fixture）；进度引用测试名与实际测试文件逐名相符（16+23 函数+×5 参数化）。**未发现假引，"宁缺毋假"纪律满足**。

## §5 建议

**裁定：按现状合并（PASS）**——minor-1/nit-1/nit-2 均不阻塞，归入 S7 处置批：

1. **minor-1 处方**：S7 回归批补 glob/grep/ls 三适配器多根冒烟（roots 构造+次根路径允许+次根条目**绝对形渲染断言**）；`_display_path` 跨盘分支标注 skip-if-single-drive（Windows CI 双盘符构造受限）。
2. **nit-2 处方**：四分支候选 (a)(c)(d)(e) 与席位已披露六候选中专测价值最高者（exit-70 专测、`_coerce` dict-v2 路由）并入 S7 回归批；其余维持披露档。
3. **nit-1 处方**：本报告已记档；后续席报/进度对 Windows 符号链机制统一表述为"symlink 实跑（1314 跳过分支在册）"，不称 junction。
4. **任务书改进**：涟漪授权条款字面增列"渲染面（多根条目绝对形）"，避免字面边界缝隙再依赖追认裁定（沿 S2 裁定 a、本次 1b 先例）。
5. **CLI 单次装载形态候选**留 S7 裁量（与必答 1c 一致）。
6. **调度器台账落位备忘**（沿先例待处理）：S2 m-1（行 #27 恢复切片三条款）、S5 调度器②载体（invoker pre-execute 钩子→not_started 升级候选条件行）、S5 nit-1/nit-3 载体（行 #30 追加）、S4 nit-3 四档列示口径——本切片无新增台账行诉求。

## §6 开放点（仅存报告，不进台账）

1. config 声明 permission.rules 目前记档不执法——S7 审批面接线时，需对"适配器多根放行 × policy/Invoker 单根冻结面"分立（read.py ①注记）做一次复核，确认执法面与多根面的合成语义。
2. CLI 双次装载漂移窗（ms 级 TOCTOU，UX 级非正确性级）与单次装载更优形态候选——S7 CLI 面裁量。
3. 未测分支池合并清单（席位六候选+本报告 (a)(c)(d)(e) 四候选+minor-1 渲染面）——S7 回归批候选池。
4. 恢复停住两条件（凭据引用失效/MCP 指纹变化）当前协议不可达分档正当——R4 MCP 生产者落地后补协议测试并复核分档。
5. 跨根逃逸测试的 Windows 机制基准：symlink 实跑+1314 分支在册；junction 等价性依 strict realpath 论证成立——若后续引入 junction 专属构造测试，需显式开发者模式/特权声明与独立跳过分支。
