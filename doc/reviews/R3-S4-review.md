# R3-S4 结构化审查（席位B · T-B-030）

> 审查对象：worktree r3-s4 **`5828d2f`**（T-C1-028 交付，7 files +1914/−20）vs 基线 **`8c3ef79`**（#113，分叉点）。diff 取法 `git -C <worktree> diff 8c3ef79 5828d2f`（只读）。
> 审查人：席位B（T-B-030），止损 40min 内完成。写权限：仅本报告（新增单文件）；git 零写；worktree 零写入。
> 门禁基线：调度器已验收（15:05 PASS），任务书免复跑——pytest **820**（=780+40）/ ruff 双绿 / format 240 exit 0 / mypy **79**（=76+3）。B 独立校验基线数学：780=703+33(S2)+44(S3) ✓；820−780=40=本切片净增测数（36 集成+4 契约）✓。
> 调度器已验项（直接引用、未重复执行）：四门禁数字、④四项裁定（全 concur 席位立场）。**必答 1 为 B 独立复核，非背书。**
> B 独立复核面：git 对账五连亲跑（stat / name-only / log / status --porcelain / 冻结面 diff，§2 实录）+ 进度 T-C1-028.md ①-⑤ 全文 + write.py 263L / edit.py 496L 全文 + write.ts 101L / edit.ts 332L 全文（TS 对拍基础）+ CORE-CONTRACTS §5.3 L268-280 直读（L274 直锚、L275 竞态免责；§5.2 L266/§2.1 L47/§7 系列沿 S2/S3 审查累计亲证）+ r3-slices §2.4（L75-83）+ 台账行#27（L154）/行#29（L156）+ 36 集成测名全清单（Select-String）与进度②逐一映射 + 测试体三段亲读（L515-600 / L650-740 / L795-896）+ test_tool_listing.py / cli.py / builtin\_\_init\_\_.py diff 全文。
> 实现过程披露（评审面=两段合计的最终树）：主跑 14:09-14:45 收官三件缺失（后台 pytest 通知丢失）→同会话续行 14:48-14:52 补齐（台账 14:48 条目）。

## 结论

**PASS**（blocker 0 / major 0 / minor 0 / **nit 3**）+ **开放点 1**（登记不计数）。

R3-S4 与 spec（r3-slices §2.4）和契约逐项对齐：§5.3 L274 直锚兑现——edit 的出现分析针对读到的版本，零/歧义匹配干净失败零落盘，接受后经同目录临时文件 `os.replace` 原子落盘（复用 write.py `_atomic_replace` 单一纪律）；write 以量化字节上限+UTF-8 门延续 L272 先例；创建目标根边界（点名①）以最深现存祖先 strict-realpath+尾段纯组件成立，残余 TOCTOU 落 L275 竞态免责；matchView（点名②）`bisect_left` 严格在前双边界与 edit.ts（L141-146）一致；算法核逐行对拍零行为偏差（28 项归一表/三层互斥/LCS/精确字节路径/倒放拼接），四项不移植（BOM/formatter/lock/external_directory）与 TS 全文对账无遗漏面；unchanged 族（点名③）非 endingsOnly 变体可达已测（#22）、endingsOnly 变体结构不可达（nit-3 登记级）；失败面四档与 edit.ts 措辞逐字一致（唯一包装措辞偏差见 nit-1）；审批面 E2E 双向（allow 落盘 / deny 零触）+默认 ask 双钉（#14 直面 R2 默认、#40 evaluate）；中断注入 ×3 证旧字节完好+临时零残留；进度如实（两段过程+3 真错+7 风格修复全入账，唯 ⑤ 措辞 nit-2）。无 m 级以上发现；nit 三项与开放点均为登记级，处方见 §5。

## 1. 审查维度逐条（12）

**维 1 · 变更面对账+写权限面** ✓：恰 7 路径=授权清单精确一致（新增 write.py 263L / edit.py 496L / test_tool_write_edit.py 896L / T-C1-028.md 111L；修改 builtin\_\_init\_\_.py / cli.py / test_tool_listing.py，合计 +1914/−20）——**无未披露文件、无越权路径**（name-only 穷尽亲证，§2）；单提交 `5828d2f`（log 亲证）；树净（status 空）；冻结面零差量（§2）。

