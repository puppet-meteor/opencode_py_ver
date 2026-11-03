# R2-S4 结构化代码审查（席位B · T-B-019 · 2026-11-03）

- **审查对象**（S4 窗口两 worktree，各自单提交，父均=36cf828，diff 面即该提交）：
  1. r2-s4 `8fcc2cb`（`feat(session): R2-S4 ContextBuilder (T-C1-017)`，3 files +980：`src/opencode/session/context.py` 375L 新增 + `tests/contract/test_context.py` 522L 27 测函数/42 测点新增 + 进度文件）。
  2. r2-s4a `8fd30ff`（`test(ai): pin connect-refusal retry exhaustion + 5xx surfacing (T-C1-017a)`，3 files +157：maas.py 纯注释 +26 + test_ai_adapter.py +75 两测 + 进度文件）。
- **审查基准**：`doc/CORE-CONTRACTS.md`（工作区当前文本，含 A-007 五项终审 §10 L391-397）；`doc/plans/r2-slices.md` §2 L98-109；TS 原始实现三文件（指纹 B 本会话独立复验：`Get-FileHash` SHA256 三匹配 `doc/source-baseline.json` L29/31/32——to-llm-message.ts / context.ts / model-request.ts）；`doc/reviews/R2-S3-review.md` §2/§4/§6-5；`doc/reviews/R2-S5-review.md` §2 M-1；进度文件 T-C1-017.md / T-C1-017a.md。
- **门禁**：任务书明示无需重跑——调度器已在两冻结面全量重跑：r2-s4 pytest 486 passed（444+42 零回归）/ruff 全过/format 152/mypy 50；r2-s4a 446 passed（444+2）/ruff 全过/format 150/mypy 48。B 未重跑门禁，按调度器实测值采信。
- **写权面**：B 仅新增本文件（主树 `doc/reviews/R2-S4-review.md`）；git 全程只读。r2-s4 写权面亲核=3 路径全新增（git show 全 new file mode）；r2-s4a=maas.py +26 全注释行（diff 逐行核验）+ test 追加 + 进度文件，与各自授权清单精确一致。

---

## 1. 结论

**通过**（r2-s4 与 r2-s4a 均可并回；无修复前置条件）。

- 12 个审查维度全部符合契约与切片方案；TS 三锚（to-llm-message.ts L318/L243-244、model-request.ts L89/L99/L183、context.ts L28/L35）与 L51 "~4 chars/token" 引文逐条核对无误，TS 收窄处均有披露且理由成立（unsupportedParts=Python 内容并集无 media 面；Selection/Loaded=收窄为注入设置+内存序列；outputLimit 的"压缩迁就"被契约 §7 L339 反向钉死为"拒绝不缩水"，docstring 明示）。
- §2.2 L80 红线结构性成立：`build()` 入=契约消息 union+`ToolSpec`、出=`GenerationRequest`，全仓 grep 生产代码构造点唯一（context.py:256）；模块零 openai/SDK import。
- ④-3 点分名与 `fs.read↔fs__read` 往返按 T-A-006 裁决落地并经冻结 S3 面双测钉定（详见 §4 三方闭合结论）。
- 发现计数：**blocker 0 / major 0 / minor 3 / nit 3**。三项 minor 均为测试强度或文档指引性质，无实现正确性缺陷，建议随 S6 前微补丁或任意后续触点落实，不构成并回条件。r2-s4a 微补丁 M-1/N-2 注释与真实代码路径逐句核对**无注释性失真**；M-2/③-3 两测强度达标（attempts 直数、零耗时下界推断）。

## 2. 发现分级

### blocker / major

无。③ 强制浮报完整性抽查（R1-S8 教训）清洁：B 独立走查 context.py 全量防御分支，与进度文件 ③ 清单 18 项一一对应，**未发现未浮报防御分支**（详见 §6.1）。

### minor（3）

**m-1 · `ceil` 取整语义未被任何边界测试钉定**
- 位置：`src/opencode/session/context.py:214`（`return ceil(chars / _CHARS_PER_TOKEN)`）× `tests/contract/test_context.py:376-403,405-416,418-434`。
- 理由：全部预算边界值（恰值 3000 字符→750、超限 3004→751、肥工具 224 字符→56）**均为 4 的倍数**——floor 实现与 ceil 实现在这些输入上产出相同，若有人把 `ceil` 改为 `//` 或 `round`，27 测全绿零报警。估算器取整规则（§7 L339 预算语义的一部分：向上取整=保守方向）因此未被锁定。进度 ③-8 "text/reasoning 有恰好/超限边界用例"的表述成立，但所选数值对取整规则不敏感——属断言强度缺口，非防御分支漏报（故不按 R1-S8 计 major）。
- 建议：+1 测试例：`_user("a" * 3001)`（3001/4=750.25→ceil=751>750）断言 `ContextLimitExceeded` 且 details `estimated_input_tokens==751`；可选再加 2997 字符（→750 恰过）双向钉定。两行改动、零实现触碰。

**m-2 · catalog 深拷贝隔离测试未钉定嵌套 schema 变异**
- 位置：`src/opencode/session/context.py:254`（`snapshot = [tool.model_copy(deep=True) for tool in tools]`）× `tests/contract/test_context.py:440-451`。
- 理由：测试构建后变异了 `catalog.append(...)`（证明请求不绑定调用方列表——浅/深拷贝下均成立）与 `spec.description`（标量字段；`model_copy` 无论深浅都生成新实例并按值拷贝标量——**浅拷贝实现同样通过该断言**）。`deep=True` 相对浅拷贝的增量价值恰在嵌套结构（`input_schema` dict）隔离，而测试未变异嵌套字段。§5.1 L253 "固定 catalog 快照"的深隔离性质因此未被锁定。
- 建议：测试追加构建后 `spec.input_schema["properties"]["mutated"] = ...`（或对既有 key 赋值），断言 `request.tools[0].input_schema == _read_tool().input_schema` 不受影响。三行改动。

