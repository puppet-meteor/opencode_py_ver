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

import base64
import inspect
import json
import ssl
from pathlib import Path
from typing import Any, cast

import pytest

#: A throwaway self-signed test pair (CN=opencode-py-test-client, expires
#: 2036) -- generated for this suite; embedded base64 so hand transcription cannot corrupt a byte.
_TEST_CERT_PEM = base64.b64decode(
    "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSURKVENDQWcyZ0F3SUJBZ0lVWmtRTlJWQXk3QXNic2JOdVF1K3lucW9h"
    "T2RRd0RRWUpLb1pJaHZjTkFRRUwKQlFBd0lqRWdNQjRHQTFVRUF3d1hiM0JsYm1OdlpHVXRjSGt0ZEdWemRDMWpiR2xsYm5R"
    "d0hoY05Nall4TVRJdwpNakl5TURRNFdoY05Nell4TVRFM01qSXlNRFE0V2pBaU1TQXdIZ1lEVlFRRERCZHZjR1Z1WTI5a1pT"
    "MXdlUzEwClpYTjBMV05zYVdWdWREQ0NBU0l3RFFZSktvWklodmNOQVFFQkJRQURnZ0VQQURDQ0FRb0NnZ0VCQUpoVlZmSFYK"
    "ZkxUYWorbmsvWUkwalpEZVkwWGZ2dHkwa3c4VHdxRHFwYkRydC9zR3BVaTNGR1g2THhIdGkxbnhSenRJNXdnSApwZnZQQzRl"
    "TEVmdjdCZnIvYUFnS1pNd3M5OW54NWFlZy81aG9xcHdMQXlsbVRTa3NHeUFsN1MvK0JCYjdLZVh6CmR4TFJPR0VuS24weXMz"
    "UlVZNXl1OGFRWHhrbUttSndWbXFxSllUS1g4cDRaNXBSSVVuZGhmMlNFS3FEeVRYUHcKSi91cXBSNG96VXk0K3ZFc3NJY2Nz"
    "VDFwYTRXdkVJR0pieVllVGdTc1kvMTI0RVB0emlQaW5BMnpWaEI5MjVBYQpLNjFTcEhwU25NTGNZUmhkV1BUWnE3QWg2aWgy"
    "cW41MHhEVDN5MUlXaTJhYmZ3Zm9RNVNtbWFSWlc3MTErNWhWCms2eitaTGtKNGk5Y1JtRUNBd0VBQWFOVE1GRXdIUVlEVlIw"
    "T0JCWUVGSHhpQko0OHV3bTVOTzdpa1hNdmV0ZTcKMUtZUk1COEdBMVVkSXdRWU1CYUFGSHhpQko0OHV3bTVOTzdpa1hNdmV0"
    "ZTcxS1lSTUE4R0ExVWRFd0VCL3dRRgpNQU1CQWY4d0RRWUpLb1pJaHZjTkFRRUxCUUFEZ2dFQkFKQ2RWV1crUjgrelp5ZlAr"
    "SE9GR01IaENHVGJDM2tLCisxU25oYTJEeU1Pb2FqTGxFaHMvYzZGYlA4aWRqWHFYN0J5M3JNcWpDS2tJTU4rQi96N2pTdHlM"
    "ampoLzVBVGIKbG03ci9YREZINnZUN1Q1NnljS216d0lVdGtjT21CL1gvbldqcHdLWE1ENGtKd3NOT3g1d2ZuRHpvWG5ac2g1"
    "YQpVcGxkeDNSUDAyLzY1UmhHQ2VOUm1SNGxvdElaU1htWGhTd0VHK2NObHIzYU9VN2Y5RnpOUXRjRGpNZHNlcnUrCko2ZWlU"
    "UmVUWGxaQXB5T0dFR05NL0RpbkptRDBSSlVMazB3NzFpaVlIRFBaK0puU1pPdDdRcGh6Z1VJdzdHTGMKOUFJK0VpZHpVdSs4"
    "cFRYVUVLNVovZVpHaEUrR1BKVENUYm5yT2sxYUx3WU9LZkZ0a01UWlNHST0KLS0tLS1FTkQgQ0VSVElGSUNBVEUtLS0tLQo="
).decode("ascii")

