# R1-S6 结构化审查（席位B · T-B-011）

- 审查对象：worktree `r1-s6` 分支 r1-s6 两提交（`df7c899` S6 主体 5 文件 +1818 ∥ `f8f85ec` T-C1-009 验收微修 1 文件 +39/-2；父=#47 `35bc781`）：`bus/eventlog.py`（612 行）/`storage/repos.py`（258 行）/`storage/__init__.py`（纯追加 12 行）/`tests/integration/test_eventlog.py`（915 行 20 测）/进度 `T-C1-008.md`。
- 基线：`doc/CORE-CONTRACTS.md` §4 L222-238（read L224-227/坏页 L227/follow L229-236/L238）+ §3.2.1 L189-196 + §8 + TS `bus.ts` **四处行号 B 亲核属实**（`readAfter` L786-828 含跳过前进 L809-827、`subscribeDurable` sliding(1) L830-848、`log` L850-893 含 marker L879-884、`logReadPageSize=512` L207）+ `doc/plans/r1-slices.md` §2 R1-S6（L121-132，含 T-A-002 终裁）。
- 复验方式：B 全文亲读 eventlog.py/repos.py/test_eventlog.py（20 测逐个清点：read×7/读门禁×1/follow×6/bus 语义×1/repos×3/纯度×1/结构×1）/进度文件/storage/__init__.py；git 亲验——两提交 stat（df7c899=1818+/0-、f8f85ec 全 patch 只动测试文件）、`storage/__init__.py` diff **纯追加零删除/修改行**（7 行 import + 5 个 `__all__` 条目）、累计 5 文件 +1855；另亲核 txn.py `StorageCorruptError` L186-200、`TransactionWriter._decode_stored` L531-543、contracts events.py L196/L311/L337 与 errors.py L17/L26/L30/L41。四门禁（166 passed=146 基线+19+1 / ruff / format / mypy 36 files）采信调度器亲跑。

## 1. 结论：**通过**

**0 blocker / 0 major / 0 minor / 2 nit + 3 观察**（nit 均为测试覆盖类可选项，非并回前置）。§4 read/follow/坏页/读门禁逐条行号级落地；T-A-002 终裁（未知版本/类型 fail closed 不跳过，TS L809-827 行为不沿用）正确执行并显式文档化；follow 四步源码次序亲核正确（wakeup 注册先于 H 捕获）；六项未决全部同意 C-1 保守读法，T-C1-009 minor 已验证修复；**无升 A 项**。

## 2. 发现分级

- **nit ①（测试覆盖）**：四类退出的"消费方任务取消"类无直接测例。位置：eventlog.py `__aexit__` L515-527（无条件 `_release_wakeup`）。理由：正常退出/失败 enter/坏页终止三类均直接断言 `subscription_count==0`（测试 L264/L587/L618/L682/L732），任务级取消经 async with 语义走同一 `__aexit__` 路径（单次 cancel 下完整执行），风险低，但该维度由任务书明列（四类退出）。建议：可选微测——在 context 内 cancel 消费任务后断言 `subscription_count==0`。
- **nit ②（测试覆盖）**："探针行坏→整页 fail"无直接测例。位置：eventlog.py L347-363（fetchall limit+1，探针行参与解码）。理由：页内坏行路径已测（测试 L332-376），探针坏是同一解码循环的边界实例（观察 c 的设计落点），未独立钉住。建议：可选微测——limit=2、坏行落在 seq 3（探针位），断言整页 StorageCorrupt 且 last_confirmed_cursor=输入。
- **观察（B 新增）**：repos.session 对"合法 JSON 但非 dict"的 config_snapshot/diagnostic 静默取 None（repos.py L153-155）。§3.2.1 L194 fail-closed 字面只覆盖"无法解码"，形状错≠解码错，归一到声明类型 `JsonObject | None` 可辩护；S6 逻辑只消费 status/applied_seq，无影响；S7/S8 消费这两字段前需裁决形状异常语义。记台账，无行动。

（观察 a/b/c 三条既有项见 §4 表，均维持接受。）

## 3. 契约符合性清单（§4 逐条 → 实现/测试行号）

### §4 read（L224-227）

