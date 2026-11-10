# T-B-077 · P2-S16 微评审（席位B 规划评审）——T-C1-065 shell 解析 C2（PowerShell 语法打样 + bash advisory 富化接线）

- 评审对象：worktree p2-s16 @`1a7d824`（父 `79f9793`=#187 检查点=origin/main）；恰 7 路径 920 插入 9 删除（numstat 逐文件亲证见 §2.5）。
- 评审基据：C 席实现票 `T-C1-065.md`（调度器档 sha256 `39696a3d…a6e1d`）票面承诺 vs 实交付逐项对照；`git show 1a7d824` 全量 diff 亲读；Python 探针复核（④ 浮报与边缘行为，探针脚本 `C:/Users/lyu/AppData/Local/Temp/opencode/probe_tb077.py`）；B 席独立四门禁复跑（fresh temproot 纪律）。
- 评审人：席位B `ses_orch_B_p2s18`（T-B-077）；2026-11-11；止损 45min 未触；零 git 写（HEAD 恒 `1a7d824`，本评审件为唯一写面）。

## §0 总裁决

**PASS**。计数：**blocker 0 / major 0 / minor 2 / suggestion 3**。①②两面交付均达票面：富化接线红线五钉全落测、既有 advisory 测试零翻转；PS 打样 IR 全复用零扩展、七码零新增、unsupported 清单 fail-closed 完备、golden 33+26 如报。唯 MINOR-1（跨 pattern label 迁移边缘——票面红线③字面与票面优先序条款内在冲突，实现取序优先语义但未上浮该冲突）与 MINOR-2（④(g) 自报 49L vs 实 82L 口径不实）需记档处置；三 suggestion 均可并入 C3/收口票。门禁四件 B 独立复跑全过（install 族 4F=WDAC 相位型机器态，隔离复跑 7P 绿，**install 族外 0F**）。

## §1 七面判定表

| # | 面 | 判定 | 要点 |
|---|---|---|---|
| 1 | 设计 | PASS | 扫描面=raw 地板逐字∪decoded[joined argv+各 argv 项]忠实票面；label 元组序优先保留；fail-open `except Exception` 与票面「解析面任何异常」一致 |
| 2 | 接线 | PASS | bash.py 恰 3 hunk（docstring/import/_deny_label 面）；`_ADVISORY_TEXT`/`_render`/`_execute` 零触碰；shellparse 叶子包无循环依赖 |
| 3 | PS 打样 | PASS | IR 全复用零扩展（别名+解码吸收+`$_`/`$?` 口径）；七码零新增；unsupported 11 形全实现全 golden 钉；近似披露探针证真 |
| 4 | 测试 | PASS | 新钉 7+66=73 逐项落测；既有 advisory 测试零改动（−1=docstring 行）；C1 三件零触碰原样过；unwired+纯度三钉+冻结钉+闭集钉在 |
| 5 | 冻结 | PASS | `git show --stat` 恰 7 路径 920+/9− 亲证；冻结清单零触碰；HEAD=1a7d824 净树 |
| 6 | ④ 浮报核实 | 7 真 1 假 | (a)(b)(c)(d)(e)(f)(h) 真；(g) 假=自报口径不实（交付件本身与 numstat 一致） |
| 7 | 门禁 | PASS | 四件复跑全过；install 族 4F=机器态相位（隔离复跑 7P 绿） |

## §2 逐面证据

### 2.1 设计面

