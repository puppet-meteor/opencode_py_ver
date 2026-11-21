"""The artifact service (upstream ``core/src/artifact.ts``, 571 lines).

Artifacts are multi-file bundles (an HTML report plus its assets) created by
the model through four tools or over HTTP, stored by a pluggable provider.
The built-in ``local`` provider writes ``<data_dir>/artifacts/<id>/`` with a
staging directory and a backup rename, so an update never leaves a torn
tree; records live in the ``artifact`` sqlite table (the upstream KV record
``artifact:item:<id>`` with its StoredRecord fields, the ItemPrefix folded
into the primary key).

Recorded py deviations (doc/plans/port-232-increments.md T-C3-011): the
per-directory ``experimental.artifacts`` gate is the process-level
:data:`ARTIFACTS_ENABLED` constant (T-C3-009 decision 2 house style); the
projectID isolation filter is the record's ``directory`` (py has no Project
service); no console remote provider (no Console integration face); no
event emission on delete (py has no per-session event face here); IDs are
``art_`` + uuid4 hex (the upstream ascending counter needs the shared id
factory; list order is the sqlite insertion order either way).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from opencode import flags
from opencode.blob_store import BlobStore
from opencode.util.mime import detect_file_mime

#: The TS ``Flag.enabled("artifacts")`` gate, resolved through the T-C3-007
#: flag chain at import time (channel "local": on by default; turn it off
#: with ``OPENCODE_FLAGS=-artifacts``).
ARTIFACTS_ENABLED = flags.resolve_from_environment()["artifacts"]["enabled"]

LOCAL_PROVIDER_ID = "local"


def viewURL(artifact_id: str) -> str:
    """The TS ``viewURL``: the sandboxed view route with its trailing slash."""
    return f"/api/experimental/artifacts/{artifact_id}/view/"


class ArtifactDisabledError(RuntimeError):
    """The TS Artifact.DisabledError: the flag is off."""


class ArtifactNotFoundError(LookupError):
    """The TS Artifact.NotFoundError: no record for that id."""


class ArtifactProviderNotFoundError(LookupError):
    """The TS Artifact.ProviderNotFoundError."""


class ArtifactInvalidError(ValueError):
    """The TS Artifact.InvalidError (reason/field)."""

    def __init__(self, reason: str, field: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.field = field


class ArtifactProviderError(RuntimeError):
    """The TS Artifact.ProviderError: a provider call failed."""


def _normalize_path(raw: str) -> str:
    """The TS ``normalizePath``: relative, no traversal, no NUL."""
    trimmed = raw.replace("\\", "/")
    trimmed = re.sub(r"^(?:\./)+", "", trimmed).lstrip("/")
    if not trimmed or "\x00" in trimmed:
        raise ArtifactInvalidError(f"Invalid artifact file path: {raw!r}", "files")
    segments = trimmed.split("/")
    if any(not segment or segment in (".", "..") for segment in segments):
        raise ArtifactInvalidError(f"Artifact file path must stay relative: {raw!r}", "files")
    return "/".join(segments)


def _normalize_files(files: dict[str, str]) -> dict[str, str]:
    """The TS ``normalizeFiles``: at least one file, clean relative keys."""
    if not files:
        raise ArtifactInvalidError("Artifact must include at least one file", "files")
    normalized: dict[str, str] = {}
    for raw_path, content in files.items():
        normalized[_normalize_path(raw_path)] = content
    return normalized


def _resolve_entry(files: dict[str, str], entry: str | None) -> str:
    """The TS ``resolveEntry``: explicit must exist; else html, md, sole."""
    if entry is not None:
        if entry not in files:
            raise ArtifactInvalidError(f"Entry file not in artifact: {entry!r}", "entry")
        return entry
    for candidate in ("index.html", "index.md"):
        if candidate in files:
            return candidate
    if len(files) == 1:
        return next(iter(files))
    raise ArtifactInvalidError("Artifact has no obvious entry file", "entry")


class ArtifactService:
    """CRUD over artifact records with the pluggable provider registry."""

    def __init__(
        self,
        *,
        database: Any,
        data_dir: str,
        directory: str,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._engine: AsyncEngine = database.engine
        self._data_dir = Path(data_dir)
        self._directory = directory
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._providers: dict[str, dict[str, Any]] = {}
        self._default_provider = LOCAL_PROVIDER_ID
        self._register_local_provider()

    # -- the seams the eval/contract sets measure -------------------------

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def blobs(self) -> BlobStore:
        """The blob store over the same data root (upstream Global.data)."""
        return BlobStore(data_dir=str(self._data_dir), now_ms=self._now_ms)

    def enabled(self) -> bool:
        return ARTIFACTS_ENABLED

    # -- the provider registry -------------------------------------------

    def _register_local_provider(self) -> None:
        root = self._data_dir / "artifacts"

        def _write_tree(artifact_id: str, files: dict[str, str]) -> None:
            target = root / artifact_id
            staging = root / f"{artifact_id}.staging"
            backup = root / f"{artifact_id}.backup"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            for rel_path, content in files.items():
                dest = staging / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8", newline="\n")
            had_existing = target.exists()
            if had_existing:
                os.replace(target, backup)
            try:
                os.replace(staging, target)
            except OSError:
                if had_existing:
                    os.replace(backup, target)
                raise
            if had_existing:
                shutil.rmtree(backup, ignore_errors=True)

        def _read_tree(artifact_id: str) -> dict[str, str]:
            base = root / artifact_id
            files: dict[str, str] = {}
            for path in sorted(base.rglob("*")):
                if path.is_file():
                    files[path.relative_to(base).as_posix()] = path.read_text(encoding="utf-8")
            return files

        def _local_create(input: dict[str, Any]) -> dict[str, Any]:
            _write_tree(cast(str, input["id"]), cast(dict[str, str], input["files"]))
            return {}

        def _local_update(artifact_id: str, input: dict[str, Any]) -> dict[str, Any]:
            _write_tree(artifact_id, cast(dict[str, str], input["files"]))
            return {}

        def _local_delete(artifact_id: str) -> None:
            shutil.rmtree(root / artifact_id, ignore_errors=True)

        self._providers[LOCAL_PROVIDER_ID] = {
            "id": LOCAL_PROVIDER_ID,
            "name": "Local",
            "capabilities": ["static"],
            "prompt": "Artifacts are files on this machine, served from the local server.",
            "create": _local_create,
            "read": _read_tree,
            "update": _local_update,
            "delete": _local_delete,
        }

    async def register_provider(
        self,
        *,
        id: str,
        name: str,
        capabilities: list[str],
        prompt: str,
        create: Callable[[dict[str, Any]], Any],
        read: Callable[[str], Any],
        update: Callable[[str, dict[str, Any]], Any],
        delete: Callable[[str], Any],
    ) -> None:
        """The TS provider registry ``transform(editor => editor.add(...))``."""
        if id == LOCAL_PROVIDER_ID:
            raise ArtifactInvalidError("The local provider id is reserved", "provider")
        self._providers[id] = {
            "id": id,
            "name": name,
            "capabilities": capabilities,
            "prompt": prompt,
            "create": create,
            "read": read,
            "update": update,
            "delete": delete,
        }

    async def set_default_provider(self, provider_id: str) -> None:
        if provider_id not in self._providers:
            raise ArtifactProviderNotFoundError(provider_id)
        self._default_provider = provider_id

    def provider_prompt_text(self) -> str:
        """The TS ``prompts()`` two-line create-tool description."""
        default = self._providers[self._default_provider]
        others = [item for key, item in self._providers.items() if key != self._default_provider]
        lines = [
            "Default provider ({} [{}]): {}".format(
                default["id"], ", ".join(default["capabilities"]), default["prompt"]
            )
        ]
        if others:
            lines.append(
                "Other active providers (pass provider to override): "
                + ", ".join("{} [{}]".format(item["id"], ", ".join(item["capabilities"])) for item in others)
                + "."
            )
        return "\n".join(lines)

    def providers(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item["id"],
                "name": item["name"],
                "capabilities": item["capabilities"],
                "default": item["id"] == self._default_provider,
            }
            for item in self._providers.values()
        ]

    # -- records ----------------------------------------------------------

    def _require_enabled(self) -> None:
        if not ARTIFACTS_ENABLED:
            raise ArtifactDisabledError("Artifacts are disabled (experimental.artifacts)")

    async def _require_record(self, artifact_id: str) -> dict[str, Any]:
        async with self._engine.connect() as connection:
            row = await connection.execute(text("SELECT info FROM artifact WHERE id = :id").bindparams(id=artifact_id))
        found = row.first()
        if found is None:
            raise ArtifactNotFoundError(f"Artifact not found: {artifact_id}")
        return cast(dict[str, Any], json.loads(found[0]))

    async def _check_session(self, session_id: str | None) -> None:
        if session_id is None:
            return
        async with self._engine.connect() as connection:
            row = await connection.execute(
                text("SELECT 1 FROM sessions WHERE session_id = :sid").bindparams(sid=session_id)
            )
        if row.first() is None:
            raise ArtifactNotFoundError(f"Session not found: {session_id}")

    async def create(self, input: dict[str, Any]) -> dict[str, Any]:
        """Create one artifact; returns its Info."""
        self._require_enabled()
        raw_files = cast(dict[str, str], input.get("files") or {})
        session_id = cast(str | None, input.get("sessionID"))
        await self._check_session(session_id)
        title = cast(str, input.get("title", "")).strip()
        if not title:
            raise ArtifactInvalidError("Artifact title must not be empty", "title")
        files = _normalize_files(raw_files)
        entry = _resolve_entry(files, cast(str | None, input.get("entry")))
        provider_id = cast(str, input.get("provider") or self._default_provider)
        provider = self._providers.get(provider_id)
        if provider is None:
            raise ArtifactProviderNotFoundError(provider_id)
        artifact_id = "art_" + uuid4().hex
        now = self._now_ms()
        try:
            result = provider["create"]({"id": artifact_id, "title": title, "entry": entry, "files": files})
            if hasattr(result, "__await__"):
                result = await result
        except Exception as error:
            raise ArtifactProviderError(str(error)) from error
        url = cast(dict[str, Any], result or {}).get("url") or viewURL(artifact_id)
        info: dict[str, Any] = {
            "id": artifact_id,
            "provider": provider_id,
            "title": title,
            "entry": entry,
            "url": url,
            "createdAt": now,
            "updatedAt": now,
        }
        if session_id is not None:
            info["sessionID"] = session_id
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO artifact (id, info, directory, project_id) VALUES (:id, :info, :directory, NULL)"
                ).bindparams(id=artifact_id, info=json.dumps(info), directory=self._directory)
            )
        return info

    async def get(self, artifact_id: str) -> dict[str, Any]:
        """Info plus the provider-read files (the TS Content face)."""
        self._require_enabled()
        info = await self._require_record(artifact_id)
        provider = self._providers.get(cast(str, info["provider"]))
        if provider is None:
            raise ArtifactProviderNotFoundError(cast(str, info["provider"]))
        try:
            files = provider["read"](artifact_id)
            if hasattr(files, "__await__"):
                files = await files
        except Exception as error:
            raise ArtifactProviderError(str(error)) from error
        return {"info": info, "files": files}

    async def update(self, artifact_id: str, input: dict[str, Any]) -> dict[str, Any]:
        """Whole-file-set replacement at the same id (title optional)."""
        self._require_enabled()
        await self._check_session(cast(str | None, input.get("sessionID")))
        info = await self._require_record(artifact_id)
        title = input.get("title")
        if title is None:
            title = info["title"]
        else:
            title = cast(str, title).strip()
        if not title:
            raise ArtifactInvalidError("Artifact title must not be empty", "title")
        if "files" in input and input["files"] is not None:
            files = _normalize_files(cast(dict[str, str], input["files"]))
            entry = _resolve_entry(files, cast(str | None, input.get("entry")))
        else:
            files = cast(dict[str, str], (await self.get(artifact_id))["files"])
            entry = info["entry"]
            if input.get("entry") is not None:
                entry = _resolve_entry(files, cast(str, input["entry"]))
        provider = self._providers.get(cast(str, info["provider"]))
        if provider is None:
            raise ArtifactProviderNotFoundError(cast(str, info["provider"]))
        try:
            result = provider["update"](artifact_id, {"title": title, "entry": entry, "files": files})
            if hasattr(result, "__await__"):
                result = await result
        except Exception as error:
            raise ArtifactProviderError(str(error)) from error
        url = cast(dict[str, Any], result or {}).get("url") or info["url"]
        updated = {**info, "title": title, "entry": entry, "url": url, "updatedAt": self._now_ms()}
        async with self._engine.begin() as connection:
            await connection.execute(
                text("UPDATE artifact SET info = :info WHERE id = :id").bindparams(
                    info=json.dumps(updated), id=artifact_id
                )
            )
        return updated

    async def delete(self, artifact_id: str) -> None:
        self._require_enabled()
        info = await self._require_record(artifact_id)
        provider = self._providers.get(cast(str, info["provider"]))
        if provider is not None:
            try:
                result = provider["delete"](artifact_id)
                if hasattr(result, "__await__"):
                    await result
            except Exception as error:
                raise ArtifactProviderError(str(error)) from error
        async with self._engine.begin() as connection:
            await connection.execute(text("DELETE FROM artifact WHERE id = :id").bindparams(id=artifact_id))

    async def list(
        self,
        *,
        session_id: str | None = None,
        provider: str | None = None,
        directory: str | None = None,
    ) -> list[dict[str, Any]]:
        """Records in insertion order, filtered (the sqlite rowid order)."""
        self._require_enabled()
        query = "SELECT info, directory FROM artifact ORDER BY rowid"
        async with self._engine.connect() as connection:
            rows = await connection.execute(text(query))
            listed = [(json.loads(row[0]), row[1]) for row in rows.fetchall()]
        result: list[dict[str, Any]] = []
        for info, row_directory in listed:
            if directory is not None and row_directory != directory:
                continue
            if session_id is not None and info.get("sessionID") != session_id:
                continue
            if provider is not None and info.get("provider") != provider:
                continue
            result.append(info)
        return result

    # -- the view face ----------------------------------------------------

    def local_file(self, artifact_id: str, rel_path: str) -> tuple[bytes, str]:
        """One local-provider file's bytes and mime (the /view face).

        The path goes through the same normalization as create, so a
        traversal attempt is rejected before any filesystem touch.
        """
        clean = _normalize_path(rel_path)
        base = (self._data_dir / "artifacts" / artifact_id).resolve()
        target = (base / clean).resolve()
        if str(target) != str(base) and not str(target).startswith(str(base) + os.sep):
            raise ArtifactNotFoundError(f"Artifact file not found: {rel_path}")
        try:
            data = target.read_bytes()
        except OSError as error:
            raise ArtifactNotFoundError(f"Artifact file not found: {rel_path}") from error
        return data, detect_file_mime(clean, data)
