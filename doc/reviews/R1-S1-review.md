# R1-S1 结构化审查（席位B · T-B-002）

- 审查对象：commit `e61cdd5 feat(contracts): R1-S1 minimal wire contracts, event registry, fixtures`（19 文件 +1176）；实现者进度 `doc/orchestration/progress/T-C1-001.md`。
- 契约基线：工作区未提交的 `doc/CORE-CONTRACTS.md`（T-A-002 裁决，视同最新契约文本）。
- 对照基准：TS `D:/26_code/opencode_py_ver/opencode-2.2.2/`（指纹锁定）+ B 方案 `doc/plans/r1-slices.md` §2 R1-S1。
- B 复验记录：`uv run pytest -q tests/contract` → **44 passed**；`uv run ruff check .` → clean；R1 快照摘要用 stdlib `json.dumps`+`hashlib` 独立复算 = `77c932c3…f802`（与契约 §6.2.4/实现/fixtures 三方一致）；`EventDraft(version=True)` 探针见 Minor-1。mypy/全量回归取 C-1 报告值（B 止损未重跑）。

## 1. 结论：**通过**

0 blocker / 0 major / **4 minor / 3 nit**。切片交付完整（含 T-A-002 中途合入的 §6.1/§6.2 对齐，§9 冲突表 4 行逐项核对通过：无内嵌 hash ✓、两字段必填 ✓、非整数 1 拒绝 ✓、摘要字节不变 ✓），TS 映射忠实（`versionedType` 格式对 `event.ts:174-176` 逐字符核验一致；`durableMap` 重复定义拒绝语义对齐），fail-closed 解码、测试钉 wire 行为、门禁全绿。minor 项均不阻塞 R1-S2 派发（涉及的是 contracts 边角与待 A 裁决事项），建议并入下次触及 contracts 的任务或 A 裁决批次。

## 2. 逐维度发现

### 维度1 · 切片方案符合度 —— 通过

交付清单逐项核对：EventDraft/DurableEvent/EphemeralEvent（events.py L118-163）✓、type+version 注册表（EventRegistry L238-298）✓、R1 两事件（R1_EVENT_DEFINITIONS L301-304）✓、UserMessage（messages.py L28-42）✓、CoreError（errors.py）✓、正/反例 fixture 8 个 ✓、round-trip（test_round_trip_is_lossless）✓。超出清单项：ConfigSnapshot/canonical_json/compute_snapshot_hash/RUN_SCOPED_EVENT_TYPES——均为 T-A-002 裁决的合理承接，B 方案修订清单见 §4。

### 维度2 · TS 基准映射正确性 —— 通过（1 项开放问题归 §3-2）

- `versionedType`：TS `${type}.${version}`（event.ts:174-176）↔ Python `f"{event_type}.{version}"` ✓（test_versioned_type_matches_ts_format 钉住）。
- `durableMap` 重复定义 throw（event.ts:178-187）↔ `EventRegistry.register` ValueError ✓（含 ephemeral 侧）。
- seq 域差异（Python ≥1/初值 1 vs TS ≥0/latest -1）、created_at ISO 8601 vs TS epoch ms、session_id vs aggregateID：三处差异均有契约 §3.1 字面依据并在模块 docstring 声明 ✓。
- `Event.ID` `evt_` 前缀 TS 强制（event.ts:9-14 `isStartingWith("evt_")`）、Python 未强制：契约 §3.1 现文 `id: str`，实现合规；是否收紧归 A 裁决（§3-2）。
- `DurabilityMismatchError` 为 TS 无对应物的 Python 细化（TS bus.ts:60-66/78-90 只有单一 `InvalidDurableEventError`）：行为更精确、无契约冲突，建议 parity.md 记录（Nit-3）。

### 维度3 · 契约符合度 —— 通过（3 项 minor）

§1（Pydantic v2、工厂默认集合、JSON 值域、fail-closed）✓；§2.1 UserMessage 字段 ✓ 但 `files` 处理见 Minor-2；§3.1 两事件 data 全集、envelope 字段、metadata run_id 规则（RUN_SCOPED_EVENT_TYPES）✓；§6.1/§6.2 快照精确形状/canonical JSON/摘要（独立复算通过）✓；§8 CoreError 22 码全集锁定+未知码拒绝 ✓。

- **Minor-2 · UserMessage 缺可选 `files`，合法 `files: []` 载荷被拒**
  - 位置：`src/opencode/contracts/messages.py` L28-42（`extra="forbid"`，无 files 字段）。
  - 理由：CORE-CONTRACTS §2.1 user 行 `files` 是可选数据、"Phase 1 非空则拒绝 UnsupportedCapability"——字面含义是空/缺省 files 应可解码；现在 `files: []` 被当未知字段拒绝（ValidationError）。方向 fail-closed 无安全风险，但形状与契约字面不符。根因是 B 方案 R1-S1 清单漏列 files（§4-3 自我修正）。
  - 建议：待 A 裁决二选一——(a) 加 `files: list[FileContent] = Field(default_factory=list, max_length=0)`（FileContent 以最小 wire 模型落地）；(b) 修订 §2.1 明确"R1 v1 wire 不含 files，R2+ 版本再引入"。
