# Phase 2 模块切片拆分（p2-slices）

> 产出：[T-B-054] 2026-11-07 08:23:07（现读时钟）首版产出（R5 收官基线 main **#164 `447785f`** 后；Phase 1 全链闭合——R5 六切片+R5-A1 俱收口，checkpoint 链尾引 r5-slices §0.1 不重述；硬性输入①-⑥[T-B-054 任务书]逐条对照见 §7 自检清单）。
> 席位B（T-B-054）产出，**规划产出票**（先例 T-B-046→r5-slices），供调度器按序派发实现切片/微票。状态：**v1 候选序列就绪——模块选择序待定夺（OP-P2-1），未预写任何裁决**。
> 依据（全部本会话现读）：doc/PHASE2-MODULES.md（163L 全量——模块菜单 §1/依赖图/扩展规则 §2/各模块节 §3-§10 原文）、doc/PHASE1-SLIM.md（152L——Done L9-17+L19 留接+§4 故障矩阵 L118-132+§5 运行验证 L134-144+§6 缩减停点 L146-152）、doc/CORE-CONTRACTS.md（424L——六接缝契约）、doc/ROADMAP.md（122L——接缝扩展列 L39-46+M5 单 worker 红线 L48+组合预算表 L78-80）、doc/plans/r5-slices.md（187L 现态——格式先例+§0.1 链+§3 台账+§8 模板）、doc/adr/0002（81L——B.2-6 现态接线+B.4 未覆盖面）、doc/platform-support.md（103L——§9 已知限制 15 件实枚举）、doc/parity.md（provider 行 L28「待 MAAS smoke 终翻」态）、tools/r0/platform_probe.py（1078L——L318-331 等待环现态亲证未加固）、doc/orchestration/tasks/T-B-054.md（硬输入六项+纪律）、doc/ORCHESTRATION.md（§1 路径所有权+§2 门禁/时间戳/止损条款）。
> TS 锚纪律：**零新 TS 引用**——本文件以 Python 现态+在库文档原文为锚；PHASE2-MODULES 原文中的 TS 比较面（M2 shell 解析「以选定 TS 用例比较行为」/M4「TS Code Mode 语法子集」/M7「TS 用例可追踪清单」等）为**照录非新引用**；实现触达确需新 TS file:line 时先停下报调度器补 doc/source-baseline.json 指纹（58 条在册口径）后方可引。不宣称与 TS 同构。
> 开放点纪律：OP-P2-*（§6）不预写裁决；周估沿用 PHASE2-MODULES L5 粗估口径（「单人全职 + AI 的粗估，在模块 spike 后复估，不能据此承诺 TS 全量等价」）。

## 0. 范围锚定（Phase 1 收官现态+验收面+留接清单）

- **Phase 1 收官现态**：R5 六切片+R5-A1 全闭合（checkpoint 链尾 #164 行见 r5-slices §0.1，不重述细节）。基线门禁（#164 主树实跑台账口径）：**pytest 1134 / mypy 109 / ruff clean / format 350 files**（py+md 合计；B 本票预写复验 format **351**=350 树+调度器在飞任务书 md 1——算术亲证基线口径）。B 侧独立复证：#164 src 面（=r5-a1 `d653088`，纯文档增量）pytest **1134**[260.70s]/109（T-B-053 复跑，temproot r5-a1-b）。
- **Phase 1 验收面（Done 九项 L9-17 对账）**：**8 绿+L10「待」**——L9/L11-L17 逐项√（L17 ADR 子件随 #164 R5-A1 闭合全绿）；L10 前半（真实 MAAS 端点多轮文本+流式工具调用短会话）=**T-C1-048 动态条款**（用户外部输入等待面——方案存档 r5-slices §2.7 R5-MAAS）+后半（fake server 无网络复现）√（R0-R2 测试承载）。**不阻塞 Phase 2 规划**；Phase 1 完整收口（tag=OP-R5-4）以 T-C1-048 为前置——依赖关系本计划如实标注（§3 等待面+§6 注记）。L13（stdio MCP server 列出/调用工具）=R4 交付+组合根测试直组承载√；**生产路径 spawn+bind 接线=OP-R5-1 两路评估入本计划 §2.0（OP-P2-2 定夺）**。
- **Phase 2 留接清单（PHASE1-SLIM L19 原文照录）**：「可用性以“小到中等上下文的本地 coding 任务”为边界；超上下文预算明确停止。自动 compaction、skills、subagent、HTTP、插件、PTY、媒体和多 provider 留给 Phase 2。」——模块/卡映射：compaction/skills/subagent=M6 卡、HTTP=M5（server/SDK）+M6 HTTP MCP 卡、插件=M3、PTY=M2 子项、媒体=M6 image/voice 卡、多 provider=M6 第二 provider 卡。
- **ROADMAP 接缝扩展列（L39-46 要旨）**：S1→媒体、多 provider；S2→SSE、跨进程 feed；S3→MCP、插件、codemode；S4→workspace、kv、多实例；S5→skills、插件配置、reload；S6→CLI/HTTP 共用、subagent、compaction——各模块「前置」的契约落点。
- **时点态注记（非本票写面）**：ROADMAP L3「实现前的设计基线；本文的验收项尚未实现」与 L88 技术栈行「官方 mcp SDK」为 R0 期时点态——与现态（Phase 1 已收官+OP-R4-1 用户裁决=自研 MCP client，ADR-0002 决策 B）不一致；刷新归 A 席/调度器，本计划不代改。

### 0.1 Phase 1→Phase 2 交付面衔接（基线口径+模块菜单总览）

