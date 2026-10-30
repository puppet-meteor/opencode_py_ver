# Phase 1:带接缝的瘦身版(5 周)

> 目标:交付**可日常使用**的 Python agent CLI,同时埋好 6 个接缝。
> 详细路线见 `ROADMAP.md`;本文是执行计划。

---

## 1. 交付目标(Done 的定义)

- [ ] `pip install -e .` 后,`opencode` 命令进入 REPL,与 MAAS 模型多轮对话
- [ ] agent 循环可调用工具:bash / read / write / edit / glob / grep / ls
- [ ] 权限门:危险 bash 命令触发 ask/deny,规则可配置
- [ ] 配置发现:`opencode.json`(全局+项目)+ `AGENTS.md` 注入系统提示
- [ ] MCP:接入一个 stdio MCP server,其工具经 S3 registry 可被模型调用
- [ ] 持久化:进程重启后 `--continue` 恢复会话(事件落库 + 投影恢复)
- [ ] 6 个接缝接口冻结并有契约测试

**明确不做**(留给 Phase 2):完整迁移链、file watcher、插件系统、codemode、client SDK、server、image/voice/websearch、worktree/快照、多 provider、compaction 高级形态(只做简化版)。

## 2. 接缝的接口定义(冻结前评审这两节!)

### S1 消息 schema —— `opencode/ai/types.py`

```python
from typing import Any, Literal
from pydantic import BaseModel

Role = Literal["user", "assistant", "system"]

class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str

class ToolCallPart(BaseModel):
    type: Literal["toolCall"] = "toolCall"
    tool: str
    toolCallID: str            # 对齐 TS:toolCallID 命名
    input: dict[str, Any]

class ToolResultPart(BaseModel):
    type: Literal["toolResult"] = "toolResult"
    tool: str
    toolCallID: str
    output: str                # M5(server/client)将复用此模型,不引 openai 类型

class Message(BaseModel):
    id: str
    role: Role
    parts: list[TextPart | ToolCallPart | ToolResultPart]
    modelID: str | None = None
    cost: Cost | None = None   # usage/成本结构,对齐 TS session/message.ts

class GenerationChunk(BaseModel):        # provider → runner 的流式事件
    type: Literal["text-delta", "tool-call-start", "tool-call-delta", "tool-call-end", "usage", "done"]

class Provider(Protocol):                # MAAS 是第一个实现,未来 provider 都走它
    async def stream(self, messages: list[Message], tools: list[ToolInfo]) -> AsyncIterator[GenerationChunk]: ...
```

**评审要点**:part 的 `type` 字面量命名、`toolCallID` 驼峰、事件 chunk 类型集合——冻结后 client/server/codemode 全依赖它。

### S2 事件总线 —— `opencode/bus/bus.py`

```python
class Event(BaseModel):                  # 事件类型定义在 events.py,名称逐条对齐 TS session 事件名
    type: str                            # 如 "message.updated" / "session.idle"
    sessionID: str | None = None
    seq: int                             # 单调递增,持久化用

class Bus(Protocol):
    async def publish(self, event: Event) -> None               # 先落库(S6)再分发
    def subscribe(self, *types: str) -> AsyncIterator[Event]    # M5 client SDK 直接复用
    def project(self, event_type: str, fn: Projector) -> None   # 投影到读模型
```

实现约束:**publish = append events 表 → 通知订阅者**;顺序保证按 `seq`。TS 参照:`core/src/bus.ts`(durable event / replay / claim 语义在 Phase 2 M1 补全,但今天的 publish 路径必须已经是"先落库再分发")。

### S3 工具注册中心 —— `opencode/tools/registry.py`

```python
class ToolContext(BaseModel):
    session_id: str
    working_dir: str
    permission: "PermissionPolicy"       # 注入,工具不自行判断权限

class ToolResult(BaseModel):
    output: str
    metadata: dict[str, Any] = {}

class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]           # JSON Schema(pydantic model_json_schema())

class Tool(Protocol):
    info: ToolInfo
    async def execute(self, ctx: ToolContext, input: dict[str, Any]) -> ToolResult: ...

class ToolRegistry:
    def register(self, tool: Tool) -> None                  # MCP 工具、插件工具、codemode 工具同入口
    def before_execute(self, hook: PermissionHook) -> None  # 权限钩子管道(M3 插件 hooks 也挂这里)
```

### S4 存储仓库 —— `opencode/storage/`

```python
class SessionRepo(Protocol):
    async def create(self, s: SessionRow) -> None
    async def append_events(self, session_id: str, events: list[Event]) -> None
    async def read_events(self, session_id: str, after: int = 0) -> list[Event]
    # messages/sessions 读模型由 projector 维护,repo 不直接写

class Database:
    engine: AsyncEngine                  # SQLAlchemy;alembic 从第一天管理迁移
```

初始表(alembic revision 0001):`sessions`、`events`、`messages`(读模型)、`permissions`(saved 决定)。

### S5 配置发现管道 —— `opencode/config/loader.py`

