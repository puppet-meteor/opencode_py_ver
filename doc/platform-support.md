# 平台支持（R0-B 探针实测+R4/R5 交付态刷新）

> 状态：R0-B 平台与本地存储探针的实测结论；R5-S5（T-C1-047）追加 CI 绿链（§1.1）与已知限制清单（§9）——PHASE1-SLIM 验收 C 后半「记录已支持平台和已知限制后才标记 core 0.1 稳定」的记录面。
> 证据：[evidence/r0/platform.json](evidence/r0/platform.json)（本地 Windows 全量重跑生成，probe_version `r0b-2`）；
> 测试：`tests/r0/test_platform_probe.py`（8 项）；探针：`tools/r0/platform_probe.py`。
> CI 证据（R0-B 时点）：run [41182347042](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41182347042)（ubuntu+windows 全绿，artifact `platform-probe-<os>`）——**保留为历史实测锚不删**；本地结果只记 `manual_verified`，CI 结果只在 CI 侧记 `ci_verified`，互不冒充（本纪律对本文件全部 CI 引用生效）。现行全量套件 CI 绿链见 §1.1。
> `r0b-2` 语义修正：`ci_verified`/`manual_verified` 只收录 status=verified 的检查项；restricted 检查项单独记入顶层 `restricted` 名单（配 `unsupported` 原因），绝不混入 verified 名单；进程树孙进程统一使用探针选中的解释器（含 pwsh 回退），不再硬编码。

## 1. 已验证平台（实测）

| 平台 | Python | 四项探针 | 证据级别 |
|---|---|---|---|
| Windows 11 本地（AMD64） | 3.13.12 | interpreter / process_tree / path_containment / sqlite_writer_lock 全部 verified | manual_verified |
| ubuntu-latest CI（Linux 7.0.0-1017-azure） | 3.12.16 | 同上，全部 verified | ci_verified |
| windows-latest CI（Windows Server 2025） | 3.12.10 | 同上，全部 verified（含 symlink/junction 越界拒绝） | ci_verified |

R4/R5 全量套件（1134 测）本机复跑口径：Windows 11 + CPython 3.12.16（worktree .venv 现读）——本地 manual 与 CI 分列不冒充，门禁数字与分解见 [T-C1-047 进度②](orchestration/progress/T-C1-047.md)。

## 1.1 CI 绿链（R4→R5 全量套件双 OS；run id=调度器台账供给锚）