**m-3 · chars/4 估算器对 CJK 文本系统性低估（~2-4×）**
- 位置：`src/opencode/session/context.py:102-107`（`_CHARS_PER_TOKEN = 4`，引 TS model-request.ts L51 "can run low on dense text such as code"）。
- 理由：4 chars/token 对拉丁文本合理，对中文主导文本（实际约 1-2 chars/token）**低估 2-4 倍**——中文为主的会话（本项目文档与用户面即中文场景）估算输入可能仅实际 token 的 1/2~1/4，本地 `ContextLimitExceeded` 可能永不触发，改由 provider 侧拒绝（fail-visible、无静默裁剪，方向安全；TS 同款弱点=继承面非新引入，且 TS L51 自认密集文本偏低）。但 safety_margin 是唯一吸收旋钮，常规尺寸（如 50/1000=5%）无法覆盖中文误差量级。后果限于"超限错误由 provider 而非本地报出、错误面体验略差"，不损数据正确性。
- 建议：docstring 增一句宿主 sizing 指引（CJK 主导历史须显著加大 safety_margin 或下调可用预算）；R3+ 可考虑可配置乘数或真实 tokenizer（扩展点已单点化：`RuntimeSettings`+`_estimate_input_tokens`，进度 ④-6 已浮报）。需 A/调度器表态采纳与否（见 §8-3）。

### nit（3）

**n-1 · 模块 docstring TS 锚精度可补一行**：`context.py:24` "empty text/reasoning parts are dropped" 未注 TS 例外——to-llm-message.ts L206 空 reasoning **携 providerMetadata 时保留**（Python §2.1 `AssistantReasoningPart` 无 metadata 字段=结构性不可达，零行为影响）；TS L173-185 的 reasoning→text 降级族（错误/中断/换模型）亦未注"收窄"（Python 无 native/providerMetadata 面，MAAS wire 侧本就丢弃回放 reasoning——maas.py:322 "replayed reasoning is not sent back"，S3 评审 ③-9 已裁）。可选一行注记，便利后续移植对照。

**n-2 · `timeout_s=+inf` 被接受**：`context.py:159-161`——NaN 因 `not (nan > 0)` 偶然被拒，但 `float("inf") > 0` 为真、且 `GenerationRequest.timeout_s`（llm.py:148 `gt=0`）同样放行，"无期限请求"可直达 adapter deadline 推导。可选 `math.isfinite` 守卫一行。契约 §2.2 L78 未设上界，非违规。

**n-3 · `temperature` 无界（负值接受）**：`context.py:162-166` 只拒 bool/非数值——负温度将由 provider 侧拒绝。契约 §2.2 L78 未设界；可选守卫，优先级最低。

（核对注：r2-s4a M-1 注释把 R2-S3-review §2 M-1 缓解四点中的 ④"立即拒绝型 ConnectError 不经过该竞态"折入 (3) 末句表述——内容零丢失、纯编号压缩，不构成注释性失真，不单列发现。）

---

## 3. 契约符合性清单（行号级，实现/测试双位置）

