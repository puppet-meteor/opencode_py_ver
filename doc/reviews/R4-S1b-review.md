# T-B-037 · R4-S1b（r4-slices 行#35 伴生收尾批）结构化评审报告

> 席位B · 2026-11-05 · 评审对象 worktree r4-s1b `96d60f0` vs `e12e4bb`（main #136） · 结论 **PASS 0/0/0/1**

- **评审对象**：worktree `r4-s1b` B-baseline `96d60f0`（单提交，父=`e12e4bb`=main 检查点 #136）vs 基线 `e12e4bb`；diff 面=恰 10 授权路径，+157/−37，全量 diff 逐文件亲读；TS 锚 `packages/core/src/ripgrep.ts` L223-235 亲读为准。评审纪律：worktree 全程只读、B 零 git 写；唯一写面=本报告；止损 40min 未触。
- **写面对账（1:1）**：`T-C1-032.md` 2± / `T-C1-034.md` +50（新）/ `r3-exit-evidence.md` 2± / `interpreter.py` 2± / `winjob.py` 4± / `bash.py` 8± / `grep.py` 29± / `test_cli_r3.py` 2± / `test_e2e_r3.py` 33± / `test_tool_static_trio.py` +62（纯增）；Σ=194=+157+−37 ✓。面外零写（porcelain 树净 + diff 全集恰 10 路径双证）。
- **门禁复核（B 亲跑）**：collect-only **926**（=924+2 两新测，trio 39=37+2 算术闭合；1.30s）== C 报告 926 对账 ✓；`ruff format --check .` = **"278 files already formatted" exit 0** ✓；mypy 92 files=C 报告对账（src 无新文件，文件数不变自洽）。C 全量门禁（T-C1-034）：926 passed 223.09s / lint clean（ASYNC221 修复后）/ mypy 92 / format 278。
- **结论**：**PASS——放行并入。blocker 0 / major 0 / minor 0 / nit 1**（nit-1 过程·保障级，§4，不阻并入）。三测定向复跑 `-k "hidden or git_directory or locked"` = **5 passed**（0.75s：两新测 + B3 锁测 + 2 既有 hidden 面）。

## §1 十二维逐维（每维附亲证）