**维 2 ·【点名复核①】创建目标根边界方案** ✓（write.py 全文亲读）：`_resolve_write_target` 对"目标可不存在、父链可缺失"的写场景取**最深现存祖先 strict realpath + 尾段纯组件拼回**——现存段的越界 symlink/junction 全被 strict 解析捕获（逃逸面闭合），不存在的尾段尚无 symlink 可藏（纯组件语义）；残余 TOCTOU（解析后、创建前尾段被替换为越界 symlink）为窗口极窄的并发竞态，落 §5.3 L275 竞态免责之内（L275 本会话直读）。目录目标拒绝为 UNSUPPORTED_CAPABILITY "Path is a directory, not a file: {path}"。方案成立，无需改。

**维 3 · 契约符合性（§5.3 L274 直锚）** ✓：冲突检测=对读到的内存版本做匹配分析，零匹配/非 replaceAll 歧义匹配→VALIDATION_ERROR 干净失败、字节零落盘（#17-20 族断言）；原子替换=同目录 `mkstemp` 临时文件+fsync+`os.replace`（`_atomic_replace` 单点，edit.py 复用不自建第二路径）；L272 量化先例延续：WRITE_MAX_BYTES 上限报文 "content is {N+1} bytes, over the {N} byte write limit"（#10 断言亲读）；L275 防误用（非 UTF-8 拒绝 TOOL_FAILED）+竞态免责对齐；§5.2 L266 默认表 fs\_\_write/fs\_\_edit=ask 由 #40（PermissionPolicy.evaluate → (ask,"default")）钉定；审批挂点/终态/事件面沿 §5.1 L255-261 与 §2.1 L47 既有形态（S2/S3 累计亲证），无新偏差。

**维 4 ·【点名复核②】matchView 视图回映** ✓（edit.py `_match_view` 亲读 vs edit.ts L141-146）：LF 视图上算得的匹配偏移经 removed-\r 位点表 + `bisect_left` 严格在前计数映射回原字节偏移——双边界均正确：匹配起点恰在 \r 上（同位不计）与起点前有 \r（计入）语义与 TS 一致；保偏移归一（28 项 1:1 表）保证视图/原文偏移差不因多字符替换漂移。#24-27 族断言回映正确性。成立。

**维 5 · TS 对拍·移植保真** ✓（edit.py/edit.ts 全文逐行）：`normalizeForMatch`（str.translate 28 项 1:1 表，保偏移）、`findOccurrences`（非重叠精确跨度）、`findLineOccurrences`（尾随空白容忍整行跨度+重叠去重，edit.ts L66/L84-89）、三层互斥（exact or unicode or trailing）、`lineEnding`/`_with_region_endings`/`_align_lines`（LCS 复用匹配区行尾）、endingsOnly 精确字节路径（edit.ts L211-214）、多匹配倒放拼接。四项不移植（BOM/formatter/lock/external_directory）与 TS 全文对账**无遗漏行为面**——external_directory 属 S4 范围外（仅工作目录），BOM/formatter/lock 属宿主层非工具叶。唯一措辞偏差=nit-1。

**维 6 · 失败面映射** ✓：identical / empty-oldString / zero-match / multi-match / unchanged → VALIDATION_ERROR（措辞逐字 edit.ts）；missing → NOT_FOUND "File not found: {path}"；directory → UNSUPPORTED_CAPABILITY "Path is a directory, not a file: {path}"；invalid UTF-8 / over-limit / OSError → TOOL_FAILED。四档与 TS 源对账，除包装措辞（nit-1）外零偏差。

**维 7 ·【点名复核③】unchanged 报告族** ✓：非 endingsOnly 变体可达且已测（#22，测试体亲读：newString==old 推导路径→unchanged 报告+零落盘）；endingsOnly-unchanged 变体在 TS 与 Python **均结构不可达**（构造该报告需 old/new 无差异判定，而 endingsOnly 路径以"仅 \r 差异"为前提，两谓词互斥）——edit.py 代码注释已披露（"(④ disclosure)"）但进度 ③ 四档未列 → **nit-3**（登记级，见 §5）。族内其余面（报告措辞/批内状态/审计）与 TS 对账一致。