- **Minor-3 · `extra="forbid"` 与"同版本新增可选字段"兼容路径未声明**
  - 位置：events.py 全部模型 `ConfigDict(extra="forbid")`。
  - 理由：契约 §4 允许"新增可选字段（验证旧 fixture/旧消费者）"不升版本；§1 另有"未知的可选扩展字段可保存在 metadata"选项。全量 forbid 实际选择"任何未知字段=损坏"（S4/S6 将映射 StorageCorrupt/只读诊断）。取向合理，但与兼容承诺的交互无文档记录——未来按 §4 加同版本可选字段会把旧读者静默打成诊断态。
  - 建议：请 A 裁决并在契约或 parity.md 记录："py.* v1 事件新增字段一律升版本+upcaster，不走同版本可选字段路径"。
- **Nit-1 · CoreError.details 恒序列化**（errors.py L56）：契约 §8 `details?` 可选，实现总是出现（值 `{}`）。§1 集合工厂默认规则支持现状，无碍；仅需 parity 记录 wire 形状差异。

### 维度4 · 测试质量 —— 通过（补测项见 Minor-1/Minor-4）

反例覆盖：未知 type（用 TS 名 `session.created`，同时证明 py.* 域不冒充 TS wire）✓、未知 version ✓、缺字段×2 ✓、非法扩展×2（data 未知字段、metadata 非对象）✓、坏 JSON/NaN/重复键/缺 type-version/bool version ✓、快照篡改（解码时重算）✓、缺 metadata.run_id ✓、naive 时间戳 ✓、durable/ephemeral 交叉 ✓、重复定义×2 ✓、非 JSON Python 值（datetime/set）✓。断言检验 wire 行为而非实现细节（round-trip、raises、utcoffset、fixture 原始检视）✓；fixture 钉住完整 envelope 形状（含 seq≥1）✓。

- **Minor-4 · 反例矩阵缺 3 个廉价用例**
  - 位置：`tests/contract/test_events.py`。
  - 理由：`version: "1"`（字符串，decode 路径 EventDecodeError 分支无直测）；`seq: True`（与 Minor-1 联动）；孤立 surrogate 经 `canonical_json`（UnicodeEncodeError=ValueError 子类，fail-closed 行为无测试钉住；R1 值域不可触发，C-1 ①-5 已声明）。
  - 建议：并入 Minor-1 修补批次，参数化各一行。
- **Nit-2 · 边界断言缺失**：`EventDraft(id="")` 拒绝（min_length=1 已有约束未直测）、`UserMessage(text="")` 允许（契约未禁，值得钉住防回归）。

### 维度5 · 实现质量取舍 —— 通过（1 minor）

- 双异常路（EventDecodeError 家族 vs pydantic ValidationError）语义边界清晰：envelope/注册表结构失败 vs 字段级失败，docstring 声明 S4/S6 映射责任 ✓。`decode_durable` 的 type/version 手工前置检查是注册表解析顺序所需（先定位 data model 再校验），合理。
- `_parametrize` Any 路由：文档化、无 type: ignore、行为等价 ✓（意见见 §3-5）。
- `EventDefinition.event_type` 避开 `type` 内建遮蔽：有注释 ✓。
- `default_event_registry()` 每次新建实例：符合 §1 不共享可变容器 ✓。
- **Minor-1 · `EventDraft.version`/`DurableEvent.seq` bool 穿透**
  - 位置：`src/opencode/contracts/events.py` L130（`version: int = Field(ge=1)`）、L145（`seq: int = Field(ge=1)`）。
  - 理由：pydantic v2 lax 模式把 `True` 强转为 1（B 实测 `EventDraft(version=True)` 通过）。decode 路径（decode_durable L286 显式 `isinstance(version, bool)` 拒绝）与 `ConfigSnapshot.schema_version` 都有 bool 守卫，唯独生产侧模型没有——`version=True` 的 draft `model_dump_json()` 得 `"version":true`，再经 decode_durable 即被拒：同一对象一半合法一半不合法，留隐患给 S3 生产路径。
  - 建议：EventDraft 加 `field_validator("version", mode="before")` 拒 bool（DurableEvent 同理对 seq），补 `EventDraft(version=True)` 拒绝测试钉住 pydantic 行为。

## 3. 对 C-1 六项未决问题（T-C1-001 §③）的 B 意见

