"""T-C1-049 integration tests: the product config bridge (the ConfigLoader fifth layer).

The bridge turns the machine's TS opencode gateway config (``$XDG_CONFIG_HOME/
opencode`` else ``~/.config/opencode``; ``opencode.json``/``opencode.jsonc``;
``provider.<name>`` block) into the product tier of the §6 L283 discovery
order (builtin < product < global < project(root→cwd) < CLI). Covered faces:

* the module face: dual-read shallow merge (jsonc keys override json), the
  string-aware jsonc scanner (``//`` inside URL strings and commas inside
  string values survive; line comments and trailing commas do not), flat and
  options-nested provider fields, the models-map first key, the
  ``OPENCODE_PY_PROVIDER`` block-name override, and the pure directory
  resolution (explicit > XDG > home);
* the fail-closed error matrix: each missing piece is a named ``ConfigError``
  naming what is missing — never a value that was read (zero value echo);
* the silent-absent face: both files missing -> ``None`` (no layer built), and
  ``auth.json`` is never a product source;
* the snapshot no-secret discipline: with the product layer supplying
  credentials, the v3 snapshot's canonical serialization carries no api_key
  value and ``endpoint_ref`` is the sanitized (userinfo/query-free) URL.

Hermeticity (T-C1-049 §4): every test injects a tmp product directory and the
ambient env is scrubbed — the real ``~/.config/opencode`` (live developer
credentials) is never read by anything here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencode.config import CliOverrides, ConfigError, ConfigLoader
from opencode.config.product import product_config_dir, resolve_product_values
from opencode.contracts.events import canonical_json


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub the ambient env faces the bridge consults (provider name, XDG dir)."""
    monkeypatch.delenv("OPENCODE_PY_PROVIDER", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


def _write(config_dir: Path, name: str, payload: object) -> Path:
    """Write one product config file (str payloads pass through verbatim — raw jsonc)."""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def _gateway(
    config_dir: Path,
    *,
    base_url: str = "https://product-gw.test/v1",
    api_key: str = "r0-product-key",
    model: str = "product-model",
    provider: str = "gateway",
    nested: bool = True,
    name: str = "opencode.json",
) -> Path:
    """One product config file with a gateway provider block (nested by default)."""
    fields: dict[str, object]
    if nested:
        fields = {"options": {"baseURL": base_url, "apiKey": api_key}}
    else:
        fields = {"baseURL": base_url, "apiKey": api_key}
    block: dict[str, object] = {**fields, "models": {model: {}}}
    return _write(config_dir, name, {"provider": {provider: block}})


# ---------------------------------------------------------------------------
# Directory resolution: explicit > XDG_CONFIG_HOME > home.
# ---------------------------------------------------------------------------


def test_product_config_dir_explicit_then_xdg_then_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert product_config_dir(explicit=tmp_path) == tmp_path  # the test injection point
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert product_config_dir() == tmp_path / "xdg" / "opencode"
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert product_config_dir() == Path.home() / ".config" / "opencode"  # pure path math, no reads


# ---------------------------------------------------------------------------
# The silent-absent face: no product files -> no layer.
# ---------------------------------------------------------------------------


def test_both_files_absent_returns_none(tmp_path: Path) -> None:
    (tmp_path / "opencode").mkdir()
    assert resolve_product_values(tmp_path / "opencode") is None


def test_a_missing_directory_also_returns_none(tmp_path: Path) -> None:
    assert resolve_product_values(tmp_path / "never-created") is None


def test_auth_json_is_never_a_product_source(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    config_dir.mkdir()
    _write(config_dir, "auth.json", {"api_key": "r0-auth-key"})
    assert resolve_product_values(config_dir) is None  # auth.json alone builds no layer


# ---------------------------------------------------------------------------
# Dual-read shallow merge + the string-aware jsonc scanner.
# ---------------------------------------------------------------------------


def test_dual_read_shallow_merge_jsonc_overrides_json(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    _write(
        config_dir,
        "opencode.json",
        {
            "theme": "dark",  # a foreign key: read, ignored, never a layer value
            "provider": {
                "gateway": {
                    "baseURL": "https://from-json.test/v1",
                    "apiKey": "r0-json-key",
                    "models": {"json-model": {}},
                }
            },
        },
    )
    _write(
        config_dir,
        "opencode.jsonc",
        {
            "provider": {
                "gateway": {
                    "baseURL": "https://from-jsonc.test/v1",
                    "apiKey": "r0-jsonc-key",
                    "models": {"jsonc-model": {}},
                }
            }
        },
    )
    values = resolve_product_values(config_dir)
    # The jsonc provider block replaces json's whole top-level key (shallow merge).
    assert values == {"endpoint": "https://from-jsonc.test/v1", "api_key": "r0-jsonc-key", "model": "jsonc-model"}


def test_jsonc_scanner_preserves_double_slash_inside_url_strings(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    _write(
        config_dir,
        "opencode.jsonc",
        "{\n"
        "  // gateway endpoint for the local TS install\n"
        '  "provider": {\n'
        '    "gateway": {\n'
        '      "baseURL": "https://gw.example.test//double//slashes",\n'
        '      "apiKey": "r0-slash-key",\n'
        '      "models": {"slash-model": {}},\n'
        "    },\n"
        "  },\n"
        "}\n",
    )
    values = resolve_product_values(config_dir)
    # A naive `//` comment splitter would truncate the URL at the first slash pair.
    assert values == {
        "endpoint": "https://gw.example.test//double//slashes",
        "api_key": "r0-slash-key",
        "model": "slash-model",
    }


def test_jsonc_scanner_preserves_commas_inside_strings(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    _write(
        config_dir,
        "opencode.jsonc",
        "{\n"
        '  "provider": {\n'
        '    "gateway": {\n'
        '      "baseURL": "https://gw.example.test/v1", // trailing comment\n'
        '      "apiKey": "r0-key,with,commas",\n'
        '      "models": {"m,c": {}, "plain": {},},\n'
        "    },\n"
        "  },\n"
        "}\n",
    )
    values = resolve_product_values(config_dir)
    # A naive trailing-comma stripper would eat the string content and the map key.
    assert values == {"endpoint": "https://gw.example.test/v1", "api_key": "r0-key,with,commas", "model": "m,c"}


# ---------------------------------------------------------------------------
# Block shapes: flat vs options-nested; the models first key; provider env.
# ---------------------------------------------------------------------------


def test_flat_and_nested_provider_fields_both_resolve(tmp_path: Path) -> None:
    flat = tmp_path / "flat"
    _gateway(flat, base_url="https://flat.test/v1", api_key="r0-flat-key", model="flat-model", nested=False)
    assert resolve_product_values(flat) == {
        "endpoint": "https://flat.test/v1",
        "api_key": "r0-flat-key",
        "model": "flat-model",
    }
    nested = tmp_path / "nested"  # the local gateway file's shape
    _gateway(nested, base_url="https://nested.test/v1", api_key="r0-nested-key", model="nested-model", nested=True)
    assert resolve_product_values(nested) == {
        "endpoint": "https://nested.test/v1",
        "api_key": "r0-nested-key",
        "model": "nested-model",
    }


def test_model_is_the_first_models_key_in_insertion_order(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    _gateway(config_dir, model="zeta-model")
    _write(
        config_dir,
        "opencode.json",
        {
            "provider": {
                "gateway": {
                    "options": {"baseURL": "https://product-gw.test/v1", "apiKey": "r0-product-key"},
                    "models": {"zeta-model": {}, "alpha-model": {}},
                }
            }
        },
    )
    assert resolve_product_values(config_dir) is not None
    values = resolve_product_values(config_dir)
    assert values is not None
    assert values["model"] == "zeta-model"  # insertion order, never sorted


def test_provider_env_selects_the_block_default_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_dir = tmp_path / "opencode"
    _write(
        config_dir,
        "opencode.json",
        {
            "provider": {
                "gateway": {
                    "options": {"baseURL": "https://gw.test/v1", "apiKey": "r0-key"},
                    "models": {"gateway-model": {}},
                },
                "other": {
                    "options": {"baseURL": "https://other.test/v1", "apiKey": "r0-key"},
                    "models": {"other-model": {}},
                },
            }
        },
    )
    default_values = resolve_product_values(config_dir)
    assert default_values is not None
    assert default_values["model"] == "gateway-model"  # the default block name
    monkeypatch.setenv("OPENCODE_PY_PROVIDER", "other")
    env_values = resolve_product_values(config_dir)
    assert env_values is not None
    assert env_values["model"] == "other-model"  # the env override


# ---------------------------------------------------------------------------
# Fail-closed error matrix: named missing piece, zero value echo.
# ---------------------------------------------------------------------------


def test_invalid_json_is_a_named_error_per_file(tmp_path: Path) -> None:
    json_dir = tmp_path / "json-dir"
    json_dir.mkdir()
    (json_dir / "opencode.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"product config opencode\.json is not valid JSON"):
        resolve_product_values(json_dir)
    jsonc_dir = tmp_path / "jsonc-dir"
    jsonc_dir.mkdir()
    (jsonc_dir / "opencode.jsonc").write_text('{"provider": }', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"product config opencode\.jsonc is not valid JSON"):
        resolve_product_values(jsonc_dir)


def test_missing_provider_block_is_a_named_error_then_resolves(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    _write(config_dir, "opencode.json", {"theme": "dark"})  # a foreign config with no provider block
    with pytest.raises(ConfigError, match="provider block 'gateway' not found in the product config"):
        resolve_product_values(config_dir)
    _write(
        config_dir, "opencode.json", {"provider": {"not-gateway": {"baseURL": "https://x.test/v1"}}}
    )  # wrong block name
    with pytest.raises(ConfigError, match="provider block 'gateway' not found in the product config"):
        resolve_product_values(config_dir)
    _gateway(config_dir)  # green leg: the same face resolves once the block exists
    assert resolve_product_values(config_dir) == {
        "endpoint": "https://product-gw.test/v1",
        "api_key": "r0-product-key",
        "model": "product-model",
    }


def test_non_object_provider_block_is_a_named_error(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    _write(config_dir, "opencode.json", {"provider": {"gateway": "not-an-object"}})
    with pytest.raises(ConfigError, match="provider block 'gateway' in the product config is not a JSON object"):
        resolve_product_values(config_dir)
    _write(config_dir, "opencode.json", {"provider": 42})
    with pytest.raises(ConfigError, match="provider block 'gateway' not found in the product config"):
        resolve_product_values(config_dir)


def test_missing_base_url_is_a_named_error_without_value_echo(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    _write(
        config_dir,
        "opencode.json",
        {"provider": {"gateway": {"options": {"apiKey": "r0-must-not-echo"}, "models": {"m": {}}}}},
    )
    with pytest.raises(ConfigError, match="provider block 'gateway' in the product config has no baseURL") as exc_info:
        resolve_product_values(config_dir)
    assert "r0-must-not-echo" not in str(exc_info.value)  # zero value echo
    _gateway(config_dir, base_url="https://gw.test/v1", api_key="r0-must-not-echo", model="m")  # green leg
    assert resolve_product_values(config_dir) == {
        "endpoint": "https://gw.test/v1",
        "api_key": "r0-must-not-echo",
        "model": "m",
    }


def test_missing_api_key_is_a_named_error_without_value_echo(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    _write(
        config_dir,
        "opencode.json",
        {"provider": {"gateway": {"options": {"baseURL": "https://leak-endpoint.test/v1"}, "models": {"m": {}}}}},
    )
    with pytest.raises(ConfigError, match="provider block 'gateway' in the product config has no apiKey") as exc_info:
        resolve_product_values(config_dir)
    assert "https://leak-endpoint.test/v1" not in str(exc_info.value)  # zero value echo
    _gateway(config_dir, base_url="https://leak-endpoint.test/v1", api_key="r0-key", model="m")  # green leg
    assert resolve_product_values(config_dir) == {
        "endpoint": "https://leak-endpoint.test/v1",
        "api_key": "r0-key",
        "model": "m",
    }


def test_empty_models_map_is_a_named_error(tmp_path: Path) -> None:
    config_dir = tmp_path / "opencode"
    _write(
        config_dir,
        "opencode.json",
        {"provider": {"gateway": {"options": {"baseURL": "https://gw.test/v1", "apiKey": "r0-key"}}}},  # no models key
    )
    with pytest.raises(ConfigError, match="provider block 'gateway' in the product config has no non-empty models map"):
        resolve_product_values(config_dir)
    _write(
        config_dir,
        "opencode.json",
        {
            "provider": {"gateway": {"options": {"baseURL": "https://gw.test/v1", "apiKey": "r0-key"}, "models": {}}}
        },  # empty map
    )
    with pytest.raises(ConfigError, match="provider block 'gateway' in the product config has no non-empty models map"):
        resolve_product_values(config_dir)
    _write(
        config_dir,
        "opencode.json",
        {
            "provider": {
                "gateway": {"options": {"baseURL": "https://gw.test/v1", "apiKey": "r0-key"}, "models": {"": {}}}
            }
        },
    )
    with pytest.raises(ConfigError, match="provider block 'gateway' in the product config has an empty models key"):
        resolve_product_values(config_dir)
    _gateway(config_dir, base_url="https://gw.test/v1", api_key="r0-key", model="m")  # green leg
    assert resolve_product_values(config_dir) == {"endpoint": "https://gw.test/v1", "api_key": "r0-key", "model": "m"}


# ---------------------------------------------------------------------------
# The snapshot no-secret discipline with product-supplied credentials.
# ---------------------------------------------------------------------------


def test_v3_snapshot_carries_no_api_key_and_a_sanitized_endpoint_ref(tmp_path: Path) -> None:
    """§6.1 L302/L287 discipline: the product layer supplies credentials, the v3
    snapshot's canonical serialization still carries no api_key value, and
    ``endpoint_ref`` is the sanitized URL (userinfo/query/fragment stripped)."""
    product_dir = tmp_path / "opencode"
    gateway = "https://user:leak-candidate@gw.example.test:8443/v1?token=leaked#frag"
    _gateway(product_dir, base_url=gateway, api_key="r0-snapshot-secret", model="product-model")
    loader = ConfigLoader(working_dir=tmp_path, product_config_dir=product_dir)
    runtime = loader.resolve_runtime_v3(cli=CliOverrides(context_limit=100000), endpoint_url=gateway)
    serialized = canonical_json(runtime.snapshot.model_dump(mode="json"))
    assert "r0-snapshot-secret" not in serialized  # the api_key value never rides the snapshot
    assert "leak-candidate" not in serialized and "token=leaked" not in serialized
    assert runtime.snapshot.endpoint_ref == "https://gw.example.test:8443/v1"  # _sanitize_endpoint discipline
    assert runtime.settings.model.model == "product-model"  # the product tier supplied the model
