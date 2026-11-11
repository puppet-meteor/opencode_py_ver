"""Host-facing file-change notification service (P2-S20 M2 watcher W1).

Public surface: :class:`~opencode.watcher.service.WatcherService` (the
notify-only file watcher over watchfiles ``awatch``) and
:class:`~opencode.watcher.service.ChangeNotification` (the merged,
deduplicated change record the host consumes).

Deferred-optional gate (PHASE2-MODULES 2 L41 / p2-slices 4 L142, the httpapi
precedent): the watcher extras (``pip install 'opencode-py[watcher]'``) are
the only thing that pulls watchfiles in, and importing this package without
them raises :exc:`WatcherExtrasMissingError` (code ``UnsupportedCapability``,
the section 8 closed registry reused, no new code) instead of a raw
``ImportError`` — core never imports this package, so core startup is
unaffected either way.

CORE-CONTRACTS 6.4 ruling 1 (T-A-016, merged #193): the watcher's
deduplicated/merged file-change notification is a HOST-FACE SIGNAL ONLY —
it is not registered as a py.* durable or ephemeral event, never enters the
durable log, the Bus or any projection path, and produces no state change at
all (a lost notification is harmless: the next change or an explicit reload
re-supplies it). The host embedder is the sole consumer.
"""

from __future__ import annotations

from opencode.contracts.errors import CoreErrorCode
from opencode.storage import StoreError

__all__ = ["ChangeNotification", "WatcherExtrasMissingError", "WatcherService"]


class WatcherExtrasMissingError(StoreError):
    """The optional watcher extras are absent (PHASE2-MODULES 2 L41: call-time face).

    Reuses the registered ``UnsupportedCapability`` code — the section 8
    registry is closed and this adds nothing; the wire shape comes free from
    :class:`~opencode.storage.StoreError`.
    """

    code = CoreErrorCode.UNSUPPORTED_CAPABILITY


def _require_watcher_extras() -> None:
    """The deferred-import gate: watchfiles must be importable.

    Runs at package import, before the service submodule that imports
    watchfiles at module level, so the public face fails with the
    section-8-shaped ``UnsupportedCapability`` instead of a raw
    ``ImportError``.
    """
    try:
        import watchfiles  # noqa: F401
    except ImportError as error:
        raise WatcherExtrasMissingError(
            "the file-watcher face requires the optional 'watcher' extras "
            "(install with: pip install 'opencode-py[watcher]'); the core library works without them",
        ) from error


_require_watcher_extras()

from opencode.watcher.service import ChangeNotification, WatcherService  # noqa: E402
