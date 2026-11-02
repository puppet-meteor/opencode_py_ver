# R1-S2 结构化审查（席位B · T-B-006）

- 审查对象：worktree `r1-s2` 检查点#19（commit `99cf753`，11 文件 +1157）：`src/opencode/storage/`（db.py/__init__.py/migrations/env.py/migrations/versions/0001_initial.py）、仓库根 `alembic.ini`、`pyproject.toml`+`uv.lock`、`tests/integration/`（conftest.py+test_migrations.py 4 测试）、进度 `doc/orchestration/progress/T-C1-003.md`。
- 基线：`doc/CORE-CONTRACTS.md`（§3.2 L175-183 迁移 0001 清单/§3.2.1/§3.3/单 writer+WAL/FK/busy_timeout）+ TS `sqlite.bun.ts`/`event/sql.ts` + `doc/plans/r1-slices.md` §2 R1-S2（T-B-004 对齐版）。
- 复验方式：B 全文亲读上述六核心文件逐点核验；四门禁（94 passed / ruff / format 62 files / mypy 22 文件）与 §3.2 列集对照采用调度器已复核结果（止损未重跑），B 补充源码级证据见下。

## 1. 结论：**通过**

0 blocker / 0 major / 0 minor / 2 nit + 1 观察。切片交付完整、契约逐列吻合、TS 适配忠实、无越界实现（未提前实现 S3+ 的 commit/水位/projector）。

## 2. §3.2 逐列核对（维度1）——通过

- **events**（11 列）：id PK/session_id/seq/type/version/created INTEGER（TS `EventTable.created` epoch ms 同型）/data/metadata JSON/batch_id/batch_ordinal/dedupe_key 可空——与 §3.2 L177 逐列一致，无多余列；三条 unique 均落命名 index：`events_session_seq_uq(session_id,seq)`、`events_session_batch_uq(session_id,batch_id,batch_ordinal)`、`events_session_dedupe_uq(session_id,dedupe_key)` ✓。
- **sessions**：session_id PK/status/active_run_id 可空/config_snapshot JSON 可空/snapshot_hash 可空/applied_seq NOT NULL default 0/diagnostic 可空——§3.2.1 最小诊断记录（配置/active_run_id 为空）所需的 nullability 成立 ✓。
- **messages**：4 列 + `messages_session_message_uq(session_id,message_id)` ✓。
- **tool_executions**：8 列精确 + 两条 unique（`…_session_execution_uq`/`…_session_step_tool_call_uq`）✓。
- **permissions**：契约 4 语义字段（input_hash/scope/expires_at/state）全含；10 列超集意见见 §5-③-6。
- **无独立 sequence 表** ✓（TS `EventSequenceTable` 不移植）；测试钉死表集恰为五表+alembic_version。

## 3. events 无 FK 裁决（维度2）——**B 确认零 FK，无异议，无需升 A**

C-1 最保守读法 B 复核**成立且是契约+TS 证据下唯一自洽读法**：① §3.2 L177 events 列清单无 FK 字样；② L183"events 不依赖可重建的 messages 外键"确立日志独立于读模型存活的原则；③ **§3.2.1 L193 明文预期"sessions 行已被删除而 events 保留"**（隔离入口建立最小诊断记录的前提）——若 events→sessions FK CASCADE，删 sessions 行即连带毁 events，直接消灭 §3.3 验收 C 的 rebuild（先删读模型再重放）与 §3.2.1 检测范围"读模型缺失时先重建"；④ TS `event/sql.ts:10-26` 的 FK 指向 **EventSequenceTable**（契约裁定不移植的表）而非 sessions——任务书"归属 FK cascade"系转述 TS 该 FK 时的措辞笔误，任务书从属于契约。归属约束由 unique(session_id,…) 与 §3.2 commit 第 3 步归属校验承担，不需要 FK。测试 `_foreign_keys(db, "events") == []` 钉死 ✓。若 A 仍判需 FK，属契约修订+新迁移 revision（§3.3 升级路径），当前实现方向正确。

## 4. db.py 对照 TS sqlite.bun.ts（维度3）——通过

- **nativeLayer L83-96**：open 三参数（readonly/create/readwrite）↔ `OpenMode` 三模式，缺文件先 `DatabaseNotFoundError`（不碰锁不建目录）✓；close finalizer L92 ↔ `close()` dispose 后释放锁、幂等+async 上下文 ✓；`PRAGMA journal_mode=WAL`（L93）↔ 连接级注入——TS 单连接在 open 时设，Python 池化连接必须 per-connect 注入，且 journal_mode 持久于文件故幂等，是**正确适配而非偏离**；readonly 连接跳过 WAL（只读写不了 journal_mode）合理。
- **单 writer OS 锁（契约）**：锁先于引擎打开、第二 writer `WriterLockHeldError` 且不碰库（R0-B 结论复用不重测，符合方案）✓；引擎创建失败 `except BaseException` 回滚释放锁 ✓；close 顺序 dispose→release（持锁期间引擎不得再写）✓。
- **错误分层**：`StorageError(RuntimeError)` 基类+两子类，供上层映射 CoreError（§8）——合理，S3/S6 落 StorageCorrupt 类映射时再扩展。
- **make L23-81**（语句执行/错误分类）由 SQLAlchemy/aiosqlite 栈承担，Python 不复刻——方案既定差异 ✓。

