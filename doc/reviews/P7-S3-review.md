# P7-S3-review — T-C1-102 微评审：webfetch 工具新增——URL 取内容 text/markdown/html 三形（#5a 复评票，P7 主池 S3；提案 §3-C S–M 定级）

- 评审对象：worktree `p7-s3` 席位工作区（未提交态，席位 git 写零）：7 跟踪文件改动（app +11/cli +13/serving +27/−2/builtin __init__ +32/−1/policy +15/−2/test_httpapi_server +9/−7/test_workflow_tools +14/−9）+新 `src/opencode/tools/builtin/webfetch.py` 814L+新 `tests/integration/test_tool_webfetch.py` 748L 21 测+进度档+票面拷贝 untracked——面广恰合 S–M 票预判：全新增面（PY 零现成）+注册/权限/census 翻新，零邻接回归。
- 评审基据（全部独立现读，禁继承）：webfetch.py 全文分三段亲读（L1-150 模块 docstring=TS 映射/偏差全记录+常量/description/schema verbatim；L300-470 手动重定向链+有界 collect+text 抽取 void 矫正；L690-814 slice_bytes 二分+convert 门+`_execute_webfetch` fail-closed 复检+adapter unbound 面）、五小 diff 全图（policy url 第三面+注册 17 导出+双宿主 bind+app.policy property+census 17/18）、21 测签名结构（§3-8 逐项恰合）、进度档 12 节全文、TS 钉面（调度器派发前亲读 webfetch.ts 全文+席位亲读四件 webfetch.ts/html-markdown.ts/http-body.ts/mime.ts）、哈希亲证（四件 sha256 采集+追加 source-baseline.json 62 条）。
- 评审时刻：2026-11-16 05:13-06:4x（席位弧两段：attempt1 [05:13→05:39 infra 断：compaction failure 非代码失败，死点=实现全落后写测试前，产出全保留]+attempt2 续跑 [05:41→06:08 单段闭合 SEAT-EXIT 0：重定向→git 亲证工作树在位→续测试+门禁+进度档]；席位 git 写零——HEAD 恒 `25d509b` 验收时点亲证）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 3（全观察面，无返工）

## 六面逐条结论

### 1. 票面纪律 — PASS

- 禁 git 写遵守：HEAD 恒 `25d509b`（验收时点独立核）；工作区=7 改+两新文件+两档件恰合席位终态。
- 单段闭合（attempt2）：全量 pytest 前台收齐（670.05s）+进度档全档后 SEAT-EXIT 0；attempt1 infra 断=compaction failure（席位会话压缩基础设施错误）诚实记档非代码失败，attempt2 以 git 亲证恢复——恢复弧记档先例（同会话续跑+死点锚定提示）。
- 机制矫正一例全档：TS 序=scheme 先于初始 assert；PY 序=Invoker 门先于一切（宿主级门先于每 adapter）——双序同 fail-closed（网络 I/O 前双断）记档不翻面；text 抽取 void 元素矫正（htmlparser2 自闭 void vs stdlib 不发 endtag——`<embed>` skip 栈计数器泄漏防）+进度档「one Python correction」诚实面。
- §5 八项披露全落位+席位扩充至 12 项（含 §6 调度器须知：source-baseline 指纹补录归调度器——已由调度器执行：62 条追加+四哈希亲证）。

### 2. 决策锚落面（§3 八锚）— PASS

