"""Structural validation for uploaded PDFs.

Runs before any downstream rasterization. Rejects anything that could be used
to attack the pipeline: encryption (nothing to validate against), embedded
JavaScript or files (arbitrary active content and exfiltration vectors), and
page counts far beyond what a product label could plausibly need (both a
resource-exhaustion guard and evidence that the file is not a real label).
A malformed or unparseable PDF is rejected outright rather than allowed to
propagate an exception into the request handler.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError


@dataclass(slots=True, frozen=True)
class PdfValidationResult:
    ok: bool
    page_count: int = 0
    reason: str = ""


def validate_pdf(data: bytes, *, max_pages: int) -> PdfValidationResult:
    try:
        reader = PdfReader(io.BytesIO(data))
    except (PdfReadError, ValueError, OSError):
        return PdfValidationResult(ok=False, reason="The PDF could not be parsed.")

    if reader.is_encrypted:
        return PdfValidationResult(ok=False, reason="Encrypted PDFs are not accepted.")

    try:
        page_count = len(reader.pages)
    except (PdfReadError, ValueError, OSError):
        return PdfValidationResult(ok=False, reason="The PDF page tree could not be read.")

    if page_count <= 0:
        return PdfValidationResult(ok=False, reason="The PDF has no pages.")
    if page_count > max_pages:
        return PdfValidationResult(
            ok=False,
            page_count=page_count,
            reason=f"The PDF has {page_count} pages; the maximum is {max_pages}.",
        )

    if _has_javascript(reader):
        return PdfValidationResult(
            ok=False, page_count=page_count, reason="PDFs containing JavaScript are not accepted."
        )
    if _has_embedded_files(reader):
        return PdfValidationResult(
            ok=False,
            page_count=page_count,
            reason="PDFs containing embedded files are not accepted.",
        )

    return PdfValidationResult(ok=True, page_count=page_count)


def _has_javascript(reader: PdfReader) -> bool:
    try:
        root = reader.trailer["/Root"]
        names: object = root.get("/Names", {})  # type: ignore[attr-defined]
        has_js_names = isinstance(names, dict) and "/JavaScript" in names
        return bool(has_js_names or "/OpenAction" in root)  # type: ignore[operator]
    except (KeyError, TypeError):
        return False


def _has_embedded_files(reader: PdfReader) -> bool:
    try:
        root = reader.trailer["/Root"]
        names: object = root.get("/Names", {})  # type: ignore[attr-defined]
        return isinstance(names, dict) and "/EmbeddedFiles" in names
    except (KeyError, TypeError):
        return False
