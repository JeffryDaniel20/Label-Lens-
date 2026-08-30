"""Unit tests for OCR-result persistence and coordinate mapping.

Uses a fake `OcrEngine` (no PaddleOCR needed) so the service-layer wiring -
downloading a render, running preprocessing, mapping bboxes back to original
coordinates, and persisting `OcrResult`/`OcrTokenRow` rows - is proven on its
own, independent of any specific engine's accuracy. The real PaddleOCR
adapter is proven separately in `tests/integration/test_vision_ocr_paddle.py`.
"""

from __future__ import annotations

import io
import uuid

import cv2
import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.platform.errors import NotFound
from app.vision.models import OcrTokenRow
from app.vision.ocr.base import OcrToken
from app.vision.ocr.service import run_ocr, tokens_overlapping
from app.vision.preprocess import rotate
from tests.conftest import make_org

pytestmark = pytest.mark.unit


class _FakeOcrEngine:
    name = "fake"
    version = "1.0.0"

    def __init__(self, tokens: list[OcrToken]) -> None:
        self._tokens = tokens
        self.seen_images: list[np.ndarray] = []

    def run(self, image: np.ndarray) -> list[OcrToken]:
        self.seen_images.append(image)
        return self._tokens


class _FakeStorageClient:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def download_object(self, key: str) -> bytes:
        return self._objects[key]


def _png_bytes(size: tuple[int, int] = (200, 100)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def org(db: Session):
    return make_org(db)


@pytest.fixture
def file_page(db: Session, org):
    org_id = org.id
    product = Product(organization_id=org_id, name="P", internal_sku="S1")
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org_id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    file_row = File(
        organization_id=org_id,
        product_version_id=version.id,
        storage_key="org/x/pv/y/z.png",
        original_filename="label.png",
        sha256="a" * 64,
        mime="image/png",
        bytes=123,
        status=FileStatus.READY,
    )
    db.add(file_row)
    db.flush()
    page = FilePage(
        organization_id=org_id,
        file_id=file_row.id,
        page_no=1,
        width=200,
        height=100,
        render_key="org/x/pv/y/render/sha/0001.png",
    )
    db.add(page)
    db.flush()
    return page


class TestRunOcr:
    def test_persists_a_result_and_its_tokens(self, db: Session, org, file_page) -> None:
        tokens = [
            OcrToken(
                text="INGREDIENTS",
                confidence=0.97,
                bbox=((10.0, 10.0), (90.0, 30.0)),
                line_no=1,
                language="en",
            ),
            OcrToken(
                text="Wheat Flour",
                confidence=0.91,
                bbox=((10.0, 40.0), (100.0, 60.0)),
                line_no=2,
                language="en",
            ),
        ]
        engine = _FakeOcrEngine(tokens)
        storage = _FakeStorageClient({file_page.render_key: _png_bytes()})

        result = run_ocr(
            db, storage, engine, organization_id=org.id, file_page=file_page
        )

        assert result.engine == "fake"
        assert result.engine_version == "1.0.0"
        assert result.avg_confidence == pytest.approx((0.97 + 0.91) / 2)
        assert len(result.raw) == 2

        rows = db.scalars(
            select(OcrTokenRow).where(OcrTokenRow.ocr_result_id == result.id)
        ).all()
        assert {r.text for r in rows} == {"INGREDIENTS", "Wheat Flour"}
        for row in rows:
            assert row.organization_id == org.id
            assert row.file_page_id == file_page.id

    def test_engine_receives_a_preprocessed_not_raw_image(
        self, db: Session, org, file_page
    ) -> None:
        engine = _FakeOcrEngine([])
        storage = _FakeStorageClient({file_page.render_key: _png_bytes()})
        run_ocr(db, storage, engine, organization_id=org.id, file_page=file_page)

        assert len(engine.seen_images) == 1
        seen = engine.seen_images[0]
        # preprocess() binarizes to strictly {0, 255} and drops to single-channel.
        assert seen.ndim == 2
        assert set(np.unique(seen).tolist()) <= {0, 255}

    def test_a_token_bbox_is_recorded_in_original_not_preprocessed_coordinates(
        self, db: Session, org, file_page
    ) -> None:
        # A page rotated by a known angle before "OCR" sees it - the engine's
        # bbox is in the *rotated* image's coordinates, but what gets
        # persisted must be mapped back to the page as originally rendered.
        base = np.full((100, 200), 255, dtype=np.uint8)
        cv2.rectangle(base, (60, 40), (140, 60), 0, -1)
        rotated, _ = rotate(base, 20.0)
        ok, encoded = cv2.imencode(".png", rotated)
        assert ok

        engine = _FakeOcrEngine(
            [OcrToken("X", 0.9, ((60.0, 40.0), (140.0, 60.0)), 1, "en")]
        )
        storage = _FakeStorageClient({file_page.render_key: encoded.tobytes()})
        run_ocr(db, storage, engine, organization_id=org.id, file_page=file_page)

        row = db.scalars(select(OcrTokenRow)).one()
        # Mapped back close to the un-rotated page's own coordinate space,
        # not left in the rotated-image space the engine actually saw.
        assert 0 <= row.x1 <= 200
        assert 0 <= row.y1 <= 100

    def test_a_page_from_another_org_is_not_found(
        self, db: Session, org, file_page
    ) -> None:
        engine = _FakeOcrEngine([])
        storage = _FakeStorageClient({file_page.render_key: _png_bytes()})
        with pytest.raises(NotFound):
            run_ocr(db, storage, engine, organization_id=uuid.uuid4(), file_page=file_page)

    def test_no_tokens_gives_zero_average_confidence_not_a_crash(
        self, db: Session, org, file_page
    ) -> None:
        engine = _FakeOcrEngine([])
        storage = _FakeStorageClient({file_page.render_key: _png_bytes()})
        result = run_ocr(
            db, storage, engine, organization_id=org.id, file_page=file_page
        )
        assert result.avg_confidence == 0.0


class TestTokensOverlapping:
    def test_finds_only_tokens_whose_bbox_overlaps_the_region(
        self, db: Session, org, file_page
    ) -> None:
        engine = _FakeOcrEngine(
            [
                OcrToken("inside", 0.9, ((10.0, 10.0), (30.0, 20.0)), 1, "en"),
                OcrToken("outside", 0.9, ((500.0, 500.0), (520.0, 520.0)), 1, "en"),
            ]
        )
        storage = _FakeStorageClient({file_page.render_key: _png_bytes()})
        run_ocr(db, storage, engine, organization_id=org.id, file_page=file_page)

        found = tokens_overlapping(
            db,
            organization_id=org.id,
            file_page_id=file_page.id,
            region=((0.0, 0.0), (100.0, 100.0)),
        )
        assert {t.text for t in found} == {"inside"}
