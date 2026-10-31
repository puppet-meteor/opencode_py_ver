# Phase 2：按需扩展模块

> 核心前提：[Phase 1](PHASE1-SLIM.md) 的验收通过。契约以 [CORE-CONTRACTS](CORE-CONTRACTS.md) 为准。
> 每个模块都是一次增量交付；基础测试、权限、取消和迁移验证随模块完成。
> 下列工期是单人全职 + AI 的粗估，在模块 spike 后复估，不能据此承诺 TS 全量等价。

## 1. 模块与真实依赖

| 模块 | 核心范围 | 前置 | 增量开发周 |
|---|---|---|---:|
| M1 存储扩展 | workspace/kv、已用库升级、同机多进程协调 | S4/S6/S2 | 2–4 |
| M2 本地执行扩展 | watcher、shell 解析、git、PTY；可选子项 | S3/S5/S6 | 3–5 |
| M3 插件 | 受信任 Python 插件 + 有限生命周期 hooks | S3/S5/S6 | 2–3 |
| M4 codemode | 受限代码解释执行的可用子集 | S1/S3/S6；M3 可选 | 2–4 |
| M5 client/server | 单用户本地 HTTP + Python 远程客户端 | S1/S2/S3/S4/S6 | 2–3 |
| M6 能力卡片 | skills、subagent、HTTP MCP、compaction、媒体等 | 按子卡片 | 单独估算 |
| M7 差异/负载验证 | 所选路径的深入测试与运行基线 | 已选能力；基础测试一直执行 | 1–2 |

范围假设：M1 限同机 SQLite；M3 限受信任插件；M4 不含完整 TS 解释器移植；M5 限单用户本地服务。跨主机存储、恶意插件隔离、多租户平台分别立项，不塞进这些数字。

组合预算含 Phase 1：core 6.5–9 周；core + M5 为 8.5–12 周；core + M1 + M5 + M7 为 11.5–18 周。其余模块按选择逐项相加，并为集成重新评估；M6 没有一个“全部两周”的总额度。

### 依赖图

    Phase 1 ─┬─ M1 存储/同机多进程 ──────────────┐
             ├─ M2 本地执行 ── git 子项 ────────┤→ M6 worktree/snapshot
             ├─ M3 受信任插件 ── 可选 hooks ────┐
             ├─ M4 codemode ←──────────────────┘
             ├─ M5 单用户本地 client/server
             └─ M6 各能力：skills / subagent / HTTP MCP / compaction / 媒体

M5 首版不依赖 M1：一个 server 进程持有 writer，多个客户端通过它调用。ASGI 必须以单 app/单 worker 启动（禁止 `--workers > 1`、多进程 reload 和同库第二个 server），启动时获取 Phase 1 的数据目录锁；检测到第二个 writer 直接失败并报告 `Conflict`。多个 server 进程共享同一数据库才需要 M1。M7 是所选版本的额外验证阶段，Phase 1 和每模块已有基础质量门。

## 2. 扩展与完成规则

每张卡片开始时列出：用户场景、必需契约、修改点、可选依赖、失败行为、fixture、安装/停用方式和数据升级方案。

- 新代码放模块目录；允许修改 `app.py` 组合入口、依赖声明、配置注册和相关 adapter，不承诺零改动。
- 无真实变化的接口不提前抽象；新能力优先组合 adapter 或增加独立 capability，Protocol 必需方法变化需要版本评估。
- 新 event/content 不意味着旧消费者能理解；进行能力协商，未知必需类型明确报错。
- 可选依赖使用 extras/延迟导入；未安装模块不影响 core 启动，调用时报 UnsupportedCapability。
- 新表/字段追加 Alembic revision，以真实 Phase 1 数据验证升级；停用模块保留数据。回退代码与回退 DB 分别记录，不能只打 tag。
- 每模块同时交付正常路径、错误、取消、秘密处理和适用平台测试。标签不代替验收证据。

## 3. M1：存储和同机多进程

**改动点**：S4 迁移/事务、S6 owner 检查、S2 follow 适配；不是只新增 repo 文件。

