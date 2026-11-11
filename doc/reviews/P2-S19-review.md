# T-B-081 · P2-S19 微评审（席位B 规划评审）——T-C1-067 产品级配置桥（两提交链）

- 评审对象：worktree p2-s19 @`9e537ac`（两提交链 `1343e4c` 主交付 + `9e537ac` fixup[改号+install 测试隔离]；父 `eb24cdd`=#190 检查点=origin/main——本席 `rev-parse` 亲证）。合并面双记账自洽：rename 检出形 9 路径 1047+/23−（`{T-C1-049.md => T-C1-049-r5-s6-release-check.md}` 恢复链坍缩为 1+）；逐提交和=票面口径 1106+/82−（c1 8 路径 1071+/70− + c2 8 路径 35+/12−，numstat 逐文件亲证）。
- 评审基据（方法）：票 `T-B-081.md`；C 席进度档 `doc/orchestration/progress/T-C1-067.md`（③④ 浮报 5 项 + §⑤ fixup 收口自报）；两提交全 diff 亲读（product.py 189L 全文、loader.py/cli.py hunk 逐条、四测试文件新增段全文、install +6）；B 席独立复算面：R5 恢复档字节同一性（base/tail 字节循环比对 + git blob 三算）、`_strip_jsonc` 与 T-C1-048 `tests/integration/_maas_real_config.py` 逐行比对、`git ls-tree` 文件数复算（format/mypy 计数底账）、`git grep` 改号余量、`_instruction_sources` 两分支亲读。四门禁免重跑（调度器双源同值在案），本席仅静态交叉核对（§2.9）。
- 评审人：席位B（T-B-081）；2026-11-11；止损 45min 未触；worktree 零 git 写（HEAD 恒 `9e537ac` 净树，只读 `git -C`）；主树唯一写面=本评审件+日志。

## §0 总裁决

**PASS**。计数：**blocker 0 / major 0 / minor 1 / suggestion 2**。十面全 PASS（③ 浮报 5 项逐项真；漏报抽查 4 面零旁路/零稀释/零漂移）。两提交链对票面钉死项零偏离：第五层插入恰成（builtin < product < global < project(root→cwd) < CLI；产品层恰三既有键 endpoint/api_key/model；`_validate_layer_keys(host_tier=True)`；`directory=None`；全缺席层静默不建恰原三段）；桥为 T-C1-048 扫描器**代码体逐字符同一**的独立副本（零 refactor，审查面分离主张成立）；脱敏纪律全钉（9 raise 点零值回显、快照零 api_key、endpoint_ref 剥 userinfo/query/fragment、错误面/DB 面/快照面负断言真）；CLI 重排三源回退 fail-closed 且 main() 零改动、model/context_limit 校验行零改动；install 修复 +6 行机况无关、断言零弱化；R5-S6 被覆盖档逐字节恢复本席三算亲证（blob `d23a4ef9` 双端 MATCH）；改号 11 处 src+tests 零余量。唯一 MINOR 为 c1 门禁表 format 读数的跑点-提交点时序注记（462=恢复档落盘前的工作树；终态 463 席位+调度器双源闭合），文档精度级零终态影响。

## §1 十面判定表

