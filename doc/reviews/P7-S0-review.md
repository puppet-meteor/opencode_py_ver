# P7-S0-review — T-C1-099 微评审：e2e_r4 §⑥ 合并 close 面回归腿（T-C1-098 registry 矫正开口项收口，测试面小票）

- 评审对象：worktree `p7-s0` 席位工作区（未提交态，席位 git 写零）：改 1 跟踪文件[`tests/integration/test_e2e_r4.py` +152/−12] + 新 2[进度档；票面拷贝 untracked]——**恰合票面测试面小票预期**（实现面零触碰=numstat 亲证，超预期偏差零）。
- 评审基据（全部独立现读，禁继承）：test_e2e_r4.py diff 全读（4 hunk：模块 docstring L3/L55-71 翻新+§⑥ 头注释块 L1207-1218 历史化+⑥c 追加 L1397-1520 全文）、⑥a/⑥b 体 verbatim 零触碰核证（hunk 边界=变更区两侧上下文，非 hunk 内）、进度档全文、⑥c 断言面与 ⑥a/⑥b 既有断言语义对照、席位机制探针复现腿记档复核。
- 评审时刻：2026-11-15 22:2x-22:4x（席位弧单段闭合：发射→SEAT-EXIT 0 @22:17:49[全前台 611.27s+进度档全档]；席位 git 写零——HEAD 恒 `ebf4025` 调度器亲证）。

## VERDICT: **PASS**

阻断 0 / 重大 0 / 次要 0 / 建议 2（全观察面，无返工）

## 六面逐条结论

### 1. 票面纪律 — PASS

- 禁 git 写遵守：席位全程 HEAD 恒 `ebf4025`（调度器独立核：工作区=1 改未提交+2 未跟踪恰合）。
- **机制探针零 git 纪律**：bounded red 复现腿=registry.py 临时还原全局锁形经 **edit**（非 git checkout）+测毕同法复原——探针零 git 写零残留（`git status`/`numstat` 复核证 registry.py 终态无改动）。
- 单段闭合（S4 教训延续）：全量前台收齐+进度档全档后 SEAT-EXIT 0——零续跑令。
- §5 披露余量四项全落位（⑥c 构造形/⑥b unknown 恰形/docstring L68 同步翻新[已同步——L55-71 块亲证]/collect 算术）。

### 2. 决策锚落面（§3 四锚）— PASS

- 锚1 ⑥c 回归腿恰形：一 App 双会话**并发各持 invoke**（A=fs__write 审批停泊[invoker L745 `await future` 持 A 会话 per-session 槽]+B=`_ParkedAfterCallAdapter` 包装 mcp__echo mid-execution 停泊持 B 会话槽——任务书 sanctioned wrapper 形）→`App.close(close_timeout_s=1.0)`→断言三面：**资源序** `order==_CLOSE_ORDER`（MCP[DB 仍可读]→provider→DB handle，§7 L339）+**副作用守卫**（`note.txt` 不存在=未批准写零发生+`_fixture_call_count==1`=close 零重执行外部调用）+**双 fold 各自恰形**（A=⑥a 序[cancel_requested(shutdown)→permission.closed(cancelled)→tool.finished(cancelled, not_started)→step.finished→run.finished(interrupted)]；B=⑥b 序[cancel_requested→tool.finished(unknown, unknown)→step.finished→run.finished(interrupted)]）——`_assert_ordered_subsequence` 全序钉+`_wait_pids_gone` 子进程零泄漏。
- 锚2 §⑥ 注释块翻新恰形：「structurally unreachable」子句历史化（过去时+「at the time of the split ... was registry-wide」+T-C1-098 per-session 键控事实句+「⑥c now pins the combined close face directly」）；docstring L68 同句同步翻新（§5-2 裁量项已行使）；语义句零改写纪律遵守（历史化措辞+事实更新形）。
- 锚3 实现面零触碰兑现：numstat 亲证唯一改动=测试文件；§3-3 机制矫正条款未触发（⑥c 一次过绿——close 路径双停泊 fold 无缺陷=开口项收口干净负证）。
- 锚4 format 基线归一载档：§6 记 format 583=580+3（.md 计数面算术记档）——三席 +2 漂移同形解释面落位（文件计数非码面），门绿为准。

### 3. 测试面（§4 两项）— PASS

