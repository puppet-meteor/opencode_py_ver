# R5 实现切片拆分

> 产出：[T-B-046] 2026-11-07 首版产出（R4 收官 main **#156 `ef2b0b5`** 后；R4 全链闭合——S1..S8+微票收口+`r4-exit-evidence.md` 1199L 在库；硬性输入①-⑤[T-B-046 任务书]逐条对照见 §7 自检清单）。
> 席位B（T-B-046）产出，供调度器按序派发 C 席实现切片+三条并行线（微票/A 席 ADR 线/MAAS smoke 形态待定夺项）。状态：**R5 六切片全交付（S1-S6）——S6 评审与并回归调度器记档；T-C1-048 MAAS 待端点供给（动态条款）**（S1 已闭合：T-C1-043 PASS+T-B-047 0/0/1/1+并回 `9d8a163`；S2 已闭合：T-C1-044 PASS+T-B-048 0/1/0/3+并回 `f29e253`；S4 已闭合：T-C1-045 PASS+T-B-049 0/0/2/0+并回 `e88ec8a`；S3 已闭合：T-C1-046 PASS+T-B-050 0/0/0/0+并回 `f71c31c`；S5 已闭合：T-C1-047 PASS+T-B-051 0/0/0/2+并回 `1be7295`——**基线 1134/109**[format 341]；主树基线与 checkpoint 链见 §0.1）。
> 依据：doc/PHASE1-SLIM.md（§1 L9-19 Done 九项勾选+§2 L23/L28 推进规则+§3 表 L40 R5 行+R5 节 L109-116+§4 L118-132 必测故障矩阵+§5 L134-144 运行验证+§6 L146-152 缩减和停点）、doc/CORE-CONTRACTS.md（§1 L17 统一 JSON Schema R5 条款+§2-§7 六接缝定义+§8 兼容）、doc/ROADMAP.md（L62 core 0.1 稳定门+L72 外部端点等待另计）、doc/plans/r4-slices.md（格式先例+§0.1 链+§3 台账+§8 模板）、doc/reference/r4-exit-evidence.md（R4 收官基线+§7 移交台账）、doc/orchestration/tasks/T-B-046.md（硬输入五项+纪律）、doc/ORCHESTRATION.md（§1 席位路径所有权+§2 门禁/进度时间戳/止损 re-arm/temproot 条款）、doc/parity.md（71L 现态盘点对象）、doc/platform-support.md（66L 现态）、doc/adr/0001-core-architecture.md（43L 现态）、doc/evidence/r0/provider.md（真实端点证据脱敏先例）。
> TS 基准：`D:/26_code/opencode_py_ver/opencode-2.2.2/`（指纹 doc/source-baseline.json 58 条在册）。**R5 原则零新 TS 引用**——发布/文档/证据面以 Python 实测为准；parity 翻新确需新 TS file:line 对照时，先报调度器补指纹（SHA256 调度器亲算录入），未补不引。parity 表内既有 TS 路径为 R0 期在册引用，翻新复用不构成新引用。
> 拆分基准：按现行文档拆；A 席若修订 R5 定义，由调度器对齐后本文件再改版。

## 0. R5 范围锚定（摘自 PHASE1-SLIM §3 R5 节+§1/§2/§5/§6）

- 交付（L111 原文）："README、fixture 清单、parity 实测记录、ADR 更新、发布/备份操作说明。"
- 验收 A（L113 原文）："干净环境安装后运行 CLI 和同进程示例；缺配置、解释器或 rg 时明确提示；退出码区分成功/失败/中断。"
- 验收 B（L114 原文）："全量当前测试、类型、格式/静态检查通过；空库迁移、最早可用库 fixture、备份恢复演练完成。只有 0001 时验证空库/已有 0001 no-op，出现 0002 后必须验证真实升级。"
- 验收 C（L115 原文）："六个接缝的旧 JSON fixture 可读、未知能力显式拒绝；记录已支持平台和已知限制后才标记 core 0.1 稳定。"
- 验证（L116 原文）："下节发布检查 + 实际 MAAS 短会话，证据记录到 parity；不在此时补做此前缺失的基础测试。"
- 推进规则（L23/L28 原文）："测试从 R0 起交付，R5 负责汇总交付证据。"；"切片通过后保存可回退提交；tag 只在发布检查通过后创建，数据库回退另按备份方案执行。"
- 表行（L40 原文）：R5 发布基线｜基础预算 0.5 周｜前置 R4｜结束时可演示=可安装的 core 0.1 和完整验证记录。
- §5（L134-144）：标准命令五件（pytest/ruff check/ruff format --check/mypy src/alembic upgrade head）；L144"CI 至少有 Windows + Linux，固定测试 SDK 版本；真实 MAAS 作为单独显式 smoke，不是默认 CI 网络依赖"。
- §6（L146-152）：L148 削减次序（超支先移出 R4 的 MCP 扩展证明发布为 core preview；其次 REPL polish/非关键展示；不推迟错误恢复、权限、取消和实际支持平台的验证）+L150 Windows 进程树条款（现态=已实测验证[platform-support §5 在册]，非阻塞面；若后续失败才触发收缩发布面）+L152 后续契约变更 fixture 回归纪律。
- **契约侧 R5 条款（CORE-CONTRACTS §1 L17，PHASE1-SLIM L111 清单外的硬义务）**："统一 JSON Schema 在 R5 由实际模型的 `model_json_schema()` 汇总生成并锁定差异"（§2 Changelog L9 同源）——入 §2.4 切片承载。
- Done 九项（L9-17）：R5=逐项对账汇总（§2.6 收口承载），非新增功能义务；L116 红线=R5 不补做此前缺失的基础测试。

