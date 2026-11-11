# T-B-080 · P2-S18 微评审（席位B 规划评审）——T-C1-048 MAAS 真实端点短会话证据回路

- 评审对象：worktree p2-s18 @`95c2178`（父 `35b37d1`=#189 检查点=origin/main）；恰 4 路径 442 插入 0 删除（numstat 逐文件亲证见 §2.5）。
- 评审基据：C 席实现票 `T-C1-048.md`（调度器档 sha256 `21c515d2…32ae`，本席 Get-FileHash 复核吻合）票面承诺 vs 实交付逐项对照；`git show 95c2178` 全量 diff 亲读；Python 脱敏探针复核（§2.2 共 21 判定 + 本机真实配置文件模式脱敏探针 + 伪 provider 消息面探针）；skip 路径定向探针（默认 env 恰 2 skip）；B 席独立四门禁复跑（fresh temproot 先建后设纪律）+ install 族隔离复跑。
- 评审人：席位B `ses_orch_B_p2s22`（T-B-080）；2026-11-11；止损 45min 未触；零 git 写（HEAD 恒 `95c2178`，本评审件为唯一写面）。

## §0 总裁决

**PASS**。计数：**blocker 0 / major 0 / minor 1 / suggestion 2**。七面全 PASS（④ 浮报 10/10 真）。Option B 忠实度成立：`src/` 零 hunk、桥只落 `tests/` 面、严格 `=1` 双重门 + 可解析桥两 skip 理由分离、默认套件/CI 恒 skip（定向探针 2 skipped 实证、全量 4S=既有 2+新 2、1599P 基线逐位同）。桥接正确性独立复核成立（解析序逐项探针+代码亲读、11 个 raise 点消息零值、真实配置文件模式 source=config 脱敏探针亲证）。测试面断言纪律全结构事实 + capsys 即时消费 + `monkeypatch.chdir`+`--project-root` 免审批形态，预算/权限结论 10 项源码锚逐一亲核。证据面脱敏合规（零占位残留、全 diff 零 http(s):// URL、数字与调度器真跑事实逐项对账吻合）。冻结面恰 4 路径。门禁四件 B 独立复跑全对预期（1607 收集=1599P+4S+4F @359.79s，4F 全 install 族 WinError 4551 相位型，隔离复跑 7P 全绿再证相位漂移，install 族外 0F；format 458 / mypy 143-0 / lint clean）。MINOR-1（观察-1）为票面规格自带矛盾所致死分支 + docstring 措辞级瑕疵——零行为影响，处方留后续触该文件票。

## §1 七面判定表

| # | 面 | 判定 | 要点 |
|---|---|---|---|
| 1 | 设计 | PASS | Option B 忠实：src/ 零 hunk（numstat 亲证）、桥只落 tests/integration、产品级配置桥明确另票（证据档头注）；双重门=严格 `==1` + 桥可解析，两 skip 理由分点写实（:75-76 / :79-80）；默认套件/CI 恒 skip（2 skipped 探针 + 全量 4S） |
| 2 | 桥接正确性 | PASS | 解析序逐项核（env 任一即 env 模式须齐不混掺；文件模式 XDG>home、json+jsonc 双读浅合并、jsonc string-aware 剥注释/尾逗、provider 默认 gateway、baseURL/apiKey 平铺或 options 嵌套、模型=env>models 首键）；auth.json 零引用；11 个 raise 点消息零值；真实配置文件模式脱敏探针 source=config 三要素齐 http(s) 形 |
| 3 | 测试面 | PASS | 断言全结构事实（exit/runs.status/事件计数≥1/fs.read+succeeded/文本非空/固定标记词 `quokka-7f3a`），零 endpoint/model/key 相等断言；capsys 即时消费；chdir+`--project-root`=工作目录（loader.py:498 校验净形）；预算/权限 10 源码锚逐一亲核 |
| 4 | 证据面 | PASS | 零 `DISPATCHER-FILL` 占位残留（grep 匹配仅 commit message 措辞）；全 diff 零 http(s):// URL、零真实值（仅 `<REDACTED_*>` 指代）；usage/finish「不落库」注记与 events.py:453-460 一致；数字与调度器真跑事实逐项对账吻合；复跑命令形零 key |
| 5 | 冻结 | PASS | `git show --stat` 恰 4 路径 442+/0−（75/3/185/179）；src/ 与产品冻结面零触碰；HEAD=`95c2178` 净树（评审期零 git 写，仅读侧 git 命令） |
| 6 | ④ 浮报核实 | 10/10 真 | 逐条证据见 §2.6（GATES 五项计数与 B 独立复跑逐位一致；REPORT 路径行数与 numstat 终态 50→75 回填差自洽；真实值零落盘亲证） |
| 7 | 门禁 | PASS | 四件独立复跑：1607 collected=1599P+4S+4F @359.79s（4F 全 install 族 WinError 4551 相位型；隔离复跑 7P @12.88s 全绿——同文件零代码差下 4F↔7P 漂移，机器相位实证；install 族外 0F）+ruff clean+format 458+mypy 143-0；skip 定向探针 2 skipped @0.82s EXIT=0 |

