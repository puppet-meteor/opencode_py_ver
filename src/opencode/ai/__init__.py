"""Provider layer: ``Provider`` protocol + OpenAI-compatible MAAS adapter.

Imports of any ``opencode.ai`` submodule pull in ``openai``/``httpx2`` — the SDK
lives and dies inside this package (A-PROVIDER red line: SDK types never cross
the adapter boundary; callers only see contracts models).
"""

from __future__ import annotations

from opencode.ai.maas import MaasAdapter, MaasTimeouts
from opencode.ai.provider import Provider

__all__ = ["MaasAdapter", "MaasTimeouts", "Provider"]
