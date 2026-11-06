# R5 退出条件证据汇总（PHASE1-SLIM §3 R5：确认 core 0.1 的交付证据）

> 用途：core 0.1 结项的 R5 收口证据映射+发布检查执行记录+tag 前置门确认清单（OP-R5-4 调度器决策输入）。数据=契约/计划文档行号+源码 file/symbol 锚+测试 file::test 锚+checkpoint 链+本票门禁自跑读数；**无新主张**（本文件只汇总既有实现与测试的位置）。
> 范围锚定：`doc/PHASE1-SLIM.md` §3 R5 节 L109-116（节头 L109+交付 L111+验收 A/B/C L113-115+验证行 L116，本任务全文核验）+§1 L9-17（Done 九项）+§2 L23/L28（推进规则）+表 L40（R5 行）；`doc/ROADMAP.md` L62（core 0.1 稳定门）；切片定界以 `doc/plans/r5-slices.md` §2 为准（§0.1 checkpoint 链+L4 状态行，本任务现读）。
> 基线：main 检查点 #162（`333c95f`，fixup 后 HEAD）——worktree `r5-s6` HEAD 亲证（`git log -1`）。本任务（T-C1-049）**零代码变更**：写面恰 3 文件=新增本文件+`doc/orchestration/progress/T-C1-049.md`+M `doc/plans/r5-slices.md` 仅 L4 状态行（零哈希零 verdict 预写）；R5 收官态 **1134 passed/109**（见 §8）。
> 行号声明：本文件全部 file:line=本任务现读重锚（全文亲读+Select-String，禁继承）；已知漂移一处：`cli.py` `--continue`/`--session`=L281/L288（S3 档记 L280/L287，+1 漂移，以现读为准）。`doc/reference/r4-exit-evidence.md` 仅作模式参照，其行号不引用。
> TS 锚纪律：R5 收口切片零新 TS 引用——本文件**零 TS file:line**、零「与 TS 同构」类断言；指纹册 `doc/source-baseline.json` 58 条在册（r5-slices L6 口径），本票零新增。
> ④ 浮报声明：CI 双 OS 链=调度器供给锚照录（席位侧协议不可达，不实测不冒充）；真实 MAAS 短会话=「待」（T-C1-048 动态条款，端点/凭据未供给）；ADR 增量=零（OP-R5-5 待定夺）。

## 0. R5 切片收口台账（定界 r5-slices §2；闭合实值=r5-slices L4+§0.1 现读照录）

| 切片 | 交付 | 主交付物（现读在库引注） | 评审链（r5-slices L4 照录） | 门禁/收口锚（r5-slices §0.1） |
|---|---|---|---|---|
| S1 可安装命令面 | console 入口+干净环境安装 smoke+退出码/缺件提示面实测 | `pyproject.toml` `[project.scripts]`（`opencode = "opencode.cli:main"`，现读）；`tests/integration/test_cli_install.py`（7 测：L126/L139/L155/L177/L218/L239/L248） | T-C1-043 PASS+T-B-047 0/0/1/1 | 并回 `9d8a163`→#158（**S2 起基线 1028/106**） |
| S2 迁移与备份演练 | 空库迁移/0001→0002 真实升级/head 幂等 no-op+备份恢复演练 | `tests/integration/test_migrations.py`（9 测：L164/L240/L252/L296/L423/L451/L574/L636/L685）；迁移链=`src/opencode/storage/migrations/versions/`（0001_initial+0002_runs，现读枚举） | T-C1-044 PASS+T-B-048 0/1/0/3 | 并回 `f29e253`→#159（**S4 起基线 1031/106**） |
| S4 契约证据面 | 统一 JSON Schema 汇总+差异锁定+六接缝 fixture 清单 | `doc/reference/r5-fixture-inventory.md`（189L：92 件枚举 25/48/19+63 模型映射+§6 UNSUPPORTED_CAPABILITY 三面+§8 零差异结论）；`doc/reference/schema/` 6 工件；`tools/r5/generate_schema_artifacts.py`；`tests/contract/test_schema_lock.py`（103 测，L267-L369 八类） | T-C1-045 PASS+T-B-049 0/0/2/0 | 并回 `e88ec8a`→#160（**S3 起基线 1134/109**） |
| S3 README+发布/备份操作说明 | README+发布检查清单实体化（tag 前置门） | `README.md`（160L：L7 安装/L40 运行/L83 同进程示例/L119 退出码/L134 缺件提示/L152 文档互链）；`doc/release-and-backup.md`（108L：§1 发布清单五小节+§2 备份恢复+§3 回退分离+§4 待项全枚举） | T-C1-046 PASS+T-B-050 0/0/0/0 | 并回 `f71c31c`→#161（**S5 起基线 1134/109**[format 338]） |
| S5 parity 翻新+platform-support 刷新 | parity 逐行翻新+CI 链追加+已知限制清单化 | `doc/parity.md`（87L：L14 本册现态 verified 8+待 MAAS 终翻 1+deferred 5+§2.1 六列证据）；`doc/platform-support.md`（103L：§1.1 CI 链+§9 已知限制 15 件） | T-C1-047 PASS+T-B-051 0/0/0/2 | 并回 `1be7295`→#162（**S6 起基线 1134/109**[format 341]） |
| S6 发布检查+Done 对账+收口（本票） | 本文件+Done 九项对账+tag 前置门确认清单+r5-slices L4 翻牌 | `doc/reference/r5-exit-evidence.md`（本文件 §1-§10+附录 A） | T-C1-049 终报（本票）；**评审与并回归=调度器记档**（零哈希零 verdict 预写——#163 终态翻牌归调度器） | 门禁四件自跑（§8）；CI 链照录（§8） |

