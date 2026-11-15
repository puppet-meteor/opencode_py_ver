"""Builtin ``webfetch__fetch`` tool (P7-S3; T-C1-102, ticket §1/§3).

Fetch content from an HTTP or HTTPS URL and return it as text, markdown
or HTML (markdown default), adapted from TS
``packages/core/src/tool/plugin/webfetch.ts``: a read-only GET whose
redirects are followed MANUALLY (one request per hop) so every new URL
is re-checked against the permission policy before it is ever contacted
(the TS comment above its ``execute`` helper is the design statement).

TS mapping / deviations, quantified (progress file):

- The TS permission faces split across two tiers: ``permission.assert``
  (the full ask/approval protocol) runs BEFORE any HTTP on the initial
  URL, and ``permission.assertNotDenied`` re-checks every redirect hop.
  PY maps them onto the two existing surfaces: the Invoker's §5.1 L259
  policy/approval gate IS the assert face (it already runs before every
  adapter, with ``resources`` bound to the URL through
  :func:`opencode.tools.policy.extract_resources`), and this adapter
  carries the assertNotDenied face itself: the host binds the SAME
  :class:`~opencode.tools.policy.PermissionPolicy` the Invoker uses
  (:meth:`WebFetchAdapter.bind`) and each hop evaluates it fail-closed
  on ``deny`` only (ask/allow pass -- a redirect must not re-trigger an
  approval the initial URL already cleared). Anchor-order note: TS
  checks the URL scheme before the initial assert; PY's scheme check
  lives in the adapter, after the Invoker gate -- a recorded mechanism
  correction (the gate is host-tier and precedes every adapter; both
  orders still fail closed before any network I/O).
- The redirect chain is ported shape-for-shape: statuses
  {301,302,303,307,308}, ``MAX_REDIRECTS = 10`` with the verbatim
  sentence, relative locations resolved against the current URL
  (``urljoin`` = ``new URL(location, url)``) and the scheme re-asserted
  per hop. Cloudflare face: a 403 with ``cf-mitigated: challenge``
  retries the WHOLE chain once with the UA ``"opencode"`` (the TS
  ``catchIf(isCloudflareChallenge, ...)`` arm).
- Body collection mirrors ``tool/http-body.ts``
  ``collectBoundedResponseBody``: the declared ``content-length`` is
  prechecked against ``MAX_RESPONSE_BYTES`` (5242880, shared with the
  markdown converter's cap like the TS shared constant), then the
  streamed chunks are capped per-chunk; the verbatim over-limit
  sentence carries the number. Fail-closed mime gate in the TS order:
  image attachments rejected (svg+xml and vnd.fastbidsheet exempt),
  non-textual types rejected (``Mime.isTextual`` criteria set,
  ``mime.ts`` verbatim), unknown mime = reject.
- The timeout is the TS whole-chain face: ``asyncio.wait_for`` over
  fetch+mime+collect (the decode/convert steps stay outside it, the
  TS ``Effect.timeoutOrElse`` boundary), default 30 s, schema-capped at
  120, answering the verbatim ``Request timed out`` sentence. httpx2's
  own 5 s default timeout is disabled (``Timeout(None)``) so wait_for
  is the only deadline; ``follow_redirects=False`` is the manual chain.
- The HTML converters are DIFFERENT-BY-DESIGN (ticket §3-3): the TS
  ``html-markdown.ts`` is a 658-line state machine over the
  htmlparser2 tokenizer with no 1:1 Python counterpart, so PY is a
  minimal own-shape converter over the stdlib ``html.parser`` covering
  the ticket's core surfaces (headings, paragraphs, links, fenced code
  blocks with ``class="language-x"`` detection, lists with nested
  indent) and dropping the rest (tables/blockquotes/details/summary/
  images/hr/dl/inline markers; ``br`` collapses to a space) --
  recorded with the full omission list in the progress file. The
  ``MAX_MARKDOWN_BYTES`` cap survives as a code-point-safe UTF-8 byte
  slice (the TS ``sliceBytes`` binary-search semantics). Text
  extraction is the verbatim ``extractTextFromHTML`` port: the
  six-tag skip stack (script/style/noscript/iframe/object/embed) with
  one Python correction -- void tags never enter the stack because
  ``html.parser`` fires no endtag for them (htmlparser2 closes voids
  itself), so an ``<embed>`` inside a skipped subtree cannot leak the
  counter.
- Output face verbatim: ``{url, contentType, format, output}`` as
  ``structured_output``, the converted text as the model-visible
  content, ``{"contentType": ...}`` as metadata. Every fetch-leg error
  is the structured ToolFailure wrapper ``Unable to fetch {url}`` with
  the TS detail sentence; the ``HTTP {status}`` and network-failure
  details are seat-composed (progress file).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import httpx2

from opencode.contracts.errors import CoreErrorCode
from opencode.contracts.messages import JsonObject, JsonValue, ToolTextContent
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.tools.builtin.read import _failure
from opencode.tools.invoker import ToolInvokeError
from opencode.tools.policy import PermissionPolicy

if TYPE_CHECKING:
    # Annotation-only (the question.py house rule): the runtime context
    # flows in through the Invoker.
    from opencode.tools.invoker import ToolContext

WEBFETCH_NAMESPACE = "webfetch"
WEBFETCH_NAME = "fetch"
WEBFETCH_VERSION = 1

#: html-markdown.ts ``MAX_MARKDOWN_BYTES``: the shared cap of the converted
#: markdown output and (as ``MAX_RESPONSE_BYTES``, the webfetch.ts alias) of
#: the collected response body.
WEBFETCH_MAX_MARKDOWN_BYTES = 5 * 1024 * 1024
WEBFETCH_MAX_RESPONSE_BYTES = WEBFETCH_MAX_MARKDOWN_BYTES

#: webfetch.ts ``DEFAULT_TIMEOUT_SECONDS`` / ``MAX_TIMEOUT_SECONDS``: the
#: whole-chain deadline default and the schema ``maximum`` bound.
WEBFETCH_DEFAULT_TIMEOUT_SECONDS = 30
WEBFETCH_MAX_TIMEOUT_SECONDS = 120

#: webfetch.ts ``MAX_REDIRECTS``: the manual chain hops before the verbatim
#: over-limit sentence fires (11 requests total: the initial one plus ten
#: redirects).
WEBFETCH_MAX_REDIRECTS = 10

#: webfetch.ts description, verbatim (two paragraphs).
WEBFETCH_DESCRIPTION = (
    "Fetch content from an HTTP or HTTPS URL and return it as text, markdown, or HTML. "
    "Markdown is the default.\n"
    "\n"
    "Use a more targeted tool when one is available. This tool is read-only. Large text results "
    "may be replaced with a preview while the complete output is retained in managed storage."
)

#: Input fields verbatim (webfetch.ts ``Input``): the ``format`` literal and
#: the ``timeout`` bounds cannot be expressed in the Invoker's strict schema
#: subset (no enum, no exclusiveMinimum), so the adapter re-checks both
#: fail-closed (the ``timeout`` ``maximum`` IS expressible and stays in the
#: schema); the absent ``format`` key defaults to ``"markdown"`` (the TS
#: decoding-default-key face).
WEBFETCH_INPUT_SCHEMA: JsonObject = {
    "type": "object",
    "description": (
        "Fetch content from an HTTP or HTTPS URL and return it as text, markdown, or HTML; markdown is the default."
    ),
    "properties": {
        "url": {
            "type": "string",
            "description": "The HTTP or HTTPS URL to fetch content from",
        },
        "format": {
            "type": "string",
            "description": "The format to return the content in. Defaults to markdown.",
        },
        "timeout": {
            "type": "number",
            "maximum": WEBFETCH_MAX_TIMEOUT_SECONDS,
            "description": f"Optional timeout in seconds (maximum: {WEBFETCH_MAX_TIMEOUT_SECONDS})",
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}

WEBFETCH_SPEC = ToolSpec(
    namespace=WEBFETCH_NAMESPACE,
    name=WEBFETCH_NAME,
    version=WEBFETCH_VERSION,
    description=WEBFETCH_DESCRIPTION,
    input_schema=WEBFETCH_INPUT_SCHEMA,
    supported_content_types=["text"],
    # An outbound HTTP(S) request is observable off-host even though the
    # tool is read-only (ticket §3-5: the ask default documents the real
    # external effect).
    side_effects=True,
    # A GET is convergent: the same URL answers the same body (the honest
    # read-only face; the provider may still see server-side variation).
    idempotency_support=True,
)

#: The request-header faces, verbatim (webfetch.ts ``headers``/``acceptHeader``):
#: the OpenCode UA, the per-format Accept q-order and the Accept-Language.
_OPENCODE_USER_AGENT = (
    "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OpenCode-User/1.0; +https://opencode.ai"
)
#: The Cloudflare challenge retry UA (webfetch.ts ``execute`` catchIf arm).
_CLOUDFLARE_RETRY_USER_AGENT = "opencode"
_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
_ACCEPT_HEADERS: dict[str, str] = {
    "markdown": "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1",
    "text": "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1",
    "html": "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, text/markdown;q=0.7, */*;q=0.1",
}

#: webfetch.ts ``redirectStatuses``: the statuses whose ``location`` is followed.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: The adapter-side format enum (the TS Schema.Literals face).
_FORMATS = frozenset({"text", "markdown", "html"})


def _request_headers(format: str, user_agent: str) -> dict[str, str]:
    """The per-request header set (webfetch.ts ``headers``, verbatim values)."""
    return {
        "User-Agent": user_agent,
        "Accept": _ACCEPT_HEADERS[format],
        "Accept-Language": _ACCEPT_LANGUAGE,
    }


def _assert_http_url(url: str) -> None:
    """webfetch.ts ``assertHttpUrl``: only http/https (verbatim sentence)."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise _FetchError("URL must use http:// or https://")


