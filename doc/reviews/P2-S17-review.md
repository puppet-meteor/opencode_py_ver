# T-B-079 · P2-S17 微评审（席位B 规划评审）——T-C1-066 shell 解析 C3 收口（边缘 fixture 扩充+MINOR-1 处方+parity 矩阵）

- 评审对象：worktree p2-s17 @`57a4bce`（父 `3ad43c2`=#188 检查点=origin/main）；恰 8 路径 272 插入 12 删除（numstat 逐文件亲证见 §2.5）。
- 评审基据：C 席实现票 `T-C1-066.md`（调度器档 sha256 `40db8e7b…711f`，本席 Get-FileHash 复核吻合）票面承诺 vs 实交付逐项对照；`git show 57a4bce` 全量 diff 亲读；Python 探针复核（golden 增量-only 字节级 difflib 验证+15 新条目活解析器一致性+五形反例偏移独立再推导；探针脚本 `C:/Users/lyu/AppData/Local/Temp/opencode/probe-t-b-079/face3_probe2.py`）；B 席独立四门禁复跑（fresh temproot 纪律）。
- 评审人：席位B `ses_orch_B_p2s20`（T-B-079）；2026-11-11；止损 45min 未触；零 git 写（HEAD 恒 `57a4bce`，本评审件为唯一写面）。

## §0 总裁决

**PASS**。计数：**blocker 0 / major 0 / minor 1 / suggestion 2**。七面全 PASS（④ 浮报 10/10 真）。增量-only「零行为变化」主张成立：② S-1 try 域扩展经 ir.py 冻结形状源级论证运行时恒等；① MINOR-1 处方 (a) 钉忠实（union 序优先=label 迁移钉为预期行为）+弃 (b) 理由完备记档；golden 两件字节级增量-only（既有条目唯一 diff=行尾逗号）；新钉 20 计数精确闭合 1585→1605 收集；冻结面 8 路径外零触碰；门禁四件 B 独立复跑全过（install 族 4F=WDAC 相位型机器态——隔离复跑 7P 绿，install 族外 0F，全量读数与 C 席 full-1 完全一致）。MINOR-1（本评审）为进度档一处 −1 行归因措辞不实——文档级瑕疵，非交付缺陷。

## §1 七面判定表

| # | 面 | 判定 | 要点 |
|---|---|---|---|
| 1 | 设计 | PASS | 增量-only 主张核成立：S-1 运行时恒等（frozen IR 元组迭代+纯串属性无抛出路径——ir.py 源级逐行论证）；MINOR-1 (a) 钉忠实处方、弃 (b) 记档完备；bash.py docstring 措辞零改动亲证 |
| 2 | 接线 | PASS | tools/bash.py 恰 S-1 一处 hunk（try 域+4 行注释，+10/−6 全额在该 hunk）；`_deny_label`/`_ADVISORY_TEXT`/`_render`/`_execute`/子进程面零 hunk；powershell.py 恰 docstring 一句 |
| 3 | golden 边界 | PASS | 字节级：每件恰 2 行移除且均=原行+行尾逗号（difflib 探针亲证）；JSON 级既有前缀同一、仅数组尾追加；15 新条目与活解析器全一致（负例四元组探针独立再推导全对、正例经 162P 定向权威面）；五形反例偏移逐一核验 |
| 4 | 测试 | PASS | 新钉 20 计数对账精确（golden 参数化 9+6+两新函数×2+迁移钉）；既有测试零改动零翻转（−1=import 行扩名亲证；1599P=1579P+20P 精确闭合零翻转）；静态钉 `_error` 漏斗覆盖全部 ShellParseError raise-site；`$-` 分歧三处披露 |
| 5 | 冻结 | PASS | `git show --stat` 恰 8 路径 272+/12−；解析器 bash.py/ir.py/__init__.py/policy/process/events/schema/pyproject/uv.lock/fixtures 零触碰；HEAD=57a4bce 净树 |
| 6 | ④ 浮报核实 | 10/10 真 | 逐条证据见 §2.6（含 ⑥ 票面歧义澄清成立、⑨ 日志字节级复核完好） |
| 7 | 门禁 | PASS | 四件独立复跑：1605 collected/1599P+2S+4F @367.99s（4F 全 install 族=WinError 4551 相位型，隔离复跑 7P 绿→install 族外 0F）+ruff clean+format 453+mypy 141-0 |

## §2 逐面证据

### 2.1 设计面

