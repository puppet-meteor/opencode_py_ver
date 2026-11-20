"""Canonical JSON: the upstream ``core/util/canonical-json.ts`` semantics.

A deterministic serialization for ids and hash keys (upstream consumers:
``Hash.make`` and the ``mcpsub_`` subscription id): object keys sorted by
the JS comparison order (UTF-16 code units -- an astral key sorts BEFORE
U+FFFF, unlike Python's code-point order), compact separators, arrays in
insertion order, scalars via ``json.dumps`` with ``ensure_ascii=False``
(non-ASCII raw, as JSON.stringify).

Recorded deviation: an integral Python float keeps its ``.0`` (``1.0``)
where JSON.stringify emits ``1`` -- no cross-language consumer of py ids
exists, and normalizing would lie about the value's type.
"""

from __future__ import annotations

import json
from typing import Any


def _key_utf16(key: Any) -> bytes:
    """Sort key: the JS ``<`` comparison (UTF-16 code units, big endian)."""
    return str(key).encode("utf-16-be")


def canonical_json(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if value is not None and isinstance(value, dict):
        body = ",".join(
            f"{json.dumps(str(key), ensure_ascii=False)}:{canonical_json(value[key])}"
            for key in sorted(value, key=_key_utf16)
        )
        return "{" + body + "}"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