def _mime_from(content_type: str) -> str:
    """webfetch.ts ``mimeFrom``: the bare lowercased media type."""
    return content_type.split(";", 1)[0].strip().lower()


def _is_image_attachment(mime: str) -> bool:
    """webfetch.ts ``isImageAttachment``: image/* minus the two exemptions."""
    return mime.startswith("image/") and mime != "image/svg+xml" and mime != "image/vnd.fastbidsheet"


def _is_textual(mime: str) -> bool:
    """mime.ts ``Mime.isTextual``, verbatim criteria set (fail-closed)."""
    return (
        not mime
        or mime.startswith("text/")
        or mime == "application/json"
        or mime.endswith("+json")
        or mime == "application/xml"
        or mime.endswith("+xml")
        or mime == "application/javascript"
        or mime == "application/x-javascript"
    )


def _too_large() -> _FetchError:
    """The verbatim collect-cap sentence (http-body.ts caller face)."""
    return _FetchError(f"Response too large (exceeds {WEBFETCH_MAX_RESPONSE_BYTES} byte limit)")


class _FetchError(Exception):
    """One fetch-leg rejection (ticket §8): every instance maps to the
    structured ``Unable to fetch {url}`` ToolFailure wrapper, never a bare
    raise and never a silent failure."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _CloudflareChallengeError(_FetchError):
    """The 403 + ``cf-mitigated: challenge`` face (webfetch.ts
    ``isCloudflareChallenge``): the chain driver retries the WHOLE chain once
    with the ``"opencode"`` UA, so the detail sentence never surfaces."""

    def __init__(self) -> None:
        # No-arg by design: the chain driver consumes this error internally
        # before any sentence could surface, but the fail-closed wrapper
        # contract keeps the ``detail`` field shaped.
        super().__init__("Cloudflare challenge")


def _make_hop_check(policy: PermissionPolicy, context: ToolContext) -> Callable[[str], None]:
    """The TS ``permission.assertNotDenied`` face: each redirect hop evaluates
    the SAME policy the Invoker gated the initial URL with and fails closed on
    ``deny`` only -- ask/allow pass (the hop must not re-run the approval
    protocol the initial URL already cleared)."""

    def check(url: str) -> None:
        decision = policy.evaluate(
            session_id=context.session_id,
            action=f"{WEBFETCH_NAMESPACE}__{WEBFETCH_NAME}",
            resources=(url,),
            # Audit-only field: a redirect hop never activates a saved
            # decision (its binding is the initial URL's approval, TS
            # assertNotDenied parity), so no hash is recorded here.
            input_hash="",
            working_dir=context.working_dir,
            side_effects=True,
        )
        if decision.effect == "deny":
            raise _FetchError(f"Redirect to {url} was denied by the permission policy: {decision.reason}")

    return check


async def _follow(
    client: httpx2.AsyncClient,
    check: Callable[[str], None],
    url: str,
    format: str,
    user_agent: str,
    redirects: int = 0,
) -> httpx2.Response:
    """The manual redirect chain (webfetch.ts ``execute``, shape-for-shape).

    One request per hop; a redirect status with a ``location`` resolves the
    target against the current URL, re-asserts the scheme, re-checks the
    policy and recurses. Non-redirect responses below 400 pass through (the
    TS ``filterStatusOk`` boundary: only 4xx/5xx are rejections, and the
    exact ``HTTP {status}`` detail is the seat-composed PY sentence).
    """
    request = client.build_request("GET", url, headers=_request_headers(format, user_agent))
    response = await client.send(request, stream=True)
    location = response.headers.get("location", "")
    if response.status_code not in _REDIRECT_STATUSES or not location:
        if response.status_code >= 400:
            cloudflare = response.status_code == 403 and response.headers.get("cf-mitigated") == "challenge"
            await response.aclose()
            if cloudflare:
                raise _CloudflareChallengeError()
            raise _FetchError(f"HTTP {response.status_code}")
        return response
    await response.aclose()
    if redirects >= WEBFETCH_MAX_REDIRECTS:
        raise _FetchError(f"Too many redirects (more than {WEBFETCH_MAX_REDIRECTS})")
    target = urljoin(url, location)
    _assert_http_url(target)
    check(target)
    return await _follow(client, check, target, format, user_agent, redirects + 1)


async def _collect_bounded_body(
    response: httpx2.Response,
    maximum_bytes: int,
    too_large: Callable[[], _FetchError],
) -> bytes:
    """http-body.ts ``collectBoundedResponseBody``, shape-for-shape: the
    declared ``content-length`` is prechecked (an unparsable or negative value
    is treated as undeclared, like the TS parseInt guard), then each streamed
    chunk is capped; the over-limit error comes from the caller's factory."""
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            declared_size = -1
        if declared_size >= 0 and declared_size > maximum_bytes:
            raise too_large()
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            if size + len(chunk) > maximum_bytes:
                raise too_large()
            chunks.append(chunk)
            size += len(chunk)
    finally:
        # Effect-runtime cleanup parity: a rejected or cancelled collect must
        # still release the stream, and a close failure must never mask the
        # original error (CancelledError keeps propagating -- it is a
        # BaseException, outside the suppress).
        try:
            await response.aclose()
        except Exception:  # cleanup only
            pass
    return b"".join(chunks)