- **② S-1 运行时恒等论证成立（源级亲证）**：`ir.py` 全 dataclass `@dataclass(frozen=True, slots=True)`——`BashCommand.statements: tuple[Statement, ...]`（ir.py:95）、`Statement.segments: tuple[CommandSegment, ...]`（:87）、`CommandSegment.tokens: tuple[Token, ...]`（:75）；`CommandSegment.argv` 属性=`tuple(token.text for token in self.tokens)`（:80）；`Token.text` 属性=isinstance 分支+f-string+`"".join` 纯串操作（:59-68）——迭代与构造面无任何可抛路径（MemoryError 级理论例不构成行为面差异）→ 循环体入 try 域运行时恒等成立。函数 docstring「a parse anomaly (ShellParseError or **any other exception from the parse face**) yields no faces」措辞逐字保留——S-1 后代码向措辞字面对齐（此前循环在 try 外、措辞与代码有字面缝隙，现无）；`except Exception` 不吞 BaseException 不变。
- **① MINOR-1 处方 (a) 钉忠实**：`test_deny_label_union_order_priority_label_migration_edge`（tests/integration/test_tool_bash.py）双断言——raw-only 地板 `filesystem format`（pattern 1 尾前瞻 `(?=\s|\*|$)` 被 `/"` 打断→pattern 3 经 mkfs）+富化后 `recursive root/home deletion`（decoded 面 argv 项 `rm -rf /` 命中更早元组项 pattern 1）。钉法=union 序优先语义（`_deny_label` L182-184：外层 patterns 循环+任一面命中即返）的边缘显式化，与 T-C1-065 优先序条款及 T-B-077 处方 (a)（边缘测试+披露）逐字对应。**raw_only 直算合理性裁定：合理**——`_DENY_HINT_PATTERNS` 元组即 label 族唯一事实源，反事实（raw-only）直算=最小忠实路径（C2 前旧 raw-only 函数已不存在、复制正则集=劣化）；若元组被改序，钉与语义同步移动，钉语义仍成立。docstring 明示「预期行为非缺陷」+两 label 均真述危险内容（advisory-only 不阻断）。
- **弃 (b) 记档完备**：进度档 ①（两遍扫描=改 C2 union 扫描语义=违本票零行为变化性质；序优先与票面优先序条款一致，条款冲突时既定裁决在先；启用须微码面票）+③④-1 双处载明——处方闭环。

### 2.2 接线正确性

