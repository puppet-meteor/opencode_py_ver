# R0-A Provider 证据

> spike 落点：`tools/r0/provider_spike.py`（类型与 adapter）、`tests/r0/fake_openai_server.py`（fake SSE server）、`tests/r0/test_provider_spike.py`（契约测试，23 用例）。
> 行为依据：TS 锚点 `aisdk.ts:65-169,746-857`、`http.ts:94-140`、`executor.ts:22-35`、`model-retry.ts:62-103`、`testing.ts:40-150`（指纹见 `doc/source-baseline.json`）。

## 1. 覆盖的 R0-A 必测集（pytest 23/23）

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
| wire 重复 finish（adapter 级） | `test_wire_double_finish_yields_single_terminal` | wire 发两个 finish 帧仍只产出 **1 个终态**，最后 raw_reason 胜出——结构性保证，非仅输出校验 |
| wire 终态后乱序内容（adapter 级） | `test_wire_content_after_finish_is_normalized` | 逻辑 finish 压到流末，wire 终态后内容不产生"终态后 chunk"违规，输出仍为 [text, text, finish] |
| 序列契约单元（输出校验器） | `test_validate_sequence_*`（7 拒绝 + 1 接受） | request_id / index 严格递增 / 终态唯一且最后 / 类型必填字段。注意：这是对手工构造序列的**输出侧校验**；"重复/乱序被拒绝"的 **wire 侧结构保证**由上面两个 adapter 级用例证明 |
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
  - `06_wire_double_finish.json` — wire 双 finish 帧归一化为单终态（最后 raw_reason 胜出）；
  - `07_wire_content_after_finish.json` — wire 终态后内容被归一化，输出仍合法；
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
7. **usage 顺序**：usage 在 wire 上位于 finish 前或后都能保留——逻辑 finish 被压到流末，顺序恒为 usage → tool_call_end(s) → finish；
8. **base_url 挂载**：OpenAI SDK 在 base_url 后拼 `/chat/completions`，故 base_url 必须含 `/v1` 段（fake server 只服务 `/v1/chat/completions`，并校验路径与 `stream=true`，否则 404/400）；
9. **5xx 不自动重试**（见 §4），与 TS 行为有差（TS 对"明确没跑"的请求重试），留给 R2 对照真实后端再定；
10. **直接依赖声明**：`provider_spike.py` 直接 `import httpx2`，故 pyproject 声明 `httpx2>=2.12.0,<3`（与 openai 3.42 声明的 `httpx2<3,>=2.12.0` 范围一致），不依赖传递关系泄漏。

## 7. R2 承接清单（fixture 待补，不计入 R0-A 通过条件）

以下场景属 **R2 runner 验收**（需要"解析出的调用计划交给谁"的语义），R0-A 只记录待承接：

- 错误分片（error shard）；
- 重复 tool call ID；
- 不完整 JSON 参数；
- 未知工具名；
- 合法 JSON 但参数不符合 schema。

R2 重建 `src/opencode/ai/` 时：**必须重跑** `tests/r0/` 全部用例与 `doc/evidence/r0/fixtures/` 全部 fixture（不搬移 spike 代码，但证据要延续）。

## 8. 真实 MAAS smoke 状态

- **状态：R0-A 外部证据待补**（本地无 `OPENCODE_MAAS_BASE_URL` / `OPENCODE_MAAS_API_KEY` / `OPENCODE_MAAS_MODEL` 凭据，不得伪造）；
- smoke 入口：`uv run python tools/r0/provider_spike.py`（显式命令触发，不在 pytest/CI 内）；
- 与 fake 走同一 `MaasSpikeAdapter`；合成 prompt + 只读无副作用工具声明；只解析文本与调用计划，不执行任何工具；
- 输出仅含摘要：chunk 计数、按类型计数、文本/参数的长度与 SHA-256 前 12 位、usage 数值、finish reason——不落任何原文；`endpoint` 与 `model` 字段打印为 `<REDACTED_ENDPOINT>` / `<REDACTED_MODEL>`，不回显真实值。

## 9. CI 验证状态

- 工作流：`.github/workflows/ci.yml`——单 job matrix（`ubuntu-latest` + `windows-latest`），`astral-sh/setup-uv@v5`，Python 3.12；步骤 = `uv lock --check` → `uv sync --locked` → `uv run pytest -q tests/r0` → `uv run ruff check .` → `uv run ruff format --check .` → `uv run mypy src tools/r0 tests/r0`；无 secrets，不访问真实端点；
- **状态：已验证**——commit `edcf891` 触发 Actions run [41177228445](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41177228445)（2026-10-31），两个 OS 均成功：
  - [test (ubuntu-latest)](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41177228445/job/124467183985)：success——日志行 `23 passed in 2.87s`、`All checks passed!`、`18 files already formatted`、`Success: no issues found in 7 source files`；
  - [test (windows-latest)](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41177228445/job/124467184184)：success——日志行 `23 passed in 3.73s`、`All checks passed!`、`Success: no issues found in 7 source files`；
- 按 STEP-01-R0 L142，Windows 与 Linux 各至少一次 Actions 成功记录已满足（同一 run 的 matrix 两个 job）。

## 10. 验证命令记录

```
uv lock --check                    # 通过（29 packages resolved）
uv sync --locked                   # 通过
uv run pytest -q tests/r0         # 23 passed（连续 10 次）
uv run ruff check .                # All checks passed
uv run ruff format --check .       # 18 files already formatted
uv run mypy src tools/r0 tests/r0  # Success: no issues in 7 source files
```
