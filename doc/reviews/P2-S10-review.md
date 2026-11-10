# T-B-071 · 席位B分析核验微评审——T-C1-061a（mcp_stdio 树杀再现 triage·停下上浮形）

- 评审时刻：2026-11-10 19:22:56（现读时钟，先读后落盘零占位）；评审者=席位B（ses_orch_B2 串行会话）。
- 对象：worktree p2-s10 HEAD fa6a19f（父 d00f54b=main 现 HEAD 18:28:17=#181 push 检查点，git log -1 亲证）；恰 1 路径=新进度件 65L/10151B（B 复读 numstat/name-status 亲证；porcelain 0 行净树）；零代码 delta（src/**、tests/**、fixture 零触——诊断优先+停下上浮形票面纪律的结构性自证）。
- 方法：六面分析核验（票面「调度器已验项免重复」条款引用：git 状态面+进度件 ①-⑤ 全读+stdio.py L482-505 锚点亲证+调度器静态复证 @19:13:13[收集 1426/ruff clean/format 427/mypy 130-0]；B 独立面=假说 (a) 定谳链逐环 src 只读复核+(b)(c) 排除独立复核+两现 CI 日志档亲证+④ 七项反向抽查+四门禁独立复跑）；B 零 git 写（起止 HEAD 恒 fa6a19f）；temproot=p2-s10-b（调度器预建）；报告=唯一写面。

## 六面结论

1. 票面纪律面对照：PASS——诊断优先核实（唯一写面=诊断档本身[内含定谳]，numstat 恰 1 路径=定谳前零写动作的结构性证据）；停下上浮触发正确（定谳落 (a)=src 侧→零 src 写+分析 only；写权限①前置「假说 (c) 成立」未满足→零测试写——写面恰 1 新 .md 全部吻合）；席位纪律面（单发 SEAT-EXIT 0+门禁回帖内等待完成）与 ③ 节写后终态数字+调度器在册记录一致——「门禁后台挂跑时禁止退席」条款本票生效的结构性证据成立（对照 T-C1-061 两例提前退席先例）。
2. 假说 (a) 定谳链逐环复核（核心面）：PASS——B 逐环 src 现读独立成立：环1 _capture_in_job L494-497 JobObject() ctor OSError→静默 return None+L498-504 job.capture OSError→close+return None+L501-502 已退出空壳 return job——三形皆零日志零暴露（docstring 自证 any other failure degrades to the main-process-only kill=静默退化为设计意图）；环2 _terminate_tree L511-519/_kill_tree_hard L532-540：_job=None 时直落 proc.terminate()/proc.kill() 仅主进程杀，terminate(1) 全 kill 路径 exit code 1（无 259 生产者）；环3 crash 分支 L230-231 双扫同步内核调用+L243-246 KILL_ON_JOB_CLOSE 兜底条件于 _job is not None——job=None 即兜底不存在（铁证）；环4 _stdout_ended L349-360 stderr/proc.wait() 双 wait_for 有界+L373 reader task 内 inline await self.close()（非 fire-and-forget）+L361 exited with code 诊断面（两现 raises 侧 code 3 通过=诊断链完成证据）；环5 grep 全文件 create_task 仅 L178-179 两 pipe reader；winjob L110 BREAKAWAY_OK 故意不设（无 breakaway 逃逸面）+L114-129 ctor OSError 双形；fixture L104-107 普通 child 全 DEVNULL 零旗标（capture 成功必继承成员）；探针 L76/95/97 259 面结构如册——链闭环：幸存者必为 child（server 自愿退出 3 探针立报死）→job 缺席→capture spawn 时返回 None→触发器（WDAC/AV 瞬时 ACCESS_DENIED 候选，#167 同 runner 先例）从现有日志不可证=「静默性本身即缺陷第一项」主张成立（缺陷主张=不可观测 fallback 这一 src 可证事实，独立于触发器成立）。
3. (b)(c) 排除复核：PASS——(b) 证伪独立成立：清扫=reader task 内 inline await close（L373）+清扫前等待全有界（L352/L358 wait_for）+模块仅两 create_task（grep 亲证）+测试 0.05s tick 让出事件循环——同 loop 调度语义下「排队 >30s」无构造路径；(c) 证伪独立成立：全 kill 路径 exit code 1 无 STILL_ACTIVE(259) 生产者+探针失败方向=假死（OpenProcess/GetExitCodeProcess 失败→False→幸存集合变小，只会漏报不会误报存活）+pid 回绕在 584s run 内不可达——假阳无机制。
4. 浮报七项完备性反向抽查：PASS（含一项补充）——七项逐项亲证成立（①L494-504 双 return None+空壳分支②L513-518/L534-539 双静默 pass③L214-217 Unreapable④L360 code=None 无码上报⑤manager L242-245 except Exception: pass⑥探针 259 理论残余⑦winjob capture OSError 面=修复靶面）+处方四项[可观测化/有界重试+CREATE_SUSPENDED 治本权衡/最坏情形上报/job_captured 断言面]+次要缺口三代码位[L501-502/L513-518/L534-539]全数入册且以可裁决形态移交；反向抽查获一处未浮报面（→建议1）：stdio.py L367-371 on_unexpected_exit 回调守卫 except Exception: pass（the manager's bookkeeping must not break the teardown）亦为静默防御面，不在 ④ 七项册。
5. 门禁独立复跑：PASS——B 复跑与 C 席/调度器三方逐值同：pytest 1424P+2S+0F @331.58s（exit 0；收集 1426=基线恰合；含 test_crash_sweeps_tree 绿=本地零再现再证）+ruff clean+format 427（=426+进度件 .md）+mypy 130/0；docs-only delta 判据面说明成立（收集数/代码路径/类型面原理上零影响+实测零差）。
6. 存量兼容+处方移交完备性：PASS——⑤ 声明核（零 src/tests/fixture 触=numstat 亲证；「无代码改动即无可注入红绿面→可注入性 TDD 不适用」判定逻辑成立）；处方以用户可直接裁决形态完整移交（四项主处方每项含具体形+权衡标注[CREATE_SUSPENDED 变更面大]+可选测试面标注须裁决[断言面冻结]+次要缺口三处随同）；「再现即排程」纪律链自洽（第二现触发本票+attempt-2 绿=间歇态再证+两现幸存 pid 不同[4192/3632]=非同进程粘滞）。

