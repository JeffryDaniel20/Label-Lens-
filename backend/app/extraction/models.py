"""Persisted extraction output (P3-T5).

`Extraction` is one LLM pass over one analysis's OCR text; `ExtractedField`
is one row per dotted field path - the join point evidence hangs off, keyed
by the same path `rules/*.yaml` already references (`ingredients.items`), so
a rule, a fact, and its evidence all address the same field the same way.

`cited_token_ids` holds the OCR tokens the model said each value came from.
P3-T6 (the evidence verification gate) is what turns those citations into
`evidence_spans` with bboxes and snippets, and what enforces the
substring-match check that demotes an uncited or unmatchable value to
`unverified`. P3-T5's job stops at recording *what was cited*.

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
