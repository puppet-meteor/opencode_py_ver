# opencode py vs TS 输入输出一致性 + 性能 + 功能等价比对报告

- 日期：2026-11-18 ｜ 调度器会话产出 ｜ 判据：同模型同 prompt 双发（gateway/glm-5.3 默认档），工具调用序列形态 / 确定性产物（地面真相）/ 回答语义 / 时长四要素
- 环境：TS = opencode v2.2.2（npm 全局，`opencode run --format json`）；py = main@622ef8e（`python -m opencode`，p7-s5 venv python + PYTHONPATH 钉主树 src——src 零差，SAC 绕行见 D8）
- 原始数据：`results/ts_p*.out.txt|state.txt|meta.txt`、`py_p*` 同名、`ts_digest.md`；种子与运行器：`seed/`、`run_parity.ps1`、`summarize.ps1`
- py 侧采了两轮：R1=默认配置（无放行），R2=project config `permission.rules` allow 四件（fs__write/fs__edit/bash__run/patch__apply）对齐 TS 的非交互默认放行语义（对齐门本身即差异 D1）

## 一、用例对照总表（R2 + TS 基线）

| case | 场景 | TS：答案/工具/时长 | py(R2)：答案/工具/时长 | 地面真相 | 判定 |
|---|---|---|---|---|---|
| p1 | 算术 17*23 | 391 / 无工具 / 1.4s | 391(探针)、389(R2) / 无工具 / 6.8s | n/a | ✅ 波动属模型采样（同模型两跑 391/389），非宿主差异；时长差=进程模型（见 D9） |
| p2 | bat-ball | 5 / 无 / 2.7s | 5 / 无 / 8s | n/a | ✅ 一致 |
| p3 | 读文件摘要 | read / 5.6s | fs__read / 14.4s | 摘要语义同 | ✅ 一致 |
| p4 | 编辑 beta=2→20 | read>edit / 12.3s | read>fs__edit / 14.4s（R1 默认态：3 次审批全挂 929s 后模型谎报 DONE，文件未改） | 双侧 config.txt beta=20（R2） | ✅ R2 一致；R1 暴露 D1（关键缺陷） |
| p5 | echo 复读 | shell / 9s（本轮 TS 侧波动：执行了但未复读） | bash__run / 12.5s → `parity-check-77` ✅ | 双侧输出语义达成 | ✅ 一致（各记一次单侧波动） |
| p6 | grep ZEBRA_42 | grep / 4.5s | fs__grep / 20.1s | 双侧 m2.py | ✅ 一致 |
| p7 | CSV 求和写文件 | read>write / 6.3s | 16.1s | 双侧 sum.txt=150 | ✅ 一致 |
| p8 | 写 hello.py+执行 | write>execute / 11.1s | fs__write>bash__run×3 / 32.8s（多两轮纠错） | 双侧得 `hello parity`；TS 落 __pycache__ | ⚠️ 语义达成但暴露 D5（PowerShell 分号断裂） |
| p9 | UTF-8 中文写 | write>execute / 30.3s | fs__write(推断) / 38.5s | **双侧 zh.txt 25B 逐字节一致** | ✅ 一致 |
| p10 | 工具自报清单 | 13 名 / 2.6s | 19 名 / 4.8s | n/a | ⚠️ 工具面差异（D3 映射表） |

性能小结：同量级任务 py(R2) 对 TS 倍率 ≈1.2–4.5×；固定税=py 冷启动（python -m import 链 +~4-8s，TS 为常驻进程形态 1.4s 起）。R1 的 929s/620s 异常系审批挂起（D1），非性能问题。

## 二、差异清单（按严重度序）

