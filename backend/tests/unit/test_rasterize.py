"""Unit tests for PDF rasterization and image page normalization.

No live services needed here - everything is pure decode/encode against
bytes built in-process (via pypdf, Pillow, and pillow-heif).
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from pypdf import PdfWriter

from app.ingestion.rasterize import RasterizationFailed, rasterize

pytestmark = pytest.mark.unit


def _pdf_bytes(page_sizes: list[tuple[int, int]]) -> bytes:
    writer = PdfWriter()
    for width, height in page_sizes:
        writer.add_blank_page(width=width, height=height)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _jpeg_bytes(*, size: tuple[int, int] = (40, 30), orientation: int | None = None) -> bytes:
    image = Image.new("RGB", size, color=(120, 40, 200))
    buf = io.BytesIO()
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(buf, format="JPEG", exif=exif.tobytes())
    else:
        image.save(buf, format="JPEG")
    return buf.getvalue()


def _png_bytes(size: tuple[int, int] = (25, 15)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(10, 200, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _heic_bytes(size: tuple[int, int] = (50, 60)) -> bytes:
    import pillow_heif

    heif_file = pillow_heif.from_pillow(Image.new("RGB", size, color=(10, 20, 220)))
    buf = io.BytesIO()
    heif_file.save(buf, quality=50)
    return buf.getvalue()


class TestPdfRasterization:
    def test_multi_page_pdf_yields_correct_page_count_and_dimensions(self) -> None:
        # Points at 72 DPI; rendering at 300 DPI should scale by 300/72.
        pages = rasterize(_pdf_bytes([(200, 200), (100, 300)]), mime="application/pdf")
        assert len(pages) == 2
        assert [p.page_no for p in pages] == [1, 2]
        assert pages[0].width == pytest.approx(200 * 300 / 72, abs=2)
        assert pages[0].height == pytest.approx(200 * 300 / 72, abs=2)
        assert pages[1].width == pytest.approx(100 * 300 / 72, abs=2)
        assert pages[1].height == pytest.approx(300 * 300 / 72, abs=2)
        for page in pages:
            assert Image.open(io.BytesIO(page.png_bytes)).format == "PNG"

    def test_single_page_pdf_yields_one_page(self) -> None:
        pages = rasterize(_pdf_bytes([(100, 100)]), mime="application/pdf")
        assert len(pages) == 1
        assert pages[0].page_no == 1

    def test_malformed_pdf_raises_rasterization_failed(self) -> None:
        garbage = b"%PDF-1.4\nnot a real pdf" + b"\x00" * 200
        with pytest.raises(RasterizationFailed):
            rasterize(garbage, mime="application/pdf")


class TestImageNormalization:
    def test_plain_jpeg_normalizes_to_a_png_page(self) -> None:
        pages = rasterize(_jpeg_bytes(size=(40, 30)), mime="image/jpeg")
        assert len(pages) == 1
        page = pages[0]
        assert page.page_no == 1
        assert (page.width, page.height) == (40, 30)
        decoded = Image.open(io.BytesIO(page.png_bytes))
        assert decoded.format == "PNG"

    def test_rotated_jpeg_normalizes_orientation(self) -> None:
        # Orientation 6 = rotate 270 (displayed as landscape<->portrait swap).
        pages = rasterize(_jpeg_bytes(size=(40, 30), orientation=6), mime="image/jpeg")
        assert len(pages) == 1
        page = pages[0]
        # Width/height are swapped once orientation is applied.
        assert (page.width, page.height) == (30, 40)
        decoded = Image.open(io.BytesIO(page.png_bytes))
        # The re-encoded PNG carries no EXIF orientation to reapply.
        assert decoded.getexif().get(0x0112) is None

    def test_png_passes_through_as_a_single_page(self) -> None:
        pages = rasterize(_png_bytes(size=(25, 15)), mime="image/png")
        assert len(pages) == 1
        assert (pages[0].width, pages[0].height) == (25, 15)

    def test_heic_is_decoded_and_normalized(self) -> None:
        pages = rasterize(_heic_bytes(size=(50, 60)), mime="image/heic")
        assert len(pages) == 1
        assert (pages[0].width, pages[0].height) == (50, 60)
        decoded = Image.open(io.BytesIO(pages[0].png_bytes))
        assert decoded.format == "PNG"

    def test_corrupt_image_bytes_raise_rasterization_failed(self) -> None:
        # Valid JPEG magic bytes but truncated/garbage body - passes the
        # magic-byte sniff in ingestion but cannot actually be decoded.
        corrupt = b"\xff\xd8\xff\xe0" + b"\x00" * 10
        with pytest.raises(RasterizationFailed):
            rasterize(corrupt, mime="image/jpeg")