- **扫描面定义忠实**：`src/opencode/tools/builtin/bash.py:177` `surfaces = (command, *_decoded_scan_surfaces(command))`——raw command string 为首面逐字保留（地板不变式）；decoded 面=`bash.py:161` 每 pipeline stage 的 `" ".join(argv)`（joined argv）+ `bash.py:162` 各 argv 项。票面措辞「每 pipeline stage 的 argv 项/decoded token 文本」在进度档①明确定义为 joined+各项，与实现一致；joined 面是引号遮蔽命中的实际载体（探针：`rm -rf "/"` 的裸项 `/` 单独不命中任何 deny 正则，仅 joined 面 `rm -rf /` 以行尾 `/` 命中 pattern 1）。
- **label 元组序优先保留**：`bash.py:178-180` 外层 `for label, pattern in _DENY_HINT_PATTERNS` + 内层 `any(pattern.search(surface) for surface in surfaces)`——外层 patterns 循环=序优先语义，任一面命中即该 label，与票面「沿元组序不变（首命中语义保留——raw 与 decoded 面任一命中即该 label）」逐字对应。
- **fail-open 恰当**：`bash.py:153-156` `try: parse_bash(command) except Exception: return ()`——broad-except 与票面钉「ShellParseError 或解析面任何异常」一致（ShellParseError 为其子集）；不吞 BaseException（KeyboardInterrupt/SystemExit 正常传播）。ruff BLE 未启用且此处语义正当。
- **空 Token 惰性**（T-B-076 MINOR-2）：探针 `parse_bash("echo \\\n").statements[0].segments[0].argv == ('echo', '')`，空串对五条非空 deny 正则惰性、`_deny_label` 返 None——零特判成立。
- suggestion 级备注：try 域仅包 `parse_bash`，面构造循环（`bash.py:157-163`）在 try 外——IR 为 frozen dataclass 元组迭代确定不抛，但纳入 try 更贴合「任何异常」字面全吞（见 §3 S-1）。

### 2.2 接线正确性

- **diff 仅 advisory 富化面**：bash.py 恰 3 hunk——模块 docstring +6/−1（C2 说明句）、runtime import +1（`bash.py:70` `from opencode.shellparse import parse_bash`）、`_decoded_scan_surfaces` 新增 + `_deny_label` 重写；合计 **+41/−3**（numstat 亲证；调度转录「+44/−3」系把 `--stat` 总行数 44 误作插入数，实际插入 41）。`_ADVISORY_TEXT`（`bash.py:139-142`）零触碰（hunk 上下文亲证+冻结文本测试逐字节钉）；`_render`/`_execute` 零 hunk——子进程/超时/截断/结构化输出执行路径零触碰。
- **循环依赖风险核**：shellparse 为叶子包——`__init__.py` 仅 import `.ir`/`.powershell`；`powershell.py` 仅 `.ir`；`ir.py` 仅 stdlib（dataclasses/enum/typing，`ir.py:14-18` 亲读）。无任何反向引用 tools/process 面 → `bash.py` runtime import 无环。且 `test_shellparse_powershell.py:154` 纯度 AST allowlist（`_ALLOWED_MODULES = {"__future__","dataclasses","enum","typing"}`）结构性封死未来反向 import。
- **tools 面引用面**：grep 亲证 `src/opencode/tools` 下 shellparse 引用仅 bash.py 3 处（docstring×2+import×1），零 `parse_powershell` 引用（与 unwired tripwire 测试双证）。

### 2.3 PS 打样面

