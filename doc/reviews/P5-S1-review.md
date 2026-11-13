# P5-S1-review — T-B-096 微评审：T-C1-085 生产管线注册面（manifest → engine glue）

- 评审对象：worktree `p5-s1` 单提交 `d842dc0`（父=`9eca0c0`，numstat 恰 10 路径：新 5[registration.py 216L+三测文件 360/131/205L+进度档]+改 5[app.py +11 / cli.py +25 / httpapi/app.py +22/-1 / serving.py +6 / catalog.py +2/-1]——与钉面预期逐路径吻合）；席位票 `T-C1-085.md` 钉面全量；进度档五节证据俱全（红证→绿证→四门禁→install 裁定→warning 记档→终态 DONE）。
- 评审基据（全部独立现读，禁继承）：钉面+进度档+门禁日志（`gates-t085-dispatch.txt`）全文亲读；`git show d842dc0` 全量 diff 亲读；registration.py 216L + 三测文件 696L 全文亲读；engine.py L740-905（EngineCheckpointer/saver/register/bound）+L134、catalog.py（SKILLS_MANIFEST_NAME/S5a 条件规则/docstring）、loader.py L616-628（skills_directory→Path）、cli.py L680-700（asyncio.run(_drive(...)) 调用位）亲证；本席独立复跑四项（见文末附）。
- 评审时刻：2026-11-13 15:55:22（止损 40min 内收口；只读评审，git 写操作零）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 1 / 建议 2（0B/0M 形）

## 六面逐条结论

### 1. 票面纪律 — PASS

- **registration.py 纯面**亲证：模块零 stdout/stderr 写，报告单出口（宿主自有披露）；入口=钉面原形 `async def register_pipelines(engine, skills_dir) -> RegistrationReport`。
- **SkillCatalog 唯一发现门**：`SkillCatalog(root).list()`，manifest 文件名同源常量 `SKILLS_MANIFEST_NAME`（catalog.py:62）——零第二套解析、零 drift；`SkillDiscoveryError`→manifest_error 不上抛。
- **langgraph-only 开文件**：`!= "langgraph"` 计数跳过；单测以「声明体不存在盘上」证不读（任何 open 必落 rejection 行而零出现，`test_default_entries_are_skipped_without_opening_any_file`）。
- **containment**：词法→realpath 双侧 resolve + `is_relative_to`，逃逸=`PipelinePathEscape` 先于任何读拒绝（机制核见第 2 面）。
- **逐条目拒绝不中断**：per-entry 捕获+测试钉住（broken 条目后 healthy 条目仍注册）。
- **报告形**：具名 dataclass 四面（registered/rejected/manifest_error/non_langgraph），registered 行=skill_id+spec 自身 pipeline_id+spec_hash。
- **同一 loop 硬约束**：协程 await 于宿主各自 loop（定谳见第 4 面）。
- **stderr 披露三形全实现**、前缀分宿主（`opencode: ` / `opencode-http: `，MCP ADR-0002 先例）：目录/manifest 缺失=静默零态；坏 manifest=恰一行；条目拒=恰一行（点名 skill id+pipeline 串+错误类）；stdout 恒零写。
- **catalog.py 微授权**：陈锚一句改指（wrap 2 行，定谳 3），零行为改动。
- **app.py/httpapi/app.py 触碰=预授权条款履行**：进度档两处记档（15:04 设计段「此处记档」+15:28 绿灯段「记档于此」）；确需性成立——ASGI lifespan startup 面只存在于 `httpapi/app.py::_lifespan_for`，skills_dir 承载需 `App` 面；**CLI 的 App 不携带 root**（cli.py:695 传参位=`asyncio.run(_drive(...))` 调用位而非 App 构造——L680-697 亲读证），故无双注册面。
- **披露面**：P5-HANDOFF.md 不在 worktree 已如实记档（#212 声明票面自含）；既有测试 test_skills_manifest_langgraph.py L16 陈旧 S5b 标签零触碰（零回改纪律遵守，披露在案）。

### 2. 机制与选型 — PASS（含次要 1）

