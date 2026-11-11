# MAAS 真实端点短会话证据（T-C1-048，ROADMAP L62 core 0.1 稳定门）

> 形态 Option B（2026-11-11 用户批准）：零产品改动，桥接只落测试面（产品级配置桥 = ConfigLoader 第五层，本票完成后另票）。
> 模板由 C 席交付；真实数值由调度器于 2026-11-11 验收时显式真端点跑回填（本文件无占位符残留）。

## 1. 目的

为 Python 版补真实 MAAS 短会话的证据回路：经本机 opencode 现用网关配置（OpenAI 兼容）跑两个场景（文本回合 / 工具真执行回路），以结构化断言 + 本文件的摘要数值，构成 core 0.1 稳定门的真实运行证据。测试面：`tests/integration/test_maas_real_session.py`（恰 2 场景，`maas_real` marker，默认恒 skip、CI 零影响）；配置桥：`tests/integration/_maas_real_config.py`。

## 2. 配置来源（脱敏）

- 解析序（`_maas_real_config.py`）：env 三要素（`OPENCODE_PY_MAAS_BASE_URL` / `OPENCODE_PY_MAAS_API_KEY` / `OPENCODE_PY_MAAS_MODEL`）任一存在即进入 env 模式且三者须齐（缺任一 = 不可用，不与文件源混掺）；否则文件模式：配置目录 = `$XDG_CONFIG_HOME/opencode`（设了的话）否则 `~/.config/opencode`，`opencode.json` 与 `opencode.jsonc` 双读合并（jsonc 剥 `//` 注释与尾逗号后解析；jsonc 键浅覆盖 json）；provider 块 = `OPENCODE_PY_MAAS_PROVIDER`（默认 `gateway`）；`baseURL`/`apiKey` 取自该块（平铺或 `options` 嵌套均可读，本机网关文件为嵌套形）；模型 = `OPENCODE_PY_MAAS_MODEL`（若设）> 块内 `models` 映射首键。**不读 `auth.json`**。
- `source` 字段口径：`"env"`（三要素齐）或 `"config"`（文件模式解析成功）——仅此二字面量入证据行，不含任何真实值。
- 本次实测：source = `config`（文件模式：本机 opencode 网关配置双读合并解析成功）；endpoint 以 `<REDACTED_ENDPOINT>` 指代；模型以 `<REDACTED_MODEL>` 指代。

## 3. 预算与超时选值（R0 §8.1 教训）

- 映射核实（C 席亲读源码）：`RuntimeSettings.output_reserve` **逐字**成为 `GenerationRequest.max_output_tokens`（`src/opencode/session/context.py` L154-156、L282-284——"the full reserve, never shrunk to fit the history"），适配器以 wire `max_tokens` 发送（`src/opencode/ai/maas.py` L425）。生成的 reasoning 计入同一预算，不从别处扣减。
- R0 §8.1 实测教训（`doc/evidence/r0/provider.md` L103-108）：`max_output_tokens=64` 被 reasoning 61/64 全额吃掉——零正文、`finish=length`。真实会话必须给足 output 预算。
- 选值：`--output-reserve` 不传（builtin 默认 4096，充足覆盖 reasoning + 短正文）；`--context-limit 100000`（仓内先例值；可用输入预算 = 100000 − 4096 − 0（safety_margin）= 95904，远超短会话输入，且满足 RuntimeSettings 不变式 `context_limit > output_reserve + safety_margin`，不触 exit 70）；`--timeout-s 180`。
- 权限面结论（场景 2，C 席亲读源码）：§5.2 L266 默认表对 in-root `fs__read` 自动放行（`src/opencode/tools/policy.py` `_DEFAULT_EFFECTS`），host 层 root containment 以会话 working_dir（新会话 = 进程 cwd，`src/opencode/app.py` L185）解析 fs 族资源；`--project-root` 单独传 tmp 路径会 exit 70（ConfigLoader 要求 project root 为工作目录或其祖先，`src/opencode/config/loader.py` L494-501），故测试 `monkeypatch.chdir(tmp_path)` 后再传 `--project-root <tmp_path>`（等于工作目录，校验通过）——目标文件落根内、读取免审批，脚本化会话不可能卡在审批面。
- 输出面结论：CLI REPL/单发 print 面只落 `[final]`/`[tool]`/`[run]` 等结构行，不回显 endpoint/model/key；测试仍在 `main()` 返回后立即 `capsys.readouterr()` 消费输出，断言只看 db。

## 4. 两场景结果（调度器 2026-11-11 亲跑回填）