| 契约条款 | 要求 | 实现位置（r2-s4 worktree） | 测试位置 | 判定 |
|---|---|---|---|---|
| §2.2 L78 | `GenerationRequest` 字段全集；`max_output_tokens` 推导 | llm.py:131-148（S1 冻结面，docstring 自钉 "ContextBuilder is the only producer"）；context.py:256-267 构造全集，L263 `max_output_tokens=output_reserve` 逐字 | test_context.py:141-196 逐字段断言+整请求 wire 往返（`model_validate(json.loads(model_dump_json()))==request`）；:376-385 恰值预算下断言全额 reserve==200 | 符合 |
| §2.2 L80 红线 | 契约消息+`ToolSpec` 入、契约类型出；SessionMessage/openai/wire 不过界；ContextBuilder 唯一生产者 | context.py:100（`type SessionMessage`=§2.1 并集别名，非新 wire 模型）；build() 签名与构造（L230-267）；模块 import 面零 openai/SDK（B 全文走查：仅 json/collections/dataclasses/math/typing+contracts/storage）；全仓 grep：src 内 `GenerationRequest(` 构造点唯一=context.py:256（llm.py:131 为类定义） | 结构性（grep 证据）；tests 侧 test_ai_adapter.py:89 / test_provider_spike.py:64 为 S3/r0 冻结测试 fixture（adapter 测试需无 builder 构造请求），非生产者、非 S4 引入 | 符合 |
| §2.2 L80 | tool_call part `name` 必须点分 `namespace.name`；tool_result 携配对 tool_call_id | context.py:331-346（call+result 同源成对、name 原样携带）；L315-330（非终态/重复拒绝） | test_context.py:289-323（call_ids==result_ids 逐序恰一）；:456-465（点分+与 ToolSpec 一致） | 符合（点分名专项见 §4） |
| §3.3 L212 | 每个合法 tool_call_id 恰一 tool_result 才能进下一请求 | context.py:112（`_TERMINAL_TOOL_STATES` 五终态）；:315-323（pending/running→`tool_result_missing`）；:325-330（跨消息查重→`duplicate_tool_call_id`）；:286-292（未知 history 条目 fail-closed） | test_context.py:325-331（五终态参数化回放）；:333-343（pending/running 参数化+details 钉定）；:345-360（重复+details）；:275-279（object() 注入+details） | 符合（fail-closed；S6 义务不被吞并，见 §4） |
| §5.1 L253 | 每次 provider 请求固定 catalog 快照 | context.py:253-255（入参即快照+`model_copy(deep=True)` 隔离，零 registry 重查）；registry.snapshot() 接线延后 S6（B 亲核：r2-s4 src/opencode 无 tools/ 包，S5 未在基线——延后成立且 docstring 已注） | test_context.py:440-451（构建后调用方变异不达已建请求+调用方自身变异存活）——强度缺口见 m-2 | 符合（附 m-2） |
| §7 L339 | `available = context_limit − output_reserve − safety_margin`；达到预算→ContextLimitExceeded、保留历史、提示新建会话；不静默裁剪/无边界摘要；未知上限=宿主配置（context_limit 必填无默认） | context.py:361（公式）；:363-374（超限拒绝，details 五字段，文案含 "start a new session"）；:140-142（`context_limit` 必填无默认——类型面强制）；:173-186（异常类）；docstring:43-48 | test_context.py:376-385（恰值过+全额 reserve）；:387-403（超限全 wire 面断言 code/retryable=False/details+**历史原样保留断言**）；:490-497（缺省→TypeError） | 符合（附 m-1 取整钉定缺口） |
| §8.1 L107（provider.md:106-107） | reasoning 计入输出侧预算（实测 max_output_tokens=64 被 reasoning=61 全额消耗、零正文、finish=length） | 结构保证：公式 L361（reserve 不入 available——输入侧永不借用）+ L263（全额 reserve——生成 reasoning 骑其内）；docstring:49-56 注记（实测行为逐字：64/61/零正文/finish=length）；测试注记 ：376 内注释+:405 docstring（回放 reasoning=输入侧/生成 reasoning=输出侧对偶） | 方案 r2-slices L100 硬要求"注记落 docstring+测试"——**两处均落** | 符合 |
| §8 L346 | `ContextLimitExceeded` 在 code 枚举 | contracts/errors.py:39（S1 冻结）；context.py:184-185（`code=CoreErrorCode.CONTEXT_LIMIT_EXCEEDED`、`retryable=False`，经 `StoreError` 基类携 §8 `.core` wire 面——txn.py:171-189） | test_context.py:387-403（code/retryable/message/details 全断言） | 符合（类归属提请 A 确认，见 §8-1） |
| §10 L381-387（T-A-006 ④-3） | S4 核对请求 part 点分名及 `fs.read↔fs__read` 往返；S4 请求面不得使 S6 义务（chunk→计划→请求回放一致性+跨响应重复 tool_call_id 查重）不可达 | context.py:331-336（`name=part.name` 原样——"neither invents nor normalizes"，docstring:225-227 自钉）；:325-330（查重为 fail-closed 拒绝而非吞并） | test_context.py:456-484（点分一致性+经冻结 S3 面 `tool_wire_name_from_dotted`/`parse_tool_wire_name` 往返） | 符合（详见 §4） |
| §10 L391-397（T-A-007 五项） | 与 S4 相关者：裁决 5 invoke `tool_ref` 收原始 requested name（S5 catalog 接缝为 wire 名、模型层点分按 §2.2 转换、调用方不得预解析） | context.py 不预解析、不发明名、不校验点分性（原样透传）——S5 的 requested_name 原值语义保持可达；其余四项（④-2 runs 主键/④-3 idle 契约/④-13 UnsupportedCapability/④-1 ToolContext）非 S4 触点，S4 零读取 ConfigSnapshot（grep 核：仅 docstring 提及 L30/L134） | — | 符合（零触点确认） |
| r2-slices §2 L98-109 | 目标全量+TS 三文件指纹+验收 A 前半/验收 C 终态面+测试计划四项+OP-1 注入面 | L100 全要素落（组装/预算/reasoning 注记/超限语义/max_output_tokens）；L103-106 三文件映射+指纹（B 独立 SHA256 复验三匹配）；L109 OP-1=RuntimeSettings 构造注入（context.py:130-171），ConfigSnapshot v1 零读取 | L108 测试计划四项全落：逐字段（:141）/预算边界（:376-434）/配对完整性（:289-370）/快照不可变性（:440） | 符合 |
| §10 L383+L389（r2-s4a） | OP-8 ③-3 补测四断言；M-1 注释/M-2 直数补测排期 | maas.py:433-451（M-1 注记）/529-535（N-2 注记）——逐句对照真实代码无失真（见 §5 维 11）；test_ai_adapter.py:1060-1106（M-2）/1108-1124（③-3） | 四断言齐：恰一末位 error 无 finish/code=ProviderUnavailable/retryable=True/`len(server.requests)==1`；M-2 另加 `calls==3` 直数 | 符合 |

## 4. S4↔S5↔S3 点分名三方闭合专项结论（T-B-016 §6-5/§6-6 的 S4 段）

**结论：S4 段闭合，链路级论证成立；三方全闭合的剩余条件在 S5（T-B-017B 热修）与 S6（接线），S4 面未使任何一段不可达。**