1. **grep TS 对齐忠实度 ✓**：TS 锚亲读 L223-235 args 序 = `--no-config → --json → --hidden → --no-messages → [literal→--fixed-strings] → [caseSensitive===false→--ignore-case] → [include→--glob=<pat>] → --glob=!**/.git/**（后置）→ -- → pattern → file ?? "."`。Python args 亲读对拍 = 精确镜像：基线五元组 `[executable, "--no-config", "--json", "--hidden", "--no-messages"]`（`--hidden` 位序=TS L226）+ 可选 `-F/-i/-g` 分立 append + 收尾 `["--glob=!**/.git/**", "-e", pattern, "--", file_arg]`——用户 include glob 前置、.git 排除 glob 后置 = **TS 承载序**。docstring 偏差段改写亲读：hidden 被搜 + .git 被排除 = TS 同款；grep 硬开 vs glob/ls 入参门控非对称 = TS 同款非新偏差（T-B-035 §2 点名⑤ 照录承接，语义保真确认）。parked 注释块 → 对齐记：锚引 ripgrep.ts L223-235 + 裁定 2026-11-05 + r3-slices row#35/args 行 ✓。*维度内观察（不记分）*：docstring 引 "(T-B-035 §2)" 而非对称论断精确出处（R4-S1-review §3 必答② 附带说明）——任务书自身同用 §2 点名⑤ 归属，C 照任务书执行，语义照录无误；精确出处备查见 §3-4。
2. **两回归测试质量 ✓**：断言逐字亲读（`"Found 1 matches\n.hidden.txt:\n  Line 1: needle inside"` 等）。`test_grep_hidden_files_are_searched_after_ts_alignment` = dotfile 跨平台腿 + win32 attrib 腿（`sys.platform` 守卫 + A-PLATFORM 披露 "no attrib on Unix, that leg is simply not exercised there" = Unix 腿未实机跑的披露完整性 ✓）；`test_grep_git_directory_is_excluded_by_glob` = .git 排除钉 + 第二段 `include="*.txt"` 并存序钉（序倒置即红）。`_real_rg_or_skip`+`_patch_rg_path` 真跑模式沿袭（本机 bundled rg 真跑）✓；B 定向复跑实证（头部 5 passed）。
3. **B3 锁测试交互核验 ✓（独立复核）**：locked.txt 非 hidden → 可见性不变 → code 2 容忍路径断言原样（diff 面零触碰锁测）；C 亲核（T-C1-034：locked.txt 从未读到 / 该 tmp 无其他 hidden、无 .git）+ B 定向复跑含锁测双证 ✓。
4. **-g 与 .git glob 并存语义 ✓（双证据）**：探针 D 双序对照（rg 15.2.0 实机：TS 序排除生效 / 倒序 .git 并入）+ 套件第二段序钉（include 并存下 .git 仍排除）；TS 源序三重证据闭合（§2①）。
5. **7 处 L277 去行号化 ✓**：逐位亲读 = interpreter L45 + winjob L1/L15 + bash L8/L74/L233/L287，全为 `§5.3 L277` → `§5.3`（/`§5.3 discipline`）纯注释零行为；全库 src "L277" 复扫 = **0 命中** ✓。裁量复核：bash L1 `§5.3 L276-277` 连写与 L6/L8 `§5.3 L276` 引用不在清单 = L276 语义仍准确，正确保留（L1 连写 → 批三另授权候选，§3-3）。
6. **doc 修正批四处 ✓（最小面）**：r3-exit-evidence §6 L76 双面更正（容忍语义 + 部署形态实锚 = cli.py `--db ./opencode.db` 落 cwd + app.py getcwd，advisory 保留）；T-C1-032 L10 句内双更正 + "T-C1-034 更正"标记；e2e 四注释（L388/508/593/682；三同文+一变体）must → deterministic-environment choice + tolerated since R4-S1；cli L791 Row #28→#33 单词级。每处 diff 恰该句上下文 = 已交付件回改最小面 ✓。
7. **写权限面+覆盖对账 ✓**：10 路径 1:1（见头部）；926=924+2 算术 ✓。
8. **冻结面零改动 ✓（亲证）**：全量 diff 亲读恰 10 路径（面外全集零触碰）；porcelain 树净；tracked `.py` 集 `eb01698..e12e4bb` 零差亲证。src 触碰仅 4 文件且均在授权内（interpreter/winjob/bash=纯注释；grep=授权行为改动）。
9. **门禁复核 ✓（含 +4 注记）**：collect-only 926 == C 报告 926 对账（1.30s）；format "278 files already formatted" exit 0；278 较 r4-s1 期 274 的 +4：tracked `.py` 集两基线间零差 + 树净 → 差量不可能源于评审对象，计入**非追踪环境面**（uv sync 自愈重建环境的盘面漂移；构成注记 = 94 tracked + ~184 非追踪面，盘上非 .venv `.py`=94 恰=tracked 集；pyproject 无 ruff exclude 自定义）。**计数非判据，判据 = exit 0（亲跑）**。
10. **上浮三项处置 ✓（均 concur）**：① Unix 腿 CI 复验归并回流（win32 attrib 腿设计性 skip，Unix dotfile 腿首跑 = 真增量验证面）；② bash L1 连写批三另授权（不混票 = 最小面纪律）；③ 可选 flag append 序未重排 = 最小差量裁量（独立立场见 §2②）。
11. **过程完整性 ✓（含 nit-1）**：23min 单段 + WDAC 自愈（uv sync --python D:/miniconda3，34 包；r1-s8/r4-s1 同形态先例）+ 波折如实记（2×ASYNC221 → 抽 `_set_hidden_attribute` 同步 helper[attrib ±h，check=True，win32-only 调用点]——lint 形状修复，运行时 ~10ms 阻塞在测试上下文可忽略，`_upgrade_head` 先例 + helper docstring 如实披露；format 签名并单行；复跑绿）。无独立留痕/进度无时间戳 = nit-1（§4）。
12. **③漏报抽查 ✓**：文件级对账 = 授权 10 路径外零写（porcelain + diff 双证）；C ③ 门禁自述与 B 复跑对账自洽（926/278/92），⑤ 自述 9 修改 + 进度文件自身第 10 项 = 10 路径对账 ✓，④ 两上浮在册（Unix 腿/bash L1）+ B 侧 append 序裁量项 = 处置面三项全覆盖（§1⑩）；rgprobe.py 探针工件在册（2809B，2026/11/5 11:55:17）= 探针链可复核；未发现漏报。