- 签名/None=本 session seq 0/limit 1-1000 越界**钳制**（bool/非整 ValueError）：常量 L93-95、L273-275、`clamp_page_limit` repos L108-112；测试 L191-242（分页边界；空页保持输入 cursor L211-214；钳制含 0、-7、10_000 直通与 True、2.5、"3" 拒绝）。
- next_cursor=最后 item 的 seq、空页保持输入 cursor：L297；测试 L201-220、L293-295（seq==H 尾页合法且保持）。
- 未建 session NotFound：L280-281（record None **且** H==0）；read+follow 双面测试 L245-264（失败 enter 注销 wakeup L264）。
- InvalidCursor 三类+非法编码+超 H 明确不空等：`_resolve_cursor` L172-208（session 不符 L189-197 / 负数 L203-207 / 缺字段·非字符串·bool·非整等编码非法 L184-202）；超 H L284-293（details 含 high_watermark）；测试 L267-304（三类 + SimpleNamespace 编码非法 ×3 + seq==5>4）。
- H=已提交日志 `COALESCE(MAX(events.seq),0)` 非投影水位：_SELECT_HIGH L110、`_log_high` L324-327；测试 L307-326（手改 applied_seq=10 → H 仍 4，超 H 仍报 4）。

### §4 坏页（L227，T-A-002 终裁逐项）

- 按 seq 连续解码、首个坏行**整页** fail closed 无半页：`_fetch_page` L347-367（解码循环首坏即抛）；测试 L332-370。
- next_cursor 不推进、同 cursor 重试**同错**：confirmed 贯穿 L294-296；测试 L362-365（两次 details 全等）。
- read 失败 cursor=输入 after（None→0）：测试 L367-370。
- details 五字段脱敏、不含坏行原文：`_page_corrupt` L389-401（reason/session_id/first_bad_seq/first_bad_event_id/last_confirmed_cursor）；测试 L353-360（dict **全等** + `"{not json" not in json.dumps`）。
- 未知版本/类型 fail closed **不跳过**（TS L809-827 跳过并前进 raw seq 的行为不沿用）：`_decode_row` L369-387 → `registry.decode_durable`（contracts/events.py L311，EventDecodeError L196）→ L362 捕获整页；测试 L379-406（version=99 / type=py.future.event 两面，first_bad_seq=2）。
- 发现前已返回的健康页仍有效（未隔离 session 健康后缀经更大 after 可读=S8 接线前过渡行为，测试 L372-374 自文档）：测试 L375-376（seq 3 坏、after=3 → [4]）。
- 探针行（limit+1）参与解码、探针坏则整页 fail：L347-363（保守方向，观察 c）。

### 读门禁（L227 + §3.2.1 L195）

- read_only_diagnostic **任一 cursor** 都 StorageCorrupt 无绕过（gate L282-283 **先于** cursor>H 检查 L284）：read 与 follow `__aenter__` L482-483 双面；测试 L409-442（None/2/3 三 cursor + follow；reason=session_read_only_diagnostic；last_confirmed_cursor=输入）。
- 经只读 repo 读 status、读模型仍可查（§3.2.1"读取仍可用"）：测试 L444-457（SessionRecord **全等** + messages 仍可查 [msg_0..2]）。

### §4 follow（L229-236）