1. **S4 发出面**：`LLMToolCallPart.name`/`LLMToolResultPart.name` 直载历史 `AssistantToolPart.name`（context.py:331-346，`name=part.name` 原样），不发明、不规范化、不预解析——点分性是 S3/S6 生产侧不变量（§10 L387 已裁"不可反解 provider 名原样保留+未知名 not_started 失败"语义，原样透传与之自洽）。
2. **S3 wire 面（本基线冻结代码，B 亲核）**：请求侧 call part 名经 `tool_wire_name_from_dotted`（maas.py:330）、catalog 名经 `tool_wire_name`（maas.py:352）→ wire `fs__read`；流侧 `ToolCallEndChunk.name=parse_tool_wire_name(acc.name)`（maas.py:562）→ 回 `fs.read`。**test_context.py:468-484 钉定的正是 wire 路径实际使用的两个映射函数**（L330/L562），非仅 S4 内部自洽往返——这是 S4 在无 S5 树内可做的最强链路钉定。
3. **S5 注册面（不在本树）**：`parse_tool_wire_name` 首 `__` partition 的可逆前提=namespace `__`-free 且不以单 `_` 结尾——R2-S5-review §2 M-1 已实证单下划线边界族穿透 register() 且往返断裂（`("fs_","read")`→wire `fs___read`→错回 `fs._read`），热修=T-B-017B 在审。**三方闭合以此为前置**：热修落地则 `fs.read↔fs__read` 在注册门保证下唯一可逆。
4. **S6 接线面（未到）**：`registry.snapshot()` 入 `build(tools=...)`（仅调用方变，context.py docstring 已注）；跨响应重复 tool_call_id 查重与 chunk→计划→请求回放一致性归 S6（§10 L387）——S4 的组装面查重（context.py:325-330）为 fail-closed 拒绝而非静默跳过/合成占位，**不吞并也不阻断 S6 在持久化层的独立实施**；S6 回放测试可继续用非点分/畸形名走 S4 透传路径验证 not_started 失败语义。
5. 闭合链全文：S4 `fs.read` → S3 `fs__read` → provider 回显 → S3 `fs.read` → S6 持久化 `AssistantToolPart.name="fs.read"` → S4 回放原样。**每一段均有已测代码或已裁义务承载，无断裂点**（S5 段待 T-B-017B）。

---

## 5. 审查重点 12 维明细（实现+测试双行号+断言强度）

**维 1 · §2.2 L80 红线结构性** — 实现：build() 入参 `request_id: str / history: Sequence[SessionMessage] / tools: Sequence[ToolSpec]`、出参 `GenerationRequest`（context.py:230-267）；`SessionMessage` 为 §2.1 三型别名（L100）；import 面零 openai/SDK。测试：结构性反面钉定 test_context.py:250-273（产出消息永不混合 part 型——system/user 纯 text、assistant=text/reasoning/tool_call、tool 恰一 tool_result，直接回应 R2-S3-review ③-9 "S4 审查核对项①"）。断言强度：强（逐 part isinstance 全枚举）。**grep 全仓：src 构造点唯一（context.py:256）**；tests 侧两处直构为 S3/r0 冻结 fixture（非生产者，红线约束的是层间传递非测试自备请求）。判定：符合。

**维 2 · 组装规则 TS 锚逐条** — 实现→测试映射（TS to-llm-message.ts 锚点逐条亲核）：
- user→单 text part：context.py:277-280 ↔ TS L256-262（含 L259 `text === ""` 精确空串判定，Python L278 `== ""` 同款无 trim——**逐字对齐**）；测 test_context.py:141-196（值断言）+:213-215（空 user 丢弃）。强。
- assistant→text/reasoning+tool_call parts：context.py:298-352 ↔ TS L162-201（本地工具 `executed !== true`→仅 call 入 assistant 内容，TS L196）；测 :141-196（parts 型序+值）+:217-233（空 text/空 reasoning part 丢弃，TS L204/L206 前半）。强。
- 每 tool result 独立 tool-role 消息紧随发 call 的 assistant 消息：context.py:352-354 ↔ TS L208-219（`results`→`Message.tool` 列表缀于 assistant 消息后，顺序=parts 内序）；测 :141-196（角色序断言）+:289-323（双 call 逐序配对）。强。TS L200-201 hosted 工具 `[call, result]` 内联路径=Python §2.1 无 hosted 概念，收窄合理（docstring 列锚未逐一列差异——见 n-1）。
- idle→零贡献：context.py:283-284 ↔ TS L243-244（`case "idle": return []`，行号吻合）；测 :206-211。强。
- 空产出：空 user 文本/空 text part/空 reasoning part 全丢弃（L278/L307/L311）+:235-240（仅空 part 的 assistant 不产消息）+:242-244（空 history→空 messages）+:246-248（无 tools=`[]` 状态非缺字段）。强。
- TS L206 后半（空 reasoning 携 providerMetadata 保留）与 L173-185（reasoning→text 降级族）为 Python 无对应面的收窄——结构性不可达（messages.py:117-126 无 metadata 字段）、S3 ③-9 已裁 wire 侧丢弃回放 reasoning（maas.py:322），n-1 建议补注记。判定：符合（附 n-1）。

