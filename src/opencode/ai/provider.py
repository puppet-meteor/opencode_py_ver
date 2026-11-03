"""Provider seam (CORE-CONTRACTS §2.2 L82-84): request in, chunk stream out.

The only runtime face of the provider layer: ``Provider.stream`` returns an
async iterator consumed with ``async for`` (never ``await``-ed). Implementations
are async generators and must support closing (``aclose``) and cancellation
(CancelledError propagation, §2.3 L102) — both are pinned by tests for the
concrete MAAS adapter in ``opencode.ai.maas``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from opencode.contracts.llm import GenerationRequest, ProviderChunk


class Provider(Protocol):
    """§2.2 L83: ``stream`` is the whole provider contract for Phase 1."""

    def stream(self, request: GenerationRequest) -> AsyncIterator[ProviderChunk]: ...
