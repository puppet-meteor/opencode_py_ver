# T-PL-MEM-001（v2，评审修订稿）「Agent Memory 长期存储未来规划方案书」规划票任务包

> v2 说明：经三席独立评审（架构完备性/治理合规/华为云落地，各有条件通过）修订；12 项跨席必修全部应用。本文件为可直接派发版本。

---

## 【角色章程】

- **执行席位**：B（规划评审席；模型 GLM-5.3#max；固定会话 ses_orch_B；主树工作）。
- **职责边界**：仅撰写方案书。无裁决权、无实施权、无排期权；遇本包未定事项**停笔回报调度器**，不得自行假设。
- **写权限（显式白名单，唯一写面）**：`doc/plans/memory-longterm-plan.md`（路径固定，本票唯一新增文件）。白名单外禁止创建/修改任何文件。
- **禁止 git 写操作**（add/commit/push/checkout/reset/stash 等一律禁止，只读 git 允许）；产出仅落盘工作区，由调度器统一入库。
- **止损（分档）**：核心章（§0–§10）90min；含 §11–§13 全量 180min。触发止损即停笔，按回报格式回报已完成章节与未完成清单，产出保留工作区，由调度器决定续票或收敛。
- **撰写语言**：中文。防截断纪律：增量写文件，勿单回合超长输出。
- **交付件组装**：本任务包 = 角色章程 + A 节（硬输入）+ B 节（证据表）+ C 节（任务与骨架）三部分随包整体派发，无外部悬空引用。

## 【A. 背景与既定边界】（硬输入——撰写约束，非已裁决结论，与 C 节状态行口径一致；不得改写）

1. **宿主**：opencode-py（opencode 2.2.2 的 Python 移植），六接缝 S1–S6、事件溯源、单一 durable 写入路径；Phase 1 已收官（pytest 1139/mypy 110 基线），Phase 2 进行中；部署形态 = 平台沙箱，用完即弃，**沙箱无状态化为目标**（因果链：沙箱无状态 ⇒ 记忆必须外置 ⇒ 会话数据须在沙箱存活期内流出）。
2. **选型**：外挂 TencentDB-Agent-Memory 2.0.3（开源，Node ≥22.16；核心 MemoryCore gateway，可选 MemoryProxy/MemoryPanel/MemoryKnowledge）。**不进宿主进程、不与沙箱合镜像**。理由：(a) 记忆是第二持久库，吸收将违反宿主单一 durable 写入路径契约；(b) L0→L3 提取产物非确定性、不可重放，不能作为宿主事件溯源状态，只能外置为投影存在；(c) 多运行时（Python+Node）耦合。补强理由（方案书 §1 展开）：记忆生命周期独立于沙箱（须比沙箱存活更久）、跨沙箱多租户共享、凭证隔离（中心侧凭证不进沙箱）、独立升级节奏。
3. **接入缝**：宿主侧走 S3 工具缝——注册三工具 `memory_search` / `conversation_search` / `read_scene`，经 registry/invoker/policy 链（审批默认 ask），失败走既有 error→unknown 语义。**fail-soft 精确口径**：读工具失败返回结构化错误载荷（服务不可用），语义层标 unknown、主链路继续；**禁止将 unknown 折叠为「无相关记忆」断言**；审计日志须区分 empty-result 与 service-unavailable。工具实现内部使用官方 Python SDK `tencentdb_agent_memory`；契约锚 = v2/v3 REST（Bearer + serviceId）。
4. **ask 的 headless 语义（方案书 §4/§7 展开）**：headless 无交互审批者时，ask = 拒绝并按 fail-soft 降级（不挂起主链路）；审批粒度候选（逐次调用/逐会话/逐 space）登记为待裁决。
5. **云约束（硬约束）**：华为云。计算 = CCE（标准 K8s）；存储映射：DCS（Redis 协议→分布式锁+任务队列，协议级直连，**实例类型选型见 S-4**）；CSS（Elasticsearch/OpenSearch→L0/L1，需自写 IMemoryStore 适配器，召回融合复用产品客户端 RRF 路径，**向量原语可用性见 S-5**）；OBS（版本化→L2/L3 文件，需自写 IStorageBackend 适配器，**append 语义见 S-2**）。**SFS 不承载任何数据库底座**（SQLite-on-NFS 禁忌）；仅可作纯文件交换面（seed/export），若采用须声明 close-to-open 一致性口径与清理生命周期（归 U-3）。
6. **HA 模型**：不自建主备；gateway 无状态多副本（Deployment ×N + Service/LB + /health 探活，readiness 含 DCS 连通性检查）。**一致性分三层（措辞钉死，禁止笼统「强一致」）**：(1) 写路径单点串行（Redis 锁+写确认）；(2) 检索可见性按服务语义分别定义——OBS 单对象强一致/多对象与 list 最终一致、CSS 近实时（写后立读需 refresh=wait_for 或读主分片）、DCS 异步主备复制（切换窗口可能丢锁丢写，由 S-4 幂等兜底）；(3) 语义层最终一致（L1/L2/L3 异步 LLM 管线：每 5 轮对话触发 L1、L2 每 5min、L3 每 10min；多副本轮次计数语义待验证）。L1+ 为派生数据，**可由 L0 全量重建**（队列丢失的补偿基线）。
7. **多租户**：serviceId/spaceId 隔离，分配粒度待裁决（U-2，含 CSS 索引隔离策略：共享索引+过滤器 vs index-per-tenant）。MemoryProxy 透明代理路线降级为 PoC 备选（绕审计链 + 首轮交互式 session-init 不匹配 headless）。
8. **演进路线**：P0 单机 PoC（合成数据优先，真实数据须脱敏并过审批；退出判据=三工具贯通+fail-soft 演练+一次完整 L0→L3 提取闭环）→ P1 适配器 spike（S-1~S-9）→ P2 产品化能力卡（单租户单副本试点）→ P3 多副本+多租户（副本>1+租户>1+隔离审计）。P0/P1 部分可并行（S-3/网络类 spike 不依赖 P0 产出）。**当前全部未排期**。

