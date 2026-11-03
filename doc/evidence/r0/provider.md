# R0-A Provider 证据

> spike 落点：`tools/r0/provider_spike.py`（类型与 adapter）、`tests/r0/fake_openai_server.py`（fake SSE server）、`tests/r0/test_provider_spike.py`（契约测试，24 用例）。
> 行为依据：TS 锚点 `aisdk.ts:65-169,746-857`、`http.ts:94-140`、`executor.ts:22-35`、`model-retry.ts:62-103`、`testing.ts:40-150`（指纹见 `doc/source-baseline.json`）。

## 1. 覆盖的 R0-A 必测集（pytest 24/24）

| 场景 | 用例 | 断言要点 |
|---|---|---|
| 文本 delta（usage 在 finish 之后） | `test_text_stream_usage_after_finish` | 逻辑 finish 压到流末，usage 保留 |
| usage 在 finish 之前 | `test_text_stream_usage_before_finish` | 两序均保留 |
| 双交错 tool call + 参数分片 | `test_interleaved_tool_calls_with_argument_shards` | `{pa`+`th": "/a"}` 聚合，`tool_call_end` 按 call_index 排序，finish=tool_calls |
| EOF 无终止 chunk | `test_eof_without_terminal_is_interrupted` | `ProviderStreamInterrupted`，1 次请求 |
| 429（首内容前）重试后成功 | `test_429_before_content_retries_then_succeeds` | Retry-After（retry-after-ms）被尊重，2 次请求 |
| 429 重试耗尽 | `test_429_before_content_retry_exhaustion` | attempts=3（1+2），`ProviderUnavailable` |
| 已产出内容后不重试 | `test_no_retry_after_first_content` | 内容后干净结束 → 中断错误，**1 次请求**（脚本中的第二个 429 未被消费，证明未重发） |
| header 超时 | `test_header_timeout` | `timeout_kind=header` |
| chunk/read 超时 | `test_chunk_timeout` | `timeout_kind=chunk`，首帧已交付 |
| total deadline | `test_total_deadline` | `timeout_kind=total`，部分内容后触发 |
| 取消 | `test_cancellation_propagates_and_cleans_up` | `CancelledError` 传播；adapter.close + server.aclose 后 `asyncio.all_tasks()` 无泄漏 |
| wire 重复 finish（adapter 级） | `test_wire_double_finish_is_rejected` | wire 发两个 finish 帧 → `CoreError(ValidationError, violation=duplicate_finish)`，无 finish 终态、1 次请求不重试——STEP-01-R0 L86 拒绝，不归一化 |
| wire 终态后文本（adapter 级） | `test_wire_text_after_finish_is_rejected` | finish 后文本 → `ValidationError(content_after_finish)`，仅保留 finish 前内容，held-back finish 被丢弃 |
| wire 终态后 tool call（adapter 级） | `test_wire_tool_call_after_finish_is_rejected` | finish 后 tool-call 帧 → 同上拒绝；无 tool_call_end、无 finish |
| 序列契约单元（输出校验器） | `test_validate_sequence_*`（7 拒绝 + 1 接受） | request_id / index 严格递增 / 终态唯一且最后 / 类型必填字段。注意：这是对手工构造序列的**输出侧校验**；"重复/乱序被拒绝"的 **wire 侧结构保证**由上面三个 adapter 级用例证明 |
| Retry-After 解析单元 | `test_retry_after_seconds_prefers_ms_header` | ms 优先于秒；非法/负值忽略 |
| SDK 类型不泄漏 | `test_sdk_types_do_not_leak` | 所有 chunk 字段的类型模块均非 `openai*`/`httpx*` |

## 2. 脱敏 JSON fixtures（STEP-01-R0 L98 要求）

