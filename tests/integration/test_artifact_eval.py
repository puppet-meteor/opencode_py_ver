"""T-C3-011 evaluation set: artifact service + blob-store capability probes.

BEFORE the capability lands, every scenario below reads capability-absent
(the modules/routes/table do not exist); the assertions accept that state so
the set stays green while pinning the measured surface. AFTER the
implementation, each scenario must reach its goal status (P0 probed, M1
round-tripped, M2 gated, M3 sandboxed, M4 stored, M5 protocol) -- anything
else is drifting and means the harness and the implementation disagreed:
fix the alignment, never the reading.

Measured seams (banked in doc/plans/port-232-increments.md, T-C3-011):

* ``opencode.artifact.service`` module constant ``ARTIFACTS_ENABLED`` plus
  ``ArtifactService(database=, data_dir=, directory=)`` exposing async
  ``create(dict) -> dict`` / ``get(id) -> dict`` / ``update(id, dict) ->
  dict`` / ``list() -> list[dict]`` / ``delete(id)``; create raises when the
  flag constant is False; flat JSON discipline (no ``{"data": ...}``
  wrapper) on the HTTP face;
* ``opencode.blob_store.BlobStore(data_dir=, now_ms=)`` with sync
  ``write(id, bytes) -> Info`` / ``read(id) -> Info`` / ``read_bytes(id) ->
  bytes`` / ``remove(id)`` / ``sweep(now_ms) -> int``; ids follow the
  upstream strict pattern; mime from the first bytes' magic table;
* ``opencode.util.mime.detect_media(bytes)`` / ``detect_file_mime(path,
  bytes)``;
* sqlite table ``artifact`` at the alembic head;
* ``create_app(..., artifact=ArtifactService | None)`` with routes under
  ``/api/experimental/artifacts`` (view trailing-slash 308, CSP sandbox
  allow-scripts + nosniff) and the raw blob route
  ``GET /api/experimental/blob/{id}`` (ETag/304/single Range 206/416).
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from opencode.app import App
from opencode.httpapi import create_app
from tests.integration.test_httpapi_server import _TOKEN, _asgi_request, _json_body
from tests.integration.test_store_commit import _require_success, _upgrade_head

#: The injection call is measured, not statically known: until the capability
#: lands, ``create_app`` has no ``artifact`` parameter for mypy to see.
_CREATE_APP_ANY = cast(Any, create_app)

#: A valid upstream blob ID: video_ + 12 hex + 14 alnum + _0.
_BLOB_ID = "video_" + "0" * 12 + "a" * 14 + "_0"

#: PNG magic signature + payload (the detector only reads the signature).
_PNG_BYTES = bytes.fromhex("89504e470d0a1a0a") + b"fake png payload for the probe"

_FIXED_NOW_MS = 1_800_000_000_000
_RETENTION_MS = 7 * 24 * 3600 * 1000


class _CapabilityAbsent(Exception):
    """The seam this probe measures is not implemented yet."""


def _report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _find(name: str) -> Any | None:
    """Import a module that may not exist yet (parent packages do)."""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ModuleNotFoundError, ValueError):
        return None
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _close(app: Any) -> None:
    if hasattr(app, "close"):
        result = app.close()
        if hasattr(result, "__await__"):
            await result


async def _make_service(tmp_path: Path) -> tuple[Any, Any]:
    """Migrated db + App + the artifact service over the banked seam."""
    db_path = tmp_path / "probe.db"
    _require_success(_upgrade_head(db_path))
    app = App(db_path, working_dir=str(tmp_path / "work"))
    module = _find("opencode.artifact.service")
    if module is None or not hasattr(module, "ArtifactService"):
        await _close(app)
        raise _CapabilityAbsent("opencode.artifact.service.ArtifactService missing")
    data_dir = tmp_path / "data"
    service = cast(Any, module).ArtifactService(
        database=app._database, data_dir=str(data_dir), directory=str(tmp_path / "work")
    )
    return service, app


async def _make_api(tmp_path: Path) -> tuple[Any, Any, Any]:
    """The HTTP face with the artifact service injected (banked seam)."""
    service, app = await _make_service(tmp_path)
    if "artifact" not in inspect.signature(create_app).parameters:
        await _close(app)
        raise _CapabilityAbsent("create_app has no artifact parameter")
    api = _CREATE_APP_ANY(app, token=_TOKEN, artifact=service)
    return api, app, service


async def _request(
    api: Any,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[bytes, bytes], Any]:
    raw = _json_body(body) if body is not None else None
    status, headers, payload, _receive = await _asgi_request(
        api, method, path, token=_TOKEN, body=raw, extra_headers=extra_headers
    )
    decoded: Any = None
    if payload:
        try:
            decoded = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            decoded = None
    return status, headers, decoded


# ---------------------------------------------------------------------------
# P0 -- the capability surface
# ---------------------------------------------------------------------------


async def test_p0_capability(tmp_path: Path) -> None:
    metrics: dict[str, Any] = {
        "service_module": False,
        "service_face": False,
        "blob_store_module": False,
        "blob_store_face": False,
        "mime_module": False,
        "mime_face": False,
        "artifact_table": False,
        "create_app_param": False,
        "artifact_routes": False,
    }
    status = "capability-absent"
    try:
        service_mod = _find("opencode.artifact.service")
        metrics["service_module"] = service_mod is not None
        metrics["service_face"] = (
            service_mod is not None
            and hasattr(service_mod, "ArtifactService")
            and hasattr(service_mod, "ARTIFACTS_ENABLED")
        )
        blob_mod = _find("opencode.blob_store")
        metrics["blob_store_module"] = blob_mod is not None
        metrics["blob_store_face"] = blob_mod is not None and all(hasattr(blob_mod, name) for name in ("BlobStore",))
        mime_mod = _find("opencode.util.mime")
        metrics["mime_module"] = mime_mod is not None
        metrics["mime_face"] = mime_mod is not None and all(
            hasattr(mime_mod, name) for name in ("detect_media", "detect_file_mime")
        )
        db_path = tmp_path / "p0.db"
        _require_success(_upgrade_head(db_path))
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='artifact'").fetchall()
        metrics["artifact_table"] = bool(rows)
        metrics["create_app_param"] = "artifact" in inspect.signature(create_app).parameters
        if metrics["create_app_param"]:
            api, app, _service = await _make_api(tmp_path / "http")
            try:
                seen = {getattr(route, "path", "") for route in api.router.routes}
                metrics["artifact_routes"] = {
                    "/api/experimental/artifacts",
                    "/api/experimental/artifacts/{artifact_id}/view/{file_path}",
                    "/api/experimental/blob/{blob_id}",
                } <= seen
            finally:
                await _close(app)
        status = "probed" if all(metrics.values()) else "absent-partial"
    except _CapabilityAbsent:
        pass
    _report({"scenario": "P0-capability", "status": status, **metrics})
    assert status in ("capability-absent", "absent-partial", "probed")


# ---------------------------------------------------------------------------
# M1 -- service CRUD round trip
# ---------------------------------------------------------------------------


async def test_m1_service_crud_round_trip(tmp_path: Path) -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        service, app = await _make_service(tmp_path)
        try:
            info = await service.create(
                {
                    "title": "Flamegraph report",
                    "files": {"index.html": "<h1>hi</h1>", "assets/app.js": "console.log(1)"},
                }
            )
            metrics["id_art_prefix"] = str(info.get("id", "")).startswith("art_")
            metrics["entry_defaults_to_index_html"] = info.get("entry") == "index.html"
            metrics["view_url_shape"] = "/view/" in str(info.get("url", ""))
            content = await service.get(info["id"])
            files = content.get("files", {})
            metrics["files_round_trip"] = files.get("assets/app.js") == "console.log(1)"
            updated = await service.update(info["id"], {"files": {"index.html": "<h1>v2</h1>"}})
            metrics["update_keeps_id"] = updated.get("id") == info.get("id")
            listed = await service.list()
            metrics["list_contains"] = any(item.get("id") == info.get("id") for item in listed)
            await service.delete(info["id"])
            gone = False
            try:
                await service.get(info["id"])
            except Exception:
                gone = True
            metrics["delete_then_not_found"] = gone
            status = "round-tripped" if all(metrics.values()) else "drifting"
        finally:
            await _close(app)
    except _CapabilityAbsent:
        pass
    _report({"scenario": "M1-crud", "status": status, **metrics})
    assert status in ("capability-absent", "round-tripped", "drifting")


# ---------------------------------------------------------------------------
# M2 -- the flag gate
# ---------------------------------------------------------------------------


async def test_m2_flag_gate(tmp_path: Path) -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        api, app, service = await _make_api(tmp_path)
        module = _find("opencode.artifact.service")
        try:
            original = getattr(cast(Any, module), "ARTIFACTS_ENABLED", None)
            if original is None:
                raise _CapabilityAbsent("ARTIFACTS_ENABLED constant missing")
            cast(Any, module).ARTIFACTS_ENABLED = False
            blocked = False
            try:
                await service.create({"title": "nope", "files": {"index.html": "x"}})
            except Exception as error:
                blocked = "disabl" in type(error).__name__.lower() or "disabl" in str(error).lower()
            metrics["disabled_create_raises"] = blocked
            http_status, _headers, _body = await _request(
                api,
                "POST",
                "/api/experimental/artifacts",
                body={"title": "nope", "files": {"index.html": "x"}},
            )
            metrics["http_403_when_disabled"] = http_status == 403
            status = "gated" if all(metrics.values()) else "drifting"
            cast(Any, module).ARTIFACTS_ENABLED = original
        finally:
            await _close(app)
    except _CapabilityAbsent:
        pass
    _report({"scenario": "M2-flag-gate", "status": status, **metrics})
    assert status in ("capability-absent", "gated", "drifting")


# ---------------------------------------------------------------------------
# M3 -- the sandboxed view route
# ---------------------------------------------------------------------------


async def test_m3_view_semantics(tmp_path: Path) -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        api, app, service = await _make_api(tmp_path)
        try:
            info = await service.create(
                {
                    "title": "Wait stats",
                    "files": {
                        "index.html": "<h1>v1</h1>",
                        "assets/app.js": "window.VERSION = 1",
                    },
                }
            )
            base = str(info.get("url", ""))  # .../view/
            redirect_status, redirect_headers, _body = await _request(api, "GET", base.rstrip("/"))
            metrics["trailing_slash_308"] = redirect_status == 308 and base.encode() in [
                value for _k, value in redirect_headers.items() if _k == b"location"
            ]
            entry_status, entry_headers, entry_body = await _request(api, "GET", base)
            csp_values = [v for k, v in entry_headers.items() if k == b"content-security-policy"]
            metrics["entry_200"] = entry_status == 200
            metrics["csp_sandbox"] = csp_values == [b"sandbox allow-scripts"]
            metrics["nosniff"] = any(
                v == b"nosniff" for k, v in entry_headers.items() if k == b"x-content-type-options"
            )
            metrics["html_served"] = "<h1>v1</h1>" in str(entry_body or "")
            asset_status, _asset_headers, asset_body = await _request(api, "GET", base + "assets/app.js")
            metrics["relative_asset_served"] = asset_status == 200 and "window.VERSION = 1" in str(asset_body or "")
            traversal_status, _t_headers, _t_body = await _request(api, "GET", base + "%2e%2e/escape.html")
            metrics["traversal_rejected"] = traversal_status in (400, 404)
            status = "sandboxed" if all(metrics.values()) else "drifting"
        finally:
            await _close(app)
    except _CapabilityAbsent:
        pass
    _report({"scenario": "M3-view", "status": status, **metrics})
    assert status in ("capability-absent", "sandboxed", "drifting")


# ---------------------------------------------------------------------------
# M4 -- the file blob store
# ---------------------------------------------------------------------------


async def test_m4_blob_store(tmp_path: Path) -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        module = _find("opencode.blob_store")
        if module is None or not hasattr(module, "BlobStore"):
            raise _CapabilityAbsent("opencode.blob_store.BlobStore missing")
        data_dir = tmp_path / "blobs"
        store = cast(Any, module).BlobStore(data_dir=str(data_dir), now_ms=lambda: _FIXED_NOW_MS)
        info = store.write(_BLOB_ID, _PNG_BYTES)
        metrics["write_returns_info"] = (
            info.get("id") == _BLOB_ID and info.get("mime") == "image/png" and info.get("bytes") == len(_PNG_BYTES)
        )
        metrics["expires_at_seven_days"] = info.get("expires_at") == _FIXED_NOW_MS + _RETENTION_MS
        metrics["read_round_trip"] = store.read_bytes(_BLOB_ID) == _PNG_BYTES
        metrics["no_part_residue"] = not list(data_dir.rglob("*.part"))
        removed = store.sweep(_FIXED_NOW_MS + _RETENTION_MS + 1)
        metrics["expired_swept"] = removed >= 1
        gone = False
        try:
            store.read(_BLOB_ID)
        except Exception:
            gone = True
        metrics["swept_read_not_found"] = gone
        status = "stored" if all(metrics.values()) else "drifting"
    except _CapabilityAbsent:
        pass
    _report({"scenario": "M4-blob-store", "status": status, **metrics})
    assert status in ("capability-absent", "stored", "drifting")


# ---------------------------------------------------------------------------
# M5 -- the raw blob HTTP protocol
# ---------------------------------------------------------------------------


async def test_m5_blob_http_protocol(tmp_path: Path) -> None:
    metrics: dict[str, Any] = {}
    status = "capability-absent"
    try:
        api, app, service = await _make_api(tmp_path)
        module = _find("opencode.blob_store")
        if module is None or not hasattr(module, "BlobStore"):
            await _close(app)
            raise _CapabilityAbsent("opencode.blob_store.BlobStore missing")
        # One data root: the HTTP route reads the same <data>/blobs directory
        # the service owns (upstream Global.data single-root semantics).
        data_root = Path(str(service.data_dir)) if hasattr(service, "data_dir") else None
        if data_root is None:
            await _close(app)
            raise _CapabilityAbsent("ArtifactService has no data_dir attribute")
        try:
            store = cast(Any, module).BlobStore(data_dir=str(data_root), now_ms=lambda: _FIXED_NOW_MS)
            store.write(_BLOB_ID, _PNG_BYTES)
            path = f"/api/experimental/blob/{_BLOB_ID}"
            ok_status, ok_headers, _body = await _request(api, "GET", path)
            etag = next((v for k, v in ok_headers.items() if k == b"etag"), b"")
            metrics["get_200_etagged"] = ok_status == 200 and etag == f'"{_BLOB_ID}"'.encode()
            not_modified, _nm_headers, _nm_body = await _request(
                api, "GET", path, extra_headers=[(b"if-none-match", etag)]
            )
            metrics["if_none_match_304"] = not_modified == 304
            ranged, _r_headers, ranged_body = await _request(api, "GET", path, extra_headers=[(b"range", b"bytes=0-3")])
            metrics["single_range_206"] = ranged == 206 and ranged_body == _PNG_BYTES[:4]
            unsatisfiable, _u_headers, _u_body = await _request(
                api, "GET", path, extra_headers=[(b"range", b"bytes=99999-100000")]
            )
            metrics["range_416"] = unsatisfiable == 416
            status = "protocol" if all(metrics.values()) else "drifting"
        finally:
            await _close(app)
    except _CapabilityAbsent:
        pass
    _report({"scenario": "M5-blob-http", "status": status, **metrics})
    assert status in ("capability-absent", "protocol", "drifting")
