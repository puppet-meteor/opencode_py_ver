"""T-C3-007 contract set: flag chain, canonical-json, shared-cache.

Upstream 2.3.2 ``schema/config/flag.ts`` + ``core/flag.ts`` +
``core/util/canonical-json.ts`` + ``core/shared-cache.ts`` distilled into
the py repo's contract face (doc/plans/port-232-increments.md T-C3-007,
decisions 1-6): RED until the three modules land, GREEN after.

Pinned semantics (upstream-verbatim or banked deviations):

- flag chain -- priority env over config over default; ``OPENCODE_FLAGS``
  items (``key`` / ``-key`` / ``all`` wildcard minus opt-in-only / old
  aliases); ``experimental.<key>`` from the highest-priority layer that
  SETS ``experimental`` (the whole block replaces a lower layer's, never
  merges); ``all_flags: true`` from the highest-priority layer that sets
  it (``false`` falls through, never turns off); default = non-opt-in and
  channel in {local, dev}. Every state carries its source.
- attempts for a failing load = 1 + ``len(retry_delays_s)`` (Effect
  ``times: 3`` with exponential 1s = the (1, 2, 4) default).
- canonical key order = the JS comparison order (UTF-16 code units: an
  astral key sorts BEFORE U+FFFF, unlike Python's code-point order).
- eviction = unread for max(24h, 2*interval) drops even a still-fresh
  entry (a caller refreshing less often than daily never finds its entry
  gone between two refreshes).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from opencode.flags import ALIASES, OPT_IN_ONLY, REGISTRY, WILDCARD, resolve
from opencode.shared_cache import INTERVAL_S, UNREAD_MS, SharedCache, fingerprint, transient
from opencode.util.canonical_json import canonical_json

REPO_ROOT = Path(__file__).resolve().parents[2]

_KEYS = {
    "todo",
    "media_blob",
    "automations",
    "artifacts",
    "video_generate",
    "console_watches",
    "mcp_events",
    "full_codemode",
}

#: Timing (repo discipline: injectable short).
_INTERVAL_S = 0.25
_RETRY_DELAYS_S = (0.01, 0.01, 0.01)


# ---------------------------------------------------------------------------
# the flag registry (schema/config/flag.ts verbatim)
# ---------------------------------------------------------------------------


def test_registry_is_upstream_verbatim() -> None:
    assert set(REGISTRY) == _KEYS
    assert OPT_IN_ONLY == {"full_codemode"}
    assert ALIASES == {"schedules": "automations"}
    assert WILDCARD == "all"
    assert all(isinstance(text, str) and text for text in REGISTRY.values())


def test_default_channel_gate() -> None:
    for channel in ("local", "dev"):
        resolved = resolve([], channel)
        for key in _KEYS - {"full_codemode"}:
            assert resolved[key] == {"enabled": True, "source": "default"}, (channel, key)
        assert resolved["full_codemode"] == {"enabled": False, "source": "default"}
    release = resolve([], "release")
    for key in _KEYS:
        assert release[key] == {"enabled": False, "source": "default"}, key


def test_env_overrides() -> None:
    off = resolve([], "local", "-automations")
    assert off["automations"] == {"enabled": False, "source": "env"}
    assert off["media_blob"] == {"enabled": True, "source": "default"}
    alias = resolve([], "release", "schedules")
    assert alias["automations"] == {"enabled": True, "source": "env"}
    every = resolve([], "release", "all")
    assert every["mcp_events"] == {"enabled": True, "source": "env"}
    assert every["full_codemode"] == {"enabled": False, "source": "default"}
    carve = resolve([], "release", "all,-mcp_events")
    assert carve["mcp_events"] == {"enabled": False, "source": "env"}
    assert carve["automations"] == {"enabled": True, "source": "env"}
    opt_in = resolve([], "local", "full_codemode")
    assert opt_in["full_codemode"] == {"enabled": True, "source": "env"}
    spaced = resolve([], "release", " automations , -media_blob ")
    assert spaced["automations"]["enabled"] is True
    assert spaced["media_blob"]["enabled"] is False


def test_config_layers() -> None:
    config = resolve([{"experimental": {"mcp_events": True}}], "release")
    assert config["mcp_events"] == {"enabled": True, "source": "config"}
    # the highest-priority layer that SETS experimental replaces the whole
    # block -- a lower layer's experimental keys never leak through
    replaced = resolve([{"experimental": {"mcp_events": True}}, {"experimental": {"media_blob": False}}], "release")
    assert replaced["mcp_events"] == {"enabled": False, "source": "default"}
    assert replaced["media_blob"] == {"enabled": False, "source": "config"}
    aliased = resolve([{"experimental": {"schedules": True}}], "release")
    assert aliased["automations"] == {"enabled": True, "source": "config"}


def test_all_flags_layers() -> None:
    on = resolve([{"experimental": {"all_flags": True}}], "release")
    assert on["artifacts"] == {"enabled": True, "source": "config"}
    assert on["full_codemode"] == {"enabled": False, "source": "default"}
    # false falls through rather than turning flags off
    off = resolve([{"experimental": {"all_flags": False}}], "release")
    assert off["artifacts"] == {"enabled": False, "source": "default"}
    # env beats both config channels
    env_wins = resolve([{"experimental": {"all_flags": True}}], "release", "-artifacts")
    assert env_wins["artifacts"] == {"enabled": False, "source": "env"}


def test_mirror_constants_derive_from_the_chain() -> None:
    probe = (
        "import json;"
        "from opencode.automation.service import EVENT_AUTOMATIONS_ENABLED as a;"
        "from opencode.tools.builtin.mcp_events import MCP_EVENTS_ENABLED as m;"
        "print(json.dumps({'automations': a, 'mcp_events': m}))"
    )
    for overrides, expected in (
        (None, {"automations": True, "mcp_events": True}),
        ("-mcp_events", {"automations": True, "mcp_events": False}),
        ("-automations", {"automations": False, "mcp_events": True}),
    ):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        if overrides is None:
            env.pop("OPENCODE_FLAGS", None)
        else:
            env["OPENCODE_FLAGS"] = overrides
        ran = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, env=env, check=False
        )
        assert ran.returncode == 0, ran.stderr[-300:]
        assert json.loads(ran.stdout.strip().splitlines()[-1]) == expected


# ---------------------------------------------------------------------------
# canonical-json (util/canonical-json.ts verbatim)
# ---------------------------------------------------------------------------


def test_key_order_is_utf16_code_units() -> None:
    # U+10000 (surrogate D800..) sorts BEFORE U+FFFF in JS comparison
    # order; Python's code-point order says the opposite. The serializer
    # must follow JS so ids hash identically to upstream.
    assert canonical_json({"\uffff": 1, "\U00010000": 2}) == '{"\U00010000":2,"\uffff":1}'
    # JS order: "a"(0x0061) < "b"(0x0062) < U+10000(0xD800..) < U+FFFF --
    # only the astral-vs-U+FFFF pair flips against Python's code-point order.
    assert canonical_json({"b": 0, "a": 0, "\uffff": 0, "\U00010000": 0}) == ('{"a":0,"b":0,"\U00010000":0,"\uffff":0}')


def test_scalars_nesting_and_compactness() -> None:
    assert canonical_json({"b": [3, {"d": None, "c": True}], "a": "x"}) == '{"a":"x","b":[3,{"c":true,"d":null}]}'
    assert canonical_json([]) == "[]"
    assert canonical_json({}) == "{}"
    assert canonical_json([2, 1]) == "[2,1]"  # array order preserved, never sorted
    assert canonical_json({"é": "ü"}) == '{"é":"ü"}'  # non-ASCII raw, as JSON.stringify
    assert canonical_json({"k": 1.5}) == '{"k":1.5}'
    first = canonical_json({"z": 1, "a": 2})
    second = canonical_json({"a": 2, "z": 1})
    assert first == second == '{"a":2,"z":1}'  # insertion order never leaks


# ---------------------------------------------------------------------------
# shared-cache (core/shared-cache.ts)
# ---------------------------------------------------------------------------


class _Clock:
    """Injectable monotonic clock (ms), the repo timing discipline."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_module_constants() -> None:
    assert INTERVAL_S == 600.0  # upstream Duration.minutes(10)
    assert UNREAD_MS == 24 * 3600 * 1000  # upstream Duration.hours(24)


