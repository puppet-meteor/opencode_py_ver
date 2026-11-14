"""Unit tests for the real tool node body (P5-S5, T-C1-089).

Real child processes throughout (``sys.executable`` argv -- the
runner/workers precedent: cross-platform, no shell, no PATH games). The
§5.3 primitive underneath is pinned by its own contract tests, so these
pin the body's faces: the success envelope (exactly five keys, the
truncation flags never silent), the fail-closed arms (a non-zero exit
carrying the code and the stderr tail, the deadline with the tree-kill
face, the spawn failure, the empty argv), the 64 KiB cap flagging the
envelope, the honest ``tool_ref`` refusal with its full pinned message,
the tool_ref-first precedence (an attempt-shaped node carrying both faces
refuses, never re-runs the primary argv), and the params pin: neither the
submitted params nor the ``node_params`` view ever substitute into the
static argv.
"""

from __future__ import annotations

import sys
from typing import cast

import pytest

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import JsonObject
from opencode.contracts.pipelines import PipelineNode
from opencode.orchestrator import tool
from opencode.orchestrator.engine import EngineState
from opencode.orchestrator.tool import TOOL_REF_UNSUPPORTED_MESSAGE, run_command_tool_body
from opencode.orchestrator.workers import WorkerError


def _node(command: list[str] | None = None, *, tool_ref: str | None = None) -> PipelineNode:
    return PipelineNode(node_id="collect", node_type="tool", command=command, tool_ref=tool_ref)


def _state(params: JsonObject | None = None, node_params: JsonObject | None = None) -> EngineState:
    """The body-state face the engine hands the body (the same ``node_params``
    ephemeral view the engine itself builds at node entry, cast and all)."""
    view: dict[str, object] = dict(EngineState(params=params or {}))
    if node_params is not None:
        view["node_params"] = node_params
    return cast("EngineState", view)


class TestCommandArm:
    async def test_a_successful_command_returns_the_pinned_envelope(self) -> None:
        result = await run_command_tool_body(
            _node([sys.executable, "-c", "import sys; sys.stdout.write('tool-ok')"]), _state()
        )
        assert result == {
            "stdout": "tool-ok",
            "stderr": "",
            "exit_code": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }

    async def test_stderr_is_captured_onto_the_envelope(self) -> None:
        result = await run_command_tool_body(
            _node([sys.executable, "-c", "import sys; sys.stderr.write('noise')"]), _state()
        )
        assert result == {
            "stdout": "",
            "stderr": "noise",
            "exit_code": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }

    async def test_a_non_zero_exit_is_tool_failed_with_the_code_and_the_stderr_tail(self) -> None:
        with pytest.raises(WorkerError, match="exited with code 3") as error:
            await run_command_tool_body(
                _node([sys.executable, "-c", "import sys; sys.stderr.write('kaputt'); sys.exit(3)"]), _state()
            )
        assert error.value.code is CoreErrorCode.TOOL_FAILED
        assert "kaputt" in str(error.value)

    async def test_a_command_past_the_deadline_is_tool_timeout_with_the_tree_kill_face(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert tool.TOOL_NODE_TIMEOUT_S == 120.0  # the pinned production deadline
        monkeypatch.setattr(tool, "TOOL_NODE_TIMEOUT_S", 0.3)
        with pytest.raises(WorkerError, match="process tree killed") as error:
            await run_command_tool_body(_node([sys.executable, "-c", "import time; time.sleep(30)"]), _state())
        assert error.value.code is CoreErrorCode.TOOL_TIMEOUT
        assert "tool node 'collect'" in str(error.value)
        assert "0.3s" in str(error.value)

    async def test_an_unspawnable_argv_is_tool_failed_on_the_spawn_face(self) -> None:
        with pytest.raises(WorkerError, match="command spawn failed") as error:
            await run_command_tool_body(_node(["definitely-not-an-executable-opencode-e2e"]), _state())
        assert error.value.code is CoreErrorCode.TOOL_FAILED

    async def test_an_empty_argv_fails_closed_without_spawning(self) -> None:
        with pytest.raises(WorkerError, match="command argv is empty") as error:
            await run_command_tool_body(_node([]), _state())
        assert error.value.code is CoreErrorCode.TOOL_FAILED

    async def test_output_past_the_cap_flags_the_envelope_and_keeps_the_prefix(self) -> None:
        result = await run_command_tool_body(_node([sys.executable, "-c", "print('x' * 70_000)"]), _state())
        assert result == {
            "stdout": "x" * 65_536,
            "stderr": "",
            "exit_code": 0,
            "stdout_truncated": True,
            "stderr_truncated": False,
        }

    async def test_node_params_never_substitute_into_the_static_argv(self) -> None:
        state = _state(params={"submitted": "param-value"}, node_params={"argv0": "INJECTED-VALUE"})
        result = await run_command_tool_body(
            _node([sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", "{argv0} {submitted}"]), state
        )
        assert result == {
            "stdout": "{argv0} {submitted}",
            "stderr": "",
            "exit_code": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
        }


class TestToolRefArm:
    async def test_the_tool_ref_arm_refuses_with_the_unsupported_capability_face(self) -> None:
        with pytest.raises(WorkerError, match="tool-registry slice") as error:
            await run_command_tool_body(_node(tool_ref="fs.read_text"), _state())
        assert error.value.code is CoreErrorCode.UNSUPPORTED_CAPABILITY
        assert str(error.value) == TOOL_REF_UNSUPPORTED_MESSAGE.format(node_id="collect")
        assert "node 'collect'" in str(error.value)

    async def test_a_node_with_both_faces_refuses_tool_ref_first_never_running_the_argv(self) -> None:
        argv = [sys.executable, "-c", "print('must not run')"]
        with pytest.raises(WorkerError, match="tool-registry slice") as error:
            await run_command_tool_body(_node(argv, tool_ref="fs.read_text"), _state())
        assert error.value.code is CoreErrorCode.UNSUPPORTED_CAPABILITY
        assert str(error.value) == TOOL_REF_UNSUPPORTED_MESSAGE.format(node_id="collect")
