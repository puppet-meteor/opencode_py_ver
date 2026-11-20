"""T-C3-010 evaluation set -- the provider mTLS client-certificate probes.

Always green: every probe reports what it measured as one JSON line; the
BEFORE run (capability absent) must answer ``capability-absent`` on every
scenario with zero noise, and the AFTER run flips each column. The banked
decisions live in doc/plans/port-232-increments.md T-C3-010.

Upstream face (2.3.2, five layers pinned by the recon): provider settings
``tls: {cert, key, passphrase?}`` (PEM text; ``{file:...}`` expands through
the config-wide substitution chain) -> ``Provider.tls()`` drops empty
cert/key -> model options ``http.tls`` -> ``withTls`` presents it on
provider requests only -> the node runtime layer carries it on https via an
undici agent, redirects forced to error.
"""

from __future__ import annotations

import inspect
import json
import ssl
from pathlib import Path
from typing import Any, cast

import pytest

#: A throwaway self-signed test pair (CN=opencode-py-test-client, expires
#: 2036) -- generated for this suite, safe to embed verbatim.
_TEST_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIDJTCCAg2gAwIBAgIUZkQNRVAy7AsbsbNuQu+ynqoaOdQwDQYJKoZIhvcNAQEL
BQAwIjEgMB4GA1UEAwwXb3BlbmNvZGUtcHktdGVzdC1jbGllbnQwHhcNMjYxMTIw
MjIyMDQ4WhcNMzYxMTE3MjIyMDQ4WjAiMSAwHgYDVQQDDBdvcGVuY29kZS1weS10
ZXN0LWNsaWVudDCCASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBAJhVVfHV
fLTaj+nk/YI0jZDeY0Xfvty0kw8TwqDqpbDrt/sGpUi3FGX6LxHti1nxRztI5wgH
pfvPC4eLEfv7Bfr/aAgKZMws99nx5aeg/5hoqpwLAylmTSksGyAl7S/+BBb7KeXz
dxLROGEnKn0ys3RUY5yu8aQXxkmKmJwVmqqJYTKX8p4Z5pRIUndhf2SEKqDyTXPw
J/uqpR4ozUy4+vEssIccsT1pa4WvEIGJbyYeTgSsY/124EPtziPinA2zVhB925Aa
K61SpHpSnMLcYRhdWPTZq7Ah6ih2qn50xDT3y1IWi2abfwfo5SmmaRZW711+5hV
k6z+ZLkJ4i9cRmECAwEAAaNTMFEwHQYDVR0OBBYEFHxiBJ48uwm5NO7ikXMvete7
1KYRMB8GA1UdIwQYMBaAFHxiBJ48uwm5NO7ikXMvete71KYRMA8GA1UdEwEB/wQF
MAMBAf8wDQYJKoZIhvcNAQELBQADggEBAJCdVWW+R8+zZyfP+HOFGMHhCGTbC3kK
+1Snha2DyMOoajLlEhs/c6FbP8idjXqX7By3rMqjCKkIMN+B/z7jStyLjjh/5ATb
lm7r/XDFH6vT7T56ycKmzwIUtkcOmB/X/nWjpwKXMD4kJwsNOx5wfnDzoXnZsh5a
Upldx3RP02/65RhGCeNRmR4lotIZSXmXhSwEG+cNlr3aOU7f9FzNQtcDjMdseru+
J6eiTReTXlZApyOGEGNM/DinJmD0RJULk0w71iiYHDPZ+JnSZOt7QphzgUIw7GLc
9AI+EidzUu+8pTXUEK5Z/eZGhE+GPJTCTbnrOk1aLwYOKfFtkMTZSGI=
-----END CERTIFICATE-----"""

_TEST_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCYVVXx1Xy02o/p
5P2CNI2Q3mNF377ctJMPE8Kg6qWw67f7BqVItxRl+i8R7YtZ8Uc7SOcIB6X7zwuH
ixH7+wX6/2gICmTMLPfZ8eWnoP+YaKqcCwMpZk0pLBsgJe0v/gQW+ynl83cS0Thh
Jyp9MrN0VGOcrvGkF8ZJipicFZqqiWEyl/KeGeaUSFJ3YX9khCqg8k1z8Cf7qqUe
KM1MuPrxLLCHHLE9aWuFrxCBiW8mHk4ErGP9duBD7c4j4pwNs1YQfduQGiutUqR6
UpzC3GEYXVj02auwIeoodqp+dMQ098tSFotmm38H6EOUppmkWVu9dfuYVZOs/mS5
CeIvXEZhAgMBAAECggEAHY06e8x8YZJf5lJFyr9asj6uOAUwn9zJBZ0B/hkfNRv5
aFGQ+XMI2kKnZH02ciegN7RMG27uaJPxKuWUAtPfBGMNYbfwd3qGx4lJkdQmGYE+
jcCGJ78gm2rixUEOSyfmT7YAWgt571TWsOQ59ilCjn/7hg8bMgfpr+Ua93aqd0gc
TBK2tdOskYtjFsZZXcQgmYEDgO9HVxfBr6i7M4hKx7xtzIC/YRAzSW7UxpWQkcQq
2sx4vQAe6D94JC6cZ/pkApPNH2dmha5rYOvLUuU5gWAyE1uKlSgxv2lruQ6aXkeQ
rkdOHEJCN0x6OXl9mYiCsM0QQZYyJwNWlAWteCYNNQKBgQDFyGivIaLcciIhselU
lrzpERYysk9i6oc0Q5d8ggUIOXOs+QNTT5y4WNitGD5Oge70KZ+IA0Ttwopyb5rT
w6fPLnEpeJhGvh1NrV2nHmvtLZdp6aJMyM0zGpUSrw/FPDU+k7ibVW3VoeGRqLhe
cQLJXG8m2HcFsgcJY6QaD36+BQKBgQDFLCgMoXi/iU9pHN6cEyQPInCyE7h0Mc/J
XKK+7wcfUAZYIZ9JYl5Vj8q4dL+0eIk+itQBZR+Zute9WWIDKgdCI/PS9teB9LEE
KCxNdIurDTTIGvmizBJa1Kb4ZXq0KPN1zBCEr/DWXYpEH2nG+hcYOijlnsMhSexn
+gBp8sf5rQKBgG4W2mY5I8zu3p7Ax+7kpKxabW+0nsmhX7awtN6CBMkKKaArTzWN
R9+43IqkxvGuE1Lm5DyyDSkGs0d1Zcnfy0VgRW7fCGvAINF+4XVHUq3HRdOXT1bM
IqQCdN3cgbu4gq6dTRFxYNyb6dHJBy7SWsm4YMpPjRinepE4XDyBSXv9AoGAY4hN
00pzxOSQxLQBH/J9onI1R3+y/lzi0nhD43SQONtRyI14djgE/NYwdHrjBzxNs9eq
++VLVXQ/UeNcGdLOhTHW9KkM0xptfXa2Hod1texdt6H+YJL5tQZiH0RC8800UUOp
FwIl5mCj2o2s/PfkTqOVX4yMXjKN91L5gIJBNqUCgYBKrxyD64f5jfMT+eLeLJHK
jncrwLE7bgPo+XTNupYkvlBpTN3bTqcbLgPsDgRbx3Yi0oa1JkIdQR0o/ikNSWs8
SS8hAG16T1cLagu8ZZN3YJ6D4y0KWdHmGm2Y9+dv3FGiLKitqZMImb29yYDLXyMT
399khp7A+7CDzlwn8gyalg==
-----END PRIVATE KEY-----"""

