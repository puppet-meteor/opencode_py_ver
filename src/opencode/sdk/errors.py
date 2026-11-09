"""SDK exception faces (P2-S5 / M5-C, PHASE2-MODULES §7 L110).

Three concrete failures cross the SDK seam, one per wire face:

* :class:`OpencodeApiError` — a non-2xx answer: the HTTP status plus the §8
  CoreError wire body (``code``/``message``/``retryable``/``details``), so
  callers branch on ``retryable`` (a 429 SlowConsumer retries later; a 401,
  a 404 or an IdempotencyConflict does not). An unauthenticated request is
  the same face — the server answers 401 with the same §8 body.
* :class:`OpencodeStreamError` — the SSE terminal ``event: error`` frame: the
  §8 body rides the data line of an already-200 response, so there is no
  meaningful HTTP status to carry; the non-retryable faces should not be
  blindly reconnected (the 056 note carries over).
* :class:`OpencodeConnectionError` — the request never got an answer (server
  down, socket broken, read timeout): the SDK holds no state to retry
  against, so the cause is chained and the caller decides.

The three share :class:`OpencodeClientError`, so ``except OpencodeClientError``
catches everything this SDK raises on its own behalf.
"""

from __future__ import annotations

from opencode.contracts.errors import CoreError

__all__ = [
    "OpencodeApiError",
    "OpencodeClientError",
    "OpencodeConnectionError",
    "OpencodeStreamError",
]


class OpencodeClientError(Exception):
    """Base of every failure the SDK raises on its own behalf."""


class OpencodeApiError(OpencodeClientError):
    """A non-2xx answer: the HTTP status plus the §8 CoreError wire body.

    ``core`` is ``None`` exactly when the error body did not carry the §8
    shape (an uncaught exception falls through to the ASGI default 500 plain
    text); callers must check before branching on ``core.retryable``.
    """

    def __init__(self, status_code: int, core: CoreError | None) -> None:
        detail = f"{core.code.value}: {core.message}" if core is not None else "no §8 CoreError body"
        super().__init__(f"HTTP {status_code} ({detail})")
        self.status_code = status_code
        self.core = core


class OpencodeStreamError(OpencodeClientError):
    """The terminal SSE ``event: error`` frame (the §8 body, no HTTP status).

    The 200 is already on the wire when the frame is sent, so a status code
    would be fiction; the stream ends with this exception instead.
    """

    def __init__(self, core: CoreError) -> None:
        super().__init__(f"stream error frame ({core.code.value}: {core.message})")
        self.core = core


class OpencodeConnectionError(OpencodeClientError):
    """The request never got an answer (server down, socket broken, read timeout)."""
