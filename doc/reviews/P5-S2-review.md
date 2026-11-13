# P5-S2-review — T-B-097 微评审：T-C1-086 StepDef 四字段移植面（transform / output_format / instance_binding / fallback_tools）

- 评审对象：worktree `p5-s2` 未提交工作区（基=派发检查点 `14c8103`，6 改+3 新；席位零 git 写——HEAD 面亲证恒 `14c8103`）；席位票 `T-C1-086.md` 钉面全量（UTF-8 精确重读——PS 直读乱码勘误见第 5 面）；进度档 T-C1-086.md 触碰面+OPEN 记档俱全。
- 评审基据（全部独立现读，禁继承）：票面/进度档/席位日志（`doc/orchestration/logs/T-C1-086.txt` 2930 行，尾=SEAT-EXIT 0）亲读；工作区 diff 全量亲读（contracts/pipelines.py +93/−19、engine.py +621/−28[席位语义块+调度器披露补丁合计]、test_pipelines.py +437/−2、test_pydag_import.py +4、test_schema_lock.py +3/−2、schema/pipelines.json +156 再生成工件、test_engine_v2_fields.py 新 574L[席位 10 测+补丁 1 测]）；ADR-0002 L60 先例+cli.py L392/701 机制先例+jobs.py emit 族（L907-1028）事件面边界亲证；调度器独立四门禁（补丁后终态，见第 6 面）。
- 评审时刻：2026-11-13 23:22（止损内；席位 git 写零；调度器直执补丁一处——第 4 面记档）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 1 / 建议 1

## 六面逐条结论

### 1. 票面纪律 — PASS

- **pydag_import.py 零触+registration.py 零触**：git status 亲证两文件不在改面（087 票面归属）；converter 对四名的拒绝现状保持（ruling-18：映射唯一实现归框架仓 emitter）。
- **无第二实现路径**：四语义单写点（node_fn 写点序 raw→extract→transform→fallback 链→output_format→history）；fallback 尝试走既有 `_tool` 缝+既有 node-params 解析机制；instance_binding 复用既有 params 视图机制。
- **v1 钉哈希不翻**：零态钉测试（纯 v1 spec dump 剥 None 九字段+空链后字节恒等→`a881d3f4…8772`）。
- **无新依赖**：pyproject/uv.lock 零触碰（numstat 亲证）。
- 触碰面恰票面授权：契约九名扩展+引擎语义块+四测试面+进度档。

### 2. 契约与哈希面 — PASS

- 四字段纯加法（Optional、默认 None）；`FallbackToolSpec` 闭模型（tool+params，params 复用 JsonObject 面）。
- 共享 ref 形状 helper 消息字节不变（`_require_dot_path` 复用——既有 v2 字段读者零涟漪，亲读对拍）。
- 哈希配方：None 剥除集九名+空 `fallback_tools` 链剥除——v1 spec 规范字节前后恒等（零态钉）；携带四字段的 spec 哈希确定可复算。
- `output_format`/`instance_binding` 校验器形与 v2 先例（output_schema/fallback）同构——结构拒绝面一致。
- schema.json 再生成工件与生成器幂等；schema_lock PINNED_COUNTS 3→4 同步（pipelines.json +1 FallbackToolSpec）。

### 3. 引擎语义面 — PASS

- **写点序**：raw→extract→transform→fallback 链→output_format→history——与框架语义权威对齐（框架行引用入 docstring：pydag_engine.py L6229-6294 / L6461-6474 / render_param_template L147-155）。
- **transform**：派生字段算到节点自身结果上；失败 op 跳过不失败节点+披露；保留名（框架 compute transform 名集）无原生执行器=跳过披露。
- **output_format**：确定性零 LLM field_map 渲染成为写值（直键解析→嵌套 finder→example/default 回退→类型强制）。
- **fallback 链**：触发谓词三态（空主值/空 extract/缺期望字段）；每次尝试全谓词复评（非子集检查）；首个干净尝试胜出；耗尽=保主值且不渲染（诚实：不假装渲染过）；尝试 params 经既有解析机制自解析；instance_binding 不适用于尝试（披露）。
- **instance_binding**：查询 id 渲染进 params 视图，永不覆写 params（权威在步骤自身 params）。
- **控制缝**：EnginePause/WorkerSuspended 穿透不落尝试记录（取消/暂停语义不被 fallback 吞）。
- **记录面**：`fallback_history` 骑写值（非 dict 包装 `{"value": ...}` 形——进度档 OPEN 如实记档）。

