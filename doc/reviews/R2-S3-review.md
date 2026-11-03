# R2-S3 结构化代码审查（Provider 协议 + MAAS adapter + R0 证据延续）

> 审查：席位B（T-B-016），2026-11-03。对象：worktree `r2-s3` 单提交 **`45ac954`**（父=main `fdb979f`；`git diff main...r2-s3 --stat` = 6 files **+1844/-0**：provider.py +21 / __init__.py +13 / maas.py +637 / test_ai_adapter.py +1049 / provider.md +26 / T-C1-015.md +98；worktree porcelain 干净）。基准：CORE-CONTRACTS（#68 终稿，含 T-A-005 两裁决）+ doc/plans/r2-slices.md §2 R2-S3（L86-96）/§4 OP-4/OP-7/OP-8 + doc/evidence/r0/provider.md（worktree 版 §1-§11，R0 原文零改动）+ TS 四文件（http.ts/executor.ts/model-retry.ts/aisdk.ts 行段亲读）+ S1 contracts 面（R2-S1 已并回）。
> 门禁复核：**免重跑**（调度器已四门禁全量复核与 C-1 自报一致：pytest **444 passed**=410 基线+34 新增零回归 / ruff check 全过 / format 140 files / mypy 48 files；7 fixture 逐文件 sha256 **7/7 byte MATCH**）。本审查为并回前最后一道质量门 + A-PROVIDER 风险面 R2 复验时点（r2-slices §6 L224）。

## 1. 结论

**通过**。

- 发现计数：**blocker 0 / major 0 / minor 2 / nit 4**。
- 两 minor（M-1 双计时器竞态、M-2 connect 重试分支零测试）均为**建议项**，不构成并回阻塞：M-1 两路径皆契约合法失败面且 spike 同构已存在（provider.md §3 映射表即该结构实测）；M-2 属如实浮报（③-4）的未测防御（B §8-3 定案允许），重试结构已由 429 路径全钉。二者的处置建议（补测草案/注释钉定）列入 §6 调度器清单，可随 S4 窗口或 OP-8 终审一并，不要求本切片返工。
- 契约符合性清单（§3）逐条全过；A-PROVIDER 复验五项全过；③ 强制浮报完整性抽查**无漏报**（2 项浮报盲区= M-1 行为不确定性注记+N-2 防御性边缘，均 nit/minor 级非分支）；§11 追记节七条差异与实现逐一对应准确。
- 证据延续硬条款（provider.md L94）：tests/r0 32 绿+7 fixture byte MATCH 由调度器亲核免重跑；本审查核 §11 内容准确性——11.2 七条逐条与 maas.py 实际实现对应无误（含 aisdk.ts `?? "unknown"` 回退 vs Python fails closed 的硬化差异如实记入 11.2-4）；11.3 归置理由正确（错误分片=本切片落地/重复 tool call ID=adapter/runner 边界留 runner）；R0 原文 §1-§10 零改动核对（diff 仅追加，§11=L131-155 共 25 行）。

## 2. 发现分级

### minor

**M-1 · create() 阶段双计时器竞态——连接超时场景的重试决策非确定**
- 位置：maas.py `_attempt` create() 段（`asyncio.timeout(self._timeouts.header_s)` 包裹 create()）× `MaasAdapter.__init__` 的 `httpx2.Timeout(connect=effective.header_s)`。
- 理由：两者计时基数同为 header_s 且同时启动。真实连接超时（服务器不 accept）场景下哪个先触发不定：asyncio.timeout 先→`TimeoutError`→header 超时终态（retryable=**False**，不重试）；httpx2 connect 先→`APIConnectionError(cause=ConnectTimeout)`→`_RetrySignal(None)`→**重试**（§8 L350"明确可重试的短暂网络错误"第二成员）。同一物理场景非确定地分叉为"不重试终态"vs"重试≤2 次"。
- 缓解（为何非阻塞）：① 两路径均为契约合法失败面（header 超时/重试耗尽 ProviderUnavailable），对上层终态等价（不成功即失败，不拼接不重启）；② spike 同结构已存在——provider.md §3 L47 差异列"另以 httpx2.Timeout(connect=header_s) 兜底连接阶段，同样映射 header"记录的正是 asyncio 分支，§4 L56 记录 ConnectError/ConnectTimeout 重试，二者在 R0 即并存，非本切片新引入；③ 已测路径不受影响——test_header_timeout（hang_headers，连接已建立）确定走 asyncio/httpx2 read 分支归 header（L606-620 断言 timeout_kind=header 稳定）；④ 立即拒绝型 ConnectError（无超时）不经过该竞态。
- 建议：maas.py 注释钉定该竞态与接受理由（两路径皆合法失败面）；可选后续微任务统一（如 header 包裹预算与 connect 预算解耦）。列入 §6。

