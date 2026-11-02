# R1-S3 结构化审查（席位B · T-B-008）

- 审查对象：worktree `r1-s3` 检查点#27（commit `72de622`，6 文件 +1339）：`store.py`（101 行）/`txn.py`（594 行）/`__init__.py`（仅扩展导出）/`alembic.ini`（仅 N-1 删占位 URL 键）/`tests/integration/test_store_commit.py`（12 测试 549 行）/进度 `doc/orchestration/progress/T-C1-004.md`。
- 基线：`doc/CORE-CONTRACTS.md` §3.2（L146-187）/§3.2.1（L189-198）+ TS `bus.ts`（commitDurableEvent/publishAll/latestSequence/durableLocks）+ `doc/plans/r1-slices.md` §2 R1-S3（T-B-004 对齐版）。
- 复验方式：B 全文亲读 store.py/txn.py；12 测试经名称+断言密度核验（19 处 §8 code/reason/first_bad_seq 硬断言、AST 零 Bus 依赖、N-1/N-2 直测）；四门禁（106 passed/ruff/format/mypy 25 文件）采信调度器亲跑结果。

## 1. 结论：**通过**

0 blocker / 0 major / 0 minor / 3 nit。§3.2 顺序逐项吻合（见 §2），TS 差异全部有契约依据且文档化，§3.2.1 隔离入口语义精确，无越界实现（TransactionWriter 不导出、无 Bus/S4+ 提前实现——AST 结构性钉死）。

## 2. §3.2 顺序对照（维度1）——逐项吻合

锁（per-session asyncio.Lock + 进程写锁 + OS 文件锁跨进程）→ 读 H=COALESCE(MAX(seq),0)（连 COUNT/DISTINCT/MIN 供分类）→ **写入门禁先于幂等**：内存隔离集→SessionReadOnly；DB status read_only_diagnostic→SessionReadOnly、cleanup_failed→LifecycleCleanupFailed、closed→SessionClosed、未知 status→StorageCorrupt；sessions 行缺失且 H>0→读模型缺失→StorageCorrupt（契约 L163"已有日志却缺失 sessions 行属于读模型缺失，不能当作新建会话"字面落地）；首次创建（无行且 H=0）豁免 ✓ → 幂等先于 expected_seq：整批逐字段比对（ordinal/id/type/version/created epoch ms/canonical data/metadata）一致→返回原事件（过期 expected_seq 亦然，零再投影零推进）；跨会话 id/混合/同 id 异内容/部分批重试→IdempotencyConflict ✓ → H/A 不变量先于 Conflict（A≠H/空洞/重复/非法 seq→StorageCorrupt 且 reason+first_bad_seq 可定位）✓ → expected_seq 等值语义（≠H→Conflict）✓ → 归属（draft.session_id）/版本（注册表 resolve）✓ → 连续 seq H+1..H+n 插入（IntegrityError→IdempotencyConflict 兜底）✓ → 投影器同事务逐事件 ✓ → 复核 A=H+n（isolate=False 只回滚）✓ → commit 后返回；Bus 通知缺席=R1-S5 正确边界 ✓。契约 L158/L163"同一 projector 更新…和 sessions.applied_seq"——投影器推进+store 复核的分工与契约字面一致。

## 3. TS 基线对照（维度2）——忠实

`commitDurableEvent` L321-327（读 latest）↔ `_log_stats`；L346-352（重放比对）↔ `_idempotent_replay`（Python 差异：批级键+内容全比，文档化）；L373（seq=latest+1）↔ H+1..H+n；L402-405（projector 同事务）↔ in-tx apply；L438-445（commit 后才 notify）↔ 缺席（S5）。`publishAll` L570-578（同 aggregate）↔ 归属校验；L579 KeyedMutex ↔ per-session+进程锁；L592-593 连续 seq ✓；L604-624 重复/已存在 id 拒绝 ↔ `_validate_arguments` 批内重复 + IntegrityError 兜底 + 跨会话/混合 IdempotencyConflict（TS die→Python 显式错误，方案既定差异）；L582-657 整批单事务 ✓。`latestSequence`→COALESCE(MAX) ✓；`durableLocks`→三层锁 ✓；owner/strictOwner 不做（文档化）✓。

## 4. §3.2.1 隔离入口（维度4）——精确

