# P6-S5-review — T-C1-098 微评审：动态 subagent 工具——`subagent__run` 前台子代理面（P6 收池票）

- 评审对象：worktree `p6-s5` 席位工作区（未提交态，席位 git 写零）：改 11 跟踪文件[registry.py/service.py/contracts/events.py/两 schema 工件（event-envelopes.json+events.json）/cli.py/serving.py/policy.py/`builtin/__init__.py`/两基线 census 测试] + 新 4[`tools/builtin/subagent.py` 409L；`tests/integration/test_tool_subagent.py` 11 测；进度档；票面拷贝 untracked]——**两处与票面钉面的偏差均完整记档**（registry.py 机制矫正=「触碰须记档理由」条款覆盖；census 算术矫正=调度器票面自身误差，见建议 1）；契约面触碰全落 §3-3 ①形预授权圈内（contracts/events+两 schema 工件+service 接缝）；httpapi 仅 serving.py 组合面；SDK/路由零触碰。
- 评审基据（全部独立现读，禁继承）：subagent.py 全图+执行体逐行（十锚执行序+超时/取消/最终文本 helper 面）、registry/service/contracts/两宿主/policy/init diff 逐行、两 schema 工件、两 census 测试 diff、新测试 11 测全图+两专测断言面、进度档全文、TS `subagent.ts` 317L 钉面语义对照、CORE-CONTRACTS 零触碰核证（11 改文件清单不含）。
- 评审时刻：2026-11-15 20:35-20:45（席位弧单段闭合：发射→SEAT-EXIT 0 @20:35:03[全前台 622.75s+进度档全档——S4 教训吸取]；席位 git 写零——HEAD 恒 `9f8e997` 调度器亲证）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 4（全观察面，无返工）

## 六面逐条结论

### 1. 票面纪律 — PASS

- 禁 git 写遵守：席位全程 HEAD 恒 `9f8e997`（调度器独立核：工作区=11 改未提交+4 未跟踪恰合席位终态）。
- **单段闭合**（S4 教训直接吸取）：全量 pytest 前台收齐 622.75s+进度档全档后 SEAT-EXIT 0——无续跑令需要。
- 两处偏差全记档：①registry.py 全局锁→per-session 惰性锁机制矫正（超 §6 钉面，「触碰须记档理由」条款覆盖——三腿取证+回归腿直测，见 §5）；②census 算术矫正（调度器票面「16→19」误差，席位修正 16 基名/17 含 pty——评审侧诚实记档，见建议 1）。
- §3-3 父链接缝三选一：席位选 ①契约事件可选 `parent_id`（预授权面）+理由记档（三 wire 版 decode 兼容优于 metadata/服务级映射两替代）。

### 2. 决策锚落面（§3 十锚）— PASS