**维 3 · 配对守卫 fail-closed（§3.3 L212）** — 实现：非终态拒绝 context.py:315-323（`tool_result_missing`）、跨消息重复 tool_call_id 拒绝 :325-330（`emitted_calls` 有序集全程跨消息）、未知 history 条目 :286-292（`history_entry_type`）、未知 assistant part :348-353（`assistant_part_type` fail-closed，未测=③-4 浮报）、五终态回放 L112+331-346。测试：:333-343 参数化 ×2 拒绝+details 三字段钉定；:345-360 重复+details；:325-331 五终态参数化；:275-279 object() 注入+details；:362-370 终态空 content 原样携带（不合成占位文本——§2.1 L72 是上游规则，处置正确）。分类意见：`StoreValidationError`（txn.py:210-213 "caller input is invalid"）合理——history 是 build() 的调用方输入、经 `StoreError` 基类携 `.core` wire 面（txn.py:182-189）供 S6 映射；孤儿 result/结果侧重复=结构性不可能（③-1/③-2，B 复核成立：call/result 同迭代同源、含 call part 的 content 必非空→result 必随其后；同消息内重复与跨消息同分支同 violation）。判定：符合。

**维 4 · token 预算（§7 L339）** — 实现：公式 context.py:361；估算器 L188-214 纯函数确定性（无时钟无随机），覆盖 text/reasoning 文本（L197-198）、tool_call 名+`json.dumps(input)`（L199-200）、tool_result 名+content（text 文本/file 按 uri+mime+name 存储尺寸保守计，L201-206）、catalog namespace+name+description+`json.dumps(input_schema)`（L209-210——TS L53-56 WINDOW_HEADROOM 理据"Providers count a few tokens more than we do (chat template, tool schemas)"如实映射）；`ceil` L214。注：回放 reasoning wire 侧被 adapter 丢弃（maas.py:322）而估算器仍计入——保守方向，安全。测试：恰值 3000→750 过（:376-385）、超限 3004→751 拒+全 wire 面+历史原样保留（:387-403）、回放 reasoning 计入输入侧（:405-416）、catalog 计入（:418-434）。无静默裁剪/摘要=结构保证+测试双钉。强度缺口：ceil 语义（m-1）+CJK 保守性（m-3）。判定：符合（附 m-1/m-3）。

**维 5 · max_output_tokens=output_reserve 逐字（§2.2 L78）** — 实现：context.py:263 单点赋值+docstring:44-46+代码内注释 :261-262（§8.1 L107 引用）；TS outputLimit L89 的"压缩迁就历史（含 OUTPUT_TOKEN_MIN=1024 下限）"路径被契约 §7 L339 反向钉死为"全额 reserve+超限拒绝"，差异在 docstring 明示（"never shrunk to fit a larger history"）——契约优先于 TS，收窄披露成立。测试：:376-385 恰值预算下**同时断言** `== settings.output_reserve` 与 `== 200`（满额双断言）；:387-403 超限拒绝而非缩水（结构性：无任何缩水分支存在）。判定：符合。

**维 6 · reasoning 计入输出侧（§8.1 L107 硬输入）** — 实现：结构保证=公式 L361（reserve 从 available 中整体扣除——输入侧永不借用）+L263（全额 reserve——生成 reasoning 骑其内）；docstring:49-56 注记（实测数字 64/61/零正文/finish=length 与 provider.md:106-107 逐字一致；"TS records no such endpoint behaviour; this is a Python-side hard input"归属正确）。测试：:376-385 内注释+"reserve is never shrunk … generated reasoning rides inside it"；:405-416 docstring 注记+对偶钉定（回放 reasoning=输入侧计入估算恰值/超限双向）。方案 L100 "注记落 docstring+测试"**两处均落**。判定：符合。

**维 7 · ④-3 点分名+往返** — 见 §4 专项。实现 context.py:331-336（`name=part.name` 直载+docstring:225-227 "neither invents nor normalizes"）；测试 :456-465（点分且与 `ToolSpec.namespace.name` 一致——Catalog 一致性即 S5 resolve 前提）+:468-484（`fs.read↔fs__read` 经冻结 S3 面两函数往返，`ai/maas.py` 只读 import 消费=冻结清单合规）。判定：符合。

**维 8 · catalog 快照（§5.1 L253）** — 实现：context.py:253-255（入参即快照、深拷贝、零重查）；registry.snapshot() 接线延后 S6=docstring 披露+B 亲核（r2-s4 src/opencode 无 tools/ 包——S5 未在基线，延后归类成立，S6 接线仅调用方变）。测试：:440-451（列表 append+标量 description 变异双隔离+调用方自身变异存活）。强度缺口：嵌套 `input_schema` 变异未钉（m-2）。判定：符合（附 m-2）。

**维 9 · OP-1 注入面（方案 L109）** — 实现：RuntimeSettings 构造注入（context.py:130-171，frozen dataclass——运行时资源用 dataclass 符 §1）；零 ConfigSnapshot 读取（grep 核：仅 docstring L30/L134 文字提及；service.py/state.py 的 ConfigSnapshot 为 storage 自有面，与 S4 无涉）；`context_limit` 必填无默认（L140——§7 L339 "未知时要求宿主配置"的类型面强制）；system_prompt 可选独立面（L155-156，None/空→不产 system 消息，L272-276——TS baseTranscript L99 锚：system 面独立于 history 组装，结构同构）。测试：:490-497（缺省 TypeError）；:198-204（缺省/空串双形态）；零环境/文件/全局读取=B 全文走查（无 os/sys/env import）。判定：符合。