**并行线现状（如实对账，④）**：R5-MAAS=OP-R5-2 已裁 T-C1-048 独立票（端点/凭据未供给=用户外部输入等待面在册；MAAS 相关行按动态条款标「待」，见 §4/§6 L10 行）；R5-A1（ADR 线）=OP-R5-5 已裁定执行并闭合（#163 ⑧→T-A-011 交付[0001 L3 状态行刷新+0002 新卷 81L]+调度器验收+T-B-053 PASS 0/0/0/1，merge `dd9421f` 随 #164 入库——`doc/adr/` 现读枚举 0001+0002）；R5-M1（probe 加固微票，r5-slices §3 行 4 建议设行）=席位侧未见派发/收口记录，以调度器台账为准（#160 Windows 腿瞬态经法证裁定为计时型非标记读型，不构成发布检查阻塞）。

## 1. 验收 A（L113 原文）："干净环境安装后运行 CLI 和同进程示例；缺配置、解释器或 rg 时明确提示；退出码区分成功/失败/中断。"

| 句 | 证据锚（本任务现读重锚） |
|---|---|
| 干净环境安装后运行 CLI | `tests/integration/test_cli_install.py` L126 `test_wheel_carries_the_package_and_the_console_entry_point`（wheel 含包+console 入口点）/ L139 `test_installed_command_help_is_zero_side_effect_exit_0` / L155 `test_installed_single_shot_run_succeeds_exit_0` / L177 `test_installed_repl_round_trip_exits_0`——S1 实测在册 |
| 同进程示例 | `README.md` §同进程示例（L83 起，SessionService 最小片段；python 代码块受 format 门禁覆盖[ORCH L32 口径]——本票 format 344 files 绿含 .md） |
| 缺配置 | `test_cli_install.py` L218 `test_installed_cli_missing_maas_config_is_an_argparse_exit_2`（argparse exit 2+旗标名/环境变量名双提示）；文案面 `README.md` L134-L137 |
| 缺 rg | `test_cli_install.py` L239 `test_grep_missing_rg_under_real_path_isolation`（真实 PATH 隔离→TOOL_FAILED 诊断）；原文 `README.md` L11-L19；包装文案钉测=`tests/integration/test_tool_static_trio.py` |
| 缺解释器 | `test_cli_install.py` L248 `test_bash_missing_interpreter_under_real_path_isolation`（真实 PATH 隔离→TOOL_FAILED 诊断）；文案面 `README.md` L146-L150；钉测=`tests/integration/test_tool_bash.py` |
| 退出码三分 | `src/opencode/cli.py` L148 `_MIGRATION_EXIT_CODE=70`/L150 `_CLEANUP_EXIT_CODE=3`/L152 `_SELECTION_EXIT_CODE=4`+L167 `_ROUND_EXIT_CODES`（0/1/2）+返回面 L343/L631/L637/L640/L861/L863/L892/L918；全枚举表=`README.md` §退出码（L119-L133：0/1/2/3/4/70/130 七码+三分两列并记+「2 双载两义」原样声明）；**三分在**（0 成功；1·2[用法]·3·4·70 失败；130 中断；2 双载[中断终态/用法错]原样不合并） |