1. **T-A-002 对齐（信息项）**：确认无异议。H/A 不变量（§3.2）、坏日志只读诊断与读取边界（§3.2.1/§4）、prompt 幂等顺序（§7）已正确识别为 S3-S8 各自消化项；B 将把它们并入 r1-slices.md 对应切片验收标准（见 §4-4~7）。
2. **ID 前缀是否模型层强制**：建议**强制**。TS 基准 `event.ts:9-14` `Event.ID = isStartingWith("evt_")`，成本一行 `pattern`，可杜绝生产侧裸 ID 并提升可 grep 性；`msg_/ses_/run_` 建议一并 pattern 化。但契约 §3.1 现文 `id: str`——需 A 同步修订契约或以 ADR 记录有意差异；在此之前现实现合规，非缺陷。
3. **JSON Schema 产出归属**：建议归 **R5**（验收 C 的 0.1 稳定门槛本就要求六接缝 fixture/Schema 汇总），每切片以 fixture 钉形状即可（本切片已达标）。若 A 要求逐切片落盘：建议 `tests/contract/schemas/` 由 `model_json_schema()` 生成、测试 diff 锁定，防手写漂移。
4. **Ephemeral 生产类型注册时点**：同意留 R2（`py.text.delta` 等属流式链路，R1 无生产者）；机制已由 `_HeartbeatData` 测试证明"加类型=加一行"。B 在方案修订时于 R2 部分补注。
5. **mypy `_parametrize` Any 路由**：接受。建议 S3/S4 复用该 helper 而非各写一套；pydantic mypy 插件收紧列为 R5 后的硬化工序，不占 R1 预算。
6. **`Literal[1]` bool 陷阱**：接受现有守卫；建议升级为仓库级约定（凡 `Literal[int]`/裸 int 语义字段必须防 bool），并连同 Minor-1 一并给 `version`/`seq` 补守卫——本审查已列为修订项。

## 4. 对 doc/plans/r1-slices.md 的修订建议（调度器据此派 B 修订任务）

1. **S2 依赖清单更正**：pydantic 已在 S1 引入（调度器已批准前移），S2 只加 alembic/sqlalchemy/aiosqlite（纠正 C-1 ①-1 指出的 B 方案矛盾）。
2. **S1 交付清单补显式项**（文档追认已交付物）：ConfigSnapshot、canonical_json、compute_snapshot_hash、RUN_SCOPED_EVENT_TYPES、R1 摘要常量钉死测试。
3. **S1 UserMessage 字段清单补 `files`**：按 A 对 Minor-2 的裁决结果修订；同时记录 B 原方案的漏列。
4. **S3 验收标准并入 §3.2 水位裁决**：H/A 定义与不变量、expected_seq=提交前 H（非独立水位、非目标 seq）、写入门禁（只读诊断检查）先于幂等查询、tool_executions 投影入同事务；§3.2.1 隔离入口（SessionStore 私有 writer、短事务持久化诊断）归 S3 实现，检测范围全量与 rebuild-失败隔离路径归 S8。
5. **S6 验收标准并入 §4 修订**：坏页整页 StorageCorrupt、不返半页、不推进 next_cursor、follow 遇坏页保健康 yield 后终止且不发越界 Synced、H=日志水位（非投影水位）。
6. **S7 验收标准并入 §7 修订**：prompt 固定顺序（状态门禁→durable dedupe `prompt:`+request_id→幂等/IdempotencyConflict→active_run_id/SessionBusy→生成 ID、expected_seq=H 提交）、R1 accepted 持续占槽、close 只释放自有资源、RunRef 三字段非空字符串；补 ID 生成器设计注记（对齐 TS `ascending()` 可排序语义，避免随机 UUID 使日志不可排序）。
7. **S8 并入 §3.2.1/§3.3 修订**：诊断期间禁止运行恢复补写、rebuild 必须保留诊断标记、重建失败保留原投影全量回滚（含清空操作）。
8. **§3 差异表补三条**：`versionedType`/`durableMap` 已对齐（格式逐字符一致）；`EventDecodeError` 家族与 `DurabilityMismatchError` 为 Python 细化（TS 单一 InvalidDurableEventError）；TS `readAfter` 跳过未知类型 vs Python fail-closed 的最终裁决已落在 §3.2.1（撤回 B 原开放问题 #3 的临时方案）。
9. **§4 开放问题清理**：#1（prompt 语义）、#2（快照形状/hash）、#4（expected_seq）已被 T-A-002 裁决关闭，标记已决并引用 §9；#3（未知版本读取行为）同上；新增一条"A 待裁：ID 前缀 / files 字段 / extra-forbid 兼容路线"（即本报告 Minor-1~3 对应项）。

## 5. 修订项汇总

| 级别 | 条目 | 归属 |
|---|---|---|
| minor | M-1 version/seq bool 穿透（events.py L130/L145） | 下次触及 contracts 的任务（或 A 裁决批次顺带） |
| minor | M-2 UserMessage `files` 字段（messages.py） | A 裁决 → B 修订方案 + C 补丁 |
| minor | M-3 extra=forbid 与同版本可选字段的兼容声明 | A 裁决（契约/parity 一行记录） |
| minor | M-4 反例补测（version:"1" / seq:True / surrogate） | 随 M-1 补丁 |
| nit | N-1 CoreError.details 恒序列化记录 | parity.md 一行 |
| nit | N-2 id/text 边界断言 | 随 M-1 补丁 |
| nit | N-3 DurabilityMismatchError 差异记录 | parity.md 一行 |