**维 10 · 设置校验守卫** — 实现：`_require_int_setting`（context.py:115-127：拒 bool+下限）+`__post_init__`（L158-171：timeout_s>0 拒 bool、temperature 数值或 None 拒 bool、`context_limit > output_reserve + safety_margin` 不变式 L167-171）。测试：:499-517（250≤250 拒/251 过边界双向）+:520-522 参数化 ×11（bool/float/0/负值全形态）。分类意见：**ValueError 而非 StoreValidationError 是正确选择**——RuntimeSettings 是宿主注入面（编程/配置错误），非 wire/存储面；StoreValidationError 携 CoreError wire 面（code=VALIDATION_ERROR）服务于调用方可见的存储拒绝，把宿主配置 bug 放上 wire 面反而污染错误语义。build() 内对 history 违规用 StoreValidationError、对 settings 违规用 ValueError 的不对称在两处 docstring 各有交代。边界注记：inf/n-2、temperature 无界/n-3。判定：符合。

**维 11 · r2-s4a 微补丁** —
- **M-1 注释逐句核对（maas.py:433-451，零代码改动=diff 逐行全注释）**：①"asyncio.timeout(header_s) wrapper and the httpx2 connect timeout … share one base and start together"↔实code：`async with asyncio.timeout(self._timeouts.header_s)` 包裹 create()（L452-454）×`httpx2.Timeout(connect=effective.header_s, …)`（L290-295）——同基数同时启动✓；②"asyncio.timeout -> TimeoutError -> header-timeout terminal (retryable=False, no retry)"↔L461-468（`_failure` 默认 retryable=False）✓；③"httpx2 connect -> APIConnectionError(cause=ConnectTimeout) -> _RetrySignal(None) -> up to 2 retries"↔L480-487（cause_name in ("ConnectError","ConnectTimeout")→`_RetrySignal(None)`）+L365-415 重试环（`_max_retries=2`，attempt=3 时耗尽）✓；④接受理由三点=(review §2 M-1 缓解 ①②③，④折入(3) 末句"an immediately-refused ConnectError never waits on either timer"——内容齐)✓；⑤"test_header_timeout's connection is established, so it deterministically maps to header"=review 缓解③原文语义✓；⑥"Possible follow-up: decouple … annotated only, deliberately not implemented"=任务书"可选项只注记不实现"✓。**无注释性失真**。
- **N-2 注释（maas.py:529-535）**：`finally: await stream.close()`（L536）前——"a raise inside finally replaces the in-flight one"=Python 语义准确；"SDK stream cleanup is not expected to raise … wrapping … deliberately not implemented"=review §2 N-2 原文语义。✓
- **M-2 测强度（test_ai_adapter.py:1060-1106）**：attempts **直数于 create() 边界**（monkeypatch 包装 `adapter._client.chat.completions.create`，`calls==3`——初次+两次重试，零耗时下界推断，A §10 L387 方法论合规）；断言族与 test_retry_exhaustion_caps_at_two_retries（:704）同款+`details=={"attempts": 3}`（↔maas.py:383-394 实际产出）+恰一终止 error+ProviderUnavailable+retryable=True；立即拒绝型（bind 0→取号→close→用）避开 M-1 竞态；NO_PROXY 于 client 构建前 setenv（monkeypatch 内自足，复跑零操作）；timeout_s=30.0 使命中面只剩重试上限（环境拒绝延迟 ~2s/次×3 被吸收）；TOCTOU 残余已浮报且失败方向正确（误抢占→连接不被拒→测试失败而非误绿）。**强度达标**。
- **③-3 测强度（test_ai_adapter.py:1108-1124）**：原生 `ScriptedResponse(status=500)` 走既有 `_write_status_response`（fake_openai_server.py:99-101/154-157/243-244——零 fake-server 改动，B 亲核路径存在）；四断言恰=§10 L383 义务原文：恰一末位 error 无 finish（`[c.type for c in chunks]==["error"]`）/code=ProviderUnavailable/retryable=True/`len(server.requests)==1` 不自动重试（↔maas.py:469-479 `retryable=status_code>=500`、无 RetrySignal）。**强度达标**。既有 34 测零触碰（append-only diff 核）。
- 判定：符合（0 blocker/0 major；M-1 编号压缩注记不构成失真）。

**维 12 · ③ 漏报抽查+④ 逐项裁决** — 见 §6/§7。

---

## 6. ③ 强制浮报完整性（漏报抽查）+ 浮报项逐项裁决

### 6.1 漏报抽查方法与结论

B 独立走查 context.py 全量分支（_require_int_setting ×3、__post_init__ ×4、request_id 守卫 ×1、_assemble ×5、_append_assistant ×7、_enforce_budget ×1、估算器 ×4、ceil ×1），与进度文件 T-C1-017 ③ 清单 1-14 项+架构注记 15-18 **一一对应，未发现未浮报防御分支**。R1-S8 教训项（漏报即 major）抽查清洁。两处测试敏感度缺口（ceil 取整、深拷贝嵌套）**非分支**（无任何分支被漏浮报——③-8 已如实浮报 tool 载荷边界未钉定，缺口在所选数值对实现细节不敏感），故按断言强度单列 m-1/m-2 而不按漏报计 major——此归类提请调度器知悉（§8-2）。

### 6.2 ③ 浮报项逐项裁决表（T-C1-017 ③ 1-18）