## 5. 七项未决问题 B 意见（维度4）

1. **events 无 FK**：**同意**（§3 详证），无需 A 裁决。
2. **命名 unique index 替代内联 UNIQUE**：**同意**。SQLite 语义等价（UNIQUE 约束=唯一索引），命名索引可被后续 revision 寻址、可被集成测试断言（内联即不可寻址的 `sqlite_autoindex_*`）；docstring 已注明。无需 A 裁决。
3. **conftest `PYTEST_DEBUG_TEMPROOT` 重定向**：**短期同意保留**（sandbox 内跑 integration 的唯一通路；`getbasetemp` 懒解析时机正确、标准 `tmp_path` 语义不变、`setdefault` 尊重显式覆盖、默认根可用机器上无害）。长期建议：R5 证据收口时视 sandbox 政策决定去留（迁 pytest.ini env 或平台文档），非本切片缺陷。
4. **`script.py.mako` 未建**：**同意**。本批修订为手写、无 CLI 生成需求；未来若要求 `alembic revision` 生成，补授权后加即可（属生成样板，零风险）。
5. **alembic.ini 占位 URL**：**建议 fail-fast（nit N-1）**。裸跑 `alembic upgrade head` 会在 cwd 静默建 `./opencode.db`（schema 0001），有误触污染风险；env.py 对无 URL 的 `RuntimeError` 信息已清晰，**建议删除 `alembic.ini` 的 `sqlalchemy.url` 键**让裸跑即报错——一行改动，并入下次触及 storage 的任务即可，不阻塞本切片。
6. **permissions 10 列超集**：**同意**。契约对 permissions 只给 4 语义字段（全含），列集由 wire 投影推导是契约留白的必然；`input_hash` 可空论证成立（`py.permission.saved` 数据不含）；无 unique（契约未列）正确。**观察（非缺陷）**：`py.permission.saved` 的 `rule`/`boot_id` 无对应列——R1 注册表子集无权限事件、本表不会被投影；权限投影真正落地（R2+）时若需要，按 §3.3 走新 revision 即可。
7. **sync 迁移+async 运行时组合**：**结论采纳**——测试实证 async 引擎在 sync 迁移所建 schema 上写入/PRAGMA/WAL 持久/新 writer 重开全通过，Windows 路径无坑；`sqlalchemy[asyncio]`（greenlet）extra 依赖正确且必要（纯包 async 引擎运行时失败）。**r1-slices §4 #6 可据此标已决**（该更新归下次 B 方案任务，非本审查范围）。

## 6. 测试质量（维度5）——通过

4 测试断言 schema/wire 行为：表集精确相等、五表列集精确相等、关键 nullability/PK 旗标、六条命名 unique 的精确列序、FK 图（events 空+三读模型表 CASCADE）、幂等（sqlite_master 指纹+版本号）、PRAGMA 三项+**FK 强制性反证**（IntegrityError）+WAL 跨 dispose 持久+锁释放后重开、三 open 模式+close 幂等+readonly URI 拒写（OperationalError）。`test_runtime_…` 无 sessions 行插 events 成功顺带实证了零 FK。列集精确断言对 0002+ 有意脆性（钉死 0001 基线），后续 revision 自带新测试——可接受。缺口仅 trivial：unknown open mode 的 `ValueError` 未测（nit N-2）。

## 7. r1-slices §2 R1-S2 一致性（维度6）——通过

交付清单逐项兑现（db.py/env.py/0001/alembic.ini/三依赖含 [asyncio] extra 属授权三依赖内/tests/integration）；验收标准全覆盖（upgrade 幂等、PRAGMA 断言、第二 writer 复用 R0-B 不重测）；无越界（无 commit/expected_seq/水位/projector/EventLog 提前实现）；TS 映射与方案 §2 逐点一致（含"不建独立 sequence 表、applied_seq 落 sessions"的既定差异）。

## 8. 修订项汇总

| 级别 | 条目 | 归属 |
|---|---|---|
| nit | N-1 alembic.ini 占位 URL 裸跑静默建库，建议删键 fail-fast | 下次触及 storage/配置的任务 |
| nit | N-2 unknown open mode ValueError 未测 | 随下次 storage 测试补充 |
| 观察 | permissions 无 rule/boot_id 列 | 权限投影落地（R2+）时按 §3.3 新 revision 处理 |

无阻塞项：R1-S2 可合入，R1-S3（SessionStore.commit + TransactionWriter）可按序派发。