**M-2 · 连接建立失败重试分支零测试覆盖（③-4 如实浮报的实质缺口）**
- 位置：maas.py create() 异常映射 `APIConnectionError` cause 分流段（cause_name in ("ConnectError","ConnectTimeout") → `_RetrySignal(None)`）；测试面无任何 connect 场景。
- 理由：§8 L350 重试集两个明文成员（429+连接建立失败）中 connect 分支完全无测试——重试循环结构（RetrySignal/sleep/上限/耗尽形态）已由 429 路径四点钉定，但 connect 分支的**异常源分流本身**（cause 检查→RetrySignal vs STREAM_INTERRUPTED 的边界）零覆盖。
- 建议（补测草案，低成本）：make_adapter 指向未监听端口→ConnectError（立即拒绝，非超时，**避开 M-1 竞态**）→断言形态与 test_retry_exhaustion_caps_at_two_retries（L703-714）同款：恰一个 error、`ProviderUnavailable retryable=True`、`details={"attempts": 3}`、重试请求发出 2 次（端口级计数或 elapsed 下界）。可现在补或与 OP-8 终审一并（若终审维持，建议必补；翻转则重写时一并）。
- 非阻塞依据：③-4 已如实浮报（B §8-3 披露义务达成）；行为与 429 路径共享全部下游结构。

### nit

**N-1 · 进度文件误记 provider.md 追加行数**：T-C1-015.md ① 记"+34L"，git stat 实为 **+26L**、§11=L131-155 共 25 行内容（任务书已知 nit，本审查核实确认）。仅进度文件措辞，不改证据文件本身（provider.md §11 实际内容准确）。

**N-2 · `_attempt` 读循环 finally `await stream.close()` 若自身抛异常会掩盖传播中的原始异常**：SDK 清理通常不抛，属未浮报的防御性边缘（③ 21 项外的唯一盲区）。建议注释或 `try/except Exception` 包装（可选；与 M-1 注释一并处理即可）。

**N-3 · `provider_metadata` 恒空 `{}`**：R0 §2 已记 spike 同款（"adapter 不产出 provider metadata，恒为空字典"）；契约无强制填充要求（§2.3 L102 末句仅约束"保存前按敏感字段规则处理"）。注记：S4/runner 若需 raw 模型名等元数据可经此槽（有界化+脱敏前提）——留后续需求，本切片不填合法。

**N-4 · ③-6 `tool_call_without_name` 与 without_id 同族对称但仅后者有代表测试**：两者位于同一收尾校验块（sorted(state.tools) 循环内相邻两检查）、同错误码 ValidationError、对称 violation 值。可选参数化补一例（改 `start_frame_without_id` 同款 helper 去掉 name 即可，极低成本）。

## 3. 契约符合性清单（实现/测试双行号）

### §2.2 模型请求

| 条款 | 实现位置 | 测试位置 | 断言强度 |
|---|---|---|---|
| L78 GenerationRequest 全字段消费 | maas.py `_attempt` params：model/messages/stream/include_usage/max_tokens；tools/temperature 条件注入 | L998-1035（params 注入+未设省略） | body 全断言：`stream_options={"include_usage": True}`、`max_tokens=64`、temperature=0.2；**未设省略断言 `"temperature" not in body`+`"tools" not in body`（"无工具是状态非缺字段" L1035 注释钉）** |
| L80 tool_result 配对 tool_call_id→每 tool_result part 一条 OpenAI tool 消息 | `_openai_messages` tool 分支逐 part 发射；`_tool_result_text` text 直传/file→占位 | L919-964（全形组装）+L967-995（文件占位） | body["messages"] 整列表断言（system/user/assistant reasoning 丢弃+tool_calls 点分反向/tool 逐 result）；file URI 替换断言 `[attachment not loaded]\nsummary` |
| L80 ContextBuilder 红线（SDK 类型不发另一层）——adapter 侧半条 | adapter 消费 S1 GenerationRequest/产出 S1 ProviderChunk，签名/返回类型/docstring 无 SDK 渗出 | L890-911 SDK 红线测试 | **模块面断言 `type(chunk).__module__=="opencode.contracts.llm"` + `model_dump(mode="json")` 后 `json.dumps` 可行 + JSON 原语递归断言（SDK 对象不得随行）** |
| L83-84 async for 消费/实现 async generator/支持关闭与取消 | provider.py 协议 docstring 钉定（async for/aclose/CancelledError）；maas.py `stream` 具体注解 `AsyncGenerator[ProviderChunk, None]`（协变细化，④-7） | L824-844（取消：task.cancel→CancelledError 上抛+非 error chunk+任务零泄漏）+L847-868（早 aclose：`await stream.aclose()` 后零泄漏+seen≥1） | 行为断言非仅类型断言；settle 模式同 spike 冻结用例（Windows proactor 假服务器机理注记 L861-863） |