| 项 | 内容 | 裁决 | 理由/备注 |
|---|---|---|---|
| ③-1 | 孤儿 tool_result 结构性不可能 | **同意** | B 复核：call/result 同迭代同源（context.py:331-346）；含 call part 的 content 必非空（L352）→result 必随 assistant 消息后。结构性承载成立。 |
| ③-2 | 结果侧重复 tool_call_id 不可达 | **同意** | call 侧查重先行（L325-330，emitted_calls 全程共享）；同消息内重复与跨消息同分支同 violation（测试钉跨消息即钉分支）。 |
| ③-3 | estimator `json.dumps` 失败协议不可达 | **同意** | `input`/`input_schema` 均 pydantic JsonObject（llm.py:98；tools.py ToolSpec）——JSON-able 由构造保证。 |
| ③-4 | `assistant_part_type` 未测 | **同意不设测** | AssistantPart 判别联合（messages.py:153）构造即拒并集外型——仅 `model_construct` 旁路可达，非契约面；fail-closed 留给未来并集扩员是正确防御方向，扩员时再钉。 |
| ③-5 | request_id 非 str 半边未测 | **同意** | mypy 静态拦截（`request_id: str` 签名）；空串已钉（test:281-283）；非 str 注入属类型违反非运行时防御。 |
| ③-6 | timeout_s/temperature str 形态未构造 | **同意** | 与已参数化 bool 形态同一 isinstance 拒绝面（L160/L163）；代表已测。 |
| ③-7 | estimator file content 分支未构造 | **同意** | 方案测试计划（L108）未列；file 计数逻辑（uri+mime+name 存储尺寸保守计）方向保守；S6 若引入文件结果回放可届时随边界例补。 |
| ③-8 | tool 载荷边界未钉定 | **同意（附注记）** | 确定性纯函数+低风险成立；但所选恰值/超限数值（3000/3004）对 ceil 取整不敏感——单列 m-1（§8-2）。 |
| ③-9~③-14 | 已测防御六项（history_entry_type/非终态 ×2/重复/超限全 wire 面/历史保留/request_id 空串/settings 校验面） | **核对一致** | 实现位置与测试断言强度均复核通过（见 §5 维 1/3/4/10）。 |
| ③-15 | 混合 part 消息不产出（S4 核对项①） | **同意** | 结构性+测试双钉（test:250-273）；R2-S3-review ③-9 的 S4 核对义务就此关闭：adapter 丢弃面无新触点（assistant 混合 text/reasoning/tool_call 系 §2.2 L78 常态形，非 ③-9 所指非法混合）。 |
| ③-16 | 非点分 tool_call 名原样携带（S4 核对项②） | **同意** | ④-3 已裁语义=生产侧不变量+下游 not_started 失败；S6 回放核对项承接（§4-4）。 |
| ③-17 | 重复/缺配对 fail-closed 选择（S4 核对项③） | **同意** | 见 ④-4。 |
| ③-18 | 深拷贝机制 | **同意（附注记）** | 列表级+标量级隔离已钉；嵌套 schema 变异未钉=单列 m-2（§8-2）。 |

**③ 计数：同意 18（其中 ③-8/③-18 附断言强度注记）/ 需补测 0 / 升 A 0；漏报抽查 0 项未浮报。**

---

## 7. ④ 逐项意见表（T-C1-017 ④ 1-7）

| 项 | 内容 | 意见 | 理由/代价 |
|---|---|---|---|
| ④-1 | system_prompt 骑 OP-1 注入面（契约未列 system 面来源；TS baseTranscript L99 独立于 history） | **同意不设测（行为面已测）** | 保守读法成立：RuntimeSettings 为运行时 dataclass 非 wire 面，加字段零契约触碰；None/空两形态已钉（test:198-204）；R3 ConfigLoader/agent instructions 单点替换（docstring 已注）。若终审改判 system 面归他层，改动=构造器一字段+一分派行。 |
| ④-2 | IdleMessage 按 TS 锚 L243-244 剔除 | **同意（已测）** | TS 锚行号吻合、语义逐字（idle→`[]`）；契约无明文处以 TS 行为为锚是方案既定方法论；test:206-211 钉定。改判单点（_assemble 分派）。 |
| ④-3 | 非点分 tool_call 名原样携带不校验 | **同意不设测** | §10 L387 已裁"不可反解 provider 名原样保留+未知工具 not_started 失败语义不变"——builder 预拦截反而与已裁语义冲突（会先于 S5 resolve 失败）；合法路径已钉（test:456-484）。S6 回放一致性测试承接畸形名路径（§8-4）。 |
| ④-4 | 重复/缺配对 fail-closed 拒绝（vs 静默跳过/合成占位） | **同意（已测）** | §3.3 L212 "下一次生成前必须保证"=前置拒绝读法正确；合成占位是恢复协调器/runner 职责（§3.3 L210 恢复补写语义），builder 再合成会掩盖上游缺陷；StoreValidationError 分类见 §5 维 3。 |
| ④-5 | ContextLimitExceeded 复用 StoreError 基类+session 层定义；输入面复用 StoreValidationError | **同意+提请 A 确认归属** | SessionBusyError 先例同构成立（service.py:117-125："no storage module owns it"的 session 层拒绝，携 §8 `.core`）；wire code 在 contracts（errors.py:39）、异常类在 session——与 S1 冻结面零冲突。若 A 裁异常类归 contracts/ 或 service 面，搬移是机械操作（§8-1）。 |
| ④-6 | 估算器 chars/4（确定性；safety_margin 吸收误差；不注入 tokenizer） | **同意+附 CJK 注记** | TS L51 锚+确定性要求成立；ESTIMATE_ERROR(0.15)/WINDOW_HEADROOM(0.01) 折叠为单旋钮的简化已披露。**但 chars/4 对 CJK 低估 2-4×（m-3）**——docstring sizing 指引+R3+ 乘数/tokenizer 决策提请 A（§8-3）。 |
| ④-7 | catalog 入参形态 Sequence[ToolSpec]、registry.snapshot() 接线延后 S6 | **同意（已测）** | §5.1 L253 快照语义已按"入参即快照+深拷贝+不重查"满足；S5 未在基线（B 亲核无 tools/ 包）——延后是事实约束非选择；S6 接线仅调用方变（docstring 已注）。附 m-2 测试加强。 |