- 从 Phase 1 真实旧库升级，增加 workspace/location/kv 的最小模型；不复制 58 条 TS 迁移或要求物理 schema 相同。
- 如确需多进程，替换独占数据目录锁为 DB 协调；定义 owner、租约、递增 fencing token，并在每次 commit 验证所有权。
- 跨进程 follow 仍以 DB cursor 为准，可先轮询；通知只用于加速。保存的权限若扩成跨 session 规则，必须增加明确宿主设置和作用域，不能扩大旧 always 含义。
- Phase 1 原始日志保留。日志裁剪/归档先设计 cursor 过期、snapshot/checkpoint 和恢复策略；M1 首版不以“清理日志”为验收。
- SQLite 只支持经过测试的本机文件系统；跨主机/网络盘共享及 PostgreSQL adapter 另立子模块。

**验收**：Phase 1 旧库数据保留；双实例只有一个 owner，过期 owner 的 DB 写被拒绝；cursor 重连无 durable 缺失；升级和备份恢复实测通过。

fencing 只能约束提交，不能撤销已经执行的 shell/MCP。接管未结束 run 时仍标 unknown，不能靠租约自动重跑副作用。未验证进程协调前保留单 writer 模式。

## 4. M2：本地文件和执行扩展

**改动点**：tools adapter、ConfigSnapshot 的显式 reload capability、S6 资源管理；拆子项验收。

| 子项 | 约束和退出条件 |
|---|---|
| watcher | watchfiles 等库验证后接入；去重/合并变更，只提示或在 idle 显式创建新快照，不改变运行中审批 |
| shell 解析 | Bash/PowerShell 各自打样；以选定 TS 用例比较行为，解析失败继续 ask/deny；AST 分析也不是 OS 沙箱 |
| git | 先 status/diff，再提供受审批的 worktree 操作；记录 cwd/roots 和冲突，禁止静默覆盖未提交文件 |
| PTY | Unix/Windows 分别验证 resize、Ctrl-C、编码、进程树退出，再确定库；未验证的平台不开启 |

**验收**：每个已选子项有平台与取消测试；运行中的配置快照不受文件事件偷偷修改；现有 S3 权限和退出行为不回退。仅需要 git 的用户不必安装 watcher 或 PTY。

## 5. M3：受信任插件

**改动点**：配置发现、app 组合、PromptBuilder、工具 catalog 和 result 处理的集中挂载点。

- 首版显式加载用户信任的 Python 包/entry_points，记录名称、版本和来源；不自动执行仓库指令声明的插件。
- 有限 hooks：构建系统提示、修改 catalog、校验前准备工具输入、处理工具结果、观察 run 结束。输入修改必须在最终 schema/权限校验前；之后修改则重新审批。
- 规定顺序、超时、失败策略、取消/close 和重入规则；权限/输入准备 hook 出错 fail closed，观察类 hook 出错只记诊断。
- 框架调度的工具调用仍经 S3；插件返回的模型展示内容与原始执行结果区分，不能覆盖审计事实或把 failed 改成 succeeded。
- 禁用在下一个 idle/新 session 生效；资源关闭、catalog 移除和配置缓存刷新有测试。

**验收**：一个插件修改提示并注册工具，干净卸载；两个 hook 顺序确定；异常不会跳过审批；新增依赖未安装时 core 正常。

Python 进程内插件拥有宿主进程权限，无法通过 entry_points 或 hooks 阻止其自行访问文件/网络。首版只接受受信任插件；不可信插件需要进程隔离、OS 限制、IPC/secret 设计和独立估算。

## 6. M4：codemode

**改动点**：新的执行 adapter、S3 catalog 映射和 S6 执行任务；M3 是可选接入方式。

先确定要支持的 TS Code Mode 语法子集和工具语义，再做解释执行与资源控制 spike。2–4 周仅预算可用子集，完整移植 `packages/codemode` 的解释器、标准库和兼容测试需单独拆项。

