"""Rasterization and page normalization.

Every file that reaches `files.status = ready` gets one or more rendered
pages: a PDF is rasterized page-by-page to a 300 DPI PNG; a single-page image
format (jpg/png/webp/heic/tiff) is normalized to one PNG page with its EXIF
orientation applied and baked in (so downstream vision code never has to
special-case rotated JPEGs) and all other EXIF metadata dropped by virtue of
re-encoding to a fresh PNG with no `exif=` payload attached. Anything that
fails to decode here - despite having passed the magic-byte and PDF-structural
checks in `service.py` - is treated as further evidence the upload is not a
genuine, usable file and is rejected the same way.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pillow_heif
import pypdfium2 as pdfium
from PIL import Image, ImageOps
from pypdf.errors import PdfReadError

pillow_heif.register_heif_opener()

RENDER_DPI = 300
_PDF_POINTS_PER_INCH = 72


class RasterizationFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True, frozen=True)
class RenderedPage:
    page_no: int
    width: int
    height: int
    png_bytes: bytes


def rasterize(data: bytes, *, mime: str) -> list[RenderedPage]:
    if mime == "application/pdf":
        return _rasterize_pdf(data)
    return [_normalize_image(data)]


def _rasterize_pdf(data: bytes) -> list[RenderedPage]:
    scale = RENDER_DPI / _PDF_POINTS_PER_INCH
    pages: list[RenderedPage] = []
    try:
        pdf = pdfium.PdfDocument(data)
    except (pdfium.PdfiumError, ValueError) as exc:
        raise RasterizationFailed("The PDF could not be rendered.") from exc
    try:
        for index in range(len(pdf)):
            page = pdf[index]
            try:
                bitmap = page.render(scale=scale)
                try:
                    pil_image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()
            buffer = io.BytesIO()
            pil_image.save(buffer, format="PNG")
            pages.append(
                RenderedPage(
                    page_no=index + 1,
                    width=pil_image.width,
                    height=pil_image.height,
                    png_bytes=buffer.getvalue(),
                )
            )
    except (pdfium.PdfiumError, PdfReadError, ValueError) as exc:
        raise RasterizationFailed("The PDF could not be rendered.") from exc
    finally:
        pdf.close()
    if not pages:
        raise RasterizationFailed("The PDF has no renderable pages.")
    return pages


def _normalize_image(data: bytes) -> RenderedPage:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            oriented = ImageOps.exif_transpose(image) or image
            rgb = oriented.convert("RGB")
    except Exception as exc:  # any decode failure is a rejection, not a crash
        raise RasterizationFailed("The image could not be decoded.") from exc
    buffer = io.BytesIO()
    rgb.save(buffer, format="PNG")
    return RenderedPage(page_no=1, width=rgb.width, height=rgb.height, png_bytes=buffer.getvalue())
