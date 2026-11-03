# R2-S5 合并前热修核(席位B · T-B-017B · fix-diff-only)

- 复核对象:worktree r2-s5 单提交 `b6846cd`(6 files +160/−10,父=7224956);仅审 `7224956..b6846cd`
- 输入:T-B-017 §2/§4/§6-1 建议、热修 diff 全量、maas.py 对偶锚点、进度 T-C1-018.md(61L)
- 门禁:采信调度器冻结面复跑(493=489+4 零回归 / ruff 全过 / format 158 / mypy 55),未重跑

## 1. 结论

**通过(可合并)**——六项全部按 T-B-017 §6-1 建议落地,断言强度足额,零越界,零新引入问题。

## 2. 六项+越界逐项表

| # | 项 | 位置 | 落地判定 | 断言强度/理由 |
|---|---|---|---|---|
| 1 | M-1 wire 名可逆/碰撞门 | registry.py register(),pattern 检查后、wire_name 构造前 | **通过**(=§6-1 建议的一行自校验原样) | `f"{ns}__{name}".partition("__") != (ns,"__",name)` → RegistrationError。语义核对:pattern 前置已拒两半含 `__`,guard 新增拦截恰=ns 以单 `_` 结尾——("fs_","read") 拒、("fs","_read") 留;`"fs___read".partition("__")==("fs","__","_read")` 唯一幸存解释者确定,`_by_wire_name` 静默遮蔽支消解,"provider 可见=可调用"恢复(§5.1 L253)。L55-60 注释更正为真实充要条件(可逆⟺ns 无 `__` 且不以 `_` 结尾;name 侧免约束),与新语义一致 |
| 2 | m-1 read.py 死支 | `_select_page` L191-195 | **通过** | −2 行纯删除,零逻辑改动;不可达性=T-B-017 逐案穷举证明(len>limit ⇒ break 时已赋值;=limit ⇒ 第三合取恒假;<limit ⇒ 首合取假),等价性由既有分页/截断测+493 绿背书 |
| 3 | m-2 invoker.py 死代码 | `_noop_progress` L408-410 | **通过** | −4 行纯删除;grep 复核 invoker.py 零残留(本会话实测 0 匹配);noop 语义由 `_bounded_progress` None 早退承载 |
| 4 | n-1 ns 侧 `__` 钉测 | test_tools.py `test_register_rejects_invalid_names` | **通过** | 参数化扩项 `_spec(namespace="a__b")`,双侧(`__` 在 name 与 ns)均 match RegistrationError+"wire-name reversibility";既有断言零改动 |
| 5 | ③-7 offset=0 补测 | test_tools.py `test_invoke_minimum_bound_is_enforced_before_adapter` | **通过** | 真实 `READ_INPUT_SCHEMA`(顺带钉生产 schema 携 minimum:1)+`offset:0` → failed/not_started/VALIDATION_ERROR/"offset: must be >= 1"+`adapter.calls==[]`(schema 拒先于 adapter,检查点与 adapter 行数检查 offset=99 区分)——与 TS OffsetOutOfRange L43-48 的"读法区分两检查点"对齐 |
| 6 | ③-9 NUL 路径补测 | test_tool_read.py `test_read_nul_in_path_is_validation_error` | **通过** | `"a\x00b"`(JSON 可合法携带,模型可达)→ VALIDATION_ERROR/"Path contains NUL bytes"(解析点 `nul_byte_in_path`);docstring 注明与读取点二进制窗口 NUL→TOOL_FAILED 为不同检查点 |
| 7 | 越界检查 | `git show --stat` | **通过** | diff 恰 5 代码/测试路径+进度文件,全部在 T-C1-016 授权 8 路径+进度面内;read.py/invoker.py 其余行零触碰(hunk 纯删除);冻结面(含 ai/maas.py)只读。进度 ③④ 浮报与 diff 逐条一致(死代码 2 项引 B 证明/guard 拒绝支+补集双测/未测防御 6→4;④-2 任务书"#76 仅 docs"前提漂移已如实浮报,门禁按实跑 489/55/157 口径) |

## 3. S5↔S3 接缝往返闭合(T-B-016 §6-6 终点)

**闭合确认**。`test_register_leading_underscore_name_round_trips_over_wire` 四位一体钉死:① 注册 (fs,_read) 合法;② snapshot `resolve("fs___read")` 精确回解析;③ 经原始 wire 名 invoke 成功(tool_ref=(fs,_read,v1));④ maas 侧 `parse_tool_wire_name("fs___read")=="fs._read"` 且 `tool_wire_name_from_dotted("fs._read")=="fs___read"`。guard 语义(首 `__` partition 对一切可注册 (ns,name) 成立)与 maas.py L99-102 首 `__` partition 假设严格对偶——凡合法注册名必然可逆,T-B-016 §6-6 循环就此关闭;maas 无需改动(其 docstring 命名职责归注册中心的表述与实现一致)。

## 4. 需调度器/A 裁决事项

无新增。沿进度 ④ 既有待办:n-2 标签计数互换批注、n-3 TS 锚点台账勘误、④-13/④-1/④-3 下轮 A、S6 义务清单(§6-5 累计)——均非本热修义务,不阻塞合并。

**是否有需升 A 项:无。** r2-s5(b6846cd)建议即行并回 main。