- 位置：`doc/evidence/r0/fixtures/`，共 7 个 JSON；再生命令 `uv run python -m tools.r0.dump_fixtures`（显式工具，不在 pytest/CI 内）；
- 生成方式：与契约测试同一 fake server + 同一 `MaasSpikeAdapter` 驱动，每个 fixture 记录**完整终态**：wire SSE 帧原文、输出 chunk 全序列、usage、finish、终态错误、HTTP 请求次数；
- 文件清单：
  - `01_text_usage_after_finish.json` — usage 在 wire finish 之后仍保留（逻辑 finish 压到流末，输出顺序恒为 usage → finish）；
  - `02_text_usage_before_finish.json` — usage 在 finish 之前，两序均保留；
  - `03_interleaved_tool_calls.json` — 双交错 tool call + 参数分片聚合，`tool_call_end` 携带完整 `raw_arguments`，usage 在 tool_call_end 之前保留；
  - `04_eof_without_terminal.json` — 干净结束 body 但无 `[DONE]` 行/finish 帧 → `ProviderStreamInterrupted`；
  - `05_429_retry_then_success.json` — 首内容前 429（`retry-after-ms` 被尊重）重试成功，`http_request_count=2`；
  - `06_wire_double_finish.json` — wire 双 finish 帧 → 协议错误拒绝（`ValidationError`，violation=duplicate_finish），无 finish 终态；
  - `07_wire_content_after_finish.json` — wire 终态后文本 → 协议错误拒绝（violation=content_after_finish），仅保留 finish 前内容；
- 脱敏保证：全部为合成 fake-server 数据，无真实 endpoint、凭据、prompt 或模型输出进入文件；adapter 在 spike 中不产出 provider metadata（恒为空字典），fixture 不含该字段，满足"provider metadata 经过有界化和脱敏后才进入 fixture"（STEP-01-R0 L93）；
- R2 重建 `src/opencode/ai/` 时必须重跑本目录全部 fixture（见 §7）。

## 3. 超时模型映射（TS 三级 → Python，实测）

| TS 级别 | Python 实现 | 位置 | 差异 |
|---|---|---|---|
| header（等响应头计时器） | `asyncio.timeout(header_s)` 包住整个 `create()` 阶段；**该阶段的任何失败——连接失败/连接超时/响应头未到达时的读超时——统一归 `timeout_kind=header`** | `_attempt` | 另以 `httpx2.Timeout(connect=header_s)` 兜底连接阶段，同样映射 header |
| chunk（帧间空闲 deadline） | SDK client 的 `httpx2.Timeout(read=chunk_s)`；**只覆盖已建立的流**（`create()` 返回之后） | 构造 `AsyncOpenAI` | TS 是事件粒度 deadline；Python 是 socket 读粒度。create() 阶段（含首字节/响应头等待）**不归 chunk**——实测 header_s=2.0、chunk_s=0.2、`hang_headers=True` → `timeout_kind=header`，与分类一致 |
| total（整请求 deadline） | 每次 `__anext__` 前 `deadline - now` 检查 + `asyncio.timeout(remaining)` 包住单次读；deadline 由 `request.timeout_s` 推导 | `_attempt` 读循环 | total 同时约束 429 重试间隔（`min(retry_after, remaining)`），与 TS "total 界定重试" 一致 |

映射结论：**不做"等价"声明**——header 是 asyncio 包装 create() 全阶段（含首字节等待），chunk 是 socket read 超时但仅覆盖已建立流，total 是 adapter 层显式 deadline。以上即实际实现，非硬凑三级同构。

## 4. 重试规则（实测）

- SDK `max_retries=0`（重试策略在 adapter，不在 SDK）；
- 仅在**尚未产出任何内容**且**明确可重试**时重试：429（尊重 Retry-After，ms 头优先）+ 连接建立阶段失败（`ConnectError`/`ConnectTimeout`）；≤2 次重试；
- **读取阶段**连接断裂（请求可能已被服务端处理）→ `ProviderStreamInterrupted`，不重试；
- 已产出部分内容后的任何失败 → 不重试、不拼接（测试用"内容后脚本 429 未被消费"证明）；
- 5xx → `CoreError(ProviderUnavailable, retryable=True)` 直接上抛，adapter 不自动重试（交由上层决策，TS model-retry 的"provider 明确没跑"条件未满足）；
- 无 jitter：重试 sleep 是精确 `min(retry_after, remaining)`（TS 有 jittered 调度，已记差异）。

## 5. 平台级发现（本轮最重要证据）：httpx2 不能可靠处理无长度定界的响应体