| # | 面 | 判定 | 要点 |
|---|---|---|---|
| 1 | 层序与键集合 | PASS | `load()` 层序 [builtin, product?, global, project(root→cwd)] + `layers[1:]` 顺 merge（global 覆盖 product 同键——钉）；恰三键 {endpoint, api_key, model}（皆既有：HOST_ONLY_KEYS loader.py:98 / _KNOWN_KEYS :115）；host_tier=True（与 global 同级信任）；directory=None；外语键不进层值零报错（theme 钉）；全缺席恰原三段（钉） |
| 2 | 桥模块正确性 | PASS | XDG>home+`explicit` 测试注入口；json+jsonc 双读浅合并（jsonc 键覆盖——钉）；scanner 与 T-C1-048 代码体逐字符同一（唯一差=docstring 移植注记+换行重排 8 行，0 行代码差）；URL 内 `//` 与字符串内逗号双钉；provider 块名 env 缺省 `gateway`（钉）；baseURL/apiKey 平铺或 options 嵌套两形；model=models 首键插入序；auth.json 永不读（零引用+钉） |
| 3 | 脱敏纪律（最重） | PASS | 9 raise 点逐核零值回显（3 读文件面仅 `{path.name}` 固定名；6 结构面仅 provider 名+固定措辞）；快照零 api_key（V2/V3 字段面亲证）+ endpoint_ref=`_sanitize_endpoint` 剥 userinfo/query/fragment（loader.py:310，钉：`leak-candidate`/`token=leaked` 不在规范序列化）；错误面/DB 事件面负断言真（后置错误真实触发）；product.py 零 print 零 logging |
| 4 | CLI 重排正确性 | PASS | env 回退→`load()`→合并值回退（`isinstance(str)`+真值 fail-closed）→三源命名 parser.error 零值回显；model/context_limit 校验行零改动（diff 尾上下文亲证）；main() 零改动（cli.py 恰 3 hunks 全在 `_parse_arguments` 内）；help 刷新与行为一致；flag>env>product 三优先序钉 |
| 5 | loader 接线 | PASS | 构造参数 `product_config_dir` 注入口（None=生产缺省）；两处函数级局部 import 防环（product.py 模块级 import ConfigError 反向成环——解法成立）；provenance=双读序末文件（jsonc 在则 jsonc——代码注释+形状钉双载）；merge 参与序钉 |
| 6 | 测试封闭性 | PASS | 桥/层=构造参数注入；CLI 面=XDG monkeypatch（机制差=浮报 2 属实：CLI 缝无注入口）；双 hermetic fixture 剥 env（bridge 剥 PROVIDER/XDG；CLI 剥 4 键）；既有测试读真机产品层=浮报 3 属实（诚实披露）；home 回退腿=纯路径断言零读盘 |
| 7 | 碰撞处理 | PASS | 字节同一性 B 席复算：tail 10880B==base 10880B（字节循环比对 True）；git blob 三算同值 `d23a4ef9f67f27db0636bdb0d18ed07c27db4e59`；算术 704+10880=11584 闭合；R5 档名/内容零触碰（c2 路径集不含）；改号 11 处（c2 numstat 行和=11）+ `git grep` src+tests=0 命中；进度档 049 残留=c1 正文恰两处保真引文（L49）+fixup 重编史实注记（新增叙事非残留） |
| 8 | install 修复 | PASS | +6 行=3 注释+3 代码（XDG 钉 fresh 空 tmp）；断言四行零弱化亲证（exit 2/`--base-url is required`/`OPENCODE_PY_BASE_URL`/无 opencode.db）；机况无关（XDG 已设则 home 回退不可达）；其余 install 测试零改动；iso-2/3 双 PASS+全量 0F 自洽 |
| 9 | 门禁数字交叉核对 | PASS | 免重跑（调度器双源：pytest 1634c/1630P+4S+0F seat@367.72s + dispatcher-acc2@395.34s MATCH + format 463 + lint clean + mypy 147-0）；B 席静态复算全吻合：1607 基线+K27（17+4+6）=1634、1630+4+0=1634、ls-tree 316md+147py=463（基线 315+145=460，Δ=+1md 恢复档+2py 新文件恰合）、mypy 145+2=147；WDAC 相位链与 #187-#190/T-B-080 惯例一致 |
| 10 | ③ 浮报核验+漏报抽查 | PASS | 浮报 1-5 逐项真（§2.9 表）；漏报抽查 4 面（§2.10）：指令链旁路闭环、host-only 纪律零稀释、消费单轨无第二点、外语键零报错路径——零发现 |

## §2 逐面证据

### 2.1 层序与键集合（票面钉死项）

