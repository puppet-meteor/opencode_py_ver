# P5-S3-review — T-C1-087 微评审：注册面原生单路 + converter 整体删除（ruling-17/18/19 形态落地）

- 评审对象：worktree `p5-s3` 未提交工作区（基=派发检查点 `cfd3c03`，3 删+6 改+1 新进度档；席位零 git 写——HEAD 面亲证恒 `cfd3c03`）；席位票 `T-C1-087.md` 钉面全量；进度档 T-C1-087.md 触碰面+OPEN 记档俱全（含续会 rider 审计弧）。
- 评审基据（全部独立现读，禁继承）：票面/进度档/席位日志（`doc/orchestration/logs/T-C1-087.txt` 2592 行，尾=SEAT-EXIT 0）亲读；工作区 diff 全量亲读（registration.py +64/−27、engine.py +7/−9、删除面 −2151 行三文件、test_registration.py +132/−32、test_registration_engine.py +78/−29、test_engine.py +19/−0、test_engine_v2_fields.py +345/−11[席位 288+rider 终形 57 行槽]）；引擎原形对照 `b7d8f36~1` 亲证；调度器独立门禁 1-3 终树复验（ruff 0 / format 544 exit 0 / mypy 202-0）。
- 评审时刻：2026-11-14 00:25（席位主弧 35min 止损内 + 续会 rider 审计弧 00:06→00:22；席位 git 写零；调度器直执 rider 初稿一处——席位续会审计抓红后测试侧重铸，第 4 面记档）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 0

## 六面逐条结论

### 1. 票面纪律 — PASS

- **去重令（ruling-18）全兑现**：converter 三件整体删除（`contracts/pydag_import.py` 987L 实现+`tests/contract/test_pydag_import.py` 743L 契约测+`tests/integration/test_pydag_engine.py` 421L seam 测，−2151 行）——src+tests 引用面 grep 零命中亲证（`pydag_import|parse_pipeline_spec|PydagImportError|convert_pydag_graph|PYDAG_` 零命中；余面全为票面允许的历史注记/测试数据）。
- **registration 原生单路**：声明 graph.json=路由元数据永不打开（声明文件为垃圾证零读取、engine.raws 逐字=原生 payload）；消费面=派生姊妹 `pipeline.opencode.json`（NATIVE_SPEC_NAME）。
- **engine 单点**：`parse_pipeline_spec` → `PipelineSpec.model_validate`（083 交换前原形，`b7d8f36~1` 逐字对照证）；方言 raw 达结构层=pydantic ValidationError 零嗅探零第二解析。
- pyproject/uv.lock/catalog.py/schema 锁零触碰（numstat 亲证）；pydag_import 无 pydantic 模型（BaseModel grep 零命中）→ PINNED_MODEL_COUNTS 不变，schema_lock 测试全量内实测过。

### 2. 注册面 — PASS

- **派生路径 containment 照旧**：词法→realpath→is_relative_to 三段全套用于派生姊妹（逃逸=PipelinePathEscape 面不变）。
- **NativeSpecMissing(ValueError) 具名拒绝**：`is_file()` 前检=缺位/目录同归；消息四面（技能 id+缺位事实+方言指称+框架 emitter 迁移指针）；永不静默、永不跳过、永不回落 converter。
- 既有面全保持：SkillCatalog 唯一发现门（零第二解析）、静默零态前检、default 计数跳过零开文件、一拒不断、报告四面、宿主 stderr 三形（cli.py/httpapi 只读 report 零触及）。

### 3. 引擎面 — PASS

- 单点复位+迁移级注记一行（带裁决引用，票面允许面）；三层门+spec hash 路径零变化；既有 pydag_engine.py 溯源历史注面零触（票外禁改红线保持）。

### 4. rider 审计弧（T-B-097 建议 1 承诺兑现）— 微评审发现 → 席位续会审计闭环

