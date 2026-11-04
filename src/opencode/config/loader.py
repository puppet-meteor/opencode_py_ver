"""ConfigLoader (R3-S6; CORE-CONTRACTS §6 main anchor): layered discovery, merge, runtime resolution, recovery.

OP-R3-10 ruling (①, task T-C1-030 mandatory answer 1): the full §6 L283-285
priority chain is implemented — builtin defaults < user global
``~/.config/opencode-py/opencode.json`` < per-layer ``opencode.json`` from the
project root down to the working directory < CLI — not TS ``discovery.ts``
``Sources/discover`` multi-source trust merging (r3-slices difference table 7:
Python keeps a minimal ConfigLoader + ConfigSnapshot and project config never
extends trust). The project root is the CLI-pinned ``--project-root`` or the
nearest VCS root (a ``.git`` marker, file or directory — worktrees carry it as
a file); with no marker the working directory itself is the root and the
search never falls back to the disk root (L283).

Merge laws (L285): a scalar is overridden by the later layer, maps merge
recursively, lists are replaced — with two named append exceptions,
``instructions`` and ``permission.rules``, which append outer-to-inner; file
instruction sources dedup by resolved path. Unknown structural fields are a
config error (L285). ``endpoint``/``roots``/MCP/credentials are host-tier keys
(L285): legal only in the global (host) config, rejected in project layers —
a project config can never implicitly extend host trust (PHASE1-SLIM L93).

The runtime face (L287): :meth:`ConfigLoader.resolve_runtime` turns the merged
layers + CLI overrides into a :class:`ResolvedRuntime` — the frozen
:class:`~opencode.session.context.RuntimeSettings` injection face (including
``system_prompt`` assembled from instructions; context.py is untouched), the
declared roots tuple (the same source the adapters consume — no drift), and
the executable :class:`~opencode.contracts.events.ConfigSnapshotV2` the CLI
persists through ``service.create``. MCP refs/boot fingerprints are omitted,
not defaulted (R4 face, ④ disclosure; §6.1 L291 no-fabrication spirit).

Recovery (L302, PHASE1-SLIM L93): :meth:`ConfigLoader.recover` rebuilds the
runtime face from the recorded ``py.session.created`` data — the v2 snapshot
is the behavioral baseline and instructions replay from their recorded text,
never from a re-read of the current config. A v1 row stops with
:class:`RecoveryStoppedError` (the minimal snapshot has no runtime face,
§6.1 L291 — no silent backfill); a vanished working directory stops the same
way (L302). Credentials-reference and MCP-fingerprint stop conditions have no
producer this slice (protocol-unreachable, ④ disclosure). Phase 1 has no hot
reload (L302): every process creates a fresh session; recovery is a library
capability exercised by tests.

Surrogate pre-scan (scheduler ledger row #19, T-C1-002-review ②): every
string that enters the snapshot is scanned before snapshot construction — a
lone surrogate raises a named :class:`ConfigError` instead of a raw
``UnicodeEncodeError`` from the canonical-hash path. The R1 value domain
cannot trigger it (fail-closed there was already pinned).
"""

from __future__ import annotations

import json
import math
import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from opencode.contracts.events import (
    ConfigSnapshotV2,
    InstructionSource,
    SessionCreatedData,
    SnapshotLimits,
    canonical_json,
    upcast_session_created,
)
from opencode.contracts.llm import ModelRef
from opencode.contracts.messages import JsonObject
from opencode.session.context import RuntimeSettings
from opencode.session.runner import DEFAULT_MAX_STEPS

#: §6.1 L287: per-AGENTS.md (per-source) instruction cap.
MAX_INSTRUCTION_FILE_BYTES = 64 * 1024
#: §6.1 L287: total instruction budget across all sources.
MAX_TOTAL_INSTRUCTION_BYTES = 256 * 1024

#: Host-tier keys (§6 L285): legal only in the global (host) config. A project
#: layer carrying any of them is a config error — project config cannot extend
#: host trust (PHASE1-SLIM L93 acceptance C).
HOST_ONLY_KEYS: frozenset[str] = frozenset({"roots", "endpoint", "mcp", "api_key", "credentials"})

