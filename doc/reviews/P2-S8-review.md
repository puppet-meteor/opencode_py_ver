# T-B-069 · 席位B微评审——T-C1-060（P2-S8 M6-compaction 切片一：compaction 引擎核心）

- 评审时刻：2026-11-10 16:48:29（现读时钟，先读后落盘零占位）；评审者=席位B（ses_orch_B2 串行会话）。
- 对象：worktree p2-s8 HEAD 06ca093（父 364c4af=main #179，git log -1 亲证）；恰 12 路径 +1643/−130（B 复读 numstat=6M+6A 零外溢；porcelain 0 行净树=无第 13 路径）；冻结面零触亲证（src/opencode/session/context.py[ContextBuilder=切片二接线面]/pyproject/uv.lock/.github CI/source-baseline.json/CORE-CONTRACTS/既有迁移 revision 均不在 diff；ls-files 132py+291md）。
- 方法：六面独立复核（12 路径 diff 全读+引擎 320L/引擎测 621L 全读+契约 §1/§2.4/§3.1/§3.2 现读对拍+计数链逐值复算+四门禁 B 独立复跑+install 族隔离重试）；B 零 git 写（起止 HEAD 恒 06ca093）；temproot=C:/Users/lyu/AppData/Local/Temp/opencode/pytest-tmp/p2-s8-b（调度器预建）；报告=唯一写面。

## 六面结论

