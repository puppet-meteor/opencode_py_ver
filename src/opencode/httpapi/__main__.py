"""``python -m opencode.httpapi``: run the local single-user HTTP server.

P2-S3 / M5-A (PHASE2-MODULES §7): one server process, one ASGI worker,
loopback-only, single-user token. The package import gate reports
``UnsupportedCapability`` when the optional ``server`` extras are absent.
"""

from __future__ import annotations

from opencode.httpapi import serve


def _main() -> None:
    serve()


if __name__ == "__main__":
    _main()