_TEST_ENC_KEY_PEM = """-----BEGIN ENCRYPTED PRIVATE KEY-----
MIIFNTBfBgkqhkiG9w0BBQ0wUjAxBgkqhkiG9w0BBQwwJAQQ9pOk5jluEuQ5m7B4
4T1RGwICCAAwDAYIKoZIhvcNAgkFADAdBglghkgBZQMEASoEEPC77GVsszwaWUPL
MuPARQkEggTQt3krMQXSotCUNd28+R3Zg54XXGG34e6U6WSKv8wN2OhbKTyxQasA
lG8JZ7MW4id5RfhLzvX2t823rK1X1ou6COIe69pB5+PsO493qVITcqzSLTh/Dx2W
dzy9f8MDa3G16tiu1QgvCloaf2USzJvGsymJRDFabyOIBuJtSl1bRwxfnf+rX6w
Bt8mGG9EWtTOhkPSQc5AP6hXchMSrpZgZIT4GPGw9M3mpgtf74H9t6K0iMXPKU7
S+O2px3mUQjIq5rwajql4aV4FjWStMoZXPk/1p4+UTywZxfGDJ+gAqHBu7j8xTyn
T/HaszYXcv6OBlI4At5maPbgephIeeavghMGgXjZjGYcYdLpWur8nv2gqJ+7Ovd
YxTQdN4xv3dmVEgANHSLPBaRjTXtLM22+aywvKuOlJA+02d0uVoa+KTqRy9jVRSh
3vyudyKgCRB6l0s81olO2RK0G7PW4hYz67bNP5cQktmkopuFAf1+FZDmyyoqCQwe
RS1WFtfcKsThdv+7z1gXKHiBwdDfLMx5tgxTaf4Pcvclw1ZunQFSNmMlGO1bK94+
PPay5JtI05s1a2cQovuzVj5jWOyJigAuyXnLP0Tr77KnGtF5lCgvN74SI8UE9CdH
AAcNJhMJajWlmgxCepQXwsCdr+4v6ee/acADRaK4QsZiTT2liZz9zAb1syg2mrVO
ZxsHhYG+MUs6ZqycokAJvYnvl0b4AAr3FpXekhxSFHcogw2xabyrmInACmwxRdhX
EM6H10xWxLvO+am3qlaLeTp0nAJAOlDgYBfNDCx5XSQYWPZtzfz9jF7J/nMaGK6z
x5P+L9MKKmmoR0Q20hWyRqo7G9kD/DY3XMrzhASUh/VQyIfT6JgarwVv6rJImE9t
UXMxd2QPO4YZOlRU4jzo0QDP4EObxK8aWOg1QyNosblOc8U4Qi+dhIWhsDBqIih
fQCIAocryPjjBNUQ4rynzPV+/0Xx6CcE77QtJhJhwg35QUJ6JpZtBSYrzAmfqmAn
sGjEqIVGqG8ClmtpRlX0d/nLyBuXZ3oJ3W9nrw004raoY3cJXvk1wljJ63OygKHP
rS9xOAvkEXR4etNdRB+kISkSE+cxqcpY8UDwTbuLLoVVZ7mLUE40PC+vz1Siu33
ENzJ2NJZDmZK7pzUcAeRwOCfrMOefPysFd29EZN4zKgyXgsvB5THQNxoigTg8k/z
MSQWskBbRNd1gy2zz0hLWQt/1+PBD6yBWieqiLc4Dfy5qpTveprirAnVvjROTKmw
uikE8W9cTbbjazAiuhX1+rGP30p13t2OrWb9SR5XAvqe865v+Ea5WX7dWYmG+Q6d
IzR3kmqIEpcAsljP2qsAXQdakGWasnRIZsMxWOjcCkt7ZPT3ZpwceY4DmuSAlAvS
JT3Vb1DiokKVMHzeDaGNjTuHbgSsNd9MCjYrzbV0jYQFzM25TqTq0XZtRmKfmux6
G7r6MvhP6g3H/b6UwJuRpA8NCfu8M0H8zdq/mYa49z9Zf7+ndlL94spYOX6Za56b
gjjL056SyI+AzVdMBLumPpb4K45b2WHMpI0+nrLk6OFzsmZYdOzDfxYI3yNspsgv
JtaP+c5MUUM+qo+1kaVghSVR+JONf+o1N/13rLRuVLbVYp/Y7ZWDFtA=
-----END ENCRYPTED PRIVATE KEY-----"""