**判定：验收 A √**（全句闭合；OP-R5-6 保守裁决记档：`opencode run --prompt` 字面形态不在现实现，语义面由 positional 单发承载，`README.md` L52-L58 记档句在册——Done L9 同口径）。

## 2. 验收 B（L114 原文）："全量当前测试、类型、格式/静态检查通过；空库迁移、最早可用库 fixture、备份恢复演练完成。只有 0001 时验证空库/已有 0001 no-op，出现 0002 后必须验证真实升级。"

| 句 | 证据锚（本任务现读重锚） |
|---|---|
| 全量测试/类型/格式/静态检查 | §8 门禁四件终值：pytest **1134 passed**（254.37s）/ruff check **All checks passed!**/format --check **退出码 0**（344 files）/mypy **109 source files 零问题** |
| 空库迁移 | `tests/integration/test_migrations.py` L164 `test_upgrade_head_builds_0001_schema`（六表+alembic_version=head 逐项断言） |
| 最早可用库 fixture（0001 停点）→真实升级 | 同文件 L423 `test_upgrade_0001_to_0002_preserves_legacy_data`（测试内构造 0001 停点库+存量→upgrade head→存量逐行可读；二进制库零入库，DB 态由迁移链再生可重现）+L451 `test_0002_backfill_via_rebuild_keeps_every_accepted_run`（0002 回填经 rebuild 保量） |
| head 幂等 no-op | 同文件 L240 `test_upgrade_head_is_idempotent`（空库双跑 schema fingerprint+version 不动）+L574 末段（恢复后带数据 `upgrade head`=no-op 快照复验） |
| 备份恢复演练 | 同文件 L574 `test_closed_state_backup_restore_reconciles_row_by_row`（关闭态备份→恢复→六表+alembic_version+EventLog 读面逐行对账）；边界钉测 L636（打开态主文件复制缺 WAL 提交）/L685（外部 live 读者干扰收敛）；操作步骤实体=`doc/release-and-backup.md` §2（关闭态主路径五步） |
| 「出现 0002 后必须验证真实升级」触发条件 | `0002_runs` 在库（`src/opencode/storage/migrations/versions/` 现读枚举双版本）→**触发条件成立且已验证**（L423）——非「不适用」形 |

**判定：验收 B √**（全句闭合）。

## 3. 验收 C（L115 原文）："六个接缝的旧 JSON fixture 可读、未知能力显式拒绝；记录已支持平台和已知限制后才标记 core 0.1 稳定。"

| 句 | 证据锚（本任务现读重锚） |
|---|---|
| 六接缝旧 JSON fixture 可读 | `doc/reference/r5-fixture-inventory.md` §4（**92 件**枚举：S2:16/S3:18/S4:49/S5:9；正例 25/反例 schema 级 48+行为级 19）；`tests/contract/test_schema_lock.py` L303 `TestFixtureCensus`+L314 `TestPositiveLockDiff`（正例工件校验+注册表解码往返）；S1/S6 零 fixture 属预期（fixture-inventory §2 注：S1 wire 由模型+内联构造锁定、S6 为运行时行为面） |
| 未知能力显式拒绝 | `test_schema_lock.py` L327 `TestSchemaLevelLockDiff`/L342 `TestBehaviorLevelLockDiff`/L356 `TestRegistryRejections`/L369 `TestUnsupportedCapabilityFace`；三面对账=fixture-inventory §6（定义面+schema 面+行为面五集成锚） |
| 平台记录 | `doc/platform-support.md` §1（Windows 11 本地 manual_verified+ubuntu-latest/windows-latest CI ci_verified 三行） |
| 已知限制记录 | `doc/platform-support.md` §9 已知限制清单 **15 件**（A 平台/OS 3+B 协议/能力 5+C 存储/备份 3+D 验证/记档 4——T-B-051 勘误口径，本任务现读亲数）——验收 C 后半记录面闭合（S5 T-C1-047 清单化交付） |