- ⑥c 单测全链恰形（§3-1 逐断言亲证）+构造时序诚实面：A prompt→`permission.requested==1` 停泊证→B prompt→marker+`tool.started==1` 停泊证——**双停泊在场先证后 close**（非盲等）。
- 零回归三腿：e2e_r4 全文件 8 passed（7+1 恰合）+subagent 11 passed（T-C1-098 死锁回归腿在内=两矫正面互证）+邻接 close/recovery/approval 面 47 passed；collect 2308=2307+1 恰合。

### 4. 门禁与算术 — PASS（席位侧全档；调度器独立面占位待核证定稿）

- 席位自跑四件（全前台收齐）：ruff 0 / format 583 files exit 0（580 基+3 .md=583 算术记档——§3-4 归一面）/ mypy 219-0 / 全量 **2300P+8S@611.27s** collect 2308=2307+1 恰合；install 族+winpty 本窗零遇形（相位窗绿窗）。
- 调度器独立四门禁（worktree p7-s0 真 python venv，`uv run --no-sync` 形，22:2x-22:5x 时段）：**ruff 0 / format exit 0 / mypy `Success: no issues found in 219 source files`（219-0 与席位侧同数）/ pytest `5 failed, 2295 passed, 8 skipped` @630.61s——5F=install 族五员同集+4551 逐员直证=相位窗时变判据链直接适用（092/093/095/096+S4 双态+S5 红绿双窗；**本票三窗同树双态自证**：派发探针窗红[5F+4551]→席位窗绿[8/8 含 install 族]→本验收窗复红[5F+4551]——时变非树面缺陷；install 测试文件本票零触碰[唯一改动=test_e2e_r4.py numstat 证]=零因果交叠）；2295P+5F+8S=2308 collect=2307+1 恰合；⑥c/e2e_r4/subagent/邻接面零失败=本票面全绿**。CI 终证=CI-WATCH-250（#247 检查点 `ebf4025` CI-WATCH-249 已绿=同基线树 CI 终证先行面）。

### 5. 语义面独立复核 — PASS

- **⑥c 因果咬合独立核**：席位机制探针=registry `_serial_lock` 临时还原 registry 级全局形→⑥c bounded red 32.65s（恰在 `await _await_file_text(marker)` 失败[30s 上限]——B invoke 阻塞于 A 会话锁后、wrapper 体永不执行）→edit 复原 per-session 形→⑥c 复绿+全文件复验——**确定性因果直证**（非时序偶发）：⑥c 真钉「跨会话 head-of-line blocking 移除」面（T-C1-098 矫正语义的行为钉）。
- 双 fold 语义独立核：A「not_started」终态=审批未过工具未起（⑥a 语义）；B「unknown/unknown」终态=外部调用已发未收（started 在档、终态诚实未知=⑥b 语义）——两会话 fold 互不串扰，恰为「合并 close 面」的语义本质（此前 split 因锁不可达）。
- 历史化措辞独立核：「was structurally unreachable -- ④ float report then」过去时+矫正事实句——与 S5 进度档「记录句历史化」处置形一致（描述性语句更新、规范性语义[census/契约]零触碰）。
- 资源序断言复用 `_OrderRecordingManager`/`_OrderRecordingProvider` 既有 harness——与 ⑥a 同构断言面=可直接对照。

### 6. 披露与记档 — PASS

- 票 §5 四项全落位+席位余量至 8 条（⑥c 构造时序证形/⑥b unknown 恰形段锚/机制探针 bounded red 全档/`_ParkedAfterCallAdapter` wrapper 选型理由/hunk 边界零触碰证/format 583 算术/零 git 探针纪律/install+winpty 绿窗记档）。
- 席位弧时序单段全档。

## 发现明细

- **建议 1（观察面，无动作）**：机制探针复现腿（临时还原+bounded red+edit 复原）为测试面小票的高价值验证形——后继「回归腿钉矫正语义」类票可引用此先例（探针零 git 纪律沿袭）。
- **建议 2（观察面，无动作）**：format 583=580+3 .md 计数面已归一记档——后继席位 format 基线按 583 起算（漂移判据沿「+2 连续三席同形=文件计数面」解释链）。

## 附录：席位弧时序

- 发射（探针相位窗张开形+CI-WATCH-249 绿 @`ebf4025`）→席位单段闭合：亲读锚定（§2 四锚+registry/app/service/invoker 面）→⑥c 落笔→机制探针复现腿（bounded red 32.65s+edit 复原）→零回归三腿→四门禁全前台收齐（611.27s）→进度档全档→SEAT-EXIT 0 @22:17:49→看哨 TRUE-EXIT round 23；零 git 写（HEAD 恒 `ebf4025`）。
