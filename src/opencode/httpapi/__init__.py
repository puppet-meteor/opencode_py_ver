"""Local single-user HTTP server face (P2-S3 / M5-A, PHASE2-MODULES §7 L99-112).

Public surface: :func:`create_app` (the FastAPI transport over one
:class:`~opencode.app.App` composition — six §7 command routes) and
:func:`serve` (the production host: env face + assembly + one programmatic
``uvicorn.Server``, single ASGI worker, loopback-only).

Deferred-optional gate (PHASE2-MODULES §2 L41 / p2-slices §4 L142): the
server extras (``pip install 'opencode-py[server]'``) are the only thing that
pulls fastapi/uvicorn in, and importing this package without them raises
:exc:`ServerExtrasMissingError` (code ``UnsupportedCapability``, the §8
closed registry reused, no new code) instead of a raw ``ImportError`` — core
never imports this package, so core startup is unaffected either way.
"""

from __future__ import annotations

from opencode.contracts.errors import CoreErrorCode
from opencode.storage import StoreError

__all__ = ["ServerExtrasMissingError", "create_app", "serve"]


class ServerExtrasMissingError(StoreError):
    """The optional server extras are absent (§2 L41: call-time face).

    Reuses the registered ``UnsupportedCapability`` code — the §8 registry is
    closed and this adds nothing; the wire shape comes free from
    :class:`~opencode.storage.StoreError`.
    """

    code = CoreErrorCode.UNSUPPORTED_CAPABILITY


def _require_server_extras() -> None:
    """The deferred-import gate: fastapi/uvicorn must be importable.

    Runs at package import, before the submodules that import fastapi at
    module level, so the public face fails with the §8-shaped
    ``UnsupportedCapability`` instead of a raw ``ImportError``.
    """
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as error:
        raise ServerExtrasMissingError(
            "the HTTP server face requires the optional 'server' extras "
            "(install with: pip install 'opencode-py[server]'); the core library works without them",
        ) from error


_require_server_extras()

from opencode.httpapi.app import create_app  # noqa: E402
from opencode.httpapi.serving import serve  # noqa: E402