**判定：验收 C √**（记录面前提闭合；「标记 core 0.1 稳定」的标记动作=发布流程面，见 §7 ROADMAP L62 对账与 §9 tag 前置门）。

## 4. 验证行（L116 原文）："下节发布检查 + 实际 MAAS 短会话，证据记录到 parity；不在此时补做此前缺失的基础测试。"

| 句 | 承载态 |
|---|---|
| 发布检查 | **√**：本文件 §1-§3（验收 A/B/C）+§5（交付五项）+§6（Done 九项）+§8（门禁+CI）+§9（tag 前置门确认清单）=`doc/release-and-backup.md` §1 清单五小节逐项证据化（S3 承载物的 R5-S6 执行面；该文件头部动态条款「以 tag 时点最新实测为准」由本票门禁重跑承载——终值见 §8，非沿用 S3 快照） |
| 实际 MAAS 短会话 | **「待」**（动态条款）：OP-R5-2 已裁=T-C1-048 独立票（端点/凭据未供给，用户外部输入等待面在册）；fake server 无网络复现面已由 R0-R2 测试承载（`tests/r0/fake_openai_server.py`+`test_provider_spike.py` 24+`test_ai_adapter.py` 36+`test_llm.py` 63=123 测，parity §2.1 provider 行在册）；**不虚过不冒充** |
| 证据记录到 parity | **√ 在册**：`doc/parity.md` L28 provider 行=「**待 MAAS smoke 终翻**（OP-R5-2 已裁：T-C1-048 独立票，端点供给后 S6 前执行）」显式状态行（S5 T-C1-047 已条件记档；终翻归 T-C1-048，本票零触碰 parity.md） |
| 「不在此时补做此前缺失的基础测试」红线复核 | **√ 零违反**：本票零新测（collect-only **1134**==S5 基线 1134，附录 A 对账；R5 期新增 113 全部为 S1/S2/S4 切片期交付，S3/S5/S6 零新测） |

## 5. 交付清单五项（L111 原文："README、fixture 清单、parity 实测记录、ADR 更新、发布/备份操作说明。"）逐项在库引注

| # | 交付项 | 在库态（现读） | 切片/评审链 | 状态 |
|---|---|---|---|---|
| 1 | README | `README.md` 160L（L7 安装/L40 运行/L83 同进程示例/L119 退出码/L134 缺件提示/L152 文档互链） | S3（T-C1-046+T-B-050） | √ 在库 |
| 2 | fixture 清单 | `doc/reference/r5-fixture-inventory.md` 189L（92 件枚举+63 模型映射+6 schema 工件对账+§8 零差异结论） | S4（T-C1-045+T-B-049） | √ 在库 |
| 3 | parity 实测记录 | `doc/parity.md` 87L（L14 本册现态：verified 8+待 MAAS 终翻 1+deferred 5；§2.1 六列证据逐行） | S5（T-C1-047+T-B-051） | √ 在库（provider 行待 T-C1-048 终翻） |
| 4 | ADR 更新 | `doc/adr/` 现读枚举 0001-core-architecture.md[L3 状态行刷新：2026-11-07]+0002-recovery-coordinator-and-mcp-client.md[81L 新卷：启动恢复协调器+MCP 自研 stdio client] | R5-A1（A 席线）=OP-R5-5 已裁定执行（#163 ⑧）——T-A-011 交付+调度器验收+T-B-053 PASS 0/0/0/1，merge `dd9421f` 随 #164 入库 | **√ 在库**（#164 翻牌） |
| 5 | 发布/备份操作说明 | `doc/release-and-backup.md` 108L（§1 发布检查清单[tag 前置门]五小节+§2 备份/恢复操作+§3 回退路径分离+§4 清单「待」项全枚举） | S3（T-C1-046+T-B-050） | √ 在库 |

