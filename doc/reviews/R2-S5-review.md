# R2-S5 结构化代码审查（席位B · T-B-017）

- 审查对象：worktree `C:/Users/lyu/.local/share/opencode/worktree/f5ec2d/r2-s5`，分支 r2-s5，单提交 `1b3d210`（父=fdb979f；`git diff main...r2-s5` = 8 files +2541/-0，全部新增，porcelain 干净——进度文件随提交入库）。
- 基准：CORE-CONTRACTS §5.1 L247-259 / §5.2 L263-268 / §5.3 L270-277 / §2.1 L35-53/L72 / §2.3 L100 / §3.1 L125-135 / §7 L341；r2-slices §2 R2-S5（L111-122）；TS 原始实现逐锚核验；进度文件 T-C1-016.md（③ 24 + ④ 13 直接输入）。
- 三源文件/两测试文件全文逐行读毕；两处专项实证运行（见 §2 M-1 与 §6）。

## 1. 结论

**有条件通过。** 门禁面调度器已核（pytest 455 = 410+45 零回归 / ruff / format 143 / mypy 51），本次未重跑；写权限面 = 授权清单精确（8 路径全新增）。12 个审查维度中 11 个维度契约符合性成立且测试断言强度充分；维度 1 的专项检查（T-B-016 §6-6 闭环）**发现一个 major：wire 名可逆/碰撞注册门不完整**（M-1，实证在案）。M-1 为潜伏缺陷（R2 唯一注册 `fs__read` 无边界下划线，无现行为破坏、无数据损坏路径），不构成 blocker；但 §5.1 L253 "模型名通过可逆/碰撞检测映射" 为契约字面要求，且修复面（registry.py + 2 个测试文件）全部落在 r2-s5 已授权新文件内，**应在合并前以追加提交修复**（修复包建议含 m-1/m-2/n-1 清理与 ③-7/③-9 两测，见 §6）。修复后无需全量复审，B 复核修复 diff 即可。

## 2. 发现分级

计数：**blocker 0 / major 1 / minor 2 / nit 3**。

### M-1（major）wire 名可逆/碰撞门不完整——单下划线边界族穿透 register()

- 位置：`src/opencode/tools/registry.py:58`（`_NAME_PATTERN`）、`:159-160`（pattern + `"__" in part` 检查）、`:181-195`（snapshot 的 `_by_wire_name` 构建）；交叉面 `src/opencode/ai/maas.py:99-111`（`parse_tool_wire_name`，partition 首 `__`）。
- 理由：注册门只拒 pattern 不符与含 `__` 的两半，**放过"namespace 以单个 `_` 结尾"与"name 以单个 `_` 开头"**。后果二支，均已实证（worktree 实跑 + 主树 maas 实跑）：
  1. **同 wire 名碰撞且静默遮蔽**：`("fs","_read")` 与 `("fs_","read")` 的 (ns,name) 键不同、双双通过 register（L170/L173 两道重复检查均不触发），但 wire 名同为 `"fs___read"`；snapshot 的字典推导后写覆盖先写——provider 可见 `specs` 含**两条**，`resolve("fs___read")` 只认 `("fs_","read")`，另一条 provider 可见而不可调用。违反模块 docstring 自称的 "provider 可见=可调用" 不变量与 §5.1 L253 "可逆/**碰撞检测**" 字面。实跑输出：`specs: [('fs','_read'),('fs_','read')]`、`resolve('fs___read') -> ('fs_','read')`、2 注册 / 1 wire 键。
  2. **maas 首 `__` partition 往返断裂**：maas.py L102-104 docstring 明写 "assuming a `__`-free namespace (the registry's naming concern, R2-S5)"——但 `__`-free 不充分：`("fs_","read")` 合法注册后 wire=`"fs___read"`，`parse_tool_wire_name("fs___read")` → `"fs._read"`（实跑），正确往返应为 `"fs_.read"`。S3↔S5 接缝假设不成立，T-B-016 §6-6 的闭环**未合上**。
