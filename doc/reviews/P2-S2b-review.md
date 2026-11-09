# P2-S2b 微评审（T-B-060 · 席位B）——T-C1-052（重启 v2）：P2-S2 激活半面（SkillCatalog.activate 检查序+session 接线+py.skill.activated 提交+恢复语义）

> 评审对象：worktree p2-s2b B-baseline `f62b933`（父=`bdcccbd`=#170）；基准四件现读（CORE-CONTRACTS §6.3/§5.2/§5.3/§6/§7+修订票 46L+进度 103L[57 冻结+46 追加]+p2-slices §2.1）。**终裁：PASS（严重 0/一般 0/轻微 0/nit 0）**。B 独立复跑四件：全量 pytest=**4 failed+1259 passed+2 skipped in 286.30s**（0 warnings；1265=1259+4+2；失败恰=install 四测同名，WinError 4551 @ subprocess.py:1554）+install 隔离=4F+3P in 9.37s+锁测隔离=**103 passed**+ruff clean/format **376**（=374+1py+1md；116py+260md 机器核）+mypy conda 基座 **114 checked/6 伪差全在未触碰文件**（aged-venv DLL 红 B 亲探证）；CI run `42574595687` success @ `bdcccbd` B 直查亲证。评审窗 15:51:09 起，零 git 写（HEAD 恒 `f62b933`，树净亲证）。

## 1. 七面结论

1. **写面对账 ✓**：恰 12 路径（7M+5A）+1334/−28 逐值闭合（46+19+164+59+10+214+11+811 / 4+16+8；任务书分解「新五件 825/0」系任务书侧 14 行笔误——实跑 811/0，总数恰合，非 C 面缺陷）；冻结面 name-status 全空核（events.py/R5 五件套/recovery.py/pyproject/uv.lock/migrations/loader.py/CORE-CONTRACTS/context.py/app.py/其余既有测试零触碰）；进度 +46/−0 纯追加（57 冻结完整+46=103L）。
2. **catalog.py 激活门 ✓**：检查序固定 大小→路径→信任→hash 四段具名 fail-fast 零修复零跳序（红线①，序纪律 3 测钉）+source 同值前置（§6.3 L330）；定夺 a-e 逐项兑现（a=`SKILL_BODY_MAX_BYTES` import 消费+恰 64KiB 边界过+超上限拒；b=`object_pairs_hook` 重复键拒；c=前置；d=信任段经②解析锚读+stat-vs-read 分歧+UTF-8 严格+空正文拒；e=`confirm_hash_change` keyword-only bool=False）；`SkillActivationReceipt` 五契约字段+body（唯一进程内载体，披露成立）+`durable_record()` 恰六字段；§6.3 L318-345 逐行对拍；红线②③测试锁定；stat-probe 皱纹独立复核成立（契约定序固有，stat 仅取尺寸零内容读）。
3. **session 接线 ✓**：service=锁外检查序（validate-before-lock 先例）+锁内 read_record 三态门+确认门保守并集（skill_id∪path 双面）+bool 卫兵零隐式转换+无 catalog/engine 双 fail-closed+`_commit_activated` 与 `_commit_revoked` 同型有界重试+bus.notify+`metadata={}`；state=`SkillInfo` 六字段镜像+path 键+isinstance 拒伪+ACTIVE 门+copy-on-write+幂等重放永不拒；projector=no-row dispatch 零 diff 写零新表；**recovery.py 零改**（删盘恢复测实证 fold 承载）——与 PARTIAL 冻结设计逐面一致。
4. **测试与 fixture 诚实性 ✓**：43 测六类与进度②逐测映射（正常 7[含删盘恢复/幂等/confirm]/失败 25[检查序 10+序纪律 3+前置 3+清单 2+服务 7]/触发封闭 2/平台 6/fixture 3，async def 形普查亲证 43 恰合）；test_skill_catalog 演进三处（公共面 `["activate","list"]`）；四 fixture 亲算（78B+sha256 `70152fa8…31ef8` 逐字节同正件、`FF FE` 非 UTF-8 实证、重复键形、八键信封+六字段+`metadata:{}`）；T-B-059 缺口实质闭合（正件经 default_registry decode_durable 测试锁定）+普查面未同步=裁定①。
5. **门禁算术+复跑 ✓**：1265=1259+4+2/+43P+1S 分解/376/114 四算术闭合；四件复跑数字与 C 自报逐值同；mypy 载体漂移亲证（aged venv DLL 红→conda 2.4.0：6 伪差=httpx2 ×2[maas.py+provider_spike.py]+TypeAdapter ×4[test_messages.py+test_llm.py] 全在未触碰文件，触碰文件零错）；CI 十六连绿 B 直查。
6. **时间戳+工序纪律 ✓**：v2 追加段 15:40:13→15:41:39 单调（LastWrite 15:41:45、会话窗 14:50:24-15:42:07[51m43s<75min]、提交 15:50:03）；PS 编码事故终态亲证净 UTF-8（catalog/service/test 三件直读中文正常——relay mojibake 为管线伪差）；票面引用行号抽查全部现读吻合。
7. **④ 浮报+三项裁决复核 ✓**：body-on-receipt/并集语义/双 fail-closed/协议不可达三支（read_only 先拒/close 竞态锁内兜底/_verify_prefix 未触碰）/R5 伴随目录披露/256KiB 预置逐条成立；三项调度器裁定草案独立复核均成立（①普查口径=进度④ 对账+inventory 同步归 R5 线、R5 零写纪律维持；②伴随目录+.body 工序约定随 #171 记档暂不条文化；③app.py/context.py 消费接缝后续切片）。

## 2. 处方

1. **并回**：p2-s2b merge --no-ff 入 main 为 #171+主树四门禁复跑。
2. **翻牌**：ORCH 台账 T-C1-052 行随 #171 置 DONE（P2-S2 激活半面闭）；修订票工作副本（main 树 M 状态）随 #171 入库。
3. **记档**：三项裁定+mypy 载体漂移定谳（conda 基座 2.4.0 代换面+6 伪差归属）随 #171 记档；skills fixture 缺口闭合对账在案。
4. **条文化**：无（零发现；.body 工序约定沿 T-B-059 处方④ 先例暂不条文化）。
5. **无他**。

## 3. 发现明细

- 零发现（四档全零）。附注两则（均非 C 面缺陷）：①任务书面「新五件 825/0」分解与实跑 811/0 差 14 行（总数 +1334/−28 恰合）——B 侧任务书笔误记档；②回执 body 为契约 §6.3 L332 五字段外的设计扩展——唯一进程内载体论证成立、③ 已披露、事件 wire 面恰六字段不受影响，复核接受。