## 6. Done 九项对账（L9-17 逐项↔证据三方映射）

| Done 项（原文摘引） | 状态 | 证据锚 |
|---|---|---|
| L9 安装后 `opencode` 进入 REPL；`opencode run --prompt "..."` 支持脚本调用；同进程调用使用 SessionService | **√ 已闭合**（脚本调用按 OP-R5-6 保守裁决：字面形态不在现实现，语义面由 positional 单发承载+差异记档句） | `test_cli_install.py` L155/L177（单发+REPL 往返）；`README.md` L42-L58（运行+记档句）/L83 起（同进程示例） |
| L10 一个真实 MAAS 端点完成多轮文本、流式工具调用；同样场景能在 fake server 下无网络复现 | **分拆**：后半 √ 已闭合；前半 **「待」**（T-C1-048 端点未供给——动态条款） | fake 面：`tests/r0/fake_openai_server.py`+123 测（parity §2.1 provider 行在册）；真实面：`doc/parity.md` L28「待 MAAS smoke 终翻」 |
| L11 `read/write/edit/glob/grep/ls/bash` 全经 Registry.invoke，具备输入校验、权限、输出限额、超时和取消 | √ 已闭合 | `doc/reference/r3-exit-evidence.md`（R3 交付在档） |
| L12 全局/项目 JSON 和 AGENTS.md 按规定顺序加载，session 固化可重建的配置快照，凭据只存引用 | √ 已闭合 | `doc/reference/r3-exit-evidence.md`（R3-S6 交付在档） |
| L13 一个显式配置的 stdio MCP server 能列出和调用工具，断线不自动重跑有副作用的调用 | √ 已闭合 | `doc/reference/r4-exit-evidence.md` §1（R4 MCP stdio 栈在档）；组合根承载面=platform-support §9 B2 |
| L14 `--continue` / `--session ID` 恢复已提交历史；未开始工具变 cancelled/not_started，已开始但未提交终态的工具变 unknown，不自动重执行 | √ 已闭合 | `src/opencode/cli.py` L281/L288（选择面现读锚）；`doc/reference/r4-exit-evidence.md` §5（R4-S6 在档） |
| L15 EventLog 支持分页、补读、Synced、follow 和慢消费者处理；Bus 不承担持久写入 | √ 已闭合 | `doc/reference/r4-exit-evidence.md`（R4-S5 覆盖在档） |
| L16 每个接缝都有正例/反例 fixture、行为测试和至少一个真实/fake 适配器测试 | √ 已闭合 | `doc/reference/r5-fixture-inventory.md` §2（六接缝×四列矩阵，适配器列全非空）；`tests/contract/test_schema_lock.py` L303-L369 |
| L17 README 运行说明、parity 差异、ADR、迁移/备份说明齐全；ruff/mypy/pytest 和 Windows/Linux CI 通过 | **分拆**：README √/parity √（S5 翻新）/迁移备份 √/ruff·mypy·pytest √（§8）/双 OS CI √（§8 调度器锚）；**ADR=√**（#164 翻牌：T-A-011 交付[0001 状态行刷新+0002 新卷]+调度器验收+T-B-053 PASS 0/0/0/1——A 席线闭合，见 §5 行 4） | `README.md`+`doc/release-and-backup.md`+`doc/parity.md`+§8 门禁与 CI；ADR 面=§5 行 4 |

**九项态汇总：7 项全绿（L9/L11/L12/L13/L14/L15/L16）+2 项分拆（L10 前半、L17 ADR 子件）——两处「待」均为 R5 收口外动态面：前者归 T-C1-048（MAAS 端点供给），后者归 A 席线（OP-R5-5 裁决）；非 R5-S6 可闭合缺口，如实记档不虚过。**

## 7. PHASE1-SLIM R5 节对照+ROADMAP L62 稳定门对账

**R5 节（L109-116）逐行对照**：