1. 契约执行序对照：PASS——§2.4 L126 逐句落测亲证：锁内捕获五要素（CompactionCapture：covered_seq/retained_from/message_count/history_hash/expected_seq=log_high 同锁读，CAS 基数不可 stale）→provider 视图 fail-closed（_validate_view：settle seq≥1 严格升序+log_high≥尾 seq+bool 卫兵，违例 ValueError 先于任何摘要/提交）→阈值门 40/8（两常量钉值+docstring 注明票面与 §2.4 L128 来源；39/40 边界正反测在册）→锁外摘要（SummaryGenerator Protocol 注入 seam，真实 adapter 归切片二）→短提交锁 store.commit(session_id,batch,expected_seq)（§3.2 唯一接口+L205「观察到的 H 作为 expected_seq」对接）→except ConflictError=丢弃（零提交零替换零部分写入+usage 照记 §2.4 L132，真实 store 竞争者测亲证）→其余异常传播不吞不半提交；below_threshold 短持锁面（阈值判定在 capture 锁内，③8 如实浮报锁时序未单测）。
2. 七字段模型约束：PASS——ContextCompactedData 逐行对 §2.4 L116-124 字段表：covered_seq/retained_from/message_count=int Field(ge=0)+_reject_bool field_validator(mode="before")（§1 整数拒 bool 规则）/summary_text+summary_hash=str Field(min_length=1)/model=ModelRef（§2.2 域）/usage=Usage（§3.3 域沿 §2.3 引用面）/model_config=extra="forbid"（§1 L17）；非 run-scoped 论证成立（docstring 明示+RUN_SCOPED_EVENT_TYPES 零触+§3.1 L176 行无 run_id 要求）；负例测 10 参（含 True×3 伪装）+7 缺字段+extra 全拒+registry 三探针（durable 解析/未知版本/持久性失配）。
3. events.py 四锚 vs T-C1-052a skills 同形：PASS——常量区（PY_CONTEXT_COMPACTED 沿 PY_SKILL_ACTIVATED 家族块形+注释）/模型区（SkillActivatedData 后+区段注释含与 skills「零 wire 约束」差异记档）/定义组区（COMPACTION_EVENT_DEFINITIONS 独立 tuple；R2 冻结边界算术复核 durable 11+ephemeral 4=15 不破、无 py.context.* 行入 R2）/registry 组合区（第七元组+模块 docstring 子集行与 registry docstring 双同步）；durable 注册论证在注释（§2.4 replacement 记录=恢复源）。
4. schema-lock 诚实解锁闭环：PASS——计数链 B 独立复算逐值全中：PINNED_TOTAL 92→95={positive 26/schema-level 49/behavior-level 20}（fixture 目录实测 95 件=26 valid+49 schema 级[24 missing_field+25 bad_extension]+20 行为级[15 missing_run_id+1 bad_run_id+2 bad_hash+2 registry 探针]）；PINNED_MODEL_COUNTS events.json 35→36（全模型 65=errors 1+events 36+llm 17+messages 8+tools 3 算术闭合；65 vs 55 双口径对账 L189 保留）；PINNED_DURABLE_KEYS 22→23（events.py durability="durable" 实测 23=2+11+6+2+1+1）+ephemeral 4 持平；_classify 增 _bad_run_id.json 行为级后缀族（最小扩展，③11 浮报）；两工件纯加法再生（events.json +145/−0+event-envelopes.json +206/−0；compacted 命中 3+2 处；漂移守卫在 B 复跑绿=byte-identical 证）；生成器第七组+行锚重锚（events.py L1119-1131）；对账面 L7 修订行/L19-L20 工件行/L23 durable 分解/L30 S1 行[模型 26+fixture 三元组]/L46/L59-61/L160/L189 逐值吻合；collect 算术 1424=1390+31+3 独立复证（引擎 31 收集项亲数=TestThreshold 3+TestRetainedBoundary 3+TestExecutionOrder 4+TestWireNegatives 21；lock +3=fixture 三件驱动 parametrize）。
5. 测试落点与 TDD 证据：PASS——31 引擎测逐测↔验收句映射抽查全中（C 票⑤六映射：39/40 边界正反[39=零摘要调用零提交+锁归零；40=恰一 commit+expected_seq=41+covered=seqs 2..33]/成功路径七字段全落[真实 SessionStore+alembic CLI upgrade head 实跑：seq=42+七字段 model_dump 逐值+原 41 行字节不动+唯一 append 事件]/边界变化丢弃[_RacingSummarizer 锁外 depth==0 断言+expected_seq=41 抢提交→ConflictError→零 py.context.compacted 行+usage 照记+rows[41]=竞争者事件]/retained_from 计算[(33,34,32) 事件+capture 双面]/原历史零修改不变式[rows_after[:41]==rows_before]/模型负例[17 参+3 探针]）；锁纪律测三阶段深度断言（capture=1/summary=0/commit=1）；session_id 卫兵先于锁+视图防御先于提交两测；TDD 证据形态=逐测映射入进度件（C 票载体面要求满足；红阶段无独立逐测红证日志=票面未要求，形态合规）；wire fixture 三件内容与分类钉面一致（valid=七字段全量正例双过/missing_field=去 message_count=required 缺失 schema 级/bad_run_id=metadata run_id:123=whenever-present 规则行为级——compaction 非 run-scoped 唯一 schema 不可见拒绝面论证成立）。
6. 冻结面+写权限+③④ 浮报完备性：PASS——12 路径恰合 C 票 8 项授权 1:1（①compaction.py 320/0②events.py 66/3③generator 6/3④两工件 145/0+206/0⑤test_schema_lock 13/12⑥inventory 117/112⑦引擎测 621/0+fixture 三件 27/26/29⑧进度 67/0）；③ 11 条抽查四钉面全在册且与源码对拍属实（covered 空集卫兵不可达[40-8=32 恒非空]/摘要 adapter seam/settled 判定归切片二/hash 派生式未钉面——+bus-free/stateless/ConflictError-only/真实 projector 不可投影须切片二回填/_classify 扩展全披露）；④ 4 项（切片二范围/token 面阈值细化/skills fixture 缺口保持/format 计数口径）无漏报迹象；B 反向读源码仅得两处锚点级小面（→附注①+建议1），无实质未浮报面。

## 独立复跑（temproot p2-s8-b，uv run 口径）

