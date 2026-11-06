"""Minimal JSON-RPC-over-stdio transport for MCP servers (R4-S2; OP-R4-1).

Self-built per the OP-R4-1 user ruling (2026-11-05): the smallest
newline-delimited JSON-RPC 2.0 client that owns the server subprocess --
zero new dependencies (pyproject/uv.lock untouched). The TS reference
(``packages/core/src/mcp/stdio.ts``, read verbatim at the recorded
fingerprint) is a behavioral anchor, not a structural one: the frame
order, the frame cap, the close escalation and the unexpected-exit
diagnostics mirror its observable wire behavior, while the Effect
runtime, the SDK ``ReadBuffer`` and the ``Environment`` spawner are not
ported (r4-slices §4 difference 2 -- Python's equivalent of "the scope
owns the process" is this transport plus the manager's close chain).

Contract anchor: CORE-CONTRACTS §6 L304 -- stdio only, argv direct exec
(never a shell), and the environment is the caller's minimal set passed
verbatim: the TS spawner's ``extendEnv`` host-env merge is deliberately
NOT ported ("环境变量为最小集和显式 secret refs" wins; the caller resolves
secret refs before injecting, config loading is S3's face). The host's
explicit trust is the caller-constructed definition itself.

Process-tree discipline reuses the R3-S5 machinery read-only: Windows
assigns the fresh server to a :class:`~opencode.process.winjob.JobObject`
(``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` backstop; ``runner.py`` itself is
untouched), POSIX starts the server as a new session so ``killpg`` reaps
the whole tree.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opencode.process.winjob import JobObject
elif sys.platform == "win32":
    from opencode.process.winjob import JobObject

#: OP-R4-5 (TS stdio.ts L10 ``CLOSE_GRACE = Duration.seconds(2)``): how
#: long the server may take to exit gracefully after its stdin EOF.
#: Kept at the TS value -- no Python-side evidence to deviate.
CLOSE_GRACE_S = 2.0

#: OP-R4-5 (TS stdio.ts L13 ``FORCE_KILL_AFTER = Duration.seconds(2)``):
#: how long the server may take to die after the terminate signal before
#: the hard kill escalates. Kept at the TS value.
FORCE_KILL_AFTER_S = 2.0

#: OP-R4-5 (TS stdio.ts L15 ``MAX_FRAME_BYTES = 16 * 1024 * 1024``): one
#: JSON-RPC frame may not exceed 16 MiB; a larger frame tears the
#: connection down. Kept at the TS value.
MAX_FRAME_BYTES = 16 * 1024 * 1024

#: OP-R4-5 (TS stdio.ts L17 ``EXIT_WAIT = Duration.seconds(1)``): the
#: bounded wait for the exit code (and the final stderr) after stdout
#: ends. Kept at the TS value.
EXIT_WAIT_S = 1.0

#: OP-R4-5 (TS stdio.ts L18 ``STDERR_TAIL = 1_000``): stderr is drained
#: continuously but only the last 1_000 characters are kept for
#: diagnostics. Kept at the TS value.
STDERR_TAIL_CHARS = 1000

#: How many stdout bytes one pipe read may return (framing granularity).
READ_CHUNK_BYTES = 65536


class McpTransportError(Exception):
    """Base for every stdio-transport failure (mirrors the TS error surface)."""


class McpNotConnectedError(McpTransportError):
    """A send was attempted outside the open phase (TS stdio.ts "Not connected")."""


class McpFrameTooLargeError(McpTransportError):
    """A JSON-RPC frame exceeded the cap (TS: "MCP stdio frame exceeded 16 MiB")."""


class McpProtocolError(McpTransportError):
    """A frame did not decode into a JSON-RPC 2.0 message (fail-closed teardown)."""


class McpConnectionLostError(McpTransportError):
    """The server process ended (unexpected exit, close, or a dead stdin)."""


class McpRemoteError(McpTransportError):
    """The server answered a request with a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        text = f"MCP error {code}: {message}"
        if data is not None:
            text = f"{text} (data={data!r})"
        super().__init__(text)
        self.code = code
        self.message = message
        self.data = data