- 根因注记：`_NAME_PATTERN` 处注释 "so the wire name splits back into exactly (namespace, name)" 为不实不变量声明。首 `__` partition 可逆的充要条件是 namespace 不含 `__` **且不以 `_` 结尾**（name 侧无需约束）。TS 基准自身不背书更强保证：runtime.ts L287 `normalizedName` 是字符替换而非拒绝、L289 `effectiveName` 用单 `_` 连接（py 的 `__` 方案系 provider spike §6-2 的 py 侧设计，其可逆性承诺只能由本注册门兑现）。
- 建议（最小修复）：register() 在 pattern 检查后加一行自校验 `f"{ns}__{name}".partition("__") == (ns, "__", name)`（等价于拒绝 namespace 含 `__` 且不以 `_` 结尾；name 侧维持现状）；测试补钉定：拒 `("fs_","read")`、接受并往返 `("fs","_read")`→wire `"fs___read"`。附带消解碰撞支（唯一幸存解释者确定）。
- 阻塞性：非阻塞合并的现行为，但**合并前修复**（见 §6 裁决清单第 1 项）。

### m-1（minor）`_select_page` 循环后第二 `if` 数学不可达——未浮报分支

- 位置：`src/opencode/tools/builtin/read.py:194-195`。
- 理由：`clamped = lines[offset-1:]` 为全量切片。若循环 break（limit 或预算），`next_offset` 已由首支赋值；若不 break，则 `len(selected)=len(clamped)≤limit`，且当 `=limit` 时 `offset-1+len(selected)=len(lines)`，使守卫 `offset - 1 + len(selected) < len(lines)` 恒假——第二支任何输入下不可达（逐案穷举证明：len(clamped)>limit ⇒ break；=limit ⇒ 严格小于为假；<limit ⇒ 首合取为假）。零行为影响，但属 ③ 清单外的未浮报不可达分支（维度 11 漏报抽查命中），且对审查者是"看似可达"的噪音。建议删除；如保留须按 B §8-3 浮报。

### m-2（minor）`_noop_progress` 死代码

- 位置：`src/opencode/tools/invoker.py:408-410`。
- 理由：全仓唯一出现处即定义（grep 实核）；无回调 noop 行为由 `_bounded_progress`（L412-423）的 `progress is None` 早退实现，并由 test_tools.py L605-612 钉定。docstring "adapters may always call it" 的语义已由别处兑现，本函数为遗留死代码。建议随 M-1 一并删除。

### n-1（nit）非法名测试不对称：namespace 侧 `__` 拒绝未钉

- 位置：`tests/contract/test_tools.py:255-259`。
- 理由：`__` 拒绝仅钉 name 侧（`"a__b"`），namespace 侧只钉 pattern 失败（`"bad ns"`）。检查代码为同一循环迭代（registry.py L159-160），分支语义已覆盖，故仅 nit；M-1 修复的钉定测试应顺带补 `namespace="a__b"`。

### n-2（nit）进度文件 ① 分组标签计数互换

- 位置：`doc/orchestration/progress/T-C1-016.md` ① 段。
- 理由："invoke 序列 7 测"实列 8 项、"失败/归一化 8 测"实列 7 项（逐测清点：register/snapshot 8 + invoke 8 + 失败/归一化 7 + progress 2 = 25 ✓ 总数正确）。仅标签错位，无验收影响。

### n-3（nit）read.py docstring 的 read-filesystem.ts 行号锚点偏移（溯源：任务书）

- 位置：`src/opencode/tools/builtin/read.py:1-27` docstring；源头 `T-C1-016/T-B-017` 任务书引文。
- 理由：引文 L33-38（offset 措辞）/L20-25（binary 措辞）/L42-47（目录拒绝）对应实际 L43-48 / L23-29 / L52-58（`PathKindError`），整体偏约 10 行；语义与措辞本身逐字核实无误（L48 `Offset ${offset} is out of range for this file (...)`、L27 `Cannot read binary file:`、L57 `Path is not ${expected}: ${resource}`）。C-1 系沿用任务书锚点，非其过错；建议调度器锚点台账勘误。

## 3. 契约符合性清单（12 维）