async def test_fresh_then_expire() -> None:
    cache = SharedCache(interval_s=_INTERVAL_S, retry_delays_s=_RETRY_DELAYS_S)
    loads = 0

    async def load() -> str:
        nonlocal loads
        loads += 1
        return f"v{loads}"

    assert await cache.get("k", load) == "v1"
    assert await cache.get("k", load) == "v1"  # fresh: no load
    await asyncio.sleep(_INTERVAL_S + 0.1)
    assert await cache.get("k", load) == "v2"  # aged out: reload
    assert loads == 2


async def test_single_flight_join() -> None:
    cache = SharedCache(interval_s=_INTERVAL_S, retry_delays_s=_RETRY_DELAYS_S)
    gate = asyncio.Event()
    loads = 0

    async def load() -> str:
        nonlocal loads
        loads += 1
        await gate.wait()
        return "one"

    first = asyncio.create_task(cache.get("k", load))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(cache.get("k", load))
    await asyncio.sleep(0.05)
    gate.set()
    assert await asyncio.gather(first, second) == ["one", "one"]
    assert loads == 1


async def test_force_bypasses_age_but_joins() -> None:
    cache = SharedCache(interval_s=60.0, retry_delays_s=_RETRY_DELAYS_S)
    gate = asyncio.Event()
    gate.set()  # the first (ungated) load passes straight through
    loads = 0

    async def load() -> str:
        nonlocal loads
        loads += 1
        await gate.wait()
        return f"v{loads}"

    assert await cache.get("k", load) == "v1"
    gate.clear()
    first = asyncio.create_task(cache.get("k", load, force=True))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(cache.get("k", load, force=True))  # joins the forced load
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(first, second)
    assert loads == 2  # one forced reload, joined -- never two