- **as-authored RED 定谳**：调度器初稿断言非 dict attempt 被采纳——但移植谓词（pydag_engine.py `_fallback_trigger_reasons` L7214-7247，086 落地面）把非 dict 物化值本身列为触发因，全谓词复评恒出 `materialized_output_missing` → 非 dict attempt **永不可被采纳**；采纳路径不可达（席位续会隔离跑红证）。
- **修向正确**：测试侧非引擎侧（亚语义以框架源码为权威红线；改引擎采纳非 dict=偏离移植语义，禁止）。
- **终形**：非 dict 主值（非空 list → `materialized_output_missing` 触发链）+ 空 dict attempt（复评 `primary_data_empty` → rejected[missing_after]）→ 链耗尽 → 主值存续 → `_merge_fallback_history` 包裹形 `{"value": [...], "fallback_history": [rejected 记录]}` 落消费端——086 OPEN 项「原生自定义形」消费侧钉面；rider 意图（钉包裹形消费面）与载荷值保持，docstring 补可达性注记（带框架行引）。
- 终跑全量含 rider：2204 = 2196P+8S 0F 全绿（@573.10s）。

### 5. 测试面 — PASS

- 15 新/重铸形对票面 §7 全落：缺位/目录四面断言、双文件优先证零读取、存量 8 条镜像（2 SPEC registered + 6 NativeSpecMissing，manifest 序）、方言 raw→ValidationError（引擎结构层）、084 执行语义四端到端原生重铸（params refs/condition skip+诚实下游失败/extract projection/结构化 fallback+checkpoint 通道集钉面）。
- 宿主 test_registration_hosts.py 经共享 helper `_write_langgraph_skills` 双写面零触碰全绿；删除面存活覆盖全归位（v2 执行 4 测→test_engine_v2_fields 原生形/方言拒绝→test_engine.py TestRegistrationGate/roundtrip+hash→既有测天然覆盖）。

### 6. 门禁算术 — PASS

逐值比对（席位 vs 调度器，全部独立现读）：

- **席位首跑**（rider 落盘前）：2203 = 2195P+8S 0F @557.40s——=2297−102（删除面）+8（本票新测）恰合，收账算术证未含 rider。
- **调度器全量**（rider 旧版相位）：6F+2190P+8S @545.84s——5F=install 族五名（WDAC 4551 相位，判据链照旧：隔离重跑+直接证据+CI 终证）+1F=rider 旧版不可达断言（后修）；collect 算术 2204=2203+1 恰闭合。
- **席位终跑**（rider 终形）：2204 = 2196P+8S 0F @573.10s 全绿——install 族零失败（WDAC 相位未现，振荡观察链 {0F↔5F} 续录）。
- **ruff**：席位 0 = 调度器 0 ✓。
- **format**：席位 543（23:47 相位）/544（终态）、调度器 544×2（rider 前后）+终树复验 544——判定面 **exit 0 全相位恒定**（543/544/545/546 计数振荡非判定面，S1 先例；磁盘探针证无新 .py 文件，输入集恒定）。
- **mypy**：席位 202-0 = 调度器 202-0 ✓（全 CI 形；=086 收口 205−删除面 3 自然回落，零新文件）。

### 7. 冻结面 — PASS

- catalog.py、schema/pipelines.json+schema_lock、pyproject/uv.lock、086 四字段运行时、engine 既有面、宿主测试全零触碰（diff 亲证）；v1 哈希钉不翻（零态钉测试全量内过）。

## 附：裁决处置与后补

- 086 建议 1（`fallback_history` 非 dict 包裹形消费侧专测）——本票 rider 终形兑现，**闭环**。
- 086 次要 1（持久尝试事件类型 DurableEvent 后补票）——保持挂起（披露面 fallback_history+stderr 双承载）。
- 派发探针 extras 异常（全新 `uv sync --all-groups` 不装项目自身 extras→17 collect+12 mypy 同因→`--all-extras` 修复→四探针全绿）——派发基础设施记档，CI 环境即 extras 形态，详见 #218。