- 所有实际工具调用仍经 invoke；不能把“只展示几个工具”当作代码执行能力。
- 不用裸 Python `exec/eval` 执行模型代码。解释器没有宿主 import、任意文件、网络或进程访问入口，工具 catalog 是显式允许能力。
- 计量指令/循环、内存和输出；执行任务有 execution_id、截止时间、取消与资源回收。必要时使用受控子进程，仍需验证平台限制。
- 不能保证限制的引擎不默认启用；spike 失败先停在 prototype，不宣称沙箱。

**验收**：支持语法的正常用例、未知语法、越权工具、无限循环、超限/取消都有测试；失败不能继续沿用旧执行上下文。

## 7. M5：单用户 client SDK + server

**改动点**：新 HTTP/SDK 目录、app 生命周期、可选依赖和错误到 HTTP 的映射；不重写 runner。

- FastAPI 适配 SessionService 的 create/prompt/cancel/reply_permission/messages/run_status，查询和事件 feed 共用 core 契约；生成最小 OpenAPI 和 Python 客户端。
- 进程模型固定为一个 server 进程、一个 ASGI worker、一个 SessionService/任务表；开发 reload 必须关闭或使用不会复制 writer 的受控方案。多 worker、多 server、热 reload 造成的任务/锁分裂在 M1 前均为 `UnsupportedCapability`。
- durable SSE 使用 EventLog.follow；Last-Event-ID 编码 session+seq，拒绝跨 session/超前游标。断线从 cursor 补读、按事件 ID 去重；首版不承诺回放 ephemeral token。
- 默认仅 loopback、单用户 token 认证；审批只接受已认证宿主通道。限制请求/并发/输出大小，校验 Origin/CORS，防止网页借本机服务执行命令。
- 相同 request_id 同样输入返回原 run；不同输入冲突。HTTP prompt 返回 RunRef，客户端断线不默认取消 run；显式 cancel 和服务 shutdown 分别测试。
- 服务退出由 SessionService 回收任务。慢 SSE 客户端不能阻塞 runner，断开后可以补 durable 日志。

**验收**：客户端创建 session、prompt、读终态；断线/重连、重复请求、错误码、未认证请求、审批和 shutdown 都有集成测试。以单 worker 启动为正例，第二 worker/server、reload 复制或数据目录锁冲突为明确失败用例。

只选 M5 即可在单进程服务中接入外部 UI。多进程共享库先做 M1；公网/多租户需要额外的 TLS/部署、用户隔离、配额和权限设计，不包含在 2–3 周预算。

## 8. M6：延期能力必须有卡片承接

每项都能单独选择；工期另加在所选组合上。这里的“前置”既包括模块，也包括必须扩展的契约。

| 卡片 | 增量周 | 前置/修改点 | 最低验收 |
|---|---:|---|---|
| skills | 0.5–1 | S5 发现、S6 指令装载、S3 读取；无需 M3 | `SkillCatalog.list/activate`；激活记录 source/path/hash/size；host 显式选择或受限触发，内容不授予权限，快照/恢复行为有 fixture |
| subagent | 1–2 | S6 子 session/结果递送 capability、S3 宿主委托；无需 M3 | `SubagentRef(parent_run_id, child_session_id)`、enqueue/ack/dedupe；完成结果 durable，父取消/关闭回收；权限不大于父级，限制递归/并发/预算 |
| HTTP MCP | 0.5–1 | 已有 MCP bridge、S3 catalog、ConfigSnapshot transport/auth refs | streamable-http 最小 fixture；endpoint allowlist、认证引用轮换、超时/断线 unknown、catalog version/旧批准失效；完整 OAuth 另计 |
| 自动 compaction | 1–2 | S1 ContextBuilder、S6 新事件/投影 | `context.compacted(covered_seq, retained_from, summary_hash, model, usage)`；阈值/锁/CAS 明确，不拆 call/result；失败/取消不替换原上下文 |
| 第二 provider | 1–2 | S1 request/chunk/content capability、价格/用量映射 | 同一 fake/真实契约套件通过，不支持内容显式拒绝，provider native 数据可往返 |
| image 输入/输出 | 1–2 | S1 FileContent、受控 URI resolver、provider 支持 | 大小/MIME/模型能力验证，拒绝任意路径/URL读取；文本工具结果仍兼容 |
| websearch | 0.5–1 | S3 新 adapter、API secret ref | 一家 API、引用来源、超时/配额/错误测试；搜索内容不构成批准 |
| voice | 1–2 | S1 媒体输入输出、转写/语音 API | 格式/大小/成本和取消明确，依赖未安装不影响 core |
| worktree/snapshot | 2–3 | M2 git + M1 的最小 workspace 存储 | 显式目录/快照引用和磁盘配额；失败清理、冲突和恢复有测试，不自动撤销未知外部副作用 |