- 现象：fake server 最初把 SSE 响应写成"无 Content-Length、靠连接关闭定界"的 body。全量测试约 30-60% 偶发 `ReadError(BrokenResourceError)`（anyio），失败用例随机；
- 隔离复现（200 轮/模式）：裸 asyncio 客户端 **0/200** 失败；httpx2 裸客户端对**无长度 body 122/200 失败**，对 `Content-Length` body 与 `Transfer-Encoding: chunked` body 均 **0/200** 失败；
- 结论：问题不在 fake server 的 socket/HTTP 写法，而是 httpx2（openai 3.42 的 HTTP 栈）对 close-delimited body 的读取存在竞态；
- 修复：fake server 的 SSE 响应改用 **`Transfer-Encoding: chunked`**（每帧一个 HTTP chunk，body 以 `0\r\n\r\n` 终止，`data: [DONE]` 仍作为 SSE 终止行发送）——与真实 OpenAI SSE 响应的帧法一致。修复后 200 轮复现 0 失败，pytest 连续 10 次 23/23；
- 附带发现：对 chunked body 做**硬截断**（不发终止 chunk）时，httpx2 可能在抛协议错误前不交付已解码帧（部分内容是否可见不确定）。因此"EOF 无终止"fixture 改用 `omit_done`（干净结束 body 但不发 `[DONE]` 行），确定性地覆盖 adapter 的 `StopAsyncIteration → ProviderStreamInterrupted` 路径；硬截断语义作为已知差异记录，不作为 R0-A 断言。

## 6. 其他已知差异（不硬凑等价，逐条记录）

1. **text part id**：OpenAI wire 无文本 part id，adapter 合成 `"text-0"`（单文本 part 假设）；
2. **wire 工具名**：OpenAI 函数名正则不允许 `.`，`namespace__name` 可逆映射；chunk 的 `name` 字段携带 wire 名，反解到 `namespace.name` 留给 R2；
3. **错误交付**：R0 transport-error spike 差异——错误以 `CoreError` 异常上抛，不产生 `error` 类型 ProviderChunk；这是 spike 的临时形态（非"契约允许"的长期形态），error shard 的正式交付形态由 R2 按 CORE-CONTRACTS 承接；
4. **类型实现**：`ProviderChunk` 等为 dataclass；正式版按 CORE-CONTRACTS 用 pydantic（R2）；
5. **Retry-After**：只解析 `retry-after-ms`（优先）与秒数；HTTP-date 未实现；
6. **reasoning**：OpenAI 兼容 wire 无 reasoning 字段，`reasoning_delta` 永不产生（usage 的 `completion_tokens_details.reasoning_tokens` 已映射）；
7. **usage 顺序**：usage 在 wire 上位于 finish 前或后都能保留——逻辑 finish 被压到流末，顺序恒为 usage → tool_call_end(s) → finish；finish 之后仅 usage 帧合法（STEP-01-R0 L88），其余内容帧触发协议错误（见第 11 条）；
8. **base_url 挂载**：OpenAI SDK 在 base_url 后拼 `/chat/completions`，故 base_url 必须含 `/v1` 段（fake server 只服务 `/v1/chat/completions`，并校验路径与 `stream=true`，否则 404/400）；
9. **5xx 不自动重试**（见 §4），与 TS 行为有差（TS 对"明确没跑"的请求重试），留给 R2 对照真实后端再定；
10. **直接依赖声明**：`provider_spike.py` 直接 `import httpx2`，故 pyproject 声明 `httpx2>=2.12.0,<3`（与 openai 3.42 声明的 `httpx2<3,>=2.12.0` 范围一致），不依赖传递关系泄漏；
11. **wire 协议违规拒绝**（STEP-01-R0 L86）：重复 finish 帧、finish 之后的文本/tool-call 内容帧 → `CoreError(ValidationError, retryable=False, details.violation=duplicate_finish|content_after_finish)`，整流失败、held-back finish 被丢弃、绝不重试（协议违规不在 429/connect 可重试集合内）。错误码选用 spike 错误子集中的 `ValidationError`（在 CORE-CONTRACTS §8 错误码表内，语义为"provider 流违反协议"；未新增契约外错误码），R2 重建时可重新审视是否需要更专用的错误码。

## 7. R2 承接清单（fixture 待补，不计入 R0-A 通过条件）

以下场景属 **R2 runner 验收**（需要"解析出的调用计划交给谁"的语义），R0-A 只记录待承接：

- 错误分片（error shard）；
- 重复 tool call ID；
- 不完整 JSON 参数；
- 未知工具名；
- 合法 JSON 但参数不符合 schema。

R2 重建 `src/opencode/ai/` 时：**必须重跑** `tests/r0/` 全部用例与 `doc/evidence/r0/fixtures/` 全部 fixture（不搬移 spike 代码，但证据要延续）。

