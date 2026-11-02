# T-C1-002/002B contracts 补丁结构化审查（席位B · T-B-005）

- 审查对象：工作区补丁的检查点#15 提交形态（commit `fb8aa47 feat(contracts): harden wire contracts per T-A-003 rulings`；前置 `22ac58c` 记录 002B 补 N-2 缺口）。实现者进度：`doc/orchestration/progress/T-C1-002.md`。
- 基线：`doc/CORE-CONTRACTS.md`（含 T-A-003 终裁 §1/§2.1/§3.1/§8/§10）+ TS 原始实现 + B 自身 T-B-002 报告（M-1/M-4/N-2/Minor-2）+ T-B-004 版 `doc/plans/r1-slices.md`。
- B 复验：`uv run pytest -q tests/contract` → **58 passed**（44 旧 + **14 新增 TestContractHardening**，与任务口径一致）；`uv run ruff check .` → clean。源码逐点核验：`_reject_bool` helper（events.py L42）+ 三调用点（schema_version L89/version L159/seq L175）；`_EVT_ID/_SES_ID/_RUN_ID` 常量 + EventDraft/EphemeralEvent 各自 `Field(min_length=1, pattern=…)`；DurableEvent 继承 EventDraft（L167）→ id/session_id pattern 随继承生效；`PromptAcceptedData.run_id` `pattern=_RUN_ID`；`_require_run_metadata` 用 `"run_id" in metadata`（L132）+ str+startswith 校验；messages.py `id: str = Field(min_length=1, pattern=r"^msg_")`。

## 1. 结论：**通过**

0 blocker / 0 major / 0 minor / 2 nit。四项闭环全部核销，四 pattern 与 §1 bool 规则落地完备无漏网，测试断言 wire 行为且反例矩阵覆盖充分，与 T-B-004 版切片方案一致。

## 2. 四项闭环核对（维度1）

- **M-1（version/seq bool 穿透）**：核销。构造路 `EventDraft._version_not_bool`/`DurableEvent._seq_not_bool`（field_validator mode="before" 调 `_reject_bool`）；ConfigSnapshot 守卫重构为同一 helper（三处统一）；解码路 `decode_durable` 保留 bool+非 int 显式前置检查（C-1 论证成立：`True` 与 `(type,1)` 注册表键 hash 相等，泄漏即错解析）。B 在 T-B-005 复验 `True` 路径测试钉住。
- **M-4（三反例）**：核销。`version:"1"`（decode 路 EventDecodeError）、`seq:True`（构造路 ValidationError）、孤立 surrogate 经 canonical_json → UnicodeEncodeError（ValueError 子类，fail-closed）。
- **N-2（边界断言）**：核销（002B 补齐）。`EventDraft(id="")` 拒绝（三构造入口）、`UserMessage(text="")` 允许。
- **Minor-2（files 键拒绝钉住）**：核销。`files: []` 与 `files: [{...}]` 均 ValidationError；docstring 按 §2.1 方案 (b) 更新（R1 v1 无 files、R2+ 升版本+upcaster）。

## 3. 四 pattern 落地完备性 + §1 bool 覆盖（维度2/3）

四前缀全覆盖：EventDraft.id/session_id、EphemeralEvent.id/session_id（独立基类、不依赖继承，正确）、DurableEvent 经继承（L167 确认）、UserMessage.id、PromptAcceptedData.run_id、metadata.run_id 键"一旦出现即校验"（含 null/非 str/错前缀，`in` 判定使 `{"run_id":null}` 也拒）——与 §3.1 字面一致（见 §5-③）。SessionRef/RunRef/游标不在本批范围，正确（S6/S7 切片落地时按 r1-slices §2 R1-S6/S7 执行）。§1 整数语义字段排查：S1 pydantic 模型仅 version/seq/schema_version 三个 int 字段，全守卫；EventDefinition.version 为 dataclass 非wire，正确豁免；fixtures 零改动复核合理（既有 ID 本就符合约定）。

## 4. 测试质量（维度5）

14 测均为 wire 行为断言（构造/解码拒绝、合法值接受、request_id 无 pattern 之事实钉死）；反例矩阵含错前缀交叉（ev_/ses_/run_/msg_ 互斥）、空 id、metadata.run_id 三态、非 run-scoped 事件上的 run_id 键同样生效。无该测未测发现。

## 5. C-1 三遗留问 B 意见（维度4）

- **① version="1" 构造路 lax 强转不对称**：B 意见=**维持现状，不本批收紧**。§1 仓库级规则字面只裁 bool（"显式拒绝 True/False"），str→int 未被裁决；构造路收紧会改变 API 容错语义，超出 M-1 范围。若 A 希望两侧对称（构造路也拒非 int），是一行 `_reject_bool` 扩展，建议作为 R5 硬化批次项请 A 裁，勿现在改。
- **② surrogate 前置扫描**：B 意见=**记入 R3（ConfigLoader 引入字符串快照字段的切片）任务清单一行**，R1 值域不可触发且 fail-closed 已有测试钉住，不加死代码。r1-slices.md 无需改（R3 切片不属本文件），建议调度器在 R3 任务包定义时带入。
- **③ metadata.run_id"出现即校验"是否过严**：B 判定=**与 §3.1 裁决字面一致，无需回退**。契约原文"metadata 中的保留身份键 run_id 一旦出现，也必须是符合该 pattern 的字符串"——"一旦出现"即 whenever-present；null/非 str 均非"符合该 pattern 的字符串"。现行实现恰是裁决本意，非 run-scoped 事件上生效亦然。

## 6. 与 r1-slices.md（T-B-004 版）一致性（维度6）

S1"实施交接"行所述补丁范围（四 ID pattern、全部整数字段 before-validator 拒 bool、M-4/N-2 补测、files 阶段说明）逐项兑现；§3 差异表第 10 行、§2 R1-S7 四前缀注记与实现一致。无"最保守读法"替代实现项。

## 7. 修订项汇总

| 级别 | 条目 | 归属 |
|---|---|---|
| nit | N-a 进度文件记 13 测（002B 后实为 14），自报计数过期 | 无需改（本报告即勘误） |
| nit | N-b 构造路 str→int lax 不对称（遗留问①） | 请 A 下轮裁决（建议 R5 硬化项） |

无阻塞项；contracts 补丁可提交入库，R1-S2 可继续派发。