- **步①先注册 wakeup 再短读事务捕 H**（线性化次序，B 源码亲核）：`__aenter__` L470-477（L473 注册先于 L476-477 捕 H，L470-472 注释给出窗口分离论证）；无注入钩子难以确定性竞态测试——可观察后果已测（观察 a 维持接受）。
- **步②补读 after.seq<seq<=H**（`through=self._high` 钳制捕获窗口）+ 恰**一次** `Synced(EventCursor(session_id, H))`：_run L574-589；测试 L463-507（[1,2,3,4] + `received[-1]==Synced(seq=4)` + Synced 计数==1）。
- **步③始终查 seq>last_seq、Bus 仅唤醒信号（payload 永不回放）、重复信号合并（单槽 asyncio.Event，drain 任务泵入）、≤1s 轮询兜底**（构造器钳制 `min(poll,1.0)` L251）：_run L591-612 + `_drain` L553-565；测试 L692-732（**ephemeral 先唤醒但流中下一 item 是后到的 durable 提交**、其后安静——bus-as-data-path 缺陷会被此测逮住）、L510-535（无 bus 注入，纯 ≤1s 轮询补读丢失的提交）、L538-564（**验收 B 后半**：无订阅者时 notify 丢失无害，read 仍拿全部 [1..5]）。
- **步④同连接 seq 严格递增，违反 fail closed**：L582-583/L606-607（`_non_increasing_error` L416-430）；测试 L497-500。
- 补读/yield/等待不持会话提交锁：每页短连接 L577/L601，yield 在连接外。
- **四类退出均注销 wakeup**：`__aexit__` 无条件 `_release_wakeup` L515-527；`__aenter__` 失败**自注销不依赖 __aexit__** L494-497；测试 L264/L587/L618/L682/L732（取消类=共享路径，nit ①）。
- **follow 坏页语义**：已 yield 健康事件保留后终止（测试 L621-689：1-8 保留、seq 9 坏）、**不发越过坏点的 Synced**（坏点在初始 H 之后时已发 Synced 只确认旧窗口，无带外 marker；测试 L652-680 Synced 计数钉死 1）、**last_confirmed_cursor=最后已 yield 事件游标**（confirmed 仅在 yield 后推进 L585/L609，不取失败页内部已解码 seq；测试 L674-676 =seq 8）、重连同 cursor 同错（测试 L686-689 details 全等）；catch-up 坏页先于任何 Synced（测试 L590-618：received==[]、last_confirmed=0、退出注销）。
- 只保证 durable 不回放 ephemeral（L238）：测试 L692-732。

### §8 wire

- `NotFoundError`/`InvalidCursorError`（repos L69-78；CoreErrorCode NOT_FOUND=errors.py L26 / INVALID_CURSOR=L30）经 StoreError 家族带 `.core`；`StorageCorruptError`（txn L186-200，code=STORAGE_CORRUPT=errors.py L41，`isolate` 标志=路由 §3.2.1 隔离入口的"请求"语义，读取面从不写表）；retryable=false 全程测试断言（L258/L350-351/L433-434/L610-611 等）。

### repos（§3.2.1 读取仍可用 + §2.1 投影序）

- messages **按投影 seq 序**分页非 id 字典序：_SELECT_MESSAGE_PAGE L64-66（`ORDER BY seq`）；测试 L738-788（msg_z/msg_a/msg_m 刻意与 seq 序错开）。
- after_message_id 专属尾游标（不存在→InvalidCursor）+ limit 同界钳制 + 空页保持游标：L177-217；测试 L769-783。
- 投影缺失**不伪造空页**（sessions 行删而日志存活→StorageCorrupt `sessions_row_missing_with_log`；未建→NotFound）：L181-190；测试 L791-814。
- 不可解码投影行 fail closed 定位（message_id+seq，无原文）+ 行 id/payload id 一致性二重校验：`_decode_message_row` L225-252；测试 L816-821。
- session 记录读模型面（status/applied_seq/config_snapshot 等全等）：测试 L824-843。
- **零 bus import**（AST 结构断言，repos+storage 包双模块）【T-C1-009】：测试 L902-915；**无外部调用**（socket/subprocess 禁令，S4 模式）：测试 L849-884。

### 架构定位自洽

- eventlog.py 是 §4 seam 的 **DB 耦合面**（import sqlalchemy + opencode.storage 解码助手，docstring L1-57 自文档）；`bus/__init__` 不导出 eventlog（volatile 出口 4 项不变，B 亲读现文件零改动）；S5 结构断言零触碰（146 基线无回归佐证）；storage bus-free 未破（结构测试 L902-915 + `__init__` diff 亲验**纯追加**：7 行 import + 5 个 `__all__` 条目，零删除/修改行）。
- **镜像解码约定 B 亲核成立**：`eventlog._decode_row` L377-387 与 `txn.TransactionWriter._decode_stored` L531-543 的 envelope 逐字段相同（9 字段同序同强制转换，均以 `registry.decode_durable` 收尾），今日逐字节一致；护栏=共享助手单点 import（`_json_column` txn L122 / `from_epoch_ms` L117 / REASON 常量 L63/L69）——结构护栏缺位见 §4 未决 ① 意见。

