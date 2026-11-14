# P5-S6-review — T-C1-090 微评审：tier 表 config-ification（pipeline.tiers 分层配置键+生产装配传参）

- 评审对象：worktree `p5-s6` 席位工作区（未提交态，席位 git 写零）：改 6[doc/CORE-CONTRACTS.md +3/−0、src/opencode/cli.py +20/−6、src/opencode/config/loader.py +63/−8、src/opencode/httpapi/serving.py +15/−8、tests/integration/test_config_loader.py +74/−0、tests/integration/test_llm_call_e2e.py +107/−15] 共 **+298/−21**——与票面 §8 交付面逐路径吻合零多零漏；票面拷贝 tasks/T-C1-090.md 正确保持未跟踪（#180 B-②）；新 1=进度档 `doc/orchestration/progress/T-C1-090.md`（13.9KB，20:57:50 写毕=SEAT-EXIT 前——089 教训「前台收齐」已内化，无延迟落盘相竞）。
- 评审基据（全部独立现读，禁继承）：六文件 `git diff` 全量逐行亲读（源面+测试面两批）；席位日志 `logs/T-C1-090.txt`（56.8KB）终报亲读；进度档全文亲读（改动面/测试面/决策锚映射/披露余量/门禁输出/诚实记档的门禁迭代弧：E501+RUF043→0、format 同行→0、mypy var-annotated→0）；本席独立复跑四门禁（worktree 真 python venv，`uv run --no-sync` + `-m` 形，§4 面记独立结果）。
- 评审时刻：2026-11-14 21:05（席位弧 ~20:2x 发射→20:58:29 SEAT-EXIT 0 ≈30min，止损 100min 内；席位 git 写零，提交/合流/台账归调度器）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 4（全观察面，无返工；建议 4=SAC 窗口观察项，见 §4/发现明细）

## 六面逐条结论