async def test_transient_retry_then_success() -> None:
    cache = SharedCache(interval_s=60.0, retry_delays_s=_RETRY_DELAYS_S)
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.TransportError("boom")
        return "ok"

    assert await cache.get("k", flaky) == "ok"
    assert attempts == 3  # initial + retries until success


async def test_retry_exhaustion_keeps_last_good() -> None:
    cache = SharedCache(interval_s=_INTERVAL_S, retry_delays_s=_RETRY_DELAYS_S)
    attempts = 0

    async def good() -> str:
        nonlocal attempts
        attempts += 1
        return "good"

    async def bad() -> str:
        nonlocal attempts
        attempts += 1
        raise httpx.TransportError("down")

    assert await cache.get("k", good) == "good"
    await asyncio.sleep(_INTERVAL_S + 0.1)  # age out
    assert await cache.get("k", bad) == "good"  # stale serve, never the error
    assert attempts == 1 + 1 + len(_RETRY_DELAYS_S)  # 1 + (attempt + 3 retries)


async def test_retry_exhaustion_without_good_raises() -> None:
    cache = SharedCache(interval_s=60.0, retry_delays_s=_RETRY_DELAYS_S)
    attempts = 0

    async def bad() -> str:
        nonlocal attempts
        attempts += 1
        raise httpx.TransportError("down")

    with pytest.raises(httpx.TransportError):
        await cache.get("k", bad)
    assert attempts == 1 + len(_RETRY_DELAYS_S)  # attempts = 1 + retries


async def test_non_transient_error_no_retry() -> None:
    cache = SharedCache(interval_s=60.0, retry_delays_s=_RETRY_DELAYS_S)
    attempts = 0

    async def broken() -> str:
        nonlocal attempts
        attempts += 1
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        await cache.get("k", broken)
    assert attempts == 1


async def test_cancellation_never_kept() -> None:
    cache = SharedCache(interval_s=60.0, retry_delays_s=_RETRY_DELAYS_S)
    loads = 0

    async def hanging() -> str:
        nonlocal loads
        loads += 1
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # cancelled while waiting

    task = asyncio.create_task(cache.get("k", hanging))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert loads == 1

    async def fresh() -> str:
        nonlocal loads
        loads += 1
        return "fresh"

    assert await cache.get("k", fresh) == "fresh"  # the cancelled load left nothing
    assert loads == 2


async def test_unread_eviction_drops_even_a_fresh_entry() -> None:
    clock = _Clock()
    cache = SharedCache(interval_s=3600.0, retry_delays_s=_RETRY_DELAYS_S, now_ms=clock)
    loads = 0

    async def load() -> str:
        nonlocal loads
        loads += 1
        return "v"

    await cache.get("k", load)
    assert loads == 1
    clock.now = UNREAD_MS + 1  # 24h pass; the entry is still FRESH (interval 1h)
    await cache.get("other", load)  # this get sweeps unread entries
    assert await cache.get("k", load) == "v"  # evicted: reload despite freshness
    assert loads == 3


def test_fingerprint_face() -> None:
    first = fingerprint("secret-one")
    assert len(first) == 16
    assert all(char in "0123456789abcdef" for char in first)
    assert fingerprint("secret-one") == first  # deterministic
    assert fingerprint("secret-two") != first


def test_transient_classifier() -> None:
    def response(status_code: int) -> httpx.Response:
        return httpx.Response(status_code=status_code, request=httpx.Request("GET", "https://unit"))

    assert transient(httpx.TimeoutException("t"))
    assert transient(httpx.TransportError("t"))
    for code in (408, 429, 500, 502, 503, 504):
        assert transient(response(code)), code
    for code in (400, 401, 403, 404, 418, 501):
        assert not transient(response(code)), code
    assert not transient(ValueError("x"))
    assert not transient(RuntimeError("x"))