async def _attempt(
    client: httpx2.AsyncClient,
    check: Callable[[str], None],
    url: str,
    format: str,
    user_agent: str,
) -> tuple[bytes, str]:
    """Fetch + mime + collect, in the webfetch.ts order: the whole chain
    first (with the one Cloudflare retry of the whole chain), the image and
    textual mime rejections against the raw content-type header, then the
    bounded collect."""
    try:
        response = await _follow(client, check, url, format, user_agent)
    except _CloudflareChallengeError:
        response = await _follow(client, check, url, format, _CLOUDFLARE_RETRY_USER_AGENT)
    content_type = response.headers.get("content-type") or ""
    mime = _mime_from(content_type)
    if _is_image_attachment(mime):
        await response.aclose()
        raise _FetchError(f"Unsupported fetched image content type: {mime}")
    if not _is_textual(mime):
        await response.aclose()
        raise _FetchError(f"Unsupported fetched file content type: {mime}")
    body = await _collect_bounded_body(response, WEBFETCH_MAX_RESPONSE_BYTES, _too_large)
    return body, content_type


async def _fetch(
    policy: PermissionPolicy,
    context: ToolContext,
    url: str,
    format: str,
) -> tuple[bytes, str]:
    """One client per invocation: a fresh ``AsyncClient`` with the httpx2
    default timeout disabled (``Timeout(None)`` -- wait_for is the only
    deadline) and redirects manual, closed with the invocation.

    ``trust_env=False`` is the TS parity face (#256): webfetch.ts rides
    ``FetchHttpClient`` (Node fetch), which never consults environment or
    system proxies on its own, while httpx2's default ``trust_env=True``
    would route the request through the host's configured proxy (a Windows
    system proxy answered a bare loopback refusal with an HTTP 502) --
    the tool's outbound URL is exactly the policy-gated URL, not whatever
    a local proxy decides to answer for it.
    """
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(None), follow_redirects=False, trust_env=False) as client:
        return await _attempt(client, _make_hop_check(policy, context), url, format, _OPENCODE_USER_AGENT)