### 0.1 R4→R5 交付面衔接（现状基线 #156 `ef2b0b5`：pytest **1021**[主树实测 242.43s]/mypy **105** 零问题/双 ruff 绿[format 判定面=exit 0，「N files」含 .md 合计]/collect 1021；CI 双 OS 绿链 42228816565[#154]+42242948415[#155]+42244191726[fixup]——attempt 序列与 runner 病态取证全额见调度日志 #155/#156，B 引用不重述）

- **checkpoint 链尾**：#153（S7 收口+merge `59a7d61`——S8 起基线 1021/105）→ #154 `dd305db`（T-C1-042 任务书入库+S8 派发）→ #155 `c885d89`（R4 收官：T-C1-042 验收+T-B-045 PASS 0/0/1/3+轻微-1 修正 `b759ebe`+merge `bed6d7b`[r4-exit-evidence 1199L 入库]；主树门禁 1021/format 318/mypy 105——**R5 起基线=1021/105**）→ `a6d0152`（ORCHESTRATION 台账行修复，非基线变更）→ **#156 `ef2b0b5`**（CI 绿链记档+T-B-046 任务书入库——本票）→ **#157 `cabb717`**（r5-slices 入库+T-C1-043 任务书+S1 派发）→ **#158 `6a5476f`**（S1 闭合：B-baseline `cbf7cff` 并回 merge `9d8a163`+T-B-047 0/0/1/1——**S2 起基线=1028/106**）→ **#159**（S2 闭合：B-baseline `584f476` 并回 merge `f29e253`+T-B-048 0/1/0/3——**S4 起基线=1031/106**）→ **#160**（S4 闭合：B-baseline `8d93e5d` 并回 merge `e88ec8a`+T-B-049 PASS 0/0/2/0——**S3 起基线=1134/109**）→ **#161**（S3 闭合：B-baseline `1bf5258` 并回 merge `f71c31c`+T-B-050 PASS 0/0/0/0——**S5 起基线=1134/109**[format 338，worktree 口径；主树含评审+任务书以实跑为准记档]）→ **#162**（S5 闭合：B-baseline `313cee1` 并回 merge `1be7295`+T-B-051 PASS 0/0/0/2——**S6 起基线=1134/109**[format 341，worktree 口径；主树含评审+任务书以实跑为准记档]）。
- **R4 交付面已在**：六接缝全量+MCP stdio 栈+启动恢复协调器+CLI 会话选择+审批重启+e2e 审计面（r4-exit-evidence 附录 A 42 组 1021 枚举在库）；src 面冻结=基线 1021/105，R5 各切片零回归红线。
- **交付物现状（硬输入①，B 复核闭合）**：根 **README.md 缺失**（Test-Path 亲证）；`doc/parity.md` 71L=R0 期规划面（L3「当前没有 Python 实现」已失真；L5「12 个关键文件」vs 指纹册现 58 条失真；§2 表 9 行 planned+5 行 deferred 待逐行翻 verified）；`doc/platform-support.md` 66L（CI 链引用 41182347042=R0-B 时点，待追加现行链+已知限制清单化）；`doc/adr/` 仅 0001（43L 规划基线态）；`tests/contract/fixtures` 92 JSON（六接缝正例/反例覆盖度=清单化交付物）；alembic 链=0001_initial+0002_runs 两版在库（验收 B 真实升级触发条件成立）；pyproject **无 `[project.scripts]`**（name=opencode-py+hatchling 单包声明亲证）——「安装后 `opencode` 进入 REPL」不可达，S1 承载。
- **CLI 现行面（S1/README 素材基线，实测亲证）**：`python -m opencode` 入口在（`__main__.py` 184B 委派 cli.main）；单发=positional prompt words（`opencode <words...>` 空格连接，缺省进 REPL——cli.py L234）；**Done L9 `opencode run --prompt "..."` 字面形态不在**（无 run 子命令/--prompt 旗标）→ OP-R5-6；退出码面=0/1/2（round 终态映射，cli.py L167 `_ROUND_EXIT_CODES`）/3（cleanup，L150）/4（selection，L152）/70（migration，L148）/130（SIGINT）——「成功/失败/中断」三分在（0/1/2+130），README 以全枚举实测为准；`--continue`/`--session` 选择面在（L280/L287）；alembic 迁移=CLI 启动 subprocess `python -m alembic upgrade head`（cli.py L330，幂等诚实失败）；bind_mcp_tools 生产零接线亲证（仅 policy.py 定义+测试调用——组合根测试直组承载，台账行 1）。
- **MAAS 行证据缺口**：parity §2 L28 provider 行翻 verified 依赖 L116「实际 MAAS 短会话」——R0-A 曾以 spike+fake server 落证据（doc/evidence/r0/provider.md 先例），真实端点短会话形态=OP-R5-2 待定夺。

