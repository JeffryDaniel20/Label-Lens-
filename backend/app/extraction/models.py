"""Persisted extraction output (P3-T5) and evidence verification (P3-T6).

`Extraction` is one LLM pass over one analysis's OCR text; `ExtractedField`
is one row per dotted field path - the join point evidence hangs off, keyed
by the same path `rules/*.yaml` already references (`ingredients.items`), so
a rule, a fact, and its evidence all address the same field the same way.

`cited_token_ids` holds the OCR tokens the model said each value came from.
`app.extraction.evidence` (P3-T6) is what turns those citations into
`EvidenceSpan` rows with bboxes and snippets, and what enforces the
substring-match check that sets `verified`/`match_ratio` - a field that
fails demotes its corresponding `LabelFacts` entry to an explicit absence
*before* `Extraction.payload` is ever persisted, so `verified`/`match_ratio`
here are a record of what happened, not something downstream code must
remember to re-check.

`value_norm`/`unit` are deliberately nullable and unpopulated here: §8 step 7
is explicit that normalization is deterministic Python, not the model, so
those columns are filled by the `normalizing` stage using P3-T7's library.
The columns exist now so that stage needs no second migration.
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey

# IMPLEMENTATION.md §6 calls for "JSONB for extraction payloads" and a GIN
# index on `extractions.payload`. Plain `json` cannot carry a GIN index at
# all (Postgres has no default operator class for it), so these columns are
# genuinely `jsonb` on PostgreSQL while staying ordinary JSON on SQLite,
# which the fast test suite runs against.
JsonB = JSON().with_variant(JSONB(), "postgresql")


class Extraction(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "extractions"
    __table_args__ = (Index("ix_extractions_org_analysis", "organization_id", "analysis_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("analyses.id", ondelete="CASCADE"), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    # The validated `LabelFacts` dump - the contract the rule engine reads.
    payload: Mapped[dict[str, object]] = mapped_column(JsonB, nullable=False)
    # The provider's own structured response verbatim, before mapping into
    # `LabelFacts` - kept for re-derivation and debugging, exactly like
    # `ocr_results.raw` keeps the engine's native output.
    envelope: Mapped[dict[str, object]] = mapped_column(JsonB, nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 1 on a clean first pass; 2 after a schema-repair retry; 3 once the
    # retry also failed and the escalation model was used (§8 step 6).
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # P3-T6's own acceptance line: "demotions are counted as a metric."
    verified_field_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    demoted_field_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ExtractedField(UUIDPrimaryKey, TimestampMixin, Base):
    __tablename__ = "extracted_fields"
    __table_args__ = (
        Index("ix_extracted_fields_org_extraction", "organization_id", "extraction_id"),
        Index("ix_extracted_fields_extraction_path", "extraction_id", "field_path"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("extractions.id", ondelete="CASCADE"), nullable=False
    )
    # The dotted path the rule DSL uses, e.g. "quantity.net_quantity".
    field_path: Mapped[str] = mapped_column(String(100), nullable=False)
    value_raw: Mapped[str | None] = mapped_column(String(4000), default=None)
    not_found_reason: Mapped[str | None] = mapped_column(String(500), default=None)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # `ocr_tokens.id`s the model cited for this value (see the module
    # docstring): the raw material P3-T6 turns into verified evidence spans.
    cited_token_ids: Mapped[list[str]] = mapped_column(JsonB, nullable=False, default=list)
    # Filled by the `normalizing` stage (P3-T7), not by the model.
    value_norm: Mapped[dict[str, object] | None] = mapped_column(JsonB, default=None)
    unit: Mapped[str | None] = mapped_column(String(20), default=None)
    # Filled by the P3-T6 evidence gate, before this row's extraction ever
    # returns - `None` means "not yet verified" (a field with no value at
    # all, which verification skips entirely), not "verification pending."
    verified: Mapped[bool | None] = mapped_column(Boolean, default=None)
    match_ratio: Mapped[float | None] = mapped_column(Float, default=None)


class EvidenceSpan(UUIDPrimaryKey, TimestampMixin, Base):
    """Where a verified value actually came from (P3-T6) - IMPLEMENTATION.md
    §11's evidence chain, `extracted_field → evidence_span → file_page →
    bbox → original file`, materialized as a real, immutable row. Only
    created for a field that *passed* verification - an unverified field
    already demotes its `LabelFacts` entry to an explicit absence, so there
    is nothing for a rule (or a reviewer) to trace evidence for.

    Append-only (migration 0011), reusing the same `labellens_reject_
    mutation()` trigger function `audit_logs`/`rulesets`/`rules`/
    `analysis_events` already use - §11: "Evidence is immutable". Content-
    hashing (also mentioned in §11, for embedding a stable crop in a
    generated report) is deferred to whichever of P5-T4/P7-T1 actually
    needs it; nothing here blocks adding it later.
    """

    __tablename__ = "evidence_spans"
    __table_args__ = (
        Index("ix_evidence_spans_org_field", "organization_id", "extracted_field_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    extracted_field_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("extracted_fields.id", ondelete="CASCADE"), nullable=False
    )
    file_page_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("file_pages.id", ondelete="CASCADE"), nullable=False
    )
    # The `ocr_tokens.id`s this span was built from - kept alongside the
    # bbox/snippet below so the exact tokens remain traceable, not just
    # their aggregate region.
    token_ids: Mapped[list[str]] = mapped_column(JsonB, nullable=False, default=list)
    # The union bounding box of every cited token, in the page's *original*
    # image coordinates (matching `ocr_tokens.x1`/etc.'s own convention).
    x1: Mapped[float] = mapped_column(Float, nullable=False)
    y1: Mapped[float] = mapped_column(Float, nullable=False)
    x2: Mapped[float] = mapped_column(Float, nullable=False)
    y2: Mapped[float] = mapped_column(Float, nullable=False)
    # The literal OCR text this span covers - survives even if the image is
    # later purged under retention policy (§11).
    text_snippet: Mapped[str] = mapped_column(String(2000), nullable=False)
    # "ocr" today; "vision"/"derived" are reserved for when a multimodal
    # extraction path or a computed (unit-converted) field exists.
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="ocr")
