# 平台支持（R0-B 探针实测）

> 状态：R0-B 平台与本地存储探针的实测结论。
> 证据：[evidence/r0/platform.json](evidence/r0/platform.json)（本地 Windows 全量重跑生成，probe_version `r0b-1`）；
> 测试：`tests/r0/test_platform_probe.py`（8 项）；探针：`tools/r0/platform_probe.py`。
> CI 证据：run [41182347042](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41182347042)（ubuntu+windows 全绿，artifact `platform-probe-<os>`）；本地结果只记 `manual_verified`，CI 结果只在 CI 侧记 `ci_verified`，互不冒充。

## 1. 已验证平台（实测）

| 平台 | Python | 四项探针 | 证据级别 |
|---|---|---|---|
| Windows 11 本地（AMD64） | 3.13.12 | interpreter / process_tree / path_containment / sqlite_writer_lock 全部 verified | manual_verified |
| ubuntu-latest CI（Linux 7.0.0-1017-azure） | 3.12.16 | 同上，全部 verified | ci_verified |
| windows-latest CI（Windows Server 2025） | 3.12.10 | 同上，全部 verified（含 symlink/junction 越界拒绝） | ci_verified |

## 2. 证据级别说明

- **ci_verified**：CI run [41182347042](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41182347042)（commit `d5cdb11`，2026-10-31）ubuntu+windows 两个 job 全绿；探针在 CI 内以 `GITHUB_ACTIONS=true` 运行，artifact（`platform-probe-ubuntu-latest` / `platform-probe-windows-latest`）中 `generated.by=ci`、`ci_verified` 含全部四项、`unsupported` 为空。
- **manual_verified**：本地 Windows（仓库内 `evidence/r0/platform.json`，`generated.by=manual`）。本地与 CI 结果分开记录，未混用、未互相冒充。
- 中间过程：首次 push（`7e9f0ac`）ubuntu 曾因 POSIX zombie 误判 alive 失败（kill(0) 对未收割子进程成功），修复（`f653877`）后全绿；`f653877` 又暴露 linux mypy 对 `_winapi` 桩的平台条件问题，已修（`d5cdb11`）。

## 3. 明确不支持 / 不适用的能力

- POSIX 上的 junction：`not_applicable`（junction 是 Windows 概念；POSIX 用 symlink 覆盖同一语义）。
- 本地 Windows 本轮没有 unsupported 项；受限能力预期出现在 CI Windows（见下节）。

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
