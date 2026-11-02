"""Shared wire primitives for session messages (CORE-CONTRACTS §1/§2.1).

Holds the JSON extension types and the UTC timestamp rule that every wire model
in this package reuses, plus the first S1 message shape (``UserMessage``).
Assistant/idle message models arrive with R2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue

type JsonObject = dict[str, JsonValue]


def _as_utc(value: datetime) -> datetime:
    """Reject naive timestamps, normalize aware ones to UTC (fail closed, §1)."""
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return value.astimezone(UTC)


UtcDateTime = Annotated[datetime, AfterValidator(_as_utc)]


class UserMessage(BaseModel):
    """User-authored session message (§2.1 ``user`` row).

    ``files`` stays out until media lands (M6); Phase 1 callers send plain text,
    and a non-empty ``files`` list is rejected as UnsupportedCapability at the
    tool/config layer, not here.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: Literal["user"]
    created_at: UtcDateTime
    metadata: JsonObject = Field(default_factory=dict)
    text: str