- `src/opencode/tools/builtin/bash.py` +10/−6 **恰一 hunk**（`_decoded_scan_surfaces` L152-165）：移入 6 行循环+4 行 S-1 注释；`_deny_label`（L170-186）／`_ADVISORY_TEXT`／`_render`／`_execute`／子进程面 **零 hunk**（diff 全文亲证——单 hunk 构成 numstat 全额）。
- `src/opencode/shellparse/powershell.py` +3/−1 恰模块 docstring 近似清单一句（`` `u{...}`` 不解码、经 ``{`` 达脚本块拒绝）——纯披露面非代码。

### 2.3 golden 增量-only 边界

- **字节级（difflib 探针）**：bash golden 恰 2 行移除（正例末位 `variable-suffix-literal`+反例末位 `case-terminator`），每行=原行+恰一尾逗号重加、无其他移除行；PS golden 同形（`whitespace-only-input`+`second-line-leading-semicolon`）。**JSON 级**：两件 positive/negative 既有前缀逐一同一、新条目仅数组尾追加——bash 30正/19反→34/24（+4正/+5反）、PS 33正/26反→38/27（+5正/+1反），numstat −2×2=该两条逗号行重写，条目内容字节同一。
- **新条目 vs 活解析器（探针+权威面）**：15 条全一致——负例 6 条 code+offset/line/column 四元组逐一精确（探针独立再推导：`echo $"x"`@5=`$`、`cat <>f`@4=`<`、`ls >|f`@3=`>`、`ls |& grep`@4=`|`、`cat <(ls)`@4=`<`、PS `` `u{263A}``@15=`{`——五形反例=「码内拒绝位置」语义正确）；正例 9 条文本级探针比对（`$-`/`x\r`/`a\x00b`/宽字符/4096×a 逐一相符）+冻结 dataclass 相等性权威面（定向 162P）。「期望值全探针生成」主张与观测完全一致（一致性成立即主张实质成立）。
- PS `` `u{...}`` 经 ``{`` 拒绝@offset15 与 powershell.py 新 docstring 句互证。

### 2.4 测试面

- **计数对账**：bash 契约 golden 参数化 ID 58（34+24）−旧 49（30+19）=9+2 新函数（`$-` 钉+源级码族钉）=**+11**；PS 65（38+27）−旧 59（33+26）=6+2（unicode 钉+源级钉）=**+8**；tool_bash +1（迁移钉）→ **+20**，与全量收集 1585→1605 精确闭合；1599P=1579P+20P 零翻转零失踪。
- **既有零改动零翻转**：三测试文件唯一 −1=tests/integration/test_tool_bash.py:95 import 行扩 `_DENY_HINT_PATTERNS` 一名（diff 亲证）；两契约文件 +55/−0、+57/−0 纯追加；golden 既有条目字节同一→既有参数化 ID 零变化。
- **静态钉覆盖面裁定**：`_error_call_code_literals` AST 扫 `self._error(` 调用点首参=字符串常量并断言⊆七码——两解析器**全部 ShellParseError raise-site 均经 `_error` 漏斗**（唯一直接 raise=漏斗本体：shellparse/bash.py:101、powershell.py:147；另各一处 `AssertionError` 内部不变量 bash.py:181/powershell.py:212 系异型异常非码族面）；动态码漂移会被「首参必须常量」断言当场炸出；两 parser 实际使用集=七码全集（⊆ 断言以等号成立）。
- **`$-` 分歧披露三处亲证**：测试 docstring（`test_dollar_dash_stays_literal_divergence_accepted`）+golden 正例 `dollar-dash-literal`+进度档（③④-4+表 B `$` 家族行+表 C）。
- **MINOR-1（本评审）**：进度档 T-C1-066.md:19「既有测试零改动零翻转」句内括注「test_tool_bash.py 的 −1=probe 钉行被永久钉替换+C3 新钉自身」归因不实——−1 实=import 行扩名（票面自述与 diff 双证）；TDD 红相 probe 系席位日志在档的临时运行件（档③「probe 断言 label 稳定→FAIL→证伪后落实永久钉」），未入终树，无从「被替换」。文档级措辞瑕疵，判定面见 §3。

### 2.5 冻结面

- `git show --stat 57a4bce`：恰 8 路径 272+/12−——progress/T-C1-066.md +104/0、powershell.py +3/−1、tools/builtin/bash.py +10/−6、bash golden +11/−2、PS golden +8/−2、test_shellparse_bash.py +55/0、test_shellparse_powershell.py +57/0、test_tool_bash.py +24/−1（numstat 逐项与票面吻合）。
- **冻结清单零触碰亲证**：`shellparse/bash.py`（解析器本体——注意与被改的 `tools/builtin/bash.py` 为两文件）／`ir.py`／`__init__.py`／policy.py／`process/**`／events/registry/schema 面／`tests/contract/fixtures/**`／pyproject／uv.lock——均不在 8 路径内，三票全程零触碰主张成立。HEAD=`57a4bce`、工作树净（评审期零 git 写，仅读侧 git 命令：log/status/show）。

### 2.6 ④ 浮报核实（C 席 10 项逐条真伪）

| # | 项 | 判 | 证据 |
|---|---|---|---|
| 1 | 弃 (b) 记档 | 真 | 进度档①+③④-1，理由完备（见 §2.1） |
| 2 | try 域恒等披露 | 真 | 源级论证成立（§2.1）；两次全量 install 族外读数全同佐证（本席 full-1 与 C 席 full-1 均 1599P+2S） |
| 3 | golden 增量边界 | 真 | 字节级+JSON 级双证（§2.3） |
| 4 | `$-` 三处披露 | 真 | docstring+golden 正例+进度档三处亲证 |
| 5 | bash 同形钉采纳 | 真 | 双端 `test_source_error_calls_stay_within_the_declared_seven` 各一 |
| 6 | 票面歧义澄清 | 成立 | T-C1-065.md:73 交接行原文含混（「PS 面已含 26 反例可再扩五形细分」括注确可误读为 PS 承载）；T-B-076 评审语境=P2-S15-review.md:10 ④「码内 12 项」+L17 建议2 均 bash C1 面；本票 ③ 明文「bash golden 新增：…五形反例」——C 席按票面明文落 bash golden=正确裁读（PS 语法确无此五形：`$"…"`不存在／`<`一律保留符拒／`>|`/`|&`非 PS 形） |
| 7 | mypy cast 修正时序 | 真 | 终树无 cast（isinstance 断言窄化）；typing.cast 单参恒等→修正运行时中性；B 复跑 141-0 一致 |
| 8 | WDAC 相位消散 | 真 | B 独立复现：全量 4F 全 install 族（WinError 4551 CreateProcess=#188 同签名）→隔离复跑 7P 绿 |
| 9 | 席位日志编码事件 | 真 | T-C1-066.txt 2448 字节 UTF-8 解码净、往返同一、BOM 保持——文件完好，控制台显示相主张成立（本席 PowerShell 回显 P2-S16-review.md 同样乱码而 read 工具读净，现象互证） |
| 10 | golden 探针生成 | 真 | 15 条期望值与活解析器行为一致性独立全证（§2.3）——「单一事实源生成」与观测相容 |

### 2.7 门禁面（B 席独立复跑，fresh temproot 纪律）

| 门禁 | B 席读数 | 判 |
|---|---|---|
| `uv run python -m pytest -q`（temproot `t-b-079-full-1`） | **1605 collected / 1599P+2S+4F @367.99s**；4F 全 install 族（WinError 4551） | install 族外 0F ✓；基线闭合 1579P+2S+4F+20P=1599P+2S+4F 精确；与 C 席 full-1 读数逐位一致 |
| install 族隔离复跑（temproot `t-b-079-install-1`） | `tests/integration/test_cli_install.py` **7P @13.44s** | 相位型证实非代码（#188 同款形；与 C 席 full-2／调度器复跑绿同向） |
| `uv run ruff check .` | **clean**（All checks passed!） | ✓ |
| `uv run ruff format --check .` | **453 files already formatted**，exit 0 | =452 基线+进度档 md 落 walk，与预期 453 一致 |
| `uv run python -m mypy src tools/r0 tools/r5 tests/r0 tests/contract tests/integration` | **141 source files, no issues**，exit 0 | 零新 .py，141 不变 ✓ |

定向三件（temproot `t-b-079-targeted-1`）：`test_shellparse_bash.py`+`test_shellparse_powershell.py`+`test_tool_bash.py` **162P @16.19s**——与 C 席定向复跑读数一致，20 新钉 ID 全在其中。

## §3 判定面（强制）

- **计数：blocker 0 / major 0 / minor 1 / suggestion 2。**
- **MINOR-1（T-B-079）**：定位 `doc/orchestration/progress/T-C1-066.md:19`——「test_tool_bash.py 的 −1=probe 钉行被永久钉替换+C3 新钉自身」归因不实：−1 实=import 行扩名（tests/integration/test_tool_bash.py:95，`_ADVISORY_TEXT, _deny_label`→增 `_DENY_HINT_PATTERNS`）；红相 probe 未入终树（席位日志在档）无「替换」事实。**影响**：纯文档措辞，不触任何判定面（票面/numstat/diff 三方对 −1 的正确描述均在），对交付判定零影响。**处方**：后续触该档的席位顺手改一句为「−1=import 行扩名（`_DENY_HINT_PATTERNS`）」即可，无需独立修复票。
- **SUGGESTION-1**：`_error_call_code_literals` helper 于 tests/contract/test_shellparse_bash.py:246-263 与 tests/contract/test_shellparse_powershell.py:255-272 逐字重复（~18 行）——同形钉两文件自含沿 C1/C2「同形测试文件」先例，属可接受惯例；第三 parser 出现时建议升 conftest 共享。**处方**：无需。
- **SUGGESTION-2**：源级码族钉匹配面=`self._error` 漏斗调用点；未来绕漏斗的直接 `raise ShellParseError(...)`（现两解析器零此形，亲证）将逃逸该钉（corpus 钉部分兜底）。**处方**：无需（可加「ShellParseError raise 仅许在 `_error` 体内」AST 断言，留未来微码面票裁量）。
- **总分裁决：PASS**——票面八写面、七评审面、④ 浮报 10 项、四门禁全数兑现；唯一 minor 为文档级措辞瑕疵。

## §4 结论

T-C1-066 以「增量-only 零行为变化」性质精确收口 M2 shell 解析线 C3：MINOR-1 处方 (a) 落钉+弃 (b) 完备记档、S-1 恒等扩展、golden 双件字节级增量（34/24+38/27）、双端源级七码闭集钉、parity 矩阵三表、`$-`/`$_`/`` `u{...}``/CRLF/NUL/宽字符/超长边缘全钉。M2 三票（C1/C2/C3）终态在档，待调度器验收。

SHA256（本行以上全文——评审件正文，不含本行及其前导换行；复算=去除本行与其前导换行并 CRLF→LF 归一后 Get-FileHash -Algorithm SHA256，沿 T-B-077 工序口径） CAA025F04059F6DB54E06573838CB421B38C0D5845451786E5499270D5DEEC8F