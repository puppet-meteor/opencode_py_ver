"""The flag chain: upstream ``schema/config/flag.ts`` + ``core/flag.ts``.

One source of truth for every experimental gate (the T-C3-006/T-C3-004
module constants are its resolved defaults). The upstream resolution
order, verbatim:

1. ``OPENCODE_FLAGS`` (comma separated): ``key`` turns a flag on, ``-key``
   off, ``all`` turns on every flag except opt-in-only ones (a ``-key``
   in the same list still carves out), and an old alias reads like the
   new key. Highest priority.
2. ``experimental.<key>`` from the highest-priority config layer that
   SETS ``experimental`` -- the whole block replaces a lower layer's,
   keys never merge across layers.
3. ``all_flags: true`` (inside ``experimental``) from the highest-priority
   layer that sets it; ``false`` falls through, it never turns flags off.
   A global switch this way survives a project layer's own
   ``experimental`` block.
4. Default: on for every non-opt-in-only flag on the ``local`` and ``dev``
   channels, off on any other channel (releases keep flags off so none
   turns on by accident). Opt-in-only flags default off everywhere and
   skip both wildcards.

Recorded py deviations: there is no App/channel service yet -- ``channel``
is a parameter defaulting to ``"local"`` (the py build is a local product;
the release-channel gate revives when one exists), and config layers are
merged layer values in priority order (the py ``ConfigLayer`` face), not
Effect Config documents. A remote kill switch would slot ABOVE the env
overrides, as upstream notes.
"""

from __future__ import annotations

import os
from typing import Any

#: The ``OPENCODE_FLAGS`` entry that enables all flags except opt-in-only.
WILDCARD = "all"

#: Every flag with its description (upstream registry, verbatim).
REGISTRY: dict[str, str] = {
    "todo": "Enable the experimental todowrite tool.",
    "media_blob": "Store new session media once in a deduplicated blob table instead of inline in every message row.",
    "automations": (
        "Run a prompt or Code Mode action on a timer, or on a connected MCP server's push event "
        "when mcp_events is enabled."
    ),
    "artifacts": "Enable experimental artifacts backed by pluggable providers.",
    "video_generate": "Enable the experimental video generation API.",
    "console_watches": (
        "Watch Slack threads, GitHub pull requests, issues, commits and Actions runs, and the "
        "workspace's MCP servers' events through OpenCode Console, and wake sessions when they change."
    ),
    "mcp_events": (
        "Enable experimental MCP Events, a draft MCP extension: declare support to MCP servers and "
        "read the events of servers that offer them."
    ),
    "full_codemode": (
        "Enable the full Code Mode work in progress. Off by default in every build, including local "
        "and dev; opt in with OPENCODE_FLAGS=full_codemode or experimental.full_codemode: true."
    ),
}

#: Flags that must be named explicitly: both wildcards and the channel
#: default skip them.
OPT_IN_ONLY = frozenset({"full_codemode"})

#: Earlier names of a flag, still read from ``experimental.<name>`` and
#: ``OPENCODE_FLAGS`` when the new key is unset.
ALIASES: dict[str, str] = {"schedules": "automations"}

_ON_BY_DEFAULT_CHANNELS = frozenset({"local", "dev"})


def _parse_overrides(overrides: str | None) -> dict[str, bool]:
    env: dict[str, bool] = {}
    for item in (overrides or "").split(","):
        item = item.strip()
        if not item:
            continue
        env[item[1:] if item.startswith("-") else item] = not item.startswith("-")
    return env


def resolve(
    entries: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    channel: str = "local",
    overrides: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve every flag to ``{key: {"enabled": bool, "source": str}}``.

    ``entries`` are merged config layer values in priority order (highest
    priority last); ``overrides`` is the ``OPENCODE_FLAGS`` string
    (defaults to none -- callers read the environment themselves, keeping
    this pure and testable).
    """
    env = _parse_overrides(overrides)
    all_wildcard = env.get(WILDCARD) is True
    experimental: dict[str, Any] = {}
    for layer in entries:
        block = layer.get("experimental")
        if isinstance(block, dict):
            experimental = block
    all_flags: bool | None = None
    for layer in entries:
        block = layer.get("experimental")
        if isinstance(block, dict) and "all_flags" in block:
            all_flags = block["all_flags"] is True
    resolved: dict[str, dict[str, Any]] = {}
    for key in REGISTRY:
        opt_in = key in OPT_IN_ONLY
        alias = next((old for old, new in ALIASES.items() if new == key), None)
        override = env.get(key)
        if override is None and alias is not None:
            override = env.get(alias)
        if override is None and all_wildcard and not opt_in:
            override = True
        configured = experimental.get(key)
        if configured is None and alias is not None:
            configured = experimental.get(alias)
        if configured is None and all_flags and not opt_in:
            configured = True
        if override is not None:
            resolved[key] = {"enabled": override, "source": "env"}
        elif configured is not None:
            resolved[key] = {"enabled": bool(configured), "source": "config"}
        else:
            resolved[key] = {"enabled": not opt_in and channel in _ON_BY_DEFAULT_CHANNELS, "source": "default"}
    return resolved


def resolve_from_environment(
    entries: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    channel: str = "local",
) -> dict[str, dict[str, Any]]:
    """:func:`resolve` with the process ``OPENCODE_FLAGS`` (the module
    constants' import-time face)."""
    return resolve(entries, channel, os.environ.get("OPENCODE_FLAGS"))
