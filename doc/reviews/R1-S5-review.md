# R1-S5 结构化审查（席位B · T-B-010）

- 审查对象：worktree `r1-s5` 检查点#43（commit `f27ade3`，父=#41，5 文件 +912）：`bus/__init__.py`（`__all__` 4 项）/`bus/bus.py`（299 行核心）/`tests/contract/test_bus.py`（24 测 7 组）/`tests/integration/test_bus_commit.py`（1 联测）/进度 `T-C1-007.md`。
- 基线：`doc/CORE-CONTRACTS.md` §4 L219-220 / §3.2 L159 步 5 / §8 + TS `bus.ts`（subscribe L751-782 / notify L496-509 / PubSub.unbounded L199 / local·routes L729-749——**四处行号 B 亲核属实**）+ `doc/plans/r1-slices.md` §2 R1-S5（L109-119）。
- 复验方式：B 全文亲读 bus.py / bus/__init__.py / 两测试文件全量 / 进度文件；TS 四段行号亲核；四门禁（146 passed=121 无回归+25 新 / ruff / format / mypy 33 files）采信调度器亲跑；基线 121 与 T-B-009 微任务（119+literal N-2①+N-1 直测=121）衔接一致。

## 1. 结论：**通过**

**0 blocker / 0 major / 0 minor / 0 nit + 2 观察**（均为可选强化，无行动项）。§4 五要件逐条落地（行号见 §2）、§3.2 L159 步 5 组合层定式与 S3/S4 AST 断言自洽、TS 映射四处行号属实、并发原子性成立（§4）、无越界面（§7）、25 新测断言强度高（wire 全等/前缀序/identity 断言，§6）。九项未决问题 B 全部同意（§8），**无升 A 项**。

## 2. 契约符合性清单（§4 L219-220 五要件 → 实现/测试行号）

1. **签名** `subscribe(session_id, types?) -> AsyncContextManager[AsyncIterator[Event]]`：bus.py L220-246；`_Subscription`（L89-138）同实现 ACM+迭代器（`__aenter__` L110 / `__aexit__` L117 / `__aiter__`·`__anext__` L126-138）。
2. **live-only（进入订阅后才可见）**：进入 `__aenter__` 才注册（L114）；测试时机×2（test_bus.py L121-136：订阅前、subscribe() 与 __aenter__ 之间发布均不可见，0.05s 超时窗断言）。
3. **含 ephemeral/committed durable**：`Event = DurableEvent | EphemeralEvent`（L53；契约无统一 Event 别名，bus 持有并文档化）；投递测试 L272-280（双信封按发布序、identity 断言）。
4. **队列上限**：`DEFAULT_QUEUE_LIMIT=1000`（L56）+ 构造器 `queue_limit`（L209-211 拒 bool/非正整数）+ `_offer` 计数执法（L156-161）；测试×4（L140-203：恰好填满+排空后恢复、溢出、ephemeral 溢出无 seq、慢订阅者不影响持续排空者）。
5. **溢出断开报 SlowConsumer、不静默丢 durable**：已接受前缀仍按序投递（L156-161 语义；测试 L157-158 先收 evt_1/evt_2）→ sentinel `_Terminated`（L82-87/L158）→ 迭代器抛 `SlowConsumerError` 且仅一次、之后 StopAsyncIteration（L129-138）；details 净化定位首个未投递事件（L164-177：session_id/queue_limit/event_id+**durable 才有 seq**，测试 L164-169 durable 全等含 seq / L185 ephemeral 全等无 seq）；溢出即注销、后续 notify 不入队不抛（L282-283+L154-155；测试 L172-175）；**notify 对慢消费者永不抛错**（提交已成功，通知面不反噬，L256-258 docstring）。
6. **退出 context 注销并释放队列**：`__aexit__`→`_release`（L117-124/L179-187：注销+清空未读+计数归零）；测试×3（L206-240：退出后发布不入队不抛+计数归零、未读丢弃+迭代器终止+二次进入 RuntimeError、新订阅只见新事件=live-only 无重放）。

**§3.2 L159 步 5（组合层定式）**："按 seq 发 Bus 已提交通知；重试不再重发，遗漏由 EventLog 补读"——Python 落法=调用方先 `SessionStore.commit`、后以返回值（本身按 seq）notify（bus.py docstring L26-30 + notify docstring L248-259）；**store/txn bus-free 不破**（`src/opencode/storage/` 零触碰、test_store_commit AST 断言延续、121 基线无回归）；联测 L108-160 完整钉住——Phase 1 注入抛错 projector commit 失败 → 订阅者零事件（0.05s 超时）+ events/sessions/messages 三表零残留 + 订阅保留（失败不是订阅者的问题）；Phase 2 换健康 projector 同批重试 seq 重起 1,2 → `bus.notify(ses, committed)` 后 `received == committed`（列表全等）按 seq 序、再无多余事件、终态 A=H=2+messages 1 行——**验收 A 后半"Bus 收不到未提交事件"成立**。