- **承接族完备性**：票面五类亲证皆 `(OSError, ValueError)` 族内——`PipelineSpecRejected(ValueError)` @engine.py:134、`PydagImportError(ValueError)` @pydag_import.py:238、`JSONDecodeError⊂ValueError`（stdlib）、pydantic `ValidationError⊂ValueError`；另覆盖 OSError 读面、UnicodeDecodeError 读面、PipelinePathEscape 逃逸面。逐类拒绝构造测试俱在（FileNotFoundError/JSONDecodeError/ValidationError[参数化×3]/PydagImportError/PipelineSpecRejected 各有落点）。
- **过宽面有界**：TypeError/KeyError/AttributeError 不在族内=穿透（若 catalog 契约破坏致 langgraph 条目 pipeline=None，`cast` 后 TypeError 上浮崩溃而非静默吞——响亮失败形）；被族吞的 ValueError 面全部是文档化契约面（json/UTF-8 解析、pydantic 结构层、引擎三层门）；`error_type=type(error).__name__` 保精确类名可诊。
- **cast 安全根基**：catalog S5a 条件规则亲证（catalog.py:135-140/179-180：langgraph→pipeline 必填、default 不得携）。
- **containment realpath 语义**：根与目标均 resolve（Windows 解析 junction/symlink 至最终路径）后 `is_relative_to`；逃逸判于任何读之前（单测以盘上存在可注册 spec 的外部文件证拒绝必来自 containment 非读失败）；不可解析→OSError→保守判逃逸。**四形覆盖核：绝对路径✓（另有遍历=钉面外补测✓）；junction/symlink/不可解析三形零测试——缺口见次要 1。**
- **静默零态 vs manifest_error 区分**：`_manifest_present`（root.is_dir() and manifest.is_file()）前检先于 SkillCatalog 构造——存在性=零报告零错误、存在而坏=SkillDiscoveryError→manifest_error；三单测（缺目录/缺 manifest/坏 manifest）+两宿主各一静默形钉住。

### 3. 零断言弱化 — PASS

- 提交零触碰既有测试文件（numstat 无任何既有 test 路径）。
- v1 哈希钉测 `test_pipelines.py:372`（`a881d3f4…8772`）不动（文件不在提交内）且绿（席位/调度器两全量跑零非 install 族失败）。
- collect 2232=2207+25 恰闭合（本席独立 collect 亲证 2232，见附）。

### 4. 可注入性 TDD（same-loop 承载形定谳）— PASS

- **三层齐备**：①Protocol 面——`RegistrationEngine` 结构协议仅 `register`（fake 只需一面），`_RecordingEngine` 录参+注入失败队列，契约单测 15；②真引擎集成 4——v1+`pydag-editable-graph-v1` 双生产来源各一（bound 非 None+spec_hash==compute_spec_hash 一致）、拒绝 fixture→bound None+NotFound 面保持+恰一报告行、e2e-lite 提交→驱动→done；③两宿主组合 6（CLI×3+ASGI×3，注入形沿 test_cli_r4/test_httpapi_server 模式）。
- **same-loop 承载形独立推理**：`engine.bound()` 为纯内存 dict 读（engine.py:881-883）——spy 同协程回合 bound() 回读证明的是**绑定存在**，不自身证明 loop 同一性；loop 同一性证据三足：（a）**结构性**——`register_pipelines` await 于 `_drive` 首段（cli.py:900，`asyncio.run` 的 loop 生命周期内）/lifespan startup 段（serving loop），「组合根临时 asyncio.run 包裹注册后换 loop」的违禁形在结构上不可能存在；（b）**行为性**——spy 于同步 `main()` 期间触发+boot 完成收口（注册确发生且完成于该 loop）；ASGI `after_startup` 于 serving loop 内回读非 None；（c）**aiosqlite 实证**——`register` 经 `EngineCheckpointer.saver()`（engine.py:775 `aiosqlite.connect`）于调用方 loop 建连，e2e-lite 同一测试协程（=同一 loop）内 register→submit→run→done 经 checkpointer 服务闭环——跨 loop 连接必红。**结论：非同协程巧合**——断言+结构共同钉住「注册与后续驱动同 loop」。诚实边界：CLI 宿主测试本身未驱动引擎 job（回答走 agent 路），CLI 侧「后续驱动同 loop」由结构+spy 承载、行为面由集成测承载——对钉面红线（违禁组合根形态排除）充分。

