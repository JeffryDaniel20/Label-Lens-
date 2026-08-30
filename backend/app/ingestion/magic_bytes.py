"""Magic-byte content sniffing.

Deliberately hand-rolled rather than a wrapper around libmagic: the allowed
type set is small and fixed, a signature table is trivial to audit and test,
and it avoids a native dependency that is awkward to install consistently
across Windows, Linux containers, and CI. The claimed extension or
Content-Type is never trusted on its own - only what these signatures detect.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Signature:
    mime: str
    extensions: frozenset[str]
    magic: bytes
    offset: int = 0


_SIGNATURES: tuple[Signature, ...] = (
    Signature("image/jpeg", frozenset({"jpg", "jpeg"}), b"\xff\xd8\xff"),
    Signature("image/png", frozenset({"png"}), b"\x89PNG\r\n\x1a\n"),
    Signature("application/pdf", frozenset({"pdf"}), b"%PDF-"),
    Signature("image/tiff", frozenset({"tif", "tiff"}), b"II*\x00"),
    Signature("image/tiff", frozenset({"tif", "tiff"}), b"MM\x00*"),
)

# WEBP and HEIC use a RIFF/ISO-BMFF container with the real type a few bytes
# in, so they need a small offset check rather than a single fixed prefix.
_WEBP_RIFF = b"RIFF"
_WEBP_TAG = b"WEBP"
_HEIC_BRANDS = (b"heic", b"heix", b"heim", b"heis", b"hevc", b"mif1")


def sniff_mime(data: bytes) -> str | None:
    """Return the detected MIME type, or None if nothing recognized matches."""
    for sig in _SIGNATURES:
        end = sig.offset + len(sig.magic)
        if len(data) >= end and data[sig.offset : end] == sig.magic:
            return sig.mime

    if len(data) >= 12 and data[0:4] == _WEBP_RIFF and data[8:12] == _WEBP_TAG:
        return "image/webp"

    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS:
        return "image/heic"

    return None


def content_type_matches_extension(mime: str, extension: str) -> bool:
    for sig in _SIGNATURES:
        if sig.mime == mime:
            return extension in sig.extensions
    if mime == "image/webp":
        return extension == "webp"
    if mime == "image/heic":
        return extension == "heic"
    return False
