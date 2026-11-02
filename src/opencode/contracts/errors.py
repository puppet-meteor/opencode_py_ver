"""Core error wire model (CORE-CONTRACTS §8): ``{code, message, retryable, details?}``.

Every error crossing a seam uses this shape; user errors and internal faults
share it while logging differs. ``details`` must be sanitized JSON: no auth
headers, no raw SDK requests, no credentials.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from opencode.contracts.messages import JsonObject


class CoreErrorCode(StrEnum):
    """Closed code registry from §8; R1 slices surface a subset of it.

    Later slices may need more codes, but codes are never removed or
    renumbered: they are part of the wire contract.
    """

    VALIDATION_ERROR = "ValidationError"
    UNSUPPORTED_CAPABILITY = "UnsupportedCapability"
    NOT_FOUND = "NotFound"
    SESSION_BUSY = "SessionBusy"
    CONFLICT = "Conflict"
    IDEMPOTENCY_CONFLICT = "IdempotencyConflict"
    INVALID_CURSOR = "InvalidCursor"
    PERMISSION_DENIED = "PermissionDenied"
    APPROVAL_UNAVAILABLE = "ApprovalUnavailable"
    APPROVAL_EXPIRED = "ApprovalExpired"
    TOOL_TIMEOUT = "ToolTimeout"
    TOOL_FAILED = "ToolFailed"
    EXECUTION_UNKNOWN = "ExecutionUnknown"
    PROVIDER_UNAVAILABLE = "ProviderUnavailable"
    PROVIDER_STREAM_INTERRUPTED = "ProviderStreamInterrupted"
    CONTEXT_LIMIT_EXCEEDED = "ContextLimitExceeded"
    SLOW_CONSUMER = "SlowConsumer"
    STORAGE_CORRUPT = "StorageCorrupt"
    MIGRATION_REQUIRED = "MigrationRequired"
    SESSION_READ_ONLY = "SessionReadOnly"
    SESSION_CLOSED = "SessionClosed"
    LIFECYCLE_CLEANUP_FAILED = "LifecycleCleanupFailed"


class CoreError(BaseModel):
    """Stable wire shape for every error crossing a seam (§8)."""

    model_config = ConfigDict(extra="forbid")

    code: CoreErrorCode
    message: str = Field(min_length=1)
    retryable: bool
    details: JsonObject = Field(default_factory=dict)