## §2 逐面证据

### 2.1 设计面

- **Option B 忠实度成立（三方亲证）**：numstat 4 路径无一 `src/` hunk——写面=pyproject.toml(+3) / tests/integration/_maas_real_config.py(+185 新增) / tests/integration/test_maas_real_session.py(+179 新增) / doc/evidence/maas-real-session.md(+75 新增)，恰 3 新增+1 修改=票面 §3 写面口径，产品码零改动主张成立；桥只落测试面（helper 位于 tests/integration/、仅被本测试 import，产品代码零引用）；产品级配置桥（ConfigLoader 第五层）明确记档另票（证据档头注）；标记词/预算/超时全为测试文件内非敏感常量（`_MARKER`/`_CONTEXT_LIMIT`/`_TIMEOUT_S`，:62-70）。
- **双重门形态**：`_gated_config()`（test_maas_real_session.py:73-80）先严格判 `os.environ.get("OPENCODE_PY_MAAS_REAL") != "1"` → skip（理由一「real-MAAS opt-in not enabled (OPENCODE_PY_MAAS_REAL is not exactly 1)」，:75-76），再 `resolve_maas_real_config()` 捕 `MaasRealConfigUnavailable` → skip（理由二「real-MAAS config unavailable: <仅缺件名>」，:79-80）——两理由分开写实、skip 面零值。模块级 `pytestmark = pytest.mark.maas_real`（:62）+ pyproject markers 注册——探针输出零 PytestUnknownMarkWarning。
- **默认套件/CI 恒 skip**：定向探针（temproot t-b-080-skip-2）`2 skipped in 0.82s` EXIT=0；全量 4S=基线 2S+新 2S（算术闭合），1599P 与基线逐位同——既有测试零翻转零失踪；env 面 conftest.py 仅 setdefault temproot、无 env 清洗（亲读 25 行全文），双重门不受 fixture 干扰。

### 2.2 桥接正确性