## 独立复跑（temproot p2-s10-b，uv run 口径）

- pytest 全量：1424 passed+2 skipped+0 failed @331.58s（exit 0；收集 1424+2=1426=基线恰合零回归）。
- ruff check：All checks passed!（exit 0）。
- ruff format --check：427 files already formatted（exit 0=426+进度件 .md；与 C 席/调度器逐值同）。
- mypy：Success: no issues found in 130 source files（exit 0）。
- 观察面记档：1 warning（PytestUnhandledThreadExceptionWarning/Event loop is closed 间歇族，C 轮 2W→B 轮 1W 观察值差如实记）；install 族本轮全绿（WDAC 分钟级翻转链绿相位续录：p2-s9 期 {C 4F/调度器 4F/B 全量 1F→隔离 4F}→p2-s10 期 {C 0F/B 0F}，CI 双 OS 终证承载口径不变）。

## 终裁与处方

- 终裁：PASS——阻断 0/重大 0/次要 0/建议 1。
- 建议1（浮报册完备性微面，零行为缺陷）：进度件 ④ 七项册漏录 stdio.py L367-371 on_unexpected_exit 回调守卫 except Exception: pass 静默防御面（意图注释在册但未测且未浮报）——建议随 M 线修复票处方①「fallback 可观测化」同册补第 8 项或在调度器台账记档，维持「全量」主张完整性。
- 处方（供调度器执行）：①并回（p2-s10 merge --no-ff 入 main 为 #182+主树四门禁复跑+temproot main-182 预建）②翻牌（ORCH 台账 T-C1-061a DONE+票 md 32L+进度 65L+本报告随 merge 入库）③face (a) 上浮处置（B 背书 C 定谳链：转用户裁决 src 侧修复票[M 线或专项]——处方四项+次要缺口三处+建议1 补录面；处方④ job_captured 测试面在断言面冻结纪律下须单独裁决）④记档三则（WDAC 翻转链绿相位续录/1W thread 族间歇面/建议1 微面）。
- 附注非缺陷一则：进度件诊断链「await self.close()（L371）」实读为 L373（±2 行锚微漂，语义引用无歧义）——随建议1 一并记档即可。

T-B-071-PASS 0/0/0/1
sha256=9C4182DDF4BDF091EB6742E9A7E02CA7C1B42DCF7EC9406A3CFF46B648B340C9（Get-FileHash -Algorithm SHA256；本行之前文件内容，调度器截去本行对证）
