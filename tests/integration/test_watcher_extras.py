"""Watcher W1 extras-guard tests: the missing-extras face reports UnsupportedCapability.

Mirrors the httpapi server-extras precedent (PHASE2-MODULES 2 L41): with the
``watcher`` extra absent, importing the capability fails with
``WatcherExtrasMissingError`` (code ``UnsupportedCapability``, the section 8
closed registry reused, zero new codes) and the message carries the install
guidance; with the extra present (this environment) the public face imports
cleanly and exposes the service types.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from opencode.contracts.errors import CoreErrorCode
from opencode.storage import StoreError
from opencode.watcher import ChangeNotification, WatcherExtrasMissingError, WatcherService


def test_missing_extras_report_unsupported_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing `import watchfiles` raises the 6.4-shaped UnsupportedCapability error.

    The re-import builds a fresh ``opencode.watcher`` module and therefore a
    fresh ``WatcherExtrasMissingError`` class object, so the raise is matched
    through the stable ``StoreError`` base and pinned by type name + code.
    """
    monkeypatch.setitem(sys.modules, "watchfiles", None)
    for module in ("opencode.watcher", "opencode.watcher.service"):
        monkeypatch.delitem(sys.modules, module, raising=False)
    with pytest.raises(StoreError) as excinfo:
        importlib.import_module("opencode.watcher")
    error = excinfo.value
    assert isinstance(error, StoreError)
    assert type(error).__name__ == "WatcherExtrasMissingError"
    assert error.code is CoreErrorCode.UNSUPPORTED_CAPABILITY
    assert "opencode-py[watcher]" in str(error)
    assert "core library works" in str(error)


def test_public_face_with_extras_installed() -> None:
    """Positive face: the package exposes the service types (guard passed at import)."""
    module = importlib.import_module("opencode.watcher")
    assert module.WatcherService is WatcherService
    assert module.ChangeNotification is ChangeNotification
    assert WatcherExtrasMissingError.code is CoreErrorCode.UNSUPPORTED_CAPABILITY
    notification = ChangeNotification(paths=frozenset(), event_types=frozenset())
    assert notification.paths == frozenset()
    assert notification.event_types == frozenset()