- **A1 解析序逐项核（代码亲读+探针）**：env 模式——`resolve_maas_real_config()`（:169-185）读三要素，任一 truthy（:174）即入 env 模式，缺任一 raise「missing: <env 变量名>」（:180-183，探针：仅设合成 API_KEY → raise 名单只含另两名、合成值零回显），不与文件源混掺；文件模式仅在三要素全空时入（:185 调 `_resolve_from_files`）。文件模式：`_config_dir()`=XDG_CONFIG_HOME/opencode 否则 ~/.config/opencode（探针时 XDG 未设→home 形）；`opencode.json` 先、`opencode.jsonc` 后逐 `merged.update()`——jsonc 键浅覆盖 json；jsonc 剥 `//` 行注释与尾逗号 string-aware（探针 11 项：passthrough 恒等/注释剥蚀保换行/URL 内 `//` 不蚀/尾逗数组剥蚀/尾逗嵌套剥蚀/注释到 EOF/转义引号恒等+解析（2 项字节重排见 SUGGESTION-2，parse 等价全绿）/逗号空格闭包解析/合成 jsonc 文档解析 models 序 m1,m2+options 内 URL 完好）；provider 块=`OPENCODE_PY_MAAS_PROVIDER` 默认 `gateway`（伪 provider 探针：raise「provider block 'SYNTHETIC-BOGUS' not found in the merged config」零值）；`_block_field` 平铺或 options 嵌套（探针 5 项：flat/nested/flat 优先/空串跳过取嵌套/缺失返 None）；模型=env（观察-1 见 §3 MINOR-1）>块内 models 首键（探针 m1 首键序）。
- **不读 auth.json**：diff 全文+helper 全文零 `auth.json` 引用（唯 docstring 声明「auth.json is never read」）。
- **异常消息零值泄露（11 raise 点逐点核）**：`_read_config_file` 三点（unreadable / not valid JSON / not a JSON object——仅固定文件名）；`_resolve_from_files` 七点（无配置文件 / provider 块缺失 / 非对象 / 无 baseURL / 无 apiKey / models 空映射 / 空键——仅 provider 名与字段名）；`resolve` 一点（env incomplete——仅 env 变量名）。合成值探针+伪 provider 探针亲证零回显；skip 理由文本（`-rs` 实录）承零值消息面。
- **真实文件形状亲证**：本机配置文件模式脱敏探针 source=config、三要素齐、base_url 为 http(s) 形（长度仅探针回显不入本档）——桥确按「本机网关文件为嵌套形」工作（options 嵌套 baseURL/apiKey 路径实证）。
- **非向量注记**：`MaasRealConfig` repr 理论可含值，但断言面永不触及该对象（断言只看 exit/status/事件/文本结构）、capsys 已即时消费、pytest showlocals 系 opt-in——当前零泄露向量。

### 2.3 测试面

- **A2 断言纪律**：两场景断言集=exit code==0、`runs.status=="succeeded"`（`_run_status` 直查 runs 表）、`py.tool.requested/started/finished` COUNT>=1（`_event_count` 参数化直查 events 表）、tool part `name=="fs.read" and state=="succeeded"`、非空 text part（场景 1 len>0）/终文本含 `_MARKER`（场景 2）——全结构事实，零原始 endpoint/model/key 相等断言；标记词=固定非敏感常量 `quokka-7f3a`（:66，prompt/目标文件/断言三处同源）。
- **capsys 即时消费**：两场景 `main()` 返回后立即 `capsys.readouterr()`——失败报告不可能回显 captured 输出面（docstring 自述与代码一致）。
- **monkeypatch.chdir+--project-root 形态**：场景 2 `monkeypatch.chdir(tmp_path)` 后传 `--project-root tmp_path`（等于工作目录，loader.py:498「须为工作目录或其祖先」校验净过）；目标文件落 tmp_path 根内、`fs__read` 默认放行（policy.py:88-101 `_DEFAULT_EFFECTS` 亲证 `"fs__read": "allow"`）→ 脚本化会话不可能卡审批面。
- **预算/超时选值（docstring 记录口径亲核）**：`--output-reserve` 不传（argv 无该 flag，builtin 4096）、`--context-limit 100000`（不变式 `context_limit > output_reserve + safety_margin` 成立、可用输入预算远超短会话）、`--timeout-s 180`——R0 §8.1 教训引证（reasoning 计入同一 output 预算）与源码映射双核（见下）。
- **源码锚 10 项逐一亲核**：① test_cli.py:112-114 先例（`open_database(mode="create")`+`ReadRepositories.messages`——本测试 `_assistant_messages` 同面 limit=50）；② contracts/events.py:53-55（PY_TOOL_REQUESTED/STARTED/FINISHED=「py.tool.requested/started/finished」）；③ session/runner.py:139（`RunOutcome = Literal["succeeded","failed","interrupted"]`）；④ session/context.py:154-156（output_reserve「becomes max_output_tokens verbatim」）+⑤ :282-284（`max_output_tokens=self._settings.output_reserve`，注释「the full reserve, never shrunk to fit the history」）；⑥ ai/maas.py:425（`"max_tokens": request.max_output_tokens` wire 面）；⑦ tools/policy.py:88-101（`_DEFAULT_EFFECTS` fs__read=allow）；⑧ app.py:185（`resolved_working_dir = working_dir if working_dir is not None else os.getcwd()`——新会话工作目录=进程 cwd，chdir 后即 tmp_path）；⑨ config/loader.py:494-501（project_root 须为工作目录或其祖先否则 ConfigError）；⑩ contracts/events.py:453-460（RunFinishedData 仅 run_id/outcome/error，extra="forbid"）。
- **导入面**：tests/__init__.py 与 tests/integration/__init__.py 均预存（git ls-files 亲证）→ `from tests.integration._maas_real_config import` 解析净（C 席 TDD-RED 期 ModuleNotFoundError 系 helper 未建时红相，终态探针 2 收集零碍）。