## 8. 真实 MAAS smoke 状态

- **状态：已补齐（2026-11-03，调度器亲跑，R2 前置收口）**——凭据经本地 opencode 运行配置（OpenAI 兼容网关）程序化注入进程环境变量三要素，key 不出现在命令行文本、不回显、不入日志或 CI（STEP-01-R0 L171 合规）；endpoint/model 真实值按本节脱敏规则不落库，下文以"实测模型"指代；
- smoke 入口：`uv run python tools/r0/provider_spike.py`（显式命令触发，不在 pytest/CI 内）；
- 与 fake 走同一 `MaasSpikeAdapter`；合成 prompt + 只读无副作用工具声明；只解析文本与调用计划，不执行任何工具；
- 输出仅含摘要：chunk 计数、按类型计数、文本/参数的长度与 SHA-256 前 12 位、usage 数值、finish reason——不落任何原文；`endpoint` 与 `model` 字段打印为 `<REDACTED_ENDPOINT>` / `<REDACTED_MODEL>`，不回显真实值。

### 8.1 实测结果（2026-11-03，实测模型值脱敏）

- 退出码 0；两场景 `error=null`——满足 STEP-01-R0 L104-106 验收点（一次文本流+一次双交错 tool call 流+只解析计划不执行工具）。
- **文本流**：chunk_count=2（usage×1+finish×1，无 `text_delta`）；finish reason=`length`；usage：input=19 / output=64 / reasoning=61 / cache_read=0。
  - **行为注记（reasoning 计入 output 预算）**：`max_output_tokens=64` 被 reasoning 全额消耗（reasoning=61/output=64），流无正文、finish=`length`——R2 ContextBuilder/runner 的 token 预算语义必须把 reasoning 纳入设计注记（短预算+reasoning 模型可能零正文截断）。
- **工具流**：chunk_count=36（`text_delta`×10 / `tool_call_start`×2 / `tool_call_args_delta`×20 / `usage`×1 / `tool_call_end`×2 / `finish`×1）；finish reason=`tool_calls`；tool_plan=2 个 `fs__read`（call_index 0/1，tool_call_id 唯一，arguments len=20、SHA-256 前 12 位各异）——单回复双交错 tool call 成立。
- **端点能力注记**：`stream_options.include_usage` 透传成功（usage 帧到达）；reasoning_tokens 经 `completion_tokens_details.reasoning_tokens` 传递、`cache_write_tokens` 无对应字段为 null（与 §2 已记录字段映射一致）；SSE 无缓冲失真（36 chunk 逐帧到达，本地代理链路透传）。
- 凭据纪律声明：本文件与仓库不含任何真实 endpoint/model/key 值；凭据保留在用户本地配置；复跑同样以环境变量注入，不落库。

## 9. CI 验证状态

- 工作流：`.github/workflows/ci.yml`——单 job matrix（`ubuntu-latest` + `windows-latest`），`astral-sh/setup-uv@v5`，Python 3.12；步骤 = `uv lock --check` → `uv sync --locked` → `uv run pytest -q tests/r0` → `uv run ruff check .` → `uv run ruff format --check .` → `uv run mypy src tools/r0 tests/r0`；无 secrets，不访问真实端点；
- **状态：已验证**——两次代码状态均有双 OS 成功记录：
  - commit `edcf891`（R0-A 首次落地，23 用例）→ run [41177228445](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41177228445)：[ubuntu](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41177228445/job/124467183985) / [windows](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41177228445/job/124467184184) 均 success；
  - commit `f0a6618`（L86 拒绝语义修复，24 用例）→ run [41178671282](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41178671282)：[ubuntu](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41178671282/job/124471396042) 日志行 `24 passed in 2.39s`、[windows](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41178671282/job/124471395926) 日志行 `24 passed in 2.99s`，lint/format/mypy 均绿；
- 按 STEP-01-R0 L142，Windows 与 Linux 各至少一次 Actions 成功记录已满足（同一 run 的 matrix 两个 job）。

## 10. 验证命令记录

```
uv lock --check                    # 通过（29 packages resolved）
uv sync --locked                   # 通过
uv run pytest -q tests/r0         # 24 passed（连续 10 次）
uv run ruff check .                # All checks passed
uv run ruff format --check .       # 18 files already formatted
uv run mypy src tools/r0 tests/r0  # Success: no issues in 7 source files
```