### 4. stderr 披露（票面 L43）— 微评审发现 → 调度器直执补齐

- **查证**：「沿现有事件面」的现有面=jobs.py emit 族（L907-1028：started/stalled/redispatched/gated/finished）——纯作业生命周期迁移，无运行时尝试槽位；硬造新持久事件类型=扩 DurableEvent schema+PINNED_DURABLE_KEYS/cards.json 涟漪，超出微票授权面。
- **裁定**：结构化记录实质已由 `fallback_history` 满足（持久+测试钉）；stderr 披露=可小改补齐面（调度器直执，席位未交付该半句如实记档）。
- **补丁**：`_disclose_fallback_attempt`——每尝试恰一行紧凑非致命 stderr，三态全（adopted / rejected (missing_after: …) / exception (error)）；ADR-0002 L60 先例形态（组合根一行式）+cli.py L701 机制先例（`print(f"opencode: …", file=sys.stderr)`）；三记录点后各一调用；+1 capsys 测试（三态断言+`opencode: node` 前缀）。
- **后补票**：持久尝试事件类型（DurableEvent 新型）列披露后补票——schema 锁涟漪面需独立授权。

### 5. 测试面 — PASS

- 真引擎测 11 条（席位 10+补丁 1）：transform 2 / output_format 1 / fallback 6（链序+触发三态+耗尽+异常续链+adopted 过 extract/transform+stderr 披露）/ instance_binding 2——票面 L50「每字段 ≥1 真行为测」全满足（2/1/6/2）。
- （勘误记档：评审初读「每字段 ≥3」系 PS 直读乱码误读——UTF-8 精确重读定谳票面实为「≥1」，疑点消解。）
- S1 fake-executor 纪律保持：注入体（`_ScriptedTool` script）是唯一被测缝；语义核（写点/谓词/链/渲染）恒真引擎真图。
- 钉面更新：test_pydag_import dump 表+四键（converter 拒绝现状钉）；test_schema_lock PINNED_COUNTS 3→4；v1 哈希零态钉。

### 6. 门禁算术 — PASS

逐值比对（席位 vs 调度器[补丁后终态]）：

- **collect/pytest**：席位全量多相位——2F/2286P（首发）→ 5F/2247P（contract+integration 面）→ 隔离重跑 8/8P → **0F/2288P+8S @566.33s（后台终跑，隔离 temproot 相）**；调度器全量=**5F+2284P+8S @551.20s**（合计 2297=2296+1[补丁新测] 恰闭合）；隔离复跑 5F/3P（阻止相位）——**5 败全数 install 族**，4551 直接证据（`OSError: [WinError 4551]` @ `_winapi.CreateProcess` spawn 面）+对照面因果排除（help/version argparse 面同 signature 失败——该面不触引擎语义）；零非 install 族失败双源证 → **非回归**（WDAC 相位振荡 {0F↔1F↔2F↔5F} 观察链续录），CI 终证归调度器。
- **ruff**：席位 0 = 调度器 0 ✓。
- **format**：席位 544 exit0 vs 调度器 545 exit0——判定面恒 exit 0（S1 先例；计数漂移非判定面）。
- **mypy**：席位 205-0 = 调度器 205-0 ✓（全 CI 形，含补丁后 engine+测试）。

### 7. 冻结面 — PASS

- engine 既有面/contracts 既有名/pyproject/uv.lock 零触碰（diff 亲证）；pydag_import.py/registration.py 零触（087 面）；v1 哈希钉不翻。

## 附：裁决处置与后补

- 次要 1：持久尝试事件类型（DurableEvent 新型+schema 锁涟漪）——后补票（披露面已由 fallback_history+stderr 双承载）。
- 建议 1：`fallback_history` 非 dict 包装形（`{"value": …}`）无消费者侧专测——随 087 注册面/消费端票搭载。