## 4. 六项未决 + 1 minor + 3 观察逐项意见表（T-C1-008 §③）

| # | 未决问题 | C-1 取舍 | B 意见 |
|---|---|---|---|
| ① | 解码助手跨模块私有 import（`_json_column`/`from_epoch_ms`/REASON 自 txn），codec.py 单点化不做 | 任务书明示保留，docstring 注明 | **同意**——助手已单点于 txn；B 亲核 envelope 两处逐字段一致。镜像无结构护栏属已知取舍；建议 **S8 接线或 R5** 抽 `storage/codec.py`（纯移动+双 import 替换，无行为变化）——留审查通道，非本切片行动 |
| ② | read 路径检出坏页只抛 isolate=true 不写隔离表 | 接线归 S8 | **同意**——§3.2.1 L192 明文 EventLog"只能请求该隔离入口，无权自行写表"，isolate=true 即请求语义；过渡期健康后缀可读已在测试 L372-376 自文档，S8 接线后由门禁拦截消失 |
| ③ | sessions 行缺失但日志存活时照常读 | 最保守=照常读日志（repos 侧已 fail closed） | **同意**——日志是事实源；sessions 行缺失属读模型缺失（§3.2.1 L193 最小诊断记录归隔离入口/writer 职责）；NotFound 仅对未建 session 钦定。S8 接线时应确保该场景进入隔离入口 |
| ④ | NotFoundError/InvalidCursorError 落位 repos.py | 避免单开 errors 模块 | **同意**——§8 wire 经 StoreError 家族；eventlog→storage 单向 import 无环；纯落位无行为 |
| ⑤ | anext 级取消终止整条连接 | 契约未定义逐项取消；消费方用 async for；需"超时不断流"改 `__anext__` 单点 shield | **同意**——Python 生成器原生语义 + 显式文档化；测试已按此语义重设计（流中途零超时探测，进度文件过程记要②）；S7 派发前裁决窗口仍在 |
| ⑥ | 工程选值 512/1s/1000 | 512=TS logReadPageSize L207 对应；1s 契约上限构造器钳制；1000 契约 limit 上界 | **同意**——三值均可构造器调整，1s 钳制（L251）保证永不超契约上限 |
| minor | T-C1-009 结构断言补钉 | repos docstring 声称的断言补上 | **已验证修复**——f8f85ec 仅动 test_eventlog.py（+39/-2：docstring 要点改写 + imports + 末段结构测试 L887-915），B 亲读 patch 无越界；166 passed=165+1、mypy 36 files |
| 观察a | wakeup 次序仅源码验证 | 无注入钩子难做确定性竞态测试 | **维持接受**——B 源码亲核次序正确（L470-477）；可观察后果已测（live 到达/丢通知补读/验收 B 后半） |
| 观察b | 读门禁 status 与 H 两连接微 TOCTOU | 短读+单 writer，窗口可忽略 | **维持接受**——最坏=门禁滞后一次读取，坏行本身仍 fail closed；L227"发现前已返回的健康页仍有效"容纳该前缀；S8 若做统一门禁快照可顺带消除 |
| 观察c | 探针行参与解码、探针坏则整页 fail | 保守方向 | **维持接受**——早失败优于下一页才失败；配套测例缺口=nit ② |

未决处置：**同意 6 · 已验证 1（minor）· 维持接受 3（观察）· 需改 0 · 升 A 0**。

## 5. 裁决事项（需调度器/用户）

无 blocker/major/minor 级裁决。台账转下游（不阻塞并回）：

1. **codec.py 单点化时机**（未决 ① 建议）：S8 接线或 R5——纯移动重构 + 双 import 替换，消除 envelope 镜像漂移风险。
2. **nit ①② 可选微测**（取消退出类 / 探针行坏）：可并入下一切片 C 微任务或 R1 收尾加固，非并回前置。
3. **B 新增观察**：repos.session 形状归一（L153-155，合法 JSON 非 dict → None）记台账，S7/S8 消费 config_snapshot/diagnostic 前裁决。

R1-S6 **可并回**（`df7c899`+`f8f85ec`，父=#47 `35bc781`），R1-S7（SessionService + 提交锁 + app 组合）可派发。