## 1. 切片序列总表

| ID | 一句话目标 | 面 | 前置 | C 席预计 |
|---|---|---|---|---|
| R5-S1 | 可安装命令面：console entry（pyproject scripts 块+干净环境安装 smoke）+退出码/缺件提示面实测+脚本调用形态对齐评估 | pyproject.toml（写面路由=OP-R5-3）、cli 素材、tests 扩 | 无 | 45-60min |
| R5-S2 | 迁移与备份演练：空库迁移/0001→0002 真实升级/head 幂等 no-op 三面+备份恢复演练+落测 | tests（新）、migrations 只读复用 | 无（**可与 S1/S4 并行**） | 45-60min |
| R5-S3 | README+发布/备份操作说明：安装/运行/同进程示例/退出码/缺件提示+发布检查清单实体化（tag 前置门） | README.md（新）、doc/（新） | S1+S2（素材） | 45-60min |
| R5-S4 | 契约证据面：统一 JSON Schema 汇总（`model_json_schema()`）+差异锁定+六接缝 fixture 清单 | tools（新）或 tests 承载、doc/reference（新） | 无（**可与 S1/S2 并行**） | 45-75min |
| R5-S5 | parity 逐行翻新+platform-support 刷新（CI 链追加+已知限制清单化） | doc/parity.md、doc/platform-support.md | S4（差异锁定引用面）；MAAS 行=OP-R5-2 条件 | 60-90min |
| R5-S6 | 发布检查执行+Done 九项对账+MAAS 短会话承载+r5-exit-evidence 收口+tag 前置门确认 | doc/reference（新）、doc/plans 状态翻牌、doc/evidence/r5/（若独立落盘） | S1-S5 | 60-75min |
| R5-M1（微票） | R0-B probe 等待环加固：标记读 retry+解析失败续轮询 | tools/r0/platform_probe.py L318-331、tests/r0 | 无（**随时并行**） | 15-30min |
| R5-A1（A 席线） | ADR 更新：0001 状态行刷新+0002（启动恢复协调器+MCP 自研 client）评估/撰写 | doc/adr/（**A 席所有权**） | 随 R5 窗口调度器派发 | A 席 |
| R5-MAAS（形态待定夺） | 真实 MAAS 短会话 smoke（显式非 CI）：多轮文本+流式工具调用，脱敏证据→parity | doc/evidence/r5/（建议）+doc/parity.md | 调度器定夺（B 建议=S5 前先行） | C-1 显式票 |

**依赖序**：S1∥S2∥S4 → S3（素材自 S1/S2）；S5 先行= S4；S6 收口全量；M1/A1/MAAS 并行线随调度器穿插。首切片基线=**1021/105**（硬输入④；零回归红线）。发布检查（S6）通过前**不创建 tag**（L28）；tag/版本/pyproject 版本行=调度器 git 写权（OP-R5-4）。

## 2. 每切片详情

### 2.1 R5-S1 可安装命令面

- **目标**：pyproject `[project.scripts]` 增 `opencode = "opencode.cli:main"`（写面路由=OP-R5-3：调度器亲改或任务书显式授权单块——pyproject 写权归调度器沿 OP-R4-1 先例，本项零依赖变更、uv.lock 不动）；干净环境安装验证：新 venv/temproot（调度器预建目录）`uv build`→wheel 安装（uv/pip 择一，任务书定）→构建面断言（wheel 含 opencode 包+入口点）→`opencode` 命令可用→REPL 面 smoke+单发 smoke（沿 test_cli.py 既有断言面）；退出码面全枚举实测（0/1/2/3/4/70/130 现行码表——成功/失败/中断三分映射钉入 README 素材）；缺件提示面实测：缺 rg（PATH 隔离→grep 工具报错面提示明确性评估）、缺 MAAS 配置（--base-url/--api-key+env 双缺→提示面）、缺解释器（子进程工具解释器解析失败面）——现状如实记录，提示不明确处④浮报不擅自扩码（加码归任务书授权或 A 席）。
- **涉及接缝**：pyproject.toml（仅 scripts 块+版本行[OP-R5-4]）、tests/integration/test_cli_install.py（新，或并入 test_cli.py 扩——C 席定）；安装类测试本地构建零外网。
- **新增文件**：tests 1 处；README 素材记进度文件（README 本体归 S3）。
- **TS 基准映射**：无（Python 打包面无 TS 蓝本）。
- **验收标准**：Done L9 前半+验收 A 前半（干净环境安装运行 CLI+缺件明确提示+退出码三分）；L116 红线（本切片=R5 新交付面，非回补既有缺失测试）。
- **测试计划**：安装 smoke（构建→装→命令在→REPL/单发往返）；退出码枚举（成功/失败/中断/清理/选择/迁移/SIGINT 各触发一测或 drill 记录）；缺 rg/缺配置/缺解释器三分支（可构造则落测，不可构造=drill+④如实）。
- **开放点**：OP-R5-3（scripts 块写权路由）/OP-R5-6（`run --prompt` 字面形态：补 run 子命令或 --prompt 旗标[小码面]vs README 记现行 positional 形+差异记档——A 席/调度器）。