- **IR 全复用零扩展**：ir.py 零触碰（name-only 亲证）；`PowerShellCommand = BashCommand` 纯别名（`powershell.py:81`）；反引号转义差异在解码层吸收（`_decode_backtick` `powershell.py:331` → escapes 落 `LiteralPart.text` 后才成 form，IR 无需承载）；`$_` 由 `_NAME_START`（含下划线）覆盖；`$?` 唯一 automatic 变量（`powershell.py:91` `_PS_SPECIAL_VAR_NAMES = frozenset("?")`）。判定成立。
- **七码复用零新增**：raise-site 全扫描仅七码（unclosed-single-quote/unclosed-double-quote/unclosed-brace/empty-variable-name/dangling-escape/empty-command/unsupported-construct）；`test_negative_family_codes_are_the_declared_set`（`test_shellparse_powershell.py:132`）闭集钉恰七码。PS 特有拒绝全部映射 `unsupported-construct`，零新码 ✓。
- **unsupported 清单完备性**（docstring 声明 vs 实现逐项对照）：子表达式 `$(...)`（含双引号内插值形，golden 双钉）✓；数组表达式 `@(...)`✓；哈希表 `@{...}`✓；splatting 词首 `@name`✓；脚本块 `{...}`✓；类型字面量与索引 `[...]`（含词中 `$x[0]`）✓；逗号数组 `,`✓；保留 `<`✓；call 操作符 `&`✓；pipeline-chain `&&`/`||`✓；非纯形花括变量（`${name:x}`、`${}` 空、未闭）✓——11 形全实现全 golden 反例钉（26 反例逐一对应）。
- **近似面披露如实性**（探针复核）：`` `e ``→ESC `\x1b`（PS6+ 语义、5.1 字面差异披露在案）✓；`` `u{263A}`` → `unsupported-construct` @offset 15=`{` 位（经脚本块拒绝、无独立诊断——测试 docstring+进度档两处披露，唯 `powershell.py` 模块 docstring 近似清单未列，见 S-2）；redirect 折叠探针 `*>&1` → argv `('Get-Process','*','>&','1')` ✓；表达式模式未建模（披露）✓；`` `\r\n `` CRLF 行续边缘披露并移交 C3 ✓。五项 (④-d) 全真。

### 2.4 测试面

- **票面新钉逐项落测核**（`tests/integration/test_tool_bash.py`）：引号遮蔽命中新 label→`tb:484`✓；行续遮蔽命中→`tb:490`✓；解析失败回退 raw 同 label→`tb:496`（unit）+`tb:536`（集成：解析失败命令正常执行+advisory，`(state, effect_status)==("succeeded","completed")`）✓；空 Token 惰性→`tb:503`（argv 钉+None 钉）✓；冻结文本逐字节→`tb:510`（`_ADVISORY_TEXT.format(label=...)` 全字面量比对）✓；advisory imprecision→`tb:522`（`echo "rm -rf /"` 无害 echo 照跑+advise）✓；执行面永不 block/绝不 TOOL_FAILED→两集成测 `succeeded/completed` 断言✓；`$-` 字面票内零动作=票面口径✓。
- **既有 advisory 测试零改动**：test_tool_bash.py 的 −1 行为模块 docstring 行（「4. deny hint fires but never blocks…」扩写），测试函数体零触碰；既有 `test_bash_deny_hint_fires_but_does_not_block` 原样通过零翻转（B 全量亲证）。
- **C1 tripwire**：C1 三件（`shellparse/bash.py`/`test_shellparse_bash.py`/`shellparse_bash_golden.json`）不在 7 路径内=零改动，且全量中原样通过（advisory-never-changes-ask tripwire 亲证）。
- **unwired tripwire**：`test_powershell_prototype_is_unwired_from_the_tool_faces`（`test_shellparse_powershell.py:228`，bash 工具源零 `parse_powershell` 引用）+ B 侧 grep 独立亲证 ✓。
- **纯度三钉+冻结钉+码族闭集**：AST allowlist（`pst:154`）/运行时 open 陷阱全语料（`pst:172`）/FrozenInstanceError（`pst:186`）/码族闭集（`pst:132`）全在；ask-face 三态钉（`pst:198`：clean/deny-hit/malformed 三命令同 `ask/default`）。
- **计数核算**：bash 面 +7（5 unit+2 集成）、PS 面 33 正+26 反+7 独立=66，共 **+73**；基线 1510P+2S → C 席报 1583P+2S ✓；B 全量 1579P+2S+4F（全 install 族）→ 非 install 面 1576P+2S+0F=基线非 install 1503P+2S+73 恰合（见 §2.7）。

### 2.5 冻结面

- `git show --stat 1a7d824` 亲证：**恰 7 路径，920 插入 9 删除**。numstat 逐文件：`doc/orchestration/progress/T-C1-065.md` 82/0、`src/opencode/shellparse/__init__.py` 11/5、`src/opencode/shellparse/powershell.py` 402/0、`src/opencode/tools/builtin/bash.py` 41/3、`tests/contract/shellparse_fixtures/shellparse_powershell_golden.json` 65/0、`tests/contract/test_shellparse_powershell.py` 240/0、`tests/integration/test_tool_bash.py` 79/1（合计 920/9 ✓）。
- `git diff --name-only 79f9793 1a7d824` 与上述 7 路径逐一相同 → policy.py、`process/**`、events/registry/schema 面、pyproject.toml、uv.lock、C1 三件、`tests/contract/fixtures/**` **零触碰**（缺席即证明；定向 `git diff --stat` 亦空）。
- HEAD=`1a7d824`、porcelain 净（B 自身零污染——除本评审件外零写面）。

### 2.6 ④ 浮报核实（八项逐条真伪）

| 项 | 声明 | B 核实 | 依据 |
|---|---|---|---|
| (a) | advisory 测试面实际在 `tests/integration/test_tool_bash.py`（票写 tests/contract/） | **真** | diff 路径亲证（调度 porcelain 已证一致） |
| (b) | `echo $(rm -rf /)` raw 面**不**命中（闭括号断尾前瞻）；改用 `echo $(mkfs /dev/sda)` | **真** | B 探针：raw 对前者返 `[]`、对后者命中 `filesystem format`；两者 `parse_bash` 均 `ShellParseError(unsupported-construct)`——invariant ② 落测真实 |
| (c) | `echo "rm -rf /"` 无害命中=预期不精确 | **真** | 探针 decoded 命中 `recursive root/home deletion`；`tb:522` 钉为「照跑+advise」预期行为 |
| (d) | PS 近似五项（redirect 折叠/`` `e `` PS6+/`` `u{...}`` 经 `{` 拒/表达式未建模/`` `\r\n `` CRLF） | **真** | 探针+代码逐项复核（§2.3）；CRLF 边缘披露并移交 C3 |
| (e) | `$-` 分歧接受零动作（SUGGESTION-1 接续） | **合理** | advisory 原型零行为风险、C1 现状字面、C3 候选钉在案——B 判接受成立 |
| (f) | 空 Token 惰性已落测钉 | **真** | 探针 argv=`('echo','')`+`tb:503` |
| (g) | 进度档自报 49L | **假（口径不实）** | 实际 **82L**（B 行数亲证=numstat 82 一致）；交付件本身无缺陷，系 C 席回报口径错误 → MINOR-2 |
| (h) | format 449@C 席 vs 450@调度器 | **真（已核）** | B 复跑=**450**（exit 0）；机理亲证：本仓 ruff format walk 计入 md（单 md 探针报「1 file already formatted」）；449=C 席落进度档前时序、450=落档后，commit message 自述与调度器口径、B 复跑三方一致 |

### 2.7 门禁面（B 席独立复跑，fresh temproot 纪律）

| 门禁 | B 复跑结果 | 判定 |
|---|---|---|
| `uv run python -m pytest -q` | 全量（temproot `tb077-r1` fresh）：**1585 collected / 1579P+2S+4F @368.30s**——4F 全部 install 族（`tests/integration/test_cli_install.py`，`OSError [WinError 4551]`=WDAC 应用控制策略拦截新装 exe，#185/#187 相位型机器态）；按纪律隔离复跑 install 族（temproot `tb077-r2` fresh）：**7P @12.14s** | **PASS**（判据=install 族外 0F ✓：非 install 面 1576P+2S+0F；收集 1585 ✓。C 席 1583P+2S+0F @382.71s 为 WDAC 允许相，两相位口径一致记档） |
| `uv run ruff check .` | **All checks passed!** | PASS |
| `uv run ruff format --check .` | **450 files already formatted**（exit 0） | PASS（预期 450=447 基线+2 新 py+进度档 md ✓，(h) 亲证） |
| `uv run python -m mypy src tools/r0 tools/r5 tests/r0 tests/contract tests/integration` | **Success: no issues found in 141 source files** | PASS（预期 141-0=基线 139+2 新 py ✓） |

## §3 发现清单（分级+定位+处方）

**blocker：0。major：0。**

### MINOR-1 · 跨 pattern label 迁移边缘——票面红线③字面不具普遍成立性

- **定位**：`src/opencode/tools/builtin/bash.py:177-181`（`_deny_label` union 扫描）；关联票面红线③与优先序条款的内在冲突。
- **实证**（B 探针）：`echo "rm -rf /" ; mkfs /dev/sda`——富化前（raw-only）label=`filesystem format`（pattern 3）；富化后 label=`recursive root/home deletion`（decoded joined 面 `echo rm -rf /` 命中更早的 pattern 1）。即**先前已命中命令的 label 可向更早 pattern 迁移**。票面红线③字面（「当前已命中命令的 label 与 advisory 文本逐字节同一」）在此边缘不成立；票面自身的优先序条款（「元组序优先——raw 与 decoded 任一命中即该 label」）才是实现所循——两条款冲突，C 取序优先语义（两条款中以此为准合理）但进度档将③静默收窄为「新命中命令」未上浮该冲突。
- **影响评估**：advisory-only（两 label 皆为对该命令危险内容的真实描述、无 block/ask/执行面影响、advisory 文本模板不变）；既有测试零翻转。故 minor 非 major。
- **处方**（任选其一，B 倾向 (a)）：(a) 增钉一条明示 union 序优先语义的边缘测试（先前命中命令可因 decoded 面揭示更早 pattern 而 label 迁移）+ 进度档 ④ 补披露；(b) 改两遍扫描（raw 命中先定 label，raw 未中才扫 decoded 面）恢复字面 label 稳定。可并入 C3/收口票。

### MINOR-2 · ④(g) 自报口径不实（进度档行数）

- **定位**：C 席回报面（非仓库内文件）；交付件 `doc/orchestration/progress/T-C1-065.md` 本身 82L 与 numstat 一致无缺陷。
- **实证**：B 行数亲证 82L；C 自报 49L 错误。
- **处方**：交付件无需修改；调度器台账记 49L→82L 校准（与调度转录 bash.py +44/−3→实 +41/−3 同类：**自报数字须以 numstat 为准**）。

### SUGGESTION-1 · try 域覆盖面（bash.py:153-163）

`except Exception` 仅包 `parse_bash`；面构造循环（statements/segments 迭代+join）在 try 外。IR 为 frozen dataclass 元组，迭代确定不抛；将循环纳入 try 可使「解析面任何异常」字面全吞（红线⑤ belt-and-braces）。零风险小改，可并入任意后续触 bash.py 的票。

### SUGGESTION-2 · golden 语料两缺口（`$_` 正例、`` `u{...}`` 反例）

`$_` 由 `_NAME_START` 覆盖为代码真但无 golden 钉；`` `u{263A}`` 行为探针证真（unsupported-construct @15 经 `{`）且测试 docstring+进度档两处披露，唯 `powershell.py` 模块 docstring 近似清单未列该项。两例并入 C3 fixture 批（进度档⑤「编码/超长行/控制字符边缘」已在案），模块 docstring 补一句。

### SUGGESTION-3 · 码族闭集钉加固（test_shellparse_powershell.py:132）

现闭集测试钉的是 golden 语料码集（语料→七码），非源 raise-site（源→码字面量）。可加静态钉（`powershell.py` 内 `_error(` 码字面量 ⊆ 七码）防未来第八码无语料漂移。C1 同形、票面「码族闭集测试恰七码」要求已满足，属加固非缺口。

## §4 处方汇总与调度器动作

1. **合并裁决建议：PASS**（0 blocker/0 major；MINOR×2 均非交付缺陷级——MINOR-1 为票面条款冲突的未上浮、MINOR-2 为回报口径）。
2. MINOR-1 处方 (a)（边缘测试+披露）建议并入 C3/收口票；若调度器取 (b)（两遍扫描）则需微码面票。
3. 台账校准两笔：进度档 49L→82L；bash.py +44/−3→+41/−3（numstat 口径）。
4. S-1/S-2/S-3 均可搭 C3 便车，无需独立成片（与进度档⑤「并入 C2 不独立成片」裁量条款一致——本次 PS 打样+富化接线已毕，C3 建议并入收口票或极小独立片，B 同意该裁量）。
5. 门禁机器态记档：B 相=WDAC 拦截相（install 族 4F+隔离 7P 绿）；C 相=允许相（0F）。后续席位以「install 族外 0F」为判据即可，两相均有先例。

SHA256锛堟湰琛屼互涓婂叏鏂団€斺€旇瘎瀹′欢姝ｆ枃锛屼笉鍚湰琛屽強鍏跺墠瀵兼崲琛岋紱澶嶇畻=鍘婚櫎鏈涓庡叾鍓嶅 CRLF 鍚?Get-FileHash -Algorithm SHA256锛? 42050CE99F83164751FD8C30601A9642045A09B2F1B56B8D0EF4DB92C8A54409