| # | 契约条目 | 实现 | 测试 | 判定 |
|---|---|---|---|---|
| 1a | §5.1 L253 三公开面精确（register/snapshot/invoke，无第四面） | registry.py L147/L181/L198；`__init__.py` 再导出面封闭 | test_tools.py L203-301 | ✓（唯 `invoke` 为 async 入口；`snapshot` 带 ^ses_ 校验 L183-186） |
| 1b | §5.1 L253 RegistrationError 五类 | registry.py L147-178：畸形 adapter L152-156 / 非法名 L159-163 / 超子集 schema L165-168 / 精确重复 L170-172 / 异 version 碰撞 L173-175 | L241-294 五类各一测 | ✓（**除**：非法名检查不充分 → M-1） |
| 1c | §5.1 L253 可逆/碰撞检测映射 | 同上 L58/L159-160 | L255-259 | ✗ **M-1** |
| 1d | §5.1 L253 快照钉定（provider 可见=可调用） | registry.py L181-195 钉 per-session；invoke 只认钉定目录 L217 | L218-238（后注册不漏入已钉、重钉后可见） | ✓（不变量对"快照后注册"成立；对 M-1 碰撞族不成立——随 M-1 修复恢复） |
| 2 | PHASE1-SLIM L85 按调用开始顺序串行 | registry.py L220-233 全 registry 单锁覆盖整条管线；目录解析（L217，纯同步无 await）在锁外安全；asyncio.Lock FIFO | test_tool_read.py L449-488：门控 adapter 阻塞时第二调用事件零前移，A 全序列先于 B，executions 按开始序 | ✓（断言强度足够：事件序列全量断言） |
| 3 | §5.1 L257 时序 + §3.1 L129-131 wire 字段 + §2.1 L45-51 状态表 | invoker.py invoke_tool L513-701：input/hash 前置检查 L521-530 → requested L531-547（message_id/tool_call_id/execution_id/requested_name/tool_ref?/input/input_hash=§3.1 L129 逐字段）→ catalog miss failed/not_started+NOT_FOUND 无 started（L549-569，§2.1 L46）→ schema 失败 failed/not_started+VALIDATION_ERROR adapter 未触达（L571-592）→ pre-start 取消 cancelled/not_started（L594-613，§2.1 L45/L48）→ started permission_decision="allow"（L615-633，§5.2 L264 默认面，常量 L81）→ 异常→failed+effect 按 side_effects（L644-667，§2.1 L50）→ 归一化 → finished（§2.1 L72）；每相单事件批 `_emit` L441-467（metadata={"run_id"}=§3.1 L110）；sink 错误传播无合成 finished；CancelledError 不捕获（③-22 边界主张成立：硬取消传播，started 未完结归 R2-S7 收口面） | test_tools.py L327-476（序列逐事件断言：version/session_id/created_at/evt_ 递增/全字段）；test_tool_read.py L378-418（含"无 py.permission.*"钉定 L389） | ✓ |
| 4 | §5.1 L257 输入严格验证不隐式转换修改 | invoker.py validate_schema_supported L218-289（注册时子集钉死，一次性别 developer 错误面）+ validate_input_strict L291-357：bool≠integer/number（L312/L320，Pydantic 陷阱）、3.0 非 int、原对象直传（L296-312 返回 issues 不改值）；cast 静态收窄=注册不变量（L297-299 注释成立） | L381-393（bool 拒）、L444-455（`is` 同一性直传）、L362? — schema 失败 L356-378 | ✓ |
| 5 | §5.1 L255 ToolContext（④-1） | invoker.py L108-142：六成员+run_id+tool_call_id 增补；`__post_init__` 身份校验 L136-142（③-4 未测防御，构造方=runner S6） | requested/包络字段经 L422-428/L419 钉定 | ✓ 增补（详见 §5 ④-1） |
| 6 | invoke 四参签名+tool_ref 原始名读法（④-3） | registry.py L198-233：形参 `tool_ref: str` 实传原始 requested 名，解析在 invoke 内针对钉定目录；未知工具仍产完整序列 | L327-353（未知工具 requested→finished 两事件、tool_ref=None） | ✓（详见 §5 ④-3） |
| 7 | §2.1 L72 终态必含模型可读文本 + §5.3 L276 限额 | invoker.py `_normalize_result` L367-405 四路补文本（structured→canonical JSON / error.message / no-output 注记 / file-only 注记）+ structured 64 KiB 丢弃+原始字节数标记（L373-388）；`_bounded_progress` L412-423 progress 64 KiB 截断标记；metadata 边界丢弃（L404-405 注释，§3.1 L131 字段集已核=无 metadata）；generation 三项只定义（L66-72 ToolLimits，执行归 runner=④-7 边界如实、无越权执行） | L526-577（四路各一测）、L540-553（超限）、L585-602（progress 截断+noop） | ✓ |
| 8 | §5.3 L272-273 containment 链 | read.py L99-128 逐字复用 platform_probe.py L513-539（`_contains` 同 normcase/normpath/sep 前缀；顺序 NUL→lexical→containment→realpath(strict)→containment；reason 字符串逐字同）；单根=working_dir（④-5）；`_read_window` L135-146 max_bytes+1 无 stat/reopen 竞态 | L212-303 七测：缺文件 NOT_FOUND/目录 UNSUPPORTED/traversal/根外绝对路径/symlink 放行/symlink 越界/junction（`_winapi.CreateJunction` L288-303，win32-only，symlink 1314→skip 复用 R0-B 处理） | ✓ |
| 9 | §5.3 L272 read 1 MiB/分页行号/输出 64 KiB 截断标记 | read.py：1 MiB 窗口自文件头 L139/L257-259；limit 缺省/封顶 2000 L263；行截断 2000+后缀 L150-152（read-filesystem.ts L14-20/L326-327 逐字）；`_select_page` L167-197 limit+64 KiB 双闸+next_offset 续读；limit 截断同样产出输出标记（TS `truncated = next !== undefined` L253 语义对等——已核）；空文件 `0 lines`（read.ts L199 逐字——④-12 消解）；offset 越界 VALIDATION_ERROR+窗口注记 L268-273 | L128-204（7 测）、L333-354（1 MiB 窗口+越界注记）、L357-370（64 KiB 截断+续读页） | ✓（**除** L194-195 死支 → m-1） |
| 10 | 编码/错误码四分歧（④-10/13、③-9） | read.py：严格 UTF-8 失败→TOOL_FAILED L261-263（有意分歧）；二进制窗口 NUL→TOOL_FAILED L259-260；目录→UNSUPPORTED_CAPABILITY L255-256；path NUL→VALIDATION_ERROR L243-244（解析点 vs 读取点区分正确） | L311-330（二测）、L221-229（目录） | ✓（逐项意见见 §5；path-NUL 补测见 ③-9） |
| 11 | ③ 强制浮报完整性 | 24 项逐项核对 | 见 §4 | 基本完整；漏报 1 处（m-1） |
| 12 | ④ 13 项 | 见 §5 | — | 同意 13 / 需改 0 / 升 A 0 |

