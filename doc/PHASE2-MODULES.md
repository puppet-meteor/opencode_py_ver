# Phase 2:按需补齐模块(每个模块独立可停)

> 前提:Phase 1 的 6 个接缝已冻结并保持"只增不改"。
> 每张模块卡片 = 一个独立决策单元:可单选、可推迟、可跳过。
> 全部模块都遵守:**新增文件优先;改接缝必须先写 ADR。**

---

## 0. 模块总览与工期

| 模块 | 内容 | 工期 | 前置依赖 | 何时需要 |
|---|---|---|---|---|
| M1 存储完整版 | 迁移链对齐、event log 完整语义、kv、workspace | 2 周 | 无(S4 已备) | 多工作区、审计、多实例并发 |
| M2 文件系统与 shell | watcher、shell 完整解析、安全扫描全集、git 完整版 | 2 周 | 无 | 长驻会话、文件变更感知、复杂 git 流 |
| M3 插件系统 | host + hooks + 内置插件集 | 2 周 | 无(S3 已备) | 需要生态扩展、定制工具 |
| M4 codemode | 受限执行模式 | 1 周 | M3(hooks) | 需要 Code Mode |
| M5 client SDK + server | 事件流 SDK、FastAPI+SSE、OpenAPI 规范 | 2 周 | 无(S2 已备) | 程序化接入、外部 UI |
| M6 长尾能力 | image/voice/websearch/worktree/快照/多 provider | 2 周 | M2(worktree 部分依赖 git 完整版) | 对应功能诉求出现时 |
| M7 测试对齐与硬化 | 核心路径测试移植、性能、Windows conpty | 2-3 周 | 其余模块完成后做才有意义 | 对外/生产前 |

**组合参考**:只要内部用 → M5;要给平台当 agent 引擎 → M1+M5;要对齐 opencode 全体验 → 全选(合计 ≈ 3.5-4 个月)。

## 1. 依赖关系

```
Phase1(接缝,已具备)──┬─→ M1 存储完整版
                      ├─→ M2 文件系统与 shell ──→ M6(仅 worktree/快照 需要 M2 的 git 完整版)
                      ├─→ M3 插件系统 ──→ M4 codemode
                      └─→ M5 client SDK + server
M7 硬化 = 做完所选模块之后
```

无依赖的模块(M1/M2/M3/M5)顺序任意、可并行推进;M6 的 image/voice/websearch 不依赖 M2,可先行。

## 2. 模块卡片

### M1 存储完整版(2 周)

| 项 | 内容 |
|---|---|
| 目标 | 把 S4 从"最小四表"升级为 TS 最终态等价 schema |
| 任务 | ① schema 对齐 TS 最终态(不复制 58 个历史迁移,Alembic 直接建终态 + 数据迁移脚本);② event log 完整语义:`claim/owner`、`readAfter`、`follow` 流(TS `bus.ts`);③ kv 表;④ workspace/location 表与多实例;⑤ 权限 saved 表完善 |
| TS 参照 | `core/src/database/`(schema、drizzle 定义)、`bus.ts` durable 部分、`kv/` |
| 验收 | 事件流 `follow` 模式可用;双实例并发读写同一 session 不冲突 |

### M2 文件系统与 shell 完整版(2 周)

| 项 | 内容 |
|---|---|
| 目标 | 对齐 TS 的文件感知与 shell 安全能力 |
| 任务 | ① file watcher:`watchfiles` 替代 `@parcel/watcher`(S5 loader 热重载接入);② shell 解析完整版:tree-sitter bash/powershell(Python 绑定)替代 WASM;③ 安全扫描规则全集(命令展开/注入检测);④ `git.ts` 完整版(worktree 操作等);⑤ PTY:`pexpect` + Windows conpty 打样(独立 spike,先验证再合入) |
| TS 参照 | `core/src/filesystem/`、`shell/`、`git.ts`、`persistent-pty/` |
| 验收 | 配置热重载生效;危险命令识别率对齐 TS 测试用例;Windows/Unix PTY 各一组端到端测试 |

