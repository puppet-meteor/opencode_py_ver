"""The real ``tool`` node body over the process primitives (P5-S5, T-C1-089).

The S2 engine left ``tool`` an injection seam (``ToolFn``) that only tests
ever filled; until this slice the production assembly wired the honest
fail-closed refusal instead (``unsupported_tool_body``, removed from
``llm_call.py`` with this change -- its face lives on here, turned real
for one arm). This module is the production body now:

* **The ``command`` arm is real**: the node's explicit argv runs through
  :func:`opencode.process.runner.run_command` -- the §5.3 process
  primitive. The argv passes exactly as the wire declared it (never
  ``shell=True``, never template interpolation, never ``node_params``
  substitution), the run rides ``asyncio.to_thread`` (the workers
  precedent -- a long command never blocks the event loop), the strong
  wall-clock deadline is the module constant, the output is capped at the
  runner's 64 KiB budget with explicit truncation flags, and the process
  tree is killed on every exit path.
* **The ``tool_ref`` arm stays honest fail-closed**: a static tool-registry
  reference refuses explicitly with ``UnsupportedCapability`` -- the
  registry face lands with a later slice, and refusing beats a silent
  no-op. The check runs *before* the command arm: the engine's fallback
  attempts are ``tool_ref``-shaped nodes, so a command-first read would
  silently re-run the primary argv instead of refusing.

The success value is the pinned envelope (a JSON object in
``node_results``): the captured stdout/stderr, the exit code and the two
truncation flags -- a capped run is never silent about having been capped.
Failures map onto the workers §8 precedent: a timeout is ``ToolTimeout``
(naming the node, the deadline and the tree kill), a non-zero exit is
``ToolFailed`` with the exit code and the stderr tail, a failed spawn is
``ToolFailed``, and an empty argv fails closed without spawning anything.
"""

from __future__ import annotations

import asyncio

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import JsonObject, JsonValue
from opencode.contracts.pipelines import PipelineNode
from opencode.orchestrator.engine import EngineState
from opencode.orchestrator.workers import WorkerError
from opencode.process.runner import DEFAULT_MAX_OUTPUT_BYTES, run_command

#: One ``tool`` node command's wall-clock budget. An independent constant
#: (the 120s same-value precedent: the workers budget line, git's default,
#: the llm_call request budget); the body reads it at call time so tests
#: can monkeypatch a short deadline into the timeout arm.
TOOL_NODE_TIMEOUT_S = 120.0

#: The honest ``tool_ref`` arm refusal: the static tool-registry face is not
#: wired in this composition (it lands with the tool-registry slice).
TOOL_REF_UNSUPPORTED_MESSAGE = (
    "the static tool-registry face ('tool_ref') is not wired in this composition: node '{node_id}' refuses to "
    "execute (the run_command-backed command arm is real from this slice; the tool_ref arm lands with the "
    "tool-registry slice; honest fail-closed, never a silent no-op)"
)


async def run_command_tool_body(node: PipelineNode, state: EngineState) -> JsonValue:
    """The real ``tool`` node body (a ``ToolFn`` face).

    ``tool_ref`` first (the module docstring names the precedence), then the
    explicit-argv ``command`` arm through the §5.3 primitive. The command arm
    ignores ``state`` entirely: the argv is the wire's static literal, and
    ``node_params`` never substitute into it (the parameter stays because
    the ``ToolFn`` seam passes it -- the registry arm, when it lands, will
    read it).
    """
    if node.tool_ref is not None:
        raise WorkerError(
            TOOL_REF_UNSUPPORTED_MESSAGE.format(node_id=node.node_id),
            code=CoreErrorCode.UNSUPPORTED_CAPABILITY,
        )
    argv = list(node.command) if node.command is not None else []
    if not argv:
        # The registration gate rejects this shape up front, but the body
        # still fails closed on it: an empty argv never reaches Popen.
        raise WorkerError("command argv is empty", code=CoreErrorCode.TOOL_FAILED)
    try:
        run = await asyncio.to_thread(
            run_command,
            argv,
            cwd=None,
            env=None,
            timeout_s=TOOL_NODE_TIMEOUT_S,
            max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
        )
    except OSError as error:
        # The spawn face: the argv names something this host cannot execute.
        raise WorkerError(f"command spawn failed: {error}", code=CoreErrorCode.TOOL_FAILED) from error
    if run.timed_out:
        raise WorkerError(
            f"tool node '{node.node_id}' exceeded the wall-clock deadline ({TOOL_NODE_TIMEOUT_S:g}s); "
            "process tree killed",
            code=CoreErrorCode.TOOL_TIMEOUT,
        )
    if run.returncode != 0:
        raise WorkerError(
            f"command exited with code {run.returncode}: {run.stderr.strip()[-512:]}",
            code=CoreErrorCode.TOOL_FAILED,
        )
    envelope: JsonObject = {
        "stdout": run.stdout,
        "stderr": run.stderr,
        "exit_code": run.returncode,
        "stdout_truncated": run.stdout_truncated,
        "stderr_truncated": run.stderr_truncated,
    }
    return envelope