### 1. 票面纪律 — PASS
- 禁 git 写遵守：席位全程 HEAD 恒 `e50da2e`（调度器独立核：终态工作区=6 改未提交+票面拷贝+进度档三件未跟踪）；交付=工作区。
- 票面 tasks/*.md 未提交（工作区亲证零 tasks 路径入 diff）；诚实纪律遵守——门禁迭代弧全记档（首跑 2 ruff error/1 format/1 mypy 各自修后收敛，无假绿）。
- 进度档 20:57:50 写毕先于 SEAT-EXIT（20:58:29）——089「长门禁禁后台化+前台收齐」纪律执行到位，全量 608.67s 前台收束后成文，无延迟落盘相竞面。

### 2. 决策锚落面（§3 九锚逐条）— PASS
- 锚1 键位形 ✓：顶层 `pipeline` 对象键+恰一子键 `tiers`（`_PIPELINE_KEYS = frozenset({"tiers"})`，`_PERMISSION_KEYS` 嵌套守卫先例同构）；docstring 钉「pipeline 命名空间为后续 pipeline 配置面预留生长」。
- 锚2 白名单两面 ✓：`_RUN_KEYS` 增 `"pipeline"`（九→十键）；`HOST_ONLY_KEYS` 不加——注释明钉「model selection is not a trust extension, legal in every layer, host-only never」；嵌套守卫分支在 `_validate_layer_keys` 内 permission 分支后，host_tier 两态同校（产品/全局/项目各层均合法）。
- 锚3 值验证 ✓：pipeline 必须对象；未知子键 ConfigError 点名 `'pipeline.<key>'`（消息形与顶层 unknown-field 同构）；`pipeline.tiers` 必须对象（**空对象合法=未声明形**——仅形状失败）；tier 名与 model id 值皆非空字符串、消息点名路径 `'pipeline.tiers.<name>'`。
- 锚4 合并语义 ✓：零新合并代码——reader 直取 `self.load().values.get("pipeline")`，`_merge_json` 映射递归合并原生受益（外层声明、内层补全或覆盖同 tier 名）；`load()` 全层入层时经 `_validate_layer_keys` shape-checked（docstring 钉面）。
- 锚5 reader 形 ✓：`ConfigLoader.pipeline_tiers() -> Mapping[str, str] | None`，落点 `skills_directory` 后（非快照 config reader 归位先例）；**None 折叠钉死**——`pipeline`/`tiers` 缺失、`null`、空对象四形皆 None，reader 永不返回空 dict（空 dict 会走 `MaasLlmCall` 表路径全档炸 ProviderUnavailable——「未声明」与「拒绝一切档位」刻意不同构，docstring 全载）；返回 fresh dict（冻结快照语义：调用方不可改合并层态）；类型收窄循 `mcp_server_entries` 复述先例（comprehension 只为 checker，永不丢真条目）。
- 锚6 装配 ✓：cli.py+httpapi/serving.py 两处 `MaasLlmCall(..., tiers=loader.pipeline_tiers())`（各自 loader 实例在作用域内）；注释翻面且**恢复语境注记保留**（「never the possibly-recovered session's replayed snapshot」）。
- 锚7 CLI 覆盖非目标 ✓：无 `--tier` 标志，`CliOverrides` 零触碰。
- 锚8 CORE-CONTRACTS 订正 ✓：顶部 Changelog 2026-11-14 [T-C1-090] 行+§6 配置键清单句后增 `pipeline.tiers` 记载段（键形/装载面/合并语义/None 折叠/不入快照五面全载）；「未知结构字段报配置错误」句保持不动（diff 亲证该句在上下文区未删改）。
- 锚9 契约零触碰 ✓：contracts/events.py、schema-lock、wire 契约、errors.py、`ConfigSnapshotV3`/任何快照版本、`ResolvedRuntime`、llm_call.py（消费面已备缝零改——L18「later ticket」注记为历史陈述保持原样）、engine/语义门/派发面、产品桥 `config/product.py` 全零（工作区路径亲证）。

### 3. 测试面（§4 六新测）— PASS
- loader 契测 4（`tests/integration/test_config_loader.py` 追加，全 hermetic——显式 tmp global path+tmp product 目录，真 `~/.config/opencode` 永不读）：①未知 `pipeline` 子键 → ConfigError 点名 `pipeline.oops`（regex match 形）；②形状违例**五臂矩阵**（pipeline 列表/tiers 列表/空 tier 名/空 model id/非串 model id——各臂消息点名路径逐一 match）；③递归合并（全局层 small:m1+项目层 big:m2/small:override → 两 tier 俱在+同名后层覆盖）+加密度三钉（resolve_runtime 容忍新键/`ConfigSnapshotV2.model_fields` 无 `pipeline`/snapshot dump 无 `pipeline`）；④None 折叠四形（无键/`{}`/`{"tiers":{}}`/`{"tiers":null}` → 皆 None）。
- e2e 装配测 2（`tests/integration/test_llm_call_e2e.py`，先例归属本文件——生产组合形沿 `test_llm_call_e2e`）：⑤配置链全贯通（tmp 层配置 `pipeline.tiers` → `pipeline_tiers()` → `MaasLlmCall(tiers=...)` → 真迁移库+真 EngineCheckpointer+`App(pipeline_engine=)` → 驱动 llm_call 节点）wire 面断言 `body["model"] == "tier-config-model"`（表值命中、**内嵌负对照**——与默认 `_MODEL_ID` 异值，表未生效即失败）+job done；⑥表在而节点名未知名 `'huge'` → job failed 携 §8 `ProviderUnavailable` face（error["code"]/details.node_id/details.node_type）+`server.requests` 空（**拒绝先于任何 provider 调用**）。
- helpers 产线镜像形：`_tiered_pipeline_spec`/`_config_loaded_tiers`（hermetic loader）/`_drive_one_tier_job`（镜像 `_drive_one_job`）；模块 docstring 增段诚实记载。
- 既有档位三测（test_llm_call.py L184-198）与 L351 宿主钉（「config snapshot gains no tier field」）全量跑绿=host-only 面双证成立。

### 4. 门禁与算术 — PASS（install 族=WinError 4551 OS 策略阻断相位，判据链定谳非回归，见下）
- 席位自报四件全绿（20:38-20:56 时段）：ruff 0（首跑 2 error 修后收敛）/ format 552 files already formatted exit 0（计数随未跟踪 md 漂移判 exit 0）/ mypy `Success: no issues found in 207 source files`（207-0 基线相位持平）/ 全量 pytest **2222 passed, 8 skipped in 608.67s** 前台收齐（089 教训内化——含 install 族在内全绿）。
- collect 算术闭合：**2230 = 2224（`e50da2e` 基线：2216P+8S）+ 6 新（loader 契测 4+e2e 2）恰合零漂移**；席位侧 2222P = 2216+6；8S 持平；票面预测 ~6-8，实际 6 落于预测区间。
- 调度器独立四门禁（worktree p5-s6 真 python venv，`uv run --no-sync` 形，21:05-21:15 时段）：**ruff 0 / format exit 0 / mypy 207-0 三件全绿**；pytest **5 failed + 2217 passed + 8 skipped @562.40s**——失败 5 测全为 install 族（`test_cli_install.py`），**非 install 面（含本票 6 新测全过）零回归**。
- **install 族 5 failed = WinError 4551 OS 应用控制策略阻断相位，判据链定谳非回归**：①直接证据——fixture 自建 venv `opencode.exe` Popen `OSError: [WinError 4551] 应用程序控制策略阻止了该文件的运行`（5 测同因，spawn 面）；②同树时序翻转——席位 20:4x 时段同代码树同 venv 同命令全量绿（install 族在内）vs 调度器 21:1x 红——纯 OS 策略窗口时序，非全量负载效应；③隔离重跑红且**负对照基线树同刻更早被阻**——主仓基线 venv（`e50da2e` 树，零本票改动）`ImportError: _xxhash DLL load failed` 同策略消息（连 collection 都进不去，exit 3）——阻断与代码树无关铁证；④零接触面——本票改动（loader/cli/serving/2 测试文件）与 install fixture 零因果；⑤CI 终证=CI-WATCH-229（云端 VM install 族 087-089 CI 腿历史全绿，本地 SAC 策略不随行）。089 评审建议 1 预言命中（「trampoline 阻断面随时可复发」——本轮阻断面更广，连真 PE 二进制与 DLL 扩展一并被策略窗口覆盖）。

### 5. 语义面独立复核 — PASS
- `resolve_model` 三语义对接核对：表命中 → `ModelRef(provider=default.provider, model=表值)`——tier 表值域=maas provider 内 model id 字符串，provider 继承 `--model` 解析源；未知名 → ProviderUnavailable（先于 provider 调用，e2e ⑥ 钉面）；None → 全档回退 default（None 折叠锚的下游语义闭合）。
- 空表≠空 dict 语义坑独立复核：若 reader 返回空 dict，`MaasLlmCall` 走表路径、全 tier 炸 ProviderUnavailable——席位以 None 折叠避让+四形专测钉死+docstring 记载三重防面。
- 优先级链不变：CLI 仍最高层（锚7 非目标守恒）；「product/global tiers」（cli.py L338-352 配置层术语）与 LLM 档位表无涉——票面语义区分坑全程未混。
- 信任面：tier 表是 model 选择非信任扩展——产品层携带合法（host_tier=True 同校），但产品桥本身不产出该键（`config/product.py` 零触碰，第五层恰三键守恒）。

### 6. 披露与记档 — PASS
- 票面 §5 五条余量全数落位进度档「披露余量」节（调度器复核无新增隐瞒面）：①值域=maas model id（跨 provider 非本票）②空表折叠=None（fail-closed 于配置读取面）③快照/ResolvedRuntime 不携带（恢复/replay 不重建 tier 语境）④CLI 覆盖未承接⑤产品桥不产出 pipeline.tiers。
- 进度档门禁输出留档四件+诚实迭代弧全记；决策锚映射 §3→落点逐条对照成文。

## 发现明细

- **建议 1（观察面，无动作）**：loader 契测 ③ 的快照中性断言钉 `ConfigSnapshotV2.model_fields`+`runtime.snapshot.model_dump()`（`resolve_runtime` 面）而非直接断言 V3 字段集——语义等价（两版本快照面皆不含 `pipeline`），V3 九字段冻结面由既有 test_llm_call.py L351 宿主钉全量绿双证闭合。
- **建议 2（观察面，无动作）**：e2e ⑤ 的负对照是内嵌式（wire 值 `tier-config-model` 与默认 `_MODEL_ID` 异值——表未生效断言即失败），无独立负对照测（装配缝回退 None 路径已由 test_llm_call.py 既有档位三测覆盖）——组合面覆盖完备。
- **建议 3（CI 面观察项）**：本票 +6 测增量（suite +~15s 量级），windows 腿 25min 天花板下余量充足；CI-WATCH-229 哨合流检查点终证。
- **建议 4（SAC 阻断窗口观察项，无返工）**：本验收时段 install 族 5 failed 为本地 WinError 4551 策略窗口（判据链见 §4）——与 087/088 时段「全量红隔离绿」形不同，本轮为「席位绿/调度器红/负对照基线同刻更红」的窗口时序形；后续调度侧门禁若再遇 4551 面，先做负对照基线核（同刻基线被阻=窗口活跃铁证，免全量重跑成本）；主仓 venv 恢复可用性须待窗口消退（`_xxhash` DLL import 面），不影响 worktree venv 与 CI 面。

## 附录（无——进度档 20:57:50 先于 SEAT-EXIT 落盘，无延迟相竞面，089 附录订正类事件本轮未复现）