### 2.4 证据面

- **A3 脱敏合规（三探针亲证）**：① `git show 95c2178 | Select-String 'DISPATCHER-FILL'`——匹配仅 commit message 内「dispatcher-filled」措辞，文件体零占位符残留；② `Select-String 'https?://'` 全 diff 零匹配——endpoint/model 全程 `<REDACTED_ENDPOINT>`/`<REDACTED_MODEL>` 指代、key `<REDACTED_KEY>`；③ diff 全文亲读零真实值。证据只落计数/长度/SHA-256 前 12 形态。
- **usage/finish「不落库」注记核实**：contracts/events.py:453-460 亲证 `RunFinishedData` 仅 run_id/outcome/error 三字段（model_config extra="forbid"）——运行时事件面确不携 provider metering，注记与源码一致；逻辑终态以 `runs.status`/`py.run.finished` outcome 为准的口径成立（两场景均 succeeded）。
- **数字对账（证据档 §4 vs 调度器验收事实）逐项吻合**：2 passed in 9.30s（temproot `p2s18-real-1` 目录在档亲见）；S1=succeeded/非空 text len=67/sha12=`308fa00be3b7`/事件足迹 8 事件各 1/assistant 1 条 text part 1 个含标记词；S2=succeeded/tool requested/started/finished=1/1/1、step 2/2/2、fs.read+succeeded、content_len=189/sha12=`105301f874c8`、终文本 len=11/sha12=`13c398ba05e5` 含标记词；两场景 source=`config`。零出入。
- **复跑命令形无 key**：§6 命令块仅 env 注入形（`<REDACTED_KEY>` 指代），key 零回显零命令行零落库。

### 2.5 冻结面