## §2 必答三项

**① args 相对序承载语义（独立立场）**：rg 多 `--glob` 并存取后置优先（后 glob 覆盖先 glob）。TS L230-231 将用户 include glob 前置、`--glob=!**/.git/**` 后置 = **排除不变量的承载序**——即便用户 include（如 `*.txt`）命中 .git 内文件，后置排除 glob 仍胜出；倒序则 include 重新并入 .git（探针 D 倒序对照实证并入）。Python 镜像同序 = 承载序保真的唯一正确形态。证据闭合：TS 源序（L223-235 亲读）+ 探针 D 双序对照（rg 15.2.0 实机）+ 套件第二段序钉（倒置即红）+ B 定向复跑 5 passed。

**② 可选 flag append 序未重排裁量（concur）**：唯 glob-vs-glob 相对序具语义（precedence 敏感）；`-F/-i/-g` 彼此相对位序 = 模式 flag 零语义（各自独立开关，非优先级耦合）。TS 长形（--fixed-strings/--ignore-case/--glob=）vs Python 短形（-F/-i/-g）系 R4-S1 既有形状先例；argv 全形统一 = 零行为纯化妆 churn，反扩面。边界：若未来引入第二个语义耦合 flag（如另一排除类 glob），须重开相对序审查——现面无此情形。

**③ 23min 过程完整性 + 上浮三项处置**：WDAC 自愈 + 波折如实记 ✓（§1⑪）；三上浮处置均 concur ✓（§1⑩）；T-B-035 nit-2 同类再现（单段无独立留痕）记 nit-1 不降结论——工件链（rgprobe.py 时间戳 + 门禁自洽 + 5 passed 复跑）为过程真实性佐证，无反证。

## §3 处方清单

1. **调度器并回**：r4-s1b → main（10 路径 +157/−37）。并回后更新 `doc/plans/r3-slices.md` §3 台账行#35 状态 + `doc/plans/r4-slices.md` §0.1 行#35 登记；基线传播 **926/92**（pytest/mypy files）+ format exit 0（计数面注记随行）；CI 对齐先例复核（本轮 41963085214 在跑，并回后复验）。
2. **CI 双 OS 复验 = Unix dotfile 腿首跑**：win32 attrib 腿设计性 skip（sys.platform 守卫），Unix CI 仅跑 dotfile 腿——该腿从未 Unix 实机跑过 = 真增量验证面；红则回流本票（探针 A/B/C/D 全 win32 实机，Unix 面依赖 --hidden 语义跨平台成立）。
3. **批三授权候选**：bash L1 `§5.3 L276-277` 连写去行号化（本票正确不混入；L276 语义仍准确，待其漂移再并）。
4. **记录级（零行动）**：grep.py docstring 引 "(T-B-035 §2)" 的对称论断精确出处 = R4-S1-review §3 必答② 附带说明——备查注记，不改（任务书自身同用 §2 点名，C 照录语义无误）。
5. **过程留痕建议（保障级）**：单段任务进度文件附起/止时间戳一行（可与工件链时间戳交叉复核）——回应 nit-1 同类再现。

## §4 nit 清单

- **nit-1（过程·保障级）**：23min 单段任务无独立过程留痕、进度无时间戳——T-B-035 nit-2 同类再现。工件链（rgprobe.py 2026/11/5 11:55:17 + 门禁自洽 + B 定向复跑 5 passed）佐证过程真实性，无反证；不阻并入。建议见 §3-5。