- **层序**：`load()` 建 `[builtin, product?, global, project…]`（product 层插在 builtin 后、global 前——diff 亲读）；合并 `merged = dict(_BUILTIN_VALUES); for layer in layers[1:]: merged = _merge_json(merged, layer.values)` 顺序覆盖——后层胜前层：project>global>product>builtin；CLI 面在 `resolve_runtime` 顶部另覆盖（既有面零改动）。钉：`test_global_overrides_product_on_the_same_keys`（global 三键全胜 product + CLI model 又胜 global）。
- **恰三键零增改**：桥输出恰 `{endpoint, api_key, model}`（`resolve_product_values` 返回形+钉断言 values==恰三键）；三键皆既有——`HOST_ONLY_KEYS = {roots, endpoint, mcp, api_key, credentials}`（:98）、`_KNOWN_KEYS = _RUN_KEYS | HOST_ONLY_KEYS`（:115，model 在 _RUN_KEYS——否则产品层过不了 `_validate_layer_keys`，而形状钉全绿）。产品层 `_validate_layer_keys(contributing, layer_values, host_tier=True)`——endpoint/api_key 在 host_tier=True 下合法（与 global 同级信任面）；project 层仍 host_tier=False 拒收（钉 §2.10b）。
- **directory=None**：产品层 `ConfigLayer(origin=f"product:{dir}", values, path=contributing, directory=None)`——指令链闭环节 §2.10a。
- **外语键全忽略零报错**：外语键（theme/keybinds/$schema/mcp/autoupdate…）根本不进层值（桥只产三键），故不触 `_validate_layer_keys` unknown-field 面——theme 钉（写入 theme 后层值仍恰三键、零 ConfigError）。
- **全缺席=层静默不建**：`resolve_product_values` 双文件缺席返 None → 不 append——钉 `test_no_product_config_keeps_the_original_three_tier_chain`（层链恰 [builtin, global, project] 三段，无幽灵产品层）。

### 2.2 桥模块正确性

- **目录序**：`product_config_dir(explicit)`——explicit（测试注入口）> `$XDG_CONFIG_HOME/opencode` > `Path.home()/.config/opencode`，纯路径零读盘（钉含 home 腿纯路径断言）。
- **双读浅合并**：`for name in (JSON_FILE, JSONC_FILE)` 逐 `merged.update()`——jsonc 键覆盖 json；钉 `test_dual_read_shallow_merge_jsonc_overrides_json`（jsonc provider 块整体替换 json 同名顶层键，值=纯 jsonc 来源）。
- **scanner 逐行比对（票面要求）**：B 席提取两文件 `_strip_jsonc` 函数体逐行比对——**代码体逐字符同一**（44 vs 42 行差异全部为 docstring：product 版加移植注记「independent copy by design」+行宽重排；`Compare-Object` 零代码行差）。URL 内 `//` 钉（`https://gw.example.test//double//slashes` 完好穿过剥蚀器）+字符串内逗号钉（`r0-key,with,commas` 与 map 键 `"m,c"` 完好、行注释与尾逗号照剥）。
- **结构面**：provider 块名 `os.environ.get("OPENCODE_PY_PROVIDER") or "gateway"`（钉：缺省 gateway-model 腿 + env=other 切块腿）；`_block_field` 平铺优先、options 嵌套次之（钉 flat/nested 两形）；model=`next(iter(models))` 首键插入序非排序（钉 zeta-model 在 alpha-model 前）；**auth.json 永不读**——product.py 全文零 auth.json 引用 + 钉（仅 auth.json → None 层不建）。

### 2.3 脱敏纪律（最重）

- **raise 点逐核（9 点，非票面沿写的 11——对账见 SUGGESTION-2）**：`_read_product_file` 三点（unreadable / not valid JSON / not a JSON object——消息仅 `{path.name}`=固定文件名 opencode.json/jsonc）；`resolve_product_values` 六点（块缺席 / 块非对象 / 无 baseURL / 无 apiKey / models 空映射 / 空 models 键——消息仅 `provider_name!r`（env 或缺省块名，非凭据）+固定措辞）。**零已读值回显**：值面（baseURL/apiKey/model）任何一条都不出现在任何 f-string。钉：missing-baseURL 腿负断言 `"r0-must-not-echo" not in str(err)`、missing-apiKey 腿负断言 `"https://leak-endpoint.test/v1" not in str(err)`，均带绿腿对照。
- **快照无密钉**：`ConfigSnapshotV2` 字段面=model/endpoint_ref/roots/permission_rules/instructions/limits（V3 再加 mcp_servers）——**无 api_key 字段**（构造点亲读，:781-790）；endpoint_ref=`_sanitize_endpoint(endpoint_url)`（:310 §6 L287 剥 userinfo/query/fragment）。钉 `test_v3_snapshot_carries_no_api_key_and_a_sanitized_endpoint_ref`：`https://user:leak-candidate@…?token=leaked#frag` → 序列化无 `r0-snapshot-secret`/`leak-candidate`/`token=leaked`，endpoint_ref==`https://gw.example.test:8443/v1` 净形。
- **负断言真实性**：`test_product_credentials_never_echo_on_the_error_face`——产品层凭据已解析后（context_limit 无处供给）真错误面触发，out/err 双负断言；e2e 钉 DB 面：`py.session.created` 事件 payload 含产品 model（`config_snapshot.model.model=="fake-model"`——产品层模型到达持久快照）且 `r0-e2e-product-key` 不在 `json.dumps(payload)`——stdout 与 DB 双面封口。
- **面外零向量**：product.py 零 print/零 logging/零写盘（docstring 纯度主张+代码亲证）；CLI parser.error 文案零值；loader 新增段零值输出。