### 2.2 R5-S2 迁移与备份演练

- **目标**：验收 B 后半落地——三面：①空库迁移（`alembic upgrade head`——既有承载=CLI 启动 subprocess cli.py L330）②**0001→0002 真实升级**：「最早可用库」=0001 停点库，测试内构造（upgrade 至 0001 停→断言 runs 表缺→upgrade head→断言 runs 表在+存量数据可读——不提交二进制库文件，DB 态由迁移链生成可重现；若 A 席要求字面 fixture 工件再改）③已在 head 库重复 upgrade 幂等 no-op；备份恢复演练：关闭态 DB（App close 后 WAL 收敛断言）整库复制→恢复→EventLog/投影读取对账——演练步骤+断言落测承载；「数据库回退另按备份方案执行」（L28）的备份方案实体归 S3 文档，本切片供素材。
- **涉及接缝**：tests/integration/test_migrations.py（新；只读复用 storage/app 既有面）；migrations 零触碰（0001/0002 冻结——演练暴露迁移缺陷→停下浮报，修复归调度器另行授权）。
- **TS 基准映射**：无（Python 专有迁移链；parity §3 L55「不按 TS 迁移链建 Python 库」）。
- **验收标准**：验收 B 逐句（空库/最早可用库/真实升级/备份恢复）；「出现 0002 后必须验证真实升级」触发条件成立（0002_runs 在库亲证）。
- **测试计划**：三面迁移测+WAL 收敛/备份往返测（关闭态复制=主文件自足主路径；打开态备份口径=不支持或需 checkpoint，任务书按 CORE-CONTRACTS §3.2 定）；drill 证据记进度文件+exit-evidence 素材。
- **开放点**：无预写（打开态备份口径任务书内定）。

### 2.3 R5-S3 README+发布/备份操作说明

- **目标**：根 **README.md 新建**——安装（uv/pip+Python 版本要求+rg 外部前置）、运行（REPL/单发/同进程示例=SessionService 最小片段[python 代码块须 ruff-format 合规——format 门禁含 .md，ORCHESTRATION §2]）、退出码三分全枚举（素材自 S1 实测）、缺配置/解释器/rg 提示面说明、parity/ADR/platform-support/备份文档互链；**发布/备份操作说明**（落点任务书定；B 建议独立 doc/ 文档便于 tag 流程引用）：**发布检查清单实体化**（L28+L116 两处提及的唯一承载物）——tag 创建前置门=门禁四件绿+CI 双 OS 绿 run id+验收 A/B/C 逐项√+Done 九项对账表在档+备份演练证据在档；备份/恢复操作步骤（自 S2 演练）；回退路径（tag 回退与 DB 回退分离——L28）。
- **涉及接缝**：README.md（新）、doc/ 发布说明（新）；零代码。
- **新增文件**：README.md+发布/备份说明 1 处。
- **TS 基准映射**：无。
- **验收标准**：Done L17 前半（README 运行说明+迁移/备份说明齐全）+验收 A 文档面+L28/L116 发布检查清单实体。
- **测试计划**：无新测（doc 面）；format 门禁合规自查（python 块）。
- **开放点**：发布说明落点（并入 README vs 独立文档）——任务书定。

### 2.4 R5-S4 契约证据面：JSON Schema 汇总+fixture 清单