```python
class ConfigLayer(BaseModel): ...        # 每个来源一个 loader

class ConfigLoader:
    loaders: list[ConfigLoaderProtocol]  # [global_json, project_json, agents_md, skills_dir]
    def load(self, root: Path) -> Config # 按序合并;新增来源=插入 loader,不改现有

class Config(BaseModel):
    model: str | None                    # MAAS 模型名
    mcp_servers: dict[str, McpServerConfig]
    permission: PermissionRules
    instructions: list[str]              # AGENTS.md / skills 注入
```

### S6 会话事件溯源形态 —— `opencode/session/store.py`

```python
class SessionStore:
    async def append(self, ev: Event) -> None      # 唯一写入口:events 表 → bus → projector
    async def replay(self, session_id: str) -> SessionState   # 重启恢复 = 重放事件
```

**约束**:runner 的一切状态变更(消息追加/更新/工具结果)都必须先构造 Event 走 `SessionStore.append`,禁止直接 UPDATE 读模型。这是 revert/审计/compaction 的前提。

## 3. 周计划

### W1 骨架 + 最小 agent 循环(先打通端到端)

| 任务 | 说明 | TS 参照 |
|---|---|---|
| 脚手架 | pyproject、ruff、mypy、pytest、`src/` 布局 | — |
| **MAAS 打样**(第 1 天) | 真实端点跑通:流式文本 + 流式 tool call,记录兼容性偏差 | `ai/src/providers/*`(仅看协议) |
| S1 `ai/types.py` | 消息 schema + Provider 协议 + `maas.py` | `session/message.ts` |
| S6 雏形 + runner | agent 循环:流式生成 → 工具调用 → 结果回填 → 循环;最大轮数/中断 | `session/runner/` |
| 工具 3 个 | bash(subprocess+超时)、read、write | `tool/` |
| 极简 REPL | `python -m opencode` | — |

**验收**:终端多轮对话;模型要求跑 `bash` 命令并拿到结果;Ctrl-C 中断安全。

### W2 工具全集 + 权限 + 配置

| 任务 | 说明 | TS 参照 |
|---|---|---|
| 工具补齐 | edit(精确替换语义)、glob、grep(调 rg)、ls | `tool/` |
| S3 完整 | registry + before_execute 权限管道 | `tool.ts` |
| 权限规则 | allow/deny/ask 规则集 + bash 命令扫描(危险命令清单简化版)+ saved 决定入库 | `permission.ts`、`shell/scan.ts` |
| S5 配置 | opencode.json 双层 + AGENTS.md 注入 | `config/discovery.ts`、`config/markdown.ts` |

**验收**:危险命令被拦截询问;AGENTS.md 内容出现在系统提示。

### W3 事件总线 + 持久化 + 恢复

| 任务 | 说明 | TS 参照 |
|---|---|---|
| S2 Bus | publish(先落库)/subscribe/project | `bus.ts` |
| S4 alembic | revision 0001 四表 + repo 层 | `database/schema.sql.ts`(只看最终态) |
| S6 Store | 事件追加唯一入口 + projector 维护 messages 读模型 | `session/event.ts`、`session/projector.ts` |
| 恢复 | `--continue`/`--session` 重放事件恢复上下文 | `session/history.ts` |
| 简化 compaction | 超阈值折叠历史(单条 summary 事件) | `session/compaction.ts` |

**验收**:杀进程重启后会话完整恢复;订阅 bus 能收到全事件流。

### W4 MCP + skills + 子 agent

| 任务 | 说明 | TS 参照 |
|---|---|---|
| MCP 薄壳 | 官方 SDK;stdio + http;工具桥接进 S3 registry | `core/src/mcp/client.ts` |
| skills | SKILL.md 发现 + 触发提示 | `skill/discovery.ts` |
| 子 agent(简化) | spawn 独立 session,后台任务 + 完成回报 | `session/subagent-*` |

**验收**:接一个真实 MCP server(如 filesystem),其工具可被模型调用;skill 可触发。

### W5 打磨 + 冻结 + 缓冲

| 任务 | 说明 |
|---|---|
| 契约测试 | 6 接缝各一组接口契约测试(带 mock MAAS) |
| 边界 | 流式中断、工具异常、MAAS 429/超时重试、超长输出截断 |
| 文档 | README + 接缝接口文档(冻结版) |
| 缓冲 | 吸收 W1-W4 滑期(预计滑 20%) |

## 4. 测试策略

- **单元**:每个接缝的契约测试(pytest-asyncio);
- **集成**:fake OpenAI 兼容 server(mock 流式 + tool call)驱动 runner 全链路;
- **对齐**:与 TS 版行为差异记录到 `doc/parity.md`(已知简化项写明,避免误判 bug)。

## 5. 风险与应对

| 风险 | 应对 |
|---|---|
| MAAS tool call 流式不兼容 | W1 D1 打样;偏差集中在 `maas.py` 适配层,不污染 S1 |
| edit 工具的精确替换语义 | 直接复刻 TS 的 oldString/newString 协议,契约测试对齐 |
| 事件名对齐工作量大 | W3 先抄 TS `session/event.ts` 的事件名清单,再实现 |
| 5 周滑期 | W5 全部为缓冲;接缝冻结评审最迟 W5 结束完成 |
