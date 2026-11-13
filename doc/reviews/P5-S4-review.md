# P5-S4-review — T-C1-088 微评审：工人 catalog 全量 builtin 隔离枚举断言（T-B-094 R4）+ T-B-096 三 rider（纯测试票）

- 评审对象：worktree `p5-s4` 单提交 `a150cf8`（父=`a65b3b7`，numstat 恰 4 路径：改 3[tests/integration/test_workflow_tools.py +142/−1、tests/contract/test_registration.py +134/−3、tests/integration/test_registration_hosts.py +110] + 新 1[doc/orchestration/progress/T-C1-088.md 36L]——与票面 §4 预期逐路径吻合，零新文件；src/ 与 contracts/ 零触碰）。席位票 `T-C1-088.md` 钉面全量；进度档证据俱全（两处诚实记档+四组负对照红证+时间戳诚实修正条目）。
- 评审基据（全部独立现读，禁继承）：`git show a150cf8` 全量 diff 逐行亲读（422 插入）；进度档 36L 全文亲读；基面复证 registry.py（register/snapshot/ToolCatalog.specs 排序面/resolve）、serving.py `_builtin_adapters`、builtin/__init__.py 12 adapter+常量导出面、builtin/git.py worktree 三名常量定义面、pty.py 平台分支（win32+linux）、registration.py `_load_graph` L155-192+逐条承接族 L233-248、cli.py `_drive` 拒绝披露循环、httpapi/app.py ASGI 孪生、test_httpapi_server.py L707 census 先例、三宿主测试基点；本席独立复跑四门禁（env 钉 p5s4_temproot）：ruff 0 / format 546 exit0 / mypy 全 CI 形 202-0 / pytest 2205P+8S 零失败 557.58s；collect 2213=2204+9 恰增亲证。
- 评审时刻：2026-11-14 02:02:10（席位主弧 01:19 发射→01:47 SEAT-EXIT 0 ~28min，止损 100min 内；席位 git 写零，提交/合流/台账归调度器）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 1 / 建议 2（票面文本过期一项；观察面两条）

## 六面逐条结论

### 1. 票面纪律 — PASS
- 纯测试票红线亲证：四路径全在 tests/+doc/orchestration/progress/，src/ 与 contracts/ 零触碰；三组 scratch 破坏（serving.py 组合删两项/registration.py 逃逸检查破+承接族窄化/cli.py 披露循环破）全部复原如初（diff 终态亲证）。
- 既有测试零回改：TestRegistryIsolation 既有两测、TestContainment 既有两测、TestPerEntryRejection 既有测、两宿主既有 6 测全为纯追加；−4 行全为模块 docstring 弹性扩写，非断言回改。
- git 写零：席位全程 HEAD 恒 a65b3b7（席位终报+调度器独立核）；交付=工作区+进度档。

### 2. 主票全量枚举断言面（T-B-094 R4）— PASS
- §3-3 ①-⑤ 全落：①成员全集=常量逐名拼装（`f"{READ_NAMESPACE}__{READ_NAME}"` 形 12 名；worktree 三名常量自定义模块 builtin/git.py 导入——未再导出面如实处理并注释说明）frozenset 恰等；②(namespace,name) 排序确定性+计数=集合基（重复无处藏）；③集合级零 workflow 命名空间+既有 workflow 双探针对照保留；④每期望名 resolve 非 None；⑤导出面完备性双向钉（组合类名恰一次+`set(composed)==exported−gated`，exported 取 builtin.__all__ Adapter 面，gated=平台门控 PtyRunAdapter——新增 adapter 忘组合/组合私增双向红）。
- 组合面锚=§3-1(a)：serving.py::_builtin_adapters 直导（钉真宿主组合面，test_httpapi_server.py L707 census 先例同导入面）；无共享工厂提取——「禁为可测性改产品」纪律遵守；CLI 内联元组未直接断言（票面 §5 等构图注记遵守）。
- 平台分支双臂：win32/linux 在场臂 13 名 resolve 非 None vs 缺席臂 12 名 resolve None（`_PTY_PLATFORMS` 常量锚）；双 OS CI 腿绿（本机 win32 已实测，ubuntu 由收口 CI 承载）。