业务事务完全回滚后独立短事务（新连接，原连接已关）✓；UPDATE 仅改 status+diagnostic、**保留 applied_seq/快照** ✓；行缺失 INSERT 最小记录（A=0、config/active_run_id 空——S2 迁移的 nullability 正好支撑）✓；净化载荷 {reason, first_bad_seq(可定位), first_bad_event_id, applied_seq} 无原始行内容 ✓；零业务事件、零水位推进 ✓；发现即 StorageCorrupt、后续写 SessionReadOnly（且先于幂等——已提交批重试也拒，测试钉死）✓；隔离持久化失败→内存闸门生效+原错误上抛、不声称已持久化（契约 L192 字面）✓。

## 5. 七项未决问题 B 意见（维度3）

1. **读模型缺失**：**同意，且无需升 A**——契约 L163 已明文（"读模型缺失，不能当作新建会话"），C-1 读法即契约本意。
2. **空批次 ValidationError**：**同意，无需升 A**——契约 L150 明文 `events 非空`。
3. **applied_seq 推进职责**：**同意投影器推进+store 复核，无需升 A**——契约 L158"用同一 projector 更新…和 sessions.applied_seq"、L163"同一 projector 已完整投影的最大连续 seq"字面即此分工；store 复核 A=H+n 是原子性后盾。
4. **事务本地失败只回滚不隔离（isolate 标志）**：**同意，无需升 A**——§3.2.1 检测范围限定"已存"损坏，且"不把调用方的坏输入当作已存损坏，也不隔离健康 session"；健康前置态下的投影器异常/未推进 A 回滚后仍健康，isolate=False 语义正确。
5. **dedupe_key 批级**：**同意现读法**。契约未显式定粒度；R1 唯一用途是 S7 prompt（单事件批），批级≡事件级。**唯一可选项**：若 A 想显式定事件级粒度再改（改动集中：EventBatch→draft 字段），非必须升 A。
6. **隔离持久化失败语义**：**同意，无需升 A**——契约 L192 字面吻合（内存闸门+原错误上抛+下次打开重新验证）；但见 nit N-1（进度文件"经异常链保留于 `__context__`"的表述与代码 `except StorageError: pass` 吞掉不符——功能无碍，文档-代码不符）。
7. **锁注册表/隔离集无上限**：**同意 R1 不设**（docstring 已注明）；长生命周期回收策略留后续裁决，不占 R1。

## 6. 测试质量（维度5）——通过

12 测试 vs r1-slices §2 R1-S3 验收点：验收 A（同事务/投影器异常全回滚/换健康投影器重试 seq 重起）✓、验收 B（整批重放过期 expected_seq 返回原事件/三类幂等冲突/Conflict）✓、水位测试点（提交后 A=H、A≠H 隔离）✓、隔离测试点（闸门先于幂等、内存+跨实例经 DB status、诊断写零业务事件）✓、入参校验（含 bool expected_seq 拒绝——§1 仓库级规则延伸）✓、N-1 裸跑 fail-fast 三断言 ✓、N-2 ✓、AST 零 Bus ✓。故障注入（手工 UPDATE applied_seq/DROP INDEX 手插重复/seq=0/DELETE sessions 行）合理。缺口见 nit N-2。

## 7. 越界检查（维度6）——通过

无 S4 真实 projector（Protocol+fake）、无 S5 Bus（AST 钉死）、无 S6/S7 API、TransactionWriter 不导出（外部仅依赖 SessionStore——契约 §3.2 原文）；`__init__.py` 仅扩展导出面；alembic.ini 仅 N-1。

## 8. 修订项汇总

| 级别 | 条目 | 归属 |
|---|---|---|
| nit | N-1 进度文件称 IsolationPersistFailedError 经 `__context__` 保留——代码实为吞掉（`except StorageError: pass`），调用方只见原 StorageCorrupt；功能符合契约 L192，属文档-代码不符 | 下次触及 storage 时链上抛（`raise corruption from persist_error`）或更正进度表述 |
| nit | N-2 两条隔离路径无直测：存量批重试遇坏行（REASON_STORED_DECODE_FAILED）、伪造未知 status（REASON_UNKNOWN_STATUS） | 随 S4/S6 触及 storage 测试时补两反例 |
| nit | N-3 幂等比对用 epoch_ms 毫秒截断——亚毫秒 created_at 差异低于存储精度不可分辨（符合"重试复用内容"精神，仅记录） | parity 或 docstring 一行注记即可 |

无阻塞项：R1-S3 可合入；R1-S4（projector+applied_seq 同事务）可立即派发——其真实 projector 落地时 N-1/N-2 顺带处理最顺手。
