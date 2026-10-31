# Step 01：R0 风险打样执行卡片

> 状态：待执行。本文只细化 [PHASE1-SLIM.md](PHASE1-SLIM.md) 的 R0，不改变核心契约。
> 唯一事实来源：[CORE-CONTRACTS.md](CORE-CONTRACTS.md)。路线总览：[ROADMAP.md](ROADMAP.md)。

## 目标

在实现 SessionService、事件存储、工具注册中心或完整 CLI 之前，验证 Phase 1 的四个高风险假设：

1. 目标 MAAS 端点能被一个薄的异步 provider adapter 正确消费；
2. fake OpenAI-compatible server 能无凭据复现正常流和故障流；
3. Windows/Linux 的解释器选择、进程树回收、路径边界和 SQLite 单 writer 约束可被测试证明；
4. Python 3.12、依赖锁、测试、类型检查、格式检查和双平台 CI 能建立最小质量门。

R0 交付的是证据和风险决策，不是可用 agent。R0 不实现 runner、SessionService、SessionStore、Alembic 0001、ToolRegistry、权限策略、MCP、REPL 或任何临时内存旁路。

## 先读和必须遵守的规则

- 先读 [ROADMAP.md](ROADMAP.md)、[PHASE1-SLIM.md](PHASE1-SLIM.md)、[CORE-CONTRACTS.md](CORE-CONTRACTS.md) 和 [ADR-0001](adr/0001-core-architecture.md)。
- Python 事件使用 `py.*` 语义，不能把 TS wire schema 或 TS SQLite schema 直接复制进来。
- Provider 只暴露 `Provider.stream(request) -> AsyncIterator[ProviderChunk]`；SDK/OpenAI 类型只能停留在 adapter 内。
- provider chunk 必须按 `request_id`、递增 `index` 和 `type` 校验；完整 tool-call 参数只能在 `tool_call_end` 后解析。
- 收到成功 finish 并持久化完整调用计划之前，不得执行任何工具。R0 只验证“解析和计划”，不执行真实工具。
- token delta、reasoning delta、tool progress 是 ephemeral 观察数据；测试 fixture 必须录到完整终态（finish/usage/完整 tool-call 参数），达到将来 R1 恢复测试可直接复用的标准。
- 凭据只能来自环境变量或本地未提交配置；日志、fixture、CI 输出和文档不得出现 API key、Authorization header 或 URL userinfo。
- 发现契约必须改变时，停止当前步骤，先修改 ADR/契约并说明原因；不得在 spike 中偷偷放宽约束。

## 执行顺序

### 1. 环境和仓库基线

检查并记录：

- Python 版本至少 3.12；Windows 和 Linux 的目标解释器版本；
- `git status`、当前分支和工作目录；不得覆盖已有文档修改；
- 可用的依赖管理器。优先使用 `uv`，生成 `pyproject.toml` 和 `uv.lock`；若环境不具备 `uv`，先记录阻塞原因，不生成未锁定依赖；
- 目标 MAAS endpoint、model、凭据是否可用。凭据缺失只能标记真实端点验收为“外部证据待补”，不能用 fake 结果冒充。

产物：环境记录写入 `doc/evidence/r0/environment.md`，不得写入秘密。

### 2. 最小项目和 CI 骨架

只创建 R0 所需的最小结构：

```text
pyproject.toml
uv.lock
src/opencode/__init__.py
tests/r0/
tools/r0/
.github/workflows/ci.yml
doc/evidence/r0/
```

配置要求：

- Python 3.12+；
- pytest、pytest-asyncio、ruff、mypy；HTTP client/server 依赖使用当前 spike 实际需要的最小集合；
- `ruff check`、`ruff format --check`、`mypy src`、`pytest` 都有明确命令；
- CI 至少包含 Windows 和 Linux，CI 默认不访问真实 MAAS，不要求任何凭据；
- CI 保持单 job 矩阵（pytest 与 lint 合并，不拆多 job）；若仓库为私有，Windows runner 按 2 倍分钟数计费，避免多 job 放大成本；
- 不预建 `contracts/`、`storage/`、`session/` 等未来模块的空实现。

### 3. fake OpenAI-compatible server 和 provider spike

实现一个仅供测试的可控 fake server，并在 adapter 层完成最小请求/流解析：

- 正常场景：文本 delta、两个交错 tool call、参数分片、usage、finish；
- 故障场景：错误分片、重复 tool ID、不完整 JSON、EOF、429、超时、未知工具名和合法 JSON 但参数不符合 schema；
- 超支优先级：交错 tool call、EOF、finish/usage 顺序、429 是 R0 必测；错误分片、重复 tool ID、不完整 JSON、未知工具名、参数不符 schema 可延后到 R2 的 runner 测试中补齐（它们本来就是 runner 契约的验收项）；
- 每个响应带可断言的 `request_id` 和递增 `index`；
- 交错 tool call 按 `call_index` 聚合，不能按到达顺序错误拼接；
- usage 在 finish 前后两种 provider 变体都要能保留；逻辑 finish 不能吞掉 usage；
- 取消能关闭异步流，超时不会留下后台 task；
- adapter 输出项目自己的 `ProviderChunk`，测试不能把 SDK 对象泄漏到上层；
- provider metadata 经过有界化和脱敏后才进入 fixture；
- spike 只产生解析后的文本和完整调用计划，不能调用 shell、文件或 MCP。