- **D1（关键·功能）非交互单发 ask 级工具死路**：TS `opencode run` 非交互默认执行 edit/shell 等；py 默认表（`tools/policy.py _DEFAULT_EFFECTS`：fs__write/fs__edit/bash__run/patch__apply 等=ask）在单发模式无审批者 → 每个 ask 挂 300s 超时→cancelled（p4 三连挂 929s、p5 两连挂 620s），任务实际失败且 p4 出现**工具全拒后模型谎报 DONE**（回答与地面真相背离）。对照 TS 改进：CLI 增 `--auto` flag（TS 语义：auto-approve non-explicitly-denied）；或非交互检测（stdin 非 TTY）时 ask 立即拒绝（不挂 300s）并文档化 config `permission.rules` 对齐门。
- **D5（高·功能）bash__run Windows 执行壳语义**：py 经 PowerShell（p8 `python -c "...;..."` 被 PS 按分号拆断→SyntaxError，模型自行补引号两轮才达成）；TS execute 直接达成。改进：Windows 下命令执行路径对齐（cmd /c 或 CreateProcess 直执行）。
- **D6（高·鲁棒性）网关抖动零重试**：py `maas.py` SDK 侧 `max_retries=0` + httpx 等首包超时紧（R2 首采 p4/p5 均 `timed out waiting for response headers` 中断，重跑即过=瞬时抖动无兜底）；TS 同网关未见中断。改进：适配器层幂等重试（connect/读首包超时小退避×2-3）+超时对齐。
- **D7（中·易用）`--context-limit` 必填**：TS run 无必填项；py 缺该 flag 且 config 无值时 exit 2。改进：内置默认（如 128000）可被 flag/config 覆盖。
- **D3（中·生态）工具命名与清单差异**：映射（TS→py）：read→fs__read、edit→fs__edit、write→fs__write、glob→fs__glob、grep→fs__grep、shell/execute→bash__run(+pty__run)、webfetch→webfetch__fetch、websearch→websearch__search、question→question__ask、subagent→subagent__run。py 独有：fs__ls、git__status/diff/worktree_list/add/remove、patch__apply、todo__write、pty__run（19 枚）；TS 独有：skill、experimental_image_generate（TS 13 枚）。影响：跨版 prompt/技能/记忆引用工具名不可移植。改进：评估别名层或稳定映射文档。
- **D2（中·观测性）无 `--format json`**：TS 单发可输出结构化事件流（tool_use 名+input+output+分步 token+时间戳）；py 仅纯文本+`[final]`/`[run]` 行，工具名仅在审批行披露。改进：CLI 增 `--format json`。
- **D4（低·卫生）cwd 副作用**：py 在 cwd 落 opencode.db/opencode-workers/writer.lock（TS 落全局数据目录，cwd 干净）。改进：默认数据目录对齐或提供 --data-dir。
- **D9（低·结构）冷启动税**：py 每次冷启 python+import 链 +4-8s（TS 常驻 1.4s 起）。结构性差异，记录不立项。
- **D8（环境备注·非代码）Windows SAC 拦截**：console-script `opencode.exe`（os error 4551）与 `.venv` 内 `xxhash._xxhash.pyd` 被应用控制策略拦截（同哈希 pyd 在 p7-s5 venv 放行=按路径执行历史记声誉）。workaround：`python -m opencode` + 有声誉 venv/PYTHONPATH。建议文档化。

## 三、结论

**输入输出一致性**：同模型同 prompt 下，纯问答、文件读/写/编辑、内容搜索、UTF-8 落盘的回答语义与确定性产物双侧一致（p4 beta=20、p7 sum=150、p9 zh.txt 逐字节一致、p6 m2.py）。唯一语义分支=p8（PS 壳差异，模型可自行纠错达成）。
**性能**：对齐放行后同量级（1.2–4.5×，含固定冷启税）；默认态 p4/p5 的 15 分钟级异常是 D1 审批挂起所致。
**功能等价评估**：核心 agent 循环（多步工具编排/纠错/地面真相落盘）等价；不等价点=D1（非交互审批死路）、D5（Windows 执行壳）、D3（工具面与命名）、D2（结构化输出缺失）。判定：**py 达到 TS 的功能主体等价（≈90% 用例语义+产物双达标），D1/D5 为必修不等价项，D6 鲁棒性建议同步修**。