**§8 wire**：`SlowConsumerError.core` = CoreError{code: SlowConsumer, message, retryable: False, details}——与 StoreError 族同一 `.core` 形状（§8"code 至少区分…SlowConsumer"落点）。

## 3. TS 基准对照（四处行号 B 亲核）

- `subscribe` L751-782：TS 每 typed PubSub per subscription + `acquireRelease` 注销（L762-778）↔ Python 每订阅一有界队列 + ACM 注销释放——语义等价（typed 投递经 per-session 订阅表+per-subscription frozenset 过滤器实现，结构更简、无行为差异）✓。
- `notify` L496-509：listeners→live→typed 顺序；Python 仅保留 live/typed 两面（deprecated `listen` 不移植、结构测试断言不存在）✓。TS 单事件 vs Python 批（`Sequence[Event]`）为组合缝适配；**⑦ 同调用去重系 Python 侧加严**（TS notify 无去重），已在 docstring L254 文档化——非偏离。
- `PubSub.unbounded` L199：TS 无界 ↔ Python 有界+SlowConsumer——契约 §4 明文差异项，正确执行 ✓。
- `local()`/routes L729-749：location 路由不移植（R1 单进程）——结构测试断言无 local/routes ✓。

## 4. 并发正确性（事件循环原子性）

- 投递面五方法（`notify`/`_register`/`_unregister`/`_offer`/`_release`）**零 await** → 在事件循环内原子执行，注册/注销/投递/溢出之间无交错窗口（bus.py L140 注释自文档化该不变量）。
- notify 对订阅表做**快照迭代**（`list(subscribers)`，L281）：迭代中发生的注销（含溢出路径自身触发的注销）不破坏迭代——无 set-changed-during-iteration 风险；健康订阅者不受同批慢消费者影响（测试 L140-203 组：慢订阅者溢出断开，持续排空者仍收全部）。
- `_Subscription` 无 `__eq__`/`__hash__` 覆写 → 注销按**对象恒等**（L295），同参数并发两订阅互不误删。
- sentinel 单次消费：`__anext__` 见 `_Terminated` 抛 SlowConsumerError 一次后置 done 标记，其后一律 StopAsyncIteration（L129-138）——重入迭代不重复抛错。
- `_offer` 早退守卫（L154-155）：已注销/已终止订阅的迟到投递直接丢弃，不入队不抛错。
- 内存上界 = queue_limit 事件 + 1 sentinel（queue_limit+1），停滞订阅者不无限滞留事件——与 TS `PubSub.unbounded`（L199，§4 明文否决的形态）相对。

## 5. 边界与错误形状

- 最小上限 queue_limit=1 的 ephemeral 溢出：details **dict 全等**断言且无 seq 字段（测试 L177-185）——seq 仅 durable 有，边界处不变形。
- `types=()` 匹配零事件（bus L259-263，字面语义非"空=全部"）；单个字符串拒绝 ValueError（L265-268，防按字符过滤笔误）；过滤器不查 registry（允许订阅未来类型）。
- 先验证后投递：注入 registry 时坏批零投递——**好前缀也不投**（bus L266-276；测试 L319-326），无半批投递；无 registry 则不校验、信任组合层（测试 L328-333；store 已在提交时校验 durable）。
- 程序性参数错误统一 ValueError（queue_limit 含 bool 拒绝、空 session_id、types 字符串、跨会话事件 L301-306）——与 storage 层 `open_database` 未知模式同习语，未新增重复 wire 类；`SlowConsumerError` 是 bus 模块唯一带 §8 wire 形状的错误。
- 同调用 id 去重：同 id 事件一次调用只投首个（event+model_copy 构造重复测，L282-288；TS 无对应物，系加严，见 §8 ⑦）。

## 6. 测试质量（24 contract + 1 integration = 25 新增）

