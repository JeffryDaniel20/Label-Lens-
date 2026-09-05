"""Runs an `OcrEngine` against a rasterized page and persists its tokens.

This is the one place OCR tokens get their bounding boxes mapped out of the
preprocessed image `preprocess()` actually ran on and into the *original*
page's coordinates - the mapping X-10 depends on. Nothing here interprets
what a token says; it only records text, confidence, position, and language,
per the "AI extracts, rules decide" principle.
"""

from __future__ import annotations

import uuid

import cv2
import numpy as np
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.catalog.models import FilePage
from app.db.session import is_postgres
from app.platform.errors import NotFound
from app.storage.client import ObjectStorageClient
from app.vision.models import OcrResult, OcrTokenRow
from app.vision.ocr.base import OcrEngine, OcrToken
from app.vision.preprocess import map_bbox_to_original, preprocess
from app.vision.transform import Point

_default_engine: OcrEngine | None = None


def get_default_ocr_engine() -> OcrEngine:
    """A process-wide cached `PaddleOcrEngine` (P3-T2).

    Constructing one loads real model weights - expensive enough that
    building a fresh engine per analysis, rather than once per worker
    process, would dominate the whole pipeline's latency. Callers that need
    a different engine (a fake in tests, a future cloud-fallback adapter)
    pass their own rather than going through this cache.
    """
    global _default_engine
    if _default_engine is None:
        from app.vision.ocr.paddle import PaddleOcrEngine  # noqa: PLC0415 - lazy, see paddle.py

        _default_engine = PaddleOcrEngine()
    return _default_engine


def _decode_image(data: bytes) -> np.ndarray:
    array = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("The rendered page could not be decoded as an image.")
    return image


def _token_to_raw(token: OcrToken) -> dict[str, object]:
    return {
        "text": token.text,
        "confidence": token.confidence,
        "bbox": [list(token.bbox[0]), list(token.bbox[1])],
        "line_no": token.line_no,
        "language": token.language,
    }


def run_ocr(
    db: Session,
    storage: ObjectStorageClient,
    engine: OcrEngine,
    *,
    organization_id: uuid.UUID,
    file_page: FilePage,
) -> OcrResult:
    if file_page.organization_id != organization_id:
        raise NotFound("File page not found.")

    data = storage.download_object(file_page.render_key)
    image = _decode_image(data)
    processed = preprocess(image)
    tokens = engine.run(processed.image)

    result = OcrResult(
        organization_id=organization_id,
        file_page_id=file_page.id,
        engine=engine.name,
        engine_version=engine.version,
        avg_confidence=(sum(t.confidence for t in tokens) / len(tokens)) if tokens else 0.0,
        raw=[_token_to_raw(t) for t in tokens],
    )
    db.add(result)
    db.flush()

    for token in tokens:
        top_left, bottom_right = map_bbox_to_original(token.bbox, processed.to_original)
        db.add(
            OcrTokenRow(
                organization_id=organization_id,
                file_page_id=file_page.id,
                ocr_result_id=result.id,
                text=token.text,
                confidence=token.confidence,
                x1=top_left[0],
                y1=top_left[1],
                x2=bottom_right[0],
                y2=bottom_right[1],
                line_no=token.line_no,
                language=token.language,
            )
        )
    db.flush()
    return result


def tokens_overlapping(
    db: Session,
    *,
    organization_id: uuid.UUID,
    file_page_id: uuid.UUID,
    region: tuple[Point, Point],
) -> list[OcrTokenRow]:
    """Tokens on `file_page_id` whose bbox overlaps `region` (top-left,
    bottom-right, in the page's original coordinates).

    On PostgreSQL this runs as a real spatial query against the generated
    `bbox_box` column via the native `&&` (overlap) operator, which is what
    the `ix_ocr_tokens_bbox_gist` GiST index exists to accelerate. SQLite has
    no `box` type, so there it falls back to an equivalent plain range-overlap
    filter over the x1/y1/x2/y2 columns - same result, no index acceleration.
    """
    (rx1, ry1), (rx2, ry2) = region
    if is_postgres(db):
        rows = db.scalars(
            select(OcrTokenRow)
            .where(
                OcrTokenRow.organization_id == organization_id,
                OcrTokenRow.file_page_id == file_page_id,
            )
            .where(
                text("bbox_box && box(point(:rx1, :ry1), point(:rx2, :ry2))").bindparams(
                    rx1=rx1, ry1=ry1, rx2=rx2, ry2=ry2
                )
            )
        ).all()
        return list(rows)

    stmt = select(OcrTokenRow).where(
        OcrTokenRow.organization_id == organization_id,
        OcrTokenRow.file_page_id == file_page_id,
        OcrTokenRow.x1 <= rx2,
        OcrTokenRow.x2 >= rx1,
        OcrTokenRow.y1 <= ry2,
        OcrTokenRow.y2 >= ry1,
    )
    return list(db.scalars(stmt).all())