## 【B. 证据表】（锚点事实，可经 2.0.3 官方开源源码包独立核验；核验日期 2026-11-09；方案书中锚点须直指源码包/宿主文件，禁止锚到任务包或方案书自身）

| # | 事实 | 锚点 | 含义 |
|---|---|---|---|
| 1 | IStorageBackend = 对象存储语义接口（~9 方法），现实现 local\|cos\|rowfs\|mongofs | MemoryCore/src/core/storage/types.ts | OBS 适配器=新增实现；mongofs 证明非 COS 可行 |
| 2 | appendObject 依赖 COS Append API + APPENDABLE_KEY_PREFIXES 守卫 | 同上 | OBS append 需 spike（S-2）；兜底=分块模拟 |
| 3 | IMemoryStore = 数据库抽象，DbChoice kind=mongodb\|tcvdb\|sqlite | src/core/backend-selection/store-configs.ts | CSS 适配器落点 |
| 4 | 召回融合双路径：TCVDB 服务端 hybrid 短路；SQLite 路径客户端 RRF（k=60） | src/core/hooks/auto-recall.ts L519/L531 | CSS 适配器复用客户端 RRF |
| 5 | IConfigSource/IQuotaReporter vendor-neutral 注释 | src/core/abstractions/types.ts | 上游 PR 机会（接受度未承诺，U-4） |
| 6 | service 模式=TCVDB+COS+Redis；K8s 示例、/health、TDAI_DEPLOY_MODE、端口 8420/3100 | README.deployment.md | CCE 部署照搬；模式切换客户端零改动 |
| 7 | MemoryProxy(:8096) 透明代理+首轮交互 session-init | MemoryProxy/README.md | 降级 PoC 备选依据 |
| 8 | 官方 Python SDK：包名 tencentdb_agent_memory | sdk/memory-core/python/pyproject.toml | 宿主侧客户端载体 |
| 9 | 迁移先例 migrate-sqlite-to-tcvdb | MemoryCore/scripts/migrate-sqlite-to-tcvdb/ | sqlite→CSS 迁移参照（同端点迁移，**不覆盖重嵌入问题**→S-6） |
| 10a | 宿主 CLI 参数面 --base-url/--api-key 已存在 | 宿主 src/opencode/cli.py | 接入缝现状 |
| 10b | 宿主 MCP stdio client 已在核心 | 宿主 src/opencode/mcp/（R4 交付） | 同上 |
| 10c | M6 HTTP MCP 卡未落地 | 宿主 doc/plans/p2-slices.md | 接入缝差距 |
| 11 | 参考源码：TencentDB-Agent-Memory 2.0.3（官方开源源码包） | github.com/TencentCloud/TencentDB-Agent-Memory | 方案书内引用口径（本机缓存路径不入方案书） |
| 12 | 宿主治理口径：ROADMAP §5 + CORE-CONTRACTS 六接缝 | 宿主 doc/ROADMAP.md §5、doc/CORE-CONTRACTS.md | §1/§10 治理参照 |

