> [PROVENANCE] 本票为 **T-C1-067 fixup 票**（源票 sha256 `CA6E520A28E5D825E0CF391C80D318A3202C21C2E338EF998EB45822051A29C7`/3517B）——承载撞号重编（T-C1-049→T-C1-067，11 处改号）+install 测试 XDG home 隔离（断言零改动）。归档于 #191 检查点（2026-11-11）。以下为源票全文（逐字节）：

# T-C1-067-fixup · 产品级配置桥修复补票（同 worktree 第二提交）

- worktree：`p2-s19` @`1343e4c`（第一提交=产品桥交付，未 push，树净）；席位 C-1；止损 30min
- 背景一行：T-C1-067（产品级配置桥——**派发号 T-C1-049 与 R5 纪元 R5-S6 票撞号**：任务表 L203 在册+`doc/orchestration/tasks/T-C1-049.md` 已档；调度器已裁定本票重编 **T-C1-067**，第一提交内自指字串沿旧行）交付后两件收口，本补票=同分支第二提交。
- 基线读数（`1343e4c` 调度器亲测）：pytest 1634 收集=1629P+4S+**1F**（唯一 1F=install 族前提位移——本票修复对象）；`ruff format --check` 463；`ruff check` clean；`mypy`（`uv run python -m mypy .`）147 source files 0 issues。

## 1. 写面（恰 8 路径；未列零触碰）

1. **`tests/integration/test_cli_install.py`（M）**：唯 `test_installed_cli_missing_maas_config_is_an_argparse_exit_2` 一测修复——其子进程 env 增 `XDG_CONFIG_HOME=<空 tmp 目录>`（fresh tmp_path 下建空 `xdg` 目录），使产品桥层静默不建、原断言面（exit 2 + `--base-url is required` 于 stderr）恢复。**该文件其余测试零改动；断言本体零弱化（原样保留）——修复=环境隔离，非断言修改**。机器无关性钉：修复后本测在有/无真实产品配置的机器上同形（产品目录恒=tmp 空目录）。
2. **票号重编 067（7 文件 M/R，字串/注释级零行为）**：`git grep -c 'T-C1-049' 1343e4c` 定界=**11 处 6 文件**（cli.py 2/loader.py 3/product.py 1/test_cli.py 2/test_config_loader.py 1/test_product_bridge.py 2）——全部改 `T-C1-067`：
   - `doc/orchestration/progress/T-C1-049.md` → **rename** 为 `doc/orchestration/progress/T-C1-067.md`：首行标题改号+正文首节增一行注记「dispatched as T-C1-049; re-designated T-C1-067 after the cross-era ticket-number collision (the R5-S6 release-check ticket owns 049)」+文内其余 T-C1-049 自指同步改 067。
   - `doc/orchestration/progress/T-C1-049-r5-s6-release-check.md` **名字与内容零触碰**（它是 R5 纪元 049 真身，归位 banner 已注明）。
   - 进度档尾追加 fixup 节（发现/修复/门禁终值/时戳阶梯）。
3. 提交：p2-s19 **第二提交**（fixup 形，ASCII 信息）；防引号啃噬用 `git commit -F <msgfile>`（先例：第一提交 PS 内嵌双引号被啃后 -F 修复）；**不 push；不 add -A**。

## 2. 硬约束

- 断言零弱化（install 测试只加环境隔离）；产品桥/加载器/CLI **代码行为零改动**（本补票源码面=注释/字串 only）。
- 冻结面照第一票（`_maas_real_config.py`/`session/`/`contracts/`/`tools/`/shellparse/golden/`doc/` 除进度档）零触碰。
- 机器事实：pytest 全量走 `uv run python -m pytest`+`PYTEST_DEBUG_TEMPROOT` 父目录先 `New-Item` 再设 env。

## 3. 门禁预期（自报精确数字）

- pytest：1634 收集=**1633P+4S+0F**（1F 修复归零；K 不变 27=1607+27）；install 族外 0F 硬判据照旧（如有 WDAC 拦截相须隔离复跑证据）。
- `ruff format --check .`：**463**（无新文件；rename 不改计数）。
- `ruff check .`：clean。
- `uv run python -m mypy .`：**147 source files 0 issues**。

## 4. 回报协议

终报 `SEAT-EXIT <code> <ISO时间>`+：路径/插入/删除计数、11 处改号逐文件计数、四门禁各自精确数字、与 §1/§2 钉死项偏差（零偏离=明说）。中途阻塞：停下报实况。
