"""Real PaddleOCR verification (P3-T2).

PaddleOCR's inference engine (`paddlepaddle`) publishes no wheel past cp313,
so it cannot be installed in every environment this project might run tests
from - these tests are marked `paddleocr` and skip (not fail) when the
package isn't importable, the same opt-in pattern already used for
`postgres`/`object_storage`/`clamav`. To actually exercise this file, install
the optional extra in a compatible interpreter: `pip install ".[ocr]"`
(Python <=3.13) and run `pytest -m paddleocr`.

Everything here runs a *real* PaddleOCR pipeline - no stubbing - against
synthetic label fixtures with known ground-truth text, proving the adapter,
the coordinate mapping through a real rotation, and end-to-end persistence
all work with the genuine engine, not just the fake one used in
`tests/unit/test_vision_ocr_service.py`.
"""

from __future__ import annotations

import importlib.util
import os
import uuid

import cv2
import numpy as np
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.paddleocr]

_PADDLEOCR_AVAILABLE = importlib.util.find_spec("paddleocr") is not None
pytestmark.append(
    pytest.mark.skipif(
        not _PADDLEOCR_AVAILABLE,
        reason="paddleocr/paddlepaddle not installed - pip install '.[ocr]' (Python <=3.13)",
    )
)

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


def _font_path() -> str:
    for candidate in _FONT_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    pytest.skip("no usable TrueType font found for rendering the OCR fixture")