### 5. 门禁算术 — PASS

逐值比对（席位 vs 调度器 vs 本席复跑）：

- **collect**：2232=2207+25 ✓（本席独立 collect 2232 ✓ 三证闭合）。
- **25 新测=15（契约）+4（真引擎）+6（两宿主）** 文件级对账 ✓；本席隔离复跑 **25 passed in 10.72s 零 warning** ✓（复现席位宣称）。
- **ruff**：席位 0 = 调度器 RUFF-EXIT 0 ✓。
- **format**：席位 542 exit0 = 调度器 542 FORMAT-EXIT 0 ✓（计数漂移=未跟踪 md，判定面恒为 exit 0）。
- **mypy**：席位 200-0（子集 `src tools/r0 tests/r0 tests/contract tests/integration`——恰票面钉数 200）vs 调度器 204-0。**差 4=恰 tools/pty0(2 py)+tools/r5(2 py)**（tools 树亲证；tests 树唯 contract/integration/r0 三目录，席位子集已全覆盖 tests 面）；本票 numstat 零触碰该两目录；本席以「席位子集+pty0+r5」复跑=**Success: no issues found in 204 source files** ✓——差值恰闭合、无遮蔽、受影响面=无（验证形完结）。
- **pytest**：席位 2223P+8S+1F/560.69s vs 调度器 2219P+8S+5F+2 warnings/585.77s——合计皆 2232 ✓；两跑失败**全数 install 族**（test_cli_install.py）；本席文件级隔离复跑=5F/3P，5 败全数 `OSError [WinError 4551]` @ `_winapi.CreateProcess`（subprocess.py:1538）spawn 面、失败集合与调度器逐测相同——「隔离重跑仍 4551」判据本席独立满足；非 spawn 失败模式三跑皆缺失；**对照测** `test_installed_cli_missing_maas_config_is_an_argparse_exit_2`（永不入 _drive/注册面）同 signature 失败——注册面因果排除；席位 8P/1F 窗口与本席 5F/调度器 5F=环境协议已记的 WDAC 间歇相位（「历史账本常为 5F 相位」）；零非 install 族失败双全量证 → **非回归**，CI 终证归调度器。

### 6. 冻结面 — PASS

