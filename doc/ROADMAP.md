# opencode Python 移植路线图(TS → Python)

> 源项目:`D:\26_code\opencode_py_ver\opencode-2.2.2`(TypeScript/Bun)
> 目标项目:`opencode-2.2.2-py`(Python 3.12+)
> 文档版本:2026-10-30

---

## 1. 背景与定位

| 项 | 决策 |
|---|---|
| 移植范围 | packages/ 中非 UI 的 agent 框架核心(约 18 万行 TS 源码) |
| 放弃 | TUI / Web / 桌面 / console / gui-plugins 等全部 UI(约 45 万行) |
| 模型接入 | **MAAS 直连**(OpenAI 兼容 API,`openai` SDK),不复刻 109 个 provider |
| MCP | 官方 `mcp` Python SDK,薄壳适配 |
| 持久化 | SQLite + SQLAlchemy 2.0 (async) + Alembic |
| 界面 | 极简 REPL CLI;headless 优先,程序化访问走 Phase 2 的 server 模块 |

## 2. 核心原则:演进式,不是一次性

1. **骨架不偷工**:6 个接缝(见 §3)在 Phase 1 就按全功能形态落地;
2. **加文件、不改文件**:Phase 2 所有功能以新模块进入,不返工接缝;
3. **每阶段可独立交付、可停**:Phase 2 各模块按需选装,不做不损失已有能力;
4. **事件名/行为对齐 TS 版**:同名事件、同形消息,降低与 TS 版对照调试的成本。

## 3. 六个接缝(架构红线)

这 6 处是全功能版真正的地基。Phase 1 可以少功能,但这 6 处的**形状**必须与最终形态一致。

| # | 接缝 | Python 落点 | TS 参照 | Phase 2 依赖它的人 |
|---|---|---|---|---|
| S1 | 内部消息 schema(不外泄 openai 类型) | `opencode/ai/types.py` | `packages/ai` + `core/src/session/message.ts` | client SDK、server、codemode、多 provider 路由 |
| S2 | 事件总线(publish/subscribe/project) | `opencode/bus/bus.py` | `core/src/bus.ts`(930 行,含事件持久化) | client 事件流、server SSE、恢复/revert/compaction |
| S3 | 工具注册中心 + 权限钩子 | `opencode/tools/registry.py` | `core/src/tool.ts`(366 行) | 插件工具、MCP 工具、codemode 工具 |
| S4 | 存储仓库 + 迁移链 | `opencode/storage/` | `core/src/database/`(88 文件,58 迁移) | event log、kv、workspace、权限持久化 |
| S5 | 配置发现 loader 管道 | `opencode/config/loader.py` | `core/src/config/`(28 文件) | skills、插件配置、AGENTS.md、指令集 |
| S6 | 事件先落库再投影(事件溯源形态) | `opencode/session/store.py` | `core/src/session/event.ts` + `projector.ts` | 会话恢复、revert、审计、compaction |

**红线**:接缝接口与事件名一经发布不得随意变更;确需变更必须走 ADR(doc/adr/)并做兼容层。

## 4. 总时间线(AI 辅助开发,全职)

```
Phase 1  带接缝瘦身版          5 周   ── 可日常使用(见 PHASE1-SLIM.md)
Phase 2  按需补齐模块(7 个)   选配   ── 每个模块独立可停(见 PHASE2-MODULES.md)
  M1 存储完整版                 2 周
  M2 文件系统与 shell 完整版     2 周
  M3 插件系统全套               2 周
  M4 codemode                  1 周
  M5 client SDK + server       2 周
  M6 长尾能力(image 等)       2 周
  M7 测试对齐与硬化            2-3 周
─────────────────────────────────────
全选 ≈ 3.5-4 个月;只选 M5 ≈ 7 周
```

## 5. 按需补充逻辑(回答:能否以后再加?)

**能。** 前提是守住两条:

1. Phase 1 的 6 个接缝**只增不改**(接口可以加方法,不能改签名/删语义);
2. Phase 2 各模块按依赖顺序补(见 PHASE2-MODULES.md §依赖图),无依赖的模块可任意单选。

不能"按需"的情况只有一种:**想改消息 schema(S1)或事件模型(S2)本身**。因此 Phase 1 的这两个接口需要认真评审后再冻结。

## 6. 技术栈

| 用途 | 选型 |
|---|---|
| 运行时 | Python 3.12+(asyncio) |
| 模型 | `openai` SDK(base_url → MAAS 端点);兜底/路由再考虑 `litellm`(Phase 2 M6) |
| MCP | 官方 `mcp` Python SDK(stdio + streamable-http) |
| 数据 | SQLAlchemy 2.0 async + aiosqlite + Alembic |
| 模型/校验 | pydantic v2 |
| 终端输出 | rich |
| Shell | `asyncio.create_subprocess_shell` 起步;完整 PTY 在 Phase 2 M2(Windows conpty 需打样) |
| 质量 | pytest + pytest-asyncio + ruff + mypy |

## 7. 包结构规划

```
opencode-2.2.2-py/
  pyproject.toml
  src/opencode/
    ai/                 # S1: types.py(消息 schema)、provider 协议、maas.py
    bus/                # S2: bus.py、events.py(事件类型,对齐 TS 事件名)
    tools/              # S3: registry.py、permission.py、builtin/(bash/read/write/edit/glob/grep/...)
    session/            # S6: runner.py(agent 循环)、store.py、projector.py
    storage/            # S4: db.py、repo/、migrations/(alembic)
    config/             # S5: loader.py、markdown.py(AGENTS.md/skills)
    mcp/                # MCP 薄壳:manager.py、tool_bridge.py(注册进 S3)
    cli.py              # 极简 REPL 入口
  tests/
  doc/
    ROADMAP.md          # 本文件
    PHASE1-SLIM.md      # Phase 1 计划
    PHASE2-MODULES.md   # Phase 2 模块卡片
    adr/                # 接缝变更记录(未来)
```

## 8. 主要风险

| 风险 | 缓解 |
|---|---|
| MAAS 的 OpenAI 兼容度(流式 tool call 偏差) | Phase 1 W1 第一天先打样真实端点,不对齐处集中在 `maas.py` 适配 |
| 流式 tool call 边界(部分流/JSON 修复) | W1-W2 用 MAAS mock(fake server)做契约测试 |
| Windows PTY | 瘦身版用 subprocess+超时;conpty 打样放 Phase 2 M2 之前单独做 |
| Effect → asyncio 语义转换错误 | 不逐行翻译 Effect 链,按行为重写;并发点全部配 asyncio 测试 |
| 58 个 TS 迁移 → Alembic | 不复制历史,Python 侧从空库直接建"最终态",仅对齐 TS 最终 schema |