| 行 | 原文（摘引） | 承载 |
|---|---|---|
| L109 | ### R5：确认 core 0.1 的交付证据 | 本节=本文件全文 |
| L111 | 交付：README、fixture 清单、parity 实测记录、ADR 更新、发布/备份操作说明 | §5 五项逐项引注（4 √+1 待[ADR]） |
| L113 | 验收 A（干净环境安装/缺件提示/退出码三分） | §1 √ |
| L114 | 验收 B（全量检查/迁移三面/备份恢复/0002 真实升级） | §2 √ |
| L115 | 验收 C（六接缝 fixture/未知能力拒绝/平台+已知限制记录） | §3 √ |
| L116 | 验证：下节发布检查+实际 MAAS 短会话，证据记录到 parity；不在此时补做此前缺失的基础测试 | §4（发布检查 √/MAAS「待」/parity 在册/红线零违反） |
| 表 L40 | R5 发布基线｜0.5 周｜前置 R4｜结束时可演示=可安装的 core 0.1 和完整验证记录 | 可安装面=S1（console script+wheel 安装测 L126-L177）；完整验证记录=本文件+parity+platform-support+fixture 清单+release-and-backup |
| L23 | 测试从 R0 起交付，R5 负责汇总交付证据 | **汇总义务闭合**：附录 A 1134 全量枚举+§0 切片台账+§1-§6 三方映射（本文件即汇总承载） |
| L28 | 切片通过后保存可回退提交；tag 只在发布检查通过后创建，数据库回退另按备份方案执行 | checkpoint 链 #157-#162 在册（r5-slices §0.1）；tag=调度器 git 写权（OP-R5-4）→§9 前置门确认清单；DB 回退=release-and-backup §2/§3 |

**ROADMAP L62 稳定门对账**（原文："先交付 Python 的 `core 0.1` 契约。接口只有经过真/假适配器、golden fixture 和失败场景验证后，才标为该版本稳定；不是在编码前永久冻结。"）：

| 稳定门要件 | 在册验证面 |
|---|---|
| 真/假适配器 | 六接缝适配器矩阵全非空（fixture-inventory §2）：fake=`tests/r0/fake_openai_server.py`（S1）+`tests/integration/mcp_stdio_server.py`（S3）9 模式；真=真实 SQLite+alembic 链（S4）/真实 fs·bash 工具（S3 工具面）/真实 service·CLI 进程面（S6，无 fake 属预期）；**唯一真适配器缺口=真实 MAAS 端点（→「待」T-C1-048，§4）** |
| golden fixture | 92 contract fixture（正例 25=golden 往返）+6 schema 工件（`model_json_schema()` 汇总，CORE-CONTRACTS §1 L17 R5 条款闭合）+lock-diff 漂移锁定（`test_schema_lock.py` 103 测） |
| 失败场景验证 | 反例 67 件（schema 级 48+行为级 19，fixture-inventory §5）+必测故障矩阵承载（R4 各验收 B/C 故障注入面，r4-exit-evidence §2-§4 在档）+恢复三窗/半批注入/进程树杀（R4-S4/S7） |

**对账结论**：三要件验证面在册；core 0.1 稳定标记=发布流程动作（tag=调度器 OP-R5-4），其中 MAAS 真端点面待 T-C1-048 闭合后方可宣称完整闭环（不虚过——§9 前置门将此列为显式「待」项交调度器裁量）。

## 8. 门禁与 CI（R5 收官态实测，T-C1-049 自跑，worktree `r5-s6`，前台阻塞承载）