### 3. rider 1 containment 三形（T-B-096 次要 1）— PASS
- junction：win32 skipif+`cmd /c mklink /J` 免提权（subprocess 承载，returncode+is_dir 双断言）+盘外可注册 payload 证拒绝来自 containment 非读失败（沿既有 traversal 测形）+PipelinePathEscape 行+`engine.raws == []` 零读。
- symlink：os.symlink try/except OSError→pytest.skip 诚实跳过（权限不可用环境）；同 PipelinePathEscape 断言面。
- 「不可解析」形：探针五候选（非法字符 `<`/`|`、保留名 CON/NUL、尾点尾空格、junction 自环、symlink 自环 ELOOP）全数 NO RAISE（CPython 3.12 non-strict `Path.resolve()` 语义+POSIX realpath ELOOP 同为 seen 守卫部分解析）→ §3-6 诚实条款起用：改钉可达面（自环 symlink→零读+registered 空+manifest_error None+恰一行 NativeSpecMissing 结构化行，docstring 载探针发现）。「某形实测不可构造→如实记档禁造假」条款遵守；is_file() 对 ELOOP 的忽略集行为使该测双平台可移植（ubuntu 由收口 CI 承载）。

### 4. rider 3 非 UTF-8 读面（T-B-096 建议 2，087 后现形重铸）— PASS
- 原生 spec `b"\xff\xfe{not utf-8\xff}"` → `read_text(encoding="utf-8")` UnicodeDecodeError → 逐条承接行 `error_type=="UnicodeDecodeError"`+healthy 条目续注册（registered+raws 双证）+`manifest_error is None`（manifest 本身健康）。票面原文「graph.json」系 087 前旧形，重铸缘由票面 §3-4+进度档双注；helper `_write_native_bytes` 与既有 helper 零交集。

### 5. rider 2 宿主级披露（T-B-096 建议 1）— PASS
- CLI 宿主：有效 manifest+healthy/legacy-broken 两条目→code 0+monkeypatch spy 证 healthy-pipeline 在 drive loop 内注册+恰一行 stderr 点名三要素（skill id `legacy-broken`/pipeline 串 `legacy-broken/graph.json`/错误类 `NativeSpecMissing`）+stdout 零披露双断言（`"NativeSpecMissing" not in captured.out`+`"rejected" not in captured.out`）。
- ASGI 孪生顺手双写（票面 §3-5 允许面）：lifespan 完成事件序+同一行断言+`engine.bound("healthy-pipeline")` 证 boot 续。
- helper 本地构造（`_host_entry`/`_write_two_entry_skills`），零跨套件私有导入新增；hash 常量沿契约套锁定形。

### 6. 门禁与算术 — PASS
- 四件全绿（本席独立复跑，非继承席位自报）：ruff All checks passed!/format 546 files already formatted exit0（计数漂移+1=新进度档 md 面，判定面恒 exit0）/mypy 全 CI 形 Success: no issues found in 202 source files（零新文件基线不变）/pytest **2205 passed, 8 skipped** 零失败 557.58s。
- collect 算术闭合：2213=2204（#219 基线）+9 新测恰增零漂移（主票 3+rider1 3+rider3 1+rider2 2）；8 skips=基线既存集（新测零 skip 全落 P 面——本机 win32 junction/symlink 双权限可用）；warnings=aiosqlite GC 竞争既存面（席位跑 3 条/复跑 1 条，时序漂移，085/086/087 三方证先例）。install 族零失败（WDAC 4551 相位未现）。

## 发现明细

- **次要 1（票面文本过期——调度器钉面缺陷；席位处置正确零返工）**：票面 §3-2「pty_adapters() win32→1 adapter、非 win32→空」引用 T-B-087 ruling 2/T-C1-073 旧文，未反映 T-C1-074 linux T2 授权——实际 win32+linux 双启用。席位未盲从票文：循 test_httpapi_server.py L707 census 先例钉现实现形（`_PTY_PLATFORMS=(win32, linux)`）+进度档明记缘由。教训=票面钉面须核对实现现形，禁引用过期 ruling 文本（本评审件即档）。
- **建议 1（信息面，无动作）**：registration.py `_load_graph` 的 resolve→OSError 保守逃逸分支在 CPython 3.12 non-strict resolve 语义下实测不可达（探针五候选全 NO RAISE）——防御保留合理（未来语义变更/strict 化护栏），自环 symlink 测试已钉其邻接可达面；勿据此删分支。
- **建议 2（CI 面观察项）**：windows CI 腿时长链 13m30s→14m35s→**15m3s（run 43471329694 首跑撞 `timeout-minutes: 15` 天花板→cancelled，非代码红——ubuntu ✓ 4m50s）**→14m9s（`gh run rerun --job` 重跑腿 ✓→run **success**=a65b3b7 派发检查点 CI 终证达成）。余量薄（±1m 相位）；若再现 15m0s 超时，后补小票调 ci.yml `timeout-minutes`（15→20）；本轮仅记档不动 CI 面。
