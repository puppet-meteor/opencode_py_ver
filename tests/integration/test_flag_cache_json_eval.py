"""T-C3-007 evaluation harness: flag chain, canonical-json, shared-cache.

A measurement face, not an assertion face (the test_automation_eval.py
precedent): every probe records a metric and passes, whether or not the
capability exists. Running this module on pre-implementation code captures
the BEFORE baseline; the identical inputs afterwards give the AFTER column.
The delta is the ticket's evidence against doc/GOAL.md's determinism-first
gate (doc/plans/port-232-increments.md T-C3-007, decisions 1-6):

* P0 capability -- the banked seam contract: module ``opencode.config.flags``
  (``resolve(entries, channel, overrides)`` pure function + ``REGISTRY``/
  ``OPT_IN_ONLY``/``ALIASES``/``WILDCARD``), module ``opencode.util.canonical_json``
  (``canonical_json(value) -> str``) and module ``opencode.shared_cache``
  (``SharedCache`` async single-flight cache + ``fingerprint``/``transient``).
* M1 flag-chain matrix -- the upstream priority chain verbatim: env overrides
  (key / ``-key`` / ``all`` wildcard minus opt-in-only / aliases) over
  ``experimental.<key>`` from the highest-priority layer over ``all_flags``
  from the highest-priority layer that sets it, over the channel default
  (on for local/dev, off otherwise); every state carries its source.
* M2 mirror unification -- ``MCP_EVENTS_ENABLED`` (tools/builtin/mcp_events)
  and ``EVENT_AUTOMATIONS_ENABLED`` (automation/service) derive from the
  resolved defaults: import-time ``OPENCODE_FLAGS`` turns each off by key.
* M3 canonical determinism -- key order is the JS comparison order (UTF-16
  code units, where an astral key sorts BEFORE U+FFFF, unlike Python's code
  point order), compact separators, nested arrays preserved.
* M4 single-flight freshness -- two concurrent gets join one load; a fresh
  hit runs no load; ``force`` reloads while still joining.
* M5 stale-serve -- a load failing after a good value keeps serving the last
  good value once the transient retries are exhausted, never an error.
* M6 pure faces -- ``fingerprint`` is 16 hex chars; ``transient`` classifies
  timeouts/transport errors/HTTP 408/429/500/502/503/504 as retryable and
  everything else not.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Injected timing (repo discipline: short values, anti-flake).
_INTERVAL_S = 0.4
_RETRY_DELAYS_S = (0.01, 0.01, 0.01)
_PROBE_WAIT_S = 5.0
#: The upstream freshness default, as a named fact (not waited on).
_UPSTREAM_INTERVAL_S = 600.0


def _report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _module(name: str) -> Any | None:
    try:
        if importlib.util.find_spec(name) is None:
            return None
    except ModuleNotFoundError:
        return None
    return importlib.import_module(name)


# ---------------------------------------------------------------------------
# P0 -- the capability seam
# ---------------------------------------------------------------------------


def test_p0_capability_probe() -> None:
    flags = _module("opencode.config.flags")
    canonical = _module("opencode.util.canonical_json")
    cache = _module("opencode.shared_cache")
    faces = {
        "flags_module": callable(getattr(flags, "resolve", None)) if flags else False,
        "flags_registry": hasattr(flags, "REGISTRY") if flags else False,
        "canonical_module": callable(getattr(canonical, "canonical_json", None)) if canonical else False,
        "cache_module": cache is not None and hasattr(cache, "SharedCache"),
        "cache_fingerprint": callable(getattr(cache, "fingerprint", None)) if cache else False,
    }
    status = "capability-absent" if not any(faces.values()) else ("probed" if all(faces.values()) else "partial")
    _report({"scenario": "P-capability", "status": status, **faces})
    assert status in ("capability-absent", "partial", "probed")


# ---------------------------------------------------------------------------
# M1 -- the resolution matrix (pure face)
# ---------------------------------------------------------------------------

#: Upstream registry keys (schema/config/flag.ts): the eight, verbatim.
_KEYS = (
    "todo",
    "media_blob",
    "automations",
    "artifacts",
    "video_generate",
    "console_watches",
    "mcp_events",
    "full_codemode",
)


def _matrix_cases() -> list[dict[str, Any]]:
    """(entries, channel, overrides) -> {key: (enabled, source)} expectations.

    Entries are merged layer values (ConfigLayer order, highest priority
    last in upstream findLast terms -- the py seam passes them in priority
    order and resolve reads the highest-priority layer that sets the key).
    """

    def case(entries: list[dict[str, Any]], channel: str, overrides: str | None) -> dict[str, Any]:
        return {"entries": entries, "channel": channel, "overrides": overrides}

    return [
        case([], "local", None),
        case([], "release", None),
        case([], "dev", None),
        case([], "local", "-automations"),
        case([], "release", "schedules"),
        case([], "release", "all"),
        case([], "release", "all,-mcp_events"),
        case([{"experimental": {"mcp_events": True}}], "release", None),
        case([{"experimental": {"mcp_events": False}}, {"experimental": {"mcp_events": True}}], "release", None),
        case([{"experimental": {"all_flags": True}}], "release", None),
        case([{"experimental": {"all_flags": False}}], "release", None),
        case([], "local", "full_codemode"),
        case([{"experimental": {"all_flags": True}}], "release", "all"),
    ]


def _expect(entry: dict[str, Any]) -> dict[str, tuple[bool, str]]:
    """The upstream-semantic expectation for one matrix case.

    env (key/-key/all minus opt-in, aliases) > experimental.<key> from the
    highest-priority layer that sets experimental > all_flags:true from the
    highest-priority layer that sets it > default (non-opt-in and local/dev).
    """
    entries: list[dict[str, Any]] = entry["entries"]
    channel = entry["channel"]
    env: dict[str, bool] = {}
    for item in filter(None, (entry["overrides"] or "").split(",")):
        item = item.strip()
        if not item:
            continue
        env[item[1:] if item.startswith("-") else item] = not item.startswith("-")
    wild_all = env.get("all") is True
    experimental: dict[str, Any] = {}
    for layer in entries:
        block = layer.get("experimental")
        if isinstance(block, dict):
            experimental = block
    all_flags = None
    for layer in entries:
        block = layer.get("experimental")
        if isinstance(block, dict) and "all_flags" in block:
            all_flags = block["all_flags"] is True
    expected: dict[str, tuple[bool, str]] = {}
    for key in _KEYS:
        opt_in = key == "full_codemode"
        alias = {"automations": "schedules"}.get(key)
        override = env.get(key)
        if override is None and alias is not None:
            override = env.get(alias)
        if override is None and wild_all and not opt_in:
            override = True
        configured = experimental.get(key)
        if configured is None and alias is not None:
            configured = experimental.get(alias)
        if configured is None and all_flags and not opt_in:
            configured = True
        if override is not None:
            expected[key] = (override, "env")
        elif configured is not None:
            expected[key] = (configured, "config")
        else:
            expected[key] = (not opt_in and channel in ("local", "dev"), "default")
    return expected


def test_m1_flag_resolution_matrix() -> None:
    flags = _module("opencode.config.flags")
    status = "capability-absent"
    total = passed = 0
    worst: str | None = None
    if flags is not None and callable(getattr(flags, "resolve", None)):
        for entry in _matrix_cases():
            expectation = _expect(entry)
            total += 1
            try:
                resolved = flags.resolve(
                    entries=entry["entries"], channel=entry["channel"], overrides=entry["overrides"]
                )
                ok = True
                for key, (enabled, source) in expectation.items():
                    state = resolved.get(key) if isinstance(resolved, dict) else None
                    if not isinstance(state, dict) or state.get("enabled") != enabled or state.get("source") != source:
                        ok = False
                        worst = worst or f"{key}@{entry['overrides'] or entry['channel']}"
                passed += ok
            except Exception:
                pass
        status = "chain-verified" if passed == total and total else f"chain-broken:{passed}/{total}"
    _report({"scenario": "M1-flag-matrix", "status": status, "passed": passed, "total": total, "worst": worst})
    assert status.startswith(("capability-absent", "chain-verified", "chain-broken"))


# ---------------------------------------------------------------------------
# M2 -- the mirror constants unify (import-time env, probed in a subprocess)
# ---------------------------------------------------------------------------


def _mirror_probe(overrides: str | None) -> dict[str, Any] | None:
    probe = (
        "import json;"
        "from opencode.automation.service import EVENT_AUTOMATIONS_ENABLED as a;"
        "from opencode.tools.builtin.mcp_events import MCP_EVENTS_ENABLED as m;"
        "print(json.dumps({'automations': a, 'mcp_events': m}))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    if overrides is None:
        env.pop("OPENCODE_FLAGS", None)
    else:
        env["OPENCODE_FLAGS"] = overrides
    ran = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, env=env, check=False
    )
    if ran.returncode != 0:
        return None
    try:
        parsed = json.loads(ran.stdout.strip().splitlines()[-1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def test_m2_mirror_constants_unify() -> None:
    status = "capability-absent"
    results: dict[str, Any] = {}
    expected = {
        "default": {"automations": True, "mcp_events": True},
        "-mcp_events": {"automations": True, "mcp_events": False},
        "-automations": {"automations": False, "mcp_events": True},
    }
    all_ok = True
    for label, overrides in (("default", None), ("-mcp_events", "-mcp_events"), ("-automations", "-automations")):
        found = _mirror_probe(overrides)
        results[label] = found
        all_ok = all_ok and found == expected[label]
    if any(value is not None for value in results.values()):
        status = "unified" if all_ok else "constants-only"
    _report({"scenario": "M2-mirror-unification", "status": status, **results})
    assert status.startswith(("capability-absent", "unified", "constants-only"))


# ---------------------------------------------------------------------------
# M3 -- canonical determinism (pure face)
# ---------------------------------------------------------------------------


def test_m3_canonical_determinism() -> None:
    canonical = _module("opencode.util.canonical_json")
    status = "capability-absent"
    metrics: dict[str, Any] = {}
    if canonical is not None and callable(getattr(canonical, "canonical_json", None)):
        try:
            # UTF-16 code-unit order: U+10000 (surrogate D800) sorts BEFORE
            # U+FFFF, though Python's code-point order says the opposite.
            astral = canonical.canonical_json({"\uffff": 1, "\U00010000": 2})
            utf16_ordered = astral == '{"\U00010000":2,"\uffff":1}'
            nested = canonical.canonical_json({"b": [3, {"d": None, "c": True}], "a": "x"})
            nested_ok = nested == '{"a":"x","b":[3,{"c":true,"d":null}]}'
            compact = canonical.canonical_json({"k": {"z": 1, "a": 2}})
            compact_ok = " " not in compact and compact == '{"k":{"a":2,"z":1}}'
            metrics = {"utf16_key_order": utf16_ordered, "nested": nested_ok, "compact": compact_ok}
            status = "deterministic" if all(metrics.values()) else "drifting"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}"
    _report({"scenario": "M3-canonical-determinism", "status": status, **metrics})
    assert status.startswith(("capability-absent", "deterministic", "drifting", "api-mismatch"))


# ---------------------------------------------------------------------------
# M4/M5 -- the cache machine (async faces)
# ---------------------------------------------------------------------------


def _drive_cache(metric: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        module = _module("opencode.shared_cache")
        if module is None or not hasattr(module, "SharedCache"):
            return {"status": "capability-absent"}
        out: dict[str, Any] = {}
        if metric == "single-flight":
            cache = module.SharedCache(interval_s=_INTERVAL_S, retry_delays_s=_RETRY_DELAYS_S)
            gate = asyncio.Event()
            loads = 0

            async def load() -> str:
                nonlocal loads
                loads += 1
                await gate.wait()
                return f"value-{loads}"

            first = asyncio.create_task(cache.get("k", load))
            await asyncio.sleep(0.05)  # let the first get start its load
            second = asyncio.create_task(cache.get("k", load))
            await asyncio.sleep(0.05)
            gate.set()
            a, b = await asyncio.gather(first, second)
            await cache.get("k", load)  # within interval: no load
            forced = await cache.get("k", load, force=True)
            out = {
                "status": "single-flight",
                "concurrent_loads": loads == 1,
                "joined_same_value": a == b == "value-1",
                "fresh_no_load": loads == 1,
                "force_reloads": loads == 2 and forced == "value-2",
                "loads": loads,
            }
            out["status"] = "single-flight" if all(v for k, v in out.items() if k != "loads") else "overlapping"
        else:  # stale-serve
            cache = module.SharedCache(interval_s=_INTERVAL_S, retry_delays_s=_RETRY_DELAYS_S)
            loads = 0

            async def good() -> str:
                nonlocal loads
                loads += 1
                return "good"

            async def bad() -> str:
                nonlocal loads
                loads += 1
                raise RuntimeError("remote is down")

            first = await cache.get("k", good)
            await asyncio.sleep(_INTERVAL_S + 0.1)  # age out the good value
            after = await cache.get("k", bad)
            out = {
                "status": "stale-served",
                "kept_last_good": first == "good" and after == "good",
                "retried_then_kept": loads == 1 + 3,
                "loads": loads,
            }
            out["status"] = "stale-served" if out["kept_last_good"] and out["retried_then_kept"] else "error-surfaced"
        return out

    return asyncio.run(run())


def test_m4_single_flight_freshness() -> None:
    found = _drive_cache("single-flight")
    _report({"scenario": "M4-single-flight", **found})
    assert found.get("status", "capability-absent").startswith(("capability-absent", "single-flight", "overlapping"))


def test_m5_stale_serve() -> None:
    found = _drive_cache("stale")
    _report({"scenario": "M5-stale-serve", **found})
    assert found.get("status", "capability-absent").startswith(("capability-absent", "stale-served", "error-surfaced"))


# ---------------------------------------------------------------------------
# M6 -- the pure faces (fingerprint + transient classification)
# ---------------------------------------------------------------------------


def test_m6_pure_faces() -> None:
    module = _module("opencode.shared_cache")
    status = "capability-absent"
    metrics: dict[str, Any] = {}
    if module is not None:
        try:
            import httpx

            def response(status_code: int) -> httpx.Response:
                return httpx.Response(status_code=status_code, request=httpx.Request("GET", "https://unit"))

            checks = {
                "fingerprint_16hex": isinstance(module.fingerprint("secret"), str)
                and len(module.fingerprint("secret")) == 16
                and all(c in "0123456789abcdef" for c in module.fingerprint("secret")),
                "timeout_transient": module.transient(httpx.TimeoutException("t")),
                "transport_transient": module.transient(httpx.TransportError("t")),
                "503_transient": module.transient(response(503)),
                "408_transient": module.transient(response(408)),
                "404_not_transient": not module.transient(response(404)),
                "value_error_not_transient": not module.transient(ValueError("x")),
            }
            metrics = checks
            status = "classified" if all(checks.values()) else "misclassified"
        except Exception as error:
            status = f"api-mismatch:{error.__class__.__name__}"
    _report({"scenario": "M6-pure-faces", "status": status, **metrics})
    assert status.startswith(("capability-absent", "classified", "misclassified", "api-mismatch"))
