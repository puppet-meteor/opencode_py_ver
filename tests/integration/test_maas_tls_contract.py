"""T-C3-010 contract set -- RED first: the provider mTLS client certificate.

Upstream face (2.3.2): provider settings ``tls: {cert, key, passphrase?}``
(PEM text, ``{file:...}`` expanded), ``Provider.tls()`` dropping empty
values, the model's ``http.tls`` presented on provider requests only, the
node runtime carrying it on https with redirects forced to error. The py
shape (decisions in doc/plans/port-232-increments.md T-C3-010):

- ``ClientTls`` (frozen dataclass) + ``MaasAdapter(client_tls=...)`` build
  one ``ssl.SSLContext`` (``load_cert_chain``) handed to the injected
  ``httpx2.AsyncClient(verify=...)``;
- construction fails fast on garbage PEM / a missing passphrase / a missing
  ``{file:...}`` reference (recorded deviation: upstream unfolds to empty
  and drops the certificate; py raises);
- the CLI resolves flag > env > config-layer into ``args.client_tls``;
- no ``client_tls`` keeps the SDK's own default client untouched.
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import httpx2
import pytest

from tests.integration.test_maas_tls_eval import (
    _TEST_CERT_PEM,
    _TEST_ENC_CERT_PEM,
    _TEST_ENC_KEY_PEM,
    _TEST_ENC_PASSPHRASE,
    _TEST_KEY_PEM,
)

_BASE = "https://gateway.internal/v1"


def _maas() -> Any:
    """The adapter module; ``ClientTls`` arrives with the ticket (RED assert)."""
    import opencode.ai.maas as module

    assert hasattr(module, "ClientTls"), "the ClientTls contract lands with T-C3-010"
    return module


def _adapter(tls: Any) -> Any:
    module = _maas()
    try:
        return module.MaasAdapter(_BASE, "k", client_tls=tls)
    except TypeError as error:
        raise AssertionError(f"client_tls parameter missing: {error}") from error


def _sdk_http_client(adapter: Any) -> Any:
    """AsyncOpenAI keeps its httpx2 client on ``_client`` (None = SDK default)."""
    return getattr(adapter._client, "_client", None)


def _ssl_context_of(adapter: Any) -> ssl.SSLContext | None:
    http_client = _sdk_http_client(adapter)
    if http_client is None:
        return None
    transport = getattr(http_client, "_transport", None)
    pool = getattr(transport, "_pool", None)
    context = getattr(pool, "_ssl_context", None)
    return context if isinstance(context, ssl.SSLContext) else None


# ---------------------------------------------------------------------------
# family 1: the adapter contract
# ---------------------------------------------------------------------------


def test_client_tls_builds_ssl_context_on_injected_http_client() -> None:
    adapter = _adapter(_maas().ClientTls(cert=_TEST_CERT_PEM, key=_TEST_KEY_PEM))
    http_client = _sdk_http_client(adapter)
    assert type(http_client) is httpx2.AsyncClient, "client_tls must inject an explicit httpx2.AsyncClient"
    context = _ssl_context_of(adapter)
    assert context is adapter._tls_context, "the pool must carry exactly our context"


def test_without_client_tls_keeps_sdk_default_client() -> None:
    adapter = _maas().MaasAdapter(_BASE, "k")
    http_client = _sdk_http_client(adapter)
    assert http_client is not None, "the SDK always owns an http client"
    # The SDK default is its own AsyncHttpxClientWrapper subclass (carrying
    # the truststore verify context); only the mTLS path injects a plain
    # httpx2.AsyncClient, so the exact type is the injection probe.
    assert type(http_client) is not httpx2.AsyncClient


# ---------------------------------------------------------------------------
# family 2: the ssl construction face (fail fast)
# ---------------------------------------------------------------------------


def test_plain_pair_and_encrypted_key_passphrase_matrix() -> None:
    module = _maas()
    _adapter(module.ClientTls(cert=_TEST_CERT_PEM, key=_TEST_KEY_PEM))
    with pytest.raises(ssl.SSLError):
        _adapter(module.ClientTls(cert=_TEST_ENC_CERT_PEM, key=_TEST_ENC_KEY_PEM))
    _adapter(module.ClientTls(cert=_TEST_ENC_CERT_PEM, key=_TEST_ENC_KEY_PEM, passphrase=_TEST_ENC_PASSPHRASE))


def test_garbage_pem_fails_at_construction() -> None:
    module = _maas()  # outside raises: the contract assert must not be swallowed
    # Garbage falls through to a bare path: the file read fails (OSError)
    # before ssl ever parses it; a same-named file in cwd would die in PEM.
    with pytest.raises((ssl.SSLError, OSError)):
        _adapter(module.ClientTls(cert="not a pem", key="not a key"))


# ---------------------------------------------------------------------------
# family 3: the config chain (flag > env > layer)
# ---------------------------------------------------------------------------


def test_cli_flag_and_env_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencode.cli import _parse_arguments

    for name in ("OPENCODE_PY_TLS_CERT", "OPENCODE_PY_TLS_KEY", "OPENCODE_PY_TLS_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    client_tls_type = _maas().ClientTls
    argv = ["--base-url", _BASE, "--api-key", "k", "--model", "m"]
    args = _parse_arguments(argv)
    assert getattr(args, "client_tls", None) is None

    monkeypatch.setenv("OPENCODE_PY_TLS_CERT", "env-cert")
    monkeypatch.setenv("OPENCODE_PY_TLS_KEY", "env-key")
    args_env = _parse_arguments(argv)
    env_tls = getattr(args_env, "client_tls", None)
    assert isinstance(env_tls, client_tls_type)
    assert env_tls.cert == "env-cert" and env_tls.key == "env-key"

    args_flag = _parse_arguments([*argv, "--client-tls-cert", "flag-cert", "--client-tls-key", "flag-key"])
    flag_tls = getattr(args_flag, "client_tls", None)
    assert isinstance(flag_tls, client_tls_type)
    assert flag_tls.cert == "flag-cert" and flag_tls.key == "flag-key"


def test_cli_layer_tls_key_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The config layer's ``tls`` map (upstream Settings.tls shape verbatim)."""
    from opencode.cli import _parse_arguments

    for name in ("OPENCODE_PY_TLS_CERT", "OPENCODE_PY_TLS_KEY", "OPENCODE_PY_TLS_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    client_tls_type = _maas().ClientTls
    config = tmp_path / "opencode.json"
    config.write_text(
        '{"tls": {"cert": "layer-cert", "key": "layer-key", "passphrase": "layer-pass"}}',
        encoding="utf-8",
    )
    argv = [
        "--base-url",
        _BASE,
        "--api-key",
        "k",
        "--model",
        "m",
        "--config",
        str(config),
    ]
    args = _parse_arguments(argv)
    layer_tls = getattr(args, "client_tls", None)
    assert isinstance(layer_tls, client_tls_type)
    assert layer_tls.cert == "layer-cert"
    assert layer_tls.key == "layer-key"
    assert layer_tls.passphrase == "layer-pass"


# ---------------------------------------------------------------------------
# family 4: {file:...} expansion
# ---------------------------------------------------------------------------


def test_file_refs_expand_and_missing_file_fails_fast(tmp_path: Path) -> None:
    module = _maas()  # outside raises: the contract assert must not be swallowed
    cert_file = tmp_path / "client.pem"
    key_file = tmp_path / "client.key"
    cert_file.write_text(_TEST_CERT_PEM, encoding="ascii")
    key_file.write_text(_TEST_KEY_PEM, encoding="ascii")
    adapter = _adapter(module.ClientTls(cert=f"{{file:{cert_file}}}", key=f"{{file:{key_file}}}"))
    assert _ssl_context_of(adapter) is not None
    with pytest.raises(ValueError, match="client-TLS file not found"):
        _adapter(module.ClientTls(cert="{file:C:/no/such/dir/client.pem}", key=_TEST_KEY_PEM))