**④ 计数：同意 7（④-5 附归属确认、④-6 附 CJK 注记、④-7 附 m-2）/ 需补测 0 / 升 A 0。**

---

## 8. 需调度器/A 裁决事项清单

1. **④-5 类归属确认（提请 A）**：`ContextLimitExceeded` 落 `session/context.py:173-186` 继承 `StoreError`（SessionBusyError 先例 service.py:117-125 同构）。维持=零动作；若裁归 contracts/ 或 service 面=机械搬移（类 14 行+import+测试引用三处）。
2. **m-1/m-2 测试加强排期（提请调度器）**：ceil 取整钉定（+2 行：3001 超限例，可选 2997 恰过例）与深拷贝嵌套 schema 变异钉定（+3 行）——纯测试、零实现触碰，建议 S6 窗口前微补丁或随 S6 交付一并（S6 将实接 registry.snapshot()，m-2 的嵌套隔离届时更必要）。同时知悉 6.1 的归类：两缺口按断言强度计 minor、未按 ③ 漏报计 major（无分支漏浮报）。
3. **④-6/m-3 估算器 CJK 低估（提请 A/调度器）**：docstring 加宿主 sizing 指引一句（低成本、建议做）；可配置乘数或真实 tokenizer 留 R3+ 决策（扩展点已单点化）。本阶段不阻塞（fail-visible、TS 继承面）。
4. **S6 任务书义务保留确认（提请调度器）**：点分名 chunk→计划→请求回放一致性核对+跨响应重复 tool_call_id 查重（§10 L387）+ registry.snapshot() 接线 + 畸形/非点分名经 S4 透传的 not_started 失败路径回放测试（④-3 联动）——S4 面均为其保留可达性（§4-4），S6 任务书须显式列。
5. **环境事实知悉（r2-s4a ④-1，提请调度器）**：本机 Windows 注册表系统代理（127.0.0.1:10808 本地 VPN）经 httpx2 urllib 回落自动生效，会把"未监听端口"连接测试的 ConnectError 吞成代理 502/ReadError——后续任何连接失败/断流注入类测试需同款 NO_PROXY 处理或作全局决策（M-2 测内已 monkeypatch.setenv 自足）。
6. **n-1/n-2/n-3 顺势处理**：docstring TS 锚精度注记、`math.isfinite(timeout_s)` 守卫、temperature 界限——均无专窗必要，随 context.py 下次任意触点顺手处理即可。

---

## 附：核查方法与证据

- **TS 指纹独立复验**：`Get-FileHash -Algorithm SHA256` 三文件 vs `doc/source-baseline.json` L29/31/32——to-llm-message.ts `E7AF…8FF3`、context.ts `A24F…4E8B`、model-request.ts `E6DA…E009` **三匹配**（任务书称 T-A-006 亲核，B 独立复验一致）。
- **TS 锚行号亲核**：model-request.ts L51（"~4 characters per token…dense text such as code"——context.py:102-104 引文逐字）/L52-58（ESTIMATE_ERROR/WINDOW_HEADROOM/OUTPUT_TOKEN_MIN）/L89-97（outputLimit 拟合语义）/L99（baseTranscript）/L183-187（unsupportedParts）/L222/L231；context.ts L28/L35（Selection/Loaded）；to-llm-message.ts L155-219（assistant 组装：L196 本地工具仅 call 入内容、L200-201 hosted 内联、L203-207 空 part 过滤、L208-219 results 缀后）/L240-244（idle→[]）/L256-262（user，L259 精确空串）/L318-322（toLLMMessages flatMap）。
- **maas.py（r2-s4a）M-1 竞态机制亲核**：L290-295（httpx2.Timeout(connect=effective.header_s)）/L452-454（asyncio.timeout(header_s) 包裹 create()）/L461-468（TimeoutError→header 终态 retryable=False）/L480-487（APIConnectionError cause 分流→_RetrySignal(None)）/L365-415（重试环：max_retries=2、耗尽 attempts=3 retryable=True details={"attempts":3}）——M-1 注释六项事实主张逐句对上。
- **fake server 500 路径亲核**：fake_openai_server.py:99-101（ScriptedResponse status 默认 200）/154-157（_write_status_response：`HTTP/1.1 {status} Error`+JSON body）/243-244（status!=200 分流）——③-3 测零 fake-server 改动成立。
- **生产者唯一性 grep**：`GenerationRequest\(` 全仓——src：llm.py:131（类定义）+context.py:256（唯一构造点）；tests：test_ai_adapter.py:89、test_provider_spike.py:64（S3/r0 冻结 fixture）。
- **ConfigSnapshot 零读取 grep**：session/ 内仅 context.py:30/L134 docstring 文字提及；service.py/state.py 为 storage 自有面。
- **门禁**：B 未重跑（任务书明示调度器已复跑且与 C-1 自报一致）；两树写权面（git show --stat + diff 逐行）与授权清单精确一致。
- 审查耗时约 35 分钟（止损线内）；本报告增量写入（骨架+四段落盘）。