**维 8 · 测试质量** ✓：36 集成测名（Select-String 全清单）与进度②逐一对应；+4 契约新测（#37-40，体读：#40 钉 evaluate→(ask,"default")）+六件套扩展（目录清单+重复拒绝 3→5，diff 亲读）。断言强度足够：字节级文件内容、终态机、权限面（decision/资源对）、`_temp_leftovers == []`、事件序批配对。⑤"既有 8 测原名原断言"两处断言必然扩展 → nit-2（登记级）。

**维 9 · 审批面** ✓（测试体三段亲读）：#35 once-allow E2E——[replied, started] batch_id 等值+started permission_decision "allow"+批复后落盘（requested action "fs\_\_write"，resources ["out.txt"]）；#36 拒绝→(denied, not_started)+PERMISSION_DENIED+零 started+文件字节未动；#14 直注册面=R2 默认放行（policy=None，S2 既定）；#40 钉默认表 ask。policy.py 冻结零差量（§2）→审批引擎面零漂移。

**维 10 · 中断/原子性** ✓（#12/#13/#34 体读）：monkeypatch `os.replace`/mkstemp/fsync 失败点注入 → 旧字节完好+`_temp_leftovers == []`+state "failed"+TOOL_FAILED——原子性承诺（永无半写文件）阳控成立；Windows FILE_SHARE_DELETE 面如实入 ③ 未测防御档（诚实披露，环境特定面不虚构覆盖）。

**维 11 · 进度如实性** ✓（T-C1-028.md ①-⑤ 全文）：两段过程如实记档（段界+台账 14:48 条目）；3 真实错误自修复+7 ruff 风格修复全入账；④披露档与代码注释交叉自证（唯 nit-2/nit-3 两处措辞级出入，见 §5）；③四档浮报完整性独立清点见必答 2。

**维 12 · 门禁/基线** ✓：调度器四门禁数字直接引用（任务书免复跑）；B 独立校验 780=703+33(S2)+44(S3) ✓、820=780+40（净增 40=36+4 恰合）✓、mypy 79=76+3（两新模块+进度文件）✓；必答三项见 §3。

## 2. ③漏报抽查结果（B 义务亲跑实录，本会话只读）

- `git diff 8c3ef79 5828d2f --stat` → 7 files changed, **1914 insertions(+), 20 deletions(-)**。
- `git diff 8c3ef79 5828d2f --name-only` → 恰授权 7 路径逐一相符，**无多余路径**（含 test_tool_static_trio.py 等冻结测试零触碰的穷尽证明）。
- `git log --oneline 8c3ef79..5828d2f` → 单提交 `5828d2f`（"S4 B-baseline: fs\_\_write + fs\_\_edit (T-C1-028) - exact conflict detection + atomic replace"）。
- `git status --porcelain` → **空**（树净，无未提交残留）。
- 冻结面 diff（`-- src/opencode/contracts/ src/opencode/tools/policy.py src/opencode/tools/invoker.py src/opencode/tools/registry.py src/opencode/session/service.py src/opencode/app.py src/opencode/tools/builtin/read.py ls.py glob.py grep.py`）→ **空**（审批引擎/读族/契约面零差量）。
- **结论：文件级零漏报**；内容级与进度①-⑤对账仅 nit-2/nit-3 两处措辞级出入（§5），无实质漏测、无未披露行为面。

## 3. 必答三项（B 独立意见，给据）