### M3 插件系统全套(2 周)

| 项 | 内容 |
|---|---|
| 目标 | 对齐 TS 的插件能力:hooks 生命周期 + 内置插件 |
| 任务 | ① plugin host(加载/沙箱化第三方 Python 包);② hooks:`before_execute` 之外补 `system_prompt`、`tool_result` 等对齐 TS hook 清单;③ 内置 plugins 移植(择常用);④ 插件配置接入 S5 loader 管道 |
| TS 参照 | `core/src/plugin/`(82 文件)、`packages/plugin`、`packages/plugins` |
| 验收 | 一个第三方包通过 entry_points 挂入:注册工具(S3)+ 修改系统提示(S5)生效 |

### M4 codemode(1 周)

| 项 | 内容 |
|---|---|
| 目标 | Code Mode(受限代码执行)移植 |
| 任务 | catalog、受限 tool 集、instructions 生成;执行仍走 S3/S6 |
| TS 参照 | `packages/codemode`(10k 行)、`core/src/codemode/` |
| 验收 | codemode 会话中模型只能调用受限工具集 |

### M5 client SDK + server(2 周)

| 项 | 内容 |
|---|---|
| 目标 | headless 程序化访问:事件流 SDK + HTTP API |
| 任务 | ① client SDK:包装 S2 bus 事件 → 公开流式 API(类型来自 S1);② FastAPI + SSE:`session.create/prompt/event` 等端点,对齐 TS server 路由;③ OpenAPI 规范精简版(不做完整 merman,只对齐 client 用到的路由) |
| TS 参照 | `packages/server`(4.5k)、`packages/client`(15.4k)、`packages/protocol` |
| 验收 | Python SDK 五行代码发起会话并消费事件流;与 TS server 的路由命名对照表可追溯 |

### M6 长尾能力(2 周,内部可再拆)

| 项 | 内容 |
|---|---|
| 目标 | 按需补齐可选能力,互不依赖 |
| 子项 | image(Pillow 管道替代 photon-wasm)→ 1 周;websearch(先接一家 API)→ 2 天;voice → 2 天;worktree/快照(依赖 M2 git)→ 2 天;多 provider 路由(litellm 兜底或第二 provider)→ 2 天 |
| TS 参照 | `core/src/image/`、`websearch.ts`、`voice.ts`、`worktree/`、`snapshot.ts` |
| 验收 | 每子项独立验收,可单独合并 |

### M7 测试对齐与硬化(2-3 周)

| 项 | 内容 |
|---|---|
| 目标 | 质量对齐 TS 版核心路径 |
| 任务 | ① 移植 TS 核心路径测试的 ~1/3(session/permission/tool 循环优先,18.8 万行中约 6 万行对应);② 并发压测(多 session 并发、事件序不乱);③ 性能(启动 <2s、流式延迟对齐);④ fuzz(tool input 边界、超大输出) |
| TS 参照 | `packages/core/test`、`packages/ai/test` |
| 验收 | 核心路径用例与 TS 行为一致(差异记录 `doc/parity.md`) |

## 3. "按需补充"的保障机制

1. **接缝只增不改**:接口加方法 OK,改签名必须 ADR + 兼容层(`doc/adr/NNN-*.md`);
2. **事件名兼容**:S2 事件类型是公开契约,新增事件不改动旧事件语义;
3. **模块自包含**:每个模块改动都在自己的目录 + 接缝的挂载点,不横向侵入;
4. **每模块结束打 tag**:版本可回退,后续模块基于稳定点开发。

## 4. 推进节奏建议

- 每 2 周一个模块,结束时**验收 + 打 tag**;
- 顺序参考:内部自用 → **M5** → M3 → M2 → M6;平台化 → **M1** → M5 → M7;
- 全部完成约 3.5-4 个月(与一次性重写持平,但全程每个点都可用)。