_TEST_KEY_PEM = base64.b64decode(
    "LS0tLS1CRUdJTiBQUklWQVRFIEtFWS0tLS0tCk1JSUV2QUlCQURBTkJna3Foa2lHOXcwQkFRRUZBQVNDQktZd2dnU2lBZ0VB"
    "QW9JQkFRQ1lWVlh4MVh5MDJvL3AKNVAyQ05JMlEzbU5GMzc3Y3RKTVBFOEtnNnFXdzY3ZjdCcVZJdHhSbCtpOFI3WXRaOFVj"
    "N1NPY0lCNlg3end1SAppeEg3K3dYNi8yZ0lDbVRNTFBmWjhlV25vUCtZYUtxY0N3TXBaazBwTEJzZ0plMHYvZ1FXK3lubDgz"
    "Y1MwVGhoCkp5cDlNck4wVkdPY3J2R2tGOFpKaXBpY0ZacXFpV0V5bC9LZUdlYVVTRkozWVg5a2hDcWc4azF6OENmN3FxVWUK"
    "S00xTXVQcnhMTENISExFOWFXdUZyeENCaVc4bUhrNEVyR1A5ZHVCRDdjNGo0cHdOczFZUWZkdVFHaXV0VXFSNgpVcHpDM0dF"
    "WVhWajAyYXV3SWVvb2RxcCtkTVEwOTh0U0ZvdG1tMzhINkVPVXBwbWtXVnU5ZGZ1WVZaT3MvbVM1CkNlSXZYRVpoQWdNQkFB"
    "RUNnZ0VBSFkwNmU4eDhZWkpmNWxKRnlyOWFzajZ1T0FVd245ekpCWjBCL2hrZk5SdjUKYUZHUStYTUkya0tuWkgwMmNpZWdO"
    "N1JNRzI3dWFKUHhLdVdVQXRQZkJHTU5ZYmZ3ZDNxR3g0bEprZFFtR1lFKwpqY0NHSjc4Z20ycml4VUVPU3lmbVQ3WUFXZ3Q1"
    "NzFUV3NPUTU5aWxDam4vN2hnOGJNZ2ZwcitVYTkzYXFkMGdjClRCSzJ0ZE9za1l0akZzWlpYY1FnbVlFRGdPOUhWeGZCcjZp"
    "N000aEt4N3h0eklDL1lSQXpTVzdVeHBXUWtjUXEKMnN4NHZRQWU2RDk0SkM2Y1ovcGtBcFBOSDJkbWhhNXJZT3ZMVXVVNWdX"
    "QXlFMXVLbFNneHYybHJ1UTZhWGtlUQpya2RPSEVKQ04weDZPWGw5bVlpQ3NDMFFRWll5SndOV2xBV3RlQ1lOTlFLQmdRREZ5"
    "R2l2SWFMY2NpSWhzZWxVCmxyenBFUll5c2s5aTZvYzBRNWQ4Z2dVSU9YT3MrUU5UVDV5NFdOaXRHRDVPZ2U3MEtaK0lBMFR0"
    "d29weWI1clQKdzZmUExuRXBlSmhHdmgxTnJWMm5IbXZ0TFpkcDZhSk15TTB6R3BVU3J3L0ZQRFUrazdpYlZXM1ZvZUdScUxo"
    "ZQpjUUxKWEc4bTJIY0ZzZ2NKWTZRYUQzNitCUUtCZ1FERkxDZ01vWGkvaVU5cEhONmNFeVFQSW5DeUU3aDBNYy9KClhLSys3"
    "d2NmVUFaWUlaOUpZbDVWajhxNGRMKzBlSWsraXRRQlpSK1p1dGU5V1dJREtnZENJL1BTOXRlQjlMRUUKS0N4TmRJdXJEVFRJ"
    "R3ZtaXpCSmExS2I0WlhxMEtQTjF6QkNFci9EV1hZcEVIMm5HK2hjWU9pamxuc01oU2V4bgorZ0JwOHNmNXJRS0JnRzRXMm1Z"
    "NUk4enUzcDdBeCs3a3BLeGFiVyswbnNtaFg3YXd0TjZDQk1rS0thQXJUeldOClI5KzQzSXFreHZHdUUxTG01RHl5RFNrR3Mw"
    "ZDFaY25meTBWZ1JXN2ZDR3ZBSU5GKzRYVkhVcTNIUmRPWFQxYk0KSXFRQ2ROM2NnYnU0Z3E2ZFRSRnhZTnliNmRISkJ5N1NX"
    "c200WU1wUGpSaW5lcEU0WER5QlNYdjlBb0dBWTRoTgowMHB6eE9TUXhMUUJIL0o5b25JMVIzK3kvbHppMG5oRDQzU1FPTnRS"
    "eUkxNGRqZ0UvTll3ZEhyakJ6eE5zOWVxCisrVkxWWFEvVWVOY0dkTE9oVEhXOUtrTTB4cHRmWGEySG9kMXRleGR0NkgrWUpM"
    "NXRRWmlIMFJDODgwMFVVT3AKRndJbDVtQ2oybzJzL1Bma1RxT1ZYNHlNWGpLTjkxTDVnSUpCTnFVQ2dZQktyeHlENjRmNWpm"
    "TVQrZUxlTEpISwpqbmNyd0xFN2JnUG8rWFROdXBZa3ZsQnBUTjNiVHFjYkxnUHNEZ1JieDNZaTBvYTFKa0lkUVIwby9pa05T"
    "V3M4ClNTOGhBRzE2VDFjTGFndThaWk4zWUo2RDR5MEtXZEhtR20yWTkrZHYzRkdpTEtpdHFaTUltYjI5eVlETFh5TVQKMzk5"
    "a2hwN0ErN0NEemx3bjhneWFsZz09Ci0tLS0tRU5EIFBSSVZBVEUgS0VZLS0tLS0K"
).decode("ascii")