建议文件：`tests/r0/fake_openai_server.py`、`tests/r0/test_provider_spike.py`、`tools/r0/provider_spike.py`。如果实际依赖导致文件名不同，必须在证据文档中记录映射，不得扩展到未来模块。

R0 的 ProviderChunk 类型和 adapter 是 spike 版，落点在 `tests/r0/`；R2 进入 `src/opencode/ai/` 时按 [CORE-CONTRACTS](CORE-CONTRACTS.md) 重建正式版，spike 版不直接搬移。

### 4. 真实 MAAS smoke

使用与 fake server 相同的 adapter 契约，执行一次最小真实端点 smoke：

- 一次文本流；
- 一次包含两个交错 tool call 的流；
- 记录 usage、finish reason、模型上下文上限和端点特殊行为；
- 保存脱敏后的 request/response fixture 和差异说明；
- 只在显式 smoke 命令中访问网络，默认 pytest/CI 不访问真实端点。

如果没有真实凭据，必须输出“R0-A 外部证据待补”，保留 fake 和平台验收结果，并明确 Phase 1 发布不能因此标记完成。

### 5. 原生平台探针

编写独立探针，不把平台代码塞进 provider 或未来工具模块：

- 解释器选择：Unix 明确 bash，Windows 明确 PowerShell；不依赖平台默认 shell；
- 进程树：父进程启动子进程和孙进程，取消/超时后验证整个进程树退出；
- 路径：验证 workspace root 内路径、traversal、symlink；Windows 额外验证 junction；无法可靠判断时返回拒绝；
- Windows 上创建 symlink 可能因 `SeCreateSymbolicLinkPrivilege` 被拒（含 GitHub Actions Windows runner）；若 CI 中创建失败，须在本地 Windows 补跑并将 CI 结果标注“受限”，不得直接判失败或静默跳过；
- SQLite：启用 WAL、`foreign_keys`、`busy_timeout`，两个 writer 竞争同一数据目录时第二个明确失败；
- 输出 JSON 证据，不记录用户名、绝对秘密路径或环境凭据。

建议文件：`tools/r0/platform_probe.py`、`tests/r0/test_platform_probe.py`。平台探针必须能在不启动完整 agent 的情况下运行。

### 6. 证据整理和 R0 闸门

把结果写入：

- `doc/evidence/r0/environment.md`：版本、依赖、命令和环境限制；
- `doc/evidence/r0/provider.md`：fake/真实端点行为、脱敏 fixture 位置和差异；
- `doc/evidence/r0/platform.json`：Windows/Linux 探针结果；
- `doc/platform-support.md`：已验证平台、未验证平台、明确不支持的能力；
- `doc/parity.md`：仅更新 R0 已实测的 MAAS 差异，不把设计预期写成 verified。

R0 结束时给出三种明确结论之一：

- **通过**：A/B/C 全部有证据，可以开始 R1；
- **有条件通过**：fake 和平台通过，真实 MAAS 外部证据待补；允许继续不依赖真实端点的 R1，但不能宣称 Phase 1 发布通过；
- **失败并收缩范围**：平台能力、流协议或依赖锁无法证明；先更新 parity/平台支持表和路线，再决定是否继续。

## 验收标准

- [ ] `python --version` 为 3.12+；依赖锁可在干净环境重建。
- [ ] fake server 在无网络、无凭据环境下覆盖正常流、两个交错 tool call、usage/finish、EOF、429、超时、非法 JSON、重复 ID 和参数错误。
- [ ] adapter 仅输出项目自己的异步 chunk 类型；工具计划在完整 finish 之前不会被执行。
- [ ] 真实 MAAS smoke 有脱敏证据，或明确记录为外部证据待补；不得用 fake 结果替代。
- [ ] Windows/Linux 平台探针覆盖解释器、进程树、symlink/junction、SQLite WAL/文件锁；第二 writer 明确失败。
- [ ] 双平台 CI 运行 pytest、ruff、format check、mypy，CI 不需要秘密。
- [ ] 所有证据、限制和结论已写入文档；没有未提交秘密、占位项或未解释的失败。
- [ ] R0 没有实现或旁路 SessionService、事件持久化、工具执行和权限系统。

## 标准验证命令

```powershell
uv lock --check
uv run pytest -q tests/r0
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run python tools/r0/platform_probe.py --output doc/evidence/r0/platform.json
```

真实 smoke 必须单独运行，命令和环境变量名记录在 `doc/evidence/r0/provider.md`；不得将真实凭据写入命令历史、日志或 CI。

## 停止条件

- 真实端点行为无法解释时，不修改核心契约去迎合它；把差异隔离在 MAAS adapter 并记录。
- 进程树或路径边界在目标平台无法可靠验证时，不写“支持”；收缩平台能力并停止进入下一切片。
- 第二 writer 没有明确失败时，不开始 R1 的并发/恢复实现。
- 依赖无法锁定、CI 无法在双平台运行或测试需要秘密时，先修 R0 基础设施。
- 任何人要求在 R0 中实现完整 agent、MCP、权限、事件存储或临时 JSON/内存旁路时，拒绝该范围变更并回到本卡片。

## 交付格式

完成后报告：变更文件、执行命令、每项验收结果、证据路径、真实 MAAS 是否可用、平台支持结论、遗留风险和 R0 结论。没有证据的项目写“未验证”，不能写“通过”。