### §2.3 ProviderChunk（L88-102 逐条）

| 条款 | 实现位置 | 测试位置 | 断言强度 |
|---|---|---|---|
| L88 公共字段：request_id 恒一/index 从 0 递增/type/provider_metadata | `_Sequencer` 请求级（非 attempt 级）——中断部分流的终止 error chunk 续接序 | `validate_sequence`（L135-158）：request_id 恒一/index 0 基连续无跳号无重复/单一终止恒末位；6 反例参数化（L173-186）+正例含 error 终止合法（L189-197） | **34 测点共用序列校验器**（每 golden/超时/重试/组装测试均过 validate_sequence）——比 spike helper 收窄处（每型必填查删除）有 docstring 论证（pydantic 构造即强制，OP-4） |
| L92-98 八 wire 型发射 | text_delta（合成 `text-0`，§6-1 已记）；reasoning_delta **永不产生**（§6-6：OpenAI wire 无槽，已记差异非缺陷）；tool_call_start（id?/name? 可选——首帧带 id/name 才发）；tool_call_args_delta（按 call_index）；tool_call_end（id+name 必需，缺失→ValidationError `tool_call_without_id`/`tool_call_without_name` 新防御路径）；usage（`_map_usage`：reasoning 经 completion_tokens_details、cache_read 经 prompt_tokens_details.cached_tokens、**cache_write=None 注释钉 §8.1 L109"不冒充 0"**）；finish（`_map_finish_reason` stop/tool_calls/length/content_filter|filter→五值+raw_reason 原样）；error（终止 ErrorChunk） | L205-300（golden 01/02 双序）、L303-397（golden 03 交错+args 分片+tool_call_end 按 call_index 序）、L774-816（缺 id 拒绝）、L575-598（error 唯一终止） | golden **`model_dump(mode="json")` 全字段面**逐 chunk 逐键断言（provider_metadata={}、cost_usd=None、Usage.cache_write=None 显式钉——OP-4 全字段面）；usage 双 wire 序皆存活（finish 后仅 usage 帧合法） |
| L100 adapter 聚合 ID/名、runner 聚合参数、tool_call_end 才解析完整 JSON、未知工具不猜测 | `_ToolAccumulator`（首帧带 id 或 name 才建+发 start；name 分片覆盖更新；args 追加）；`raw_arguments` 原样透传不解析（解析归 runner）；`parse_tool_wire_name` 不可反解原样透传 | L303-397（聚合+分片+名字点分化 fs__read→fs.read）+L744-771（`read` 无分隔符透传，注释钉"unknown-tool handling is downstream §2.3 L100"）+L744-755 单元（`fs__`/`__read` 空侧透传+往返一致） | **协议分工钉定**：adapter 只发身份（start 携带 id/name 可选）+end 携带完整 id/name/raw_arguments；未知名不猜测不在 adapter 查工具 |
| L102 一次请求最多一个终止 chunk（finish 或 error）；EOF 无终止=中断；取消经 CancelledError；held-back finish+usage 后发不丢 | 终止唯一性由结构承载（FinishChunk 仅流尾发射一次/error 经 `_StreamFailure`→stream() 边界转换一次）；EOF 无 finish→`ProviderStreamInterrupted`（"stream ended without a terminal chunk"）；CancelledError 无 except 直通；`_map_chunk` finish_reason 只存 `state.pending_finish`，流尾统一发 end+Finish | L575-598（4 场景参数化：无终止/重复 finish/终态后内容/重试耗尽——每场景恰一个 error+`not [...finish]` 断言 held-back finish 永不落地）+L400-428（EOF golden）+L469-572（wire 违规拒绝 06/07+tool call 变体）+L824-844（取消） | **四种失败面共用"单一终止+恒末位"断言**；违规流 `len(server.requests)==1` 断言绝不重试（L507/L548/L572） |