- 绿链：`42228816565`[#154]→`42242948415`[#155]→`42244191726`[fixup]→`42260728594`[#158，R5-S1 并回]→`42269916522`[#159，R5-S2 并回]→`42283944194`[#160，R5-S3 并回——**首跑 Windows 腿计时型瞬态红→法证裁定→failed-job rerun 绿 @ 04:18:30**，形态记档：计时型瞬态非代码面，attempt 病态取证归调度日志]→`#161` run[本票派发时**在飞**——动态条款：以最新绿链为准；席位侧无 CI 网络核验面，终值归调度器并回流程记档，在飞态如实标注]。
- R0-B 原 run `41182347042`（头部）保留为历史实测锚不删——「本地 manual_verified 与 CI ci_verified 互不冒充」纪律对 §1.1 同样适用：链上 run 为 CI 侧 ci_verified 口径，不冒充本地实测。
- 本地对账（T-C1-047 门禁现跑，Windows 11+CPython 3.12.16）：pytest **1134 passed**（253.03s）/ruff check clean/`ruff format --check` exit 0（**341 files**=111 py+229 md[#161 树 340]+1 进度 md）/mypy **109 source files 零问题**——对账与分解见 [T-C1-047 进度②](orchestration/progress/T-C1-047.md)。

## 2. 证据级别说明

- **ci_verified**：CI run [41182347042](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41182347042)（commit `d5cdb11`，2026-10-31）ubuntu+windows 两个 job 全绿；探针在 CI 内以 `GITHUB_ACTIONS=true` 运行，artifact（`platform-probe-ubuntu-latest` / `platform-probe-windows-latest`）中 `generated.by=ci`、`ci_verified` 含全部四项、`unsupported` 为空。R4/R5 时点全量套件绿链见 §1.1。
- **manual_verified**：本地 Windows（仓库内 `evidence/r0/platform.json`，`generated.by=manual`）。本地与 CI 结果分开记录，未混用、未互相冒充。
- 中间过程：首次 push（`7e9f0ac`）ubuntu 曾因 POSIX zombie 误判 alive 失败（kill(0) 对未收割子进程成功），修复（`f653877`）后全绿；`f653877` 又暴露 linux mypy 对 `_winapi` 桩的平台条件问题，已修（`d5cdb11`）。

## 3. 明确不支持 / 不适用的能力

- POSIX 上的 junction：`not_applicable`（junction 是 Windows 概念；POSIX 用 symlink 覆盖同一语义）。
- 本地 Windows 本轮没有 unsupported 项；受限能力预期出现在 CI Windows（见下节）。
- watcher 文件变更监视与网络文件系统（NFS/SMB/网络驱动器等不可靠文件系统）：不宣称支持——watcher 平台面仅覆盖本机与 CI runner 已实测的本地文件系统（CORE-CONTRACTS §6.4 L415 红线三、M1 L53 同口径）；通知丢失窗口如实记档且丢失无害（下一次变更或显式 reload 重补），不引入重试/补偿/确认机制（§6.4 裁定①延伸面）。

## 4. symlink/junction 权限限制

- Windows 创建 symlink 需要开发者模式或 SeCreateSymbolicLinkPrivilege。权限不足（winerror 1314）时探针不静默跳过：在 `platform.json` 记录 `restricted` 状态 + `unsupported` 条目（capability=symlink creation / symlink escape rejection），退出码仍为 0，且必须本地补测、单独标注（STEP-01-R0 L122：restricted 不等于失败，也不能伪装成 CI verified）。
- junction 通过 `_winapi.CreateJunction` 创建，不需要该特权，Windows 上始终实测。
- 实测结果：本地 Windows 与 CI Windows（windows-latest，Server 2025）symlink 特权均可用——symlink 越界拒绝、junction 越界拒绝均实测 verified，`unsupported` 为空；上述受限处理机制保留作为防御路径，未被触发。
- POSIX（ubuntu CI）：symlink 无特权要求，symlink_escape 拒绝实测 verified；junction 为 `not_applicable`（POSIX 无该概念，非受限）。

## 5. 进程树回收结论

- 机制（CORE-CONTRACTS L234）：
  - Windows：Job Object（`JOB_OBJECT_LIMIT_KILL_ON_CLOSE`）；先 `AssignProcessToJobObject` 再放行 proceed-gate 文件让子 shell 创建孙进程（顺序确定，孙进程必在 job 内）；`TerminateJobObject` 终止整棵树。
  - POSIX：进程组（`start_new_session`；`killpg` SIGTERM→SIGKILL 升级）。
- 实测（本地 Windows）：`cancel` 与 `timeout` 两场景，子 shell 与孙进程均在 grace（5s，0.1s 轮询）内退出（实测约 100ms）；临时目录与标记文件清理验证通过；失败路径同样尽力清理（finally 内终止 + 等待 + 关句柄）。
- 实测（CI）：ubuntu（bash 5.3.9，进程组，两场景 ~100ms）与 windows（Job Object，~93–109ms）均全绿；POSIX zombie（已退出未收割的直连子进程）按已退出一一处理，不算残留。
- 孙进程退出是独立断言（`descendants.grandchild`），不是只看父进程退出码。

## 6. SQLite + OS 数据目录 writer lock 结论

- 机制（CORE-CONTRACTS L165）：数据目录 `writer.lock` 上的原生 OS 锁（Windows `msvcrt.locking(LK_NBLCK)` 即 LockFile 区域锁；POSIX `fcntl.flock(LOCK_EX|LOCK_NB)`），先取得锁、后打开数据库；SQLite 开 WAL、`foreign_keys=ON`、`busy_timeout=5000`。零第三方依赖。
- 实测（本地 Windows）：
  - 第二 writer 在写事务前锁失败，返回 `conflict` 且 `database_opened=false`；
  - 无写入证据：writer-2 窗口前后数据目录快照（文件名/大小/mtime/sha256）完全一致；writer 1 持锁期间 event 行数不变；
  - 持锁 writer 异常退出（exit 3）后 OS 自动释放锁，下一 writer 可取得；
  - 外键违规被 SQLite 拒绝（IntegrityError 实测）；
  - 正常释放后 WAL/SHM 收敛，临时数据目录删除。
- 实测（CI）：ubuntu（`fcntl.flock`）与 windows（`msvcrt.locking`）同样全绿——writer 2 conflict 且无写入、崩溃释放、WAL/FK/busy_timeout 全部成立；两平台锁语义均以 CI 证据确认。
- 已知限制：writer.lock 被持锁进程占用期间，第三方进程读它会被共享模式拒绝；快照对该文件记录 `hash_unreadable`（仍比较 size/mtime），不影响无写入结论。SQL 层的 busy 不作为 OS 锁证据，两者分开记录。

## 7. 解释器选择

- Windows：显式解析 `powershell.exe`（脚本经 `-NoProfile -ExecutionPolicy Bypass -File`，其余调用 `-NoProfile -Command`），实测版本 5.1.26100.9711。
- POSIX：显式 `bash`（`-c`，`BASH_VERSION`）。
- 全程 argv 形式 spawn；从未使用平台默认 shell，从未 `shell=True`。

## 8. 证据与复现

- 证据脱敏：主机路径只出现 `<WORKSPACE>`/`<TEMP>` 占位；不含用户名、PID、prompt、模型输出、凭据或 endpoint；写入前有泄漏自检（`_leak_self_check`，违规即抛错）。
- 复现：`uv run python tools/r0/platform_probe.py --output <path>`；测试 `uv run pytest -q tests/r0/test_platform_probe.py`。
- CI：单 job matrix（ubuntu-latest + windows-latest）内运行探针，`actions/upload-artifact@v4` 上传脱敏 JSON（`platform-probe-<os>`，`if: always()`），无 secrets、无真实 MAAS 访问。

## 9. 已知限制清单（验收 C 后半承载；R5-S5 T-C1-047 清单化——引注一律现读锚，逐项可核）

### A 平台/OS 面

- **A1 POSIX 腿承载**：MCP stdio/进程树相关测试本机仅 win32 腿实跑；POSIX process-group/killpg 树杀腿+close 升级序 POSIX 分支归 CI 复验承载（`doc/reviews/R4-S2-review.md` L19、L36；§5 CI ubuntu 实测基线）。Windows 无 SIGTERM：close 升级序中间态不可稳定注入（`doc/reviews/R4-S2-review.md` L22，④ 在案）。
- **A2 symlink 特权受限防御路径**：winerror 1314 → restricted+unsupported 记录、不静默跳过（§4）；现态未触发（本机与 CI Windows 特权均可用），机制保留为防御路径（§4 实测复核在册）。
- **A3 writer.lock 第三方读**：持锁进程占用期间第三方进程读该文件被共享模式拒绝——快照记 `hash_unreadable`（仍比较 size/mtime）；SQL 层 busy 不作为 OS 锁证据（§6 在册复核）。

### B 协议/能力面

- **B1 MCP client 协议面子集**：未覆盖协议面=评审件实枚举 13 面值——cancellation/progress/ping/sampling/elicitation/roots/resources/prompts/tools·list 分页/completion/logging/setLevel/协议版本协商（`doc/reviews/R4-S2-review.md` L22；`doc/plans/r5-slices.md` §3 行 5 计「12 项」与实枚举计数差异以评审件为准——T-C1-047 进度④ 浮报在案）；server→client 请求应 -32601、notification 忽略（差异表承载）。
- **B2 bind_mcp_tools 生产不 spawn**：生产 CLI 注册不 spawn MCP server=T8 既定（#152 调度器裁决在册）；run 级路由由组合根测试直组 `MCPToolAdapter` 承载（`doc/reference/r4-exit-evidence.md` L84；OP-R5-1 台账=`doc/plans/r5-slices.md` §3 行 1 待定夺）。
- **B3 故障矩阵 L131 后两句（新 catalog/boot_id 旧批准失效）未 e2e 直载**：间接承载在册=S3 catalog/指纹 re-ask+S6 T7 boot_id 隔离+S2 断线 unknown 不自动重试（`doc/reference/r4-exit-evidence.md` L86；`doc/reviews/R4-S7-review.md` L52——R5+ catalog 轮换票顺手直载）。
- **B4 deferred 能力面（core 0.1 不宣称）**：HTTP client（M5）/skills·subagent·HTTP MCP（M6）/compaction（M6）/媒体·多 provider·worktree（M6）/插件·codemode·PTY（M3/M4/M2）——`doc/parity.md` §2 deferred 5 行（差异列逐行复核在册）。
- **B5 grep `_is_invalid_pattern` `.lower()` 超集**：大小写不敏感超集面（rg 15.2.0 banner 恒小写直跑实证现实等价；docstring 披露 float-reported）（`doc/reviews/R4-S1-review.md` L46、L69）。

### C 存储/备份面

- **C1 打开态 DB 备份不支持**：唯一支持面=「先 close 后复制」（close 后 WAL/-shm 伴文件消失断言→整库复制→逐行对账）；建议口径=close 后核验伴文件消失（或将 -wal 一并纳入备份）+逐行对账后信任（`doc/reviews/R5-S2-review.md` L21 素材包+`tests/integration/test_migrations.py` 测 5/6/7 钉住事实面）。
- **C2 外部 live 读者效应平台依赖**：关闭前外部连接读取的侧车文件存活分支平台依赖——钉平台稳定关系式而非断言特定分支（`doc/reviews/R5-S2-review.md` L15、L16；根因=SQLite/aiosqlite 内部行为，冻结面外，机理未根因如实披露）。
- **C3 句柄释放 gc 限界**：关闭后文件句柄释放受 gc 时点影响（`doc/reviews/R5-S2-review.md` L21 素材包）。

### D 验证/记档面

- **D1 MAAS 真实端点短会话未执行**：provider 行待 MAAS smoke 终翻（OP-R5-2 已裁：T-C1-048 独立票，端点供给后 S6 前执行；PHASE1-SLIM L116 验证行）——fake server 无网络面已测（`doc/parity.md` §2.1 provider 行），不虚过不冒充。
- **D2 会话选择漂移面停止行内容未钉**：T5② 漂移停住面以单行 stderr+exit 4+零新建足证；消息内容（fingerprint/drift 词）未断言（`doc/reviews/R4-S6-review.md` L54——触达该文件时顺手补）。
- **D3 src 存量 34 处历史 TS mapping 注释**：非义务低优先清理评估（`doc/reference/r4-exit-evidence.md` L85；`doc/plans/r5-slices.md` §3 行 2=登记不设行；`doc/reviews/R4-S7-review.md` L51 零 M 亲证非新增）。
- **D4 代码记档级注记两件**：resolve_runtime_v3 双载链读（boot-only 单线程无正确性影响）/mcp namespace 单向固定（plain register 可占 `mcp` 名，一行保留检查可闭合——硬化候选）（`doc/reviews/R4-S3-review.md` L42、L43）。