#: Every structural key a config file may carry (unknown keys are a config
#: error, §6 L285). Host-only keys are listed separately for the tier check.
_RUN_KEYS: frozenset[str] = frozenset(
    {
        "model",
        "context_limit",
        "output_reserve",
        "timeout_s",
        "safety_margin",
        "temperature",
        "max_steps",
        "instructions",
        "permission",
    }
)
_KNOWN_KEYS: frozenset[str] = _RUN_KEYS | HOST_ONLY_KEYS

#: The two L285 append exceptions: these list paths concatenate outer-to-inner
#: instead of being replaced by the later layer.
_APPEND_LIST_PATHS: frozenset[tuple[str, ...]] = frozenset({("instructions",), ("permission", "rules")})

#: Known sub-keys of the ``permission`` map (anything else is a config error).
_PERMISSION_KEYS: frozenset[str] = frozenset({"rules"})

#: Known fields of one ``permission.rules`` entry (R3-S7, 必答②: load-time
#: fail-closed shape validation on the exit-2 face). ``source`` is deliberately
#: not a config field: the tier of a config-file rule is host authority (§5.2
#: L266), so an entry trying to declare its own source fails closed here.
_RULE_KEYS: frozenset[str] = frozenset({"action", "effect", "resources", "reason"})

#: §6 L283 builtin layer: the R2 CLI defaults move here so an absent CLI flag
#: means "fall through to config" instead of pinning the default at the top
#: layer. ``model``/``context_limit`` are deliberately absent — an unknown
#: model or context window is a host configuration error (§7 L339), never a
#: value this module guesses.
_BUILTIN_VALUES: JsonObject = {
    "output_reserve": 4096,
    "timeout_s": 30.0,
    "safety_margin": 0,
    "max_steps": DEFAULT_MAX_STEPS,
}


class ConfigError(Exception):
    """The layered config face is broken (§6): discovery, parse, merge, or a semantic violation."""


class InstructionLimitError(ConfigError):
    """An instruction source exceeds the §6.1 L287 caps (64 KiB per source / 256 KiB total)."""


class RecoveryStoppedError(ConfigError):
    """Recovery refused (§6.1 L302): the recorded baseline has no runnable face or its directory is gone."""


# ---------------------------------------------------------------------------
# §6 L285 merge semantics.
# ---------------------------------------------------------------------------


def _merge_json(base: JsonObject, override: JsonObject, path: tuple[str, ...] = ()) -> JsonObject:
    """Layer merge (§6 L285): scalars override, maps recurse, lists replace.

    The two named append exceptions (``instructions``, ``permission.rules``)
    concatenate outer-to-inner instead of replacing. A type mismatch (scalar
    vs map, map vs list, ...) resolves as "override wins" — the later layer
    replaces the earlier value whole; the merged result is always a fresh
    object graph (deep-copied override values), so callers cannot mutate a
    layer's parsed file through the merge result.
    """
    result = dict(base)
    for key, value in override.items():
        if key in result:
            current = result[key]
            if isinstance(current, dict) and isinstance(value, dict):
                result[key] = _merge_json(current, value, (*path, key))
                continue
            if isinstance(current, list) and isinstance(value, list) and (*path, key) in _APPEND_LIST_PATHS:
                result[key] = [*current, *value]
                continue
        result[key] = deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# Layer shapes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigLayer:
    """One discovered layer: its parsed values plus its instruction provenance.

    ``path`` is the config file (None when the layer contributes no config
    file — a directory whose ``AGENTS.md`` still counts); ``directory`` is the
    directory whose ``AGENTS.md`` joins the instruction chain (the global
    config directory for the global tier, the layer directory itself for
    project tiers; None for builtin/CLI).
    """

    origin: str
    values: JsonObject
    path: Path | None = None
    directory: Path | None = None


@dataclass(frozen=True)
class LayeredConfig:
    """The §6 L283 chain after discovery + merge (outermost layer first).

    ``values`` is builtin < global < project(root→cwd) merged; CLI overrides
    are applied later, at :meth:`ConfigLoader.resolve_runtime`.
    """

    layers: tuple[ConfigLayer, ...]
    values: JsonObject