### §8 脱敏与 retry owner（L348/L350 逐句）

| L350 句 | 实现位置 | 测试位置 |
|---|---|---|
| 唯一 retry owner=MAAS adapter、关闭 SDK 默认重试 | `__init__`：`AsyncOpenAI(..., max_retries=0)` 注释钉 §8 L350 | ——（构造器内联，无需测） |
| 未收到首个内容且明确可重试的 429/短暂网络错误最多 2 次重试 | `_RetrySignal` 仅 create() 阶段可抛（docstring 钉结构性不变量：已产 chunk 的 attempt 永不重启/拼接）；stream() 耗尽 `attempt > self._max_retries`（默认 2）→ProviderUnavailable retryable=True details.attempts | L703-714（三连 429→attempts=3+retryable=True+恰一 error）+L717-736（**部分流后第二 429 不被消费——len(server.requests)==1**） |
| 尊重 Retry-After | `retry_after_seconds`：retry-after-ms 优先→retry-after 秒；无效/负值 None | L1043-1049（ms 优先/ms+秒并存取 ms/无效 ms 回落秒/负秒 None/空表 None）+golden 05（L431-466） |
| **Phase 1 确定性 `min(retry_after, remaining)` 等待，无 Retry-After 按 0，不加抖动，受总超时限制**（OP-7 终稿=T-A-005；§10 L379 下游义务四验证点） | stream() 重试决策：`await asyncio.sleep(min(delay, remaining))`——**L408-412 注释钉 OP-7（T-A-005，2026-11-03）**；无头 delay=0.0；remaining≤0→total 超时不 sleep | **四点逐一对齐 §10 L379 义务原文**：①确定性延迟=golden 05 elapsed 0.19≤t<0.6（L465-466——下界容差+上界防 jittered/退避 overshoot）；②无头按 0=elapsed<0.5（L679）；③总 deadline=RA 5000ms+timeout_s=0.4→2 请求+elapsed<1.0+timeout_kind=total（L682-700）；④两次上限=attempts=3（L703-714） |
| 部分流已到达则结束并标 interrupted，不能把旧流和新流拼接 | `_RetrySignal` 结构性承载（部分流后唯一出路=STREAM_INTERRUPTED error chunk） | L717-736（1 请求断言+第二 429 不消费） |
| 用户要求重试生成时创建新 attempt / 既有 tool.finished 绝不重新执行 / MCP、shell、文件变更默认不自动重试 | 非 adapter 职责（runner/工具层；adapter 无 attempt>1 语义、无工具执行面）——正确留白 | ——（方案 §2 R2-S3 范围锚定，非本切片） |
| L348 details/异常链脱敏不暴露认证头或原始 SDK request | `_failure` 构造 CoreError：message 固定字符串、details 只含 timeout_kind/violation/status/attempts/cause（异常**类名**非敏感）；429 `from None` 抑制链 | golden 04/06/07 error 对象全字段断言（details 无原始请求/头内容） |

### A-PROVIDER 复验五项（r2-slices §6 L224）

1. **错误 chunk 正式形态落地** ✓——终止 ErrorChunk（§2.3 L98/L102；spike 差异 #3 正式承接；provider.md §11.2-1）。
2. **SDK 红线** ✓——`__init__.py` docstring（"openai/httpx2 生死均在本包内"）+maas.py 签名/返回/docstring 零 SDK 渗出+测试模块面+JSON 原语递归双断言（L890-911）。
3. **OP-7 终稿一致** ✓——四验证点逐一对齐 §10 L379 义务原文（上表）。
4. **OP-8 维持 R0** ✓——5xx→ProviderUnavailable retryable=True 不自动重试（create() APIStatusError 分支）；未测=③-3 如实浮报，终审倾向见 §5 ④-2。
5. **§8.1 注记** ✓——include_usage 透传（L1010 body 断言）+cache_write=null（`_map_usage` 注释+golden 显式钉）+SSE 逐帧基线（fake server chunked 编码沿用 provider.md §5 教训，冻结 tests/r0 复用+7 fixture byte MATCH）。

### TS 基准收窄边界核对（收窄如实）

