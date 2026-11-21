"""Byte-sniffing mime detection (upstream ``core/src/mime.ts``, verbatim).

``detect_media`` reads only the leading signature bytes -- the same table as
the TS original, including the MP4 ``ftyp`` brand walk (AVIF/HEIC/QuickTime/
M4A share the container; the major brand and, for generic HEIF brands, the
compatible-brand list tell them apart). ``is_text`` mirrors the upstream
three-gate heuristic: a NUL byte is binary, a strict UTF-8 decode failure is
binary, and more than 30% control characters (outside TAB..CR) is binary.
``detect_file_mime`` is the artifact view face (``core/src/artifact.ts``
``detectFileMime``): the extension's registered mime wins, the magic table
is the fallback.
"""

from __future__ import annotations

import mimetypes

_TEXTUAL_SUFFIXES = (
    "text/",
    "+json",
    "+xml",
)
_TEXTUAL_EXACT = frozenset(
    {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-javascript",
    }
)


def is_textual(mime: str) -> bool:
    """The TS ``isTextual``: a mime the message pipeline treats as text."""
    if not mime or mime.startswith("text/"):
        return True
    return mime in _TEXTUAL_EXACT or mime.endswith(_TEXTUAL_SUFFIXES)


def _ascii(data: bytes, start: int, end: int) -> str:
    return data[start:end].decode("ascii", "replace")


def _starts_with(data: bytes, prefix: bytes) -> bool:
    return data[: len(prefix)] == prefix


def _is_text(data: bytes) -> bool:
    if not data:
        return True
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    controls = sum(1 for byte in data if byte < 9 or (13 < byte < 32))
    return controls / len(data) <= 0.30


def detect_media(data: bytes) -> str | None:
    """The TS ``detectMedia`` magic table; None when no signature matches."""
    if _starts_with(data, bytes.fromhex("89504e470d0a1a0a")):
        return "image/png"
    if _starts_with(data, b"\xff\xd8\xff"):
        return "image/jpeg"
    signature = _ascii(data, 0, 6)
    if signature in ("GIF87a", "GIF89a"):
        return "image/gif"
    if len(data) >= 18 and _starts_with(data, b"BM"):
        pixels = int.from_bytes(data[10:14], "little")
        dib = int.from_bytes(data[14:18], "little")
        # Pixels follow the 14-byte file header and a Windows or OS/2 DIB
        # header, possibly beyond this sample.
        if dib in (12, 16, 40, 52, 56, 64, 108, 124) and pixels >= 14 + dib:
            return "image/bmp"
    if _starts_with(data, b"%PDF-"):
        return "application/pdf"
    if _starts_with(data, b"RIFF") and _starts_with(data[8:12], b"WEBP"):
        return "image/webp"
    if len(data) >= 12 and _ascii(data, 4, 8) == "ftyp":
        # AVIF, HEIC, QuickTime and M4A share MP4's container; the major
        # brand tells them apart, and generic HEIF brands list avif/avis
        # among the compatible brands that follow the minor version.
        brand = _ascii(data, 8, 12)
        box_end = min(int.from_bytes(data[0:4], "big"), len(data))
        compatible = [_ascii(data, 16 + index * 4, 20 + index * 4) for index in range(max(0, (box_end - 16) // 4))]
        generic = brand in ("mif1", "msf1")
        if brand in ("avif", "avis") or (generic and any(item in ("avif", "avis") for item in compatible)):
            return "image/avif"
        if generic or brand in ("heic", "heix", "hevc", "hevx"):
            return "image/heic"
        if brand == "qt  ":
            return "video/quicktime"
        if brand == "M4A ":
            return "audio/mp4"
        return "video/mp4"
    return None


def detect(data: bytes) -> str:
    """The TS ``detect``: magic table, then the text heuristic, else bytes."""
    media = detect_media(data)
    if media is not None:
        return media
    return "text/plain" if _is_text(data) else "application/octet-stream"


def detect_file_mime(file_path: str, data: bytes) -> str:
    """Extension lookup first, magic sniffing second (the view face)."""
    guessed = mimetypes.guess_type(file_path)[0]
    if guessed:
        return guessed
    return detect(data)