_TEST_ENC_CERT_PEM = base64.b64decode(
    "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSURMVENDQWhXZ0F3SUJBZ0lVVlZNbmhVQXdCOEtRQUxSSXA2ZFM1cnhj"
    "eEswd0RRWUpLb1pJaHZjTkFRRUwKQlFBd0pqRWtNQ0lHQTFVRUF3d2JiM0JsYm1OdlpHVXRjSGt0ZEdWemRDMWpiR2xsYm5R"
    "dFpXNWpNQjRYRFRJMgpNVEV5TURJeU1qQTBPRm9YRFRNMk1URXhOekl5TWpBME9Gb3dKakVrTUNJR0ExVUVBd3diYjNCbGJt"
    "TnZaR1V0CmNIa3RkR1Z6ZEMxamJHbGxiblF0Wlc1ak1JSUJJakFOQmdrcWhraUc5dzBCQVFFRkFBT0NBUThBTUlJQkNnS0MK"
    "QVFFQWtoOGpaWG5OZEFHQVlTc1AzNEN1SFZBeDZqNmdhRzBwSHhDMDI0UW1hRUF1TTdOTUgwNkZHY21xck85YQo3RVI0Y1F0"
    "QW1XNmpFRG8rbG52NXZsZ3ptc2U1VGx3WFpnbnhZRis2azZzZnJmTkV6ZkFlM0ErM1hKcTBmaGxFCldFOWtBVWgrLzdGc0sx"
    "Vnp2eERQNzhLaFErYjJXZEVQaytxMUtFakFkZWdSMWFlbmtjQUk3ejBuRXRSYm9RR3MKblgxVXdUdU4vYXA3RmExVkowNmxZ"
    "b05aMTA2Ry9RZTQxcCsvQkcvZHNYVGdMcXp2L2l3SnNpcWFoWEFhKzBtVQpIQjExS1ZWbzAzNWpBV3hzM1drYnQrdGQrT1hy"
    "dDVxUW5wc2pHcWNjZUY0akFaWnVLZGFYL3FQKzB2bitVWmFCClFtM3ZTRFREemg4VnRkcE5NYjRNWTFiVURRSURBUUFCbzFN"
    "d1VUQWRCZ05WSFE0RUZnUVVtVW9KNUthQlo3OHoKS0E1L3QwQTZuTUx3SHlVd0h3WURWUjBqQkJnd0ZvQVVtVW9KNUthQlo3"
    "OHpLQTUvdDBBNm5NTHdIeVV3RHdZRApWUjBUQVFIL0JBVXdBd0VCL3pBTkJna3Foa2lHOXcwQkFRc0ZBQU9DQVFFQWExZXVB"
    "SWJBTm85a3Y5MVRTN050CjRxTldlRktyRHQreFFBWWNjT1hlM3B6NzRIZ2lCQXVOYnBmWkFqbUorU0lFRDhaVGxSL01SMllR"
    "OXdCQ1N1S3QKditUNVBpaUowTlJBYnRydHJHOTBWbGNHaDRQUGh4eTVzY0FHNlE4b2RaVXd3M1lvZWRhSWV0MkNpQUtObW1I"
    "WgpYb2V6TGVyZHZ5bi9LQVBNcVcwRFFHVjFIVWFyTnRYaWhkOWZkVWZPeHhWT2dVUmQwUC9HUXVCUTd0QTI2dFRCClRoSFlF"
    "SWRTSk1ZWjh2b202T1dDUjFrMEFzSE9PNUZNNXFmVHVBN3kyazNCSC9Qc2xQalRBR1lrc0R5VlE2cTMKUGdPVGlReVpWQWYy"
    "YUFRck1DUlBPNFp5SzIyNjJFT2JsUUFZKy84NTB3VzkyUzRVZWt3SmpaNnErcFpyNnozZAo4dz09Ci0tLS0tRU5EIENFUlRJ"
    "RklDQVRFLS0tLS0K"
).decode("ascii")