# ---------------------------------------------------------------------------
# text extraction (webfetch.ts extractTextFromHTML, verbatim port)
# ---------------------------------------------------------------------------

#: The six hidden/raw tags whose whole subtrees are dropped (webfetch.ts).
_TEXT_SKIP_TAGS = frozenset({"script", "style", "noscript", "iframe", "object", "embed"})

#: Python's HTMLParser fires no endtag for void elements, so a void tag must
#: never enter the skip stack (htmlparser2 closes voids itself; an unpaired
#: increment would leak the counter -- the one stdlib correction, progress
#: file). ``embed`` is void AND in the skip set, so the void check runs first.
_VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)


class _TextExtractor(HTMLParser):
    """The skip-stack body: text outside the hidden subtrees, then ``strip()``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_ELEMENTS:
            return
        if self.skip_depth > 0 or tag in _TEXT_SKIP_TAGS:
            self.skip_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # A self-closing tag has no content to hide.
        return

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth == 0:
            self.parts.append(data)


def extract_text_from_html(html: str) -> str:
    """The TS ``extractTextFromHTML`` face: visible text, entities decoded,
    hidden subtrees dropped, whitespace trimmed at both ends."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts).strip()


# ---------------------------------------------------------------------------
# HTML -> markdown (ticket §3-3: minimal own-shape converter, DIFFERENT-BY-DESIGN)
# ---------------------------------------------------------------------------