锚点纪律：锚点不达标条目须补勘至文件级后方可写入方案书。

## 【C. 任务】：撰写方案书 doc/plans/memory-longterm-plan.md

**§0 文档状态行（硬性）**：状态：未来规划（**未排期、未裁决、不落地**）；所有架构决策均为候选、未经 A 席裁决；所有验证项未经 spike 实证；撰写日期/产出席位/证据基线三要素必须写明。

**章节骨架（§号固定，§0–§13）**：
- §0 状态行 + 一页摘要
- §1 目标与非目标 + **替代方案与排除论证**（自建记忆/本地 stdio MCP/其他记忆产品为何不选）；非目标：不进核心、不做 TS wire 兼容宣称、不给总工期承诺（宿主 ROADMAP §5 口径）
- §2 架构总图：三层（沙箱侧客户端/CCE 中心服务/托管存储）+ **双向数据流**（读路径 + **写/摄入路径**，摄入路径候选并列禁预写，归 U-5）+ 网络面与信任边界
- §3 组件与职责（gateway×N、DCS、CSS、OBS、LLM/embedding 端点、Panel 运维面含其华为云部署形态候选）；附计算公式型成本表（非总额承诺）
- §4 接入设计（S3 三工具 + policy/审批含 ask 的 headless 语义 + fail-soft 精确口径；MemoryProxy 降级理由）
- §5 华为云适配工程**四件套**：适配器×2 + conformance 测试（参照上游测试组织）+ E2E smoke + 迁移（双写-比对-切换-回收四步单列）；工作量表述以 spike 结果为前提再估、任何周期数字仅覆盖适配器本体；fork vs 上游 PR 两条路线对比与各自决策要件（**选择归 U-4，不预设双轨或单轨**）
- §6 HA 与一致性模型：三层一致性表 + **故障域九项**（gateway 挂[liveness/readiness 分列]/Redis 挂[含主备切换双锁]/CSS 抖动/OBS 抖动/LLM·embedding 端点不可用/网络分区/CNI·egress 策略/凭证过期轮换/升级窗口）×三列（受影响路径[读/写/管线]×层[L0–L3]×恢复动作[重跑/补跑/熔断]）+ 定时任务所有权模型（集中调度或 Redis 锁 leader）+ RPO/RTO 候选值
- §7 安全与多租户（Bearer、serviceId 策略+CSS 索引隔离、审批默认 ask、凭证注入、**数据生命周期与删除语义**）
- §8 演进路线（P0→P3，每阶段触发/退出/回退三要素，**退出条件必须含量化阈值与失败即止损条款**；P1→P2 = 立项裁决点显式标注；当前全部未排期）
- §9 风险与开放问题（fork 维护、embedding 端点与召回质量、管线幂等、OBS append、上游 v2.0.x 演进、**成本模型、数据合规/保留策略、多副本重复提取**）
- §10 与宿主治理体系的关系（M6 外挂簇、A 座裁决位；**不修改 CORE-CONTRACTS/六接缝语义，仅经 S3 既有扩展点接入**）
- §11 容量与成本模型（CSS 索引/OBS 增长/副本×QPS/管线 token 消耗×沙箱数）
- §12 可观测性与审计（管线 lag、召回命中率、工具错误率；**中心侧读写审计日志补位**——S3 路线只审计宿主侧读，管线写在中心侧）
- §13 数据生命周期与合规（保留/删除/被遗忘权/PII/备份恢复/**L1+ 由 L0 重建策略**）

**待裁决位（只列选项+利弊，禁止预写结论）**：
- U-1 沙箱出网能力与网络路径（配套 S-7 实测）
- U-2 共享粒度（一池 vs 分池→serviceId 策略 + CSS 索引隔离策略）
- U-3 沙箱生命周期与 seed/export 需求（含是否经 SFS 交换面及一致性口径；含沙箱存活期内数据流出时点）
- U-4 fork vs 上游 PR 维护策略（配套 S-8 实测证据）
- **U-5 摄入/写路径**（候选并列：(a) 显式 S3 写工具走审批链；(b) 宿主侧 SDK 定向推送/事件导出桥；(c) 网关代理模式——方案书必须给出设计或显式登记待裁决，**不得空缺**）
- **U-6 S3 工具集边界**（是否增补：显式记忆写入/遗忘工具[与 U-5 联动]、枚举原语[list spaces/scenes]、read_scene 粒度与分页）

**Spike 验证项（只登记，P1 内容）**：S-1 IMemoryStore 接口面勘察+上游测试组织+调度器多副本行为；S-2 OBS append 语义 vs COS Append+追加对象限制矩阵+限流延迟实测；S-3 MAAS /v1/embeddings 可得性+模型/维度/限流清单；S-4 DCS 实例类型选型+命令/Lua 兼容矩阵+主备切换窗口/丢锁率实测+任务重跑幂等；S-5 CSS 版本选型与检索原语实测（dense_vector/kNN 可用性、中文 analyzer 选型含插件可装性、refresh 语义）；S-6 embedding 模型选型与召回质量基线（recall@k 对照+存量重嵌入判定）；S-7 沙箱→中心网络实测（延迟 P99/并发上限/断连重试→fail-soft 阈值）；S-8 fork 与上游 v2.0.x dry-run merge 成本实测；S-9 华为栈最小 E2E smoke（CCE+DCS+CSS+OBS+MAAS 写入→提取→召回闭环）。（可选 S-10：DR 演练）

**禁止事项**：不写任何代码；不改宿主与 Agent-Memory 源码包任何文件（方案书为唯一写面）；不预写任何裁决；不宣称未验证事实为已验证（推测性内容显式标「待验证」）；不给"全量 TS 对齐"式总工期承诺；文档内代码/配置示例必须标注「示意，未编译未测试」。

**产出**：`doc/plans/memory-longterm-plan.md`（唯一写面）。

**验收 checklist**（调度器逐项复核）：
1. [机器] git status --porcelain 仅 `?? doc/plans/memory-longterm-plan.md` 一条，其余零改动
2. [机器] §0–§13 标题齐全（grep）；状态行含「未排期/未裁决/不落地」精确串
3. [机器] 证据表 ≥13 条（含 10a/10b/10c 分计）且锚点为路径样式；U-1~U-6、S-1~S-9 编号与数量存在
4. [机器] P0–P3 各阶段含「触发/退出/回退」关键词；全文「已验证/已支持/已完成」token 扫描（命中转人工判合法引用）
5. [人工] 每节含 ≥1 表或 ≥3 实义要点且无占位文本；U/S 项开放未预写的语义判断（无「双轨」式暗含倾向）；写/摄入路径有设计或显式登记待裁决（不得空缺）；锚点内容抽查 3 条实核
6. [人工] 故障域九项×三列展开；一致性措辞无笼统「强一致」

**回报格式**：正文 1–2 段 = 状态/章节清单/证据条数/U/S 清单/止损档位与用时 + §验收自检表逐项 ✓/✗ + 未决问题（如有，停笔回报项单列）。

## 【派发与登记说明】（调度器侧动作，不入方案书）

- 本票登记台账（T-PL-MEM-001，类型=规划产出票，状态=未排期），**不占 Phase 2 检查点号/序列位**；入库 commit message 引用票号。先例：T-B-054→p2-slices。
- 派发命令（先例 §4.1）：
```powershell
opencode run --model "gateway/glm-5.3#max" --session "ses_orch_B" --auto `
  "Read the file doc/orchestration/tasks/T-PL-MEM-001.md and execute the instructions in it." `
  > "doc/orchestration/logs/T-PL-MEM-001.txt" 2>&1
```
