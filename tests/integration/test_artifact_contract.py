"""T-C3-011 contract set: artifact service + blob store + HTTP routes.

The red set distilled from the upstream families (core/test/artifact.test.ts
2 tests + server/test/artifact.test.ts 1 large test) onto the banked py
seams (doc/plans/port-232-increments.md T-C3-011). Every test here is RED
until the implementation lands and must stay green after.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
from pathlib import Path
from typing import Any, cast

import pytest

from opencode.app import App
from opencode.httpapi import create_app
from tests.integration.test_httpapi_server import _TOKEN, _asgi_request, _json_body
from tests.integration.test_store_commit import _require_success, _upgrade_head

_CREATE_APP_ANY = cast(Any, create_app)

_BLOB_ID = "video_" + "0" * 12 + "a" * 14 + "_0"
_BLOB_ID_1 = "video_" + "1" * 12 + "b" * 14 + "_1"
_PNG_BYTES = bytes.fromhex("89504e470d0a1a0a") + b"png payload"
_FIXED_NOW_MS = 1_800_000_000_000
_RETENTION_MS = 7 * 24 * 3600 * 1000

_FILES = {"index.html": "<h1>v1</h1>", "assets/app.js": "window.VERSION = 1"}


def _load(name: str) -> Any:
    """Import a to-be-created module; None until the implementation lands."""
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ModuleNotFoundError, ValueError):
        return None
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _real(name: str) -> Any:
    """The module object the *implementation* sees (the flag patch face).

    ``_load`` execs a fresh copy for existence probes; monkeypatching that
    copy would not touch the constants the service and the routes read.
    """
    return importlib.import_module(name)


async def _close(app: Any) -> None:
    if hasattr(app, "close"):
        result = app.close()
        if hasattr(result, "__await__"):
            await result


async def _service(tmp_path: Path) -> tuple[Any, Any]:
    module = _real("opencode.artifact.service")
    assert module is not None and hasattr(module, "ArtifactService"), (
        "opencode.artifact.service.ArtifactService must exist"
    )
    db_path = tmp_path / "contract.db"
    _require_success(_upgrade_head(db_path))
    app = App(db_path, working_dir=str(tmp_path / "work"))
    service = cast(Any, module).ArtifactService(
        database=app._database, data_dir=str(tmp_path / "data"), directory=str(tmp_path / "work")
    )
    return service, app


async def _api(tmp_path: Path) -> tuple[Any, Any, Any]:
    assert "artifact" in inspect.signature(create_app).parameters, "create_app must take the artifact service"
    service, app = await _service(tmp_path)
    api = _CREATE_APP_ANY(app, token=_TOKEN, artifact=service)
    return api, app, service


async def _request(
    api: Any,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[bytes, bytes], Any, bytes]:
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
    return status, headers, decoded, payload


# ---------------------------------------------------------------------------
# group 1 -- the flag gate
# ---------------------------------------------------------------------------


def test_flag_constant_defaults_on_in_local_channel() -> None:
    module = _load("opencode.artifact.service")
    assert module is not None, "opencode.artifact.service must exist"
    assert getattr(module, "ARTIFACTS_ENABLED", None) is True


async def test_disabled_create_raises_and_tool_adapters_hidden(tmp_path: Path) -> None:
    module = _real("opencode.artifact.service")
    tools_module = _real("opencode.tools.builtin.artifact")
    assert tools_module is not None, "opencode.tools.builtin.artifact must exist"
    service, app = await _service(tmp_path)
    try:
        original = cast(Any, module).ARTIFACTS_ENABLED
        cast(Any, module).ARTIFACTS_ENABLED = False
        try:
            with pytest.raises(Exception, match=r"(?i)disabl"):
                await service.create({"title": "x", "files": _FILES})
            adapters_off = cast(Any, tools_module).adapters(service=service)
            assert adapters_off == [], "the flag off must hide all four tool adapters"
        finally:
            cast(Any, module).ARTIFACTS_ENABLED = original
        assert cast(Any, tools_module).TOOL_SPECS == [
            "opencode.artifact_create",
            "opencode.artifact_read",
            "opencode.artifact_update",
            "opencode.artifact_delete",
        ]
    finally:
        await _close(app)


async def test_disabled_http_is_403(tmp_path: Path) -> None:
    module = _real("opencode.artifact.service")
    api, app, _service = await _api(tmp_path)
    try:
        original = cast(Any, module).ARTIFACTS_ENABLED
        cast(Any, module).ARTIFACTS_ENABLED = False
        try:
            status, _headers, _body, _raw = await _request(
                api,
                "POST",
                "/api/experimental/artifacts",
                body={"title": "x", "files": _FILES},
            )
            assert status == 403
        finally:
            cast(Any, module).ARTIFACTS_ENABLED = original
    finally:
        await _close(app)


# ---------------------------------------------------------------------------
# group 2 -- service CRUD and validation
# ---------------------------------------------------------------------------


async def test_create_stores_local_files_and_resolves_entry(tmp_path: Path) -> None:
    service, app = await _service(tmp_path)
    try:
        info = await service.create({"title": "Flamegraph", "files": _FILES})
        assert str(info["id"]).startswith("art_")
        assert info["entry"] == "index.html"
        assert info["title"] == "Flamegraph"
        root = Path(str(service.data_dir)) / "artifacts" / str(info["id"])
        assert (root / "index.html").read_text(encoding="utf-8") == _FILES["index.html"]
        assert (root / "assets" / "app.js").read_text(encoding="utf-8") == _FILES["assets/app.js"]
    finally:
        await _close(app)


async def test_get_update_and_delete_round_trip(tmp_path: Path) -> None:
    service, app = await _service(tmp_path)
    try:
        info = await service.create({"title": "Draft", "files": _FILES})
        content = await service.get(info["id"])
        assert content["files"]["assets/app.js"] == _FILES["assets/app.js"]
        updated = await service.update(info["id"], {"files": {"index.html": "<h1>v2</h1>"}})
        assert updated["id"] == info["id"]
        after = await service.get(info["id"])
        assert after["files"]["index.html"] == "<h1>v2</h1>"
        assert "assets/app.js" not in after["files"], "update replaces the whole file set"
        await service.delete(info["id"])
        with pytest.raises(LookupError):
            await service.get(info["id"])
    finally:
        await _close(app)


async def test_validation_rejects_traversal_blank_title_empty_files(tmp_path: Path) -> None:
    service, app = await _service(tmp_path)
    try:
        with pytest.raises(ValueError, match=r"(?i)path|relative"):
            await service.create({"title": "Bad", "files": {"../escape.html": "nope"}})
        with pytest.raises(ValueError, match=r"(?i)title"):
            await service.create({"title": "  ", "files": _FILES})
        with pytest.raises(ValueError, match=r"(?i)file"):
            await service.create({"title": "Empty", "files": {}})
        info = await service.create({"title": "Only", "files": {"notes.md": "# hi"}})
        assert info["entry"] == "notes.md", "a sole file becomes the entry"
        with pytest.raises(ValueError, match=r"(?i)entry"):
            await service.create({"title": "Missing entry", "files": _FILES, "entry": "gone.html"})
    finally:
        await _close(app)


async def test_missing_artifact_and_session_errors(tmp_path: Path) -> None:
    service, app = await _service(tmp_path)
    try:
        with pytest.raises(LookupError):
            await service.get("art_missing0000000000000000")
        with pytest.raises(LookupError):
            await service.create({"title": "Ghost", "sessionID": "ses_does_not_exist", "files": _FILES})
    finally:
        await _close(app)


async def test_list_filters_by_session_and_provider(tmp_path: Path) -> None:
    service, app = await _service(tmp_path)
    try:
        first = await service.create({"title": "A", "files": _FILES})
        await service.create({"title": "B", "files": _FILES})
        listed = await service.list()
        ids = [item["id"] for item in listed]
        assert first["id"] in ids and len(ids) == 2
    finally:
        await _close(app)


# ---------------------------------------------------------------------------
# group 3 -- the provider registry
# ---------------------------------------------------------------------------


async def test_register_provider_default_override_and_prompt_rendering(tmp_path: Path) -> None:
    service, app = await _service(tmp_path)
    try:
        store: dict[str, dict[str, str]] = {}

        async def p_create(input: dict[str, Any]) -> dict[str, Any]:
            store[cast(str, input["id"])] = dict(cast(dict[str, str], input["files"]))
            return {"url": f"https://cf.example.test/{input['id']}/"}

        async def p_read(artifact_id: str) -> dict[str, str]:
            return store.get(artifact_id, {})

        async def p_update(artifact_id: str, input: dict[str, Any]) -> dict[str, Any]:
            store[artifact_id] = dict(cast(dict[str, str], input["files"]))
            return {"url": f"https://cf.example.test/{artifact_id}/"}

        async def p_delete(artifact_id: str) -> None:
            store.pop(artifact_id, None)

        await service.register_provider(
            id="cloudflare",
            name="Cloudflare",
            capabilities=["static", "server-code"],
            prompt="Artifacts deploy to Cloudflare.",
            create=p_create,
            read=p_read,
            update=p_update,
            delete=p_delete,
        )
        await service.set_default_provider("cloudflare")
        text = cast(str, service.provider_prompt_text())
        assert "Default provider (cloudflare [static, server-code]): Artifacts deploy to Cloudflare." in text
        assert "Other active providers (pass provider to override): local [static]." in text
        remote = await service.create({"title": "Remote", "files": {"index.html": "x"}})
        assert remote["url"] == f"https://cf.example.test/{remote['id']}/"
        assert store[cast(str, remote["id"])] == {"index.html": "x"}
        local = await service.create({"title": "Local", "files": {"index.html": "y"}, "provider": "local"})
        assert cast(str, local["url"]).startswith("/api/experimental/artifacts/")
        assert local["provider"] == "local"
    finally:
        await _close(app)


# ---------------------------------------------------------------------------
# group 4 -- the HTTP face
# ---------------------------------------------------------------------------


async def test_providers_route_lists_local_default(tmp_path: Path) -> None:
    api, app, _service = await _api(tmp_path)
    try:
        status, _headers, body, _raw = await _request(api, "GET", "/api/experimental/artifacts/providers")
        assert status == 200
        providers = cast(list[dict[str, Any]], cast(dict[str, Any], body)["data"])
        assert providers == [{"id": "local", "name": "Local", "capabilities": ["static"], "default": True}]
    finally:
        await _close(app)


async def test_http_crud_error_mapping(tmp_path: Path) -> None:
    api, app, _service = await _api(tmp_path)
    try:
        bad, _bh, _bb, _raw = await _request(api, "POST", "/api/experimental/artifacts", body={"title": "x"})
        assert bad == 422, "a payload without files is invalid"
        missing_session, _mh, _mb, _raw = await _request(
            api,
            "POST",
            "/api/experimental/artifacts",
            body={"title": "x", "files": _FILES, "sessionID": "ses_nope"},
        )
        assert missing_session == 404
        ok, _oh, created, _raw = await _request(
            api, "POST", "/api/experimental/artifacts", body={"title": "Wait stats", "files": _FILES}
        )
        assert ok == 200
        artifact_id = cast(dict[str, Any], cast(dict[str, Any], created)["data"])["id"]
        got, _gh, content, _raw = await _request(api, "GET", f"/api/experimental/artifacts/{artifact_id}")
        assert got == 200
        assert (
            cast(dict[str, Any], cast(dict[str, Any], content)["data"])["files"]["index.html"] == _FILES["index.html"]
        )
        updated, _uh, _ub, _raw = await _request(
            api,
            "PATCH",
            f"/api/experimental/artifacts/{artifact_id}",
            body={"files": {"index.html": "<h1>v2</h1>"}},
        )
        assert updated == 200
        deleted, _dh, _db, _raw = await _request(api, "DELETE", f"/api/experimental/artifacts/{artifact_id}")
        assert deleted in (200, 204)
        gone, _gh2, _gb, _raw = await _request(api, "GET", f"/api/experimental/artifacts/{artifact_id}")
        assert gone == 404
        never, _nh, _nb, _raw = await _request(api, "GET", "/api/experimental/artifacts/art_never00000000000")
        assert never == 404
    finally:
        await _close(app)


async def test_http_list_and_isolation(tmp_path: Path) -> None:
    api, app, service = await _api(tmp_path)
    try:
        await service.create({"title": "One", "files": _FILES})
        status, _headers, body, _raw = await _request(api, "GET", "/api/experimental/artifacts")
        assert status == 200
        listed = cast(list[dict[str, Any]], cast(dict[str, Any], body)["data"])
        assert len(listed) == 1 and listed[0]["title"] == "One"
        other, _oh2, obody, _raw = await _request(
            api,
            "GET",
            "/api/experimental/artifacts",
            extra_headers=[(b"x-opencode-directory", b"C:/nowhere-else")],
        )
        assert other == 200
        assert cast(list[dict[str, Any]], cast(dict[str, Any], obody)["data"]) == [], "another directory sees nothing"
    finally:
        await _close(app)


async def test_view_route_sandbox_and_assets(tmp_path: Path) -> None:
    api, app, service = await _api(tmp_path)
    try:
        info = await service.create({"title": "V", "files": _FILES})
        base = f"/api/experimental/artifacts/{info['id']}/view/"
        redirect, rheaders, _rb, _raw = await _request(api, "GET", base.rstrip("/"))
        assert redirect == 308
        assert dict(rheaders).get(b"location", b"").endswith(b"/view/")
        ok, headers, _body, _raw = await _request(api, "GET", base)
        assert ok == 200
        assert dict(headers).get(b"content-security-policy") == b"sandbox allow-scripts"
        assert dict(headers).get(b"x-content-type-options") == b"nosniff"
        assert _FILES["index.html"] in _raw.decode("utf-8", "replace")
        asset, _ah, _abody, _araw = await _request(api, "GET", base + "assets/app.js")
        assert asset == 200
        assert _FILES["assets/app.js"] in _araw.decode("utf-8", "replace")
        missing, _mh3, _mb3, _raw = await _request(api, "GET", base + "gone.js")
        assert missing == 404
    finally:
        await _close(app)


# ---------------------------------------------------------------------------
# group 5 -- the blob store and its raw route
# ---------------------------------------------------------------------------


def test_blob_store_write_read_sweep_remove(tmp_path: Path) -> None:
    module = _load("opencode.blob_store")
    assert module is not None, "opencode.blob_store must exist"
    store = cast(Any, module).BlobStore(data_dir=str(tmp_path), now_ms=lambda: _FIXED_NOW_MS)
    info = store.write(_BLOB_ID, _PNG_BYTES)
    assert info == {
        "id": _BLOB_ID,
        "mime": "image/png",
        "bytes": len(_PNG_BYTES),
        "expires_at": _FIXED_NOW_MS + _RETENTION_MS,
    }
    assert store.read_bytes(_BLOB_ID) == _PNG_BYTES
    assert store.read(_BLOB_ID)["mime"] == "image/png"
    assert not list(tmp_path.rglob("*.part")), "no .part residue after a committed write"
    assert store.sweep(_FIXED_NOW_MS) == 0, "a fresh blob survives the sweep"
    store.write(_BLOB_ID_1, b"second")
    assert store.sweep(_FIXED_NOW_MS + _RETENTION_MS + 1) == 2
    with pytest.raises(LookupError):
        store.read(_BLOB_ID)
    with pytest.raises(LookupError):
        store.read("video_" + "9" * 12 + "z" * 14 + "_2")


def test_blob_store_rejects_foreign_ids(tmp_path: Path) -> None:
    module = _load("opencode.blob_store")
    assert module is not None
    store = cast(Any, module).BlobStore(data_dir=str(tmp_path), now_ms=lambda: _FIXED_NOW_MS)
    with pytest.raises(ValueError):
        store.write("../../../etc/passwd", b"x")
    with pytest.raises(ValueError):
        store.write("video_notvalid_0", b"x")


async def test_blob_http_route_protocol(tmp_path: Path) -> None:
    api, app, service = await _api(tmp_path)
    module = _load("opencode.blob_store")
    assert module is not None
    try:
        data_root = Path(str(cast(Any, service).data_dir))
        store = cast(Any, module).BlobStore(data_dir=str(data_root), now_ms=lambda: _FIXED_NOW_MS)
        store.write(_BLOB_ID, _PNG_BYTES)
        path = f"/api/experimental/blob/{_BLOB_ID}"
        ok, headers, _body, _raw = await _request(api, "GET", path)
        assert ok == 200
        assert _raw == _PNG_BYTES
        assert dict(headers).get(b"etag") == f'"{_BLOB_ID}"'.encode()
        assert b"attachment" in dict(headers).get(b"content-disposition", b"")
        nm, _nmh, _nmb, _raw = await _request(
            api, "GET", path, extra_headers=[(b"if-none-match", f'"{_BLOB_ID}"'.encode())]
        )
        assert nm == 304
        ranged, _rh, _ranged_body, _raw = await _request(api, "GET", path, extra_headers=[(b"range", b"bytes=0-3")])
        assert ranged == 206
        assert _raw == _PNG_BYTES[:4]
        unsat, _uh2, _ub2, _raw = await _request(api, "GET", path, extra_headers=[(b"range", b"bytes=100000-100001")])
        assert unsat == 416
        missing, _mh4, _mb4, _raw = await _request(api, "GET", f"/api/experimental/blob/{_BLOB_ID_1}")
        assert missing == 404
    finally:
        await _close(app)