@dataclass(frozen=True)
class CliOverrides:
    """The CLI layer (§6 L283 top priority): only explicitly passed flags land here."""

    model: str | None = None
    context_limit: int | None = None
    output_reserve: int | None = None
    timeout_s: float | None = None
    max_steps: int | None = None


@dataclass(frozen=True)
class ResolvedRuntime:
    """The runtime face one host process runs on (§6 L287).

    ``settings`` is the frozen RuntimeSettings injection face (system_prompt
    assembled from instructions); ``roots`` is the declared-roots tuple the
    fs adapters receive (the same source as ``snapshot.roots`` — one loader,
    no drift); ``snapshot`` is the executable v2 snapshot for
    ``service.create``. ``max_steps`` rides ``snapshot.limits``.
    """

    settings: RuntimeSettings
    roots: tuple[str, ...]
    snapshot: ConfigSnapshotV2


# ---------------------------------------------------------------------------
# Helpers: VCS root, endpoint sanitization, setting extraction.
# ---------------------------------------------------------------------------


def _vcs_root(start: Path) -> Path | None:
    """Nearest enclosing VCS root: a ``.git`` marker (file or directory), L283.

    Worktrees carry ``.git`` as a *file* pointing at the real gitdir, so the
    marker test is existence, not isdir. The walk covers the whole parent
    chain; with no marker anywhere the caller falls back to the working
    directory itself (never the disk root, L283).
    """
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _sanitize_endpoint(endpoint_url: str) -> str:
    """§6 L287 ``endpoint_ref``: the base URL with userinfo/query/fragment stripped.

    The snapshot stores a non-secret endpoint reference — credentials ride the
    CLI/env channel, never the snapshot (L302: credentials stored as
    references only). A non-http(s) or non-absolute URL is a config error.
    """
    parts = urlsplit(endpoint_url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.hostname is None:
        raise ConfigError(f"endpoint_url must be an absolute http(s) URL, got {endpoint_url!r}")
    host = parts.hostname
    netloc = f"[{host}]" if ":" in host else host  # bare IPv6 hosts need their brackets back
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "", "", ""))


def _int_setting(values: JsonObject, key: str, *, minimum: int, default: int | None = None) -> int:
    """Extract an int setting with the §1 bool rule; a missing no-default key is an error."""
    raw: Any = values[key] if key in values and values[key] is not None else default
    if raw is None:
        raise ConfigError(f"{key} is required: set it in a config layer or pass the CLI flag")
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"{key} must be an integer, got {raw!r}")
    if raw < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {raw}")
    return raw


def _seconds_setting(values: JsonObject, key: str, *, default: float) -> float:
    """Extract a finite positive seconds setting; bools are rejected."""
    raw: Any = values[key] if key in values and values[key] is not None else default
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(float(raw)) or raw <= 0:
        raise ConfigError(f"{key} must be a finite positive number of seconds, got {raw!r}")
    return float(raw)


def _temperature_setting(values: JsonObject) -> float | None:
    if "temperature" not in values or values["temperature"] is None:
        return None
    raw = values["temperature"]
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(float(raw)):
        raise ConfigError(f"temperature must be a finite number or null, got {raw!r}")
    return float(raw)


