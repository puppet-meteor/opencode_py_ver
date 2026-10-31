"""Dump sanitized JSON fixtures for R0-A provider evidence.

Explicit tool (NOT part of pytest or CI): regenerates
``doc/evidence/r0/fixtures/`` by driving the scripted fake SSE server through
the same ``MaasSpikeAdapter`` the contract tests use, so each fixture records
the complete terminal state (full chunk sequence, usage, finish, tool plan or
terminal error) that future rounds (R1 recovery, R2 runner) can replay against.

Every payload here is synthetic fake-server data: no real endpoints,
credentials, prompts or model outputs ever enter these files.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from tests.r0.fake_openai_server import (
    FakeOpenAIServer,
    Frame,
    ScriptedResponse,
    finish_frame,
    text_delta_frame,
    tool_call_args_frame,
    tool_call_start_frame,
    usage_frame,
)

from tools.r0.provider_spike import (
    CoreError,
    GenerationRequest,
    LLMMessage,
    MaasSpikeAdapter,
    ModelRef,
    ProviderChunk,
    ToolSpec,
)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "doc" / "evidence" / "r0" / "fixtures"

READ_TOOL = ToolSpec(
    namespace="fs",
    name="read",
    version="1",
    description="Read a file (synthetic fixture tool; never executed)",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    side_effects="read",
)


def _request(request_id: str, *, tools: tuple[ToolSpec, ...] = (), timeout_s: float = 10.0) -> GenerationRequest:
    return GenerationRequest(
        request_id=request_id,
        model=ModelRef("maas", "fake-model"),
        messages=(LLMMessage(role="user", text="hi"),),
        tools=tools,
        timeout_s=timeout_s,
    )


def _chunk_dict(chunk: ProviderChunk) -> dict[str, Any]:
    return {
        "request_id": chunk.request_id,
        "index": chunk.index,
        "type": chunk.type,
        "part_id": chunk.part_id,
        "delta": chunk.delta,
        "call_index": chunk.call_index,
        "tool_call_id": chunk.tool_call_id,
        "name": chunk.name,
        "raw_arguments": chunk.raw_arguments,
        "usage": None
        if chunk.usage is None
        else {
            "input_tokens": chunk.usage.input_tokens,
            "output_tokens": chunk.usage.output_tokens,
            "reasoning_tokens": chunk.usage.reasoning_tokens,
            "cache_read_tokens": chunk.usage.cache_read_tokens,
            "cache_write_tokens": chunk.usage.cache_write_tokens,
        },
        "reason": chunk.reason,
        "raw_reason": chunk.raw_reason,
    }


def _error_dict(error: CoreError | None) -> dict[str, Any] | None:
    if error is None:
        return None
    return {"code": error.code, "message": error.message, "details": error.details}


def _wire_description(responses: list[ScriptedResponse]) -> list[dict[str, Any]]:
    return [
        {
            "status": response.status,
            "headers": response.headers,
            "sse_frames": [frame.payload.decode("utf-8") for frame in response.frames],
            "terminates_body": response.terminate,
            "omits_done_line": response.omit_done,
            "note": "server frames each payload as one HTTP chunk; terminate=True appends the SSE 'data: [DONE]' line",
        }
        for response in responses
    ]


async def _run(
    responses: list[ScriptedResponse], request: GenerationRequest
) -> tuple[list[ProviderChunk], CoreError | None, int]:
    server = FakeOpenAIServer(responses)
    base_url = await server.start()
    adapter = MaasSpikeAdapter(f"{base_url}/v1", api_key="r0-fixture-key")
    chunks: list[ProviderChunk] = []
    error: CoreError | None = None
    try:
        async for chunk in adapter.stream(request):
            chunks.append(chunk)
    except CoreError as exc:
        error = exc
    finally:
        await adapter.close()
    request_count = len(server.requests)
    await server.aclose()
    return chunks, error, request_count


async def _fixture(
    name: str,
    description: str,
    responses: list[ScriptedResponse],
    request: GenerationRequest,
) -> dict[str, Any]:
    chunks, error, request_count = await _run(responses, request)
    return {
        "scenario": name,
        "description": description,
        "sanitization": "synthetic fake-server data only; no real endpoint, credential, prompt or model output",
        "request": {
            "request_id": request.request_id,
            "model_provider": request.model.provider,
            "model": request.model.model,
            "tools": [tool.wire_name for tool in request.tools],
            "timeout_s": request.timeout_s,
        },
        "wire": _wire_description(responses),
        "http_request_count": request_count,
        "chunks": [_chunk_dict(chunk) for chunk in chunks],
        "error": _error_dict(error),
    }


async def _scenarios() -> list[tuple[str, dict[str, Any]]]:
    done: list[tuple[str, dict[str, Any]]] = []

    done.append(
        (
            "01_text_usage_after_finish.json",
            await _fixture(
                "text-stream-usage-after-finish",
                "wire order: finish frame, then usage frame; both survive",
                [
                    ScriptedResponse(
                        frames=[
                            Frame(text_delta_frame("Hello")),
                            Frame(text_delta_frame(" world")),
                            Frame(finish_frame("stop")),
                            Frame(usage_frame(12, 34, reasoning=5, cached=2)),
                        ]
                    )
                ],
                _request("r0-fixture-text-001"),
            ),
        )
    )

    done.append(
        (
            "02_text_usage_before_finish.json",
            await _fixture(
                "text-stream-usage-before-finish",
                "wire order: usage frame before finish frame; both survive",
                [
                    ScriptedResponse(
                        frames=[
                            Frame(text_delta_frame("ok")),
                            Frame(usage_frame(3, 4)),
                            Frame(finish_frame("stop")),
                        ]
                    )
                ],
                _request("r0-fixture-text-002"),
            ),
        )
    )

    done.append(
        (
            "03_interleaved_tool_calls.json",
            await _fixture(
                "interleaved-tool-calls",
                "two interleaved calls with argument sharding; full args parsed only at tool_call_end",
                [
                    ScriptedResponse(
                        frames=[
                            Frame(tool_call_start_frame(0, "call-1", "fs__read")),
                            Frame(tool_call_args_frame(0, '{"pa')),
                            Frame(tool_call_start_frame(1, "call-2", "fs__read")),
                            Frame(tool_call_args_frame(1, '{"path": "/b"}')),
                            Frame(tool_call_args_frame(0, 'th": "/a"}')),
                            Frame(finish_frame("tool_calls")),
                            Frame(usage_frame(9, 11)),
                        ]
                    )
                ],
                _request("r0-fixture-tools-001", tools=(READ_TOOL,)),
            ),
        )
    )

    done.append(
        (
            "04_eof_without_terminal.json",
            await _fixture(
                "eof-without-terminal",
                "clean body end but no SSE [DONE] line and no finish frame; maps to ProviderStreamInterrupted",
                [ScriptedResponse(frames=[Frame(text_delta_frame("partial"))], omit_done=True)],
                _request("r0-fixture-eof-001"),
            ),
        )
    )

    done.append(
        (
            "05_429_retry_then_success.json",
            await _fixture(
                "429-retry-then-success",
                "429 before first content (retry-after-ms respected), then success on the retry",
                [
                    ScriptedResponse(status=429, headers={"retry-after-ms": "10"}),
                    ScriptedResponse(frames=[Frame(text_delta_frame("ok")), Frame(finish_frame("stop"))]),
                ],
                _request("r0-fixture-retry-001"),
            ),
        )
    )

    done.append(
        (
            "06_wire_double_finish.json",
            await _fixture(
                "wire-duplicate-finish-rejected",
                "two finish frames on the wire; the stream is rejected with a protocol error, no finish terminal",
                [
                    ScriptedResponse(
                        frames=[
                            Frame(text_delta_frame("a")),
                            Frame(finish_frame("stop")),
                            Frame(finish_frame("tool_calls")),
                        ]
                    )
                ],
                _request("r0-fixture-wire-001"),
            ),
        )
    )

    done.append(
        (
            "07_wire_content_after_finish.json",
            await _fixture(
                "wire-content-after-finish-rejected",
                "text content after a finish frame; the stream is rejected with a protocol error",
                [
                    ScriptedResponse(
                        frames=[
                            Frame(text_delta_frame("a")),
                            Frame(finish_frame("stop")),
                            Frame(text_delta_frame("b")),
                        ]
                    )
                ],
                _request("r0-fixture-wire-002"),
            ),
        )
    )

    return done


async def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for filename, payload in await _scenarios():
        path = FIXTURES_DIR / filename
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(path.parents[3])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
