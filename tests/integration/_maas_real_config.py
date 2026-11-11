"""Config bridge for the opt-in real-MAAS integration tests (T-C1-048).

Resolution order (``auth.json`` is never read):

1. **env mode** -- when ANY of ``OPENCODE_PY_MAAS_BASE_URL`` /
   ``OPENCODE_PY_MAAS_API_KEY`` / ``OPENCODE_PY_MAAS_MODEL`` is set, all
   three must be set; a partial triple is unavailable and never mixes with
   the file sources;
2. **file mode** -- the config directory is ``$XDG_CONFIG_HOME/opencode``
   (when set) else ``~/.config/opencode``; ``opencode.json`` and
   ``opencode.jsonc`` are both read (jsonc ``//`` comments and trailing
   commas stripped before parsing) and merged with jsonc keys shallowly
   overriding json; the provider block is ``OPENCODE_PY_MAAS_PROVIDER``
   (default ``gateway``); ``baseURL``/``apiKey`` are read from the block,
   flat or nested under its ``options`` object (the local gateway file uses
   the nested shape); the model is ``OPENCODE_PY_MAAS_MODEL`` (if set) else
   the first key of the block's ``models`` map.

Redaction discipline (R0 provider.md §8): resolved values live only in
memory and reach the in-process ``main()`` argv -- never printed, logged, or
persisted; :class:`MaasRealConfigUnavailable` messages name only what is
missing, never a value that was read.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_ENV_BASE_URL = "OPENCODE_PY_MAAS_BASE_URL"
_ENV_API_KEY = "OPENCODE_PY_MAAS_API_KEY"
_ENV_MODEL = "OPENCODE_PY_MAAS_MODEL"
_ENV_PROVIDER = "OPENCODE_PY_MAAS_PROVIDER"
_DEFAULT_PROVIDER = "gateway"

_JSON_FILE = "opencode.json"
_JSONC_FILE = "opencode.jsonc"


class MaasRealConfigUnavailable(Exception):
    """The real-MAAS bridge cannot be resolved; messages name the missing piece only."""


@dataclass(frozen=True)
class MaasRealConfig:
    """The resolved bridge; ``source`` is the only field safe to display."""

    base_url: str
    api_key: str
    model: str
    source: Literal["env", "config"]


def _strip_jsonc(text: str) -> str:
    """Strip ``//`` line comments and trailing commas, both string-aware.

    A naive split on ``//`` would corrupt URLs inside string values, and a
    regex comma strip could eat string content, so both edits ride one
    scanner that tracks string/escape state; a comma is held back and only
    emitted when the next significant character proves it is not trailing.
    """
    out: list[str] = []
    pending_comma = False
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if not in_string and char == "/" and text[index + 1 : index + 2] == "/":
            while index < length and text[index] != "\n":
                index += 1
            continue  # the newline itself is kept
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == ",":
            pending_comma = True
        elif char in " \t\r\n":
            out.append(char)
        else:
            if pending_comma:
                if char not in "}]":
                    out.append(",")
                pending_comma = False
            if char == '"':
                in_string = True
            out.append(char)
        index += 1
    return "".join(out)


def _read_config_file(path: Path) -> dict[str, Any]:
    """Parse one config file (jsonc gets comment/trailing-comma stripping)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise MaasRealConfigUnavailable(f"config file {path.name} is unreadable") from error
    try:
        data = json.loads(_strip_jsonc(text) if path.suffix == ".jsonc" else text)
    except ValueError as error:
        raise MaasRealConfigUnavailable(f"config file {path.name} is not valid JSON") from error
    if not isinstance(data, dict):
        raise MaasRealConfigUnavailable(f"config file {path.name} is not a JSON object")
    return data


def _config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) / "opencode" if xdg else Path.home() / ".config" / "opencode"


def _block_field(block: dict[str, Any], key: str) -> str | None:
    """One provider-block string field, flat or nested under ``options``."""
    options = block.get("options")
    sources: tuple[dict[str, Any], ...] = (block, options if isinstance(options, dict) else {})
    for source in sources:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _resolve_from_files() -> MaasRealConfig:
    """File mode: merge the local opencode config files and pick the block."""
    config_dir = _config_dir()
    merged: dict[str, Any] = {}
    found_any = False
    for name in (_JSON_FILE, _JSONC_FILE):
        path = config_dir / name
        if not path.is_file():
            continue
        found_any = True
        merged.update(_read_config_file(path))  # jsonc keys shallowly override json
    if not found_any:
        raise MaasRealConfigUnavailable(f"no {_JSON_FILE}/{_JSONC_FILE} under the opencode config directory")
    provider_name = os.environ.get(_ENV_PROVIDER) or _DEFAULT_PROVIDER
    providers = merged.get("provider")
    if not isinstance(providers, dict) or provider_name not in providers:
        raise MaasRealConfigUnavailable(f"provider block {provider_name!r} not found in the merged config")
    block = providers[provider_name]
    if not isinstance(block, dict):
        raise MaasRealConfigUnavailable(f"provider block {provider_name!r} is not a JSON object")
    base_url = _block_field(block, "baseURL")
    if base_url is None:
        raise MaasRealConfigUnavailable(f"provider block {provider_name!r} has no baseURL")
    api_key = _block_field(block, "apiKey")
    if api_key is None:
        raise MaasRealConfigUnavailable(f"provider block {provider_name!r} has no apiKey")
    model = os.environ.get(_ENV_MODEL)
    if not model:
        models = block.get("models")
        if not isinstance(models, dict) or not models:
            raise MaasRealConfigUnavailable(f"provider block {provider_name!r} has no non-empty models map")
        model = next(iter(models))
        if not model:
            raise MaasRealConfigUnavailable(f"provider block {provider_name!r} has an empty models key")
    return MaasRealConfig(base_url=base_url, api_key=api_key, model=model, source="config")


def resolve_maas_real_config() -> MaasRealConfig:
    """Resolve the real-MAAS bridge: the env triple first, then the config files."""
    base_url = os.environ.get(_ENV_BASE_URL, "")
    api_key = os.environ.get(_ENV_API_KEY, "")
    model = os.environ.get(_ENV_MODEL, "")
    if base_url or api_key or model:
        missing = [
            name
            for name, value in ((_ENV_BASE_URL, base_url), (_ENV_API_KEY, api_key), (_ENV_MODEL, model))
            if not value
        ]
        if missing:
            raise MaasRealConfigUnavailable(
                f"env mode entered but incomplete (missing: {', '.join(missing)}); file sources are never mixed in"
            )
        return MaasRealConfig(base_url=base_url, api_key=api_key, model=model, source="env")
    return _resolve_from_files()