§2.1 L37 标识约束（tool_call_id 唯一/execution_id 唯一归属）与 §5.1 L257"整批 generated 提交后才开始 invoke"：均为 runner 侧义务（S6），invoker 面无违反；重复 tool_call_id 跨响应查重已在 T-B-016 §6-5 记 S6 义务——确认，无需重复展开。

## 4. ③ 处置（24 项）

**协议不可达 3 项（③-1/2/3）**：同意。ask/deny、state=denied（pre-start 策略映射，R3）、state=unknown（close/cancel 收口，R2-S7）归置主张均成立；③-3 对 `state="unknown"`（不可达）与 `effect_status="unknown"`（可达且已测，L500-508）的区分准确。

**未测防御 6 项（③-4..9）逐项裁决**：

| # | 分支 | 裁决 | 理由 |
|---|---|---|---|
| ③-4 | ToolContext.__post_init__ 身份拒绝 | 同意不设测 | 唯一构造方=runner（S6）；建议 S6 落地时补构造面测试（列入 §6 义务） |
| ③-5 | validate_schema_supported 其余拒绝形状 | 同意不设测 | 注册门一次性 developer 错误面；已测 2 形状钉住拒绝语义与错误归类 |
| ③-6 | number/boolean/null 类型分支 | 同意不设测 | 本切片注册 schema 全用 object/string/integer；分支保留给 S6+ 工具作者 |
| ③-7 | minimum/maximum 违例（offset=0） | **需补测** | 模型可发（JSON 合法整数 0）；现走 `_bounds_issues` "must be >= 1"（invoker.py L269-277）零覆盖；offset=99 测走 adapter 行数检查，两检查点不同。一例 `{"path":"…","offset":0}` 即钉 |
| ③-8 | object 键非字符串 | 同意不设测 | JSON wire 键恒 str；仅绕过 JSON 的调用方可达 |
| ③-9 | path 含 NUL（解析点） | **需补测** | JSON 可合法携带 `\u0000`，模型可达；R0-B probe 验证的是方案成员，adapter 映射 `nul_byte_in_path→VALIDATION_ERROR` 无直接测试。一例 `{"path":"a\u0000b"}` 即钉 |

