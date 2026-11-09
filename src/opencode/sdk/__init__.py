"""Python client SDK over the local single-user HTTP face (P2-S5 / M5-C).

PHASE2-MODULES §7 L99/L103/L110/L112 — the single-user client SDK + server: a
programmatic client (an external UI, a test harness, another tool) talks to
the one-process server over the seven §7 routes — create session, prompt and
read the run's terminal state, cancel, run status, message pages, permission
replies, and the durable SSE stream. The acceptance faces (disconnect and
reconnect over the Last-Event-ID cursor, duplicate request_id, error codes,
unauthenticated requests, approval, shutdown) are integration tests over the
real wire (``tests/integration/test_sdk_client.py``) — the SDK itself is the
programmatic access face they exercise.

The package is a pure client: stdlib ``urllib`` transport, core model reuse
(``session.service`` / ``storage.repos`` / ``contracts``), and **no**
fastapi/uvicorn/``opencode.httpapi`` import — it stays on the core dependency
surface (L101: the SDK directory is new; the runner is not rewritten). The
server's runtime ``/openapi.json`` is the single OpenAPI source: no static
spec ships in the repo, because a copy would be a second source that drifts.
"""

from __future__ import annotations

from opencode.sdk.client import DEFAULT_BASE_URL, DEFAULT_TIMEOUT_S, OpencodeClient
from opencode.sdk.errors import (
    OpencodeApiError,
    OpencodeClientError,
    OpencodeConnectionError,
    OpencodeStreamError,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_S",
    "OpencodeApiError",
    "OpencodeClient",
    "OpencodeClientError",
    "OpencodeConnectionError",
    "OpencodeStreamError",
]