## 11. R2-S3 正式化差异与证据延续（OP-4 追记节，2026-11-03）

R2 重建 `src/opencode/ai/`（T-C1-015：provider.py Provider 协议 + maas.py adapter）时，按 OP-4 保守读法，上方 R0 原始证据**一字不改**；本节仅追记新 golden 行为差异与证据延续结果。

### 11.1 证据延续（硬条款，§7 L94）

- `tests/r0/` 重跑：**32 passed**（spike 及其测试零触碰——不搬移不修改）；
- 7 个 fixture 经冻结 `tools/r0/dump_fixtures.py` 再生成（输出到仓库外临时目录，不覆盖冻结文件），与 `doc/evidence/r0/fixtures/` 逐字节比对：**7/7 MATCH（ALL-BYTES-MATCH）**——无环境漂移，R0 证据仍可复现。

### 11.2 OP-4 差异清单（spike → 正式 adapter，新 golden 钉行为）

1. **错误交付（§6-3 临时形态 → 正式承接）**：spike 终端错误以 `CoreError` 异常上抛；正式 adapter 以**终止 ErrorChunk** 交付（CORE-CONTRACTS §2.3 L98/L102），一次请求最多一个终止 chunk（finish 或 error，error 恒为末位），仅 `CancelledError` 以异常传播。场景 04 → `ProviderStreamInterrupted`，场景 06/07 → `ValidationError`（details.violation=`duplicate_finish`|`content_after_finish`）；错误码与 violation 值沿用 spike，message 措辞为新定（golden 内嵌记录）；已交付的部分 chunk 不重组、不重试。
2. **类型实现（§6-4 兑现）**：dataclass → S1 pydantic contracts（`ProviderChunk` 8 型 union）。golden 以 `model_dump(mode="json")` 钉全字段面（`provider_metadata={}`、`cost_usd=null`、`Usage.cache_write=null` 等显式呈现）。
3. **工具名可逆反解（§6-2"留给 R2"兑现）**：spike chunk `name` 携带 wire 名（`fs__read`）；正式 adapter chunk `name` 为反解后的点分名（`fs.read`，按首个 `__` 切分），wire 名仍 `namespace__name`；请求侧（assistant 回放）反向由点分名组装 wire 名。无 `__` 的名字原样透传（S5 注册面关注）。
4. **新增防御路径**：tool call end 缺 id/name（分片流缺 start 帧等）在 spike dataclass 形状下不可表达；正式 adapter 产生 `ValidationError`（details.violation=`tool_call_without_id`|`tool_call_without_name`）error chunk。R0 fixture 无此场景——新 golden 新增断言。
5. **请求消息组装**：spike `LLMMessage(role, text)` 简化 → S1 `content` parts 组装：system/user 仅拼接 text parts；tool 角色每个 tool_result part 发一条 OpenAI tool 消息；assistant 回放丢弃 reasoning parts（§6-6 无 wire 槽）；文件类 tool 内容以 `"[attachment not loaded]"` 占位（CORE-CONTRACTS §2.1 L72）。
6. **重试语义（OP-7 终稿，§8 L350）**：仅 429+连接建立失败、且未产出内容，≤2 次；确定性等待 `min(retry_after, remaining)`（retry-after-ms 优先于秒数、无头按 0、无抖动）、受总 deadline 截断；部分流后不重试（重试点唯一）。
7. **不变项确认**：`stream_options.include_usage` 透传、`cache_write` 无对应字段=null、usage 压末/finish 逻辑压流末、text part 合成 `text-0`、HTTP-date Retry-After 仍未实现（§6-5 维持）——与 §2/§6/§8.1 一致。

新 golden 位置：`tests/contract/test_ai_adapter.py`（断言内嵌，未新增 `doc/evidence/` 文件；场景 01-07 成功侧序列一致，错误侧差异如第 1 条）。R0 fixture 文件零迁移零修改。

### 11.3 §7 R2 承接清单前两项

- **错误分片**：本切片落地（终止 ErrorChunk 交付，见 11.2-1）；
- **重复 tool call ID**：adapter 单响应内按 call_index 聚合、不做跨响应 ID 查重——属 adapter/runner 边界，本切片不裁决，留给 R2 runner 切片承接（方案 §2 R2-S3）。