- 锚1 工具面：name/description verbatim 两段；Input {url 必填/format/timeout}——format enum+timeout 下界=strict subset 不可表达→adapter fail-closed 双面复检（§8 缺席键缺省 markdown=decoding-default-key 形）；Output 四字段+metadata={contentType}。
- 锚2 HTTP 执行面：httpx2 `follow_redirects=False`+`Timeout(None)`（wait_for 唯一 deadline）+一调用一 client；手动链逐跳（redirectStatuses 五集+urljoin 相对解析+逐跳 scheme 复断+逐跳 policy check+MAX 10 verbatim 句）+Cloudflare 403+cf-mitigated=challenge→UA "opencode" 整链重试一次。
- 锚3 html→markdown：席位亲读 html-markdown.ts 658L 后定形=PY 自形最小转换器 DIFFERENT-BY-DESIGN（stdlib html.parser 事件流块构建；覆盖面：h1-6/段落/链接/fenced pre+language-x 检出+嵌套列表缩进；放弃面全清单：tables/blockquotes/details/summary/images/hr/dl/内联标记；论证：第三方库=违 pyproject 零改纪律，逐 token 移植=658 行状态机非「最小」锚）；MAX_MARKDOWN_BYTES=5MB 保留（`_slice_bytes` 二分=TS sliceBytes 语义）。
- 锚4 text 抽取：六标签 skip 栈 verbatim 移植+void 矫正+strip()。
- 锚5 mime 面：isTextual 判据集 verbatim PY 化（text/*+json 两形+xml 两形+javascript 两形；未知=拒 fail-closed）；image 附件拒+svg+xml/vnd.fastbidsheet 两豁免。
- 锚6 policy：`webfetch__fetch="ask"` 行级默认（subagent 形）+`extract_resources` url 第三面（既有两面零变）+`_ASK_NAMESPACES` 不触（git-trio 先例）+双权限面映射（Invoker 门=assert 面；adapter bind(app.policy) 逐跳 check=assertNotDenied 面——deny-only 失败：ask/allow 过=重定向不重跑初始 URL 已清的审批协议，跳审计位 input_hash=""）。
- 锚7 契约面：预期恒零变兑现——路由/SDK census 零变（工具非端点）；工具目录 Spec=census 翻新测试两钉面（17/18+18 在/17 缺两臂）非契约工件。
- 锚8 非目标全兑现：websearch 零触碰/既有 13 工具零回改/pyproject 零改/外网零依赖（_Loopback 回环 fixture）。

### 3. 测试面（§4 九项→21 测）— PASS

- 21 测与 §4 九项映射恰形：三形全链×4（markdown 转换形状+text 隐藏子树+html 透传+非 html 透传）/缺省+边界×2（缺省 markdown/timeout 0·负·121+format 非法）/scheme 拒句/重定向×4（相对+绝对多跳/11 请求上限句/逐跳 deny 零命中/HTTP 404·500 包装）/mime×3（image+file 拒/svg 豁免）/字节×2（declared 预检+streamed per-chunk）/超时（scripted slow server 全链）/初始 URL deny→零网络 IO 直证/policy·Spec 钉/unbound fail-closed/传输错 Request failed 包装/Cloudflare UA 两请求直证。
- `_Loopback` fixture 自形（asyncio.start_server 随机口+每连接一请求+handler 注入式+`__aexit__` 取消在册 task+中段断连吞 OSError 不吞 CancelledError）——零外网依赖。
- 零回归：全量 2358P+1F+8S（1F=install 族相位窗红，§4 三窗处置）；邻接 census/两钉测全绿。

### 4. 门禁与算术 — PASS（席位侧全档；调度器独立面待 accept 档定稿）

- 席位自跑四件（全前台收齐）：ruff 0（首查 RUF059×1→修；RUF100 预防删 noqa）/ format 590→594（591 基线+2 新 .py+1 票拷 .md；进度档入库后终态 595）/ mypy **225-0**（223+2 恰合；首查 2 错皆新文件→修）/ 全量 **2358P+1F+8S@670.05s** collect **2367**=2346+21 恰合。
- install 族 4551 三窗处置（席位实测）：全量窗 1F→隔离窗单测 1F→隔离窗整文件重跑 **8P 全绿**（窗间零代码变更）=SAC/WDAC 相位窗抖动（flap 链红红绿形）非代码缺陷；判据链引用（092/093/095/096+S4 双态+S5 双窗+S0 红绿红+S1 绿绿红+S2 绿绿红复现形+#254）+本票改动面与 install 路径零交叠。
- 调度器独立四门禁（worktree p7-s3 真 python venv，`uv run --no-sync` 形，~06:1x-06:3x 时段）：**ruff 0 / format exit 0（`595 files already formatted` 与席位侧同数）/ mypy `Success: no issues found in 225 source files`（225-0 与席位同数）/ pytest `2359 passed, 8 skipped` exit 0 @701.65s——零失败全绿**；install 族本窗绿相位（席位窗 1F 红相位→验收窗绿相位=红绿时变形，flap 链 {0F↔1F↔4F} 谱内时变非树面缺陷；席位侧三窗红红绿处置+本窗绿=四窗谱恰合判据链；2367 collect 恰合；webfetch 21 测+邻接 census/两钉面零失败）。CI 终证=CI-WATCH-257（#253 派发检查点 `25d509b` CI-WATCH-256 已绿=同基线树 CI 终证先行面）。

### 5. 语义面独立复核 — PASS

- **重定向逐跳权限复查独立核**：`_make_hop_check` 闭包持 policy+context，每 hop evaluate deny-only——TS assertNotDenied 语义等价（ask/allow 过=不重触发初始已清审批）；初始 URL 门=Invoker §5.1 L259（resources=[url] 经 extract_resources）——双层 fail-closed 结构核通过。
- **Cloudflare 整链重试独立核**：`_CloudflareChallengeError` 于 `_attempt` 捕获→`_follow` 整链重跑 UA="opencode"——TS catchIf(isCloudflareChallenge) 臂；detail 永不外露（无参 `__init__` 消费于链内）。
- **有界 collect 独立核**：declared content-length 预检（int 失败=未声明=TS parseInt 守卫）+per-chunk 累计越限即断+finally 释放（close 失败不掩原错；CancelledError 不吞）——http-body.ts shape-for-shape。
- **超时边界独立核**：wait_for 覆 fetch+mime+collect（decode/convert 界外=TS Effect.timeoutOrElse 边界）；缺省 30/schema maximum 120/adapter 下界复检三面闭合。
- **输出面独立核**：structured_output 四字段+content 文本==output+metadata={contentType}（adapter 自层 ToolResult；Invoker 终态 ToolOutcome 无 metadata=测试经 direct execute 钉，诚实面）。
- **错误包装独立核**：`_FetchError` detail 句族（HTTP {status}/Unsupported image·file content type/Too many redirects/Response too large/Request timed out）+传输错 `Request failed: {error}`+统一 `Unable to fetch {url}: {detail}`——零裸抛零静默。
- **census 独立核**：17 base+platform PTY=18/双宿主（cli/serving）同 tuple 序+`len(catalog.specs)` 18 在/17 缺两臂钉+bijective census 推导面保持绿。

### 6. 披露与记档 — PASS

- §5 八项+席位扩充 12 项全档（html-markdown 选型全清单/两依赖件亲读/policy 形/注册 Spec 形/fixture 形/Cloudflare 可造/三窗算术/命名选型/续跑注记/§6 须知）；席位弧两段时序全档（attempt1 infra 断+attempt2 恢复）。
- 调度器已执行 §6 须知件：source-baseline.json 58→62 条（webfetch.ts+html-markdown.ts+http-body.ts+mime.ts 四指纹追加，哈希亲证）。

## 发现明细

- **建议 1（观察面，无动作）**：format 595 口径=594+进度档自身（T-C1-015 同口径先例）——收口后主仓复跑将现 596（+票拷消解+进度档入库+台账条 .md 三面时序差），调度器收口条算术须按实际复跑值记档（非漂移）。
- **建议 2（观察面，无动作）**：install 族 flap 本窗再现「红红绿」部分相位形（仅 1 员红非五员全红）——S2 窗形为五员全红「绿绿红」，本窗 1 员红=flap 链 {0F↔1F↔4F} 谱内时变形（T-A-014 #167 同判形态），判据链覆盖面无缺口。
- **建议 3（流程面，无动作）**：attempt1 compaction failure=席位会话压缩基础设施错误首例记档——后继席位若再遇同形，恢复弧先例=同会话续跑+死点锚定提示（本次一次过验证有效）。

## 附录：席位弧时序

- #253 派发检查点 `c0fb589` push→CI-WATCH-255 failure（S2 flaky 竞态，#254 修复 `25d509b`+CI-WATCH-256 success）→worktree p7-s3 ff 追新→四探针全绿（2338P+8S exit 0 install 族本窗绿相位）→发射 C 席（`ses_orch_C_p7s3`，票面 T-C1-102 §0-§8）→**attempt1** [05:13→05:39：TS 四件亲读+实现全落（webfetch.py+注册+五面翻新）→写测试前 infra 断（compaction produced no summary，非代码失败）；日志归档 T-C1-102.attempt1.txt]→**attempt2** 续跑 [05:41→06:08：同会话+死点锚定提示→git status/numstat 亲证工作树在位→三处实现自纠（scheme 门补位/Cloudflare __init__/窄化+RUF 两清）→748L 21 测落盘→四门禁全绿前台收齐（670.05s）→install 族三窗红红绿处置→进度档 12 节全档→SEAT-EXIT 0]；席位 git 写零（HEAD 恒 `25d509b`）；调度器侧 source-baseline 62 条指纹补录。