实测运行记录：2026-11-11，调度器亲跑（`OPENCODE_PY_MAAS_REAL=1`，fresh temproot `p2s18-real-1`，worktree p2-s18 四路径落定、合并前态）：`uv run pytest -q tests/integration/test_maas_real_session.py -rs` → **2 passed in 9.30s**。

usage/finish reason 注记（如实）：运行时事件面不携带 provider metering——`RunFinishedData` 仅 `run_id/outcome/error`（`src/opencode/contracts/events.py` L453-460），`UsageChunk`/`FinishChunk` 停在 provider chunk 层不落库；下表 usage/finish 行按此记「不落库」，逻辑终态以 `runs.status` / `py.run.finished` outcome 为准（两者均 succeeded）。

### 场景 1 文本回合（`test_real_text_turn`）

| 项 | 值 |
| --- | --- |
| exit code | 0 |
| `runs.status`（断言 = succeeded） | succeeded（1 run） |
| assistant 非空 text part 长度（不落原文） | 67 |
| usage（input / output / reasoning） | 不落库（见上注记） |
| finish reason | 逻辑 outcome=succeeded；raw finish reason 不落库（见上注记） |
| 正文 SHA-256 前 12 | 308fa00be3b7 |

事件足迹：`py.session.created`=1 / `py.prompt.accepted`=1 / `py.run.started`=1 / `py.step.started`=1 / `py.step.generated`=1 / `py.step.finished`=1 / `py.text.finalized`=1 / `py.run.finished`=1；assistant 消息 1 条（text part 1 个，含标记词 `quokka-7f3a`）。

### 场景 2 工具真执行回路（`test_real_tool_execution_round`）

| 项 | 值 |
| --- | --- |
| exit code | 0 |
| `runs.status`（断言 = succeeded） | succeeded（1 run） |
| `py.tool.requested` / `py.tool.started` / `py.tool.finished` 计数 | 1 / 1 / 1（另 `py.step.started/generated/finished` = 2/2/2） |
| 工具名 / 终态（断言 = `fs.read` + succeeded） | fs.read + succeeded |
| 最终文本含标记词（标记词为固定非敏感常量） | 是（len=11，恰为标记词长度；sha12=13c398ba05e5） |
| usage（input / output / reasoning） | 不落库（见上注记） |
| finish reason | 逻辑 outcome=succeeded；raw finish reason 不落库（见上注记） |
| 工具结果文本 SHA-256 前 12 | 105301f874c8（content_len=189） |

事件足迹：`py.session.created`=1 / `py.prompt.accepted`=1 / `py.run.started`=1 / `py.step.started/generated/finished`=2/2/2 / `py.tool.requested/started/finished`=1/1/1 / `py.text.finalized`=1 / `py.run.finished`=1；assistant 消息 2 条（[0] tool part `fs.read` state=succeeded；[1] text part len=11）。工具回路全闭合：tool_call → runtime 真执行 → result 回传 → 次回合收口。

## 5. 脱敏与凭据纪律声明

沿 R0 §8（`doc/evidence/r0/provider.md` L98-101、L110）措辞：本文件与仓库不含任何真实 endpoint/model/key 值；endpoint/model 以 `<REDACTED_ENDPOINT>` / `<REDACTED_MODEL>` 指代；key 零回显、零落库、零命令行。证据只落摘要（计数、长度、SHA-256 前 12、usage 数值、finish reason——本票实测面 usage/finish 不落库，如实注记于 §4）。凭据保留在用户本地配置；复跑同样以环境变量注入，命令文本不含 key。

## 6. 复跑命令形态

```text
# 双重门之一：严格等于 1（PowerShell 形态）
$env:OPENCODE_PY_MAAS_REAL = "1"
uv run pytest -q tests/integration/test_maas_real_session.py -rs

# 可选：env 三要素覆盖文件源（三者须齐，不与文件源混掺）
$env:OPENCODE_PY_MAAS_BASE_URL = "<REDACTED_ENDPOINT>"
$env:OPENCODE_PY_MAAS_API_KEY  = "<REDACTED_KEY>"
$env:OPENCODE_PY_MAAS_MODEL    = "<REDACTED_MODEL>"
```

缺省走本机 opencode gateway 配置文件（`~/.config/opencode/opencode.json` + `opencode.jsonc`，provider 块默认 `gateway`）。测试断言只看 exit code / `runs.status` / events 计数 / text part 结构与固定标记词；stdout 在 `main()` 返回后立即消费，不入断言。