| # | 项 | B 意见 | 依据 |
|---|---|---|---|
| 必答1 | ④四裁定独立复核 | **四项全 concur** | ④-1 temp 清扫→行#27：进程内纪律独立核验（write.py 唯一 mkstemp 创建点+失败路径清扫，全文亲读；#12/#13/#34 均断言 `_temp_leftovers==[]`），跨进程/崩溃残留的宿主级清扫本质在工具作用域外→恢复切片行#27（含 ④-1 扩展条款）为正确载体，本切片零缺口。④-2/④-3 重启条件→行#29：E2E #35/#36 仅覆盖会话内审批生命周期；重启面既有承接=S2 boot_id 机器（saved 审计休眠/当前 boot 激活，T-B-028 维 3 亲证）；write.py/edit.py 全文零重启语义承载、未新增声明；残余重启条件归行#29 条款化正确。④-4 留原样：cli.py 净增 11 行（stat 亲证）→冻结测试 docstring "cli.py L276" 行锚机械漂移；漂移源在授权改动内、锚物在冻结面（name-only 穷尽证其零触碰）——修锚即越冻结面写权限；docstring 级零功能影响；先例=S2 events.py L333（T-B-028 裁定b concur）。 |
| 必答2 | ③四档浮报完整性独立清点 | **无实质漏测；两处登记级出入=评审发现项** | 四档逐档对照代码+测试+冻结面：已测档与 36+4 测名逐一对应 ✓；未测防御档含 FILE_SHARE_DELETE 如实披露 ✓；不移植档四项与 TS 全文对账无遗漏 ✓；披露档缺口=nit-3（endingsOnly-unchanged 结构不可达：代码注释披露但四档未列）+nit-2（⑤"8 测原断言"两处必然扩展）；冻结面零差量（§2 git 亲证）排除未披露代码面。两项均记档处置、非返工。 |
| 必答3 | 中断+续行过程完整性 | **过程完整无瑕疵** | 终树对账：单提交+树净+恰 7 路径+无孤儿文件（§2 五连）；进度如实记段：主跑收官三件缺失系后台 pytest 通知丢失（环境面非纪律面）→同会话续行补齐+台账 14:48 条目留痕；3 真错+7 风格修复全入账；两段合计树与单次交付树在 git 面不可区分（单提交自洽）→无过程缺陷，无需处置。 |

## 4. 点名复核三项小结

- **点名①（创建目标根边界）成立**：最深现存祖先 strict-realpath+尾段纯组件；逃逸面在现存段闭合，残余竞态落 L275 免责（维 2）。
- **点名②（matchView 回映）成立**：bisect_left 严格在前双边界与 edit.ts L141-146 一致，保偏移归一无漂移（维 4）。
- **点名③（unchanged 报告族）成立**：非 endingsOnly 可达已测 #22；endingsOnly 变体结构不可达（双实现一致）——代码注释已披露、③四档缺列为 nit-3 登记级（维 7）。

## 5. 发现清单与处方

**nit-1 · TOOL_FAILED 包装措辞未披露偏差**：write/edit OSError 包装 "Cannot write/edit {path}: {error}" vs TS "Unable to write/edit"——语义等价（失败档/原因链保真），纯措辞面，但进度 ④ 披露档未列此偏差；集成测试已 pin "Cannot"（体读）。**处方**：台账记措辞统一项一行（调度器裁量挂行#29 或新行），后续触达 write.py/edit.py 的切片顺手对齐或维持 Python 措辞并在④补记——非本切片返工（改动需连动测试断言，收益不成比例）。

**nit-2 · 进度 ⑤/①-3 "既有 8 测原名原断言"不精确**：六件套扩展使 2 测断言必然扩展——test_tool_listing 目录清单断言纳入 write/edit、重复拒绝循环 3→5 适配器（diff 亲读）；测名未改属实，"原断言"不成立。**处方**：进度文件一行更正（调度器提交时顺手）或台账备注；不阻塞。

**nit-3 · ③四档缺 endingsOnly-unchanged 一行**：该分支结构不可达（§4-③），代码注释披露"(④ disclosure)"而进度 ③ 四档未列——浮报完整性缺口=本评审发现项（必答 2 义务产出）。**处方**：③四档补记一行（载体：进度文件随手更正或恢复切片触达时补记）；不阻塞。

**开放点（登记不计数）· 悬空 symlink 写目标拒绝面**：write 对指向不存在目标的悬空 symlink 拒为 NOT_FOUND（保守安全面）。TS 对应语义未对拍——file-access.ts 不在本移植指纹面（write.ts/edit.ts 全文亲证其依赖 FileAccess，但指纹义务不含该文件）。**处置**：无行动需求，仅本报告登记；若未来 file-access.ts 入指纹面，补一次对拍即可。

## 6. 建议

- **按现状合并（PASS）**：零 blocker/major/minor；nit 三项+开放点均为登记级，无返工需求。
- 处方载体：nit-1 → 台账措辞统一行；nit-2 → 进度文件更正一行；nit-3 → ③四档补记一行；开放点仅存本报告（义务面外，不进台账）。
- S5（bash+受控子进程，r3-slices §2.5）可开工，S4 无遗留阻塞；既有前瞻项承接不变——行#27（temp 清扫+恢复三条款，含 ④-1 扩展）与行#29（④-2/④-3 重启条件）待恢复切片任务书条款化，S5 任务书已收编本席 S3 m-1（rg 超时收编）。