def _parse_config_file(path: Path) -> JsonObject:
    """One config file -> a JSON object; every failure is a named ConfigError."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"cannot read config file {path}: {error}") from error
    except UnicodeDecodeError as error:
        raise ConfigError(f"config file {path} is not valid UTF-8: {error}") from error
    try:
        parsed = json.loads(text)
    except ValueError as error:
        raise ConfigError(f"config file {path} is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ConfigError(f"config file {path} must contain a JSON object, got {type(parsed).__name__}")
    return parsed


def _validate_permission_rule(path: Path, index: int, rule: object) -> None:
    """One ``permission.rules`` entry: known fields + action/effect/resources/reason shapes.

    R3-S7 (必答② ruling): rule entries are config *content* discovered in a
    layer, so a malformed one fails at load time with the file path in the
    message -- the same exit-2 face as every other unknown config field (§6
    L285) -- instead of surfacing later as an exit-70 assembly crash. The
    policy-side snapshot conversion remains the fail-closed backstop for
    snapshots that never passed through a layer load (recovery face).
    """
    if not isinstance(rule, dict):
        raise ConfigError(f"'permission.rules[{index}]' in {path} must be a JSON object")
    for key in rule:
        if key not in _RULE_KEYS:
            raise ConfigError(
                f"unknown config field 'permission.rules[{index}].{key}' in {path} (CORE-CONTRACTS §6 L285)"
            )
    action = rule.get("action")
    if not isinstance(action, str) or not action:
        raise ConfigError(f"'permission.rules[{index}].action' in {path} must be a non-empty string")
    effect = rule.get("effect")
    if effect not in ("allow", "ask", "deny"):
        raise ConfigError(f"'permission.rules[{index}].effect' in {path} must be one of allow/ask/deny")
    resources = rule.get("resources")
    if resources is not None:
        if not isinstance(resources, list) or not all(isinstance(item, str) and item for item in resources):
            raise ConfigError(
                f"'permission.rules[{index}].resources' in {path} must be a list of non-empty path strings"
            )
    reason = rule.get("reason", "")
    if not isinstance(reason, str):
        raise ConfigError(f"'permission.rules[{index}].reason' in {path} must be a string")


def _validate_layer_keys(path: Path, values: JsonObject, *, host_tier: bool) -> None:
    """Unknown keys are a config error; host-only keys are rejected in project layers (L285)."""
    for key in values:
        if key not in _KNOWN_KEYS:
            raise ConfigError(f"unknown config field {key!r} in {path} (CORE-CONTRACTS §6 L285)")
        if not host_tier and key in HOST_ONLY_KEYS:
            raise ConfigError(
                f"config field {key!r} in {path} is host-tier configuration; a project layer cannot "
                "extend host trust (CORE-CONTRACTS §6 L285)"
            )
    permission = values.get("permission")
    if permission is not None:
        if not isinstance(permission, dict):
            raise ConfigError(f"'permission' in {path} must be an object")
        for key in permission:
            if key not in _PERMISSION_KEYS:
                raise ConfigError(f"unknown config field 'permission.{key}' in {path} (CORE-CONTRACTS §6 L285)")
        rules = permission.get("rules")
        if rules is not None:
            if not isinstance(rules, list):
                raise ConfigError(f"'permission.rules' in {path} must be a list of rule objects")
            for index, rule in enumerate(rules):
                _validate_permission_rule(path, index, rule)


# ---------------------------------------------------------------------------
# The loader.
# ---------------------------------------------------------------------------


class ConfigLoader:
    """§6 ConfigLoader: discovery + merge (``load``), runtime resolution
    (``resolve_runtime``), and the recovery face (``recover``).

    All three faces share one discovery run configuration (working directory,
    pinned project root, global config path), so the snapshot roots and the
    adapter roots can never drift apart (one loader source, test-pinned).
    """

    def __init__(
        self,
        *,
        working_dir: str | Path | None = None,
        project_root: str | Path | None = None,
        global_config: str | Path | None = None,
    ) -> None:
        self._working_dir = Path(working_dir) if working_dir is not None else Path.cwd()
        self._working_dir = self._working_dir.resolve()
        if not self._working_dir.is_dir():
            raise ConfigError(f"working_dir {self._working_dir} does not exist")
        if project_root is not None:
            self._project_root = Path(project_root).resolve()
            if not self._project_root.is_dir():
                raise ConfigError(f"project_root {self._project_root} does not exist")
            if self._project_root != self._working_dir and self._project_root not in self._working_dir.parents:
                raise ConfigError(
                    f"project_root {self._project_root} must be the working directory or an ancestor of it"
                )
        else:
            self._project_root = _vcs_root(self._working_dir) or self._working_dir
        if global_config is not None:
            self._global_path = Path(global_config).resolve()
            if not self._global_path.is_file():
                raise ConfigError(f"--config {self._global_path} does not exist (explicit path override)")
            self._global_dir = self._global_path.parent
        else:
            self._global_dir = Path.home() / ".config" / "opencode-py"
            self._global_path = self._global_dir / "opencode.json"

    # -- §6 L283-285: discovery + merge --------------------------------------

    def _layer_dirs(self) -> list[Path]:
        """Project layer directories, outermost (project root) first."""
        dirs = [self._working_dir]
        current = self._working_dir
        while current != self._project_root:
            parent = current.parent
            if parent == current:  # structural stop; the constructor already validated ancestry
                raise ConfigError(f"project_root {self._project_root} is not an ancestor of {self._working_dir}")
            dirs.append(parent)
            current = parent
        dirs.reverse()
        return dirs

    def load(self) -> LayeredConfig:
        """Discover + validate + merge the §6 L283 chain (CLI not included).

        The builtin layer is always first; the global tier is always present
        (its directory may contribute an ``AGENTS.md`` even without a config
        file); every project directory from the root down to the working
        directory contributes its ``opencode.json`` when present and its
        ``AGENTS.md`` when present.
        """
        layers: list[ConfigLayer] = [ConfigLayer(origin="builtin", values=dict(_BUILTIN_VALUES))]

        global_values: JsonObject = {}
        if self._global_path.is_file():
            global_values = _parse_config_file(self._global_path)
            _validate_layer_keys(self._global_path, global_values, host_tier=True)
        layers.append(
            ConfigLayer(
                origin=f"global:{self._global_path}",
                values=global_values,
                path=self._global_path if global_values else None,
                directory=self._global_dir,
            )
        )

        for directory in self._layer_dirs():
            config_path = directory / "opencode.json"
            values: JsonObject = {}
            if config_path.is_file():
                values = _parse_config_file(config_path)
                _validate_layer_keys(config_path, values, host_tier=False)
            layers.append(
                ConfigLayer(
                    origin=f"project:{directory}",
                    values=values,
                    path=config_path if values else None,
                    directory=directory,
                )
            )

        merged: JsonObject = dict(_BUILTIN_VALUES)
        for layer in layers[1:]:
            merged = _merge_json(merged, layer.values)
        return LayeredConfig(layers=tuple(layers), values=merged)

    # -- §6 L287: runtime resolution -----------------------------------------

    def _instruction_sources(self, layers: tuple[ConfigLayer, ...]) -> list[InstructionSource]:
        """Assemble instruction sources outer-to-inner with dedup + the §6.1 L287 caps.

        Per tier: the tier directory's ``AGENTS.md`` first, then the tier's
        config-declared instruction strings (path provenance = the declaring
        config file). AGENTS.md files dedup by resolved path; config strings
        are enumerated per declaration. Every source is byte-capped (64 KiB)
        and the running total capped at 256 KiB — exact-boundary overruns
        raise :class:`InstructionLimitError`. The surrogate pre-scan runs here
        (ledger row #19): an unpaired surrogate raises a named ConfigError
        before any snapshot construction.
        """
        sources: list[InstructionSource] = []
        seen_files: set[str] = set()
        total = 0

        def _add(path: str, text: str) -> None:
            nonlocal total
            try:
                size = len(text.encode("utf-8"))
            except UnicodeEncodeError as error:
                raise ConfigError(
                    f"instruction source {path!r} contains unpaired surrogate code points; the config "
                    "snapshot must be UTF-8 encodable (scheduler ledger row #19 pre-scan)"
                ) from error
            if size > MAX_INSTRUCTION_FILE_BYTES:
                raise InstructionLimitError(
                    f"instruction source {path!r} is {size} bytes, over the "
                    f"{MAX_INSTRUCTION_FILE_BYTES} byte per-source cap (CORE-CONTRACTS §6.1 L287)"
                )
            if total + size > MAX_TOTAL_INSTRUCTION_BYTES:
                raise InstructionLimitError(
                    f"instructions total {total + size} bytes with {path!r}, over the "
                    f"{MAX_TOTAL_INSTRUCTION_BYTES} byte cap (CORE-CONTRACTS §6.1 L287)"
                )
            total += size
            sources.append(InstructionSource(path=path, text=text))

        for layer in layers:
            if layer.directory is not None:
                agents = layer.directory / "AGENTS.md"
                if agents.is_file():
                    resolved = str(agents.resolve())
                    if resolved not in seen_files:
                        seen_files.add(resolved)
                        try:
                            _add(resolved, agents.read_text(encoding="utf-8"))
                        except ConfigError:
                            raise
                        except (OSError, UnicodeDecodeError) as error:
                            raise ConfigError(f"cannot read instruction file {agents}: {error}") from error
            if layer.path is not None:
                # L285 path dedup: a config file discovered by two tiers (the global
                # config directory doubling as the project root) contributes its
                # declared strings exactly once.
                resolved_config = str(Path(layer.path).resolve())
                if resolved_config in seen_files:
                    continue
                seen_files.add(resolved_config)
                declared = layer.values.get("instructions")
                if declared is None:
                    continue
                if not isinstance(declared, list):
                    raise ConfigError(f"'instructions' in {layer.path} must be a list of strings")
                for text in declared:
                    if not isinstance(text, str):
                        raise ConfigError(f"'instructions' in {layer.path} must be a list of strings")
                    _add(str(layer.path), text)
        return sources

    def _declared_roots(self, values: JsonObject) -> tuple[str, ...]:
        """Validate + normalize the host-tier ``roots`` (global config only, L285/L287)."""
        raw = values.get("roots")
        if raw is None:
            return ()
        if not isinstance(raw, list):
            raise ConfigError("'roots' must be a list of absolute path strings")
        roots: list[str] = []
        for item in raw:
            if not isinstance(item, str) or not item:
                raise ConfigError("'roots' entries must be non-empty path strings")
            if not Path(item).is_absolute():
                raise ConfigError(f"roots entry {item!r} must be an absolute path")
            normalized = os.path.normpath(item)
            if normalized not in roots:
                roots.append(normalized)
        return tuple(roots)

    def _permission_rules(self, values: JsonObject) -> list[JsonObject]:
        """Extract ``permission.rules`` (L285/L287): an ordered list of JSON objects.

        The snapshot carries the rules for the record; wiring them into the
        live :class:`~opencode.tools.policy.PermissionPolicy` (whose §5.2
        engine already ranks host over project sources) is the injection seam
        disclosed in ④ — this slice records, it does not enforce.
        """
        permission = values.get("permission")
        if permission is None:
            return []
        rules = permission.get("rules", []) if isinstance(permission, dict) else None
        if not isinstance(rules, list):
            raise ConfigError("'permission.rules' must be a list of rule objects")
        checked: list[JsonObject] = []
        for rule in rules:
            if not isinstance(rule, dict):
                raise ConfigError("'permission.rules' entries must be JSON objects")
            checked.append(rule)
        return checked

    def resolve_runtime(
        self,
        *,
        cli: CliOverrides | None = None,
        endpoint_url: str | None = None,
    ) -> ResolvedRuntime:
        """Layered values + CLI overrides -> the §6 L287 runtime face.

        CLI is the top layer (L283): every explicitly passed flag overrides
        the merged config. ``model``/``context_limit`` must come from CLI or
        config (no builtin guess, §7 L339); the remaining limits fall back to
        the builtin layer. Fails with :class:`ConfigError` on any violation —
        never a silent default.
        """
        if endpoint_url is None:
            raise ConfigError("endpoint_url is required to resolve the runtime face (§6.1 L287 endpoint_ref)")
        layered = self.load()
        values: JsonObject = dict(layered.values)
        if cli is not None:
            for key, override in (
                ("model", cli.model),
                ("context_limit", cli.context_limit),
                ("output_reserve", cli.output_reserve),
                ("timeout_s", cli.timeout_s),
                ("max_steps", cli.max_steps),
            ):
                if override is not None:
                    values[key] = override

        raw_model = values.get("model")
        if not isinstance(raw_model, str) or not raw_model:
            raise ConfigError("model is required: set it in a config layer or pass --model")
        limits = SnapshotLimits(
            context_limit=_int_setting(values, "context_limit", minimum=1),
            output_reserve=_int_setting(values, "output_reserve", minimum=1, default=4096),
            timeout_s=_seconds_setting(values, "timeout_s", default=30.0),
            safety_margin=_int_setting(values, "safety_margin", minimum=0, default=0),
            temperature=_temperature_setting(values),
            max_steps=_int_setting(values, "max_steps", minimum=1, default=DEFAULT_MAX_STEPS),
        )
        roots = self._declared_roots(values)
        rules = self._permission_rules(values)
        instructions = self._instruction_sources(layered.layers)
        system_prompt = "\n\n".join(source.text for source in instructions) if instructions else None

        snapshot = ConfigSnapshotV2(
            schema_version=2,
            model=ModelRef(provider="maas", model=raw_model),
            endpoint_ref=_sanitize_endpoint(endpoint_url),
            roots=list(roots),
            permission_rules=rules,
            instructions=instructions,
            limits=limits,
        )
        # Surrogate pre-scan over the whole snapshot domain (row #19): the
        # canonical hash text must be UTF-8 encodable, and the failure must
        # be a named ConfigError, not a raw UnicodeEncodeError.
        try:
            canonical_json(snapshot.model_dump(mode="json")).encode("utf-8")
        except UnicodeEncodeError as error:
            raise ConfigError(
                "the resolved config snapshot contains unpaired surrogate code points and cannot be "
                "canonically serialized (scheduler ledger row #19 pre-scan)"
            ) from error
        try:
            settings = RuntimeSettings(
                model=snapshot.model,
                context_limit=limits.context_limit,
                output_reserve=limits.output_reserve,
                timeout_s=limits.timeout_s,
                safety_margin=limits.safety_margin,
                temperature=limits.temperature,
                system_prompt=system_prompt,
            )
        except ValueError as error:
            raise ConfigError(f"the resolved config is not a legal runtime face: {error}") from error
        return ResolvedRuntime(settings=settings, roots=roots, snapshot=snapshot)

    # -- §6.1 L302: recovery ---------------------------------------------------

    def recover(self, data: SessionCreatedData) -> ResolvedRuntime:
        """Rebuild the runtime face from one recorded ``py.session.created`` row.

        The v2 snapshot is the behavioral baseline (L302): settings, roots,
        and instructions replay from the recorded snapshot, never from a
        re-read of the current config (§6.1 L291 — no silent backfill). A v1
        row stops (:class:`RecoveryStoppedError` — the minimal snapshot has
        no runtime face); a vanished working directory stops the same way.
        Credentials-reference and MCP-fingerprint stop conditions (L302) have
        no snapshot fields this slice and are protocol-unreachable (④).
        """
        runtime = upcast_session_created(data)
        if runtime.snapshot is None:
            raise RecoveryStoppedError(
                "session was created with the v1 minimal config snapshot, which has no runtime face; "
                "recovery refuses to backfill it from the current config (CORE-CONTRACTS §6.1 L291/L302)"
            )
        if not os.path.isdir(runtime.working_dir):
            raise RecoveryStoppedError(
                f"the recorded working directory {runtime.working_dir!r} no longer exists; the host must "
                "reconfigure before recovery can proceed (CORE-CONTRACTS §6.1 L302)"
            )
        snapshot = runtime.snapshot
        texts = [source.text for source in snapshot.instructions]
        try:
            settings = RuntimeSettings(
                model=snapshot.model,
                context_limit=snapshot.limits.context_limit,
                output_reserve=snapshot.limits.output_reserve,
                timeout_s=snapshot.limits.timeout_s,
                safety_margin=snapshot.limits.safety_margin,
                temperature=snapshot.limits.temperature,
                system_prompt="\n\n".join(texts) if texts else None,
            )
        except ValueError as error:
            raise ConfigError(f"the recorded v2 snapshot is not a legal runtime face: {error}") from error
        return ResolvedRuntime(settings=settings, roots=tuple(snapshot.roots), snapshot=snapshot)


#: Instruction cap constants are re-exported for tests and hosts (§6.1 L287).
__all__ = [
    "HOST_ONLY_KEYS",
    "MAX_INSTRUCTION_FILE_BYTES",
    "MAX_TOTAL_INSTRUCTION_BYTES",
    "CliOverrides",
    "ConfigError",
    "ConfigLayer",
    "ConfigLoader",
    "InstructionLimitError",
    "LayeredConfig",
    "RecoveryStoppedError",
    "ResolvedRuntime",
]