- **目标**：CORE-CONTRACTS §1 L17 契约义务——统一 JSON Schema 由实际模型 `model_json_schema()` 汇总生成（六接缝 wire/persisted 模型全量）+**锁定差异**=生成物 vs 既有 92 fixture 形状对账（差异清单化：schema 未覆盖角落/未知字段拒绝面）+落盘形态（建议 doc/reference/schema/ JSON 工件+生成脚本 tools/r5/——任务书定）；**fixture 清单交付物**（L111）：六接缝（S1 历史消息/S2 live·durable feed/S3 工具权限/S4 事件写入·投影·数据库/S5 配置凭据/S6 应用命令·会话服务——CORE-CONTRACTS §2-§7）×正例/反例 fixture×行为测试×真实/fake 适配器测试四列矩阵+92 fixtures 枚举对账（Done L16 逐项）。
- **涉及接缝**：tools/r5/（新，生成脚本）或 tests 内承载（C 席定）；doc/reference（新）；contracts 模型**只读消费**（`model_json_schema()` 零触碰源）。
- **新增文件**：生成脚本+schema 工件+fixture 清单 doc（建议 doc/reference/r5-fixture-inventory.md）。
- **TS 基准映射**：无（Python 模型自述面）；「不是 JSON 字段转换器、Python 的版本 1 不能当作 TS 的同名版本」（parity §3 L41）——schema 工件不宣称 TS wire 兼容。
- **验收标准**：CORE-CONTRACTS L17 R5 条款闭合+Done L16 对账+验收 C 前半素材（六接缝旧 JSON fixture 可读+未知能力显式拒绝=既有测试对账面）。
- **测试计划**：schema 生成往返（重新生成==落盘工件，漂移检测）；92 fixtures 逐件归接缝（枚举机械转录沿 r4-exit-evidence 附录 A 先例）；未知能力拒绝面=UNSUPPORTED_CAPABILITY 既有测试对账（CORE-CONTRACTS §8 追认先例）。
- **开放点**：schema 工件落点与锁定形态（工件+漂移测 vs 仅工件）——任务书定。

### 2.5 R5-S5 parity 翻新+platform-support 刷新

- **目标**：`doc/parity.md` 逐行翻新——头部失真修正（L3「当前没有 Python 实现」→实测态声明；L5 指纹口径 12→58 刷新）；§2 表 9 行 planned 逐行翻 **verified**：每行补实际 Python 测试路径+正常/失败 fixture+运行平台/依赖版本+验证结果+仍有差异（§4 L59 纪律逐项）；provider 行（L28）翻 verified 依赖 MAAS 短会话证据（未执行则行内显式「待 MAAS smoke 终翻」——OP-R5-2 条件）；deferred 5 行维持+差异列复核；§3 事件映射/§5 R0 实测/T-A-003 两行复核刷新；`doc/platform-support.md`：CI 链**追加**现行绿链（42228816565/42242948415/42244191726+M1 加固后新 run）——R0-B 原 41182347042 **保留为历史实测锚不删**（「本地 manual_verified 与 CI ci_verified 互不冒充」纪律 L6）；「已知限制」清单化（验收 C 后半+R4 float-reported 边界引注[r4-exit-evidence §7 各评审件]）。
- **涉及接缝**：doc/parity.md、doc/platform-support.md；零代码零新测。
- **TS 基准映射**：既有 parity 表内 TS 路径为在册引用（非新引用）；翻新确需新 TS file:line→先报调度器补指纹（头部纪律）。
- **验收标准**：验收 C（记录已支持平台和已知限制后才标记 core 0.1 稳定——本切片=该标记的前置记录面）+L111「parity 实测记录」交付。
- **测试计划**：无新测；对账自检=每 verified 行测试路径 --collect-only/文件面现读亲证（沿 r2/r3 行号漂移教训：**全部现读重锚，禁继承行号**）。
- **开放点**：OP-R5-2（MAAS 行条件翻）。

### 2.6 R5-S6 发布检查+MAAS 短会话+Done 对账+R5 收口

- **目标**：**发布检查执行**（S3 清单逐项证据化：门禁四件终值+CI 双 OS 绿 run id+验收 A/B/C√+Done 九项对账表[L9-17 逐项↔证据 file:line/测试/run id 三方映射]）；MAAS 短会话承载（若未先行：按 OP-R5-2 裁决形态执行，证据记录到 parity——L116）；`doc/reference/r5-exit-evidence.md`（沿 r1-r4 模式：验收 A/B/C+验证行 L116+交付清单五项[L111]映射+切片收口+PHASE1-SLIM R5 节对照；继承行号必须重锚）；r5-slices 状态翻牌；tag 前置门确认清单（tag 创建本身=调度器 git 写权，OP-R5-4——版本 0.0.1→0.1.0 同步归调度器）。
- **涉及接缝**：doc/reference/r5-exit-evidence.md（新）、doc/plans/r5-slices.md（状态翻牌）、doc/evidence/r5/（若 MAAS 证据独立落盘）。
- **TS 基准映射**：无新引用（收口）。
- **验收标准**：L116 验证行全量（发布检查+实际 MAAS 短会话+证据记录到 parity+「不补做此前缺失基础测试」红线复核）+L23 R5 汇总义务闭合+ROADMAP L62 稳定门对账（真/假适配器+golden fixture+失败场景验证后才标 core 0.1 稳定）。
- **测试计划**：--collect-only 1021 终值枚举+门禁四件+CI run id 记档（沿 R4-S8 先例）。
- **开放点**：OP-R5-2（若 MAAS 未先行）/OP-R5-4（tag 形态与版本号）。

### 2.7 并行线（非 C 席主序列，随调度器穿插）

