# P2-S6 微评审（T-B-061 · 席位B）——T-C1-053：M2-git 只读工具对 git__status/git__diff（断网改派票）

> 评审对象：worktree p2-s6 B-baseline `5ea0b1e`（父=`2e7461d`=#171）；基准三件现读（主树票 49L[断网改派依据+定谳 a-i+红线+写权限 6 项]+CORE-CONTRACTS §5.1/§5.2/§5.3+PHASE2-MODULES L67/L70）。**终裁：PASS（阻断 0/重大 0/次要 0/建议 0）**。B 独立复跑四件：全量 pytest=**1285 passed+2 skipped in 345.05s**（0F 0W；1287=基线 1265+新增 22 恰合；install 族 B 轮 0F 与 C 终态轮 4F 构成 WDAC 断续面 {0F↔4F} 翻转实证，CI 在册 run `42584602674` GREEN @ `2e7461d` 十七连绿——断网 gh 不可达 B 未复验，以调度器在册引证为准）+ruff check clean+format **380**（=118py+262md 机器核）+mypy conda --python-executable 形 **116 source files/0 错**。会话锚：C 16:59:11→SEAT-EXIT 0 @ 17:32:12（33m01s<止损 90min）+提交 17:36:00→B LAUNCH 17:36:46；进度分节时间戳 17:29:33→17:31:44 单调。B 零 git 写（worktree 全程净树+HEAD 恒 `5ea0b1e` 亲证）。

## 1. 六面结论

1. **票面符合性 ✓**：恰 6 路径（3A+3M）+1081/−1 逐值闭合（progress 67/0+git.py 444/0+test 525/0+cli 4/0+`__init__` 33/1+policy 8/0）vs 写权限 6 项逐项合；冻结面 name-status 全空核（pyproject/uv.lock/CI/source-baseline/契约面/registry/invoker/process/既有测试/tests-r0 零触碰）。定谳 a-i 逐项落码（git.py：a=L335/b=L339/c=L294-312/d=L176-221+L325-330/e=L358-406/f=L100-104+L229-232+L350-357/g=L343-344/h=L148-168+L264-276/i=L332-339）——a=调度器预裁定修正形（flag 集逐字+全局位前移），B 独立探针双证：票面字面形 exit 129 usage error（error: unknown option no-optional-locks）/落地形 exit 0 正常出数（temproot probe-repo P1'/P3'）；b-i 原样落地零席位发明。
2. **契约接地 ✓**：§5.1 L254 ToolSpec 八字段全（含 supported_content_types=["text"]）+L255 execute(context, validated_input)->ToolResult 面；§5.2 L268 默认表语义；§5.3 L276 roots 解析+traversal/越界 symlink 拒+64KiB 输出截断显式标记+原始大小（L229-232）+L279 子进程 120s+取消/超时终止回收进程树（run_command 承载）+L280 structured_output 面；PHASE2-MODULES L67 权威句前半（status/diff 两只读子命令+记录 cwd/roots——structured L264-276 承载；冲突面=worktree 域红线记档）+L70 零新依赖（imports 仅 stdlib+仓内）。
3. **policy 零行为闭合 ✓**：evaluate 现读 L634-662 逐行亲证——表外 action 且 namespace 不在 `_ASK_NAMESPACES`（L95={bash,mcp} 零改）落入 side_effects 残余径（False→allow），故 git 两键入表前效果即 allow、入表后 allow 零变；`_escaping_resource` L349 fs__ 前缀专属（git__ 结构性跳过，root 约束由 adapter 内 read.py helper 族承载）+本票 diff 外零改；两新动作 decision.source 标签 side_effects→default=调度器预裁定信息性记档面（存量动作零涉；测试 L357-371 钉 ("allow","default")）。
4. **测试质量 ✓**：22 测逐测清点与进度②映射逐测吻合（正常 9=status 空/脏/pathspec+diff unstaged/staged/pathspec+catalog wire 名 pinned+policy allow+默认值钉；失败 7=越根+traversal+NUL+缺失+git 缺失 TOOL_FAILED[monkeypatch]+非仓库 128/129 数据面；边界与契约 3=空 path+spec 只读语义+schema 验证器子集；平台 3=正斜杠+原生分隔符+驱动器大小写 win32 skipif）；真公开面（adapter→register→snapshot→invoke wire 名）；fixture 仓一律 temproot tmp_path（worktree git 面零触）；不可构造分支（超时/取消/not-reaped/Windows 别名残桩）如实披露+共享 run_command 进程面由 bash.py 专测承载论证成立。
5. **门禁数字 ✓**：B 独立复跑四件与 C 自报口径相容（format 380/mypy 116/ruff clean 三件逐值同；pytest B 轮 0F=断续面翻转第三观察）；1287=1265+22/380=118+262/116=114+2 三算术闭合；mypy 修复轨迹（首轮 6 错全在新 git.py→注解+窄化 assert 修面→终轮 0）=C 进度②自报，B 终态亲证 116/0 一致。
6. **存量兼容 ✓**：catalog 纯追加（新 git 命名空间整体纯增，既有条目相对序零扰动；test_tool_listing 零触全绿）；注册零重名（git__status/git__diff wire 名全新，catalog resolve 测试钉）；policy 表外行为零变（面 3 论证）；既有测试文件零触碰（name-status）；零新依赖红线维持。

## 2. 处方

1. **并回**：p2-s6 merge --no-ff 入 main 为 #172+主树四门禁复跑（断网面：venv 直跑形+conda mypy 形）。
2. **翻牌**：ORCH 台账 T-C1-053 行随 #172 置 DONE；主树 untracked 票 `doc/orchestration/tasks/T-C1-053.md`（49L）随 #172 入库。
3. **记档**：①定谳 a argv 位置修正确认（调度器已预裁定+B 探针双证+C 浮报纪律完备——待裁项呈报/探针记档/docstring 量化偏差记档，流水线正确运作范本）；②policy source 标签信息性记档；③断网改派依据（NET-WATCH 九轮全 False+B 场景键控表行 1 序+零新依赖免疫）随 #172（票面背景已载）；④M5/P2-S3 顺延 T-C1-054 待网络恢复派发。
4. **条文化**：无（零发现）。
5. **无他**。

## 3. 发现明细

- 零发现（四档全零）。附注四则（均非 C 面缺陷）：①定谳 a 位置修正=调度器起草笔误预裁定在案（`--no-optional-locks` 系 git 全局选项，子命令位=exit 129），B 独立探针 P1'/P3'（129/0）复核成立，落地形 flag 集逐字保留仅前移一位；②decision.source 标签 side_effects→default=信息性记档面（效果 allow 零变，B 代码亲证+测试钉死）；③B 侧工具面注记：Get-Content 对两份长 CJK 行文件计数伪读（24/17 vs 真值 67/49）——ReadAllLines+numstat+read 工具三证定谳文件实态健康；④取消/超时径无本票专测=票面测试纪律范围外+共享 run_command 进程面由 bash.py 专测承载+③ 如实披露，复核接受（PHASE2-MODULES L70 模块级「取消测试」验收面由共享进程面承载）。