- 锚1 新文件+双宿主：subagent.py 409L（常量形沿 S4 先例：SUBAGENT_NAMESPACE/SUBAGENT_NAME/VERSION/DESCRIPTION[席位自撰记档]/INPUT_SCHEMA/SPEC）；双宿主组合+1（cli.py+serving `_builtin_adapters`）+`builtin/__init__.py` 导出+`__all__`（双射守卫联动）；两 census 加法翻新（**16 基名/17 含 pty**——席位矫正数，非票面 19）。
- 锚2 Input 面：`{agent, description, prompt, sessionID?}` 恰形；model 不移植（DIFFERENT-BY-DESIGN）；background 拒=UNSUPPORTED_CAPABILITY **hoisted 前置**（席位细化：先于副作用校验零成本拒）；agent 收敛 `SUBAGENT_AGENTS=frozenset({"task"})`+未知 agent 拒；primary 概念 N/A 记档。
- 锚3 父链接缝：①形落面——`py.session.created` data 增可选 `parent_id`（contracts 三 wire 版 decode 兼容+两 schema 工件再生成+service `create(parent_id=)` 接缝+`session_created_data` 公面）；**删除级联零引入**（L1645 记录句 verbatim 维持，「no parent chain」子句如实历史化披露）；深度守卫=`SUBAGENT_MAX_DEPTH=1` 常量+durable 父链上溯（get_session 链走+**corrupt cycle 防护**——席位细化面）。
- 锚4 执行序 PY 化全落：父存在性（VALIDATION_ERROR fail-closed）→depth 守卫（超限拒收含限值消息）→agent 校验→resume 面（存在+`parent_id===context.session_id` 双校验，「not a child」拒）→create（**snapshot 自父 verbatim**+title=description）或 resume 复用→prompt→**前台轮询**（600s 限+0.05s 节流）→终态提取（**最终 assistant 文本分页提取**+TS 逐字 no-text 句「Subagent completed without a text response.」）→Output 结构化+text 同载。子 run 失败终态=fail-closed（含终态摘要，禁静默 completed）。席位细化增益面：**SessionBusy（409 语义）拒收**（resume 遇子会话在飞 run=防双 run 竞态）+超时 face=TOOL_TIMEOUT retryable（子 run 续跑+后继调用可经 sessionID 收割——bash/pty 超时先例形）+取消=bounded best-effort（孤儿子 run 保 cancelling 态交 R4 recovery coordinator）。
- 锚5 policy：`subagent__run`="ask" 行+`_ASK_NAMESPACES` 零触碰+side_effects=True/idempotency=False（Spec 钉）。
- 锚6 permission.assert 专项不移植（policy ask 行+审批协议既有面承载）记档。
- 锚7 完成交付导入不移植（v1 工具输出即模型可见交付）记档。
- 锚8 契约面边界：①形预授权圈内触碰全记档；registry.py 矫正=「触碰须记档理由」条款覆盖+三腿取证（契约腿 PHASE1-SLIM L85+CORE-CONTRACTS L118/L506 per-run 语义钉+复现腿全链测试钉+回归腿 bounded-red 设计）。
- 锚9 CORE-CONTRACTS 档面零触碰（调度器独立核证：11 改文件清单不含；L118/L506 系引用非触碰）。
- 锚10 非目标面：既有 16 adapter+workflow 零回改（census 两处纯加法）；httpapi 路由/SDK/CLI 命令面零触碰（cli/serving 仅组合清单行）；agent 目录/三内置代理/模型覆盖/后台作业/删除级联全不做。

### 3. 测试面（§4 七组→11 测+两基线 census 翻新）— PASS

- 守卫矩阵：父会话缺/未知 agent/background 拒/depth 超限（含限值错误消息断言）。
- resume 校验：非子会话「not a child」拒+真子会话复用（同 session_id 二次调用=同子会话续跑+messages 增量）。
- 前台全链：scripted provider settled→Output 恰形 `{sessionID, status:"completed", output=最终 assistant 文本}`+子会话 durable 在册（events/sqlite 探针：`py.session.created`+prompt 链事件）+父链接缝持久证（§3-3 机制形对应断言）。
- 子 run 失败终态 fail-closed+无文本终态 TS 同义句两腿。
- census/policy/Spec：双宿主 16/17 恰形（两基线 census 加法翻新）；policy 行为级（evaluate ask/default）；Spec 八字段钉（含 side_effects/idempotency）。
- **席位增益两专测**：registry 死锁回归腿**直测**（矫正机制的行为钉——非仅既有测试间接覆盖）+**unbound wiring defect 专测**（named instance late-bind 接缝的未绑定缺陷面 fail-closed 钉）。
- collect 增量=11（2307=2296+11 恰合）；零回归=全量面承载（recovery 全文件绿+S4 三工具族绿含内）。

### 4. 门禁与算术 — PASS（席位侧全档；调度器独立面占位待核证定稿）

- 席位自跑四件（全前台收齐）：ruff 0 / format 580 files exit 0（578 预期[576+2 新]+2 漂移——**连续三席同 +2 漂移形**，门绿为准）/ mypy 219-0（217+subagent.py+新测试=219 预期恰合）/ 全量 **5F+2294P+8S@622.75s** collect 2307=2296+11 恰合。
- 5F=install 族五员三腿裁定（standalone 隔离绿+全文件绿+S4 双态先例）——4551 相位窗判据链引用形（092/093/095/096+S4），不阻断。
- 调度器独立四门禁（worktree p6-s5 真 python venv，`uv run --no-sync` 形，20:3x-20:5x 时段）：**ruff 0 / format exit 0 / mypy `Success: no issues found in 219 source files`（219-0 与席位侧同数）/ pytest `2299 passed, 8 skipped` 零失败 @647.47s exit 0——collected 2307=2296 基+11 新恰合；2299P=席位 2294P+5 install 族（本窗绿）=席位三腿裁定的相位窗时变直接复证（红窗[席位侧 5F 全 install 族+4551 逐员直证]→绿窗[本窗零 4551 零失败]；092/093/095/096+S4 判据链免启用=0F 干净负对照）**——零失败面免判据链定谳，install 族+全量双绿直接证明。CI 终证=CI-WATCH-247（#244 派发检查点 `9f8e997` CI-WATCH-246 已绿=同基线树 CI 终证先行面）。