- **R5-M1 probe 加固微票**（台账行 4，建议设行）：`tools/r0/platform_probe.py` L318-331 `_wait_for_tree_markers`——Set-Content 写句柄共享冲突（exists()→read_text() 即读零重试）+`int()` 半写行零容忍（#155 fixup attempt-1 实测面）：标记读 retry（短退避重读）+解析失败续轮询（不因 PermissionError/ValueError 提前失败）；tests/r0/test_platform_probe.py 零回归+加固分支落测；服务发布检查 CI 稳定性；attempt-2/3 病态 runner 取证在调度日志，与本加固项独立不混同（硬输入③原文口径）。
- **R5-A1 ADR 更新（A 席线）**：0001 状态行刷新（「已采纳为规划基线；实现/平台行为尚待测试验证」→实测验证态+r4/r5 证据引用）；0002 评估/撰写=启动恢复协调器（Python 专有无 TS 蓝本——r4-slices §4 差异表 1）+MCP 自研最小 JSON-RPC stdio client（OP-R4-1 用户裁决）两决策；**doc/adr/ 写权归 A 席**（ORCHESTRATION §1 路径所有权）——B/C 零触碰，素材（各评审/exit-evidence 引注）由调度器随任务书供给。
- **R5-MAAS 短会话 smoke（形态待定夺=OP-R5-2）**：B 方案——**C-1 显式本地票**（非 CI 网络，L144）；时点建议 S5 前（parity 一次翻全）；证据面建议 `doc/evidence/r5/maas-session.md`（脱敏沿 r0/platform.json `_leak_self_check`+provider.md 先例：主机路径/凭据/endpoint 占位化，prompt 以最小无害示例记录）+parity provider 行终翻；端点/凭据=调度器供给（外部输入等待另计——ROADMAP L72）；Done L10 语义=「多轮文本+流式工具调用」最小短会话（≥2 轮/≥1 工具流式）+fake server 无网络复现已由 R0-R2 测试承载（对账引用即可）。

## 3. R5 台账（硬输入③移交四件分流+R4 在册残留再分流）

| # | 项 | 来源 | 处置（建议；定夺归调度器/A） |
|---|---|---|---|
| 1 | bind_mcp_tools 生产 spawn+bind 接线立项评估 | r4-exit-evidence §7（T-B-044 处方 5）+硬输入③ | **OP-R5-1 待定夺**：B 建议设行最小接线票——Done L13「一个显式配置的 stdio MCP server 能列出和调用工具」生产路径可达性（现态=组合根测试直组 MCPToolAdapter，生产 CLI 注册不 spawn=T8 既定 #152 调度器裁决）；若选 §6 L148 降级路径（core preview）则转 README/parity 已知限制+不宣称 MCP 可用——两路均先落记录后动 |
| 2 | src 存量 34 处历史 TS mapping 注释清理评估 | r4-exit-evidence §7（T-B-044 nit-2）+硬输入③ | **建议登记不设行**：非义务低优先零行为面（34 处=调度器亲证口径为准；B 复核 grep 为超集口径[「TS:」109 命中/「mapping」51 命中]不构成反证）；Phase 2 卡片/后续触达顺手票承载 |
| 3 | 故障矩阵 L131 后两句（新 catalog/boot_id 旧批准失效）e2e 直载 | r4-exit-evidence §7（T-B-044 nit-3）+硬输入③ | **建议登记不设行**：L116「不在此时补做此前缺失的基础测试」红线；间接承载在册（S3 catalog/指纹 re-ask+S6 T7 boot_id 隔离+S2 断线 unknown）+已知覆盖缺口记档；后续 catalog 轮换票顺手直载（原文先例） |
| 4 | R0-B probe 等待环加固（L318-331 共享冲突零重试+半写行零容忍） | #155 fixup CI attempt-1 实测+硬输入③ | **建议设行微票 R5-M1**（§2.7）：服务发布检查 CI 稳定性；tools/ 面（非 src 冻结面）；attempt-2/3 病态取证独立不混同 |
| 5 | R4 float-reported 边界全集（S2 协议面 12 项未覆盖/POSIX 腿 CI 承载/S6 T5② 漂移面等） | r4-exit-evidence §7 L87（以各评审件为准） | **维持登记**：S5 已知限制清单化+S6 Done 对账逐件引注承载；零独立行 |
| 6 | r4-slices §3 行 3-6 r3 登记级残留（frozen-invoker 评估结论/#28 nit-2·3 条件触发/#30 六项顺手/#26 rg 测试卫生+编号歧义记档） | r4-slices §3（「不触达则顺延 R5+」原文） | **维持登记**：R5 各切片触达对应文件时顺手条款承载（沿 R4 先例）；r4-exit-evidence §7 已收口 R4 面处置；非 R5 义务不设独立行；r3 行#26/#27 编号歧义维持调度器在册裁定 |

## 4. 证据与发布面备忘（Python 专有，勿找 TS 蓝本）