- engine.py / contracts/* / pyproject / uv.lock / schema-lock 面**零触碰**（numstat 亲证）。
- catalog.py 行为零改动：+2/-1 全落 docstring 块内（定谳 3）。
- **serving 宿主链无共享工厂提取**：registration.py=领域面（纯注册+报告）；两宿主各自 compose——CLI：main 取 `loader.skills_directory()`→`_drive` 新参→_drive 首段注册+`opencode: ` 披露；HTTP：serving 取同 loader 面→`App(skills_dir=...)`→`_lifespan_for` startup 段注册+`opencode-http: ` 披露；等构而非提取——082 frozen-face 纪律延续 ✓。
- **App skills_dir 可选参 additive 形**：默认 None；None 语义=无注册面（lifespan `if app.pipeline_engine is not None and app.skills_dir is not None` 跳过）；既有组合零受扰（纯新增 keyword 参）。
- base_url/api_key/model 等 host-only 纪律面不触 ✓（diff 亲证）。

## 四焦点定谳

1. **承接族 widening（票面五类→(OSError, ValueError) 整族）→ LEGITIMATE**：(OSError, ValueError)=票面五类的**族闭包**（五类亲证皆族内）；widening 增量恰为读面 UnicodeDecodeError——无此则单个非 UTF-8 graph.json 崩整 boot、违「逐条目拒绝不中断」纪律；过宽面有界（TypeError/KeyError 穿透、被吞面皆契约面、error_type 保精确类名可诊）；progress 记档披露在案。
2. **id 语义+重复 pipeline_id new-binding → LEGITIMATE**：引擎按 spec 自身 pipeline_id 绑定（engine.py:878 dict 赋值亲证=新绑定/hash 锁既有语义）；报告行携 spec pipeline_id+spec_hash、manifest id 仅路由元数据不强等（单测 `bound("code-review") is None` / `bound("digest") is not None` 钉死）；重复绑定如实双行记录+终态=后注册行（单测 spec_hash 异+终态哈希=后行）+真引擎 bound 哈希==compute_spec_hash(注册 spec) 闭环——与引擎既有语义一致。
3. **catalog.py docstring wrap 2 行（120 列）→ LEGITIMATE**：授权为**一句陈锚**（S5b→P5-S1 注册面）的语义面；120 列 format 门强制物理 wrap（ruff format exit0 亲证）；+2/-1=一句两行；「恰一行」按语义单元履行，行数漂移已记档披露；零行为改动。
4. **尾部 2× aiosqlite event-loop-closed warning 归因 → LEGITIMATE**（三方证据汇聚指向既有现象）：①三新文件隔离=25P 零 warning（席位宣称+本席复现）；②调度器全量 2 warnings 归属**既有测试** `test_httpapi_server.py::test_second_app_on_the_same_data_dir_refuses_the_writer_lock`（本票零触碰该文件）；③**前 commit 证据** `gates-161-closing.txt`（2026-11-07 @ f71c31c，早于本票六日）全量即有同一 aiosqlite `_connection_worker_thread`「Event loop is closed」PytestUnhandledThreadExceptionWarning（彼时归属 test_approval.py）——现象先在、归属随全量序漂移。席位「未定谳归因，如实记档」升级为汇聚指向既有现象；绝对终证（全量序稳定性）归 CI。

## 发现明细

- **次要 1（containment 四形覆盖缺口）**：评审钉面四形中 junction/symlink/不可解析路径**三形零测试**（已测：绝对路径✓+遍历[钉面外补测]✓）。代码路径存在且由 Python resolve 语义正确覆盖（junction/symlink 解析至最终路径后 is_relative_to 必拒；不可解析→OSError→保守判逃逸），但 Windows junction 免提权可构造（`mklink /J`），安全相关 realpath 面宜有直接钉测。非阻断：机制亲读正确+绝对/遍历两形已钉住 resolve-比对主路径。建议随 R4 管线枚举票或独立小票补三形。
- **建议 1（宿主面条目拒披露行零执行）**：两宿主 rejection print 循环（每条目恰一行 stderr）无任何测试执行过——宿主三形测试恰覆盖票面验收清单所列三形（清单本身未列条目拒形，席位履约无缺）；报告行面由契约单测钉住、属性面由 mypy 钉住，残余风险=纯 print 循环笔误类。建议后续补一宿主级「有效 manifest+坏条目→boot 继续+恰一行 stderr」形。
- **建议 2（UnicodeDecodeError 读面零专测）**：承接族 widening 新覆盖的读面错误类无非 UTF-8 graph.json 专测（票面五类各有专测）。族内捕获已覆盖之，建议同建议 1 一并补形。

## 附：本席独立复跑记录（worktree p5-s1 @ d842dc0，temproot `pytest-tmp/p5-s1-b096`）

- 隔离三新文件：`25 passed in 10.72s`，零 warning。
- collect-only：`2232 tests collected in 2.02s`。
- mypy（席位子集+tools/pty0+tools/r5）：`Success: no issues found in 204 source files`。
- test_cli_install.py 文件级隔离：`5 failed, 3 passed`——5 败全数 `OSError [WinError 4551]` @ `_winapi.CreateProcess`（spawn 面），失败集合与调度器逐测相同（4551 相位判据独立满足）。
sha256=ceba074dc874ff1642d2c9d060a2c1c8e08f77d55f88b983a4dd00b6675c2d63 (Get-FileHash -Algorithm SHA256; file content before this line, byte count 14340, dispatcher truncates this line for verification)