def _label_image(lines: list[str], *, font_size: int = 32) -> np.ndarray:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(_font_path(), font_size)
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    max_width = max(probe.textbbox((0, 0), line, font=font)[2] for line in lines)

    margin = 40
    line_height = font_size + 28
    canvas = Image.new("RGB", (max_width + 2 * margin, margin + line_height * len(lines)), "white")
    draw = ImageDraw.Draw(canvas)
    for i, line in enumerate(lines):
        draw.text((margin, margin // 2 + line_height * i), line, fill="black", font=font)
    return cv2.cvtColor(np.array(canvas), cv2.COLOR_RGB2BGR)


def _word_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein distance over words, normalized by reference word count."""
    ref = reference.split()
    hyp = hypothesis.split()
    if not ref:
        return 0.0 if not hyp else 1.0

    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        curr = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, start=1):
            cost = 0 if r == h else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1] / len(ref)


GOLDEN_LINES = [
    "INGREDIENTS: Wheat Flour, Sugar, Salt",
    "Contains: Milk, Soy",
    "Net Wt 250g  Best Before 12/2027",
]


@pytest.fixture(scope="module")
def paddle_engine():
    from app.vision.ocr.paddle import PaddleOcrEngine

    return PaddleOcrEngine(lang="en")


class TestGoldenCropWer:
    def test_word_error_rate_is_below_threshold_on_a_clean_synthetic_label(
        self, paddle_engine
    ) -> None:
        image = _label_image(GOLDEN_LINES)
        tokens = paddle_engine.run(image)
        assert len(tokens) == len(GOLDEN_LINES)

        # Tokens come back in detection order, which for stacked horizontal
        # lines is top-to-bottom - the same order as GOLDEN_LINES.
        tokens_by_y = sorted(tokens, key=lambda t: t.bbox[0][1])
        total_wer = 0.0
        for expected, token in zip(GOLDEN_LINES, tokens_by_y, strict=True):
            wer = _word_error_rate(expected, token.text)
            total_wer += wer
            assert wer <= 0.15, f"WER too high for {expected!r}: got {token.text!r} (wer={wer})"
        assert total_wer / len(GOLDEN_LINES) <= 0.1

    def test_confidence_is_high_on_clean_synthetic_text(self, paddle_engine) -> None:
        image = _label_image(GOLDEN_LINES)
        tokens = paddle_engine.run(image)
        assert all(t.confidence >= 0.90 for t in tokens)


class TestBboxSanity:
    def test_bboxes_are_well_formed_and_within_image_bounds(self, paddle_engine) -> None:
        image = _label_image(GOLDEN_LINES)
        tokens = paddle_engine.run(image)
        height, width = image.shape[:2]
        for token in tokens:
            (x1, y1), (x2, y2) = token.bbox
            assert 0 <= x1 < x2 <= width
            assert 0 <= y1 < y2 <= height

    def test_lines_are_ordered_top_to_bottom_and_non_overlapping_vertically(
        self, paddle_engine
    ) -> None:
        image = _label_image(GOLDEN_LINES)
        tokens = sorted(paddle_engine.run(image), key=lambda t: t.bbox[0][1])
        for earlier, later in zip(tokens, tokens[1:]):  # noqa: B905 - deliberately uneven pairs
            assert earlier.bbox[1][1] <= later.bbox[0][1] + 5  # small slack for line spacing


class TestEngineVersionDeterminism:
    def test_version_is_stable_and_matches_the_installed_package(self, paddle_engine) -> None:
        import importlib.metadata

        assert paddle_engine.version == importlib.metadata.version("paddleocr")
        # Re-querying is a pure metadata lookup, so it must be identical
        # every time - the whole point of recording it per model manifest.
        assert paddle_engine.version == importlib.metadata.version("paddleocr")

    def test_two_engine_instances_report_the_same_version(self) -> None:
        from app.vision.ocr.paddle import PaddleOcrEngine

        first = PaddleOcrEngine(lang="en")
        second = PaddleOcrEngine(lang="en")
        assert first.version == second.version
        assert first.name == second.name == "paddleocr"


class TestCoordinateMappingWithRealOcr:
    def test_a_token_found_on_a_rotated_fixture_maps_back_onto_the_real_text(
        self, paddle_engine
    ) -> None:
        from app.vision.preprocess import map_bbox_to_original, preprocess, rotate

        image = _label_image(GOLDEN_LINES)
        skewed, _ = rotate(image, 6.0)

        processed = preprocess(skewed)
        tokens = paddle_engine.run(processed.image)
        assert len(tokens) == len(GOLDEN_LINES)

        gray_skewed = cv2.cvtColor(skewed, cv2.COLOR_BGR2GRAY)
        for token in tokens:
            top_left, bottom_right = map_bbox_to_original(token.bbox, processed.to_original)
            x1, y1 = max(0, int(top_left[0]) - 8), max(0, int(top_left[1]) - 8)
            x2 = min(gray_skewed.shape[1], int(bottom_right[0]) + 8)
            y2 = min(gray_skewed.shape[0], int(bottom_right[1]) + 8)
            crop = gray_skewed[y1:y2, x1:x2]
            # A crop of the *original* (still-skewed) image at the mapped
            # bbox must actually contain dark text pixels, not blank
            # background - proof the mapping lands in the right place, not
            # just that it produces *some* coordinates.
            assert crop.size > 0
            dark_fraction = float((crop < 128).mean())
            assert dark_fraction > 0.02, (
                f"mapped bbox for {token.text!r} contains almost no ink "
                f"(dark_fraction={dark_fraction}) - coordinate mapping is likely wrong"
            )


class TestFullPersistencePipeline:
    """Runs the real engine through `run_ocr()` against a real (SQLite) DB
    session and a fake in-memory storage client standing in for a render
    already uploaded by P2-T4 - that storage *contract* is already proven
    live by P2-T4's own MinIO-backed tests, so this focuses on what P3-T2
    adds: real OCR output persisted with correctly-mapped coordinates."""

    def test_ocr_of_a_fixture_label_persists_tokens_queryable_spatially(
        self, db, paddle_engine
    ) -> None:
        from sqlalchemy import select

        from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
        from app.vision.models import OcrTokenRow
        from app.vision.ocr.service import run_ocr, tokens_overlapping
        from tests.conftest import make_org

        class _FakeStorage:
            def __init__(self, key: str, data: bytes) -> None:
                self._key, self._data = key, data

            def download_object(self, key: str) -> bytes:
                assert key == self._key
                return self._data

        org = make_org(db)
        product = Product(organization_id=org.id, name="P", internal_sku="S1")
        db.add(product)
        db.flush()
        version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
        db.add(version)
        db.flush()
        file_row = File(
            organization_id=org.id,
            product_version_id=version.id,
            storage_key="org/x/pv/y/z.png",
            original_filename="label.png",
            sha256="b" * 64,
            mime="image/png",
            bytes=1,
            status=FileStatus.READY,
        )
        db.add(file_row)
        db.flush()
        render_key = "org/x/pv/y/render/sha/0001.png"
        page = FilePage(
            organization_id=org.id,
            file_id=file_row.id,
            page_no=1,
            width=1,
            height=1,
            render_key=render_key,
        )
        db.add(page)
        db.flush()

        image = _label_image(GOLDEN_LINES)
        ok, encoded = cv2.imencode(".png", image)
        assert ok
        storage = _FakeStorage(render_key, encoded.tobytes())

        result = run_ocr(
            db, storage, paddle_engine, organization_id=org.id, file_page=page
        )
        assert result.engine == "paddleocr"
        assert result.avg_confidence >= 0.90

        rows = db.scalars(
            select(OcrTokenRow).where(OcrTokenRow.ocr_result_id == result.id)
        ).all()
        assert len(rows) == len(GOLDEN_LINES)
        for row in rows:
            assert row.text.strip() != ""
            assert row.confidence >= 0.90

        # Spatially query the region covering just the first (topmost) line.
        first_line = min(rows, key=lambda r: r.y1)
        found = tokens_overlapping(
            db,
            organization_id=org.id,
            file_page_id=page.id,
            region=((0.0, 0.0), (image.shape[1], first_line.y2 + 2)),
        )
        assert {r.id for r in found} == {first_line.id}
        assert uuid.UUID(str(first_line.ocr_result_id)) == result.id