- 断言强度高：details dict **全等**（非子集）——durable 含 seq（L164-169）/ ephemeral 无 seq（L185）；前缀-先投递-后抛错的**顺序**断言（L157-158）；`is` **identity** 断言（L272-280）；`_assert_silent` 0.05s 超时窗三用（订阅前不可见 / 退出后静默 / 联测 commit 失败零事件）；联测 `received == committed` **列表全等**（test_bus_commit.py L144）。
- 覆盖核对 r1-slices §2 R1-S5 测试计划四项全落地（订阅时机 / 队列上限→SlowConsumer / 退出注销 / 类型过滤），另含计划外加分：identity、慢订阅者不殃及排空者、跨 session、去重、先验证后投递、AST 结构断言×2（零 DB import 含 `opencode.storage.*` 禁入、无 dropped TS surfaces）。
- 联测真实建库（alembic upgrade head）+ 抛错 projector（S4 FailingProjector 模式复用）→ 三表零残留 + 订阅保留 + 重试 seq 重起端到端按序——钉死验收 A 后半。
- 无缺口发现。

## 7. 无越界（范围与依赖）

- `VolatileBus` 无 listen/project/local/routes/read/follow/replay/cursor——结构测试逐名断言不存在（测试 L372-376）。
- AST 断言 bus 两模块零 DB import（sqlalchemy/sqlite3/subprocess）且 **`opencode.storage.*` 禁入**（测试 L359-370）——与 B 亲读 bus.py 全文（零 storage import）互证。
- 改动面 = 授权清单五文件；`src/opencode/storage/` 零触碰（121 基线无回归佐证）、`pyproject.toml`/`uv.lock` 零触碰（零新增依赖）。

## 8. 九项未决意见表（T-C1-007 §③）

| # | 未决问题 | C-1 取舍 | B 意见 |
|---|---|---|---|
| ① | queue_limit 默认 1000 | 工程选值（批次典型数十至数百 + 一个数量级余量；小信封个位 MB；只影响误伤率不影响正确性），构造器可调 | **同意**——契约只钉"有上限 + 溢出报错"未定数值；1000 论证充分且非硬编码 |
| ② | 溢出交付语义 = 已接受前缀按序投递后断开 | 最保守读法；备选（溢出立即断流）仅改 `_offer` 一处 | **同意**——最大化已接受事件可见性且满足"不静默丢"；改动点已隔离备裁 |
| ③ | retryable=False | 订阅已终局断开，"重试同一操作"不可行；恢复 = 重订阅 + S6 补读 | **同意**——该错误对订阅者不可重试，置 True 反而误导恢复路径 |
| ④ | seq 顺序由组合层保证 | L159 字面落调用方；commit 返回值本身按 seq；混合批无良定义重排键 | **同意**——bus 不越权重排；docstring 已注明调用方契约，联测端到端验证 |
| ⑤ | `types=()` 零匹配 + 过滤器不查 registry + notify 仅注入时校验 | 字面语义；允许订阅未来类型；S7 注入时自然 fail-closed | **同意**——bus 不耦合 schema；store 已校验 durable，S7 定式注入获得完整校验面 |
| ⑥ | 程序性错误用 ValueError，不新增 wire 类 | 避免 §8 重复决策；SlowConsumerError 是唯一 wire 错误 | **同意**——与 open_database 未知模式同习语；S7 组合层可映射 ValidationError |
| ⑦ | 同一 notify 调用内按 event.id 去重 | "不重复投递"最强读法；合法调用下无行为影响 | **同意**——TS notify 无去重，此为 Python 侧**加严**且 docstring L254 已文档化，非偏离 |
| ⑧ | 保留 subscription_count 只读属性 | 测试与最小可观测性所需 | **同意**——零行为面、零持久化，保留 |
| ⑨ | 无契约矛盾 | 逐条对齐 §4 / §3.2 L159 / §8 / r1-slices L109-119 | **同意**——B 独立复核证实 |

未决处置：**同意 9 / 需改 0 / 升 A 0**。

## 9. 观察与修订项（非行动项）

- **Obs-1（可选强化）**：`test_event_union_covers_both_envelopes` 用 isinstance 代理验证 `Event` 别名覆盖双信封——若别名退化（缩窄为单信封）该测仍过；可用 `typing.get_args(Event)` 断言恰好两参数强化。但 mypy 已 pin 该别名（33 files 绿含新模块），实际风险低，无行动。
- **Obs-2（R5 候选）**：`SlowConsumerError` 直接继承 `Exception` 并自带 `.core`——wire 形状与 StoreError 族一致但基类不同（bus 按 AST 断言不得 import storage，无法复用同基类）。R5 可在 contracts 层引入共享的 `.core` 携带基类统一错误族；记入 nit 台账。
- 裁决事项：无。R1-S5 **可并回**（#43 `f27ade3`），R1-S6 可派发。