### 2.4 CLI 重排正确性

- **序**：env 回退两行原形保留（`args.base_url = args.base_url or os.environ.get("OPENCODE_PY_BASE_URL")` 等）→ `ConfigLoader(global_config=args.config, project_root=args.project_root).load()`（既有提前发现面，位置未动）→ 合并值回退：`endpoint if isinstance(endpoint, str) and endpoint else None`——**字符串真值校验 fail-closed**（非盲赋值）→ 仍缺者 `parser.error` 三源命名（`--base-url is required (or set OPENCODE_PY_BASE_URL, or supply 'endpoint' in the product/global config)` 形，api_key 对称）。
- **零改动钉**：model/context_limit 校验行原样（diff 尾上下文亲证——model 面原已走 `values.get("model")` 回退，第五层自然供及）；**main() 零改动**（cli.py 合并 diff 恰 3 hunks 全在 `_parse_arguments` 内，`resolve_runtime_v3(endpoint_url=args.base_url)` 与 `MaasAdapter(args.base_url, args.api_key)` 均为既有行）；help 文本刷新与实际行为一致（`--base-url` help「flag > env > 层」=实现序）。
- **三优先序钉**：flag>product（`test_explicit_flags_beat_the_product_config`）、env>product（`test_env_beats_the_product_config`）、免三旗标 parse 钉（`test_parse_arguments_runs_three_flag_free_on_product_config`——args.model 保持 None、模型经合并值供给）+ e2e 主循环钉（`test_cli_runs_e2e_three_flag_free_on_product_config` 全回路）。

### 2.5 loader 接线

- 构造参数 `product_config_dir: str | Path | None = None`——None 时惰性 import 取生产缺省（`__init__` 内局部 import 注释自述防环）；`load()` 内第二处局部 import 取 `resolve_product_values`。防环解法成立：product.py 模块级 `from opencode.config.loader import ConfigError`（错误类型归属 loader 既有面），反向模块级导入会成环。
- **provenance**：`contributing = jsonc_path if jsonc_path.is_file() else json_path`——双读序末位文件（jsonc 键覆盖 json，合并值对末位文件负责）；代码注释+钉 `test_product_layer_shape_origin_path_directory`（origin=`product:{dir}`、path=末位文件、directory=None、values 恰三键；仅 json/仅 jsonc/双文件三腿均钉）。
- **merge 参与序**：§2.1——global 可覆盖 product 同键（钉）。

### 2.6 测试封闭性

- 桥面 17 测：构造参数/tmp_path 注入 + autouse `_hermetic_env` 剥 OPENCODE_PY_PROVIDER/XDG_CONFIG_HOME——真实 `~/.config/opencode` 零读。
- 层面 4 测：`ConfigLoader(product_config_dir=tmp)` 注入。
- CLI 面 6 测：`XDG_CONFIG_HOME` monkeypatch 指向 tmp（生产 env 路径）+ `_hermetic_cli_env` 剥 BASE_URL/API_KEY/PROVIDER/XDG 四键 + `_no_product_config` 显式指空 tmp（真实家回退路径存活故必须显式指空——浮报 2 的诚实处理）。机制差=浮报 2 属实：`_parse_arguments` 构造自有 ConfigLoader 无 product_config_dir 面（diff 亲证）、main() 零改动为票面钉死——XDG setenv 是 CLI 缝唯一生产注入口。
- 既有测试读真机产品层=浮报 3 属实：loader 缺省目录=生产目录（票面设计）；本机真实配置参与既有测试 `load()`（席位子集 70P 自报+全量 1630P+0F 双源旁证）。B 席亲核既有 `test_argparse_missing_required_args_exit_2` 机况无关形：有产品配置机=model 由产品层供及、context_limit 缺→exit 2；无产品配置机=model 缺→exit 2——两态同 exit 2（docstring 自述「missing --model or --context-limit」恰覆盖两腿）。