**已测防御 ③-10..20（11 项）**：逐项核准——各分支确有对应测试且断言到错误码/事件序列/未触达级别（③-14 双路径：无快照（空 `_pinned`）经 L327-353、钉定后未知工具经 L218-233，分别覆盖；③-18 `_clamp_line` 经 1 MiB 测 `MAX_LINE_SUFFIX` 断言隐式覆盖；③-19 窗口+输出双截断标记含续读 offset 均断言）。

**架构注记 ③-21..24**：同意。③-21 直取索引不变量成立（append-only + (ns,name) 唯一键 + 钉定 spec 复制自本注册表）；③-22 CancelledError 边界主张成立；③-23 cast/assert 收窄论证成立；③-24 recording sink 是正确接缝决策（真 sink 归 S6）。

**漏报抽查结论**：三源文件全量防御分支清点，命中 1 处未浮报不可达分支（m-1，read.py L194-195）与 1 处不实注释性不变量声明（M-1 内含，registry.py L55-57 注释）；此外无漏报。R1-S8 教训项：m-1 因零行为影响降 minor，未按"漏报即 major"顶格处理——如调度器要求顶格，M-1+m-1 合并视作同一 major 处置。

## 5. ④ 处置（13 项）

| # | 未决点 | 裁决 | 依据/意见 |
|---|---|---|---|
| ④-1 | ToolContext +run_id+tool_call_id | 同意 | 事件面必需已实证：py.tool.requested data 要 tool_call_id（§3.1 L129）、包络 metadata 要 run_id（§3.1 L110）；TS parity 已核（schema/tool.ts L14-20 Context 携 CallID L18）。内部运行面增补（非 wire 面），25 契约测已钉 requested.tool_call_id/metadata.run_id；契约 L255 为最小列举非穷尽。若终审坚持严格六字段，最小改动=invoke 侧传参（重构面小，两字段仅 `_emit`/requested 使用） |
| ④-2 | 同 (ns,name) 异 version 拒绝 | 同意 | wire 名唯一性的正确保守读法；已测钉定（L248-252）。注：该检查正是 M-1 碰撞门的正确部分——M-1 是其未覆盖的补集 |
| ④-3 | tool_ref 实传原始 requested 名 | 同意 | 唯一同时满足 §5.1 L253 四参签名与 §2.3 L100"未知工具→该调用 not_started 失败结果+配对保持"的读法；预解析式传参将使未知工具无法进入 invoke 产事件。保持现状 |
| ④-4 | ToolResult.metadata 边界丢弃 | 同意 | §3.1 L131 字段集已核=py.tool.finished 无 metadata；metadata 64 KiB 的执行面=py.tool.progress（bounded callback）分工正确 |
| ④-5 | read 单根=working_dir | 同意 | §5.1 L255 R2 唯一合法根；R3 config 多根归置（§5.2/§6） |
| ④-6 | 同步 fs IO | 同意 | 串行锁前提背书成立（阻塞读不饿死其他调用）+1 MiB 窗口有界；线程执行器迁移延后合理 |
| ④-7 | generation 三限额只定义未执行 | 同意 | §5.3 L276 三项语义主体=停止本次生成→runner（S6）；"只定义"边界如实（ToolLimits L66-72 仅声明，invoke 链无 generation 级执行）；S6 义务确认 |
| ④-8 | 非对象 input→ToolInvokeError | 同意 | §7 L341 fail-closed 正确；§2.3 L100"不完整 JSON→本次生成失败不执行工具"的归类主体=runner，invoke 层基础设施错一致。S6 接入时若模型可产非对象参数再评估重分类（义务确认） |
| ④-9 | AscendingIds 自 session.service import | 同意 | import 链推演：tools.registry→session.service（AscendingIds/Clock/IdFactory），session.service 不 import tools——无环；冻结 R1 面零触碰，import 优于复制 |
| ④-10 | 严格 UTF-8 vs TS U+FFFD | 同意 | 契约 §5.3"文本文件"读法下的有意分歧；TOOL_FAILED+模型可读文本已测（L322-330） |
| ④-11 | 输出截断标记口径 | 同意 | TS `truncated = next !== undefined`（read-filesystem.ts L253）已核——limit 截断产出标记系语义对等而非扩张；字节口径（clamped join、无行号前缀）系 §5.3 L272"明确标记截断和原始大小"的契约增强（TS 标记无字节数）；两截断标记（窗口/输出）正交组合正确 |
| ④-12 | 空文件 `0 lines` 措辞 | 同意（浮报消解） | 本次核到 TS 锚点：read.ts L199 `Read file ${path}, 0 lines` 逐字一致——不再是"未验证"项，可关闭 |
| ④-13 | 目录→UNSUPPORTED_CAPABILITY | 同意（建议 A 追认） | TS 锚点已核（PathKindError L52-58，"Path is not a file" 措辞逐字采纳）；错误码选型理由成立：R2 无 list 能力面→"不支持"语义准确，NOT_FOUND 会误导。建议 A 追认入 §8 词汇表选型档（见 §6） |