_TEST_ENC_PASSPHRASE = "secret123"


def _report(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _client_tls_type() -> Any | None:
    """The ClientTls contract type, or None before the capability lands."""
    import opencode.ai.maas as maas_module

    return getattr(maas_module, "ClientTls", None)


async def _build(tls_value: Any) -> tuple[Any, BaseException | None]:
    """Construct the adapter with client_tls=..., returning (adapter, error).

    The call is measured, not statically known: until the capability lands,
    ``MaasAdapter.__init__`` has no ``client_tls`` parameter for mypy to see.
    """
    from opencode.ai.maas import MaasAdapter

    constructor = cast(Any, MaasAdapter)
    try:
        return constructor("https://gateway.internal/v1", "k", client_tls=tls_value), None
    except TypeError as error:
        raise error
    except BaseException as error:  # ssl failures surface as SSLError/OSError
        return None, error


async def test_p0_capability() -> None:
    """The adapter signature carries the client_tls parameter, plus the type."""
    from opencode.ai.maas import MaasAdapter

    parameters = inspect.signature(MaasAdapter.__init__).parameters
    present = "client_tls" in parameters
    _report(
        {
            "scenario": "P-capability",
            "status": "probed" if present else "capability-absent",
            "client_tls_parameter": present,
            "client_tls_type": _client_tls_type() is not None,
        }
    )


async def test_m1_construction_and_ssl_face(tmp_path: Path) -> None:
    """client_tls -> one ssl context presented as the http client's verify."""
    tls_type = _client_tls_type()
    if tls_type is None:
        _report({"scenario": "M1-tls-context", "status": "capability-absent"})
        return
    metrics: dict[str, Any] = {}
    # Plain pair: the adapter builds and its SDK client carries an SSLContext.
    try:
        adapter, error = await _build(tls_type(cert=_TEST_CERT_PEM, key=_TEST_KEY_PEM))
    except TypeError:
        _report({"scenario": "M1-tls-context", "status": "capability-absent"})
        return
    metrics["plain_pair_builds"] = error is None
    verify = None
    if adapter is not None:
        http_client = getattr(adapter._client, "_client", None)
        inner = getattr(http_client, "_client", None) if http_client is not None else None
        candidate = getattr(inner, "_verify", None) if inner is not None else None
        verify = candidate if isinstance(candidate, ssl.SSLContext) else None
    metrics["verify_is_ssl_context"] = verify is not None
    # Encrypted key without the passphrase fails at construction (fail fast).
    _, enc_error = await _build(tls_type(cert=_TEST_CERT_PEM, key=_TEST_ENC_KEY_PEM))
    metrics["encrypted_key_without_passphrase_raises"] = enc_error is not None
    # With the passphrase it builds.
    adapter_enc, enc2_error = await _build(
        tls_type(cert=_TEST_CERT_PEM, key=_TEST_ENC_KEY_PEM, passphrase=_TEST_ENC_PASSPHRASE)
    )
    metrics["encrypted_key_with_passphrase_builds"] = enc2_error is None and adapter_enc is not None
    # Garbage PEM fails at construction.
    _, garbage_error = await _build(tls_type(cert="not a pem", key="not a key"))
    metrics["garbage_pem_raises"] = garbage_error is not None
    _report(
        {
            "scenario": "M1-tls-context",
            "status": "mapped" if all(metrics.values()) else "partial",
            **metrics,
        }
    )


async def test_m2_config_chain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """flag > env > layer resolves the three tls sources onto the args face."""
    from opencode.cli import _parse_arguments

    tls_type = _client_tls_type()
    if tls_type is None:
        _report({"scenario": "M2-config-chain", "status": "capability-absent"})
        return
    for name in ("OPENCODE_PY_TLS_CERT", "OPENCODE_PY_TLS_KEY", "OPENCODE_PY_TLS_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    argv = ["--base-url", "https://g.internal", "--api-key", "k", "--model", "m"]
    args = _parse_arguments(argv)
    if not hasattr(args, "client_tls"):
        _report({"scenario": "M2-config-chain", "status": "capability-absent"})
        return
    metrics: dict[str, Any] = {}
    metrics["absent_when_unset"] = getattr(args, "client_tls", None) is None
    monkeypatch.setenv("OPENCODE_PY_TLS_CERT", _TEST_CERT_PEM)
    monkeypatch.setenv("OPENCODE_PY_TLS_KEY", _TEST_KEY_PEM)
    args_env = _parse_arguments(argv)
    env_tls = getattr(args_env, "client_tls", None)
    metrics["env_builds_tls"] = isinstance(env_tls, tls_type) and env_tls.cert == _TEST_CERT_PEM
    args_flag = _parse_arguments(
        [
            *argv,
            "--client-tls-cert",
            "flag-cert",
            "--client-tls-key",
            "flag-key",
        ]
    )
    flag_tls = getattr(args_flag, "client_tls", None)
    metrics["flag_wins_over_env"] = isinstance(flag_tls, tls_type) and flag_tls.cert == "flag-cert"
    _report(
        {
            "scenario": "M2-config-chain",
            "status": "chained" if all(metrics.values()) else "partial",
            **metrics,
        }
    )


async def test_m3_file_expansion(tmp_path: Path) -> None:
    """``{file:...}`` values expand to the file's text; missing files fail fast."""
    tls_type = _client_tls_type()
    if tls_type is None:
        _report({"scenario": "M3-file-expansion", "status": "capability-absent"})
        return
    cert_file = tmp_path / "client.pem"
    key_file = tmp_path / "client.key"
    cert_file.write_text(_TEST_CERT_PEM, encoding="ascii")
    key_file.write_text(_TEST_KEY_PEM, encoding="ascii")
    metrics: dict[str, Any] = {}
    try:
        adapter, error = await _build(tls_type(cert=f"{{file:{cert_file}}}", key=f"{{file:{key_file}}}"))
    except TypeError:
        _report({"scenario": "M3-file-expansion", "status": "capability-absent"})
        return
    metrics["file_refs_build"] = error is None and adapter is not None
    _, missing_error = await _build(tls_type(cert="{file:C:/no/such/dir/client.pem}", key=_TEST_KEY_PEM))
    metrics["missing_file_raises"] = missing_error is not None
    _report(
        {
            "scenario": "M3-file-expansion",
            "status": "expanded" if all(metrics.values()) else "partial",
            **metrics,
        }
    )
