"""Integration-test conftest: redirect pytest's temp root to the approved location.

The default pytest temp root ``$TMP/pytest-of-<user>`` is access-denied in this
environment (WinError 5 even for directory listing), while ``$TMP/opencode``
is the pre-approved external temp location. ``TempPathFactory.getbasetemp``
resolves its root lazily -- at the first ``tmp_path`` use, which happens after
conftest import -- and honors the ``PYTEST_DEBUG_TEMPROOT`` environment
variable, so setting it here keeps the standard ``tmp_path`` fixture working
unchanged (same semantics: per-test isolated directories). On machines where
the default root is accessible this redirect is harmless, and an explicitly
exported value always wins (``setdefault``).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

try:
    _BASE = Path(tempfile.gettempdir()) / "opencode" / "pytest-tmp"
    _BASE.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(_BASE))
except OSError:
    pass  # keep pytest's default temp root
