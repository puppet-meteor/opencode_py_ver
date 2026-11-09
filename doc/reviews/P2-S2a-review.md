# P2-S2a 微评审（T-B-059 · 席位B）——T-C1-052a 解锁微票：`py.skill.activated` 事件落地 + R5 schema-lock 诚实全解锁

> 评审对象：worktree p2-s2a B-baseline `0495a11`（父=`b2c8cc2`=#169）；基准=票据 T-C1-052a+CORE-CONTRACTS/p2-slices §2.1/T-C1-052 PARTIAL 进度（两级解锁方案源头）全部现读重锚。**终裁：PASS（严重 0/一般 0/轻微 0/nit 0）**。B 独立复跑四件：全量 pytest=**4 failed+1216 passed+1 skipped in 266.01s**（0 warnings；1221=1216+4+1；失败恰=install 四测同名，WinError 4551 @ subprocess.py:1554）+install 隔离=4F+3P in 9.17s+锁测隔离=**103 passed**+ruff check clean+format **370**（=115 py+255 md）+mypy 载体 **113**（六路径 55+4+2+4+15+33）。评审窗 14:06:38 起，零 git 写（HEAD 恒 `0495a11`，树净亲证）。

## 1. 七面结论

1. **写面对账 ✓**：恰 7 授权路径（1A 进度+6M：events.py/generator/test_schema_lock/inventory/events.json/event-envelopes.json）；+275/−23 逐值闭合（52+13+103+40+55+5+7 / 13+2+4+4）；events.py +55/−2 分段算术闭合（docstring 1+家族块 5+模型区 35+元组区 12+registry 2）；冻结面 name-status 全空核（skills/session/contracts 其余四模块/loader/pyproject/migrations 零触碰）。
2. **events.py 契约对拍 ✓**：`SkillActivatedData` 恰 6 字段（skill_id/source/path/hash/size/body）逐字段对齐 §6.3 L336-343+§3.1 L142；零字段级约束（六裸注解）合票面裁定，§6.3 表界归激活检查序；§1 L16 `extra="forbid"`（L757）+§1 L17 `_size_not_bool` before-validator（L766-769，`_reject_bool` 单点复用）两面独立亲证；`SKILL_BODY_MAX_BYTES=64*1024`（L747，§6 L289「每份 64 KiB」接地）；家族块 L72-75；分离元组 L1052-1054（R2 冻结钉 15 条逐行亲数、零 py.skill.* 行）+registry 第六线 L1066。
3. **锁不变式恢复 ✓**：PINNED 34→35/21→22 恰值（L64/L65 现读）、ephemeral 4 不动（L66）、普查 92·25·48·19 不动（L56-57）；工件活数复核 models=35、durable=22、`py.skill.activated.1` 在册、字节 102734/122043 与普查件尺寸列逐值同；锁测散文 63→64；锁套件 103 passed（漂移测绿，全量+隔离双证）。
4. **普查件同步诚实性 ✓**：13 处同步齐——§1 两行（尺寸/键数）、durable 键叙事 22=2+11+6+1+1+1、seam S5 events 10·fixture 9 不动、§3 头 64=1+8+35+17+3+S5 行+计数行、§5 正例行 26 定义 vs 25 正例、§5 反例行锚、§6 两行、§8-1「64 vs 54」、§8-3「25↔26」；行锚 +40 位移五处亲算（L772/L979/L987/L1040/L1057）；§8-1 grep 口径独立复算 **54**=1+33+9+8+3；skills fixture 缺口在册（§6.3 L357 归 P2-S2 实现切片）。
5. **门禁算术+独立复跑 ✓**：四件读数见头块，与 C 自报及调度器预核逐值同；1221/370/113 三算术闭合。
6. **时间戳法证+TS 锚 ✓**：进度 Created 13:35:44/LastWrite 13:56:24（C 会话窗 13:34:21-13:56:48 内、B-baseline 提交与 B LAUNCH 14:06:38 前）；首行戳 13:35:37=先 Get-Date 后落盘；added 行 TS 锚扫描（真锚形态口径）零匹配，docstring 仅契约 § 锚。
7. **④ 浮报充分性+两项裁决独立复核 ✓**：`_size_not_bool` 唯一新防御分支已浮报（`_reject_bool` 单点+家族先例论证成立；零新测合票红线，收集 1221 不变）；install 漂移定谳复核成立（B 复跑失败恰四测+WinError 4551+漂移测绿+其余零失败，与调度器 pristine A/B 同形=环境面非本票因果）；诚实同步追认成立（26↔25 缺口在册+§6.3 L357 接地）。

## 2. 处方

1. **并回**：p2-s2a merge --no-ff 入 main 为 #170+主树四门禁复跑。
2. **翻牌**：ORCHESTRATION 台账 T-C1-052a 行随 #170 置 DONE；T-C1-052 重启票派发时直接消费本解锁（生产者/fold/fixture 面）。
3. **记档**：install 族环境面新形态（5E→4F+1P，WinError 4551）B 独立复跑在案；skills fixture 缺口（26 定义 vs 25 正例）归 T-C1-052 重启票闭合。
4. **条文化**：无（零发现）。
5. **无他**。

## 3. 发现明细

- 零发现（四档全零）。要点复核记录：six-field 权威链闭合（契约表=模型裸注解=events.json `required` 全六=envelopes `$defs` 六属性=PINNED 35）；生成器六元组接线兑现 T-C1-045 披露（ENVELOPE_DEFINITIONS 增线+注释重锚 L1057-1068 亲证吻合）；registry 26 定义=2+15+6+1+1+1 与 durable 22+ephemeral 4 键闭合；envelope 键插入位 alphabetical 正确（session.created.3 < skill.activated.1 < step.finished.1）；`py.skill.activated` 非 run-scoped（不入 RUN_SCOPED_EVENT_TYPES，与 §3.1 L142 行无 run 标记一致）；EventDefinition 默认 version=1 与键尾 `.1` 一致；零字段级约束裁定与 §6.3 L343「经检查序的 bounded」措辞自洽。
