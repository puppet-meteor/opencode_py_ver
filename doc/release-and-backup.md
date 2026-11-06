# 发布与备份操作说明（opencode-py · Phase 1 / core 0.1）

本文是 PHASE1-SLIM §2 L28「tag 只在发布检查通过后创建，数据库回退另按备份方案执行」与 §3 R5 节验证行 L116「下节发布检查」两处提及的**发布检查清单的唯一承载物**（spec=doc/plans/r5-slices.md §2.3 L68-77；T-C1-046 落地）。tag 创建本身与版本号属调度器 git 写权（OP-R5-4）——本文只定义前置门与备份/恢复操作面。

> **动态条款**：本文所有「在档值」是编写时点（checkpoint #160 后、R5-S3）的证据快照；发布检查执行时（R5-S6）以 **tag 时点最新实测**为准——门禁数字重跑、CI 取最新绿链、验收/Done 锚复核，不沿用本文快照冒充 tag 时点状态。

## 1. 发布检查清单（tag 前置门，逐项证据化）

### 1.1 门禁四件绿（worktree 内自跑）

| 门禁 | 判定 | 在档基线（S3 起口径） |
|---|---|---|
| `uv run python -m pytest -q` | 基线 **1134** passed 零回归（S3 起基线，S4 并回后；R5 各切片零新测零回归红线） | **1134 passed**（本票 #160 后重跑 257.65s，零失败零跳过；T-C1-045 门禁同值在档） |
| `uv run ruff check .` | clean（退出码 0） | All checks passed!（本票重跑 rc 0） |
| `uv run ruff format --check .` | **退出码 0**（「N files」=py+md 合计，勿读作 .py 计数） | **338 files already formatted**（=335[#160 提交树]+3[本票新 md：README/本文/进度文件]） |
| `uv run python -m mypy src tools/r0 tools/r5 tests/r0 tests/contract tests/integration` | 基线 **109** source files 零问题（S4 起口径含 tools/r5） | **109 source files** 零问题（本票零新 py，重跑同值零漂移） |

### 1.2 CI 双 OS 绿

以 **tag 时点最新绿链**为准（本地 manual_verified 与 CI ci_verified 互不冒充）。编写时点在档链（调度器供给锚）：run `42260728594`（#158 并回后）+ run `42269916522`（#159 并回后）+ #160 并回 run（本票派发时在飞）。

### 1.3 验收 A/B/C 逐项（PHASE1-SLIM §3 L113-115）

**验收 A**（L113：干净环境安装后运行 CLI 和同进程示例；缺配置、解释器或 rg 时明确提示；退出码区分成功/失败/中断）：

| 句 | 证据锚（本会话现读重锚） |
|---|---|
| 干净环境安装+CLI 运行 | `tests/integration/test_cli_install.py` L126（wheel 含包+console 入口点）/ L139（`--help` 零副作用 exit 0）/ L155（单发 exit 0）/ L177（REPL 往返 exit 0）——S1 实测在档 |
| 缺配置 | 同文件 L218（缺 MAAS 配置=argparse exit 2，旗标名+环境变量名双提示） |
| 缺 rg | 同文件 L239（真实 PATH 隔离→发现面诊断；包装文案钉测在 `tests/integration/test_tool_static_trio.py`） |
| 缺解释器 | 同文件 L248（真实 PATH 隔离→解释器缺失诊断；包装文案钉测在 `tests/integration/test_tool_bash.py`） |
| 退出码三分 | `src/opencode/cli.py` L148-152（70/3/4 常量）+ L167（`_ROUND_EXIT_CODES` 0/1/2）+ L637/L640/L861/L863/L892/L918（130/70/3/round 查表/130/3 返回面）码表；0/1/2/3/4/70/130 全枚举触发测在册（枚举表=README §退出码） |
| 同进程示例 | `README.md` §同进程示例（SessionService 片段，本票交付） |

**验收 B**（L114：全量当前测试、类型、格式/静态检查通过；空库迁移、最早可用库 fixture、备份恢复演练完成；出现 0002 后必须验证真实升级）：

| 句 | 证据锚（本会话现读重锚） |
|---|---|
| 全量测试/类型/格式/静态检查 | §1.1 门禁四件 |
| 空库迁移 | `tests/integration/test_migrations.py` L164（`test_upgrade_head_builds_0001_schema`：六表+alembic_version=head 逐项） |
| head 幂等 | 同文件 L240（空库双跑 schema fingerprint+version 不动）+ L574 末段（恢复后带数据 `upgrade head`=no-op，快照复验） |
| 0001→0002 真实升级 | 同文件 L423（`test_upgrade_0001_to_0002_preserves_legacy_data`：0001 停点库+存量构造→upgrade head→存量逐行可读） |
| 备份恢复演练 | 同文件 L574（`test_closed_state_backup_restore_reconciles_row_by_row`：关闭态备份→恢复→六表+alembic_version+EventLog 读面逐行对账） |

**验收 C**（L115：六个接缝的旧 JSON fixture 可读、未知能力显式拒绝；记录已支持平台和已知限制后才标记 core 0.1 稳定）：

| 句 | 证据锚（本会话现读重锚） |
|---|---|
| 旧 JSON fixture 可读 | `doc/reference/r5-fixture-inventory.md`（92 件枚举对账+分类 25/48/19，§4 L53 起）+ `tests/contract/test_schema_lock.py` L314（`TestPositiveLockDiff`：正例工件校验+注册表解码） |
| 未知能力显式拒绝 | 同文件 L327（schema 级反例双拒）/ L342（行为级反例）/ L356（注册表成员拒绝）/ L369（`TestUnsupportedCapabilityFace`：UNSUPPORTED_CAPABILITY 定义面+schema 面可达性） |
| 平台+已知限制记录 | `doc/platform-support.md` 现态在档（R0-B 实测锚）；**已知限制清单化+CI 链追加=S5「待」**（验收 C 后半的完成面归 S5） |

### 1.4 Done 九项对账（PHASE1-SLIM §1 L9-17，逐项）

| Done 项 | 状态 | 证据锚 |
|---|---|---|
| L9 安装后 `opencode` 进入 REPL；脚本调用；同进程 SessionService | 已闭合（脚本调用按 OP-R5-6 保守裁决：现行 positional 形+差异记档句） | `tests/integration/test_cli_install.py` L155/L177（安装体单发+REPL）；`README.md` §运行（记档句）+§同进程示例 |
| L10 真实 MAAS 端点多轮文本+流式工具调用；fake server 无网络复现 | 前半**待**（真实 MAAS 短会话=OP-R5-2 待定夺，归 S5 前/S6 承载）；后半已闭合（fake server 无网络复现=R0-A 起在册） | fake 面：`doc/evidence/r0/provider.md`+在册 fake server 测试；真实短会话：「待」 |
| L11 七工具全经 Registry.invoke（校验/权限/限额/超时/取消） | 已闭合 | `doc/reference/r3-exit-evidence.md`（R3 交付在档） |
| L12 配置分层加载+快照固化+凭据只存引用 | 已闭合 | `doc/reference/r3-exit-evidence.md`（R3-S6 交付在档） |
| L13 stdio MCP server 列出/调用工具、断线不自动重跑 | 已闭合 | `doc/reference/r4-exit-evidence.md`（R4 MCP stdio 栈在档） |
| L14 `--continue`/`--session` 恢复+未终态工具语义 | 已闭合 | `src/opencode/cli.py` L280/L287（选择面）+ `doc/reference/r4-exit-evidence.md`（R4-S6 在档） |
| L15 EventLog 分页/补读/Synced/follow/慢消费者；Bus 不持久写 | 已闭合 | `doc/reference/r4-exit-evidence.md`（R4 在档） |
| L16 每接缝正例/反例 fixture+行为测试+适配器测试 | 已闭合 | `doc/reference/r5-fixture-inventory.md`（六接缝×证据矩阵）+ `tests/contract/test_schema_lock.py` L303-L353（fixture 全集钉测） |
| L17 README/parity/ADR/迁移备份说明齐全+四件+双 OS CI | 本票闭合 README 运行说明+迁移/备份说明；**parity 差异=S5 待；ADR=A 席线 R5-A1 待**；ruff/mypy/pytest/CI=绿在案 | `README.md`+本文（本票）；§1.1/§1.2（门禁+CI） |

### 1.5 备份演练证据在档

`tests/integration/test_migrations.py` L574（关闭态备份恢复逐行对账主路径）+ L636（打开态主文件复制缺 WAL 提交边界）+ L685（外部 live 读者干扰收敛边界）——S2 演练在档，操作步骤实体化见本文 §2。

## 2. 备份/恢复操作步骤（Phase 1 支持面）

### 2.1 关闭态主路径（唯一支持面）

1. **close**：正常退出 host（REPL `/exit`/EOF）或显式 `App.close()`/`SessionService.close()`——engine dispose+writer lock 释放；
2. **断言 WAL 收敛**：`-wal`/`-shm` 伴文件消失，或主文件自足（fresh sqlite3 连接全量可读）——两口径取强面（并证）；
3. **整库主文件复制**到数据目录外的安全位置（进程内 `shutil.copyfile` 形态）；
4. **恢复=副本就位**：删除丢失/损坏的库文件（连同伴文件清场）后将副本复制回原路径；
5. **逐行对账后方可信任**：六张业务表——`events`/`sessions`/`messages`/`tool_executions`/`permissions`（0001_initial）+ `runs`（0002_runs）——加 `alembic_version`，逐行与源库快照比对，并经 **EventLog 读取面**逐事件（id/seq/type）对账；恢复后可再跑一次 `upgrade head` 验证带数据幂等 no-op（CORE-CONTRACTS §3.3 L223「备份并验证旧数据」的验证步；测试锚 `test_migrations.py` L574）。

### 2.2 打开态备份：不支持（Phase 1 口径）

无 checkpoint API 面+WAL 半写风险（CORE-CONTRACTS §3.2 L193：单 writer 进程+SQLite WAL 条款）。实测边界：打开态仅复制主文件=副本缺**全部**运行时提交行（有效迁移 schema 但 events/tool_executions/permissions 全 0 行）——`test_migrations.py` L636 回归钉住。**「先 close 后复制」为唯一支持面**，不宣称打开态备份支持。

### 2.3 已知限制

1. **外部 live 读者干扰收敛**：打开期若有外部连接读 live WAL（监控/巡检模式），close 时 checkpoint 可能不收敛（本栈 Windows+aiosqlite+SQLAlchemy 实测可复现：`-wal` 残留全量提交、主文件副本=空库；无外部读者时收敛干净——机制未根因，`test_migrations.py` L685 钉测）。建议口径：**close 后核验伴文件已消失（或将 `-wal` 一并纳入备份），并对备份副本逐行对账后再信任**。
2. **句柄释放**：外部 sqlite3 读连接可能要等循环 GC 才释放 OS 句柄（Windows 实测 unlink WinError 32→`gc.collect()` 后 OK）——删除/就位数据库文件前显式 close 一切连接。

### 2.4 工件规约

- 备份工件落**数据目录外**安全位；
- **二进制库文件零入库**（git）：0001 停点库等测试态由迁移链测试内再生（可重现），不提交二进制 fixture。

## 3. 回退路径分离（L28 原文「数据库回退另按备份方案执行」）

- **tag 回退**：git 面，归调度器（git 写权+版本面=OP-R5-4，本清单外）；
- **DB 回退**：按本文 §2 备份方案恢复步执行（restore-from-verified-backup）；
- **Phase 1 边界**：alembic downgrade 显式拒绝——`0001_initial.py` L132-134 与 `0002_runs.py` L56-59 的 `downgrade()` 均 `raise RuntimeError`（restore-from-verified-backup only）；CORE-CONTRACTS §3.3 L223：「降级默认从已验证备份恢复，**tag 不能代替数据回退**」。

## 4. 清单「待」项全枚举（如实显式，不虚过）

- **真实 MAAS 短会话**（Done L10 前半/验证行 L116）——OP-R5-2 待定夺（席位/时点/证据面），归 S5 前/S6 承载；
- **parity 逐行翻新**（Done L17「parity 差异」）——S5（含 provider 行 MAAS 证据条件）；
- **platform-support 刷新**（验收 C 后半：已知限制清单化+CI 链追加）——S5；
- **ADR 更新**（Done L17「ADR」）——A 席线 R5-A1（doc/adr/ 写权归 A 席）；
- **发布检查执行本体**（本清单终值化+tag 前置门确认+Done 对账终表）——S6（r5-slices §2.6）；tag 创建/版本号 0.0.1→0.1.0=调度器（OP-R5-4）；
- **bind_mcp_tools 生产接线 vs core preview 降级**——OP-R5-1 待定夺（r5-slices §3 台账行 1）。