#: Subtrees the markdown converter drops (the TS converter's skip set).
_MARKDOWN_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "iframe", "object", "embed", "meta", "link", "template", "head"}
)

_HEADING_LEVELS: dict[str, int] = {f"h{level}": level for level in range(1, 7)}


def _language_from_class_value(class_value: str) -> str:
    """The fenced-block language: the first ``language-x`` class token."""
    for token in class_value.split():
        if token.startswith("language-"):
            return token.removeprefix("language-")
    return ""


class _MarkdownConverter(HTMLParser):
    """The minimal converter body (progress file §5-1 for the full surface).

    Event-driven block builder over the stdlib parser: block-level elements
    (headings, paragraphs, fenced pre blocks, list items) each render one
    markdown block; ``a``/``code`` capture inline spans over the same buffer
    stack; skip tags drop their whole subtree; inside ``pre`` every further
    tag is transparent (only a nested ``code`` may donate the fence language).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._blocks: list[str | list[str]] = []  # str = block; list = contiguous list group
        self._orphan: list[str] = []  # text before any block element accumulates here
        self._buffers: list[list[str]] = []  # inline capture stack
        self._kinds: list[tuple[str, str]] = []  # parallel openers: "p"/"a"/"code"/"li"/"h{n}"
        self._lists: list[tuple[str, int]] = []  # (ul|ol, item counter) per nesting level
        self._pre: list[str] | None = None
        self._pre_language = ""

    # -- emit helpers -------------------------------------------------------

    @staticmethod
    def _collapse(parts: list[str]) -> str:
        return " ".join("".join(parts).split())

    def _flush_orphan(self) -> None:
        if self._orphan:
            text = self._collapse(self._orphan)
            self._orphan = []
            if text:
                self._blocks.append(text)

    def _emit_block(self, text: str) -> None:
        self._flush_orphan()
        if text:
            self._blocks.append(text)

    def _emit_list_line(self, line: str) -> None:
        self._flush_orphan()
        # A local bind: the isinstance narrow below only sticks on a local,
        # not on a repeated ``self._blocks[-1]`` subscript.
        group = self._blocks[-1] if self._blocks else None
        if not isinstance(group, list):
            group = []
            self._blocks.append(group)
        group.append(line)

    def _emit_li_line(self) -> None:
        _kind, payload = self._kinds.pop()
        text = self._collapse(self._buffers.pop())
        if not text:
            return
        marker_kind = self._lists[-1][0] if self._lists else "ul"
        indent = "  " * (len(self._lists) - 1)
        marker = "-" if marker_kind == "ul" else f"{payload}."
        self._emit_list_line(f"{indent}{marker} {text}")

    def _enter_pre(self, attribute: dict[str, str | None]) -> None:
        self._pre = []
        self._pre_language = _language_from_class_value(attribute.get("class") or "")

    def _emit_pre(self) -> None:
        body = "".join(self._pre or [])
        self._pre = None
        if body.startswith("\n"):  # the browser <pre> first-newline drop
            body = body[1:]
        body = body.rstrip("\n")
        language = self._pre_language
        self._pre_language = ""
        if body:
            self._emit_block(f"```{language}\n{body}\n```")

    # -- tag handlers -------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._pre is not None:
            # Inside <pre>: only a <code class="language-x"> can still donate
            # the fence language; every other tag is transparent.
            if tag == "code" and not self._pre_language:
                self._pre_language = _language_from_class_value(dict(attrs).get("class") or "")
            return
        if self._skip_depth > 0 or tag in _MARKDOWN_SKIP_TAGS:
            if tag not in _VOID_ELEMENTS:
                self._skip_depth += 1
            return
        attribute = dict(attrs)
        if tag == "pre":
            self._enter_pre(attribute)
            return
        if tag in _HEADING_LEVELS:
            if self._buffers:
                return  # a heading inside an open span/li: transparent text
            self._emit_li_pending()
            self._buffers.append([])
            self._kinds.append((tag, ""))
            return
        if tag == "p":
            if self._buffers:
                return  # a paragraph inside an open span/li: transparent text
            self._emit_li_pending()
            self._buffers.append([])
            self._kinds.append(("p", ""))
            return
        if tag in ("ul", "ol"):
            self._emit_li_pending()
            self._lists.append((tag, 0))
            return
        if tag == "li":
            self._emit_li_pending()
            if not self._lists:
                return  # a bare <li> outside any list: transparent text
            kind, count = self._lists[-1]
            self._lists[-1] = (kind, count + 1)
            self._buffers.append([])
            self._kinds.append(("li", str(count + 1)))
            return
        if tag == "a":
            self._buffers.append([])
            self._kinds.append(("a", attribute.get("href") or ""))
            return
        if tag == "code":
            self._buffers.append([])
            self._kinds.append(("code", ""))
            return
        if tag == "br":
            # DIFFERENT-BY-DESIGN: the TS converter emits a markdown hard
            # break; the minimal face collapses it to a space.
            if self._buffers:
                self._buffers[-1].append(" ")
            else:
                self._orphan.append(" ")
            return
        # Every other tag is transparent; its text flows into the open buffer.

    def _emit_li_pending(self) -> None:
        # A nested list (or a pre block) inside an open <li>: the item's own
        # text is already complete, so its line is emitted NOW and the nested
        # lines follow it in the same contiguous group.
        if self._buffers and self._kinds and self._kinds[-1][0] == "li":
            self._emit_li_line()

    def handle_endtag(self, tag: str) -> None:
        if self._pre is not None:
            if tag == "pre":
                self._emit_pre()
            return
        if self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if not self._kinds:
            return
        kind, payload = self._kinds[-1]
        if tag in _HEADING_LEVELS and kind in _HEADING_LEVELS:
            level = _HEADING_LEVELS[kind]
            self._kinds.pop()
            text = self._collapse(self._buffers.pop())
            self._emit_block(f"{'#' * level} {text}".rstrip())
            return
        if tag == "p" and kind == "p":
            self._kinds.pop()
            self._emit_block(self._collapse(self._buffers.pop()))
            return
        if tag == "a" and kind == "a":
            self._kinds.pop()
            span_text = self._collapse(self._buffers.pop())
            rendered = f"[{span_text}]({payload})" if payload else span_text
            if self._buffers:
                self._buffers[-1].append(rendered)
            else:
                self._orphan.append(rendered)
            return
        if tag == "code" and kind == "code":
            self._kinds.pop()
            span_text = "".join(self._buffers.pop()).strip()
            if self._buffers:
                self._buffers[-1].append(f"`{span_text}`")
            elif span_text:
                self._orphan.append(f"`{span_text}`")
            return
        if tag == "li" and kind == "li":
            self._emit_li_line()
            return
        if tag in ("ul", "ol") and self._lists:
            self._lists.pop()
            return
        # Every other endtag is transparent.

    def handle_data(self, data: str) -> None:
        if self._pre is not None:
            self._pre.append(data)
            return
        if self._skip_depth > 0:
            return
        if self._buffers:
            self._buffers[-1].append(data)
        else:
            self._orphan.append(data)

    def close(self) -> None:
        super().close()
        if self._pre is not None:
            self._emit_pre()
        while self._kinds:  # EOF with unclosed elements: best-effort close
            kind, _payload = self._kinds[-1]
            if kind == "a":
                self.handle_endtag("a")
            elif kind == "code":
                self.handle_endtag("code")
            elif kind == "li":
                self._emit_li_line()
            elif kind == "p":
                self.handle_endtag("p")
            elif kind in _HEADING_LEVELS:
                self.handle_endtag(kind)

    def render(self) -> str:
        self._flush_orphan()
        return "\n\n".join(block if isinstance(block, str) else "\n".join(block) for block in self._blocks).strip()


def _slice_bytes(text: str, maximum_bytes: int) -> str:
    """The longest prefix fitting ``maximum_bytes`` UTF-8 bytes without
    splitting a code point (html-markdown.ts ``sliceBytes`` binary-search
    semantics)."""
    if len(text.encode("utf-8")) <= maximum_bytes:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(text[:middle].encode("utf-8")) <= maximum_bytes:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def convert_html_to_markdown(html: str) -> str:
    """The minimal own-shape converter (ticket §3-3), capped at
    ``WEBFETCH_MAX_MARKDOWN_BYTES`` UTF-8 bytes (the TS shared constant)."""
    converter = _MarkdownConverter()
    converter.feed(html)
    converter.close()
    return _slice_bytes(converter.render(), WEBFETCH_MAX_MARKDOWN_BYTES)


def _convert(content: str, content_type: str, format: str) -> str:
    """webfetch.ts ``convert``: the raw-header substring gate (``text/html``
    present) decides between passthrough and the two converter faces."""
    if "text/html" not in content_type:
        return content
    if format == "markdown":
        return convert_html_to_markdown(content)
    if format == "text":
        return extract_text_from_html(content)
    return content


# ---------------------------------------------------------------------------
# The adapter.
# ---------------------------------------------------------------------------


async def _execute_webfetch(policy: PermissionPolicy, context: ToolContext, input: JsonObject) -> ToolResult:
    # The Invoker strictly validated this input against WEBFETCH_INPUT_SCHEMA
    # before execute (adapter contract, §5.1 L257); the re-checks below are
    # the fail-closed double face for everything the strict subset cannot
    # express (the format enum, the timeout lower bound -- ticket §8).
    url = input.get("url")
    if not isinstance(url, str) or not url:
        return _failure(CoreErrorCode.VALIDATION_ERROR, "url is required and must be a non-empty string")
    format = input.get("format", "markdown")
    if not isinstance(format, str) or format not in _FORMATS:
        return _failure(
            CoreErrorCode.VALIDATION_ERROR,
            f"format must be one of 'text', 'markdown' or 'html', got {format!r}",
        )
    timeout = input.get("timeout", WEBFETCH_DEFAULT_TIMEOUT_SECONDS)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not 0 < timeout <= WEBFETCH_MAX_TIMEOUT_SECONDS
    ):
        return _failure(
            CoreErrorCode.VALIDATION_ERROR,
            f"timeout must be a number greater than 0 and at most {WEBFETCH_MAX_TIMEOUT_SECONDS} seconds",
        )
    try:
        # webfetch.ts assertHttpUrl: the scheme gate runs before any HTTP is
        # issued (the TS order). Inside the try so the wrapper below maps the
        # rejection to the structured fail-closed sentence, never a bare raise.
        _assert_http_url(url)
        # The TS timeout boundary: wait_for covers fetch + mime + collect
        # only; the decode/convert steps stay outside it.
        body, content_type = await asyncio.wait_for(_fetch(policy, context, url, format), timeout=float(timeout))
        content = body.decode("utf-8", errors="replace")  # TextDecoder parity: UTF-8 with replacement
        output = _convert(content, content_type, format)
    except _FetchError as error:
        return _failure(CoreErrorCode.TOOL_FAILED, f"Unable to fetch {url}: {error.detail}")
    except TimeoutError:
        return _failure(CoreErrorCode.TOOL_FAILED, f"Unable to fetch {url}: Request timed out")
    except (httpx2.HTTPError, httpx2.InvalidURL) as error:
        return _failure(CoreErrorCode.TOOL_FAILED, f"Unable to fetch {url}: Request failed: {error}")
    payload: dict[str, JsonValue] = {"url": url, "contentType": content_type, "format": format, "output": output}
    return ToolResult(
        content=[ToolTextContent(type="text", text=output)],
        structured_output=payload,
        metadata={"contentType": content_type},
    )


class WebFetchAdapter:
    """The ``webfetch__fetch`` builtin (§5.1 L253 face: ``spec`` + ``execute``).

    Stateful in exactly one field (the subagent bind precedent): the
    host-bound :class:`~opencode.tools.policy.PermissionPolicy`. The Invoker
    gates the INITIAL url (the TS ``permission.assert`` face) before this
    adapter runs; the adapter re-checks every redirect hop against the SAME
    policy (the TS ``permission.assertNotDenied`` face) -- a redirect to a
    denied URL never makes its request. An unbound adapter is a host wiring
    defect, fail-closed as :class:`ToolInvokeError` (§7 L341: wiring errors
    are infrastructure failures, not model-facing results). No ``roots``
    constructor argument: the tool touches no files.
    """

    def __init__(self) -> None:
        self._policy: PermissionPolicy | None = None

    def bind(self, policy: PermissionPolicy) -> None:
        """Bind the App's permission policy (each host calls this once, after construction)."""
        self._policy = policy

    @property
    def spec(self) -> ToolSpec:
        return WEBFETCH_SPEC

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult:
        policy = self._policy
        if policy is None:
            raise ToolInvokeError(
                "webfetch__fetch is not bound to a permission policy "
                "(host wiring defect: call bind(app.policy) after App construction)"
            )
        return await _execute_webfetch(policy, context, input)
