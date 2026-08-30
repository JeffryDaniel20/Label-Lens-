"""Persisted OCR output.

`OcrResult` is one OCR pass over one `FilePage` (an engine may be re-run, so
there can be more than one per page - e.g. once P3-T3 adds cloud escalation).
`OcrTokenRow` is one recognized token from that pass, with its bounding box
already mapped into the *original* page's coordinate space (see
`app/vision/ocr/service.py`), not the preprocessed image OCR actually saw -
downstream code should never have to know a preprocessing pipeline ran at
all.

`analysis_id` is deliberately absent: the DB schema in IMPLEMENTATION.md scopes
`ocr_results` to an analysis, but `analyses` does not exist until P5-T1. These
tables are scoped to `file_page_id` only for now, which is everything P3-T2's
acceptance criterion needs; a nullable `analysis_id` foreign key is a
follow-up for whoever builds P5-T1, exactly like `product_versions.locked_at`
being triggered manually until then (see P2-T1).
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey


class OcrResult(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "ocr_results"
    __table_args__ = (Index("ix_ocr_results_org_file_page", "organization_id", "file_page_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    file_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("file_pages.id", ondelete="CASCADE"), nullable=False
    )
    engine: Mapped[str] = mapped_column(String(50), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(50), nullable=False)
    avg_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    # The engine's own token/polygon output, verbatim (module-native types
    # coerced to plain JSON), before any coordinate remapping - kept for
    # re-derivation and debugging, distinct from the normalized rows below.
    raw: Mapped[list[object]] = mapped_column(JSON, nullable=False)


class OcrTokenRow(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "ocr_tokens"
    __table_args__ = (
        Index("ix_ocr_tokens_org_file_page", "organization_id", "file_page_id"),
        Index("ix_ocr_tokens_result", "ocr_result_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalized from `ocr_results.file_page_id` so a page's tokens can be
    # queried directly, the same way `files.organization_id` is denormalized
    # to avoid a join for every tenant-scoped lookup.
    file_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("file_pages.id", ondelete="CASCADE"), nullable=False
    )
    ocr_result_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("ocr_results.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(String(500), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    # Bounding box in the *original* page's pixel coordinates (top-left,
    # bottom-right). Plain floats for portability across SQLite and
    # PostgreSQL; on PostgreSQL a generated `box` column + GiST index
    # (migration 0004) additionally makes this genuinely spatially queryable.
    x1: Mapped[float] = mapped_column(Float, nullable=False)
    y1: Mapped[float] = mapped_column(Float, nullable=False)
    x2: Mapped[float] = mapped_column(Float, nullable=False)
    y2: Mapped[float] = mapped_column(Float, nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str | None] = mapped_column(String(10), default=None)