## 6. 需调度器/A 裁决事项清单

1. **M-1 修复授权（合并前热修，建议）**：追加提交于 r2-s5 分支，范围=registry.py 一行 guard（`wire.partition("__") == (ns, "__", name)` 自校验式）+ 钉定测试（拒 `("fs_","read")`、往返 `("fs","_read")`、补 namespace="a__b" 即 n-1）+ 建议同批清理 m-1（read.py L194-195 删除）、m-2（invoker.py L408-410 删除）、③-7/③-9 两补测——全部落在已授权 8 路径内，零冻结面触碰。修复后 B 复核 diff 即可，无需全量复审。若调度器选择"先合并后修补"，须把 M-1 列为 S6 前置硬条件（S6 注册非 fs 工具前必须完成）。
2. **namespace `__`-free 闭环核对结论（T-B-016 §6-6 归还）**：**闭环未合上**。S5 实现的检查（pattern+`__` 排除）不足以支撑 maas.py L102-104 的首 `__` partition 假设——单 `_` 边界族穿透（M-1，双树实证）。修复后闭环成立；maas.py 侧无需改动（其 docstring 已正确把命名职责归注册中心）。
3. **④-13 追认**：目录拒绝码 UNSUPPORTED_CAPABILITY 的 §8 词汇表选型，建议 A 追认入档（低风险，语义已由测试钉定）。
4. **④-1/④-3 终审确认（低优先）**：两读法均有充分依据（见 §5），B 意见=维持现状；若 A 要求契约文本显式化（ToolContext 字段表/ invoke tool_ref 语义注），属契约附注非代码改动。
5. **S6 义务确认（累计，含 T-B-016 §6-5）**：真 sink 接线（③-24）；generation 三限额执行（④-7）；非对象 input 重分类评估（④-8）；runner 构造 ToolContext 的身份校验测试（③-4）；重复 tool_call_id 跨响应查重（T-B-016 §6-5，确认不展开）；M-1 若未热修则为注册面扩展前置（第 1 项）。
6. **锚点台账勘误（n-3 溯源）**：read-filesystem.ts 错误类行号（BinaryFileError L23-29 / OffsetOutOfRange L43-48 / PathKindError L52-58），任务书引文整体偏约 10 行，建议后续任务包沿用勘误后锚点。

---
*审查方法记录：三源文件+两测试文件全文逐行；TS 锚点 14 处逐一比对（tool.ts/runtime.ts/read.ts/read-filesystem.ts/schema tool.ts/platform_probe.py）；maas.py 交叉面读取；专项实证运行 2 次（worktree registry 碰撞演示 / 主树 maas parse 往返），脚本落批准 temp 目录未触仓库；git 只读（diff --stat/show/log/porcelain）；未重跑门禁（调度器冻结面已核，口径 455/143/51 与 C-1 自报差一已由调度器核自洽）。*