### 5. 语义面独立复核 — PASS

- **registry 死锁因果链独立核**：registry 级串行锁覆盖整个 requested→finished 管道×子会话工具调用经同一 registry→父 invoke 持锁等子 run、子 invoke 等锁=确定性进程级死锁（非时序偶发——scripted provider 子链工具调用即复现）；矫正=per-session 惰性锁字典——**PHASE1-SLIM L85「calls execute serially in call-start order」per-run 语义保持**（同会话内仍串行，跨会话并发解禁——EventCommitter 形先例）；CORE-CONTRACTS L118/L506 per-run 语义钉为契约腿证据。**e2e_r4 §⑥ 记录句历史化=调度器开口项如实披露**（两会话可同时持 pipeline 槽——split 测试仍绿，合并 close 面未测，记档）。
- 深度守卫语义核：durable 父链上溯+cycle 防护（corrupt 环不挂死）+超限消息含 SUBAGENT_MAX_DEPTH 限值。
- 超时/取消语义核：TOOL_TIMEOUT retryable+子 run 不杀续跑（后继 sessionID 收割缝真实）vs 取消 bounded best-effort（孤儿子 run 保 cancelling 态交 R4 recovery coordinator）——两面对照自洽（超时=可收割暂时态；取消=终局路径交恢复协调器）。
- create snapshot 自父 verbatim 核：子会话继承父工作面（working_dir/配置 verbatim——TS parentID 语义 PY 化正确；模型继承=宿主 provider 单一面）。
- census 算术独立核：16 基名（12+trio+subagent）/17 含 pty 两侧——席位矫正正确（票面 19=调度器 S4 +3 形误套本票 +1）。
- Output/错误面核：结构化 ToolResult.structured_output+metadata+text 同载；失败终态含子 run 终态与错误摘要（禁静默 completed 兑现）。

### 6. 披露与记档 — PASS

- 票 §5 十项全数落位+席位余量至 **16 条**（10 票面+6 席位增益：seam ①选型理由/L1645 历史化/registry 矫正三腿/census 矫正/SessionBusy 增益/超时收割缝+取消交 R4/corrupt cycle 防护/分页提取/unbound wiring 专测等）。
- 席位弧时序单段全档（发射→亲读锚定→实现+机制矫正→11 新测→四门禁全前台→进度档→SEAT-EXIT 0）。

## 发现明细

- **建议 1（观察面，无动作）**：调度器票面 census 算术误差（「16→19」=S4 +3 形误套本票 +1，实际 16 基名/17 含 pty）——流程改进：census 算术逐票现算，禁前票形套用；席位矫正+记档=正面范本。
- **建议 2（观察面，无动作）**：e2e_r4 §⑥ 合并 close 面=调度器开口项（registry 矫正副作用：两会话可同时持 pipeline 槽；split 绿、合并面未测）——后继票据候选（补合并面回归腿）。
- **建议 3（观察面，无动作）**：format +2 漂移连续三席同形（576→580=578 预期+2）——稳定漂移形，门绿为准；若后续席位再现可议 ruff format 全量一次归一（非本票面）。
- **建议 4（观察面，无动作）**：SubagentJob 后台作业面+完成交付 durable 导入（§5-4/§5-5）=backlog 候选已记档——P7 提案时可并入复评。

## 附录：席位弧时序

- 派发 #244 检查点 `9f8e997` push（INSERT OK 18:50，numstat 1/0 钉）→worktree p6-s5 建面（venv 3.12.16+69 包一次过）→四探针相位窗张开形（ruff 0/format 0/mypy 217-0/pytest 5F+2283P+8S+4551 直证）→CI-WATCH-246 success（run 43706039255）→发射 C 席（`ses_orch_C_p6s5`）→**单段闭合**：亲读锚定→实现（含 registry 机制矫正）→11 新测→四门禁全前台收齐（622.75s）→进度档全档→SEAT-EXIT 0 @20:35:03→看哨 TRUE-EXIT round 61；零 git 写（HEAD 恒 `9f8e997`）。