- **CI 十连绿链（调度台账锚照录；席位侧协议不可达，引注非实测）**：42260728594[#158]→42269916522[#159]→42283944194[#160——Windows 腿计时型瞬态红已法证裁定 failed-job rerun 绿，B 引用不重述]→42289983998[#161]→42290071459[#161 fixup]→42296809889[#162 主体]→42296887477[#162 fixup]→42305288937[#163 主体]→42305579724[#163 fixup]→42315178558[#164 success @ 08:07:33]。
- **模块菜单（PHASE2-MODULES §1 表 L9-17 原文照录）**：

| 模块 | 核心范围 | 前置 | 增量开发周 |
|---|---|---|---:|
| M1 存储扩展 | workspace/kv、已用库升级、同机多进程协调 | S4/S6/S2 | 2–4 |
| M2 本地执行扩展 | watcher、shell 解析、git、PTY；可选子项 | S3/S5/S6 | 3–5 |
| M3 插件 | 受信任 Python 插件 + 有限生命周期 hooks | S3/S5/S6 | 2–3 |
| M4 codemode | 受限代码解释执行的可用子集 | S1/S3/S6；M3 可选 | 2–4 |
| M5 client/server | 单用户本地 HTTP + Python 远程客户端 | S1/S2/S3/S4/S6 | 2–3 |
| M6 能力卡片 | skills、subagent、HTTP MCP、compaction、媒体等 | 按子卡片 | 单独估算 |
| M7 差异/负载验证 | 所选路径的深入测试与运行基线 | 已选能力；基础测试一直执行 | 1–2 |

- **范围假设（L19 原文）**：「M1 限同机 SQLite；M3 限受信任插件；M4 不含完整 TS 解释器移植；M5 限单用户本地服务。跨主机存储、恶意插件隔离、多租户平台分别立项，不塞进这些数字。」**组合预算（L21 原文）**：「组合预算含 Phase 1：core 6.5–9 周；core + M5 为 8.5–12 周；core + M1 + M5 + M7 为 11.5–18 周。其余模块按选择逐项相加，并为集成重新评估；M6 没有一个“全部两周”的总额度。」
- **依赖图（L25-30 原文照录）**：

```
Phase 1 ─┬─ M1 存储/同机多进程 ──────────────┐
         ├─ M2 本地执行 ── git 子项 ────────┤→ M6 worktree/snapshot
         ├─ M3 受信任插件 ── 可选 hooks ────┐
         ├─ M4 codemode ←──────────────────┘
         ├─ M5 单用户本地 client/server
         └─ M6 各能力：skills / subagent / HTTP MCP / compaction / 媒体
```

- **M5/M7 定位（L32 要旨）**：M5 首版不依赖 M1——单 server 进程持有 writer、多客户端经它调用；ASGI 必须以单 app/单 worker 启动（禁止 `--workers > 1`、多进程 reload 和同库第二个 server），启动获取 Phase 1 数据目录锁，检测到第二个 writer 直接失败报告 `Conflict`；多个 server 进程共享同一数据库才需要 M1。M7=所选版本的额外验证阶段，每个对外发布点执行、未选模块不阻碍发布（L155）。

## 1. 候选序列总表（B 推荐序+就绪度评估；**模块选择本身=OP-P2-1 定夺**）

**B 现势判定**：当前唯一在册使用场景=**本地 CLI 自用**（调度器驱动 REPL/单发——R0-R5 全链形态）；无外部 UI/跨进程消费者在册（platform-support §9 B4 deferred 能力清单佐证——HTTP client[M5]/skills 等在册为 deferred 态）。无场景输入时 B 给默认序；场景键控表并行在册（表尾）。

| 序位 | 模块/卡 | 前置/周估（§1 表照录） | Python 现态就绪度评估（本会话锚证） | B 推荐优先级与理由 |
|---|---|---|---|---|
| 1 | **M6-skills 卡** | S5 发现/S6 指令装载/S3 读取；无需 M3；0.5–1 周 | **最高**——前置接缝全闭合（S5 指令发现+不可变快照在库：ConfigSnapshot[events.py L147 等]）；`SkillCatalog` 零实现（grep 0 命中=纯新增面）；契约补充面窄（L130 前置） | 最小卡直兑现 §10 行 1「本地 CLI 自用按需增加 skills」；零模块前置；**R5-M1 probe 加固顺手承载候选（OP-P2-3）** |
| 2 | **M5** | S1/S2/S3/S4/S6；2–3 周 | **最高**——前置六接缝全闭合现证（1134 全绿）；命令面六件在（service.py create L534/prompt L636/cancel L839/reply_permission L929/messages L615/run_status L733+close L1067）；durable follow 在（eventlog.py L302）；`UnsupportedCapability` 错误类型在库（errors.py L25）；M5 首版不依赖 M1（L32）+单 worker 红线在册（ROADMAP L48） | 解锁面最大（外部 UI+程序化 SDK——L112「只选 M5 即可在单进程服务中接入外部 UI」）；规格最详（PHASE2-MODULES §7 全节） |
| 3 | **M2（git 子项先行）** | S3/S5/S6；3–5 周 | **高**——四子项独立可选（L63-68 子项表+L70「仅需要 git 的用户不必安装 watcher 或 PTY」）；git 子项 B 判读可零新依赖先行（卡片开始时按 L36 复核）；watcher/PTY=库 spike 前置（L65/L68） | 最大模块子项拆分降险；git 先行为 M6 worktree/snapshot 卡开路（依赖图 L25-26）；纯 CLI 自用场景可与 M5 对调（场景键控表行 1） |
| 按需 | M3→M4 | S3/S5/S6；2–3 周／S1/S3/S6+M3 可选；2–4 周 | M3=高（挂载点=配置发现/app 组合/PromptBuilder/catalog，L74）；M4=中（L90 语法子集+资源控制 spike 门槛；L95 spike 失败停 prototype） | 生态线触发（§10 行 4「先 M3 受信任插件；codemode 根据实际脚本需求选 M4」） |
| 按需 | M1 | S4/S6/S2；2–4 周 | 高（改动点=存量面扩展，L47）但**无需求牵引不动**——L32 多 server 共库才需；§10 行 3「同机多个执行服务：Phase 1 → M1 → M5 → M7」 | 多进程线触发时插到 M5 前 |
| 按需 | M6 其余卡 | 按子卡片（L118-128 卡表照录） | 各卡前置接缝全闭合；L130 契约补充前置（四详节卡=skills/subagent/HTTP MCP/compaction） | compaction=上下文压力触发即插任意位；HTTP MCP 卡=硬输入④ L131 e2e 直载载体（§3 行 4）；worktree/snapshot 卡=M2 git+M1 最小 workspace 存储 |
| 收口 | M7 | 已选能力；1–2 周 | 跟随性 | 每对外发布点执行（L150-153 发布门+L155）；未选模块不阻碍发布 |

**场景键控（PHASE2-MODULES §10 L159-163 原文照录→B 模块序映射）**：

| §10 原文场景行 | B 模块序映射 |
|---|---|
| 「本地 CLI 自用：Phase 1 已能交付；按需要增加 skills、compaction 或 M2。」 | skills 卡→M2-git 子项→compaction/watcher 按需——M5 延后 |
| 「程序化同进程使用：Phase 1 SessionService；需要跨进程/外部 UI 再选 M5。」 | M5（P2-S3..S5 三切片） |
| 「同机多个执行服务：Phase 1 → M1 → M5 → M7；跨主机另设计。」 | M1→M5→M7 |
| 「扩展生态：先 M3 受信任插件；codemode 根据实际脚本需求选 M4。」 | M3→M4（按需） |
| 「worktree/媒体：只做卡片列出的依赖，不强迫安装全部 Phase 2。」 | worktree/snapshot 卡=M2 git 子项+M1 最小 workspace 存储 |

**默认序依赖注**：各切片基线=派发时点 main 树实跑门禁+新增计数（1134/109 零回归红线延续，§4）；模块间硬序仅「M1→M5（多进程线）」「M2 git→M6 worktree 卡」「M3→M4（可选）」，其余可并行/调序。

## 2. 每模块切片详情（默认序前 3 给到切片粒度；其余卡片级摘要——够派发时再细拆）

**测试统摄条款（PHASE2-MODULES §2 L43 原文）**：「每模块同时交付正常路径、错误、取消、秘密处理和适用平台测试。标签不代替验收证据。」——各切片测试策略按此+PHASE1-SLIM §4 故障矩阵延续（L118-132 既有行零回退+按模块扩展矩阵行）。

### 2.0 P2-S0（条件性微票——OP-P2-2 路 A 定夺后执行）：OP-R5-1 生产 MCP 接线微票

- **两路评估（硬输入①；r5-slices §3 行 1 已定夺并入本规划——#163 ⑩+#164 台账补记）**：
  - **路 A（B 建议采纳）=设行最小接线票**：生产 CLI 路径对显式配置的 stdio server 执行 spawn+bind——闭合 Done L13「一个显式配置的 stdio MCP server 能列出和调用工具，断线不自动重跑有副作用的调用」的**生产路径可达性**（现态=注册不 spawn：cli.py `_compose_mcp_manager` L517-530 add_server 从不 spawn；run 级路由由组合根测试直组 MCPToolAdapter 承载[test_cli_r4.py L666]——T8 既定 #152 调度器裁决；ADR-0002 B.2-6 保守记现态零预写）。
  - **路 B（否决回退）=§6 L148 降级路**：PHASE1-SLIM L148 原文「预算超出时先移出 R4 的 MCP 扩展证明，发布为“core preview”；保留 S3 adapter 接口和工具契约」——Phase 1 已全量交付无超支，此路现势=纯记档不扩码：README MCP 节+parity 已知限制+platform-support §9 B2 行强化「不宣称 MCP 生产可用」。
- **B 载体建议：独立微票（非并入模块切片）**——理由：①栈全在库零实现差（mcp/ 四件+`bind_mcp_tools`[policy.py L658]+`MCPToolAdapter`[bridge.py L68]），接线面=cli.py/app.py 组合根+ConfigSnapshot server entries 生产消费；②面窄可 45-60min C 席微票承载（B 估——派发前 spike 复估）；③与 M6-HTTP MCP 卡正交（stdio 已自研 vs HTTP 卡=新 transport+契约扩展 L122/L136），合并不省预算反耦合；④直接闭合 R5 移交台账行 1。
- **前置确认（票内承载）**：#152 T8 裁决原文边界重读（本票翻转「注册不 spawn」语义——若裁决含 spawn 时机/catalog 注册/权限绑定边界条款，票面逐条对照）；ADR-0002 B.2-6+B.4 现态记录随接线翻新（doc/adr/ 写权归 A 席——调度器随票裁量）。
- **测试策略**：正常（配置 server entry→spawn→tools/list→tools/call 受审批往返）；失败（server 启动失败→failed 标记不空工具面[manager 既有]；断线→unknown 不自动重试[既有]）；平台（win32 Job Object/POSIX 进程组回收=stdio.py 既有面复用；CI 双 OS）。
- **路 B 记档票（若定夺 B）**：README/parity/platform-support §9 B2 三面记档 15min 量级，零代码零新测。

### 2.1 M6-skills 卡（默认序第 1——契约补充前置票+两实现切片）

- **契约补充前置（L130 原文）**：「上述四项的接口/事件是各模块开始前需要补充的契约，**不属于 core 0.1 已实现能力**；建立 fixture 并复估后才进入实现，不要求一期提前建空扩展框架。」——skills 契约补充票先行（写权路由=OP-P2-5）：`SkillCatalog` 接口面（list/activate 签名+审计命令语义）、durable activation 事件/快照字段、instructions snapshot 写入语义、fixture 形态（沿 92 JSON 先例）；**复估后**才派发实现切片。
- **目标（L120 最低验收原文）**：「`SkillCatalog.list/activate`；激活记录 source/path/hash/size；host 显式选择或受限触发，内容不授予权限，快照/恢复行为有 fixture」+L132 详节原文：「skills 的激活是可审计命令：`activate(skill_id, source, expected_hash)` 把经大小/路径/信任检查的正文写入 instructions snapshot；模型只能建议，宿主策略决定是否激活。durable activation 保存 bounded 正文、source/path/hash/size；恢复使用已保存内容，不悄悄重读磁盘。重新激活时 hash 变化要求确认。」
- **涉及接缝（ROADMAP L45/L46 扩展列对照）**：S5 发现（skills 目录/清单发现——config/ 扩展）、S6 指令装载（instructions snapshot+激活命令）、S3 读取（skill 正文读取走既有工具/权限语义——**内容不授予权限**红线）。
- **写面预估（L38 解冻路由）**：src/opencode/skills/（新模块目录）+config/ 发现扩展+session/ 装载接线（既有文件触碰=任务书逐文件显式授权）+app.py 组合入口（授权面）+tests/contract+integration 新测；pyproject/uv.lock 零触碰（零新依赖）。
- **切片拆分建议（C 席 45-90min 票形）**：**P2-S1**=发现+`SkillCatalog.list`（目录扫描/清单解析/大小边界+正例反例 fixture）+**顺手面：R5-M1 probe 加固（OP-P2-3 定夺承载）**——`tools/r0/platform_probe.py` L318-331 `_wait_for_tree_markers`（现态：exists()→read_text() 即读零重试+`int()` 半写行零容忍，本会话亲证）标记读 retry（短退避重读）+解析失败续轮询（不因 PermissionError/ValueError 提前失败）+tests/r0/test_platform_probe.py 零回归+加固分支落测（tools/ 非冻结面，15-30min——r5-slices §2.7 技术方案存档原文承载；服务发布检查 CI 稳定性）；**P2-S2**=`activate` 审计命令+instructions snapshot 写入+durable activation（bounded 正文+source/path/hash/size）+恢复用已保存内容+重激活 hash 变化确认+fixture/平台测试。
- **测试策略**：正常（发现→list→activate→snapshot 写入→恢复往返）；失败（hash 不符确认面/超 bounds 拒绝/清单损坏/目录缺失=明确报错不猜修）；平台（Windows/POSIX 路径分隔/大小写/权限位）；S5 矩阵行延续（快照决定行为/项目不能扩大宿主信任——L126）。
- **验收标准**：L120 逐句+L132 逐句+L36 卡片开始列出八项（用户场景/必需契约/修改点/可选依赖/失败行为/fixture/安装停用方式/数据升级方案——契约补充票承载）。

### 2.2 M5：单用户 client SDK+server（默认序第 2——三切片 P2-S3/S4/S5）

- **目标（L103 原文）**：「FastAPI 适配 SessionService 的 create/prompt/cancel/reply_permission/messages/run_status，查询和事件 feed 共用 core 契约；生成最小 OpenAPI 和 Python 客户端。」**改动点（L101 原文）**：「新 HTTP/SDK 目录、app 生命周期、可选依赖和错误到 HTTP 的映射；不重写 runner。」
- **过程模型红线（L104 要旨+ROADMAP L48 原文）**：「M5 首版固定单 server/单 ASGI worker，不能以多 worker 或 reload 破坏 Phase 1 的单 writer/任务所有权假设」——多 worker/多 server/热 reload 造成的任务/锁分裂在 M1 前均为 `UnsupportedCapability`（错误类型在库：errors.py L25）。
- **切片拆分**：**P2-S3（M5-A）**=server 骨架+命令面（FastAPI app 单 worker/loopback/单用户 token 认证+六命令 endpoint+错误到 HTTP 映射+相同 request_id 同输入返回原 run/不同输入冲突[RunRef 语义]+HTTP prompt 断线不默认取消 run）；**P2-S4（M5-B）**=durable SSE（`EventLog.follow`[eventlog.py L302 在库]承载+Last-Event-ID 编码 session+seq+拒绝跨 session/超前游标+断线从 cursor 补读/按事件 ID 去重+慢 SSE 客户端不阻塞 runner+首版不承诺回放 ephemeral token——L105 逐句）；**P2-S5（M5-C）**=Python 客户端 SDK+最小 OpenAPI+验收集成测试收口。
- **安全面（L106 原文）**：「默认仅 loopback、单用户 token 认证；审批只接受已认证宿主通道。限制请求/并发/输出大小，校验 Origin/CORS，防止网页借本机服务执行命令。」——三面落测。
- **写面预估（L38 解冻路由+OP-P2-4）**：src/opencode/httpapi/（新模块目录——命名任务书定）+src/opencode/clients/（或 sdk/）+app.py 生命周期接线（授权面）+**pyproject extras[fastapi/uvicorn]——pyproject/uv.lock 写权=OP-P2-4 调度器路由（沿 OP-R4-1/OP-R5-3 先例）**+tests/integration 新测。
- **测试策略（L110 验收原文）**：「客户端创建 session、prompt、读终态；断线/重连、重复请求、错误码、未认证请求、审批和 shutdown 都有集成测试。以单 worker 启动为正例，第二 worker/server、reload 复制或数据目录锁冲突为明确失败用例。」——平台面：loopback 绑定/端口占用 Windows/POSIX 双 CI。
- **验收标准**：L110 逐句+L112 边界句（「只选 M5 即可在单进程服务中接入外部 UI。多进程共享库先做 M1；公网/多租户需要额外的 TLS/部署、用户隔离、配额和权限设计，不包含在 2–3 周预算」——如实记档不越界）。

### 2.3 M2-git 子项（默认序第 3 首切片 P2-S6；其余子项卡片级）

- **目标（L67 约束原文）**：「先 status/diff，再提供受审批的 worktree 操作；记录 cwd/roots 和冲突，禁止静默覆盖未提交文件」+M2 验收（L70 原文）：「每个已选子项有平台与取消测试；运行中的配置快照不受文件事件偷偷修改；现有 S3 权限和退出行为不回退。仅需要 git 的用户不必安装 watcher 或 PTY。」
- **改动点（L61 原文）**：「tools adapter、ConfigSnapshot 的显式 reload capability、S6 资源管理；拆子项验收。」
- **切片拆分**：**P2-S6**=git status/diff 工具 adapter（受审批执行——S3 policy 面；输出限额/超时/取消沿用既有工具面）+平台（Windows/POSIX git 面双 CI）+取消测试；worktree 操作=后续切片（受审批+记录 cwd/roots/冲突+禁静默覆盖——连接 M6 worktree/snapshot 卡，依赖图 L25-26）。B 判读：git 子项可零新依赖先行（subprocess git 面），卡片开始时按 L36 列可选依赖复核。
- **其余子项卡片级摘要（约束/退出条件 L65/L66/L68 原文照录）**：watcher=「watchfiles 等库验证后接入；去重/合并变更，只提示或在 idle 显式创建新快照，不改变运行中审批」；shell 解析=「Bash/PowerShell 各自打样；以选定 TS 用例比较行为，解析失败继续 ask/deny；AST 分析也不是 OS 沙箱」；PTY=「Unix/Windows 分别验证 resize、Ctrl-C、编码、进程树退出，再确定库；未验证的平台不开启」——各自独立切片/卡，库引入走 OP-P2-4。

### 2.4 其余模块/卡片级摘要（卡片开始时按 L36 八项列出，够派发时再细拆）

- **M1（§3）**：改动点（L47 原文）「S4 迁移/事务、S6 owner 检查、S2 follow 适配；不是只新增 repo 文件」；验收（L55 原文）「Phase 1 旧库数据保留；双实例只有一个 owner，过期 owner 的 DB 写被拒绝；cursor 重连无 durable 缺失；升级和备份恢复实测通过」；fencing 边界（L57 原文）「fencing 只能约束提交，不能撤销已经执行的 shell/MCP。接管未结束 run 时仍标 unknown，不能靠租约自动重跑副作用。未验证进程协调前保留单 writer 模式」；日志裁剪/归档先设计 cursor 过期与恢复策略、首版不以「清理日志」为验收（L52）；载体注：多进程线触发（§10 行 3），触发时插 M5 前；迁移面=新 revision 追加（§4 纪律）。
- **M3（§5）**：验收（L82 原文）「一个插件修改提示并注册工具，干净卸载；两个 hook 顺序确定；异常不会跳过审批；新增依赖未安装时 core 正常」；信任边界（L84 原文）「Python 进程内插件拥有宿主进程权限……首版只接受受信任插件」——不可信插件=进程隔离/OS 限制/IPC/secret 设计另行立项；载体注：生态线触发（§10 行 4）；M4 的可选接入方式（L88）。
- **M4（§6）**：spike 前置（L90「先确定要支持的 TS Code Mode 语法子集和工具语义，再做解释执行与资源控制 spike」——**TS 语法子集确定触达 TS 源=先补指纹纪律**；完整移植需单独拆项）；红线（L93「不用裸 Python `exec/eval` 执行模型代码。解释器没有宿主 import、任意文件、网络或进程访问入口，工具 catalog 是显式允许能力」+L95「不能保证限制的引擎不默认启用；spike 失败先停在 prototype，不宣称沙箱」）；验收（L97）「支持语法的正常用例、未知语法、越权工具、无限循环、超限/取消都有测试；失败不能继续沿用旧执行上下文」。
- **M6 其余卡（L118-128 卡表+L132-140 四详节）**：subagent（L121/L134/L140——`SubagentRef(parent_run_id, child_session_id)`、enqueue/ack/dedupe、完成结果 durable、父取消/关闭回收、权限不大于父级、限制递归/并发/预算、显式队列不直接改父 runner 内存）；HTTP MCP（L122/L136——streamable-http 最小 fixture、endpoint allowlist、认证引用轮换、超时/断线 unknown、**catalog version/旧批准失效=硬输入④ L131 e2e 直载载体**；完整 OAuth 另计；ConfigSnapshot 新增 transport/auth_ref/server identity/catalog version）；自动 compaction（L123/L138——`context.compacted(covered_seq, retained_from, summary_hash, model, usage)`、阈值/锁/CAS 明确、失败/取消不替换原上下文、保留原始事件永不拆 call/result、物理删除另走 M1 归档设计）；第二 provider（L124——同一 fake/真实契约套件通过、不支持内容显式拒绝、provider native 数据可往返）；image 输入/输出（L125——大小/MIME/模型能力验证、拒绝任意路径/URL 读取）；websearch（L126——一家 API、引用来源、超时/配额/错误测试、搜索内容不构成批准）；voice（L127——格式/大小/成本和取消明确、依赖未安装不影响 core）；worktree/snapshot（L128——显式目录/快照引用和磁盘配额、失败清理/冲突/恢复有测试、不自动撤销未知外部副作用）。
- **M7（§9）**：不以移植多少行测试衡量（L144 原文）；预算面（L146——多 session/owner 冲突压测、慢消费者/大输出、流/事件/路径输入 fuzz、长时间运行资源泄漏检查、备份恢复演练；「测量并记录启动、首 token、事件延迟、内存与测试环境，缺少测量不写“性能对齐 TS”」）；发布门（L150-153：正常/故障/取消测试+升级 fixture+平台 CI 通过；不装 extras core 可用；旧 JSON/DB fixture 与旧客户端支持范围有记录；新增 event/capability 的 ADR、停用/升级/回退说明齐全）；执行点（L155「M7 可在每个对外发布点执行；未选的模块不阻碍发布。问题回归测试回到对应模块，不等最后统一补写」）。

## 3. R5 移交台账承接（硬输入①-④逐条对账+六行承接总表）

| # | 来源 | 项 | Phase 2 承载（B 建议；定夺归调度器） |
|---|---|---|---|
| 1 | r5-slices §3 行 1（**已定夺**：#163 ⑩+#164 补记） | OP-R5-1 bind_mcp_tools 生产接线两路评估 | **§2.0 P2-S0 承载**：路 A 独立微票（B 建议采纳）vs 路 B 记档——**OP-P2-2 定夺**；#152 T8 边界重读票内承载；ADR-0002 B.2-6/B.4 现态随接线翻新归 A 席 |
| 2 | r5-slices §3 行 4（**已定夺**：#163 ⑨+#165 补记） | R5-M1 probe 等待环加固（技术方案 §2.7 存档） | **§2.1 P2-S1 顺手面承载（B 建议）**：platform_probe.py L318-331 标记读 retry+解析失败续轮询+test_platform_probe 零回归+加固分支落测；**OP-P2-3 载体定夺**（备选=M2 首切片） |
| 3 | r5-slices §3 行 2（登记） | src 存量 34 处历史 TS mapping 注释清理评估（platform-support §9 D3 在册） | **维持登记不设行**：非义务低优先零行为面；Phase 2 各切片触达 src 对应文件时顺手清理（任务书沿用 r5-slices §3 顺手项纪律条款）；M1/M2/M3/M5 触达面=首批自然载体 |
| 4 | r5-slices §3 行 3（登记） | 故障矩阵 L131 后两句（新 catalog/boot_id 旧批准失效）e2e 直载（platform-support §9 B3 在册） | **M6-HTTP MCP 卡承载（B 建议）**：L122 最低验收天然含「catalog version/旧批准失效」——卡实现时 e2e 直载 L131 后两句（间接覆盖在册：S3 catalog/指纹 re-ask+S6 T7 boot_id 隔离+S2 断线 unknown）；**备选**=任何触达 catalog 轮换的切片顺手直载（r5-slices 原文先例） |
| 5 | r5-slices §3 行 5（登记） | R4 float-reported 边界全集 | **维持登记**：S5 已知限制清单化+S6 Done 对账逐件引注已承载；Phase 2 触达对应文件时顺手条款延续 |
| 6 | r5-slices §3 行 6（登记） | r3 登记级残留（frozen-invoker 结论/#28 条件触发/#30 六项顺手/#26 rg 测试卫生） | **维持登记**：Phase 2 各切片触达对应文件时顺手条款延续（沿 R4/R5 先例）；非义务不设独立行 |

**等待面（硬输入⑤——动态条款不阻塞规划）**：T-C1-048（真实 MAAS 短会话——r5-slices §2.7 R5-MAAS 方案存档）+OP-R5-4（tag 0.0.1→0.1.0+pyproject 版本行）=用户外部输入等待面（ROADMAP L72「外部端点/凭据等待另计」口径）；依赖其闭合的终态面：parity provider 行终翻（L28 在册「待」态）+发布检查 tag 门+Done L10 终翻+platform-support §9 D1 行翻牌——Phase 2 各切片**不预写其闭合**，触达上述文件时以「待 T-C1-048」现态承载。

## 4. 冻结面与回归红线

- **零回归红线延续**：pytest **1134+新增** 基线零回归（R5 收官基线延续——每切片任务书基线=派发时点 main 树实跑+新增计数）；mypy **109 基线** 零问题+新增文件计数如实；format 判定面=退出码 0（「N files」含 .md 合计——计数随新文件增长**以实跑为准记档**，T-B-041 nit-2 语义）。
- **src 解冻路由（PHASE2-MODULES §2 L38 原文）**：「新代码放模块目录；允许修改 `app.py` 组合入口、依赖声明、配置注册和相关 adapter，不承诺零改动。」——Phase 2 执行语义：新模块=新目录（skills/httpapi/clients 等）；src 既有文件触碰=**切片任务书逐文件显式授权面**（零默认）；CORE-CONTRACTS 触碰=OP-P2-5 裁定票承载。
- **迁移纪律（L42 原文）**：「新表/字段追加 Alembic revision，以真实 Phase 1 数据验证升级；停用模块保留数据。回退代码与回退 DB 分别记录，不能只打 tag。」——既有 0001_initial+0002_runs 冻结（本会话枚举亲证）；M1/skills durable activation 等 DB 面卡走新 revision+真实升级测（R5-S2 模式复用）。
- **可选依赖纪律（L41 原文）**：「可选依赖使用 extras/延迟导入；未安装模块不影响 core 启动，调用时报 UnsupportedCapability。」——错误类型在库（errors.py L25）；M5 fastapi/uvicorn、M2 watcher/PTY 库的 extras 块建立+uv.lock=OP-P2-4 调度器路由。
- **接口纪律（L39/L40 原文）**：「无真实变化的接口不提前抽象；新能力优先组合 adapter 或增加独立 capability，Protocol 必需方法变化需要版本评估」；「新 event/content 不意味着旧消费者能理解；进行能力协商，未知必需类型明确报错」——事件新增走契约补充票（L130）+未知能力拒绝既有面延续（CORE-CONTRACTS §8/R5-S4 对账在册）。
- **Windows 红线（PHASE1-SLIM L150 原文沿用）**：「如果 Windows 进程树回收验证失败，Windows 的 shell/MCP 执行能力不能发布为支持；继续修复或显式收缩发布平台并更新 Done/ROADMAP。CLI 纯对话成功不能替代子进程测试。」——M2 PTY/M5 平台面延续承载。

## 5. 风险表（模块级；沿 r5-slices §5 形态）

| 风险 | 面 | 缓解/证据要求 |
|---|---|---|
| 契约补充前置缺失即实现（L130 红线） | M6 四详节卡 | 各卡开工前契约补充票（OP-P2-5 路由）+fixture 建立+复估；缺则停下浮报 |
| M5 多 worker/reload 任务/锁分裂 | M5 | 单 worker 正例+第二 worker/server/reload 复制/数据目录锁冲突=明确失败用例（L104/L110）；ROADMAP L48 红线 |
| 本机服务被网页借用执行命令 | M5 | loopback+token+Origin/CORS 校验落测（L106）+请求/并发/输出限额 |
| skills 激活面绕权限/超界 | M6-skills | 内容不授予权限（L120）+大小/路径/信任检查+hash 变化确认+快照/恢复 fixture（L132） |
| compaction 上下文损坏/竞态 | M6-compaction | settled 边界捕获+expected_seq/CAS 校验+失败/取消/崩溃不替换原上下文+原始事件保留（L138）；永不拆 call/result |
| watcher 偷改运行中配置快照 | M2 | 只提示或 idle 显式新快照（L65）+ConfigSnapshot reload 显式 capability（L61）+L70 验收句落测 |
| M1 fencing 越权撤销副作用 | M1 | fencing 只约束提交（L57）；接管未结束 run 标 unknown；未验证协调前保留单 writer |
| M4 限制不可保证即宣称沙箱 | M4 | spike 失败停 prototype（L95）；裸 exec/eval 禁用+工具 catalog=显式允许能力（L93）；不宣称沙箱 |
| 新 event 旧消费者误读 | 全模块 | 能力协商+未知必需类型明确报错（L40）；UNSUPPORTED_CAPABILITY 既有面复用 |
| probe 加固引入 CI 回归 | P2-S1 顺手面 | test_platform_probe.py 零回归+加固分支落测（PermissionError/ValueError 注入）；tools/ 非冻结面 |
| TS 等价宣称越界 | M7/parity/各卡 | 零新 TS 引用纪律+「缺少测量不写“性能对齐 TS”」（L146）+同名字符串不构成行为一致证据（parity 在册） |
| 周估失真（L5 粗估口径） | 全模块 | spike 后复估纪律（L5+L130）；超支削减=PHASE1-SLIM §6 次序沿用（先可选能力后核心验证） |

## 6. 开放问题 OP-P2-*（不预写裁决，留 A/调度器/用户）

- **OP-P2-1**：模块/卡片选择序定夺——B 默认推荐序=**M6-skills 卡→M5→M2-git 子项**（§1 就绪度+理由；场景键控表并行）；用户/调度器。
- **OP-P2-2**：OP-R5-1 两路定夺（路 A 独立微票=B 建议 vs 路 B 降级记档）+P2-S0 派发形态——含 #152 T8 裁决边界复核；调度器。
- **OP-P2-3**：R5-M1 probe 加固载体定夺——B 建议=P2-S1 顺手面；备选=M2 首切片；调度器。
- **OP-P2-4**：可选依赖引入的 pyproject extras/uv.lock 写权路由（M5 fastapi/uvicorn、M2 watcher/PTY 库）——沿 OP-R4-1/OP-R5-3 先例（调度器亲改 vs 任务书显式授权单块）；调度器。
- **OP-P2-5**：Phase 2 契约增补（CORE-CONTRACTS）与后续 ADR 立卷的写权/派发形态（A 席线 vs 调度器亲改）——沿 OP-R5-5/OP-R5-3 先例；doc/CORE-CONTRACTS.md+doc/adr/ 所有权。
- **OP-P2-6**：故障矩阵 L131 后两句 e2e 直载载体确认——B 建议=M6-HTTP MCP 卡（L122 最低验收天然含）；备选=catalog 轮换触达票顺手；调度器。
- **等待面注记（非裁决项）**：T-C1-048/OP-R5-4=用户外部输入等待面（§3 尾段）；Phase 1 完整收口（tag）以其为前置，不阻塞 Phase 2 派发。

## 7. 硬性输入①-⑥对照自检清单

| 输入 | 落点 | 状态 |
|---|---|---|
| ① OP-R5-1 两路评估（#163 ⑩定夺/#164 补记——行 1 已翻牌） | §2.0 P2-S0 两路评估+载体建议（路 A 独立微票）+OP-P2-2；ADR-0002 B.2-6/B.4 现态引注 | ✓ |
| ② R5-M1 probe 加固（#163 ⑨——不设微票并入本规划） | §2.1 P2-S1 顺手面+OP-P2-3；技术方案 r5-slices §2.7 存档原文引用（platform_probe.py L318-331 现态亲证未加固） | ✓ |
| ③ r5-slices §3 行 2（34 处 TS 注释清理评估） | §3 行 3 维持登记+触达顺手票载体建议（platform-support §9 D3 在册佐证） | ✓ |
| ④ r5-slices §3 行 3（L131 后两句 e2e 直载） | §3 行 4：M6-HTTP MCP 卡承载（L122「catalog version/旧批准失效」天然含）+OP-P2-6+备选顺手票 | ✓ |
| ⑤ 基线与等待面（#164 1134/clean/350/109+CI 十连绿 42315178558；T-C1-048+OP-R5-4 动态条款+行 5/6 承接盘点） | 头部+§0/§0.1 基线口径（B format 351-1=350 算术亲证+十连链照录）+§3 等待面段+行 5/6 承载 | ✓ |
| ⑥ 纪律延续面（§4 矩阵 L118-132+§5 L134-144+§6 L146-152 含 L148 降级/L150 Windows） | §4 红线延续（L148=路 B 依据/L150 沿用/L42/L41/L38/L39/L40 照录）+§2 测试统摄条款+各切片测试策略（正常+失败+平台+矩阵行扩展）+§5 风险表 | ✓ |

## 8. 首切片任务书模板（供调度器直接取用派发 C 席；沿 r5-slices §8 形态 P2 适配）

```markdown
# T-C1-0XX · 席位C-1：P2-SX <模块/卡·切片标题>（<契约补充票>已入库前置——OP-P2-5 裁定）

【角色章程·C 实现者】（三角色流水线：A 仲裁者 / B 规划评审 / C 实现者；本会话由调度器驱动，你是席位C-1。以契约文本+切片方案（doc/plans/p2-slices.md §2.X）为唯一基准；禁止 git 写操作（提交唯一归调度器）；门禁自跑后回报数字；④ 强制浮报所有未测/协议不可达防御分支；⑤ 存量兼容回归零破坏）
- 工作目录：worktree `C:/Users/lyu/.local/share/opencode/worktree/<id>/p2-sX`（分支 p2-sX，基于 main 检查点 #NN——worktree HEAD 即基线，`git log -1` 自证）。
- 写权限（本任务显式授权，仅此）：<新模块目录逐文件+src 既有文件触碰逐文件显式授权（p2-slices §4 解冻路由）>。**其余一切冻结**：pyproject.toml/uv.lock/.github CI/source-baseline.json/CORE-CONTRACTS/既有迁移 revision 零触碰（extras 依赖引入=停下上浮 OP-P2-4；契约面=OP-P2-5 前置票）；测试暴露 src 缺陷→停下浮报不修，修复归调度器另行授权。
- 门禁（worktree 内自跑）：temproot 调度器预建路径（`$env:PYTEST_DEBUG_TEMPROOT` 指向）；四件：`uv run python -m pytest -q`（基线 **1134+新增** 零回归+新增如实计数——逐测落点入进度文件）/`uv run ruff check .`/`uv run ruff format --check .`（判定面=退出码 0；「N files」含 .md 以实跑为准）/`uv run python -m mypy src tools/r0 tools/r5 tests/r0 tests/contract tests/integration`（109 基线+新增零问题）；**写后终态复验**（T-B-053 起正向形态）：落盘后重跑双 ruff 确认终态数字。
- 止损：45min（ORCHESTRATION §2 re-arm 条款：到线即停，既有工作面中段可续跑至自然检查点；禁止开启新工作面）；进度文件会话起始即首建+分节时间戳现读时钟（先 Get-Date 后落盘单动作批，零占位零回填）。
- TS 锚纪律：零新 TS 引用；确需新 TS file:line→先停下报调度器补 doc/source-baseline.json 指纹（SHA256 调度器亲算录入）后方可引。不宣称与 TS 同构。
- 开放点纪律：p2-slices §6 OP-P2-* 不预写裁决——触及时按保守读法落地+进度④浮报，越出即停下待裁。
- 顺手项纪律：r5-slices §3 维持登记项（行 2/3/5/6——34 处 TS 注释/L131 e2e/float-reported/r3 残留）若本切片触达对应文件，按 p2-slices §3 处置列顺手补记并在进度⑤记档；<本票承载顺手面：R5-M1 probe 加固[若 P2-S1]／其他>。
- 测试纪律：正常+错误+取消+秘密处理+平台（PHASE2-MODULES §2 L43+PHASE1-SLIM §4 矩阵延续+按模块扩展矩阵行——§5 L134-144 标准命令+CI 双 OS+固定 SDK 版本）。

【任务】<逐项：spec 引用（p2-slices §2.X 目标/验收句原文+PHASE2-MODULES 对应节原文）+最低落测结构+每测落点要求（测名↔验收句映射入进度文件）>

【④ 强制浮报】协议不可达/未测防御/已测防御/注记四档（R2 定案模式）；不可构造分支如实披露。

【⑤ 存量兼容】全量基线 1134/109 零回归红线；授权清单外零触碰；既有测试文件零触碰；新增仅授权清单。

【进度文件】`doc/orchestration/progress/T-C1-0XX.md`：①方案审视发现（选型论证）②门禁四件数字+新增测试逐测落点 ③强制浮报清单 ④未决问题 ⑤存量兼容声明+顺手项记档。

【回报格式（回复正文，1-2 段）】完成度 / 门禁四件数字（含新增测数+写后终态复验）/ 落点概要 / ③④ 概要 / 是否有需调度器裁决项。
```