### 2.7 碰撞处理（B 席复算）

- **字节同一性三算**：base=`eb24cdd:T-C1-049.md` 10880B；restored=`9e537ac:T-C1-049-r5-s6-release-check.md` 11584B；banner+LF=704B；tail=10880B 与 base **逐字节相等**（PowerShell 字节循环比对 True）。git blob 三算同值：`rev-parse eb24cdd:…` = `hash-object tail` = `hash-object base` = **`d23a4ef9f67f27db0636bdb0d18ed07c27db4e59`**（调度器 MATCH 独立复证在案，本席复算确认）。算术 704+10880=11584 闭合（与席位自报同值）。
- **banner**：一行 704B 席位注记（原路径/R5-S6 席位出处 2026-11-07/跨纪元撞号机制/按「新建」口径覆盖经过/逐字节恢复方式/blob 亲证/原档 `git show eb24cdd:…` 直取路径）——亲读，零密、叙事与 git 事实吻合。
- **R5 档名与内容零触碰**：c2 路径集不含该档（8 路径=1R 进度档+6M 改号+1M install）；banner 以下=基线原样（字节同一性即证）。
- **改号 11 处零行为**：c2 numstat cli 2+loader 3+product 1+test_cli 2+test_config_loader 1+test_product_bridge 2=11（与席位分列口径逐项同）——全注释/docstring 字串面；`git grep 'T-C1-049' 9e537ac -- src tests` **rc=1 零命中**（B 席亲跑）。
- **进度档保真**：049 残留=c1 正文恰两处保真引文（L49：派发票面原文引「新建 `…T-C1-049.md`」+R5 档真名 `…T-C1-049-r5-s6-release-check.md`——改号将致引文失实/指向不存在文件）；其余命中（L3 首节重编注记、L63/L66/L67 fixup 重编史实）皆 fixup 新增叙事非残留；小写 temproot 名（t-c1-049-full-1/iso-1）为跑记录保真。改号史实由首节注记+§⑤ 承载——主张成立。

### 2.8 install 修复

- +6 行=3 注释+3 代码：`xdg_config_home = tmp_path / "xdg"; xdg_config_home.mkdir(); env["XDG_CONFIG_HOME"] = str(xdg_config_home)`——子进程 env 钉空 tmp，产品桥层静默不建。
- **断言原文未动零弱化**：`returncode == 2` / `"--base-url is required" in result.stderr` / `"OPENCODE_PY_BASE_URL" in result.stderr` / `not (tmp_path / "opencode.db").exists()` 四断言原样（文件终态亲读）；diff 上下文其余行零改动；该文件唯一 hunk=本测——其余 install 测试零改动（numstat 6+/0−）。
- **机况无关形**：XDG 已设则 `product_config_dir()` 取 XDG 腿、home 回退不可达——产品目录恒=空 tmp（无论真机有无 `~/.config/opencode`）。修复后该测 iso-2/iso-3 双 PASS + 全量 0F（席位报，与 1630P+0F 自洽）。

### 2.9 门禁数字交叉核对（免重跑；静态三腿+算术底账）

