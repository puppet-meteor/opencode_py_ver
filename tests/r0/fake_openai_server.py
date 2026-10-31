"""Scripted OpenAI-compatible SSE fake server for R0-A provider contract tests.

Per doc/STEP-01-R0.md: test-only, lives under tests/r0, never imported by
production code. Serves /v1/chat/completions style POSTs with `stream=true`
SSE framing (`data:` frames, `data: [DONE]` terminator) plus fault injection:
HTTP 429 with Retry-After, header hang, chunk hang, EOF without [DONE], and
per-frame delays.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

DONE = b"data: [DONE]\r\n\r\n"


def sse_frame(obj: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(obj)}\r\n\r\n".encode()


def _chunk_base(model: str = "fake-model") -> dict[str, Any]:
    return {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 0, "model": model}


def text_delta_frame(content: str) -> bytes:
    body = _chunk_base()
    body["choices"] = [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]
    return sse_frame(body)


def tool_call_start_frame(call_index: int, call_id: str, name: str) -> bytes:
    body = _chunk_base()
    body["choices"] = [
        {
            "index": 0,
            "delta": {
                "tool_calls": [
                    {
                        "index": call_index,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": ""},
                    }
                ]
            },
            "finish_reason": None,
        }
    ]
    return sse_frame(body)


def tool_call_args_frame(call_index: int, arguments: str) -> bytes:
    body = _chunk_base()
    body["choices"] = [
        {
            "index": 0,
            "delta": {"tool_calls": [{"index": call_index, "function": {"arguments": arguments}}]},
            "finish_reason": None,
        }
    ]
    return sse_frame(body)


def finish_frame(reason: str) -> bytes:
    body = _chunk_base()
    body["choices"] = [{"index": 0, "delta": {}, "finish_reason": reason}]
    return sse_frame(body)


def usage_frame(
    prompt_tokens: int,
    completion_tokens: int,
    *,
    reasoning: int | None = None,
    cached: int | None = None,
) -> bytes:
    body = _chunk_base()
    body["choices"] = []
    body["usage"] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "prompt_tokens_details": {"cached_tokens": cached} if cached is not None else None,
        "completion_tokens_details": {"reasoning_tokens": reasoning} if reasoning is not None else None,
    }
    return sse_frame(body)


@dataclass
class Frame:
    payload: bytes
    delay_s: float = 0.0


@dataclass
class ScriptedResponse:
    frames: list[Frame] = field(default_factory=list)
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    terminate: bool = True  # end the chunked body cleanly after frames
    omit_done: bool = False  # clean body end, but without the SSE [DONE] line (EOF-without-terminal)
    hang_after_frames: bool = False  # keep the connection open, send nothing
    hang_headers: bool = False  # accept the request, never answer
    json_body: dict[str, Any] | None = None


@dataclass
class RequestLog:
    time_s: float
    method: str
    path: str
    body: dict[str, Any] | None


class FakeOpenAIServer:
    """Single-request-per-connection asyncio HTTP server with scripted responses."""

    def __init__(self, responses: list[ScriptedResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[RequestLog] = []
        self._server: asyncio.AbstractServer | None = None
        self._stop = asyncio.Event()
        self._handlers: set[asyncio.Task[None]] = set()
        self.base_url: str = ""

    async def start(self) -> str:
        self._stop.clear()
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    async def aclose(self) -> None:
        self._stop.set()
        for handler in list(self._handlers):
            if not handler.done():
                handler.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        self._handlers.clear()
        if self._server is not None:
            server, self._server = self._server, None
            server.close()
            await server.wait_closed()

    def _next_response(self) -> ScriptedResponse:
        if self._responses:
            return self._responses.pop(0)
        return ScriptedResponse(status=500, json_body={"error": {"message": "fake server: no scripted response"}})

    def _write_status_response(self, writer: asyncio.StreamWriter, response: ScriptedResponse) -> None:
        payload = json.dumps(response.json_body or {"error": {"message": f"HTTP {response.status}"}}).encode("utf-8")
        lines = [
            f"HTTP/1.1 {response.status} Error",
            "Content-Type: application/json",
            f"Content-Length: {len(payload)}",
            "Connection: close",
        ]
        lines += [f"{key}: {value}" for key, value in response.headers.items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload)

    @staticmethod
    def _write_chunk(writer: asyncio.StreamWriter, payload: bytes) -> None:
        writer.write(f"{len(payload):x}\r\n".encode("latin-1") + payload + b"\r\n")

    async def _graceful_close(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """RST-free close for Windows loopback.

        Closing a socket that still has unread inbound data makes the OS send
        RST instead of FIN, which can reset the client mid-response (observed
        as anyio BrokenResourceError while awaiting response headers). Half-
        close with write_eof() first, then drain until the client's own EOF
        (bounded), then close.
        """
        try:
            if not writer.is_closing():
                writer.write_eof()
        except (OSError, RuntimeError):
            pass
        try:
            try:
                while True:
                    data = await asyncio.wait_for(reader.read(), timeout=5.0)
                    if not data:
                        break
            except (TimeoutError, OSError):
                pass
        except asyncio.CancelledError:
            # aclose() cancels in-flight handlers; still close the socket below.
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            header_bytes = await reader.readuntil(b"\r\n\r\n")
            lines = header_bytes.decode("latin-1").split("\r\n")
            method, path, _version = lines[0].split(" ", 2)
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    key, _, value = line.partition(":")
                    headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            body_bytes = await reader.readexactly(length) if length else b""
            body: dict[str, Any] | None = json.loads(body_bytes) if body_bytes else None
            self.requests.append(
                RequestLog(
                    time_s=time.monotonic(),
                    method=method,
                    path=path,
                    body=body,
                )
            )
            if path.split("?", 1)[0] != "/v1/chat/completions":
                self._write_status_response(
                    writer,
                    ScriptedResponse(status=404, json_body={"error": {"message": f"unknown path: {path}"}}),
                )
                await writer.drain()
                return
            if not isinstance(body, dict) or body.get("stream") is not True:
                # The fake only speaks streaming chat completions (STEP-01-R0).
                self._write_status_response(
                    writer,
                    ScriptedResponse(status=400, json_body={"error": {"message": "fake server requires stream=true"}}),
                )
                await writer.drain()
                return
            response = self._next_response()
            if response.hang_headers:
                await self._stop.wait()
                return
            if response.status != 200:
                self._write_status_response(writer, response)
                await writer.drain()
                return
            extra = "".join(f"{key}: {value}\r\n" for key, value in response.headers.items())
            # Chunked framing (like the real OpenAI SSE responses): httpx2
            # cannot reliably handle length-less close-delimited bodies, so
            # every frame becomes one HTTP chunk and the body ends with the
            # terminating 0-chunk. See doc/evidence/r0/provider.md.
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n{extra}"
                "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n".encode("latin-1")
            )
            await writer.drain()
            for frame in response.frames:
                if frame.delay_s > 0:
                    await asyncio.sleep(frame.delay_s)
                self._write_chunk(writer, frame.payload)
                await writer.drain()
            if response.hang_after_frames:
                await self._stop.wait()
            elif response.terminate:
                if not response.omit_done:
                    self._write_chunk(writer, DONE)
                    await writer.drain()
                writer.write(b"0\r\n\r\n")
                await writer.drain()
        except (
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
            OSError,
        ):
            pass
        finally:
            await self._graceful_close(reader, writer)
            if task is not None:
                self._handlers.discard(task)