- `git show --stat 95c2178`：恰 4 路径 442+/0−——doc/evidence/maas-real-session.md +75 / pyproject.toml +3 / tests/integration/_maas_real_config.py +185 / tests/integration/test_maas_real_session.py +179（numstat 逐项与票面吻合，0 删除）。
- **冻结清单零触碰亲证**：src/** 全树（shellparse/ai/config/session/tools/storage/httpapi/process 等）、uv.lock、tests/contract/fixtures/**——均不在 4 路径内；既有测试文件零改动（tests/ 下仅两新文件）。
- HEAD=`95c2178`、评审起始工作树净（git status --porcelain 空）；评审期零 git 写，仅读侧 git 命令（rev-parse/status/show/ls-files）。

### 2.6 ④ 浮报核实（C 席 GATES/REPORT/FACES 行逐条真伪）

| # | 项 | 判 | 证据 |
|---|---|---|---|
| 1 | GATES format 458 | 真 | B 独立复跑 `458 files already formatted` 逐位一致（455 基线+2 py+1 md 恰合） |
| 2 | GATES ruff clean | 真 | B 复跑 `All checks passed!` |
| 3 | GATES mypy 143 | 真 | B 复跑 `Success: no issues found in 143 source files`（141→143=+2 新 .py 恰合） |
| 4 | GATES pytest 全量 1607=1599P+4S+4F、4F 全 install 族 | 真 | B 复跑 1607 collected=1599P+4S+4F @359.79s；4F 全 tests/integration/test_cli_install.py、全 WinError 4551 @subprocess.py:1538 CreateProcess 同签名；1599P 与基线逐位同=既有零翻转 |
| 5 | GATES install 隔离复跑（C 席读数 4F+3P WinError 4551）→install 族外 0F | 真 | B 隔离复跑 **7P @12.88s 全绿**（temproot t-b-080-install-1）——同文件零代码差下全量态 4F↔隔离态 7P 漂移（C 席 4F+3P、T-B-079 席 7P、本席 7P 三态异读），机器相位型实证加强；判据 install 族外 0F 于 B 全量复跑成立 |
| 6 | FACES skip 探针 2 skipped 0.80s | 真 | B 复跑 2 skipped in 0.82s EXIT=0（temproot t-b-080-skip-2），理由文本实录逐字一致 |
| 7 | FACES helper 脱敏探针 source=config、部分 env/伪 provider 消息零值 | 真 | B 复跑：source=config；部分三元组 raise 名单零值；伪 provider raise 零值（§2.2） |
| 8 | FACES 桥性质（string-aware jsonc 剥蚀/options 嵌套/models 首键/不读 auth.json/值零落盘） | 真 | B 探针 16 项+代码亲读全证（§2.2）；TDD-RED ModuleNotFoundError 系 helper 未建红相，终态导入净（__init__.py 预存） |
| 9 | REPORT 4 路径 185+179+50+3 | 真 | numstat 终态 185/179/75/3；50→75 之差=调度器验收回填行（证据档自述「模板由 C 席交付；真实数值由调度器回填；无占位符残留」+p2s18-real-1 temproot 在档），计数自洽 |
| 10 | REPORT 真实 endpoint/model/key 零落盘（文件/日志/报告） | 真 | B 亲证：全 diff 零 http(s):// URL、零占位残留、全文亲读零真实值；C 席日志（T-C1-048.txt 7 行）零值；证据档仅 `<REDACTED_*>` 指代 |

### 2.7 门禁面（B 席独立复跑，fresh temproot 先建后设纪律）

| 门禁 | B 席读数 | 判 |
|---|---|---|
| `uv run python -m pytest -q`（temproot `t-b-080-full-1`） | **1607 collected / 1599P+4S+4F @359.79s**；4F 全 install 族（WinError 4551 @subprocess.py:1538 CreateProcess） | install 族外 0F ✓；4S=既有 2+新 maas_real 2 ✓；1599P 基线逐位同=零翻转 ✓（时长 335.10s→359.79s 波动不计入判据） |
| install 族隔离复跑 `uv run python -m pytest -q tests/integration/test_cli_install.py`（temproot `t-b-080-install-1`） | **7P @12.88s 全绿** | WDAC 相位型机器态记档：同文件零代码差下全量态 4F↔隔离态 7P 漂移再证实；判据=install 族外 0F（全量面已成立） |
| `uv run ruff check .` | **All checks passed!**（clean） | ✓ |
| `uv run ruff format --check .` | **458 files already formatted**，exit 0 | 455 基线+2 py+1 md=458 恰合 ✓ |
| `uv run python -m mypy src tools/r0 tools/r5 tests/r0 tests/contract tests/integration` | **Success: no issues found in 143 source files** | 141→143=+2 新 .py ✓ |
| skip 定向探针 `uv run python -m pytest -q -rs tests/integration/test_maas_real_session.py`（temproot `t-b-080-skip-2`） | **2 skipped in 0.82s**，EXIT=0，理由=「real-MAAS opt-in not enabled (OPENCODE_PY_MAAS_REAL is not exactly 1)」×2，零 marker 警告 | 恰 2 skip ✓ 双门默认形态 ✓ |

## §3 判定面（强制）

- **计数：blocker 0 / major 0 / minor 1 / suggestion 2。**
- **MINOR-1（观察-1 定性：MINOR，文档/死代码级）**：定位 `tests/integration/_maas_real_config.py:158-159`——`_resolve_from_files` 尾段 `model = os.environ.get(_ENV_MODEL)` + `if not model:` 的文件模式 env 覆盖分支，经唯一调用路径**不可达**：`resolve_maas_real_config()`（:169-185）在 `_ENV_MODEL` 任一 truthy（:171-174）时即入 env 模式（缺件 raise :180-183），`_resolve_from_files()` 仅在 :185 三要素全空时被调，故 :158 读数恒空、:159 恒真、models 首键路径（:160-165）恒走。根因=实现票 §3.A1 第 1 条（任一即 env 模式且须齐）与第 3 条（模型=env（若设）>models 首键）并立的自相矛盾，C 席忠实双条落码所致；模块 docstring L16-17「the model is OPENCODE_PY_MAAS_MODEL (if set) else the first key」在函数级单独读来具误导性（resolve 层等效序主张仍真，证据档 §2 同口径不构成失实）。**影响**：零行为影响（死分支、错误消息仍零值、resolve 层等效序与文档一致）；唯一例外形=直接调用私有函数（本仓零此形）。**处方**：代码改动须另票，本评审不开写面——建议后续触该文件的票顺手将 docstring 改为「env 模型覆盖在 `resolve_maas_real_config` 层强制；`_resolve_from_files` 内回读为防御性」或删 :158-159 死读（models 首键直取）；保留作防御亦零害。无需独立修复票。
- **SUGGESTION-1（观察-2 裁定）**：证据 §4 usage/finish「不落库」注记所述运行时 metering 持久化缺口（RunFinishedData 仅 run_id/outcome/error，UsageChunk/FinishChunk 停 provider chunk 层，events.py:453-460 亲证）——**裁定：应记 roadmap 候选项**（观测性/计费面缺口真实；core 0.1 稳定门以逻辑 outcome+事件计数已足；落库须事件 schema 扩展票另行设计），非本票缺陷。**处方**：调度器裁量入 roadmap 待办池。
- **SUGGESTION-2**：`_strip_jsonc` 尾逗延迟发射使「逗号+空白」序列字节重排（探针：`[1, 2,]`→`[1 ,2]` 形、`{"a": "b\"c", "d": 2}`→`{"a": "b\"c" ,"d": 2}` 形）——JSON 语义不变（全部 parse 等价探针绿），唯一消费面 json.loads，非缺陷；若未来需字节恒等，可将 pending 逗号先于空白发射。**处方**：无需。
- **总分裁决：PASS**——票面四写面、七评审面、④ 浮报 10 项、四门禁+定向探针全数兑现；唯一 minor 为票面规格矛盾遗留的文档/死代码级瑕疵，零行为影响。

## §4 结论

T-C1-048 以 Option B 形态精确交付 MAAS 真实端点短会话证据回路：双重门 opt-in 测试（默认恒 skip、CI 零影响）、纯 stdlib 配置桥（env 三要素>本机 gateway 配置双读合并、异常消息零值）、两场景结构化断言（文本回合+工具真执行回路全闭合）、脱敏证据档与调度器真跑回填（2 passed in 9.30s）逐项对账吻合。产品码零改动、冻结面零触碰、四门禁 B 席独立复跑全对预期（install 族 WDAC 相位型隔离复跑 7P 全绿再证实）。观察-1 定性 MINOR-1（docstring/死代码级，处方留后续触该文件票）、观察-2 裁定记 roadmap 候选。P2-S18 交付在档，待调度器验收合并。

SHA256（本行以上全文——评审件正文，不含本行及其前导换行；复算=去除本行与其前导换行并 CRLF→LF 归一后 Get-FileHash -Algorithm SHA256，沿 T-B-077 工序口径） F7ABB4703B05CE87C35668DA2267FE199B28E97928848DFC5D22DFF44A6B4FA6