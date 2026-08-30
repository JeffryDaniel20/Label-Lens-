"""Unit tests for magic-byte sniffing, PDF structural validation, and the
antivirus adapter's Null/error-path behaviour (no live daemon needed here)."""

from __future__ import annotations

import io

import pytest
from pypdf import PdfWriter

from app.ingestion.av import AvVerdict, NullAvScanner, build_av_scanner
from app.ingestion.magic_bytes import content_type_matches_extension, sniff_mime
from app.ingestion.pdf_checks import validate_pdf

pytestmark = pytest.mark.unit

JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 20
PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
WEBP_HEADER = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 20
HEIC_HEADER = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20
SVG_CONTENT = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
EXE_HEADER = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff"


def _pdf_bytes(*, pages: int = 1, encrypt: bool = False) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if encrypt:
        writer.encrypt("secret-password")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestMagicSniffing:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (JPEG_HEADER, "image/jpeg"),
            (PNG_HEADER, "image/png"),
            (WEBP_HEADER, "image/webp"),
            (HEIC_HEADER, "image/heic"),
            (b"%PDF-1.7\n%...", "application/pdf"),
        ],
    )
    def test_recognized_signatures(self, data: bytes, expected: str) -> None:
        assert sniff_mime(data) == expected

    def test_svg_disguised_as_png_is_not_recognized(self) -> None:
        # The classic spoofing case: SVG content with a .png filename/Content-Type.
        assert sniff_mime(SVG_CONTENT) is None

    def test_executable_is_not_recognized(self) -> None:
        assert sniff_mime(EXE_HEADER) is None

    def test_empty_and_short_input(self) -> None:
        assert sniff_mime(b"") is None
        assert sniff_mime(b"\xff") is None

    def test_polyglot_gif_pdf_is_recognized_as_only_one_type(self) -> None:
        # A polyglot crafted to look like two formats at once: our sniffer
        # matches the *first* signature at the correct offset and does not
        # attempt to reconcile ambiguity - the content-type cross-check in
        # `content_type_matches_extension` is what actually rejects a mismatch
        # against whatever extension the caller claimed.
        polyglot = b"GIF89a" + b"\x00" * 4 + b"%PDF-1.4"
        assert sniff_mime(polyglot) is None  # GIF is not in our allowlist at all


class TestContentTypeCrossCheck:
    @pytest.mark.parametrize(
        ("mime", "ext", "expected"),
        [
            ("image/jpeg", "jpg", True),
            ("image/jpeg", "jpeg", True),
            ("image/jpeg", "png", False),
            ("image/png", "png", True),
            ("application/pdf", "pdf", True),
            ("application/pdf", "jpg", False),
            ("image/webp", "webp", True),
            ("image/heic", "heic", True),
            ("image/heic", "jpg", False),
        ],
    )
    def test_cross_check(self, mime: str, ext: str, expected: bool) -> None:
        assert content_type_matches_extension(mime, ext) is expected


class TestPdfValidation:
    def test_valid_pdf_is_accepted_with_correct_page_count(self) -> None:
        result = validate_pdf(_pdf_bytes(pages=3), max_pages=30)
        assert result.ok
        assert result.page_count == 3

    def test_encrypted_pdf_is_rejected(self) -> None:
        result = validate_pdf(_pdf_bytes(pages=1, encrypt=True), max_pages=30)
        assert not result.ok
        assert "encrypt" in result.reason.lower()

    def test_pdf_exceeding_page_cap_is_rejected(self) -> None:
        result = validate_pdf(_pdf_bytes(pages=5), max_pages=3)
        assert not result.ok
        assert result.page_count == 5
        assert "maximum" in result.reason.lower()

    def test_malformed_pdf_is_rejected_not_crashed(self) -> None:
        garbage = b"%PDF-1.4\nthis is not a real pdf structure at all" + b"\x00" * 200
        result = validate_pdf(garbage, max_pages=30)
        assert not result.ok

    def test_empty_bytes_rejected(self) -> None:
        result = validate_pdf(b"", max_pages=30)
        assert not result.ok


class TestAvScannerFactory:
    def test_no_host_configured_gives_null_scanner(self) -> None:
        scanner = build_av_scanner(host="", port=3310)
        assert isinstance(scanner, NullAvScanner)
        verdict, signature = scanner.scan(b"anything")
        assert verdict is AvVerdict.SKIPPED
        assert signature is None

    def test_configured_host_gives_a_real_scanner_type(self) -> None:
        from app.ingestion.av import ClamdAvScanner

        scanner = build_av_scanner(host="localhost", port=3310)
        assert isinstance(scanner, ClamdAvScanner)

    def test_unreachable_daemon_reports_error_not_a_crash(self) -> None:
        from app.ingestion.av import ClamdAvScanner

        scanner = ClamdAvScanner(host="127.0.0.1", port=1, timeout=1.0)
        verdict, signature = scanner.scan(b"test payload")
        assert verdict is AvVerdict.ERROR
        assert signature is None