上述四项的接口/事件是各模块开始前需要补充的契约，**不属于 core 0.1 已实现能力**；建立 fixture 并复估后才进入实现，不要求一期提前建空扩展框架。

skills 的激活是可审计命令：`activate(skill_id, source, expected_hash)` 把经大小/路径/信任检查的正文写入 instructions snapshot；模型只能建议，宿主策略决定是否激活。durable activation 保存 bounded 正文、source/path/hash/size；恢复使用已保存内容，不悄悄重读磁盘。重新激活时 hash 变化要求确认。

subagent 必须有 `parent_run_id`、递归深度、并发/总 token 预算和 `child.completed/failed/interrupted` 结果事件。父 session 重启后通过 `child_session_id` 查询，子任务终态已提交则幂等投递/ack；父取消由 core cancel 流程收回子 run，未开始调用 cancelled/not_started，已开始无结果的调用 unknown。任务结果进入父会话的持久队列，不能直接修改父 runner 内存。

HTTP MCP 的 ConfigSnapshot 新增 transport、endpoint allowlist、auth_ref、server identity 和 catalog version。工具调用携带该版本；tools/list 或身份变化使旧 approval 失效。断线期间已 started 调用标 unknown，重连只为下一次显式调用服务。

compaction 以新 `py.context.compacted` 事件记录有效上下文替换，payload 至少含 covered_seq、retained_from、summary_text、summary_hash、model、usage、message_count。锁内捕获 settled 历史边界/hash，释放锁后生成摘要，再在短提交锁内用 expected_seq/CAS 校验；边界变化就丢弃该摘要。保留原始事件和工具结果，ContextBuilder 从已保存的 replacement + retained history 生成，永远不拆 call/result。摘要失败/取消/崩溃不替换原上下文，额外模型调用也记录 usage；物理删除原日志另走 M1 归档设计。

subagent 完成回报需要新增显式队列/命令能力，不能直接改父 runner 的内存历史。父 session 正忙时延后消费；不把 Phase 1 不支持 steering 的接口当成已具备。

## 9. M7：所选版本的进一步验证

**不以移植多少行测试衡量质量。** 先选 session/permission/tool/provider 的关键行为，形成 TS 用例到 Python 测试的可追踪清单；差异更新 parity。

额外 1–2 周预算用于所选范围的：多 session/owner 冲突压测、慢消费者/大输出、流/事件/路径输入 fuzz、长时间运行资源泄漏检查和备份恢复演练。测量并记录启动、首 token、事件延迟、内存与测试环境，缺少测量不写“性能对齐 TS”。

发布门：

- 所有已选卡片的正常/故障/取消测试、升级 fixture、适用平台 CI 通过；
- 不安装任何可选 extras 时 core 仍可用；
- 旧 JSON/DB fixture 和旧客户端支持范围有记录；
- 新增 event/capability 的 ADR、停用/升级/回退说明齐全。

M7 可在每个对外发布点执行；未选的模块不阻碍发布。问题回归测试回到对应模块，不等最后统一补写。

## 10. 选择顺序

- 本地 CLI 自用：Phase 1 已能交付；按需要增加 skills、compaction 或 M2。
- 程序化同进程使用：Phase 1 SessionService；需要跨进程/外部 UI 再选 M5。
- 同机多个执行服务：Phase 1 → M1 → M5 → M7；跨主机另设计。
- 扩展生态：先 M3 受信任插件；codemode 根据实际脚本需求选 M4。
- worktree/媒体：只做卡片列出的依赖，不强迫安装全部 Phase 2。