1. **退出码面**：Python CLI 自有码表（0/1/2/3/4/70/130——cli.py L148-152/L167 实测枚举为准）；TS mini.ts 无对应发布码面约定，不引。
2. **MAAS smoke 证据纪律**：显式 smoke 非 CI 默认（L144）；脱敏沿 r0 证据先例（`_leak_self_check`）；「同名字符串不能作为行为一致的证据」（parity §3 L55）——smoke 证据只记 Python 实测，不宣称 TS 等价。
3. **发布检查/tag**：Phase 1 计划自有流程（L28/L116），无 TS 对应物；tag=调度器 git 写权+版本面同步（OP-R5-4）。
4. **JSON Schema**：`model_json_schema()` 自述面=Python 侧统一产物（CORE-CONTRACTS §1 L17）；「Python 的版本 1 不能当作 TS 的同名版本」（parity §3 L41）——schema 工件不宣称 TS wire 兼容。
5. **迁移链**：Python 专有 alembic 链（0001/0002）；「不按 TS 迁移链建 Python 库」（parity §3 L55）。

## 5. 风险表

| 风险 | 面 | 缓解/证据要求 |
|---|---|---|
| 干净环境安装不可重复（网络/构建缓存依赖） | S1 | 本地 `uv build` wheel 离线装+环境面（Python/uv 版本）实测记录 |
| hatchling wheel 内容差（src 布局包遗漏） | S1 | 构建后枚举 wheel 内容断言 opencode 包+入口点在 |
| 打开态 DB 备份半写（WAL/-shm 伴随文件） | S2 | 关闭态备份=演练主路径（WAL 收敛断言）；打开态口径任务书明示（不支持或 checkpoint） |
| 0001 停点库构造脆弱 | S2 | 测试内 upgrade 0001→断言→upgrade head 构造，不提交二进制 fixture |
| README python 代码块 format 红 | S3 | ORCHESTRATION §2 语义：.md 内 python 块须 ruff-format 合规；落盘后自跑 format --check |
| parity 翻新行号/测试路径继承漂移 | S5 | r2/r3 教训（行#23 类）：全部现读重锚+--collect-only/文件面亲证+每行测试路径可追 |
| MAAS smoke 网络依赖与脱敏 | MAAS | 显式非 CI（L144）；脱敏自检沿 r0 先例；端点/凭据调度器供给不入库 |
| 发布检查清单虚过（tag 门形同虚设） | S6 | 清单逐项证据化（run id/file:line/命令输出摘要）；tag 创建以清单全绿为前置（L28），调度器执行 |
| src 冻结面回归 | 全切片 | 基线 1021/105 零回归红线；文档切片零代码自证（diff 面） |

## 6. 开放问题（不预写裁决，留 A/调度器）

- **OP-R5-1**：bind_mcp_tools 生产接线 vs §6 L148 core preview 降级——Done L13 生产可达性与发布范围裁量（台账行 1）。
- **OP-R5-2**：MAAS 短会话执行形态（席位/时点/证据面/端点凭据供给）——B 方案见 §2.7（C-1 显式票+S5 前+doc/evidence/r5/ 脱敏先例）。
- **OP-R5-3**：pyproject `[project.scripts]`（+版本行）写权路由——调度器亲改 vs 任务书显式授权单块（零依赖变更，uv.lock 不动）。
- **OP-R5-4**：tag 形态与版本号（v0.1.0 vs core-0.1；pyproject 0.0.1→0.1.0 同步时点）——git 写+版本面归调度器。
- **OP-R5-5**：ADR 更新范围与派发形态（0001 刷新+0002 新卷[恢复协调器+MCP 自研]vs 仅刷新）——doc/adr/ 所有权归 A 席（ORCHESTRATION §1）。
- **OP-R5-6**：Done L9 `opencode run --prompt "..."` 字面形态 vs 现行 positional 单发——补 run 子命令/--prompt 旗标（小码面）vs README 记现行形+差异记档；A 席/调度器。

## 7. 硬性输入①-⑤对照自检清单

| 输入 | 落点 | 状态 |
|---|---|---|
| ① 交付物现状盘点（README 缺/parity 71L planned 失真/platform 66L R0-B 态/adr 仅 0001/92 fixtures/迁移 0001+0002） | §0.1 现状面+§1 表（README→S3/parity→S5/platform→S5/ADR→A1/fixture 清单→S4/迁移→S2）；B 复核闭合（Test-Path/pyproject scripts 缺/迁移版本/行数亲证） | ✓ |
| ② 可安装命令面缺口（无 [project.scripts]；REPL/approve face 在） | §2.1 S1 全承载+§0.1 CLI 现行面（退出码/选择面/迁移面实测基线）+OP-R5-3/OP-R5-6 | ✓ |
| ③ R4 移交台账四件分流（设行/登记/关闭各给建议） | §3 台账行 1-4（bind=OP-R5-1 待定夺/注释清理=登记/L131=登记/probe=M1 设行建议）+§2.7 M1 方案 | ✓ |
| ④ 基线口径（pytest 1021/mypy 105/format exit-0 语义/CI 三链引用不重述） | 头部+§0.1+§1（首切片基线 1021/105 零回归红线；CI 链引用调度日志 #155/#156 不重述） | ✓ |
| ⑤ MAAS 短会话+发布检查执行形态（B 给方案待定夺） | §2.7 R5-MAAS 方案+OP-R5-2；发布检查清单实体化=§2.3 S3（tag 前置门）+§2.6 S6 执行 | ✓ |