| 命令 | 结果（本会话工具读数照录） |
|---|---|
| `uv run python -m pytest -q`（temproot=`C:/Users/lyu/AppData/Local/Temp/opencode/pytest-tmp/r5-s6`） | **1134 passed**，254.37s（0:04:14），零失败零跳过（基线 1134 零回归；本票零新测） |
| `uv run ruff check .` | **All checks passed!**（退出码 0） |
| `uv run ruff format --check .` | **344 files already formatted**（**退出码 0**=判定面；计数分解=111 tracked py+232 tracked md[#162 提交树合计 343]+1 本票进度 md[untracked `?? doc/orchestration/progress/T-C1-049.md` 亲证]；ruff 0.17.3 计数含 .md=ORCH L32 定谳口径。注：本文件落盘后工作树口径 345——主树门禁以调度器并回时点实跑为准） |
| `uv run python -m mypy src tools/r0 tools/r5 tests/r0 tests/contract tests/integration` | **Success: no issues found in 109 source files**（基线 109 零漂移） |
| `uv run python -m pytest --collect-only -q` | **1134 tests collected**（1.13s）==门禁 pytest 1134 passed，闭合（附录 A 数据源；存档 temproot `pytest-tmp/r5-s6/collect-only-r5-s6.txt`） |

- 运行环境：Windows 11 本地+CPython 3.12.16（`uv run python --version` 亲证；与 platform-support §1.1 本机口径一致——本地 manual 与 CI 分列不冒充）。依赖版本=pyproject/uv.lock 现读口径：pydantic 2.14.2/sqlalchemy 2.1.6/aiosqlite 0.22.1/openai 3.42.0/httpx2 2.16.0/alembic 1.20.1/pytest 9.2.0/pytest-asyncio 1.4.1/ruff 0.17.3/mypy 2.4.1（④ 注记：未做逐依赖矩阵实测）。
- **门禁数字演化**（r5-slices §0.1+L4 现读照录）：R4 收官 1021/105（#155）→ S1 **1028/106**（merge `9d8a163`→#158）→ S2 **1031/106**（merge `f29e253`→#159）→ S4 **1134/109**（merge `e88ec8a`→#160）→ S3 **1134/109**[format 338]（merge `f71c31c`→#161）→ S5 **1134/109**[format 341]（merge `1be7295`→#162）→ **S6 本票 1134/109**（零代码变更零回归；format 344[+进度 md]）。
- **CI 双 OS 绿链（调度器供给锚照录；席位侧协议不可达未实测——④ 浮报，结论以调度器台账为准）**：`42260728594`[#158]→`42269916522`[#159]→`42283944194`[#160 首跑 Windows 腿计时型瞬态红→法证裁定→failed-job rerun 绿]→`42289983998`[#161]→`42290071459`[#161 fixup]→`42296809889`[#162 主体]→`42296887477`[#162 fixup]——**全链现行绿零悬挂红**。`doc/platform-support.md` §1.1 在册链为 S5 编写时点态（止于 #160+#161 在飞标注），本票零触碰该文件；tag 时点最新绿链由调度器按 release-and-backup §1.2 动态条款取值。本票（S6）推送与 CI 复验归调度器并回流程记档。

## 9. tag 前置门确认清单（OP-R5-4 调度器决策输入——逐项可核；本票零 tag 操作零版本号写[0.0.1→0.1.0=调度器]）

| # | 前置项 | 态 | 可核锚 |
|---|---|---|---|
| 1 | 门禁四件绿 | **√** | §8 终值（1134 passed/All checks passed!/format exit 0[344 files]/mypy 109 零问题——本票 worktree 自跑） |
| 2 | CI 双 OS 绿链 | **√（照录锚）** | §8 七连 run id 链（调度器供给；席位侧未实测 ④） |
| 3 | 验收 A/B/C 逐项 | **√** | §1/§2/§3（file:line+测试锚三方映射） |
| 4 | Done 九项对账在档 | **√（7 绿+2 分拆）** | §6（L10 前半/L17 ADR=「待」如实） |
| 5 | MAAS 行状态 | **「待」**（不虚过） | §4+`doc/parity.md` L28（T-C1-048 动态条款——端点/凭据供给后执行，时点=调度器裁量） |
| 6 | 发布检查执行本体 | **√** | 本文件 §1-§9+release-and-backup §1 五小节（动态条款「tag 时点最新实测」已由本票重跑承载） |
| 7 | 备份演练证据在档 | **√** | §2 备份恢复行+release-and-backup §1.5/§2 |
| — | tag 形态/版本号/创建时点 | **调度器裁量**（OP-R5-4：git 写权+pyproject 版本行同步） | 本清单=决策输入，非执行记录 |

## 10. 已知边界与 R5+ 移交（非 R5 收口退出条件缺口）

- **真实 MAAS 短会话**：T-C1-048 独立票（OP-R5-2 已裁）——端点/凭据未供给（用户外部输入等待面在册）；fake server 无网络复现已测（123 测）；parity L28 终翻归该票。
- **ADR 更新**：已闭合（#163 ⑧ OP-R5-5 裁定执行→T-A-011 交付：0001 状态行刷新+0002 新卷[启动恢复协调器+MCP 自研 client]；调度器验收+T-B-053 PASS 0/0/0/1；merge `dd9421f` 随 #164 入库——`doc/adr/` 现读枚举 0001+0002）。
- **bind_mcp_tools 生产接线 vs core preview 降级**：OP-R5-1 待定夺（r5-slices §3 台账行 1；现态=组合根测试直组承载，platform-support §9 B2 在册）。
- **R5-M1 probe 加固微票**：r5-slices §3 行 4 建议设行；席位侧未见派发/收口记录（调度器台账为准）——#160 Windows 腿瞬态经法证裁定为计时型非标记读型，不构成发布检查阻塞。
- **R4 float-reported 边界全集**：platform-support §9 15 件清单化承载（S5 交付；本文件 §3 引注）。
- **r5-slices §3 维持登记项（行 2/3/5/6）**：处置列在册（行 2 历史注释清理/行 3 故障矩阵 L131 后两句/行 5 R4 边界/行 6 r3 残留——均登记不设行，后续触达顺手承载）。

## 附录 A 函数级测试枚举（`uv run python -m pytest --collect-only -q` 一次性全量，1134）

R5 期间新增测试 **113**（1021→1134，R4 收官基线）：S1 +7（新增 `test_cli_install.py`）/ S2 +3（存量扩增 `test_migrations.py` 6→9）/ S4 +103（新增 `test_schema_lock.py`）；S3/S5/S6 零新测（文档/翻新/收口票）。spec r5-slices §2.6 测试计划行「collect-only 1021 终值」为 R4 时点口径（B 撰写时基线），以本票现读 **1134** 为准（任务书票面更正口径）。

全量 file 级分组枚举（总数 **1134 == §8 门禁 pytest 1134 passed，闭合**；44 组=tests/contract 13 组 648+tests/integration 29 组 454+tests/r0 2 组 32；collect 输出机械转录，存档 temproot `pytest-tmp/r5-s6/collect-only-r5-s6.txt`）：

### tests/contract/test_ai_adapter.py (36)
### tests/contract/test_bus.py (24)
### tests/contract/test_config.py (16)
### tests/contract/test_context.py (46)
### tests/contract/test_events.py (181)
### tests/contract/test_llm.py (63)
### tests/contract/test_mcp_naming.py (44)
### tests/contract/test_messages.py (29)
### tests/contract/test_permission_events.py (41)
### tests/contract/test_policy.py (21)
### tests/contract/test_schema_lock.py (103)
### tests/contract/test_tool_listing.py (16)
### tests/contract/test_tools.py (28)
### tests/integration/test_approval.py (12)
### tests/integration/test_bus_commit.py (1)
### tests/integration/test_cli.py (13)
### tests/integration/test_cli_install.py (7)
### tests/integration/test_cli_r3.py (30)
### tests/integration/test_cli_r4.py (10)
### tests/integration/test_config_loader.py (30)
### tests/integration/test_e2e.py (3)
### tests/integration/test_e2e_r3.py (4)
### tests/integration/test_e2e_r4.py (7)
### tests/integration/test_eventlog.py (20)
### tests/integration/test_follow_recovery.py (4)
### tests/integration/test_fs_multiroot.py (5)
### tests/integration/test_mcp_catalog.py (3)
### tests/integration/test_mcp_stdio.py (13)
### tests/integration/test_messages_union.py (4)
### tests/integration/test_migrations.py (9)
### tests/integration/test_permission_projector.py (11)
### tests/integration/test_projector.py (29)
### tests/integration/test_rebuild.py (17)
### tests/integration/test_recovery.py (14)
### tests/integration/test_runner.py (34)
### tests/integration/test_service.py (28)
### tests/integration/test_service_run.py (20)
### tests/integration/test_store_commit.py (16)
### tests/integration/test_tool_bash.py (14)
### tests/integration/test_tool_read.py (21)
### tests/integration/test_tool_static_trio.py (39)
### tests/integration/test_tool_write_edit.py (36)
### tests/r0/test_platform_probe.py (8)
### tests/r0/test_provider_spike.py (24)
