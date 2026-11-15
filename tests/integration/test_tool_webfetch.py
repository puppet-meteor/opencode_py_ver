"""Builtin ``webfetch__fetch`` integration tests (P7-S3; T-C1-102, ticket §4).

The full ticket §4 face map through the public faces (adapter.bind ->
registry register -> snapshot -> invoke by wire name), all against a
scripted loopback HTTP/1.1 server (``_Loopback``: one request per
connection, every served request logged with its headers, then closed):

* §4-1 -- the three formats over a real fetch: markdown (the html ->
  markdown converter's core surfaces), text (the skip-stack extractor:
  script/style/noscript content never leaks), html (verbatim passthrough);
  the ``{url, contentType, format, output}`` structured payload, the
  content/output identity, the ``metadata={"contentType": ...}`` face via
  the direct adapter execute (the only surface that carries metadata),
  and the non-HTML content-type passthrough leg.
* §4-2 -- the absent ``format`` key defaults to markdown; the timeout
  bounds and the unknown-format re-check (the adapter double face for
  what the strict schema subset cannot express), plus the schema
  ``maximum`` arm and the missing-``url`` arm through the Invoker.
* §4-3 -- the non-http scheme rejection with the verbatim sentence.
* §4-4 -- the manual redirect chain: relative + absolute locations, the
  ten-redirect ceiling with the verbatim sentence, the per-hop policy
  re-check (a denied target is never requested), and the 4xx/5xx
  ``HTTP {status}`` face.
* §4-5 -- the mime gate: image attachment rejection, non-textual file
  rejection, and the ``image/svg+xml`` exemption leg.
* §4-6 -- the byte cap (the declared ``content-length`` precheck and the
  streamed per-chunk cap, both with the verbatim over-limit sentence) and
  the whole-chain timeout with the verbatim sentence.
* §4-7 -- the Invoker's initial-URL deny: zero network calls, the
  ``denied`` / PERMISSION_DENIED terminal with not-started effects.
* §4-8 -- the policy pins (the ``_DEFAULT_EFFECTS`` row, the untouched
  ``_ASK_NAMESPACES``, the ``extract_resources`` url binding, the Spec
  faces) and the unbound-adapter fail-closed wiring defect.
* §4-9 -- the two seat-composed error arms: the transport failure wrapper
  and the Cloudflare challenge retry with the verbatim ``"opencode"`` UA.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest
from pydantic import BaseModel

from opencode.contracts import CoreErrorCode
from opencode.contracts.events import EventDraft
from opencode.contracts.messages import JsonObject, ToolTextContent
from opencode.session.service import IdFactory
from opencode.tools import ToolContext, ToolLimits, ToolOutcome, ToolRegistry
from opencode.tools.builtin.webfetch import WEBFETCH_MAX_RESPONSE_BYTES, WEBFETCH_SPEC, WebFetchAdapter
from opencode.tools.invoker import ToolInvokeError
from opencode.tools.policy import (
    _ASK_NAMESPACES,
    _DEFAULT_EFFECTS,
    PermissionPolicy,
    PermissionRule,
    extract_resources,
)

_FIXED_TIME = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)
_SESSION_ID = "ses_twf"
_RUN_ID = "run_twf"
_MESSAGE_ID = "msg_twf"
_TOOL_CALL_ID = "call_twf"

#: The shared fixture page (the §4-1 three-format corpus): every core
#: converter surface is present -- heading, inline link, fenced pre block
#: with a ``language-`` class, a two-item list -- plus the hidden subtree
#: faces (``style`` inside ``head``, ``script``, ``noscript``) whose content
#: must never leak into either conversion.
_PAGE = (
    "<!DOCTYPE html><html><head><style>body{color:red}</style></head>"
    "<body><h1>Docs</h1>"
    '<p>Some <a href="/docs">intro</a> text.</p>'
    '<pre><code class="language-python">print(1)</code></pre>'
    "<ul><li>one</li><li>two</li></ul>"
    "<script>alert(1)</script><noscript>nojs</noscript>"
    "<p>Tail</p></body></html>"
)
_CONTENT_TYPE = "text/html; charset=utf-8"

#: The svg+xml exemption body (§4-5): an image/* type that must pass the
#: mime gate (exempt from the image rejection, textual via ``+xml``).
_SVG = '<svg xmlns="http://www.w3.org/2000/svg"></svg>'


# ---------------------------------------------------------------------------
# harness (the test_tool_patch_todo_question plain-registry recipe)
# ---------------------------------------------------------------------------


def _clock() -> datetime:
    return _FIXED_TIME


class _FixedIds:
    """Deterministic IdFactory: ascending zero-padded ids per prefix."""

    def __init__(self) -> None:
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:06d}"

    def session_id(self) -> str:
        return self._next("ses_")

    def event_id(self) -> str:
        return self._next("evt_")

    def message_id(self) -> str:
        return self._next("msg_")

    def run_id(self) -> str:
        return self._next("run_")

    def batch_id(self) -> str:
        return self._next("batch_")


class _RecordingSink:
    """EventSink stand-in: records every committed batch verbatim."""

    def __init__(self) -> None:
        self.batches: list[tuple[EventDraft[BaseModel], ...]] = []
        self.drafts: list[EventDraft[BaseModel]] = []

    async def __call__(self, drafts: Sequence[EventDraft[BaseModel]]) -> None:
        self.batches.append(tuple(drafts))
        self.drafts.extend(drafts)


def _context(working_dir: Path) -> ToolContext:
    return ToolContext(
        session_id=_SESSION_ID,
        run_id=_RUN_ID,
        message_id=_MESSAGE_ID,
        tool_call_id=_TOOL_CALL_ID,
        working_dir=str(working_dir),
        limits=ToolLimits(),
    )


def _adapter(rules: tuple[PermissionRule, ...] = ()) -> WebFetchAdapter:
    """A host-bound adapter: the hop-check policy is mandatory wiring.

    With no rules the fresh policy evaluates every hop to the
    ``_DEFAULT_EFFECTS`` ask default, which the hop check only fails on
    ``deny`` -- so ruleless fetches pass (the redirect must not re-run the
    approval protocol the initial URL already cleared).
    """
    adapter = WebFetchAdapter()
    adapter.bind(PermissionPolicy(boot_id="boot_webfetch", rules=rules))
    return adapter


def _registry(adapter: WebFetchAdapter, policy: PermissionPolicy | None = None) -> ToolRegistry:
    """A plain registry with the webfetch adapter, one pinned snapshot.

    ``policy`` is the Invoker's gate policy: absent (the default) the gate
    is skipped -- the fetch tests must use this arm, since a policy whose
    evaluation lands on ``ask`` without an approval channel would cancel
    instead of fetching; the §4-7 deny test passes the rule-bearing policy.
    """
    registry = ToolRegistry(
        sink=cast("Any", _RecordingSink()),
        clock=_clock,
        ids=cast(IdFactory, _FixedIds()),
        policy=policy,
    )
    registry.register(adapter)
    registry.snapshot(_SESSION_ID)
    return registry


async def _invoke(registry: ToolRegistry, working_dir: Path, input: JsonObject) -> ToolOutcome:
    return await registry.invoke(_context(working_dir), "webfetch__fetch", input, _TOOL_CALL_ID)


def _texts(outcome: ToolOutcome) -> list[str]:
    return [part.text for part in outcome.content if isinstance(part, ToolTextContent)]


def _one_text(outcome: ToolOutcome) -> str:
    texts = _texts(outcome)
    assert len(texts) == 1
    return texts[0]


def _failed(outcome: ToolOutcome, code: CoreErrorCode) -> str:
    """Assert the fail-closed terminal and return its model-readable text."""
    assert outcome.state == "failed"
    assert outcome.error is not None
    assert outcome.error.code is code, _one_text(outcome)
    return _one_text(outcome)


# ---------------------------------------------------------------------------
# the scripted loopback HTTP/1.1 server (ticket §4: local loopback only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ServedRequest:
    method: str
    path: str
    headers: dict[str, str]


def _respond(
    writer: asyncio.StreamWriter,
    status: int,
    reason: str,
    headers: dict[str, str],
    body: bytes = b"",
) -> None:
    """Write one complete response (auto content-length, then the body)."""
    head = [f"HTTP/1.1 {status} {reason}", "Connection: close"]
    if "content-length" not in {name.lower() for name in headers}:
        head.append(f"Content-Length: {len(body)}")
    head.extend(f"{name}: {value}" for name, value in headers.items())
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
    if body:
        writer.write(body)


class _Loopback:
    """A scripted loopback server: parse one request per connection, log it,
    hand ``(request, writer)`` to the test's handler, then close.

    Every served request is recorded (``requests``) with its header set, so
    hit counts and the per-request User-Agent are directly assertable. On
    exit the listener closes and any still-served handler task is cancelled,
    so hung/slow handlers (the timeout leg) cannot outlive the test.
    """

    def __init__(self, handler: Callable[[_ServedRequest, asyncio.StreamWriter], Awaitable[None]]) -> None:
        self._handler = handler
        self.requests: list[_ServedRequest] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self.url = ""
        self._server: asyncio.Server

    async def __aenter__(self) -> _Loopback:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        sockets = self._server.sockets
        assert sockets is not None
        self.url = f"http://127.0.0.1:{sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.close()
        for task in self._tasks:
            task.cancel()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
                return
            lines = head.decode("latin-1").split("\r\n")
            method, path, _version = lines[0].split(" ", 2)
            headers: dict[str, str] = {}
            for line in lines[1:]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
            length = headers.get("content-length")
            if length and length.isdigit():
                try:
                    await reader.readexactly(int(length))
                except (asyncio.IncompleteReadError, OSError):
                    pass
            request = _ServedRequest(method=method, path=path, headers=headers)
            self.requests.append(request)
            try:
                await self._handler(request, writer)
            except (OSError, RuntimeError):
                pass  # the client hung up mid-response (cap/timeout legs)
        finally:
            if task is not None:
                self._tasks.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, RuntimeError):
                pass


async def _serve_page(request: _ServedRequest, writer: asyncio.StreamWriter) -> None:
    _respond(writer, 200, "OK", {"content-type": _CONTENT_TYPE}, _PAGE.encode())


# ---------------------------------------------------------------------------
# §4-1: the three formats over a real fetch
# ---------------------------------------------------------------------------


async def test_webfetch_markdown_conversion_shapes_the_output_contract(tmp_path: Path) -> None:
    async with _Loopback(_serve_page) as loopback:
        registry = _registry(_adapter())
        outcome = await _invoke(registry, tmp_path, {"url": loopback.url, "format": "markdown"})

        assert outcome.state == "succeeded"
        output = _one_text(outcome)
        assert "# Docs" in output
        assert "Some [intro](/docs) text." in output
        assert "```python\nprint(1)\n```" in output
        assert "- one\n- two" in output
        assert "Tail" in output
        # The hidden subtrees never leak into the conversion.
        assert "alert" not in output
        assert "nojs" not in output
        assert "color:red" not in output
        assert outcome.structured_output == {
            "url": loopback.url,
            "contentType": _CONTENT_TYPE,
            "format": "markdown",
            "output": output,
        }

        # The metadata face rides the adapter's own ToolResult (the Invoker's
        # ToolOutcome terminal carries no metadata field).
        direct = await _adapter().execute(_context(tmp_path), {"url": loopback.url})
        assert direct.metadata == {"contentType": _CONTENT_TYPE}
        assert [part.text for part in direct.content if isinstance(part, ToolTextContent)] == [output]
        assert direct.structured_output == {
            "url": loopback.url,
            "contentType": _CONTENT_TYPE,
            "format": "markdown",
            "output": output,
        }


async def test_webfetch_text_format_hides_the_hidden_subtrees(tmp_path: Path) -> None:
    async with _Loopback(_serve_page) as loopback:
        registry = _registry(_adapter())
        outcome = await _invoke(registry, tmp_path, {"url": loopback.url, "format": "text"})

        assert outcome.state == "succeeded"
        output = _one_text(outcome)
        # The visible text nodes concatenated (the extractTextFromHTML face:
        # no inter-element whitespace in the fixture, then strip()).
        assert output == "DocsSome intro text.print(1)onetwoTail"
        assert "alert" not in output
        assert "nojs" not in output
        assert "color:red" not in output
        assert outcome.structured_output == {
            "url": loopback.url,
            "contentType": _CONTENT_TYPE,
            "format": "text",
            "output": output,
        }


async def test_webfetch_html_format_passes_the_page_through_verbatim(tmp_path: Path) -> None:
    async with _Loopback(_serve_page) as loopback:
        registry = _registry(_adapter())
        outcome = await _invoke(registry, tmp_path, {"url": loopback.url, "format": "html"})

        assert outcome.state == "succeeded"
        assert _one_text(outcome) == _PAGE
        assert outcome.structured_output == {
            "url": loopback.url,
            "contentType": _CONTENT_TYPE,
            "format": "html",
            "output": _PAGE,
        }


async def test_webfetch_passes_non_html_content_types_through_untouched(tmp_path: Path) -> None:
    async def serve(request: _ServedRequest, writer: asyncio.StreamWriter) -> None:
        _respond(writer, 200, "OK", {"content-type": "text/plain"}, b"plain body text")

    async with _Loopback(serve) as loopback:
        registry = _registry(_adapter())
        outcome = await _invoke(registry, tmp_path, {"url": loopback.url})

        assert outcome.state == "succeeded"
        # The convert gate keys on the raw content-type substring: without
        # "text/html" even the markdown format is a verbatim passthrough.
        assert _one_text(outcome) == "plain body text"
        assert outcome.structured_output == {
            "url": loopback.url,
            "contentType": "text/plain",
            "format": "markdown",
            "output": "plain body text",
        }


# ---------------------------------------------------------------------------
# §4-2: defaults and bounds
# ---------------------------------------------------------------------------


async def test_webfetch_absent_format_defaults_to_markdown(tmp_path: Path) -> None:
    async with _Loopback(_serve_page) as loopback:
        registry = _registry(_adapter())
        outcome = await _invoke(registry, tmp_path, {"url": loopback.url})

        assert outcome.state == "succeeded"
        output = _one_text(outcome)
        assert "# Docs" in output
        payload = outcome.structured_output
        assert isinstance(payload, dict)
        assert payload["format"] == "markdown"


async def test_webfetch_rejects_bad_timeout_and_format_values(tmp_path: Path) -> None:
    registry = _registry(_adapter())
    url = "http://127.0.0.1:9/x"  # never contacted: every leg fails before the fetch

    # The schema subset carries no lower bound: the adapter's double face.
    zero = await _invoke(registry, tmp_path, {"url": url, "timeout": 0})
    assert "greater than 0" in _failed(zero, CoreErrorCode.VALIDATION_ERROR)

    negative = await _invoke(registry, tmp_path, {"url": url, "timeout": -3})
    assert "greater than 0" in _failed(negative, CoreErrorCode.VALIDATION_ERROR)

    # The schema ``maximum`` arm: rejected by the Invoker before the adapter.
    over = await _invoke(registry, tmp_path, {"url": url, "timeout": 121})
    assert "must be <= 120" in _failed(over, CoreErrorCode.VALIDATION_ERROR)

    # The format literal is not expressible in the strict subset: the
    # adapter re-checks it fail-closed.
    unknown = await _invoke(registry, tmp_path, {"url": url, "format": "bogus"})
    assert "format must be one of" in _failed(unknown, CoreErrorCode.VALIDATION_ERROR)

    # The required-``url`` arm: the Invoker's own schema rejection.
    missing = await _invoke(registry, tmp_path, {"format": "text"})
    assert "required field is missing" in _failed(missing, CoreErrorCode.VALIDATION_ERROR)


# ---------------------------------------------------------------------------
# §4-3: the scheme gate
# ---------------------------------------------------------------------------


async def test_webfetch_rejects_non_http_schemes_with_the_verbatim_sentence(tmp_path: Path) -> None:
    registry = _registry(_adapter())
    outcome = await _invoke(registry, tmp_path, {"url": "ftp://example.com/file"})
    assert (
        _failed(outcome, CoreErrorCode.TOOL_FAILED)
        == "Unable to fetch ftp://example.com/file: URL must use http:// or https://"
    )


# ---------------------------------------------------------------------------
# §4-4: the manual redirect chain
# ---------------------------------------------------------------------------


async def test_webfetch_follows_relative_and_absolute_redirect_locations(tmp_path: Path) -> None:
    async def route(request: _ServedRequest, writer: asyncio.StreamWriter) -> None:
        if request.path == "/start":
            _respond(writer, 302, "Found", {"location": "/mid"}, b"")  # relative
        elif request.path == "/mid":
            host = request.headers["host"]
            _respond(writer, 301, "Moved Permanently", {"location": f"http://{host}/end"}, b"")  # absolute
        else:
            _respond(writer, 200, "OK", {"content-type": "text/plain"}, b"final hop")

    async with _Loopback(route) as loopback:
        registry = _registry(_adapter())
        outcome = await _invoke(registry, tmp_path, {"url": f"{loopback.url}/start"})

        assert outcome.state == "succeeded"
        assert [request.path for request in loopback.requests] == ["/start", "/mid", "/end"]
        assert _one_text(outcome) == "final hop"
        # The payload's url is the ORIGINAL request url, not the final hop.
        assert outcome.structured_output == {
            "url": f"{loopback.url}/start",
            "contentType": "text/plain",
            "format": "markdown",
            "output": "final hop",
        }


async def test_webfetch_stops_after_ten_redirects_with_the_verbatim_sentence(tmp_path: Path) -> None:
    async def route(request: _ServedRequest, writer: asyncio.StreamWriter) -> None:
        host = request.headers["host"]
        _respond(writer, 302, "Found", {"location": f"http://{host}/loop"}, b"")

    async with _Loopback(route) as loopback:
        registry = _registry(_adapter())
        outcome = await _invoke(registry, tmp_path, {"url": f"{loopback.url}/loop"})

        assert (
            _failed(outcome, CoreErrorCode.TOOL_FAILED)
            == f"Unable to fetch {loopback.url}/loop: Too many redirects (more than 10)"
        )
        # The initial request plus the ten followed hops.
        assert len(loopback.requests) == 11


async def test_webfetch_rechecks_the_permission_policy_on_each_hop(tmp_path: Path) -> None:
    async def route(request: _ServedRequest, writer: asyncio.StreamWriter) -> None:
        if request.path == "/hop":
            host = request.headers["host"]
            _respond(writer, 302, "Found", {"location": f"http://{host}/denied"}, b"")
        else:
            _respond(writer, 200, "OK", {"content-type": "text/plain"}, b"never reached")

    async with _Loopback(route) as loopback:
        target = f"{loopback.url}/denied"
        # The hop-check policy carries the deny rule for the redirect target;
        # the registry itself stays policy-less, so the initial URL clears
        # the Invoker gate and only the hop face rejects.
        adapter = _adapter((PermissionRule(action="webfetch__fetch", effect="deny", resources=(target,)),))
        registry = _registry(adapter)
        outcome = await _invoke(registry, tmp_path, {"url": f"{loopback.url}/hop"})

        text = _failed(outcome, CoreErrorCode.TOOL_FAILED)
        assert text.startswith(f"Unable to fetch {loopback.url}/hop: Redirect to {target}")
        assert "denied by the permission policy" in text
        assert "rule for 'webfetch__fetch' -> deny" in text
        # The denied target is never contacted: only the initial hop ran.
        assert [request.path for request in loopback.requests] == ["/hop"]


async def test_webfetch_wraps_http_error_statuses(tmp_path: Path) -> None:
    async def route(request: _ServedRequest, writer: asyncio.StreamWriter) -> None:
        if request.path == "/notfound":
            _respond(writer, 404, "Not Found", {"content-type": "text/plain"}, b"missing")
        else:
            _respond(writer, 500, "Internal Server Error", {"content-type": "text/plain"}, b"boom")

    async with _Loopback(route) as loopback:
        registry = _registry(_adapter())

        missing = await _invoke(registry, tmp_path, {"url": f"{loopback.url}/notfound"})
        assert _failed(missing, CoreErrorCode.TOOL_FAILED) == f"Unable to fetch {loopback.url}/notfound: HTTP 404"

        broken = await _invoke(registry, tmp_path, {"url": f"{loopback.url}/boom"})
        assert _failed(broken, CoreErrorCode.TOOL_FAILED) == f"Unable to fetch {loopback.url}/boom: HTTP 500"


# ---------------------------------------------------------------------------
# §4-5: the mime gate
# ---------------------------------------------------------------------------


async def test_webfetch_rejects_image_and_file_content_types(tmp_path: Path) -> None:
    async def route(request: _ServedRequest, writer: asyncio.StreamWriter) -> None:
        if request.path == "/image":
            _respond(writer, 200, "OK", {"content-type": "image/png"}, b"\x89PNG stub")
        else:
            _respond(writer, 200, "OK", {"content-type": "application/octet-stream"}, b"\x00\x01")

    async with _Loopback(route) as loopback:
        registry = _registry(_adapter())

        image = await _invoke(registry, tmp_path, {"url": f"{loopback.url}/image"})
        assert (
            _failed(image, CoreErrorCode.TOOL_FAILED)
            == f"Unable to fetch {loopback.url}/image: Unsupported fetched image content type: image/png"
        )

        binary = await _invoke(registry, tmp_path, {"url": f"{loopback.url}/binary"})
        assert (
            _failed(binary, CoreErrorCode.TOOL_FAILED)
            == f"Unable to fetch {loopback.url}/binary: Unsupported fetched file content type: application/octet-stream"
        )


async def test_webfetch_allows_the_svg_xml_image_exemption(tmp_path: Path) -> None:
    async def serve(request: _ServedRequest, writer: asyncio.StreamWriter) -> None:
        _respond(writer, 200, "OK", {"content-type": "image/svg+xml"}, _SVG.encode())

    async with _Loopback(serve) as loopback:
        registry = _registry(_adapter())
        outcome = await _invoke(registry, tmp_path, {"url": loopback.url})

        assert outcome.state == "succeeded"
        # Exempt from the image rejection and textual via ``+xml``; without
        # "text/html" in the content type the convert gate passes it through.
        assert _one_text(outcome) == _SVG
        payload = outcome.structured_output
        assert isinstance(payload, dict)
        assert payload["contentType"] == "image/svg+xml"


# ---------------------------------------------------------------------------
# §4-6: the byte cap and the whole-chain timeout
# ---------------------------------------------------------------------------


async def test_webfetch_rejects_a_declared_body_over_the_byte_cap(tmp_path: Path) -> None:
    async def serve(request: _ServedRequest, writer: asyncio.StreamWriter) -> None:
        # The declared content-length is prechecked before any body byte is
        # read (the http-body.ts caller face), so the body stays empty.
        _respond(
            writer,
            200,
            "OK",
            {"content-type": "text/plain", "content-length": str(WEBFETCH_MAX_RESPONSE_BYTES + 1)},
            b"",
        )

    async with _Loopback(serve) as loopback:
        registry = _registry(_adapter())
        outcome = await _invoke(registry, tmp_path, {"url": loopback.url})
        assert (
            _failed(outcome, CoreErrorCode.TOOL_FAILED)
            == f"Unable to fetch {loopback.url}: Response too large (exceeds 5242880 byte limit)"
        )


async def test_webfetch_rejects_a_streamed_body_over_the_byte_cap(tmp_path: Path) -> None:
    async def serve(request: _ServedRequest, writer: asyncio.StreamWriter) -> None:
        # No content-length: the cap fires on the accumulated stream.
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n")
        chunk = b"x" * 65536
        for _ in range(WEBFETCH_MAX_RESPONSE_BYTES // len(chunk) + 1):
            writer.write(chunk)
            await writer.drain()

    async with _Loopback(serve) as loopback:
        registry = _registry(_adapter())
        outcome = await _invoke(registry, tmp_path, {"url": loopback.url})
        assert (
            _failed(outcome, CoreErrorCode.TOOL_FAILED)
            == f"Unable to fetch {loopback.url}: Response too large (exceeds 5242880 byte limit)"
        )


async def test_webfetch_times_the_whole_chain_out(tmp_path: Path) -> None:
    async def serve(request: _ServedRequest, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(1.0)
        _respond(writer, 200, "OK", {"content-type": "text/plain"}, b"too late")

    async with _Loopback(serve) as loopback:
        registry = _registry(_adapter())
        outcome = await _invoke(registry, tmp_path, {"url": loopback.url, "timeout": 0.2})
        assert _failed(outcome, CoreErrorCode.TOOL_FAILED) == f"Unable to fetch {loopback.url}: Request timed out"


# ---------------------------------------------------------------------------
# §4-7: the Invoker's initial-URL deny
# ---------------------------------------------------------------------------


async def test_webfetch_denies_the_initial_url_before_any_network_io(tmp_path: Path) -> None:
    async def serve(request: _ServedRequest, writer: asyncio.StreamWriter) -> None:
        _respond(writer, 200, "OK", {"content-type": "text/plain"}, b"never fetched")

    async with _Loopback(serve) as loopback:
        url = f"{loopback.url}/page"
        # The gate policy carries the deny rule for the exact initial URL;
        # the adapter's own hop policy is ruleless, so only the Invoker's
        # gate (the TS permission.assert face) can produce this terminal.
        policy = PermissionPolicy(
            boot_id="boot_webfetch_deny",
            rules=(PermissionRule(action="webfetch__fetch", effect="deny", resources=(url,)),),
        )
        registry = _registry(_adapter(), policy=policy)
        outcome = await _invoke(registry, tmp_path, {"url": url})

        assert outcome.state == "denied"
        assert outcome.effect_status == "not_started"
        assert outcome.error is not None and outcome.error.code is CoreErrorCode.PERMISSION_DENIED
        text = _one_text(outcome)
        assert 'Tool "webfetch__fetch" was denied by the permission policy' in text
        assert "rule for 'webfetch__fetch' -> deny" in text
        assert outcome.structured_output is None
        # The deny runs before any HTTP: the server was never contacted.
        assert loopback.requests == []


# ---------------------------------------------------------------------------
# §4-8: the policy and registration pins
# ---------------------------------------------------------------------------


def test_webfetch_policy_and_spec_pins() -> None:
    assert _DEFAULT_EFFECTS["webfetch__fetch"] == "ask"
    # The git-trio precedent: a per-action default row, not an ask namespace.
    assert "webfetch" not in _ASK_NAMESPACES
    # The permission.assert resource binding is the url (the TS anchor face).
    assert extract_resources("webfetch__fetch", {"url": "https://example.com/x"}) == ("https://example.com/x",)
    assert extract_resources("webfetch__fetch", {"url": ""}) == ()

    assert WEBFETCH_SPEC.namespace == "webfetch"
    assert WEBFETCH_SPEC.name == "fetch"
    assert WEBFETCH_SPEC.version == 1
    assert WEBFETCH_SPEC.side_effects is True
    assert WEBFETCH_SPEC.idempotency_support is True
    assert WEBFETCH_SPEC.supported_content_types == ["text"]


async def test_webfetch_unbound_adapter_fails_closed_as_a_wiring_defect(tmp_path: Path) -> None:
    with pytest.raises(ToolInvokeError, match="not bound to a permission policy"):
        await WebFetchAdapter().execute(_context(tmp_path), {"url": "http://127.0.0.1:9/x"})


# ---------------------------------------------------------------------------
# §4-9: the seat-composed error arms
# ---------------------------------------------------------------------------


async def test_webfetch_wraps_transport_errors_as_request_failed(tmp_path: Path) -> None:
    registry = _registry(_adapter())
    # Loopback port 1: nothing listens there, so the connection is refused
    # synchronously (the bounded timeout keeps a filtered environment from
    # hanging instead of refusing).
    outcome = await _invoke(registry, tmp_path, {"url": "http://127.0.0.1:1/x", "timeout": 10})
    text = _failed(outcome, CoreErrorCode.TOOL_FAILED)
    assert text.startswith("Unable to fetch http://127.0.0.1:1/x: Request failed")


async def test_webfetch_retries_a_cloudflare_challenge_once_with_the_opencode_ua(tmp_path: Path) -> None:
    async def serve(request: _ServedRequest, writer: asyncio.StreamWriter) -> None:
        if request.headers.get("user-agent") == "opencode":
            _respond(writer, 200, "OK", {"content-type": "text/html"}, b"<html><body><p>Real page</p></body></html>")
        else:
            _respond(
                writer,
                403,
                "Forbidden",
                {"content-type": "text/html", "cf-mitigated": "challenge"},
                b"challenge page",
            )

    async with _Loopback(serve) as loopback:
        registry = _registry(_adapter())
        outcome = await _invoke(registry, tmp_path, {"url": loopback.url})

        assert outcome.state == "succeeded"
        assert "Real page" in _one_text(outcome)
        assert len(loopback.requests) == 2
        assert "OpenCode-User" in loopback.requests[0].headers["user-agent"]
        assert loopback.requests[1].headers["user-agent"] == "opencode"