## 8. 每切片任务书模板（供调度器直接取用派发 C 席；对齐 r4-slices §8/T-C1-042 风格）

```markdown
# T-C1-0XX · 席位C-1：R5-SX <切片标题>

【角色章程·C 实现者】（三角色流水线：A 仲裁者 / B 规划评审 / C 实现者；本会话由调度器驱动，你是席位C-1。以契约文本+切片方案（doc/plans/r5-slices.md §2.X）为唯一基准；**禁止 git 写操作**（提交唯一归调度器）；门禁自跑后回报数字；④ 强制浮报所有未测/协议不可达防御分支；⑤ 存量兼容回归零破坏）
- 工作目录：worktree `C:/Users/lyu/.local/share/opencode/worktree/<id>/r5-sX`（分支 r5-sX，基于 main 检查点#NN——worktree HEAD 即基线，`git log -1` 自证）。
- 写权限（本任务显式授权，仅此）：<文件清单逐文件列出，含"新增"/"修改"与红线声明（src 冻结面/契约文档/pyproject[除 S1 显式授权块外]/uv.lock/CI/source-baseline.json/ORCHESTRATION 零触碰；pyproject/uv.lock 需触碰=停下上浮，写权归调度器[OP-R5-3 裁决前]）>。**其余一切冻结**；测试若暴露 src 缺陷→停下浮报不修，修复归调度器另行授权；fixture 只读复用（跨模块导入或同形态新建，不改既有文件）。
- 门禁（worktree 内自跑，python -m 形式）：先导出专属 temproot（调度器预建，任务书给全路径）：`$env:PYTEST_DEBUG_TEMPROOT=$env:TEMP+'/opencode/pytest-tmp/r5-sX'`；四件：`uv run python -m pytest -q`（基线 **1021+N** 零回归+新增如实计数——逐测落点入进度文件）；`uv run ruff check .`；`uv run ruff format --check .`（判定面=退出码 0；「N files」含 .md 合计勿读作 .py 计数）；`uv run python -m mypy src tools/r0 tests/r0 tests/contract tests/integration`（基线 **105+N files** 零问题）。
- 止损：NNmin（到线即停——ORCHESTRATION §2 re-arm 条款：既有工作面中段可续跑至自然检查点，hard-limit 到线强制收口回报；**禁止开启新工作面**）；防截断：进度文件**会话起始即首建**（首行戳=创建时刻现读时钟——勘察后落盘勿以勘察时刻冒充会话起点，T-B-048 nit-2 消歧）+分节时间戳**现读时钟**（ORCHESTRATION §2 硬条款——零占位预估形；**先 Get-Date 后落盘单动作批，禁止「跑完门禁再回填」工作流形态**——T-B-048 处方 2c，#159 增补）。
- TS 锚纪律：R5 **零新 TS 引用**（发布/文档/证据面以 Python 实测为准）；确需新 TS file:line（如 parity 翻新对照）→先停下报调度器补 doc/source-baseline.json 指纹（SHA256 调度器亲算录入）后方可引。不宣称与 TS 同构；差异按 r5-slices §4 备忘保守读法。
- 开放点纪律：r5-slices §6 所列 OP-R5-* **不预写裁决**——实现触及时按保守读法落地+进度④浮报，越出即停下待裁。
- 顺手项纪律：r5-slices §3 维持登记项（行 2/3/5/6）若本切片触达对应文件，按台账处置列顺手补记/强化并在进度⑤记档；不触达则不顺延不改道。

【任务】<逐项：spec 引用（r5-slices §2.X 目标/验收句原文）+最低落测结构+每测落点要求（测名↔验收句映射入进度文件）>

【④ 强制浮报】协议不可达/未测防御/已测防御/注记四档（R2 定案模式）；安装/备份类 drill 不可构造分支如实披露。

【⑤ 存量兼容】全量基线 1021/105 零回归红线；src 冻结面零触碰（授权清单外）；既有测试文件零触碰；新增仅授权清单。

【进度文件】`doc/orchestration/progress/T-C1-0XX.md`：①方案审视发现（选型论证）②门禁四件数字+新增测试逐测落点 ③强制浮报清单 ④未决问题 ⑤存量兼容声明。

【回报格式（回复正文，1-2 段）】完成度 / 门禁四件数字（含新增测数）/ 落点概要 / ③④ 概要 / 是否有需调度器裁决项。
```
