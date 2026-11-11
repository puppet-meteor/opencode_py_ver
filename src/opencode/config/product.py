"""Product config bridge (T-C1-067): the TS opencode gateway config as a ConfigLoader layer.

The machine's TS opencode installation keeps its gateway provider config at
``$XDG_CONFIG_HOME/opencode`` (when set) else ``~/.config/opencode`` —
``opencode.json`` and/or ``opencode.jsonc`` with a ``provider.<name>`` block.
This module turns that foreign-language config into the ConfigLoader product
tier (§6 L283 discovery order: builtin < product < global < project(root→cwd)
< CLI), so the CLI runs without explicit ``--base-url``/``--api-key``/
``--model`` once the gateway is configured for TS opencode. T-C1-048 already
proved the same read logic for the opt-in real-MAAS tests
(``tests/integration/_maas_real_config.py``); this is the PRODUCT face — a
deliberate independent copy, not a refactor of that helper (the opt-in test
bridge and the product bridge stay separate: review-face separation).

Contract face (§6 L283/L285): the product tier is a discovery-order extension
only — the key set gains nothing. ``endpoint``/``api_key`` are existing
host-only keys, ``model`` an existing run key, and the bridge produces exactly
those three; every other foreign config key (theme, keybinds, autoupdate,
``$schema``, ``mcp``, ``disabled_providers``, ...) is read and ignored —
never an error, never a layer value. The product directory is NOT an
instruction source: its ``AGENTS.md`` never joins the instruction chain (the
loader sets ``directory=None`` — AGENTS.md semantics belong to the -py layers
only). ``auth.json`` is NEVER read (the TS auth block is out of scope;
credentials ride the provider block only).

Read face: ``opencode.json`` and ``opencode.jsonc`` are both read; when both
exist they merge shallowly with jsonc keys overriding json. A jsonc file gets
``//`` line comments and trailing commas stripped before parsing —
string-aware, so ``//`` inside a URL value and a comma inside a string value
survive. The scanner is the T-C1-048 ``_strip_jsonc`` port: a string/escape
state machine plus a pending-comma lookahead (a comma is only emitted once
the next significant character proves it is not trailing).

Error face (fail-closed, named missing piece, ZERO value echo): a present
file that is not valid JSON, a missing/non-object provider block, a block
without ``baseURL``/``apiKey``, or an empty ``models`` map is a
:class:`~opencode.config.loader.ConfigError` naming what is missing — never a
value that was read. Both files absent: ``None`` — the layer is silently not
built (the machine has no TS product config to bridge).

Purity: no global state beyond ``os.environ`` (``XDG_CONFIG_HOME`` for the
directory, ``OPENCODE_PY_PROVIDER`` for the block name), no printing, no
filesystem writes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from opencode.config.loader import ConfigError

#: The TS product config file names, in dual-read order (jsonc overrides json).
JSON_FILE = "opencode.json"
JSONC_FILE = "opencode.jsonc"

#: The provider block name env; the default matches the local gateway install.
_PROVIDER_ENV = "OPENCODE_PY_PROVIDER"
_DEFAULT_PROVIDER = "gateway"


def product_config_dir(explicit: Path | None = None) -> Path:
    """The TS opencode product config directory; ``explicit`` is the test injection point.

    ``$XDG_CONFIG_HOME/opencode`` when the env is set, else
    ``~/.config/opencode`` — pure path math, no filesystem reads.
    """
    if explicit is not None:
        return Path(explicit)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) / "opencode" if xdg else Path.home() / ".config" / "opencode"


def _strip_jsonc(text: str) -> str:
    """Strip ``//`` line comments and trailing commas, both string-aware.

    Port of the T-C1-048 scanner (kept as an independent copy by design: the
    opt-in test bridge and the product bridge stay separate). A naive split on
    ``//`` would corrupt URLs inside string values, and a regex comma strip
    could eat string content, so both edits ride one scanner that tracks
    string/escape state; a comma is held back and only emitted when the next
    significant character proves it is not trailing.
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


def _read_product_file(path: Path) -> dict[str, Any]:
    """One product config file -> a JSON object; jsonc gets comment/trailing-comma stripping."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"product config {path.name} is unreadable") from error
    try:
        data = json.loads(_strip_jsonc(text) if path.suffix == ".jsonc" else text)
    except ValueError as error:
        raise ConfigError(f"product config {path.name} is not valid JSON") from error
    if not isinstance(data, dict):
        raise ConfigError(f"product config {path.name} is not a JSON object")
    return data


def _block_field(block: dict[str, Any], key: str) -> str | None:
    """One provider-block string field, flat or nested under ``options`` (the local shape)."""
    options = block.get("options")
    sources: tuple[dict[str, Any], ...] = (block, options if isinstance(options, dict) else {})
    for source in sources:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def resolve_product_values(config_dir: Path) -> dict[str, object] | None:
    """The product tier values from a TS opencode config directory, or None.

    Reads ``opencode.json`` then ``opencode.jsonc`` (shallow merge, jsonc keys
    override json). Both absent -> ``None`` (no layer). Otherwise the
    ``OPENCODE_PY_PROVIDER`` block (default ``gateway``) must exist and carry
    ``baseURL``/``apiKey`` (flat or under ``options``) and a non-empty
    ``models`` map — every failure is a named :class:`ConfigError` with zero
    value echo. The result is exactly ``{"endpoint", "api_key", "model"}``;
    every other foreign key is ignored, and ``auth.json`` is never read.
    """
    config_dir = Path(config_dir)
    merged: dict[str, Any] = {}
    found_any = False
    for name in (JSON_FILE, JSONC_FILE):
        path = config_dir / name
        if not path.is_file():
            continue
        found_any = True
        merged.update(_read_product_file(path))  # jsonc keys shallowly override json
    if not found_any:
        return None
    provider_name = os.environ.get(_PROVIDER_ENV) or _DEFAULT_PROVIDER
    providers = merged.get("provider")
    if not isinstance(providers, dict) or provider_name not in providers:
        raise ConfigError(f"provider block {provider_name!r} not found in the product config")
    block = providers[provider_name]
    if not isinstance(block, dict):
        raise ConfigError(f"provider block {provider_name!r} in the product config is not a JSON object")
    base_url = _block_field(block, "baseURL")
    if base_url is None:
        raise ConfigError(f"provider block {provider_name!r} in the product config has no baseURL")
    api_key = _block_field(block, "apiKey")
    if api_key is None:
        raise ConfigError(f"provider block {provider_name!r} in the product config has no apiKey")
    models = block.get("models")
    if not isinstance(models, dict) or not models:
        raise ConfigError(f"provider block {provider_name!r} in the product config has no non-empty models map")
    model = next(iter(models))
    if not model:
        raise ConfigError(f"provider block {provider_name!r} in the product config has an empty models key")
    return {"endpoint": base_url, "api_key": api_key, "model": model}
