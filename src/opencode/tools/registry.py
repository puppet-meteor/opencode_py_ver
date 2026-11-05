"""Tool registry (R2-S5; CORE-CONTRACTS §5.1 L247-253, plan §2 R2-S5 ①).

Owns the registration side of the tool chain: :class:`ToolAdapter` (the
adapter seam an implementation plugs into), :class:`ToolCatalog` (the
immutable per-request catalog shown to the provider) and
:meth:`ToolRegistry.invoke` -- the single call entry that drives
:func:`opencode.tools.invoker.invoke_tool` under the Phase-1 serial lock
(PHASE1-SLIM L85: calls execute serially in call-start order).

Contract notes (float-reported where the plan left a choice):

- ``register`` rejects an exact ``(namespace, name, version)`` duplicate
  (§5.1 L253 RegistrationError) *and* the same ``(namespace, name)`` under
  a different version: the reversible wire name ``namespace__name``
  (provider spike §6-2) would be ambiguous otherwise.
- ``snapshot(session_id)`` returns a fresh catalog and pins it for that
  session; ``invoke`` validates the requested name against the pinned
  catalog only (a tool registered after the snapshot is unknown, keeping
  the provider-visible list and the invocable list identical).
- ``invoke(context, tool_ref, input, execution_id)`` keeps the contract's
  four-argument shape; ``tool_ref`` is the raw requested name string the
  model used (e.g. ``"fs__read"``). Resolution to a catalog entry happens
  inside the invoke so that an unknown tool still produces the full
  requested→finished(not_started) event sequence.
- Adapter lookup after catalog resolution is a direct index: the pinned
  spec was copied from this registry's own registrations, registration is
  append-only, and ``(namespace, name)`` is a unique key, so the entry is
  provably present (no defensive branch, no silent unknown-tool path).
- R4-S3 MCP naming (§5.1 L255): remote tools register under the FIXED
  ``mcp`` namespace with their own remote name (wire ``mcp__<name>``);
  the registry separately records name -> owning server, so two servers
  exposing the same tool name are rejected (cross-server collision) while
  a same-server duplicate rides the existing exact-duplicate rejection.
  ``replace_mcp_server`` swaps a server's whole set after a ``tools/list``
  change — every new entry is validated before any mutation, and
  already-pinned catalogs are immutable snapshots, so old wire names keep
  resolving there; the forced re-ask rides the policy binding
  (startup fingerprint + :meth:`ToolRegistry.mcp_catalog_version` digest,
  §5.2 L268), never a mutation of pinned state.

TS mapping (``packages/core/src/tool.ts``): RegistrationError L21,
Editor L26, Snapshot L53, Interface L43.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Protocol

from opencode.contracts.events import canonical_json
from opencode.contracts.messages import JsonObject
from opencode.contracts.tools import ToolResult, ToolSpec
from opencode.session.service import AscendingIds, Clock, IdFactory
from opencode.tools.invoker import (
    EventSink,
    ResolvedTool,
    ToolContext,
    ToolInvokeError,
    ToolOutcome,
    invoke_tool,
    validate_schema_supported,
)
from opencode.tools.policy import PermissionPolicy

#: §5.1 L250 / TS normalizedName: each half of the wire name
#: ``namespace__name`` is restricted to URL-safe characters. Reversibility
#: under a first-"__" partition holds iff the namespace contains no ``__``
#: and does not end with "_" (the name side needs no constraint: a single
#: leading "_" stays unambiguous) -- the pattern below rejects the ``__``
#: halves and the register() partition guard enforces the rest
#: (T-B-017 M-1, §6-1 scheduler ruling).
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

#: §5.1 L255 (R4-S3): remote MCP tools live in one fixed namespace. The wire
#: name is ``mcp__<remote name>`` and the owning server is tracked separately
#: (name -> server_id), so the model-facing name stays the server's own tool
#: name and cross-server name collisions stay detectable without inflating
#: the wire name with the server id.
_MCP_NAMESPACE = "mcp"


def mcp_tool_spec(name: str, description: str, input_schema: JsonObject) -> ToolSpec:
    """Build the catalog spec of one remote MCP tool (§5.1 L255, R4-S3).

    The conservative pin: ``version`` stays 1 (``tools/list`` declares no
    versions — a changed shape is a NEW catalog through
    :meth:`ToolRegistry.replace_mcp_server` and a new catalog digest, never
    a silent same-version edit, §8 closed-evolution spirit), side effects
    are assumed True (remote tools are not idempotent by default; the
    ``mcp`` namespace already defaults to ask, §5.2 L266), and results may
    carry text or file content (the ``to_tool_result`` bridge face).
    """
    return ToolSpec(
        namespace=_MCP_NAMESPACE,
        name=name,
        version=1,
        description=description,
        input_schema=input_schema,
        supported_content_types=["text", "file"],
        side_effects=True,
        idempotency_support=False,
    )


class RegistrationError(ValueError):
    """§5.1 L253: a tool registration was rejected (duplicate or invalid).

    Raised by :meth:`ToolRegistry.register` for a malformed adapter, an
    invalid namespace/name, an input schema outside the strict validator's
    supported subset, an exact ``(namespace, name, version)`` duplicate, or
    a wire-name collision (same ``(namespace, name)`` under another
    version).
    """


class ToolAdapter(Protocol):
    """The adapter seam: a registered tool implementation.

    ``spec`` is the catalog entry shown to the provider; ``execute``
    receives the Invoker-validated input object (strictly validated
    against ``spec.input_schema``, never modified) and returns a raw
    :class:`~opencode.contracts.tools.ToolResult`; the Invoker owns event
    commits, normalization and the terminal outcome.
    """

    @property
    def spec(self) -> ToolSpec: ...

    async def execute(self, context: ToolContext, input: JsonObject) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    """§5.1 L253 snapshot face: the immutable catalog pinned per session.

    ``specs`` is sorted by ``(namespace, name)`` so the provider-visible
    list is deterministic across processes; ``resolve`` maps the wire name
    (``namespace__name``) back to the spec for the invoke path.
    """

    specs: tuple[ToolSpec, ...]
    _by_wire_name: dict[str, ToolSpec]

    def resolve(self, name: str) -> ToolSpec | None:
        """Resolve a wire name (e.g. ``fs__read``) to its pinned spec."""
        return self._by_wire_name.get(name)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_session_id(session_id: object) -> None:
    """§3.1: session ids on the tools surface must be non-empty ^ses_ strings."""
    if not isinstance(session_id, str) or not session_id:
        raise ToolInvokeError("session_id must be a non-empty string")
    if not session_id.startswith("ses_"):
        raise ToolInvokeError("session_id must match ^ses_")


def _require_mcp_server_id(server_id: str) -> str:
    """§5.1 L255 (R4-S3): the MCP server identity must be a non-empty string."""
    if not isinstance(server_id, str) or not server_id:
        raise RegistrationError("MCP server_id must be a non-empty string")
    return server_id


def _validated_spec(adapter: ToolAdapter) -> ToolSpec:
    """The adapter's spec after the full registration shape checks (no state).

    Shared by :meth:`ToolRegistry.register` and the R4-S3 MCP faces: the
    adapter shape, the wire-name pattern/partition rules and the strict
    input-schema subset run identically everywhere. Duplicate/ownership
    checks need registry state and stay with the callers.
    """
    spec = getattr(adapter, "spec", None)
    if not isinstance(spec, ToolSpec):
        raise RegistrationError("adapter.spec must be a ToolSpec")
    if not callable(getattr(adapter, "execute", None)):
        raise RegistrationError("adapter.execute must be a callable")
    for label, part in (("namespace", spec.namespace), ("name", spec.name)):
        if not isinstance(part, str) or not _NAME_PATTERN.fullmatch(part) or "__" in part:
            raise RegistrationError(
                f"tool {label} {part!r} must match ^[A-Za-z0-9_-]+$ and contain no '__' (wire-name reversibility)"
            )
    # T-B-017 M-1 (§6-1 pre-merge hotfix): the wire name must split back
    # into exactly (namespace, name) at the first "__". Given the pattern
    # check above (no "__" in either half), this guard's only new
    # interception is a namespace ending in a single "_" -- ("fs_", "read")
    # would wire to the same "fs___read" as ("fs", "_read") (silent
    # shadowing in the snapshot + a broken first-__ round trip).
    if f"{spec.namespace}__{spec.name}".partition("__") != (spec.namespace, "__", spec.name):
        raise RegistrationError(f"tool namespace {spec.namespace!r} must not end with '_' (wire-name reversibility)")
    try:
        validate_schema_supported(spec.input_schema, root=True)
    except ValueError as error:
        raise RegistrationError(f"tool {spec.namespace}__{spec.name}: {error}") from error
    return spec


class ToolRegistry:
    """§5.1 L247-253: register adapters, snapshot catalogs, invoke tools.

    Constructed with the Invoker's :term:`EventSink` (the commit callback
    the runner wires to the session store; without one, ``invoke`` fails
    closed) plus optional clock/ID factory (open point B; defaults reuse
    :class:`~opencode.session.service.AscendingIds` so ``evt_`` ids share
    one ascending format with the rest of the process).

    Phase 1 executes calls serially in call-start order: one registry-wide
    :class:`asyncio.Lock` covers the whole requested→finished pipeline, so
    concurrent invokes never interleave (PHASE1-SLIM L85).
    """

    def __init__(
        self,
        *,
        sink: EventSink | None = None,
        clock: Clock | None = None,
        ids: IdFactory | None = None,
        policy: PermissionPolicy | None = None,
    ) -> None:
        self._sink = sink
        self._clock: Clock = clock if clock is not None else _utc_now
        self._ids: IdFactory = ids if ids is not None else AscendingIds(self._clock)
        self._lock = asyncio.Lock()
        self._adapters: dict[tuple[str, str], ToolAdapter] = {}
        self._pinned: dict[str, ToolCatalog] = {}
        # R4-S3 (§5.1 L255): remote tool name -> owning MCP server id.
        self._mcp_ownership: dict[str, str] = {}
        # R3-S2: the PermissionPolicy injected into the Invoker (§5.1 L257).
        # None keeps the R2 default-allow face (no policy gate at all).
        self._policy = policy

    # -- §5.1 L251 registration --------------------------------------------

    def register(self, adapter: ToolAdapter) -> None:
        """Register an adapter; rejects duplicates and invalid entries (§5.1 L253).

        The input schema must stay inside the strict validator's supported
        subset (fail closed at registration, never an invoke-time surprise),
        and both name halves must form a reversible wire name.
        """
        spec = _validated_spec(adapter)
        wire_name = f"{spec.namespace}__{spec.name}"
        existing = self._adapters.get((spec.namespace, spec.name))
        if existing is not None and existing.spec.version == spec.version:
            raise RegistrationError(f"tool {wire_name} v{spec.version} is already registered")
        if existing is not None:
            raise RegistrationError(
                f"tool {wire_name} is already registered as v{existing.spec.version}; "
                "a changed schema must keep one version per wire name (float report)"
            )
        self._adapters[(spec.namespace, spec.name)] = adapter

    # -- §5.1 L255 MCP naming/ownership (R4-S3) ------------------------------

    def register_mcp(self, server_id: str, adapter: ToolAdapter) -> None:
        """Register one remote tool under the fixed ``mcp`` namespace (§5.1 L255).

        Name -> server ownership is recorded around the plain registration:
        a DIFFERENT server exposing an already-owned name is rejected here
        (cross-server collision), while the same server re-exposing a name
        rides :meth:`register`'s exact ``(namespace, name, version)``
        duplicate rejection (same-server collision). Wire name:
        ``mcp__<remote name>``.
        """
        server_id = _require_mcp_server_id(server_id)
        spec = _validated_spec(adapter)
        if spec.namespace != _MCP_NAMESPACE:
            raise RegistrationError(
                f"MCP tool {spec.name!r} must use the fixed namespace 'mcp', got {spec.namespace!r}"
            )
        owner = self._mcp_ownership.get(spec.name)
        if owner is not None and owner != server_id:
            raise RegistrationError(
                f"MCP tool name {spec.name!r} is already owned by server {owner!r}; two servers cannot "
                "expose the same tool name (CORE-CONTRACTS §5.1 L255)"
            )
        self.register(adapter)
        self._mcp_ownership[spec.name] = server_id

    def replace_mcp_server(self, server_id: str, adapters: Sequence[ToolAdapter]) -> None:
        """Swap one server's whole tool set after a ``tools/list`` change (§6 L304/§5.1 L255).

        The TS ``onToolsChanged`` watch is deliberately NOT ported
        (r4-slices §4 difference table 4): the host calls this explicitly
        after observing a changed listing. Every new entry is validated
        BEFORE any mutation (an atomic swap: a rejected batch leaves the
        previous set untouched), the server's previous entries are removed
        wholesale, and already-pinned catalogs are immutable
        :class:`ToolCatalog` snapshots — old wire names keep resolving
        there; the forced re-ask rides the policy binding (startup
        fingerprint + :meth:`mcp_catalog_version` digest, §5.2 L268), never
        a mutation of pinned state.
        """
        server_id = _require_mcp_server_id(server_id)
        validated: list[tuple[ToolSpec, ToolAdapter]] = []
        names: set[str] = set()
        for adapter in adapters:
            spec = _validated_spec(adapter)
            if spec.namespace != _MCP_NAMESPACE:
                raise RegistrationError(
                    f"MCP tool {spec.name!r} must use the fixed namespace 'mcp', got {spec.namespace!r}"
                )
            if spec.name in names:
                raise RegistrationError(
                    f"MCP server {server_id!r}: tool name {spec.name!r} appears twice in the new catalog"
                )
            names.add(spec.name)
            self._require_mcp_name_available(spec.name, server_id)
            validated.append((spec, adapter))
        doomed = [
            key for key in self._adapters if key[0] == _MCP_NAMESPACE and self._mcp_ownership.get(key[1]) == server_id
        ]
        for key in doomed:
            del self._adapters[key]
            self._mcp_ownership.pop(key[1], None)
        for spec, adapter in validated:
            self._adapters[(spec.namespace, spec.name)] = adapter
            self._mcp_ownership[spec.name] = server_id

    def mcp_server_of(self, name: str) -> str:
        """The owning server of a registered MCP tool name (§5.1 L255 reversible mapping)."""
        if not isinstance(name, str) or not name:
            raise RegistrationError("MCP tool name must be a non-empty string")
        owner = self._mcp_ownership.get(name)
        if owner is None:
            raise RegistrationError(f"MCP tool name {name!r} is not registered (no owning server)")
        return owner

    def mcp_catalog_version(self) -> str:
        """The deterministic digest of the provider-visible MCP tool face (§5.1 L255/§6 L304).

        sha256 over the canonical JSON of the ``(name, description,
        input_schema)`` entries sorted by name — process-order independent,
        so an equal visible face always digests equal. Any ``tools/list``
        change (rename, description/schema edit, add, remove) changes the
        digest and thereby invalidates saved approvals bound to the old
        catalog version (§5.2 L268). Server ownership is deliberately NOT
        part of the digest: a tool moving servers keeps its name and shape;
        the launch-identity change rides the startup fingerprint.
        """
        entries: list[Any] = []
        for key in sorted(self._adapters):
            if key[0] != _MCP_NAMESPACE:
                continue
            spec = self._adapters[key].spec
            entries.append({"name": spec.name, "description": spec.description, "input_schema": spec.input_schema})
        return sha256(canonical_json(entries).encode("utf-8")).hexdigest()

    def _require_mcp_name_available(self, name: str, server_id: str) -> None:
        """The name must be free or owned by ``server_id`` itself (replace face)."""
        owner = self._mcp_ownership.get(name)
        if owner is not None and owner != server_id:
            raise RegistrationError(
                f"MCP tool name {name!r} is already owned by server {owner!r}; two servers cannot "
                "expose the same tool name (CORE-CONTRACTS §5.1 L255)"
            )
        if owner is None and (_MCP_NAMESPACE, name) in self._adapters:
            raise RegistrationError(
                f"MCP tool name {name!r} is already registered outside the MCP ownership face "
                "(a plain mcp-namespace registration cannot be replaced by an MCP server set)"
            )

    # -- §5.1 L253 snapshots ------------------------------------------------

    def snapshot(self, session_id: str) -> ToolCatalog:
        """Pin the current catalog for ``session_id`` and return it (§5.1 L253).

        Every provider request pins its own catalog; ``invoke`` validates
        against the pinned one only, so a registration landing between the
        snapshot and the call is invisible to that request.
        """
        _require_session_id(session_id)
        # (namespace, name) tuple keys sort lexicographically into the
        # provider-visible (namespace, name) order.
        specs = tuple(self._adapters[key].spec for key in sorted(self._adapters))
        catalog = ToolCatalog(specs=specs, _by_wire_name={f"{s.namespace}__{s.name}": s for s in specs})
        self._pinned[session_id] = catalog
        return catalog

    # -- §5.1 L247 the single call entry ------------------------------------

    async def invoke(
        self,
        context: ToolContext,
        tool_ref: str,
        input: JsonObject,
        execution_id: str,
    ) -> ToolOutcome:
        """Invoke a tool by its raw requested name (the §5.1 L247 call entry).

        ``tool_ref`` carries the name the model requested (the wire name,
        e.g. ``"fs__read"``); it is resolved against the session's pinned
        catalog here so unknown tools still produce the full
        requested→finished(not_started) sequence. The whole pipeline runs
        under the Phase-1 serial lock in call-start order.
        """
        if self._sink is None:
            raise ToolInvokeError("invoke requires an event sink (construct ToolRegistry with sink=)")
        if not isinstance(tool_ref, str) or not tool_ref:
            raise ToolInvokeError("tool_ref must be a non-empty string (the raw requested name)")
        catalog = self._pinned.get(context.session_id)
        spec = catalog.resolve(tool_ref) if catalog is not None else None
        tool = (
            None
            if spec is None
            # Invariant: pinned specs are copied from this registry's own
            # append-only (namespace, name)-keyed registrations, so the entry
            # below is provably present -- no defensive unknown-tool path.
            else ResolvedTool(spec=spec, adapter=self._adapters[(spec.namespace, spec.name)])
        )
        async with self._lock:
            return await invoke_tool(
                tool=tool,
                context=context,
                requested_name=tool_ref,
                raw_input=input,
                execution_id=execution_id,
                sink=self._sink,
                clock=self._clock,
                ids=self._ids,
                policy=self._policy,
            )