| 项 | 席位自报 | 调度器双源 | B 席静态复算 | 判 |
|---|---|---|---|---|
| pytest 全量 | 1634 collected=1630P+4S+0F @367.72s（rc 0） | SEAT-LAUNCH 基线行：seat@367.72s + dispatcher-acc2@395.34s **MATCH** | 1607 基线（T-B-080）+K27（桥 17+层 4+CLI 6）=1634；1630+4+0=1634；c1 1F→fixup 0F 修复归零自洽（1629P+4S+1F→1630P+4S+0F） | ✓ |
| format | 463 files already formatted（rc 0） | 独立静态腿 463 同值 | `git ls-tree` 底账：base 315 md+145 py=460 → head 316 md+147 py=**463**（Δ=+1md[恢复档]+2py[新文件]恰三新文件） | ✓（c1 时点 462 见 MINOR-1） |
| lint | All checks passed!（rc 0） | 同值 clean | —（免重跑） | ✓ |
| mypy | Success: no issues found in 147 source files | 147-0 同值 | 145+2 新 py=147 ✓ | ✓ |
| WDAC 相位 | iso-1 4F+3P（全 4551 CreateProcess 拦截形）→iso-2/3 各 6P+1F（1F=零触碰面首 spawn 拦截）→全量 install 族 7/7 绿 | — | 与 #187-#190/T-B-080 相位型惯例一致；被修测试两 iso+全量三腿全绿=修复实证 | ✓ |

- **K=27 新测试**收集面精确闭合（1607+27=1634）；§3 勘误注记（补票预期 1633P 与自报基线算术不合→实报为准）为诚实披露。

### 2.10 ③ 浮报核验 + 漏报抽查

**浮报核验（5 项全真）**：

| # | 席位自报 | 判 | B 席证据 |
|---|---|---|---|
| 1 | install 前提位移+机况依赖（预测形落地，非拦截形） | 真 | 机制亲证：该测 env-strip 仅剥 OPENCODE_PY_* 两键（终态文件亲读）；XDG 未设→home 回退对 installed exe 可见；真机产品层经第五层供及 endpoint/api_key/model→期望的 `--base-url is required` 被换为 context-limit 错——预测形与重排后代码路径逐字吻合（凭据+model 全解析后唯一可缺=context_limit）；隔离复跑 1F+6P 确定性（非相位漂移）与全量相位放行并存自洽 |
| 2 | CLI 注入机制差（§4 字面 product_config_dir 注入 vs 实走 XDG env 路径） | 真 | `_parse_arguments` 构造 ConfigLoader(global_config, project_root) 无 product_config_dir 面（diff 亲证）；main() 零改动票面钉死；`_no_product_config` 显式指空=同产封闭纪律的正确处理 |
| 3 | 既有未改测试读真机产品层 | 真 | loader 缺省目录=生产目录（票面设计 None=生产缺省）；既有测试无注入面；子集 70P（席位报）+全量 1630P+0F（双源）证无碍；exit-2 测试两态同码亲核（§2.6） |
| 4 | 偏差声明（层序键集合零偏离；写面外零触碰；写面偏差恰一处=碰撞恢复） | 真 | 合并 9 路径全落写面声明内（§1 numstat 亲证）；pyproject/session/contracts/tools/shellparse/golden/_maas_real_config.py/c1 时点 test_cli_install.py 零触碰 |
| 5 | 票号跨纪元碰撞+被覆盖档恢复 | 真 | §2.7 B 席复算全证（字节同一性三算/b banner/零触碰/残留账） |

**漏报抽查（4 面，零发现）**：

- **a) 指令链旁路（directory=None 真闭环）**：`_instruction_sources` 两分支亲读——`if layer.directory is not None:` 分支产品层 directory=None 短路（产品目录 AGENTS.md 不可达）；`if layer.path is not None:` 分支读 `layer.values.get("instructions")`——桥输出恰三键无 instructions 键→declared None→continue。产品层 path 入 seen_files 仅参与去重，无第二旁路。**闭环**。
- **b) host-only 键恰容性（既有纪律零绕行/零稀释）**：产品层 host_tier=True 允 endpoint/api_key（与 global 同级信任——既有纪律原本就允许 global 层携此二键，产品层同形非新口子）；project 层拒收回归钉 `test_project_layers_still_reject_host_only_keys_with_the_product_tier_present`（endpoint/api_key 各腿 host-tier ConfigError）——产品桥不稀释 project 层信任边界。
- **c) 消费路径单轨（无第二消费点/无双轨漂移）**：main() 只传 `args.base_url`（`MaasAdapter` 与 `resolve_runtime_v3(endpoint_url=…)` 两用同源）；层值经 `_parse_arguments` 回填 args 即单轨；`recover()` 重放已录快照（L302 纪律，不重读当前配置——产品层后续变更不影响恢复面）。零第二消费点。
- **d) 外语键零报错路径**：外语键不进层值故永不触 `_validate_layer_keys` unknown-field 面（theme 钉）；产品层永不触 host-only 拒绝面（host_tier=True）。

