# P2-S11 评审报告 · 席位B · T-B-072（T-C1-062 compaction 切片二微评审）

- 对象与自证：worktree p2-s11 B-baseline 899d376（父 acd2874=main #182 检查点 19:32:01）；评审 diff 恰 12 路径 9M+3A，+1386/−10 加法闭合；起讫 HEAD 恒定零 git 写（porcelain 起讫均 0，本报告为唯一写面）；冻结面零触 numstat 亲证（context.py/compaction.py/test_compaction_engine.py/test_schema_lock.py/tools/r5/.github/pyproject/uv.lock 均不在 diff）。
- 六面复核（全 PASS）：
  1. fold 语义（state.py）：分发 L475-476 显式分支双落点+fail-closed else 保留；_apply_context_compacted L1169-1230 与 §2.4 L126 逐环吻合——None-linked summary（message_seqs=None 下轮读面跳过、严格递增不变量存活）+整消息粒度 retained 过滤（seq≥retained_from，call/result 不跨界）+strict zip 断链守卫 L1191-1198+二轮替换丢首 summary+applied_seq=event.seq（A=H 前置判定成立）。
  2. 投影（projector.py）：分发 L369-372+_project_context_compacted L512-529 读模型足迹刻意为空（仅 applied_seq 推进），docstring 引 §2.4 L134/M1，store A=H+n 兜底成立。
  3. 接线与触发（service.py）：settled_history L1292-1323 真实读面（EventLog 分页折叠+log_high，summary 永不计入）；_post_run_compaction L1325-1354 notify 仅 outcome.event is not None；_drive_run L1579-1593 结算后三分支（正常结算→触发/CancelledError re-raise→永不/崩溃入 _schedule_errors→永不）；失败策略 _compaction_errors 按会话独立 L1350-1351 绝不入 _schedule_errors；休眠面 engine None→return。
  4. runner 事实锚：runner.py L525-529 request=self._context_builder.build(request_id 等三参, history=state.messages, tools=catalog.specs)——fold 物化的有效视图经 state.messages 直喂 build，链路闭合。
  5. 两 rider：rider(a)=T-B-069 建议1 契约措辞（CORE-CONTRACTS §2.4 L118+§3.2 L205+events.py docstring L790-807 亲证）；rider(b)=T-B-070 建议1 尾行卫兵（test_httpapi_server L1402-1403+test_sdk_client L645-646，pre-raise ValueError+断言面冻结 docstring）。
  6. 测试与漂移：18 新测（契约 9+集成 9）名册↔票面矩阵逐项映射成立；漂移处置=恰 2 工件各 1 行 description-only（ContextCompactedData 新旧措辞替换，pydantic 机械传导），④-0 裁决实录随档。
- B 独立四门禁复跑（uv run 原生前台）：pytest 收集 1444=1426+18、1442P+2S+0F @336.47s exit 0（本轮零 WDAC 事件）；ruff check all passed exit 0；ruff format --check 431 files already formatted exit 0；mypy 族门口径 132 source files 0 errors exit 0（=调度器读数；CI 正口径 ci.yml L46 不含 tools/r5 为 130/0 亦绿，两新测试文件经目录遍历+显式双路 0 错；r5 不入 CI 系基线既有偏斜，非本变更引入）。
- 终裁：PASS 0 阻断/0 重大/0 次要/1 建议。建议1=app.py 组合根未接真实 summarizer（C 进度件 ④-1 已浮报）——建议调度器立为后续切片派发前置，防 compaction 链停留于测试桩驱动的半接线态。
- 处方（呈调度器）：①并回为 #183+主树四门禁+temproot main-183；②翻牌 ORCH 台账 T-C1-062 DONE+票/进度/报告入库；③④-0 漂移事件裁决实录随档；④记档 WDAC/warnings 观察面+CI 与族门 mypy 目标集偏斜观察（tools/r5 双冻结面，后续切片可顺手对齐）。
- 时钟：门禁收口 2026-11-10 20:54:56（Get-Date 工具读数）；B 席位会话 ses_orch_B2；评审窗 20:41-20:55。
sha256=bf1205dac834fe29b4dcbe7f7aabe8ca86f5126aae669c7f900a49e981333db1（Get-FileHash -Algorithm SHA256；本行之前文件内容，调度器截去本行对证）