- **http.ts L94-140 三级超时同构**：header=`Effect.timeoutOrElse(min(headerTimeout,total))` 包 request 执行（"Timed out waiting for response headers"）↔ maas.py `asyncio.timeout(header_s)` 包 create()（同义 message "timed out waiting for response headers"）；chunk=`Stream.timeoutOrElse(chunkTimeout)`（"Timed out waiting for response data"）↔ httpx2 read 超时→APITimeoutError→同义 message；total=`interruptWhen(sleep(remaining))` ↔ 读前 remaining 检查+`asyncio.timeout(remaining)` 包单读。粒度差异（TS 事件粒度/Python socket 读粒度）已记 provider.md §3，**不做"等价"声明**——如实。
- **executor.ts L22-35 Retry-After**：ms 优先→秒→**HTTP-date（Date.parse）**；Python 不实现 date（§6-5 缺口维持，date 形态按无效→等待 0）。差异已记。
- **model-retry.ts L22-36/L62-103**：TS=Schedule.jittered+min(exponential,spaced) 上限 10 次+`max(delay, retryAfter)` 下界；Python=**min(retry_after, remaining) 无 jitter、≤2 次**——OP-7 终稿明确选择（R0 证据赢，T-A-005 记档）。notRun 集（L78-90）：TS 含 ECONNREFUSED/ENOTFOUND/EAI_AGAIN/RateLimit/**503/529**；Python 收窄为 429+ConnectError/ConnectTimeout（5xx 不重试）——OP-8 差异已记 provider.md §6-9。收窄方向一致（更保守），无越界扩张。
- **aisdk.ts L746-857 聚合**：TS `state.toolNames[id] ?? "unknown"` 回退（L796/L802）；Python `_ToolAccumulator` 缺 id/name **fails closed**（ValidationError）——Python 侧显式硬化（STEP-01-R0 L86 拒绝语义+pydantic 必填倒逼），已记 §11.2-4，非照抄、非静默差异。

## 4. ③ 强制浮报完整性 + 10 项未测防御逐项裁决

**漏报抽查**（B §8-3/R1-S8 教训）：通读 maas.py 全量 raise/except/分支（create() 六路异常映射/读循环四路/`_map_chunk` 拒绝两型+聚合/收尾校验两型/`_openai_messages` 丢弃面/`retry_after_seconds` 解析面）——③ 21 项覆盖良好，**无漏报**。两个浮报盲区均非"分支"：M-1 双计时器竞态（行为不确定性注记，③/④ 均未提及——本审查新增）+N-2 finally `stream.close()` 自身抛异常的防御性边缘。二者皆不构成 R1-S8 级漏报（非未测防御分支、非代码路径）。

**协议不可达复核（③-1/③-2）**：③-1 `_RetrySignal` 仅由 `_attempt` 的 create() 异常映射抛出（429 分支/ConnectError·ConnectTimeout 分支），全部位于任何 `yield` 之前——部分流后该异常结构性不可达，docstring 钉定成立 ✓。③-2 `_map_chunk` 末尾 `state.pending_finish = choice.finish_reason` 二次赋值：重复 finish 帧在函数顶部拒绝块（`state.pending_finish is not None` + `choice.finish_reason is not None` → duplicate_finish）先行失败整流，赋值仅首 finish 可达，行内注释钉定 ✓。

**已测防御标注抽查（③-13..③-18）**：⑥ 项标注全部准确（13=golden 06/07+tool call 变体 L469-572 ✓；14=缺 id 拒绝 L774-816 ✓；15=不可反解透传 L744-771 ✓；16=部分流不重试 L717-736 ✓；17=error 唯一终止 L575-598 ✓；18=取消+泄漏+早 aclose L824-868 ✓）。

### 10 项未测防御逐项裁决

| 项 | 内容 | 裁决 | 理由 |
|---|---|---|---|
| ③-3 | 5xx→ProviderUnavailable retryable=True 不自动重试（OP-8 保守读法）未测 | **同意暂缓，OP-8 终审一并** | OP-8 开放（方案 §4），现在补测若终审翻转会立即重写（双写浪费）；终审维持时**建议必补**一条 status=500 注入（fake server 支持，断言=恰一 error+retryable=True+1 请求不重试）。归 §6。 |
| ③-4 | 连接建立失败重试分支（cause 分流）未测 | **需补测（M-2）** | §8 L350 重试集第二明文成员；异常源分流边界（cause_name 检查→RetrySignal vs STREAM_INTERRUPTED）零覆盖。草案见 §2 M-2（未监听端口→ConnectError→耗尽形态同 L703-714）。优先级随 OP-8 终审排期，但**独立于 OP-8**（连接失败重试不在 OP-8 范围）。 |
| ③-5 | create() 阶段 APITimeoutError 分支（httpx2 超时源）未测 | 同意 | 与 asyncio.timeout→TimeoutError 分支产出**完全同构**（同 code/message/details=timeout_kind:header）；asyncio 分支已测（L606-620）且实测该路径稳定（hang_headers 走 asyncio/httpx2 read 皆归 header）；仅异常源不同无行为分叉。 |
| ③-6 | `tool_call_without_name`（start 带无 name）未测 | 同意（可选补=N-4） | 与 without_id 同族对称：同一收尾校验块相邻检查、同错误码、对称 violation 值；代表已测（L774-816）。 |
| ③-7 | tool args 无 start 帧→STREAM_INTERRUPTED 未测 | 同意 | 防御已实现；tool_call 分片路径已由 03 golden（交错+分片）/无 id 拒绝/终态后 tool call 拒绝三点覆盖结构族；本分支为同族极端序。 |
| ③-8 | `retry_after_seconds` 边角（负 ms 回落秒/无效秒/HTTP-date 按无效）未测 | 同意 | 单元已覆盖主路径五形（L1043-1049：ms 优先/ms+秒并存/无效 ms 回落/负秒 None/空表 None）；HTTP-date 有意不实现=§6-5 缺口（④-6 跟踪），按无效处理→等待 0 行为安全（不阻塞不越界）。 |
| ③-9 | 请求组装丢弃面（system/user 非 text parts/tool 角色非 tool_result parts/assistant 仅 reasoning→content None/空 content→""）未测 | 同意+S4 核对项 | 丢弃分支属非法输入防御——生产者 ContextBuilder（S4）契约上不产生混合 part/空消息；测试需构造"adapter 不该收到的输入"属负例面。**S4 审查须核**（④-4 联动）：S4 是否可能产生混合 part 消息，若可能→再议丢弃语义。 |
| ③-10 | `_map_finish_reason` length/filter/unknown 三映射未测 | 同意 | 纯查表四行 if；stop/tool_calls 已 golden 覆盖；length 已在真实 smoke §8.1 观测（finish=length）端到端经 `_map_finish_reason` 正常工作。可选补一条 length finish golden。 |
| ③-11 | stream() 重试决策处 remaining≤0（"total deadline exceeded before retry"）未测 | 同意 | 近重复防御：截断测试（L682-700）的 deadline 命中在读循环/重试等待，本分支（429 到达即耗尽）与已测路径产出同构（timeout_kind=total/retryable=False）；语义同源（§8 L350"受总超时限制"）。 |
| ③-12 | `MaasTimeouts` 默认值（10/30）仅构造器隐式行使 | 同意 | 纯构造默认，与 provider.md §3 映射表一致（spike 同值）；超时行为全部由注入值测试钉定（三级注入各自断言 kind）；默认值无数值语义风险。docstring 注记即可。 |

计数：**同意 8（③-3 暂缓含条件建议）/需补测 1（③-4=M-2，含草案）/升 A 0**；③-9 附 S4 审查核对义务、③-3 附 OP-8 终审联动义务。

## 5. ④ 7 项未决逐项意见

| 项 | 内容 | 意见 | 依据/代价 |
|---|---|---|---|
| ④-1 | OP-4：golden 与 spike 必然差异（错误 chunk/pydantic 全字段面/点分名反解） | 同意保守读法；**终审倾向：维持** | 三差异各有依据：错误 chunk=§2.3 L98/L102 正式承接（spike §6-3 自认"临时形态"）；全字段面=§1 仓库级 pydantic 规则；点分名=§6-2 反解兑现。翻转代价=golden 断言+§11 追记+请求侧 `tool_wire_name_from_dotted` 反向组装三处同步，高成本零收益。 |
| ④-2 | OP-8：5xx 不自动重试（维持 R0） | 同意保守读法；**终审倾向：维持** | 契约 §8 L350 字面"429/短暂网络错误"不含 5xx；真实 MAAS smoke（§8.1）无 5xx 对照证据；TS notRun 含 503/529（model-retry.ts L86）为已记 TS 面差异（provider.md §6-9）。若翻转：create() 5xx 分支改 `_RetrySignal(retry_after_seconds(...))`+补测（单处改动，但 5xx Retry-After 头不一定存在→None→等待 0 语义需一并定）。维持时须补 ③-3 测试。 |
| ④-3 | chunk `name` 点分约定（S1 契约 `LLMToolCallPart.name` 仅 min_length=1 未钉格式） | **建议升 A（契约面钉定）** | 风险：S4 ContextBuilder 若产生非点分名（如裸 `read`），`tool_wire_name_from_dotted` 按原样透传→wire 名无 namespace→S5 registry 按 (namespace,name) 查不到→**静默退化**（模型端看到无命名空间工具名）。契约面一句钉定（§2.2"tool_call part name 须为点分 `namespace.name`"或 §5.1 侧）是唯一权威且免 S4/S6 各自解读；**最低限度动作**=S4 任务书注记+S4 审查核对项。归 §6。 |
| ④-4 | tool 角色多 part 语义（混合 part 非 tool_result 静默丢弃） | 同意维持 | 丢弃已浮报（③-9）；S4 审查核对项（ContextBuilder 是否产生混合 part——若产生需再议丢弃语义）。 |
| ④-5 | 重复 tool call ID 跨响应查重归 runner | 同意归置 | §11.3 记录正确：adapter 单响应按 call_index 聚合；跨响应查重需 run 上下文（§2.1 L37 标识约束在 commit 时校验=S2 职责）+验收 C"重复 ID 使本次生成失败"=runner 语义。**S6 任务书须列该项**。 |
| ④-6 | HTTP-date Retry-After 缺口维持 | 同意维持 | §6-5 已记缺口；date 形态按无效→等待 0（安全：不阻塞不越界）；真实 smoke 未观测 date 形态（§8.1）；后端若发 date 需补 executor.ts L32-33 同款解析（届时独立微任务）。 |
| ④-7 | 协议签名 AsyncIterator vs 具体 AsyncGenerator | **同意现状（与重点 1 合并结论）** | §2.2 L84 契约原文"返回异步迭代器…实现可以是带 yield 的 async generator"——Protocol 面 `AsyncIterator` 忠实契约文本；具体面 `AsyncGenerator` 协变细化是 aclose 可达的诚实类型表达（mypy 通过）；aclose/取消在具体面钉定（L824-868）分工成立。若终审要求协议面表达关闭语义：改动面=provider.py 单文件（注解+docstring），结构匹配不破坏、无测试改动——但无必要（Protocol 结构匹配已允许实现细化）。 |

计数：同意 6 / 需改 0 / 升 A 1（④-3，附最低限度动作）。

## 6. 需调度器/A 裁决事项清单

1. **④-3（建议升 A）**：`LLMToolCallPart.name` 点分格式契约面钉定——避免 S4 产生非点分名→wire 名丢 namespace→S5 registry 静默查不到。A 裁选项：§2.2 加一句约定 vs 仅 S4 任务书注记。**最低限度**：S4 任务书注记+S4 审查核对项。
2. **OP-8 终审倾向：维持**（④-2，契约字面+无 smoke 对照+TS 差异已记）；若翻转=create() 5xx 分支单处改+补测。**维持时须补 ③-3 测试**（status=500 注入：恰一 error+retryable=True+1 请求）。
3. **M-2/③-4 补测排期**（独立于 OP-8）：连接建立失败重试分支——未监听端口草案（§2 M-2）；随 OP-8 终审一并排或先行。
4. **M-1 竞态注记**：maas.py 注释钉定 asyncio.timeout(header_s)×httpx2 connect(header_s) 双计时器竞态+接受理由（两路径皆合法失败面；spike 同构）——可与 N-2（finally close 异常掩盖注记）一次微改。
5. **S4 审查核对项**（④-3/④-4/③-9）：ContextBuilder 产生的 tool_call part name 点分一致性+不产生混合 part 消息；**S6 任务书须列 ④-5**（重复 tool call ID 跨响应查重=runner 职责，验收 C 语义）。
6. **S5 审查核对项（架构注记 21）**：ToolRegistry 是否钉定 namespace `__`-free 约束（`parse_tool_wire_name` 按首个 `__` 切分的假设）——若无→S5 补丁（RegistrationError 面加 namespace/name 字符校验）或升 A。
7. N-1（进度文件 +34L 误记，实 +26L）：调度器知悉即可，无需修复（进度文件为 C-1 产出快照）。