## §3 判定面（强制）

- **计数：blocker 0 / major 0 / minor 1 / suggestion 2。**
- **MINOR-1（文档精度级）**：定位 `doc/orchestration/progress/T-C1-067.md` §② c1 门禁表 format 行自报「462 files already formatted（基准 460+新增 2 py）」。B 席 ls-tree 底账：c1 **提交树**=316 md+147 py=463（R5 恢复档 +1md 已在 c1 提交内）——462 读数=跑点在恢复档落盘**之前**的工作树（碰撞系提交亲证发现，恢复件于门禁跑后、提交前落盘，时序自然但门禁表未注记该树差）。**影响**：零终态影响——fixup 全量重跑 463 + 调度器独立静态腿 463 双源闭合（§2.9）；证据链无恙，仅中间读数与提交树描述错位。**处方**：后续多提交链若门禁跑后又有文件落盘，在门禁表注记跑点树差或补跑快腿（调度器工序裁量；无需独立票、无需代码改动）。
- **SUGGESTION-1**：`loader.load()` 在 `resolve_product_values` 已完成双读后再次 `jsonc_path.is_file()/json_path.is_file()` 复查定 provenance——读与复查间理论 TOCTOU 窗，单线程启动零实险（与既有双重发现模式同性质）；若求紧凑可让桥一并返回 `(values, contributing)`。零行为差异，非缺陷。**处方**：无需。
- **SUGGESTION-2**：票面维度 3「11 raise 点逐核」系 T-C1-048 桥（env 模式 1+resolve 7+读文件 3）模板沿写；产品桥实为 **9 raise 点**（读文件 3+结构 6），本席逐点核毕全部零值回显——席位自报口径（「5 错误面」分组）与实况自洽，非席位失实。建议后续派发票按目标模块面钉 raise 计数，免评审端对数消耗（调度器票面卫生项）。**处方**：调度器裁量。
- **总分裁决：PASS**——票面钉死项（层序/键集合/脱敏/重排）十面全兑现；浮报 5 项全真；漏报抽查 4 面零发现；R5 恢复字节同一性本席三算亲证；四门禁双源同值+静态底账吻合。

## §4 结论

T-C1-067 以两提交链精确交付产品级配置桥：第五层插入恰成（恰三既有键、host_tier=True、directory=None、全缺席静默不建、global 覆盖 product 同键钉）、桥为 T-C1-048 扫描器代码体逐字符同一的独立副本（string-aware 剥蚀双钉、auth.json 永不读、9 raise 点零值回显）、CLI 三源回退 fail-closed 且 main()/model/context_limit 校验行零改动（免三旗标 e2e 全回路+DB 无密钉）、loader 接线构造注入口+防环+末文件 provenance、测试三面封闭（构造参数/XDG 生产路径+机制差诚实披露/既有测试机况依赖面披露）、R5-S6 被覆盖档逐字节恢复（本席 blob 三算 MATCH `d23a4ef9`）+改号 11 处零余量、install 修复 +6 行机况无关断言零弱化。四门禁调度器双源同值（1634c/1630P+4S+0F、format 463、lint clean、mypy 147-0），B 席静态底账（ls-tree 文件数、K=27 收集算术）全吻合。MINOR-1 为 c1 门禁表 format 读数时序注记（终态双源闭合零影响），SUGGESTION×2 为工序卫生项。P2-S19 交付在档，待调度器验收合并。
SHA256（本行以上全文——评审件正文，不含本行及其前导换行；复算=去除本行与其前导换行并 CRLF→LF 归一、UTF-8 无 BOM 后 Get-FileHash -Algorithm SHA256，沿 T-B-073 工序口径） E13346FA3A5041377D2327F4BA2F44C2C41E95142F865C800AD3AF3C6DAA17B1
F1=P2-S19-review.md 125L
