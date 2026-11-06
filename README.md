# opencode-py

Phase 1 核心闭环：一个 stdlib-only 的 CLI host，经 MAAS provider 驱动**单发 prompt 或交互式 REPL**。存储为 SQLite（WAL，每数据目录单 writer 进程），事件溯源 + 投影读模型，迁移由 Alembic 承载。

> 本文=运行说明（安装/运行/退出码/缺件提示/同进程示例）。**发布检查清单与备份/恢复操作说明**见 [doc/release-and-backup.md](doc/release-and-backup.md)。

## 安装

### 前置要求

- **Python ≥ 3.12**（`pyproject.toml` `requires-python = ">=3.12"`）。
- **ripgrep（rg）**：`fs__grep` 工具的外部前置。缺失时 grep 工具返回 TOOL_FAILED 明确诊断（无静默回退搜索），提示原文：

  ```text
  ripgrep (rg) is an external dependency of the fs__grep tool, but it was not found on PATH. Install a working ripgrep (https://github.com/BurntSushi/ripgrep) and retry; no silent fallback search is performed.
  ```

  安装指引：<https://github.com/BurntSushi/ripgrep>。
- **shell 解释器**：Unix 为 bash，Windows 为 PowerShell（pwsh 或 powershell）。缺失时 bash 工具同样 TOOL_FAILED 明确诊断、无静默回退（原文见[下文](#缺件提示)）。

### 源检出安装（当前支持形态）

```console
uv sync
```

装出 `.venv` 后两种入口同源等价（`opencode.cli:main`，`pyproject.toml` `[project.scripts]`）：

```console
uv run opencode …        # console script
uv run python -m opencode …
```

MAAS 凭据经旗标或环境变量供给：`--base-url` / `OPENCODE_PY_BASE_URL`、`--api-key` / `OPENCODE_PY_API_KEY`。

**已知限制（wheel 安装形态）**：构建目标是 hatchling 单包声明（只打包 `src/opencode`），wheel 不含 `alembic.ini` 与迁移目录。CLI 启动时以子进程运行 `python -m alembic upgrade head`，其 `script_location` 相对 `alembic.ini`、从 cwd 向上发现（`src/opencode/cli.py` `_alembic_root`）——因此**需在源检出目录内运行**：检出外启动安装体=迁移子进程失败=退出码 70，stderr 双行诊断（`database migration failed …` + alembic `FAILED: No 'script_location' key found in configuration.`），零 db 落盘。

（CI/测试环境使用的 `UV_PROJECT_ENVIRONMENT` + `uv sync --offline --frozen` 是零外网测试专用形，与用户常规安装无关。）

## 运行

### REPL（无参进入）

```console
uv run opencode
```

进入 REPL（提示符 `opencode> `），`/exit` 或 EOF 退出，退出码 0。REPL 命令面：`/exit`、`/history`、`/approve <id> [always]`、`/reject <id>`。

### 单发（single-shot）：现行 positional 形态

多词以空格连接，省略则进入 REPL：

```console
uv run opencode say hello
```

> 记档（OP-R5-6 保守裁决）：Phase 1 计划文本（PHASE1-SLIM §1 L9）中的字面形态 `opencode run --prompt "..."` **不在现实现中**——无 `run` 子命令、无 `--prompt` 旗标。语义面（单发/REPL 双模+退出码三分）均在：单发即上面的 positional 形态。

### 会话选择

- `--continue`：恢复当前工作目录下最近创建的会话；无匹配则新建。
- `--session ID`：恢复指定会话；未知 id 报 NotFound（一行 stderr 诊断，退出码 4）。
- 两者互斥；同时给出=用法错（退出码 2）。

### 旗标与环境变量

| 旗标 | 环境变量 | 说明 |
|---|---|---|
| `--db PATH` | — | 数据库文件路径（默认 `./opencode.db`） |
| `--base-url URL` | `OPENCODE_PY_BASE_URL` | MAAS base URL（必填之一） |
| `--api-key KEY` | `OPENCODE_PY_API_KEY` | MAAS API key（必填之一） |
| `--config PATH` | — | 显式全局配置文件（默认 `~/.config/opencode-py/opencode.json`） |
| `--project-root PATH` | — | 配置层发现的项目根覆盖（默认最近 VCS 根，否则 cwd） |
| `--model NAME` | — | 模型名（CLI 旗标优先于配置层；两层皆无=用法错） |
| `--context-limit N` | — | 模型上下文窗口 tokens（同上） |
| `--output-reserve N` | — | 输出预留 tokens（未设落配置层，内建默认 4096） |
| `--timeout-s N` | — | 请求总超时秒数（未设落配置层，内建默认 30） |
| `--max-steps N` | — | runner 步数预算（未设落配置层，内建默认 20） |

配置分层（builtin < global < project < CLI）与快照固化见 `doc/CORE-CONTRACTS.md` §6。

### 同进程示例（SessionService）

同进程嵌入不走 CLI，直接组合 `SessionService`（§7 应用命令面；构造与库内集成测试同构，`tests/integration/test_service.py`）。前提：目标库已完成迁移（先运行一次 `opencode`，或 `python -m alembic upgrade head`）：

```python
import asyncio
from pathlib import Path

from opencode.bus import VolatileBus
from opencode.session import SessionProjector, SessionService
from opencode.storage import ReadRepositories, SessionStore, open_database


async def main() -> None:
    async with open_database(Path("opencode.db"), mode="create") as database:
        service = SessionService(
            store=SessionStore(database.engine, projector=SessionProjector()),
            repos=ReadRepositories(database.engine),
            bus=VolatileBus(),
            working_dir=str(Path.cwd()),
        )
        session = await service.create({"schema_version": 1, "permission_rules": []})
        run = await service.prompt(session.session_id, "hello, agent", "req_1")
        page = await service.messages(session.session_id)
        for message in page.items:
            print(message.type, message.id)
        status = await service.run_status(run.run_id)
        print(status.status)
        await service.close()


asyncio.run(main())
```

`prompt` 持久化 `py.prompt.accepted` 并返回 `RunRef`；驱动模型轮次（provider/工具/审批）是 host 的职责——CLI 的 `App` 组合即其参考实现。

## 退出码

全枚举 + 三分（成功/失败/中断）两列并记。**退出码 2 双载两义**（argparse 用法错与 run 中断终态同码）——`src/opencode/cli.py` 文档面原样，不合并语义：

| 退出码 | 三分 | 语义 |
|---|---|---|
| 0 | 成功 | 单发 run 成功终态；REPL `/exit` 或 EOF 正常退出 |
| 1 | 失败 | run 失败终态 |
| 2 | 失败 | argparse 用法错：缺 `--base-url`/`--api-key`（或环境变量）、缺 `--model`/`--context-limit`（或配置层供给）、`--continue` 与 `--session` 同给等 |
| 2 | 中断 | run 中断终态（Ctrl-C 取消意图 / 流 EOF） |
| 3 | 失败 | 取消后清理失败（cancel cleanup failed） |
| 4 | 失败 | 会话选择停：未知 `--session` id、不可读 creation 行、恢复拒绝续跑（v1 快照 / 记录的工作目录消失 / MCP 指纹漂移）——一行 stderr 诊断，非 traceback |
| 70 | 失败 | 迁移失败或任何未捕获异常（traceback + stderr 诊断） |
| 130 | 中断 | 提示符面 SIGINT（REPL 输入面按 Ctrl-C） |

## 缺件提示

三面均明确诊断、无静默回退：

1. **缺 MAAS 配置**（`--base-url`/`--api-key` 与环境变量双缺）——exit 2，提示同时给出旗标名与环境变量名：

   ```text
   opencode: error: --base-url is required (or set OPENCODE_PY_BASE_URL)
   ```

   `--api-key` 同构：`opencode: error: --api-key is required (or set OPENCODE_PY_API_KEY)`。
2. **缺 rg**——grep 工具 TOOL_FAILED（原文见「安装·前置要求」），无静默回退搜索。
3. **缺 shell 解释器**——bash 工具 TOOL_FAILED，无静默回退解释器：

   ```text
   bash__run requires an explicit shell interpreter, but no PowerShell interpreter was found (neither pwsh nor powershell resolved on PATH and passed the availability probe). Install bash (Unix) or PowerShell (Windows) and retry; no silent fallback interpreter is used (CORE-CONTRACTS §5.3 L276).
   ```

## 文档

- [doc/release-and-backup.md](doc/release-and-backup.md) — 发布检查清单（tag 前置门）+ 备份/恢复操作说明
- [doc/parity.md](doc/parity.md) — 与 TypeScript 版的对照记录（只是对照、不是 JSON 字段转换器；Python 的版本 1 不当作 TS 的同名版本）
- [doc/platform-support.md](doc/platform-support.md) — 平台探针与支持面实测记录
- [doc/adr/](doc/adr/) — 架构决策记录
- [doc/reference/r5-fixture-inventory.md](doc/reference/r5-fixture-inventory.md) — 六接缝 fixture 清单（92 件对账）
- [doc/reference/schema/](doc/reference/schema/) — 统一 JSON Schema 工件（模型 `model_json_schema()` 汇总）
- [doc/CORE-CONTRACTS.md](doc/CORE-CONTRACTS.md) / [doc/PHASE1-SLIM.md](doc/PHASE1-SLIM.md) — 契约定义与执行计划