class StdioTransport:
    """One MCP server's stdio transport: spawn, frame, correlate, shut down.

    ``request`` assigns a fresh JSON-RPC id and awaits its correlated
    response under the caller's timeout; late or duplicate responses are
    discarded by id. ``close`` is the TS ``stop`` sequence -- stdin EOF ->
    :data:`CLOSE_GRACE_S` -> terminate the tree -> :data:`FORCE_KILL_AFTER_S`
    -> hard kill -- and is safe to call twice or concurrently. An
    unexpected stdout end (the server died outside a close) gathers the
    exit code and the stderr tail into one diagnostic, fails every
    pending request with it, invokes ``on_unexpected_exit`` and closes.
    """

    def __init__(
        self,
        *,
        command: str,
        args: Sequence[str] = (),
        cwd: str | None = None,
        env: Mapping[str, str],
        server: str = "mcp",
        on_unexpected_exit: Callable[[Exception], None] | None = None,
        close_grace_s: float = CLOSE_GRACE_S,
        force_kill_after_s: float = FORCE_KILL_AFTER_S,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        exit_wait_s: float = EXIT_WAIT_S,
    ) -> None:
        self._command = command
        self._args = tuple(args)
        self._cwd = cwd
        self._env = dict(env)
        self._server = server
        self._on_unexpected_exit = on_unexpected_exit
        self._close_grace_s = close_grace_s
        self._force_kill_after_s = force_kill_after_s
        self._max_frame_bytes = max_frame_bytes
        self._exit_wait_s = exit_wait_s
        self._proc: asyncio.subprocess.Process | None = None
        self._job: JobObject | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._buffer = bytearray()
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._next_id = 0
        self._stderr_tail = ""
        self._started = False
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn the server (argv direct exec, §6 L304) and start both pipe readers."""
        if self._started:
            raise McpTransportError("Stdio transport already started")
        popen_kwargs: dict[str, Any] = {}
        if sys.platform != "win32":
            # POSIX process-group discipline (R3-S5 runner.py precedent):
            # the server is its own session leader, so killpg reaps the tree.
            popen_kwargs["start_new_session"] = True
        self._proc = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            cwd=self._cwd,
            env=self._env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **popen_kwargs,
        )
        self._job = self._capture_in_job()
        self._started = True
        self._stdout_task = asyncio.create_task(self._read_stdout(), name=f"mcp-stdio-{self._server}-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name=f"mcp-stdio-{self._server}-stderr")

    async def close(self) -> None:
        """Shut the server down: stdin EOF -> grace -> terminate tree -> hard kill.

        Mirrors TS stdio.ts ``stop`` (L58-63): wait for a graceful exit
        after the stdin EOF, escalate to a tree terminate, escalate again
        to a hard kill. Idempotent and concurrent-safe (one lock, one
        ``_closed`` re-check); every pending request fails with a lost
        connection so no caller waits on a dead pipe. A leader that
        already exited on its own (the crash path) still gets its
        surviving tree swept: the sweep belongs to every exit path.
        """
        async with self._close_lock:
            if self._closed:
                return
            # Phase "closed" first (TS close() sets it before stopping) so a
            # concurrent stdout end is expected, never an unexpected exit.
            self._closing = True
            proc = self._proc
            if proc is not None:
                if proc.stdin is not None:
                    proc.stdin.close()
                try:
                    await asyncio.wait_for(proc.wait(), self._close_grace_s)
                except TimeoutError:
                    pass
                if proc.returncode is None:
                    self._terminate_tree()
                    try:
                        await asyncio.wait_for(proc.wait(), self._force_kill_after_s)
                    except TimeoutError:
                        self._kill_tree_hard()
                        try:
                            await asyncio.wait_for(proc.wait(), self._force_kill_after_s)
                        except TimeoutError:
                            # Unreapable after two escalations: nothing left to
                            # try; the caller faces an unknown outcome (④).
                            pass
                else:
                    # The leader already exited on its own (the crash path: a
                    # server that dies mid-session can leave descendants
                    # alive in its process group or job). The tree sweep
                    # belongs to every exit path, not only the live-leader
                    # escalation: killpg on the dead leader's still-populated
                    # group reaps the survivors, an empty group is the
                    # ProcessLookupError the sweep already swallows, and the
                    # Windows job terminate mirrors the KILL_ON_JOB_CLOSE
                    # release at the end of close(). A leaderless group owes
                    # no grace, so the terminate escalates straight to the
                    # hard kill. Pinned by test_crash_sweeps_tree.
                    self._terminate_tree()
                    self._kill_tree_hard()
                # Let the readers observe the pipes' EOF, bounded; a straggler
                # (e.g. a grandchild holding the write end) is cancelled.
                current = asyncio.current_task()
                for task in (self._stdout_task, self._stderr_task):
                    if task is None or task.done() or task is current:
                        continue
                    try:
                        await asyncio.wait_for(asyncio.shield(task), self._exit_wait_s)
                    except TimeoutError:
                        task.cancel()
            self._fail_pending(McpConnectionLostError(f"MCP server {self._server!r} connection closed"))
            if self._job is not None:
                # KILL_ON_JOB_CLOSE backstop: releasing the handle is the
                # last-resort sweep if this process itself dies later.
                self._job.close()
                self._job = None
            self._closed = True

    # -- wire face ---------------------------------------------------------

    async def request(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout_s: float,
    ) -> object:
        """Send a JSON-RPC request and await its correlated response.

        A timeout cancels the wait only: the server may still be running
        the request, so the outcome is the caller's "unknown" (§6 L304) --
        the late response is discarded by the id correlation.
        """
        if not self._open:
            raise McpNotConnectedError("MCP stdio transport is not connected")
        request_id = self._next_id
        self._next_id += 1
        frame: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = dict(params)
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send_frame(frame)
            return await asyncio.wait_for(future, timeout_s)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Mapping[str, object] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        frame: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = dict(params)
        await self._send_frame(frame)

    @property
    def stderr_tail(self) -> str:
        """The last :data:`STDERR_TAIL_CHARS` characters of the server's stderr."""
        return self._stderr_tail

    @property
    def pid(self) -> int | None:
        """The server's pid (None before start)."""
        return self._proc.pid if self._proc is not None else None

    @property
    def closed(self) -> bool:
        """True once :meth:`close` finished."""
        return self._closed

    @property
    def _open(self) -> bool:
        return self._started and not self._closing and not self._closed

    # -- readers -----------------------------------------------------------

    async def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None  # PIPE
        fatal: BaseException | None = None
        while True:
            chunk = await proc.stdout.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            try:
                for message in self._feed(chunk):
                    await self._dispatch(message)
            except McpTransportError as error:
                fatal = error
                break
            except Exception as error:  # reader plumbing: fail closed
                fatal = McpProtocolError(f"stdout reader failed: {error}")
                break
        await self._stdout_ended(fatal)

    async def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None  # PIPE
        # Drain stderr continuously so a chatty server cannot stall on a full
        # pipe (TS stdio.ts L146-148); only the tail is kept for diagnostics.
        while True:
            chunk = await proc.stderr.read(READ_CHUNK_BYTES)
            if not chunk:
                return
            text = chunk.decode("utf-8", "replace")
            self._stderr_tail = (self._stderr_tail + text)[-STDERR_TAIL_CHARS:]

    async def _stdout_ended(self, fatal: BaseException | None) -> None:
        """stdout ended: diagnose an unexpected exit, then close (TS ensuring block)."""
        if self._closing or self._closed:
            return  # expected end: close() owns the pending failures
        diagnostic: BaseException | None = fatal
        if diagnostic is None:
            proc = self._proc
            assert proc is not None
            # stdout can end before the final stderr output has been read
            # (TS stdio.ts L174-179): bound both waits by EXIT_WAIT.
            stderr_task = self._stderr_task
            if stderr_task is not None and not stderr_task.done():
                try:
                    await asyncio.wait_for(stderr_task, self._exit_wait_s)
                except TimeoutError:
                    pass  # abandoned: the tail holds whatever arrived
            code = proc.returncode
            if code is None:
                try:
                    code = await asyncio.wait_for(proc.wait(), self._exit_wait_s)
                except TimeoutError:
                    code = None  # never confirmed: reported without a code
            exited = "MCP server process exited" if code is None else f"MCP server process exited with code {code}"
            tail = self._stderr_tail.strip()
            diagnostic = McpConnectionLostError(f"{exited}: {tail}" if tail else exited)
        assert diagnostic is not None
        if not isinstance(diagnostic, Exception):
            diagnostic = McpProtocolError(str(diagnostic))
        if self._on_unexpected_exit is not None:
            try:
                self._on_unexpected_exit(diagnostic)
            except Exception:
                pass  # the manager's bookkeeping must not break the teardown
        self._fail_pending(diagnostic)
        await self.close()

    # -- framing -----------------------------------------------------------

    def _feed(self, chunk: bytes) -> list[dict[str, object]]:
        """Append one stdout chunk and extract complete frames.

        The cap counts the bytes since the last newline (TS ``trailingBytes``,
        byte-accurate across chunk boundaries); an oversized frame -- whether
        still partial or already complete in this chunk -- is rejected.
        """
        self._buffer.extend(chunk)
        last_newline = self._buffer.rfind(b"\n")
        pending = len(self._buffer) if last_newline < 0 else len(self._buffer) - last_newline - 1
        if pending > self._max_frame_bytes:
            raise McpFrameTooLargeError(f"MCP stdio frame exceeded {self._max_frame_bytes} bytes")
        messages: list[dict[str, object]] = []
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if len(line) > self._max_frame_bytes:
                raise McpFrameTooLargeError(f"MCP stdio frame exceeded {self._max_frame_bytes} bytes")
            text = line.decode("utf-8", "replace")
            if text.endswith("\r"):
                text = text[:-1]
            if not text:
                continue  # SDK ReadBuffer: an empty line carries no message
            try:
                message = json.loads(text)
            except ValueError as error:
                raise McpProtocolError(f"frame is not valid JSON: {error}") from error
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise McpProtocolError("frame is not a JSON-RPC 2.0 message")
            messages.append(message)
        return messages

    async def _dispatch(self, message: dict[str, object]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            self._resolve_response(message)
            return
        if "method" in message and "id" in message:
            # Server->client request: Phase 1 implements none of sampling /
            # elicitation / roots -- answer method-not-found so the server
            # never waits on us (④: the capability faces are out of R4-S2).
            if not self._open:
                return
            await self._send_frame(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": f"Method not found: {message['method']}"},
                }
            )
            return
        if "method" in message:
            # Server notification (notifications/tools/list_changed etc.):
            # no hot refresh in Phase 1 (r4-slices §4 difference 4) -- ignored.
            return
        raise McpProtocolError("message is neither a response nor a request")

    def _resolve_response(self, message: dict[str, object]) -> None:
        request_id = message["id"]
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            raise McpProtocolError("response id must be an integer")
        future = self._pending.get(request_id)
        if future is None or future.done():
            return  # late or duplicate response: discarded by id
        if "error" in message:
            error = message["error"]
            if (
                not isinstance(error, dict)
                or isinstance(error.get("code"), bool)
                or not isinstance(error.get("code"), int)
                or not isinstance(error.get("message"), str)
            ):
                raise McpProtocolError("malformed JSON-RPC error object")
            future.set_exception(McpRemoteError(error["code"], error["message"], error.get("data")))
        else:
            future.set_result(message.get("result"))

    async def _send_frame(self, frame: Mapping[str, object]) -> None:
        if not self._open:
            raise McpNotConnectedError("MCP stdio transport is not connected")
        proc = self._proc
        stdin = proc.stdin if proc is not None else None
        if stdin is None:
            raise McpNotConnectedError("MCP stdio transport has no stdin")
        data = json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            # TS stdio.ts L14 OUTGOING_CAPACITY=64 bounds the outgoing frame
            # queue; asyncio's StreamWriter buffers internally and drain()
            # applies backpressure past the transport's high-water mark, so
            # the bound is structural (awaitable backpressure), not a count.
            stdin.write(data)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise McpConnectionLostError(f"MCP server stdin is gone: {error}") from error

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    # -- process-tree discipline (R3-S5 winjob/killpg reuse) ---------------

    def _capture_in_job(self) -> JobObject | None:
        """Windows: assign the fresh server to a Job Object; None = defensive fallback.

        Mirrors ``opencode/process/runner.py``'s capture (the winjob module is
        reused read-only; ``runner.py`` itself is untouched): a process that
        exited between spawn and capture is not an error, any other failure
        degrades to the main-process-only kill instead of failing the spawn.
        """
        if sys.platform != "win32":
            return None
        proc = self._proc
        assert proc is not None
        try:
            job = JobObject()
        except OSError:
            return None
        try:
            job.capture(proc.pid)
        except OSError:
            if proc.returncode is not None:
                return job  # already exited: nothing left to capture
            job.close()
            return None
        return job

    def _terminate_tree(self) -> None:
        """Terminate the whole tree: TerminateJobObject / SIGTERM the process group."""
        proc = self._proc
        assert proc is not None
        if sys.platform == "win32":
            if self._job is not None:
                try:
                    self._job.terminate(1)
                    return
                except OSError:
                    pass  # defensive: fall through to the main-process kill
            proc.terminate()  # Windows TerminateProcess (no SIGTERM analog)
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)  # pgid == pid (start_new_session)
        except (ProcessLookupError, PermissionError):
            try:
                proc.terminate()  # defensive: the group is gone but the leader lingers
            except ProcessLookupError:
                pass

    def _kill_tree_hard(self) -> None:
        """Hard-kill the whole tree: TerminateJobObject / SIGKILL the process group."""
        proc = self._proc
        assert proc is not None
        if sys.platform == "win32":
            if self._job is not None:
                try:
                    self._job.terminate(1)
                    return
                except OSError:
                    pass
            proc.kill()
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