_TEST_ENC_KEY_PEM = base64.b64decode(
    "LS0tLS1CRUdJTiBFTkNSWVBURUQgUFJJVkFURSBLRVktLS0tLQpNSUlGTlRCZkJna3Foa2lHOXcwQkJRMHdVakF4QmdrcWhr"
    "aUc5dzBCQlF3d0pBUVE5cE9rNWpsdUV1UTVtN0I0CjRUMVJHd0lDQ0FBd0RBWUlLb1pJaHZjTkFna0ZBREFkQmdsZ2hrZ0Ja"
    "UU1FQVNvRUVQQzc3R1Zzc3p3YVdVUEwKTXVQQVJRa0VnZ1RRdDNrck1RWFNvdENVTmQyOCtSM1pnNTRYWEdHMzRlNlU2V1NL"
    "djh3TjJPaGJLVHl4UWFzQQpsRzhKWjdNVzRpZDVSZmhMenZYMnQ4MjNySzFYMW91NkNPSWU2OXBCNStQc080OTNxVklUY3F6"
    "U0xUaC9EeDJXCmR6eTlmOE1EYTNHMTZ0aXUxUWd2Q2xvYWYyVVN6Sk52R3N5bUpSREZhYnlPSUJ1SnRTbDFiUnd4Zm5mK3JY"
    "NncKQnQ4bUdHOUVXdFRPaGtQU3BRYzVBUDZoWGNoTVNycFpnWklUNEdQR3c5TTNtcGd0Zjc0SDl0NkswaU1YUEtVNwpTK08y"
    "cHgzbVVRaklxNXJ3YWpxbDRhVjRGaldTdE1vWlhQay8xcDQrVVR5d1p4ZkdESitnS3FIQnU3ajh4VHluCmtUL0hhc3pZWGN2"
    "Nk9CbEk0QXQ1bWFQYmdlcGhJZWVhdmdoTUdnWGpaakdZY1lkTHBXdXI4bnYyZ3FKKzdPdmQKWXhUUWRONHh2M2RtVkVnQU5I"
    "U0xQQmFSalRYdExNMjIrYXl3dkt1T2xKQSswMmQwdVZvYStLVHFSeTlqVlJTaAozdnl1ZHlLZ0NSQjZsMHM4MW9sTzJSSzBH"
    "N1BXNGhZejY3Yk5QNWNRa3Rta29wdUZBZjErRlpEbXl5b3FDUXdlClJTMVdGdGZjS3NUaGR2Kzd6MWdYS0hpQndkRGZMTXg1"
    "dGd4VGFmNFBjdmNsdzFadW5RRlNObU1sR08xYks5NCsKUFBheTVKdEkwNXMxYTJjUW92dXpWajVqV095SmlnQXV5WG5MUDBU"
    "cjc3S25HdEY1bENndk43NFNJOFVFOUNkSApBQWNOSmhNSmFqV2xtZ3hDZXBRWHdzQ2RyKzR2NmVlL2FjQURSYUs0UXNaaVRU"
    "MmxpQ3o5ekFiMXN5ZzJtclZPClp4c0hoWUcrTVVzNlpxeWNva0FKdlludmwwYjRBQXIzRnBYZWtoeFNGSGNvZ3cyeGFieXJt"
    "SW5BQ213eFJkaFgKRU02SDEweFd4THZPK2FtM3FsYUxlVHAwbkFKQU9sRGdZQmZOREN4NVhTUVlXUFp0emZ6OWpGN0ovbk1h"
    "R0s2egp4NVArTDlNS0ttbW9SMFEyMGhXeVJxbzdKOUdrRC9EWVhNcnpoQVNVaC9WUXlJZlQ2SmdhcndWdjZySkltRTl0ClVY"
    "TXhkMlFQTzRZWk9sUlU0UWp6bzBRRFA0RU9ieEs4YVdPZzFReU5vc2JsT2M4VTRRaStkaElXaHNEQnFJaWgKZlFDSUFvY3J5"
    "UGpqQk5VUTRyeW56UFYrLzBYeDZDY0U3N1F0SmhKaHdnMzVRVUo2UHBadEJTWXJ6QW1mcW1BbgpzR2pFcUlWR3FHOENsbXRw"
    "UmxYMGQvbkx5QnVYWjNvSjNXOW5ydzAwNHJhb1kzY0pYdmsxd2xqSjYzT3lnS0hQCnJTOXhPQXZrRVhSNGV0TmRSQitrSVN3"
    "U0UrY3hxY3BZOFVEd1RidUxMb1ZWWjdtTFVFNDBQQyt2ejFTaVh1MzMKRU56SjJOSlpEbVpLN3B6VWNBZVJ3T0Nmck1PZWZQ"
    "eXNGZDI5RVpONHpLZ3lYZ3N2QjVUSFFOeG9pZ1RnOGsvegpNU1FXc2tCYlJOZDFneTJ6ejBoTFdRdC8xK1BCRDZ5QldpZXFp"
    "TGM0ZEZ5NXFwVHZlcHJpckFuVnZqUk9US213CnVpa0U4VzljVGJiamF6QWl1aFgxK3JHUDMwcDEzdDJPckJxOVNSNVhBdnFl"
    "ODY1ditFYTVXWDdkV1ltRytRNmQKSXpSM2ttcUlFcGNBc2xqUDJxc0FYUWRha0dXYXNuUklac014V09qY0NrdDdaUFQzWnB3"
    "Y2VZNERtdVNBbEF2UwpKVDNWYjFEaW9rS1ZNSHplRGFHTmpUdUhiZ1NzTmQ5TUNqWXJ6YlYwallRRnpNMjVUcVRxMFhadFJt"
    "S2ZtdXg2Ckc3cjZNdmhQNmczSC9iNlV3SnVScEE4TkNmdThNMEg4emRxL21ZYTQ5eDlaZjcrbmRsTDk0c3BZT1g2WmE1NmIK"
    "Z2pqTDA1NlN5SStBelZkTUJMdW1QcGI0SzQ1YjJXSE1wSTArbnJMazZPRnpzbVpZZE96RGZ4WUkzeU5zcHNndgpKdGFQK2M1"
    "TVVVTStxbysxa2FWZ2hTVlIrSk9OZitvMU4vMTNyTFJ1VkxiVllwL1k3WldERnRBPQotLS0tLUVORCBFTkNSWVBURUQgUFJJ"
    "VkFURSBLRVktLS0tLQo="
).decode("ascii")

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
        # AsyncOpenAI._client is the httpx2.AsyncClient; its pool carries the
        # injected SSLContext (verified experimentally on httpx2 2.x).
        sdk_http = getattr(adapter._client, "_client", None)
        transport = getattr(sdk_http, "_transport", None) if sdk_http is not None else None
        pool = getattr(transport, "_pool", None) if transport is not None else None
        candidate = getattr(pool, "_ssl_context", None) if pool is not None else None
        verify = candidate if isinstance(candidate, ssl.SSLContext) else None
    metrics["verify_is_ssl_context"] = verify is not None
    # Encrypted-key pair (its own certificate -- a cross-pair mix would fail
    # the pubkey match, not the passphrase face under test).
    _, enc_error = await _build(tls_type(cert=_TEST_CERT_PEM, key=_TEST_ENC_KEY_PEM))
    metrics["encrypted_key_without_passphrase_raises"] = enc_error is not None
    # With the passphrase it builds.
    adapter_enc, enc2_error = await _build(
        tls_type(cert=_TEST_ENC_CERT_PEM, key=_TEST_ENC_KEY_PEM, passphrase=_TEST_ENC_PASSPHRASE)
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
