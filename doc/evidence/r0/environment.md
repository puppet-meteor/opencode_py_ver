# R0-A 环境证据

> 生成时间:2026-10-31(本地 Windows)。本文件只记录可复现的环境事实,不包含任何主机路径以外的敏感信息。凭据类信息一律不落库。

## 1. 工具链

| 项 | 本地实测值 | 说明 |
|---|---|---|
| OS | Windows(开发机) | CI 另覆盖 `ubuntu-latest` / `windows-latest` |
| Python | 3.13.12(系统解释器) | 项目要求 `requires-python >= 3.12`;CI 固定 3.12 |
| uv | 0.13.6(通过 `python -m pip install uv` 安装) | 环境初始无 uv,安装动作已记录,锁文件由该版本生成 |
| git | 分支 `main`,起点 `bdf3c8a` | 工作区起点干净,见下 |

## 2. 依赖与锁

- `uv lock`:解析 **29 个包**(运行依赖 `openai>=1.60`,实际解析为 openai 3.42.0,其 HTTP 栈为 `httpx2 2.16.0`);
- 直接依赖声明:`openai>=1.60` + `httpx2>=2.12.0,<3`(`provider_spike.py` 直接 `import httpx2`,按 openai 3.42 的自身约束 `httpx2<3,>=2.12.0` 声明为直接依赖,不依赖传递关系泄漏);
- `uv sync --locked`:在干净环境重建 `.venv` 成功(pytest 9.2.0 / pytest-asyncio 1.4.1 / ruff 0.17.3 / mypy);
- `uv.lock` 与 `pyproject.toml` 同库提交,CI 用 `uv sync --locked` 保证可复现;
- `.gitignore` 覆盖 `.venv/`、`.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/` 等生成物,仓库内无任何本地环境文件。

## 3. 本地验证命令(全部实际执行过)

| 命令 | 结果 |
|---|---|
| `uv lock --check` | 通过(锁与 pyproject 一致) |
| `uv sync --locked` | 通过 |
| `uv run pytest -q tests/r0` | **24 passed**,连续 10 次全绿(曾存在高偶发失败,根因与修复见 provider.md §5) |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 18 files already formatted |
| `uv run mypy src tools/r0 tests/r0` | Success: no issues in 7 source files |

## 4. 结构落点(R0-A 约束遵守情况)

- 正式包 `src/opencode/` 只有 `__init__.py` 标记 —— 未实现任何未来模块(SessionService/Store/runner/SQLite/工具/权限/MCP/REPL 均未动);
- spike 类型与 adapter 在 `tools/r0/`,fake server 与契约测试在 `tests/r0/`,**未创建** `src/opencode/ai/`;
- 证据文档在 `doc/evidence/r0/`;
- 脱敏 JSON fixtures 在 `doc/evidence/r0/fixtures/`(7 个文件,由 `uv run python -m tools.r0.dump_fixtures` 再生,内容见 provider.md §2);
- CI 在 `.github/workflows/ci.yml`(单 job 矩阵、无 secret、不访问真实 MAAS)。

## 5. 未验证项

- ~~Linux 端实际运行~~ **已验证**:CI run [41177228445](https://github.com/puppet-meteor/opencode_py_ver/actions/runs/41177228445)(commit `edcf891`,2026-10-31)`ubuntu-latest` 与 `windows-latest` 两个 job 均 success,pytest 各 `23 passed`(详见 provider.md §9);
- 真实 MAAS smoke:无凭据,标记“R0-A 外部证据待补”(见 provider.md §8)。