- pytest 全量：1418 passed+2 skipped+4 failed @321.73s（收集=4+1418+2=1424=1390+31+3 恰合零回归；2 warnings=SAWarning GC 连接面，aiosqlite 族间歇观察）；判定面=install 族外全绿 1420/1420。
- install 族处置（#167 口径）：4F 全数=tests/integration/test_cli_install.py 四测（WinError 4551=WDAC/SAC 环境面，与调度器 16:20-16:28 复跑逐值同形 1418P+2S+4F@348.07s）；B 隔离重试=4 failed/3 passed @3.93s 亦红——观察值链续录：C 轮 {4F→7/7 绿→全量绿 0F}→调度器三投 4F+主树洁净基线同刻拒+worktree Start-Process exit=0=分钟级翻转定谳→B 轮 {全量 4F→隔离 4F}；本 diff 零 install 面触碰+调度器控制组双证在册=环境面定谳成立，CI 终证承载（调度器档）。
- ruff check：All checks passed!（exit 0）。
- ruff format --check：423 files already formatted（exit 0=132py+291md；与 C 席/调度器逐值同）。
- mypy：Success: no issues found in 130 source files（exit 0；基线 128+新增 2 恰合）。

## 终裁与处方

- 终裁：PASS——阻断 0/重大 0/次要 0/建议 1。
- 建议1（文档精确化候选，零行为缺陷）：§2.4 L118 字段表「covered_seq……短提交锁内 expected_seq/CAS 校验的基准」与实装/§3.2 L205（「同一会话提交锁内读取 H/A，并把观察到的 H 作为 expected_seq」）存在精度差——实装 CAS 字面基数=同锁捕获的 log_high 水位，covered_seq=被 CAS 守护的覆盖边界（字面以 covered_seq 作 expected_seq 在 retained 尾部存在时恒 Conflict 不可实现）；ContextCompactedData docstring「the expected_seq/CAS basis」同模糊。实装方向正确且更严（任意日志前进即丢弃摘要）。建议调度器记文档微票或切片二 rider：契约句+docstring 措辞精确化。
- 处方（供调度器执行）：①并回（p2-s8 merge --no-ff 入 main 为 #180+主树四门禁复跑+temproot main-180 预建）②翻牌（ORCH 台账 T-C1-060 DONE+票 md 41L 入库+进度 67L 与本报告随 merge 入库）③切片二 T-C1-062 派发前置（范围沿 A 席复估+C ④1：ContextBuilder replacement/retained 接线+真实 settled 读面+py.context.compacted 投影 fold[须核实真实 projector applied_seq 推进=A=H 不变量面，③10 框架内]+触发策略+bus notify+失败/取消/崩溃全矩阵；建议1 措辞 rider 可随载）④记档三则（附注①行锚漂移+2W SAWarning 间歇面+WDAC 观察值链 B 轮续录）。
- 附注非缺陷三则：①events.py 注释「§1 L16/L17 repo-wide rules」行锚漂移——§1 实位 L17（extra="forbid"）/L18（整数拒 bool），漂移源=#178 Changelog 行插入后全文 +1（B/C 票面同载旧锚，语义引用无歧义）；②wire fixture 三件=schema-lock 分类面（C 票⑦授权），与 p2-slices L122「fixture/平台测试收口=切片二」的 §2.4 L136 行为 fixture 面不同族——C 票权威 spec 已切分，边界读法一致无冲突；③pytest 2 warnings（SAWarning「garbage collector ... non-checked-in connection」）与既往 aiosqlite「Event loop is closed」同域间歇族观察差（C 轮进度未记 W 计数面→B 轮 2W）。

T-B-069-PASS 0/0/0/1
sha256=EB5A28F703DA0860C319E37207B7E860EBCAB326B70AACD3FA570FC36DADB237（Get-FileHash -Algorithm SHA256；本行之前文件内容，调度器截去本行对证）
