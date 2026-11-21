"""The file blob store (upstream ``core/src/blob-store.ts``, 110 lines).

Blobs are immutable files named by their strict ID (no extension) under
``<data_dir>/blobs/``; the mime type is sniffed from the first bytes
(upstream reads the first 64, which covers every signature the magic table
needs). A write lands at ``<id>.part`` first and renames into place, so a
reader never observes a torn file. Retention is seven days: ``sweep`` takes
the current epoch milliseconds and deletes every blob whose expiry has
passed, plus any ``.part`` residue a crashed write left behind.

The only upstream writer is video generation (not ported this ticket --
see doc/plans/port-232-increments.md T-C3-011); the store and its raw HTTP
route are the durable base the future video ticket consumes.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from opencode.util.mime import detect

#: Upstream RETENTION = Duration.days(7), in epoch milliseconds.
RETENTION_MS = 7 * 24 * 3600 * 1000

#: Upstream DIRECTORY = "blobs".
DIRECTORY = "blobs"

#: The server names files after the ID, so it is strict (schema/blob-store.ts).
_ID_PATTERN = re.compile(r"^video_[0-9a-f]{12}[0-9A-Za-z]{14}_(0|[1-9][0-9]{0,2})$")


class BlobNotFoundError(LookupError):
    """The TS BlobStore.NotFoundError: no blob with that id."""

    def __init__(self, blob_id: str) -> None:
        super().__init__(f"Blob not found: {blob_id}")
        self.blob_id = blob_id


def valid_id(blob_id: str) -> bool:
    return _ID_PATTERN.match(blob_id) is not None


class BlobStore:
    """Immutable content-addressed-by-id blobs under one data directory."""

    def __init__(
        self,
        *,
        data_dir: str,
        now_ms: Callable[[], int],
    ) -> None:
        self._data_dir = Path(data_dir)
        self._now_ms = now_ms

    @property
    def blobs_dir(self) -> Path:
        return self._data_dir / DIRECTORY

    def _path_of(self, blob_id: str) -> Path:
        if not valid_id(blob_id):
            raise ValueError(f"Invalid blob id: {blob_id!r}")
        return self.blobs_dir / blob_id

    def write(self, blob_id: str, data: bytes) -> dict[str, object]:
        """Write one blob atomically and return its Info.

        The file's mtime IS the logical write moment (the injected clock),
        so read/sweep agree with this Info without a side table.
        """
        now_ms = self._now_ms()
        target = self._path_of(blob_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        with open(part, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, target)
        now_s = now_ms / 1000
        os.utime(target, (now_s, now_s))
        return {
            "id": blob_id,
            "mime": detect(data[:64]),
            "bytes": len(data),
            "expires_at": now_ms + RETENTION_MS,
        }

    def read(self, blob_id: str) -> dict[str, object]:
        """The blob's Info (id/mime/bytes/expires_at)."""
        target = self._path_of(blob_id)
        try:
            stat = target.stat()
            with open(target, "rb") as handle:
                head = handle.read(64)
        except OSError as error:
            raise BlobNotFoundError(blob_id) from error
        return {
            "id": blob_id,
            "mime": detect(head),
            "bytes": stat.st_size,
            "expires_at": int(stat.st_mtime * 1000) + RETENTION_MS,
        }

    def read_bytes(self, blob_id: str) -> bytes:
        """The blob's bytes (the raw HTTP route and tests)."""
        target = self._path_of(blob_id)
        try:
            return target.read_bytes()
        except OSError as error:
            raise BlobNotFoundError(blob_id) from error

    def remove(self, blob_id: str) -> None:
        """Delete one blob; a missing blob is already gone (idempotent)."""
        try:
            self._path_of(blob_id).unlink(missing_ok=True)
        except OSError:
            pass

    def sweep(self, now_ms: int) -> int:
        """Delete every expired blob and .part residue; return the count.

        A blob's expiry is its write moment plus RETENTION, so the sweep
        visits each file's mtime once (upstream FileRetention compares the
        same stamps).
        """
        removed = 0
        directory = self.blobs_dir
        if not directory.is_dir():
            return 0
        for entry in directory.iterdir():
            if entry.name.endswith(".part"):
                entry.unlink(missing_ok=True)
                removed += 1
                continue
            try:
                written_ms = int(entry.stat().st_mtime * 1000)
            except OSError:
                continue
            if written_ms + RETENTION_MS <= now_ms:
                entry.unlink(missing_ok=True)
                removed += 1
        return removed
